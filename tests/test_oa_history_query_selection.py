from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentbridge.adapters.base import AdapterLoginRequired
from agentbridge.browser.read_budget import ReadBudgetExceeded
from agentbridge.core.oa_history_queries import OaHistoryQueryError, OaHistoryQueryStore, response_chars
from agentbridge.core.oa_history_query_service import OaHistoryQueryService, QueryLimits, history_query_capability_specs


def history_row(index, *, state="in_progress", initiated="2026-08-15T09:00:00+08:00", processed=None):
    return {"affair_id": str(index), "title": f"事项-{index}", "category": "协同",
        "initiated_at": initiated, "processed_at": processed or initiated,
        "workflow_state": state, "workflow_state_text": state,
        "current_handlers": [] if state == "ended" else ["处理人"],
        "current_nodes": [] if state == "ended" else None,
        "observed_at": "2026-11-11T09:00:00+08:00",
        "unavailable_fields": [] if state == "ended" else ["current_nodes"],
        "_source": {"affair_id": str(index), "summary_id": str(1000 + index),
            "process_id": str(2000 + index), "case_id": str(3000 + index)}}


def workflow_detail(state="in_progress"):
    return {"workflow_state": state, "workflow_state_text": state,
        "current_nodes": [] if state == "ended" else [{"name": "审核", "handlers": ["处理人"]}],
        "current_handlers": [] if state == "ended" else ["处理人"],
        "observed_at": "2026-11-11T09:01:00+08:00", "unavailable_fields": []}


class OaHistoryQuerySelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="oa-history-selection-")
        self.addCleanup(temporary.cleanup)
        self.clock = datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc)
        self.store = OaHistoryQueryStore(Path(temporary.name) / "state.db", clock=lambda: self.clock)
        self.binding = {"user_subject": "owner-a", "session_id": "session-a", "principal_ref": "principal-a"}
        self.rows, self.pages, self.details, self.references = [], [], [], []
        self.page_override = self.detail_override = None
        self.service = OaHistoryQueryService(self.store, read_page=self.read_page, read_nodes=self.read_nodes,
            target_ref_factory=self.target_ref)

    def read_page(self, adapter, worker, *, collection, page_number, temporal):
        self.pages.append(page_number)
        page = {"total": len(self.rows), "pages": (len(self.rows) + 49) // 50,
            "page": page_number, "size": 50,
            "items": deepcopy(self.rows[(page_number - 1) * 50:page_number * 50])}
        return self.page_override(page) if self.page_override else page

    def read_nodes(self, adapter, worker, *, collection, row):
        self.details.append(row["affair_id"])
        return self.detail_override(row) if self.detail_override else workflow_detail()

    def target_ref(self, record, source):
        self.references.append(source["affair_id"])
        return "owned-reference-" + source["affair_id"]

    def query(self, **kwargs):
        return self.service.query(**self.binding, adapter=object(), worker=object(),
            accepted_at="2026-11-11T09:00:00+08:00", **{
                "collection": "sent", "temporal": {"kind": "all"},
                "selection": {"kind": "latest", "count": 1, "workflow_state": "in_progress"}, **kwargs})

    def next(self, cursor, **binding):
        return self.service.next(cursor=cursor, adapter=object(), worker=object(),
            **{**self.binding, **binding})

    def exhaust(self, response):
        responses, items = [response], list(response["items"])
        while responses[-1]["has_more"]:
            self.assertLess(len(responses), 100)
            responses.append(self.next(responses[-1]["next_cursor"]))
            items.extend(responses[-1]["items"])
        return responses, items

    def assert_partial(self, response, reason, ids=()):
        self.assertEqual(response["state"], "partial")
        self.assertFalse(response["selection"]["selection_complete"])
        self.assertEqual(response["selection"]["reason"], reason)
        self.assertEqual(response["coverage"]["completionReason"], reason)
        self.assertEqual([item["affair_id"] for item in response["items"]], list(ids))
        self.assertFalse(response["has_more"])

    def test_224_rows_keep_full_scan_but_verify_and_deliver_only_latest_running(self):
        self.rows = [history_row(i, state="ended", initiated="2026-06-01T09:00:00+08:00") for i in range(224)]
        for index in range(5):
            self.rows[index]["initiated_at"] = f"2026-11-{10-index:02d}T09:00:00+08:00"
        self.rows[5] = history_row(5)
        for index in range(6, 34):
            self.rows[index] = history_row(index, initiated="2026-07-01T09:00:00+08:00")
        response = self.query()
        self.assertEqual(self.pages, [1, 2, 3, 4, 5, 1, 5])
        self.assertEqual(self.details, ["5"])
        self.assertEqual(self.references, ["5"])
        self.assertEqual([item["affair_id"] for item in response["items"]], ["5"])
        self.assertEqual(response["progress"]["scanned_count"], 224)
        self.assertEqual(response["progress"]["matched_count"], 224)
        self.assertEqual(response["progress"]["details_checked"], 1)
        self.assertEqual(response["state"], "complete")
        self.assertEqual(response["coverage"]["details_scope"], "selection_candidates")
        self.assertEqual(response["selection"], {"kind": "latest", "count": 1, "workflow_state": "in_progress",
            "date_basis": "initiated_at", "order": "desc", "scope": "all_matching_history",
            "selected_count": 1, "selection_complete": True, "reason": "complete"})
        self.assertFalse(response["has_more"])
        self.assertLessEqual(response_chars(response), 10000)
        self.assertNotIn("_source", response["items"][0])

    def test_unsorted_cross_page_source_uses_authoritative_timestamp_not_title_or_id(self):
        self.rows = [history_row(i, state="ended") for i in range(101)]
        self.rows[0] = history_row(900, initiated="2026-07-01T09:00:00+08:00")
        self.rows[1] = history_row(901, state="ended")
        self.rows[100] = history_row(1, initiated="2026-10-01T09:00:00+08:00")
        self.rows[0]["title"] = self.rows[100]["title"] = "相同标题"
        result = self.query()
        self.assertEqual([item["affair_id"] for item in result["items"]], ["1"])
        self.assertEqual(self.details, ["1"])

    def test_unknown_newer_state_is_resolved_before_older_candidate(self):
        self.rows = [history_row(1, state="unknown", initiated="2026-10-01T09:00:00+08:00"), history_row(2)]
        self.detail_override = lambda row: workflow_detail("ended" if row["affair_id"] == "1" else "in_progress")
        result = self.query()
        self.assertEqual(self.details, ["1", "2"])
        self.assertEqual([item["affair_id"] for item in result["items"]], ["2"])
        self.assertTrue(result["selection"]["selection_complete"])

    def test_unknown_newer_failure_never_selects_older_row(self):
        self.rows = [history_row(2), history_row(1, state="unknown", initiated="2026-10-01T09:00:00+08:00")]
        self.detail_override = lambda row: {"current_nodes": None}
        self.assert_partial(self.query(), "selection_state_unverified")
        self.assertEqual(self.details, ["1"])

    def test_unknown_resolved_running_can_be_the_selected_row(self):
        self.rows = [history_row(1, state="unknown"), history_row(2, initiated="2026-07-01T09:00:00+08:00")]
        result = self.query()
        self.assertEqual(result["items"][0]["workflow_state"], "in_progress")
        self.assertEqual(self.details, ["1"])

    def test_state_value_marked_unavailable_is_resolved_instead_of_excluded(self):
        self.rows = [history_row(1, state="ended", initiated="2026-10-01T09:00:00+08:00"), history_row(2)]
        self.rows[0]["unavailable_fields"] = ["workflow_state"]
        result = self.query()
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["items"][0]["affair_id"], "1")
        self.assertEqual(self.details, ["1"])

    def test_detail_state_still_marked_unavailable_cannot_exclude_unknown(self):
        self.rows = [history_row(1, state="ended", initiated="2026-10-01T09:00:00+08:00"), history_row(2)]
        self.rows[0]["unavailable_fields"] = ["workflow_state"]
        detail = workflow_detail("ended")
        detail["unavailable_fields"] = ["workflow_state"]
        self.detail_override = lambda row: detail
        self.assert_partial(self.query(), "selection_state_unverified")
        self.assertEqual(self.details, ["1"])

    def test_date_value_marked_unavailable_cannot_establish_order(self):
        self.rows = [history_row(1), history_row(2, initiated="2026-10-01T09:00:00+08:00")]
        self.rows[0]["unavailable_fields"].append("initiated_at")
        self.assert_partial(self.query(), "selection_date_unverified")
        self.assertEqual(self.details, [])

    def test_unverified_date_cannot_be_silently_filtered_out_of_done_initiation_range(self):
        self.rows = [history_row(1, initiated="2026-07-01T09:00:00+08:00")]
        self.rows[0]["unavailable_fields"].append("initiated_at")
        result = self.query(collection="done", temporal={"kind": "previous_day", "date_basis": "initiated_at"})
        self.assert_partial(result, "selection_date_unverified")
        self.assertFalse(result["coverage"]["scan_complete"])

    def test_candidate_finishing_during_detail_read_uses_next_candidate(self):
        self.rows = [history_row(1, initiated="2026-10-01T09:00:00+08:00"), history_row(2)]
        self.detail_override = lambda row: workflow_detail("ended" if row["affair_id"] == "1" else "in_progress")
        result = self.query()
        self.assertEqual([item["affair_id"] for item in result["items"]], ["2"])
        self.assertEqual(self.details, ["1", "2"])

    def test_missing_or_naive_date_on_possible_match_blocks_latest_claim(self):
        for stamp in (None, "bad-date", "2026-10-01T09:00:00", "0001-01-01T00:00:00+01:00"):
            with self.subTest(stamp=stamp):
                self.rows = [history_row(1, initiated=stamp), history_row(2)]
                self.assert_partial(self.query(), "selection_date_unverified")

    def test_undated_unknown_must_be_resolved_and_can_only_be_excluded_if_ended(self):
        self.rows = [history_row(1, state="unknown", initiated=None), history_row(2)]
        self.rows[0]["unavailable_fields"].append("initiated_at")
        self.detail_override = lambda row: workflow_detail("ended" if row["affair_id"] == "1" else "in_progress")
        result = self.query()
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["items"][0]["affair_id"], "2")
        self.detail_override = lambda row: workflow_detail()
        self.assert_partial(self.query(), "selection_date_unverified")

    def test_undated_known_excluded_row_does_not_block_selection(self):
        self.rows = [history_row(1, state="ended", initiated=None), history_row(2)]
        result = self.query()
        self.assertEqual(result["state"], "complete")
        self.assertEqual(self.details, ["2"])

    def test_latest_tie_is_ambiguous_instead_of_broken_using_id(self):
        self.rows = [history_row(2), history_row(1)]
        self.assert_partial(self.query(), "selection_tie")
        self.assertEqual(self.details, ["2", "1"])

    def test_large_equal_time_group_stops_as_soon_as_two_verified_matches_prove_tie(self):
        self.rows = [history_row(i) for i in range(100)]
        result = self.query()
        self.assert_partial(result, "selection_tie")
        self.assertEqual(self.details, ["0", "1"])
        self.assertEqual(result["progress"]["scanned_count"], 100)

    def test_malformed_or_inconsistent_verified_handlers_never_reach_selection(self):
        for handlers in ("处理人", [None], ["其他人员"]):
            with self.subTest(handlers=handlers):
                self.rows = [history_row(1)]
                detail = workflow_detail()
                detail["current_handlers"] = handlers
                self.detail_override = lambda row: detail
                self.assert_partial(self.query(), "selection_detail_unverified")

    def test_invalid_cached_handler_type_requires_exact_detail_verification(self):
        self.rows = [history_row(1, state="ended")]
        self.rows[0]["current_handlers"] = "错误类型"
        self.detail_override = lambda row: workflow_detail("ended")
        result = self.query(selection={"kind": "latest", "count": 1, "workflow_state": "ended"})
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["items"][0]["current_handlers"], [])
        self.assertEqual(self.details, ["1"])

    def test_tie_retains_only_strictly_newer_proven_prefix(self):
        self.rows = [history_row(1, initiated="2026-10-01T09:00:00+08:00"), history_row(2), history_row(3)]
        result = self.query(selection={"kind": "latest", "count": 2, "workflow_state": "in_progress"})
        self.assert_partial(result, "selection_tie", ["1"])
        self.assertEqual(result["selection"]["selected_count"], 1)

    def test_equal_instants_with_different_offsets_are_a_tie(self):
        self.rows = [history_row(1), history_row(2, initiated="2026-08-15T01:00:00Z")]
        self.assert_partial(self.query(), "selection_tie")

    def test_tie_fitting_requested_count_is_complete(self):
        self.rows = [history_row(1), history_row(2)]
        result = self.query(selection={"kind": "latest", "count": 2, "workflow_state": "in_progress"})
        self.assertEqual(result["state"], "complete")
        self.assertEqual(len(result["items"]), 2)

    def test_no_matching_workflow_is_complete_empty_result(self):
        self.rows = [history_row(1, state="ended")]
        result = self.query()
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["selection"]["selected_count"], 0)
        self.assertTrue(result["selection"]["selection_complete"])
        self.assertEqual(self.details, [])

    def test_done_ordering_uses_explicit_temporal_basis(self):
        self.rows = [history_row(1, state="ended", initiated="2026-10-01T09:00:00+08:00", processed="2026-10-02T09:00:00+08:00"),
            history_row(2, state="ended", initiated="2026-09-01T09:00:00+08:00", processed="2026-11-01T09:00:00+08:00")]
        selection = {"kind": "latest", "count": 1, "workflow_state": "ended"}
        by_processed = self.query(collection="done", selection=selection)
        by_initiated = self.query(collection="done", temporal={"kind": "all", "date_basis": "initiated_at"}, selection=selection)
        self.assertEqual(by_processed["items"][0]["affair_id"], "2")
        self.assertEqual(by_processed["selection"]["date_basis"], "processed_at")
        self.assertEqual(by_initiated["items"][0]["affair_id"], "1")

    def test_state_all_selects_latest_any_state_and_resolves_unknown(self):
        self.rows = [history_row(1, state="unknown"), history_row(2, state="ended", initiated="2026-07-01T09:00:00+08:00")]
        result = self.query(selection={"kind": "latest", "count": 1, "workflow_state": "all"})
        self.assertEqual(result["items"][0]["affair_id"], "1")
        self.assertEqual(self.details, ["1"])

    def test_detail_pause_preserves_candidate_and_replay_never_repeats_io(self):
        self.rows = [history_row(1)]
        def deadline(_row):
            raise ReadBudgetExceeded("bounded call expired")
        self.detail_override = deadline
        first = self.query()
        self.assertEqual(first["state"], "details")
        self.assertEqual(first["items"], [])
        self.detail_override = None
        cursor = first["next_cursor"]
        result = self.next(cursor)
        counts = (len(self.pages), len(self.details))
        self.assertEqual(self.next(cursor), result)
        self.assertEqual((len(self.pages), len(self.details)), counts)
        self.assertEqual(self.details, ["1", "1"])

    def test_bounded_detail_continuation_reuses_verified_candidates(self):
        self.rows = [history_row(i, initiated=f"2026-10-{10-i:02d}T09:00:00+08:00") for i in range(7)]
        self.detail_override = lambda row: workflow_detail("in_progress" if row["affair_id"] == "6" else "ended")
        first = self.query()
        self.assertEqual(first["state"], "details")
        self.assertEqual(self.details, ["0", "1", "2", "3", "4"])
        result = self.next(first["next_cursor"])
        self.assertEqual(result["items"][0]["affair_id"], "6")
        self.assertEqual(self.details, [str(i) for i in range(7)])

    def test_selected_set_delivery_pages_respect_limit_and_never_include_other_rows(self):
        self.rows = [history_row(i, state="ended", initiated=f"2026-10-{10-i:02d}T09:00:00+08:00") for i in range(6)]
        responses, items = self.exhaust(self.query(limit=1,
            selection={"kind": "latest", "count": 3, "workflow_state": "ended"}))
        self.assertEqual([item["affair_id"] for item in items], ["0", "1", "2"])
        self.assertEqual(len(responses), 3)
        self.assertTrue(all(response["selection"]["selected_count"] == 3 for response in responses))
        self.assertTrue(all(response_chars(response) <= 10000 for response in responses))

    def test_long_selected_rows_and_target_refs_are_whole_within_original_budget(self):
        self.rows = [history_row(i, state="ended", initiated=f"2026-10-{20-i:02d}T09:00:00+08:00") for i in range(10)]
        title = "长标题😀" * 250
        for row in self.rows:
            row["title"] = title
        responses, items = self.exhaust(self.query(selection={"kind": "latest", "count": 10, "workflow_state": "ended"}))
        self.assertGreater(len(responses), 1)
        self.assertEqual(len(items), 10)
        self.assertTrue(all(item["title"] == title for item in items))
        self.assertTrue(all(item["target_ref"] == "owned-reference-" + item["affair_id"] for item in items))
        self.assertTrue(all(response_chars(response) <= 10000 for response in responses))

    def test_selection_and_temporal_are_frozen_across_continuation(self):
        self.rows = [history_row(1), history_row(2, initiated="2026-07-01T09:00:00+08:00")]
        self.service.limits = QueryLimits(call_details=1)
        self.detail_override = lambda row: workflow_detail("ended" if row["affair_id"] == "1" else "in_progress")
        selection = {"kind": "latest", "count": 1, "workflow_state": "in_progress"}
        first = self.query(selection=selection)
        selection["workflow_state"], selection["count"] = "ended", 50
        result = self.next(first["next_cursor"])
        self.assertEqual(result["items"][0]["affair_id"], "2")
        self.assertEqual(result["selection"]["workflow_state"], "in_progress")
        self.assertEqual(result["selection"]["count"], 1)
        self.assertEqual(result["temporal"], first["temporal"])

    def test_cursor_bindings_and_fixed_ttl_apply_to_selection(self):
        self.rows = [history_row(i, state="ended", initiated=f"2026-10-{10-i:02d}T09:00:00+08:00") for i in range(2)]
        first = self.query(limit=1, selection={"kind": "latest", "count": 2, "workflow_state": "ended"})
        cursor = first["next_cursor"]
        for changed in ({"user_subject": "owner-b"}, {"session_id": "session-b"}, {"principal_ref": "principal-b"}):
            with self.subTest(changed=changed), self.assertRaises(OaHistoryQueryError):
                self.next(cursor, **changed)
        self.clock += timedelta(minutes=31)
        with self.assertRaises(OaHistoryQueryError):
            self.next(cursor)

    def test_source_incomplete_changed_or_duplicate_never_claims_latest(self):
        for mode in ("budget", "anchor", "duplicate"):
            with self.subTest(mode=mode):
                self.rows = [history_row(i, state="ended") for i in range(60)]
                self.rows[-1] = history_row(59)
                self.pages.clear()
                self.service.limits = QueryLimits(total_rows=50) if mode == "budget" else QueryLimits()
                def mutate(page):
                    if mode == "duplicate" and page["page"] == 2:
                        page["items"][0] = deepcopy(self.rows[0])
                    if mode == "anchor" and page["page"] == 1 and len(self.pages) > 2:
                        page["items"][0]["title"] = "changed"
                    return page
                self.page_override = mutate
                result = self.query()
                self.assertEqual(result["state"], "partial")
                self.assertEqual(result["items"], [])
                self.assertFalse(result["selection"]["selection_complete"])
        self.assertEqual(self.details, [])

    def test_detail_total_budget_does_not_skip_unknown_candidate(self):
        self.rows = [history_row(1, state="unknown"), history_row(2, initiated="2026-07-01T09:00:00+08:00")]
        self.service.limits = QueryLimits(total_details=0)
        self.assert_partial(self.query(), "detail_item_budget")
        self.assertEqual(self.details, [])

    def test_authentication_loss_propagates_instead_of_partial_or_older_fallback(self):
        self.rows = [history_row(1)]
        def expired(_row):
            raise AdapterLoginRequired("expired")
        self.detail_override = expired
        with self.assertRaises(AdapterLoginRequired):
            self.query()

    def test_selection_validation_happens_before_source_io(self):
        invalid = ({}, [], {"kind": "first", "count": 1, "workflow_state": "in_progress"},
            {"kind": "latest", "count": True, "workflow_state": "in_progress"},
            {"kind": "latest", "count": 1.0, "workflow_state": "in_progress"},
            {"kind": "latest", "count": 0, "workflow_state": "in_progress"},
            {"kind": "latest", "count": 51, "workflow_state": "in_progress"},
            {"kind": "latest", "count": 1, "workflow_state": "unknown"},
            {"kind": "latest", "count": 1, "workflow_state": []},
            {"kind": "latest", "count": 1, "workflow_state": "all", "url": "https://invalid.example"})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(OaHistoryQueryError):
                self.query(selection=value)
        self.assertEqual(self.pages, [])

    def test_default_mode_preserves_existing_output_and_persisted_query_shape(self):
        self.rows = [history_row(1, state="ended"), history_row(2, state="ended")]
        result = self.query(selection=None)
        self.assertEqual(len(result["items"]), 2)
        self.assertNotIn("selection", result)
        self.assertNotIn("details_scope", result["coverage"])
        record = self.store.get(result["query_id"], **self.binding)
        self.assertNotIn("selection", record["query"])
        self.assertNotIn("selection", record["state_data"])

    def test_capability_selection_is_optional_closed_and_count_bounded(self):
        schema = history_query_capability_specs()[0].input_schema
        self.assertNotIn("selection", schema["required"])
        selection = schema["properties"]["selection"]
        self.assertFalse(selection["additionalProperties"])
        self.assertEqual(selection["properties"]["count"]["maximum"], 50)
        self.assertEqual(set(selection["required"]), {"kind", "count", "workflow_state"})
