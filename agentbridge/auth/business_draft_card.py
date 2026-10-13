"""A trusted editing page for the task-bound Taihua work-log draft."""
from __future__ import annotations

from html import escape
import json
from pathlib import Path
import re
import secrets

from agentbridge.auth.card import AuthCardResponse, MAX_AUTH_BODY_BYTES
from agentbridge.auth.field_card import _document, _render_control, _security_headers
from agentbridge.adapters.taihua import TAIHUA_WORK_LOG_FIELD_CARD_SCHEMA


FIELDS = ("log_date", "hours", "project", "content")
_SCRIPT = Path(__file__).with_name("static").joinpath("business_draft.mjs")


def business_draft_target(path: str) -> tuple[str, str | None, str] | None:
    match = re.fullmatch(
        r"/flow/input/([A-Za-z0-9_-]{32,128})(?:/present/([A-Za-z0-9_-]{32,128}))?"
        r"/draft/(state|values|preview|approve)", path.split("?", 1)[0],
    )
    return (match[1], match[2], match[3]) if match else None


class BusinessDraftApplication:
    def __init__(self, *, service=None, draft_service=None) -> None:
        self.service = service
        self._backend = draft_service

    @property
    def backend(self):
        if self._backend is None:
            from agentbridge.core.business_drafts import BusinessDraftService
            self._backend = BusinessDraftService(self.service)
        return self._backend

    @staticmethod
    def supports(context) -> bool:
        schema = context.resource.get("form_schema") or {}
        return (context.kind == "input" and bool(context.task_id)
                and context.resource.get("capability_name") == "taihua.work_log.create.prepare"
                and schema.get("_agentbridge_business_draft") == "v1"
                and schema.get("schema_version") == "agentbridge.taihua_work_log_fields.v1"
                and {field.get("name") for field in schema.get("fields", [])} == set(FIELDS))

    def has_decision(self, source_submission_id: str) -> bool:
        return self.backend.has_decision(source_submission_id=source_submission_id)

    def get_card(self, source_submission_id: str, *, presentation_id: str | None,
                 csrf_cookie: str, secure_cookie: bool) -> AuthCardResponse:
        try:
            opened = self.backend.open(source_submission_id=source_submission_id,
                presentation_id=presentation_id, csrf_cookie=csrf_cookie)
            state, token = opened["state"], opened["csrfToken"]
            path = _path(source_submission_id, presentation_id)
            nonce = secrets.token_urlsafe(18)
            headers = _security_headers(nonce)
            headers["Content-Security-Policy"] += "; connect-src 'self'"
            headers["Set-Cookie"] = (
                f"agentbridge_csrf={token}; Path={path}; HttpOnly; SameSite=Strict; Max-Age={opened['maxAge']}"
                + ("; Secure" if secure_cookie else "")
            )
            return AuthCardResponse(200, headers, _render_page(state, token, path, nonce).encode())
        except Exception as error:
            response = self._error(error)
            nonce = secrets.token_urlsafe(18)
            message = json.loads(response.body)["error"]["message"]
            return AuthCardResponse(response.status, _security_headers(nonce), _document(
                title="办理页面不可用", nonce=nonce,
                body='<main class="shell"><section class="card status-card"><h1>办理页面不可用</h1>'
                     f'<p class="status-copy">{escape(message)}</p></section></main>',
            ).encode())

    def state(self, source_submission_id: str, *, presentation_id: str | None,
              token: str, csrf_cookie: str) -> AuthCardResponse:
        try:
            value = self.backend.state(source_submission_id=source_submission_id,
                presentation_id=presentation_id, csrf_token=token, csrf_cookie=csrf_cookie)
            return self.json(200, value)
        except Exception as error:
            return self._error(error)

    def mutate(self, source_submission_id: str, *, presentation_id: str | None,
               action: str, token: str, csrf_cookie: str, content_type: str,
               body: bytes) -> AuthCardResponse:
        if len(body) > MAX_AUTH_BODY_BYTES:
            return self.json(413, {"error": {"code": "BODY_TOO_LARGE", "message": "填写内容过大。"}})
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            return self.json(415, {"error": {"code": "CONTENT_TYPE", "message": "请求格式不支持。"}})
        try:
            payload = json.loads(body)
            expected_keys = {
                "values": {"expectedRevision", "values"},
                "preview": {"expectedRevision"},
                "approve": {"expectedRevision", "previewId", "requestKey"},
            }.get(action)
            if (not isinstance(payload, dict) or expected_keys is None or set(payload) != expected_keys
                    or type(payload["expectedRevision"]) is not int or payload["expectedRevision"] < 0):
                raise ValueError("invalid request")
            arguments = dict(source_submission_id=source_submission_id, presentation_id=presentation_id,
                csrf_token=token, csrf_cookie=csrf_cookie, expected_revision=payload["expectedRevision"])
            if action == "values":
                if (not isinstance(payload["values"], dict) or set(payload["values"]) != set(FIELDS)
                        or any(not isinstance(value, str) for value in payload["values"].values())):
                    raise ValueError("invalid values")
                value = self.backend.save(**arguments, values=payload["values"])
            elif action == "preview":
                value = self.backend.preview(**arguments)
            else:
                if not all(isinstance(payload[key], str) and 1 <= len(payload[key]) <= 128
                           for key in ("previewId", "requestKey")):
                    raise ValueError("invalid decision")
                value = self.backend.approve(**arguments,
                    preview_id=payload["previewId"], request_key=payload["requestKey"])
            return self.json(200, value)
        except Exception as error:
            return self._error(error)

    @staticmethod
    def json(status: int, value: dict) -> AuthCardResponse:
        headers = _security_headers(secrets.token_urlsafe(18))
        headers["Content-Type"] = "application/json; charset=utf-8"
        return AuthCardResponse(status, headers, json.dumps(value, ensure_ascii=False).encode())

    def _error(self, error: Exception) -> AuthCardResponse:
        # Only the backend's intentional user-facing error contract may expose text.
        from agentbridge.core.business_drafts import BusinessDraftError
        if isinstance(error, BusinessDraftError):
            value = {"error": {"code": error.code, "message": error.message,
                               "fieldErrors": error.field_errors}}
            if error.current is not None:
                value["current"] = error.current
            return self.json(error.status, value)
        if isinstance(error, (ValueError, UnicodeDecodeError)):
            return self.json(400, {"error": {"code": "INVALID_REQUEST", "message": "请求字段无效，请检查后重试。"}})
        if isinstance(error, (KeyError, PermissionError)):
            return self.json(403, {"error": {"code": "DRAFT_UNAVAILABLE", "message": "请从原任务重新打开有效办理入口。"}})
        return self.json(503, {"error": {"code": "TEMPORARILY_UNAVAILABLE", "message": "暂时无法读取办理状态，请保留本页后重试。"}})


