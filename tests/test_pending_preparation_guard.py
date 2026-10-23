"""Direct capability calls share the query-card OA duplicate boundary."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from agentbridge.core.business_drafts import BusinessDraftService, BusinessDraftError
from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture, OWNER
from tests.test_seeyon_pending_actions import ALL_PENDING_PROFILES


class PendingPreparationGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = FullBusinessAcceptanceFixture(Path(temporary.name), seed=False)
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service

    def new_task(self):
        account = self.fixture.account
        return self.service.ensure_host_task(user_subject=OWNER, token_id="fixture-token", agent_host="openclaw",
            host_task_key="direct-guard|" + uuid4().hex, endpoint_key=account["endpoint_key"], client_type="web",
            external_subject=account["account_id"], conversation_ref=account["openclaw_session_key"],
            title="隔离直接办理入口")["task"]["taskId"]

    def count(self, table):
        with self.service.tasks._connect() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def invoke_again(self, case, *, task_id=None):
        source = self.service.field_submissions.get(case["input_id"])
        return self.service.invoke(user_subject=OWNER, capability_name=source["capability_name"],
            arguments=case["arguments"], task_id=task_id or self.new_task(), idempotency_key=uuid4().hex)

    def test_every_pending_profile_blocks_second_direct_field_card(self):
        for profile in ALL_PENDING_PROFILES:
            with self.subTest(profile=profile):
                case = self.fixture.add_pending_case(profile)
                before = {table: self.count(table) for table in (
                    "field_submissions", "write_authorizations", "business_draft_decisions")}
                response = self.invoke_again(case)
                self.assertEqual(response["status"], "failed", response)
                self.assertEqual(response["error"]["code"], "OA_PENDING_ALREADY_PREPARED")
                self.assertEqual(before, {table: self.count(table) for table in before})
        self.assertEqual(self.fixture.oa.writes, [])

    def test_original_unconfirmed_task_can_revalidate_without_duplicate_authority(self):
        case = self.fixture.add_pending_case("efficiency_data")
        before = self.count("write_authorizations")
        response = self.invoke_again(case, task_id=case["task_id"])
        self.assertEqual(response["status"], "requires_user_action", response)
        self.assertEqual(response["interaction"]["type"], "business_input")
        self.assertEqual(self.count("write_authorizations"), before)
        self.assertEqual(self.fixture.oa.writes, [])

    def test_unknown_cannot_reprepare_even_inside_original_task(self):
        case = self.fixture.add_pending_case("efficiency_data")
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE agent_tasks SET status='outcome_unknown' WHERE task_id=?", (case["task_id"],))
        before = self.count("field_submissions")
        for _ in range(3):
            response = self.invoke_again(case, task_id=case["task_id"])
            self.assertEqual(response["status"], "failed", response)
            self.assertEqual(response["error"]["code"], "OA_PENDING_RESULT_UNRESOLVED")
            self.assertEqual(self.service.tasks.get_task(case["task_id"], user_subject=OWNER)["status"], "outcome_unknown")
        self.assertEqual(self.count("field_submissions"), before)
        self.assertEqual(self.count("write_authorizations"), 0)
        self.assertEqual(self.fixture.oa.writes, [])

    def test_other_affair_with_same_profile_still_opens_normally(self):
        first = self.fixture.add_pending_case("efficiency_data")
        second = self.fixture.add_pending_case("efficiency_data")
        self.assertNotEqual(first["arguments"], second["arguments"])
        self.assertNotEqual(first["input_id"], second["input_id"])
        self.assertEqual(self.count("write_authorizations"), 0)
        self.assertEqual(self.fixture.oa.writes, [])

    def test_legacy_duplicate_field_cannot_create_authority_through_draft_preview(self):
        case = self.fixture.add_pending_case("efficiency_data")
        # Simulate a second field card left by the pre-fix direct entry path.
        with patch.object(self.service, "_guard_pending_preparation"):
            second = self.invoke_again(case)
        self.assertEqual(second["status"], "requires_user_action")
        source_id = second["nextAction"]["inputSubmissionId"]
        drafts = BusinessDraftService(self.service)
        opened = drafts.open(source_id)
        credentials = {"source_submission_id": source_id, "csrf_token": opened["csrfToken"],
                       "csrf_cookie": opened["csrfToken"]}
        saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"], values={"opinion": "合成同意"})
        with self.assertRaises(BusinessDraftError) as caught:
            drafts.preview(**credentials, expected_revision=saved["revision"])
        self.assertIn(caught.exception.code, {"OA_PENDING_ALREADY_PREPARED", "DRAFT_VALIDATION_FAILED"})
        self.assertEqual(self.count("business_draft_decisions"), 0)
        self.assertEqual(self.count("write_authorizations"), 0)
        self.assertEqual(self.fixture.oa.writes, [])
