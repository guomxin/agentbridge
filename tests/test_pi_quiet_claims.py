"""Only authenticated Pi claims may acknowledge authoritative, non-display task events."""
import asyncio
from datetime import datetime, timedelta, timezone
import threading
from uuid import uuid4

import pytest

from agentbridge.core import actionable_results
from agentbridge.core.telegram_result_views import ResultViewService
from agentbridge.core.workspace_result_views import delivery
from agentbridge.mcp.central import _claim_host_notifications_with_wait
from tests.test_pi_workspace_result_views import paired_query, pi  # noqa: F401 (shared fixture)


@pytest.fixture
def queue(pi):
    service = pi.service
    cards, args, web_task, operation, target, links, pair = paired_query(pi)
    scope = {"user_subject": "user-a", "agent_host": "pi", "host_instance_id": "pi-office",
             "endpoint_key": target["endpointKey"], "include_workspace_chat": True, "include_workspace_result_views": True}
    opt = {"quiet_pairing_ref": args["pairing_ref"], "quiet_token_id": args["token_id"]}
    # Create a fixed private-origin task via the genuine paired button, without executing any business command.
    first, = cards
    view = delivery(service, action="issue", **args, delivery_id=first["deliveryId"], attempt_count=first["attemptCount"])
    view_token = view["resultView"]["presentation"]["url"].rsplit("/", 1)[1]
    views = ResultViewService(service)
    item = views.read(view_token)["groups"][0]["targets"][0]
    tg_task = views.prepare(view_token, {"targetKeys": [item["targetKey"]], "operationId": operation,
                                        "requestKey": uuid4().hex})["taskId"]
    return service, scope, opt, web_task, tg_task, target, links, pair


def append(service, task, event, *, at=None):
    with service.tasks._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        return service.tasks._append_event(db, task_id=task, user_subject="user-a", event_type=event,
            payload={"reason": "synthetic"}, causation_ref=uuid4().hex, created_at=(at or datetime.now(timezone.utc)).isoformat())


def test_opt_in_mixed_sources_preserves_order_fences_and_protected_classes(queue):
    svc, scope, opt, web, tg, target, *_ = queue
    for task in (web, tg):
        svc.runtime_governance.ensure_trace(task_id=task, user_subject="user-a", host_type="pi", host_instance_id="pi-office")
    with svc.tasks._connect() as db:
        # Original fixture's first claim is still delivering, not available to this consumer.
        held = {r[0] for r in db.execute("SELECT delivery_id FROM notification_outbox WHERE state='delivering'")}
    for task in (web, tg):
        for event in ("task.created", "task.operation.linked", "task.operation.running", "task.interaction.completed"):
            append(svc, task, event)
    important = [append(svc, web, e) for e in
                 ("task.interaction.waiting", "task.operation.outcome_unknown", "task.operation.succeeded", "task.completed")]
    # A similar-looking task envelope must not mask its event of record.
    tampered = append(svc, web, "task.operation.failed")
    with svc.tasks._connect() as db:
        db.execute("UPDATE notification_outbox SET payload_json=json_set(payload_json,'$.eventType','task.created') WHERE event_id=?", (tampered,))
    result = svc.claim_host_notifications(**scope, **opt, limit=100)
    assert result["quietAcknowledged"] == 9  # includes the TG command's original task.created
    with svc.tasks._connect() as db:
        quiet = db.execute("SELECT * FROM notification_outbox WHERE state='acknowledged' AND endpoint_id=? ORDER BY created_at,rowid",
                           (target["endpointId"],)).fetchall()
        assert len(quiet) == 9 and {r["task_id"] for r in quiet} == {web, tg}
        assert all(r["attempt_count"] == 1 and r["acknowledged_at"] for r in quiet)
        assert not held.intersection(r["delivery_id"] for r in quiet)
        for r in quiet:
            assert {s[0] for s in db.execute("SELECT stage FROM runtime_spans WHERE delivery_id=?", (r["delivery_id"],))} == {
                "delivery.claimed", "delivery.result"}
        returned = [svc.tasks.get_delivery(n["deliveryId"]) for n in result["notifications"]]
        assert [r["event_id"] for r in returned if r["event_id"] in important] == important
        assert tampered in [r["event_id"] for r in returned]
        assert all(r["state"] == "delivering" for r in returned)
        assert any(n["deliveryMode"] == "prepared_command" for n in result["notifications"])
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    assert svc.claim_host_notifications(**scope, **opt)["quietAcknowledged"] == 0


