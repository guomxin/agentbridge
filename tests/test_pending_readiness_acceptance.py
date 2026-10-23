"""Authorization/effect assertions through the real Central single-decision flow."""
import json
import sqlite3

import pytest

from agentbridge.core.business_drafts import BusinessDraftService
from tests.support.pending_readiness_acceptance import PendingReadinessAcceptanceFixture
from tests.test_full_business_acceptance import decide, drive


@pytest.mark.parametrize("mode", ["late", "never", "identity_drift", "material_drift", "unknown"])
def test_real_decision_body_readiness_preserves_consumption_and_effect_boundaries(tmp_path, mode):
    fixture = PendingReadinessAcceptanceFixture(tmp_path, modes=(mode,))
    try:
        case = fixture.meta["full_cases"][0]
        drafts = BusinessDraftService(fixture.service)
        opened = drafts.open(case["input_id"])
        credentials = {"source_submission_id": case["input_id"], "presentation_id": None,
                       "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"], values={"opinion": "合成同意意见"})
        preview = drafts.preview(**credentials, expected_revision=saved["revision"])
        decision = decide(drafts, credentials, preview)
        drive(fixture, case)
        authorization = fixture.service.write_authorizations.get(decision["receipt"]["authorizationId"])
        with sqlite3.connect(fixture.service.db_path) as db:
            status, code, message, result_json = db.execute(
                "SELECT o.status,o.error_code,o.error_message,o.result_json FROM operations o "
                "JOIN task_operations t USING(operation_id) WHERE t.task_id=? "
                "AND o.capability_name='oa.efficiency_data.approve'", (case["task_id"],),
            ).fetchone()
            assert db.execute("SELECT count(*) FROM business_draft_decisions").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM write_authorizations").fetchone()[0] == 1
        if mode in {"never", "identity_drift", "material_drift"}:
            assert status == "failed", (code, message)
            assert authorization["state"] == "approved"
            assert authorization["consumed_at"] is None
            assert fixture.oa.writes == []
            if mode == "never":
                assert code == "OA_PENDING_ACTION_NOT_READY"
        else:
            assert status == ("succeeded" if mode == "late" else "unknown"), (code, message)
            assert authorization["state"] == "consumed"
            assert len(fixture.oa.writes) == 1
            if mode == "late":
                result = json.loads(result_json)
                assert result["verification"]["done_identity_confirmed"] is True
                assert result["execution"]["readiness"]["wait_ms"] >= 300
            drive(fixture, case)
            assert len(fixture.oa.writes) == 1
        assert any(not row["ready"] for row in fixture.oa.data["readiness_observations"])
    finally:
        fixture.close()
