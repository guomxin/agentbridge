from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import MagicMock, patch

from agentbridge.adapters.base import AdapterLoginRequired
from agentbridge.browser.read_budget import ReadBudgetExceeded
from agentbridge.core.oa_history_queries import OaHistoryQueryError, OaHistoryQueryStore, response_chars
from agentbridge.core.oa_history_query_service import OaHistoryQueryService, QueryLimits


def row(index, *, ended=True, title=None, initiated="2026-11-01", processed="2026-11-02"):
    return {"affair_id": str(index), "title": title or f"业务结算单-{index}", "category": "报销",
        "initiated_at": f"{initiated}T09:00:00+08:00" if initiated else None,
        "processed_at": f"{processed}T10:00:00+08:00" if processed else None,
        "workflow_state": "ended" if ended else "in_progress",
        "workflow_state_text": "已结束" if ended else "流程未结束",
        "current_handlers": [] if ended else ["会计甲"], "current_nodes": [] if ended else None,
        "observed_at": "2026-11-09T09:00:00+08:00", "unavailable_fields": [] if ended else ["current_nodes"],
        "_source": {"affair_id": str(index), "summary_id": f"summary-{index}",
            "process_id": f"process-{index}", "case_id": f"case-{index}", "href": "must-not-be-public"}}


class OaHistoryQueryServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="oa-history-service-")
        self.addCleanup(temporary.cleanup)
        self.store = OaHistoryQueryStore(Path(temporary.name) / "state.db",
            clock=lambda: datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc))
        self.binding = {"user_subject": "user-a", "session_id": "session-a", "principal_ref": "Alice"}
        self.rows, self.pages, self.details = [], [], []
        self.page_override, self.detail_override = None, None
        self.service = OaHistoryQueryService(self.store, read_page=self.read_page, read_nodes=self.read_nodes)
        self.adapter, self.worker = object(), object()

    def read_page(self, adapter, worker, *, collection, page_number, temporal):
        self.pages.append({"page": page_number, "temporal": deepcopy(temporal), "collection": collection})
        result = {"total": len(self.rows), "pages": (len(self.rows) + 49) // 50, "page": page_number,
            "size": 50, "items": deepcopy(self.rows[(page_number - 1) * 50:page_number * 50])}
        return self.page_override(result) if self.page_override else result

    def read_nodes(self, adapter, worker, *, collection, row):
        self.details.append(row["affair_id"])
        if self.detail_override:
            return self.detail_override(row)
        return {"current_nodes": [{"name": "财务审核", "handlers": ["会计甲"]}],
            "current_handlers": ["会计甲"], "workflow_state": "in_progress", "workflow_state_text": "处理中",
            "observed_at": "2026-11-09T09:01:00+08:00", "unavailable_fields": []}

    def query(self, **kwargs):
        return self.service.query(**self.binding, adapter=self.adapter, worker=self.worker,
            accepted_at="2026-11-09T09:00:00+08:00", **{
                "collection": "done", "temporal": {"kind": "all"}, **kwargs})

    def next(self, cursor, **binding):
        return self.service.next(cursor=cursor, adapter=self.adapter, worker=self.worker,
            **{**self.binding, **binding})

    def exhaust(self, first, maximum=100):
        responses, rows = [first], list(first["items"])
        while responses[-1]["has_more"]:
            self.assertLess(len(responses), maximum, "query must make bounded forward progress")
            response = self.next(responses[-1]["next_cursor"])
            responses.append(response)
            rows.extend(response["items"])
        return responses, rows

    def test_full_source_over_1000_and_results_over_200_are_delivered_without_truncation(self):
        self.rows = [row(i, title=f"业务结算单-{i}" if i % 2 else f"其他-{i}") for i in range(1200)]
        initial = self.query(keyword="业务结算单", limit=50)
        self.assertEqual(initial["state"], "scanning")
        self.assertEqual(initial["items"], [])
        self.assertEqual(initial["progress"]["scanned_count"], 1000)
        responses, delivered = self.exhaust(initial)
        self.assertEqual(len(delivered), 600)
        self.assertEqual(len({item["affair_id"] for item in delivered}), 600)
        self.assertEqual(responses[-1]["coverage"]["status"], "complete")
        self.assertEqual(responses[-1]["progress"]["scanned_count"], 1200)
        self.assertEqual(self.details, [])
        for response in responses:
            self.assertLessEqual(len(response["items"]), 50)
            self.assertLessEqual(response_chars(response), 10000)
        self.assertFalse(any("_source" in item or "href" in item for item in delivered))
        self.assertTrue(all(item["workflow_state_text"] == "已结束" for item in delivered))

    def test_details_progresses_five_at_a_time_then_delivers_all_rows(self):
        self.rows = [row(i, ended=False) for i in range(12)]
        first = self.query(limit=7)
        self.assertEqual(first["state"], "details")
        self.assertEqual(first["items"], [])
        self.assertEqual(len(self.details), 5)
        second = self.next(first["next_cursor"])
        self.assertEqual(len(self.details), 10)
        self.assertEqual(second["items"], [])
        responses, delivered = self.exhaust(second)
        self.assertEqual(len(self.details), 12)
        self.assertEqual(len(delivered), 12)
        self.assertTrue(responses[-1]["coverage"]["details_complete"])
        self.assertTrue(all(item["current_nodes"][0]["name"] == "财务审核" for item in delivered))

    def test_detail_failure_is_partial_even_when_source_is_complete(self):
        self.rows = [row(1, ended=False), row(2, ended=False)]
        self.detail_override = lambda item: {"current_nodes": None}
        result = self.query()
        self.assertEqual(result["state"], "partial")
        self.assertTrue(result["coverage"]["scan_complete"])
        self.assertFalse(result["coverage"]["details_complete"])
        self.assertEqual(result["progress"]["details_failed"], 2)
        self.assertEqual(result["items"][0]["current_handlers"], ["会计甲"])
        self.assertIsNone(result["items"][0]["current_nodes"])

    def test_detail_call_budget_pause_retries_the_unfinished_row_without_claiming_it_failed(self):
        self.rows = [row(1, ended=False), row(2, ended=False)]
        def deadline(_row):
            raise ReadBudgetExceeded("current call budget expired")
        self.detail_override = deadline
        first = self.query()
        self.assertEqual(first["state"], "details")
        self.assertEqual(first["progress"]["details_checked"], 0)
        self.assertEqual(first["progress"]["details_failed"], 0)
        self.detail_override = None
        complete = self.next(first["next_cursor"])
        self.assertEqual(complete["state"], "complete")
        self.assertEqual(self.details, ["1", "1", "2"])

    def test_owned_cursor_replay_returns_exact_receipt_without_new_reads(self):
        self.rows = [row(i, ended=False) for i in range(11)]
        first = self.query()
        cursor = first["next_cursor"]
        second = self.next(cursor)
        counts = (len(self.pages), len(self.details))
        self.assertEqual(self.next(cursor), second)
        self.assertEqual((len(self.pages), len(self.details)), counts)
        self.exhaust(second)
        self.assertEqual(self.next(cursor), second)
        for changed in ({"user_subject": "user-b"}, {"session_id": "session-b"}, {"principal_ref": "Bob"}):
            with self.subTest(changed=changed), self.assertRaises(OaHistoryQueryError):
                self.next(cursor, **changed)

    def test_done_initiation_date_filters_all_source_rows_and_freezes_relative_anchor(self):
        self.rows = [row(1, initiated="2026-10-01"), row(2, initiated="2026-11-08")]
        result = self.query(temporal={"kind": "previous_day", "date_basis": "initiated_at"})
        self.assertEqual(result["progress"]["scanned_count"], 2)
        self.assertEqual([item["affair_id"] for item in result["items"]], ["2"])
        self.assertEqual(result["temporal"]["absoluteRange"], {"start": "2026-11-08", "end": "2026-11-08"})
        self.assertTrue(all(call["temporal"] == result["temporal"] for call in self.pages))
        with self.assertRaises(ValueError):
            self.query(collection="sent", temporal={"kind": "all", "date_basis": "processed_at"})

    def test_date_missing_or_server_filter_violation_is_not_silently_dropped(self):
        for initiated, processed, reason in ((None, "2026-11-08", "source_date_unverified"),
                                             ("2026-11-08", "2026-11-07", "source_date_filter_mismatch")):
            with self.subTest(reason=reason):
                self.rows = [row(1, initiated=initiated, processed=processed)]
                basis = "initiated_at" if initiated is None else "processed_at"
                result = self.query(temporal={"kind": "previous_day", "date_basis": basis})
                self.assertEqual(result["coverage"]["completionReason"], reason)
                self.assertEqual(result["state"], "partial")
                self.assertFalse(result["coverage"]["scan_complete"])

    def test_metadata_duplicate_and_anchor_changes_end_with_partial_coverage(self):
        for change in ("metadata", "duplicate", "anchor"):
            with self.subTest(change=change):
                self.rows = [row(i) for i in range(100)]
                self.pages.clear()
                def alter(page):
                    if change == "metadata" and page["page"] == 2:
                        page["total"], page["pages"] = 101, 3
                    if change == "duplicate" and page["page"] == 2:
                        page["items"][0] = deepcopy(self.rows[0])
                    if change == "anchor" and len(self.pages) > 2 and page["page"] == 1:
                        page["items"][0]["title"] = "changed-title"
                    return page
                self.page_override = alter
                result = self.query()
                self.assertEqual(result["state"], "partial")
                self.assertFalse(result["coverage"]["scan_complete"])
                self.assertIn(result["coverage"]["completionReason"], {"source_changed", "source_duplicate_ids"})

    def test_bounded_batches_eventually_finish_final_anchor_validation(self):
        self.rows = [row(i) for i in range(200)]
        self.service.limits = QueryLimits(call_rows=150)
        responses, delivered = self.exhaust(self.query())
        self.assertGreater(len(responses), 1)
        self.assertEqual(len(delivered), 200)
        self.assertEqual(responses[-1]["state"], "complete")

    def test_source_detail_and_execution_budgets_stop_without_claiming_completeness(self):
        self.rows = [row(i) for i in range(200)]
        self.service.limits = QueryLimits(total_rows=150)
        result = self.query()
        self.assertEqual(result["coverage"]["completionReason"], "source_row_budget")
        self.assertEqual(result["state"], "partial")
        self.rows = [row(i, ended=False) for i in range(6)]
        self.service.limits = QueryLimits(total_details=2)
        result = self.query()
        self.assertEqual(result["coverage"]["completionReason"], "detail_item_budget")
        self.assertEqual(result["progress"]["details_checked"], 2)
        self.assertEqual(len(result["items"]), 6)
        start = time.monotonic()
        elapsed = [0.0]
        self.service.monotonic = lambda: start + elapsed[0]
        self.service.limits = QueryLimits(total_seconds=1)
        self.page_override = lambda page: (elapsed.__setitem__(0, elapsed[0] + 2) or page)
        result = self.query()
        self.assertEqual(result["coverage"]["completionReason"], "execution_time_budget")
        self.assertEqual(result["state"], "partial")

    def test_authentication_error_propagates_and_never_becomes_successful_partial(self):
        self.rows = [row(1)]
        def expired(_page):
            raise AdapterLoginRequired("expired")
        self.page_override = expired
        with self.assertRaises(AdapterLoginRequired):
            self.query()

    def test_public_keyword_compatibility_excludes_private_identity_fields(self):
        self.rows = [row(1)]
        self.rows[0]["_keyword_fields"] = {"title": "业务结算单", "category": "采购", "status": "会计甲", "affair_id": "1"}
        self.assertEqual(self.query(keyword="采购")["progress"]["matched_count"], 1)
        self.assertEqual(self.query(keyword="must-not-be-public")["progress"]["matched_count"], 0)

    def test_empty_source_is_complete_and_has_no_spurious_cursor(self):
        result = self.query()
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["items"], [])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_cursor"])


