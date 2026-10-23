"""Business outcome views disclose bounded evidence, never replay unknown writes."""
import json
import sqlite3
import threading

import pytest

from agentbridge.core.business_drafts import BusinessDraftService
from agentbridge.workspace.application import _public_business_execution
from tests.support.business_view_acceptance import BusinessViewAcceptanceFixture, OWNER
from tests.test_workspace import PASSWORD, _request, _cookies


@pytest.fixture
def fixture(tmp_path):
    value = BusinessViewAcceptanceFixture(tmp_path)
    try:
        yield value
    finally:
        value.close()


def _case(fixture, profile="leave"):
    return next(case for case in fixture.meta["view_cases"] if case["profile"] == profile)


def _approve(fixture, case):
    drafts = BusinessDraftService(fixture.service)
    opened = drafts.open(case["input_id"])
    args = {"source_submission_id": case["input_id"], "csrf_token": opened["csrfToken"],
            "csrf_cookie": opened["csrfToken"]}
    state = drafts.save(**args, expected_revision=opened["state"]["revision"], values={"opinion": "合成核对意见"})
    state = drafts.preview(**args, expected_revision=state["revision"])
    return drafts.approve(**args, expected_revision=state["revision"], preview_id=state["preview"]["previewId"],
                          request_key="one-confirmation-display")


def _unknown(fixture, case, message):
    service = fixture.service
    operation, _ = service.operations.create(user_subject=OWNER, capability_name="oa.leave.approve",
        capability_version="1", input_summary={"authorization_id": "PRIVATE_AUTHORIZATION"})
    service.operations.mark_unknown(operation["operation_id"], code="RESULT_UNKNOWN", message=message)
    service.observe_host_task(user_subject=OWNER, task_id=case["task_id"], operation_ids=[operation["operation_id"]])
    return operation


def test_trusted_draft_and_durable_confirmation_collapse_only_the_new_flow(fixture):
    case = _case(fixture)
    with sqlite3.connect(fixture.service.db_path) as connection:
        original = connection.execute("SELECT schema_json,schema_hash FROM field_submissions WHERE submission_id=?",
                                      (case["input_id"],)).fetchone()
    pending = fixture.app.task_detail(fixture.account, case["task_id"])
    source = next(item for item in pending["interactions"] if item["interactionId"] == case["input_interaction"])
    assert source["confirmation"] == {"mode": "single", "state": "pending"}
    assert "合成申请人" in pending["businessTitle"]
    receipt = _approve(fixture, case)["receipt"]
    recorded = fixture.app.task_detail(fixture.account, case["task_id"])
    source, authorization = recorded["interactions"]
    assert source["confirmation"] == {"mode": "single", "state": "recorded"}
    assert authorization["confirmation"] == {"mode": "single", "state": "recorded",
                                            "sourceInteractionId": case["input_interaction"]}
    assert receipt["authorizationId"] not in json.dumps(recorded.get("businessExecution", {}))
    with sqlite3.connect(fixture.service.db_path) as connection:
        assert connection.execute("SELECT schema_json,schema_hash FROM field_submissions WHERE submission_id=?",
                                  (case["input_id"],)).fetchone() == original
        assert connection.execute("SELECT COUNT(*) FROM business_draft_decisions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM write_authorizations").fetchone()[0] == 1
    assert fixture.oa.writes == []

    legacy = fixture.app.task_detail(fixture.account, _case(fixture, "legacy_overtime")["task_id"])
    assert all("confirmation" not in item for item in legacy["interactions"])


def test_unknown_projects_only_whitelisted_evidence_and_cannot_continue_as_a_write(fixture):
    case = _case(fixture)
    _unknown(fixture, case, "PRIVATE_PREFIX " + json.dumps({"readback": {"reason": "still_pending"},
        "execution": {"dispatch": {"state": "invoked", "url": "PRIVATE_URL"},
            "readback": {"reason": "still_pending", "last_observed_at": "2026-10-23T01:02:03Z", "attempts": 3}},
        "password": "PRIVATE_PASSWORD", "target": {"affair_id": "PRIVATE_AFFAIR"}}))
    detail = fixture.app.task_detail(fixture.account, case["task_id"])
    assert detail["businessExecution"] == {"schemaVersion": "agentbridge.business-execution.v1",
        "phase": "outcome_unknown", "reason": "still_pending", "lastObservedAt": "2026-10-23T01:02:03+00:00",
        "observationCount": 3, "submissionState": "invoked", "readOnlyQuery": "oa_pending",
        "updatedAt": detail["businessExecution"]["updatedAt"]}
    public = json.dumps(detail, ensure_ascii=False)
    assert "PRIVATE_" not in public
    with pytest.raises(ValueError, match="只读查询"):
        fixture.app.continue_task(fixture.account, case["task_id"])
    assert fixture.oa.writes == []


