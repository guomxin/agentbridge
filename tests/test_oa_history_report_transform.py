from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.oa_history_query_service import history_query_capability_specs
from agentbridge.core.oa_history_queries import response_chars, result_page
from agentbridge.core.task_plan_runtime import TaskPlanRuntime
from agentbridge.core.task_plan_validation import PlanValidationError, validate_and_compile_task_plan
from agentbridge.core.task_plans import TaskPlanStore
from agentbridge.core.task_projections import build_plan_result_projection
from agentbridge.core.transforms import OA_HISTORY_REPORT, TransformRejected, build_transform_registry
from tests.test_task_plan_runtime import FakePlanService


def report_fixture(*, state="complete", total=300, offset=0, more=True):
    complete = state == "complete"
    items = [{"affair_id": "a", "title": "业务结算单", "category": "报销",
              "initiated_at": "2026-11-04 09:00", "processed_at": "2026-11-05 10:00",
              "workflow_state": "in_progress", "workflow_state_text": "处理中",
              "current_handlers": ["Alice"], "current_nodes": [{"name": "财务审核", "handlers": ["Alice"]}],
              "observed_at": "2026-11-09T09:00:00+08:00", "unavailable_fields": []}]
    if state in {"scanning", "details"} or not total:
        items = []
    report = {"schema_version": "agentbridge.oa_history_query.v1", "query_id": "query-fixture",
        "collection": "done", "state": state,
        "temporal": {"constraint": {"kind": "all"}, "acceptedAt": "2026-11-09T09:00:00+08:00",
            "timeZone": "Asia/Shanghai", "precision": "day", "bounds": "inclusive",
            "absoluteRange": {"start": None, "end": None}, "dateBasis": "processed_at"},
        "progress": {"source_total": 1125, "scanned_count": 1125 if complete else 50,
            "matched_count": total, "details_checked": total if complete else 0, "details_failed": 0},
        "coverage": {"status": "complete" if complete else "partial", "completionReason": "complete" if complete else "query_in_progress",
            "scan_complete": complete or state == "details", "details_complete": complete},
        "items": items, "next_cursor": "opaque-next" if more else None,
        "replay_cursor": "opaque-current", "has_more": more}
    if state in {"complete", "partial"}:
        report.update(total=total, returned_count=len(items), delivery={"offset": offset, "complete": not more})
    return report


class HistoryPlanService(FakePlanService):
    def __init__(self, db_path, report):
        super().__init__(db_path)
        for spec in history_query_capability_specs():
            self.registry.register(spec)
        self.report = report

    def invoke(self, *, user_subject, capability_name, arguments, idempotency_key, **kwargs):
        if capability_name != "oa.workflow.history.query":
            return super().invoke(user_subject=user_subject, capability_name=capability_name,
                                  arguments=arguments, idempotency_key=idempotency_key, **kwargs)
        self.source_calls += 1
        spec = self.registry.get(capability_name)
        operation, reused = self.operations.create(user_subject=user_subject, capability_name=capability_name,
            capability_version=spec.version, input_summary=arguments, input_identity=arguments, idempotency_key=idempotency_key)
        if not reused:
            self.operations.mark_running(operation["operation_id"])
            operation = self.operations.mark_succeeded(operation["operation_id"], self.report)
        return self._response(operation, reused=reused)


class OaHistoryReportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="oa-history-report-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "agentbridge.db"
        self.transforms = build_transform_registry()
        self.service = HistoryPlanService(self.path, report_fixture())

    def proposal(self, version="v2"):
        binding = {"step": "query", "pointer": ""}
        if version == "v2":
            binding["mode"] = "single"
        return {"schemaVersion": "agentbridge.task-plan.proposal." + version,
            "goal": "查看全部已办业务结算单和当前处理状态",
            **({"constraints": {"temporal": {"kind": "all"}}} if version == "v2" else {}),
            "steps": [
                {"stepKey": "query", "kind": "capability", "capabilityName": "oa.workflow.history.query",
                 "arguments": {"collection": "done", "temporal": {"kind": "all"}, "keyword": "业务结算单"}},
                {"stepKey": "report", "kind": "transform", "transformName": OA_HISTORY_REPORT,
                 "dependsOn": ["query"], "bindings": {"report": binding}},
            ]}

    def compile(self, proposal):
        return validate_and_compile_task_plan(proposal, registry=self.service.registry, transforms=self.transforms,
            trusted_write_prepares={"sink.prepare", "taihua.work_log.create.prepare"},
            hidden_commit_capabilities={"sink.commit"}, scope_resolver=lambda _name: frozenset())

    def assert_plan_error(self, proposal, code):
        with self.assertRaises(PlanValidationError) as caught:
            self.compile(proposal)
        self.assertEqual(caught.exception.code, code)

    def test_saved_collection_reference_has_no_200_item_limit_or_field_clipping(self):
        report = report_fixture(total=500)
        report["items"][0]["title"] = "完整业务标题" * 180
        before = deepcopy(report)
        result = self.transforms.invoke(OA_HISTORY_REPORT, {"report": report})
        for key in ("query_id", "temporal", "progress", "items", "coverage", "next_cursor", "total", "delivery"):
            self.assertEqual(result[key], report[key])
        self.assertEqual(result["total"], 500)
        self.assertTrue(result["source_complete"])
        self.assertFalse(result["delivery_complete"])
        self.assertFalse(result["report_complete"])
        self.assertEqual(report, before)

    def test_scanning_and_details_are_incomplete_references_instead_of_empty_results(self):
        for state in ("scanning", "details", "partial"):
            with self.subTest(state=state):
                report = report_fixture(state=state)
                result = self.transforms.invoke(OA_HISTORY_REPORT, {"report": report})
                self.assertEqual(result["state"], state)
                self.assertTrue(result["source_incomplete"])
                self.assertFalse(result["report_complete"])
                self.assertFalse(result["empty"])
                self.assertEqual(result["next_cursor"], report["next_cursor"])

    def test_last_page_is_not_the_complete_delivery_and_complete_empty_is_explicit(self):
        last = self.transforms.invoke(OA_HISTORY_REPORT, {"report": report_fixture(total=300, offset=299, more=False)})
        self.assertTrue(last["source_complete"])
        self.assertFalse(last["delivery_complete"])
        whole = self.transforms.invoke(OA_HISTORY_REPORT, {"report": report_fixture(total=1, more=False)})
        self.assertTrue(whole["report_complete"])
        empty = self.transforms.invoke(OA_HISTORY_REPORT, {"report": report_fixture(total=0, more=False)})
        self.assertTrue(empty["empty"])
        self.assertTrue(empty["report_complete"])

    def test_private_source_fields_never_reach_transform_or_task_projection(self):
        report = report_fixture()
        report["_source"] = {"private": "private-marker"}
        report["session_id"] = "private-marker"
        report["coverage"]["_source"] = "private-marker"
        report["items"][0]["_source"] = {"process_id": "private-marker"}
        report["items"][0]["current_nodes"][0]["process_id"] = "private-marker"
        report["temporal"]["constraint"]["_source"] = "private-marker"
        result = self.transforms.invoke(OA_HISTORY_REPORT, {"report": report})
        self.assertNotIn("private-marker", str(result))
        projection = build_plan_result_projection(projection_kind="oa_history_report", operation_id="op-fixture",
            step={"step_key": "report", "bindings": {"report": {"mode": "single", "step": "query", "pointer": ""}}}, result=report)
        self.assertNotIn("private-marker", str(projection))
        self.assertEqual(projection["result"]["items"], result["items"])

    def test_oversized_page_is_rejected_without_cutting_rows_or_fields(self):
        report = report_fixture(total=1)
        report["items"][0]["title"] = "😀" * 10000
        with self.assertRaises(TransformRejected) as caught:
            self.transforms.invoke(OA_HISTORY_REPORT, {"report": report})
        self.assertEqual(caught.exception.code, "TRANSFORM_OUTPUT_TOO_LARGE")
        self.assertEqual(len(report["items"][0]["title"]), 10000)

    def test_model_filled_or_sliced_or_unrelated_source_cannot_back_the_report(self):
        proposal = self.proposal()
        proposal["steps"][-1]["bindings"] = {}
        proposal["steps"][-1]["arguments"] = {"report": report_fixture()}
        self.assert_plan_error(proposal, "PLAN_PROVENANCE_REQUIRED")
        proposal = self.proposal()
        proposal["steps"][-1]["bindings"]["report"]["pointer"] = "/items"
        self.assert_plan_error(proposal, "PLAN_PROVENANCE_REQUIRED")
        proposal = self.proposal()
        proposal["steps"][0] = {"stepKey": "query", "kind": "capability", "capabilityName": "source.items"}
        self.assert_plan_error(proposal, "PLAN_READ_ONLY_REFERENCE_REQUIRED")

    def test_history_query_cannot_be_used_by_legacy_merge_or_any_write_plan(self):
        for version in ("v1", "v2"):
            with self.subTest(version=version):
                proposal = self.proposal(version)
                source_binding = ({"mode": "many", "items": [{"step": "query", "pointer": ""}]}
                                  if version == "v2" else {"step": "query", "pointer": ""})
                proposal["steps"][-1] = {"stepKey": "merge", "kind": "transform", "transformName": "merge_work_items.v1",
                    "dependsOn": ["query"], "bindings": {"sources": source_binding}}
                self.assert_plan_error(proposal, "PLAN_READ_ONLY_REFERENCE_REQUIRED")
                proposal = self.proposal(version)
                proposal["steps"].append({"stepKey": "write", "kind": "capability", "capabilityName": "sink.prepare",
                    "dependsOn": ["report"], "arguments": {"content": "cannot use a reference as a write source"}})
                self.assert_plan_error(proposal, "PLAN_READ_ONLY_REFERENCE_REQUIRED")

    def test_other_business_sources_cannot_be_silently_ignored(self):
        proposal = self.proposal()
        proposal["steps"].insert(1, {"stepKey": "done", "kind": "capability", "capabilityName": "oa.workflow.done.list"})
        self.assert_plan_error(proposal, "PLAN_READ_ONLY_REFERENCE_REQUIRED")
        proposal = self.proposal()
        other = deepcopy(proposal["steps"][0])
        other["stepKey"] = "other_query"
        proposal["steps"].insert(1, other)
        self.assert_plan_error(proposal, "PLAN_READ_ONLY_REFERENCE_REQUIRED")

    def test_runtime_reports_pending_query_reference_and_continuation_without_requerying(self):
        self.service.report = report_fixture(state="scanning")
        plans = TaskPlanStore(self.path)
        plan, _ = plans.create(user_subject="user-1", parent_task_id="task-1", compiled_plan=self.compile(self.proposal()),
            proposal_source="agent_host", coordinator_lease_version=1, idempotency_key="pending-history-report")
        runtime = TaskPlanRuntime(service=self.service, plans=plans, transforms=self.transforms)
        response = runtime.start(plan["plan_id"], user_subject="user-1")
        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(response["completionMeaning"], "read_only_query_reference")
        self.assertFalse(response["queryComplete"])
        self.assertFalse(response["deliveryComplete"])
        self.assertEqual(response["nextAction"]["tool"], "oa_workflow_history_next")
        self.assertEqual(response["nextAction"]["arguments"]["cursor"], "opaque-next")
        self.assertTrue(response["nextAction"]["doNotClaimFullResult"])
        self.assertEqual(response["plan"]["resultProjection"]["result"]["state"], "scanning")
        self.assertEqual(self.service.source_calls, 1)
        self.assertIsNone(self.service.interaction)

    def test_catalog_declares_read_only_terminal_and_exact_authoritative_sources(self):
        spec = self.transforms.get(OA_HISTORY_REPORT).to_dict()
        self.assertTrue(spec["readOnlyTerminal"])
        self.assertEqual(spec["requiredBoundInputs"], ["report"])
        self.assertEqual(spec["allowedSourceCapabilities"], ["oa.workflow.history.query", "oa.workflow.history.next"])
        for version in ("v1", "v2"):
            self.assertEqual(self.compile(self.proposal(version))["steps"][-1]["transformName"], OA_HISTORY_REPORT)

    def test_runtime_keeps_delivery_cursor_when_saved_query_is_complete_but_page_is_not(self):
        self.service.report = report_fixture(state="complete", total=500)
        plans = TaskPlanStore(self.path)
        plan, _ = plans.create(user_subject="user-1", parent_task_id="task-1", compiled_plan=self.compile(self.proposal()),
            proposal_source="agent_host", coordinator_lease_version=1, idempotency_key="complete-history-first-page")
        response = TaskPlanRuntime(service=self.service, plans=plans, transforms=self.transforms).start(
            plan["plan_id"], user_subject="user-1")
        self.assertEqual(response["status"], "succeeded")
        self.assertTrue(response["queryComplete"])
        self.assertFalse(response["deliveryComplete"])
        self.assertTrue(response["nextAction"]["doNotClaimFullResult"])
        self.assertEqual(response["nextAction"]["tool"], "oa_workflow_history_next")
        projected = response["plan"]["resultProjection"]["result"]
        self.assertEqual(projected["total"], 500)
        self.assertNotIn("items", projected)
        self.assertEqual(projected["items_available_via"], "replay_cursor")
        self.assertEqual(response["nextAction"]["arguments"]["cursor"], self.service.report["replay_cursor"])
        self.assertEqual(projected["next_cursor"], self.service.report["next_cursor"])
        saved = plans.get(plan["plan_id"], user_subject="user-1")["result_projection"]["result"]
        self.assertEqual(saved["items"], self.service.report["items"])
        self.assertEqual(self.service.source_calls, 1)

    def test_single_complete_page_uses_replay_even_without_next_cursor(self):
        self.service.report = report_fixture(total=1, more=False)
        plans = TaskPlanStore(self.path)
        plan, _ = plans.create(user_subject="user-1", parent_task_id="task-1", compiled_plan=self.compile(self.proposal()),
            proposal_source="agent_host", coordinator_lease_version=1, idempotency_key="single-history-page")
        response = TaskPlanRuntime(service=self.service, plans=plans, transforms=self.transforms).start(
            plan["plan_id"], user_subject="user-1")
        self.assertTrue(response["queryComplete"])
        self.assertFalse(response["deliveryComplete"])
        self.assertEqual(response["deliveryMode"], "via_replay_cursor")
        self.assertEqual(response["nextAction"]["type"], "read_saved_query_page")
        self.assertEqual(response["nextAction"]["arguments"]["cursor"], "opaque-current")
        projected = response["plan"]["resultProjection"]["result"]
        self.assertIsNone(projected["next_cursor"])
        self.assertFalse(projected["report_complete"])
        saved = plans.get(plan["plan_id"], user_subject="user-1")["result_projection"]["result"]
        self.assertTrue(saved["report_complete"])
        self.assertEqual(saved["items"], self.service.report["items"])

    def test_full_runtime_response_is_bounded_with_near_10000_character_source_pages(self):
        for repeats in (0, 10, 25, 50):
            with self.subTest(repeats=repeats):
                report = report_fixture(total=500)
                rows = []
                for index in range(500):
                    row = deepcopy(report["items"][0])
                    row.update(affair_id=str(index), title="业务结算单" + "项目报销说明😀" * repeats)
                    rows.append(row)
                envelope = {key: value for key, value in report.items() if key not in
                    {"items", "total", "returned_count", "has_more", "next_cursor", "replay_cursor"}}
                page = result_page(rows, envelope=envelope)
                page.pop("next_offset")
                page.update(next_cursor="ohc_" + "n" * 43, replay_cursor="ohc_" + "r" * 43)
                self.assertGreater(response_chars(page), 9000)
                self.assertLessEqual(response_chars(page), 10000)
                self.service.report = page
                proposal = self.proposal()
                proposal["goal"] = "查询业务结算单当前状态" * 40
                plans = TaskPlanStore(self.path)
                plan, _ = plans.create(user_subject="user-1", parent_task_id="task-1", compiled_plan=self.compile(proposal),
                    proposal_source="agent_host", coordinator_lease_version=1, idempotency_key=f"bounded-page-{repeats}")
                response = TaskPlanRuntime(service=self.service, plans=plans, transforms=self.transforms).start(
                    plan["plan_id"], user_subject="user-1")
                serialized = json.dumps(response, ensure_ascii=False, indent=2)
                self.assertLessEqual(response_chars(response), 12000)
                self.assertEqual(json.loads(serialized[:12000]), response)
                self.assertEqual(response["nextAction"]["arguments"]["cursor"], page["replay_cursor"])
                self.assertNotIn("goal", response["plan"])
                self.assertNotIn("steps", response["plan"])
                stored = plans.get(plan["plan_id"], user_subject="user-1")["result_projection"]["result"]
                self.assertEqual(stored["items"], page["items"])

    def test_verbose_metadata_is_available_through_replay_without_overflow_or_truncation(self):
        from agentbridge.core.task_projections import plan_result_response
        report = report_fixture(total=1, more=False)
        report["progress"]["source_total"] = [0] * 900
        self.assertLessEqual(response_chars(report), 10000)
        projected = self.transforms.invoke(OA_HISTORY_REPORT, {"report": report})
        plans = TaskPlanStore(self.path)
        plan, _ = plans.create(user_subject="user-1", parent_task_id="task-1", compiled_plan=self.compile(self.proposal()),
            proposal_source="agent_host", coordinator_lease_version=1, idempotency_key="verbose-metadata")
        plan.update(state="succeeded", result_projection=build_plan_result_projection(projection_kind="oa_history_report",
            operation_id="operation-fixture", step=plan["steps"][-1], result=projected))
        response = plan_result_response(plan)
        self.assertLessEqual(response_chars(response), 12000)
        reference = response["plan"]["resultProjection"]["result"]
        self.assertNotIn("progress", reference)
        self.assertIn("progress", reference["deferred_fields"])
        self.assertEqual(plan["result_projection"]["result"]["progress"], report["progress"])


if __name__ == "__main__":
    unittest.main()