def _path(source_submission_id: str, presentation_id: str | None) -> str:
    return f"/flow/input/{source_submission_id}" + (f"/present/{presentation_id}" if presentation_id else "")


def _render_page(state: dict, token: str, path: str, nonce: str) -> str:
    definitions = TAIHUA_WORK_LOG_FIELD_CARD_SCHEMA["fields"]
    controls = []
    differences = []
    for field in definitions:
        name = field["name"]
        value = str(state["values"].get(name) or "")
        control = _render_control(field, value)
        control = control.replace(f'name="{name}"', f'name="{name}" aria-describedby="error-{name}"', 1)
        controls.append(control.replace("</div>", f'<p class="field-error" id="error-{name}" aria-live="polite"></p></div>', 1))
        differences.append(f'<div class="draft-difference" id="conflict-row-{name}"><h3>{escape(field["label"])}</h3>'
            f'<div><p>本页输入</p><pre id="conflict-local-{name}"></pre></div>'
            f'<div><p>另一页面的版本</p><pre id="conflict-server-{name}"></pre></div></div>')
    config = json.dumps({"state": state, "csrfToken": token, "apiUrl": path + "/draft"},
                        ensure_ascii=False).replace("<", "\\u003c")
    body = f"""
<style nonce="{nonce}">
  [hidden] {{display:none!important}} .draft-card {{width:760px}} .draft-lead {{color:var(--muted);font-size:14px;line-height:1.6}}
  .draft-card button:disabled {{opacity:.5;cursor:not-allowed}} .draft-card input:disabled,.draft-card textarea:disabled {{background:#f3f5f2;color:#62706a}}
  .draft-card .field:has(#project) {{grid-column:1/-1}} .draft-status {{margin:18px 0 10px;color:var(--teal-dark);font-size:13px;line-height:1.6}}
  .draft-review {{margin:20px 0;background:#f1f8f5;border:1px solid #c4ded4;border-radius:6px;padding:18px}}
  .draft-review h2,.draft-conflict h2 {{font-size:17px;margin:0 0 12px}} .draft-review dl {{margin:0;display:grid;grid-template-columns:110px 1fr;gap:10px}}
  .draft-review dt {{color:var(--muted);font-size:13px}} .draft-review dd {{margin:0;overflow-wrap:anywhere;font-size:14px;line-height:1.6;white-space:pre-wrap}}
  .draft-review details {{margin-top:12px;font-size:13px}} .draft-review pre,.draft-difference pre {{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.6;margin:6px 0 0}}
  .draft-conflict {{padding:18px;margin:18px 0;border:1px solid #dcc69f;background:#fff8ea;border-radius:6px}}
  .draft-conflict>p {{font-size:13px;line-height:1.6}} .draft-difference {{display:grid;grid-template-columns:1fr 1fr;gap:12px;border-top:1px solid #dcc69f;padding:12px 0}}
  .draft-difference h3 {{grid-column:1/-1;margin:0;font-size:13px}} .draft-difference p {{color:var(--muted);margin:0;font-size:12px}}
  .draft-difference pre {{max-height:160px;overflow:auto;font-size:13px}} .draft-actions {{display:flex;gap:10px;flex-wrap:wrap}}
  .draft-actions button {{width:auto;flex:1;font-size:13px}} .draft-card button.secondary {{background:#edf1ee;color:var(--ink);border:1px solid var(--line)}}
  .field-error {{margin:5px 0 0;color:var(--red);font-size:12px}} #draft-retry {{margin-top:10px}}
  @media(max-width:620px) {{.draft-review dl {{grid-template-columns:85px 1fr}} .draft-difference {{grid-template-columns:1fr}}}}
</style>
<main class="shell"><section class="card draft-card" aria-labelledby="card-title">
  <header class="card-header"><div class="brand-mark" aria-hidden="true"><span></span></div><div>
    <p class="eyebrow">AGENTBRIDGE · 可信办理</p><h1 id="card-title">提交工作日志</h1><p class="system-name">日志系统</p>
  </div></header>
  <p class="draft-lead">填写后会自动解析项目并检查重复日志。核对下方结果，再确认正式提交。</p>
  <form id="draft-form" method="post" action="{escape(path)}"><div class="form-grid">{''.join(controls)}</div>
    <p id="draft-status" class="draft-status" role="status" aria-live="polite">正在读取草稿</p>
    <p id="draft-error" class="form-error" role="alert" hidden></p>
    <section id="draft-conflict" class="draft-conflict" aria-labelledby="conflict-title" hidden>
      <h2 id="conflict-title">另一页面更新了这份草稿</h2><p>本页输入已保留。请核对以下差异，再选择要继续使用的内容。</p>
      {''.join(differences)}<div class="draft-actions">
        <button id="draft-use-server" type="button" class="secondary">采用另一页面的版本</button>
        <button id="draft-keep-local" type="button" class="secondary">按本页内容更新草稿</button>
      </div>
    </section>
    <section id="draft-review" class="draft-review" aria-labelledby="review-title" hidden>
      <h2 id="review-title">核对本次提交</h2><dl>
        <dt>实际项目</dt><dd id="review-project"></dd><dt>项目编码</dt><dd id="review-project-code"></dd>
        <dt>重复检查</dt><dd id="review-duplicate"></dd><dt>执行后果</dt><dd id="review-effect"></dd>
      </dl><details><summary>查看将提交的日志正文</summary><pre id="review-content"></pre></details>
    </section>
    <button id="draft-submit" type="submit" disabled>确认并提交日志</button>
    <button id="draft-retry" type="button" class="secondary" hidden>重新检查</button>
  </form>
  <footer>草稿仅保存在可信办理服务中；确认前不会创建日志。页面到期后，请从原任务重新打开。</footer>
  <noscript><p class="notice">此办理页需要 JavaScript。请启用后刷新；页面不会自动提交日志。</p></noscript>
</section></main>
<script type="module" nonce="{nonce}">
{_SCRIPT.read_text(encoding='utf-8')}
mountBusinessDraft({config}, document, window);
</script>"""
    return _document(title="提交工作日志", nonce=nonce, body=body, close_when_complete=False)
