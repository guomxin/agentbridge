"""Content-free Workspace timings, isolated from business task lifecycle."""
from datetime import datetime, timedelta, timezone
import logging
import threading
import time
import re

_LOG = logging.getLogger(__name__)


class WorkspacePerformance:
    def __init__(self, governance):
        self.governance = governance
        self._lock = threading.Lock()
        self._seen = set()

    def record_action(self, account, kind, request_key, *, started, started_at, task_id=None, status="succeeded"):
        if kind not in {"query", "selection", "recovery_probe", "recovery_prepare"} or not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{16,128}", request_key):
            return
        try:
            trace, _ = self.governance.ensure_trace(user_subject=account["user_subject"], task_id=task_id,
                request_id=f"workspace-action:{kind}:{request_key}", origin_endpoint_id=account["endpoint_id"],
                host_type="workspace", request_kind="read" if kind in {"query", "recovery_probe"} else "interaction", lock_timeout=.02)
            self.governance.record_timing(trace_id=trace["trace_id"], stage=f"workspace.action.{kind}",
                started_at=started_at, duration_ms=max(0, round((time.perf_counter()-started)*1000)), status=status,
                metadata={"requestId":request_key, "taskId":task_id, "durationClock":"central.monotonic"},
                dedupe_key=f"action:{kind}:{request_key}:{status}")
            with self.governance._connect(timeout=.02) as connection:
                finished_at = datetime.now(timezone.utc).isoformat()
                connection.execute("UPDATE runtime_traces SET status=?,finished_at=?,updated_at=? WHERE trace_id=? AND user_subject=?",
                    (status, finished_at, finished_at, trace["trace_id"], account["user_subject"]))
        except Exception as exc:
            _LOG.debug("Workspace action timing unavailable: %s", type(exc).__name__)

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