@pytest.mark.parametrize("change", ["wrong_token", "stale_pair", "wrong_instance", "other_subject", "rebound"])
def test_wrong_scope_cannot_retire_or_claim_attempt(queue, change):
    svc, scope, opt, web, _, target, links, pair = queue
    quiet = append(svc, web, "task.operation.running")
    if change == "wrong_token":
        opt["quiet_token_id"] = "foreign"
    elif change == "stale_pair":
        opt["quiet_pairing_ref"] = "e" * 64
    elif change == "wrong_instance":
        scope["host_instance_id"] = "pi-foreign"
    elif change == "other_subject":
        scope["user_subject"] = "user-b"
    else:
        with svc.tasks._connect() as db:
            account = db.execute("SELECT account_id FROM workspace_accounts WHERE user_subject='user-a'").fetchone()[0]
        links.revoke(account, "pi-office", "8801")
        assert pair()["pairingRef"] != opt["quiet_pairing_ref"]
    with pytest.raises((PermissionError, KeyError, actionable_results.ActionableResultError)):
        svc.claim_host_notifications(**scope, **opt)
    with svc.tasks._connect() as db:
        assert tuple(db.execute("SELECT state,attempt_count FROM notification_outbox WHERE event_id=? AND endpoint_id=?",
                               (quiet, target["endpointId"])).fetchone()) == ("pending", 0)


def test_no_opt_in_keeps_original_ack_and_bounded_quiet_page_drain(queue):
    svc, scope, opt, web, *_ = queue
    # Clear preceding items via the legacy projection. Original consumers still see task.created.
    normal = svc.claim_host_notifications(**scope, limit=100)
    assert "quietAcknowledged" not in normal and any(n["event"]["eventType"] == "task.created" for n in normal["notifications"])
    for _ in range(7):
        append(svc, web, "task.operation.linked")
    visible = append(svc, web, "task.operation.failed")
    async def claim():
        return await _claim_host_notifications_with_wait(svc, **scope, **opt, limit=2, wait_seconds=0)
    first = asyncio.run(claim())
    assert first["quietAcknowledged"] == 6 and first["count"] == 0
    second = asyncio.run(claim())
    assert second["quietAcknowledged"] == 1 and second["count"] == 1
    assert svc.tasks.get_delivery(second["notifications"][0]["deliveryId"])["event_id"] == visible


def test_reclaim_preserves_attempt_count_and_new_pair_cannot_inline_old_sources(queue):
    svc, scope, opt, web, _, target, links, pair = queue
    svc.claim_host_notifications(**scope, limit=100)
    event = append(svc, web, "task.operation.linked")
    first, = svc.claim_host_notifications(**scope)["notifications"]
    assert first["event"]["eventId"] == event and first["attemptCount"] == 1
    assert svc.claim_host_notifications(**scope, **opt)["quietAcknowledged"] == 0
    key = first["deliveryId"]
    with svc.tasks._connect() as db:
        db.execute("UPDATE notification_outbox SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE delivery_id=?", (key,))
    assert svc.claim_host_notifications(**scope, **opt)["quietAcknowledged"] == 1
    assert svc.tasks.get_delivery(key)["attempt_count"] == 2
    svc.acknowledge_host_notification(user_subject=scope["user_subject"], agent_host="pi", endpoint_key=scope["endpoint_key"],
        host_instance_id="pi-office", delivery_id=key, attempt_count=1, succeeded=False)
    assert svc.tasks.get_delivery(key)["state"] == "acknowledged"
    stale = append(svc, web, "task.operation.running")
    with svc.tasks._connect() as db:
        account = db.execute("SELECT account_id FROM workspace_accounts WHERE user_subject='user-a'").fetchone()[0]
    links.revoke(account, "pi-office", "8801")
    opt["quiet_pairing_ref"] = pair()["pairingRef"]
    response = svc.claim_host_notifications(**scope, **opt)
    assert response["quietAcknowledged"] == 0
    assert any(item.get("event", {}).get("eventId") == stale for item in response["notifications"])


def test_full_32_quiet_page_immediately_reaches_the_next_genuine_result_card(queue, monkeypatch):
    from agentbridge.mcp import central
    svc, scope, opt, web, *_ = queue
    svc.claim_host_notifications(**scope, limit=100)
    with svc.tasks._connect() as db:
        card = db.execute("SELECT delivery_id,created_at FROM notification_outbox WHERE payload_type='workspace_result_view'").fetchone()
        db.execute("UPDATE notification_outbox SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE delivery_id=?", (card[0],))
    for offset in range(32, 0, -1):
        append(svc, web, "task.operation.linked", at=datetime.fromisoformat(card[1]) - timedelta(microseconds=offset))
    async def unexpected_wait(*args, **kwargs):
        pytest.fail("Slept with a ready result card behind the quiet page")
    monkeypatch.setattr(central, "wait_for_change_async", unexpected_wait)
    result = asyncio.run(_claim_host_notifications_with_wait(svc, **scope, **opt, limit=32, wait_seconds=15))
    assert result["quietAcknowledged"] == 32
    current, = result["notifications"]
    assert (current["deliveryId"], current["deliveryMode"], current["attemptCount"]) == (card[0], "workspace_result_view", 2)


