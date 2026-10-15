"""Continuous trusted pages over the existing field and authorization cards."""
from __future__ import annotations

from html import escape
import json
import re
import secrets

from agentbridge.auth.card import AuthCardResponse
from agentbridge.auth.business_draft_card import BusinessDraftApplication
from agentbridge.auth.field_card import _document, _security_headers
from agentbridge.core.field_submissions import FieldSubmissionIntegrityError
from agentbridge.core.interactions import InteractionIntegrityError
from agentbridge.core.tasks import TaskIntegrityError
from agentbridge.core.trusted_flows import TrustedFlowReader
from agentbridge.core.trusted_flows import TrustedFlowDenied
from agentbridge.core.write_authorizations import WriteAuthorizationIntegrityError


_UNAVAILABLE_ERRORS = (KeyError, ValueError, PermissionError, FieldSubmissionIntegrityError,
                       InteractionIntegrityError, TaskIntegrityError, WriteAuthorizationIntegrityError)


def flow_target(path: str) -> tuple[str, str, str | None, bool] | None:
    match = re.fullmatch(
        r"/flow/(input|authorize)/([A-Za-z0-9_-]{32,128})"
        r"(?:/present/([A-Za-z0-9_-]{32,128}))?(/status)?",
        path.split("?", 1)[0],
    )
    return (match[1], match[2], match[3], bool(match[4])) if match else None


class TrustedFlowApplication:
    def __init__(self, *, service, field_application, action_application, draft_application=None) -> None:
        self.reader = TrustedFlowReader(service)
        self.applications = {"input": field_application, "authorize": action_application}
        self.draft_application = draft_application or BusinessDraftApplication(service=service)

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
                 secure_cookie: bool, csrf_cookie: str) -> AuthCardResponse:
        try:
            context = self.reader.context(kind, resource_id, presentation_id)
            if self.draft_application.supports(context) and (
                context.resource["state"] == "pending"
                or self.draft_application.has_decision(resource_id)
            ):
                return self.draft_application.get_card(resource_id, presentation_id=presentation_id,
                    secure_cookie=secure_cookie, csrf_cookie=csrf_cookie)
            if context.resource["state"] == "pending":
                if not context.actionable:
                    raise TrustedFlowDenied("batch item is no longer actionable")
                response = self.applications[kind].get_card(
                    resource_id, presentation_id=presentation_id, secure_cookie=secure_cookie,
                )
                return self._adapt(response, kind, resource_id, presentation_id)
            try:
                self.reader.authenticate(context, token=csrf_cookie, cookie=csrf_cookie)
            except TrustedFlowDenied:
                token, ttl = self.reader.issue_read_session(context)
                response = self._waiting(kind, resource_id, presentation_id, token)
                path = f"/flow/{kind}/{resource_id}" + (f"/present/{presentation_id}" if presentation_id else "")
                response.headers["Set-Cookie"] = (
                    f"agentbridge_csrf={token}; Path={path}; HttpOnly; SameSite=Strict; Max-Age={ttl}"
                    + ("; Secure" if secure_cookie else "")
                )
                return response
            return self._waiting(kind, resource_id, presentation_id, csrf_cookie)
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
            response = self.applications[kind].submit_card(
                resource_id, presentation_id=presentation_id, body=body,
                content_type=content_type, csrf_cookie=csrf_cookie,
            )
            if response.status == 200:
                return self._waiting(kind, resource_id, presentation_id, csrf_cookie)
            return self._adapt(response, kind, resource_id, presentation_id)
        except _UNAVAILABLE_ERRORS:
            return self._unavailable()

    def status(self, kind: str, resource_id: str, *, presentation_id: str | None,
               token: str, csrf_cookie: str) -> AuthCardResponse:
        try:
            context = self.reader.context(kind, resource_id, presentation_id)
            self.reader.authenticate(context, token=token, cookie=csrf_cookie)
            value = self.reader.status(context)
        except _UNAVAILABLE_ERRORS:
            return self._json(403, {"state": "unavailable", "terminal": True,
                                    "title": "办理页面不可用", "message": "请从原任务重新打开有效入口。"})
        return self._json(200, value)

    @staticmethod
    def _adapt(response: AuthCardResponse, kind: str, resource_id: str,
               presentation_id: str | None) -> AuthCardResponse:
        original = f"/{kind}/{resource_id}" + (f"/present/{presentation_id}" if presentation_id else "")
        continued = "/flow" + original
        headers = dict(response.headers)
        if "Set-Cookie" in headers:
            headers["Set-Cookie"] = headers["Set-Cookie"].replace(f"Path={original};", f"Path={continued};", 1)
        body = response.body.replace(f'action="{original}"'.encode(), f'action="{continued}"'.encode(), 1)
        return AuthCardResponse(response.status, headers, body)

    @staticmethod
    def _waiting(kind: str, resource_id: str, presentation_id: str | None,
                 token: str) -> AuthCardResponse:
        nonce = secrets.token_urlsafe(18)
        path = f"/flow/{kind}/{resource_id}" + (f"/present/{presentation_id}" if presentation_id else "")
        # All configuration values are opaque IDs, never business fields. Escape
        # '<' nonetheless, so the script cannot terminate via future callers.
        config = json.dumps({"statusUrl": path + "/status", "token": token}).replace("<", "\\u003c")
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
  <noscript><p>请启用 JavaScript 后刷新此页查看办理状态；页面不会自行提交业务。</p></noscript>
