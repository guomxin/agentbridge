from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.task_plans import (
    TaskPlanConflict,
    TaskPlanNotFound,
    TaskPlanStore,
    task_plan_response,
)


class TaskPlanStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.store = TaskPlanStore(Path(self.temporary.name) / "agentbridge.db")

    def tearDown(self):
        self.temporary.cleanup()

    def compiled(self):
        return {
            "goal": "读取并转换",
            "planHash": "a" * 64,
            "riskSummary": {
                "systems": ["oa"],
                "requiredScopes": ["oa:read"],
                "writeSinkCount": 0,
            },
            "steps": [
                {
                    "stepKey": "read",
                    "ordinal": 1,
                    "kind": "capability",
                    "title": "读取",
                    "capabilityName": "oa.workflow.done.list",
                    "transformName": None,
                    "version": "0.2.0",
                    "dependsOn": [],
                    "arguments": {},
                    "bindings": {},
                    "effect": "read",
                    "systemId": "oa",
                }
            ],
        }

    def test_create_and_idempotent_reuse(self):
        plan, reused = self.store.create(
            user_subject="user-1",
            parent_task_id="task-1",
            compiled_plan=self.compiled(),
            proposal_source="agent_host",
            coordinator_lease_version=1,
            idempotency_key="same",
        )
        replay, replayed = self.store.create(
            user_subject="user-1",
            parent_task_id="task-1",
            compiled_plan=self.compiled(),
            proposal_source="agent_host",
            coordinator_lease_version=1,
            idempotency_key="same",
        )

        self.assertFalse(reused)
        self.assertTrue(replayed)
        self.assertEqual(replay["plan_id"], plan["plan_id"])
        self.assertEqual(plan["steps"][0]["state"], "queued")

    def test_one_task_cannot_have_two_active_plans(self):
        self.store.create(
            user_subject="user-1",
            parent_task_id="task-1",
            compiled_plan=self.compiled(),
            proposal_source="agent_host",
            coordinator_lease_version=1,
            idempotency_key="one",
        )

        with self.assertRaises(TaskPlanConflict):
            self.store.create(
                user_subject="user-1",
                parent_task_id="task-1",
                compiled_plan={**self.compiled(), "planHash": "b" * 64},
                proposal_source="agent_host",
                coordinator_lease_version=1,
                idempotency_key="two",
            )

    def test_cross_user_read_is_hidden(self):
        plan, _ = self.store.create(
            user_subject="user-1",
            parent_task_id="task-1",
            compiled_plan=self.compiled(),
            proposal_source="agent_host",
            coordinator_lease_version=1,
            idempotency_key="one",
        )

        with self.assertRaises(TaskPlanNotFound):
            self.store.get(plan["plan_id"], user_subject="user-2")

    def test_v2_context_authority_and_projection_survive_reload_without_token_exposure(self):
        compiled = {
            **self.compiled(),
            "schemaVersion": "agentbridge.task-plan.compiled.v2",
            "proposalSchemaVersion": "agentbridge.task-plan.proposal.v2",
            "temporalContext": {
                "acceptedAt": "2026-08-31T09:30:43+08:00",
                "timeZone": "Asia/Shanghai",
                "absoluteRange": {"start": "2026-08-30", "end": "2026-08-30"},
            },
            "authoritySnapshot": {
                "tokenId": "token-internal-only",
                "userSubject": "user-1",
                "requiredScopes": ["oa:read"],
            },
        }
        plan, _ = self.store.create(
            user_subject="user-1",
            parent_task_id="task-1",
            compiled_plan=compiled,
            proposal_source="agent_host",
            coordinator_lease_version=1,
            idempotency_key="v2",
        )
        projection = {
            "schemaVersion": "agentbridge.plan-result-projection.v1",
            "visibility": "user_private",
            "kind": "private_draft",
            "result": {"draft": "草稿正文"},
        }
        reloaded = self.store.set_result_projection(
            plan["plan_id"],
            user_subject="user-1",
            projection=projection,
        )

        self.assertEqual(reloaded["schema_version"], "agentbridge.task-plan.proposal.v2")
        self.assertEqual(reloaded["authority_snapshot"]["tokenId"], "token-internal-only")
        public = task_plan_response(reloaded)
        self.assertEqual(public["resultProjection"]["result"]["draft"], "草稿正文")
        self.assertNotIn("authority", str(public).lower())
        self.assertNotIn("token-internal-only", str(public))


if __name__ == "__main__":
    unittest.main()
