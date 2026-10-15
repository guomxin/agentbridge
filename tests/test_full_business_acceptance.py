"""Independent complete OA single-decision integration against real adapters."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture, ALL_PENDING_PROFILES, OWNER


@pytest.fixture
def full_fixture():
    with TemporaryDirectory() as directory:
        fixture = FullBusinessAcceptanceFixture(Path(directory), seed=False)
        try:
            yield fixture
        finally:
            fixture.close()


def prepared(fixture, profile):
    case = fixture.add_pending_case(profile)
    drafts = BusinessDraftService(fixture.service)
    opened = drafts.open(case["input_id"])
    credentials = {"source_submission_id": case["input_id"], "presentation_id": None,
                   "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
    saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"],
                        values={"opinion": "已核对完整合成材料，本次为隔离验收。"})
    preview = drafts.preview(**credentials, expected_revision=saved["revision"])
    return case, drafts, credentials, preview


def decide(drafts, credentials, preview):
    return drafts.approve(**credentials, expected_revision=preview["revision"],
                          preview_id=preview["preview"]["previewId"], request_key="full-single-decision")


def drive(fixture, case):
    interaction_id = case["input_interaction"]
    for _ in range(4):
        result = fixture.resume(interaction_id)
        interaction = result.get("interaction")
        if interaction and interaction["resume"]["ready"]:
            interaction_id = interaction["interactionId"]
        else:
            return result
    raise AssertionError("Original coordinator did not settle")


@pytest.mark.parametrize("profile", ALL_PENDING_PROFILES)
def test_every_oa_type_has_one_decision_one_effect_and_authoritative_readback(full_fixture, profile):
    case, drafts, credentials, preview = prepared(full_fixture, profile)
    assert full_fixture.oa.writes == []
    decision = decide(drafts, credentials, preview)
    assert decide(drafts, credentials, preview)["receipt"] == decision["receipt"]
    assert full_fixture.oa.writes == []
    drive(full_fixture, case)
    authorization = full_fixture.service.write_authorizations.get(decision["receipt"]["authorizationId"])
    operation = full_fixture.service.operations.get(authorization["commit_operation_id"])
    assert operation["status"] == "succeeded", operation
    assert operation["result"]["verification"]["confirmed"] is True
    assert operation["result"]["verification"]["done_identity_confirmed"] is True
    assert authorization["state"] == "consumed"
    drive(full_fixture, case)
    assert len(full_fixture.oa.writes) == 1
    with sqlite3.connect(full_fixture.service.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM business_draft_decisions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM write_authorizations").fetchone() == (1,)


@pytest.mark.parametrize("profile", ["missed_punch", "efficiency_data", "weekly_report", "standard_collaboration"])
def test_full_body_change_requires_new_explicit_decision(full_fixture, profile):
    case, drafts, credentials, preview = prepared(full_fixture, profile)
    full_fixture.oa.data["affairs"][case["arguments"]["affair_id"]]["body_text"] += "新增的重要材料。"
    with pytest.raises(BusinessDraftError) as caught:
        decide(drafts, credentials, preview)
    assert caught.value.code == "DRAFT_REVIEW_CHANGED"
    assert full_fixture.oa.writes == []
    with sqlite3.connect(full_fixture.service.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM business_draft_decisions").fetchone() == (0,)
    replacement = caught.value.current
    assert replacement["preview"]["previewId"] != preview["preview"]["previewId"]
    decide(drafts, credentials, replacement)
    drive(full_fixture, case)
    assert len(full_fixture.oa.writes) == 1


def test_material_change_after_decision_does_not_cross_write_boundary(full_fixture):
    case, drafts, credentials, preview = prepared(full_fixture, "weekly_report")
    decision = decide(drafts, credentials, preview)
    full_fixture.oa.data["affairs"][case["arguments"]["affair_id"]]["body_text"] += "决定后发生的变化。"
    drive(full_fixture, case)
    assert full_fixture.oa.writes == []
    authorization = full_fixture.service.write_authorizations.get(decision["receipt"]["authorizationId"])
    assert authorization["state"] == "approved"


@pytest.mark.parametrize("legacy", [False, True])
def test_batch_every_item_uses_its_own_view_and_one_decision(full_fixture, legacy):
    profiles = ("missed_punch", "missed_punch") if legacy else ALL_PENDING_PROFILES
    case = full_fixture.add_pending_batch(profiles, legacy=legacy)
    drafts = BusinessDraftService(full_fixture.service)
    original_case = dict(case)
    for ordinal, profile in enumerate(profiles, 1):
        opened = drafts.open(case["input_id"])
        assert opened["state"]["ui"]["reviewKind"] == profile
        credentials = {"source_submission_id": case["input_id"], "presentation_id": None,
                       "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"],
                            values={"opinion": f"已核对第 {ordinal} 项合成材料。"})
        preview = drafts.preview(**credentials, expected_revision=saved["revision"])
        decision = decide(drafts, credentials, preview)
        assert decide(drafts, credentials, preview)["receipt"] == decision["receipt"]
        result = drive(full_fixture, case)
        assert len(full_fixture.oa.writes) == ordinal
        batch = full_fixture.service.tasks.get_batch_for_task(parent_task_id=case["task_id"], user_subject=OWNER)
        assert batch["succeeded_count"] == ordinal
        if ordinal < len(profiles):
            assert result["interaction"]["type"] == "business_input", result
            case = {**case, "input_id": result["nextAction"]["inputSubmissionId"],
                    "input_interaction": result["interaction"]["interactionId"]}
        else:
            assert batch["state"] == "succeeded", batch
    # Reopening the first consumed decision cannot approve the next item or
    # replay an already completed external action.
    drive(full_fixture, original_case)
    assert len(full_fixture.oa.writes) == len(profiles)
    with sqlite3.connect(full_fixture.service.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM business_draft_decisions").fetchone() == (len(profiles),)


def test_unknown_batch_item_never_advances_or_replays(full_fixture):
    case = full_fixture.add_pending_batch(("attendance_confirmation", "weekly_report"))
    drafts = BusinessDraftService(full_fixture.service)
    opened = drafts.open(case["input_id"])
    credentials = {"source_submission_id": case["input_id"], "presentation_id": None,
                   "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
    saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"], values={"opinion": "合成核对意见"})
    preview = drafts.preview(**credentials, expected_revision=saved["revision"])
    decision = decide(drafts, credentials, preview)
    full_fixture.oa.arm("approval_reply_lost")
    drive(full_fixture, case)
    authorization = full_fixture.service.write_authorizations.get(decision["receipt"]["authorizationId"])
    operation = full_fixture.service.operations.get(authorization["commit_operation_id"])
    assert operation["status"] == "unknown"
    full_fixture.oa.arm(None)
    drive(full_fixture, case)
    batch = full_fixture.service.tasks.get_batch_for_task(parent_task_id=case["task_id"], user_subject=OWNER)
    assert batch["succeeded_count"] == 0
    assert batch["current_ordinal"] == 1
    assert len(full_fixture.oa.writes) == 1
    assert full_fixture.oa.data["affairs"][case["affair_ids"][1]]["processed"] is False
