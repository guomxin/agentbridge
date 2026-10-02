from __future__ import annotations

from dataclasses import dataclass
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
from urllib.parse import parse_qs, urlparse

from bscli.admin.application import AdminControlPlane
from bscli.auth.server import validate_auth_server_config
from bscli.core.mcp_identities import TokenEditConflict
from bscli.core.user_grants import UserGrantConflict
from bscli.core.tls_http import ThreadedTLSHTTPServer
from bscli.database.independent import DatabaseGrantConflict
from bscli.database.sources import SourceConflict


MAX_ADMIN_BODY_BYTES = 64 * 1024
SESSION_COOKIE = "agentbridge_admin_session"
CSRF_COOKIE = "agentbridge_admin_csrf"
STATIC_ROOT = Path(__file__).with_name("static")


@dataclass(frozen=True)
class AdminServerConfig:
    host: str
    port: int
    public_base_url: str
    tls_cert: Path | None
    tls_key: Path | None

    @property
    def secure_cookie(self) -> bool:
        return self.public_base_url.startswith("https://")


class AdminHTTPServer(ThreadedTLSHTTPServer):
    pass


class LoginRateLimiter:
    def __init__(self, *, attempts: int = 5, window_seconds: int = 900) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            values = [
                value
                for value in self._failures.get(key, [])
                if now - value < self.window_seconds
            ]
            self._failures[key] = values
            return len(values) < self.attempts

    def fail(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.monotonic())

    def success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def validate_admin_server_config(
    *,
    host: str,
    port: int,
    public_base_url: str | None,
    tls_cert: str | Path | None,
    tls_key: str | Path | None,
) -> AdminServerConfig:
    validated = validate_auth_server_config(
        host=host,
        port=port,
        public_base_url=public_base_url,
        tls_cert=tls_cert,
        tls_key=tls_key,
        allow_insecure_private_http=False,
    )
    return AdminServerConfig(
        host=validated.host,
        port=validated.port,
        public_base_url=validated.public_base_url,
        tls_cert=validated.tls_cert,
        tls_key=validated.tls_key,
    )


