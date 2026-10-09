import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import httpx
import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import StoredIdentityTokenVerifier
from agentbridge.mcp.resource_budget import BoundedMcpProtocol, McpIdentityBudget, bounded_mcp_app


@asynccontextmanager
async def serve(app, protocol=BoundedMcpProtocol):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
        http=protocol, ws="none", log_level="critical", timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        if not server.started:
            raise RuntimeError("isolated server did not start")
        port = server.servers[0].sockets[0].getsockname()[1]
        yield port
    finally:
        server.should_exit = True
        await task


class FastBudgets(BoundedMcpProtocol):
    header_seconds = .8
    body_seconds = 1.0
    max_connections = 4
    max_peer_connections = 3


class McpResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_body_never_starts_business_and_releases_slot(self):
        calls = []
        async def work(scope, receive, send):
            calls.append(await receive())
        store = SimpleNamespace(resolve_client=lambda token: {'user_subject': token})
        budget = McpIdentityBudget(work, identity_store=store, max_body_bytes=5)
        async def run(chunks, headers):
            messages = []
            async def receive():
                chunk = chunks.pop(0)
                return {'type': 'http.request', 'body': chunk, 'more_body': bool(chunks)}
            async def send(message):
                messages.append(message)
            with patch('agentbridge.mcp.resource_budget.get_access_token', return_value=SimpleNamespace(client_id='alice')):
                await budget({'type': 'http', 'path': '/mcp', 'method': 'POST', 'headers': headers}, receive, send)
            self.assertEqual((budget.active, budget.subjects), (0, {}))
            return messages
        self.assertEqual((await run([], [(b'content-length', b'6')]))[0]['status'], 413)
        self.assertEqual((await run([b'123', b'456'], []))[0]['status'], 413)
        self.assertEqual(calls, [])
        await run([b'12', b'34'], [])
        self.assertEqual(calls[0]['body'], b'1234')

    async def test_identity_database_wait_does_not_block_event_loop(self):
        release = threading.Event()
        released_by_loop = []
        class SlowStore:
            def verify(self, token):
                released_by_loop.append(release.wait(3))
                return None
        verifier = StoredIdentityTokenVerifier(SlowStore(), resource='http://localhost/mcp')
        asyncio.get_running_loop().call_later(.05, release.set)
        self.assertIsNone(await verifier.verify_token('synthetic'))
        self.assertEqual(released_by_loop, [True])

    async def test_global_budget_and_exception_cancellation_release_without_replay(self):
        entered = asyncio.Event()
        calls = 0
        async def work(scope, receive, send):
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.Event().wait()
        store = SimpleNamespace(resolve_client=lambda token: {'user_subject': token})
        budget = McpIdentityBudget(work, identity_store=store, total=1)
        messages = []
        async def send(message):
            messages.append(message)
        async def receive():
            return {'type': 'http.disconnect'}
        scope = {'type': 'http', 'path': '/mcp'}
        with patch('agentbridge.mcp.resource_budget.get_access_token', return_value=SimpleNamespace(client_id='alice')):
            pending = asyncio.create_task(budget(scope, receive, send))
            await entered.wait()
        with patch('agentbridge.mcp.resource_budget.get_access_token', return_value=SimpleNamespace(client_id='bob')):
            await budget(scope, receive, send)
        self.assertEqual(messages[0]['status'], 429)
        self.assertEqual(calls, 1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual((budget.active, budget.subjects), (0, {}))
        async def fail(scope, receive, send):
            raise RuntimeError('handler failure')
        budget.app = fail
        with patch('agentbridge.mcp.resource_budget.get_access_token', return_value=SimpleNamespace(client_id='bob')):
            with self.assertRaisesRegex(RuntimeError, 'handler failure'):
                await budget(scope, receive, send)
        self.assertEqual((budget.active, budget.subjects), (0, {}))

    async def test_slow_header_body_and_keepalive_release_connections(self):
        async def app(scope, receive, send):
            if scope['type'] != 'http':
                return
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                if not message.get('more_body'):
                    break
            await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-length', b'2')]})
            await send({'type': 'http.response.body', 'body': b'ok'})

        async with serve(app, FastBudgets) as port:
            for payload in (b'GET / HTTP/1.1\r\nX-Slow: ',
                            b'POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 999\r\n\r\nx'):
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.write(payload)
                await writer.drain()
                # Byte trickles do not extend the absolute input deadline.
                for _ in range(2):
                    await asyncio.sleep(.08)
                    writer.write(b'x')
                    await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.read(), 3), b'')
                writer.close()
                await writer.wait_closed()
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
            await writer.drain()
            await reader.readuntil(b'\r\n\r\n')
            self.assertEqual(await reader.readexactly(2), b'ok')
            writer.write(b'GET / HTTP/1.1\r\nX-Slow: ')
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 3), b'')
            writer.close()
            await writer.wait_closed()
            async with httpx.AsyncClient() as client:
                self.assertEqual((await client.get(f'http://127.0.0.1:{port}/')).status_code, 200)

    async def test_connection_admission_rejects_and_recovers(self):
        class AdmissionBudgets(FastBudgets):
            header_seconds = 10
        async with serve(lambda scope, receive, send: None, AdmissionBudgets) as port:
            sockets = [await asyncio.open_connection('127.0.0.1', port) for _ in range(3)]
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            self.assertEqual(await asyncio.wait_for(reader.read(), 3), b'')
            writer.close()
            await writer.wait_closed()
            for reader, writer in sockets:
                writer.close()
                await writer.wait_closed()
            await asyncio.sleep(.05)
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(reader.read(1), .05)
            writer.close()
            await writer.wait_closed()

    async def test_verified_subject_shared_across_tokens_and_other_user_progresses(self):
        with tempfile.TemporaryDirectory() as directory:
            store = McpIdentityTokenStore(Path(directory) / 'identities.sqlite3')
            tokens = [store.issue(user_subject=subject, expected_principal_ref=subject)['token']
                      for subject in ('alice', 'alice', 'bob')]
            mcp = FastMCP('budget-test', stateless_http=True, json_response=True,
                token_verifier=StoredIdentityTokenVerifier(store, resource='http://localhost/mcp'),
                auth=AuthSettings(issuer_url=AnyHttpUrl('http://localhost'), resource_server_url=AnyHttpUrl('http://localhost/mcp'), required_scopes=[]))
            entered = asyncio.Event()
            release = asyncio.Event()
            calls = []

            @mcp.tool()
            async def hold(wait: bool = False) -> str:
                calls.append(wait)
                if wait:
                    if len(calls) == 8:
                        entered.set()
                    await release.wait()
                return 'ok'

            async with serve(bounded_mcp_app(mcp, store)) as port:
                async with httpx.AsyncClient(timeout=30) as client:
                    async def call(token, wait=False):
                        return await client.post(f'http://127.0.0.1:{port}/mcp',
                            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json, text/event-stream'},
                            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                  'params': {'name': 'hold', 'arguments': {'wait': wait}}})
                    pending = [asyncio.create_task(call(tokens[i % 2], True)) for i in range(8)]
                    try:
                        await asyncio.wait_for(entered.wait(), 20)
                        rejected = await call(tokens[1])
                        self.assertEqual(rejected.status_code, 429)
                        self.assertEqual(rejected.headers['retry-after'], '1')
                        self.assertEqual((await call(tokens[2])).status_code, 200)
                        self.assertEqual(len(calls), 9)  # Rejected request never executes.
                        self.assertEqual((await call('invalid')).status_code, 401)
                    finally:
                        release.set()
                        results = await asyncio.gather(*pending)
                    self.assertTrue(all(result.status_code == 200 for result in results))
                    self.assertEqual((await call(tokens[0])).status_code, 200)
