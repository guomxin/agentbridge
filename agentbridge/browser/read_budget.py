"""An opt-in monotonic budget for a related group of OA reads.

Outside a scope the worker's existing timeouts are unchanged. Nested recovery
checks inherit the caller's remaining time rather than starting a new minute.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import time


class ReadBudgetExceeded(TimeoutError):
    pass


_READ_DEADLINE: ContextVar[float | None] = ContextVar("oa_read_deadline", default=None)


def current_read_deadline() -> float | None:
    return _READ_DEADLINE.get()


def _effective_deadline(deadline: float | None) -> float | None:
    current = current_read_deadline()
    return min(current, deadline) if current is not None and deadline is not None else (
        current if deadline is None else deadline
    )


@contextmanager
def read_deadline_scope(deadline: float | None, *, inherit: bool = True):
    effective = _effective_deadline(deadline) if inherit else deadline
    token = _READ_DEADLINE.set(effective)
    try:
        check_read_deadline()
        yield effective
    finally:
        _READ_DEADLINE.reset(token)


def check_read_deadline(*, deadline: float | None = None) -> None:
    effective = _effective_deadline(deadline)
    if effective is not None and time.monotonic() >= effective:
        raise ReadBudgetExceeded("OA read budget exhausted")


def remaining_seconds(cap_seconds: float, *, deadline: float | None = None) -> float:
    effective = _effective_deadline(deadline)
    if effective is None:
        return cap_seconds
    remaining = effective - time.monotonic()
    if remaining <= 0:
        raise ReadBudgetExceeded("OA read budget exhausted")
    return min(cap_seconds, remaining)


def remaining_ms(cap_ms: float, *, deadline: float | None = None) -> float:
    return remaining_seconds(cap_ms / 1000, deadline=deadline) * 1000
