"""Validate the browser fixture's evidence boundary and repeatable state, not the UI."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import urlencode

from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.core.trusted_flows import TrustedFlowReader
from tests.support.interaction_acceptance import AcceptanceFixture, OWNER
from tests.test_field_card import _csrf


class InteractionAcceptanceFixtureTests(unittest.TestCase):
    def test_seed_has_mixed_batch_and_cannot_execute_business_capabilities(self):
        with TemporaryDirectory() as tmp:
            fixture = AcceptanceFixture(Path(tmp))
            self.addCleanup(fixture.close)
            batch = fixture.service.tasks.get_batch_for_task(parent_task_id=fixture.meta["batch_task_id"], user_subject=OWNER)
            self.assertEqual([item["state"] for item in batch["items"]],
                ["succeeded", "succeeded", "outcome_unknown", "queued", "queued"])
            detail = fixture.app.task_detail(fixture.account, fixture.meta["batch_task_id"])
            self.assertEqual(detail["task"]["status"], "outcome_unknown")
            reader = TrustedFlowReader(fixture.service)
            record = fixture.service.interactions.get(batch["items"][2]["interaction_id"], user_subject=OWNER)
            context = reader.context("authorize", record["resource_id"], None)
            self.assertEqual(reader.status(context)["state"], "unknown")
            with self.assertRaisesRegex(RuntimeError, "cannot invoke"):
                fixture.service.invoke("oa.leave.save_draft")
            other = fixture.service.workspace.authenticate(username="cardother", password="AgentBridge!Workspace9")
            with self.assertRaises(KeyError):
                fixture.app.task_detail(other, fixture.meta["batch_task_id"])
            self.assertEqual((fixture.home / "session.key").stat().st_mode & 0o777, 0o600)
            self.assertEqual(len((fixture.home / "session.key").read_bytes()), 32)
            self.assertEqual(fixture.app.chat_history(fixture.account)["messages"], [])

    def test_new_case_retains_old_batch_and_creates_fresh_independent_forms(self):
        with TemporaryDirectory() as tmp:
            fixture = AcceptanceFixture(Path(tmp))
            self.addCleanup(fixture.close)
            old_batch = fixture.meta["batch_task_id"]
            old_input = fixture.meta["flows"][0]["input_id"]
            fixture.add_case("second")
            self.assertNotEqual(fixture.meta["batch_task_id"], old_batch)
            self.assertNotEqual(fixture.meta["flows"][-1]["input_id"], old_input)
            old = fixture.service.tasks.get_batch_for_task(parent_task_id=old_batch, user_subject=OWNER)
            self.assertEqual(old["items"][2]["state"], "outcome_unknown")
            self.assertEqual(fixture.service.field_submissions.get(old_input)["state"], "pending")

    def test_field_authorization_and_synthetic_result_survive_restart_without_replay(self):
        with TemporaryDirectory() as tmp:
            fixture = AcceptanceFixture(Path(tmp))
            flow = fixture.meta["flows"][0]
            fields = TrustedFieldApplication(submission_store=fixture.service.field_submissions)
            csrf, cookie = _csrf(fields, flow["input_id"])
            response = fields.submit_card(flow["input_id"], body=urlencode({"csrf_token": csrf, "reason": "合成材料", "category": "review"}).encode(),
                content_type="application/x-www-form-urlencoded", csrf_cookie=cookie)
            self.assertEqual(response.status, 200)
            result = fixture.resume(OWNER, flow["input_interaction"])
            self.assertEqual(result["interaction"]["type"], "execution_authorization")
            reader = TrustedFlowReader(fixture.service)
            status = reader.status(reader.context("input", flow["input_id"], None))
            self.assertEqual(status["state"], "review_ready")
            self.assertIn(flow["authorization_id"], status["nextUrl"])
            authorization = flow["authorization_id"]
            self.assertEqual(fixture.service.write_authorizations.get(authorization)["state"], "pending")
            token = fixture.service.write_authorizations.issue_csrf(authorization)
            fixture.service.write_authorizations.decide(authorization, decision="approve", csrf_token=token, csrf_cookie=token)
            result = fixture.resume(OWNER, flow["authorization_interaction"])
            self.assertEqual(result["status"], "succeeded")
            operation = fixture.service.write_authorizations.get(authorization)["commit_operation_id"]
            fixture.close()
            recovered = AcceptanceFixture(Path(tmp))
            self.addCleanup(recovered.close)
            self.assertEqual(recovered.resume(OWNER, flow["authorization_interaction"]), result)
            self.assertEqual(recovered.service.write_authorizations.get(authorization)["commit_operation_id"], operation)

    def test_refuses_unmarked_existing_runtime_directory(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "agentbridge.db").touch()
            with self.assertRaisesRegex(ValueError, "empty isolated"):
                AcceptanceFixture(Path(tmp))
