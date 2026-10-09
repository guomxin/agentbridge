from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any


class HostControlDiagnostics:
    """In-memory, non-sensitive latency diagnostics for host coordination calls."""

    def __init__(self, *, slow_after_ms: int = 1000, recent_limit: int = 100) -> None:
        self.slow_after_ms = max(int(slow_after_ms), 1)
        self._lock = Lock()
        self._operations: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "call_count": 0,
                "slow_count": 0,
                "error_count": 0,
                "total_elapsed_ms": 0,
                "max_elapsed_ms": 0,
                "last_elapsed_ms": 0,
                "last_called_at": None,
            }
        )
        self._recent_slow_calls: deque[dict[str, Any]] = deque(
            maxlen=max(int(recent_limit), 1)
        )

    def record(
        self,
        *,
        operation_name: str,
        user_subject: str,
        elapsed_ms: int,
        error_code: str | None = None,
    ) -> None:
        operation_name = str(operation_name or "unknown")[:160]
        user_subject = str(user_subject or "unknown")[:256]
        elapsed_ms = max(int(elapsed_ms), 0)
        called_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            metrics = self._operations[operation_name]
            metrics["call_count"] += 1
            metrics["total_elapsed_ms"] += elapsed_ms
            metrics["max_elapsed_ms"] = max(metrics["max_elapsed_ms"], elapsed_ms)
            metrics["last_elapsed_ms"] = elapsed_ms
            metrics["last_called_at"] = called_at
            if error_code:
                metrics["error_count"] += 1
            if elapsed_ms >= self.slow_after_ms:
                metrics["slow_count"] += 1
                self._recent_slow_calls.append(
                    {
                        "operation_name": operation_name,
                        "user_subject": user_subject,
                        "elapsed_ms": elapsed_ms,
                        "error_code": str(error_code)[:120] if error_code else None,
                        "called_at": called_at,
                    }
                )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            operations = []
            for operation_name, metrics in sorted(self._operations.items()):
                calls = metrics["call_count"]
                operations.append(
                    {
                        "operation_name": operation_name,
                        "call_count": calls,
                        "slow_count": metrics["slow_count"],
                        "error_count": metrics["error_count"],
                        "average_elapsed_ms": (
                            round(metrics["total_elapsed_ms"] / calls)
                            if calls
                            else 0
                        ),
                        "max_elapsed_ms": metrics["max_elapsed_ms"],
                        "last_elapsed_ms": metrics["last_elapsed_ms"],
                        "last_called_at": metrics["last_called_at"],
                    }
                )
            recent = list(reversed(self._recent_slow_calls))
        return {
            "slow_after_ms": self.slow_after_ms,
            "operations": operations,
            "recent_slow_calls": recent,
        }

    def reset(self) -> None:
        with self._lock:
            self._operations.clear()
            self._recent_slow_calls.clear()


HOST_CONTROL_DIAGNOSTICS = HostControlDiagnostics()
