from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import ssl
import time
from typing import Any, Callable, Mapping

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from .recovery_policy import transport_recovery_strategy, transport_retry_allowed


TRANSIENT_TRANSPORT_CODES = frozenset(
    {
        "CONNECTERROR",
        "CONNECTTIMEOUT",
        "ECONNRESET",
        "ETIMEDOUT",
        "MCP_TIMEOUT",
        "NETWORKERROR",
        "POOLTIMEOUT",
        "READERROR",
        "READTIMEOUT",
        "REMOTE_PROTOCOL_ERROR",
        "WRITEERROR",
        "WRITETIMEOUT",
    }
)
SAFE_RECOVERY_DELAYS = (0.5, 2.0)
MAXIMUM_RECOVERY_SECONDS = 5.0


@dataclass(frozen=True)
class McpCallResult:
    content: list[dict[str, Any]]
    structured: dict[str, Any]
    private_meta: dict[str, Any]
    is_error: bool
    attempts: int = 1

    def payload(self) -> dict[str, Any]:
        if self.structured:
            return self.structured
        for item in self.content:
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        return {}


@dataclass
class _OwnedMcpSession:
    token_hash: str
    session: Any
    stop: asyncio.Event
    task: asyncio.Task[None]
    created_at: float
    last_used: float

    async def close(self) -> None:
        self.stop.set()
        if not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            except BaseException:
                pass
        else:
            await asyncio.gather(self.task, return_exceptions=True)