def create_admin_http_server(
    *,
    config: AdminServerConfig,
    control_plane: AdminControlPlane,
) -> ThreadingHTTPServer:
    expected = urlparse(config.public_base_url)
    expected_origin = f"{expected.scheme.lower()}://{expected.netloc.lower()}"
    allowed_host = (expected.hostname or "").lower()
    limiter = LoginRateLimiter()

    class AdminRequestHandler(BaseHTTPRequestHandler):
        server_version = "AgentBridgeAdmin/0.1"
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
                        "service": "agentbridge_admin",
                        "releaseId": control_plane.release_id,
                    },
                )
                return
            if route.path == "/readyz":
                readiness = control_plane.readiness()
                self._json(200 if readiness["status"] == "ready" else 503, readiness)
                return
            if route.path in {"/", "/index.html"}:
                self._static("index.html")
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
                actor = control_plane.admin_sessions.verify(self._session_token())
                if actor is None:
                    self._json(200, {"authenticated": False})
                else:
                    self._json(
                        200,
                        {
                            "authenticated": True,
                            "account": actor,
                            "authorization_model": "user_grants",
                        },
                    )
                return
            actor = self._authenticate()
            if actor is None:
                return
            if actor["must_change_password"]:
                self._json(403, {"error": {"code": "PASSWORD_CHANGE_REQUIRED", "message": "Change the bootstrap password before continuing."}})
                return
            query = parse_qs(route.query)
            try:
                if route.path == "/api/admin-accounts":
                    self._json(200, {"items": control_plane.list_admin_accounts()})
                elif route.path == "/api/overview":
                    self._json(200, control_plane.overview())
                elif route.path == "/api/runtime":
                    self._json(200, control_plane.runtime())
                elif route.path == "/api/governance":
                    self._json(
                        200,
                        control_plane.runtime_governance(
                            evaluate=_query_value(query, "evaluate") == "true"
                        ),
                    )
                elif route.path == "/api/traces":
                    self._json(
                        200,
                        {
                            "items": control_plane.runtime_traces(
                                user_subject=_query_value(query, "user"),
                                status=_query_value(query, "status"),
                                limit=_query_int(query, "limit", 300),
                            )
                        },
                    )
                elif trace_match := re.fullmatch(
                    r"/api/traces/([0-9a-f-]{36})", route.path
                ):
                    self._json(200, control_plane.runtime_trace(trace_match.group(1)))
                elif route.path == "/api/incidents":
                    self._json(
                        200,
                        {
                            "items": control_plane.runtime_incidents(
                                state=_query_value(query, "state"),
                                severity=_query_value(query, "severity"),
                                limit=_query_int(query, "limit", 500),
                            )
                        },
                    )
                elif route.path == "/api/coordination":
                    self._json(
                        200,
                        control_plane.coordination(
                            user_subject=_query_value(query, "user"),
                            task_status=_query_value(query, "status"),
                            limit=_query_int(query, "limit", 200),
                        ),
                    )
                elif route.path == "/api/users":
                    self._json(200, {"items": control_plane.users()})
                elif route.path == "/api/database-grants":
                    self._json(200, control_plane.database_grant_config(_query_value(query, "user"),_query_value(query,"source") or 'taihua_primary'))
                elif route.path == "/api/skill-reviews":
                    request_id = _query_value(query, "id")
                    self._json(200, control_plane.service.skill_authoring.review_detail(request_id) if request_id else control_plane.service.skill_authoring.reviews())
                elif route.path == "/api/skills":
                    self._json(200, control_plane.skill_config(_query_value(query, "user")))
                elif route.path == "/api/user-grants":
                    self._json(200, control_plane.user_grant_config(_query_value(query, "user")))
                elif route.path == "/api/database-sources":
                    self._json(200, control_plane.database_sources())
                elif route.path == "/api/tokens":
                    self._json(
                        200,
                        {
                            "items": control_plane.list_tokens(
                                user_subject=_query_value(query, "user"),
                                limit=_query_int(query, "limit", 500),
                            )
                        },
                    )
                elif route.path == "/api/sessions":
                    self._json(200, {"items": control_plane.sessions()})
                elif session_events_match := re.fullmatch(
                    r"/api/sessions/([0-9a-f-]{36})/events", route.path
                ):
                    self._json(
                        200,
                        {
                            "items": control_plane.session_events(
                                session_id=session_events_match.group(1),
                                limit=_query_int(query, "limit", 100),
                            )
                        },
                    )
                elif route.path == "/api/capabilities":
                    self._json(200, {"items": control_plane.capabilities()})
                elif route.path == "/api/policies":
                    self._json(200, {"items": control_plane.policies.list()})
                elif route.path == "/api/operations":
                    self._json(
                        200,
                        {
                            "items": control_plane.operations(
                                user_subject=_query_value(query, "user"),
                                status=_query_value(query, "status"),
                                limit=_query_int(query, "limit", 200),
                            )
                        },
                    )
                elif route.path == "/api/interactions":
                    self._json(
                        200,
                        {
                            "items": control_plane.interactions(
                                user_subject=_query_value(query, "user"),
                                interaction_type=_query_value(query, "type"),
                                limit=_query_int(query, "limit", 200),
                            )
                        },
                    )
                elif route.path == "/api/audit":
                    self._json(
                        200,
                        {"items": control_plane.audit.list(limit=_query_int(query, "limit", 200))},
                    )
                else:
                    self._json(404, {"error": {"code": "NOT_FOUND"}})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            route = urlparse(self.path)
            if not self._host_allowed() or not self._origin_allowed(expected_origin):
                self._json(403, {"error": {"code": "INVALID_REQUEST_ORIGIN"}})
                return
            if route.path == "/api/login":
                self._login(limiter)
                return
            actor = self._authenticate(require_csrf=True)
            if actor is None:
                return
            if actor["must_change_password"] and route.path not in {"/api/account/password", "/api/logout"}:
                self._json(403, {"error": {"code": "PASSWORD_CHANGE_REQUIRED", "message": "Change the bootstrap password before continuing."}})
                return
            try:
                body = self._read_json()
                if route.path == "/api/logout":
                    control_plane.admin_sessions.revoke(self._session_token())
                    control_plane.audit.append(
                        actor=actor,
                        action="admin.logout",
                        result="succeeded",
                        request_ip=self.client_address[0],
                    )
                    self._json(200, {"status": "signed_out"}, clear_cookies=True)
                    return
                if route.path == "/api/account/password":
                    account = control_plane.accounts.change_password(
                        account_id=actor["account_id"],
                        current_password=_required_string(body, "current_password"),
                        new_password=_required_string(body, "new_password"),
                    )
                    control_plane.admin_sessions.revoke_account_sessions(
                        actor["account_id"], except_session_id=actor["session_id"]
                    )
                    control_plane.audit.append(
                        actor=actor,
                        action="admin.password.change",
                        target_type="admin_account",
                        target_id=actor["account_id"],
                        result="succeeded",
                        request_ip=self.client_address[0],
                        after={"password_changed_at": account["password_changed_at"]},
                    )
                    self._json(200, {"account": account})
                    return
                if route.path == "/api/admin-accounts":
                    account = control_plane.create_admin_account(
                        actor=actor,
                        request_ip=self.client_address[0],
                        username=_required_string(body, "username"),
                        role=_required_string(body, "role"),
                        reason=_required_string(body, "reason"),
                    )
                    self._json(201, account)
                    return
                if route.path == "/api/database-sources":
                    self._json(200,control_plane.save_database_source(actor=actor,request_ip=self.client_address[0],body=body))
                    return
                if route.path == "/api/database-grants":
                    if set(body)-{'user_subject','capabilities','expected_revision','reason','source_id'} or not {'user_subject','capabilities','expected_revision','reason'}.issubset(body):
                        raise ValueError('数据库授权请求字段无效')
                    result = control_plane.save_database_grants(
                        actor=actor, request_ip=self.client_address[0],
                        user_subject=_required_string(body, 'user_subject'), capabilities=body['capabilities'],
                        expected_revision=body['expected_revision'], reason=_required_string(body, 'reason'), source_id=body.get('source_id','taihua_primary'))
                    self._json(200, result)
                    return
                if route.path == "/api/skill-reviews":
                    self._json(200, control_plane.decide_skill(actor=actor, request_ip=self.client_address[0], body=body))
                    return
                if route.path == "/api/skills":
                    if set(body) != {"user_subject", "value", "expected_revision", "reason"}:
                        raise ValueError("invalid Skill configuration fields")
                    self._json(200, control_plane.save_skill_config(actor=actor, request_ip=self.client_address[0],
                        user_subject=body["user_subject"], value=body["value"], expected_revision=body["expected_revision"], reason=body["reason"]))
                    return
                if route.path == "/api/user-grants":
                    if set(body) != {"user_subject", "permissions", "expected_revision", "reason"}:
                        raise ValueError("invalid user grant fields")
                    result = control_plane.save_user_grants(
                        actor=actor, request_ip=self.client_address[0],
                        user_subject=_required_string(body, "user_subject"),
                        permissions=body["permissions"],
                        expected_revision=body["expected_revision"],
                        reason=_required_string(body, "reason"),
                    )
                    self._json(200, result)
                    return
                if route.path == "/api/tokens":
                    issued = control_plane.issue_token(
                        actor=actor,
                        request_ip=self.client_address[0],
                        user_subject=_required_string(body, "user_subject"),
                        expected_principal_ref=_optional_string(body, "expected_principal_ref"),
                        principal_bindings=_optional_string_map(body, "principal_bindings"),
                        label=_optional_string(body, "label"),
                        scopes=_required_string_list(body, "scopes") if body.get("scopes") else [],
                        ttl_hours=int(body.get("ttl_hours") or 0),
                        reason=_required_string(body, "reason"),
                    )
                    self._json(201, issued)
                    return
                token_renew_match = re.fullmatch(r"/api/tokens/([0-9a-f-]{36})/renew", route.path)
                if token_renew_match:
                    if set(body) != {"new_expires_at", "expected_revision", "request_id", "reason"}:
                        raise ValueError("invalid token renewal fields")
                    renewed = control_plane.renew_token(
                        actor=actor, request_ip=self.client_address[0],
                        token_id=token_renew_match.group(1),
                        new_expires_at=_required_string(body, "new_expires_at"),
                        expected_revision=body["expected_revision"],
                        request_id=_required_string(body, "request_id"),
                        reason=_required_string(body, "reason"),
                    )
                    self._json(200, renewed)
                    return
                token_match = re.fullmatch(r"/api/tokens/([0-9a-f-]{36})/revoke", route.path)
                if token_match:
                    token = control_plane.revoke_token(
                        actor=actor,
                        request_ip=self.client_address[0],
                        token_id=token_match.group(1),
                        reason=_required_string(body, "reason"),
                    )
                    self._json(200, token)
                    return
                session_match = re.fullmatch(
                    r"/api/sessions/([0-9a-f-]{36})/(invalidate|check|rebind)", route.path
                )
                if session_match:
                    action = session_match.group(2)
                    kwargs = {
                        "actor": actor,
                        "request_ip": self.client_address[0],
                        "session_id": session_match.group(1),
                        "reason": _required_string(body, "reason"),
                    }
                    if action == "invalidate":
                        result = control_plane.invalidate_session(**kwargs)
                    elif action == "check":
                        result = control_plane.inspect_session(**kwargs)
                    else:
                        result = control_plane.rebind_session_principal(
                            **kwargs,
                            expected_principal_ref=_required_string(
                                body, "expected_principal_ref"
                            ),
                        )
                    self._json(200, result)
                    return
                if route.path == "/api/policies/pause":
                    policy = control_plane.pause_policy(
                        actor=actor,
                        request_ip=self.client_address[0],
                        scope_type=_required_string(body, "scope_type"),
                        scope_value=str(body.get("scope_value") or "*"),
                        capability_version=str(body.get("capability_version") or "*"),
                        reason=_required_string(body, "reason"),
                    )
                    self._json(201, policy)
                    return
                incident_match = re.fullmatch(
                    r"/api/incidents/([0-9a-f-]{36})/"
                    r"(acknowledge|investigate|resolve|suppress)",
                    route.path,
                )
                if incident_match:
                    state = {
                        "acknowledge": "acknowledged",
                        "investigate": "investigating",
                        "resolve": "resolved",
                        "suppress": "suppressed",
                    }[incident_match.group(2)]
                    result = control_plane.transition_runtime_incident(
                        actor=actor,
                        request_ip=self.client_address[0],
                        incident_id=incident_match.group(1),
                        state=state,
                        reason=_required_string(body, "reason"),
                    )
                    self._json(200, result)
                    return
                if route.path == "/api/recovery":
                    result = control_plane.runtime_recovery_action(
                        actor=actor,
                        request_ip=self.client_address[0],
                        action_type=_required_string(body, "action_type"),
                        target_id=_required_string(body, "target_id"),
                        reason=_required_string(body, "reason"),
                        idempotency_key=_required_string(body, "idempotency_key"),
                    )
                    self._json(200, result)
                    return
                policy_match = re.fullmatch(
                    r"/api/policies/([0-9a-f-]{36})/resume", route.path
                )
                if policy_match:
                    policy = control_plane.resume_policy(
                        actor=actor,
                        request_ip=self.client_address[0],
                        policy_id=policy_match.group(1),
                        reason=_required_string(body, "reason"),
                    )
                    self._json(200, policy)
                    return
                self._json(404, {"error": {"code": "NOT_FOUND"}})
            except Exception as exc:
                try:
                    control_plane.audit.append(
                        actor=actor,
                        action="admin.request",
                        target_type="admin_api",
                        target_id=route.path,
                        request_ip=self.client_address[0],
                        result="failed",
                        error=str(exc),
                    )
                except Exception:
                    pass
                self._handle_error(exc)

        def do_OPTIONS(self) -> None:
            self.send_response(405)
            self._security_headers()
            self.send_header("Allow", "GET, POST")
            self.end_headers()

        def _login(self, rate_limiter: LoginRateLimiter) -> None:
            key = self.client_address[0]
            if not rate_limiter.allowed(key):
                self._json(
                    429,
                    {"error": {"code": "LOGIN_RATE_LIMITED", "message": "Too many failed sign-in attempts."}},
                )
                return
            try:
                body = self._read_json()
                username = _required_string(body, "username")
                password = _required_string(body, "password")
            except Exception as exc:
                self._handle_error(exc)
                return
            account = control_plane.accounts.authenticate(username=username, password=password)
            if account is None:
                rate_limiter.fail(key)
                control_plane.audit.append(
                    actor=None,
                    action="admin.login",
                    target_type="admin_account",
                    target_id=username[:64],
                    request_ip=key,
                    result="failed",
                    error="invalid credentials",
                )
                self._json(
                    401,
                    {"error": {"code": "INVALID_CREDENTIALS", "message": "Username or password is incorrect."}},
                )
                return
            rate_limiter.success(key)
            account = control_plane.accounts.record_login(account["account_id"])
            session = control_plane.admin_sessions.create(
                account_id=account["account_id"],
                request_ip=key,
                user_agent=self.headers.get("User-Agent") or "",
            )
            actor = {**account, "session_id": session["session_id"]}
            control_plane.audit.append(
                actor=actor,
                action="admin.login",
                target_type="admin_account",
                target_id=account["account_id"],
                request_ip=key,
                result="succeeded",
            )
            self._json(
                200,
                {"account": account, "csrf_token": session["csrf_token"]},
                cookies=session,
            )

        def _authenticate(self, *, require_csrf: bool = False) -> dict | None:
            token = self._session_token()
            csrf = (self.headers.get("X-AgentBridge-CSRF") or "") if require_csrf else None
            actor = control_plane.admin_sessions.verify(token, csrf_token=csrf)
            if actor is None:
                self._json(
                    401,
                    {"error": {"code": "ADMIN_AUTH_REQUIRED", "message": "Administrator sign-in is required."}},
                    clear_cookies=True,
                )
                return None
            return actor

        def _session_token(self) -> str:
            return _cookie_value(self.headers.get("Cookie") or "", SESSION_COOKIE)

        def _read_json(self) -> dict:
            try:
                content_length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise ValueError("invalid request body length") from exc
            if content_length < 1 or content_length > MAX_ADMIN_BODY_BYTES:
                raise ValueError("request body is empty or too large")
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                raise ValueError("request body must be JSON")
            body = self.rfile.read(content_length)
            try:
                value = json.loads(body.decode("utf-8"))
            finally:
                body = b""
            if not isinstance(value, dict):
                raise ValueError("request body must be an object")
            return value

        def _host_allowed(self) -> bool:
            try:
                host = urlparse(f"//{self.headers.get('Host') or ''}").hostname
            except ValueError:
                return False
            return (host or "").lower() == allowed_host

        def _origin_allowed(self, origin: str) -> bool:
            request_origin = (self.headers.get("Origin") or "").strip().lower()
            fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            return request_origin == origin or (
                not request_origin and fetch_site in {"", "same-origin"}
            )

        def _static(self, filename: str) -> None:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
                self._json(404, {"error": {"code": "NOT_FOUND"}})
                return
            path = STATIC_ROOT / filename
            if not path.is_file():
                self._json(404, {"error": {"code": "NOT_FOUND"}})
                return
            body = path.read_bytes()
            self.send_response(200)
            self._security_headers()
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(
            self,
            status: int,
            value: Any,
            *,
            cookies: dict | None = None,
            clear_cookies: bool = False,
        ) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if cookies is not None:
                session_max_age = control_plane.admin_sessions.ttl_seconds
                self.send_header(
                    "Set-Cookie",
                    _session_cookie(
                        cookies["token"],
                        secure=config.secure_cookie,
                        max_age=session_max_age,
                    ),
                )
                self.send_header(
                    "Set-Cookie",
                    _csrf_cookie(
                        cookies["csrf_token"],
                        secure=config.secure_cookie,
                        max_age=session_max_age,
                    ),
                )
            elif clear_cookies:
                self.send_header("Set-Cookie", _session_cookie("", secure=config.secure_cookie, max_age=0))
                self.send_header("Set-Cookie", _csrf_cookie("", secure=config.secure_cookie, max_age=0))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
            )
            if config.secure_cookie:
                self.send_header("Strict-Transport-Security", "max-age=31536000")

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, TokenEditConflict):
                status, code = 409, "TOKEN_EDIT_CONFLICT"
            elif isinstance(exc, UserGrantConflict):
                status, code = 409, "USER_GRANT_CONFLICT"
            elif isinstance(exc, (DatabaseGrantConflict, SourceConflict)):
                status, code = 409, 'DATABASE_GRANT_CONFLICT'
            elif isinstance(exc, PermissionError):
                status, code = 403, "ADMIN_FORBIDDEN"
            elif isinstance(exc, KeyError):
                status, code = 404, "NOT_FOUND"
            elif isinstance(exc, (ValueError, TypeError)):
                status, code = 400, "INVALID_INPUT"
            else:
                status, code = 500, "ADMIN_INTERNAL_ERROR"
            self._json(
                status,
                {"error": {"code": code, "message": str(exc) if status < 500 else "The request failed."}},
            )

    server = AdminHTTPServer((config.host, config.port), AdminRequestHandler)
    if config.tls_cert is not None and config.tls_key is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config.tls_cert, config.tls_key)
        server.enable_tls(context)
    return server


