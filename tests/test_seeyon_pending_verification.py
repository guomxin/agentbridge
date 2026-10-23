from copy import deepcopy
from types import SimpleNamespace
import unittest

from agentbridge.adapters.base import AdapterLoginRequired
from agentbridge.adapters.seeyon_pending_verification import (
    VERIFICATION_METHOD,
    verify_pending_action,
)


TARGET = {
    "affair_id": "authorized-affair",
    "summary_id": "authorized-summary",
    "process_id": "authorized-process",
    "template_id": "authorized-template",
    "form_app_id": "authorized-form",
    "form_record_id": "authorized-record",
    "title": "合成待办核验",
}


class FakePage:
    def __init__(self):
        self.identity = deepcopy(TARGET)
        self.affair_matches = True
        self.page_path = "/seeyon/collaboration/collaboration.do"
        self.calls = []

    def evaluate(self, script, affair_id):
        self.calls.append(affair_id)
        return {
            "affair_matches": self.affair_matches,
            "page_path": self.page_path,
            "identity": deepcopy(self.identity),
        }


class FakeAdapter:
    def __init__(self, worker):
        self.worker = worker
        self.pending = {"items": [], "coverage": {"status": "complete", "hasMore": False}}
        # Positive evidence from a partial done page is sufficient; the pending
        # collection must already prove absence independently.
        self.done = {
            "items": [{"affair_id": TARGET["affair_id"], "title": TARGET["title"]}],
            "coverage": {"status": "partial", "hasMore": True},
        }
        self.source = deepcopy(self.done["items"][0])
        self.calls = []
        self.error = None

    def list_workflows(self, worker, *, collection, arguments):
        if worker is not self.worker:
            raise AssertionError("Readback must use the supplied independent worker")
        self.calls.append(collection)
        expected_limit = 100 if collection == "pending" else 1000
        if arguments != {"limit": expected_limit}:
            raise AssertionError("Unexpected query contract")
        if self.error:
            raise self.error
        return deepcopy(getattr(self, collection))

    def resolve_workflow_detail(self, worker, *, collection, affair_id):
        if worker is not self.worker or collection != "done" or affair_id != TARGET["affair_id"]:
            raise AssertionError("Readback must resolve the exact done affair")
        self.calls.append("done_detail")
        return deepcopy(self.source), {"actions": []}


