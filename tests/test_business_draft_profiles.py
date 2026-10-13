"""Profile-specific authority, version and candidate boundary regressions."""
from copy import deepcopy
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from tests.authorization_fixtures import grant_permissions
from tests.support.business_view_acceptance import BusinessViewAcceptanceFixture, OWNER, ROOM_VALUES


class BusinessDraftProfileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = BusinessViewAcceptanceFixture(Path(self.temp.name))
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        self.drafts = BusinessDraftService(self.service)

    def open(self, profile):
        self.case = next(case for case in self.fixture.meta["view_cases"] if case["profile"] == profile)
        opened = self.drafts.open(self.case["input_id"])
        self.args = {"source_submission_id": self.case["input_id"], "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        return opened["state"]

    def save(self, state, values):
        return self.drafts.save(**self.args, expected_revision=state["revision"], values=values)

    def leave_preview(self):
        saved = self.save(self.open("leave"), {"opinion": "已核对材料，同意。"})
        return self.drafts.preview(**self.args, expected_revision=saved["revision"])

    def approve(self, state):
        return self.drafts.approve(**self.args, expected_revision=state["revision"],
            preview_id=state["preview"]["previewId"], request_key="profile-boundary-approval")

    def test_non_time_save_reuses_expiry_but_time_save_invalidates_candidates(self):
        state = self.save(self.open("meeting_room"), ROOM_VALUES)
        state = self.drafts.options(**self.args, expected_revision=state["revision"])
        option = state["options"]["items"][0]
        expiry = state["options"]["expiresAt"]
        selected = self.save(state, {**state["values"], "room": option["value"], "purpose": "核对候选有效期"})
        self.assertEqual(selected["options"]["revision"], selected["revision"])
        self.assertEqual(selected["options"]["expiresAt"], expiry)
        self.assertEqual(selected["options"]["selected"], option)
        edited = self.save(selected, {**selected["values"], "end_time": "2026-10-20 12:00"})
        self.assertIsNone(edited["options"])
        self.assertEqual(edited["values"]["room"], option["value"])
        with self.assertRaises(BusinessDraftError) as caught:
            self.drafts.preview(**self.args, expected_revision=edited["revision"])
        self.assertEqual(caught.exception.code, "DRAFT_OPTIONS_STALE")

    def test_first_room_preview_does_not_mistake_authorization_notice_for_a_change(self):
        state = self.save(self.open("meeting_room"), ROOM_VALUES)
        state = self.drafts.options(**self.args, expected_revision=state["revision"])
        state = self.save(state, {**state["values"], "room": state["options"]["items"][0]["value"]})
        ready = self.drafts.preview(**self.args, expected_revision=state["revision"])
        self.assertTrue(ready["preview"]["summary"]["authorization_notice"])
        self.assertIs(ready["preview"]["summary"]["reviewChanged"], False)
        self.assertIs(ready["preview"]["reviewChanged"], False)

    def test_first_leave_preview_does_not_mistake_authorization_notice_for_a_change(self):
        ready = self.leave_preview()
        self.assertTrue(ready["preview"]["summary"]["authorization_notice"])
        self.assertIs(ready["preview"]["reviewChanged"], False)
        self.assertIs(ready["ui"]["review"]["reviewChanged"], False)

    def test_leave_change_since_initial_source_is_explicit_on_first_preview(self):
        state = self.open("leave")
        self.fixture.oa.change_material(self.case["arguments"]["affair_id"])
        state = self.save(state, {"opinion": "按当前材料审批"})
        ready = self.drafts.preview(**self.args, expected_revision=state["revision"])
        self.assertIs(ready["preview"]["reviewChanged"], True)
        self.assertIn("材料已经变更", str(ready["ui"]["review"]))

    def test_candidate_query_rechecks_revoked_permission_before_network(self):
        state = self.save(self.open("meeting_room"), ROOM_VALUES)
        grant_permissions(self.service.user_grants, OWNER, [])
        before = len(self.fixture.oa.data["calls"])
        with self.assertRaises(BusinessDraftError):
            self.drafts.options(**self.args, expected_revision=state["revision"])
        self.assertEqual(len(self.fixture.oa.data["calls"]), before)

    def test_final_recheck_has_no_sqlite_writer_lock_and_preserves_displayed_plan(self):
        state = self.leave_preview()
        with sqlite3.connect(self.service.db_path) as connection:
            frozen = connection.execute("SELECT plan_json FROM business_draft_previews WHERE preview_id=?", (state["preview"]["previewId"],)).fetchone()[0]
        original = self.drafts._prepare
        def unlocked_read(*args):
            with sqlite3.connect(self.service.db_path, timeout=0.1) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            return original(*args)
        with patch.object(self.drafts, "_prepare", side_effect=unlocked_read):
            receipt = self.approve(state)["receipt"]
        with sqlite3.connect(self.service.db_path) as connection:
            approved = connection.execute("SELECT plan_json FROM write_authorizations WHERE authorization_id=?", (receipt["authorizationId"],)).fetchone()[0]
        self.assertEqual(approved, frozen)

    def test_target_identity_change_invalidates_preview_without_approval(self):
        state = self.leave_preview()
        affair = self.fixture.oa.data["affairs"][self.case["arguments"]["affair_id"]]
        affair["signals"]["identity"]["summary_id"] = "replacement-summary"
        with self.assertRaises(BusinessDraftError) as caught:
            self.approve(state)
        self.assertEqual(caught.exception.code, "DRAFT_TARGET_CHANGED")
        self.assertIsNone(caught.exception.current["preview"])
        self.assertEqual(self.fixture.oa.writes, [])

    def test_material_review_replaces_source_snapshot_and_omits_opinion(self):
        state = self.leave_preview()
        self.fixture.oa.change_material(self.case["arguments"]["affair_id"])
        with self.assertRaises(BusinessDraftError) as caught:
            self.approve(state)
        review = caught.exception.current["ui"]["review"]
        self.assertEqual(review["snapshot"], "preview")
        self.assertIn("材料已经变更", str(review))
        self.assertNotIn("合成请假事由", str(review))
        self.assertNotIn("处理意见", {field["label"] for field in review["fields"]})

    def test_unknown_profile_version_cannot_open_a_new_immutable_source(self):
        self.open("leave")
        source = self.service.field_submissions.get(self.case["input_id"])
        schema = deepcopy(source["form_schema"])
        schema["_agentbridge_business_draft_profile"]["version"] = "future"
        created = self.service.field_submissions.create(
            **{key: source[key] for key in ("user_subject", "system_id", "session_id", "capability_name", "capability_version", "create_operation_id")},
            supersession_key="test-unknown-profile", form_schema=schema, card_base_url=self.service.trusted_card_base_url)
        envelope = self.service._business_input_interaction(created)
        self.service.tasks.link_interaction(task_id=self.case["task_id"], user_subject=OWNER,
            interaction_record=self.service.interactions.get(envelope["interactionId"], user_subject=OWNER), interaction=envelope)
        with self.assertRaises(BusinessDraftError) as caught:
            self.drafts.open(created["submission_id"])
        self.assertEqual(caught.exception.code, "DRAFT_PROFILE_VERSION_UNSUPPORTED")

    def test_direct_prepare_receipt_cannot_transfer_to_another_affair(self):
        state = self.leave_preview()
        self.approve(state)
        with self.assertRaises(BusinessDraftError) as caught:
            self.service.invoke(user_subject=OWNER, capability_name="oa.leave.approval.prepare", task_id=self.case["task_id"],
                arguments={"input_submission_id": self.case["input_id"], "affair_id": "different-affair"})
        self.assertEqual(caught.exception.code, "DRAFT_CONTEXT_CHANGED")
        self.assertEqual(self.fixture.oa.writes, [])
