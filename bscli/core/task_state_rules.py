"""Pure task state decisions from operation, interaction, plan and batch facts.

These functions decide only; the task ledger owns identity checks, observations,
transactions, events and notifications. Existing terminal reopening and unknown
outcome rules intentionally differ by observation source.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

TASK_STATUSES = {
    "active",
    "waiting_user",
    "running",
    "succeeded",
    "failed",
    "outcome_unknown",
    "canceled",
    "expired",
    "superseded",
    "partially_succeeded",
}

ACTIVE_TASK_STATUSES = {"active", "waiting_user", "running"}
TERMINAL_TASK_STATUSES = TASK_STATUSES - ACTIVE_TASK_STATUSES
BATCH_STATES = {
    "running",
    "waiting_user",
    "paused",
    "succeeded",
    "partially_succeeded",
    "failed",
    "outcome_unknown",
    "canceled",
    "expired",
}
ACTIVE_BATCH_STATES = {"running", "waiting_user", "paused"}
BATCH_ITEM_STATES = {
    "queued",
    "preparing",
    "waiting_user",
    "succeeded",
    "failed",
    "outcome_unknown",
    "canceled",
    "expired",
    "skipped",
    "superseded",
}
def _task_status_for_operation(status: str) -> str:
    return {
        "pending": "running",
        "running": "running",
        "requires_user_action": "waiting_user",
        "succeeded": "succeeded",
        "failed": "failed",
        "unknown": "outcome_unknown",
    }.get(status, "active")


def _event_type_for_operation(status: str) -> str:
    return {
        "pending": "task.operation.running",
        "running": "task.operation.running",
        "requires_user_action": "task.operation.requires_user_action",
        "succeeded": "task.operation.succeeded",
        "failed": "task.operation.failed",
        "unknown": "task.operation.outcome_unknown",
    }.get(status, "task.operation.updated")


def _task_status_for_interaction(
    state: str,
    *,
    interaction_type: str = "",
    has_current_operation: bool = False,
) -> str:
    if (
        state == "completed"
        and interaction_type == "credential"
        and not has_current_operation
    ):
        return "succeeded"
    return {
        "pending": "waiting_user",
        "processing": "waiting_user",
        "completed": "active",
        "declined": "canceled",
        "expired": "expired",
        "failed": "failed",
        "superseded": "superseded",
    }.get(state, "active")


def _event_type_for_interaction(state: str) -> str:
    return {
        "pending": "task.interaction.waiting",
        "processing": "task.interaction.waiting",
        "completed": "task.interaction.completed",
        "declined": "task.canceled",
        "expired": "task.interaction.expired",
        "failed": "task.interaction.failed",
        "superseded": "task.interaction.superseded",
    }.get(state, "task.interaction.updated")


def _interaction_may_update_task(
    *,
    task: Mapping[str, Any],
    interaction_id: str,
    newly_linked: bool,
) -> bool:
    if task["status"] in TERMINAL_TASK_STATUSES:
        return False
    if newly_linked:
        return True
    return task["current_interaction_id"] == interaction_id


@dataclass(frozen=True)
class TaskObservationDecision:
    status: str
    event_type: str
    update_task: bool
    emit_event: bool
    observation_changed: bool = False


def operation_observation(
    *, task_status: str, operation_status: str,
    batch_state: str | None, newly_linked: bool,
) -> TaskObservationDecision:
    status = _task_status_for_operation(operation_status)
    if batch_state is not None:
        if batch_state in ACTIVE_BATCH_STATES:
            status = "waiting_user" if batch_state in {"waiting_user", "paused"} else "running"
        else:
            status = str(batch_state)
    # Operation observations may reopen an old terminal task. Unknown outcomes
    # alone remain sticky; interaction/plan observations have stricter rules.
    return TaskObservationDecision(
        status=status,
        event_type=_event_type_for_operation(operation_status),
        update_task=task_status != "outcome_unknown" or status == "outcome_unknown",
        emit_event=newly_linked or task_status != status,
    )


def interaction_observation(
    *, task: Mapping[str, Any], interaction_id: str, state: str,
    interaction_type: str, newly_linked: bool, previous_state: str | None,
) -> TaskObservationDecision:
    status = _task_status_for_interaction(
        state, interaction_type=interaction_type,
        has_current_operation=bool(task["current_operation_id"]),
    )
    event_type = _event_type_for_interaction(state)
    changed = newly_linked or previous_state != state
    event_changed = newly_linked or _event_type_for_interaction(str(previous_state or "")) != event_type
    may_update = _interaction_may_update_task(
        task=task, interaction_id=interaction_id, newly_linked=newly_linked,
    )
    return TaskObservationDecision(
        status=status, event_type=event_type,
        update_task=may_update and changed,
        emit_event=may_update and changed and event_changed,
        observation_changed=changed,
    )


def plan_task_status(task_status: str, event_type: str | None = None) -> str | None:
    """Child operations and plan events cannot reopen a terminal parent."""
    if task_status not in ACTIVE_TASK_STATUSES:
        return None
    return "waiting_user" if event_type in {"plan.step.waiting", "plan.authorization.waiting"} else "running"


def terminal_transition(task_status: str, target_status: str) -> str:
    """Return apply/reuse/reject; the ledger keeps its operation-specific errors."""
    if task_status == target_status:
        return "reuse"
    return "apply" if task_status in ACTIVE_TASK_STATUSES else "reject"


def batch_failure_status(item_state: str, succeeded_count: int) -> str:
    if item_state == "outcome_unknown":
        return "outcome_unknown"
    if succeeded_count:
        return "partially_succeeded"
    if item_state in {"canceled", "expired"}:
        return item_state
    return "failed"


def batch_task_status(batch_state: str) -> str:
    return {
        "waiting_user": "waiting_user", "running": "running", "paused": "waiting_user",
        "partially_succeeded": "partially_succeeded", "outcome_unknown": "outcome_unknown",
        "failed": "failed", "canceled": "canceled", "expired": "expired", "succeeded": "succeeded",
    }.get(str(batch_state), "active")


def task_finished_at(status: str, now: str) -> str | None:
    if status not in TASK_STATUSES:
        raise ValueError(f"unsupported task status: {status}")
    return now if status in TERMINAL_TASK_STATUSES else None