class AgentBridgeMcpClient:
    def __init__(
        self,
        *,
        mcp_url: str,
        token_provider: Callable[[], str],
        ca_bundle: str | None = None,
        allow_insecure_tls: bool = False,
        private_transport: bool = False,
        request_timeout_seconds: float = 150,
        idle_ttl_seconds: float = 120.0,
        max_age_seconds: float = 300.0,
    ) -> None:
        self.mcp_url = _http_url(mcp_url)
        self.token_provider = token_provider
        self.ca_bundle = ca_bundle
        self.private_transport = private_transport
        self.allow_insecure_tls = bool(allow_insecure_tls)
        self.request_timeout_seconds = max(
            5.0,
            min(float(request_timeout_seconds), 600.0),
        )
        self.idle_ttl_seconds = max(float(idle_ttl_seconds), 1.0)
        self.max_age_seconds = max(float(max_age_seconds), 1.0)
        self._lane_locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, _OwnedMcpSession] = {}

    async def close(self) -> None:
        entries = list(self._sessions.values())
        self._sessions.clear()
        self._lane_locks.clear()
        for entry in entries:
            await entry.close()

    def _lane_for_call(self, arguments: Mapping[str, Any]) -> str:
        raw_wait = arguments.get("wait_seconds")
        if isinstance(raw_wait, (int, float)) and not isinstance(raw_wait, bool) and raw_wait > 0:
            return "long_poll"
        return "default"

    def _is_fresh(self, entry: _OwnedMcpSession, token_hash: str, now: float) -> bool:
        return (
            not entry.task.done()
            and entry.token_hash == token_hash
            and (now - entry.last_used) < self.idle_ttl_seconds
            and (now - entry.created_at) < self.max_age_seconds
        )

    async def _open_session(self, token: str, token_hash: str, lane: str) -> _OwnedMcpSession:
        headers = {"Authorization": f"Bearer {token}"}
        factory = self._httpx_factory()
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[Any] = loop.create_future()
        stop = asyncio.Event()

        async def owner() -> None:
            try:
                async with streamablehttp_client(
                    self.mcp_url,
                    headers=headers,
                    timeout=self.request_timeout_seconds,
                    sse_read_timeout=max(self.request_timeout_seconds, 300),
                    httpx_client_factory=factory,
                ) as (read_stream, write_stream, _session_id):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        if not ready.done():
                            ready.set_result(session)
                        await stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)

        task = asyncio.create_task(owner(), name=f"reference-host-mcp:{lane}")
        try:
            session = await ready
        except BaseException:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            raise
        now = time.monotonic()
        return _OwnedMcpSession(
            token_hash=token_hash,
            session=session,
            stop=stop,
            task=task,
            created_at=now,
            last_used=now,
        )

    async def _with_session(self, lane: str, action: Callable[[Any], Any]) -> Any:
        token = self.token_provider()
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        lock = self._lane_locks.setdefault(lane, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            entry = self._sessions.get(lane)
            if entry is not None and not self._is_fresh(entry, token_hash, now):
                self._sessions.pop(lane, None)
                await entry.close()
                entry = None
            if entry is None:
                entry = await self._open_session(token, token_hash, lane)
                self._sessions[lane] = entry
            try:
                outcome = await action(entry.session)
            except BaseException:
                popped = self._sessions.pop(lane, None)
                if popped is not None:
                    await popped.close()
                raise
            else:
                entry.last_used = time.monotonic()
                return outcome

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        meta: Mapping[str, Any] | None = None,
        recovery_class: str = "unsafe",
    ) -> McpCallResult:
        recovery_class = str(recovery_class or "unsafe").lower()
        if recovery_class not in {"read", "prepare", "unsafe"}:
            raise ValueError("MCP recovery class is invalid")
        maximum_attempts = 3 if transport_recovery_strategy(recovery_class).startswith("bounded_retry") else 1
        attempt = 0
        recovery_deadline: float | None = None
        while True:
            attempt += 1
            try:
                operation = self._call_tool_once(
                    name,
                    dict(arguments or {}),
                    meta=dict(meta or {}),
                )
                if recovery_deadline is None:
                    result = await operation
                else:
                    remaining = recovery_deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError("bounded MCP transport recovery expired")
                    result = await asyncio.wait_for(operation, timeout=remaining)
                return McpCallResult(
                    content=result.content,
                    structured=result.structured,
                    private_meta=result.private_meta,
                    is_error=result.is_error,
                    attempts=attempt,
                )
            except Exception as exc:
                if not transport_retry_allowed(
                    retryable=is_transient_transport_error(exc),
                    attempt=attempt, maximum_attempts=maximum_attempts,
                ):
                    raise
                if recovery_deadline is None:
                    recovery_deadline = (
                        asyncio.get_running_loop().time()
                        + MAXIMUM_RECOVERY_SECONDS
                    )
                delay = SAFE_RECOVERY_DELAYS[attempt - 1]
                if not transport_retry_allowed(
                    retryable=True, attempt=attempt, maximum_attempts=maximum_attempts,
                    delay_ms=delay * 1000,
                    remaining_ms=(recovery_deadline - asyncio.get_running_loop().time()) * 1000,
                ):
                    raise
                await asyncio.sleep(delay)

    async def list_tools(self) -> list[dict[str, Any]]:
        response = await self._with_session("default", lambda session: session.list_tools())
        return [
            tool.model_dump(by_alias=True, exclude_none=True)
            for tool in response.tools
        ]

    async def _call_tool_once(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any],
    ) -> McpCallResult:
        lane = self._lane_for_call(arguments)
        result = await self._with_session(
            lane,
            lambda session: session.call_tool(
                name,
                arguments,
                read_timeout_seconds=timedelta(
                    seconds=self.request_timeout_seconds
                ),
                meta=meta,
            ),
        )
        dumped = result.model_dump(by_alias=True, exclude_none=True)
        content = [
            item.model_dump(by_alias=True, exclude_none=True)
            if hasattr(item, "model_dump")
            else dict(item)
            for item in result.content
        ]
        structured = dumped.get("structuredContent")
        private_meta = dumped.get("_meta")
        return McpCallResult(
            content=content,
            structured=(structured if isinstance(structured, dict) else {}),
            private_meta=(private_meta if isinstance(private_meta, dict) else {}),
            is_error=bool(result.isError),
        )

    def _httpx_factory(self):
        verify: ssl.SSLContext | str | bool
        if self.allow_insecure_tls:
            verify = False
        elif self.ca_bundle:
            verify = self.ca_bundle
        else:
            verify = True

        def create_client(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                headers=headers,
                timeout=timeout or httpx.Timeout(self.request_timeout_seconds),
                limits=httpx.Limits(keepalive_expiry=3.5),
                auth=auth,
                verify=verify,
                follow_redirects=not self.private_transport,
                trust_env=not self.private_transport,
            )

        return create_client


def recovery_class_for_tool(tool: Mapping[str, Any]) -> str:
    name = str(tool.get("name") or "")
    annotations = tool.get("annotations")
    if isinstance(annotations, Mapping) and annotations.get("readOnlyHint") is True:
        return "read"
    if name.endswith("_prepare"):
        return "prepare"
    return "unsafe"


def is_transient_transport_error(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.WriteError,
            httpx.WriteTimeout,
        ),
    ):
        return True
    code = str(
        getattr(exc, "transport_code", None)
        or getattr(exc, "code", None)
        or exc.__class__.__name__
    ).upper()
    normalized = "".join(
        character if character.isalnum() else "_" for character in code
    ).strip("_")
    return normalized in TRANSIENT_TRANSPORT_CODES


def _http_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("AgentBridge MCP URL must use HTTP or HTTPS")
    return normalized