def test_pair_replaced_during_wait_cannot_claim_newly_available_event(queue, monkeypatch):
    svc, scope, opt, web, _, target, links, pair = queue
    svc.claim_host_notifications(**scope, limit=100)
    checked = threading.Event()
    original = svc.claim_host_notifications
    def observed(**params):
        response = original(**params)
        checked.set()
        return response
    monkeypatch.setattr(svc, "claim_host_notifications", observed)
    async def run():
        waiting = asyncio.create_task(_claim_host_notifications_with_wait(svc, **scope, **opt, limit=32, wait_seconds=5))
        assert await asyncio.to_thread(checked.wait, 2)
        with svc.tasks._connect() as db:
            account = db.execute("SELECT account_id FROM workspace_accounts WHERE user_subject='user-a'").fetchone()[0]
        links.revoke(account, "pi-office", "8801")
        assert pair()["pairingRef"] != opt["quiet_pairing_ref"]
        event = append(svc, web, "task.operation.running")
        with pytest.raises(PermissionError):
            await waiting
        with svc.tasks._connect() as db:
            assert tuple(db.execute("SELECT state,attempt_count FROM notification_outbox WHERE endpoint_id=? AND event_id=?",
                                   (target["endpointId"], event)).fetchone()) == ("pending", 0)
    asyncio.run(run())


@pytest.mark.parametrize("interrupted_stage", ["delivery.claimed", "delivery.result"])
def test_quiet_audit_and_mixed_page_roll_back_together(queue, interrupted_stage):
    import sqlite3

    svc, scope, opt, web, _, target, *_ = queue
    svc.claim_host_notifications(**scope, limit=100)
    svc.runtime_governance.ensure_trace(task_id=web, user_subject="user-a", host_type="pi", host_instance_id="pi-office")
    events = [append(svc, web, kind) for kind in ("task.created", "task.completed", "task.operation.running")]
    endpoint = target["endpointId"]
    with svc.tasks._connect() as db:
        original = [db.execute("SELECT delivery_id FROM notification_outbox WHERE event_id=? AND endpoint_id=?",
                               (event, endpoint)).fetchone()[0] for event in events]
        # Fail on the last quiet row, after another quiet row and a visible claim
        # were already processed. Both trigger arguments are fixture constants/UUIDs.
        db.execute(f"""CREATE TRIGGER interrupt_audit BEFORE INSERT ON runtime_spans
            WHEN NEW.stage='{interrupted_stage}' AND NEW.delivery_id='{original[-1]}'
            BEGIN SELECT RAISE(ABORT,'interrupted quiet audit'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="interrupted quiet audit"):
        svc.claim_host_notifications(**scope, **opt, limit=32)
    with svc.tasks._connect() as db:
        for delivery in original:
            assert tuple(db.execute("SELECT state,attempt_count,acknowledged_at FROM notification_outbox WHERE delivery_id=?",
                                    (delivery,)).fetchone()) == ("pending", 0, None)
            assert not db.execute("SELECT 1 FROM runtime_spans WHERE delivery_id=?", (delivery,)).fetchone()
        db.execute("DROP TRIGGER interrupt_audit")
    response = svc.claim_host_notifications(**scope, **opt, limit=32)
    assert response["quietAcknowledged"] == 2
    assert [item["deliveryId"] for item in response["notifications"]] == [original[1]]
    assert svc.claim_host_notifications(**scope, **opt)["quietAcknowledged"] == 0
    with svc.tasks._connect() as db:
        for delivery in (original[0], original[2]):
            spans = db.execute("SELECT stage,attempt,status FROM runtime_spans WHERE delivery_id=? ORDER BY stage", (delivery,))
            assert list(map(tuple, spans)) == [("delivery.claimed", 1, "succeeded"), ("delivery.result", 1, "succeeded")]


def test_quiet_success_retains_previous_attempt_and_its_failed_delivery_receipt(queue):
    svc, scope, opt, web, *_ = queue
    svc.claim_host_notifications(**scope, limit=100)
    svc.runtime_governance.ensure_trace(task_id=web, user_subject="user-a", host_type="pi", host_instance_id="pi-office")
    append(svc, web, "task.operation.running")
    item, = svc.claim_host_notifications(**scope)["notifications"]
    key = item["deliveryId"]
    route = {k: scope[k] for k in ("user_subject", "agent_host", "endpoint_key", "host_instance_id")}
    svc.acknowledge_host_notification(**route, delivery_id=key, attempt_count=1, succeeded=False, retry_after_seconds=1)
    with svc.tasks._connect() as db:
        db.execute("UPDATE notification_outbox SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE delivery_id=?", (key,))
    assert svc.claim_host_notifications(**scope, **opt)["quietAcknowledged"] == 1
    with svc.tasks._connect() as db:
        rows = db.execute("""SELECT stage,attempt,json_extract(metadata_json,'$.attemptCount'),status
            FROM runtime_spans WHERE delivery_id=? ORDER BY attempt,stage""", (key,))
        assert list(map(tuple, rows)) == [
            ("delivery.claimed", 1, 1, "succeeded"), ("delivery.result", 1, 1, "waiting"),
            ("delivery.claimed", 2, 2, "succeeded"), ("delivery.result", 2, 2, "succeeded"),
        ]
