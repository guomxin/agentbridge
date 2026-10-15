"""Batch finalization and warning continuations bind the exact item/version."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.adapters.seeyon_submit_phases import SeeyonBusinessValidationRequired
from agentbridge.core.business_drafts import BusinessDraftService, BusinessDraftError
from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS
from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture, OWNER


class BusinessDraftBatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = FullBusinessAcceptanceFixture(Path(self.temp.name), seed=False)
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        self.case = self.fixture.add_pending_batch(("leave", "overtime"))
        self.drafts = BusinessDraftService(self.service)
        opened = self.drafts.open(self.case["input_id"])
        self.args = {"source_submission_id": self.case["input_id"], "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        saved = self.drafts.save(**self.args, expected_revision=1, values={"opinion": "核对当前合成材料"})
        self.ready = self.drafts.preview(**self.args, expected_revision=saved["revision"])

    def approve(self):
        return self.drafts.approve(**self.args, expected_revision=self.ready["revision"],
            preview_id=self.ready["preview"]["previewId"], request_key="batch-explicit-decision")["receipt"]

    def query(self, sql, parameters=()):
        with sqlite3.connect(self.service.db_path) as connection:
            return connection.execute(sql, parameters).fetchall()

    def mutate(self, sql, parameters=()):
        with sqlite3.connect(self.service.db_path) as connection:
            connection.execute(sql, parameters)

    def warning(self):
        self.receipt = self.approve()
        name = _TRUSTED_WRITE_DEFINITIONS["oa.leave.approval.prepare"]["commit_function"]
        def warn(_adapter, _worker, _plan, *, enter_commit_boundary):
            enter_commit_boundary()
            raise SeeyonBusinessValidationRequired({"code": "synthetic", "message": "合成额外确认", "force_check": False,
                "can_continue": True, "fingerprint": "sha256:batch-warning"})
        with patch("agentbridge.core.write_catalog." + name, side_effect=warn):
            result = self.fixture.resume(self.receipt["interactionId"])
        self.assertEqual(result["error"]["code"], "OA_BUSINESS_VALIDATION_CONFIRMATION_REQUIRED", result)
        child_id = result["nextAction"]["authorizationId"]
        token = self.service.write_authorizations.issue_csrf(child_id)
        self.service.write_authorizations.decide(child_id, decision="approve", csrf_token=token, csrf_cookie=token)
        return result, child_id

    def test_final_batch_activity_failure_rolls_back_every_decision_ledger(self):
        original_version = self.query("SELECT version FROM task_batches")[0][0]
        self.mutate("CREATE TRIGGER fail_draft_batch_activity BEFORE UPDATE ON task_batch_items BEGIN SELECT RAISE(ABORT, 'synthetic batch rollback'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.approve()
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_decisions"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.query("SELECT version FROM task_batches"), [(original_version,)])
        self.assertEqual(self.service.field_submissions.get(self.case["input_id"])["state"], "pending")
        self.assertEqual(self.fixture.oa.writes, [])

    def test_preview_version_cannot_absorb_an_unrelated_batch_change(self):
        self.mutate("UPDATE task_batches SET version=version+1")
        with self.assertRaises(BusinessDraftError) as caught:
            self.approve()
        self.assertEqual(caught.exception.code, "DRAFT_CONTEXT_CHANGED")
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.fixture.oa.writes, [])

    def test_proven_warning_advances_exactly_one_version_and_current_item(self):
        warning, child_id = self.warning()
        result = self.fixture.resume(warning["interaction"]["interactionId"])
        self.assertEqual(self.service.write_authorizations.get(child_id)["state"], "consumed", result)
        self.assertEqual(len(self.fixture.oa.writes), 1)
        self.assertEqual(self.query("SELECT current_ordinal,succeeded_count FROM task_batches"), [(2, 1)])
        self.assertEqual(result["interaction"]["type"], "business_input")

    def test_warning_does_not_absorb_other_mutations_with_same_current_item(self):
        warning, child_id = self.warning()
        # A pause/resume or unrelated activity increments this version even when
        # the ordinal and displayed interaction eventually look unchanged.
        self.mutate("UPDATE task_batches SET version=version+1")
        result = self.fixture.resume(warning["interaction"]["interactionId"])
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.service.write_authorizations.get(child_id)["state"], "approved")
        self.assertEqual(self.fixture.oa.writes, [])

    def test_warning_cannot_follow_the_next_item(self):
        warning, child_id = self.warning()
        self.mutate("UPDATE task_batches SET current_ordinal=2,version=version+1")
        with self.service.business_drafts._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            with self.assertRaises(BusinessDraftError):
                self.drafts.guard_consumption(connection, child_id, OWNER)
        self.assertEqual(self.fixture.oa.writes, [])
