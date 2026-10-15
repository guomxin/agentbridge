"""Independent action-matrix acceptance, with evidence boundaries kept explicit."""
import json
import re
import sqlite3
from urllib.parse import urlencode

import pytest

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.core.business_drafts import BusinessDraftService
from tests.support.action_matrix_acceptance import (
    ActionMatrixAcceptanceFixture, MATRIX_CAPABILITIES, FIELDLESS, SYNTHETIC_BINDINGS,
    SYNTHETIC_EVIDENCE, HTTP_EVIDENCE,
)
from tests.test_full_business_acceptance import drive


@pytest.fixture
def matrix_fixture(tmp_path):
    fixture = ActionMatrixAcceptanceFixture(tmp_path, seed=False)
    try:
        yield fixture
    finally:
        fixture.close()


def effects(fixture):
    return (len(fixture.matrix["effects"]) + len(fixture.smartlight.data["writes"])
            + fixture.transport.write_calls + len(fixture.oa.writes))


def preview_case(fixture, case):
    drafts = BusinessDraftService(fixture.service)
    opened = drafts.open(case["input_id"])
    credentials = {"source_submission_id": case["input_id"], "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
    state = opened["state"]
    raw = {}
    for field in state["ui"]["fields"]:
        name, control = field["name"], field["control"]
        raw[name] = ("2026-10-21" if control == "date" else
            "2026-10-21T10:00" if control == "datetime-local" and name == "start_time" else
            "2026-10-21T11:00" if control == "datetime-local" else
            str(field["options"][0]["value"]) if control in {"select", "segmented"} and field.get("options") else
            "2" if control == "number" else "合成动作矩阵验收")
    if "project" in raw:
        raw["project"] = "合成验收项目"
    if state["ui"]["viewKind"] == "meeting_room":
        raw["room"] = ""
    state = drafts.save(**credentials, expected_revision=state["revision"], values=raw)
    if state["ui"]["viewKind"] == "meeting_room":
        state = drafts.options(**credentials, expected_revision=state["revision"])
        raw["room"] = state["options"]["items"][0]["value"]
        state = drafts.save(**credentials, expected_revision=state["revision"], values=raw)
    state = drafts.preview(**credentials, expected_revision=state["revision"])
    return drafts, credentials, state


def decide(fixture, case):
    if case["capability"] not in FIELDLESS:
        drafts, credentials, state = preview_case(fixture, case)
        assert effects(fixture) == 0
        kwargs = {**credentials, "expected_revision": state["revision"], "preview_id": state["preview"]["previewId"], "request_key": "matrix-final-decision"}
        result = drafts.approve(**kwargs)
        assert drafts.approve(**kwargs)["receipt"] == result["receipt"]
        return result["receipt"]["authorizationId"]
    app = TrustedActionApplication(authorization_store=fixture.service.write_authorizations)
    page = app.get_card(case["authorization_id"], secure_cookie=False)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.body.decode()).group(1)
    cookie = page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    response = app.submit_card(case["authorization_id"], body=urlencode({"csrf_token": csrf, "decision": "approve"}).encode(),
        content_type="application/x-www-form-urlencoded", csrf_cookie=cookie)
    assert response.status == 200
    return case["authorization_id"]


@pytest.mark.parametrize("capability", MATRIX_CAPABILITIES)
def test_each_remaining_action_uses_original_coordinator_and_one_effect(matrix_fixture, capability):
    fixture = matrix_fixture
    case = fixture.add_matrix_case(capability)
    assert case["evidence_boundary"] == (SYNTHETIC_EVIDENCE if capability in SYNTHETIC_BINDINGS else HTTP_EVIDENCE)
    assert effects(fixture) == 0
    authorization_id = decide(fixture, case)
    assert effects(fixture) == 0
    drive(fixture, case)
    authorization = fixture.service.write_authorizations.get(authorization_id)
    operation = fixture.service.operations.get(authorization["commit_operation_id"])
    assert operation["status"] == "succeeded", operation
    assert authorization["state"] == "consumed"
    assert effects(fixture) == 1
    drive(fixture, case)
    assert effects(fixture) == 1
    with sqlite3.connect(fixture.service.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM write_authorizations").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM business_draft_decisions").fetchone() == (0 if capability in FIELDLESS else 1,)


@pytest.mark.parametrize("capability", ["oa.leave.submit.prepare", "smartlight.alarm.dispose.prepare"])
def test_unknown_effect_does_not_replay_or_claim_success(matrix_fixture, capability):
    fixture = matrix_fixture
    case = fixture.add_matrix_case(capability)
    authorization_id = decide(fixture, case)
    (fixture.matrix if capability in SYNTHETIC_BINDINGS else fixture.smartlight.data)["fault"] = "lost_reply"
    drive(fixture, case)
    authorization = fixture.service.write_authorizations.get(authorization_id)
    operation = fixture.service.operations.get(authorization["commit_operation_id"])
    assert operation["status"] == "unknown", operation
    drive(fixture, case)
    assert effects(fixture) == 1


def test_matrix_restart_keeps_existing_tasks_and_evidence(tmp_path):
    fixture = ActionMatrixAcceptanceFixture(tmp_path)
    original_cases = json.loads(json.dumps(fixture.meta["matrix_cases"]))
    prior_case = fixture.add_pending_case("leave")
    fixture.close()
    restarted = ActionMatrixAcceptanceFixture(tmp_path)
    try:
        assert restarted.meta["matrix_cases"] == original_cases
        assert prior_case in restarted.meta["full_cases"]
        assert len(restarted.meta["matrix_cases"]) == 14
        assert effects(restarted) == 0
    finally:
        restarted.close()


def test_matrix_workers_reject_external_requests(matrix_fixture):
    for transport in (matrix_fixture.oa, matrix_fixture.smartlight, matrix_fixture.transport):
        with pytest.raises(AssertionError):
            transport.request("POST", "https://example.com/anything")
    with pytest.raises(RuntimeError, match="disabled"):
        matrix_fixture._deny_worker()
