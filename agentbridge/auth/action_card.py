from __future__ import annotations

from datetime import datetime
from html import escape
from math import ceil
import secrets
from urllib.parse import parse_qs

from agentbridge.auth.card import AuthCardResponse, MAX_AUTH_BODY_BYTES
from agentbridge.auth.embedded import EMBEDDED_SAFE_AREA_CSS, render_embedded_web_app_bridge
from agentbridge.core.write_authorizations import (
    WriteAuthorizationAccessDenied,
    WriteAuthorizationNotFound,
    WriteAuthorizationStateError,
    WriteAuthorizationStore,
)


class TrustedActionApplication:
    def __init__(self, *, authorization_store: WriteAuthorizationStore) -> None:
        self.authorization_store = authorization_store

    def get_card(
        self,
        authorization_id: str,
        *,
        secure_cookie: bool,
        presentation_id: str | None = None,
    ) -> AuthCardResponse:
        try:
            authorization = self.authorization_store.get(authorization_id)
            if presentation_id is not None:
                self.authorization_store.get_presentation(
                    authorization_id,
                    presentation_id,
                )
        except WriteAuthorizationNotFound:
            return self._message_response(
                status=404,
                title="操作确认不存在",
                message="请从智能体重新生成操作计划。",
                tone="error",
            )
        state = authorization["state"]
        if state == "pending":
            csrf_token = self.authorization_store.issue_csrf(
                authorization_id,
                presentation_id=presentation_id,
            )
            nonce = secrets.token_urlsafe(18)
            body = _render_confirmation(
                authorization,
                csrf_token=csrf_token,
                nonce=nonce,
                presentation_id=presentation_id,
            )
            card_path = _card_path(authorization_id, presentation_id)
            cookie = (
                f"agentbridge_csrf={csrf_token}; Path={card_path}; "
                f"HttpOnly; SameSite=Strict; Max-Age={_ttl_seconds(authorization)}"
            )
            if secure_cookie:
                cookie += "; Secure"
            headers = _security_headers(nonce)
            headers["Set-Cookie"] = cookie
            return AuthCardResponse(200, headers, body.encode("utf-8"))
        if state == "approved":
            return self._message_response(
                status=200,
                title="操作已授权",
                message="计划已经授权。此页面可以关闭，智能体将检测状态并继续执行。",
                tone="success",
            )
        if state == "consumed":
            return self._message_response(
                status=200,
                title="授权已使用",
                message="这份一次性授权已经进入执行流程。",
                tone="success",
            )
        if state == "rejected":
            return self._message_response(
                status=200,
                title="操作已取消",
                message="中心服务不会执行这份操作计划。",
                tone="neutral",
            )
        return self._message_response(
            status=410,
            title="操作确认已失效",
            message="请返回智能体重新生成操作计划。",
            tone="error",
        )

    def submit_card(
        self,
        authorization_id: str,
        *,
        body: bytes,
        content_type: str,
        csrf_cookie: str,
        presentation_id: str | None = None,
    ) -> AuthCardResponse:
        if len(body) > MAX_AUTH_BODY_BYTES:
            return self._message_response(
                status=413,
                title="请求过大",
                message="操作确认已被拒绝。",
                tone="error",
            )
        if content_type.split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            return self._message_response(
                status=415,
                title="请求格式不支持",
                message="操作确认已被拒绝。",
                tone="error",
            )

        payload = bytearray(body)
        fields: dict[str, list[str]] = {}
        try:
            try:
                fields = parse_qs(
                    payload.decode("utf-8"),
                    keep_blank_values=True,
                    max_num_fields=3,
                    strict_parsing=True,
                )
            except (UnicodeDecodeError, ValueError):
                return self._message_response(
                    status=400,
                    title="请求格式错误",
                    message="操作确认已被拒绝。",
                    tone="error",
                )
            if set(fields) != {"csrf_token", "decision"} or any(
                len(values) != 1 for values in fields.values()
            ):
                return self._message_response(
                    status=400,
                    title="确认字段不匹配",
                    message="请从智能体重新打开操作确认卡。",
                    tone="error",
                )
            decision = fields["decision"][0]
            if decision not in {"approve", "reject"}:
                return self._message_response(
                    status=400,
                    title="确认选项无效",
                    message="操作确认已被拒绝。",
                    tone="error",
                )
            try:
                if presentation_id is not None:
                    self.authorization_store.get_presentation(
                        authorization_id,
                        presentation_id,
                    )
                authorization = self.authorization_store.decide(
                    authorization_id,
                    decision=decision,
                    csrf_token=fields["csrf_token"][0],
                    csrf_cookie=csrf_cookie,
                    presentation_id=presentation_id,
                )
            except WriteAuthorizationNotFound:
                return self._message_response(
                    status=404,
                    title="操作确认不存在",
                    message="请从智能体重新生成操作计划。",
                    tone="error",
                )
            except WriteAuthorizationAccessDenied:
                return self._message_response(
                    status=403,
                    title="操作确认校验失败",
                    message="请从智能体重新打开操作确认卡。",
                    tone="error",
                )
            except WriteAuthorizationStateError:
                try:
                    current = self.authorization_store.get(authorization_id)
                except WriteAuthorizationNotFound:
                    current = None
                if current is not None and current["state"] in {
                    "approved",
                    "consumed",
                }:
                    return self._message_response(
                        status=200,
                        title="操作已在其他可信端确认",
                        message="中心服务已经接受首次有效确认，业务操作只会执行一次。",
                        tone="success",
                    )
                if current is not None and current["state"] == "rejected":
                    return self._message_response(
                        status=200,
                        title="操作已在其他可信端取消",
                        message="中心服务不会执行这份操作计划。",
                        tone="neutral",
                    )
                return self._message_response(
                    status=410,
                    title="操作确认已失效",
                    message="请返回智能体重新生成操作计划。",
                    tone="error",
                )
            if authorization["state"] == "approved":
                return self._message_response(
                    status=200,
                    title="操作已授权",
                    message="计划已经授权。此页面可以关闭，智能体将检测状态并继续执行。",
                    tone="success",
                )
            return self._message_response(
                status=200,
                title="操作已取消",
                message="中心服务不会执行这份操作计划。",
                tone="neutral",
            )
        finally:
            for index in range(len(payload)):
                payload[index] = 0
            fields.clear()

    @staticmethod
    def _message_response(
        *,
        status: int,
        title: str,
        message: str,
        tone: str,
    ) -> AuthCardResponse:
        nonce = secrets.token_urlsafe(18)
        body = _document(
            title=title,
            nonce=nonce,
            close_when_complete=tone in {"success", "neutral"},
            body=f"""
            <main class="shell">
              <section class="card status-card {escape(tone)}" aria-labelledby="status-title">
                <div class="status-mark" aria-hidden="true"></div>
                <p class="eyebrow">AGENTBRIDGE TRUSTED ACTION</p>
                <h1 id="status-title">{escape(title)}</h1>
                <p class="status-copy">{escape(message)}</p>
              </section>
            </main>
            """,
        )
        return AuthCardResponse(status, _security_headers(nonce), body.encode("utf-8"))


