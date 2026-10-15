"""Warning continuations keep a draft's original authority and immutable intent.

Business warning and final callbacks are synthetic; central task/authorization,
source freezing, operation receipts, successor routing and consumption are real.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from agentbridge.adapters.seeyon_submit_phases import SeeyonBusinessValidationRequired
from agentbridge.core.business_drafts import BusinessDraftService, BusinessDraftError
from agentbridge.core.user_grants import PERMISSIONS
from tests.authorization_fixtures import grant_permissions
from tests.support.business_view_acceptance import BusinessViewAcceptanceFixture, OWNER


class BusinessDraftWarningChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = BusinessViewAcceptanceFixture(Path(self.temp.name), seed=False)
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        grant_permissions(self.service.user_grants, OWNER, PERMISSIONS)
        self.drafts = BusinessDraftService(self.service)
        account = self.fixture.account
        self.task_id = self.service.ensure_host_task(user_subject=OWNER, token_id="synthetic-token", agent_host="test",
            host_task_key=uuid4().hex, endpoint_key=account["endpoint_key"], client_type="web",
            external_subject=account["account_id"], conversation_ref=account["openclaw_session_key"], title="合成申请提交边界")["task"]["taskId"]
        self.started = self.service.invoke(user_subject=OWNER, capability_name="oa.leave.submit.prepare", arguments={}, task_id=self.task_id)
        self.source_id = self.started["nextAction"]["inputSubmissionId"]
        opened = self.drafts.open(self.source_id)
        self.args = {"source_submission_id": self.source_id, "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        values = {"leave_type": "事假", "start_time": "2026-10-20T09:00", "end_time": "2026-10-20T10:00", "reason": "合成边界", "has_direct_supervisor": "false"}
        saved = self.drafts.save(**self.args, expected_revision=1, values=values)
        def prepare(_adapter, _worker, values):
            return {"plan": {"business_intent": "submit_leave_request", "target": {"template_id": "fixed", "form_app_id": "fixed-form"},
                "exact_input": values, "form_contract": {"version": "synthetic"}, "expected_effect": {"submitted": True}},
                "summary": {"title": "合成请假申请", "effect": "提交合成申请", "fields": [{"label": "事由", "value": values["reason"]}]}}
        with patch("agentbridge.core.write_catalog.prepare_leave_submission", side_effect=prepare):
            ready = self.drafts.preview(**self.args, expected_revision=saved["revision"])
            self.receipt = self.drafts.approve(**self.args, expected_revision=ready["revision"],
                preview_id=ready["preview"]["previewId"], request_key="initial-explicit-decision")["receipt"]
        self.writes = []

    def resume(self, interaction_id):
        return self.service.resume_interaction(user_subject=OWNER, interaction_id=interaction_id,
                                                idempotency_key="warning-test:" + interaction_id)

    def warning(self, interaction_id=None, *, boundary=True, mutate=None, fingerprint="sha256:first"):
        def warn(_adapter, _worker, plan, *, enter_commit_boundary):
            if boundary:
                enter_commit_boundary()
            if mutate:
                mutate(plan)
            raise SeeyonBusinessValidationRequired({"code": "synthetic-warning", "message": "合成提交提示",
                "force_check": False, "can_continue": True, "fingerprint": fingerprint})
        with patch("agentbridge.core.write_catalog.submit_leave_request", side_effect=warn):
            result = self.resume(interaction_id or self.receipt["interactionId"])
        self.assertEqual(result["status"], "requires_user_action", result)
        self.assertEqual(result["error"]["code"], "OA_BUSINESS_VALIDATION_CONFIRMATION_REQUIRED", result)
        return result

    def approve_warning(self, result):
        child_id = result["nextAction"]["authorizationId"]
        token = self.service.write_authorizations.issue_csrf(child_id)
        self.service.write_authorizations.decide(child_id, decision="approve", csrf_token=token, csrf_cookie=token)
        return child_id

    def finish(self, result):
        def commit(_adapter, _worker, plan, *, enter_commit_boundary):
            enter_commit_boundary()
            self.writes.append(deepcopy(plan))
            return {"workflow_submitted": True}
        with patch("agentbridge.core.write_catalog.submit_leave_request", side_effect=commit):
            return self.resume(result["interaction"]["interactionId"])

    def test_new_warning_requires_its_own_decision_then_consumes_once(self):
        warning = self.warning()
        child = warning["nextAction"]["authorizationId"]
        self.assertEqual(self.service.write_authorizations.get(child)["state"], "pending")
        self.assertEqual(self.service.field_submissions.get(self.source_id)["state"], "consumed")
        self.assertEqual(self.writes, [])
        waiting = self.finish(warning)
        self.assertEqual(waiting["status"], "requires_user_action")
        self.assertEqual(self.writes, [])
        self.approve_warning(warning)
        result = self.finish(warning)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0]["_business_draft_parent_authorization"], self.receipt["authorizationId"])
        self.assertIn(self.finish(warning)["status"], {"succeeded", "already_resumed"})
        self.assertEqual(len(self.writes), 1)
        with sqlite3.connect(self.service.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM business_draft_decisions").fetchone()[0], 1)

    def test_preboundary_warning_keeps_source_submitted_until_real_boundary(self):
        warning = self.warning(boundary=False)
        self.assertEqual(self.service.field_submissions.get(self.source_id)["state"], "submitted")
        self.approve_warning(warning)
        self.assertEqual(self.finish(warning)["status"], "succeeded")
        self.assertEqual(self.service.field_submissions.get(self.source_id)["state"], "consumed")

    def test_original_source_recovers_exact_preboundary_warning_after_restart(self):
        warning = self.warning(boundary=False)
        restored = BusinessDraftService(self.service)
        recovered = restored.resume_successor(self.source_id, user_subject=OWNER)
        self.assertEqual(recovered["interaction"]["interactionId"], warning["interaction"]["interactionId"])
        self.assertEqual(recovered["operationId"], warning["operationId"])
        self.assertEqual(self.writes, [])
        self.approve_warning(warning)
        recovered = restored.prepare_successor(self.source_id, OWNER, self.task_id,
            capability_name="oa.leave.submit.prepare", arguments={"input_submission_id": self.source_id})
        self.assertEqual(recovered["interaction"]["interactionId"], warning["interaction"]["interactionId"])
        self.assertEqual(self.finish(warning)["status"], "succeeded")

    def test_plan_target_and_input_mutations_do_not_inherit_the_original_decision(self):
        warning = self.warning(mutate=lambda plan: plan["target"].update(template_id="replaced"))
        child = self.approve_warning(warning)
        self.assertEqual(self.finish(warning)["status"], "failed")
        self.assertEqual(self.service.write_authorizations.get(child)["state"], "approved")
        self.assertEqual(self.writes, [])

    def test_original_operation_becoming_unknown_blocks_warning_chain(self):
        warning = self.warning()
        self.approve_warning(warning)
        self.service.operations.mark_unknown(warning["operationId"], code="RESULT_UNKNOWN", message="Synthetic uncertainty")
        self.assertEqual(self.finish(warning)["status"], "failed")
        self.assertEqual(self.writes, [])

    def test_second_new_warning_preserves_chain_and_never_silently_approves(self):
        first = self.warning()
        self.approve_warning(first)
        second = self.warning(first["interaction"]["interactionId"], fingerprint="sha256:second")
        self.assertEqual(self.service.write_authorizations.get(second["nextAction"]["authorizationId"])["state"], "pending")
        self.approve_warning(second)
        result = self.finish(second)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(self.writes[0]["business_validation_overrides"]), 2)

    def test_same_warning_cannot_mint_another_continuation(self):
        first = self.warning()
        self.approve_warning(first)
        with patch("agentbridge.core.write_catalog.submit_leave_request", side_effect=lambda *args, **kwargs:
            (_ for _ in ()).throw(SeeyonBusinessValidationRequired({"code": "same", "message": "same", "force_check": False,
                "can_continue": True, "fingerprint": "sha256:first"}))):
            result = self.resume(first["interaction"]["interactionId"])
        self.assertEqual(result["status"], "failed")
        with sqlite3.connect(self.service.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM write_authorizations").fetchone()[0], 2)

    def test_forged_parent_pointer_cannot_inherit_a_draft_decision(self):
        original = self.service.write_authorizations.create
        def forged(**kwargs):
            if kwargs["plan"].get("_business_draft_parent_authorization"):
                kwargs["plan"] = {**kwargs["plan"], "_business_draft_parent_authorization": "invented-parent"}
            return original(**kwargs)
        with patch.object(self.service.write_authorizations, "create", side_effect=forged):
            warning = self.warning()
        child = self.approve_warning(warning)
        self.assertEqual(self.finish(warning)["status"], "failed")
        self.assertEqual(self.service.write_authorizations.get(child)["state"], "approved")
        self.assertEqual(self.writes, [])

    def test_exact_input_mutation_in_warning_does_not_gain_consent(self):
        warning = self.warning(mutate=lambda plan: plan["exact_input"].update(reason="different input"))
        self.approve_warning(warning)
        self.assertEqual(self.finish(warning)["status"], "failed")
        self.assertEqual(self.writes, [])

    def test_warning_task_link_cannot_move_to_another_owned_task(self):
        warning = self.warning()
        child = self.approve_warning(warning)
        account = self.fixture.account
        other = self.service.ensure_host_task(user_subject=OWNER, token_id="synthetic-token", agent_host="test",
            host_task_key=uuid4().hex, endpoint_key=account["endpoint_key"], client_type="web",
            external_subject=account["account_id"], conversation_ref=account["openclaw_session_key"], title="other task")["task"]["taskId"]
        with sqlite3.connect(self.service.db_path) as connection:
            connection.execute("UPDATE task_interactions SET task_id=? WHERE interaction_id=?", (other, warning["interaction"]["interactionId"]))
        with self.service.business_drafts._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            with self.assertRaises(BusinessDraftError):
                self.drafts.guard_consumption(connection, child, OWNER)
        self.assertEqual(self.writes, [])

    def test_revoked_permission_blocks_warning_without_recovering_execution_rights(self):
        warning = self.warning()
        child = self.approve_warning(warning)
        grant_permissions(self.service.user_grants, OWNER, [])
        with self.assertRaises(PermissionError):
            self.finish(warning)
        self.assertEqual(self.service.write_authorizations.get(child)["state"], "approved")
        self.assertEqual(self.writes, [])

    def test_concurrent_resumes_of_approved_warning_write_once(self):
        warning = self.warning()
        self.approve_warning(warning)
        def commit(_adapter, _worker, plan, *, enter_commit_boundary):
            enter_commit_boundary()
            self.writes.append(plan)
            return {"workflow_submitted": True}
        with patch("agentbridge.core.write_catalog.submit_leave_request", side_effect=commit), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.resume(warning["interaction"]["interactionId"]), range(2)))
        self.assertTrue(all(result["status"] == "succeeded" for result in results), results)
        self.assertEqual(len(self.writes), 1)
