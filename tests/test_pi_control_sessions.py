"""Count MCP protocol requests across Pi Telegram control leases using a real SDK peer."""

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import sys
from typing import Any

import anyio
import pytest
from mcp import ClientSession
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.memory import create_client_server_memory_streams

INTEGRATIONS = Path(__file__).resolve().parents[1] / "integrations"
for package in ("pi-agentbridge", "nanobot-agentbridge"):
    sys.path.insert(0, str(INTEGRATIONS / package))

from pi_agentbridge import central


@pytest.fixture
def channel_grant():
    return {"active": True, "generation": "a" * 64, "endpointId": str(uuid4())}


@pytest.fixture
def control(tmp_path, monkeypatch, channel_grant):
    counts = Counter()
    owners = {"A" * 32: "first", "B" * 32: "second", "C" * 32: "first"}
    spec = {"instanceId": "pi-test", "mcpUrl": "https://test.invalid/mcp"}

    class CountedSession(ClientSession):
        async def list_tools(self, *args, **kwargs):
            counts["tools/list"] += 1
            return await super().list_tools(*args, **kwargs)

        async def initialize(self):
            counts["initialize"] += 1
            return await super().initialize()

    @asynccontextmanager
    async def transport(url, headers, **kwargs):
        token = headers["Authorization"].removeprefix("Bearer ")
        api = FastMCP("isolated-pi")

        @api.tool()
        def agentbridge_host_register() -> dict[str, Any]:
            counts["register"] += 1
            return {"registered": True}

        @api.tool()
        def agentbridge_host_identity_profile(agent_host: str, ctx: Context) -> dict[str, Any]:
            assert agent_host == "pi"
            assert "_pi_local_token_hash" not in ctx.request_context.meta.model_dump()
            counts["identity"] += 1
            return {"identity": {"userSubject": owners.get(token)}, "host": {
                "hostInstanceId": "pi-test", "hostVersion": central.VERSION, "acceptedLevel": spec.get("requiredLevel", "L1")},
                "agentToolAccess": {"allowedToolNames": []}}

        @api.tool()
        def agentbridge_host_telegram_endpoint(bot_id: str, sender_id: str) -> dict[str, Any]:
            from agentbridge.workspace.telegram_pilot_links import telegram_conversation_ref

            counts["endpoint"] += 1
            if not channel_grant["active"] or owners.get(token) != "first" or (bot_id, sender_id) != ("8801", "501"):
                raise PermissionError("No current pairing")
            return {"endpointId": channel_grant["endpointId"], "endpointKey": "telegram:8801:501",
                    "workspaceEndpointId": "test-id", "workspaceEndpointKey": "workspace:pi-test:first",
                    "conversationRef": telegram_conversation_ref("pi-test", "first", "test-id", bot_id, sender_id),
                    "pairingRef": channel_grant["generation"]}

        server = api._mcp_server
        async with create_client_server_memory_streams() as (client, peer), anyio.create_task_group() as tasks:
            tasks.start_soon(server.run, *peer, server.create_initialization_options())
            try:
                yield *client, None
            finally:
                tasks.cancel_scope.cancel()

    def binding(subject, char):
        path = tmp_path / subject
        path.write_text(char * 32)
        path.chmod(0o600)
        return {"user_subject": subject, "endpoint_key": "tg-test", "endpoint_id": "test-id",
                "session_key": "conversation", "credential_ref": "pair-1", "tokenFile": str(path)}

    monkeypatch.setattr(central, "streamablehttp_client", transport)
    monkeypatch.setattr(central, "ClientSession", CountedSession)
    def read(path):
        return Path(path).read_text()

    return central.ControlSessions(spec, read), counts, owners, binding


def test_cold_and_warm_directory_and_live_revocation(control):
    sessions, counts, owners, binding = control
    first = binding("first", "A")

    async def check():
        try:
            for expected in (1, 2):
                async with sessions.borrow(first) as tools:
                    assert tools.subject == "first" and tools.visible_tools() == []
                assert counts == {"initialize": 1, "register": 1, "tools/list": 1, "identity": expected}
            owners.clear()
            with pytest.raises(PermissionError):
                async with sessions.borrow(first):
                    pytest.fail("revoked identity was accepted on a warm session")
            assert counts["identity"] == 3 and counts["initialize"] == 1
        finally:
            await sessions.close()

    asyncio.run(check())


def test_rotation_subject_pairing_generation_and_long_poll_are_isolated(control):
    sessions, counts, _, binding = control
    first = binding("first", "A")
    second = binding("second", "B")

    async def connection(record, lane="control"):
        async with sessions.borrow(record, lane=lane) as tools:
            return tools.session

    async def check():
        try:
            original = await connection(first)
            assert await connection(first) is original
            assert await connection(second) is not original
            assert await connection({**first, "credential_ref": "pair-2"}) is not original
            async with sessions.borrow(first, lane="notifications") as poll:
                assert poll.session is not original
                assert await asyncio.wait_for(connection(first), timeout=3) is original
            Path(first["tokenFile"]).write_text("C" * 32)
            assert await connection(first) is not original
            assert counts == {"initialize": 5, "register": 5, "tools/list": 5, "identity": 7}
        finally:
            await sessions.close()

    asyncio.run(check())


def test_each_pi_delivery_rechecks_current_subject_and_exact_pairing_generation(control, channel_grant):
    from starlette.exceptions import HTTPException
    from pi_agentbridge.channel_delivery import Delivery
    from pi_agentbridge.server import binding_key
    from pi_agentbridge.telegram_routes import PiTelegramRoutes

    sessions, counts, owners, binding = control
    web = {**binding("first", "A"), "endpoint_key": "workspace:pi-test:first"}
    sessions.spec.update(requiredLevel="L3", coordinatorProgram="/unused-in-this-test",
                         trustedCardOrigins=["https://isolated.invalid"],
                         telegramPilot={"botId": "8801", "allowedSenders": {"first": "501"}})
    host = SimpleNamespace(spec=sessions.spec, instance="pi-test", bindings={binding_key(web): web},
                           control_sessions=sessions, coordinator=True, commands=None, cards=SimpleNamespace(origins=[]))
    host.telegram = PiTelegramRoutes(host)

    async def run():
        try:
            route = await host.telegram.action("telegram-route", {"botId": "8801", "senderId": "501"})
            key = binding_key(route["binding"])
            assert await host.telegram.delivery_hint(("second", *key[1:])) is None
            original = await host.telegram.delivery_hint(key)
            delivery = Delivery(host, original, "telegram-chat-validate")
            request = {"endpointKey": original["endpoint_key"]}
            for expected in (2, 3):
                async with delivery.paired_notification_session(request) as (_, _, level):
                    assert level == "L3"
                assert counts["identity"] == counts["endpoint"] == expected
            assert counts["initialize"] == 2
            for state in ({"active": False}, {"active": True, "generation": "b" * 64}):
                channel_grant.update(state)
                with pytest.raises(HTTPException) as exc:
                    async with delivery.paired_notification_session(request):
                        pytest.fail("Old pairing was accepted")
                assert exc.value.status_code == 403
            owners.clear()
            assert await host.telegram.delivery_hint(("other", *key[1:])) is None
            with pytest.raises(PermissionError):
                async with delivery.paired_notification_session(request):
                    pytest.fail("Revoked token was accepted")
        finally:
            await sessions.close()

    asyncio.run(run())