def _render_confirmation(
    authorization: dict,
    *,
    csrf_token: str,
    nonce: str,
    presentation_id: str | None,
) -> str:
    summary = authorization.get("summary") if isinstance(authorization.get("summary"), dict) else {}
    fields = summary.get("fields") if isinstance(summary.get("fields"), list) else []
    title = str(summary.get("title") or "确认执行计划")
    effect = str(summary.get("effect") or "按上方冻结计划执行")
    notice = str(
        summary.get("authorization_notice")
        or f"授权后，AgentBridge 将执行：{effect}。请确认操作内容和执行效果无误。"
    )
    authorize_label = str(summary.get("authorize_label") or "授权执行")
    rows = []
    for item in fields:
        if not isinstance(item, dict):
            continue
        label = escape(str(item.get("label") or "项目"))
        value = escape(str(item.get("value") or "未填写"))
        rows.append(f"<div><dt>{label}</dt><dd>{value}</dd></div>")
    return _document(
        title=f"确认{title}",
        nonce=nonce,
        body=f"""
        <main class="shell">
          <section class="card" aria-labelledby="card-title">
            <header class="card-header">
              <div class="brand-mark" aria-hidden="true"><span></span></div>
              <div>
                <p class="eyebrow">AGENTBRIDGE TRUSTED ACTION</p>
                <h1 id="card-title">{escape(title)}</h1>
              </div>
            </header>
            <dl class="detail-list">
              <div><dt>执行身份</dt><dd>{escape(str(summary.get('principal') or '当前用户'))}</dd></div>
              <div><dt>目标系统</dt><dd>{escape(str(summary.get('system') or '未标明'))}</dd></div>
              <div><dt>执行效果</dt><dd>{escape(effect)}</dd></div>
              {''.join(rows)}
              <div><dt>计划指纹</dt><dd class="hash">{escape(str(authorization.get('plan_hash') or ''))}</dd></div>
            </dl>
            <p class="notice">{escape(notice)}</p>
            <form method="post" action="{escape(_card_path(authorization['authorization_id'], presentation_id))}" id="action-form">
              <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
              <div class="actions">
                <button type="submit" name="decision" value="reject" class="secondary">取消操作</button>
                <button type="submit" name="decision" value="approve" class="primary">{escape(authorize_label)}</button>
              </div>
            </form>
            <footer>可在任一可信端确认；中心服务只接受一次决定并只执行一次</footer>
          </section>
        </main>
        """,
    )


def _card_path(
    authorization_id: str,
    presentation_id: str | None,
) -> str:
    base = f"/authorize/{authorization_id}"
    if presentation_id is None:
        return base
    return f"{base}/present/{presentation_id}"