def _cookie_value(value: str, name: str) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(value)
    except Exception:
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel is not None else ""


def _session_cookie(value: str, *, secure: bool, max_age: int) -> str:
    attributes = [
        f"{SESSION_COOKIE}={value}",
        "Path=/",
        f"Max-Age={max_age}",
        "HttpOnly",
        "SameSite=Strict",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


def _csrf_cookie(value: str, *, secure: bool, max_age: int) -> str:
    attributes = [
        f"{CSRF_COOKIE}={value}",
        "Path=/",
        f"Max-Age={max_age}",
        "SameSite=Strict",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


def _query_value(query: dict[str, list[str]], name: str) -> str | None:
    value = (query.get(name) or [""])[0].strip()
    return value or None


def _query_int(query: dict[str, list[str]], name: str, default: int) -> int:
    value = _query_value(query, name)
    return int(value) if value else default


def _required_string(body: dict, name: str) -> str:
    value = str(body.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _optional_string(body: dict, name: str) -> str | None:
    value = str(body.get(name) or "").strip()
    return value or None


def _optional_string_map(body: dict, name: str) -> dict[str, str]:
    value = body.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        normalized_key = str(key or "").strip()
        normalized_value = str(item or "").strip()
        if not normalized_key or not normalized_value:
            continue
        result[normalized_key] = normalized_value
    return result


def _required_string_list(body: dict, name: str) -> list[str]:
    value = body.get(name)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} is required")
    return [str(item).strip() for item in value]
