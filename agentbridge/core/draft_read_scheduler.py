"""Bounded, process-local scheduling for authenticated read-only draft checks.

The execution session mutex remains authoritative. A queued check consumes no
execution slot until it can acquire that mutex without waiting. No decisions or
business writes run here, and completed reads are never cached for later calls.
"""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition
from time import monotonic


class DraftReadBusy(RuntimeError):
    pass


class SharedDraftReadFailure(RuntimeError):
    def __init__(self, cause):
        self.cause = cause


@dataclass(eq=False)
class _QueuedRead:
    sequence: int
    session_key: tuple
    mutex: object
    priority: bool


class DraftReadScheduler:
    def __init__(self, *, concurrency=2, max_pending=32, max_followers=64, wait_timeout=30.0):
        self.concurrency = concurrency
        self.max_pending = max_pending
        self.max_followers = max_followers
        self.wait_timeout = wait_timeout
        self._condition = Condition()
        self._pending = []
        self._active_sessions = set()
        self._last_served = {}
        self._sequence = 0
        self._priority_streak = 0
        self._flights = {}
        self._followers = 0

    def _next(self):
        heads = {}
        for entry in self._pending:
            if entry.session_key in self._active_sessions:
                continue
            lane = (entry.session_key, entry.priority)
            previous = heads.get(lane)
            if previous is None or entry.sequence < previous.sequence:
                heads[lane] = entry
        available = [entry for entry in heads.values() if not entry.mutex.locked()]
        priority = [entry for entry in available if entry.priority]
        ordinary = [entry for entry in available if not entry.priority]
        # Final rechecks normally go first; two consecutive priority admissions
        # still leave a turn for an ordinary check when both classes are ready.
        choices = priority if priority and (self._priority_streak < 2 or not ordinary) else ordinary
        return min(choices, key=lambda entry: (self._last_served.get(entry.session_key, -1), entry.sequence), default=None)

    @contextmanager
    def slot(self, session_key, mutex, *, priority=False):
        deadline = monotonic() + self.wait_timeout
        acquired = False
        with self._condition:
            if len(self._pending) >= self.max_pending:
                raise DraftReadBusy("Draft read queue is full")
            self._sequence += 1
            entry = _QueuedRead(self._sequence, session_key, mutex, bool(priority))
            if (session_key not in self._active_sessions
                    and not any(pending.session_key == session_key for pending in self._pending)):
                self._last_served[session_key] = self._sequence
            self._pending.append(entry)
            try:
                while True:
                    if (len(self._active_sessions) < self.concurrency and self._next() is entry
                            and mutex.acquire(blocking=False)):
                        acquired = True
                        self._pending.remove(entry)
                        self._active_sessions.add(session_key)
                        self._priority_streak = self._priority_streak + 1 if priority else 0
                        self._condition.notify_all()
                        break
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise DraftReadBusy("Draft read queue wait expired")
                    # Ordinary business execution uses the same mutex but does
                    # not join this queue. Poll only its availability, never hold
                    # a global slot or a database transaction while waiting.
                    self._condition.wait(min(remaining, 0.02))
            finally:
                if not acquired:
                    self._pending.remove(entry)
                    self._forget_idle_session(session_key)
                    self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                mutex.release()
                self._active_sessions.remove(session_key)
                if any(entry.session_key == session_key for entry in self._pending):
                    self._sequence += 1
                    self._last_served[session_key] = self._sequence
                else:
                    self._forget_idle_session(session_key)
                self._condition.notify_all()

    def _forget_idle_session(self, session_key):
        if (session_key not in self._active_sessions
                and not any(entry.session_key == session_key for entry in self._pending)):
            self._last_served.pop(session_key, None)

    def singleflight(self, key, run):
        """Share only an in-flight preview; callers rebuild their own views."""
        with self._condition:
            future = self._flights.get(key)
            leader = future is None
            if leader:
                if len(self._flights) >= self.max_pending:
                    raise DraftReadBusy("Draft preview flight limit reached")
                future = Future()
                self._flights[key] = future
            else:
                if self._followers >= self.max_followers:
                    raise DraftReadBusy("Draft preview follower limit reached")
                self._followers += 1
        if not leader:
            try:
                return future.result(timeout=self.wait_timeout), True
            except TimeoutError as exc:
                raise DraftReadBusy("Draft preview wait expired") from exc
            except Exception as exc:
                raise SharedDraftReadFailure(exc) from exc
            finally:
                with self._condition:
                    self._followers -= 1
        try:
            result = run()
            future.set_result(result)
            return result, False
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._condition:
                del self._flights[key]

    def status(self):
        with self._condition:
            return {"active": len(self._active_sessions), "queued": len(self._pending),
                    "flights": len(self._flights), "followers": self._followers}


DRAFT_READS = DraftReadScheduler()
