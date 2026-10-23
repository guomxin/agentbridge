"""Workspace recovery transport and isolated OA draft boundaries."""
from contextlib import contextmanager
import json
import sqlite3
import threading
from unittest.mock import patch

import pytest

from agentbridge.core.business_drafts import BusinessDraftService
from agentbridge.core.oa_pending_recovery import ACK_VERSION, OaPendingRecoveryError
from tests.support.business_view_acceptance import BusinessViewAcceptanceFixture, OWNER
from tests.support.pending_readiness_acceptance import PendingReadinessAcceptanceFixture
from tests.test_full_business_acceptance import decide, drive
from tests.test_workspace import PASSWORD, _cookies, _request, _raw_request


ASSESSMENT = "33333333-3333-3333-3333-333333333333"
CHILD = "22222222-2222-2222-2222-222222222222"
ASSESSMENT_HASH = "sha256:" + "a" * 64


@contextmanager
def workspace_http(fixture):
    cards, server = fixture.servers()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    origin = f"http://127.0.0.1:{port}"
    try:
        status, headers, _ = _request(port, "POST", "/api/login", origin=origin,
            body={"username":"cardreview", "password":PASSWORD})
        assert status == 200
        cookies = _cookies(headers)
        def post(path, body, **options):
            return _request(port, "POST", path, body=body, **{
                "origin":origin, "cookies":cookies, "csrf":cookies["agentbridge_workspace_csrf"], **options})
        post.get = lambda path: _request(port,"GET",path,cookies=cookies)
        post.stream = lambda path: _raw_request(port,"GET",path,cookies=cookies)
        yield post, port, origin
    finally:
        server.shutdown(); thread.join(timeout=2); server.server_close(); cards.server_close()


def prepare_body(**overrides):
    return {"assessmentId":ASSESSMENT, "assessmentHash":ASSESSMENT_HASH, "expectedRevision":1,
            "requestKey":"synthetic-prepare-key", "acknowledgement":ACK_VERSION, **overrides}


def test_recovery_http_authenticates_origin_csrf_owner_and_passes_only_the_authenticated_endpoint(tmp_path):
    fixture = BusinessViewAcceptanceFixture(tmp_path)
    try:
        task_id = fixture.meta["view_cases"][0]["task_id"]
        path = f"/api/tasks/{task_id}/oa-pending-recovery/probe"
        private = {"authorization":"PRIVATE_AUTH", "principal":"PRIVATE_PRINCIPAL", "plan":{"secret":"PRIVATE_PLAN"}}
        response = {"status":"blocked", "canPrepare":False, "reason":"read_unavailable", "doneCoverage":"partial", **private}
        with workspace_http(fixture) as (post, port, origin), patch(
                "agentbridge.core.oa_pending_recovery.probe_unknown_pending", return_value=response) as probe:
            body = {"requestKey":"synthetic-probe-key"}
            assert post(path, body, csrf=None)[0] == 401
            assert post(path, body, origin="http://example.invalid")[0] == 403
            assert post(path, body, cookies={}, csrf=None)[0] == 401
            assert post(path, {**body, "endpointId":"forged"})[0] == 400
            assert post(path, {**body, "capability":"oa.efficiency_data.approve"})[0] == 400
            status, _, public = post(path, body)
            assert status == 200
            assert public == {"status":"blocked", "canPrepare":False, "reason":"read_unavailable", "doneCoverage":"partial", "canExecute":False}
            assert "PRIVATE_" not in json.dumps(public)
            assert probe.call_count == 1
            assert probe.call_args.kwargs == {"subject":OWNER, "task_id":task_id,
                "request_key":"synthetic-probe-key", "endpoint_id":fixture.account["endpoint_id"]}

            status, headers, _ = _request(port,"POST","/api/login",origin=origin,
                body={"username":"cardother","password":PASSWORD})
            assert status == 200
            other = _cookies(headers)
            assert post(path, body, cookies=other, csrf=other["agentbridge_workspace_csrf"])[0] == 404
            assert probe.call_count == 1
        assert fixture.oa.writes == []
    finally:
        fixture.close()