@pytest.mark.parametrize("error", [
    "prefix {not json}", "prefix " + "{" * 70_000,
    'prefix {"readback":{"reason":"PASSWORD=secret"},"execution":{"dispatch":{"state":"<script>"},'
    '"readback":{"last_observed_at":"not-a-date","attempts":true}}}',
], ids=["malformed", "oversized", "unsafe-values"])
def test_unknown_legacy_and_untrusted_error_text_never_escape_the_projection(error):
    projected = _public_business_execution({"capability_name": "oa.efficiency_data.approve", "status": "unknown",
        "error_code": "RESULT_UNKNOWN", "error_message": error}, {"status": "outcome_unknown"})
    assert projected == {"schemaVersion": "agentbridge.business-execution.v1", "phase": "outcome_unknown",
                         "readOnlyQuery": "oa_pending"}


def test_legacy_unknown_reason_and_non_oa_or_non_write_boundaries():
    operation = {"capability_name": "oa.efficiency_data.approve", "status": "unknown", "error_code": "RESULT_UNKNOWN",
        "error_message": '旧日志 {"readback":{"reason":"still_pending"},"secret":"hidden"}'}
    assert _public_business_execution(operation, {"status": "outcome_unknown"})["reason"] == "still_pending"
    assert _public_business_execution({**operation, "error_code": "UNTRUSTED"}, {}) == {
        "schemaVersion": "agentbridge.business-execution.v1", "phase": "outcome_unknown"}
    assert _public_business_execution({**operation, "capability_name": "oa.workflow.pending.list"}, {}) is None
    assert "readOnlyQuery" not in _public_business_execution(
        {**operation, "capability_name": "taihua.work_log.create"}, {"status": "outcome_unknown"})


def test_submission_readiness_failure_and_verified_success_have_distinct_meanings():
    operation = {"capability_name": "oa.efficiency_data.approve", "status": "failed", "error_code": "OA_PENDING_ACTION_NOT_READY",
        "error_message": '未提交 {"execution":{"readiness":{"reason":"body_loading"},"dispatch":{"state":"not_invoked"}}}'}
    assert _public_business_execution(operation, {}) == {"schemaVersion": "agentbridge.business-execution.v1",
        "phase": "not_submitted", "reason": "body_loading", "submissionState": "not_invoked"}
    page_loading = {**operation, "error_message": operation["error_message"].replace("body_loading", "page_loading")}
    assert _public_business_execution(page_loading, {})["reason"] == "page_loading"
    assert _public_business_execution({**operation, "error_code": "INVALID_ARGUMENT"}, {}) == {
        "schemaVersion": "agentbridge.business-execution.v1", "phase": "failed"}
    assert _public_business_execution({**operation, "status": "succeeded", "error_code": None}, {}) == {
        "schemaVersion": "agentbridge.business-execution.v1", "phase": "verified"}
    success = {**operation, "status": "succeeded", "error_code": None,
        "result": {"execution": {"readback": {"last_observed_at": "2026-10-23T01:02:03Z", "attempts": 2},
                                  "dispatch": {"state": "invoked", "private": "PRIVATE_SUCCESS"}}}}
    view = _public_business_execution(success, {})
    assert view["lastObservedAt"] == "2026-10-23T01:02:03+00:00" and view["submissionState"] == "invoked"
    assert "PRIVATE_SUCCESS" not in json.dumps(view)
    assert _public_business_execution(success, {"status": "outcome_unknown"})["phase"] == "outcome_unknown"
    assert _public_business_execution(success, {"status": "failed"})["phase"] == "failed"


@pytest.mark.parametrize("missing", [None, "decision", "task_link", "input_hash", "approved_state", "later_consumed", "parent_unknown"],
                         ids=["proven", "no-decision", "no-task-link", "wrong-hash", "not-approved", "later-consumed", "parent-unknown"])
