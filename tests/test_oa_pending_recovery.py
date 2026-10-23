"""Recovery uses new authority; pending presence never erases the old unknown."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import shutil
from unittest.mock import patch

import pytest

from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from agentbridge.core.capability_runtime import CapabilityRejected
from agentbridge.core.oa_pending_recovery import (
    ACK_VERSION, OaPendingRecoveryError, can_recover_pending, probe_unknown_pending,
    prepare_recovery_attempt, recovery_child_for_origin, recovery_permit,
)
from tests.support.oa_pending_recovery_acceptance import PendingRecoveryAcceptanceFixture
from tests.support.full_business_acceptance import OWNER
from tests.test_full_business_acceptance import decide, drive


@pytest.fixture(scope="module")
def recovery_template(tmp_path_factory):
    home = tmp_path_factory.mktemp("true-unknown-template")
    fixture = PendingRecoveryAcceptanceFixture(home, modes=("unresolved",))
    fixture.close()
    return home


@pytest.fixture
def fixture(tmp_path, recovery_template):
    home = tmp_path / "isolated"
    shutil.copytree(recovery_template, home)
    fixture = PendingRecoveryAcceptanceFixture(home, modes=())
    try:
        yield fixture
    finally:
        fixture.close()


def origin(fixture):
    return fixture.meta["recovery_cases"][0]


def probe(fixture, key="recovery-probe-0001"):
    return probe_unknown_pending(fixture.service, subject=OWNER, task_id=origin(fixture)["task_id"],
        endpoint_id=fixture.account["endpoint_id"], request_key=key)


def prepare(fixture, assessment=None, key="recovery-prepare-0001", **overrides):
    assessment = assessment or probe(fixture)
    return prepare_recovery_attempt(fixture.service, **{
        "subject": OWNER, "task_id": origin(fixture)["task_id"], "assessment_id": assessment["assessmentId"],
        "assessment_hash": assessment["assessmentHash"], "expected_revision": assessment["revision"],
        "endpoint_id": fixture.account["endpoint_id"], "request_key": key, "acknowledgement": ACK_VERSION, **overrides})


def child_case(fixture, task):
    with fixture.service.tasks._connect() as db:
        row = db.execute("SELECT f.submission_id FROM field_submissions f JOIN task_operations t ON t.operation_id=f.create_operation_id WHERE t.task_id=? AND f.state='pending' ORDER BY f.created_at DESC LIMIT 1", (task,)).fetchone()
    interaction = fixture.service.tasks.get_task(task, user_subject=OWNER)["current_interaction_id"]
    return {"task_id": task, "input_id": row[0], "input_interaction": interaction}


def ready(fixture):
    child = prepare(fixture)
    case = child_case(fixture, child["taskId"])
    drafts = BusinessDraftService(fixture.service)
    opened = drafts.open(case["input_id"])
    assert not opened["state"]["values"].get("opinion")
    credentials = {"source_submission_id": case["input_id"], "presentation_id": None,
                   "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
    saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"], values={"opinion": "新的明确办理意见"})
    preview = drafts.preview(**credentials, expected_revision=saved["revision"])
    return case, drafts, credentials, preview


def counts(fixture):
    with fixture.service.tasks._connect() as db:
        return tuple(db.execute("SELECT (SELECT count(*) FROM business_draft_decisions),(SELECT count(*) FROM write_authorizations WHERE state='consumed'),(SELECT count(*) FROM operations WHERE capability_name='oa.efficiency_data.approve')").fetchone())


def test_new_attempt_has_new_material_opinion_authority_and_one_effect_without_changing_old(fixture):
    old = origin(fixture)
    old_auth = fixture.service.write_authorizations.get(old["original_authorization_id"])
    case, drafts, credentials, preview = ready(fixture)
    assert len(fixture.oa.writes) == 1 and counts(fixture) == (1, 1, 1)
    decision = decide(drafts, credentials, preview)
    drive(fixture, case)
    auth = fixture.service.write_authorizations.get(decision["receipt"]["authorizationId"])
    assert auth["state"] == "consumed" and auth["authorization_id"] != old_auth["authorization_id"]
    assert fixture.service.operations.get(auth["commit_operation_id"])["status"] == "succeeded"
    assert fixture.oa.writes[-1]["opinion"] == "新的明确办理意见"
    assert counts(fixture) == (2, 2, 2) and len(fixture.oa.writes) == 2
    assert fixture.service.write_authorizations.get(old_auth["authorization_id"]) == old_auth
    assert fixture.service.tasks.get_task(old["task_id"], user_subject=OWNER)["status"] == "outcome_unknown"
    assert fixture.service.operations.get(old["original_operation_id"])["status"] == "unknown"
    replay = fixture.service.invoke(user_subject=OWNER, capability_name="oa.efficiency_data.approve",
        arguments={"authorization_id": old_auth["authorization_id"]}, task_id=old["task_id"], idempotency_key="old-auth-never-replay")
    assert replay["status"] != "succeeded" and len(fixture.oa.writes) == 2


def test_probe_is_readonly_idempotent_and_done_positive_conflict_blocks(fixture):
    first = probe(fixture)
    assert first == probe(fixture) and first["status"] == "unresolved_still_pending"
    item = fixture.oa.data["affairs"][origin(fixture)["arguments"]["affair_id"]]
    item["done_conflict"] = True
    second = probe(fixture, "recovery-probe-conflict")
    assert second["status"] == "blocked" and not second["canPrepare"]
    assert counts(fixture) == (1, 1, 1) and len(fixture.oa.writes) == 1


def test_complete_requires_same_done_identity_and_appends_evidence_without_rewriting_unknown(fixture):
    fixture.oa.data["affairs"][origin(fixture)["arguments"]["affair_id"]]["processed"] = True
    result = probe(fixture)
    assert result["status"] == "verified_complete" and not result["canPrepare"]
    assert fixture.service.tasks.get_task(origin(fixture)["task_id"], user_subject=OWNER)["status"] == "outcome_unknown"
    with fixture.service.tasks._connect() as db:
        assert db.execute("SELECT count(*) FROM task_events WHERE event_type='task.oa_recovery.verified_complete'").fetchone()[0] == 1
    fixture.oa.data["fault"] = "done_identity_changed"
    assert probe(fixture, "recovery-probe-wrong-identity")["status"] == "blocked"
    assert len(fixture.oa.writes) == 1


def test_concurrent_different_keys_issue_one_child_and_one_blank_source(fixture):
    assessment = probe(fixture)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda i: prepare(fixture, assessment, "parallel-prepare-key-" + str(i)), range(2)))
    assert results[0]["taskId"] == results[1]["taskId"]
    with fixture.service.tasks._connect() as db:
        assert db.execute("SELECT count(*) FROM task_events WHERE event_type='task.oa_recovery.child_created'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM field_submissions f JOIN task_operations t ON t.operation_id=f.create_operation_id WHERE t.task_id=?", (results[0]["taskId"],)).fetchone()[0] == 1
    assert counts(fixture) == (1, 1, 1)


def test_confirmation_key_cannot_change_assessment_or_ack(fixture):
    first = probe(fixture)
    prepare(fixture, first)
    second = probe(fixture, "recovery-probe-second")
    with pytest.raises(OaPendingRecoveryError, match="另一核对"):
        prepare(fixture, second)
    with pytest.raises(OaPendingRecoveryError) as caught:
        prepare(fixture, first, key="missing-acknowledgement", acknowledgement="true")
    assert caught.value.code == "OA_RECOVERY_ACK_REQUIRED"


def test_transient_prepare_failure_retries_only_read_source_in_same_child(fixture):
    assessment = probe(fixture)
    actual = fixture.service.invoke
    def temporary_failure(**kwargs):
        if kwargs["capability_name"] == "oa.efficiency_data.approval.prepare":
            with patch.object(fixture.service.adapter_for_system("oa"), "resolve_workflow_detail", side_effect=ConnectionError("synthetic read unavailable")):
                return actual(**kwargs)
        return actual(**kwargs)
    with patch.object(fixture.service, "invoke", side_effect=temporary_failure):
        with pytest.raises(OaPendingRecoveryError) as caught:
            prepare(fixture, assessment)
    assert caught.value.code == "OA_RECOVERY_PREPARE_FAILED"
    child = recovery_child_for_origin(fixture.service, subject=OWNER, task_id=origin(fixture)["task_id"])
    assert child and fixture.service.tasks.get_task(child, user_subject=OWNER)["status"] == "failed"
    recovered = prepare(fixture, assessment)
    assert recovered["taskId"] == child and recovered["status"] == "existing"
    assert child_case(fixture, child)["input_id"]
    assert counts(fixture) == (1, 1, 1) and len(fixture.oa.writes) == 1


@pytest.mark.parametrize("phase", ["before_decision", "before_commit"])
def test_new_done_after_prepare_blocks_before_new_authority_consumption(fixture, phase):
    case, drafts, credentials, preview = ready(fixture)
    if phase == "before_commit":
        decision = decide(drafts, credentials, preview)
    fixture.oa.data["affairs"][origin(fixture)["arguments"]["affair_id"]]["processed"] = True
    if phase == "before_decision":
        with pytest.raises((BusinessDraftError, CapabilityRejected)):
            decide(drafts, credentials, preview)
        assert counts(fixture) == (1, 1, 1)
    else:
        drive(fixture, case)
        auth = fixture.service.write_authorizations.get(decision["receipt"]["authorizationId"])
        assert auth["state"] == "approved" and auth["consumed_at"] is None
        assert counts(fixture) == (2, 1, 2)
    assert len(fixture.oa.writes) == 1


@pytest.mark.parametrize("other_task", [None, "ordinary"])
def test_direct_commit_cannot_move_recovery_authorization_to_another_task(fixture, other_task):
    case, drafts, credentials, preview = ready(fixture)
    decision = decide(drafts, credentials, preview)
    task_id = None
    if other_task:
        task, _ = fixture.service.tasks.ensure_task(user_subject=OWNER, agent_host="openclaw", host_task_key="ordinary-commit-wrapper", origin_endpoint_id=fixture.account["endpoint_id"], active_conversation_ref=fixture.account["openclaw_session_key"], title="普通任务不能代办恢复授权")
        task_id = task["task_id"]
    result = fixture.service.invoke(user_subject=OWNER, capability_name="oa.efficiency_data.approve",
        arguments={"authorization_id": decision["receipt"]["authorizationId"]}, task_id=task_id,
        idempotency_key="wrong-task-recovery-commit-" + str(other_task))
    assert result["status"] != "succeeded" and result["error"]["code"] == "OA_RECOVERY_PERMIT_INVALID", result
    auth = fixture.service.write_authorizations.get(decision["receipt"]["authorizationId"])
    assert auth["state"] == "approved" and auth["consumed_at"] is None
    assert len(fixture.oa.writes) == 1


def test_permit_tampering_or_session_generation_change_cannot_grant_exception(fixture):
    child = prepare(fixture)["taskId"]
    session = fixture.service.sessions.find(user_subject=OWNER, system_id="oa")
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE sessions SET authority_generation=authority_generation+1 WHERE session_id=?", (session["session_id"],))
    with pytest.raises(OaPendingRecoveryError):
        recovery_permit(fixture.service, subject=OWNER, task_id=child, session=session)
    assert counts(fixture) == (1, 1, 1)


def test_expired_assessment_during_live_read_cannot_reserve_child(fixture):
    import agentbridge.core.oa_pending_recovery as recovery
    assessment = probe(fixture)
    now = recovery._now()
    original_live = recovery._live
    def read_then_expire(*args, **kwargs):
        result = original_live(*args, **kwargs)
        clock.return_value = now + timedelta(seconds=121)
        return result
    with patch.object(recovery, "_now", return_value=now) as clock, patch.object(recovery, "_live", side_effect=read_then_expire):
        with pytest.raises(OaPendingRecoveryError) as caught:
            prepare(fixture, assessment)
    assert caught.value.code == "OA_RECOVERY_ASSESSMENT_STALE"
    assert recovery_child_for_origin(fixture.service, subject=OWNER, task_id=origin(fixture)["task_id"]) is None
    assert counts(fixture) == (1, 1, 1)


def test_precreated_deterministic_task_or_forged_permit_cannot_be_overwritten(fixture):
    old = origin(fixture)
    task, _ = fixture.service.tasks.ensure_task(user_subject=OWNER, agent_host="openclaw",
        host_task_key="oa-recovery-child:" + old["original_operation_id"], origin_endpoint_id=fixture.account["endpoint_id"],
        active_conversation_ref=fixture.account["openclaw_session_key"], title="预先存在的普通任务", summary={"ordinary": True})
    with pytest.raises(OaPendingRecoveryError) as caught:
        prepare(fixture)
    assert caught.value.code == "OA_RECOVERY_PERMIT_INVALID"
    assert fixture.service.tasks.get_task(task["task_id"], user_subject=OWNER)["summary"] == {"ordinary": True}
    assert counts(fixture) == (1, 1, 1)


def test_material_drift_before_commit_blocks_new_consumption(fixture):
    case, drafts, credentials, preview = ready(fixture)
    decision = decide(drafts, credentials, preview)
    fixture.oa.data["affairs"][origin(fixture)["arguments"]["affair_id"]]["body_text"] += "决定后真实材料变化"
    drive(fixture, case)
    auth = fixture.service.write_authorizations.get(decision["receipt"]["authorizationId"])
    assert auth["state"] == "approved" and auth["consumed_at"] is None
    assert len(fixture.oa.writes) == 1


def test_recovery_child_inherits_original_skill_and_revocation_blocks_it(fixture):
    service = fixture.service
    binding = service.skills.bind(OWNER, "oa-pending", "single")
    service.skills.attach(OWNER, origin(fixture)["task_id"], binding)
    child = prepare(fixture)["taskId"]
    assert service.skills.for_task(OWNER, child)["binding_id"] == binding
    with service.tasks._connect() as db:
        db.execute("UPDATE skill_bindings SET revoked=1 WHERE binding_id=?", (binding,))
    with pytest.raises(Exception):
        prepare(fixture)
    assert counts(fixture) == (1, 1, 1) and len(fixture.oa.writes) == 1
