/** Native presentation only: never reads fields, credentials, URLs or business payloads. */
export function installTelegramBridge({window, document, closeWhenComplete = false, actionLabels = []}) {
  const launch = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  const proxy = window.TelegramWebviewProxy;
  const canPost = proxy && typeof proxy.postEvent === 'function';
  let canNotify = false;
  try { canNotify = window.external && typeof window.external.notify === 'function'; } catch {}
  if (!canPost && !canNotify) return {enabled: false};
  const root = document.documentElement;
  root.dataset.agentHost = 'telegram';
  let active = true, visible = true, lastMain = '', lastClosing = null, lastBack = null;
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
  const back = () => document.getElementById?.('agentbridge-back');
  // Only these static, registered profile actions may leave the document.
  // Never infer a native label from a title, field value, or arbitrary DOM text.
  const labels = new Set(Array.isArray(actionLabels) ? actionLabels.filter(value =>
    typeof value === 'string' && value.length > 0 && value.length <= 64 && !/[\r\n<>]/.test(value)) : []);
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
    const valid = color => typeof color === 'string' && /^#[0-9a-f]{6}$/i.test(color);
    const color = (key, fallback) => valid(value[key]) ? value[key] : fallback;
    const luminance = hex => [1, 3, 5].map(index => parseInt(hex.slice(index, index + 2), 16) / 255)
      .map(n => n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4)
      .reduce((sum, n, index) => sum + n * [.2126, .7152, .0722][index], 0);
    const contrast = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
    const readable = (preferred, backgrounds) => {
      const score = foreground => Math.min(...backgrounds.map(background => contrast(foreground, background)));
      return score(preferred) >= 4.5 ? preferred : score('#ffffff') > score('#000000') ? '#ffffff' : '#000000';
    };
    const paper = color('bg_color', '#f3f5f2'), dark = luminance(paper) < .25;
    let surface = color('section_bg_color', color('secondary_bg_color', dark ? '#202b35' : '#ffffff'));
    // Some custom themes provide opposite background families. A shared text
    // foreground cannot serve both; normalize that pair before applying it.
    if (Math.max(...['#ffffff','#000000'].map(ink => Math.min(contrast(ink, paper), contrast(ink, surface)))) < 4.5) surface = paper;
    const ink = readable(color('text_color', dark ? '#edf2f5' : '#17201d'), [paper, surface]);
    const muted = readable(color('hint_color', dark ? '#adbcc7' : '#62706a'), [paper, surface]);
    const accent = color('button_color', dark ? '#4dbea8' : '#087d72');
    const buttonInk = readable(color('button_text_color', '#ffffff'), [accent]);
    const link = readable(color('link_color', dark ? '#88d9c9' : '#075f58'), [paper, surface]);
    const subtle = dark ? '#283540' : '#f1f5f1', selected = dark ? '#214c45' : '#e5f4f1';
    const warning = dark ? '#44351d' : '#fff8ea', error = dark ? '#492a2d' : '#fff0ef';
    const disabled = dark ? '#303b44' : '#e8ede9';
    const variables = {paper, surface, ink, muted, line: color('section_separator_color', dark ? '#52606b' : '#d5dbd7'),
      teal: accent, 'teal-dark': link, 'button-ink': buttonInk, 'button-hover': accent,
      'field-bg': surface, 'field-ink': ink, 'field-border': dark ? '#7f919e' : '#899b91', 'field-label': ink,
      'subtle-bg': subtle, 'subtle-ink': readable(ink, [subtle]), 'subtle-muted': readable(muted, [subtle]), 'table-bg': subtle,
      'selected-bg': selected, 'selected-ink': readable(ink, [selected]), 'selected-border': link,
      'warning-bg': warning, 'warning-ink': dark ? '#ffdb9b' : '#684a19', 'warning-border': dark ? '#d6ab67' : '#b08038',
      'error-bg': error, 'error-ink': dark ? '#ffc4c5' : '#7f2828',
      red: readable(color('destructive_text_color', dark ? '#ffb1b4' : '#b23a3a'), [paper, surface]),
      amber: dark ? '#e7bf75' : '#9b6517', 'disabled-bg': disabled, 'disabled-ink': readable(muted, [disabled]),
      'secondary-bg': subtle, 'secondary-ink': readable(ink, [subtle]), 'focus-ring': link};
    for (const [key, value] of Object.entries(variables)) root.style.setProperty(`--${key}`, value);
    root.style.setProperty('color-scheme', dark ? 'dark' : 'light');
  }
  function sync() {
    if (!active) return;
    const current = button(), label = current?.dataset?.agentbridgeActionLabel;
    const present = Boolean(form() && current && current.hidden !== true && labels.has(label));
    if (supports(6)) {
      const data = {text: present ? label : '', is_visible: present,
        is_active: visible && present && current.disabled !== true && !locked() && !busy(), is_progress_visible: present && busy()};
      const signature = JSON.stringify(data);
      if (signature !== lastMain) { post('web_app_setup_main_button', data); lastMain = signature; }
    }
    const backPresent = Boolean(form() || back() && back().hidden !== true);
    if (supports(6, 1) && lastBack !== backPresent) {
      post('web_app_setup_back_button', {is_visible: backPresent}); lastBack = backPresent;
    }
    const protect = dirty();
    if (supports(6, 2) && lastClosing !== protect) {
      post('web_app_setup_closing_behavior', {need_confirmation: protect}); lastClosing = protect;
    }
  }
  function refreshNative() {
    lastMain = ''; lastBack = null; lastClosing = null;
    if (supports(6)) { post('web_app_request_theme'); post('web_app_request_viewport'); }
    if (supports(8)) { post('web_app_request_safe_area'); post('web_app_request_content_safe_area'); }
    viewport(); sync();
  }
  function receive(eventType, data) {
    if (!active) return;
    if (eventType === 'theme_changed') theme(data?.theme_params);
    else if (eventType === 'viewport_changed') viewport(data);
    else if (eventType === 'safe_area_changed') safeArea(data, 'safe');
    else if (eventType === 'content_safe_area_changed') safeArea(data, 'content-safe');
    else if (eventType === 'visibility_changed' && supports(8) && typeof data?.is_visible === 'boolean') {
      const wasVisible = visible; visible = data.is_visible;
      if (visible) {
        refreshNative();
        if (!wasVisible && window.CustomEvent) document.dispatchEvent?.(new window.CustomEvent('agentbridge:native-resume'));
      } else sync();
    } else if (eventType === 'main_button_pressed' && visible && supports(6)) {
      const current = button();
      if (form() && current && labels.has(current.dataset?.agentbridgeActionLabel) && current.hidden !== true && current.disabled !== true && !locked() && !busy()) current.click();
      sync();
    } else if (eventType === 'back_button_pressed' && visible && supports(6, 1) && (form() || back())) {
      const target = back();
      if (target && target.hidden !== true) { if (target.disabled !== true) target.click(); return; }
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
  window.addEventListener('pageshow', () => { active = true; visible = true; refreshNative(); });
  document.addEventListener?.('agentbridge:draft-state', sync);
  const observer = window.MutationObserver ? new window.MutationObserver(sync) : null;
  if (observer && form()) observer.observe(form(), {attributes: true, subtree: true, attributeFilter:
    ['disabled', 'hidden', 'data-agentbridge-action-label', 'data-agentbridge-dirty', 'data-agentbridge-busy', 'data-agentbridge-locked']});
  post('web_app_ready'); post('web_app_expand');
  refreshNative();
  if (closeWhenComplete) window.setTimeout(() => { if (active && !dirty()) post('web_app_close'); }, 1200);
  return {enabled: true, sync};
}
