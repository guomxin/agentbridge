from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
import json
import mimetypes
from pathlib import Path
import re
import ssl
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from bscli.auth.server import validate_auth_server_config
from bscli.core.tasks import TaskNotFound
from bscli.core.tls_http import ThreadedTLSHTTPServer
from bscli.workspace.application import (
    WorkspaceApplication,
    WorkspaceArtifactError,
)
from bscli.workspace.gateway import GatewayRequestError
from bscli.workspace.stores import (
    WorkspaceConflictError,
    WorkspaceLinkError,
)


MAX_BODY_BYTES = 128 * 1024
MAX_CHAT_BODY_BYTES = 18 * 1024 * 1024
SESSION_COOKIE = "agentbridge_workspace_session"
CSRF_COOKIE = "agentbridge_workspace_csrf"
ENROLLMENT_COOKIE = "agentbridge_workspace_enrollment"
STATIC_ROOT = Path(__file__).with_name("static")
ASSET_VERSION_PLACEHOLDER = "__WORKSPACE_ASSET_VERSION__"
_CLIENT_DISCONNECT_ERRORS = (
    TimeoutError,
    BrokenPipeError,
    ConnectionResetError,
    ssl.SSLEOFError,
)


def _workspace_asset_version() -> str:
    digest = hashlib.sha256()
    for name in (
        "index.html", "workspace.css", "workspace.js",
        "original.html", "original.css", "original.js",
        "vendor/marked.umd.js", "vendor/purify.min.js",
    ):
        digest.update(name.encode("ascii"))
        digest.update((STATIC_ROOT / name).read_bytes())
    for path in sorted(STATIC_ROOT.glob("*.mjs")):
        digest.update(path.name.encode("ascii"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


@dataclass(frozen=True)
class WorkspaceServerConfig:
    host: str
    port: int
    public_base_url: str
    tls_cert: Path | None
    tls_key: Path | None

    @property
    def secure_cookie(self) -> bool:
        return self.public_base_url.startswith("https://")


class WorkspaceHTTPServer(ThreadedTLSHTTPServer):
    def __init__(self, *args, **kwargs):
        self._stream_counts: dict[str, int] = {}
        self._stream_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def serve_forever(self, poll_interval=0.5):
        self.workbench_application.start_workbench()
        try:
            return super().serve_forever(poll_interval=poll_interval)
        finally:
            self.workbench_application.close()

    def server_close(self):
        if hasattr(self, 'workbench_application'):
            self.workbench_application.close()
        super().server_close()

    def acquire_stream(self, subject: str) -> bool:
        with self._stream_lock:
            if self._stream_counts.get(subject, 0) >= 6:
                return False
            self._stream_counts[subject] = self._stream_counts.get(subject, 0) + 1
            return True

    def release_stream(self, subject: str) -> None:
        with self._stream_lock:
            count = self._stream_counts[subject] - 1
            if count:
                self._stream_counts[subject] = count
            else:
                del self._stream_counts[subject]


def _bounded_user_stream(method):
    @wraps(method)
    def wrapped(self, account, *args, **kwargs):
        subject = account['user_subject']
        if not self.server.acquire_stream(subject):
            self._json(429, {'error': {'code': 'STREAM_LIMIT_REACHED'}})
            return
        try:
            return method(self, account, *args, **kwargs)
        finally:
            self.server.release_stream(subject)
    return wrapped


class _RateLimiter:
    def __init__(self, attempts: int = 8, window_seconds: int = 900) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._values: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            values = [
                item
                for item in self._values.get(key, [])
                if now - item < self.window_seconds
            ]
            self._values[key] = values
            return len(values) < self.attempts

    def fail(self, key: str) -> None:
        with self._lock:
            self._values.setdefault(key, []).append(time.monotonic())

    def success(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)


def validate_workspace_server_config(
    *,
    host: str,
    port: int,
    public_base_url: str | None,
    tls_cert: str | Path | None,
    tls_key: str | Path | None,
) -> WorkspaceServerConfig:
    validated = validate_auth_server_config(
        host=host,
        port=port,
        public_base_url=public_base_url,
        tls_cert=tls_cert,
        tls_key=tls_key,
        allow_insecure_private_http=False,
    )
    return WorkspaceServerConfig(
        host=validated.host,
        port=validated.port,
        public_base_url=validated.public_base_url,
        tls_cert=validated.tls_cert,
        tls_key=validated.tls_key,
    )


def create_workspace_http_server(
    *,
    config: WorkspaceServerConfig,
    application: WorkspaceApplication,
) -> WorkspaceHTTPServer:
    expected = urlparse(config.public_base_url)
    expected_origin = f"{expected.scheme.lower()}://{expected.netloc.lower()}"
    allowed_host = (expected.hostname or "").lower()
    trusted_media = urlparse(application.service.trusted_card_base_url)
    if (
        trusted_media.scheme.lower() not in {"http", "https"}
        or not trusted_media.netloc
        or trusted_media.username
        or trusted_media.password
    ):
        raise ValueError("trusted media origin is invalid")
    trusted_media_origin = (
        f"{trusted_media.scheme.lower()}://{trusted_media.netloc.lower()}"
    )
    image_sources = "'self' data:"
    if trusted_media_origin != expected_origin:
        image_sources = f"{image_sources} {trusted_media_origin}"
    limiter = _RateLimiter()
    asset_version = _workspace_asset_version()

    class WorkspaceRequestHandler(BaseHTTPRequestHandler):
        server_version = "AgentBridgeWorkspace/0.1"
        sys_version = ""

        def log_message(self, _format: str, *_args) -> None:
            return None

        def do_GET(self) -> None:
            route = urlparse(self.path)
            if not self._host_allowed():
                self._json(400, {"error": {"code": "INVALID_HOST"}})
                return
            if route.path == "/healthz":
                self._json(
                    200,
                    {
                        "status": "ok",
                        "service": "agentbridge_workspace",
                    },
                )
                return
            if route.path == "/readyz":
                readiness = application.service.runtime_governance.readiness()
                payload = {
                    **readiness,
                    "service": "agentbridge_workspace",
                    "gatewayConfigured": application.gateway is not None,
                }
                ready = readiness["status"] == "ready" and application.gateway is not None
                payload["status"] = "ready" if ready else "not_ready"
                self._json(200 if ready else 503, payload)
                return
            if route.path == "/api/client-version":
                self._json(200, {"version": asset_version})
                return
            if route.path in {"/", "/index.html"}:
                self._static("index.html")
                return
            if re.fullmatch(r'/database/logs/[a-z][a-z0-9_-]{0,63}/[0-9]{1,19}', route.path):
                self._static('original.html')
                return
            if route.path.startswith("/assets/"):
                self._static(route.path.removeprefix("/assets/"))
                return
            if route.path == "/favicon.ico":
                self.send_response(204)
                self._security_headers()
                self.end_headers()
                return
            if route.path == "/api/session":
                account = self._session()
                self._json(
                    200,
                    {
                        "authenticated": account is not None,
                        "account": application.public_account(account),
                    },
                )
                return
            if route.path == "/api/enrollment/status":
                token = self._cookie(ENROLLMENT_COOKIE)
                link = application.enrollment_status(token) if token else None
                self._json(
                    200,
                    {
                        "active": link is not None,
                        "state": link["state"] if link else None,
                        "expiresAt": link["expires_at"] if link else None,
                    },
                )
                return
            account = self._authenticate()
            if account is None:
                return
            query = parse_qs(route.query)
            try:
                original_match = re.fullmatch(r'/api/database/logs/([a-z][a-z0-9_-]{0,63})/([0-9]{1,19})', route.path)
                if route.path == "/api/skill-drafts":
                    authoring = application.service.skill_authoring
                    draft_id = query.get("id", [None])[0]
                    self._json(200, authoring.get(account["user_subject"], draft_id) if draft_id else {
                        **authoring.list(account["user_subject"]), "preferences": authoring.preferences(account["user_subject"]),
                        "scope": 'workspace:' + account['account_id'],
                        "scopes": authoring.workbench.scopes(account['user_subject']),
                        "jobs": authoring.workbench.jobs(account['user_subject']),
                        "runtime": authoring.workbench.status()})
                    return
                if route.path == "/api/skills/history":
                    self._json(200, {"items": application.service.skills.load_history(account["user_subject"])})
                    return
                if route.path == "/api/skills":
                    from bscli.core.business_skills import skill_catalog
                    self._json(200, skill_catalog(application.service, account["user_subject"]))
                    return
                if original_match:
                    from bscli.database.independent import DatabaseRejected, rejection_result
                    try:
                        result = application.database_original(account, *original_match.groups())
                        if application.session(self._cookie(SESSION_COOKIE)) is None:
                            self._json(401, {'error': {'code': 'AUTH_REQUIRED'}})
                            return
                        self._json(200, result)
                    except DatabaseRejected as exc:
                        status = 404 if str(exc) == 'DATABASE_LOG_NOT_FOUND' else 403 if str(exc) in {'DATABASE_CAPABILITY_DENIED', 'DATABASE_AUTHORIZATION_CHANGED'} else 400
                        self._json(status, rejection_result(exc))
                    return
                if route.path == "/api/tasks":
                    self._json(
                        200,
                        {
                            "items": application.list_tasks(
                                account,
                                active_only=_query_bool(
                                    query,
                                    "active_only",
                                    False,
                                ),
                                limit=_query_int(query, "limit", 100),
                            )
                        },
                    )
                    return
                if route.path == "/api/artifacts/history":
                    self._json(
                        200,
                        {
                            "items": application.artifact_history(
                                account,
                                limit=_query_int(query, "limit", 20),
                            )
                        },
                    )
                    return
                task_match = re.fullmatch(
                    r"/api/tasks/([0-9a-f-]{36})",
                    route.path,
                )
                if task_match:
                    self._json(
                        200,
                        application.task_detail(
                            account,
                            task_match.group(1),
                        ),
                    )
                    return
                if route.path == "/api/events":
                    self._json(
                        200,
                        {
                            "items": application.list_events(
                                account,
                                after_event_id=_query_value(
                                    query,
                                    "after",
                                ),
                                limit=_query_int(query, "limit", 100),
                            )
                        },
                    )
                    return
                if route.path == "/api/timeline":
                    after_value = _query_value(query, "after")
                    before_value = _query_value(query, "before")
                    after_sequence = (
                        int(after_value) if after_value is not None else None
                    )
                    before_sequence = (
                        int(before_value) if before_value is not None else None
                    )
                    entry_type = _query_value(query, "entry_type")
                    requested_limit = min(
                        max(_query_int(query, "limit", 200), 1),
                        500,
                    )
                    items = application.list_timeline(
                        account,
                        after_sequence=after_sequence,
                        before_sequence=before_sequence,
                        entry_type=entry_type,
                        limit=requested_limit + 1,
                    )
                    has_more = len(items) > requested_limit
                    if has_more:
                        items = (
                            items[:requested_limit]
                            if after_sequence is not None
                            else items[-requested_limit:]
                        )
                    global_cursor = application.timeline_cursor(account)
                    self._json(
                        200,
                        {
                            "items": items,
                            "cursor": (
                                items[-1]["sequence"]
                                if has_more and after_sequence is not None
                                else global_cursor
                            ),
                            "oldestSequence": (
                                items[0]["sequence"] if items else None
                            ),
                            "hasMore": has_more,
                        },
                    )
                    return
                if route.path == "/api/timeline/stream":
                    cursor_value = (
                        self.headers.get("Last-Event-ID")
                        or _query_value(query, "after")
                    )
                    self._timeline_stream(
                        account,
                        after_sequence=(
                            int(cursor_value) if cursor_value else None
                        ),
                    )
                    return
                database_report_match = re.fullmatch(r"/api/database/reports/([a-z][a-z0-9_-]{0,63})/([0-9a-f]{32})/download", route.path)
                if database_report_match:
                    self._download(application.database_report(account,*database_report_match.groups()))
                    return
                report_match = re.fullmatch(r"/api/analytics/reports/([0-9a-f]{32})/download", route.path)
                if report_match:
                    self._download(application.analytics_report(account, report_match.group(1)))
                    return
                attachment_match = re.fullmatch(
                    r"/api/timeline/attachments/([A-Za-z0-9_-]{32,128})/download",
                    route.path,
                )
                if attachment_match:
                    self._download(
                        application.timeline_attachment(
                            account,
                            attachment_match.group(1),
                        )
                    )
                    return
                if route.path == "/api/events/stream":
                    self._event_stream(
                        account,
                        after_event_id=(
                            self.headers.get("Last-Event-ID")
                            or _query_value(query, "after")
                        ),
                    )
                    return
                if route.path == "/api/endpoints":
                    self._json(
                        200,
                        {"items": application.list_endpoints(account)},
                    )
                    return
                if route.path == "/api/gateway":
                    self._json(200, application.gateway_status())
                    return
                if route.path == "/api/chat/dispatches":
                    self._json(
                        200,
                        {
                            "items": application.list_chat_dispatches(
                                account,
                                active_only=_query_bool(
                                    query,
                                    "active_only",
                                    True,
                                ),
                                limit=_query_int(query, "limit", 20),
                            )
                        },
                    )
                    return
                if route.path == "/api/chat/history":
                    self._json(
                        200,
                        application.chat_history(
                            account,
                            limit=_query_int(query, "limit", 100),
                        ),
                    )
                    return
                self._json(404, {"error": {"code": "NOT_FOUND"}})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            route = urlparse(self.path)
            if not self._host_allowed() or not self._origin_allowed(
                expected_origin
            ):
                self._json(
                    403,
                    {"error": {"code": "INVALID_REQUEST_ORIGIN"}},
                )
                return
            if route.path == "/api/enrollment/start":
                link = application.start_enrollment()
                self._json(
                    201,
                    {
                        "linkCode": link["link_code"],
                        "state": link["state"],
                        "expiresAt": link["expires_at"],
                    },
                    enrollment_token=link["enrollment_token"],
                )
                return
            if route.path == "/api/enrollment/complete":
                self._complete_enrollment()
                return
            if route.path == "/api/login":
                self._login(limiter)
                return
            account = self._authenticate(require_csrf=True)
            if account is None:
                return
            try:
                body = self._read_json(
                    max_bytes=(
                        MAX_CHAT_BODY_BYTES
                        if route.path in {"/api/chat/send", "/api/chat/send-stream"}
                        else MAX_BODY_BYTES
                    )
                )
                if route.path == "/api/skill-drafts":
                    self._json(200, application.service.skill_authoring.dispatch(account["user_subject"], body.get("action"), body.get("data", {})))
                    return
                if route.path == "/api/logout":
                    application.logout(self._cookie(SESSION_COOKIE))
                    self._json(200, {"status": "signed_out"}, clear=True)
                    return
                continuation_match = re.fullmatch(
                    r"/api/tasks/([0-9a-f-]{36})/continue",
                    route.path,
                )
                if continuation_match:
                    self._json(
                        200,
                        application.continue_task(
                            account,
                            continuation_match.group(1),
                        ),
                    )
                    return
                artifact_reissue_match = re.fullmatch(
                    r"/api/tasks/([0-9a-f-]{36})/artifacts/"
                    r"([0-9a-f-]{36})/reissue",
                    route.path,
                )
                if artifact_reissue_match:
                    self._json(
                        200,
                        application.reissue_artifact(
                            account,
                            task_id=artifact_reissue_match.group(1),
                            artifact_id=artifact_reissue_match.group(2),
                        ),
                    )
                    return
                dispatch_cancel_match = re.fullmatch(
                    r"/api/chat/dispatches/([0-9a-f-]{36})/cancel",
                    route.path,
                )
                if dispatch_cancel_match:
                    self._json(
                        200,
                        application.cancel_chat_dispatch(
                            account,
                            dispatch_cancel_match.group(1),
                        ),
                    )
                    return
                if route.path == "/api/chat/send":
                    result = application.send_chat(
                        account,
                        message=_required_string(body, "message"),
                        idempotency_key=_optional_string(
                            body,
                            "idempotencyKey",
                        ),
                        attachments=body.get("attachments"),
                    )
                    self._json(
                        202,
                        {
                            "status": result.status,
                            "runId": result.run_id,
                        },
                    )
                    return
                if route.path == "/api/chat/send-stream":
                    self._chat_send_stream(account, body)
                    return
                self._json(404, {"error": {"code": "NOT_FOUND"}})
            except Exception as exc:
                self._handle_error(exc)

        def _complete_enrollment(self) -> None:
            enrollment = self._cookie(ENROLLMENT_COOKIE)
            if not enrollment:
                self._json(
                    409,
                    {"error": {"code": "ENROLLMENT_NOT_STARTED"}},
                )
                return
            try:
                body = self._read_json()
                result = application.complete_enrollment(
                    enrollment_token=enrollment,
                    username=_required_string(body, "username"),
                    password=_required_string(body, "password"),
                )
            except Exception as exc:
                self._handle_error(exc)
                return
            self._json(
                201,
                {
                    "authenticated": True,
                    "account": result["account"],
                },
                session=result["session"],
                clear_enrollment=True,
            )

        def _login(self, limiter: _RateLimiter) -> None:
            key = self.client_address[0]
            if not limiter.allowed(key):
                self._json(
                    429,
                    {"error": {"code": "LOGIN_RATE_LIMITED"}},
                )
                return
            try:
                body = self._read_json()
                result = application.login(
                    username=_required_string(body, "username"),
                    password=_required_string(body, "password"),
                )
            except Exception as exc:
                limiter.fail(key)
                self._handle_error(exc)
                return
            if result is None:
                limiter.fail(key)
                self._json(
                    401,
                    {"error": {"code": "LOGIN_FAILED"}},
                )
                return
            limiter.success(key)
            self._json(
                200,
                {
                    "authenticated": True,
                    "account": result["account"],
                },
                session=result["session"],
            )

        def _session(self, *, require_csrf: bool = False) -> dict | None:
            csrf_token = (
                self.headers.get("X-AgentBridge-CSRF")
                if require_csrf
                else None
            )
            if require_csrf and not csrf_token:
                return None
            return application.session(
                self._cookie(SESSION_COOKIE),
                csrf_token=csrf_token,
            )

        def _authenticate(self, *, require_csrf: bool = False) -> dict | None:
            account = self._session(require_csrf=require_csrf)
            if account is None:
                self._json(
                    401,
                    {"error": {"code": "AUTHENTICATION_REQUIRED"}},
                )
            return account

        @_bounded_user_stream
        def _event_stream(
            self,
            account: dict,
            *,
            after_event_id: str | None,
        ) -> None:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self._security_headers()
            self.end_headers()
            cursor = after_event_id or application.event_cursor(account)
            deadline = time.monotonic() + 25
            try:
                if not after_event_id:
                    self.wfile.write(
                        (
                            f"id: {cursor}\n"
                            "event: cursor\n"
                            'data: {"ready":true}\n\n'
                        ).encode("utf-8")
                    )
                    self.wfile.flush()
                while time.monotonic() < deadline:
                    events = application.list_events(
                        account,
                        after_event_id=cursor,
                        limit=100,
                    )
                    for event in events:
                        cursor = event["event_id"]
                        payload = json.dumps(
                            event,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        self.wfile.write(
                            (
                                f"id: {cursor}\n"
                                "event: task\n"
                                f"data: {payload}\n\n"
                            ).encode("utf-8")
                        )
                    if events:
                        self.wfile.flush()
                    time.sleep(1)
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            except _CLIENT_DISCONNECT_ERRORS:
                return

        @_bounded_user_stream
        def _timeline_stream(
            self,
            account: dict,
            *,
            after_sequence: int | None,
        ) -> None:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self._security_headers()
            self.end_headers()
            cursor = (
                max(int(after_sequence), 0)
                if after_sequence is not None
                else application.timeline_cursor(account)
            )
            deadline = time.monotonic() + 25
            try:
                if after_sequence is None:
                    self.wfile.write(
                        (
                            f"id: {cursor}\n"
                            "event: cursor\n"
                            'data: {"ready":true}\n\n'
                        ).encode("utf-8")
                    )
                    self.wfile.flush()
                while time.monotonic() < deadline:
                    entries = application.list_timeline(
                        account,
                        after_sequence=cursor,
                        limit=100,
                    )
                    for entry in entries:
                        cursor = int(entry["sequence"])
                        payload = json.dumps(
                            entry,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        self.wfile.write(
                            (
                                f"id: {cursor}\n"
                                "event: timeline\n"
                                f"data: {payload}\n\n"
                            ).encode("utf-8")
                        )
                    if entries:
                        self.wfile.flush()
                    time.sleep(1)
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            except _CLIENT_DISCONNECT_ERRORS:
                return

        @_bounded_user_stream
        def _chat_send_stream(self, account: dict, body: dict) -> None:
            message = _required_string(body, "message")
            idempotency_key = _optional_string(body, "idempotencyKey")
            # Validation and durable acceptance can fail before an SSE response
            # exists. Let do_POST return its normal JSON error in that case.
            stream = application.send_chat_stream(
                account,
                message=message,
                idempotency_key=idempotency_key,
                attachments=body.get("attachments"),
            )
            try:
                self.close_connection = True
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/event-stream; charset=utf-8",
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self._security_headers()
                self.end_headers()
                self.wfile.write(b"retry: 1000\n\n")
                self.wfile.flush()
                for item in stream:
                    event_name = {
                        "accepted": "accepted",
                        "chat": "chat",
                    }.get(item.get("type"), "progress")
                    payload = json.dumps(
                        item,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    self.wfile.write(
                        (
                            f"event: {event_name}\n"
                            f"data: {payload}\n\n"
                        ).encode("utf-8")
                    )
                    self.wfile.flush()
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            except _CLIENT_DISCONNECT_ERRORS:
                return
            except Exception as exc:
                # Once headers are sent, a second HTTP/JSON response would be
                # swallowed by the SSE parser. Always finish with an SSE error.
                payload = json.dumps(
                    _public_gateway_stream_error(exc)
                    if isinstance(exc, GatewayRequestError)
                    else {"code": "WORKSPACE_STREAM_FAILED", "safeToRetry": False},
                    separators=(",", ":"),
                )
                try:
                    self.wfile.write(
                        (
                            "event: stream-error\n"
                            f"data: {payload}\n\n"
                        ).encode("utf-8")
                    )
                    self.wfile.flush()
                except _CLIENT_DISCONNECT_ERRORS:
                    return
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()

        def _read_json(
            self,
            *,
            max_bytes: int = MAX_BODY_BYTES,
        ) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise ValueError("invalid request body length") from exc
            if length <= 0 or length > max_bytes:
                raise ValueError("invalid request body length")
            try:
                value = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("request body must be valid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError("request body must be a JSON object")
            return value

        def _cookie(self, name: str) -> str | None:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie") or "")
            except Exception:
                return None
            morsel = cookie.get(name)
            return morsel.value if morsel else None

        def _host_allowed(self) -> bool:
            host = (self.headers.get("Host") or "").split(":", 1)[0].lower()
            return host == allowed_host

        def _origin_allowed(self, expected_origin: str) -> bool:
            return (self.headers.get("Origin") or "").lower() == expected_origin

        def _static(self, relative: str) -> None:
            if (
                not relative
                or ".." in relative
                or relative.startswith(("/", "\\"))
            ):
                self._json(404, {"error": {"code": "NOT_FOUND"}})
                return
            path = (STATIC_ROOT / relative).resolve()
            try:
                path.relative_to(STATIC_ROOT.resolve())
                body = path.read_bytes()
            except (ValueError, OSError):
                self._json(404, {"error": {"code": "NOT_FOUND"}})
                return
            if path.name in {"index.html", "original.html"}:
                body = body.replace(
                    ASSET_VERSION_PLACEHOLDER.encode("ascii"),
                    asset_version.encode("ascii"),
                )
            self.send_response(200)
            self.send_header(
                "Content-Type",
                ("text/javascript" if path.suffix == ".mjs" else mimetypes.guess_type(path.name)[0])
                or "application/octet-stream",
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Cache-Control",
                (
                    "no-store"
                    if path.name in {"index.html", "original.html"} or path.suffix == ".mjs"
                    else "public, max-age=31536000, immutable"
                ),
            )
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _download(self, attachment: dict) -> None:
            body = attachment["body"]
            filename = str(attachment["filename"])
            ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
            if not ascii_name:
                ascii_name = "image"
            encoded_name = quote(filename, safe="")
            self.send_response(200)
            self.send_header("Content-Type", attachment["content_type"])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header(
                "Content-Disposition",
                (
                    f'attachment; filename="{ascii_name}"; '
                    f"filename*=UTF-8''{encoded_name}"
                ),
            )
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _json(
            self,
            status: int,
            payload: dict,
            *,
            session: dict | None = None,
            enrollment_token: str | None = None,
            clear: bool = False,
            clear_enrollment: bool = False,
        ) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if session is not None:
                self._set_cookie(
                    SESSION_COOKIE,
                    session["session_token"],
                    http_only=True,
                    max_age=30 * 24 * 3600,
                )
                self._set_cookie(
                    CSRF_COOKIE,
                    session["csrf_token"],
                    http_only=False,
                    max_age=30 * 24 * 3600,
                )
            if enrollment_token is not None:
                self._set_cookie(
                    ENROLLMENT_COOKIE,
                    enrollment_token,
                    http_only=True,
                    max_age=15 * 60,
                )
            if clear:
                self._set_cookie(SESSION_COOKIE, "", http_only=True, max_age=0)
                self._set_cookie(CSRF_COOKIE, "", http_only=False, max_age=0)
            if clear_enrollment:
                self._set_cookie(
                    ENROLLMENT_COOKIE,
                    "",
                    http_only=True,
                    max_age=0,
                )
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _set_cookie(
            self,
            name: str,
            value: str,
            *,
            http_only: bool,
            max_age: int,
        ) -> None:
            parts = [
                f"{name}={value}",
                "Path=/",
                f"Max-Age={max_age}",
                "SameSite=Strict",
            ]
            if config.secure_cookie:
                parts.append("Secure")
            if http_only:
                parts.append("HttpOnly")
            self.send_header("Set-Cookie", "; ".join(parts))

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=()",
            )
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                f"form-action 'self'; img-src {image_sources}; "
                "style-src 'self'; script-src 'self'; connect-src 'self'",
            )
            if config.secure_cookie:
                self.send_header(
                    "Strict-Transport-Security",
                    "max-age=31536000",
                )

        def _handle_error(self, exc: Exception) -> None:
            from bscli.core.user_grants import UserGrantConflict
            if isinstance(exc, UserGrantConflict):
                self._json(409, {"error": {"code": "REVISION_CONFLICT", "message": str(exc)}})
                return
            if isinstance(exc, KeyError):
                self._json(404, {"error": {"code": "NOT_FOUND", "message": "记录不存在或不可访问"}})
                return
            if isinstance(exc, GatewayRequestError):
                status = (
                    503
                    if exc.code.startswith(("GATEWAY_", "WEBSOCKET_"))
                    else 502
                )
                self._json(
                    status,
                    {
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                        }
                    },
                )
                return
            if isinstance(exc, TaskNotFound):
                self._json(404, {"error": {"code": "TASK_NOT_FOUND"}})
                return
            if isinstance(exc, WorkspaceConflictError):
                conflict_code = str(exc)
                if conflict_code not in {
                    "IDEMPOTENCY_PAYLOAD_MISMATCH",
                    "WORKSPACE_HOST_QUEUE_CONVERSATION_LIMIT",
                    "WORKSPACE_HOST_QUEUE_USER_LIMIT",
                    "HOST_DISPATCH_CANCEL_NOT_ALLOWED",
                    "HOST_DISPATCH_CLAIM_LOST",
                    "HOST_ACCEPT_DUPLICATE_RUN_VIOLATION",
                }:
                    conflict_code = "WORKSPACE_CONFLICT"
                self._json(
                    409,
                    {
                        "error": {
                            "code": conflict_code,
                            "message": str(exc),
                        }
                    },
                )
                return
            if isinstance(exc, WorkspaceLinkError):
                self._json(
                    409,
                    {
                        "error": {
                            "code": "WORKSPACE_LINK_INVALID",
                            "message": str(exc),
                        }
                    },
                )
                return
            if isinstance(exc, WorkspaceArtifactError):
                self._json(
                    409,
                    {
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                        }
                    },
                )
                return
            if isinstance(exc, (ValueError, PermissionError)):
                self._json(
                    400 if isinstance(exc, ValueError) else 403,
                    {
                        "error": {
                            "code": (
                                "INVALID_REQUEST"
                                if isinstance(exc, ValueError)
                                else "FORBIDDEN"
                            ),
                            "message": str(exc),
                        }
                    },
                )
                return
            self._json(
                500,
                {"error": {"code": "WORKSPACE_INTERNAL_ERROR"}},
            )

    server = WorkspaceHTTPServer(
        (config.host, config.port),
        WorkspaceRequestHandler,
    )
    server.workbench_application = application
    if config.tls_cert and config.tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config.tls_cert, config.tls_key)
        server.enable_tls(context)
    return server


_PUBLIC_GATEWAY_DETAIL_KEYS = {
    "abortPurpose",
    "abortRequested",
    "aborted",
    "accepted",
    "acceptedElapsedMs",
    "firstProgressElapsedMs",
    "hadProgress",
    "hadToolActivity",
    "promptObserved",
    "recoveryAttempt",
    "recoveryUsed",
    "stage",
}


def _public_gateway_stream_error(error: GatewayRequestError) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for key in _PUBLIC_GATEWAY_DETAIL_KEYS:
        value = error.details.get(key)
        if isinstance(value, bool) or isinstance(value, int):
            details[key] = value
        elif isinstance(value, str):
            details[key] = value[:80]
    safe_to_retry = error.details.get("safeToRetry") is True
    if (
        error.code
        in {
            "GATEWAY_RUN_TIMEOUT_ABORTED",
            "GATEWAY_START_STALLED_ABORTED",
        }
        and details.get("aborted") is True
        and details.get("hadToolActivity") is not True
    ):
        safe_to_retry = True
    return {
        "code": error.code,
        "safeToRetry": safe_to_retry,
        **({"details": details} if details else {}),
    }


def _required_string(body: dict, name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _optional_string(body: dict, name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value.strip() or None


def _query_value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0].strip() if values and values[0].strip() else None


def _query_int(
    query: dict[str, list[str]],
    name: str,
    default: int,
) -> int:
    value = _query_value(query, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _query_bool(
    query: dict[str, list[str]],
    name: str,
    default: bool,
) -> bool:
    value = _query_value(query, name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes"}:
        return True
    if value.lower() in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be a boolean")