def test_prepare_http_rejects_missing_ack_revision_hash_or_extra_arguments_and_preserves_core_conflicts(tmp_path):
    fixture = BusinessViewAcceptanceFixture(tmp_path)
    try:
        task_id = fixture.meta["view_cases"][0]["task_id"]
        path = f"/api/tasks/{task_id}/oa-pending-recovery/prepare"
        response = {"status":"existing", "taskId":CHILD, "originalTaskId":task_id,
                    "assessmentId":ASSESSMENT, "authorizationId":"PRIVATE_AUTH"}
        with workspace_http(fixture) as (post, _port, _origin), patch(
                "agentbridge.core.oa_pending_recovery.prepare_recovery_attempt", return_value=response) as prepare:
            for invalid in [prepare_body(acknowledgement=False), prepare_body(expectedRevision=True),
                    prepare_body(assessmentHash="wrong"), prepare_body(assessmentId="wrong"),
                    {key:value for key,value in prepare_body().items() if key != "acknowledgement"},
                    {**prepare_body(), "opinion":"carry-over old opinion"}, {**prepare_body(), "endpointId":"forged"}]:
                assert post(path, invalid)[0] == 400
            assert not prepare.called
            status, _, public = post(path, prepare_body())
            assert status == 200 and public["taskId"] == CHILD and public["canExecute"] is False
            assert "PRIVATE_AUTH" not in json.dumps(public)
            assert prepare.call_args.kwargs == {"subject":OWNER,"task_id":task_id,
                "assessment_id":ASSESSMENT,"assessment_hash":ASSESSMENT_HASH,"expected_revision":1,
                "request_key":"synthetic-prepare-key","acknowledgement":ACK_VERSION,
                "endpoint_id":fixture.account["endpoint_id"]}
            prepare.side_effect = OaPendingRecoveryError("OA_RECOVERY_ASSESSMENT_STALE", "请重新只读核对原事项。")
            status, _, error = post(path, prepare_body())
            assert status == 409 and error["error"] == {"code":"OA_RECOVERY_ASSESSMENT_STALE","message":"请重新只读核对原事项。"}
        assert fixture.oa.writes == []
    finally:
        fixture.close()


def _unknown_case(fixture):
    case = fixture.meta["full_cases"][0]
    drafts = BusinessDraftService(fixture.service)
    opened = drafts.open(case["input_id"])
    credentials = {"source_submission_id":case["input_id"], "presentation_id":None,
        "csrf_token":opened["csrfToken"], "csrf_cookie":opened["csrfToken"]}
    saved = drafts.save(**credentials,expected_revision=opened["state"]["revision"],values={"opinion":"原单明确意见"})
    preview = drafts.preview(**credentials,expected_revision=saved["revision"])
    decision = decide(drafts,credentials,preview)
    drive(fixture,case)
    assert fixture.service.tasks.get_task(case["task_id"],user_subject=OWNER)["status"] == "outcome_unknown"
    return case, decision


