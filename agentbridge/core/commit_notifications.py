"""Process-local hints for committed ledgers; durable rows remain authoritative.

Writers mark a tracked connection. Only its owner publishes after commit. A
missed hint, another process, or a disabled hub is handled by bounded rechecks.
No business operation is ever executed by this module.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import os
from pathlib import Path
import threading


@dataclass
class _Transaction:
    namespace: str
    changes: set[tuple[str, str]] = field(default_factory=set)


_condition = threading.Condition()
_transactions: dict[int, _Transaction] = {}
_versions: OrderedDict[tuple[str, str, str], int] = OrderedDict()
_waiters: dict[tuple[str, str, str], set[asyncio.Future]] = {}
_sequence = 0
_MAX_KEYS = 8192
_MAX_WAITERS_PER_KEY = 256


def _key(db_path, user_subject, topic):
    return (str(Path(db_path).resolve()), str(user_subject), str(topic))


def track_transaction(connection, db_path):
    with _condition:
        _transactions[id(connection)] = _Transaction(str(Path(db_path).resolve()))


def mark_transaction_change(connection, *, user_subject, topics=("timeline", "notifications")):
    with _condition:
        transaction = _transactions.get(id(connection))
        if transaction is not None:
            transaction.changes.update((str(user_subject), str(topic)) for topic in topics)


def transaction_closed(connection):
    with _condition:
        _transactions.pop(id(connection), None)


def transaction_committed(connection):
    with _condition:
        transaction = _transactions.pop(id(connection), None)
    if transaction is not None:
        for subject, topic in transaction.changes:
            try:
                publish_change(transaction.namespace, user_subject=subject, topic=topic)
            except Exception:
                pass  # Committed decisions remain successful; rechecks recover.


def publish_change(db_path, *, user_subject, topic="timeline"):
    """Wake listeners only after the caller's durable commit has succeeded."""
    global _sequence
    if os.environ.get("AGENTBRIDGE_NOTIFICATION_WAKEUPS", "1").lower() in {"0", "false", "off"}:
        return
    key = _key(db_path, user_subject, topic)
    with _condition:
        _sequence += 1
        version = _sequence
        _versions[key] = version
        _versions.move_to_end(key)
        while len(_versions) > _MAX_KEYS:
            _versions.popitem(last=False)
        listeners = tuple(_waiters.get(key, ()))
        _condition.notify_all()
    for listener in listeners:
        try:
            listener.get_loop().call_soon_threadsafe(_complete_waiter, listener, version)
        except RuntimeError:
            pass  # A closed event loop will unregister its canceled listener.


def _complete_waiter(listener, version):
    if not listener.done():
        listener.set_result(version)


def current_version(db_path, *, user_subject, topic="timeline"):
    with _condition:
        return _versions.get(_key(db_path, user_subject, topic), 0)


def wait_for_change(db_path, *, user_subject, topic="timeline", after_version, timeout=2.0):
    key = _key(db_path, user_subject, topic)
    with _condition:
        return _condition.wait_for(lambda: _versions.get(key, 0) != after_version, timeout=max(0.0, timeout))


async def wait_for_change_async(db_path, *, user_subject, topic="timeline", after_version, timeout=2.0):
    """Wait without occupying a database connection or executor thread."""
    key = _key(db_path, user_subject, topic)
    loop = asyncio.get_running_loop()
    waiter = None
    with _condition:
        if _versions.get(key, 0) != after_version:
            return True
        listeners = _waiters.setdefault(key, set())
        if len(listeners) < _MAX_WAITERS_PER_KEY:
            waiter = loop.create_future()
            listeners.add(waiter)
    if waiter is None:
        await asyncio.sleep(max(0.0, timeout))
        return current_version(db_path, user_subject=user_subject, topic=topic) != after_version
    try:
        await asyncio.wait_for(waiter, timeout=max(0.0, timeout))
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        with _condition:
            listeners = _waiters.get(key)
            if listeners is not None:
                listeners.discard(waiter)
                if not listeners:
                    _waiters.pop(key, None)
