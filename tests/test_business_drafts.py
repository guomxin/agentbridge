from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from tests.authorization_fixtures import grant_permissions
from tests.support.business_draft_acceptance import BusinessDraftAcceptanceFixture, OWNER, PREPARE


class BusinessDraftBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = BusinessDraftAcceptanceFixture(Path(self.temp.name))
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        self.app = BusinessDraftService(self.service)
        self.source = self.fixture.meta["draft_cases"][-1]
        self.opened = self.app.open(self.source["input_id"])
        self.args = {"source_submission_id": self.source["input_id"], "csrf_token": self.opened["csrfToken"], "csrf_cookie": self.opened["csrfToken"]}
        self.values = {"log_date": "2026-10-13", "hours": "2", "project": "SYNTHETIC", "content": "合成边界验收"}

    def query(self, sql, values=()):
        with sqlite3.connect(self.service.db_path) as connection:
            return connection.execute(sql, values).fetchall()

    def ready(self):
        saved = self.app.save(**self.args, expected_revision=1, values=self.values)
        return self.app.preview(**self.args, expected_revision=saved["revision"])

    def approve(self, ready):
        return self.app.approve(**self.args, expected_revision=ready["revision"],
                                preview_id=ready["preview"]["previewId"], request_key="boundary-approve-01")

    def test_reopening_same_presentation_preserves_token_and_cross_tab_conflicts(self):
        second = self.app.open(self.source["input_id"], csrf_cookie=self.opened["csrfToken"])
        self.assertEqual(second["csrfToken"], self.opened["csrfToken"])
        saved = self.app.save(**self.args, expected_revision=1, values=self.values)
        with self.assertRaises(BusinessDraftError) as caught:
            self.app.save(**self.args, expected_revision=1, values={**self.values, "content": "迟到页面"})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.current["revision"], saved["revision"])
        self.assertEqual(caught.exception.current["values"]["content"], self.values["content"])

    def test_credentials_cannot_cross_presentation_and_state_obeys_revocation(self):
        presentation = self.service.field_submissions.create_presentation(self.source["input_id"], user_subject=OWNER, endpoint_id="other-endpoint")
        with self.assertRaises(BusinessDraftError) as caught:
            self.app.state(**self.args, presentation_id=presentation["presentation_id"])
        self.assertEqual(caught.exception.status, 403)
        grant_permissions(self.service.user_grants, OWNER, [])
        with self.assertRaises(BusinessDraftError):
            self.app.state(**self.args)

    def test_incomplete_values_save_and_preview_errors_are_field_scoped(self):
        saved = self.app.save(**self.args, expected_revision=1, values={name: "" for name in self.values})
        with self.assertRaises(BusinessDraftError) as caught:
            self.app.preview(**self.args, expected_revision=saved["revision"])
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(set(caught.exception.field_errors), {"log_date", "hours", "content"})
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.fixture.transport.write_calls, 0)

    def test_late_preview_cannot_enable_an_edited_revision(self):
        saved = self.app.save(**self.args, expected_revision=1, values=self.values)
        entered, release = Event(), Event()
        original = self.app._prepare
        def blocked(*args):
            prepared = original(*args)
            entered.set()
            self.assertTrue(release.wait(10))
            return prepared
        with patch.object(self.app, "_prepare", side_effect=blocked), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.app.preview, **self.args, expected_revision=saved["revision"])
            self.assertTrue(entered.wait(10))
            changed = self.app.save(**self.args, expected_revision=saved["revision"], values={**self.values, "content": "新修订"})
            release.set()
            with self.assertRaises(BusinessDraftError) as caught:
                pending.result(10)
        self.assertEqual(caught.exception.code, "DRAFT_REVISION_CONFLICT")
        state = self.app.state(**self.args)
        self.assertEqual(state["revision"], changed["revision"])
        self.assertIsNone(state["preview"])
        self.assertNotIn("approve", state["allowedActions"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_previews"), [(0,)])

    def test_preview_full_hash_changes_but_semantic_hash_ignores_check_time(self):
        first = self.ready()
        second = self.app.preview(**self.args, expected_revision=first["revision"])
        self.assertNotEqual(first["preview"]["planHash"], second["preview"]["planHash"])
        self.assertEqual(first["preview"]["semanticReviewHash"], second["preview"]["semanticReviewHash"])
        self.assertGreater(second["version"], first["version"])
        with self.assertRaises(BusinessDraftError):
            self.approve(first)
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(0,)])

    def test_live_principal_mismatch_stops_before_project_or_log_reads(self):
        adapter = self.service.adapter_for_system("taihua")
        saved = self.app.save(**self.args, expected_revision=1, values=self.values)
        before = len(self.fixture.transport.data["calls"])
        with patch.object(adapter, "probe_session", return_value={"observed_principal_ref": "wrong-person"}):
            with self.assertRaises(BusinessDraftError) as caught:
                self.app.preview(**self.args, expected_revision=saved["revision"])
        self.assertEqual(caught.exception.code, "DRAFT_SESSION_CHANGED")
        self.assertEqual(len(self.fixture.transport.data["calls"]), before)
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(0,)])

    def test_duplicate_preview_does_not_create_an_authorization(self):
        self.fixture.transport.data["records"].append({"id": "9001", "logDate": self.values["log_date"],
            "hours": 2, "projectId": 901, "content": self.values["content"]})
        with self.assertRaises(BusinessDraftError) as caught:
            self.ready()
        self.assertEqual(caught.exception.code, "DUPLICATE_WORK_LOG")
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(0,)])

    def test_session_change_after_preview_blocks_final_and_preserves_pending_source(self):
        ready = self.ready()
        self.service.sessions.activate(self.fixture.session_id, observed_principal_ref=OWNER)
        with self.assertRaises(BusinessDraftError) as caught:
            self.approve(ready)
        self.assertEqual(caught.exception.code, "DRAFT_CONTEXT_CHANGED")
        self.assertEqual(self.service.field_submissions.get(self.source["input_id"])["state"], "pending")
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(0,)])

    def test_new_or_removed_skill_binding_cannot_downgrade_source_constraints(self):
        self.service.skills.save("user:" + OWNER, {"oa-work-log": {"profiles": ["fill"]}}, expected_revision=0, actor="test", reason="fixture")
        binding = self.service.skills.bind(OWNER, "oa-work-log", "fill")
        self.service.skills.attach(OWNER, self.source["task_id"], binding)
        with self.assertRaises(BusinessDraftError):
            self.ready()  # It was born without a skill binding; attaching one changes its contract.
        result = self.service.invoke(user_subject=OWNER, capability_name=PREPARE, arguments={}, task_id=self.source["task_id"], idempotency_key="skill-bound-source")
        source_id = result["nextAction"]["inputSubmissionId"]
        self.query("DELETE FROM skill_task_bindings WHERE task_id=?", (self.source["task_id"],))
        with self.assertRaises(BusinessDraftError) as caught:
            self.app.open(source_id)
        self.assertEqual(caught.exception.code, "DRAFT_CONTEXT_CHANGED")

    def test_post_preview_batch_insertion_is_not_treated_as_an_atomic_write(self):
        ready = self.ready()
        self.service.tasks.create_batch(parent_task_id=self.source["task_id"], user_subject=OWNER, system_id="taihua",
            capability_name=PREPARE, selection_summary={}, failure_policy="stop_on_failure", items=[{"resource_ref": "one"}])
        with self.assertRaises(BusinessDraftError) as caught:
            self.approve(ready)
        self.assertEqual(caught.exception.code, "DRAFT_BATCH_UNSUPPORTED")
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(0,)])

    def test_approved_source_and_authorization_consumption_roll_back_together(self):
        final = self.approve(self.ready())
        receipt = final["receipt"]
        self.query("CREATE TRIGGER fail_draft_consume BEFORE UPDATE OF state ON write_authorizations "
                   "WHEN NEW.state='consumed' BEGIN SELECT RAISE(ABORT,'atomic boundary failure'); END")
        self.fixture.resume(receipt["interactionId"])
        self.assertEqual(self.service.field_submissions.get(self.source["input_id"])["state"], "submitted")
        self.assertEqual(self.service.write_authorizations.get(receipt["authorizationId"])["state"], "approved")
        self.assertEqual(self.fixture.transport.write_calls, 0)

    def test_missing_receipt_or_authorization_task_link_cannot_bypass_consumption_guard(self):
        receipt = self.approve(self.ready())["receipt"]
        self.query("DELETE FROM business_draft_decisions WHERE authorization_id=?", (receipt["authorizationId"],))
        self.fixture.resume(receipt["interactionId"])
        self.assertEqual(self.fixture.transport.write_calls, 0)
        self.assertEqual(self.service.field_submissions.get(self.source["input_id"])["state"], "submitted")

    def test_finalization_erases_temporary_payloads_and_cleanup_preserves_receipt(self):
        receipt = self.approve(self.ready())["receipt"]
        self.assertEqual(self.query("SELECT values_json FROM business_drafts"), [("{}",)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_previews"), [(0,)])
        expires = self.service.field_submissions.get(self.source["input_id"])["expires_at"]
        self.service.business_drafts.clock = lambda: datetime.fromisoformat(expires) + timedelta(seconds=1)
        self.service.business_drafts.cleanup()
        self.assertEqual(self.query("SELECT authorization_id FROM business_draft_decisions"), [(receipt["authorizationId"],)])

    def test_expired_unapproved_draft_removes_temporary_values_and_plan(self):
        ready = self.ready()
        self.service.business_drafts.clock = lambda: datetime.fromisoformat(ready["expiresAt"]) + timedelta(seconds=1)
        self.service.business_drafts.cleanup()
        self.assertEqual(self.query("SELECT state,values_json FROM business_drafts"), [("expired", "{}")])
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_previews"), [(0,)])

    def test_legacy_field_submission_without_draft_decision_keeps_prepare_fallback(self):
        source_id = self.source["input_id"]
        self.service.field_submissions.submit(source_id, csrf_token=self.opened["csrfToken"], csrf_cookie=self.opened["csrfToken"],
            values={**self.values, "hours": 2})
        result = self.fixture.resume(self.source["input_interaction"])
        self.assertEqual(result["interaction"]["type"], "execution_authorization")
        self.assertEqual(result["interaction"]["state"], "pending")
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_decisions"), [(0,)])
        self.assertEqual(self.fixture.transport.write_calls, 0)

    def test_old_host_direct_prepare_returns_existing_decision_without_repreparing(self):
        receipt = self.approve(self.ready())["receipt"]
        before = len(self.fixture.transport.data["calls"])
        result = self.service.invoke(user_subject=OWNER, capability_name=PREPARE,
            arguments={"input_submission_id": self.source["input_id"]}, task_id=self.source["task_id"],
            idempotency_key="old-host-direct-prepare")
        self.assertEqual(result["interaction"]["interactionId"], receipt["interactionId"])
        self.assertTrue(result["interaction"]["resume"]["ready"])
        self.assertEqual(len(self.fixture.transport.data["calls"]), before)
        self.assertEqual(self.query("SELECT state FROM write_authorizations"), [("approved",)])
        self.fixture.resume(receipt["interactionId"])
        self.assertEqual(self.fixture.transport.write_calls, 1)
        self.service.invoke(user_subject=OWNER, capability_name=PREPARE,
            arguments={"input_submission_id": self.source["input_id"]}, task_id=self.source["task_id"],
            idempotency_key="old-host-after-completion")
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(1,)])
        self.assertEqual(self.fixture.transport.write_calls, 1)
        self.assertEqual(self.service.tasks.get_task(self.source["task_id"], user_subject=OWNER)["status"], "succeeded")

    def test_direct_prepare_cannot_move_a_final_decision_to_another_task(self):
        receipt = self.approve(self.ready())["receipt"]
        other = self.fixture.add_case("different-task")
        with self.assertRaises(BusinessDraftError) as caught:
            self.service.invoke(user_subject=OWNER, capability_name=PREPARE,
                arguments={"input_submission_id": self.source["input_id"]}, task_id=other["task_id"],
                idempotency_key="attempt-task-move")
        self.assertEqual(caught.exception.code, "DRAFT_CONTEXT_CHANGED")
        self.assertEqual(self.service.tasks.task_id_for_interaction(receipt["interactionId"], user_subject=OWNER), self.source["task_id"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(1,)])
        self.assertEqual(self.fixture.transport.write_calls, 0)
