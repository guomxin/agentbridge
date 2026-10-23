"""A source-bound selection page; selecting only queues governed preparation."""
from __future__ import annotations

import hashlib
import hmac
from html import escape
import json
from pathlib import Path
import re
import secrets

from agentbridge.auth.card import AuthCardResponse, MAX_AUTH_BODY_BYTES
from agentbridge.auth.field_card import _document, _security_headers


_SCRIPT = Path(__file__).with_name("static") / "result_view.mjs"


def result_view_target(path):
    match = re.fullmatch(r"/results/([A-Za-z0-9_-]{32,128})(?:/(state|prepare|refresh|open|recovery-probe|recovery-prepare|recovery-open))?", path.split("?", 1)[0])
    return (match[1], match[2] or "page") if match else None


def _proof(cookie, view_token):
    return hmac.new(cookie.encode(), view_token.encode(), hashlib.sha256).hexdigest()


class TrustedResultViewApplication:
    def __init__(self, *, service=None, backend=None):
        self.service = service
        self._backend = backend

    @property
    def backend(self):
        if self._backend is None:
            from agentbridge.core.telegram_result_views import ResultViewService
            self._backend = ResultViewService(self.service)
        return self._backend

    @staticmethod
    def json(status, value):
        return AuthCardResponse(status, {**_security_headers(secrets.token_urlsafe(18)),
            "Content-Type": "application/json; charset=utf-8"}, json.dumps(value, ensure_ascii=False).encode())

    def error(self, error, *, page=False):
        code = str(getattr(error, "code", "RESULT_VIEW_UNAVAILABLE"))
        status = getattr(error, "status", 409)
        if not isinstance(status, int) or not 400 <= status < 500:
            status = 409
        # Unexpected errors never reflect internals or bearer URLs into a page.
        message = str(error) if getattr(error, "code", None) else "办理入口暂不可用，请回到原聊天重新打开或查询。"
        if not page:
            return self.json(status, {"error": {"code": code, "message": message}})
        nonce = secrets.token_urlsafe(18)
        return AuthCardResponse(status, _security_headers(nonce), _document(
            title="待办入口不可用", nonce=nonce,
            body='<main class="shell"><section class="card"><h1>待办入口不可用</h1>'
                 f'<p>{escape(message)}</p><p>请回到原 Telegram 私聊继续。关闭页面不会取消任务。</p></section></main>',
        ).encode())

    def get_card(self, token, *, secure_cookie=False):
        try:
            state = self.backend.read(token)
            cookie = secrets.token_urlsafe(32)
            nonce = secrets.token_urlsafe(18)
            path = "/results/" + token
            data = json.dumps({"state": state, "csrfToken": _proof(cookie, token), "path": path}, ensure_ascii=False)
            data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            headers = _security_headers(nonce)
            headers["Content-Security-Policy"] += "; connect-src 'self'"
            headers["Set-Cookie"] = (
                f"agentbridge_result_csrf={cookie}; Path={path}; HttpOnly; SameSite=Strict; Max-Age=3600"
                + ("; Secure" if secure_cookie else "")
            )
            body = '''<main class="shell"><section class="card">
<p class="eyebrow">Telegram · 原查询结果</p><h1 id="result-title">选择待办并核对办理</h1>
<p>选择只会准备办理材料，不会授权或提交业务。请在后续专门界面逐项核对并确认。</p>
<p id="result-status" role="status" aria-live="polite"></p>
<div class="result-query-actions"><button id="result-refresh" type="button">查询最新待办</button>
<button id="result-query-recover" type="button" hidden>恢复原查询</button></div>
<p id="result-validity" class="muted"></p>
<div class="result-filters"><label for="result-search">搜索事项<input id="result-search" type="search" placeholder="标题、发起人或日期" autocomplete="off"></label>
<label for="result-type">事项类型<select id="result-type"><option value="">全部类型</option></select></label>
<label class="result-selected-filter"><input id="result-selected-only" type="checkbox">只看已选</label></div>
<div id="result-groups"></div><p id="result-count"></p>
<div class="actions"><button id="result-prepare" type="button" disabled>请选择待办</button>
<button id="result-recover" type="button" hidden>恢复同一准备请求</button></div>
<div id="result-resume"></div>
<p class="muted">关闭或返回仅离开页面，不会取消任务。若需取消，请回到原聊天要求取消对应任务。</p>
</section></main>'''
            body += f'<script nonce="{nonce}" type="application/json" id="result-data">{data}</script>'
            body += f'<script nonce="{nonce}" type="module">{_SCRIPT.read_text()}</script>'
            body += f'''<style nonce="{nonce}">
              .result-choice {{ display:flex; align-items:center; gap:12px; min-height:48px; padding:10px 0; cursor:pointer; }}
              .result-choice input[type=checkbox] {{ width:24px; height:24px; min-height:24px; flex:0 0 24px; margin:0; }}
              .result-choice span {{ overflow-wrap:anywhere; font-size:16px; }}
              #result-groups section {{ border-top:1px solid var(--line); margin-top:18px; }}
              #result-groups p {{ font-size:14px; overflow-wrap:anywhere; }}
              #result-resume {{ display:grid; gap:12px; margin-top:16px; }}
              .result-filters {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:16px 0; }}
              .result-filters label {{ display:grid; gap:6px; font-size:14px; min-width:0; }}
              .result-filters input,.result-filters select {{ width:100%; min-width:0; }}
              .result-filters .result-selected-filter {{ display:flex; align-items:center; gap:8px; }}
              .result-selected-filter input[type=checkbox] {{ width:22px; height:22px; min-height:22px; margin:0; }}
              .result-query-actions {{ display:flex; flex-wrap:wrap; gap:10px; }}
              .result-select-current {{ font-size:14px; }}
              .result-choice[hidden],#result-groups p[hidden] {{ display:none; }}
              .result-choice:has(input:checked) {{ background:var(--selected-bg);color:var(--selected-ink);border-radius:5px; }}
              .result-choice:has(input:disabled) {{ color:var(--muted);cursor:default; }}
              .result-recovery {{ padding:16px;border:1px solid var(--line);border-radius:6px;background:var(--subtle-bg);color:var(--subtle-ink); }}
              .result-recovery h3 {{ margin:0 0 10px;font-size:17px; }}
              .result-recovery p {{ line-height:1.65;overflow-wrap:anywhere; }}
              .result-recovery button {{ margin-top:10px; }}
              .result-recovery [hidden] {{ display:none!important; }}
              .result-risk {{ display:flex;align-items:flex-start;gap:10px;margin-top:16px;padding:12px;background:var(--warning-bg);color:var(--warning-ink);line-height:1.65; }}
              .result-risk input {{ width:22px;height:22px;min-height:22px;flex:0 0 22px;margin-top:2px; }}
              @media(max-width:480px) {{ .result-filters {{ grid-template-columns:1fr; }} }}
            </style>'''
            return AuthCardResponse(200, headers, _document(title="选择待办并核对办理", body=body, nonce=nonce).encode())
        except Exception as error:
            return self.error(error, page=True)

    def _authenticate(self, view_token, token, cookie):
        if (not isinstance(cookie, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", cookie)
                or not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token)
                or not hmac.compare_digest(_proof(cookie, view_token), token)):
            raise _ResultDenied()

    def state(self, view_token, *, token, cookie, task_id=None, query_task_id=None):
        try:
            self._authenticate(view_token, token, cookie)
            if task_id is not None or query_task_id is not None:
                if task_id is not None and query_task_id is not None:
                    return self.json(400, {"error": {"code": "RESULT_TASK_INVALID", "message": "任务标识无效。"}})
                task_id = query_task_id if query_task_id is not None else task_id
                if not isinstance(task_id, str) or not re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", task_id,
                ):
                    return self.json(400, {"error": {"code": "RESULT_TASK_INVALID", "message": "任务标识无效。"}})
                state = self.backend.query_status(view_token, task_id) if query_task_id is not None else self.backend.status(view_token, task_id)
            else:
                state = self.backend.read(view_token)
            return self.json(200, state)
        except Exception as error:
            return self.error(error)

    def prepare(self, view_token, *, token, cookie, content_type, body, action="prepare"):
        try:
            self._authenticate(view_token, token, cookie)
            if len(body) > MAX_AUTH_BODY_BYTES:
                return self.json(413, {"error": {"code": "BODY_TOO_LARGE", "message": "请求过大。"}})
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                return self.json(415, {"error": {"code": "CONTENT_TYPE", "message": "请求格式不支持。"}})
            try:
                value = json.loads(body)
            except (ValueError, UnicodeError):
                return self.json(400, {"error": {"code": "INVALID_JSON", "message": "请求内容无效。"}})
            if action not in {"prepare", "refresh", "open", "recovery-probe", "recovery-prepare", "recovery-open"}:
                return self.json(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "请求方法不支持。"}})
            return self.json(202, getattr(self.backend, action.replace("-", "_"))(view_token, value))
        except Exception as error:
            return self.error(error)


class _ResultDenied(ValueError):
    code = "RESULT_CSRF_INVALID"
    status = 403

    def __str__(self):
        return "页面会话已失效，请重新打开原可信入口。"
