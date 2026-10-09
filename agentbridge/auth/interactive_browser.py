from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
from math import ceil
import secrets
from urllib.parse import parse_qs

from agentbridge.auth.card import AuthCardResponse, MAX_AUTH_BODY_BYTES
from agentbridge.auth.embedded import EMBEDDED_SAFE_AREA_CSS, render_embedded_web_app_bridge
from agentbridge.broker.remote_browser import (
    RemoteBrowserAccessDenied,
    RemoteBrowserUnavailable,
)
from agentbridge.core.auth_challenges import (
    AuthChallengeStore,
    ChallengeAccessDenied,
    ChallengeNotFound,
    ChallengeStateError,
)


class TrustedInteractiveBrowserApplication:
    """Trusted card shell for the challenge-scoped native noVNC browser."""

    def __init__(self, *, challenge_store: AuthChallengeStore, broker) -> None:
        self.challenge_store = challenge_store
        self.broker = broker

    def get_card(self, challenge_id: str, *, secure_cookie: bool) -> AuthCardResponse:
        try:
            challenge = self.challenge_store.get(challenge_id)
        except ChallengeNotFound:
            return _message_response(
                status=404,
                title="认证请求不存在",
                message="请从智能体重新发起登录。",
                tone="error",
            )
        if challenge["challenge_type"] != "interactive_browser_login":
            return _message_response(
                status=400,
                title="认证类型不匹配",
                message="请从智能体重新发起登录。",
                tone="error",
            )
        if challenge["state"] == "pending":
            csrf_token = self.challenge_store.issue_csrf(challenge_id)
            nonce = secrets.token_urlsafe(18)
            response = AuthCardResponse(
                200,
                _security_headers(nonce, frame_origin=self.broker.public_origin),
                _render_interactive_card(
                    challenge,
                    csrf_token=csrf_token,
                    nonce=nonce,
                ).encode("utf-8"),
            )
            cookie = (
                f"agentbridge_csrf={csrf_token}; Path=/auth/{challenge_id}; "
                f"HttpOnly; SameSite=Strict; Max-Age={_challenge_ttl_seconds(challenge)}"
            )
            if secure_cookie:
                cookie += "; Secure"
            response.headers["Set-Cookie"] = cookie
            return response
        if challenge["state"] == "processing":
            return _message_response(
                status=409,
                title="登录正在进行",
                message="请返回刚才打开的登录卡继续操作；刷新页面会丢失临时控制通道。",
                tone="processing",
            )
        if challenge["state"] == "succeeded":
            return _message_response(
                status=200,
                title="认证完成",
                message="部门信息库会话已经建立。此页面可以关闭，智能体将继续原操作。",
                tone="success",
                close_when_complete=True,
            )
        return _message_response(
            status=410,
            title="认证未完成",
            message="本次交互式登录已失效，请返回智能体重新发起。",
            tone="error",
        )

    def start(
        self,
        challenge_id: str,
        *,
        body: bytes,
        content_type: str,
        csrf_cookie: str,
    ) -> AuthCardResponse:
        if not _valid_body(body, content_type, "application/x-www-form-urlencoded"):
            return _json_response(
                400,
                {"status": "failed", "error": {"code": "INVALID_REQUEST"}},
            )
        try:
            fields = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                max_num_fields=2,
                strict_parsing=True,
            )
            if set(fields) != {"csrf_token"} or len(fields["csrf_token"]) != 1:
                raise ValueError("invalid CSRF form")
            result = self.broker.start(
                challenge_id=challenge_id,
                csrf_token=fields["csrf_token"][0],
                csrf_cookie=csrf_cookie,
            )
        except ChallengeAccessDenied:
            return _json_response(
                403,
                {"status": "failed", "error": {"code": "CSRF_REJECTED"}},
            )
        except (ChallengeNotFound, ChallengeStateError, UnicodeDecodeError, ValueError):
            return _json_response(
                409,
                {"status": "failed", "error": {"code": "CHALLENGE_UNAVAILABLE"}},
            )
        status = 202 if result.get("status") == "processing" else 500
        return _json_response(status, result)

    def status(self, challenge_id: str, *, control_token: str) -> AuthCardResponse:
        try:
            result = self.broker.status(
                challenge_id=challenge_id,
                control_token=control_token,
            )
        except RemoteBrowserAccessDenied:
            return _json_response(403, {"error": {"code": "CONTROL_DENIED"}})
        except (RemoteBrowserUnavailable, ChallengeNotFound):
            return _json_response(409, {"error": {"code": "BROWSER_UNAVAILABLE"}})
        return _json_response(200, result)


