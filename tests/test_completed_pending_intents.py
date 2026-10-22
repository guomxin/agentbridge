"""A verified OA completion retires only unused copies of its exact intent."""
from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

import pytest

from agentbridge.adapters.seeyon_pending_actions import PendingActionContractMismatch
from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from agentbridge.core.oa_pending_reconciliation import reconcile_completed_pending_intents
from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract, OWNER
from tests.support.telegram_result_acceptance import add_reward_affair


PREPARE = "oa.intellectual_property_reward.acknowledgement.prepare"
COMMIT = "oa.intellectual_property_reward.acknowledge"


@pytest.fixture
def run(tmp_path):
    fixture = LatencyAcceptanceFixture(tmp_path, seed=False,
        latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
    affair = add_reward_affair(fixture)
    yield fixture, affair
    fixture.close()


def source(run, *, affair=None, batch=False):
    fixture, default = run
    service = fixture.service
    task, _ = service.tasks.ensure_task(user_subject=OWNER, agent_host="openclaw", host_task_key=uuid4().hex,
        origin_endpoint_id=fixture.account["endpoint_id"], active_conversation_ref=fixture.account["openclaw_session_key"],
        title="合成奖励知会核对")
    result = service.invoke(user_subject=OWNER, task_id=task["task_id"],
        capability_name="oa.workflow.pending.batch.prepare" if batch else PREPARE,
        arguments={"affair_ids": [affair or default]} if batch else {"affair_id": affair or default},
        idempotency_key=uuid4().hex)
    assert result["status"] == "requires_user_action", result
    return {"task": task["task_id"], "input": result["nextAction"]["inputSubmissionId"],
            "interaction": result["interaction"]["interactionId"]}


def ready(service, case):
    drafts = BusinessDraftService(service)
    opened = drafts.open(case["input"])
    credentials = dict(source_submission_id=case["input"], csrf_token=opened["csrfToken"], csrf_cookie=opened["csrfToken"])
    saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"], values={"opinion": "已核对合成奖励材料"})
    preview = drafts.preview(**credentials, expected_revision=saved["revision"])
    return drafts, credentials, preview


def approve(drafts, credentials, preview):
    return drafts.approve(**credentials, expected_revision=preview["revision"],
        preview_id=preview["preview"]["previewId"], request_key=uuid4().hex)


def complete(service, case):
    drafts, credentials, preview = ready(service, case)
    receipt = approve(drafts, credentials, preview)["receipt"]
    result = service.resume_interaction(user_subject=OWNER, interaction_id=receipt["interactionId"], idempotency_key=uuid4().hex)
    authorization = service.write_authorizations.get(receipt["authorizationId"])
    return result, service.operations.get(authorization["commit_operation_id"] or result["operationId"])


def detached_source(run, *, subject=OWNER, system="oa", session=None, capability=PREPARE):
    """Create a valid local card contract without touching another OA account."""
    fixture, affair = run
    service = fixture.service
    template = service.field_submissions.get(source(run)["input"])
    conversation = "agent:main:synthetic:" + uuid4().hex
    endpoint, _ = service.tasks.ensure_endpoint(user_subject=subject, token_id="synthetic-token-" + uuid4().hex,
        agent_host="openclaw", endpoint_key="synthetic:" + uuid4().hex, client_type="web",
        external_subject=subject, conversation_ref=conversation)
    task, _ = service.tasks.ensure_task(user_subject=subject, agent_host="openclaw", host_task_key=uuid4().hex,
        origin_endpoint_id=endpoint["endpoint_id"], active_conversation_ref=conversation,
        title="合成独立授权范围")
    spec = service.registry.get(capability)
    operation, _ = service.operations.create(user_subject=subject, capability_name=capability,
        capability_version=spec.version, input_summary={"affair_id": affair})
    schema = {**deepcopy(_TRUSTED_WRITE_DEFINITIONS[capability]["field_schema"]),
              "_agentbridge_resume_arguments": {"affair_id": affair}}
    submission = service.field_submissions.create(user_subject=subject, system_id=system,
        session_id=session or template["session_id"], capability_name=capability, capability_version=spec.version,
        create_operation_id=operation["operation_id"], supersession_key=uuid4().hex, form_schema=schema,
        card_base_url=service.trusted_card_base_url)
    envelope = service._business_input_interaction(submission)
    operation = service.operations.mark_requires_user_action(operation["operation_id"], code="FIELD_INPUT_REQUIRED",
        message="合成独立表单", next_action={"interaction": envelope})
    service.tasks.link_operation(task_id=task["task_id"], user_subject=subject, operation=operation)
    service.tasks.link_interaction(task_id=task["task_id"], user_subject=subject,
        interaction_record=service.interactions.get(envelope["interactionId"], user_subject=subject), interaction=envelope)
    return {"task": task["task_id"], "input": submission["submission_id"], "interaction": envelope["interactionId"]}


def pending(service, case):
    assert service.field_submissions.get(case["input"])["state"] == "pending"
    assert service.tasks.get_task(case["task"], user_subject=OWNER)["status"] == "waiting_user"


def test_verified_completion_retires_old_ready_card_and_all_entry_sessions(run):
    fixture, _ = run
    service = fixture.service
    old, winner = source(run), source(run)
    drafts, credentials, preview = ready(service, old)
    presentation = service.field_submissions.create_presentation(old["input"], user_subject=OWNER,
        endpoint_id=fixture.account["endpoint_id"])
    assert service.field_submissions.get(old["input"])["supersession_key"] != service.field_submissions.get(winner["input"])["supersession_key"]
    result, operation = complete(service, winner)
    assert result["status"] == "succeeded", result
    assert operation["result"]["verification"]["confirmed"] is True
    assert len(fixture.oa.writes) == 1
    assert service.field_submissions.get(old["input"])["state"] == "superseded"
    assert service.tasks.get_task(old["task"], user_subject=OWNER)["status"] == "superseded"
    assert service.field_submissions.get_presentation(old["input"], presentation["presentation_id"])["state"] == "expired"
    with service.tasks._connect() as connection:
        draft = connection.execute("SELECT * FROM business_drafts WHERE source_submission_id=?", (old["input"],)).fetchone()
        assert (draft["state"], draft["values_json"], draft["preview_id"]) == ("expired", "{}", None)
        assert connection.execute("SELECT COUNT(*) FROM business_draft_previews WHERE draft_id=?", (draft["draft_id"],)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM field_submission_card_sessions WHERE submission_id=? AND state='pending'", (old["input"],)).fetchone()[0] == 0
        events = connection.execute("SELECT payload_json FROM task_events WHERE task_id=? AND event_type='task.pending_intent.superseded'", (old["task"],)).fetchall()
        assert len(events) == 1
        assert json.loads(events[0][0])["completedOperationId"] == operation["operation_id"]
        assert json.loads(events[0][0])["completedTaskId"] == winner["task"]
    with pytest.raises(BusinessDraftError):
        approve(drafts, credentials, preview)
    assert service.resume_interaction(user_subject=OWNER, interaction_id=old["interaction"])["error"]["code"] == "INTERACTION_SUPERSEDED"
    assert reconcile_completed_pending_intents(service, operation, task_id=winner["task"]) == []
    assert len(fixture.oa.writes) == 1


@pytest.mark.parametrize("outcome", ["unknown", "failed", "unverified"])
def test_non_authoritative_outcomes_leave_the_other_intent_untouched(run, outcome):
    fixture, _ = run
    service = fixture.service
    old, winner = source(run), source(run)
    if outcome == "unknown":
        fixture.oa.arm("approval_reply_lost")
        result, _operation = complete(service, winner)
        assert result["status"] == "unknown"
    else:
        def handler(_adapter, _worker, plan, *, enter_commit_boundary):
            if outcome == "failed":
                raise PendingActionContractMismatch("Synthetic contract rejection before write")
            enter_commit_boundary()
            return {"target": {"affair_id": plan["target"]["affair_id"]}, "verification": {"confirmed": True}}
        with patch("agentbridge.core.write_catalog.acknowledge_intellectual_property_reward", handler):
            result, _operation = complete(service, winner)
        assert result["status"] == ("failed" if outcome == "failed" else "succeeded")
    pending(service, old)


def test_cleanup_failure_keeps_the_committed_success_and_records_diagnostic(run):
    fixture, _ = run
    old, winner = source(run), source(run)
    with patch("agentbridge.core.oa_pending_reconciliation.reconcile_completed_pending_intents", side_effect=RuntimeError("synthetic cleanup fault")):
        result, operation = complete(fixture.service, winner)
    assert result["status"] == operation["status"] == "succeeded"
    pending(fixture.service, old)
    with fixture.service.tasks._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_spans WHERE operation_id=? AND error_code='PENDING_INTENT_RECONCILIATION_FAILED'", (operation["operation_id"],)).fetchone()[0] == 1
    assert reconcile_completed_pending_intents(fixture.service, operation, task_id=winner["task"]) == [old["task"]]
    assert len(fixture.oa.writes) == 1


def test_an_already_approved_decision_is_never_retired(run):
    fixture, _ = run
    service = fixture.service
    old, winner = source(run), source(run)
    old_receipt = approve(*ready(service, old))["receipt"]
    result, _operation = complete(service, winner)
    assert result["status"] == "succeeded"
    assert service.field_submissions.get(old["input"])["state"] == "submitted"
    assert service.write_authorizations.get(old_receipt["authorizationId"])["state"] == "approved"
    assert service.tasks.get_task(old["task"], user_subject=OWNER)["status"] != "superseded"


def test_submit_winning_after_selection_is_rechecked_in_the_retirement_transaction(run):
    fixture, _ = run
    service = fixture.service
    old, winner = source(run), source(run)
    token = service.field_submissions.issue_csrf(old["input"])
    original = service.field_submissions.supersede
    def submit_first(submission_id, **kwargs):
        if submission_id == old["input"]:
            service.field_submissions.submit(submission_id, csrf_token=token, csrf_cookie=token, values={"opinion": "已阅"})
        return original(submission_id, **kwargs)
    with patch.object(service.field_submissions, "supersede", side_effect=submit_first):
        result, _operation = complete(service, winner)
    assert result["status"] == "succeeded"
    assert service.field_submissions.get(old["input"])["state"] == "submitted"
    assert service.tasks.get_task(old["task"], user_subject=OWNER)["status"] != "superseded"


def legacy_authorization(run):
    fixture, _ = run
    service = fixture.service
    old = detached_source(run)
    token = service.field_submissions.issue_csrf(old["input"])
    service.field_submissions.submit(old["input"], csrf_token=token, csrf_cookie=token, values={"opinion": "已阅"})
    result = service.resume_interaction(user_subject=OWNER, interaction_id=old["interaction"], idempotency_key=uuid4().hex)
    assert result["error"]["code"] == "WRITE_AUTHORIZATION_REQUIRED", result
    old["authorization"] = result["nextAction"]["authorizationId"]
    return old


def test_unused_legacy_authorization_is_retired_but_consumed_source_is_preserved(run):
    fixture, _ = run
    service = fixture.service
    old, winner = legacy_authorization(run), source(run)
    assert service.field_submissions.get(old["input"])["state"] == "consumed"
    result, _operation = complete(service, winner)
    assert result["status"] == "succeeded"
    assert service.write_authorizations.get(old["authorization"])["state"] == "superseded"
    assert service.field_submissions.get(old["input"])["state"] == "consumed"
    assert service.tasks.get_task(old["task"], user_subject=OWNER)["status"] == "superseded"


def test_authorization_approval_winning_after_selection_is_not_retired(run):
    fixture, _ = run
    service = fixture.service
    old, winner = legacy_authorization(run), source(run)
    original = service.write_authorizations.supersede
    def approve_first(authorization_id, **kwargs):
        if authorization_id == old["authorization"]:
            token = service.write_authorizations.issue_csrf(authorization_id)
            service.write_authorizations.decide(authorization_id, decision="approve", csrf_token=token, csrf_cookie=token)
        return original(authorization_id, **kwargs)
    with patch.object(service.write_authorizations, "supersede", side_effect=approve_first):
        result, _operation = complete(service, winner)
    assert result["status"] == "succeeded"
    assert service.write_authorizations.get(old["authorization"])["state"] == "approved"
    assert service.tasks.get_task(old["task"], user_subject=OWNER)["status"] != "superseded"


def test_retirement_audit_failure_rolls_back_resource_draft_and_task_together(run):
    fixture, _ = run
    service = fixture.service
    old, winner = source(run), source(run)
    _drafts, _credentials, preview = ready(service, old)
    with service.tasks._connect() as connection:
        connection.execute("CREATE TRIGGER fail_pending_retirement_audit BEFORE INSERT ON task_events "
                           "WHEN NEW.event_type='task.pending_intent.superseded' "
                           "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    result, operation = complete(service, winner)
    assert result["status"] == "succeeded"
    pending(service, old)
    with service.tasks._connect() as connection:
        row = connection.execute("SELECT state,preview_id FROM business_drafts WHERE source_submission_id=?", (old["input"],)).fetchone()
        assert (row["state"], row["preview_id"]) == ("ready", preview["preview"]["previewId"])
        assert connection.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND event_type='task.interaction.superseded'", (old["task"],)).fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_pending_retirement_audit")
    assert old["task"] in reconcile_completed_pending_intents(service, operation, task_id=winner["task"])
    assert len(fixture.oa.writes) == 1


@pytest.mark.parametrize("change", ["user", "system", "session", "affair", "action", "unknown", "running", "batch", "plan"])
def test_scope_and_inflight_exclusions(run, change):
    fixture, _ = run
    service = fixture.service
    old, winner = source(run), source(run)
    if change in {"user", "system", "session", "action"}:
        old = detached_source(run, **{
            "user": {"subject": "someone-else"}, "system": {"system": "another-system"},
            "session": {"session": "another-session"},
            "action": {"capability": "oa.intellectual_property_declaration.approval.prepare"},
        }[change])
    if change == "affair":
        add_reward_affair(fixture, "synthetic-other-reward")
        old = source(run, affair="synthetic-other-reward")
    if change == "batch":
        old = source(run, batch=True)
    if change == "plan":
        from tests.test_task_plan_store import TaskPlanStoreTests
        service.task_plans.create(user_subject=OWNER, parent_task_id=old["task"],
            compiled_plan=TaskPlanStoreTests.compiled(None), proposal_source="agent_host",
            coordinator_lease_version=1, idempotency_key=uuid4().hex)
    with service.tasks._connect() as connection:
        if change in {"unknown", "running"}:
            connection.execute("UPDATE agent_tasks SET status=? WHERE task_id=?", ("outcome_unknown" if change == "unknown" else "running", old["task"]))
    result, _operation = complete(service, winner)
    assert result["status"] == "succeeded"
    assert service.field_submissions.get(old["input"])["state"] == "pending"

