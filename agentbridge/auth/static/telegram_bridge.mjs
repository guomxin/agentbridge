/** Native presentation only: never reads fields, credentials, URLs or business payloads. */
export function installTelegramBridge({window, document, closeWhenComplete = false}) {
  const launch = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  const proxy = window.TelegramWebviewProxy;
  const canPost = proxy && typeof proxy.postEvent === 'function';
  let canNotify = false;
  try { canNotify = window.external && typeof window.external.notify === 'function'; } catch {}
  if (!canPost && !canNotify) return {enabled: false};
  const root = document.documentElement;
  root.dataset.agentHost = 'telegram';
  let active = true, lastMain = '', lastClosing = null, lastBack = null;
  const version = /^\d{1,2}\.\d{1,2}(?:\.\d{1,2})?$/.test(launch.get('tgWebAppVersion') || '')
    ? launch.get('tgWebAppVersion').split('.').map(Number) : [0, 0];
  const supports = (major, minor = 0) => version[0] > major || version[0] === major && version[1] >= minor;
  const post = (eventType, eventData = {}) => {
    try {
      if (canPost) { proxy.postEvent(eventType, JSON.stringify(eventData)); return true; }
      if (canNotify) { window.external.notify(JSON.stringify({eventType, eventData})); return true; }
    } catch {}
    return false;
  };
  const form = () => document.getElementById?.('draft-form');
  const button = () => document.getElementById?.('draft-submit');
  const dirty = () => form()?.dataset.agentbridgeDirty === 'true';
  const busy = () => form()?.dataset.agentbridgeBusy === 'true';
  const locked = () => form()?.dataset.agentbridgeLocked === 'true';
  const number = (value, maximum) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= maximum;
  function viewport(data) {
    const height = data?.height ?? window.visualViewport?.height ?? window.innerHeight;
    if (number(height, 20000) && height > 0) {
      root.style.setProperty('--agent-viewport-height', `${height}px`);
      if (data?.is_state_stable === true) root.style.setProperty('--agent-viewport-stable-height', `${height}px`);
    }
  }
  function safeArea(data, prefix) {
    for (const edge of ['top', 'right', 'bottom', 'left']) {
      if (number(data?.[edge], 1000)) root.style.setProperty(`--agent-${prefix}-${edge}`, `${data[edge]}px`);
    }
  }
  function theme(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return;
    const colors = {bg_color: '--paper', secondary_bg_color: '--surface', text_color: '--ink', hint_color: '--muted',
      button_color: '--teal', link_color: '--teal-dark', section_separator_color: '--line'};
    for (const [key, variable] of Object.entries(colors)) {
      if (typeof value[key] === 'string' && /^#[0-9a-f]{6}$/i.test(value[key])) root.style.setProperty(variable, value[key]);
    }
    if (typeof value.bg_color === 'string' && /^#[0-9a-f]{6}$/i.test(value.bg_color)) {
      const rgb = [1, 3, 5].map(index => parseInt(value.bg_color.slice(index, index + 2), 16));
      root.style.setProperty('color-scheme', rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114 < 128 ? 'dark' : 'light');
    }
  }
  function sync() {
    if (!active) return;
    const current = button(), present = Boolean(form() && current && current.hidden !== true);
    if (supports(6)) {
      const data = {text: '确认本页操作', is_visible: present,
        is_active: present && current.disabled !== true && !locked() && !busy(), is_progress_visible: present && busy()};
      const signature = JSON.stringify(data);
      if (signature !== lastMain) { post('web_app_setup_main_button', data); lastMain = signature; }
    }
    if (supports(6, 1) && lastBack !== present) {
      post('web_app_setup_back_button', {is_visible: present}); lastBack = present;
    }
    const protect = dirty();
    if (supports(6, 2) && lastClosing !== protect) {
      post('web_app_setup_closing_behavior', {need_confirmation: protect}); lastClosing = protect;
    }
  }
  function receive(eventType, data) {
    if (!active) return;
    if (eventType === 'theme_changed') theme(data?.theme_params);
    else if (eventType === 'viewport_changed') viewport(data);
    else if (eventType === 'safe_area_changed') safeArea(data, 'safe');
    else if (eventType === 'content_safe_area_changed') safeArea(data, 'content-safe');
    else if (eventType === 'main_button_pressed' && supports(6)) {
      const current = button();
      if (form() && current && current.hidden !== true && current.disabled !== true && !locked() && !busy()) current.click();
      sync();
    } else if (eventType === 'back_button_pressed' && supports(6, 1) && form()) {
      if (dirty() && (typeof window.confirm !== 'function' || !window.confirm('本页还有未保存的输入。离开可能丢失这些输入，已开始的任务会继续。确认返回聊天？'))) return;
      post('web_app_close');
    }
  }
  // Preserve an existing receiver while adapting the three native client entrypoints.
  function hook(target, key) {
    const previous = target[key];
    target[key] = function(eventType, data) {
      if (typeof previous === 'function') { try { previous.call(this, eventType, data); } catch {} }
      receive(eventType, data);
    };
  }
  const telegram = window.Telegram && typeof window.Telegram === 'object' ? window.Telegram : (window.Telegram = {});
  const webview = telegram.WebView && typeof telegram.WebView === 'object' ? telegram.WebView : (telegram.WebView = {});
  const game = window.TelegramGameProxy && typeof window.TelegramGameProxy === 'object' ? window.TelegramGameProxy : (window.TelegramGameProxy = {});
  hook(webview, 'receiveEvent'); hook(game, 'receiveEvent'); hook(window, 'TelegramGameProxy_receiveEvent');
  try {
    const raw = launch.get('tgWebAppThemeParams');
    if (raw && raw.length <= 4096) theme(JSON.parse(raw));
  } catch {}
  viewport();
  window.visualViewport?.addEventListener('resize', () => viewport());
  window.addEventListener('pagehide', () => {
    active = false;
    if (supports(6)) post('web_app_setup_main_button', {is_visible: false});
    if (supports(6, 1)) post('web_app_setup_back_button', {is_visible: false});
  });
  window.addEventListener('pageshow', () => { active = true; lastMain = ''; lastBack = null; viewport(); sync(); });
  document.addEventListener?.('agentbridge:draft-state', sync);
  const observer = window.MutationObserver ? new window.MutationObserver(sync) : null;
  if (observer && form()) observer.observe(form(), {attributes: true, subtree: true, attributeFilter:
    ['disabled', 'hidden', 'data-agentbridge-dirty', 'data-agentbridge-busy', 'data-agentbridge-locked']});
  post('web_app_ready'); post('web_app_expand');
  if (supports(6)) { post('web_app_request_theme'); post('web_app_request_viewport'); }
  if (supports(8)) { post('web_app_request_safe_area'); post('web_app_request_content_safe_area'); }
  sync();
  if (closeWhenComplete) window.setTimeout(() => { if (active && !dirty()) post('web_app_close'); }, 1200);
  return {enabled: true, sync};
}
