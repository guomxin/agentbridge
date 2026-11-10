"""Attempt-fenced watch ACKs use real isolated outbox rows, never OA IO."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest

from agentbridge.core.tasks import TaskHubStore
from tests.test_business_watch_notifications import context, create_watch, observe, revoke_permission


def prepare(ctx):
    observe(ctx, create_watch(ctx), "合成竞态新节点")
    endpoint = ctx.endpoints["user-a:selected"]
    parameters = dict(user_subject="user-a", agent_host="openclaw", endpoint_key=endpoint["endpoint_key"])
    return parameters, ctx.service.claim_host_notifications(**parameters)["notifications"][0]


def expire(ctx, delivery_id):
    with sqlite3.connect(ctx.service.db_path) as db:
        db.execute("UPDATE notification_outbox SET next_attempt_at=? WHERE delivery_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), delivery_id))


@pytest.mark.parametrize("succeeded,defer", [(True, False), (False, False), (False, True)])
def test_store_old_attempt_cannot_ack_retry_or_defer_new_attempt(context, succeeded, defer):
    ctx = context
    parameters, first = prepare(ctx)
    expire(ctx, first["deliveryId"])
    second = ctx.service.claim_host_notifications(**parameters)["notifications"][0]
    assert second["attemptCount"] == first["attemptCount"] + 1
    before = ctx.service.tasks.get_delivery(first["deliveryId"])
    result = ctx.service.tasks.acknowledge_outbox(user_subject="user-a", endpoint_id=before["endpoint_id"],
        delivery_id=before["delivery_id"], succeeded=succeeded, defer_until_activity=defer,
        expected_attempt_count=first["attemptCount"])
    assert result == before == ctx.service.tasks.get_delivery(first["deliveryId"])
    assert before["state"] == "delivering" and ctx.attempts == []


def test_central_old_ack_does_not_change_new_lease_then_current_attempt_can_ack(context):
    ctx = context
    parameters, first = prepare(ctx)
    expire(ctx, first["deliveryId"])
    second = ctx.service.claim_host_notifications(**parameters)["notifications"][0]
    before = ctx.service.tasks.get_delivery(first["deliveryId"])
    assert ctx.service.validate_host_notification(**parameters, delivery_id=first["deliveryId"],
        attempt_count=first["attemptCount"]) == {"allowed": False, "stale": True}
    obsolete = ctx.service.acknowledge_host_notification(**parameters, delivery_id=first["deliveryId"],
        succeeded=True, attempt_count=first["attemptCount"])
    assert obsolete["status"] == "succeeded" and obsolete["delivery"]["state"] == "delivering"
    assert obsolete["delivery"]["attemptCount"] == second["attemptCount"]
    assert ctx.service.tasks.get_delivery(first["deliveryId"]) == before
    current = ctx.service.acknowledge_host_notification(**parameters, delivery_id=second["deliveryId"],
        succeeded=True, attempt_count=second["attemptCount"])
    assert current["delivery"]["state"] == "acknowledged" and ctx.attempts == []


def test_expired_attempt_never_changes_row_even_before_replacement_claim(context):
    ctx = context
    parameters, first = prepare(ctx)
    expire(ctx, first["deliveryId"])
    before = ctx.service.tasks.get_delivery(first["deliveryId"])
    assert ctx.service.validate_host_notification(**parameters, delivery_id=first["deliveryId"],
        attempt_count=first["attemptCount"]) == {"allowed": False, "stale": True}
    result = ctx.service.acknowledge_host_notification(**parameters, delivery_id=first["deliveryId"],
        succeeded=False, attempt_count=first["attemptCount"])
    assert result["delivery"]["state"] == "delivering"
    assert ctx.service.tasks.get_delivery(first["deliveryId"]) == before
    assert ctx.service.claim_host_notifications(**parameters)["notifications"][0]["attemptCount"] == first["attemptCount"] + 1


@pytest.mark.parametrize("attempt", [None, True, 1.0, "1", 0, 6])
def test_watch_ack_requires_a_valid_attempt_and_preserves_row_on_invalid_input(context, attempt):
    ctx = context
    parameters, first = prepare(ctx)
    before = ctx.service.tasks.get_delivery(first["deliveryId"])
    with pytest.raises(ValueError, match="current attempt count"):
        ctx.service.acknowledge_host_notification(**parameters, delivery_id=first["deliveryId"],
            succeeded=True, attempt_count=attempt)
    assert ctx.service.tasks.get_delivery(first["deliveryId"]) == before


def test_duplicate_ack_of_retry_pending_attempt_does_not_cancel_pending_retry(context):
    ctx = context
    parameters, first = prepare(ctx)
    ctx.service.acknowledge_host_notification(**parameters, delivery_id=first["deliveryId"], succeeded=False,
        attempt_count=first["attemptCount"], retry_after_seconds=1)
    before = ctx.service.tasks.get_delivery(first["deliveryId"])
    assert before["state"] == "pending"
    ctx.service.acknowledge_host_notification(**parameters, delivery_id=first["deliveryId"], succeeded=True,
        attempt_count=first["attemptCount"])
    assert ctx.service.tasks.get_delivery(first["deliveryId"]) == before


def test_current_attempt_revocation_can_cancel_but_cannot_send_content(context):
    ctx = context
    parameters, first = prepare(ctx)
    revoke_permission(ctx, "oa.workflow.watch")
    assert ctx.service.validate_host_notification(**parameters, delivery_id=first["deliveryId"],
        attempt_count=first["attemptCount"]) == {"allowed": False}
    result = ctx.service.acknowledge_host_notification(**parameters, delivery_id=first["deliveryId"], succeeded=False,
        attempt_count=first["attemptCount"])
    assert result["delivery"]["state"] == "acknowledged" and ctx.attempts == []


def test_invalid_claim_cleanup_is_fenced_if_another_worker_reclaimed_during_validation(context, monkeypatch):
    ctx = context
    observe(ctx, create_watch(ctx), "合成竞态新节点")
    endpoint = ctx.endpoints["user-a:selected"]
    from agentbridge.core import business_watch_notifications as notifications
    original = notifications.validate_watch_delivery
    replacement = []

    def revoking(service, delivery):
        expire(ctx, delivery["delivery_id"])
        replacement.extend(service.tasks.claim_outbox(user_subject="user-a", endpoint_id=endpoint["endpoint_id"]))
        revoke_permission(ctx, "oa.workflow.watch")
        return original(service, delivery)

    monkeypatch.setattr(notifications, "validate_watch_delivery", revoking)
    assert ctx.service.claim_host_notifications(user_subject="user-a", agent_host="openclaw",
        endpoint_key=endpoint["endpoint_key"])["notifications"] == []
    assert len(replacement) == 1 and replacement[0]["attempt_count"] == 2
    assert ctx.service.tasks.get_delivery(replacement[0]["delivery_id"]) == replacement[0]


def test_real_threads_with_stale_ack_cannot_reschedule_a_concurrent_current_ack(context):
    ctx = context
    parameters, first = prepare(ctx)
    expire(ctx, first["deliveryId"])
    second = ctx.service.claim_host_notifications(**parameters)["notifications"][0]
    row = ctx.service.tasks.get_delivery(first["deliveryId"])
    stores = [TaskHubStore(ctx.service.db_path, maintain_on_startup=False) for _ in range(8)]
    barrier = threading.Barrier(8)

    def acknowledging(index):
        barrier.wait(timeout=10)
        return stores[index].acknowledge_outbox(user_subject="user-a", endpoint_id=row["endpoint_id"],
            delivery_id=row["delivery_id"], succeeded=index == 0,
            expected_attempt_count=second["attemptCount"] if index == 0 else first["attemptCount"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(acknowledging, range(8)))
    actual = ctx.service.tasks.get_delivery(first["deliveryId"])
    assert actual["state"] == "acknowledged" and actual["attempt_count"] == second["attemptCount"]
    assert ctx.attempts == []


def test_ordinary_task_notification_still_acknowledges_without_attempt_parameter(context):
    ctx = context
    endpoint = ctx.endpoints["user-a:selected"]
    ctx.service.tasks.ensure_task(user_subject="user-a", agent_host="openclaw", host_task_key="synthetic-legacy-task",
        origin_endpoint_id=endpoint["endpoint_id"], active_conversation_ref=endpoint["conversation_ref"], title="Legacy task")
    parameters = dict(user_subject="user-a", agent_host="openclaw", endpoint_key=endpoint["endpoint_key"])
    notification = ctx.service.claim_host_notifications(**parameters)["notifications"][0]
    assert notification["deliveryMode"] == "origin_handled"
    result = ctx.service.acknowledge_host_notification(**parameters, delivery_id=notification["deliveryId"], succeeded=True)
    assert result["delivery"]["state"] == "acknowledged" and ctx.attempts == []