class PendingVerificationTests(unittest.TestCase):
    def setUp(self):
        self.worker = SimpleNamespace(page=FakePage())
        self.adapter = FakeAdapter(self.worker)

    def verify(self):
        return verify_pending_action(self.adapter, self.worker, deepcopy(TARGET))

    def test_complete_absence_and_done_detail_identity_confirm_processing(self):
        observation = self.verify()
        self.assertTrue(observation["confirmed"])
        self.assertEqual(observation["method"], VERIFICATION_METHOD)
        self.assertTrue(observation["pending_absence_confirmed"])
        self.assertTrue(observation["done_identity_confirmed"])
        self.assertEqual(self.adapter.calls, ["pending", "done", "done_detail"])
        # Done detail has no pending submit controls and is still valid evidence.
        self.assertEqual(self.worker.page.calls, [TARGET["affair_id"]])

    def test_complete_done_row_identity_avoids_detail_navigation(self):
        self.adapter.done["items"][0].update(TARGET)
        self.assertTrue(self.verify()["confirmed"])
        self.assertEqual(self.adapter.calls, ["pending", "done"])
        self.assertEqual(self.worker.page.calls, [])

    def test_nested_done_row_identity_is_supported(self):
        self.adapter.done["items"][0]["identity"] = deepcopy(TARGET)
        self.assertTrue(self.verify()["confirmed"])
        self.assertEqual(self.adapter.calls, ["pending", "done"])

    def test_target_still_pending_blocks_even_when_done_contains_same_id(self):
        self.adapter.pending["items"] = [deepcopy(TARGET)]
        self.assertEqual(self.verify()["reason"], "still_pending")
        self.assertEqual(self.adapter.calls, ["pending"])

    def test_target_beyond_first_hundred_cannot_be_inferred_absent(self):
        self.adapter.pending = {
            "items": [{"affair_id": f"other-{index}"} for index in range(100)],
            "total": 101,
            "coverage": {"status": "partial", "hasMore": True},
        }
        observation = self.verify()
        self.assertFalse(observation["confirmed"])
        self.assertEqual(observation["reason"], "pending_coverage_incomplete")
        self.assertEqual(self.adapter.calls, ["pending"])

    def test_missing_or_partial_coverage_never_proves_empty_pending_collection(self):
        for coverage in (None, {}, {"status": "partial"}, {"status": "unknown"}):
            with self.subTest(coverage=coverage):
                self.adapter.pending = {"items": [], "coverage": coverage}
                self.assertEqual(self.verify()["reason"], "pending_coverage_incomplete")

    def test_claimed_complete_coverage_rejects_contradictory_counts_or_more_pages(self):
        for extra in (
            {"total": 101}, {"count": 1}, {"source_count": 1},
            {"matched_count": 1}, {"total": "0"}, {"total": True},
            {"coverage": {"status": "complete", "hasMore": True}},
        ):
            with self.subTest(extra=extra):
                self.adapter.pending = {"items": [], "coverage": {"status": "complete"}, **extra}
                self.assertEqual(self.verify()["reason"], "pending_coverage_inconsistent")

    def test_duplicate_pending_rows_do_not_prove_exhaustion(self):
        self.adapter.pending["items"] = [{"affair_id": "other"}, {"affair_id": "other"}]
        self.assertEqual(self.verify()["reason"], "pending_coverage_inconsistent")

    def test_invalid_pending_payloads_never_count_as_empty_lists(self):
        for pending in (None, {}, {"items": None}, {"items": {}}, {"items": [None]}, {"items": [{}]}):
            with self.subTest(pending=pending):
                self.adapter.pending = pending
                self.assertEqual(self.verify()["reason"], "pending_response_invalid")

    def test_done_title_or_summary_match_without_exact_affair_does_not_confirm(self):
        self.adapter.done["items"] = [{**TARGET, "affair_id": "another-affair"}]
        observation = self.verify()
        self.assertFalse(observation["confirmed"])
        self.assertEqual(observation["reason"], "done_affair_not_found")

    def test_empty_done_collection_does_not_confirm_pending_disappearance(self):
        self.adapter.done = {"items": [], "coverage": {"status": "complete"}}
        self.assertEqual(self.verify()["reason"], "done_affair_not_found")

    def test_invalid_done_collection_does_not_confirm(self):
        self.adapter.done = {}
        self.assertEqual(self.verify()["reason"], "done_response_invalid")

    def test_duplicate_done_affair_is_ambiguous(self):
        self.adapter.done["items"] *= 2
        self.assertEqual(self.verify()["reason"], "done_affair_ambiguous")

    def test_done_page_requires_both_frozen_summary_and_process_identity(self):
        for name in ("summary_id", "process_id"):
            for value in ("another", ""):
                with self.subTest(name=name, value=value):
                    self.worker.page.identity = {**TARGET, name: value}
                    self.assertEqual(self.verify()["reason"], "done_identity_mismatch")

    def test_optional_identity_must_not_contradict_target_when_exposed(self):
        for name in ("template_id", "form_app_id", "form_record_id"):
            with self.subTest(name=name):
                self.worker.page.identity = {**TARGET, name: "another"}
                self.assertEqual(self.verify()["reason"], "done_identity_mismatch")

    def test_done_page_can_omit_non_core_identity_fields(self):
        self.worker.page.identity = {name: TARGET[name] for name in ("summary_id", "process_id")}
        self.assertTrue(self.verify()["confirmed"])

    def test_changed_done_title_is_not_silently_accepted(self):
        self.adapter.done["items"][0]["title"] = "another title"
        self.assertEqual(self.verify()["reason"], "done_title_mismatch")

    def test_detail_must_resolve_same_affair_and_title(self):
        self.adapter.source["affair_id"] = "another"
        self.assertEqual(self.verify()["reason"], "done_detail_affair_mismatch")
        self.adapter.source = {**TARGET, "title": "another"}
        self.assertEqual(self.verify()["reason"], "done_title_mismatch")

    def test_detail_identity_must_come_from_expected_affair_and_page(self):
        self.worker.page.affair_matches = False
        self.assertEqual(self.verify()["reason"], "done_detail_identity_unavailable")
        self.worker.page.affair_matches = True
        self.worker.page.page_path = "/seeyon/login.do"
        self.assertEqual(self.verify()["reason"], "done_detail_identity_unavailable")

    def test_read_errors_propagate_for_shared_polling_and_login_handling(self):
        for error in (RuntimeError("temporary read failure"), AdapterLoginRequired("login required")):
            with self.subTest(error=type(error).__name__):
                self.adapter.error = error
                with self.assertRaises(type(error)) as caught:
                    self.verify()
                self.assertIs(caught.exception, error)

    def test_incomplete_frozen_identity_cannot_start_verification(self):
        for name in ("summary_id", "process_id"):
            with self.subTest(name=name):
                target = {**TARGET, name: ""}
                observation = verify_pending_action(self.adapter, self.worker, target)
                self.assertEqual(observation["reason"], "frozen_identity_incomplete")
                self.assertEqual(self.adapter.calls, [])

    def test_missing_affair_identity_is_rejected_before_reading(self):
        with self.assertRaises(ValueError):
            verify_pending_action(self.adapter, self.worker, {})
        self.assertEqual(self.adapter.calls, [])

    def test_same_observation_snapshots_are_not_read_twice(self):
        self.adapter.done["items"][0]["identity"] = deepcopy(TARGET)
        result = verify_pending_action(self.adapter, self.worker, TARGET,
            pending_snapshot=self.adapter.pending, done_snapshot=self.adapter.done)
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.adapter.calls, [])

    def test_drift_or_budget_failure_invalidates_a_matching_partial_done_row(self):
        self.adapter.done["items"][0]["identity"] = deepcopy(TARGET)
        for reason in ("server_query_changed_during_pagination", "server_query_duplicate_rows",
                       "server_query_time_budget_reached", "server_query_page_failed"):
            with self.subTest(reason=reason):
                self.adapter.done["query_evidence"] = {"completionReason": reason}
                result = self.verify()
                self.assertFalse(result["confirmed"])
                self.assertEqual(result["reason"], "done_scan_unproven")

    def test_controlled_done_row_resolves_directly_without_home_page_reload(self):
        self.adapter.resolve_done_workflow_row_detail = lambda worker, source_item: (source_item, {})
        result = self.verify()
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.adapter.calls, ["pending", "done"])


if __name__ == "__main__":
    unittest.main()
