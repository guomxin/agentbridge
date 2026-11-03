"""OA opinion confirmation against real adapters and network-free downstream IO."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import patch

from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from tests.authorization_fixtures import grant_permissions
from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture, OWNER


class BusinessDraftConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = FullBusinessAcceptanceFixture(Path(self.temp.name), seed=False)
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        self.drafts = BusinessDraftService(self.service)

    def query(self, sql, parameters=()):
        with sqlite3.connect(self.service.db_path) as connection:
            return connection.execute(sql, parameters).fetchall()

    def open_case(self, case=None, *, profile="leave"):
        self.case = case or self.fixture.add_pending_case(profile)
        opened = self.drafts.open(self.case["input_id"])
        self.args = {"source_submission_id": self.case["input_id"],
                     "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        return opened["state"]

    def save(self, state, opinion="已核对原始材料，同意办理。"):
        return self.drafts.save(**self.args, expected_revision=state["revision"],
                                values={"opinion": opinion})

    def confirm(self, state, *, request_key="confirm-current-opinion-01", **overrides):
        arguments = {**self.args, "expected_revision": state["revision"],
                     "review_id": state["confirmation"]["reviewId"], "request_key": request_key}
        arguments.update(overrides)
        return self.drafts.confirm(**arguments)

    def no_authority(self):
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_decisions"), [(0,)])
        self.assertEqual(self.fixture.oa.writes, [])

    def changed(self, state):
        affair_id = self.case.get("affair_ids", [self.case["arguments"].get("affair_id")])[0]
        self.fixture.oa.change_material(affair_id)
        with self.assertRaises(BusinessDraftError) as caught:
            self.confirm(state)
        self.assertEqual(caught.exception.code, "DRAFT_REVIEW_CHANGED")
        self.no_authority()
        return caught.exception.current

    def drive(self, interaction_id=None):
        interaction_id = interaction_id or self.case["input_interaction"]
        for _ in range(4):
            result = self.fixture.resume(interaction_id)
            interaction = result.get("interaction")
            if interaction and interaction["resume"]["ready"]:
                interaction_id = interaction["interactionId"]
            else:
                return result
        self.fail("The original interaction coordinator did not settle")

    def authorization(self, receipt):
        return self.service.write_authorizations.get(receipt["authorizationId"], include_plan=True)

    def test_open_save_and_state_never_read_oa_or_create_an_authorization_plan(self):
        case = self.fixture.add_pending_case("leave")
        before = list(self.fixture.oa.data["calls"])
        workers = len(self.fixture.oa.workers)
        with patch.object(self.drafts, "_prepare", side_effect=AssertionError("editing must not prepare")):
            state = self.open_case(case)
            review_id = state["confirmation"]["reviewId"]
            self.assertEqual(state["ui"]["confirmationMode"], "on_confirm")
            self.assertIn("confirm", state["allowedActions"])
            for opinion in ("", "同意", "已核对，补充完整意见。"):
                state = self.save(state, opinion)
                self.assertEqual(state["confirmation"]["reviewId"], review_id)
                self.assertIsNone(state["preview"])
                self.assertIsNone(state["receipt"])
                self.assertEqual(self.drafts.state(**self.args)["values"]["opinion"], opinion)
            reopened = self.drafts.open(case["input_id"], csrf_cookie=self.args["csrf_cookie"])
            self.assertEqual(reopened["state"]["confirmation"]["reviewId"], review_id)
        self.assertEqual(self.fixture.oa.data["calls"], before)
        self.assertEqual(len(self.fixture.oa.workers), workers)
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_previews"), [(0,)])
        self.no_authority()

    def test_confirmation_prepares_once_and_freezes_the_saved_opinion(self):
        state = self.save(self.open_case())
        with patch.object(self.drafts, "_prepare", wraps=self.drafts._prepare) as prepare:
            result = self.confirm(state)
        self.assertEqual(prepare.call_count, 1)
        self.assertTrue(result["receipt"])
        authorization = self.authorization(result["receipt"])
        self.assertEqual(authorization["state"], "approved")
        self.assertEqual(authorization["plan"]["exact_input"]["opinion"], state["values"]["opinion"])
        self.assertEqual(authorization["plan"]["target"]["affair_id"], self.case["arguments"]["affair_id"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_decisions"), [(1,)])
        self.assertEqual(self.fixture.oa.writes, [])

    def test_missed_punch_and_acknowledgement_profiles_use_the_same_safe_confirmation(self):
        for profile in ("missed_punch", "weekly_report", "attendance_confirmation"):
            with self.subTest(profile=profile):
                state = self.save(self.open_case(profile=profile))
                self.assertEqual(state["ui"]["confirmationMode"], "on_confirm")
                with patch.object(self.drafts, "_prepare", wraps=self.drafts._prepare) as prepare:
                    result = self.confirm(state, request_key="confirm-profile-" + profile)
                self.assertEqual(prepare.call_count, 1)
                authorization = self.authorization(result["receipt"])
                self.assertEqual(authorization["plan"]["target"]["affair_id"], self.case["arguments"]["affair_id"])
                self.assertEqual(authorization["plan"]["exact_input"]["opinion"], state["values"]["opinion"])
                self.assertEqual(self.fixture.oa.writes, [])

    def test_changed_material_requires_another_explicit_confirmation(self):
        state = self.save(self.open_case())
        with patch.object(self.drafts, "_prepare", wraps=self.drafts._prepare) as prepare:
            changed = self.changed(state)
        self.assertEqual(prepare.call_count, 1)
        self.assertNotEqual(changed["confirmation"]["reviewId"], state["confirmation"]["reviewId"])
        self.assertIn("材料已经变更", str(changed["ui"]["review"]))
        self.assertNotIn("处理意见", {field["label"] for field in changed["ui"]["review"].get("fields", [])})
        self.drafts.state(**self.args)
        self.no_authority()
        with patch.object(self.drafts, "_prepare", wraps=self.drafts._prepare) as prepare:
            result = self.confirm(changed, request_key="confirm-updated-material-02")
        self.assertEqual(prepare.call_count, 1)
        self.assertTrue(result["receipt"])
        self.assertEqual(self.fixture.oa.writes, [])

    def test_updated_material_baseline_survives_edit_poll_and_reopen(self):
        changed = self.changed(self.save(self.open_case()))
        before = list(self.fixture.oa.data["calls"])
        updated = self.save(changed, "按刚刚更新的材料，同意。")
        state = self.drafts.state(**self.args)
        reopened = self.drafts.open(self.case["input_id"], csrf_cookie=self.args["csrf_cookie"])["state"]
        for current in (updated, state, reopened):
            self.assertEqual(current["confirmation"]["reviewId"], changed["confirmation"]["reviewId"])
            self.assertIn("材料已经变更", str(current["ui"]["review"]))
            self.assertEqual(current["values"]["opinion"], "按刚刚更新的材料，同意。")
        self.assertEqual(self.fixture.oa.data["calls"], before)
        self.no_authority()
        receipt = self.confirm(updated, request_key="confirm-after-text-edit-03")["receipt"]
        self.assertEqual(self.authorization(receipt)["plan"]["exact_input"]["opinion"], updated["values"]["opinion"])

    def test_old_review_cannot_authorize_newly_displayed_material(self):
        original = self.save(self.open_case())
        self.changed(original)
        with patch.object(self.drafts, "_prepare", side_effect=AssertionError("stale review must reject before reads")):
            with self.assertRaises(BusinessDraftError):
                self.confirm(original, request_key="confirm-stale-review-04")
        self.no_authority()

    def test_empty_opinion_is_rejected_without_a_downstream_read(self):
        state = self.save(self.open_case(), "  ")
        with patch.object(self.drafts, "_prepare", side_effect=AssertionError("invalid fields must reject before reads")):
            with self.assertRaises(BusinessDraftError) as caught:
                self.confirm(state)
        self.assertEqual(caught.exception.status, 422)
        self.assertIn("opinion", caught.exception.field_errors)
        self.no_authority()

    def test_changed_target_identity_is_rejected_without_authority(self):
        state = self.save(self.open_case())
        affair = self.fixture.oa.data["affairs"][self.case["arguments"]["affair_id"]]
        affair["signals"]["identity"]["summary_id"] = "a-replacement-summary"
        with self.assertRaises(BusinessDraftError) as caught:
            self.confirm(state)
        self.assertEqual(caught.exception.code, "DRAFT_TARGET_CHANGED")
        self.no_authority()

    def test_wrong_actual_oa_principal_never_reads_or_approves_the_pending_item(self):
        state = self.save(self.open_case())
        self.fixture.oa.data["principal"] = "a-different-account"
        before = sum(call["action"] == "pending" for call in self.fixture.oa.data["calls"])
        with self.assertRaises(BusinessDraftError) as caught:
            self.confirm(state)
        self.assertEqual(caught.exception.code, "DRAFT_SESSION_CHANGED")
        self.assertEqual(sum(call["action"] == "pending" for call in self.fixture.oa.data["calls"]), before)
        self.no_authority()

    def test_source_from_before_account_rebinding_is_rejected_before_prepare(self):
        state = self.save(self.open_case())
        session = self.service.sessions.find(user_subject=OWNER, system_id="oa")
        self.service.sessions.rebind_expected_principal(session["session_id"],
            expected_principal_ref="a-rebound-account", reason="Synthetic account change")
        self.service.sessions.activate(session["session_id"], observed_principal_ref="a-rebound-account")
        self.fixture.oa.data["principal"] = "a-rebound-account"
        before = list(self.fixture.oa.data["calls"])
        with patch.object(self.drafts, "_prepare", wraps=self.drafts._prepare) as prepare:
            with self.assertRaises(BusinessDraftError):
                self.confirm(state)
        self.assertEqual(prepare.call_count, 0)
        self.assertEqual(self.fixture.oa.data["calls"], before)
        self.no_authority()

    def test_revoked_permission_rejects_before_any_downstream_read(self):
        state = self.save(self.open_case())
        grant_permissions(self.service.user_grants, OWNER, [])
        before = list(self.fixture.oa.data["calls"])
        with patch.object(self.drafts, "_prepare", side_effect=AssertionError("revoked permission must reject before reads")):
            with self.assertRaises(BusinessDraftError):
                self.confirm(state)
        self.assertEqual(self.fixture.oa.data["calls"], before)
        self.no_authority()

    def test_expired_page_cannot_extend_its_material_receipt_or_confirm(self):
        state = self.save(self.open_case())
        self.service.business_drafts.clock = lambda: datetime.fromisoformat(state["expiresAt"]) + timedelta(seconds=1)
        before = list(self.fixture.oa.data["calls"])
        with patch.object(self.drafts, "_prepare", side_effect=AssertionError("expired source must reject before reads")):
            with self.assertRaises(BusinessDraftError):
                self.confirm(state)
        self.assertEqual(self.fixture.oa.data["calls"], before)
        self.no_authority()

    def test_new_material_receipts_and_edits_never_extend_the_source_expiry(self):
        state = self.save(self.open_case())
        original_expiry = state["confirmation"]["expiresAt"]
        initial_time = self.service.business_drafts.clock()
        self.service.business_drafts.clock = lambda: initial_time + timedelta(minutes=5)
        changed = self.changed(state)
        self.assertEqual(changed["confirmation"]["expiresAt"], original_expiry)
        self.service.business_drafts.clock = lambda: initial_time + timedelta(minutes=10)
        saved = self.save(changed, "继续填写不会延长办理或材料期限。")
        reopened = self.drafts.open(self.case["input_id"], csrf_cookie=self.args["csrf_cookie"])["state"]
        self.assertEqual(saved["confirmation"]["expiresAt"], original_expiry)
        self.assertEqual(reopened["confirmation"]["expiresAt"], original_expiry)
        self.assertEqual({row[0] for row in self.query("SELECT expires_at FROM business_draft_material_reviews")},
                         {original_expiry})
        self.no_authority()

    def test_parallel_save_invalidates_the_inflight_confirmation(self):
        state = self.save(self.open_case())
        entered, release = Event(), Event()
        original = self.drafts._prepare

        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            self.assertTrue(release.wait(10))
            return result

        with patch.object(self.drafts, "_prepare", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.confirm, state)
            try:
                self.assertTrue(entered.wait(10))
                changed = self.save(state, "另一页修改后的意见。")
            finally:
                release.set()
            with self.assertRaises(BusinessDraftError) as caught:
                pending.result(10)
        self.assertEqual(caught.exception.code, "DRAFT_REVISION_CONFLICT")
        self.assertEqual(self.drafts.state(**self.args)["revision"], changed["revision"])
        self.no_authority()

    def test_permission_revoked_during_prepare_rolls_back_confirmation(self):
        state = self.save(self.open_case())
        original = self.drafts._prepare

        def revoke_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            grant_permissions(self.service.user_grants, OWNER, [])
            return result

        with patch.object(self.drafts, "_prepare", side_effect=revoke_after_read):
            with self.assertRaises(BusinessDraftError):
                self.confirm(state)
        self.no_authority()

    def test_same_request_recovers_the_receipt_without_a_new_read_or_authorization(self):
        state = self.save(self.open_case())
        receipt = self.confirm(state)["receipt"]
        before = list(self.fixture.oa.data["calls"])
        with patch.object(self.drafts, "_prepare", side_effect=AssertionError("receipt replay must not prepare")):
            recovered = self.confirm(state)["receipt"]
            self.assertEqual(recovered, receipt)
            with self.assertRaises(BusinessDraftError):
                self.confirm(state, request_key="different-confirmation-key-99")
            with self.assertRaises(BusinessDraftError):
                self.confirm(state, expected_revision=state["revision"] + 1)
            with self.assertRaises(BusinessDraftError):
                self.confirm(state, review_id="different-reviewed-materials-99")
        self.assertEqual(self.fixture.oa.data["calls"], before)
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_decisions"), [(1,)])

    def test_concurrent_same_request_creates_only_one_approval_decision(self):
        state = self.save(self.open_case())
        both_prepared = Barrier(2)
        original = self.drafts._prepare

        def prepare_together(*args, **kwargs):
            prepared = original(*args, **kwargs)
            both_prepared.wait(10)
            return prepared

        with patch.object(self.drafts, "_prepare", side_effect=prepare_together), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.confirm, state)
            second = pool.submit(self.confirm, state)
            results = [first.result(15), second.result(15)]
        self.assertEqual(results[0]["receipt"], results[1]["receipt"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_decisions"), [(1,)])
        self.assertEqual(self.fixture.oa.writes, [])

    def test_concurrent_distinct_requests_cannot_create_two_approvals(self):
        state = self.save(self.open_case())
        both_prepared = Barrier(2)
        original = self.drafts._prepare

        def prepare_together(*args, **kwargs):
            prepared = original(*args, **kwargs)
            both_prepared.wait(10)
            return prepared

        with patch.object(self.drafts, "_prepare", side_effect=prepare_together), ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(self.confirm, state, request_key=key)
                       for key in ("concurrent-confirmation-01", "concurrent-confirmation-02")]
            results, errors = [], []
            for result in pending:
                try:
                    results.append(result.result(15))
                except BusinessDraftError as error:
                    errors.append(error)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "DRAFT_REVISION_CONFLICT")
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_decisions"), [(1,)])
        self.assertEqual(self.fixture.oa.writes, [])

    def test_lost_business_acknowledgement_never_replays_the_submission(self):
        state = self.save(self.open_case())
        receipt = self.confirm(state)["receipt"]
        self.fixture.oa.arm("approval_reply_lost")
        self.drive()
        authorization = self.authorization(receipt)
        operation_id = authorization["commit_operation_id"]
        self.assertEqual(self.service.operations.get(operation_id)["status"], "unknown")
        self.assertEqual(authorization["state"], "consumed")
        self.assertEqual(len(self.fixture.oa.writes), 1)
        self.fixture.oa.arm(None)
        with patch.object(self.drafts, "_prepare", side_effect=AssertionError("unknown receipt recovery must not prepare")):
            self.assertEqual(self.confirm(state)["receipt"], receipt)
        self.drive()
        self.drive(receipt["interactionId"])
        self.assertEqual(len(self.fixture.oa.writes), 1)
        self.assertEqual(self.service.operations.get(operation_id)["status"], "unknown")
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(1,)])

    def test_batch_confirmation_only_authorizes_the_current_item(self):
        case = self.fixture.add_pending_batch(("leave", "overtime"))
        before = list(self.fixture.oa.data["calls"])
        state = self.save(self.open_case(case))
        self.assertEqual(self.fixture.oa.data["calls"], before)
        self.assertEqual(state["ui"]["confirmationMode"], "on_confirm")
        with patch.object(self.drafts, "_prepare", wraps=self.drafts._prepare) as prepare:
            receipt = self.confirm(state)["receipt"]
        self.assertEqual(prepare.call_count, 1)
        authorization = self.authorization(receipt)
        self.assertEqual(authorization["plan"]["target"]["affair_id"], case["affair_ids"][0])
        self.assertEqual(self.query("SELECT current_ordinal,succeeded_count FROM task_batches"), [(1, 0)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(1,)])
        self.assertEqual(self.fixture.oa.writes, [])
        self.drive()
        self.assertEqual(len(self.fixture.oa.writes), 1)
        self.assertEqual(self.fixture.oa.writes[0]["affair_id"], case["affair_ids"][0])
        self.assertEqual(self.query("SELECT current_ordinal,succeeded_count FROM task_batches"), [(2, 1)])
        self.assertFalse(self.fixture.oa.data["affairs"][case["affair_ids"][1]]["processed"])

    def test_stale_batch_version_cannot_be_absorbed_by_confirmation(self):
        state = self.save(self.open_case(self.fixture.add_pending_batch(("leave", "overtime"))))
        original = self.drafts._prepare

        def mutate_batch_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            with sqlite3.connect(self.service.db_path) as connection:
                connection.execute("UPDATE task_batches SET version=version+1")
            return result

        with patch.object(self.drafts, "_prepare", side_effect=mutate_batch_after_read):
            with self.assertRaises(BusinessDraftError):
                self.confirm(state)
        self.no_authority()

    def test_final_batch_failure_rolls_back_the_entire_confirmation(self):
        state = self.save(self.open_case(self.fixture.add_pending_batch(("leave", "overtime"))))
        original_version = self.query("SELECT version FROM task_batches")
        with sqlite3.connect(self.service.db_path) as connection:
            connection.execute("CREATE TRIGGER fail_confirm_batch BEFORE UPDATE ON task_batch_items "
                               "BEGIN SELECT RAISE(ABORT, 'synthetic confirmation rollback'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.confirm(state)
        self.no_authority()
        self.assertEqual(self.query("SELECT version FROM task_batches"), original_version)
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_previews"), [(0,)])
        self.assertEqual(self.service.field_submissions.get(self.case["input_id"])["state"], "pending")
        current = self.drafts.state(**self.args)
        self.assertEqual(current["revision"], state["revision"])
        self.assertEqual(current["confirmation"]["reviewId"], state["confirmation"]["reviewId"])

    def test_non_opinion_room_page_keeps_its_existing_preview_contract(self):
        case = self.fixture.add_view_case("meeting_room")
        state = self.open_case(case)
        self.assertNotEqual(state["ui"].get("confirmationMode"), "on_confirm")
        self.assertNotIn("confirm", state["allowedActions"])
        self.no_authority()
