"""The Telegram message, host routing and central state have distinct authorities."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import sys
from types import SimpleNamespace
from uuid import uuid4

from tests.support.nanobot_telegram_loopback import BOT, fixture, update
from tests.test_nanobot_telegram_presentation import ORIGIN, _interaction
from nanobot_agentbridge import telegram_tasks


def _quoted(number, *, who=BOT):
    return {"message_id": number, "from": {"id": who}}


def test_navigation_names_every_central_task_state():
    from agentbridge.core.task_state_rules import TASK_STATUSES
    from nanobot_agentbridge.telegram_navigation import _STATES

    assert TASK_STATUSES <= _STATES.keys()
    assert "部分事项已完成" in _STATES["partially_succeeded"]


def test_choices_are_bound_to_original_receipt_and_stop_exact_task_once(tmp_path, monkeypatch):
    tasks = [str(uuid4()), str(uuid4())]
    calls = []
    with fixture(tmp_path) as (state, bindings, create):
        pilot = create()
        request = pilot.gateway._request

        def navigate(action, body, **kwargs):
            if action != "telegram-task":
                return request(action, body, **kwargs)
            calls.append(body)
            if body["operation"] == "list":
                return {"candidates": [{"taskId": k, "title": "合成事项", "status": "waiting_user"} for k in tasks]}
            assert body["target"] == {"id": tasks[1], "kind": "task"}
            return {"state": "canceled", "taskId": tasks[1]}

        monkeypatch.setattr(pilot.gateway, "_request", navigate)
        pilot.ingest(update(1, text="停止"))
        assert "2. " in state["deliveries"][-1]["text"]
        pilot.ingest(update(2, 502, message=12, text="2", reply_to_message=_quoted(701)))
        pilot.ingest(update(3, message=13, text="2", reply_to_message=_quoted(701, who=BOT+1)))
        assert len(calls) == 1
        chosen = update(4, message=14, text="2", reply_to_message=_quoted(701))
        pilot.ingest(chosen)
        pilot.ingest(chosen)
        assert [c["operation"] for c in calls] == ["list", "stop"]
        assert state["deliveries"][-1]["text"] == "原任务状态：已停止。"
        bindings["501"]["credential_ref"] += "-changed"
        pilot.ingest(update(5, message=15, text="停止", reply_to_message=_quoted(704)))
        assert len(calls) == 2 and not state["admissions"]
        pilot.db.close()


def test_quoted_ordinary_reply_and_card_redisplay_do_not_dispatch_new_turn(tmp_path, monkeypatch):
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        original = pilot.gateway._request
        targets = []
        task = str(uuid4())
        def gateway(action, body, **kwargs):
            if action != "telegram-task":
                return original(action, body, **kwargs)
            targets.append(body["target"])
            return {"taskId": task, "state": "waiting_user", "interaction": _interaction()}
        monkeypatch.setattr(pilot.gateway, "_request", gateway)
        pilot.ingest(update(1))
        pilot.reconcile()
        assert len(state["admissions"]) == 1
        pilot.ingest(update(2, message=10, text="继续", reply_to_message=_quoted(701)))
        assert targets[0]["kind"] == "run" and targets[0]["requestKey"] == f"tg:{BOT}:501:9"
        assert state["deliveries"][-1]["reply_markup"]["inline_keyboard"]
        pilot.ingest(update(3, message=11, text="继续", reply_to_message=_quoted(702)))
        assert targets[-1] == {"kind": "task", "id": task}
        pilot.ingest(update(4, message=12, text="同意", reply_to_message=_quoted(702)))
        assert len(targets) == 2 and len(state["admissions"]) == 1
        pilot.db.close()


def test_live_task_lookup_and_cancel_rejects_expiry_foreign_origin_and_unknown(tmp_path, monkeypatch):
    task, card_id = str(uuid4()), str(uuid4())
    route = {"subject": "alice", "endpoint": "ep-alice", "endpointKey": f"telegram:{BOT}:501",
             "instance": "office", "conversation": "alice-chat", "userDir": str(tmp_path)}
    item = {"taskId": task, "agentHost": "nanobot", "userSubject": route["subject"], "originEndpointId": route["endpoint"],
            "activeConversationRef": route["conversation"], "status": "waiting_user", "currentInteractionId": card_id}
    card = {**_interaction(interaction_id=card_id), "taskId": task}
    calls = []
    live = {"bound": True, "level": "L3"}
    class Forbidden(Exception):
        pass
    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(web=SimpleNamespace(HTTPForbidden=Forbidden, HTTPBadRequest=ValueError)))
    class Central:
        async def call_tool(self, name, args, meta=None):
            calls.append(name)
            if name == "agentbridge_host_identity_profile":
                payload = {"identity": {"userSubject": "alice"}, "host": {"hostInstanceId": "office"},
                           "telegramIdentityVerified": live["bound"]}
            elif name == "agentbridge_host_task_snapshot":
                payload = {"endpoint": {"endpointId": route["endpoint"]}, "task": item, "interaction": card}
            elif name == "agentbridge_task_cancel":
                payload = {"status": "rejected", "error": {"code": "TASK_ACTION_ALREADY_STARTED"}}
            else:
                raise AssertionError(name)
            return SimpleNamespace(isError=False, structuredContent=payload)
    @asynccontextmanager
    async def open_session(*args, **kwargs):
        yield Central(), {"agentHost": "nanobot", "hostInstanceId": "office"}, live["level"]
    monkeypatch.setattr(telegram_tasks, "open_host_session", open_session)
    host = SimpleNamespace(coordinator=True, instance="office", card_origins=[ORIGIN],
                           worker_payload=lambda _rec, _run: route)
    def run(action):
        return asyncio.run(telegram_tasks.navigate(host, {"credential_ref": "synthetic-original-pair"},
            {"operation": action, "target": {"kind": "task","id": task}}))
    import pytest
    assert run("continue")["interaction"]["interactionId"] == card_id
    card["expiresAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert "interaction" not in run("continue")
    assert run("stop")["state"] == "stop_unconfirmed"
    item["status"] = "outcome_unknown"
    assert run("stop")["state"] == "outcome_unknown"
    for key, other in (("originEndpointId", "foreign"), ("userSubject", "bob"), ("activeConversationRef", "foreign")):
        old = item[key]
        item[key] = other
        with pytest.raises(Forbidden):
            run("stop")
        item[key] = old
    live["bound"] = False
    with pytest.raises(Forbidden):
        run("continue")
    assert calls.count("agentbridge_task_cancel") == 1
    assert not any("resume" in name or "commit" in name or "prepare" in name for name in calls)


def test_navigation_uses_real_central_cancel_and_keeps_already_submitted_input(tmp_path, monkeypatch):
    from tests.test_plan_write_boundaries import PlanWriteBoundaryTests

    # The embedded central service must use an isolated session key on macOS.
    import secrets
    key = tmp_path / "session.key"
    key.write_bytes(secrets.token_bytes(32))
    key.chmod(0o600)
    monkeypatch.setenv("AGENTBRIDGE_SESSION_KEY_FILE", str(key))
    case = PlanWriteBoundaryTests()
    case.setUp()
    try:
        service = case.service
        endpoint, _ = service.tasks.ensure_endpoint(user_subject="user-a", token_id=case.origin["token_id"],
            agent_host="nanobot", endpoint_key=f"telegram:{BOT}:501", client_type="telegram", account_id=str(BOT),
            external_subject="501", conversation_ref="chat-a")
        scope = {"endpoint": endpoint["endpoint_id"], "endpointKey": endpoint["endpoint_key"], "conversation": "chat-a",
                 "subject": "user-a", "instance": "office", "userDir": str(tmp_path)}
        def create(_):
            task, _ = service.tasks.ensure_task(user_subject="user-a", agent_host="nanobot", host_task_key=str(uuid4()),
                origin_endpoint_id=scope["endpoint"], active_conversation_ref=scope["conversation"], title="合成写入")
            return task["task_id"]
        case.new_task = create
        class Central:
            async def call_tool(self, name, args, meta=None):
                if name == "agentbridge_host_identity_profile":
                    payload = {"identity": {"userSubject": "user-a"}, "host": {"hostInstanceId": "office"},
                               "telegramIdentityVerified": True}
                elif name == "agentbridge_host_task_snapshot":
                    payload = service.get_host_task_snapshot(user_subject="user-a", **args)
                elif name == "agentbridge_task_cancel":
                    payload = service.cancel_unsubmitted_task(user_subject="user-a", **args)
                else:
                    raise AssertionError(name)
                return SimpleNamespace(isError=False, structuredContent=payload)
        @asynccontextmanager
        async def opened(*args, **kwargs):
            yield Central(), {"agentHost":"nanobot", "hostInstanceId":"office"}, "L3"
        monkeypatch.setattr(telegram_tasks, "open_host_session", opened)
        monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(web=SimpleNamespace(
            HTTPForbidden=PermissionError, HTTPBadRequest=ValueError)))
        host = SimpleNamespace(coordinator=True, instance="office", worker_payload=lambda *_: scope,
                               card_origins=[ORIGIN])
        def stop():
            return asyncio.run(telegram_tasks.navigate(host, {}, {"operation":"stop", "target":{"kind":"task","id":case.task_id}}))
        case.start_atomic()
        assert stop()["state"] == "canceled"
        assert stop()["state"] == "canceled"
        assert service.tasks.get_task(case.task_id, user_subject="user-a")["status"] == "canceled"
        case.start_atomic()
        case.fields()
        assert stop()["state"] == "stop_unconfirmed"
        assert service.field_submissions.get(case.waiting["nextAction"]["inputSubmissionId"])["state"] == "submitted"
        assert case.write.created_payload is None
    finally:
        case.doCleanups()
