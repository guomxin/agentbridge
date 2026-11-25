"""Regression tests for NanoBot Host MCP session pooling, Telegram keep-alive, and sync optimizations."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import http.client
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

_NANOBOT_ROOT = str(Path(__file__).resolve().parents[1] / "integrations" / "nanobot-agentbridge")
if _NANOBOT_ROOT not in sys.path:
    sys.path.insert(0, _NANOBOT_ROOT)

from agentbridge.workspace.gateway import GatewayRequestError
from nanobot_agentbridge.notifications import TerminalMessages, notifications
from nanobot_agentbridge.session_pool import McpSessionPool
from nanobot_agentbridge.telegram import TelegramAPI, TelegramPilot, TelegramUnavailable


def _write_private(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def _req(
    token_file: Path,
    *,
    subject: str = "alice",
    endpoint_key: str = "telegram:8001:501",
    instance: str = "inst-1",
    mcp_url: str = "https://central.example/mcp",
    coordinator: str | None = "/usr/local/bin/coordinator",
    card_origins: list[str] | None = None,
) -> dict:
    return {
        "subject": subject,
        "endpointKey": endpoint_key,
        "instance": instance,
        "mcpUrl": mcp_url,
        "tokenFile": str(token_file),
        "caFile": None,
        "coordinatorProgram": coordinator,
        "trustedCardOrigins": card_origins if card_origins is not None else ["https://central.example"],
    }


def test_mcp_session_pool_isolation_reuse_owner_task_rotation_error_ttl_and_lru(tmp_path):
    async def run():
        now = [100.0]
        opened: list[dict] = []
        closed: list[int] = []
        enter_exit_tasks: list[tuple[asyncio.Task, asyncio.Task]] = []

        @asynccontextmanager
        async def fake_factory(request: dict):
            idx = len(opened) + 1
            enter_task = asyncio.current_task()
            opened.append({"idx": idx, "subject": request["subject"], "endpointKey": request["endpointKey"]})
            request["controlledPrepares"] = {"oa_prepare": {"commitCapability": f"cap-{idx}"}}
            try:
                yield SimpleNamespace(id=idx), {"subject": request["subject"], "idx": idx}, "L3"
            finally:
                exit_task = asyncio.current_task()
                assert enter_task is exit_task
                enter_exit_tasks.append((enter_task, exit_task))
                closed.append(idx)

        pool = McpSessionPool(
            idle_ttl_seconds=10.0,
            max_age_seconds=25.0,
            max_entries=2,
            session_factory=fake_factory,
            clock=lambda: now[0],
        )
        tok_a = _write_private(tmp_path / "alice.tok", "token-a-1")
        tok_b = _write_private(tmp_path / "bob.tok", "token-b-1")
        req_a1 = _req(tok_a, subject="alice", endpoint_key="telegram:8001:501")
        req_a2 = _req(tok_a, subject="alice", endpoint_key="workspace:inst-1:alice")
        req_b = _req(tok_b, subject="bob", endpoint_key="telegram:8001:502")

        # 1. Reuse across two distinct caller tasks on the same slot; owner task stays isolated.
        async def use_once(req: dict, lane: str = "control"):
            caller_task = asyncio.current_task()
            async with pool.session(req, lane=lane) as (session, meta, level):
                return session.id, meta, level, req["controlledPrepares"], caller_task

        s1, meta1, lvl1, prep1, task1 = await asyncio.create_task(use_once(req_a1))
        now[0] += 3.0
        s2, meta2, lvl2, prep2, task2 = await asyncio.create_task(use_once(req_a1))
        assert task1 is not task2
        assert s1 == s2 == 1
        assert lvl1 == lvl2 == "L3"
        assert prep1 == prep2 == {"oa_prepare": {"commitCapability": "cap-1"}}
        assert len(opened) == 1

        # 2. Strict lane isolation (notifications vs control).
        s_notif, _, _, _, _ = await use_once(req_a1, lane="notifications")
        assert s_notif == 2
        assert len(opened) == 2

        # 3. LRU eviction when max_entries=2 and a third distinct subject acquires a slot.
        now[0] += 1.0
        s_bob, _, _, _, _ = await use_once(req_b, lane="control")
        assert s_bob == 3
        assert 1 in closed  # oldest unlocked idle slot evicted

        # 4. Token rotation immediately closes and reopens the slot.
        _write_private(tok_b, "token-b-rotated")
        now[0] += 1.0
        s_bob2, _, _, _, _ = await use_once(req_b, lane="control")
        assert s_bob2 == 4
        assert 3 in closed

        # 5. Error inside consumer immediately evicts and closes the session.
        with pytest.raises(RuntimeError, match="boom"):
            async with pool.session(req_b, lane="control"):
                raise RuntimeError("boom")
        assert 4 in closed
        s_bob3, _, _, _, _ = await use_once(req_b, lane="control")
        assert s_bob3 == 5

        # 6. Idle TTL expiry forces a fresh session.
        now[0] += 11.0
        s_bob4, _, _, _, _ = await use_once(req_b, lane="control")
        assert s_bob4 == 6
        assert 5 in closed

        # 7. Max-age expiry forces rotation even if actively kept warm.
        for _ in range(3):
            now[0] += 9.0
            await use_once(req_b, lane="control")
        assert 6 in closed

        # 8. Cross-endpoint isolation for the same user.
        s_ws, _, _, _, _ = await use_once(req_a2, lane="control")
        assert s_ws != s_bob4

        await pool.close()
        assert len(closed) == len(opened)
        assert all(enter is exit_ for enter, exit_ in enter_exit_tasks)

    asyncio.run(run())


def test_stalled_handshake_and_interrupted_read_release_slot_without_replay(tmp_path):
    async def run():
        request = _req(_write_private(tmp_path / "token", "example"))
        attempts, calls, closed = [], [], []
        blocked = asyncio.Event()

        @asynccontextmanager
        async def factory(_request):
            number = len(attempts) + 1
            attempts.append(number)

            async def read():
                calls.append(number)
                if number == 2:
                    await blocked.wait()
                return "fresh"

            try:
                if number == 1:
                    await blocked.wait()
                yield SimpleNamespace(read=read), {}, "L3"
            finally:
                closed.append(number)

        pool = McpSessionPool(session_factory=factory, open_timeout_seconds=0.03)
        try:
            with pytest.raises(TimeoutError):
                async with pool.session(request):
                    pytest.fail("Stalled handshake should never reach the caller")
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.03), pool.session(request) as (session, _, _):
                    await session.read()
            async with pool.session(request) as (session, _, _):
                assert await session.read() == "fresh"
            assert calls == [2, 3]
            assert closed == [1, 2]
        finally:
            await pool.close()

    asyncio.run(asyncio.wait_for(run(), timeout=2))


def test_telegram_api_keepalive_reconnects_idempotent_only_and_never_retries_send_message(monkeypatch):
    created: list[dict] = []
    clock = [100.0]

    class FakeResponse:
        def __init__(self, status: int, body: dict, will_close: bool = False):
            self.status = status
            self._raw = json.dumps(body).encode("utf-8")
            self.will_close = will_close

        def read(self, amt=None):
            return self._raw if amt is None else self._raw[:amt]

    class FakeConnection:
        def __init__(self, host, timeout=0, context=None):
            self.host = host
            self.timeout = timeout
            self.closed = False
            self.requests = []
            self.index = len(created) + 1
            created.append({"conn": self})

        def request(self, method, path, body=None, headers=None):
            self.requests.append((method, path, body, headers))

        def getresponse(self):
            _, path, _, _ = self.requests[-1]
            action = path.rsplit("/", 1)[-1]
            behavior = behaviors.pop(0)
            if isinstance(behavior, Exception):
                raise behavior
            return FakeResponse(200, {"ok": True, "result": behavior}, will_close=False)

        def close(self):
            self.closed = True

    behaviors: list = [
        [],  # 1. initial getUpdates -> opens conn 1
        {"message_id": 10},  # 2. sendMessage reuses conn 1
        http.client.RemoteDisconnected("stale"),  # 3. getUpdates disconnects on conn 1 -> safe retry on conn 2
        [{"update_id": 1}],  # 4. retried getUpdates succeeds on conn 2
        http.client.RemoteDisconnected("lost"),  # 5. sendMessage disconnects on conn 2 -> MUST NOT retry!
        {"message_id": 11},  # 6. fresh conn 3 opened before sendMessage after idle >= 40s
        {"message_id": 12},  # 7. after 45s idle, conn 4 proactively opened before sendMessage
    ]

    monkeypatch.setattr("nanobot_agentbridge.telegram.http.client.HTTPSConnection", FakeConnection)
    monkeypatch.setattr("nanobot_agentbridge.telegram.time.monotonic", lambda: clock[0])

    api = TelegramAPI("bot-token")
    try:
        # Sequential calls reuse connection 1.
        assert api.call("getUpdates", offset=0, timeout=0) == []
        assert api.call("sendMessage", chat_id=501, text="hi") == {"message_id": 10}
        assert len(created) == 1

        # Safe idempotent method retries once on RemoteDisconnected -> connection 2.
        assert api.call("getUpdates", offset=0, timeout=0) == [{"update_id": 1}]
        assert len(created) == 2
        assert created[0]["conn"].closed is True

        # Non-idempotent sendMessage MUST NEVER retry on RemoteDisconnected!
        with pytest.raises(TelegramUnavailable):
            api.call("sendMessage", chat_id=501, text="must-not-duplicate")
        assert len(created) == 2
        assert created[1]["conn"].closed is True

        # Next call opens connection 3 cleanly.
        assert api.call("sendMessage", chat_id=501, text="fresh") == {"message_id": 11}
        assert len(created) == 3

        # Idle >= 40s proactively closes conn 3 and opens conn 4 before sendMessage.
        clock[0] += 45.0
        assert api.call("sendMessage", chat_id=501, text="after-idle") == {"message_id": 12}
        assert len(created) == 4
        assert created[2]["conn"].closed is True
    finally:
        api.close()
        assert created[3]["conn"].closed is True


def test_paired_binding_cache_verifies_live_evicts_on_revocation_and_recovers_on_host_restart(tmp_path):
    binding_v1 = {
        "user_subject": "alice",
        "endpoint_id": "ep-1",
        "endpoint_key": "telegram:8001:501",
        "session_key": "agentbridge:telegram:ep-1",
        "credential_ref": "cred-1",
    }
    binding_v2 = {
        "user_subject": "alice",
        "endpoint_id": "ep-2",
        "endpoint_key": "telegram:8001:501",
        "session_key": "agentbridge:telegram:ep-2",
        "credential_ref": "cred-2",
    }

    class FakeGateway:
        def __init__(self):
            self.active: dict | None = binding_v1
            self.resolve_calls = 0
            self.verify_calls = 0
            self.restart_once = False

        def resolve_telegram_sender(self, bot_id, chat_id):
            self.resolve_calls += 1
            return dict(self.active) if self.active else None

        def verify_telegram_binding(self, binding):
            self.verify_calls += 1
            if self.restart_once:
                self.restart_once = False
                raise GatewayRequestError("NANOBOT_BINDING_UNAVAILABLE", "host restarted")
            return self.active is not None and binding == self.active

    gw = FakeGateway()
    pilot = TelegramPilot(
        tmp_path / "messages.sqlite3",
        api=SimpleNamespace(call=lambda *a, **k: []),
        bot_id=8001,
        instance="inst-1",
        gateway=gw,
        bindings=lambda: {},
        allow_pairing=True,
    )
    try:
        # 1. Cold resolve populates cache and verifies live.
        b1, d1 = pilot._binding(501)
        assert b1 == binding_v1 and d1
        assert gw.resolve_calls == 1 and gw.verify_calls == 1

        # 2. Warm call skips resolve_telegram_sender, still calls verify_telegram_binding live.
        b2, d2 = pilot._binding(501)
        assert b2 == binding_v1 and d2 == d1
        assert gw.resolve_calls == 1 and gw.verify_calls == 2

        # 3. Host restart (NANOBOT_BINDING_UNAVAILABLE) transparently clears cache and re-resolves once.
        gw.restart_once = True
        b3, d3 = pilot._binding(501)
        assert b3 == binding_v1 and d3 == d1
        assert gw.resolve_calls == 2 and gw.verify_calls == 4

        # 4. Binding rotation replaces cached binding and produces a distinct digest.
        gw.active = binding_v2
        b4, d4 = pilot._binding(501)
        assert b4 == binding_v2 and d4 != d1

        # 5. Live revocation immediately blocks delivery and clears cache.
        gw.active = None
        b5, d5 = pilot._binding(501)
        assert b5 is None and d5 is None
        assert 501 not in pilot._paired_bindings
    finally:
        pilot.db.close()


def test_deferred_terminal_flushes_on_reconcile_without_burning_attempts_and_early_result_view_dedupes(tmp_path):
    binding = {
        "user_subject": "alice",
        "endpoint_id": "ep-1",
        "endpoint_key": "telegram:8001:501",
        "session_key": "agentbridge:telegram:ep-1",
        "credential_ref": "cred-1",
    }

    class FakeGateway:
        def __init__(self):
            self.run_state = "accepted"
            self.deliveries: list[dict] = []
            self.receipts: list[dict] = []
            self.result_view_calls = 0

        def resolve_telegram_sender(self, bot_id, chat_id):
            return dict(binding)

        def verify_telegram_binding(self, b):
            return b == binding

        def dispatch_evidence(self, _req, _binding, run_id):
            return {
                "prompt_observed": True,
                "observed_run_id": run_id,
                "state": self.run_state,
                "final_text": "已查询 2 项 OA 待办。" if self.run_state == "completed" else None,
                "has_private_card": False,
                "has_result_view": True,
            }

        def result_view(self, _req, _binding, _run_id):
            self.result_view_calls += 1
            return {
                "available": True,
                "count": 2,
                "complete": True,
                "resultView": {
                    "kind": "result_view",
                    "sourceTaskId": "task-rv-1",
                    "url": "https://central.example/views/rv-1",
                    "openUrl": "https://central.example/views/rv-1",
                    "selectionAvailable": True,
                    "expiresAt": "2099-01-01T00:00:00Z",
                    "sourceExpiresAt": "2099-01-01T00:00:00Z",
                    "title": "OA 待办",
                },
            }

        def publish_telegram_timeline(self, binding, message, role, text, run_id, receipts):
            return None

        def _request(self, action, body, timeout=8):
            if action == "telegram-notifications":
                items = list(self.deliveries)
                self.deliveries.clear()
                return {"deliveries": items}
            if action == "telegram-notification-receipt":
                self.receipts.append(body)
                return {"recorded": True}
            if action == "telegram-timeline":
                return {"published": True}
            if action == "telegram-mirror-peers":
                return {"senderIds": ["501"]}
            raise AssertionError(f"Unexpected action {action}")

    class FakeApi:
        def __init__(self):
            self.sent: list[dict] = []

        def call(self, method, **params):
            if method == "sendChatAction":
                return True
            assert method == "sendMessage"
            self.sent.append(params)
            reply = {"message_id": 100 + len(self.sent), "chat": {"id": params["chat_id"], "type": "private"}, "from": {"id": 8001}}
            if "reply_parameters" in params:
                reply["reply_to_message"] = {"message_id": params["reply_parameters"]["message_id"]}
            return reply

    gw = FakeGateway()
    api = FakeApi()
    pilot = TelegramPilot(
        tmp_path / "messages.sqlite3",
        api=api,
        bot_id=8001,
        instance="inst-1",
        gateway=gw,
        bindings=lambda: {},
        allow_pairing=True,
    )
    try:
        resolved, digest = pilot._binding(501)
        assert resolved == binding
        with pilot.db:
            pilot.db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,'working','run-1',NULL)",
                (8001, 501, 42, "alice", digest),
            )

        # 1. While run is accepted/working, reconcile delivers Result View EARLY (before LLM reply completes).
        pilot.reconcile()
        assert gw.result_view_calls == 1
        assert len(api.sent) == 1
        assert "2 项 OA 待办" in api.sent[0]["text"]

        # 2. Terminal notification arrives while message is still 'working':
        #    MUST defer in memory without sending or acking succeeded=False!
        gw.deliveries.append({
            "deliveryId": "deliv-term-1",
            "attempt": 1,
            "runId": "run-1",
            "status": "succeeded",
        })
        pilot.terminal.poll(501, resolved, digest)
        assert len(api.sent) == 1
        assert gw.receipts == []
        assert (501, "deliv-term-1") in pilot.terminal._deferred_terminal

        # 3. Run completes: reconcile sends final reply, skips duplicate Result View,
        #    and IMMEDIATELY flushes the deferred terminal event with attempt=1!
        gw.run_state = "completed"
        pilot.reconcile()
        assert len(api.sent) == 3  # [result_view, assistant_reply, terminal_succeeded]
        assert "已查询 2 项 OA 待办" in api.sent[1]["text"]
        assert api.sent[2]["text"] == TerminalMessages.TEXT["succeeded"]
        assert gw.receipts == [{
            "binding": resolved,
            "deliveryId": "deliv-term-1",
            "runId": "run-1",
            "attempt": 1,
            "delivered": True,
        }]
        assert pilot.terminal._deferred_terminal == {}
    finally:
        pilot.db.close()


def test_notifications_passes_clamped_wait_seconds_only_when_positive(tmp_path, monkeypatch):
    claim_calls: list[dict] = []

    class FakeSession:
        async def call_tool(self, name, args, meta=None):
            assert name == "agentbridge_host_notification_claim"
            claim_calls.append(dict(args))
            return SimpleNamespace(
                isError=False,
                structuredContent={"endpoint": {"endpointId": "ep-1"}, "notifications": []},
            )

    @asynccontextmanager
    async def fake_auth(_req):
        yield FakeSession(), {"io.agentbridge/host-context": {}}, "L3"

    async def fake_verify(*_args, **_kwargs):
        return True

    fake_web = SimpleNamespace(
        HTTPForbidden=type("HTTPForbidden", (Exception,), {}),
        HTTPBadRequest=type("HTTPBadRequest", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(web=fake_web))
    monkeypatch.setitem(sys.modules, "nanobot_agentbridge.worker", SimpleNamespace(authenticated_session=fake_auth))
    monkeypatch.setattr("nanobot_agentbridge.telegram_identity.verify_telegram_binding", fake_verify)

    host = SimpleNamespace(
        coordinator="/fake/coord",
        card_origins=["https://central.example"],
        worker_payload=lambda _rec, _rid: {
            "userDir": str(tmp_path),
            "instance": "inst-1",
            "subject": "alice",
            "endpoint": "ep-1",
            "endpointKey": "telegram:8001:501",
            "conversation": "agentbridge:telegram:ep-1",
            "workspaceEndpointId": "ws-1",
        },
    )
    record = {"workspaceEndpointId": "ws-1"}

    asyncio.run(notifications(host, record, "telegram-notifications", {}))
    asyncio.run(notifications(host, record, "telegram-notifications", {"waitSeconds": 0}))
    asyncio.run(notifications(host, record, "telegram-notifications", {"waitSeconds": 2}))
    asyncio.run(notifications(host, record, "telegram-notifications", {"waitSeconds": 60}))

    assert "wait_seconds" not in claim_calls[0]
    assert "wait_seconds" not in claim_calls[1]
    assert claim_calls[2]["wait_seconds"] == 2.0
    assert claim_calls[3]["wait_seconds"] == 15.0