def _document(
    *,
    title: str,
    nonce: str,
    body: str,
    close_when_complete: bool = False,
) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <title>{escape(title)} · AgentBridge</title>
  <style nonce="{nonce}">
    :root {{ color-scheme: light; --paper:#f4f5f2; --surface:#fff; --ink:#17201d;
      --muted:#64706b; --line:#d6dbd7; --teal:#087d72; --teal-dark:#075f58;
      --amber:#a8640d; --red:#b23a3a; }}
    * {{ box-sizing:border-box; }} html,body {{ min-height:100%; }}
    body {{ margin:0; background:var(--paper); color:var(--ink);
      font-family:"Microsoft YaHei UI","Noto Sans CJK SC",sans-serif; letter-spacing:0; }}
    body::before {{ content:""; position:fixed; inset:0 0 auto; height:6px;
      background:linear-gradient(90deg,var(--teal) 0 68%,var(--amber) 68% 100%); }}
    .shell {{ min-height:100vh; display:flex; align-items:center; justify-content:center;
      padding:32px 18px; }}
    .card {{ width:520px; max-width:100%; background:var(--surface); border:1px solid var(--line);
      border-radius:8px; box-shadow:0 18px 45px rgba(23,32,29,.10); padding:30px; }}
    .card-header {{ display:flex; align-items:center; gap:16px; margin-bottom:24px; }}
    .brand-mark {{ width:46px; height:46px; border:2px solid var(--teal); display:grid;
      place-items:center; flex:0 0 auto; }}
    .brand-mark span {{ width:15px; height:20px; border:2px solid var(--teal); border-top-width:7px; }}
    .eyebrow {{ margin:0 0 5px; color:var(--teal-dark); font-size:11px; font-weight:700; }}
    h1 {{ margin:0; font-size:24px; line-height:1.3; }}
    .detail-list {{ margin:0; border-block:1px solid var(--line); }}
    .detail-list div {{ display:grid; grid-template-columns:92px minmax(0,1fr); gap:12px; padding:11px 0; }}
    .detail-list div + div {{ border-top:1px solid var(--line); }}
    dt {{ color:var(--muted); font-size:13px; }} dd {{ margin:0; font-size:13px; overflow-wrap:anywhere; }}
    .hash {{ color:var(--muted); font-family:Consolas,monospace; font-size:11px; }}
    .notice {{ margin:18px 0; padding:12px; border-left:4px solid var(--amber); background:#fff8ea;
      color:#6d490f; font-size:13px; line-height:1.6; }}
    .actions {{ display:grid; grid-template-columns:1fr 1.3fr; gap:12px; }}
    button {{ min-height:47px; border-radius:5px; padding:9px 14px; font:inherit; font-weight:700; cursor:pointer; }}
    button.primary {{ border:0; background:var(--teal); color:#fff; }}
    button.primary:hover {{ background:var(--teal-dark); }}
    button.secondary {{ border:1px solid #aeb8b3; background:#fff; color:var(--ink); }}
    button:disabled {{ cursor:wait; opacity:.62; }}
    footer {{ margin-top:20px; color:var(--muted); font-size:11px; text-align:center; }}
    .status-card {{ text-align:center; padding-block:44px; }}
    .status-mark {{ width:24px; height:24px; margin:0 auto 20px; border:5px solid var(--teal); transform:rotate(45deg); }}
    .status-card.error .status-mark {{ border-color:var(--red); }}
    .status-card.neutral .status-mark {{ border-color:var(--muted); }}
    .status-copy {{ margin:16px auto 0; max-width:360px; color:var(--muted); line-height:1.7; font-size:14px; }}
    @media (max-width:560px) {{ .shell {{ align-items:flex-start; padding:22px 12px; }}
      .card {{ padding:24px 20px; box-shadow:none; }} h1 {{ font-size:21px; }}
      .detail-list div {{ grid-template-columns:76px minmax(0,1fr); }}
      .actions {{ grid-template-columns:1fr; }} }}
    {EMBEDDED_SAFE_AREA_CSS}
  </style>
</head>
<body>{body}{render_embedded_web_app_bridge(nonce=nonce, close_when_complete=close_when_complete)}</body>
</html>"""


def _security_headers(nonce: str) -> dict[str, str]:
    return {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        ),
    }


def _ttl_seconds(authorization: dict) -> int:
    try:
        created = datetime.fromisoformat(authorization["created_at"])
        expires = datetime.fromisoformat(authorization["expires_at"])
        return max(1, min(1800, ceil((expires - created).total_seconds())))
    except (KeyError, TypeError, ValueError):
        return 600


def _format_expiry(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value).astimezone()
    except (TypeError, ValueError):
        return escape(str(value))
    return escape(parsed.strftime("%Y-%m-%d %H:%M"))
