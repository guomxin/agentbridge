"""Isolated Workspace-to-private-bot handoff, including partial and ambiguous sends."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.support.nanobot_telegram_loopback import BOT
from nanobot_agentbridge.chat_mirror import projected_chat, telegram_parts
from nanobot_agentbridge.notifications import notifications
from nanobot_agentbridge.telegram import TelegramPilot, TelegramUnavailable


def event(text="网页消息", **fields):
    return {"deliveryId": str(uuid4()), "attempt": 1, "entryId": str(uuid4()), "role": "user", "text": text, **fields}


class Gateway:
    def __init__(self):
        self.events = []
        self.validations = []
        self.acks = []
        self.allowed = []
        self.binding = {"user_subject": "first", "endpoint_key": f"telegram:{BOT}:501"}
        self.discovery = ["501"]

    def _request(self, action, data):
        if action == "telegram-mirror-peers":
            return {"senderIds": self.discovery}
        if action == "telegram-notifications":
            return {"deliveries": self.events}
        if action == "telegram-chat-validate":
            self.validations.append(data)
            return {"allowed": self.allowed.pop(0) if self.allowed else True}
        assert action == "telegram-chat-receipt"
        self.acks.append(data)
        return {"recorded": True}

    def resolve_telegram_sender(self, bot, sender):
        return self.binding if bot == BOT and sender == 501 else None

    def verify_telegram_binding(self, binding):
        return binding == self.binding


class Api:
    def __init__(self):
        self.sent = []
        self.failure_at = None
        self.wrong_chat = False

    def call(self, method, **data):
        if method == "getUpdates":
            return []
        assert method == "sendMessage" and "reply_parameters" not in data
        self.sent.append(data)
        if self.failure_at == len(self.sent):
            raise TelegramUnavailable("simulated lost reply")
        return {"message_id": 90 + len(self.sent), "chat": {"id": -1 if self.wrong_chat else data["chat_id"], "type": "private"},
                "from": {"id": BOT}}


def pilot(tmp_path, api, gateway):
    return TelegramPilot(tmp_path / "messages.db", api=api, bot_id=BOT, instance="demo",
                         gateway=gateway, bindings=lambda: {}, allow_pairing=True)


def test_first_chat_after_pairing_works_without_local_inbound_or_task(tmp_path):
    gateway, api = Gateway(), Api()
    message = event(role="assistant", kind="workspace_chat")
    gateway.events = [message]
    gateway.discovery.extend(["502", "untrusted"])
    p = pilot(tmp_path, api, gateway)
    p.poll_once()
    assert p.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert len(api.sent) == len(gateway.validations) == len(gateway.acks) == 1
    assert api.sent[0]["chat_id"] == 501 and api.sent[0]["text"] == "【网页端 · 智能体】\n网页消息"
    assert gateway.acks[0]["delivered"] is True
    p.db.close()
    p = pilot(tmp_path, api, gateway)
    p.poll_once()
    assert len(api.sent) == len(gateway.validations) == 1
    assert len(gateway.acks) == 2
    p.db.close()


def test_long_unicode_resumes_only_unsent_parts_after_new_lease(tmp_path):
    api, gateway = Api(), Gateway()
    p = pilot(tmp_path, api, gateway)
    text = "汉😀" * 3600 + "最后一行"
    message = event(text)
    parts = list(telegram_parts("user", text))
    assert len(parts) == 3 and "".join(parts).removeprefix("【网页端 · 你】\n") == text
    assert all(len(part.encode("utf-16-le")) <= 8192 for part in parts)
    gateway.allowed = [True, False]
    p.terminal.chat.poll(501, gateway.binding, "pinned", message)
    assert [entry["text"] for entry in api.sent] == parts[:1] and not gateway.acks
    p.db.close()
    p = pilot(tmp_path, api, gateway)
    p.terminal.chat.poll(501, gateway.binding, "pinned", {**message, "attempt": 2})
    assert [entry["text"] for entry in api.sent] == parts and gateway.acks[-1]["delivered"]
    assert len(gateway.validations) == 4
    assert p.db.execute("SELECT COUNT(DISTINCT receipt) FROM chat_mirror_parts WHERE state='delivered'").fetchone()[0] == 3
    p.db.close()


def test_ambiguous_part_stops_later_parts_and_restart_never_resends(tmp_path):
    api, gateway = Api(), Gateway()
    p = pilot(tmp_path, api, gateway)
    message = event("汉😀" * 5000)
    api.failure_at = 2
    p.terminal.chat.poll(501, gateway.binding, "pinned", message)
    assert len(api.sent) == 2 and not gateway.acks[-1]["delivered"]
    assert [r[0] for r in p.db.execute("SELECT state FROM chat_mirror_parts ORDER BY part")] == ["delivered", "delivery_unknown"]
    p.db.close()
    p = pilot(tmp_path, api, gateway)
    p.terminal.chat.poll(501, gateway.binding, "pinned", {**message, "attempt": 2})
    p.terminal.chat.poll(501, gateway.binding, "changed", {**message, "attempt": 3})
    p.terminal.chat.poll(501, gateway.binding, "pinned", {**message, "deliveryId": str(uuid4())})
    assert len(api.sent) == len(gateway.acks) == 2 and not gateway.acks[-1]["delivered"]
    p.db.close()


def test_wrong_receipt_and_crash_after_send_boundary_never_repeat(tmp_path):
    api, gateway = Api(), Gateway()
    api.wrong_chat = True
    p = pilot(tmp_path, api, gateway)
    message = event()
    p.terminal.chat.poll(501, gateway.binding, "scope", message)
    assert not gateway.acks[-1]["delivered"] and len(api.sent) == 1
    p.db.execute("UPDATE chat_mirror_parts SET state='sending'"); p.db.commit(); p.db.close()
    p = pilot(tmp_path, api, gateway)
    p.terminal.chat.poll(501, gateway.binding, "scope", {**message, "attempt": 2})
    assert not gateway.acks[-1]["delivered"] and len(api.sent) == 1
    assert p.db.execute("SELECT state FROM chat_mirror_parts").fetchone()[0] == "delivery_unknown"
    p.db.close()


def claim_item(line, workspace="web-first"):
    return {"deliveryId": line["deliveryId"], "attemptCount": line["attempt"], "requiresRouteCheck": True,
            "deliveryMode": "timeline_message", "task": None, "event": None, "interaction": None, "attachments": [],
            "timeline": {"mirrorKind": "nanobot_workspace_text_v1", "entryId": line["entryId"], "role": line["role"],
                         "text": line["text"], "source": {"endpointId": workspace, "clientType": "web"}}}


def test_private_malformed_foreign_and_attachment_projections_are_silent():
    row = claim_item(event())
    assert projected_chat(row, "web-first")["role"] == "user"
    assert projected_chat(row, "other-web") is None
    assert projected_chat({"timeline": "invalid"}, "web-first") is None
    for fields in [{"interaction":{"url":"private"}}, {"attachments":[{"id":"private"}]}, {"requiresRouteCheck":False},
                   {"task":{"taskId":"other"}}, {"timeline":{**row["timeline"],"text":"Bearer synthetic-value"}},
                   {"timeline":{**row["timeline"],"role":"tool"}}, {"timeline":{**row["timeline"], "mirrorKind":"other"}},
                   {"timeline":{**row["timeline"], "source":{"endpointId":"web-first","clientType":"telegram"}}}]:
        assert projected_chat({**row, **fields}, "web-first") is None


@pytest.fixture
def web_errors(monkeypatch):
    import sys

    class Forbidden(Exception):
        pass

    class BadRequest(Exception):
        pass

    errors = SimpleNamespace(HTTPForbidden=Forbidden, HTTPBadRequest=BadRequest)
    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(web=errors))
    return errors


def test_host_claim_validate_ack_require_same_peer_current_lease_and_web_origin(tmp_path, monkeypatch, web_errors):
    import sys

    line = event()
    request = {"instance": "instance", "subject": "first", "endpointKey": f"telegram:{BOT}:501",
               "endpoint": "tg-first", "workspaceEndpointId": "web-first", "userDir": str(tmp_path)}
    host = SimpleNamespace(coordinator=True, worker_payload=lambda *_: request)
    record = {"workspaceEndpointId": "web-first"}
    calls = []
    lease = [1]
    permission = [True]

    class Session:
        async def call_tool(self, name, args, *, meta):
            calls.append((name, args))
            assert meta == {"private": True}
            if name.endswith("_identity_profile"):
                data = {"identity": {"userSubject": "first"}, "host": {"hostInstanceId": "instance"},
                        "telegramIdentityVerified": True}
            elif name.endswith("_claim"):
                assert args["include_workspace_chat"]
                items = [claim_item({**line, "attempt": lease[0]}), claim_item(event(), "other-web")]
                data = {"endpoint": {"endpointId": "tg-first"}, "notifications": items}
            elif name.endswith("_validate"):
                data = {"allowed": permission[0]}
            else:
                assert name.endswith("_ack")
                data = {"delivery": {"deliveryId": args["delivery_id"], "attemptCount": args["attempt_count"]}}
            return SimpleNamespace(isError=False, structuredContent=data)

    @asynccontextmanager
    async def session(_):
        yield Session(), {"private": True}, "L3"

    async def verified(*_, host_type):
        return host_type == "nanobot"

    monkeypatch.setitem(sys.modules, "nanobot_agentbridge.worker", SimpleNamespace(authenticated_session=session))
    monkeypatch.setattr("nanobot_agentbridge.telegram_identity.verify_telegram_binding", verified)
    def call(action, **data):
        return asyncio.run(notifications(host, record, action, data))
    params = {"deliveryId": line["deliveryId"], "attempt": 1}
    with pytest.raises(web_errors.HTTPForbidden):
        call("telegram-chat-validate", **params)
    projected = call("telegram-notifications")
    assert len(projected["deliveries"]) == 1 and projected["deliveries"][0]["text"] == line["text"]
    assert not (tmp_path / "coordinated-runs.sqlite3").exists()
    assert call("telegram-chat-validate", **params) == {"allowed": True}
    permission[0] = False
    assert call("telegram-chat-validate", **params) == {"allowed": False}
    request["endpoint"] = "different"
    with pytest.raises(web_errors.HTTPForbidden):
        call("telegram-chat-receipt", **params, delivered=True)
    request["endpoint"] = "tg-first"
    lease[0] = 2
    assert call("telegram-notifications")["deliveries"][0]["attempt"] == 2
    with pytest.raises(web_errors.HTTPForbidden):
        call("telegram-chat-validate", **params)
    assert call("telegram-chat-receipt", **{**params, "attempt":2}, delivered=False) == {"recorded": True}
    assert sum(name.endswith("_ack") for name, _ in calls) == 1


def test_discovery_only_uses_each_provisioned_person_and_ignores_shared_sender(monkeypatch, web_errors):
    import sys
    from nanobot_agentbridge.telegram_routes import TelegramRoutes

    class Session:
        def __init__(self, subject):
            self.subject = subject
        async def call_tool(self, name, args, *, meta):
            assert name == "agentbridge_host_telegram_mirror_peers" and args == {"bot_id": str(BOT)}
            senders = {"first": ["501", "777"], "new-user": ["502", "777"], "unlinked": []}
            return SimpleNamespace(isError=False, structuredContent={"senderIds": senders[self.subject]})

    @asynccontextmanager
    async def authenticated(subject):
        yield Session(subject), {}, "L3"
    monkeypatch.setitem(sys.modules, "nanobot_agentbridge.worker", SimpleNamespace(authenticated_session=authenticated))
    monkeypatch.setitem(sys.modules, "nanobot_agentbridge.server", SimpleNamespace(identity_denial=lambda _: False))
    host = SimpleNamespace(bindings={str(idx):{"endpoint_key":"web:"+name, "user_subject":name}
                                        for idx,name in enumerate(("first","unlinked"))},
                           enrollment=SimpleNamespace(identities={"new-user":{"user_subject":"new-user"}}),
                           worker_payload=lambda r,_:r["user_subject"])
    routes = TelegramRoutes(host, {"telegramPilot":{"botId":BOT,"username":"fixture_bot"}})
    assert asyncio.run(routes.mirror_peers({"botId":str(BOT)})) == {"senderIds":["501", "502"]}
    with pytest.raises(web_errors.HTTPForbidden):
        asyncio.run(routes.mirror_peers({"botId":"1000"}))
