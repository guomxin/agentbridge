"""Bounded, content-free observations of real OA execution boundaries.

Progress is diagnostic: it cannot consume authorization, submit an action or
establish completion. Failure to persist an observation never changes execution.
"""
from datetime import datetime, timezone
import json
import re


PENDING_PHASES = (
    "waiting_business_page", "final_revalidation", "submission_scheduled",
    "submission_invoked", "request_observed", "verifying_result", "verification_completed",
)
_REASONS = {"body_loading", "content_loading", "page_loading", "submit_entry_unavailable",
    "still_pending", "pending_coverage_incomplete", "done_affair_not_found", "verification_timeout"}
EVENT_TYPE = "task.business.progress"


def _time(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:\+00:00|Z)", value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def record_pending_phase(tasks, *, context, authorization, phase, reason=None):
    if phase not in PENDING_PHASES or not context.task_id or not context.spec.name.startswith("oa."):
        return
    try:
        with tasks._connect() as connection:
            connection.execute("PRAGMA busy_timeout=20")
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT task_id,status FROM agent_tasks WHERE task_id=? AND user_subject=?",
                (context.task_id, context.user_subject)).fetchone()
            operation = connection.execute("SELECT request_id,status,capability_name,created_at FROM operations "
                "WHERE operation_id=? AND user_subject=?", (context.operation_id, context.user_subject)).fetchone()
            bound = connection.execute("SELECT 1 FROM task_operations WHERE task_id=? AND user_subject=? AND operation_id=?",
                (context.task_id, context.user_subject, authorization.get("prepare_operation_id"))).fetchone()
            if (task is None or operation is None or bound is None or operation["status"] != "running"
                    or operation["capability_name"] != context.spec.name or operation["request_id"] != context.request_id
                    or task["status"] in {"succeeded", "failed", "outcome_unknown", "canceled", "expired"}):
                return
            causation = f"business-progress:{context.operation_id}:{phase}"
            if connection.execute("SELECT 1 FROM task_events WHERE task_id=? AND user_subject=? AND event_type=? AND causation_ref=?",
                    (context.task_id, context.user_subject, EVENT_TYPE, causation)).fetchone():
                return
            observed = datetime.now(timezone.utc).isoformat()
            payload = {"phase":phase, "operationId":context.operation_id, "requestId":context.request_id,
                "observedAt":observed, "operationStartedAt":operation["created_at"]}
            if context.trace_id:
                payload["traceId"] = context.trace_id
            if reason in _REASONS:
                payload["reason"] = reason
            tasks._append_event(connection, task_id=context.task_id, user_subject=context.user_subject,
                event_type=EVENT_TYPE, payload=payload, causation_ref=causation, created_at=observed)
            connection.execute("UPDATE agent_tasks SET version=version+1,updated_at=? WHERE task_id=? AND user_subject=?",
                (observed, context.task_id, context.user_subject))
    except Exception:
        pass


def pending_progress(tasks, *, user_subject, task_id):
    """Return only the latest observed operation and its bounded safe milestones."""
    with tasks._connect() as connection:
        rows = connection.execute("SELECT payload_json,causation_ref,created_at FROM task_events "
            "WHERE task_id=? AND user_subject=? AND event_type=? ORDER BY rowid DESC LIMIT 64",
            (task_id, user_subject, EVENT_TYPE)).fetchall()
        stages = []
        operation_id = None
        for row in rows:
            try:
                data = json.loads(row["payload_json"])
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            phase, candidate = data.get("phase"), data.get("operationId")
            if phase not in PENDING_PHASES or not isinstance(candidate, str) or row["causation_ref"] != f"business-progress:{candidate}:{phase}":
                continue
            operation = connection.execute("SELECT request_id,user_subject FROM operations WHERE operation_id=?",
                (candidate,)).fetchone()
            observed, started = _time(data.get("observedAt")), _time(data.get("operationStartedAt"))
            if (operation is None or operation["user_subject"] != user_subject or operation["request_id"] != data.get("requestId")
                    or observed is None or started is None):
                continue
            if operation_id is None:
                operation_id = candidate
            if candidate != operation_id or any(stage["phase"] == phase for stage in stages):
                continue
            stages.append({"phase":phase, "observedAt":observed})
            if len(stages) == len(PENDING_PHASES):
                break
        if not stages:
            return None
        latest = stages[0]
        # Request observation can arrive during result verification. Retain that
        # fact without regressing the visible stage from verification to submit.
        visible = next((stage for stage in stages if stage["phase"] in {"verification_completed", "verifying_result"}), latest)
        return {"operationId":operation_id, "requestId":connection.execute(
                "SELECT request_id FROM operations WHERE operation_id=?", (operation_id,)).fetchone()[0],
            "phase":visible["phase"], "phaseStartedAt":visible["observedAt"], "phaseObservedAt":latest["observedAt"],
            "stages":list(reversed(stages)), "observedPhases":[stage["phase"] for stage in reversed(stages)]}
