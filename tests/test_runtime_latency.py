"""Low-sensitivity timings must describe time without granting execution."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import perf_counter
import unittest
from unittest.mock import patch

from agentbridge.core.runtime_governance import RuntimeGovernanceStore
from agentbridge.core.capability_runtime import CapabilityRejected
from tests.authorization_fixtures import authorized_service


class RuntimeLatencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RuntimeGovernanceStore(Path(self.temp.name) / "timing.db")
        self.trace, _ = self.store.ensure_trace(user_subject="user-a", request_id="request-one")

    def test_request_trace_can_bind_task_without_losing_original_request(self):
        bound, reused = self.store.ensure_trace(user_subject="user-a", request_id="request-one", task_id="task-one")
        self.assertTrue(reused)
        self.assertEqual(bound["trace_id"], self.trace["trace_id"])
        self.assertEqual(bound["task_id"], "task-one")
        continued, reused = self.store.ensure_trace(user_subject="user-a", request_id="tool-request-two", task_id="task-one")
        self.assertTrue(reused)
        self.assertEqual(continued["request_id"], "request-one")
        replayed, reused = self.store.ensure_trace(user_subject="user-a", request_id="request-one")
        self.assertTrue(reused)
        self.assertEqual(replayed["trace_id"], bound["trace_id"])
        other, reused = self.store.ensure_trace(user_subject="user-b", request_id="request-one", task_id="task-two")
        self.assertFalse(reused)
        self.assertNotEqual(other["trace_id"], bound["trace_id"])

    def test_timing_replay_does_not_reopen_trace_or_advance_effect_boundary(self):
        self.store.update_trace(self.trace["trace_id"], status="succeeded", side_effect_boundary="B5_VERIFIED")
        before = self.store.get_trace(self.trace["trace_id"])
        kwargs = dict(trace_id=self.trace["trace_id"], stage="host.model_call", started_at=before["started_at"],
            duration_ms=124, operation_id="operation-one", dedupe_key="event-one", metadata={"password": "secret-value"})
        first = self.store.record_timing(**kwargs)
        second = self.store.record_timing(**kwargs)
        self.assertIsNotNone(first)
        self.assertEqual(first["span_id"], second["span_id"])
        self.assertEqual(first["duration_ms"], 124)
        self.assertEqual(self.store.get_trace(self.trace["trace_id"]), before)
        detail = self.store.trace_detail(self.trace["trace_id"])
        self.assertEqual(len(detail["spans"]), 1)
        self.assertEqual(detail["spans"][0]["side_effect_boundary"], "B0_NO_EFFECT")
        self.assertNotIn("secret-value", json.dumps(detail))

    def test_process_duration_uses_monotonic_clock_even_if_wall_clock_moves(self):
        with patch("agentbridge.core.runtime_governance.perf_counter", side_effect=[10.0, 10.125]):
            with self.store.timing(trace_id=self.trace["trace_id"], stage="draft.preview"):
                pass
        event = self.store.trace_detail(self.trace["trace_id"])["spans"][0]
        self.assertEqual(event["duration_ms"], 125)
        self.assertEqual(event["metadata"]["durationClock"], "central.monotonic")
        with self.assertRaisesRegex(ValueError, "original failure"):
            with self.store.timing(trace_id=self.trace["trace_id"], stage="draft.recheck"):
                raise ValueError("original failure")
        self.assertEqual(self.store.trace_detail(self.trace["trace_id"])["spans"][-1]["status"], "failed")

    def test_busy_telemetry_does_not_wait_for_business_writer(self):
        with sqlite3.connect(self.store.db_path) as writer:
            writer.execute("BEGIN IMMEDIATE")
            started = perf_counter()
            result = self.store.record_timing(trace_id=self.trace["trace_id"], stage="test.lock_budget",
                started_at=self.trace["started_at"], duration_ms=0)
            elapsed = perf_counter() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, 0.5)
        with sqlite3.connect(self.store.db_path) as writer:
            writer.execute("BEGIN IMMEDIATE")
            started = perf_counter()
            with self.assertRaises(sqlite3.OperationalError):
                self.store.ensure_trace(user_subject="user-a", request_id="new-diagnostic-trace", lock_timeout=0.02)
            elapsed = perf_counter() - started
        self.assertLess(elapsed, 0.5)

    def test_host_events_are_bounded_owned_deduplicated_and_do_not_store_payload(self):
        service = authorized_service(home=Path(self.temp.name) / "service", base_url="http://oa.example.test/seeyon/main.do?method=main")
        now = datetime.now(timezone.utc).isoformat()
        event = {"eventId": "event-one", "stage": "host.model_call", "kind": "span", "observedAt": now,
            "durationMs": 321, "durationClock": "host.wall", "requestId": "request-two", "runId": "run-one",
            "provider": "openai", "model": "test-model", "inputTokens": 10,
            "prompt": "secret business text", "arguments": {"password": "hidden"}}
        registration = {"agentHost": "openclaw", "hostInstanceId": "host-one", "hostVersion": "1.0.0"}
        snapshot = {"status": "healthy", "observedAt": now, "timingEvents": [event]}
        first = service.record_host_runtime_snapshot(user_subject="user-a", registration=registration, snapshot=snapshot)
        second = service.record_host_runtime_snapshot(user_subject="user-a", registration=registration, snapshot=snapshot)
        self.assertEqual(first["timingEvents"], {"recorded": 1, "ignored": 0})
        trace = service.runtime_governance.trace_for_request("request-two", user_subject="user-a")
        detail = service.runtime_governance.trace_detail(trace["trace_id"])
        self.assertEqual(len(detail["spans"]), 1)
        self.assertEqual(detail["spans"][0]["duration_ms"], 321)
        self.assertEqual(detail["spans"][0]["metadata"]["durationClock"], "host.wall")
        self.assertNotIn("secret business text", json.dumps(detail))
        self.assertNotIn("hidden", json.dumps(detail))
        self.assertEqual(second["timingEvents"]["recorded"], 1)
        wrong_task = {**event, "eventId": "foreign-task", "taskId": "missing-task"}
        invalid = [wrong_task, {**event, "eventId": "nan", "durationMs": float("nan")},
            {**event, "eventId": "unknown", "stage": "arbitrary.payload"}]
        counts = service._record_host_timing_events("user-a", registration, invalid)
        self.assertEqual(counts, {"recorded": 0, "ignored": 3})

    def test_first_progress_measures_trace_to_message_instead_of_recording_action(self):
        service = authorized_service(home=Path(self.temp.name) / "service", base_url="http://oa.example.test/seeyon/main.do?method=main")
        common = dict(user_subject="user-a", token_id="token-a", agent_host="openclaw", endpoint_key="workspace:a",
            client_type="web", external_subject="a", conversation_ref="conversation-a")
        task = service.ensure_host_task(**common, host_task_key="task-one", title="Timing test")["task"]
        trace = service.runtime_governance.trace_for_task(task["taskId"], user_subject="user-a")
        started = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        with sqlite3.connect(service.db_path) as connection:
            connection.execute("UPDATE runtime_traces SET started_at=? WHERE trace_id=?", (started, trace["trace_id"]))
        first = service.append_host_timeline_message(**common, message_key="progress-one", role="assistant", text="Synthetic progress", task_id=task["taskId"])
        service.append_host_timeline_message(**common, message_key="progress-two", role="assistant", text="Synthetic later progress", task_id=task["taskId"])
        spans = [item for item in service.runtime_governance.trace_detail(trace["trace_id"])["spans"] if item["stage"] == "host.first_progress"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["started_at"], started)
        self.assertGreaterEqual(spans[0]["duration_ms"], 30_000)
        self.assertLess(spans[0]["duration_ms"], 35_000)
        self.assertEqual(spans[0]["metadata"]["durationClock"], "central.wall")

    def test_session_lock_wait_records_acquisition_and_timeout_without_unlocking_owner(self):
        service = authorized_service(home=Path(self.temp.name) / "service", base_url="http://oa.example.test/seeyon/main.do?method=main")
        trace, _ = service.runtime_governance.ensure_trace(user_subject="user-a", request_id="lock-request")
        with service._session_lock("synthetic-session"):
            with self.assertRaises(CapabilityRejected) as caught:
                with service._session_lock("synthetic-session", wait_seconds=0, trace_id=trace["trace_id"], purpose="draft"):
                    self.fail("a locked session must not be entered")
            self.assertEqual(caught.exception.code, "SESSION_BUSY")
        with patch("agentbridge.core.central_service.perf_counter", side_effect=[1.0, 1.05]):
            with service._session_lock("synthetic-session", trace_id=trace["trace_id"], purpose="adapter"):
                pass
        spans = service.runtime_governance.trace_detail(trace["trace_id"])["spans"]
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["status"], "failed")
        self.assertFalse(spans[0]["metadata"]["acquired"])
        self.assertEqual(spans[1]["duration_ms"], 50)
        self.assertTrue(spans[1]["metadata"]["acquired"])
        self.assertEqual(service.runtime_governance.get_trace(trace["trace_id"])["side_effect_boundary"], "B0_NO_EFFECT")

    def test_legacy_first_progress_bookkeeping_is_not_in_current_latency_slo(self):
        self.store.record_stage_once(trace_id=self.trace["trace_id"], stage="host.first_progress", duration_ms=0)
        result = self.store.refresh_slo_rollups(hours=24)
        self.assertNotIn("host.first_progress.p95_ms", {item["metricKey"] for item in result["metrics"]})


if __name__ == "__main__":
    unittest.main()
