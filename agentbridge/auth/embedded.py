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
  postEvent("web_app_ready");
  postEvent("web_app_expand");
  if ({completion} && (platform || canPost || canNotify)) {{
    window.setTimeout(() => postEvent("web_app_close"), 1200);
  }}
}})();
</script>
"""


EMBEDDED_SAFE_AREA_CSS = """
html[data-agent-host="telegram"] body {
  padding-top: max(20px, env(safe-area-inset-top));
  padding-right: max(16px, env(safe-area-inset-right));
  padding-bottom: max(20px, env(safe-area-inset-bottom));
  padding-left: max(16px, env(safe-area-inset-left));
  overscroll-behavior: contain;
}
"""