def test_http_original_unknown_can_only_create_one_new_blank_draft_without_reusing_a_decision_or_writing(tmp_path):
    fixture = PendingReadinessAcceptanceFixture(tmp_path,modes=("unknown",))
    try:
        case, decision = _unknown_case(fixture)
        original = fixture.service.tasks.get_task(case["task_id"],user_subject=OWNER)
        original_auth = fixture.service.write_authorizations.get(decision["receipt"]["authorizationId"])
        base = f"/api/tasks/{case['task_id']}/oa-pending-recovery"
        writes = list(fixture.oa.writes)
        with workspace_http(fixture) as (post, port, _origin):
            status, _, probe = post(base+"/probe", {"requestKey":"real-isolated-probe"})
            assert status == 200 and probe["status"] == "unresolved_still_pending", probe
            assert probe["canPrepare"] is True and probe["canExecute"] is False
            status, _, original_projection = post.get(f"/api/tasks/{case['task_id']}")
            assert status == 200
            current = original_projection["oaPendingRecovery"]
            assert current["status"] == "unresolved_still_pending" and current["observedAt"] == probe["observedAt"]
            assert current["canPrepare"] is False and current["canExecute"] is False
            assert not {"assessmentId", "assessmentHash", "revision", "scopeHash", "sessionHash"}.intersection(current)
            status, _, recovery = post(base+"/prepare", {"assessmentId":probe["assessmentId"],
                "assessmentHash":probe["assessmentHash"],"expectedRevision":probe["revision"],
                "requestKey":"real-isolated-prepare","acknowledgement":ACK_VERSION})
            assert status == 200 and recovery["status"] == "prepared", recovery
            assert recovery["taskId"] != case["task_id"] and recovery["canExecute"] is False
            status, _, restored = post(base+"/prepare", {"assessmentId":probe["assessmentId"],
                "assessmentHash":probe["assessmentHash"],"expectedRevision":probe["revision"],
                "requestKey":"real-isolated-prepare","acknowledgement":ACK_VERSION})
            assert status == 200 and restored["status"] == "existing" and restored["taskId"] == recovery["taskId"]
            child = fixture.app.task_detail(fixture.account,recovery["taskId"])
            assert child["task"]["status"] == "waiting_user"
            assert child["task"]["summary"]["oaPendingRecovery"] == {"originalTaskId":case["task_id"]}
            assert case["task_id"] in child["actionableSourceTaskIds"]
            status, _, original_projection = post.get(f"/api/tasks/{case['task_id']}")
            assert status == 200 and original_projection["task"]["status"] == "outcome_unknown"
            assert original_projection["oaPendingRecovery"]["status"] == "recovery_in_progress"
            assert original_projection["oaPendingRecovery"]["recoveryTaskId"] == recovery["taskId"]
            public = json.dumps(child,ensure_ascii=False)
            assert "scopeHash" not in public and "sessionHash" not in public and "permitHash" not in public
            assert original_auth["authorization_id"] not in public
            input_interaction = next(item["interactionId"] for item in child["interactions"] if item["type"] == "business_input")
            input_id = fixture.service.interactions.get(input_interaction,user_subject=OWNER)["resource_id"]
            assert input_id != case["input_id"]
            opened = BusinessDraftService(fixture.service).open(input_id)
            assert not opened["state"]["values"].get("opinion")
            status, _, timeline = post.get("/api/timeline?entry_type=task_event&limit=500")
            assert status == 200
            recovery_events = [entry for entry in timeline["items"]
                if entry["payload"].get("eventType", "").startswith("task.oa_recovery.")]
            assert recovery_events
            for entry in recovery_events:
                assert entry["payload"] == {"eventType":entry["payload"]["eventType"],"taskId":entry["task_id"]}
            with patch("agentbridge.workspace.server.WORKSPACE_STREAM_LIFETIME_SECONDS",.1):
                status, _, wire = post.stream("/api/timeline/stream?after=0")
            assert status == 200
            streamed = [json.loads(line.removeprefix("data: ")) for line in wire.splitlines() if line.startswith("data: ")]
            streamed_recovery = [entry for entry in streamed if (entry.get("payload") or {}).get("eventType", "").startswith("task.oa_recovery.")]
            assert streamed_recovery
            for entry in streamed_recovery:
                assert entry["payload"] == {"eventType":entry["payload"]["eventType"],"taskId":entry["task_id"]}
            # The live stream retains only the minimal event; its child read
            # supplies the verified invalidation edge and the original's new
            # read-only presentation without leaking recovery credentials.
            assert recovery["taskId"] in {entry["task_id"] for entry in streamed_recovery}
            status, _, projections = post.get("/api/tasks/projections?ids=" + recovery["taskId"])
            assert status == 200
            child_projection = projections["items"][0]["detail"]
            assert case["task_id"] in child_projection["actionableSourceTaskIds"]
            assert not any(secret in json.dumps(child_projection) for secret in ("sessionHash", "scopeHash", "permitHash"))
        assert fixture.oa.writes == writes
        assert fixture.service.write_authorizations.get(original_auth["authorization_id"]) == original_auth
        assert fixture.service.tasks.get_task(case["task_id"],user_subject=OWNER)["status"] == "outcome_unknown"
        assert fixture.service.tasks.get_task(case["task_id"],user_subject=OWNER)["current_operation_id"] == original["current_operation_id"]
        with sqlite3.connect(fixture.service.db_path) as connection:
            assert connection.execute("SELECT count(*) FROM business_draft_decisions").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM write_authorizations").fetchone()[0] == 1
            private = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM task_events WHERE event_type='task.oa_recovery.assessed'")]
            assert any("sessionHash" in record and "scopeHash" in record for record in private)
    finally:
        fixture.close()


