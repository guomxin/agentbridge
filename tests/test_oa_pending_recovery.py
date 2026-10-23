"""Recovery uses new authority; pending presence never erases the old unknown."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import shutil
from unittest.mock import patch

import pytest

from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from agentbridge.core.capability_runtime import CapabilityRejected
from agentbridge.browser.read_budget import ReadBudgetExceeded
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


def test_completed_probe_reuses_only_its_own_collection_observations(fixture):
    """A probe has one collection read each; another probe must read afresh."""
    affair_id = origin(fixture)["arguments"]["affair_id"]
    fixture.oa.data["affairs"][affair_id]["processed"] = True
    fixture.oa.persist()
    adapter, _factory = fixture.service._runtime_for_system("oa")
    with patch.object(adapter, "list_workflows", wraps=adapter.list_workflows) as reads, \
            patch.object(adapter, "read_pending_done", wraps=adapter.read_pending_done) as done_reads:
        first = probe(fixture, "same-observation-completed")
        assert first["status"] == "verified_complete"
        assert [call.kwargs["collection"] for call in reads.call_args_list] == ["pending"]
        assert done_reads.call_count == 1
        assert probe(fixture, "same-observation-completed") == first
        assert reads.call_count == done_reads.call_count == 1
        # Idempotent receipt recovery can reuse its receipt, but a new probe
        # must observe the changed downstream state with fresh collections.
        fixture.oa.data["affairs"][affair_id]["processed"] = False
        fixture.oa.persist()
        current = probe(fixture, "next-observation-pending")
        assert current["status"] == "unresolved_still_pending"
        assert [call.kwargs["collection"] for call in reads.call_args_list] == ["pending", "pending"]
        assert done_reads.call_count == 2
    assert counts(fixture) == (1, 1, 1) and len(fixture.oa.writes) == 1


def test_probe_budget_exhaustion_remains_readonly_and_can_be_checked_again(fixture):
    adapter, _factory = fixture.service._runtime_for_system("oa")
    with patch.object(adapter, "read_pending_done", side_effect=ReadBudgetExceeded("test budget")):
        result = probe(fixture, "bounded-observation-timeout")
    assert result["status"] == "blocked" and result["reason"] == "read_budget_exceeded"
    assert not result["canPrepare"]
    assert counts(fixture) == (1, 1, 1) and len(fixture.oa.writes) == 1
    assert probe(fixture, "bounded-observation-fresh")["status"] == "unresolved_still_pending"


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


def current_recovery(fixture):
    from agentbridge.core.oa_pending_recovery import pending_recovery_presentation
    return pending_recovery_presentation(fixture.service, subject=OWNER, task_id=origin(fixture)["task_id"])


def query_source(fixture, number):
    from agentbridge.core.quick_queries import execute_query, queue_query
    queued = queue_query(fixture.service, fixture.account, {"requestKey": f"recovery-live-query-{number:04}"})
    result = execute_query(fixture.service, OWNER, queued["taskId"], queued["commandKey"],
        runtime_context={"endpointId": fixture.account["endpoint_id"], "hostType": "openclaw",
                         "hostInstanceId": "synthetic-recovery-projection"})
    assert result["status"] == "succeeded"
    return queued["taskId"]


def test_current_projection_restores_saved_readonly_evidence_without_restoring_prepare_authority(fixture):
    import agentbridge.core.oa_pending_recovery as recovery
    initial = current_recovery(fixture)
    assert initial["status"] == "not_checked" and initial["nextAction"] == "probe"
    assessment = probe(fixture)
    before = counts(fixture)
    with patch.object(recovery, "_live", side_effect=AssertionError("GET must never read OA")):
        result = fixture.app.task_detail(fixture.account, origin(fixture)["task_id"])["oaPendingRecovery"]
    assert result["status"] == "unresolved_still_pending" and result["stale"] is False
    assert result["observedAt"] == assessment["observedAt"] and result["expiresAt"] == assessment["expiresAt"]
    assert result["canPrepare"] is result["canExecute"] is False and result["canProbe"] is True
    assert not {"assessmentId", "assessmentHash", "revision", "sessionHash", "scopeHash", "originPlanHash"}.intersection(result)
    with patch.object(recovery, "_now", return_value=recovery._now() + timedelta(seconds=121)):
        expired = current_recovery(fixture)
    assert expired["stale"] is True and expired["nextAction"] == "probe" and expired["canPrepare"] is False
    session = fixture.service.sessions.find(user_subject=OWNER, system_id="oa")
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE sessions SET authority_generation=authority_generation+1 WHERE session_id=?", (session["session_id"],))
    assert current_recovery(fixture)["stale"] is True
    assert counts(fixture) == before and len(fixture.oa.writes) == 1


def test_recovery_completion_invalidates_original_and_six_sources_without_refresh_or_new_business_work(fixture):
    from agentbridge.core.actionable_results import preparation_source_task_ids
    sources = [query_source(fixture, number) for number in range(6)]
    old = origin(fixture)
    original_auth = fixture.service.write_authorizations.get(old["original_authorization_id"])
    case, drafts, credentials, preview = ready(fixture)
    expected = {old["task_id"], *sources}
    for phase in ("waiting", "completed"):
        if phase == "completed":
            decide(drafts, credentials, preview)
            drive(fixture, case)
        before = counts(fixture)
        detail = fixture.app.task_detail(fixture.account, case["task_id"])
        assert set(detail["actionableSourceTaskIds"]) == expected
        current = current_recovery(fixture)
        assert current["recoveryTaskId"] == case["task_id"] and current["nextAction"] == "open_recovery"
        assert current["status"] == ("recovery_in_progress" if phase == "waiting" else "recovery_succeeded")
        assert current["recoveryTaskStatus"] == ("waiting_user" if phase == "waiting" else "succeeded")
        # Simulate exactly the read-only IDs the live projection queue receives.
        for task_id in sources:
            projected = fixture.app.task_detail(fixture.account, task_id)
            targets = projected["actionableResults"][0]["targets"]
            target = next(item for item in targets if item["preparation"] and item["preparation"]["taskId"] == case["task_id"])
            assert target["preparation"]["state"] == ("waiting_user" if phase == "waiting" else "succeeded")
            assert not projected["actionableSourceTaskIds"], "source projections must not enqueue children"
        assert counts(fixture) == before
    assert fixture.service.write_authorizations.get(old["original_authorization_id"]) == original_auth
    assert fixture.service.tasks.get_task(old["task_id"], user_subject=OWNER)["status"] == "outcome_unknown"
    assert fixture.service.operations.get(old["original_operation_id"])["status"] == "unknown"
    assert counts(fixture) == (2, 2, 2) and len(fixture.oa.writes) == 2
    session = fixture.service.sessions.find(user_subject=OWNER, system_id="oa")
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE sessions SET downstream_principal_ref='other-principal' WHERE session_id=?", (session["session_id"],))
    historical = current_recovery(fixture)
    assert historical["status"] == "recovery_succeeded" and historical["stale"] is True
    assert historical["canProbe"] is False and historical["canPrepare"] is False
    # Identity changes do not erase an owned history dependency, but no old
    # completion or invalidation hint grants the new identity an action.
    assert set(fixture.app.task_detail(fixture.account, case["task_id"])["actionableSourceTaskIds"]) == expected
    with fixture.service.tasks._connect() as db:
        from agentbridge.core.actionable_results import ActionableResultError
        with pytest.raises(ActionableResultError):
            preparation_source_task_ids(db, "other-user", case["task_id"], service=fixture.service)


def test_saved_verified_completion_is_visible_after_reload_and_does_not_rewrite_original(fixture):
    import agentbridge.core.oa_pending_recovery as recovery
    sources = [query_source(fixture, number) for number in range(6)]
    original_id = origin(fixture)["task_id"]
    def source_states():
        projections = [fixture.app.task_detail(fixture.account, task_id) for task_id in sources]
        assert all(not item["actionableSourceTaskIds"] for item in projections), "query-to-origin dependency would create a loop"
        return [next(target["preparation"] for target in item["actionableResults"][0]["targets"]
            if target["preparation"] and target["preparation"]["taskId"] == original_id) for item in projections]
    assert {item["state"] for item in source_states()} == {"outcome_unknown"}
    fixture.oa.data["affairs"][origin(fixture)["arguments"]["affair_id"]]["processed"] = True
    observation = probe(fixture)
    assert observation["status"] == "verified_complete"
    with patch("agentbridge.core.oa_pending_recovery._live", side_effect=AssertionError("No OA read on projection")):
        public = current_recovery(fixture)
        again = fixture.app.task_detail(fixture.account, origin(fixture)["task_id"])["oaPendingRecovery"]
    assert public == again and public["status"] == "verified_complete"
    assert public["observedAt"] == observation["observedAt"] and not public["canPrepare"]
    assert set(fixture.app.task_detail(fixture.account, original_id)["actionableSourceTaskIds"]) == set(sources)
    assert all(item == {"taskId": original_id, "state": "verified_complete", "observedAt": observation["observedAt"],
                        "expiresAt": observation["expiresAt"]} for item in source_states())
    with patch.object(recovery, "_now", return_value=recovery._now() + timedelta(seconds=121)):
        expired = current_recovery(fixture)
        assert {item["state"] for item in source_states()} == {"outcome_unknown"}
    assert expired["status"] == "verified_complete" and expired["stale"] is True
    assert expired["canProbe"] is True and expired["nextAction"] == "probe" and expired["canPrepare"] is False
    session = fixture.service.sessions.find(user_subject=OWNER, system_id="oa")
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE sessions SET authority_generation=authority_generation+1 WHERE session_id=?", (session["session_id"],))
    assert {item["state"] for item in source_states()} == {"outcome_unknown"}
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE sessions SET downstream_principal_ref='other-principal' WHERE session_id=?", (session["session_id"],))
    changed_identity = current_recovery(fixture)
    assert changed_identity["status"] == "verified_complete" and changed_identity["stale"] is True
    assert changed_identity["canProbe"] is False and changed_identity["reason"] == "identity_changed"
    assert fixture.service.tasks.get_task(origin(fixture)["task_id"], user_subject=OWNER)["status"] == "outcome_unknown"
    assert counts(fixture) == (1, 1, 1)


def test_source_complete_requires_the_attested_verification_event_and_preserves_unknown_history(fixture):
    source = query_source(fixture, 0)
    original_id = origin(fixture)["task_id"]
    fixture.oa.data["affairs"][origin(fixture)["arguments"]["affair_id"]]["processed"] = True
    assert probe(fixture)["status"] == "verified_complete"
    with fixture.service.tasks._connect() as db:
        db.execute("DELETE FROM task_events WHERE task_id=? AND event_type='task.oa_recovery.verified_complete'", (original_id,))
    public = current_recovery(fixture)
    assert public["status"] == "blocked" and public["reason"] == "assessment_evidence_invalid"
    rows = fixture.app.task_detail(fixture.account, source)["actionableResults"][0]["targets"]
    target = next(item for item in rows if item["preparation"] and item["preparation"]["taskId"] == original_id)
    assert target["preparation"]["state"] == "outcome_unknown"
    assert fixture.service.tasks.get_task(original_id, user_subject=OWNER)["status"] == "outcome_unknown"
    assert counts(fixture) == (1, 1, 1) and len(fixture.oa.writes) == 1


def test_recovery_child_success_label_requires_authoritative_completion_not_only_task_status(fixture):
    child_id = prepare(fixture)["taskId"]
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE agent_tasks SET status='succeeded' WHERE task_id=?", (child_id,))
    result = current_recovery(fixture)
    assert result["status"] == "recovery_unknown" and result["reason"] == "recovery_result_unverified"
    assert result["nextAction"] == "open_recovery" and result["canPrepare"] is False
    assert counts(fixture) == (1, 1, 1)


def test_recovery_failure_and_unknown_remain_distinct_and_do_not_allow_a_second_child(fixture):
    child_id = prepare(fixture)["taskId"]
    for state, status in (("failed", "recovery_failed"), ("outcome_unknown", "recovery_unknown")):
        with fixture.service.tasks._connect() as db:
            db.execute("UPDATE agent_tasks SET status=? WHERE task_id=?", (state, child_id))
        result = current_recovery(fixture)
        assert result["status"] == status and result["recoveryTaskId"] == child_id
        assert result["canPrepare"] is False and result["nextAction"] == "open_recovery"
    assert counts(fixture) == (1, 1, 1)


def test_recovery_availability_explains_login_identity_and_grant_failures_without_reading_or_writing(fixture):
    session = fixture.service.sessions.find(user_subject=OWNER, system_id="oa")
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE sessions SET state='expired' WHERE session_id=?", (session["session_id"],))
    result = current_recovery(fixture)
    assert (result["availability"], result["reason"], result["nextAction"], result["canProbe"]) == (
        "login_required", "login_required", "restore_login", False)
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE sessions SET state='active',downstream_principal_ref='other-principal' WHERE session_id=?", (session["session_id"],))
    assert current_recovery(fixture)["reason"] == "identity_changed"
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE sessions SET downstream_principal_ref=? WHERE session_id=?", (session["downstream_principal_ref"], session["session_id"]))
    with patch.object(fixture.service.user_grants, "require_capability", side_effect=PermissionError("PRIVATE_GRANT")):
        result = current_recovery(fixture)
    assert result["reason"] == "permission_required" and "PRIVATE_GRANT" not in json.dumps(result)
    assert counts(fixture) == (1, 1, 1) and len(fixture.oa.writes) == 1


def test_tampered_recovery_link_never_exports_parent_or_source_dependency_or_success(fixture):
    child_id = prepare(fixture)["taskId"]
    query_source(fixture, 0)
    with fixture.service.tasks._connect() as db:
        task = fixture.service.tasks._select_owned_task(db, child_id, OWNER)
        summary = json.loads(task["summary_json"])
        summary["oaPendingRecovery"]["affairId"] = "forged-exact-affair"
        db.execute("UPDATE agent_tasks SET summary_json=? WHERE task_id=?", (json.dumps(summary), child_id))
    detail = fixture.app.task_detail(fixture.account, child_id)
    assert detail["actionableSourceTaskIds"] == []
    result = current_recovery(fixture)
    assert result["status"] == "blocked" and result["reason"] == "recovery_evidence_invalid"
    assert "recoveryTaskId" not in result and counts(fixture) == (1, 1, 1)


def test_corrupt_saved_assessment_is_not_presented_as_current_verified_evidence(fixture):
    probe(fixture)
    with fixture.service.tasks._connect() as db:
        row = db.execute("SELECT event_id,payload_json FROM task_events WHERE task_id=? AND event_type='task.oa_recovery.assessed'", (origin(fixture)["task_id"],)).fetchone()
        payload = json.loads(row["payload_json"])
        payload.update(status="verified_complete", reason="PRIVATE_RAW_ERROR")
        db.execute("UPDATE task_events SET payload_json=? WHERE event_id=?", (json.dumps(payload), row["event_id"]))
    result = current_recovery(fixture)
    assert result["status"] == "blocked" and result["reason"] == "assessment_evidence_invalid"
    assert "PRIVATE_RAW_ERROR" not in json.dumps(result) and result["canPrepare"] is False
