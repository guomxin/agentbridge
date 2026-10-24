"""Continuous trusted pages over the existing field and authorization cards."""
from __future__ import annotations

from html import escape
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from agentbridge.auth.card import AuthCardResponse
from agentbridge.auth.business_draft_card import BusinessDraftApplication
from agentbridge.auth.field_card import _document, _security_headers
from agentbridge.core.field_submissions import FieldSubmissionIntegrityError
from agentbridge.core.interactions import InteractionIntegrityError
from agentbridge.core.tasks import TaskIntegrityError
from agentbridge.core.trusted_flows import TrustedFlowReader
from agentbridge.core.trusted_flows import TrustedFlowDenied
from agentbridge.core.write_authorizations import WriteAuthorizationIntegrityError
from agentbridge.core.commit_notifications import current_version, wait_for_change


_UNAVAILABLE_ERRORS = (KeyError, ValueError, PermissionError, FieldSubmissionIntegrityError,
                       InteractionIntegrityError, TaskIntegrityError, WriteAuthorizationIntegrityError)
_WAITING_SCRIPT = Path(__file__).with_name("static").joinpath("flow_waiting.mjs")
_MAX_STATUS_WAIT_SECONDS = 20
_STATUS_RECHECK_SECONDS = 2
_PEER_CHECK_SECONDS = .2


def flow_target(path: str) -> tuple[str, str, str | None, bool] | None:
    match = re.fullmatch(
        r"/flow/(input|authorize)/([A-Za-z0-9_-]{32,128})"
        r"(?:/present/([A-Za-z0-9_-]{32,128}))?(/status)?",
        path.split("?", 1)[0],
    )
    return (match[1], match[2], match[3], bool(match[4])) if match else None


