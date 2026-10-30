"""Persist adapter outcomes without turning normal trusted-card waits into failures."""
from unittest.mock import patch

import pytest

from agentbridge.core.capability_runtime import CapabilityContext, CapabilityRejected, OutcomeUnknown, RequiresUserAction
from tests.authorization_fixtures import authorized_service
from tests.test_central_service import FakeWorker, _unit_missed_punch_preflight


BASE = "http://oa.example.test/seeyon/main.do?method=main"
READ = "oa.workflow.pending.list"
PREPARE = "oa.business_settlement.approval.prepare"
COMMIT = "oa.business_settlement.approve"


@pytest.fixture
def service(tmp_path):
    return authorized_service(home=tmp_path, base_url=BASE,
                              worker_factory=lambda *_: FakeWorker())


def _context(service, capability, *, trace=True):
    spec = service.registry.get(capability)
    operation, _ = service.operations.create(user_subject="user-a", capability_name=capability,
        capability_version=spec.version, input_summary={})
    observed, _ = service.runtime_governance.ensure_trace(user_subject="user-a",
        request_id=operation["request_id"], system_id="oa", capability_name=capability)
    context = CapabilityContext(user_subject="user-a", request_id=operation["request_id"],
        operation_id=operation["operation_id"], spec=spec,
        trace_id=observed["trace_id"] if trace else None)
    return context, observed["trace_id"]


def _adapter_span(service, trace_id):
    spans = [span for span in service.runtime_governance.trace_detail(trace_id)["spans"]
             if span["stage"] == "adapter.request"]
    assert len(spans) == 1
    return spans[0]


@pytest.mark.parametrize("code,capability,boundary", [
    ("FIELD_INPUT_REQUIRED", PREPARE, "B2_INTERACTION_CREATED"),
    ("WRITE_AUTHORIZATION_REQUIRED", PREPARE, "B2_INTERACTION_CREATED"),
    ("LOGIN_REQUIRED", READ, "B1_READ_ONLY"),
    ("SESSION_CHECK_UNAVAILABLE", READ, "B1_READ_ONLY"),
])
def test_typed_interaction_wait_is_persisted_as_waiting(service, code, capability, boundary):
    context, trace_id = _context(service, capability)
    error = RequiresUserAction(code, "Synthetic trusted interaction required.",
                               next_action={"type": "synthetic_interaction"})
    with patch.object(service, "_invoke_adapter_unobserved", side_effect=error) as invoke:
        with pytest.raises(RequiresUserAction) as caught:
            service._invoke_adapter(context=context)
    assert caught.value is error
    invoke.assert_called_once_with(context=context)
    span = _adapter_span(service, trace_id)
    assert span["status"] == "waiting"
    assert span["side_effect_boundary"] == boundary
    assert span["error_code"] == ("SESSION_EXPIRED" if code == "LOGIN_REQUIRED" else code)
    assert span["finished_at"] and span["duration_ms"] >= 0
    assert service.operations.get(context.operation_id)["status"] == "pending"


@pytest.mark.parametrize("error,status,code", [
    (RuntimeError("FIELD_INPUT_REQUIRED"), "failed", "RUNTIME_FAILURE"),
    (CapabilityRejected("FIELD_INPUT_REQUIRED", "Synthetic rejection."), "failed", "FIELD_INPUT_REQUIRED"),
    (CapabilityRejected("LOGIN_REQUIRED", "Synthetic rejection."), "failed", "SESSION_EXPIRED"),
    (TimeoutError("downstream timed out"), "failed", "DOWNSTREAM_TIMEOUT"),
    (OutcomeUnknown("RESULT_UNKNOWN", "Synthetic authoritative result lost."), "unknown", "RESULT_UNKNOWN"),
    (RuntimeError("RESULT_UNKNOWN after commit"), "unknown", "RESULT_UNKNOWN"),
])
def test_noninteraction_failure_and_unknown_keep_their_existing_classification(service, error, status, code):
    context, trace_id = _context(service, COMMIT)
    with patch.object(service, "_invoke_adapter_unobserved", side_effect=error):
        with pytest.raises(type(error)) as caught:
            service._invoke_adapter(context=context)
    assert caught.value is error
    span = _adapter_span(service, trace_id)
    assert span["status"] == status
    assert span["error_code"] == code
    assert span["side_effect_boundary"] == "B4_COMMIT_ATTEMPTED"


@pytest.mark.parametrize("capability,boundary", [
    (READ, "B1_READ_ONLY"), (PREPARE, "B2_INTERACTION_CREATED"), (COMMIT, "B5_VERIFIED"),
])
def test_success_keeps_the_registered_effect_boundary(service, capability, boundary):
    context, trace_id = _context(service, capability)
    result = {"synthetic": True}
    with patch.object(service, "_invoke_adapter_unobserved", return_value=result) as invoke:
        assert service._invoke_adapter(context=context) is result
    invoke.assert_called_once_with(context=context)
    span = _adapter_span(service, trace_id)
    assert span["status"] == "succeeded" and span["error_code"] is None
    assert span["side_effect_boundary"] == boundary


def test_untraced_call_preserves_the_original_interaction_without_creating_a_span(service):
    context, trace_id = _context(service, PREPARE, trace=False)
    error = RequiresUserAction("FIELD_INPUT_REQUIRED", "Synthetic input.", next_action={})
    with patch.object(service, "_invoke_adapter_unobserved", side_effect=error):
        with pytest.raises(RequiresUserAction) as caught:
            service._invoke_adapter(context=context)
    assert caught.value is error
    assert service.runtime_governance.trace_detail(trace_id)["spans"] == []


def _assert_real_wait(service, response, code, boundary):
    assert response["status"] == "requires_user_action" and response["error"]["code"] == code
    span = _adapter_span(service, response["runtimeTraceId"])
    assert span["status"] == "waiting" and span["side_effect_boundary"] == boundary
    assert service.runtime_governance.get_trace(response["runtimeTraceId"])["status"] == "waiting"
    with service.tasks._connect() as db:
        assert db.execute("SELECT count(*) FROM write_authorizations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM business_draft_decisions").fetchone()[0] == 0
    assert service.operations.get(response["operationId"])["next_action"] == response["nextAction"]


def test_actual_login_interaction_and_adapter_span_both_wait(service):
    response = service.invoke(user_subject="user-a", capability_name=READ, arguments={})
    _assert_real_wait(service, response, "LOGIN_REQUIRED", "B1_READ_ONLY")


def test_actual_opinion_field_card_and_adapter_span_both_wait(service):
    session = service.sessions.get_or_create(user_subject="user-a", system_id="oa",
                                             expected_principal_ref="Alice")
    service.sessions.activate(session["session_id"], observed_principal_ref="Alice")
    service.session_states.save(session["session_id"], {"cookies": []})
    with patch("agentbridge.core.write_catalog.preflight_missed_punch_approval",
               new=_unit_missed_punch_preflight):
        response = service.invoke(user_subject="user-a", capability_name="oa.missed_punch.approval.prepare",
                                  arguments={"affair_id": "synthetic-opinion-affair"})
    _assert_real_wait(service, response, "FIELD_INPUT_REQUIRED", "B2_INTERACTION_CREATED")
    field = service.field_submissions.get(response["nextAction"]["inputSubmissionId"])
    assert field["state"] == "pending"
    assert field["form_schema"]["_agentbridge_resume_arguments"] == {"affair_id": "synthetic-opinion-affair"}
