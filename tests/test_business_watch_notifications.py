"""Real watch transactions and Task Hub delivery rows; no downstream IO."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agentbridge.core.business_content_identity import principal_binding
from agentbridge.core.business_watch_notifications import on_watch_change, validate_watch_delivery, watch_task_id
from tests.authorization_fixtures import authorized_service, grant_permissions
from tests.test_workspace import _create_account


@pytest.fixture
def context(tmp_path):
    attempts = []

    def forbidden_worker(*args):
        attempts.append(args)
        raise AssertionError("watch notifications must not read OA")

    service = authorized_service(home=tmp_path, base_url="https://oa.example.test/seeyon/", worker_factory=forbidden_worker)
    clock = [datetime.now(timezone.utc) - timedelta(seconds=5)]
    service.business_watch.store.clock = lambda: clock[0]
    service.business_watch.store.on_change = lambda connection, event, watch: on_watch_change(service, connection, event, watch)
    endpoints, sessions = {}, {}
    for owner, username, principal in (("user-a", "notifyalice", "Alice"), ("user-b", "notifybob", "Bob")):
        _create_account(service, user_subject=owner, username=username, endpoint_key="workspace:" + username)
        session = service.sessions.get_or_create(user_subject=owner, system_id="oa", expected_principal_ref=principal)
        sessions[owner] = service.sessions.activate(session["session_id"], observed_principal_ref=principal)
        for suffix in ("selected", "companion"):
            endpoints[owner + ":" + suffix] = service.tasks.ensure_endpoint(user_subject=owner,
                token_id="synthetic-notify-" + owner, agent_host="openclaw", endpoint_key="telegram:" + owner + ":" + suffix,
                client_type="telegram", external_subject="synthetic-" + owner + suffix,
                conversation_ref="synthetic-conversation-" + owner + suffix, capabilities=["direct_status"])[0]
    return SimpleNamespace(service=service, store=service.business_watch.store, clock=clock,
                           endpoints=endpoints, sessions=sessions, attempts=attempts)


def snapshot(ctx, node="PRIVATE_NODE_INITIAL", *, ended=False):
    return {"workflow_state": "ended" if ended else "in_progress",
            "current_nodes": [] if ended else [{"name": node, "handlers": ["PRIVATE_HANDLER"]}],
            "current_handlers": [] if ended else ["PRIVATE_HANDLER"],
            "observed_at": ctx.clock[0].isoformat(), "unavailable_fields": []}


def create_watch(ctx, *, owner="user-a", selected=True, number=1):
    watch, _ = ctx.store.create(user_subject=owner, principal_ref=principal_binding(ctx.sessions[owner]),
        system_instance=ctx.service.business_watch.system_instance,
        source={"collection": "done", "affair_id": str(81000000000000000 + number),
                "summary_id": str(82000000000000000 + number), "process_id": str(83000000000000000 + number),
                "case_id": str(84000000000000000 + number)}, title="PRIVATE_TITLE_事项",
        endpoints=[ctx.endpoints[owner + ":selected"]["endpoint_id"]] if selected else [],
        allow_mark_read=True, include_private=True)
    return observe(ctx, watch)["watch"]


def observe(ctx, watch, node="PRIVATE_NODE_INITIAL", *, ended=False):
    return ctx.store.apply_observation(watch["watch_id"], user_subject=watch["user_subject"],
        expected_version=watch["version"], snapshot=snapshot(ctx, node, ended=ended), include_private=True)


def deliveries(ctx, *, owner="user-a"):
    return ctx.service.tasks.list_outbox(user_subject=owner, limit=100)


def ledger_counts(ctx, watch_id):
    with sqlite3.connect(ctx.service.db_path) as db:
        task_id = watch_task_id(watch_id)
        return tuple(db.execute(f"SELECT COUNT(*) FROM {table} WHERE task_id=?", (task_id,)).fetchone()[0]
                     for table in ("agent_tasks", "task_events", "user_timeline", "notification_outbox"))


def revoke_permission(ctx, permission):
    grant = ctx.service.user_grants.get("user-a")
    grant_permissions(ctx.service.user_grants, "user-a", set(grant["permissions"]) - {permission})


def test_default_workspace_event_never_subscribes_companions_or_persists_business_text(context, monkeypatch):
    ctx = context

    def forbidden(*_args, **_kwargs):
        raise AssertionError("automatic task companion subscription is forbidden")

    monkeypatch.setattr(ctx.service.tasks, "ensure_task", forbidden)
    monkeypatch.setattr(ctx.service.tasks, "_subscribe_companion_endpoints", forbidden)
    watch = create_watch(ctx, selected=False)
    result = observe(ctx, watch, "PRIVATE_NODE_NEXT")
    task = ctx.service.tasks.get_task(watch_task_id(watch["watch_id"]), user_subject="user-a")
    assert task["status"] == "succeeded" and task["finished_at"]
    assert task["title"] == "业务关注更新" and task["summary"] == {"businessWatchId": watch["watch_id"]}
    assert ledger_counts(ctx, watch["watch_id"]) == (1, 1, 1, 0)
    with sqlite3.connect(ctx.service.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM task_subscriptions WHERE task_id=?", (task["task_id"],)).fetchone()[0] == 0
        rows = [db.execute(f"SELECT payload_json FROM {table} WHERE task_id=?", (task["task_id"],)).fetchall()
                for table in ("task_events", "user_timeline", "notification_outbox")]
        serialized = json.dumps(rows, ensure_ascii=False)
    assert all(private not in serialized for private in ("PRIVATE_TITLE", "PRIVATE_NODE", "PRIVATE_HANDLER", "affair_id", "principal_ref"))
    assert result["event"]["diff"]["nodes"]["after"] == ["PRIVATE_NODE_NEXT"]
    assert ctx.attempts == []


def test_selected_owned_active_external_only_and_ephemeral_authorized_summary(context):
    ctx = context
    watch = create_watch(ctx)
    observe(ctx, watch, "PRIVATE_NODE_NEXT")
    outbox = deliveries(ctx)
    assert len(outbox) == 1 and outbox[0]["endpoint_id"] == ctx.endpoints["user-a:selected"]["endpoint_id"]
    assert deliveries(ctx, owner="user-b") == []
    assert outbox[0]["payload"]["payload"] == {"watchId": watch["watch_id"], "sequence": 1}
    assert "PRIVATE_" not in json.dumps(outbox)
    enriched = validate_watch_delivery(ctx.service, outbox[0])
    assert set(enriched) == {"message", "watchId", "sequence", "eventTypes", "observedAt"}
    assert "PRIVATE_TITLE_事项" in enriched["message"] and "PRIVATE_NODE_NEXT" in enriched["message"]
    assert "PRIVATE_HANDLER" in enriched["message"] and enriched["sequence"] == 1
    assert not {"source", "principal_ref", "system_instance", "last_snapshot"}.intersection(enriched)
    assert "PRIVATE_" not in json.dumps(deliveries(ctx))  # The enriched message was never saved.
    assert ctx.attempts == []


def test_a_b_a_b_changes_and_callback_reentry_have_four_events_not_hash_dedupe(context):
    ctx = context
    watch = create_watch(ctx)
    for node in ("节点A", "节点B", "节点A", "节点B"):
        result = observe(ctx, watch, node)
        watch = result["watch"]
    assert ledger_counts(ctx, watch["watch_id"]) == (1, 4, 4, 4)
    assert [row["payload"]["payload"]["sequence"] for row in deliveries(ctx)] == [1, 2, 3, 4]
    with sqlite3.connect(ctx.service.db_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        on_watch_change(ctx.service, db, result["event"], watch)
    assert ledger_counts(ctx, watch["watch_id"]) == (1, 4, 4, 4)
    assert all(validate_watch_delivery(ctx.service, row) is not None for row in deliveries(ctx))
    assert ctx.attempts == []


def test_finished_check_replay_does_not_duplicate_any_notification(context):
    ctx = context
    watch = create_watch(ctx)
    ctx.clock[0] += timedelta(seconds=3600)
    job = ctx.store.claim_due()[0]
    first = ctx.store.finish_check(job["check_id"], job["lease_token"], snapshot=snapshot(ctx, "新节点"))
    repeated = ctx.store.finish_check(job["check_id"], job["lease_token"], snapshot=snapshot(ctx, "新节点"))
    assert first["status"] == "finished" and repeated["status"] == "discarded"
    assert ledger_counts(ctx, watch["watch_id"]) == (1, 1, 1, 1)


def test_callback_failure_rolls_back_watch_change_task_timeline_and_outbox_together(context):
    ctx = context
    watch = create_watch(ctx)

    def failing(connection, event, private_watch):
        on_watch_change(ctx.service, connection, event, private_watch)
        raise RuntimeError("synthetic rollback after Task Hub enqueue")

    ctx.store.on_change = failing
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        observe(ctx, watch, "回滚节点")
    assert ctx.store.get(watch["watch_id"], user_subject="user-a", include_private=True) == watch
    assert ctx.store.history(watch["watch_id"], user_subject="user-a") == []
    assert ledger_counts(ctx, watch["watch_id"]) == (0, 0, 0, 0)
    with sqlite3.connect(ctx.service.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM task_subscriptions").fetchone()[0] == 0
    ctx.store.on_change = lambda connection, event, private_watch: on_watch_change(ctx.service, connection, event, private_watch)
    assert observe(ctx, watch, "提交节点")["event"]["sequence"] == 1
    assert ledger_counts(ctx, watch["watch_id"]) == (1, 1, 1, 1)


@pytest.mark.parametrize("change", ["read_permission", "watch_permission", "pause", "stop", "consent", "endpoints", "revoked_endpoint",
                                  "expired_session", "principal", "instance", "rules", "expiry"])
def test_current_delivery_boundaries_deny_pending_business_content(context, change):
    ctx = context
    watch = observe(ctx, create_watch(ctx), "PRIVATE_NODE_NEXT")["watch"]
    delivery = ctx.service.tasks.claim_outbox(user_subject="user-a",
        endpoint_id=ctx.endpoints["user-a:selected"]["endpoint_id"])[0]
    assert validate_watch_delivery(ctx.service, delivery) is not None
    if change.endswith("permission"):
        revoke_permission(ctx, "oa.workflow.read" if change == "read_permission" else "oa.workflow.watch")
    elif change in {"pause", "stop", "consent", "endpoints", "rules"}:
        kwargs = {"pause": {"state": "paused"}, "stop": {"state": "stopped"}, "consent": {"allow_mark_read": False},
                  "endpoints": {"endpoints": []}, "rules": {"rules": ["ended"]}}[change]
        ctx.store.update(watch["watch_id"], user_subject="user-a", expected_version=watch["version"], **kwargs)
    elif change == "revoked_endpoint":
        with sqlite3.connect(ctx.service.db_path) as db:
            db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?", (delivery["endpoint_id"],))
    elif change == "expired_session":
        ctx.service.sessions.mark_expired(ctx.sessions["user-a"]["session_id"], "synthetic logout")
    elif change == "principal":
        ctx.service.sessions.rebind_expected_principal(ctx.sessions["user-a"]["session_id"], expected_principal_ref="Carol", reason="synthetic switch")
        ctx.service.sessions.activate(ctx.sessions["user-a"]["session_id"], observed_principal_ref="Carol")
    elif change == "instance":
        ctx.service.adapter.base_url = "https://other-oa.example.test/seeyon/"
    else:
        ctx.clock[0] += timedelta(days=8)
    assert validate_watch_delivery(ctx.service, delivery) is None
    assert "PRIVATE_" not in json.dumps(deliveries(ctx)) and ctx.attempts == []


def test_completed_watch_may_deliver_only_its_final_end_change(context):
    ctx = context
    watch = observe(ctx, create_watch(ctx), "PRIVATE_NODE_NEXT")["watch"]
    watch = observe(ctx, watch, ended=True)["watch"]
    rows = deliveries(ctx)
    ended = next(row for row in rows if row["payload"]["eventType"] == "business_watch.completed")
    old = next(row for row in rows if row["payload"]["eventType"] == "business_watch.changed")
    assert watch["state"] == "completed" and validate_watch_delivery(ctx.service, old) is None
    final = validate_watch_delivery(ctx.service, ended)
    assert final is not None and "流程已结束，不代表付款完成" in final["message"]
    assert "ended" in final["eventTypes"] and final["sequence"] == 2


def test_account_and_reference_tampering_cannot_read_another_watch(context):
    ctx = context
    observe(ctx, create_watch(ctx), "PRIVATE_NODE_NEXT")
    delivery = deliveries(ctx)[0]
    for altered in ({**delivery, "user_subject": "user-b"}, {**delivery, "endpoint_id": ctx.endpoints["user-b:selected"]["endpoint_id"]},
                    {**delivery, "task_id": "not-the-notification-task"}, {**delivery, "payload_type": "timeline_message"},
                    {**delivery, "payload": {**delivery["payload"], "payload": {"watchId": [], "sequence": True}}}):
        assert validate_watch_delivery(ctx.service, altered) is None
    own_b = create_watch(ctx, owner="user-b", number=2)
    observe(ctx, own_b, "另一账号节点")
    assert len(deliveries(ctx, owner="user-b")) == 1 and ctx.attempts == []


def test_revocation_during_summary_lookup_is_rechecked_before_return(context, monkeypatch):
    ctx = context
    observe(ctx, create_watch(ctx), "PRIVATE_NODE_NEXT")
    delivery = deliveries(ctx)[0]
    original = ctx.store.history

    def revoking(*args, **kwargs):
        result = original(*args, **kwargs)
        revoke_permission(ctx, "oa.workflow.watch")
        return result

    monkeypatch.setattr(ctx.store, "history", revoking)
    assert validate_watch_delivery(ctx.service, delivery) is None


def test_delivery_retry_and_wechat_deferral_do_not_reobserve_or_duplicate_changes(context):
    ctx = context
    with sqlite3.connect(ctx.service.db_path) as db:
        db.execute("UPDATE client_endpoints SET client_type='openclaw-weixin' WHERE endpoint_id=?",
                   (ctx.endpoints["user-a:selected"]["endpoint_id"],))
    watch = create_watch(ctx)
    observe(ctx, watch, "PRIVATE_NODE_NEXT")
    endpoint_id = ctx.endpoints["user-a:selected"]["endpoint_id"]
    first = ctx.service.tasks.claim_outbox(user_subject="user-a", endpoint_id=endpoint_id)[0]
    message = validate_watch_delivery(ctx.service, first)["message"]
    retried = ctx.service.tasks.acknowledge_outbox(user_subject="user-a", endpoint_id=endpoint_id,
        delivery_id=first["delivery_id"], succeeded=False, retry_after_seconds=1)
    assert retried["state"] == "pending"
    # Advance only this isolated retry row, without sleeps or a downstream read.
    with sqlite3.connect(ctx.service.db_path) as db:
        db.execute("UPDATE notification_outbox SET next_attempt_at=? WHERE delivery_id=?",
                   (ctx.clock[0].isoformat(), first["delivery_id"]))
    second = ctx.service.tasks.claim_outbox(user_subject="user-a", endpoint_id=endpoint_id)[0]
    assert second["delivery_id"] == first["delivery_id"] and second["attempt_count"] == 2
    assert validate_watch_delivery(ctx.service, second)["message"] == message
    deferred = ctx.service.tasks.acknowledge_outbox(user_subject="user-a", endpoint_id=endpoint_id,
        delivery_id=first["delivery_id"], succeeded=False, defer_until_activity=True)
    assert deferred["state"] == "deferred" and ctx.service.tasks.claim_outbox(user_subject="user-a", endpoint_id=endpoint_id) == []
    assert ctx.service.tasks.reactivate_deferred_outbox(user_subject="user-a", endpoint_id=endpoint_id, delay_seconds=0) == 1
    resumed = ctx.service.tasks.claim_outbox(user_subject="user-a", endpoint_id=endpoint_id)[0]
    assert validate_watch_delivery(ctx.service, resumed)["message"] == message
    with sqlite3.connect(ctx.service.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM business_watch_observations WHERE watch_id=?", (watch["watch_id"],)).fetchone()[0] == 2
    assert ledger_counts(ctx, watch["watch_id"]) == (1, 1, 1, 1) and ctx.attempts == []


def test_without_live_endpoints_only_watch_history_survives_and_new_changes_resume(context):
    ctx = context
    watch = create_watch(ctx, selected=False)
    with sqlite3.connect(ctx.service.db_path) as db:
        db.execute("UPDATE client_endpoints SET state='revoked' WHERE user_subject='user-a'")
    watch = observe(ctx, watch, "无端点期间节点")["watch"]
    assert len(ctx.store.history(watch["watch_id"], user_subject="user-a")) == 1
    assert ledger_counts(ctx, watch["watch_id"]) == (0, 0, 0, 0)
    ctx.service.tasks.ensure_endpoint(user_subject="user-a", token_id="synthetic-returned-endpoint", agent_host="openclaw",
        endpoint_key="returned-workspace", client_type="web", external_subject="synthetic-returned-owner",
        conversation_ref="synthetic-returned-conversation")
    observe(ctx, watch, "重新绑定后的节点")
    assert ledger_counts(ctx, watch["watch_id"]) == (1, 1, 1, 0)


@pytest.mark.parametrize("scope,target", [("global", "*"), ("system", "oa"), ("user", "user-a")])
def test_maintenance_observation_controls_suppress_pending_content(context, scope, target):
    ctx = context
    observe(ctx, create_watch(ctx), "PRIVATE_NODE_NEXT")
    delivery = deliveries(ctx)[0]
    assert validate_watch_delivery(ctx.service, delivery) is not None
    ctx.service.business_watch.controls.set(scope=scope, target=target, paused=True,
        expected_version=0, reason="synthetic maintenance", actor="synthetic-operator")
    assert validate_watch_delivery(ctx.service, delivery) is None and ctx.attempts == []
