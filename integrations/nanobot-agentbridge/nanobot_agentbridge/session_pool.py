"""Per-(subject, endpoint, token, lane) isolated MCP session pool for NanoBot host routes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import time
from typing import Any, Callable

from .private_files import private_text


@dataclass
class _PooledSession:
    token_hash: str
    session: Any
    meta: dict[str, Any]
    level: str
    controlled_prepares: dict[str, Any]
    stop: asyncio.Event
    task: asyncio.Task[None]
    created_at: float
    last_used: float

    async def close(self) -> None:
        self.stop.set()
        if not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=5.0)
            except TimeoutError:
                self.task.cancel()
                await asyncio.wait({self.task}, timeout=1.0)
            except asyncio.CancelledError:
                self.task.cancel()
                raise
        else:
            await asyncio.gather(self.task, return_exceptions=True)


class McpSessionPool:
    """Reuse initialized MCP sessions strictly within one subject/endpoint/token/lane boundary.

    Each pooled session is owned by a single dedicated asyncio Task so anyio cancel scopes
    inside streamable_http_client and ClientSession always enter and exit in the same task.
    """

    def __init__(
        self,
        *,
        open_timeout_seconds: float = 30.0,
        idle_ttl_seconds: float = 120.0,
        max_age_seconds: float = 300.0,
        max_entries: int = 64,
        session_factory: Callable[[dict[str, Any]], Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.open_timeout_seconds = open_timeout_seconds
        self.idle_ttl_seconds = max(float(idle_ttl_seconds), 1.0)
        self.max_age_seconds = max(float(max_age_seconds), 1.0)
        self.max_entries = max(int(max_entries), 1)
        self._session_factory = session_factory
        self._clock = clock or time.monotonic
        self._locks: dict[tuple[Any, ...], asyncio.Lock] = {}
        self._entries: dict[tuple[Any, ...], _PooledSession] = {}

    def _slot_key(self, request: dict[str, Any], lane: str) -> tuple[Any, ...]:
        return (
            str(request["subject"]),
            str(request["endpointKey"]),
            str(request["instance"]),
            str(request["mcpUrl"]),
            str(request.get("caFile") or ""),
            bool(request.get("coordinatorProgram")),
            bool(request.get("trustedCardOrigins")),
            str(lane),
        )

    def _token_hash(self, request: dict[str, Any]) -> str:
        token = private_text(Path(request["tokenFile"])).strip()
        if not token:
            raise ValueError("Empty MCP credential")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _is_fresh(self, entry: _PooledSession, token_hash: str, now: float) -> bool:
        return (
            not entry.task.done()
            and entry.token_hash == token_hash
            and (now - entry.last_used) < self.idle_ttl_seconds
            and (now - entry.created_at) < self.max_age_seconds
        )

    async def _open(self, request: dict[str, Any], token_hash: str) -> _PooledSession:
        factory = self._session_factory
        if factory is None:
            from .worker import authenticated_session as factory

        open_request = {**request, "pooledSession": True}
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[tuple[Any, dict[str, Any], str, dict[str, Any]]] = loop.create_future()
        stop = asyncio.Event()

        async def owner() -> None:
            try:
                async with factory(open_request) as (session, meta, level):
                    prepares = dict(open_request.get("controlledPrepares") or {})
                    if not ready.done():
                        ready.set_result((session, dict(meta), str(level), prepares))
                    await stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)

        task = asyncio.create_task(owner(), name=f"mcp-pool:{request['subject']}:{request['endpointKey']}")
        try:
            async with asyncio.timeout(self.open_timeout_seconds):
                session, meta, level, prepares = await ready
        except BaseException:
            stop.set()
            task.cancel()
            await asyncio.wait({task}, timeout=1.0)
            raise
        now = self._clock()
        return _PooledSession(
            token_hash=token_hash,
            session=session,
            meta=meta,
            level=level,
            controlled_prepares=prepares,
            stop=stop,
            task=task,
            created_at=now,
            last_used=now,
        )

    async def _evict_expired_idle(self, current_key: tuple[Any, ...], now: float) -> None:
        for key, entry in list(self._entries.items()):
            if key == current_key:
                continue
            lock = self._locks.get(key)
            if lock is not None and lock.locked():
                continue
            if (
                entry.task.done()
                or (now - entry.last_used) >= self.idle_ttl_seconds
                or (now - entry.created_at) >= self.max_age_seconds
            ):
                popped = self._entries.pop(key, None)
                if popped is not None:
                    await popped.close()
                if lock is not None and not lock.locked():
                    self._locks.pop(key, None)
        while len(self._entries) >= self.max_entries:
            candidates = [
                (entry.last_used, key)
                for key, entry in self._entries.items()
                if key != current_key and not (self._locks.get(key) and self._locks[key].locked())
            ]
            if not candidates:
                break
            _, oldest_key = min(candidates)
            popped = self._entries.pop(oldest_key, None)
            if popped is not None:
                await popped.close()
            lock = self._locks.get(oldest_key)
            if lock is not None and not lock.locked():
                self._locks.pop(oldest_key, None)

    @asynccontextmanager
    async def session(self, request: dict[str, Any], *, lane: str = "control"):
        token_hash = self._token_hash(request)
        key = self._slot_key(request, lane)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = self._clock()
            await self._evict_expired_idle(key, now)
            entry = self._entries.get(key)
            if entry is not None and not self._is_fresh(entry, token_hash, now):
                self._entries.pop(key, None)
                await entry.close()
                entry = None
            if entry is None:
                entry = await self._open(request, token_hash)
                self._entries[key] = entry
            request["controlledPrepares"] = dict(entry.controlled_prepares)
            try:
                yield entry.session, dict(entry.meta), entry.level
            except BaseException:
                popped = self._entries.pop(key, None)
                if popped is not None:
                    await popped.close()
                raise
            else:
                entry.last_used = self._clock()

    async def close(self) -> None:
        entries = list(self._entries.values())
        self._entries.clear()
        self._locks.clear()
        for entry in entries:
            await entry.close()


@asynccontextmanager
async def open_host_session(host: Any, request: dict[str, Any], *, lane: str = "control"):
    """Use the host's isolated session pool unless unit tests monkeypatched worker.authenticated_session."""
    if getattr(type(host), "private_session", None) is not None:
        async with host.private_session(request) as opened:
            yield opened
        return
    from . import worker

    pool = getattr(host, "mcp_pool", None)
    original = getattr(worker, "_ORIGINAL_AUTHENTICATED_SESSION", None)
    if pool is not None and original is not None and worker.authenticated_session is original:
        async with pool.session(request, lane=lane) as opened:
            yield opened
        return
    async with worker.authenticated_session(request) as opened:
        yield opened
