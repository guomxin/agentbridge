from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.oa_history_queries import (
    MAX_DETAIL_ITEMS,
    MAX_EXECUTION_SECONDS,
    MAX_SOURCE_ROWS,
    OaHistoryQueryError,
    OaHistoryQueryStore,
    QUERY_TTL_SECONDS,
    budget_remaining,
    response_chars,
    result_page,
)


class OaHistoryQueryStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="oa-history-query-store-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "agentbridge.db"
        self.now = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
        self.store = OaHistoryQueryStore(self.path, clock=lambda: self.now)
        self.binding = {"user_subject": "user-a", "session_id": "session-a", "principal_ref": "Alice"}
        self.query = {"collection": "done", "keyword": "业务结算单",
                      "temporal": {"scope": "all", "accepted_at": "2026-11-09T09:00:00+08:00"}}
        self.created = self.store.create(**self.binding, query=self.query, state_data={"page": 1, "rows": []})
        self.cursor = self.created["cursor"]
        self.query_id = self.created["record"]["query_id"]

    def assert_error(self, code, callback):
        with self.assertRaises(OaHistoryQueryError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def advance(self, cursor=None, **changes):
        arguments = {"expected_version": 1, "state": "scan", "state_data": {"page": 2, "rows": []},
                     "response": {"state": "scan", "scanned_count": 50}, "scanned_delta": 50, "elapsed_seconds": 1.5}
        arguments.update(changes)
        return self.store.advance(cursor or self.cursor, **self.binding, **arguments)

    def test_normalized_query_and_identity_have_a_fixed_immutable_lifetime(self):
        record = self.created["record"]
        self.query["keyword"] = "changed after create"
        record["query"]["keyword"] = "changed returned copy"
        loaded = self.store.get(self.query_id, **self.binding)
        self.assertEqual(loaded["query"]["keyword"], "业务结算单")
        self.assertEqual(loaded["version"], 1)
        self.assertEqual(datetime.fromisoformat(loaded["expires_at"]) - datetime.fromisoformat(loaded["created_at"]),
                         timedelta(seconds=QUERY_TTL_SECONDS))
        for field, value in (("user_subject", "user-b"), ("session_id", "session-b"),
                             ("principal_ref", "Bob"), ("query_json", "{}"),
                             ("expires_at", "2027-01-01T00:00:00+00:00")):
            with self.subTest(field=field), sqlite3.connect(self.path) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(f"UPDATE oa_history_queries SET {field}=? WHERE query_id=?", (value, self.query_id))
        other = self.store.create(**self.binding, query=loaded["query"])
        self.assertNotEqual(other["record"]["query_id"], self.query_id)
        self.assertNotEqual(other["cursor"], self.cursor)
        self.assertNotIn("Alice", self.cursor)
        self.assertNotIn("业务", self.cursor)

    def test_access_checks_apply_before_receipt_replay(self):
        first = self.advance()
        wrong_owner = {**self.binding, "user_subject": "user-b"}
        self.assert_error("OA_HISTORY_QUERY_NOT_FOUND", lambda: self.store.resolve(self.cursor, **wrong_owner))
        self.assert_error("OA_HISTORY_QUERY_NOT_FOUND", lambda: self.store.get(self.query_id, **wrong_owner))
        for changed in ({"session_id": "renewed-session-same-person"}, {"principal_ref": "Bob"}):
            binding = {**self.binding, **changed}
            self.assert_error("OA_HISTORY_SESSION_CHANGED", lambda: self.store.resolve(self.cursor, **binding))
            self.assert_error("OA_HISTORY_SESSION_CHANGED", lambda: self.store.get(self.query_id, **binding))
            self.assert_error("OA_HISTORY_SESSION_CHANGED", lambda: self.store.advance(self.cursor, **binding,
                expected_version=1, state="scan", state_data={}, response={}))
        self.assertEqual(self.store.resolve(self.cursor, **self.binding)["cached_response"], first)

    def test_advance_and_receipt_are_atomic_and_duplicate_cursor_never_advances_twice(self):
        initial = self.store.resolve(self.cursor, **self.binding)
        self.assertEqual(initial["stage"], "scan")
        self.assertEqual(initial["delivery_offset"], 0)
        self.assertIsNone(initial["cached_response"])
        first = self.advance()
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual(first["replay_cursor"], self.cursor)
        duplicate = self.advance(response={"different": "ignored"}, scanned_delta=999)
        self.assertEqual(json.dumps(first), json.dumps(duplicate))
        loaded = self.store.get(self.query_id, **self.binding)
        self.assertEqual((loaded["version"], loaded["scanned_count"], loaded["execution_seconds"]), (2, 50, 1.5))
        second = self.advance(first["next_cursor"], expected_version=2, state="details",
                              state_data={"rows": [{"affair_id": "a"}]}, response={"state": "details"})
        self.assertEqual(self.store.resolve(second["next_cursor"], **self.binding)["stage"], "details")
        self.assert_error("OA_HISTORY_STATE_INVALID", lambda: self.advance(second["next_cursor"], expected_version=3))
        self.assertEqual(self.store.resolve(self.cursor, **self.binding)["cached_response"], first)
        reopened = OaHistoryQueryStore(self.path, clock=lambda: self.now)
        self.assertEqual(reopened.resolve(self.cursor, **self.binding)["cached_response"], first)

    def test_compare_and_swap_and_oversize_response_leave_no_partial_receipt(self):
        self.assert_error("OA_HISTORY_REVISION_CONFLICT", lambda: self.advance(expected_version=99))
        self.assert_error("OA_HISTORY_RESULT_ENVELOPE_TOO_LARGE", lambda: self.advance(response={"text": "😀" * 10000}))
        self.assertEqual(self.store.get(self.query_id, **self.binding)["version"], 1)
        self.assertIsNone(self.store.resolve(self.cursor, **self.binding)["cached_response"])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM oa_history_query_receipts").fetchone()[0], 1)
        self.assertIsNotNone(self.advance()["next_cursor"])

    def test_simultaneous_commits_share_one_receipt_and_one_progress_update(self):
        def attempt(index):
            return self.advance(response={"winner": index})
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(attempt, range(6)))
        self.assertTrue(all(response == responses[0] for response in responses))
        record = self.store.get(self.query_id, **self.binding)
        self.assertEqual(record["version"], 2)
        self.assertEqual(record["scanned_count"], 50)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM oa_history_query_receipts").fetchone()[0], 2)

    def test_expiry_never_extends_for_progress_or_cached_reads(self):
        original_expiry = self.created["record"]["expires_at"]
        self.now += timedelta(minutes=20)
        response = self.advance()
        self.assertEqual(self.store.get(self.query_id, **self.binding)["expires_at"], original_expiry)
        self.now += timedelta(minutes=9, seconds=59)
        self.assertEqual(self.store.resolve(self.cursor, **self.binding)["cached_response"], response)
        self.now += timedelta(seconds=1)
        self.assert_error("OA_HISTORY_QUERY_EXPIRED", lambda: self.store.resolve(self.cursor, **self.binding))
        self.assert_error("OA_HISTORY_QUERY_EXPIRED", lambda: self.store.resolve(response["next_cursor"], **self.binding))
        self.assert_error("OA_HISTORY_QUERY_EXPIRED", lambda: self.store.get(self.query_id, **self.binding))
        self.assert_error("OA_HISTORY_QUERY_EXPIRED", lambda: self.advance())

    def test_latest_response_recovers_owned_page_and_cursor_without_renewing_access(self):
        self.assertIsNone(self.store.latest_response(self.query_id, **self.binding))
        first = self.advance()
        second = self.advance(first["next_cursor"], expected_version=2, response={"state": "scan", "page": 2})
        self.assertEqual(self.store.latest_response(self.query_id, **self.binding), second)
        self.assert_error("OA_HISTORY_QUERY_NOT_FOUND", lambda: self.store.latest_response(self.query_id,
            **{**self.binding, "user_subject": "user-b"}))
        self.assert_error("OA_HISTORY_SESSION_CHANGED", lambda: self.store.latest_response(self.query_id,
            **{**self.binding, "session_id": "renewed-session"}))
        self.now += timedelta(seconds=QUERY_TTL_SECONDS)
        self.assert_error("OA_HISTORY_QUERY_EXPIRED", lambda: self.store.latest_response(self.query_id, **self.binding))

    def test_source_detail_and_execution_limits_stop_active_queries_as_partial(self):
        cases = (({"scanned_delta": MAX_SOURCE_ROWS}, "source_row_budget"),
                 ({"state": "details", "detail_delta": MAX_DETAIL_ITEMS}, "detail_item_budget"),
                 ({"elapsed_seconds": MAX_EXECUTION_SECONDS}, "execution_time_budget"))
        for changes, reason in cases:
            with self.subTest(reason=reason):
                created = self.store.create(**self.binding, query=self.query)
                response = self.advance(created["cursor"], **changes)
                self.assertEqual(response["state"], "partial")
                self.assertEqual(response["completion_reason"], reason)
                next_step = self.store.resolve(response["next_cursor"], **self.binding)
                self.assertEqual(next_step["stage"], "results")
                self.assertEqual(next_step["delivery_offset"], 0)
                record = next_step["record"]
                self.assertEqual(record["state_data"]["completion_reason"], reason)
                self.assertIn(0, budget_remaining(record).values())
                # Delivery of a partial result must not restart the exhausted budget.
                delivered = self.store.advance(response["next_cursor"], **self.binding,
                    expected_version=record["version"], state="partial", state_data=record["state_data"],
                    response={"items": [], "state": "partial"})
                self.assertIsNone(delivered["next_cursor"])

    def test_completion_at_exact_limits_is_not_reported_as_incomplete(self):
        response = self.advance(state="complete", state_data={"rows": []}, response={"state": "complete"},
                                scanned_delta=MAX_SOURCE_ROWS, detail_delta=MAX_DETAIL_ITEMS,
                                elapsed_seconds=MAX_EXECUTION_SECONDS)
        self.assertEqual(response["state"], "complete")
        self.assertIsNone(response["next_cursor"])
        self.assertEqual(self.store.get(self.query_id, **self.binding)["state"], "complete")

    def test_terminal_results_are_frozen_while_delivery_receipts_can_advance(self):
        rows = [{"affair_id": str(index), "title": "业务结算单"} for index in range(3)]
        state_data = {"rows": rows}
        first = self.advance(state="complete", state_data=state_data,
            response={"state": "complete", "items": rows[:1], "delivery": {"offset": 0, "complete": False}},
            next_stage="results", next_offset=1)
        first_page = result_page(rows, limit=1, envelope={"state": "complete", "query_id": self.query_id})
        response = self.advance(first["next_cursor"], expected_version=2, state="complete", state_data=state_data,
            response=first_page, next_stage="results", next_offset=first_page["next_offset"],
            scanned_delta=0, elapsed_seconds=0)
        self.assertEqual(self.store.resolve(response["next_cursor"], **self.binding)["delivery_offset"], 1)
        self.assertEqual(self.store.resolve(first["next_cursor"], **self.binding)["cached_response"], response)
        self.assertEqual(self.store.latest_response(self.query_id, **self.binding), first)
        for changes in ({"state": "scan"}, {"state_data": {"rows": []}}, {"detail_delta": 1}):
            arguments = {"expected_version": 3, "state": "complete", "state_data": state_data,
                         "response": {}, "scanned_delta": 0, "elapsed_seconds": 0, **changes}
            self.assert_error("OA_HISTORY_STATE_INVALID", lambda: self.advance(response["next_cursor"], **arguments))

    def test_receipt_and_cursor_bindings_are_database_immutable(self):
        self.advance()
        with sqlite3.connect(self.path) as connection:
            for assignment in ("stage='results'", "delivery_offset=50", "version=8", "response_json='{}'"):
                with self.subTest(assignment=assignment), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE oa_history_query_receipts SET " + assignment + " WHERE response_json IS NOT NULL")
        self.assert_error("OA_HISTORY_CURSOR_INVALID", lambda: self.store.resolve("bad", **self.binding))
        self.assert_error("OA_HISTORY_QUERY_NOT_FOUND", lambda: self.store.resolve("ohc_" + secrets.token_urlsafe(32), **self.binding))

    def test_result_pages_count_unicode_and_keep_every_row_and_field(self):
        rows = [{"id": index, "title": "😀" * 70, "node": "审批" * 15} for index in range(17)]
        original = deepcopy(rows)
        envelope = {"query_id": self.query_id, "state": "complete", "notice": "结果" * 20}
        offset, received = 0, []
        while True:
            page = result_page(rows, offset=offset, limit=5, max_chars=1500, envelope=envelope)
            measured = {**page, "next_cursor": "x" * 47 if page["has_more"] else None, "replay_cursor": "x" * 47}
            self.assertLessEqual(response_chars(measured), 1500)
            self.assertEqual(page["returned_count"], len(page["items"]))
            self.assertEqual(page["total"], len(rows))
            self.assertLessEqual(len(page["items"]), 5)
            received.extend(page["items"])
            if not page["has_more"]:
                self.assertIsNone(page["next_offset"])
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual(received, rows)
        self.assertEqual(rows, original)
        self.assertNotIn("items", envelope)

    def test_saved_page_replay_preserves_first_page_and_final_page_without_advancing(self):
        rows = [{"affair_id": "first"}, {"affair_id": "second"}]
        data = {"rows": rows}
        first = self.advance(state="complete", state_data=data,
            response={"state": "complete", "items": rows[:1]}, next_stage="results", next_offset=1)
        self.assertEqual(first["replay_cursor"], self.cursor)
        self.assertEqual(self.store.resolve(first["replay_cursor"], **self.binding)["cached_response"], first)
        final = self.advance(first["next_cursor"], expected_version=2, state="complete", state_data=data,
            response={"state": "complete", "items": rows[1:]}, scanned_delta=0, elapsed_seconds=0)
        self.assertIsNone(final["next_cursor"])
        self.assertEqual(final["replay_cursor"], first["next_cursor"])
        replayed_first = self.store.resolve(first["replay_cursor"], **self.binding)["cached_response"]
        replayed_final = self.store.resolve(final["replay_cursor"], **self.binding)["cached_response"]
        self.assertEqual(replayed_first["items"] + replayed_final["items"], rows)
        self.assertEqual(self.store.get(self.query_id, **self.binding)["version"], 3)
        self.assert_error("OA_HISTORY_SESSION_CHANGED", lambda: self.store.resolve(final["replay_cursor"],
            **{**self.binding, "session_id": "renewed-session"}))

    def test_single_oversized_row_and_invalid_delivery_parameters_do_not_skip_data(self):
        rows = [{"title": "😀" * 1000}, {"title": "second"}]
        self.assert_error("OA_HISTORY_RESULT_ROW_TOO_LARGE", lambda: result_page(rows, max_chars=1000))
        self.assertEqual(rows[0]["title"], "😀" * 1000)
        for arguments in ({"limit": 0}, {"limit": 51}, {"offset": -1}, {"offset": 3}, {"max_chars": 10001}):
            self.assert_error("OA_HISTORY_RESULT_ARGUMENTS_INVALID", lambda: result_page(rows, **arguments))
        empty = result_page([], envelope={"state": "complete"})
        self.assertEqual(empty["items"], [])
        self.assertFalse(empty["has_more"])


if __name__ == "__main__":
    unittest.main()
