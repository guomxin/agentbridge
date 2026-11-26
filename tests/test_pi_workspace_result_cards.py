"""A Workspace result is issued for its claimed Pi Telegram destination and sent at most once."""

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
from agentbridge.workspace.nanobot_gateway import PiGatewayClient
from nanobot_agentbridge.notifications import TerminalMessages, notifications
from nanobot_agentbridge.telegram import TelegramUnavailable

TASK, DELIVERY, ENDPOINT = (str(uuid4()) for _ in range(3))
PAIR = "a" * 64
ORIGIN = "https://fixture.invalid"
URL = ORIGIN + "/results/" + "a" * 43


def future_time():
    return (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()


def view():
    return {"sourceTaskId": TASK, "selectionAvailable": True, "sourceExpiresAt": future_time(),
            "expiresAt": future_time(), "url": URL}


class Peer:
    def __init__(self):
        self.enter = Event()
        self.release = Event()
        self.release.set()
        self.actions = []
        self.result = None

    def _request(self, route, body, *, timeout=8):
        if route == "telegram-notifications":
            return {"deliveries": [self.event]}
        assert route == "telegram-workspace-result-view"
        if body["action"] == "issue":
            assert timeout == PiGatewayClient.result_view_timeout and timeout > 8
        else:
            assert timeout == 8
        self.actions.append(dict(body))
        if body["action"] == "ack":
            return {"recorded": True}
        self.enter.set()
        assert self.release.wait(2)
        return self.result or {"available": True, "resultView": view(), "leaseExpiresAt": future_time()}


@pytest.fixture
def setup(tmp_path):
    conn = sqlite3.connect(tmp_path / "pilot.db")
    conn.row_factory = sqlite3.Row
    gateway = Peer()
    binding = {"user_subject": "first", "endpoint_id": ENDPOINT, "credential_ref": "tg-" + PAIR}
    p = SimpleNamespace(db=conn, gateway=gateway, host_type="pi", bot_id=8001, sent=[], authorized=True)
    p._binding = lambda chat: (binding, "digest") if p.authorized else (None, None)
    p.navigation = SimpleNamespace(remember=lambda *args: None)

    def send(method, **params):
        assert method == "sendMessage" and params["chat_id"] == 501
        p.sent.append(params)
        return {"chat": {"id": 501, "type": "private"}, "from": {"id": 8001}, "message_id": 7}

    p.api = SimpleNamespace(call=send)
    gateway.event = {"kind": "workspace_result_view", "deliveryId": DELIVERY, "attempt": 1, "sourceTaskId": TASK}
    terminal = TerminalMessages(p)
    yield p, terminal, gateway, binding
    terminal.workspace_results.executor.shutdown()
    conn.close()


def drain(terminal):
    cards = terminal.workspace_results
    while cards.pending:
        wait([entry[2] for entry in cards.pending.values()], timeout=3)
        cards.drain()


def test_slow_issue_does_not_block_poll_and_duplicate_reack_uses_original_receipt(setup):
    pilot, terminal, gateway, binding = setup
    gateway.release.clear()
    terminal.poll(501, binding, "digest")
    assert gateway.enter.wait(1)
    terminal.poll(501, binding, "digest")
    assert not pilot.sent
    gateway.release.set()
    drain(terminal)
    gateway.event["attempt"] = 2
    terminal.poll(501, binding, "digest")
    drain(terminal)
    assert [a["action"] for a in gateway.actions] == ["issue", "ack", "ack"]
    assert [(a["receipt"], a["attempt"]) for a in gateway.actions[1:]] == [(7, 1), (7, 2)]
    first, = pilot.sent
    buttons, = first["reply_markup"]["inline_keyboard"]
    assert buttons[0]["web_app"]["url"] == buttons[1]["url"] == URL
    assert len(buttons) == 2


@pytest.mark.parametrize("failure", ["revoked", "source_expired", "lease_expired", "unavailable", "unknown"])
def test_terminal_outcomes_and_final_binding_guard(setup, failure):
    pilot, terminal, gateway, binding = setup
    raw = view()
    if failure == "source_expired":
        raw["sourceExpiresAt"] = "2001-01-01T00:00:00+00:00"
    deadline = "2001-01-01T00:00:00+00:00" if failure == "lease_expired" else future_time()
    gateway.result = {"available": False} if failure == "unavailable" else {
        "available": True, "leaseExpiresAt": deadline, "resultView": raw}
    terminal.poll(501, binding, "digest")
    if failure == "revoked":
        pilot.authorized = False
    if failure == "unknown":
        def ambiguous(method, **params):
            pilot.sent.append(method)
            raise TelegramUnavailable("lost receipt")
        pilot.api.call = ambiguous
    drain(terminal)
    if failure == "unknown":
        gateway.event["attempt"] = 2
        terminal.poll(501, binding, "digest")
        drain(terminal)
        assert pilot.sent == ["sendMessage"]
        assert [a["outcome"] for a in gateway.actions[1:]] == ["delivery_unknown", "delivery_unknown"]
    elif failure == "unavailable":
        assert not pilot.sent and gateway.actions[-1]["outcome"] == "unavailable"
    else:
        assert not pilot.sent and [a["action"] for a in gateway.actions] == ["issue"]


def test_restart_in_flight_is_unknown_and_never_sends(setup):
    pilot, terminal, gateway, binding = setup
    with pilot.db:
        pilot.db.execute("INSERT INTO telegram_card_deliveries VALUES(?,?,?,?,?,?)",
                         (pilot.bot_id, 501, "result_view:" + TASK, DELIVERY, "sending", None))
    restarted = TerminalMessages(pilot)
    try:
        restarted.poll(501, binding, "digest")
        drain(restarted)
        assert not pilot.sent
        assert [(a["action"], a["outcome"]) for a in gateway.actions] == [("ack", "delivery_unknown")]
    finally:
        restarted.workspace_results.executor.shutdown()


def test_only_the_original_claim_can_issue_and_attest_its_own_receipt(tmp_path):
    web = pytest.importorskip("aiohttp.web")
    session = SimpleNamespace()
    origin = ORIGIN
    calls = []
    failed = False
    req = {"hostType": "pi", "subject": "first", "endpoint": ENDPOINT, "endpointKey": "telegram:8001:501",
           "instance": "pi-office", "workspaceEndpointId": "web-first", "userDir": str(tmp_path), "pairingRef": PAIR}
    host = SimpleNamespace(coordinator=True, card_origins={ORIGIN}, worker_payload=lambda *_: req)
    record = {"workspaceEndpointId": "web-first"}

    @asynccontextmanager
    async def paired(request):
        assert request == req
        yield session, {}, "L3"

    async def call(name, args, meta):
        calls.append((name, args))
        if name == "agentbridge_host_notification_claim":
            assert args["include_workspace_result_views"] is True
            value = {"endpoint": {"endpointId": ENDPOINT}, "notifications": [
                {"deliveryId": DELIVERY, "attemptCount": 1, "sourceTaskId": TASK, "deliveryMode": "workspace_result_view"}]}
        else:
            assert name == "agentbridge_host_workspace_result_view"
            assert args["pairing_ref"] == PAIR and args["endpoint_key"] == req["endpointKey"]
            value = {"available": True, "leaseExpiresAt": future_time(), "resultView": {
                **view(), "schemaVersion": "agentbridge.result-view.v1",
                "presentation": {"owner": "agentbridge", "modelMustNotCollectValues": True,
                                 "endpointId": ENDPOINT, "url": origin + "/results/" + "a" * 43}}}
        return SimpleNamespace(isError=failed, structuredContent=value)

    session.call_tool = call

    def route(action, body):
        return asyncio.run(notifications(host, record, action, body, paired_session=paired))

    event, = route("telegram-notifications", {})["deliveries"]
    assert set(event) == {"kind", "sourceTaskId", "deliveryId", "attempt"}
    endpoint = "telegram-workspace-result-view"
    assert route(endpoint, {**event, "action": "issue"})["resultView"]["url"] == URL
    with pytest.raises(web.HTTPForbidden):
        route(endpoint, {**event, "sourceTaskId": str(uuid4()), "action": "issue"})
    with pytest.raises(web.HTTPForbidden):
        route(endpoint, {**event, "attempt": 2, "action": "issue"})
    with pytest.raises(web.HTTPForbidden):
        route("telegram-notification-receipt", {**event, "runId": "workspace_result_view:" + TASK, "delivered": True})
    origin = "https://unreviewed.invalid"
    with pytest.raises(web.HTTPBadGateway):
        route(endpoint, {**event, "action": "issue"})
    failed = True
    with pytest.raises(web.HTTPBadGateway):
        route(endpoint, {**event, "action": "issue"})


def test_temporary_binding_check_outage_precedes_bot_reservation(setup):
    pilot, terminal, gateway, binding = setup
    check = pilot._binding

    def interrupted(chat):
        raise TelegramUnavailable("pairing lookup unavailable")

    pilot._binding = interrupted
    terminal.poll(501, binding, "digest")
    drain(terminal)
    assert not pilot.sent
    assert not pilot.db.execute("SELECT * FROM telegram_card_deliveries").fetchall()
    pilot._binding = check
    gateway.event["attempt"] = 2
    terminal.poll(501, binding, "digest")
    drain(terminal)
    assert len(pilot.sent) == 1
    assert [a["action"] for a in gateway.actions] == ["issue", "issue", "ack"]


def test_replaced_attempt_cannot_send_old_issued_ticket(setup):
    pilot, terminal, gateway, binding = setup
    gateway.release.clear()
    terminal.poll(501, binding, "digest")
    assert gateway.enter.wait(1)
    gateway.event["attempt"] = 2
    terminal.poll(501, binding, "digest")
    gateway.release.set()
    drain(terminal)
    assert len(pilot.sent) == 1
    assert [(a["action"], a["attempt"]) for a in gateway.actions] == [("issue", 1), ("issue", 2), ("ack", 2)]