def test_pre_submit_failure_requires_the_exact_owned_unconsumed_decision_boundary(fixture, missing):
    case = _case(fixture)
    receipt = _approve(fixture, case)["receipt"]
    identity = {"authorization_id": receipt["authorizationId"] if missing != "input_hash" else "different-attempt"}
    operation, _ = fixture.service.operations.create(user_subject=OWNER, capability_name="oa.leave.approve",
        capability_version=fixture.service.registry.get("oa.leave.approve").version,
        input_summary={"authorization_id": "PRIVATE_BOUNDARY"}, input_identity=identity)
    failed = fixture.service.operations.mark_failed(operation["operation_id"], code="CAPABILITY_EXECUTION_FAILED",
        message="PRIVATE_FAILURE_DETAIL {\"token\":\"PRIVATE_TOKEN\"}")
    fixture.service.observe_host_task(user_subject=OWNER, task_id=case["task_id"], operation_ids=[failed["operation_id"]])
    with sqlite3.connect(fixture.service.db_path) as connection:
        if missing == "decision":
            connection.execute("DELETE FROM business_draft_decisions WHERE authorization_id=?", (receipt["authorizationId"],))
        elif missing == "task_link":
            connection.execute("DELETE FROM task_operations WHERE task_id=? AND operation_id=?",
                               (case["task_id"], operation["operation_id"]))
        elif missing == "approved_state":
            connection.execute("UPDATE write_authorizations SET state='expired' WHERE authorization_id=?",
                               (receipt["authorizationId"],))
        elif missing == "later_consumed":
            connection.execute("UPDATE write_authorizations SET state='consumed',commit_operation_id=?,consumed_at=? "
                               "WHERE authorization_id=?", (operation["operation_id"], "2026-10-23T01:02:03Z", receipt["authorizationId"]))
        elif missing == "parent_unknown":
            connection.execute("UPDATE agent_tasks SET status='outcome_unknown' WHERE task_id=?", (case["task_id"],))
    detail = fixture.app.task_detail(fixture.account, case["task_id"])
    outcome = detail["businessExecution"]
    assert outcome["phase"] == ("outcome_unknown" if missing == "parent_unknown" else "failed" if missing else "not_submitted")
    assert outcome.get("reason") == (None if missing else "pre_submit_validation_failed")
    assert "PRIVATE_" not in json.dumps(detail)
    assert fixture.oa.writes == []


def test_unknown_http_detail_is_owned_and_continuation_cannot_dispatch_a_model_or_write(fixture):
    case = _case(fixture)
    _unknown(fixture, case, 'old result {"readback":{"reason":"still_pending"},"private":"PRIVATE_HTTP"}')
    cards, server = fixture.servers()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    origin = f"http://127.0.0.1:{port}"
    try:
        status, headers, _ = _request(port, "POST", "/api/login", origin=origin,
                                      body={"username": "cardreview", "password": PASSWORD})
        assert status == 200
        cookies = _cookies(headers)
        status, _, detail = _request(port, "GET", f"/api/tasks/{case['task_id']}", cookies=cookies)
        assert status == 200 and detail["businessExecution"]["reason"] == "still_pending"
        assert "PRIVATE_HTTP" not in json.dumps(detail)
        status, _, error = _request(port, "POST", f"/api/tasks/{case['task_id']}/continue", origin=origin,
            cookies=cookies, csrf=cookies["agentbridge_workspace_csrf"], body={})
        assert status == 400 and "只读查询" in error["error"]["message"]
        assert _request(port, "POST", "/api/actions/oa-pending-query", origin=origin, cookies=cookies,
                        body={"requestKey": "outcome-read-only"})[0] == 401
        status, _, query = _request(port, "POST", "/api/actions/oa-pending-query", origin=origin, cookies=cookies,
            csrf=cookies["agentbridge_workspace_csrf"], body={"requestKey": "outcome-read-only"})
        assert status == 202 and query["taskId"] != case["task_id"]
        assert fixture.service.tasks.get_task(case["task_id"], user_subject=OWNER)["status"] == "outcome_unknown"
        status, headers, _ = _request(port, "POST", "/api/login", origin=origin,
                                      body={"username": "cardother", "password": PASSWORD})
        assert status == 200
        foreign = _request(port, "GET", f"/api/tasks/{case['task_id']}", cookies=_cookies(headers))
        assert foreign[0] == 404
        assert "still_pending" not in json.dumps(foreign[2])
        assert fixture.oa.writes == []
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
        cards.server_close()
