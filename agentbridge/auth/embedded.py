from __future__ import annotations

from html import escape


def render_embedded_web_app_bridge(*, nonce: str, close_when_complete: bool = False) -> str:
    """Render a data-blind Telegram Mini App lifecycle bridge."""

    escaped_nonce = escape(nonce, quote=True)
    completion = "true" if close_when_complete else "false"
    return f"""
<script nonce="{escaped_nonce}">
(() => {{
  const launch = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const platform = launch.get("tgWebAppPlatform");
  const proxy = window.TelegramWebviewProxy;
  const canPost = proxy && typeof proxy.postEvent === "function";
  let canNotify = false;
  try {{
    canNotify = window.external && typeof window.external.notify === "function";
  }} catch (_error) {{
    canNotify = false;
  }}
  const postEvent = (eventType, eventData = {{}}) => {{
    try {{
      if (canPost) {{
        proxy.postEvent(eventType, JSON.stringify(eventData));
        return true;
      }}
      if (canNotify) {{
        window.external.notify(JSON.stringify({{ eventType, eventData }}));
        return true;
      }}
    }} catch (_error) {{
      return false;
    }}
    return false;
  }};
  if (!platform && !canPost && !canNotify) return;
  document.documentElement.dataset.agentHost = "telegram";
  // Only viewport geometry is observed; fields, credentials and launch data
  // are never sent to the host or persisted by this lifecycle bridge.
  const updateViewport = () => {{
    const height = Number(window.visualViewport?.height || window.innerHeight);
    if (Number.isFinite(height) && height > 0) {{
      document.documentElement.style.setProperty("--agent-viewport-height", `${{height}}px`);
    }}
  }};
  updateViewport();
  window.visualViewport?.addEventListener("resize", updateViewport);
  window.addEventListener("pageshow", updateViewport);
  postEvent("web_app_ready");
  postEvent("web_app_expand");
  if ({completion} && (platform || canPost || canNotify)) {{
    window.setTimeout(() => postEvent("web_app_close"), 1200);
  }}
}})();
</script>
<p class="embedded-session-note">关闭此窗口不会取消原任务。未保存的输入可能丢失；已确认的操作会继续办理，请从原任务查看结果，勿重复提交。</p>
"""


EMBEDDED_SAFE_AREA_CSS = """
.embedded-session-note { display: none; }
html[data-agent-host="telegram"] { scroll-padding-block: 20px; }
html[data-agent-host="telegram"] body {
  padding-top: max(20px, env(safe-area-inset-top));
  padding-right: max(16px, env(safe-area-inset-right));
  padding-bottom: max(20px, env(safe-area-inset-bottom));
  padding-left: max(16px, env(safe-area-inset-left));
  overscroll-behavior: contain;
}
html[data-agent-host="telegram"] .shell,
html[data-agent-host="telegram"] .auth-shell {
  min-height: calc(var(--agent-viewport-height, 100dvh) - 64px);
  align-items: flex-start;
  padding: 12px 0 20px;
}
html[data-agent-host="telegram"] .card,
html[data-agent-host="telegram"] .auth-card { min-width: 0; }
html[data-agent-host="telegram"] .embedded-session-note {
  display: block; max-width: 760px; margin: 0 auto 12px;
  font-size: 12px; line-height: 1.7; color: var(--muted);
}
html[data-agent-host="telegram"] .material-table {
  max-width: 100%; overflow-x: auto; overscroll-behavior-x: contain;
  -webkit-overflow-scrolling: touch;
}
html[data-agent-host="telegram"] summary { min-height: 44px; padding-block: 10px; }
@media (max-width: 620px) {
  html[data-agent-host="telegram"] input:not([type="radio"]):not([type="checkbox"]),
  html[data-agent-host="telegram"] select,
  html[data-agent-host="telegram"] textarea { font-size: 16px; }
  html[data-agent-host="telegram"] button,
  html[data-agent-host="telegram"] a[id="flow-continue"] { min-height: 44px; }
}
"""
