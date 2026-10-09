from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from html import escape
from math import ceil
import secrets
from urllib.parse import parse_qs

from agentbridge.auth.embedded import EMBEDDED_SAFE_AREA_CSS, render_embedded_web_app_bridge
from agentbridge.core.auth_challenges import (
    AuthChallengeStore,
    ChallengeAccessDenied,
    ChallengeNotFound,
    ChallengeStateError,
)


MAX_AUTH_BODY_BYTES = 16 * 1024


@dataclass(frozen=True)
class AuthCardResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class TrustedAuthApplication:
    def __init__(self, *, challenge_store: AuthChallengeStore, broker) -> None:
        self.challenge_store = challenge_store
        self.broker = broker

    def get_card(self, challenge_id: str, *, secure_cookie: bool) -> AuthCardResponse:
        try:
            challenge = self.challenge_store.get(challenge_id)
        except ChallengeNotFound:
            return self._message_response(
                status=404,
                title="认证请求不存在",
                message="请从智能体重新发起登录。",
                tone="error",
            )

        if challenge["state"] == "pending":
            prepared = None
            if any(field["name"] == "authcode" for field in challenge["fields"]):
                prepare = getattr(self.broker, "prepare_authentication", None)
                if callable(prepare):
                    try:
                        prepared = prepare(challenge_id=challenge_id)
                    except Exception:
                        return self._message_response(
                            status=503,
                            title="验证码加载失败",
                            message="目标系统暂时无法提供验证码，请稍后重新打开登录卡。",
                            tone="error",
                        )
            csrf_token = self.challenge_store.issue_csrf(challenge_id)
            nonce = secrets.token_urlsafe(18)
            html = _render_form(
                challenge,
                csrf_token=csrf_token,
                nonce=nonce,
                prepared=prepared,
            )
            cookie = (
                f"agentbridge_csrf={csrf_token}; Path=/auth/{challenge_id}; "
                f"HttpOnly; SameSite=Strict; Max-Age={_challenge_ttl_seconds(challenge)}"
            )
            if secure_cookie:
                cookie += "; Secure"
            headers = _security_headers(nonce)
            headers["Set-Cookie"] = cookie
            return AuthCardResponse(200, headers, html.encode("utf-8"))
        if challenge["state"] == "processing":
            return self._message_response(
                status=200,
                title="正在验证",
                message=f"中心服务正在核验{challenge['system_name']}登录结果。",
                tone="processing",
            )
        if challenge["state"] == "succeeded":
            return self._message_response(
                status=200,
                title="认证完成",
                message=f"{challenge['system_name']}会话已经建立。此页面可以关闭，智能体将检测状态并继续操作。",
                tone="success",
            )
        return self._message_response(
            status=410,
            title="认证请求已失效",
            message="请返回智能体重新发起登录。",
            tone="error",
        )

    def submit_card(
        self,
        challenge_id: str,
        *,
        body: bytes,
        content_type: str,
        csrf_cookie: str,
    ) -> AuthCardResponse:
        if len(body) > MAX_AUTH_BODY_BYTES:
            return self._message_response(
                status=413,
                title="请求过大",
                message="认证请求已被拒绝。",
                tone="error",
            )
        if content_type.split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            return self._message_response(
                status=415,
                title="请求格式不支持",
                message="认证请求已被拒绝。",
                tone="error",
            )

        payload = bytearray(body)
        fields: dict[str, list[str]] = {}
        credentials: dict[str, str] = {}
        try:
            try:
                fields = parse_qs(
                    payload.decode("utf-8"),
                    keep_blank_values=True,
                    max_num_fields=10,
                    strict_parsing=True,
                )
            except (UnicodeDecodeError, ValueError):
                return self._message_response(
                    status=400,
                    title="请求格式错误",
                    message="认证请求已被拒绝。",
                    tone="error",
                )
            if any(len(values) != 1 for values in fields.values()):
                return self._message_response(
                    status=400,
                    title="请求字段重复",
                    message="认证请求已被拒绝。",
                    tone="error",
                )
            try:
                challenge = self.challenge_store.get(challenge_id)
            except ChallengeNotFound:
                return self._message_response(
                    status=404,
                    title="认证请求不存在",
                    message="请从智能体重新发起登录。",
                    tone="error",
                )
            allowed_names = {field["name"] for field in challenge["fields"]}
            submitted_names = set(fields) - {"csrf_token"}
            if submitted_names != allowed_names:
                return self._message_response(
                    status=400,
                    title="认证字段不匹配",
                    message="请从智能体重新发起登录。",
                    tone="error",
                )
            csrf_token = fields.get("csrf_token", [""])[0]
            credentials = {name: fields[name][0] for name in allowed_names}
            try:
                result = self.broker.authenticate(
                    challenge_id=challenge_id,
                    csrf_token=csrf_token,
                    csrf_cookie=csrf_cookie,
                    credentials=credentials,
                )
            except ChallengeAccessDenied:
                return self._message_response(
                    status=403,
                    title="认证卡片校验失败",
                    message="请从智能体重新发起登录。",
                    tone="error",
                )
            except ChallengeStateError:
                return self._message_response(
                    status=409,
                    title="认证请求已被使用",
                    message="请检查当前会话状态或重新发起登录。",
                    tone="error",
                )
            if result.get("status") == "succeeded":
                return self._message_response(
                    status=200,
                    title="认证完成",
                    message=f"{challenge['system_name']}会话已经建立。此页面可以关闭，智能体将检测状态并继续操作。",
                    tone="success",
                )
            error_code = str((result.get("error") or {}).get("code") or "BROKER_LOGIN_FAILED")
            return self._message_response(
                status=401,
                title="认证未完成",
                message=_safe_failure_message(
                    error_code,
                    system_name=challenge["system_name"],
                ),
                tone="error",
            )
        finally:
            for index in range(len(payload)):
                payload[index] = 0
            credentials.clear()
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
        html = _render_message(title=title, message=message, tone=tone, nonce=nonce)
        return AuthCardResponse(status, _security_headers(nonce), html.encode("utf-8"))