def test_http_transient_source_failure_recovers_only_the_same_child_with_fresh_evidence_and_no_new_authority(tmp_path):
    fixture=PendingReadinessAcceptanceFixture(tmp_path,modes=("unknown",))
    try:
        case, _decision=_unknown_case(fixture)
        base=f"/api/tasks/{case['task_id']}/oa-pending-recovery"
        writes=list(fixture.oa.writes)
        with workspace_http(fixture) as (post, _port, _origin):
            status, _, first=post(base+"/probe",{"requestKey":"transient-first-probe"})
            assert status == 200 and first["canPrepare"]
            actual=fixture.service.invoke
            def failure(**kwargs):
                if kwargs["capability_name"] == "oa.efficiency_data.approval.prepare":
                    with patch.object(fixture.service.adapter_for_system("oa"),"resolve_workflow_detail",side_effect=ConnectionError("synthetic transient source read")):
                        return actual(**kwargs)
                return actual(**kwargs)
            with patch.object(fixture.service,"invoke",side_effect=failure):
                status, _, error=post(base+"/prepare",prepare_body(assessmentId=first["assessmentId"],assessmentHash=first["assessmentHash"]))
            assert status == 409 and error["error"]["code"] == "OA_RECOVERY_PREPARE_FAILED"
            from agentbridge.core.oa_pending_recovery import recovery_child_for_origin
            child_id=recovery_child_for_origin(fixture.service,subject=OWNER,task_id=case["task_id"])
            assert child_id and fixture.service.tasks.get_task(child_id,user_subject=OWNER)["status"] == "failed"
            status, _, second=post(base+"/probe",{"requestKey":"transient-second-probe"})
            assert status == 200 and second["recoveryTaskId"] == child_id
            assert second["childPreparation"] == "retryable" and second["canPrepare"] is True
            status, _, resumed=post(base+"/prepare",prepare_body(assessmentId=second["assessmentId"],assessmentHash=second["assessmentHash"],requestKey="transient-second-prepare"))
            assert status == 200 and resumed["status"] == "existing" and resumed["taskId"] == child_id
            child=fixture.app.task_detail(fixture.account,child_id)
            assert child["task"]["status"] == "waiting_user"
            interaction=next(item for item in child["interactions"] if item["type"] == "business_input")
            source=fixture.service.interactions.get(interaction["interactionId"],user_subject=OWNER)["resource_id"]
            assert not BusinessDraftService(fixture.service).open(source)["state"]["values"].get("opinion")
            status, _, ready=post(base+"/probe",{"requestKey":"transient-ready-probe"})
            assert status == 200 and ready["recoveryTaskId"] == child_id
            assert ready["childPreparation"] == "ready" and ready["canPrepare"] is False
        assert fixture.oa.writes == writes
        assert fixture.service.tasks.get_task(case["task_id"],user_subject=OWNER)["status"] == "outcome_unknown"
        with sqlite3.connect(fixture.service.db_path) as connection:
            assert connection.execute("SELECT count(*) FROM business_draft_decisions").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM write_authorizations").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM agent_tasks WHERE json_extract(summary_json,'$.oaPendingRecovery.kind')='oa_pending_recovery_v1'").fetchone()[0] == 1
    finally:
        fixture.close()
