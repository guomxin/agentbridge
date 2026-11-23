"""Keep each Workspace message, task notification and attachment on its original host."""
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agentbridge.core.endpoint_instances import endpoint_instances
from agentbridge.core.tasks import TaskNotFound
from agentbridge.core.tasks import TaskHubStore
from tests.test_workspace_host_dispatch_routing import workspace


def test_standalone_host_can_claim_only_its_exact_origin_task_event(tmp_path):
    store = TaskHubStore(tmp_path / "standalone.db")
    owner = "standalone-owner"
    def endpoint(key):
        return store.ensure_endpoint(user_subject=owner, agent_host="custom-host", token_id="shared",
            endpoint_key=key, client_type="telegram", external_subject=owner,
            conversation_ref=key, capabilities=["direct_status","timeline_message"])[0]["endpoint_id"]
    original, other = endpoint("first"), endpoint("second")
    task, _ = store.ensure_task(user_subject=owner, agent_host="custom-host", host_task_key="run",
        origin_endpoint_id=original, active_conversation_ref="first", title="Original task")
    store.append_timeline_message(user_subject=owner, source_endpoint_id=original,
                                  message_key="private", role="user", text="private")
    assert store.claim_outbox(user_subject=owner, endpoint_id=other) == []
    deliveries = store.claim_outbox(user_subject=owner, endpoint_id=original)
    assert [(d["payload_type"], d["task_id"]) for d in deliveries] == [("task_event",task["task_id"])]
    with pytest.raises(TaskNotFound):
        store.claim_outbox(user_subject=owner, endpoint_id=original, host_instance_id="foreign")


def register(service, owner, token, instance):
    path = Path(__file__).parents[1] / "schemas/agent-host/v1/test-vectors.json"
    profile = json.loads(path.read_text())["profiles"][0]["value"]
    profile["implementation"]["name"] = "openclaw"
    profile["hostInstanceId"] = instance
    service.negotiate_host(user_subject=owner, token_id=token, profile=profile)


def push_endpoint(service, owner, token, sender):
    return service.tasks.ensure_endpoint(
        user_subject=owner, token_id=token, agent_host="openclaw", client_type="telegram",
        endpoint_key=f"telegram:default:{sender}", external_subject=sender, conversation_ref="private:"+sender,
        capabilities=["direct_status","timeline_message"], route={"channel":"telegram","to":sender},
    )[0]


def test_chat_fanout_tasks_and_model_context_follow_same_instance(workspace):
    app, account = workspace
    owner = account["user_subject"]
    service = app.service
    register(service, owner, "legacy-token", "openclaw-gateway")
    register(service, owner, "second-token", "second-office")
    old = push_endpoint(service, owner, "legacy-token", "501")
    second = push_endpoint(service, owner, "second-token", "502")
    peer = push_endpoint(service, owner, "second-token", "503")
    base = dict(user_subject=owner, role="assistant")
    delivered = service.tasks.append_timeline_message(**base, source_endpoint_id=account["endpoint_id"],
                                                      message_key="old", text="旧工作台内容")[0]
    app.hosts.set_default(account["account_id"], "office", expected_revision=1)
    nano = app.store.current_host(account["account_id"])
    for role in ["user", "assistant"]:
        service.tasks.append_timeline_message(user_subject=owner, role=role, source_endpoint_id=nano["endpoint_id"],
            message_key="nano-"+role, text="Nano 私有内容", payload={"attachments":[{"filename":"私有文件"}]})
    service.tasks.append_timeline_message(**base, source_endpoint_id=second["endpoint_id"],
                                          message_key="second", text="第二实例消息")
    claim = lambda e, instance: service.claim_host_notifications(user_subject=owner, agent_host="openclaw",
        endpoint_key=e["endpoint_key"], host_instance_id=instance)["notifications"]
    assert [r["timeline"]["entryId"] for r in claim(old, "openclaw-gateway")] == [delivered["entry_id"]]
    assert [r["timeline"]["text"] for r in claim(peer, "second-office")] == ["第二实例消息"]
    assert not claim(second, "second-office")
    with pytest.raises(TaskNotFound):
        claim(second, "openclaw-gateway")
    ctx = service.get_host_cross_endpoint_context(user_subject=owner, agent_host="openclaw",
        endpoint_key=peer["endpoint_key"], host_instance_id="second-office")
    assert [entry["text"] for entry in ctx["entries"]] == ["第二实例消息"]
    with pytest.raises(PermissionError):
        service.get_host_cross_endpoint_context(user_subject=owner, agent_host="openclaw",
            endpoint_key=peer["endpoint_key"], host_instance_id="openclaw-gateway")
    task = service.tasks.ensure_task(user_subject=owner, agent_host="openclaw", host_task_key="registered",
        origin_endpoint_id=second["endpoint_id"], active_conversation_ref=second["conversation_ref"], title="第二实例任务")[0]
    with app.store._connect() as db:
        subs = {row[0] for row in db.execute("SELECT endpoint_id FROM task_subscriptions WHERE task_id=?",(task["task_id"],))}
        assert subs == {second["endpoint_id"], peer["endpoint_id"]}


def test_preexisting_foreign_chat_task_and_claimed_delivery_are_never_released(workspace):
    app, account = workspace
    owner = account["user_subject"]
    store = app.service.tasks
    old = push_endpoint(app.service, owner, "legacy-token", "501")
    app.hosts.set_default(account["account_id"], "office", expected_revision=1)
    nano = app.store.current_host(account["account_id"])
    message = store.append_timeline_message(user_subject=owner, role="user", source_endpoint_id=nano["endpoint_id"],
        message_key="existing", text="旧队列的私有消息")[0]
    task = store.ensure_task(user_subject=owner, agent_host="nanobot", host_task_key="existing-task",
        origin_endpoint_id=nano["endpoint_id"], active_conversation_ref=nano["session_key"], title="原任务")[0]
    with app.store._connect() as db:
        assert endpoint_instances(db,owner)[old["endpoint_id"]] == ("openclaw","openclaw-default")
        payload = json.dumps({"source":{"endpointId":nano["endpoint_id"]},"text":"旧队列私有内容"})
        for kind, state in (("timeline_message","pending"),("timeline_message","delivering"),("task_event","deferred"),
                            ("timeline_message","acknowledged")):
            entry_id = message["entry_id"] if state=="pending" else str(uuid4())
            did = str(uuid4())
            db.execute("""INSERT INTO notification_outbox (delivery_id,event_id,task_id,endpoint_id,user_subject,
                payload_type,payload_json,state,attempt_count,next_attempt_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,1,'2000-01-01','2000-01-01','2000-01-01')""",
                (did,entry_id,task["task_id"] if kind=="task_event" else "",old["endpoint_id"],owner,kind,payload,state))
            if state == "delivering":
                claimed_id = did
    assert not store.notification_route_allowed(user_subject=owner, endpoint_id=old["endpoint_id"], delivery_id=claimed_id)
    assert store.claim_outbox(user_subject=owner, endpoint_id=old["endpoint_id"]) == []
    with app.store._connect() as db:
        rows = db.execute("SELECT state,attempt_count FROM notification_outbox WHERE endpoint_id=?", (old["endpoint_id"],)).fetchall()
        assert sorted(tuple(r) for r in rows) == [("acknowledged",1),("failed",1),("failed",1),("failed",1)]