class OaHistoryCentralIntegrationTests(unittest.TestCase):
    def test_repair_preserves_original_request_day_and_rejects_a_different_source_turn(self):
        from tests.authorization_fixtures import authorized_service
        from agentbridge.core.tasks import TaskIntegrityError, TaskNotFound
        from agentbridge.core.oa_query_temporal import normalize_oa_query_temporal
        from agentbridge.core.task_plan_validation import PlanValidationError
        with TemporaryDirectory(prefix="oa-history-repair-") as temporary:
            service = authorized_service(home=temporary, base_url="http://oa.example.test/seeyon/")
            args = dict(user_subject="user-a", token_id="token-a", agent_host="test-host",
                host_task_key="source-turn", endpoint_key="web-source", client_type="web",
                external_subject="user-a", conversation_ref="conversation-a", title="source")
            original_time = "2026-11-09T15:59:00+00:00"
            with patch("agentbridge.core.tasks._utc_now", return_value=original_time):
                original_id = service.ensure_host_task(**args)["task"]["taskId"]
            operation, _ = service.operations.create(user_subject="user-a", capability_name="oa.workflow.done.list",
                capability_version="0.1.0", input_summary={}, input_identity={})
            service.operations.mark_running(operation["operation_id"])
            operation = service.operations.mark_succeeded(operation["operation_id"], {"items": [], "coverage": {"status": "partial"}})
            service.tasks.link_operation(task_id=original_id, user_subject="user-a", operation=operation)
            repair_args = {**args, "host_task_key": "source-turn:plan-repair", "task_scope": "independent",
                "tool_name": "agentbridge_task_plan_prepare", "planning_task_key": "source-turn",
                "planning_repair_source_task_id": original_id}
            with patch("agentbridge.core.tasks._utc_now", return_value="2026-11-09T16:01:00+00:00"):
                repair_id = service.ensure_host_task(**repair_args)["task"]["taskId"]
            repair = service.tasks.get_task(repair_id, user_subject="user-a")
            self.assertNotEqual(repair["created_at"], original_time)
            self.assertEqual(service.task_request_accepted_at(repair), original_time)
            normalized = normalize_oa_query_temporal({"kind": "previous_day"}, accepted_at=service.task_request_accepted_at(repair))
            self.assertEqual(normalized["absoluteRange"], {"start": "2026-11-08", "end": "2026-11-08"})
            self.assertEqual(service.ensure_host_task(**repair_args)["task"]["taskId"], repair_id)
            with patch("agentbridge.core.central_service.compile_temporal_constraints", side_effect=ValueError("probe")) as compile_time:
                with self.assertRaises(PlanValidationError):
                    service.prepare_task_plan(user_subject="user-a", task_id=repair_id, proposal={}, granted_scopes=[])
                self.assertEqual(compile_time.call_args.kwargs["accepted_at"], original_time)
            for changes in ({"planning_task_key": "another-turn"}, {"endpoint_key": "another-endpoint"},
                            {"tool_name": "oa_workflow_history_next"}, {"task_scope": "user_turn"},
                            {"user_subject": "user-b", "token_id": "token-b", "endpoint_key": "other-user", "external_subject": "user-b"}):
                with self.subTest(changes=changes), self.assertRaises((TaskIntegrityError, TaskNotFound)):
                    service.ensure_host_task(**{**repair_args, "host_task_key": "rejected-repair", **changes})

    def test_central_dispatch_uses_verified_session_and_operation_acceptance_time(self):
        from tests.authorization_fixtures import authorized_service
        from agentbridge.core.business_content_identity import principal_binding
        from agentbridge.core.user_grants import CAPABILITY_PERMISSIONS, TOOL_PERMISSIONS
        with TemporaryDirectory(prefix="oa-history-central-") as temporary:
            worker = MagicMock()
            worker.__enter__.return_value = worker
            worker.capture_session_state.return_value = {"cookies": []}
            service = authorized_service(home=temporary, base_url="http://oa.example.test/seeyon/",
                worker_factory=lambda *_: worker)
            session = service.sessions.get_or_create(user_subject="user-a", system_id="oa", expected_principal_ref="Alice")
            session = service.sessions.activate(session["session_id"], observed_principal_ref="Alice")
            service.session_states.save(session["session_id"], {"cookies": []})
            engine = MagicMock()
            engine.query.return_value = {"schema_version": "agentbridge.oa_history_query.v1", "state": "complete", "items": []}
            engine.next.return_value = engine.query.return_value
            service.oa_history_query_service = engine
            result = service.invoke(user_subject="user-a", capability_name="oa.workflow.history.query",
                arguments={"collection": "done", "temporal": {"kind": "all"}})
            self.assertEqual(result["status"], "succeeded")
            arguments = engine.query.call_args.kwargs
            self.assertEqual(arguments["principal_ref"], principal_binding(session))
            self.assertEqual(arguments["accepted_at"], service.operations.get(result["operationId"])["created_at"])
            self.assertEqual(arguments["session_id"], session["session_id"])
            result = service.invoke(user_subject="user-a", capability_name="oa.workflow.history.next", arguments={"cursor": "opaque"})
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(engine.next.call_args.kwargs["cursor"], "opaque")
            for capability, tool in (("oa.workflow.history.query", "oa_workflow_history_query"),
                                     ("oa.workflow.history.next", "oa_workflow_history_next")):
                self.assertEqual(CAPABILITY_PERMISSIONS[capability], "oa.workflow.read")
                self.assertEqual(TOOL_PERMISSIONS[tool], "oa.workflow.read")
