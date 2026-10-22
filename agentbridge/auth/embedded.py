from __future__ import annotations

from html import escape
from pathlib import Path


_BRIDGE_SCRIPT = Path(__file__).with_name("static") / "telegram_bridge.mjs"


def render_embedded_web_app_bridge(*, nonce: str, close_when_complete: bool = False) -> str:
    """Render the local, data-blind Telegram native presentation bridge."""

    escaped_nonce = escape(nonce, quote=True)
    completion = "true" if close_when_complete else "false"
    script = _BRIDGE_SCRIPT.read_text().replace("export function installTelegramBridge", "function installTelegramBridge", 1)
    return f"""
<script nonce="{escaped_nonce}">
(() => {{
{script}
  installTelegramBridge({{window, document, closeWhenComplete: {completion}}});
}})();
</script>
<p class="embedded-session-note">关闭此窗口不会取消原任务。未保存的输入可能丢失；已确认的操作会继续办理，请从原任务查看结果，勿重复提交。</p>
"""


EMBEDDED_SAFE_AREA_CSS = """
.embedded-session-note { display: none; }
html[data-agent-host="telegram"] { scroll-padding-block: 20px; }
html[data-agent-host="telegram"] body {
  padding-top: max(20px, env(safe-area-inset-top), var(--agent-safe-top, 0px), var(--agent-content-safe-top, 0px));
  padding-right: max(16px, env(safe-area-inset-right), var(--agent-safe-right, 0px), var(--agent-content-safe-right, 0px));
  padding-bottom: max(20px, env(safe-area-inset-bottom), var(--agent-safe-bottom, 0px), var(--agent-content-safe-bottom, 0px));
  padding-left: max(16px, env(safe-area-inset-left), var(--agent-safe-left, 0px), var(--agent-content-safe-left, 0px));
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
html[data-agent-host="telegram"] .review,
html[data-agent-host="telegram"] input,
html[data-agent-host="telegram"] select,
html[data-agent-host="telegram"] textarea { background: var(--surface); color: var(--ink); }
html[data-agent-host="telegram"] label,
html[data-agent-host="telegram"] legend { color: var(--ink); }
html[data-agent-host="telegram"] .reading-table th { background: var(--paper); }
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