class TrustedFlowApplication:
    def __init__(self, *, service, field_application, action_application, draft_application=None,
                 workspace_base_url: str | None = None) -> None:
        self.reader = TrustedFlowReader(service)
        self.applications = {"input": field_application, "authorize": action_application}
        self.draft_application = draft_application or BusinessDraftApplication(service=service)
        # This is deployment configuration, never a request parameter, referrer,
        # endpoint route or stored business field. The link contains no capability.
        self.workspace_root = _workspace_root(workspace_base_url)
        self._status_secret = secrets.token_bytes(32)
        self._wait_lock = threading.Lock()
        self._wait_owners = {}
        self._wait_count = 0
        self._max_waits = 32
        self._max_waits_per_owner = 4

    def redirect_registered_input(self, resource_id: str, *, presentation_id: str | None) -> AuthCardResponse | None:
        """Unify new registered sources without rewriting immutable legacy forms."""
        try:
            context = self.reader.context("input", resource_id, presentation_id)
            if not self.draft_application.supports(context):
                return None
        except _UNAVAILABLE_ERRORS:
            return None
        path = f"/flow/input/{resource_id}" + (f"/present/{presentation_id}" if presentation_id else "")
        headers = _security_headers(secrets.token_urlsafe(18))
        # A Location without a fragment preserves the original host lifecycle
        # fragment in the browser. The destination issues its own scoped CSRF.
        headers["Location"] = path
        return AuthCardResponse(303, headers, b"")

    def get_card(self, kind: str, resource_id: str, *, presentation_id: str | None,
                 secure_cookie: bool, csrf_cookie: str, result_token: str | None = None) -> AuthCardResponse:
        try:
            context = self.reader.context(kind, resource_id, presentation_id)
            navigation = self._workspace_navigation(context)
            if result_token is not None:
                from agentbridge.core.telegram_result_views import ResultViewService
                navigation = {"back": ResultViewService(self.reader.service).navigation_back(result_token,
                    task_id=context.task_id, interaction_id=context.interaction["interaction_id"],
                    endpoint_id=context.endpoint_id, presentation_id=context.presentation_id)}
            if self.draft_application.supports(context) and (
                context.resource["state"] == "pending"
                or self.draft_application.has_decision(resource_id)
            ):
                response = self.draft_application.get_card(resource_id, presentation_id=presentation_id,
                    secure_cookie=secure_cookie, csrf_cookie=csrf_cookie, navigation=navigation)
                return self._with_back(response, self._workspace_home()) if response.status >= 400 else response
            if context.resource["state"] == "pending":
                if not context.actionable:
                    raise TrustedFlowDenied("batch item is no longer actionable")
                response = self.applications[kind].get_card(
                    resource_id, presentation_id=presentation_id, secure_cookie=secure_cookie,
                )
                return self._adapt(response, kind, resource_id, presentation_id, navigation=navigation)
            try:
                self.reader.authenticate(context, token=csrf_cookie, cookie=csrf_cookie)
            except TrustedFlowDenied:
                token, ttl = self.reader.issue_read_session(context)
                response = self._waiting(kind, resource_id, presentation_id, token, navigation=navigation)
                path = f"/flow/{kind}/{resource_id}" + (f"/present/{presentation_id}" if presentation_id else "")
                response.headers["Set-Cookie"] = (
                    f"agentbridge_csrf={token}; Path={path}; HttpOnly; SameSite=Strict; Max-Age={ttl}"
                    + ("; Secure" if secure_cookie else "")
                )
                return response
            return self._waiting(kind, resource_id, presentation_id, csrf_cookie, navigation=navigation)
        except _UNAVAILABLE_ERRORS:
            return self._unavailable()

    def submit_card(self, kind: str, resource_id: str, *, presentation_id: str | None,
                    body: bytes, content_type: str, csrf_cookie: str) -> AuthCardResponse:
        try:
            # Preserve task/user/presentation restrictions before the existing
            # card endpoint performs its independent CSRF and decision checks.
            context = self.reader.context(kind, resource_id, presentation_id)
            if not context.actionable:
                raise TrustedFlowDenied("batch item is no longer actionable")
            self.reader.authenticate(context, token=csrf_cookie, cookie=csrf_cookie)
            navigation = self._workspace_navigation(context)
            response = self.applications[kind].submit_card(
                resource_id, presentation_id=presentation_id, body=body,
                content_type=content_type, csrf_cookie=csrf_cookie,
            )
            if response.status == 200:
                return self._waiting(kind, resource_id, presentation_id, csrf_cookie, navigation=navigation)
            return self._adapt(response, kind, resource_id, presentation_id, navigation=navigation)
        except _UNAVAILABLE_ERRORS:
            return self._unavailable()

    def status(self, kind: str, resource_id: str, *, presentation_id: str | None,
               token: str, csrf_cookie: str, after: str = "", wait: str | float = 0,
               cancelled=None, request_allowed=None) -> AuthCardResponse | None:
        """Wait only for an authenticated read projection; never resume work.

        Capture the commit hint before each fresh durable snapshot. A commit
        between the snapshot and wait therefore cannot be lost. Other processes
        and missed hints are recovered by the bounded durable recheck.
        """
        admitted_owner = None
        try:
            def authenticate():
                if request_allowed is not None and not request_allowed():
                    raise TrustedFlowDenied("flow request origin is unavailable")
                context = self.reader.context(kind, resource_id, presentation_id)
                self.reader.authenticate(context, token=token, cookie=csrf_cookie)
                return context

            initial = authenticate()  # Authorization precedes parameter/capacity admission.
            owner = initial.resource["user_subject"]
            binding = self._status_binding(initial, resource_id)
            try:
                duration = float(wait)
            except (TypeError, ValueError):
                duration = -1
            if (not math.isfinite(duration) or not 0 <= duration <= _MAX_STATUS_WAIT_SECONDS
                    or not isinstance(after, str) or (after and not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", after))):
                return self._json(400, {"state": "unavailable", "terminal": True,
                    "title": "状态请求无效", "message": "请刷新可信办理页面。"})
            deadline = time.monotonic() + duration
            namespace = self.reader.service.tasks.db_path

            def snapshot():
                version = current_version(namespace, user_subject=owner, topic="timeline")
                context = authenticate()
                if self._status_binding(context, resource_id) != binding:
                    raise TrustedFlowDenied("flow read binding changed")
                value = self.reader.status(context)
                signature = hmac.new(self._status_secret, json.dumps(
                    [binding, hashlib.sha256(token.encode()).hexdigest(), value],
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode(), hashlib.sha256).hexdigest()
                return version, {**value, "stateVersion": signature}

            def respond(value):
                # Recheck the original credential, owner and origin immediately
                # before returning, including heartbeat and terminal responses.
                if self._status_binding(authenticate(), resource_id) != binding:
                    raise TrustedFlowDenied("flow read binding changed")
                return self._json(200, value)

            version, value = snapshot()
            if not after or not duration or value["stateVersion"] != after or value.get("terminal") or value.get("nextUrl"):
                return respond(value)
            with self._wait_lock:
                if (self._wait_count >= self._max_waits
                        or self._wait_owners.get(owner, 0) >= self._max_waits_per_owner):
                    busy = True
                else:
                    busy = False
                    self._wait_count += 1
                    self._wait_owners[owner] = self._wait_owners.get(owner, 0) + 1
                    admitted_owner = owner
            if busy:
                if self._status_binding(authenticate(), resource_id) != binding:
                    raise TrustedFlowDenied("flow read binding changed")
                response = self._json(429, {"state": "busy", "terminal": False,
                    "title": "正在等待办理状态", "message": "状态读取繁忙，请稍后重试。"})
                response.headers["Retry-After"] = "1"
                return response
            next_recheck = time.monotonic() + _STATUS_RECHECK_SECONDS
            while True:
                if cancelled is not None and cancelled():
                    return None
                now = time.monotonic()
                if now >= deadline:
                    return respond(snapshot()[1])
                changed = current_version(namespace, user_subject=owner, topic="timeline") != version
                if changed or now >= next_recheck:
                    version, value = snapshot()
                    if value["stateVersion"] != after or value.get("terminal") or value.get("nextUrl"):
                        return respond(value)
                    next_recheck = time.monotonic() + _STATUS_RECHECK_SECONDS
                wait_for_change(namespace, user_subject=owner, topic="timeline", after_version=version,
                                timeout=min(_PEER_CHECK_SECONDS, deadline - now, max(0, next_recheck - now)))
        except _UNAVAILABLE_ERRORS:
            return self._json(403, {"state": "unavailable", "terminal": True,
                                    "title": "办理页面不可用", "message": "请从原任务重新打开有效入口。"})
        finally:
            if admitted_owner is not None:
                with self._wait_lock:
                    self._wait_count -= 1
                    remaining = self._wait_owners[admitted_owner] - 1
                    if remaining:
                        self._wait_owners[admitted_owner] = remaining
                    else:
                        del self._wait_owners[admitted_owner]

    @staticmethod
    def _status_binding(context, resource_id):
        return (context.kind, resource_id, context.resource["user_subject"], context.resource["session_id"],
                context.task_id, context.interaction["interaction_id"], context.presentation_id, context.endpoint_id)

    @staticmethod
    def _adapt(response: AuthCardResponse, kind: str, resource_id: str,
               presentation_id: str | None, *, navigation: dict | None = None) -> AuthCardResponse:
        original = f"/{kind}/{resource_id}" + (f"/present/{presentation_id}" if presentation_id else "")
        continued = "/flow" + original
        headers = dict(response.headers)
        if "Set-Cookie" in headers:
            headers["Set-Cookie"] = headers["Set-Cookie"].replace(f"Path={original};", f"Path={continued};", 1)
        body = response.body.replace(f'action="{original}"'.encode(), f'action="{continued}"'.encode(), 1)
        return TrustedFlowApplication._with_back(AuthCardResponse(response.status, headers, body), navigation)

    def _workspace_home(self) -> dict | None:
        return ({"back": {"kind": "workspace", "url": self.workspace_root, "label": "返回 Workspace"}}
                if self.workspace_root else None)

    def _workspace_navigation(self, context) -> dict | None:
        """Read the exact presentation's currently registered Workspace binding.

        A task can have presentations in several clients. Its origin, the user's
        other Workspace account, and a web-looking endpoint key are not proof
        that this presentation was opened in Workspace.
        """
        if not self.workspace_root or not context.presentation_id or not context.endpoint_id:
            return None
        with self.reader.service.workspace._connect() as connection:
            row = connection.execute(
                "SELECT a.account_id FROM workspace_accounts a JOIN client_endpoints e "
                "ON e.endpoint_id=a.endpoint_id AND e.user_subject=a.user_subject "
                "WHERE a.user_subject=? AND a.endpoint_id=? AND a.state='active' AND e.state='active' "
                "AND e.agent_host='openclaw' AND e.client_type IN ('web','webchat') "
                "AND e.account_id=a.account_id AND e.external_subject=a.account_id "
                "AND e.endpoint_key=a.endpoint_key AND e.conversation_ref=a.openclaw_session_key",
                (context.resource["user_subject"], context.endpoint_id),
            ).fetchone()
        return self._workspace_home() if row else None

    @staticmethod
    def _with_back(response: AuthCardResponse, navigation: dict | None) -> AuthCardResponse:
        if navigation is None or b'id="agentbridge-back"' in response.body:
            return response
        return AuthCardResponse(response.status, dict(response.headers), response.body.replace(
            b"</main>", (_back_link(navigation) + "</main>").encode(), 1))

    @staticmethod
    def _waiting(kind: str, resource_id: str, presentation_id: str | None,
                 token: str, *, navigation: dict | None = None) -> AuthCardResponse:
        nonce = secrets.token_urlsafe(18)
        path = f"/flow/{kind}/{resource_id}" + (f"/present/{presentation_id}" if presentation_id else "")
        # All configuration values are opaque IDs, never business fields. Escape
        # '<' nonetheless, so the script cannot terminate via future callers.
        config = json.dumps({"statusUrl": path + "/status", "token": token, "navigation": navigation}).replace("<", "\\u003c")
        back = _back_link(navigation)
        script = _WAITING_SCRIPT.read_text(encoding="utf-8").replace(
            "export function mountTrustedFlow", "function mountTrustedFlow", 1,
        )
        body = rf"""
<style nonce="{escape(nonce)}">
  #flow-continue {{display:inline-block;margin-top:12px;padding:12px 20px;border-radius:5px;background:var(--teal);color:#fff;font-size:14px;text-decoration:none}}
  #flow-continue[hidden] {{display:none}}
</style>
<main class="shell"><section class="card status-card" aria-labelledby="flow-title">
  <div class="status-mark" aria-hidden="true"></div>
  <p class="eyebrow">AGENTBRIDGE · 连续办理</p>
  <h1 id="flow-title">正在读取办理状态</h1>
  <p class="status-copy" id="flow-message" role="status" aria-live="polite">请保留此页，原任务将继续办理。</p>
  <p class="status-copy" id="flow-recovery"></p>
  <p class="status-copy" id="flow-batch" hidden></p>
  <a id="flow-continue" hidden>核对下一项</a>
  {back}
  <noscript><p>请启用 JavaScript 后刷新此页查看办理状态；页面不会自行提交业务。</p></noscript>
</section></main>
<script nonce="{escape(nonce)}">
{script}
mountTrustedFlow({config});
</script>"""
        headers = _security_headers(nonce)
        headers["Content-Security-Policy"] += "; connect-src 'self'"
        return AuthCardResponse(200, headers, _document(
            title="连续办理", nonce=nonce, body=body, close_when_complete=False,
        ).encode())

    @staticmethod
    def _json(status: int, value: dict) -> AuthCardResponse:
        headers = _security_headers(secrets.token_urlsafe(18))
        headers["Content-Type"] = "application/json; charset=utf-8"
        return AuthCardResponse(status, headers, json.dumps(value, ensure_ascii=False).encode())

    def _unavailable(self) -> AuthCardResponse:
        return self._with_back(self.applications["input"]._message_response(
            status=403, title="办理页面不可用", message="请从原任务重新打开有效入口。", tone="error",
        ), self._workspace_home())


def _workspace_root(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or any(character.isspace() or ord(character) < 32
                                                    for character in value) or "\\" in value:
        raise ValueError("Workspace return URL must be a configured http(s) root")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or "?" in value or "#" in value):
        raise ValueError("Workspace return URL must not contain credentials, query or fragment")
    parsed.port  # Reject malformed/out-of-range ports before creating any link.
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/") + "/", "", ""))


def _back_link(navigation: dict | None) -> str:
    if not navigation:
        return ""
    back = navigation["back"]
    return (f'<a id="agentbridge-back" href="{escape(back["url"])}" referrerpolicy="no-referrer">'
            f'{escape(back.get("label") or "返回待办列表")}</a>')
