from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.capability import CapabilityRegistry, CapabilitySpec
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.operations import OperationStore
from agentbridge.core.task_plan_runtime import TaskPlanRuntime
from agentbridge.core.task_plan_validation import PlanValidationError, validate_and_compile_task_plan
from agentbridge.core.task_plans import TaskPlanStore
from agentbridge.core.transforms import TransformSpec, build_transform_registry


class FakeTaskHub:
    def __init__(self):
        self.events = []
        self.operations = []
        self.interactions = []
        self.completed = []
        self.failed = []

    def record_plan_event(self, **values):
        self.events.append(values)
        return {}

    def link_plan_operation(self, **values):
        self.operations.append(values)
        return {}

    def link_interaction(self, **values):
        self.interactions.append(values)
        return {}

    def complete_task(self, **values):
        self.completed.append(values)
        return {}

    def fail_task(self, **values):
        self.failed.append(values)
        return {}

    def mark_task_outcome_unknown(self, **values):
        self.failed.append(values)
        return {}

    def cancel_task(self, **values):
        return {}

    def task_id_for_interaction(self, *_args, **_kwargs):
        return "task-1"

    def get_batch_for_task(self, **_kwargs):
        return None


class FakePlanService:
    def __init__(self, db_path):
        from agentbridge.core.user_grants import UserGrants
        self.user_grants = UserGrants(db_path)
        self.registry = CapabilityRegistry()
        self.registry.register(
            CapabilitySpec(
                name="source.items",
                version="1.0.0",
                description="Read source items",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"type": "object"},
                        }
                    },
                    "required": ["items"],
                    "additionalProperties": False,
                },
                effect="read",
                adapter="fake",
                workflow="source-v1",
            )
        )
        source_output_schema = {
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object"}},
                "coverage": {"type": "object"},
            },
            "required": ["collection", "items", "coverage"],
            "additionalProperties": False,
        }
        for name in ("oa.workflow.done.list", "oa.workflow.sent.list"):
            self.registry.register(
                CapabilitySpec(
                    name=name,
                    version="1.0.0",
                    description=f"Read {name}",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "start_date": {"type": "string"},
                            "end_date": {"type": "string"},
                            "limit": {"type": "integer"},
                        },
                        "additionalProperties": False,
                    },
                    output_schema=source_output_schema,
                    effect="read",
                    adapter="fake",
                    workflow=name,
                )
            )
        self.registry.register(
            CapabilitySpec(
                name="sink.prepare",
                version="1.0.0",
                description="Prepare sink write",
                input_schema={
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                effect="controlled_write",
                adapter="fake",
                workflow="sink-v1",
            )
        )
        self.registry.register(
            CapabilitySpec(
                name="taihua.work_log.create.prepare",
                version="1.0.0",
                description="Prepare Taihua work log",
                input_schema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "log_date": {"type": "string"},
                        "hours": {"type": "number"},
                    },
                    "required": ["content", "log_date", "hours"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                effect="controlled_write",
                adapter="fake",
                workflow="taihua-write-v1",
            )
        )
        self.operations = OperationStore(db_path)
        self.tasks = FakeTaskHub()
        self.interaction = None
        self.source_requires_login = False
        self.source_calls = 0
        self.source_coverage_status = "complete"
        self.authority_valid = True

    def validate_task_plan_authority(self, _plan):
        if not self.authority_valid:
            raise PlanValidationError(
                "PLAN_AUTHORITY_INVALID", "plan authority was revoked"
            )

    def invoke(self, *, user_subject, capability_name, arguments, idempotency_key, **_):
        spec = self.registry.get(capability_name)
        operation, reused = self.operations.create(
            user_subject=user_subject,
            capability_name=capability_name,
            capability_version=spec.version,
            input_summary=arguments,
            input_identity=arguments,
            idempotency_key=idempotency_key,
        )
        if reused:
            stored = self.operations.get(operation["operation_id"])
            return self._response(stored, reused=True)
        self.operations.mark_running(operation["operation_id"])
        if capability_name == "source.items" or capability_name.startswith(
            "oa.workflow."
        ):
            self.source_calls += 1
            if self.source_requires_login and self.source_calls == 1:
                interaction_id = "login-interaction-0000000000001"
                self.interaction = self._interaction(
                    interaction_id, interaction_type="credential"
                )
                operation = self.operations.mark_requires_user_action(
                    operation["operation_id"],
                    code="LOGIN_REQUIRED",
                    message="login required",
                    next_action={"interaction": self.interaction},
                )
            else:
                result = {
                    "items": [
                        {
                            "affair_id": "1",
                            "title": "流程一",
                            "date": "2026-08-30",
                            "category": "审批",
                        }
                    ]
                }
                if capability_name.startswith("oa.workflow."):
                    collection = "done" if ".done." in capability_name else "sent"
                    result = {
                        "collection": collection,
                        "items": result["items"],
                        "coverage": {
                            "status": self.source_coverage_status,
                            "queryApplied": True,
                            "dateBasis": (
                                "processed_at" if collection == "done" else "initiated_at"
                            ),
                            "requestedRange": {
                                "start": "2026-08-30",
                                "end": "2026-08-30",
                            },
                            "scannedCount": 1,
                            "matchedCount": 1,
                            "hasMore": self.source_coverage_status != "complete",
                            "completionReason": "test",
                            "observedAt": "2026-08-31T09:00:00+08:00",
                            "queryHash": f"sha256:{collection}",
                        },
                    }
                operation = self.operations.mark_succeeded(
                    operation["operation_id"], result
                )
        else:
            interaction_id = "field-interaction-0000000000001"
            self.interaction = self._interaction(
                interaction_id, interaction_type="business_input"
            )
            operation = self.operations.mark_requires_user_action(
                operation["operation_id"],
                code="BUSINESS_INPUT_REQUIRED",
                message="field input required",
                next_action={"interaction": self.interaction},
            )
        return self._response(operation, reused=False)

    def _load_interaction(self, *, interaction_id, **_):
        if self.interaction is None or self.interaction["interactionId"] != interaction_id:
            raise KeyError(interaction_id)
        record = {
            "interaction_id": interaction_id,
            "user_subject": "user-1",
        }
        return record, {}, self.interaction

    @staticmethod
    def _interaction(interaction_id, *, interaction_type):
        return {
            "schemaVersion": "agentbridge.interaction.v1",
            "interactionId": interaction_id,
            "type": interaction_type,
            "state": "pending",
            "title": "Trusted interaction",
            "message": "Continue in the trusted surface.",
            "presentation": {"url": "https://agentbridge.test/card"},
            "resume": {"ready": False, "completed": False},
        }

    @staticmethod
    def _response(operation, *, reused):
        interaction = (operation.get("next_action") or {}).get("interaction")
        return {
            "protocolVersion": "0.1",
            "operationId": operation["operation_id"],
            "status": operation["status"],
            "result": operation.get("result"),
            "error": operation.get("error"),
            "nextAction": operation.get("next_action"),
            "interaction": interaction,
            "reused": reused,
        }


class TaskPlanRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "agentbridge.db"
        self.service = FakePlanService(self.db_path)
        self.plans = TaskPlanStore(self.db_path)
        self.transforms = build_transform_registry()
        self.runtime = TaskPlanRuntime(
            service=self.service,
            plans=self.plans,
            transforms=self.transforms,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_resolved_binding_is_validated_before_creating_sink_operation(self):
        # The declared union permits planning a string sink; the actual runtime
        # result is an integer, so the sink must never be invoked or create a card.
        self.transforms.register(TransformSpec(
            name="test.union_draft.v1", description="binding validation probe",
            input_schema={"type": "object", "properties": {"items": {"type": "array"}}},
            output_schema={"type": "object", "properties": {"draft": {"type": ["string", "integer"]}}},
            maximum_input_items=100, maximum_output_chars=100,
        ), lambda _arguments: {"draft": 7})
        compiled = validate_and_compile_task_plan(
            {"schemaVersion": "agentbridge.task-plan.proposal.v1", "goal": "test binding",
             "steps": [
                 {"stepKey": "read", "kind": "capability", "capabilityName": "source.items"},
                 {"stepKey": "draft", "kind": "transform", "transformName": "test.union_draft.v1",
                  "dependsOn": ["read"], "bindings": {"items": {"step": "read", "pointer": "/items"}}},
                 {"stepKey": "write", "kind": "capability", "capabilityName": "sink.prepare",
                  "dependsOn": ["draft"], "bindings": {"content": {"step": "draft", "pointer": "/draft"}}},
             ]},
            registry=self.service.registry, transforms=self.transforms,
            trusted_write_prepares={"sink.prepare"}, hidden_commit_capabilities={"sink.commit"},
            scope_resolver=lambda _name: frozenset(),
        )
        plan, _ = self.plans.create(
            user_subject="user-1", parent_task_id="task-1", compiled_plan=compiled,
            proposal_source="agent_host", coordinator_lease_version=1, idempotency_key="bad-binding",
        )
        result = self.runtime.start(plan["plan_id"], user_subject="user-1")
        self.assertEqual(result["status"], "failed")
        stored = self.plans.get(plan["plan_id"], user_subject="user-1")
        self.assertEqual(stored["steps"][-1]["error_code"], "PLAN_SCHEMA_INVALID")
        self.assertEqual(stored["steps"][-1]["error_message"], "参数 content 类型必须是 string。")
        self.assertEqual(len(self.service.operations.list()), 2)
        self.assertFalse(any(item["capability_name"] == "sink.prepare" for item in self.service.operations.list()))
        self.assertIsNone(self.service.interaction)
        self.assertEqual(self.service.source_calls, 1)

    def compile(self, *, include_sink):
        steps = [
            {
                "stepKey": "read",
                "kind": "capability",
                "capabilityName": "source.items",
            },
            {
                "stepKey": "draft",
                "kind": "transform",
                "transformName": "work_items_to_log_draft.v1",
                "dependsOn": ["read"],
                "bindings": {"items": {"step": "read", "pointer": "/items"}},
            },
        ]
        if include_sink:
            steps.append(
                {
                    "stepKey": "write",
                    "kind": "capability",
                    "capabilityName": "sink.prepare",
                    "dependsOn": ["draft"],
                    "bindings": {
                        "content": {"step": "draft", "pointer": "/draft"}
                    },
                }
            )
        return validate_and_compile_task_plan(
            {
                "schemaVersion": "agentbridge.task-plan.proposal.v1",
                "goal": "test plan",
                "steps": steps,
            },
            registry=self.service.registry,
            transforms=self.transforms,
            trusted_write_prepares={"sink.prepare"},
            hidden_commit_capabilities={"sink.commit"},
            scope_resolver=lambda name: frozenset(
                {"sink:write"} if name == "sink.prepare" else {"source:read"}
            ),
            granted_scopes={"source:read", "sink:write"},
        )

    def create_plan(self, *, include_sink):
        plan, _ = self.plans.create(
            user_subject="user-1",
            parent_task_id="task-1",
            compiled_plan=self.compile(include_sink=include_sink),
            proposal_source="agent_host",
            coordinator_lease_version=1,
            idempotency_key="plan-1",
        )
        return plan

    def compile_v2(self, *, include_sink, include_draft=True):
        steps = [
            {
                "stepKey": "read_done",
                "kind": "capability",
                "capabilityName": "oa.workflow.done.list",
                "arguments": {
                    "start_date": "2026-08-30",
                    "end_date": "2026-08-30",
                },
            },
            {
                "stepKey": "read_sent",
                "kind": "capability",
                "capabilityName": "oa.workflow.sent.list",
                "arguments": {
                    "start_date": "2026-08-30",
                    "end_date": "2026-08-30",
                },
            },
            {
                "stepKey": "merge",
                "kind": "transform",
                "transformName": "merge_work_items.v1",
                "dependsOn": ["read_done", "read_sent"],
                "bindings": {
                    "sources": {
                        "mode": "many",
                        "items": [
                            {"step": "read_done", "pointer": ""},
                            {"step": "read_sent", "pointer": ""},
                        ],
                    }
                },
            },
        ]
        if include_draft:
            steps.append(
                {
                    "stepKey": "draft",
                    "kind": "transform",
                    "transformName": "work_items_to_log_draft.v2",
                    "dependsOn": ["merge"],
                    "bindings": {
                        "bundle": {
                            "mode": "single",
                            "step": "merge",
                            "pointer": "",
                        }
                    },
                }
            )
        if include_sink:
            self.assertTrue(include_draft)
            steps.append(
                {
                    "stepKey": "write",
                    "kind": "capability",
                    "capabilityName": "taihua.work_log.create.prepare",
                    "dependsOn": ["draft"],
                    "arguments": {"log_date": "2026-08-30", "hours": 1},
                    "bindings": {
                        "content": {
                            "mode": "single",
                            "step": "draft",
                            "pointer": "/draft",
                        }
                    },
                }
            )
        compiled = validate_and_compile_task_plan(
            {
                "schemaVersion": "agentbridge.task-plan.proposal.v2",
                "goal": "v2 composed plan",
                "constraints": {
                    "temporal": {
                        "kind": "absolute_range",
                        "start": "2026-08-30",
                        "end": "2026-08-30",
                    }
                },
                "steps": steps,
            },
            registry=self.service.registry,
            transforms=self.transforms,
            trusted_write_prepares={"taihua.work_log.create.prepare"},
            hidden_commit_capabilities={"sink.commit"},
            scope_resolver=lambda name: frozenset(
                {"taihua:write:worklog"}
                if name.startswith("taihua.")
                else {"oa:read"}
            ),
            granted_scopes={"oa:read", "taihua:write:worklog"},
        )
        compiled["authoritySnapshot"] = {
            "tokenId": "token-1",
            "userSubject": "user-1",
            "requiredScopes": compiled["requiredScopes"],
        }
        return compiled

    def create_v2_plan(self, *, include_sink, include_draft=True):
        plan, _ = self.plans.create(
            user_subject="user-1",
            parent_task_id="task-1",
            compiled_plan=self.compile_v2(
                include_sink=include_sink,
                include_draft=include_draft,
            ),
            proposal_source="agent_host",
            coordinator_lease_version=1,
            idempotency_key=f"v2-{include_sink}-{include_draft}",
        )
        return plan

    def test_read_and_transform_plan_completes_without_host_replanning(self):
        plan = self.create_plan(include_sink=False)

        response = self.runtime.start(plan["plan_id"], user_subject="user-1")

        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(response["plan"]["state"], "succeeded")
        self.assertEqual(
            [step["state"] for step in response["plan"]["steps"]],
            ["succeeded", "succeeded"],
        )
        self.assertEqual(len(self.service.tasks.completed), 1)

    def test_v2_preview_persists_private_projection_and_effect_outcome(self):
        plan = self.create_v2_plan(include_sink=False)

        response = self.runtime.start(plan["plan_id"], user_subject="user-1")

        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(response["plan"]["effectOutcome"], "preview_ready")
        projection = response["plan"]["resultProjection"]
        self.assertEqual(projection["kind"], "private_draft")
        self.assertIn("处理《流程一》", projection["result"]["draft"])
        self.assertIn("发起《流程一》", projection["result"]["draft"])
        self.assertEqual(
            sum(
                event["event_type"] == "plan.result.ready"
                for event in self.service.tasks.events
            ),
            1,
        )

    def test_v2_incomplete_source_stops_before_draft_or_write(self):
        self.service.source_coverage_status = "partial"
        plan = self.create_v2_plan(include_sink=True)

        response = self.runtime.start(plan["plan_id"], user_subject="user-1")

        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"]["code"], "PLAN_SOURCE_INCOMPLETE")
        self.assertEqual(response["plan"]["effectOutcome"], "source_incomplete")
        self.assertTrue(response["nextAction"]["doNotRetryAtomicTools"])
        self.assertFalse(response["nextAction"]["businessWriteOccurred"])
        stopped = self.runtime._plan_result(self.plans.get(
            plan["plan_id"], user_subject="user-1"
        ))
        self.assertEqual(stopped["nextAction"], response["nextAction"])
        self.assertEqual(
            [step["state"] for step in response["plan"]["steps"]],
            ["succeeded", "succeeded", "failed", "canceled", "canceled"],
        )
        self.assertIsNone(self.service.interaction)

    def test_v2_incomplete_source_remains_visible_for_read_only_preview(self):
        self.service.source_coverage_status = "partial"
        plan = self.create_v2_plan(include_sink=False)

        response = self.runtime.start(plan["plan_id"], user_subject="user-1")

        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(response["plan"]["state"], "succeeded")
        self.assertEqual(response["plan"]["effectOutcome"], "preview_ready")
        self.assertTrue(
            response["plan"]["resultProjection"]["result"]["source_incomplete"]
        )
        self.assertEqual(
            response["plan"]["resultProjection"]["result"]["coverage"]["status"],
            "partial",
        )
        self.assertEqual(
            [step["state"] for step in response["plan"]["steps"]],
            ["succeeded", "succeeded", "succeeded", "succeeded"],
        )

    def test_v2_source_summary_projection_is_self_contained(self):
        self.service.source_coverage_status = "partial"
        plan = self.create_v2_plan(include_sink=False, include_draft=False)

        response = self.runtime.start(plan["plan_id"], user_subject="user-1")

        self.assertEqual(response["status"], "succeeded")
        projection = response["plan"]["resultProjection"]
        self.assertEqual(projection["kind"], "source_summary")
        self.assertEqual(len(projection["result"]["items"]), 2)
        self.assertTrue(projection["result"]["source_incomplete"])
        self.assertEqual(
            response["nextAction"],
            {
                "type": "report_plan_result",
                "source": "plan.resultProjection.result",
                "doNotQueryOperations": True,
            },
        )

    def test_v2_revoked_authority_stops_before_first_business_call(self):
        self.service.authority_valid = False
        plan = self.create_v2_plan(include_sink=False)

        response = self.runtime.start(plan["plan_id"], user_subject="user-1")

        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"]["code"], "PLAN_AUTHORITY_INVALID")
        self.assertEqual(self.service.source_calls, 0)

    def test_write_prepare_waits_then_resumes_to_plan_completion(self):
        plan = self.create_plan(include_sink=True)
        waiting = self.runtime.start(plan["plan_id"], user_subject="user-1")

        self.assertEqual(waiting["status"], "requires_user_action")
        self.assertEqual(waiting["plan"]["state"], "waiting_user")
        operation, _ = self.service.operations.create(
            user_subject="user-1",
            capability_name="sink.commit",
            capability_version="1.0.0",
            input_summary={},
            input_identity={},
            idempotency_key="commit-1",
        )
        self.service.operations.mark_running(operation["operation_id"])
        operation = self.service.operations.mark_succeeded(
            operation["operation_id"], {"verified": True}
        )
        completed = self.runtime.resume_after_capability(
            user_subject="user-1",
            interaction_id=waiting["interaction"]["interactionId"],
            response=self.service._response(operation, reused=False),
        )

        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["plan"]["state"], "succeeded")
        self.assertEqual(completed["plan"]["steps"][-1]["state"], "succeeded")

    def test_login_completion_retries_the_same_step_with_a_new_attempt(self):
        self.service.source_requires_login = True
        plan = self.create_plan(include_sink=False)
        waiting = self.runtime.start(plan["plan_id"], user_subject="user-1")

        completed = self.runtime.resume_after_session(
            user_subject="user-1",
            interaction_id=waiting["interaction"]["interactionId"],
        )

        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(self.service.source_calls, 2)
        stored = self.plans.get(plan["plan_id"], user_subject="user-1")
        self.assertEqual(stored["steps"][0]["attempt_count"], 2)

    def test_new_runtime_recovers_an_abandoned_running_step(self):
        plan = self.create_plan(include_sink=False)
        started = self.plans.begin_next_step(
            plan["plan_id"], user_subject="user-1"
        )
        self.assertEqual(started["state"], "running")
        restarted_runtime = TaskPlanRuntime(
            service=self.service,
            plans=self.plans,
            transforms=self.transforms,
        )

        summary = restarted_runtime.recover()

        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["restarted"], 1)
        self.assertEqual(summary["completed"], 1)
        stored = self.plans.get(plan["plan_id"], user_subject="user-1")
        self.assertEqual(stored["state"], "succeeded")
        self.assertEqual(stored["steps"][0]["attempt_count"], 2)

    def test_canceling_a_completed_plan_is_a_quiet_idempotent_read(self):
        plan = self.create_plan(include_sink=False)
        self.runtime.start(plan["plan_id"], user_subject="user-1")
        event_count = len(self.service.tasks.events)

        response = self.runtime.cancel(
            plan["plan_id"], user_subject="user-1", reason="too_late"
        )

        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(len(self.service.tasks.events), event_count)

    def test_polling_declined_interaction_cancels_plan_without_resume(self):
        plan = self.create_plan(include_sink=True)
        waiting = self.runtime.start(plan["plan_id"], user_subject="user-1")
        self.service.interaction["state"] = "declined"
        self.service.task_plan_runtime = self.runtime

        for _ in range(2):
            CentralCapabilityService.get_interaction(
                self.service,
                user_subject="user-1",
                interaction_id=waiting["interaction"]["interactionId"],
            )

        stored = self.plans.get(plan["plan_id"], user_subject="user-1")
        self.assertEqual(stored["state"], "canceled")
        self.assertEqual(stored["steps"][-1]["state"], "canceled")
        self.assertEqual(self.service.source_calls, 1)
        self.assertEqual(sum(e["event_type"] == "plan.canceled" for e in self.service.tasks.events), 1)

    def assert_terminal_recovery(self, state):
        plan = self.create_plan(include_sink=True)
        self.runtime.start(plan["plan_id"], user_subject="user-1")
        self.service.interaction["state"] = state
        restarted = TaskPlanRuntime(
            service=self.service, plans=self.plans, transforms=self.transforms
        )

        result = restarted.recover()

        stored = self.plans.get(plan["plan_id"], user_subject="user-1")
        expected = "canceled" if state == "declined" else "failed"
        self.assertEqual(stored["state"], expected)
        self.assertEqual(result[expected], 1)
        self.assertEqual(self.service.source_calls, 1)
        self.assertTrue(all(s["attempt_count"] == 1 for s in stored["steps"]))

    def test_recovery_reconciles_declined_interaction(self):
        self.assert_terminal_recovery("declined")

    def test_recovery_reconciles_expired_interaction(self):
        self.assert_terminal_recovery("expired")

    def test_recovery_reconciles_failed_interaction(self):
        self.assert_terminal_recovery("failed")

    def test_recovery_reconciles_superseded_interaction(self):
        self.assert_terminal_recovery("superseded")

    def test_recovery_does_not_consume_pending_or_completed_authorization(self):
        plan = self.create_plan(include_sink=True)
        self.runtime.start(plan["plan_id"], user_subject="user-1")
        for state in ("pending", "completed"):
            with self.subTest(state=state):
                self.service.interaction["state"] = state
                self.service.interaction["resume"] = {"ready": True, "completed": False}
                result = self.runtime.recover()
                self.assertEqual(result["waiting"], 1)
                self.assertEqual(self.service.source_calls, 1)
                self.assertEqual(self.plans.get(plan["plan_id"], user_subject="user-1")["state"], "waiting_user")


if __name__ == "__main__":
    unittest.main()