def _render_form(
    challenge: dict,
    *,
    csrf_token: str,
    nonce: str,
    prepared: dict | None = None,
) -> str:
    fields_html = []
    for field in challenge["fields"]:
        input_type = "password" if field["input_type"] == "password" else "text"
        input_mode = ' inputmode="numeric"' if field["input_type"] == "otp" else ""
        required = " required" if field.get("required") else ""
        captcha_html = ""
        if field["name"] == "authcode" and prepared:
            captcha = prepared.get("captcha")
            if isinstance(captcha, dict):
                content_type = str(captcha.get("content_type") or "").lower()
                body = captcha.get("body")
                if content_type in {"image/jpeg", "image/png", "image/gif"} and isinstance(
                    body, bytes
                ):
                    encoded = base64.b64encode(body).decode("ascii")
                    captcha_html = (
                        '<span class="captcha-image">'
                        f'<img src="data:{content_type};base64,{encoded}" '
                        'alt="登录验证码" width="94" height="46">'
                        '<small>看不清时请重新打开本卡刷新</small>'
                        "</span>"
                    )
        fields_html.append(
            f"""
            <label class="field">
              <span>{escape(field['label'])}</span>
              {captcha_html}
              <input name="{escape(field['name'])}" type="{input_type}"
                     autocomplete="{escape(field['autocomplete'])}"{input_mode}{required}
                     maxlength="2048">
            </label>
            """
        )
    principal = escape(challenge.get("expected_principal_ref") or "未指定")
    return _document(
        title="系统身份认证",
        nonce=nonce,
        body=f"""
        <main class="auth-shell">
          <section class="auth-card" aria-labelledby="card-title">
            <header class="card-header">
              <div class="brand-mark" aria-hidden="true"><span></span></div>
              <div>
                <p class="eyebrow">AGENTBRIDGE TRUSTED AUTH</p>
                <h1 id="card-title">{escape(challenge['system_name'])}</h1>
              </div>
            </header>
            <dl class="identity-strip">
              <div><dt>认证身份</dt><dd>{principal}</dd></div>
              <div><dt>目标系统</dt><dd>{escape(challenge['origin'])}</dd></div>
            </dl>
            <form method="post" action="/auth/{escape(challenge['challenge_id'])}" id="auth-form">
              <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
              {''.join(fields_html)}
              <p class="form-error" id="form-error" role="alert" hidden></p>
              <button type="submit" id="submit-button">验证并登录</button>
            </form>
            <footer>挑战将在 {_format_expiry(challenge['expires_at'])} 失效</footer>
          </section>
        </main>
        <script nonce="{nonce}">
          const form = document.getElementById('auth-form');
          const button = document.getElementById('submit-button');
          form.addEventListener('submit', () => {{
            button.disabled = true;
            button.textContent = '正在验证';
          }});
        </script>
        """,
    )


