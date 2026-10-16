"""Content-free Workspace timings, isolated from business task lifecycle."""
from datetime import datetime, timedelta, timezone
import logging
import threading
import time

_LOG = logging.getLogger(__name__)


class WorkspacePerformance:
    def __init__(self, governance):
        self.governance = governance
        self._lock = threading.Lock()
        self._seen = set()

    def record(self, dispatch, stage, *, started=None, status="succeeded", terminal=False):
        # Retry/refresh observation must not produce duplicate milestones. The
        # database dedupe key also protects process restart and multiple workers.
        key = (dispatch["dispatch_id"], stage)
        with self._lock:
            if key in self._seen:
                return
            if len(self._seen) >= 2048:
                self._seen.clear()
            self._seen.add(key)
        try:
            request_id = "workspace:" + dispatch["idempotency_key"]
            trace = self.governance.trace_for_request(request_id, user_subject=dispatch["user_subject"])
            if trace is None:
                trace, _ = self.governance.ensure_trace(
                    user_subject=dispatch["user_subject"], request_id=request_id,
                    host_type="workspace", host_run_id=dispatch.get("accepted_run_id") or dispatch["idempotency_key"],
                    origin_endpoint_id=dispatch["origin_endpoint_id"], request_kind="read", lock_timeout=.02)
            now = datetime.now(timezone.utc)
            origin = datetime.fromisoformat(dispatch["created_at"].replace("Z", "+00:00"))
            elapsed = max(0, round((now - origin).total_seconds() * 1000))
            duration = elapsed if started is None else max(0, round((time.perf_counter()-started)*1000))
            self.governance.record_timing(
                trace_id=trace["trace_id"], stage=stage, status=status,
                started_at=origin.isoformat() if started is None else (now-timedelta(milliseconds=duration)).isoformat(),
                duration_ms=duration,
                metadata={"dispatchId":dispatch["dispatch_id"], "requestId":dispatch["idempotency_key"],
                          "runId":dispatch.get("accepted_run_id") or dispatch["idempotency_key"],
                          "durationClock":"central.wall" if started is None else "central.monotonic",
                          "measurementKind":"elapsed_since_durable_request" if started is None else "duration"},
                dedupe_key=dispatch["dispatch_id"] + ":" + stage)
            if terminal:
                self.governance.update_trace(trace["trace_id"], status=status, finished=True)
        except Exception as exc:
            # Telemetry cannot fail a request or disclose request contents.
            _LOG.debug("Workspace timing unavailable: %s", type(exc).__name__)
