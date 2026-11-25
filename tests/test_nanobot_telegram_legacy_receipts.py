"""Historical Telegram receipts can prove a read-only task or open an explicit chooser."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.support.nanobot_telegram_loopback import BOT, fixture, update
from tests.support.nanobot_telegram_setup import INSTANCE
from tests.test_nanobot_telegram_pairing import pair_fixture  # noqa: F401
from tests.test_nanobot_workspace_chat_mirror import mirror  # noqa: F401
from nanobot_agentbridge import telegram_tasks
from nanobot_agentbridge.notifications import TerminalMessages, _record_claim


def quote(receipt, chat=501):
    return {"message_id": receipt, "from": {"id": BOT}, "chat": {"id": chat, "type": "private"},
            "text": "办理状态更新\n原任务已完成。"}


def test_old_receipt_reads_only_its_acknowledged_terminal_task(mirror, pair_fixture, tmp_path, monkeypatch):
    service, links, _, subject, other, *_ = mirror
    endpoint = links.ensure_endpoint(subject, pair_fixture[0].tokens["a"], INSTANCE, BOT, 501)
    scope = {"subject": subject, "instance": INSTANCE, "endpoint": endpoint["endpointId"],
             "endpointKey": endpoint["endpointKey"], "conversation": endpoint["conversationRef"], "userDir": str(tmp_path)}
    args = dict(user_subject=subject, agent_host="nanobot", endpoint_key=scope["endpointKey"], host_instance_id=INSTANCE)
    store = service.tasks

    def make_task():
        return store.ensure_task(user_subject=subject, agent_host="nanobot", host_task_key=str(uuid4()),
            origin_endpoint_id=scope["endpoint"], active_conversation_ref=scope["conversation"], title="合成流程")[0]["task_id"]

    task = make_task()
    service.finish_host_task(user_subject=subject, task_id=task, outcome="succeeded")
    # More recent active work must never be substituted for the quoted task.
    active = make_task()
    with store._connect() as db:
        store._append_event(db, task_id=task, user_subject=subject, event_type="task.operation.succeeded",
                            payload={}, causation_ref=None, created_at=datetime.now(timezone.utc).isoformat())
    claimed = store.claim_outbox(user_subject=subject, endpoint_id=scope["endpoint"], host_instance_id=INSTANCE)
    event = next(item for item in claimed if item["payload"]["eventType"] == "task.operation.succeeded")
    delivery, attempt = event["delivery_id"], event["attempt_count"]
    lookup = dict(**args, delivery_id=delivery, attempt_count=attempt, lookup_acknowledged_task=True)
    assert service.validate_host_notification(**lookup) == {"allowed": False}
    service.acknowledge_host_notification(**args, delivery_id=delivery, attempt_count=attempt, succeeded=True)
    assert service.validate_host_notification(**lookup) == {"allowed": False, "acknowledgedTaskId": task}
    for change in [{"host_instance_id": "other-office"}, {"attempt_count": attempt + 1}, {"include_artifact": True}]:
        assert service.validate_host_notification(**{**lookup, **change}) == {"allowed": False}
    with pytest.raises((KeyError, PermissionError)):
        service.validate_host_notification(**{**lookup, "user_subject": other})

    calls = []
    authority = {"paired": True, "old_reply": None}

    class Session:
        async def call_tool(self, name, params, meta=None):
            calls.append(name)
            if name == "agentbridge_host_identity_profile":
                value = {"identity": {"userSubject": subject}, "host": {"hostInstanceId": INSTANCE},
                         "telegramIdentityVerified": authority["paired"]}
            elif name == "agentbridge_host_notification_validate":
                value = (authority["old_reply"] or service.validate_host_notification(
                    user_subject=subject, host_instance_id=INSTANCE, **params))
            elif name == "agentbridge_host_task_snapshot":
                value = service.get_host_task_snapshot(user_subject=subject, **params)
            elif name == "agentbridge_host_task_list":
                value = service.list_host_tasks(user_subject=subject, **params)
            else:
                raise AssertionError(name)
            return SimpleNamespace(isError=False, structuredContent=value)

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield Session(), {"agentHost": "nanobot", "hostInstanceId": INSTANCE}, "L3"

    monkeypatch.setattr(telegram_tasks, "open_host_session", session)
    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(web=SimpleNamespace(HTTPForbidden=PermissionError, HTTPBadRequest=ValueError)))
    host = SimpleNamespace(coordinator=True, instance=INSTANCE, worker_payload=lambda *_: scope)
    target = {"kind": "status_delivery", "id": delivery}

    def read(verb="continue"):
        return asyncio.run(telegram_tasks.navigate(host, {}, {"operation": verb, "target": target}))

    listed = asyncio.run(telegram_tasks.navigate(host, {}, {"operation": "history"}))
    assert [row["taskId"] for row in listed["candidates"]] == [task]
    assert read() == {"state": "unavailable"}
    path = tmp_path / "coordinated-runs.sqlite3"
    _record_claim(path, delivery, scope["endpoint"], "status:" + delivery, attempt)
    assert read("stop") == {"state": "unavailable"}
    for old in [{"allowed": True, "acknowledgedTaskId": task}, {"allowed": False}, {"allowed": False,"acknowledgedTaskId": "x"}]:
        authority["old_reply"] = old
        assert read() == {"state": "unavailable"}
    authority["old_reply"] = None
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        old = pilot.gateway._request

        def gateway(action, body, **kw):
            if action == "telegram-task":
                assert body["operation"] == "continue" and body["target"] == target
                return read()
            return old(action, body, **kw)

        monkeypatch.setattr(pilot.gateway, "_request", gateway)
        TerminalMessages(pilot)
        with pilot.db:
            pilot.db.execute("INSERT INTO telegram_card_deliveries VALUES(?,?,?,?,?,?)",
                             (BOT, 501, "status:" + delivery, delivery, "delivered", 499))
        for table in ("messages", "notification_receipts", "telegram_navigation_receipts"):
            assert pilot.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        with sqlite3.connect(path) as db:
            assert not db.execute("SELECT name FROM sqlite_schema WHERE name='tasks'").fetchone()
        pilot.ingest(update(1, message=10, text="继续", reply_to_message=quote(499)))
        assert state["deliveries"][-1]["text"] == "原任务状态：已完成。"
        pilot.ingest(update(2, message=11, text="停止", reply_to_message=quote(499)))
        assert "只可查看" in state["deliveries"][-1]["text"]
        assert not state["admissions"]
        assert store.get_task(active, user_subject=subject)["status"] == "active"
        assert pilot.navigation.quoted(501, "", quote(499, chat=502)) is None
        assert pilot.navigation.quoted(501, "", {**quote(499), "forward_origin": {}}) is None
        with pilot.db:
            pilot.db.execute("UPDATE telegram_card_deliveries SET delivery=? WHERE receipt=499", (str(uuid4()),))
        assert pilot.navigation.quoted(501, "", quote(499)) is None
        pilot.db.close()
    authority["paired"] = False
    with pytest.raises(PermissionError):
        read()
    authority["paired"] = True
    scope["conversation"] += "-changed"
    with pytest.raises(PermissionError):
        read()
    scope["conversation"] = endpoint["conversationRef"]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE notification_claims SET run='other-run'")
    assert read() == {"state": "unavailable"}
    assert set(calls) == {"agentbridge_host_identity_profile", "agentbridge_host_notification_validate",
                          "agentbridge_host_task_list", "agentbridge_host_task_snapshot"}


def test_missing_receipt_requires_choice_and_history_can_never_stop(tmp_path, monkeypatch):
    task = str(uuid4())
    with fixture(tmp_path) as (state, bindings, create):
        pilot = create()
        old = pilot.gateway._request
        calls = []
        def gateway(action, body, **kwargs):
            if action != "telegram-task":
                return old(action, body, **kwargs)
            calls.append(body["operation"])
            if body["operation"] == "history":
                return {"candidates": [{"taskId": task, "title": "历史事项", "status": "succeeded"}]}
            assert body["target"] == {"kind": "history_task", "id": task}
            return {"state": "succeeded", "taskId": task}
        monkeypatch.setattr(pilot.gateway, "_request", gateway)
        pilot.ingest(update(1, message=10, text="继续", reply_to_message=quote(499)))
        assert calls == ["history"] and "回复本条消息并输入编号" in state["deliveries"][-1]["text"]
        pilot.ingest(update(2, message=11, text="停止 1", reply_to_message=quote(701)))
        assert calls == ["history"]
        pilot.ingest(update(3, message=12, text="1", reply_to_message=quote(701)))
        assert calls == ["history", "continue"] and "已完成" in state["deliveries"][-1]["text"]
        pilot.ingest(update(4, message=13, text="停止", reply_to_message=quote(703)))
        pilot.ingest(update(5, who=502, message=14, text="1", reply_to_message=quote(701)))
        assert calls == ["history", "continue"]
        with pilot.db:
            raw = json.loads(pilot.db.execute("SELECT target_json FROM telegram_navigation_receipts WHERE receipt=701").fetchone()[0])
            assert raw["kind"] == "history_choices"
            pilot.db.execute("UPDATE telegram_navigation_receipts SET target_json=? WHERE receipt=701", (json.dumps({**raw, "expires": 0}),))
        pilot.ingest(update(6, message=15, text="1", reply_to_message=quote(701)))
        assert "已过期" in state["deliveries"][-1]["text"]
        bindings["501"]["credential_ref"] += "-changed"
        pilot.ingest(update(7, message=16, text="继续", reply_to_message=quote(703)))
        assert "无法核对" in state["deliveries"][-1]["text"] and not state["admissions"]
        assert calls == ["history", "continue"]
        pilot.db.close()


def test_terminal_notification_stores_confirmed_receipt_once(tmp_path, monkeypatch):
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        notices = TerminalMessages(pilot)
        binding, digest = pilot._binding(501)
        event = {"deliveryId": str(uuid4()), "taskId": str(uuid4()), "runId": str(uuid4()), "attempt": 1, "status": "succeeded"}
        acknowledgments = []
        monkeypatch.setattr(notices, "_ack_receipt", lambda *args: acknowledgments.append(args[-1]))
        notices._deliver_terminal_event(501, binding, digest, event, 12)
        notices._deliver_terminal_event(501, binding, digest, event, 12)
        assert len(state["deliveries"]) == 1 and acknowledgments == [True, True]
        raw = json.loads(pilot.db.execute("SELECT target_json FROM telegram_navigation_receipts WHERE receipt=701").fetchone()[0])
        assert raw == {"kind": "run", "id": event["runId"], "requestKey": pilot._key(501, 12), "terminalTaskId": event["taskId"]}
        assert pilot.navigation.quoted(501, digest, quote(701)) == {"kind":"history_task", "id":event["taskId"]}
        assert pilot.navigation.quoted(501, "changed", quote(701)) is None
        assert pilot.db.execute("SELECT state FROM notification_receipts").fetchone()[0] == "delivered"
        pilot.db.close()
