"""MCP admission budgets; never time out or replay an admitted business operation."""
from __future__ import annotations

import asyncio
from mcp.server.auth.middleware.auth_context import get_access_token
from starlette.responses import JSONResponse
from uvicorn.protocols.http.h11_impl import H11Protocol


class BoundedMcpProtocol(H11Protocol):
    """Bound incomplete HTTP messages, including keep-alive/pipelined requests.

    Uses the pinned Uvicorn H11 implementation. TLS handshakes precede this
    protocol and retain asyncio's separate handshake timeout.
    """

    header_seconds = 10.0
    body_seconds = 60.0
    write_stall_seconds = 30.0
    max_connections = 128
    max_peer_connections = 64

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._input_timer = None
        self._write_timer = None
        self._input_phase = None
        self._admitted = False

    def connection_made(self, transport):
        super().connection_made(transport)
        peer = self.client[0] if self.client else None
        count = sum(1 for connection in self.connections
                    if connection.client and connection.client[0] == peer)
        if len(self.connections) > self.max_connections or count > self.max_peer_connections:
            transport.close()
            return
        self._admitted = True
        self._track_input()

    def data_received(self, data):
        if self._admitted:
            super().data_received(data)

    def _track_input(self):
        # A completed response can precede the end of an ignored request body.
        cycle = self.cycle
        if cycle is None or (cycle.response_complete and not cycle.more_body):
            phase = ("header", cycle)
            seconds = self.header_seconds
        elif cycle.more_body:
            phase = ("body", cycle)
            seconds = self.body_seconds
        else:
            phase = None
            seconds = 0
        if phase == self._input_phase:
            return
        if self._input_timer:
            self._input_timer.cancel()
        self._input_phase = phase
        self._input_timer = self.loop.call_later(seconds, self.transport.abort) if phase else None

    def handle_events(self):
        super().handle_events()
        self._track_input()

    def on_response_complete(self):
        super().on_response_complete()
        self._track_input()

    def pause_writing(self):
        super().pause_writing()
        if self._write_timer is None:
            self._write_timer = self.loop.call_later(self.write_stall_seconds, self.transport.abort)

    def resume_writing(self):
        super().resume_writing()
        if self._write_timer:
            self._write_timer.cancel()
            self._write_timer = None

    def connection_lost(self, exc):
        for timer in (self._input_timer, self._write_timer):
            if timer:
                timer.cancel()
        super().connection_lost(exc)


class McpIdentityBudget:
    """Place after MCP authentication/context middleware, before routing.

    Shares the allowance across tokens for one verified subject. No waiting
    queue and no retry of a business call. Slots remain held until the actual
    handler exits, even if the client disconnects.
    """

    def __init__(self, app, *, identity_store, per_subject=8, total=96,
                 max_body_bytes=18 * 1024 * 1024):
        if min(per_subject, total, max_body_bytes) <= 0:
            raise ValueError("MCP request budgets must be positive")
        self.app = app
        self.identity_store = identity_store
        self.per_subject = per_subject
        self.total = total
        self.max_body_bytes = max_body_bytes
        self.active = 0
        self.subjects = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/mcp":
            return await self.app(scope, receive, send)
        token = get_access_token()
        if token is None:
            return await self.app(scope, receive, send)  # Existing MCP auth returns 401.
        try:
            identity = await asyncio.to_thread(self.identity_store.resolve_client, token.client_id)
        except (PermissionError, KeyError):
            return await JSONResponse({"error": "invalid_token"}, status_code=401)(scope, receive, send)
        subject = identity["user_subject"]
        if self.subjects.get(subject, 0) >= self.per_subject or self.active >= self.total:
            return await JSONResponse(
                {"error": "MCP_CAPACITY_EXCEEDED", "message": "Too many active requests"},
                status_code=429, headers={"Retry-After": "1"},
            )(scope, receive, send)
        # No await between checking and admission: one ASGI event loop owns these counters.
        self.active += 1
        self.subjects[subject] = self.subjects.get(subject, 0) + 1
        try:
            if scope.get('method') == 'POST':
                # Read the bounded message before MCP can start a business tool.
                # Applies to chunked bodies as well as Content-Length requests.
                lengths = [value for name, value in scope.get('headers', []) if name == b'content-length']
                if lengths and int(lengths[0]) > self.max_body_bytes:
                    return await JSONResponse({'error': 'MCP_BODY_TOO_LARGE'}, status_code=413,
                        headers={'Connection': 'close'})(scope, receive, send)
                body = bytearray()
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    chunk = message.get('body', b'')
                    if len(body) + len(chunk) > self.max_body_bytes:
                        return await JSONResponse({'error': 'MCP_BODY_TOO_LARGE'}, status_code=413,
                            headers={'Connection': 'close'})(scope, receive, send)
                    body.extend(chunk)
                    if not message.get('more_body', False):
                        break
                upstream_receive = receive
                delivered = False
                async def receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        payload = bytes(body)
                        body.clear()
                        return {'type': 'http.request', 'body': payload, 'more_body': False}
                    return await upstream_receive()
            await self.app(scope, receive, send)
        finally:
            self.active -= 1
            count = self.subjects[subject] - 1
            if count:
                self.subjects[subject] = count
            else:
                del self.subjects[subject]


def bounded_mcp_app(mcp, identity_store):
    from starlette.middleware import Middleware

    app = mcp.streamable_http_app()
    app.user_middleware.append(Middleware(McpIdentityBudget, identity_store=identity_store))
    return app