def _render_interactive_card(
    challenge: dict,
    *,
    csrf_token: str,
    nonce: str,
) -> str:
    principal = escape(challenge.get("expected_principal_ref") or "未指定")
    system_name = escape(challenge["system_name"])
    challenge_id = escape(challenge["challenge_id"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <title>{system_name}安全登录 · AgentBridge</title>
  <style nonce="{nonce}">
    :root {{
      color-scheme: light;
      --paper: #f2f4f1;
      --surface: #fff;
      --ink: #17201d;
      --muted: #65716c;
      --line: #cbd3ce;
      --teal: #087d72;
      --teal-dark: #075f58;
      --amber: #a96313;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ min-height: 100%; }}
    body {{
      margin: 0;
      padding: 16px;
      background: var(--paper);
      color: var(--ink);
      font-family: "Microsoft YaHei UI", "Noto Sans CJK SC", sans-serif;
      letter-spacing: 0;
    }}
    .shell {{ width: min(100%, 760px); margin: 0 auto; }}
    header {{ margin-bottom: 12px; }}
    .eyebrow {{
      margin: 0 0 5px;
      color: var(--teal-dark);
      font-size: 11px;
      font-weight: 700;
    }}
    h1 {{ margin: 0; font-size: 21px; line-height: 1.3; }}
    .identity {{ margin: 6px 0 0; color: var(--muted); font-size: 13px; }}
    .notice {{
      margin: 0 0 12px;
      padding: 10px 12px;
      border-left: 3px solid var(--amber);
      background: #fffaf1;
      font-size: 13px;
      line-height: 1.55;
    }}
    button, a.command {{
      min-height: 44px;
      border: 0;
      border-radius: 5px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }}
    button {{
      width: 100%;
      padding: 0 15px;
      background: var(--teal);
      color: #fff;
    }}
    button:disabled {{ opacity: .58; cursor: wait; }}
    #workspace {{ display: none; }}
    .viewer {{
      overflow: hidden;
      width: 100%;
      height: clamp(470px, 72vh, 880px);
      border: 1px solid var(--line);
      background: #dfe5e1;
    }}
    iframe {{ display: block; width: 100%; height: 100%; border: 0; }}
    .actions {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      margin-top: 9px;
    }}
    a.command {{
      display: inline-grid;
      place-items: center;
      padding: 0 14px;
      border: 1px solid var(--line);
      background: var(--surface);
      color: var(--teal-dark);
      text-decoration: none;
    }}
    #status {{
      min-height: 22px;
      margin: 9px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    {EMBEDDED_SAFE_AREA_CSS}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <p class="eyebrow">AGENTBRIDGE TRUSTED REMOTE AUTH</p>
      <h1>{system_name}</h1>
      <p class="identity">认证身份：{principal}</p>
    </header>
    <p class="notice">滑块、短信验证码和密码只在下方受控浏览器中处理。画面与输入不会进入智能体上下文，登录结果会由 AgentBridge 自动核验。</p>
    <button id="start" type="button">启动安全登录</button>
    <section id="workspace" aria-label="受控远程浏览器">
      <div class="viewer">
        <iframe id="remote" title="受控远程登录页面"
                sandbox="allow-scripts allow-same-origin allow-forms allow-pointer-lock"></iframe>
      </div>
      <div class="actions">
        <p id="status" role="status">正在启动隔离浏览器。</p>
        <a id="external" class="command" target="_blank" rel="noopener noreferrer">浏览器打开</a>
      </div>
    </section>
  </main>
  <script nonce="{nonce}">
    (() => {{
      const challengeId = "{challenge_id}";
      const csrfToken = "{escape(csrf_token)}";
      const startButton = document.getElementById("start");
      const workspace = document.getElementById("workspace");
      const remote = document.getElementById("remote");
      const external = document.getElementById("external");
      const status = document.getElementById("status");
      let controlToken = "";
      let stopped = false;

      const messageForVerification = (value) => ({{
        starting: "正在启动隔离浏览器。",
        awaiting_login: "请在上方完成登录，AgentBridge 会自动识别登录结果。",
        verification_deferred: "登录结果暂时无法核验，浏览器仍可继续操作。",
        verified: "身份已核验，正在保存加密会话。",
      }}[value] || "正在等待登录完成。");

      const pollStatus = async () => {{
        if (stopped || !controlToken) return;
        try {{
          const response = await fetch(
            `/auth/${{challengeId}}/interactive/status`,
            {{
              cache: "no-store",
              headers: {{ "X-AgentBridge-Control-Token": controlToken }},
            }},
          );
          const result = await response.json();
          if (result.status === "succeeded") {{
            stopped = true;
            status.textContent = "认证完成，正在返回智能体继续执行。";
            window.setTimeout(() => window.location.reload(), 350);
            return;
          }}
          if (["failed", "expired", "superseded"].includes(result.status)) {{
            stopped = true;
            status.textContent = "认证未完成，请返回智能体重新发起。";
            window.setTimeout(() => window.location.reload(), 500);
            return;
          }}
          status.textContent = messageForVerification(result.verification);
        }} catch (_error) {{
          status.textContent = "正在重新连接 AgentBridge 状态通道。";
        }}
        window.setTimeout(pollStatus, 1500);
      }};

      startButton.addEventListener("click", async () => {{
        startButton.disabled = true;
        startButton.textContent = "正在启动";
        status.textContent = "正在分配独立浏览器会话。";
        try {{
          const response = await fetch(
            `/auth/${{challengeId}}/interactive/start`,
            {{
              method: "POST",
              headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
              body: new URLSearchParams({{ csrf_token: csrfToken }}),
            }},
          );
          const result = await response.json();
          if (!response.ok || !result.controlToken || !result.remoteUrl) {{
            throw new Error("remote browser did not start");
          }}
          controlToken = result.controlToken;
          remote.src = result.remoteUrl;
          external.href = result.remoteUrl;
          startButton.style.display = "none";
          workspace.style.display = "block";
          status.textContent = "请完成滑块、账号和短信验证。";
          pollStatus();
        }} catch (_error) {{
          startButton.disabled = false;
          startButton.textContent = "重新启动安全登录";
          status.textContent = "隔离浏览器启动失败，请返回智能体重新发起。";
        }}
      }});
    }})();
  </script>
  {render_embedded_web_app_bridge(nonce=nonce)}
</body>
</html>"""


def _message_response(
    *,
    status: int,
    title: str,
    message: str,
    tone: str,
    close_when_complete: bool = False,
) -> AuthCardResponse:
    nonce = secrets.token_urlsafe(18)
    color = (
        "#087d72"
        if tone == "success"
        else "#b23a3a"
        if tone == "error"
        else "#b86c13"
    )
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{escape(title)} · AgentBridge</title>
<style nonce="{nonce}">
body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#f2f4f1;color:#17201d;font-family:"Microsoft YaHei UI",sans-serif;letter-spacing:0}}
main{{width:min(100%,440px);padding:30px;background:#fff;border:1px solid #d4dad6;border-radius:8px;box-shadow:0 18px 45px rgba(23,32,29,.1);text-align:center}}
.mark{{width:22px;height:22px;margin:0 auto 18px;border:4px solid {color};transform:rotate(45deg)}}
p{{color:#65716c;line-height:1.7}} {EMBEDDED_SAFE_AREA_CSS}
</style></head><body><main><div class="mark"></div><h1>{escape(title)}</h1>
<p>{escape(message)}</p></main>
{render_embedded_web_app_bridge(nonce=nonce, close_when_complete=close_when_complete)}
</body></html>"""
    return AuthCardResponse(status, _security_headers(nonce), html.encode("utf-8"))


def _json_response(status: int, value: dict) -> AuthCardResponse:
    return AuthCardResponse(
        status,
        {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
        },
        json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
    )


def _security_headers(
    nonce: str,
    *,
    frame_origin: str | None = None,
) -> dict[str, str]:
    frame_policy = f" frame-src {frame_origin};" if frame_origin else ""
    return {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"style-src 'nonce-{nonce}'; "
            f"script-src 'nonce-{nonce}'; "
            "connect-src 'self';"
            f"{frame_policy} "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        ),
    }


def _valid_body(body: bytes, content_type: str, expected: str) -> bool:
    return (
        len(body) <= MAX_AUTH_BODY_BYTES
        and content_type.split(";", 1)[0].strip().lower() == expected
    )


def _challenge_ttl_seconds(challenge: dict) -> int:
    try:
        expires = datetime.fromisoformat(challenge["expires_at"])
        now = datetime.now(expires.tzinfo or timezone.utc)
        return max(1, min(900, ceil((expires - now).total_seconds())))
    except (KeyError, TypeError, ValueError):
        return 300
