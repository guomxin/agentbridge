"""Independent complete profile coverage, real materials, restart and stop gates."""
from contextlib import contextmanager
from copy import deepcopy
import http.client
import json
import re
from threading import Thread
import time
from uuid import uuid4

import pytest

from agentbridge.auth.business_draft_card import project_draft_state
from agentbridge.core.business_draft_profiles import PROFILES
from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from agentbridge.core import actionable_results
from agentbridge.core.oa_batch_remaining import remaining_presentation
from tests.support.action_matrix_acceptance import FIELDLESS, SYNTHETIC_BINDINGS
from tests.support.card_material_acceptance import CardMaterialAcceptanceFixture, ALL_OA_PROFILES
from tests.support.full_business_acceptance import OWNER, ALL_PENDING_PROFILES, FULL_REVIEW_PROFILES
from tests.support.latency_acceptance import JsonHttpClient
from tests.support.latency_action_matrix import MatrixHttpWorkload
from tests.test_workspace import PASSWORD


@contextmanager
def serving(fixture):
    servers = fixture.servers()
    threads = [Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield servers
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_all_29_field_profiles_plus_3_fieldless_and_18_type_mixed_batch_over_real_http(tmp_path):
    fixture = CardMaterialAcceptanceFixture(tmp_path, seed="coverage")
    rows = []
    try:
        actual = {case["capability"] for case in fixture.meta["material_cases"]}
        assert actual == {profile.prepare for profile in PROFILES} | set(FIELDLESS)
        assert len(actual) == 32 and len(PROFILES) == 29
        assert set(FULL_REVIEW_PROFILES) == {profile.review_kind for profile in PROFILES if profile.view_kind == "approval"}
        assert len(ALL_PENDING_PROFILES) == 14  # Historical benchmark remains fixed.
        with serving(fixture):
            workload = MatrixHttpWorkload(fixture, JsonHttpClient, PASSWORD)
            for case in fixture.meta["material_cases"]:
                row, _ = workload.consume(case)
                rows.append(row)
            for case in (fixture.add_material_batch(ALL_OA_PROFILES),):
                for index, profile in enumerate(ALL_OA_PROFILES):
                    state = BusinessDraftService(fixture.service).open(case["input_id"])["state"]
                    assert state["ui"]["reviewKind"] == profile
                    row, result = workload.consume(case)
                    rows.append(row)
                    if index < len(ALL_OA_PROFILES) - 1:
                        case = {**case, "input_id": result["nextAction"]["inputSubmissionId"],
                            "input_interaction": result["interaction"]["interactionId"]}
            case = fixture.add_pending_batch(("missed_punch", "missed_punch"), legacy=True)
            for index in range(2):
                row, result = workload.consume(case)
                rows.append(row)
                if index == 0:
                    case = {**case, "input_id": result["nextAction"]["inputSubmissionId"],
                        "input_interaction": result["interaction"]["interactionId"]}
        assert len(rows) == 52
        assert {row["capability"] for row in rows} == {profile.commit for profile in PROFILES} | {
            "smartlight.alarm.work_area.submit", "smartlight.alarm.work_area.revoke", "smartlight.alarm.dispose"}
        assert sum("synthetic OA prepare/commit boundary" in row["evidenceBoundary"] for row in rows) == len(SYNTHETIC_BINDINGS) == 8
        audit = fixture.audit()
        assert audit["decisions"] == 49 and audit["authorizations"] == {"consumed": 52}
        assert audit["syntheticWrites"] == 52 and audit["coordinatorErrors"] == []
        assert audit["modelDispatches"] == 0
        (tmp_path / "coverage-report.json").write_text(json.dumps({"rows": rows, "audit": audit}, ensure_ascii=False, indent=2))
    finally:
        fixture.close()


@pytest.mark.parametrize("profile,field_id", [("business_settlement", "field0002_id"),
    ("quarterly_performance", "field0006_id"), ("intellectual_property_reward", "field0008_id")])
def test_added_profile_raw_material_change_rejects_old_confirmation_without_effect(tmp_path, profile, field_id):
    fixture = CardMaterialAcceptanceFixture(tmp_path, seed="empty")
    try:
        case = fixture.add_pending_case(profile)
        drafts = BusinessDraftService(fixture.service)
        opened = drafts.open(case["input_id"])
        credentials = {"source_submission_id": case["input_id"], "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        state = drafts.save(**credentials, expected_revision=opened["state"]["revision"], values={"opinion": "已核对合成原材料。"})
        preview = drafts.preview(**credentials, expected_revision=state["revision"])
        raw = fixture.oa.data["affairs"][case["arguments"]["affair_id"]]["row_snapshot"]
        next(field for field in raw["fields"] if field["id"] == field_id)["value"] += "（材料已变更）"
        fixture.oa.persist()
        with pytest.raises(BusinessDraftError) as error:
            drafts.approve(**credentials, expected_revision=preview["revision"], preview_id=preview["preview"]["previewId"], request_key="changed-source-confirmation")
        assert error.value.code == "DRAFT_REVIEW_CHANGED"
        assert fixture.audit()["decisions"] == fixture.audit()["syntheticWrites"] == 0
        assert fixture.audit()["authorizations"] == {}
    finally:
        fixture.close()


def test_rich_materials_use_real_http_reads_and_keep_exact_long_pdf_bytes(tmp_path):
    fixture = CardMaterialAcceptanceFixture(tmp_path, seed="empty")
    try:
        case = fixture.add_pending_case("efficiency_data", material_previews=True)
        opened = BusinessDraftService(fixture.service).open(case["input_id"])
        review = project_draft_state(opened["state"])["ui"]["review"]
        materials = [item for item in review["materialPreviews"] if item["state"] == "available"]
        assert len(materials) == 6
        assert {item["kind"] for item in materials} == {"image", "pdf", "archive"}
        encoded = json.dumps(review, ensure_ascii=False)
        assert "未提供" in encoded and "效能原始数据表" in encoded
        bodies = {}
        with serving(fixture) as (cards, _):
            for item in materials:
                connection = http.client.HTTPConnection("127.0.0.1", cards.server_port, timeout=5)
                try:
                    connection.request("GET", f"/flow/input/{case['input_id']}/draft/materials/{item['id']}", headers={
                        "Origin": fixture.card_url, "Sec-Fetch-Site": "same-origin",
                        "X-AgentBridge-Draft-Token": opened["csrfToken"], "Cookie": "agentbridge_csrf=" + opened["csrfToken"]})
                    response = connection.getresponse()
                    body = response.read()
                    assert response.status == 200, body[:300]
                    assert response.getheader("Cache-Control") == "no-store"
                    bodies[item["name"]] = body
                    if item["kind"] == "image":
                        assert response.getheader("Content-Type") == "image/png" and body.startswith(b"\x89PNG")
                    elif item["kind"] == "archive":
                        assert [entry["state"] for entry in json.loads(body)["items"]] == ["available", "available", "unavailable"]
                finally:
                    connection.close()
        long_pdf = next(value for key, value in bodies.items() if "long-24" in key)
        scanned = next(value for key, value in bodies.items() if "scan-no" in key)
        single = next(value for key, value in bodies.items() if "one-long" in key)
        assert b"/Count 24" in long_pdf and b"UNIQUE-FOOTER-24" in long_pdf
        assert long_pdf.count(b"MATERIAL-CHECK") == 24 * 28
        assert b"/Subtype /Image" in scanned and b"/Font" not in scanned and b" Tj" not in scanned
        assert single.count(b"MATERIAL-CHECK") == 85
        assert len(fixture.oa.data["material_read_calls"]) == 6
        assert fixture.audit()["decisions"] == fixture.audit()["syntheticWrites"] == 0
        assert fixture.audit()["authorizations"] == {}
    finally:
        fixture.close()


@pytest.mark.parametrize("attitude", ["join", "not_join", "pending"])
def test_meeting_actual_reply_and_receipt_survive_restart_without_duplicate_effect(tmp_path, attitude):
    first = CardMaterialAcceptanceFixture(tmp_path, seed="empty")
    try:
        case = first.add_meeting_case()
        first.approve_case(case, values={"attitude": attitude, "feedback": "合成回复 " + attitude})
        first.drive_case(case)
        before = first.audit()
        meetings = deepcopy(first.oa.data["meetings"])
        assert before["decisions"] == before["syntheticWrites"] == 1
    finally:
        first.close()
    second = CardMaterialAcceptanceFixture(tmp_path, seed="empty")
    try:
        assert second.oa.data["meetings"] == meetings and second.audit() == before
        second.drive_case(case)
        assert second.audit() == before
        assert next(iter(second.oa.data["meetings"].values()))["view"]["myReply"]["feedbackFlag"] == {"join": 1, "not_join": 0, "pending": -1}[attitude]
    finally:
        second.close()


@pytest.mark.parametrize("kind", ["unknown", "material-failed"])
def test_stopped_batches_keep_real_effects_and_untouched_items_across_restart(tmp_path, kind):
    first = CardMaterialAcceptanceFixture(tmp_path, seed="empty")
    try:
        case = first.seed_stopped_batch(kind)
        before = first.audit()
        assert before["decisions"] == 2
        assert before["syntheticWrites"] == (2 if kind == "unknown" else 1)
        assert before["authorizations"] == ({"consumed": 2} if kind == "unknown" else {"consumed": 1, "approved": 1})
    finally:
        first.close()
    second = CardMaterialAcceptanceFixture(tmp_path, seed="empty")
    thread = Thread(target=second.coordinate, daemon=True)
    try:
        assert second.audit() == before
        thread.start()
        time.sleep(.7)
        second.stop_event.set()
        thread.join(timeout=2)
        assert second.audit() == before
        assert second.oa.data["affairs"][case["untouchedAffairs"][0]]["processed"] is False
        meeting = next(value for value in second.oa.data["meetings"].values() if value["affair_id"] == case["untouchedAffairs"][1])
        assert meeting["view"]["myReply"]["feedbackFlag"] == -100
    finally:
        second.close()
        thread.join(timeout=2)


@pytest.mark.parametrize("kind", ["unknown", "material-failed"])
def test_remaining_http_requery_and_fresh_mixed_batch_only_touch_original_untouched_tail(tmp_path, kind):
    fixture = CardMaterialAcceptanceFixture(tmp_path, seed="empty")
    try:
        stopped = fixture.seed_stopped_batch(kind)
        fixture.oa.add_affair("synthetic-unrelated-selection", profile="leave")
        original = fixture.service.tasks.get_batch_for_task(parent_task_id=stopped["task_id"], user_subject=OWNER)
        original_auth = fixture.service.write_authorizations.get(stopped["authorizationId"])
        before = fixture.audit()
        scope = remaining_presentation(fixture.service, OWNER, stopped["task_id"])
        assert scope["available"] and [item["ordinal"] for item in scope["items"]] == [3, 4]
        runtime = {"endpointId": fixture.account["endpoint_id"], "hostType": "openclaw", "hostInstanceId": "synthetic-material-host"}
        with serving(fixture):
            workload = MatrixHttpWorkload(fixture, JsonHttpClient, PASSWORD)
            path = "/api/tasks/" + stopped["task_id"] + "/oa-batch-remaining/query"
            body = {"requestKey": uuid4().hex, "expectedBatchVersion": scope["batchVersion"]}
            assert workload.workspace.request("GET", path)[0] in {404, 405}
            untrusted = JsonHttpClient(fixture.workspace_url)
            untrusted.cookies = dict(workload.workspace.cookies)
            assert untrusted.json("POST", path, body)[0] in {401, 403}
            status, queued, _ = workload.workspace.json("POST", path, body, csrf=True)
            assert status == 202, queued
            status, replay, _ = workload.workspace.json("POST", path, body, csrf=True)
            assert status == 202 and replay["taskId"] == queued["taskId"]
            assert fixture.audit() == before
            query = actionable_results.execute_preparation(fixture.service, OWNER, queued["taskId"], queued["commandKey"], runtime_context=runtime)
            assert query["status"] == "succeeded", query
            assert query["queryResult"]["remaining"]["matchedCount"] == 2
            groups = actionable_results.list_actionable_results(fixture.service, OWNER, queued["taskId"])
            assert len(groups) == 1 and len(groups[0]["targets"]) == 2
            operation = fixture.service.operations.get(query["operationId"])
            selected_keys = {item["targetKey"] for item in groups[0]["targets"]}
            actual_ids = {item["affair_id"] for item in operation["result"]["items"]
                if actionable_results._target_key(operation["operation_id"], item) in selected_keys}
            assert actual_ids == set(stopped["untouchedAffairs"])
            unrelated = next(item for item in operation["result"]["items"] if item["affair_id"] == "synthetic-unrelated-selection")
            prepare_path = "/api/tasks/" + queued["taskId"] + "/prepare-selection"
            forged = {"operationId": query["operationId"], "targetKeys": [actionable_results._target_key(query["operationId"], unrelated)], "requestKey": uuid4().hex}
            status, denied, _ = workload.workspace.json("POST", prepare_path, forged, csrf=True)
            assert status == 409 and denied["error"]["code"] == "BATCH_REMAINING_SELECTION_INVALID"
            status, intent, _ = workload.workspace.json("POST", prepare_path, {"operationId": query["operationId"],
                "targetKeys": [target["targetKey"] for target in groups[0]["targets"]], "requestKey": uuid4().hex}, csrf=True)
            assert status == 202, intent
            prepared = actionable_results.execute_preparation(fixture.service, OWNER, intent["taskId"], intent["commandKey"], runtime_context=runtime)
            assert prepared["status"] == "requires_user_action", prepared
            assert intent["taskId"] != stopped["task_id"]
            assert fixture.audit()["decisions"] == before["decisions"]
            case = {"task_id": intent["taskId"], "profile": "remaining_mixed", "input_id": prepared["nextAction"]["inputSubmissionId"],
                "input_interaction": prepared["interaction"]["interactionId"]}
            for ordinal in range(2):
                _, result = workload.consume(case)
                if not ordinal:
                    case = {**case, "input_id": result["nextAction"]["inputSubmissionId"],
                        "input_interaction": result["interaction"]["interactionId"]}
        assert fixture.audit()["decisions"] == before["decisions"] + 2
        assert fixture.audit()["syntheticWrites"] == before["syntheticWrites"] + 2
        assert fixture.service.tasks.get_batch_for_task(parent_task_id=stopped["task_id"], user_subject=OWNER) == original
        assert fixture.service.write_authorizations.get(stopped["authorizationId"]) == original_auth
        assert fixture.audit()["coordinatorErrors"] == []
    finally:
        fixture.close()


def test_meeting_lost_receipt_persists_unknown_and_never_replies_twice_after_restart(tmp_path):
    fixture = CardMaterialAcceptanceFixture(tmp_path, seed="empty")
    try:
        case = fixture.add_meeting_case()
        decision = fixture.approve_case(case, values={"attitude": "pending", "feedback": "合成待定回执"})
        next(iter(fixture.oa.data["meetings"].values()))["fault"] = "lost_receipt"
        fixture.oa.persist()
        fixture.drive_case(case)
        auth = fixture.service.write_authorizations.get(decision["receipt"]["authorizationId"])
        assert auth["state"] == "consumed"
        assert fixture.service.operations.get(auth["commit_operation_id"])["status"] == "unknown"
        before = fixture.audit()
        assert before["decisions"] == before["syntheticWrites"] == 1
    finally:
        fixture.close()
    fixture = CardMaterialAcceptanceFixture(tmp_path, seed="empty")
    try:
        fixture.drive_case(case)
        assert fixture.audit() == before
        assert fixture.service.operations.get(auth["commit_operation_id"])["status"] == "unknown"
        assert next(iter(fixture.oa.data["meetings"].values()))["view"]["myReply"]["feedbackFlag"] == -1
    finally:
        fixture.close()
