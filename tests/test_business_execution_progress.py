"""Real task/operation bindings and content-free live progress, with no writes."""
from dataclasses import replace
import json
from unittest.mock import patch
from uuid import uuid4

from agentbridge.core.business_execution_progress import PENDING_PHASES, pending_progress, record_pending_phase
from agentbridge.core.capability_runtime import CapabilityContext
from tests.test_workspace_business_outcomes import fixture, _case, _approve
from tests.support.business_view_acceptance import OWNER


def running(fixture):
    case = _case(fixture)
    receipt = _approve(fixture, case)["receipt"]
    authorization = fixture.service.write_authorizations.get(receipt["authorizationId"], include_plan=True)
    operation, _ = fixture.service.operations.create(user_subject=OWNER, capability_name="oa.leave.approve",
        capability_version=fixture.service.registry.get("oa.leave.approve").version,
        input_summary={"authorization_id":"PRIVATE_AUTHORIZATION"}, request_id=str(uuid4()))
    fixture.service.operations.mark_running(operation["operation_id"])
    context = CapabilityContext(OWNER, operation["request_id"], operation["operation_id"],
        fixture.service.registry.get("oa.leave.approve"), task_id=case["task_id"])
    return case, authorization, operation, context


def test_real_progress_is_owned_bounded_content_free_and_never_consumes_a_decision(fixture):
    case, authorization, operation, context = running(fixture)
    original = fixture.service.tasks.get_task(case["task_id"], user_subject=OWNER)
    for phase in PENDING_PHASES:
        for _ in range(3):
            record_pending_phase(fixture.service.tasks, context=context, authorization=authorization,
                                 phase=phase, reason="PRIVATE_OPINION")
    view = pending_progress(fixture.service.tasks, user_subject=OWNER, task_id=case["task_id"])
    assert view["phase"] == "verification_completed"
    assert view["observedPhases"] == list(PENDING_PHASES)
    events = [event for event in fixture.service.tasks.list_events(task_id=case["task_id"], user_subject=OWNER)
              if event["event_type"] == "task.business.progress"]
    assert len(events) == len(PENDING_PHASES)
    assert "PRIVATE_" not in json.dumps(events)
    assert all(set(event["payload"]) == {"phase","operationId","requestId","observedAt","operationStartedAt"} for event in events)
    assert fixture.service.tasks.get_task(case["task_id"], user_subject=OWNER)["status"] == original["status"]
    assert fixture.service.write_authorizations.get(authorization["authorization_id"])["state"] == "approved"
    assert fixture.oa.writes == []
    assert pending_progress(fixture.service.tasks, user_subject="someone-else", task_id=case["task_id"]) is None


def test_progress_uses_actual_running_operation_and_terminal_result_wins_even_before_host_link(fixture):
    case, authorization, operation, context = running(fixture)
    for phase in ["waiting_business_page", "final_revalidation", "submission_scheduled", "verifying_result", "request_observed", "verification_completed"]:
        record_pending_phase(fixture.service.tasks, context=context, authorization=authorization, phase=phase)
    detail = fixture.app.task_detail(fixture.account, case["task_id"])
    current = detail["businessExecution"]
    assert current["phase"] == "verifying_result"  # An event is not a success receipt.
    assert current["actualPhase"] == "verification_completed"
    assert current["operationId"] == operation["operation_id"]
    assert current["operationStartedAt"] == operation["created_at"]
    assert current["requestObserved"] is True
    assert "PRIVATE_" not in json.dumps(detail)
    assert detail["businessTiming"]["missingStages"] == []
    fixture.service.operations.mark_unknown(operation["operation_id"], code="RESULT_UNKNOWN", message="PRIVATE_ERROR")
    detail = fixture.app.task_detail(fixture.account, case["task_id"])
    assert detail["businessExecution"]["phase"] == "outcome_unknown"
    assert fixture.oa.writes == []


def test_invalid_binding_and_telemetry_persistence_failure_do_not_change_operation_or_authorization(fixture):
    case, authorization, operation, context = running(fixture)
    for bad_context, bad_authorization in [(replace(context, request_id="wrong"), authorization),
            (replace(context, user_subject="foreign"), authorization),
            (context, {**authorization,"prepare_operation_id":"foreign-operation"})]:
        record_pending_phase(fixture.service.tasks, context=bad_context, authorization=bad_authorization,
                             phase="waiting_business_page")
    with patch.object(fixture.service.tasks, "_append_event", side_effect=RuntimeError("PRIVATE_DATABASE_ERROR")):
        record_pending_phase(fixture.service.tasks, context=context, authorization=authorization, phase="waiting_business_page")
    assert pending_progress(fixture.service.tasks, user_subject=OWNER, task_id=case["task_id"]) is None
    assert fixture.service.operations.get(operation["operation_id"])["status"] == "running"
    assert fixture.service.write_authorizations.get(authorization["authorization_id"])["state"] == "approved"
    assert fixture.oa.writes == []


def test_progress_event_timeline_is_a_closed_safe_projection(fixture):
    case, authorization, operation, context = running(fixture)
    record_pending_phase(fixture.service.tasks, context=context, authorization=authorization, phase="waiting_business_page")
    entries = fixture.app.list_timeline(fixture.account, limit=200)
    event = next(item for item in entries if item["payload"].get("eventType") == "task.business.progress")
    assert set(event["payload"]) == {"eventType","taskId","phase","operationId","requestId","observedAt"}
    assert event["payload"]["operationId"] == operation["operation_id"]


def test_missing_progress_is_visible_without_mistaking_a_prepare_for_the_current_approved_commit(fixture):
    case, authorization, operation, context = running(fixture)
    # Match the real engine's immutable invocation identity; no phase event is
    # available, as if the best-effort diagnostic store had been busy.
    import hashlib
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE operations SET input_hash=? WHERE operation_id=?", (
            hashlib.sha256(json.dumps({"authorization_id":authorization["authorization_id"]},
                ensure_ascii=False,sort_keys=True,separators=(",", ":")).encode()).hexdigest(), operation["operation_id"]))
    detail = fixture.app.task_detail(fixture.account, case["task_id"])
    assert detail["businessExecution"]["operationId"] == operation["operation_id"]
    assert detail["businessExecution"]["phase"] == "awaiting_progress"
    assert "phaseStartedAt" not in detail["businessExecution"]
    assert "waiting_business_page" in detail["businessTiming"]["missingStages"]
    assert fixture.oa.writes == []
