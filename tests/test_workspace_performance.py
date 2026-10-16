from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from agentbridge.core.runtime_governance import RuntimeGovernanceStore
from agentbridge.workspace.performance import WorkspacePerformance
from agentbridge.mcp.central import _request_runtime_context


class WorkspacePerformanceTests(unittest.TestCase):
    def test_duration_dedupe_and_host_terminal_do_not_close_business_trace(self):
        with TemporaryDirectory() as tmp:
            store = RuntimeGovernanceStore(Path(tmp)/"runtime.db")
            business, _ = store.ensure_trace(user_subject="owner", task_id="business-task", request_id="turn-1")
            dispatch = {"dispatch_id":"dispatch-1", "idempotency_key":"turn-1", "user_subject":"owner",
                        "origin_endpoint_id":"endpoint-1", "created_at":(datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat()}
            timing = WorkspacePerformance(store)
            timing.record(dispatch, "workspace.host.accepted")
            timing.record(dispatch, "workspace.host.accepted")
            timing.record(dispatch, "workspace.host.terminal", terminal=True)
            trace = store.trace_for_request("workspace:turn-1", user_subject="owner")
            with store._connect() as db:
                spans = db.execute("SELECT stage,duration_ms FROM runtime_spans WHERE trace_id=?", (trace["trace_id"],)).fetchall()
                original = db.execute("SELECT status,finished_at FROM runtime_traces WHERE trace_id=?", (business["trace_id"],)).fetchone()
            self.assertEqual(len(spans), 2)
            self.assertGreaterEqual(spans[0]["duration_ms"], 1900)
            self.assertEqual(original["status"], "active")
            self.assertIsNone(original["finished_at"])

    def test_telemetry_failure_does_not_reject_dispatch(self):
        class Unavailable:
            def trace_for_request(self, *args, **kwargs): raise RuntimeError("unavailable")
        WorkspacePerformance(Unavailable()).record({"dispatch_id":"d", "idempotency_key":"r", "user_subject":"u"}, "workspace.host.accepted")

    def test_request_and_run_correlation_do_not_change_task_or_lease(self):
        context = SimpleNamespace(request_context=SimpleNamespace(meta={
            "io.agentbridge/timing":{"requestId":"turn-1", "runId":"run-1", "taskId":"untrusted-task", "coordinatorLeaseVersion":999}}))
        result = _request_runtime_context(context)
        self.assertEqual(result["requestId"], "turn-1")
        self.assertEqual(result["hostRunId"], "run-1")
        self.assertIsNone(result["coordinatorLeaseVersion"])


if __name__ == "__main__":unittest.main()
