from datetime import date
import unittest
from unittest.mock import Mock, patch

from agentbridge.adapters.seeyon_history_query import read_filtered_history, _HISTORY_QUERY_PAGE_SCRIPT
from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter, build_central_capability_registry


def rows(count, *, start=0, day="2026-09-01", basis="processed_at"):
    return [{"affair_id": str(i), "title": f"Request {i}", "date": f"{day} 15:32",
             "date_basis": basis} for i in range(start, start + count)]


def payload(items, *, total=None, page=1):
    total = len(items) if total is None else total
    return {"items": items, "total": total, "page": page, "pages": (total + 49) // 50, "size": 50}


class HistoryQueryTests(unittest.TestCase):
    def query(self, *responses, collection="done", start="2026-09-01", end="2026-09-01"):
        self.page = Mock()
        self.page.evaluate.side_effect = responses
        return read_filtered_history(
            self.page, collection=collection,
            start_date=date.fromisoformat(start) if start else None,
            end_date=date.fromisoformat(end) if end else None,
        )

    def test_filters_and_returned_date_field_have_distinct_names(self):
        result = self.query(payload(rows(2)))
        args = self.page.evaluate.call_args.args[1]
        self.assertEqual(args["filters"]["dealDate"], "2026-09-01#2026-09-01")
        self.assertEqual(args["dateField"], "dealTime")
        self.assertEqual(args["dateBasis"], "processed_at")
        self.assertTrue(result["query_evidence"]["complete"])
        self.assertIn("row[dateField]", _HISTORY_QUERY_PAGE_SCRIPT)

    def test_sent_and_open_bounds_use_server_date_filter(self):
        for start, end in ((None, "2026-09-01"), ("2026-09-01", None)):
            result = self.query(payload(rows(1, basis="initiated_at")), collection="sent", start=start, end=end)
            args = self.page.evaluate.call_args.args[1]
            self.assertEqual(args["filters"]["createDate"], f"{start or ''}#{end or ''}")
            self.assertEqual(args["dateField"], "startDate")
            self.assertTrue(result["query_evidence"]["complete"])

    def test_filtered_pages_are_fully_read(self):
        result = self.query(payload(rows(50), total=52), payload(rows(2, start=50), total=52, page=2))
        self.assertEqual(len(result["items"]), 52)
        evidence = result["query_evidence"]
        self.assertEqual((evidence["sourceQueryTotal"], evidence["sourceQueryPages"], evidence["pagesFetched"]), (52, 2, 2))
        self.assertTrue(evidence["complete"])
        self.assertEqual(self.page.evaluate.call_args.args[1]["pageNumber"], 2)

    def test_zero_filtered_rows_is_complete(self):
        result = self.query(payload([]))
        self.assertTrue(result["query_evidence"]["complete"])
        self.assertEqual(result["total"], 0)

    def test_real_thousand_row_boundary_requires_complete_source(self):
        for total in (1000, 1001):
            with self.subTest(total=total):
                responses = [payload(rows(50, start=i * 50), total=total, page=i + 1)
                             for i in range(20)]
                with patch("agentbridge.adapters.seeyon_history_query.time.monotonic", return_value=0):
                    result = self.query(*responses)
                self.assertEqual(len(result["items"]), 1000)
                self.assertEqual(self.page.evaluate.call_count, 20)
                self.assertEqual(result["query_evidence"]["complete"], total == 1000)
                if total == 1001:
                    self.assertEqual(result["query_evidence"]["completionReason"],
                                     "server_query_scan_budget_reached")

    def test_source_changes_duplicates_and_page_failures_stay_partial(self):
        first = payload(rows(50), total=52)
        cases = [
            (payload(rows(2, start=50), total=53, page=2), "server_query_changed_during_pagination"),
            (payload(rows(2), total=52, page=2), "server_query_duplicate_rows"),
            (RuntimeError("page unavailable"), "server_query_page_failed"),
            (payload(rows(1, start=50), total=52, page=2), "server_query_page_size_mismatch"),
        ]
        for second, reason in cases:
            with self.subTest(reason=reason):
                result = self.query(first, second)
                self.assertFalse(result["query_evidence"]["complete"])
                self.assertEqual(result["query_evidence"]["completionReason"], reason)

    def test_ignored_filter_missing_date_and_bad_metadata_never_prove_complete(self):
        for data, reason in (
            (payload(rows(1, day="2026-08-31")), "server_query_filter_not_honored"),
            (payload(rows(1, day="")), "server_query_date_unproven"),
            (payload(rows(1, basis="initiated_at")), "server_query_date_unproven"),
            ({**payload([]), "total": None}, "server_query_metadata_invalid"),
            ({**payload([]), "total": False}, "server_query_metadata_invalid"),
        ):
            with self.subTest(reason=reason):
                result = self.query(data)
                self.assertFalse(result["query_evidence"]["complete"])
                self.assertEqual(result["query_evidence"]["completionReason"], reason)

    def test_scan_and_time_budgets_stop_filtered_pages(self):
        with patch("agentbridge.adapters.seeyon_history_query.HISTORY_QUERY_MAX_ROWS", 50):
            result = self.query(payload(rows(50), total=60))
        self.assertEqual(result["query_evidence"]["completionReason"], "server_query_scan_budget_reached")
        with patch("agentbridge.adapters.seeyon_history_query.time.monotonic", side_effect=[0, 21]):
            result = self.query()
        self.assertEqual(result["query_evidence"]["completionReason"], "server_query_time_budget_reached")

    def test_first_page_error_never_falls_back_to_unfiltered_scan(self):
        with self.assertRaises(RuntimeError):
            self.query(RuntimeError("query rejected"))
        self.assertEqual(self.page.evaluate.call_count, 1)

    def test_output_limit_keeps_query_complete_but_output_partial(self):
        parsed = self.query(payload(rows(2)))
        adapter = SeeyonCentralAdapter(base_url="https://oa.example.test/seeyon/")
        with patch.object(adapter, "_fetch_workflow_collection", return_value=parsed):
            result = adapter.list_workflows(Mock(), collection="done", arguments={
                "start_date": "2026-09-01", "end_date": "2026-09-01", "limit": 1,
            })
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["coverage"]["completionReason"], "result_limit_truncated")
        self.assertTrue(result["coverage"]["serverFilterApplied"])

    def test_ordered_unfiltered_page_is_not_complete_range(self):
        adapter = SeeyonCentralAdapter(base_url="https://oa.example.test/seeyon/")
        with patch.object(adapter, "_fetch_workflow_collection", return_value=payload(rows(2), total=100)):
            result = adapter.list_workflows(Mock(), collection="done", arguments={
                "start_date": "2026-09-01", "end_date": "2026-09-01",
            })
        self.assertEqual(result["coverage"]["status"], "partial")

    def test_query_contract_is_discoverable_on_atomic_capabilities(self):
        registry = build_central_capability_registry()
        for collection, basis in (("done", "processed_at"), ("sent", "initiated_at")):
            spec = registry.get(f"oa.workflow.{collection}.list")
            contract = spec.input_schema["x-agentbridge-query"]
            self.assertEqual(contract["dateBasis"], basis)
            self.assertEqual(contract["filters"]["dateRange"]["execution"], "server")
            self.assertEqual(spec.input_schema["properties"]["limit"]["maximum"], 1000)


if __name__ == "__main__":
    unittest.main()
