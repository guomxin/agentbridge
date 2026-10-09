import unittest

from agentbridge.adapters.seeyon_central import build_central_capability_registry
from agentbridge.adapters.taihua import (
    TAIHUA_WORK_LOG_CREATE_CAPABILITY,
    TAIHUA_WORK_LOG_CREATE_PREPARE_CAPABILITY,
    build_taihua_capability_registry,
)
from agentbridge.core.task_plan_validation import (
    PlanValidationError,
    TaskPlanCapabilityStepInput,
    TaskPlanTransformStepInput,
    serialize_task_plan_steps,
    task_plan_step_json_schema,
    validate_and_compile_task_plan,
)
from agentbridge.core.planning_catalog import build_planning_catalog
from agentbridge.core.planning_policy import compile_temporal_constraints, COMPOSED_TASK_PLANNING_POLICY
from agentbridge.core.transforms import build_transform_registry


class TaskPlanValidationTests(unittest.TestCase):
    def test_policy_fits_host_transport_without_losing_trailing_safety_rules(self):
        # OpenClaw normalizePlanningPolicy slices at 6000 UTF-16 code units.
        context = COMPOSED_TASK_PLANNING_POLICY['modelContext']
        self.assertLessEqual(len(context.encode('utf-16-le')) // 2, 6000)

    def setUp(self):
        self.registry = build_central_capability_registry()
        for spec in build_taihua_capability_registry().list():
            self.registry.register(spec)
        self.transforms = build_transform_registry()
        self.prepares = {TAIHUA_WORK_LOG_CREATE_PREPARE_CAPABILITY}
        self.commits = {TAIHUA_WORK_LOG_CREATE_CAPABILITY}

    def compile(self, proposal, *, granted=None):
        return validate_and_compile_task_plan(
            proposal,
            registry=self.registry,
            transforms=self.transforms,
            trusted_write_prepares=self.prepares,
            hidden_commit_capabilities=self.commits,
            scope_resolver=lambda name: (
                frozenset({"taihua:write:worklog"})
                if name.startswith("taihua.")
                else frozenset({"oa:read"})
            ),
            granted_scopes=granted,
        )

    def proposal(self):
        return {
            "schemaVersion": "agentbridge.task-plan.proposal.v1",
            "goal": "汇总今天 OA 已办并填写工作日志",
            "steps": [
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
                    "stepKey": "draft_log",
                    "kind": "transform",
                    "transformName": "work_items_to_log_draft.v1",
                    "dependsOn": ["read_done"],
                    "bindings": {
                        "items": {"step": "read_done", "pointer": "/items"}
                    },
                },
                {
                    "stepKey": "prepare_log",
                    "kind": "capability",
                    "capabilityName": TAIHUA_WORK_LOG_CREATE_PREPARE_CAPABILITY,
                    "dependsOn": ["draft_log"],
                    "arguments": {"log_date": "2026-08-30", "hours": 3},
                    "bindings": {
                        "content": {"step": "draft_log", "pointer": "/draft"}
                    },
                },
            ],
        }

    def proposal_v2(self, *, include_sink=True):
        steps = [
            {
                "stepKey": "read_done",
                "kind": "capability",
                "capabilityName": "oa.workflow.done.list",
                "arguments": {"limit": 100},
            },
            {
                "stepKey": "read_sent",
                "kind": "capability",
                "capabilityName": "oa.workflow.sent.list",
                "arguments": {"limit": 100},
            },
            {
                "stepKey": "merge_items",
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
            {
                "stepKey": "draft_log",
                "kind": "transform",
                "transformName": "work_items_to_log_draft.v2",
                "dependsOn": ["merge_items"],
                "bindings": {
                    "bundle": {
                        "mode": "single",
                        "step": "merge_items",
                        "pointer": "",
                    }
                },
            },
        ]
        if include_sink:
            steps.append(
                {
                    "stepKey": "prepare_log",
                    "kind": "capability",
                    "capabilityName": TAIHUA_WORK_LOG_CREATE_PREPARE_CAPABILITY,
                    "dependsOn": ["draft_log"],
                    "arguments": {"log_date": "2026-08-30", "hours": 1},
                    "bindings": {
                        "content": {
                            "mode": "single",
                            "step": "draft_log",
                            "pointer": "/draft",
                        }
                    },
                }
            )
        return {
            "schemaVersion": "agentbridge.task-plan.proposal.v2",
            "goal": "汇总昨天 OA 已办和已发并形成日志",
            "constraints": {"temporal": {"kind": "previous_day"}},
            "steps": steps,
        }

    def test_valid_plan_is_topologically_compiled_and_scoped(self):
        compiled = self.compile(
            self.proposal(),
            granted={"oa:read", "taihua:write:worklog"},
        )

        self.assertEqual(
            [step["stepKey"] for step in compiled["steps"]],
            ["read_done", "draft_log", "prepare_log"],
        )
        self.assertEqual(
            compiled["requiredScopes"],
            ["oa:read", "taihua:write:worklog"],
        )
        self.assertEqual(compiled["riskSummary"]["writeSinkCount"], 1)
        self.assertEqual(len(compiled["planHash"]), 64)

    def test_hidden_commit_is_rejected(self):
        proposal = self.proposal()
        proposal["steps"][-1]["capabilityName"] = TAIHUA_WORK_LOG_CREATE_CAPABILITY

        with self.assertRaises(PlanValidationError) as raised:
            self.compile(proposal)

        self.assertEqual(raised.exception.code, "PLAN_CAPABILITY_NOT_ALLOWED")

    def test_v2_many_source_plan_compiles_with_explicit_provenance(self):
        proposal, temporal = compile_temporal_constraints(
            self.proposal_v2(),
            accepted_at="2026-08-31T09:30:43+08:00",
        )

        compiled = validate_and_compile_task_plan(
            proposal,
            registry=self.registry,
            transforms=self.transforms,
            trusted_write_prepares=self.prepares,
            hidden_commit_capabilities=self.commits,
            scope_resolver=lambda name: (
                frozenset({"taihua:write:worklog"})
                if name.startswith("taihua.")
                else frozenset({"oa:read"})
            ),
            granted_scopes={"oa:read", "taihua:write:worklog"},
            temporal_context=temporal,
        )

        self.assertEqual(compiled["schemaVersion"], "agentbridge.task-plan.compiled.v2")
        self.assertEqual(
            compiled["temporalContext"]["absoluteRange"],
            {"start": "2026-08-30", "end": "2026-08-30"},
        )
        self.assertEqual(
            compiled["steps"][2]["bindings"]["sources"]["mode"], "many"
        )

    def test_day_before_yesterday_uses_trusted_acceptance_date_in_shanghai(self):
        proposal = self.proposal_v2()
        proposal["constraints"]["temporal"]["kind"] = "day_before_yesterday"
        for step in proposal["steps"]:
            if step.get("capabilityName") in {"oa.workflow.done.list", "oa.workflow.sent.list"}:
                step["arguments"] = {"limit": 1000}
        compiled, context = compile_temporal_constraints(proposal, accepted_at="2026-09-02T17:00:00Z")
        self.assertEqual(context["absoluteRange"], {"start": "2026-09-01", "end": "2026-09-01"})
        for step in compiled["steps"][:2]:
            self.assertEqual(step["arguments"]["start_date"], "2026-09-01")

    def test_v2_rejects_a_named_but_unbound_business_source(self):
        proposal = self.proposal_v2()
        proposal["steps"][2]["bindings"]["sources"]["items"] = [
            {"step": "read_done", "pointer": ""}
        ]

        with self.assertRaises(PlanValidationError) as raised:
            self.compile(proposal)

        self.assertEqual(raised.exception.code, "PLAN_PROVENANCE_INCOMPLETE")

    def test_v2_rejects_static_derived_content(self):
        proposal = self.proposal_v2()
        sink = proposal["steps"][-1]
        sink["bindings"] = {}
        sink["arguments"]["content"] = "模型自行编写的正文"

        with self.assertRaises(PlanValidationError) as raised:
            self.compile(proposal)

        self.assertEqual(raised.exception.code, "PLAN_PROVENANCE_REQUIRED")

    def test_v2_rejects_ambiguous_binding_without_mode(self):
        proposal = self.proposal_v2()
        proposal["steps"][3]["bindings"]["bundle"] = {
            "step": "merge_items",
            "pointer": "",
        }

        with self.assertRaises(PlanValidationError) as raised:
            self.compile(proposal)

        self.assertEqual(raised.exception.code, "PLAN_BINDING_INVALID")

    def test_relative_time_uses_trusted_acceptance_time_and_rejects_override(self):
        compiled, temporal = compile_temporal_constraints(
            self.proposal_v2(include_sink=False),
            accepted_at="2026-08-31T23:59:59+08:00",
        )
        self.assertEqual(
            compiled["steps"][0]["arguments"]["start_date"], "2026-08-30"
        )
        self.assertEqual(temporal["timeZone"], "Asia/Shanghai")

        conflicting = self.proposal_v2(include_sink=False)
        conflicting["steps"][0]["arguments"]["start_date"] = "2026-08-29"
        with self.assertRaisesRegex(ValueError, "时间约束不一致"):
            compile_temporal_constraints(
                conflicting,
                accepted_at="2026-08-31T23:59:59+08:00",
            )

    def test_missing_scope_is_rejected_before_execution(self):
        with self.assertRaises(PlanValidationError) as raised:
            self.compile(self.proposal(), granted={"oa:read"})

        self.assertEqual(raised.exception.code, "PLAN_SCOPE_MISSING")

    def test_binding_must_reference_dependency_chain(self):
        proposal = self.proposal()
        proposal["steps"][1]["dependsOn"] = []

        with self.assertRaises(PlanValidationError) as raised:
            self.compile(proposal)

        self.assertEqual(raised.exception.code, "PLAN_BINDING_INVALID")

    def test_cycle_is_rejected(self):
        proposal = self.proposal()
        proposal["steps"][0]["dependsOn"] = ["draft_log"]

        with self.assertRaises(PlanValidationError) as raised:
            self.compile(proposal)

        self.assertEqual(raised.exception.code, "PLAN_CYCLE_DETECTED")

    def test_write_sink_must_be_final(self):
        proposal = self.proposal()
        proposal["steps"].append(
            {
                "stepKey": "read_again",
                "kind": "capability",
                "capabilityName": "oa.workflow.done.list",
                "dependsOn": ["prepare_log"],
            }
        )

        with self.assertRaises(PlanValidationError) as raised:
            self.compile(proposal)

        self.assertEqual(raised.exception.code, "PLAN_WRITE_SINK_NOT_FINAL")

    def test_model_visible_step_schema_exposes_exact_fields(self):
        schema = task_plan_step_json_schema()

        self.assertEqual(schema["discriminator"]["propertyName"], "kind")
        capability = schema["$defs"]["TaskPlanCapabilityStepInput"]
        transform = schema["$defs"]["TaskPlanTransformStepInput"]
        binding = schema["$defs"]["TaskPlanBindingInput"]
        self.assertTrue(
            {"stepKey", "kind", "capabilityName"}.issubset(capability["required"])
        )
        self.assertTrue(
            {"stepKey", "kind", "transformName"}.issubset(transform["required"])
        )
        self.assertEqual(set(binding["required"]), {"step", "pointer"})

    def test_typed_steps_preserve_only_fields_supplied_by_host(self):
        steps = [
            TaskPlanCapabilityStepInput(
                stepKey="read_done",
                kind="capability",
                capabilityName="oa.workflow.done.list",
                arguments={"start_date": "2026-07-01"},
            ),
            TaskPlanTransformStepInput(
                stepKey="draft_log",
                kind="transform",
                transformName="work_items_to_log_draft.v1",
                dependsOn=["read_done"],
                bindings={
                    "items": {"step": "read_done", "pointer": "/items"}
                },
            ),
        ]

        self.assertEqual(
            serialize_task_plan_steps(steps),
            [
                {
                    "stepKey": "read_done",
                    "kind": "capability",
                    "capabilityName": "oa.workflow.done.list",
                    "arguments": {"start_date": "2026-07-01"},
                },
                {
                    "stepKey": "draft_log",
                    "dependsOn": ["read_done"],
                    "bindings": {
                        "items": {"step": "read_done", "pointer": "/items"}
                    },
                    "kind": "transform",
                    "transformName": "work_items_to_log_draft.v1",
                },
            ],
        )

    def test_planning_catalog_returns_scope_safe_prepare_examples(self):
        def resolve(name):
            if name.startswith("taihua."):
                return frozenset({"taihua:write:worklog"})
            return frozenset({"oa:read"})

        full = build_planning_catalog(
            registry=self.registry,
            transforms=self.transforms,
            trusted_write_prepares=self.prepares,
            hidden_commit_capabilities=self.commits,
            scope_resolver=resolve,
            granted_scopes={"oa:read", "taihua:write:worklog"},
        )
        limited = build_planning_catalog(
            registry=self.registry,
            transforms=self.transforms,
            trusted_write_prepares=self.prepares,
            hidden_commit_capabilities=self.commits,
            scope_resolver=resolve,
            granted_scopes={"oa:read"},
        )

        self.assertEqual(len(full["prepareInputGuide"]["examples"]), 3)
        self.assertEqual(len(limited["prepareInputGuide"]["examples"]), 2)
        self.assertEqual(
            limited["prepareInputGuide"]["examples"][0]["steps"][0]["stepKey"],
            "read_done",
        )
        self.assertEqual(
            limited["prepareInputGuide"]["examples"][0]["steps"][-1][
                "transformName"
            ],
            "merge_work_items.v1",
        )
        self.assertIn(
            "只读展示",
            limited["prepareInputGuide"]["examples"][0]["title"],
        )
        self.assertLess(
            list(full).index("prepareInputGuide"),
            list(full).index("capabilities"),
        )
        self.assertNotIn(
            "taihua.work_log.create.prepare",
            str(limited["prepareInputGuide"]["examples"]),
        )
        self.assertTrue(
            any(
                "inputSchema.properties" in rule
                for rule in limited["prepareInputGuide"]["rules"]
            )
        )


if __name__ == "__main__":
    unittest.main()
