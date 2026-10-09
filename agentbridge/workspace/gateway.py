from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable, Iterator
from urllib.parse import urlparse


_LOG = logging.getLogger(__name__)

_MAX_GATEWAY_TIMEOUT_MS = 300_000

_PRE_ACCEPT_RETRY_CODES = {
    "GATEWAY_CONNECTION_CLOSED",
    "GATEWAY_CONNECTION_FAILED",
    "GATEWAY_PROCESS_FAILED",
    "GATEWAY_RESPONSE_INVALID",
    "GATEWAY_TIMEOUT",
}
_PRE_ACCEPT_RETRY_STAGES = {"connect", "preflight_abort", "bind"}
_PRE_ACCEPT_RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0)
_POST_ACCEPT_RECONCILE_CODES = {
    "GATEWAY_CONNECTION_CLOSED",
    "GATEWAY_CONNECTION_FAILED",
    "GATEWAY_PROCESS_FAILED",
    "GATEWAY_RESPONSE_INVALID",
}
_POST_ACCEPT_RECONCILE_DELAYS_SECONDS = (0.0, 2.0, 5.0, 10.0, 20.0)


class GatewayRequestError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(message)


class OpenClawGatewayClient:
    def __init__(
        self,
        *,
        url: str,
        token_file: Path | str,
        state_dir: Path | str,
        node_executable: str = "node",
        script_path: Path | str | None = None,
        retry_sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.url = str(url or "").strip()
        self.token_file = Path(token_file)
        self.state_dir = Path(state_dir)
        self.node_executable = node_executable
        self.script_path = Path(script_path or Path(__file__).with_name(
            "gateway_client.mjs"
        ))
        self._retry_sleep = retry_sleep or time.sleep
        if not self.url.startswith(("ws://", "wss://")):
            raise ValueError("OpenClaw Gateway URL must use ws:// or wss://")
        self._diagnostics_lock = threading.Lock()
        self._last_attempt_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error_at: str | None = None
        self._last_error_code: str | None = None

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 30,
    ) -> Any:
        self._record_attempt()
        try:
            environment = self._environment()
        except GatewayRequestError as exc:
            self._record_error(exc.code)
            raise
        payload = {
            "method": str(method),
            "params": params or {},
            "timeoutMs": min(
                max(int(timeout_seconds * 1000), 1_000),
                _MAX_GATEWAY_TIMEOUT_MS,
            ),
        }
        try:
            completed = subprocess.run(
                [
                    self.node_executable,
                    "--experimental-websocket",
                    str(self.script_path),
                ],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=timeout_seconds + 5,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._record_error("GATEWAY_PROCESS_FAILED")
            raise GatewayRequestError(
                "GATEWAY_PROCESS_FAILED",
                "OpenClaw Gateway client process failed.",
            ) from exc
        try:
            result = json.loads(completed.stdout.strip())
        except (json.JSONDecodeError, AttributeError) as exc:
            self._record_error("GATEWAY_RESPONSE_INVALID")
            raise GatewayRequestError(
                "GATEWAY_RESPONSE_INVALID",
                "OpenClaw Gateway returned an invalid response.",
            ) from exc
        if not result.get("ok"):
            error = result.get("error") if isinstance(
                result.get("error"), dict
            ) else {}
            error_code = str(error.get("code") or "GATEWAY_REQUEST_FAILED")
            self._record_error(error_code)
            raise GatewayRequestError(
                error_code,
                str(error.get("message") or "OpenClaw Gateway request failed."),
                error.get("details") if isinstance(
                    error.get("details"), dict
                ) else None,
            )
        self._record_success()
        return result.get("payload")

    def stream(
        self,
        *,
        session_key: str,
        timeout_seconds: float = 30,
    ) -> Iterator[dict[str, Any]]:
        session_key = str(session_key or "").strip()
        if len(session_key) < 16 or len(session_key) > 1_024:
            raise ValueError("OpenClaw session key is invalid")
        payload = {
            "mode": "stream",
            "sessionKey": session_key,
            "timeoutMs": min(
                max(int(timeout_seconds * 1000), 1_000),
                _MAX_GATEWAY_TIMEOUT_MS,
            ),
        }
        return self._stream_payload(payload)

    def send_stream(
        self,
        *,
        session_key: str,
        endpoint_key: str,
        grant: str,
        binding_grant_provider: Callable[[], str] | None = None,
        message: str,
        idempotency_key: str,
        attachments: list[dict] | None = None,
        timeout_seconds: float = 300,
        retry_before_accept: bool = True,
        abort_accepted_on_close: bool = True,
    ) -> Iterator[dict[str, Any]]:
        values = {
            "sessionKey": (session_key, 16, 1_024),
            "endpointKey": (endpoint_key, 1, 768),
            "grant": (grant, 32, 256),
            "message": (message, 1, 20_000),
            "idempotencyKey": (idempotency_key, 1, 128),
        }
        normalized: dict[str, str] = {}
        for name, (value, minimum, maximum) in values.items():
            text = str(value or "").strip()
            if len(text) < minimum or len(text) > maximum:
                raise ValueError(f"OpenClaw {name} is invalid")
            normalized[name] = text
        payload = {
            "mode": "send-stream",
            **normalized,
            "attachments": list(attachments or []),
            "preflightAbort": True,
            "acceptTimeoutMs": 35_000,
            "startupProgressTimeoutMs": 15_000,
            "sessionIdleTimeoutMs": 15_000,
            "sessionIdlePollMs": 250,
            "timeoutMs": min(
                max(int(timeout_seconds * 1000), 1_000),
                _MAX_GATEWAY_TIMEOUT_MS,
            ),
        }

        def guarded_stream() -> Iterator[dict[str, Any]]:
            run_id: str | None = None
            terminal = False
            source = None
            try:
                for attempt in range(
                    len(_PRE_ACCEPT_RETRY_DELAYS_SECONDS) + 1
                ):
                    accepted = False
                    attempt_payload = payload
                    if attempt > 0 and binding_grant_provider is not None:
                        attempt_payload = {
                            **payload,
                            "grant": _refreshed_binding_grant(
                                binding_grant_provider
                            ),
                        }
                    source = self._stream_payload(attempt_payload)
                    try:
                        for item in source:
                            if item.get("type") == "accepted":
                                accepted = True
                                run_id = (
                                    str(item.get("runId") or "").strip()
                                    or None
                                )
                            if item.get("type") == "chat" and item.get("state") == "final":
                                # Stream snapshots may include abandoned generation prefixes.
                                # Only the same accepted run's persisted final answer is authoritative.
                                if not run_id or item.get("runId") != run_id:
                                    continue
                                authoritative, _ = self._reconcile_accepted_run(
                                    session_key=normalized["sessionKey"], run_id=run_id,
                                )
                                if authoritative is None:
                                    terminal = True  # host already ended; never abort/replay it
                                    raise GatewayRequestError(
                                        "GATEWAY_FINAL_HISTORY_PENDING",
                                        "The final answer is awaiting authoritative history.",
                                        {"safeToRetry": False, "reconciliationAttempted": True},
                                    )
                                item = {**authoritative, "replace": True}
                            if (
                                item.get("type") == "chat"
                                and item.get("state")
                                in {"final", "error", "aborted"}
                            ):
                                terminal = True
                            yield item
                        return
                    except GatewayRequestError as exc:
                        if (
                            accepted
                            and run_id
                            and exc.code in _POST_ACCEPT_RECONCILE_CODES
                        ):
                            recovered, observed_tool_activity = (
                                self._reconcile_accepted_run(
                                    session_key=normalized["sessionKey"],
                                    run_id=run_id,
                                )
                            )
                            if recovered:
                                terminal = True
                                yield recovered
                                return
                            exc.details = {
                                **exc.details,
                                "reconciliationAttempted": True,
                                "hadToolActivity": (
                                    exc.details.get("hadToolActivity") is True
                                    or observed_tool_activity
                                ),
                                "safeToRetry": False,
                            }
                        if not retry_before_accept or not _should_retry_before_accept(
                            exc,
                            accepted=accepted,
                            attempt=attempt,
                        ):
                            raise
                        _LOG.warning(
                            "Retrying OpenClaw Gateway before run acceptance "
                            "code=%s stage=%s attempt=%s delay_seconds=%s",
                            exc.code,
                            str(exc.details.get("stage") or "unknown"),
                            attempt + 1,
                            _PRE_ACCEPT_RETRY_DELAYS_SECONDS[attempt],
                        )
                        yield {
                            "type": "progress",
                            "runId": normalized["idempotencyKey"],
                            "kind": "system",
                            "phase": "retry",
                            "label": "OpenClaw connection is recovering",
                        }
                        self._retry_sleep(
                            _PRE_ACCEPT_RETRY_DELAYS_SECONDS[attempt]
                        )
                    finally:
                        close = getattr(source, "close", None)
                        if callable(close):
                            close()
                        source = None
            finally:
                close = getattr(source, "close", None)
                if callable(close):
                    close()
                if run_id and not terminal and abort_accepted_on_close:
                    self.abort_chat(
                        session_key=normalized["sessionKey"],
                        run_id=run_id,
                        timeout_seconds=5,
                        raise_on_error=False,
                    )

        return guarded_stream()

    def run_history_evidence(
        self,
        *,
        session_key: str,
        run_ref: str,
        timeout_seconds: float = 8,
    ) -> dict[str, Any]:
        history = self.call(
            "chat.history",
            {
                "sessionKey": session_key,
                "limit": 200,
                "maxChars": 200_000,
            },
            timeout_seconds=timeout_seconds,
        )
        return _history_evidence_for_run(history, run_ref, session_key=session_key)

    def _reconcile_accepted_run(
        self,
        *,
        session_key: str,
        run_id: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        observed_tool_activity = False
        for delay_seconds in _POST_ACCEPT_RECONCILE_DELAYS_SECONDS:
            if delay_seconds:
                self._retry_sleep(delay_seconds)
            try:
                history = self.call(
                    "chat.history",
                    {
                        "sessionKey": session_key,
                        "limit": 200,
                        "maxChars": 200_000,
                    },
                    timeout_seconds=5,
                )
            except GatewayRequestError:
                continue
            evidence = _history_evidence_for_run(history, run_id, session_key=session_key)
            observed_tool_activity = (
                observed_tool_activity or evidence["had_tool_activity"]
            )
            if evidence["final_text"]:
                _LOG.info(
                    "Recovered accepted OpenClaw run from authoritative "
                    "history run_id=%s had_tool_activity=%s",
                    run_id,
                    observed_tool_activity,
                )
                return (
                    {
                        "type": "chat",
                        "runId": run_id,
                        "state": "final",
                        "text": evidence["final_text"],
                        "recovered": True,
                        "hadToolActivity": observed_tool_activity,
                    },
                    observed_tool_activity,
                )
        return None, observed_tool_activity

    def abort_chat(
        self,
        *,
        session_key: str,
        run_id: str | None = None,
        preserve_side_runs: bool = True,
        timeout_seconds: float = 8,
        raise_on_error: bool = True,
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "sessionKey": str(session_key or "").strip(),
        }
        if not params["sessionKey"]:
            raise ValueError("OpenClaw session key is invalid")
        if run_id:
            params["runId"] = str(run_id).strip()
        elif preserve_side_runs:
            params["preserveSideRuns"] = True
        try:
            result = self.call(
                "chat.abort",
                params,
                timeout_seconds=timeout_seconds,
            )
        except GatewayRequestError as exc:
            if (
                not run_id
                and preserve_side_runs
                and exc.code == "INVALID_REQUEST"
            ):
                params.pop("preserveSideRuns", None)
                try:
                    result = self.call(
                        "chat.abort",
                        params,
                        timeout_seconds=timeout_seconds,
                    )
                except GatewayRequestError:
                    if raise_on_error:
                        raise
                    return None
                return result if isinstance(result, dict) else {}
            if raise_on_error:
                raise
            return None
        return result if isinstance(result, dict) else {}

    def _stream_payload(
        self,
        payload: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        self._record_attempt()
        try:
            environment = self._environment()
        except GatewayRequestError as exc:
            self._record_error(exc.code)
            raise
        try:
            process = subprocess.Popen(
                [
                    self.node_executable,
                    "--experimental-websocket",
                    str(self.script_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            self._record_error("GATEWAY_PROCESS_FAILED")
            raise GatewayRequestError(
                "GATEWAY_PROCESS_FAILED",
                "OpenClaw Gateway stream process failed.",
            ) from exc
        try:
            if process.stdin is None or process.stdout is None:
                raise GatewayRequestError(
                    "GATEWAY_PROCESS_FAILED",
                    "OpenClaw Gateway stream process is unavailable.",
                )
            process.stdin.write(json.dumps(payload, ensure_ascii=False))
            process.stdin.close()
            saw_eof = False
            for raw_line in process.stdout:
                try:
                    item = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "eof":
                    saw_eof = True
                    break
                if item.get("type") == "ready":
                    self._record_success()
                    continue
                if item.get("type") == "error":
                    error = (
                        item.get("error")
                        if isinstance(item.get("error"), dict)
                        else {}
                    )
                    error_code = str(
                        error.get("code") or "GATEWAY_STREAM_FAILED"
                    )
                    self._record_error(error_code)
                    raise GatewayRequestError(
                        error_code,
                        str(
                            error.get("message")
                            or "OpenClaw Gateway stream failed."
                        ),
                        error.get("details") if isinstance(
                            error.get("details"), dict
                        ) else None,
                    )
                if item.get("type") in {"accepted", "progress", "chat"}:
                    yield item
            if not saw_eof:
                self._record_error("GATEWAY_RESPONSE_INVALID")
                raise GatewayRequestError(
                    "GATEWAY_RESPONSE_INVALID",
                    "OpenClaw Gateway stream ended without a terminal frame.",
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def diagnostics(self) -> dict[str, Any]:
        parsed = urlparse(self.url)
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        target = f"{parsed.scheme}://{parsed.hostname}:{port}"
        with self._diagnostics_lock:
            return {
                "configured": True,
                "target": target,
                "last_attempt_at": self._last_attempt_at,
                "last_success_at": self._last_success_at,
                "last_error_at": self._last_error_at,
                "last_error_code": self._last_error_code,
            }

    def _record_attempt(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._diagnostics_lock:
            self._last_attempt_at = now

    def _record_success(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._diagnostics_lock:
            self._last_success_at = now

    def _record_error(self, code: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._diagnostics_lock:
            self._last_error_at = now
            self._last_error_code = str(code or "GATEWAY_UNKNOWN")

    def _environment(self) -> dict[str, str]:
        token = self._read_token()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "AB_GATEWAY_URL": self.url,
                "AB_GATEWAY_TOKEN": token,
                "AB_GATEWAY_IDENTITY_PATH": str(
                    self.state_dir / "device-identity.json"
                ),
            }
        )
        return environment

    def _read_token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise GatewayRequestError(
                "GATEWAY_TOKEN_UNAVAILABLE",
                "OpenClaw Gateway credentials are unavailable.",
            ) from exc
        if len(token) < 16:
            raise GatewayRequestError(
                "GATEWAY_TOKEN_INVALID",
                "OpenClaw Gateway credentials are invalid.",
            )
        return token


def _should_retry_before_accept(
    error: GatewayRequestError,
    *,
    accepted: bool,
    attempt: int,
) -> bool:
    if (
        accepted
        or attempt >= len(_PRE_ACCEPT_RETRY_DELAYS_SECONDS)
        or error.code not in _PRE_ACCEPT_RETRY_CODES
    ):
        return False
    stage = str(error.details.get("stage") or "").strip()
    if stage:
        return stage in _PRE_ACCEPT_RETRY_STAGES
    return error.code in {"GATEWAY_PROCESS_FAILED", "GATEWAY_RESPONSE_INVALID"}


def _history_evidence_for_run(payload: Any, run_id: str, *, session_key: str | None = None) -> dict[str, Any]:
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if (not isinstance(messages, list) or (session_key and payload.get("sessionKey")
                                          and payload["sessionKey"] != session_key)):
        return {
            "prompt_observed": False,
            "had_tool_activity": False,
            "final_text": "",
            "observed_run_id": "",
        }
    prompt_key = f"{run_id}:user"
    prompt_index = -1
    observed_run_id = ""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if (
            message.get("idempotencyKey") == prompt_key
            or message.get("runId") == run_id
        ):
            prompt_index = index
            observed_run_id = str(message.get("runId") or run_id).strip()
            break
    if prompt_index < 0:
        return {
            "prompt_observed": False,
            "had_tool_activity": False,
            "final_text": "",
            "observed_run_id": "",
        }

    had_tool_activity = False
    final_text = ""
    for value in messages[prompt_index + 1 :]:
        if not isinstance(value, dict):
            continue
        if value.get("role") == "user":
            break
        if value.get("runId") and value["runId"] != run_id:
            continue
        message_tool_activity = _history_message_has_tool_activity(value)
        had_tool_activity = had_tool_activity or message_tool_activity
        if message_tool_activity:
            final_text = ""
        if (value.get("role") == "assistant" and not message_tool_activity
                and value.get("stopReason", value.get("stop_reason")) in {"stop", "end_turn"}):
            text = _history_message_text(value.get("content"))
            if text:
                final_text = text
    return {
        "prompt_observed": True,
        "had_tool_activity": had_tool_activity,
        "final_text": final_text,
        "observed_run_id": observed_run_id,
    }


def _history_message_has_tool_activity(message: dict[str, Any]) -> bool:
    if (
        message.get("role") in {"tool", "toolResult"}
        or isinstance(message.get("toolCallId"), str)
        or isinstance(message.get("toolName"), str)
    ):
        return True
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type")
        in {"tool", "toolCall", "toolResult", "tool_use", "tool_result"}
        for item in content
    )


def _history_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"text", "output_text"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _validated_binding_grant(value: Any) -> str:
    grant = str(value or "").strip()
    if len(grant) < 32 or len(grant) > 256:
        raise GatewayRequestError(
            "GATEWAY_BINDING_GRANT_INVALID",
            "OpenClaw Workspace binding grant refresh failed.",
            {"stage": "bind", "safeToRetry": False},
        )
    return grant


def _refreshed_binding_grant(provider: Callable[[], str]) -> str:
    try:
        return _validated_binding_grant(provider())
    except GatewayRequestError:
        raise
    except Exception as exc:
        raise GatewayRequestError(
            "GATEWAY_BINDING_GRANT_REFRESH_FAILED",
            "OpenClaw Workspace binding grant refresh failed.",
            {"stage": "bind", "safeToRetry": False},
        ) from exc