</section></main>
<script nonce="{escape(nonce)}">
(() => {{
  const config = {config};
  const title = document.getElementById('flow-title');
  const message = document.getElementById('flow-message');
  const recovery = document.getElementById('flow-recovery');
  const batch = document.getElementById('flow-batch');
  const continued = document.getElementById('flow-continue');
  let stopped = false;
  let failures = 0;
  async function poll() {{
    if (stopped) return;
    try {{
      const response = await fetch(config.statusUrl, {{
        credentials: 'same-origin', cache: 'no-store',
        headers: {{'X-AgentBridge-Flow-Token': config.token}}
      }});
      const state = await response.json();
      if (!response.ok && response.status !== 403) throw new Error('status unavailable');
      if (typeof state.title !== 'string' || typeof state.message !== 'string') throw new Error('invalid status');
      title.textContent = state.title;
      message.textContent = state.message;
      recovery.textContent = '';
      continued.hidden = true;
      continued.removeAttribute('href');
      batch.hidden = !state.batch;
      if (state.batch) {{
        batch.textContent = `本批次已完成 ${{state.batch.completedCount}} / ${{state.batch.totalCount}} 项`;
        if (state.batch.nextOrdinal) batch.textContent += `；下一项（第 ${{state.batch.nextOrdinal}} 项）：${{state.batch.nextTitle}}`;
      }}
      failures = 0;
      if (state.nextUrl) {{
        const next = new URL(state.nextUrl, location.origin);
        if (next.origin !== location.origin || next.username || next.password || !/^\/flow\/authorize\/[A-Za-z0-9_-]{{32,128}}(?:\/present\/[A-Za-z0-9_-]{{32,128}})?$/.test(next.pathname) || next.search || next.hash) throw new Error('invalid continuation');
        stopped = true;
        // Keep the original Telegram lifecycle fragment on same-origin navigation.
        next.hash = location.hash;
        location.replace(next.href);
        return;
      }}
      if (state.continueUrl && state.state === 'completed') {{
        const next = new URL(state.continueUrl, location.origin);
        if (next.origin !== location.origin || next.username || next.password || !/^\/flow\/input\/[A-Za-z0-9_-]{{32,128}}(?:\/present\/[A-Za-z0-9_-]{{32,128}})?$/.test(next.pathname) || next.search || next.hash) throw new Error('invalid batch continuation');
        next.hash = location.hash;
        continued.href = next.href;
        continued.hidden = false;
      }}
      stopped = state.terminal === true;
    }} catch (_error) {{
      failures += 1;
      recovery.textContent = '连接暂不可用，正在等待恢复。已提交内容不会重发。';
    }}
    if (!stopped) window.setTimeout(poll, Math.min(10000, 2000 * (failures + 1)));
  }}
  poll();
}})();
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
        return self.applications["input"]._message_response(
            status=403, title="办理页面不可用", message="请从原任务重新打开有效入口。", tone="error",
        )