def _render_message(*, title: str, message: str, tone: str, nonce: str) -> str:
    return _document(
        title=title,
        nonce=nonce,
        close_when_complete=tone == "success",
        body=f"""
        <main class="auth-shell">
          <section class="auth-card status-card {escape(tone)}" aria-labelledby="status-title">
            <div class="status-mark" aria-hidden="true"></div>
            <p class="eyebrow">AGENTBRIDGE TRUSTED AUTH</p>
            <h1 id="status-title">{escape(title)}</h1>
            <p class="status-copy">{escape(message)}</p>
          </section>
        </main>
        """,
    )


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
    :root {{
      color-scheme: light;
      --paper: #f4f5f2;
      --surface: #ffffff;
      --ink: #17201d;
      --muted: #64706b;
      --line: #d6dbd7;
      --teal: #087d72;
      --teal-dark: #075f58;
      --amber: #c47b17;
      --red: #b23a3a;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ min-height: 100%; }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: "Microsoft YaHei UI", "Noto Sans CJK SC", sans-serif;
      letter-spacing: 0;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0 0 auto;
      height: 6px;
      background: linear-gradient(90deg, var(--teal) 0 68%, var(--amber) 68% 100%);
    }}
    .auth-shell {{
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 32px 18px;
    }}
    .auth-card {{
      width: 440px;
      max-width: 100%;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 18px 45px rgba(23, 32, 29, 0.10);
      padding: 30px;
    }}
    .card-header {{ display: flex; align-items: center; gap: 16px; margin-bottom: 24px; }}
    .brand-mark {{
      width: 46px;
      height: 46px;
      border: 2px solid var(--teal);
      display: grid;
      place-items: center;
      flex: 0 0 auto;
    }}
    .brand-mark span {{ width: 15px; height: 20px; border: 2px solid var(--teal); border-top-width: 7px; }}
    .eyebrow {{ margin: 0 0 5px; color: var(--teal-dark); font-size: 11px; font-weight: 700; text-transform: uppercase; }}
    h1 {{ margin: 0; font-size: 24px; line-height: 1.25; font-weight: 700; }}
    .identity-strip {{ margin: 0 0 24px; border-block: 1px solid var(--line); }}
    .identity-strip div {{ display: grid; grid-template-columns: 84px minmax(0, 1fr); gap: 12px; padding: 11px 0; }}
    .identity-strip div + div {{ border-top: 1px solid var(--line); }}
    dt {{ color: var(--muted); font-size: 13px; }}
    dd {{ margin: 0; font-size: 13px; overflow-wrap: anywhere; }}
    form {{ display: grid; gap: 17px; }}
    .field {{ display: grid; gap: 7px; font-size: 13px; font-weight: 700; }}
    input {{
      width: 100%;
      min-height: 46px;
      border: 1px solid #aeb8b3;
      border-radius: 5px;
      padding: 10px 12px;
      color: var(--ink);
      background: #fff;
      font: inherit;
      outline: none;
    }}
    input:focus {{ border-color: var(--teal); box-shadow: 0 0 0 3px rgba(8, 125, 114, 0.14); }}
    .captcha-image {{ display: flex; align-items: center; gap: 12px; font-weight: 400; color: var(--muted); }}
    .captcha-image img {{ width: 94px; height: 46px; border: 1px solid var(--line); object-fit: contain; background: #fff; }}
    button {{
      min-height: 47px;
      border: 0;
      border-radius: 5px;
      background: var(--teal);
      color: #fff;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: var(--teal-dark); }}
    button:disabled {{ cursor: wait; background: #789590; }}
    footer {{ margin-top: 20px; color: var(--muted); font-size: 11px; text-align: center; }}
    .status-card {{ text-align: center; padding-block: 44px; }}
    .status-mark {{ width: 24px; height: 24px; margin: 0 auto 20px; border: 5px solid var(--teal); transform: rotate(45deg); }}
    .status-card.error .status-mark {{ border-color: var(--red); }}
    .status-card.processing .status-mark {{ border-color: var(--amber); border-radius: 50%; }}
    .status-copy {{ margin: 16px auto 0; max-width: 320px; color: var(--muted); line-height: 1.7; font-size: 14px; }}
    @media (max-width: 520px) {{
      .auth-shell {{ align-items: flex-start; padding: 22px 12px; }}
      .auth-card {{ padding: 24px 20px; box-shadow: none; }}
      h1 {{ font-size: 21px; }}
      .identity-strip div {{ grid-template-columns: 72px minmax(0, 1fr); }}
    }}
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
            "img-src data:; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        ),
    }


def _format_expiry(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value).astimezone()
    except (TypeError, ValueError):
        return escape(str(value))
    return escape(parsed.strftime("%Y-%m-%d %H:%M"))


def _challenge_ttl_seconds(challenge: dict) -> int:
    try:
        created = datetime.fromisoformat(challenge["created_at"])
        expires = datetime.fromisoformat(challenge["expires_at"])
        return max(1, min(900, ceil((expires - created).total_seconds())))
    except (KeyError, TypeError, ValueError):
        return 300


def _safe_failure_message(code: str, *, system_name: str = "目标系统") -> str:
    messages = {
        "SESSION_PROFILE_UNAVAILABLE": "中心受控浏览器的用户目录不可写。",
        "PRINCIPAL_MISMATCH": "登录身份与预期身份不一致，会话已被隔离。",
        "UNSUPPORTED_AUTH_METHOD": "OA 登录需要当前认证卡片不支持的验证方式。",
        "LOGIN_CONTRACT_MISMATCH": "OA 登录页面结构已变化，中心服务已安全停止。",
        "AUTHENTICATION_REJECTED": f"{system_name}未接受本次登录信息。",
        "CAPTCHA_REJECTED": f"{system_name}未接受本次验证码，请从智能体重新发起登录。",
        "CREDENTIALS_REJECTED": f"{system_name}未接受本次账号或密码。",
        "SESSION_STATE_UNAVAILABLE": "OA 已登录，但中心服务无法安全保存会话。",
        "AUTHENTICATION_REQUEST_INVALID": "认证请求与已登记的登录契约不一致。",
        "BROKER_LOGIN_FAILED": "中心凭据代理未能完成本次登录。",
    }
    message = messages.get(code, messages["BROKER_LOGIN_FAILED"])
    return f"{message} 错误代码：{code}。请返回智能体处理。"
