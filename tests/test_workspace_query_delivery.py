"""Synthetic shared-host delivery: proof, one send permit, receipts and safe retry."""

import asyncio
from concurrent.futures import wait
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/nanobot-agentbridge"))
from agentbridge.workspace.gateway import GatewayRequestError
from nanobot_agentbridge.notifications import TerminalMessages, notifications
from nanobot_agentbridge.telegram import TelegramRejected, TelegramUnavailable

TASK, DELIVERY, ENDPOINT = (str(uuid4()) for _ in range(3))
PAIR, ORIGIN = "a" * 64, "https://fixture.invalid"


def deadline():
    return (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()


def view():
    return {"sourceTaskId": TASK, "selectionAvailable": True, "sourceExpiresAt": deadline(),
            "expiresAt": deadline(), "url": ORIGIN + "/results/" + "a" * 43}


class Peer:
    def __init__(self):
        self.event = {"kind": "workspace_query_v2", "deliveryId": DELIVERY, "attempt": 1,
                      "sourceTaskId": TASK, "pairingRef": PAIR}
        self.actions = []
        self.result = {"available": True, "resultView": view(), "publicText": "已查到 3 项 OA 待办。", "leaseExpiresAt": deadline()}
        self.begin = {"allowed": True}
        self.started, self.release = Event(), Event()
        self.release.set()

    def _request(self, route, body, *, timeout=8):
        if route == "telegram-notifications":
            return {"deliveries": [self.event]}
        assert route == "telegram-workspace-result-view"
        self.actions.append(dict(body))
        if body["action"] == "issue":
            assert timeout > 8
            self.started.set()
            assert self.release.wait(2)
            return self.result
        if body["action"] == "begin":
            if isinstance(self.begin, Exception):
                raise self.begin
            return self.begin
        return {"recorded": True}


@pytest.fixture(params=["nanobot", "pi"])
def setup(tmp_path, request):
    conn = sqlite3.connect(tmp_path / "pilot.db")
    conn.row_factory = sqlite3.Row
    binding = {"user_subject": "first", "endpoint_id": ENDPOINT, "credential_ref": "tg-static-local-binding"}
    gateway = Peer()
    pilot = SimpleNamespace(db=conn, gateway=gateway, host_type=request.param, bot_id=8001, sent=[], authorized=True)
    pilot._binding = lambda chat: (binding, "digest") if pilot.authorized else (None, None)
    pilot.navigation = SimpleNamespace(remember=lambda *args: None)

    def send(method, **params):
        assert method == "sendMessage" and params["chat_id"] == 501
        pilot.sent.append(params)
        return {"chat": {"id": 501, "type": "private"}, "from": {"id": 8001}, "message_id": 7}

    pilot.api = SimpleNamespace(call=send)
    terminal = TerminalMessages(pilot)
    yield pilot, terminal, gateway, binding
    close(terminal)
    conn.close()


def close(terminal):
    terminal.workspace_queries.executor.shutdown()
    if terminal.workspace_results:
        terminal.workspace_results.executor.shutdown()


def drain(terminal):
    queries = terminal.workspace_queries
    for _ in range(10):
        if not queries.pending:
            return
        futures = [entry[2] for entry in queries.pending.values()]
        assert not wait(futures, timeout=3).not_done
        queries.drain()
    pytest.fail("delivery did not settle")


def poll(setup):
    _, terminal, _, binding = setup
    terminal.poll(501, binding, "digest")
    drain(terminal)


def test_both_hosts_combine_public_answer_and_card_with_one_permit_and_one_message(setup):
    pilot, terminal, gateway, binding = setup
    gateway.release.clear()
    terminal.poll(501, binding, "digest")
    assert gateway.started.wait(1)
    terminal.poll(501, binding, "digest")
    assert not pilot.sent
    gateway.release.set()
    drain(terminal)
    assert [a["action"] for a in gateway.actions] == ["issue", "begin", "ack"]
    sent, = pilot.sent
    assert "已查到 3 项 OA 待办。" in sent["text"]
    assert len(sent["reply_markup"]["inline_keyboard"][0]) == 2
    assert gateway.actions[-1]["outcome"] == "sent" and gateway.actions[-1]["receipt"] == 7
    gateway.event["attempt"] = 2
    poll(setup)
    assert len(pilot.sent) == 1 and gateway.actions[-1]["action"] == "ack"


@pytest.mark.parametrize("text", ["未查到 OA 待办。", "暂时无法查询，请在工作台查看状态。", "结果" * 4000 + "😀"])
def test_public_only_completion_is_one_bounded_message(setup, text):
    pilot, _, gateway, _ = setup
    gateway.result = {"available": True, "publicText": text, "leaseExpiresAt": deadline()}
    poll(setup)
    sent, = pilot.sent
    assert "reply_markup" not in sent
    assert len(sent["text"].encode("utf-16-le")) // 2 <= 4096
    if len(text) > 3000:
        assert sent["text"].endswith("完整结果请在工作台查看。")


def test_text_parts_share_delivery_guards_and_preserve_central_chunk(setup):
    pilot, _, gateway, _ = setup
    entry = str(uuid4())
    gateway.event = {"kind": "workspace_text_v2", "deliveryId": DELIVERY, "attempt": 1, "pairingRef": PAIR,
                     "sourceEntryId": entry, "partIndex": 2, "partCount": 3}
    text = "智能体回复（2/3）\n" + "合成文字" * 900
    gateway.result = {"available": True, "leaseExpiresAt": deadline(), "publicText": text}
    poll(setup)
    assert pilot.sent[0]["text"] == text and "reply_markup" not in pilot.sent[0]
    assert all(a["sourceEntryId"] == entry and a["partIndex"] == 2 and a["partCount"] == 3
               and "sourceTaskId" not in a for a in gateway.actions)
    assert gateway.actions[-1]["outcome"] == "sent"
    poll(setup)
    assert len(pilot.sent) == 1


@pytest.mark.parametrize("invalid", ["card", "too_long", "bad_part"])
def test_text_claim_never_accepts_private_cards_or_invalid_parts(setup, invalid):
    pilot, _, gateway, _ = setup
    gateway.event = {"kind": "workspace_text_v2", "deliveryId": DELIVERY, "attempt": 1, "pairingRef": PAIR,
                     "sourceEntryId": str(uuid4()), "partIndex": 1, "partCount": 1}
    if invalid == "too_long":
        gateway.result = {"available": True, "leaseExpiresAt": deadline(), "publicText": "😀" * 2049}
    elif invalid == "bad_part":
        gateway.event["partIndex"] = True
    poll(setup)
    assert not pilot.sent


@pytest.mark.parametrize("failure", ["begin_denied", "begin_lost", "unknown", "expired", "revoked", "private_text"])
def test_invalid_or_uncertain_delivery_never_replays(setup, failure):
    pilot, _, gateway, _ = setup
    if failure == "begin_denied":
        gateway.begin = {"allowed": False}
    elif failure == "begin_lost":
        gateway.begin = GatewayRequestError("GATEWAY_TIMEOUT", "lost begin receipt")
    elif failure == "unknown":
        def ambiguous(method, **params):
            pilot.sent.append(params)
            raise TelegramUnavailable("lost receipt")
        pilot.api.call = ambiguous
    elif failure == "expired":
        gateway.result["resultView"]["sourceExpiresAt"] = "2001-01-01T00:00:00+00:00"
    elif failure == "revoked":
        pilot.authorized = False
    else:
        gateway.result["publicText"] = "https://fixture.invalid/auth/secret-ticket"
    poll(setup)
    gateway.event["attempt"] = 2
    poll(setup)
    assert len(pilot.sent) == (1 if failure == "unknown" else 0)
    if failure in {"begin_lost", "unknown"}:
        assert gateway.actions[-1]["outcome"] == "delivery_unknown"


@pytest.mark.parametrize("state", ["beginning", "sending", "delivered", "delivery_unknown"])
def test_restart_or_legacy_receipt_blocks_resending(setup, state):
    pilot, _, gateway, binding = setup
    with pilot.db:
        pilot.db.execute("INSERT INTO telegram_card_deliveries VALUES(?,?,?,?,?,?)",
                         (pilot.bot_id, 501, "result_view:" + TASK, DELIVERY, state, 7 if state == "delivered" else None))
    restarted = TerminalMessages(pilot)
    try:
        restarted.poll(501, binding, "digest")
        drain(restarted)
        assert not pilot.sent
        ack, = gateway.actions
        assert ack["action"] == "ack"
        assert ack["outcome"] == ("sent" if state == "delivered" else "delivery_unknown")
    finally:
        close(restarted)


def test_restart_recovers_lost_ack_without_another_central_claim(setup):
    pilot, terminal, gateway, _ = setup
    request = gateway._request
    def lose_ack(route, body, **options):
        if body.get("action") == "ack":
            raise GatewayRequestError("GATEWAY_TIMEOUT", "lost acknowledgement")
        return request(route, body, **options)
    gateway._request = lose_ack
    poll(setup)
    assert len(pilot.sent) == 1
    assert pilot.db.execute("SELECT acknowledged FROM telegram_workspace_query_attempts").fetchone()[0] == 0
    gateway._request = request
    with pilot.db:
        pilot.db.execute("UPDATE telegram_workspace_query_attempts SET next_ack=0")
    restarted = TerminalMessages(pilot)
    try:
        restarted.flush_deferred()
        drain(restarted)
        assert len(pilot.sent) == 1
        assert [a["action"] for a in gateway.actions] == ["issue", "begin", "ack"]
        assert gateway.actions[-1]["outcome"] == "sent" and gateway.actions[-1]["receipt"] == 7
        assert pilot.db.execute("SELECT acknowledged FROM telegram_workspace_query_attempts").fetchone()[0] == 1
    finally:
        close(restarted)


def test_crash_after_begin_reservation_recovers_unknown_without_claim_or_send(setup):
    pilot, terminal, gateway, binding = setup
    terminal.poll(501, binding, "digest")
    queries = terminal.workspace_queries
    assert not wait([entry[2] for entry in queries.pending.values()], timeout=3).not_done
    queries.drain()  # Reserves durably, starts begin, but never consumes its response.
    assert not wait([entry[2] for entry in queries.pending.values()], timeout=3).not_done
    assert not pilot.sent
    restarted = TerminalMessages(pilot)
    try:
        restarted.flush_deferred()
        drain(restarted)
        assert not pilot.sent
        assert [a["action"] for a in gateway.actions] == ["issue", "begin", "ack"]
        assert gateway.actions[-1]["outcome"] == "delivery_unknown"
    finally:
        close(restarted)


def test_binding_outage_before_reservation_can_recover(setup):
    pilot, _, gateway, _ = setup
    binding = pilot._binding
    def unavailable(chat):
        raise TelegramUnavailable("binding lookup unavailable")
    pilot._binding = unavailable
    poll(setup)
    assert not pilot.sent and not pilot.db.execute("SELECT * FROM telegram_card_deliveries").fetchall()
    pilot._binding = binding
    gateway.event["attempt"] = 2
    poll(setup)
    assert len(pilot.sent) == 1


def test_legacy_trusted_card_binding_outage_also_precedes_reservation(setup):
    pilot, terminal, _, binding = setup
    lookup = pilot._binding
    def unavailable(chat):
        raise TelegramUnavailable("binding lookup unavailable")
    pilot._binding = unavailable
    with pytest.raises(TelegramUnavailable):
        terminal.send_once(501, binding, "digest", "interaction:fixture", {"text": "trusted card fixture"})
    assert not pilot.db.execute("SELECT * FROM telegram_card_deliveries").fetchall()
    pilot._binding = lookup
    assert terminal.send_once(501, binding, "digest", "interaction:fixture", {"text": "trusted card fixture"})
    assert len(pilot.sent) == 1


@pytest.mark.parametrize("code,delay", [(429, 1), (400, None), (429, None)])
def test_only_explicit_rate_limit_can_send_again_after_central_reclaim(setup, code, delay):
    pilot, terminal, gateway, _ = setup
    send, attempts = pilot.api.call, []
    def rejected(method, **params):
        attempts.append(method)
        raise TelegramRejected(code, delay)
    pilot.api.call = rejected
    poll(setup)
    assert not pilot.sent
    assert gateway.actions[-1]["outcome"] == ("retry_after" if delay else "rejected")
    poll(setup)
    assert len(attempts) == 1  # Repeated claim cannot reuse a permit.
    pilot.api.call = send
    gateway.event["attempt"] = 2
    if delay:
        with pilot.db:
            pilot.db.execute("UPDATE telegram_workspace_query_attempts SET not_before=0")
    poll(setup)
    assert len(pilot.sent) == (1 if delay else 0)
    assert [a["action"] for a in gateway.actions].count("begin") == (2 if delay else 1)


@pytest.mark.parametrize("host_type", ["nanobot", "pi"])
def test_handler_pins_central_claim_proof_and_only_allows_new_protocol_actions(tmp_path, host_type):
    web = pytest.importorskip("aiohttp.web")
    req = {"hostType": host_type, "subject": "first", "endpoint": ENDPOINT, "endpointKey": "telegram:8001:501",
           "instance": "test-host", "workspaceEndpointId": "web-first", "userDir": str(tmp_path), "pairingRef": PAIR}
    host = SimpleNamespace(coordinator=True, card_origins={ORIGIN}, worker_payload=lambda *_: req)
    calls = []
    session = SimpleNamespace()
    @asynccontextmanager
    async def paired(_request):
        yield session, {}, "L3"
    async def call(name, args, meta):
        calls.append((name, args))
        if name == "agentbridge_host_notification_claim":
            assert args["include_workspace_query_v2"] is True
            result = {"endpoint": {"endpointId": ENDPOINT}, "notifications": [
                {"deliveryId": DELIVERY, "attemptCount": 1, "sourceTaskId": TASK,
                 "deliveryMode": "workspace_query_v2", "pairingRef": PAIR}]}
        else:
            assert name == "agentbridge_host_workspace_result_view"
            assert args["agent_host"] == host_type and args["pairing_ref"] == PAIR
            if args["action"] == "begin":
                result = {"allowed": True}
            elif args["action"] == "ack":
                result = {"recorded": True}
            else:
                result = {"available": True, "publicText": "未查到 OA 待办。", "leaseExpiresAt": deadline()}
        return SimpleNamespace(isError=False, structuredContent=result)
    session.call_tool = call
    def route(action, body):
        return asyncio.run(notifications(host, {"workspaceEndpointId": "web-first"}, action, body, paired_session=paired))
    event, = route("telegram-notifications", {})["deliveries"]
    endpoint = "telegram-workspace-result-view"
    assert route(endpoint, {**event, "action": "issue"})["publicText"] == "未查到 OA 待办。"
    assert route(endpoint, {**event, "action": "begin"}) == {"allowed": True}
    assert route(endpoint, {**event, "action": "ack", "outcome": "retry_after", "retryAfterSeconds": 9}) == {"recorded": True}
    assert calls[-1][1]["retry_after_seconds"] == 9
    for changed in ({"pairingRef": "b" * 64}, {"attempt": 2}, {"sourceTaskId": str(uuid4())}):
        with pytest.raises(web.HTTPForbidden):
            route(endpoint, {**event, "action": "issue", **changed})
    for changed in ({"outcome": "sent"}, {"outcome": "retry_after", "retryAfterSeconds": True},
                    {"outcome": "rejected", "receipt": 7}):
        with pytest.raises(web.HTTPBadRequest):
            route(endpoint, {**event, "action": "ack", **changed})
