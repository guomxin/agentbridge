import {isCancellation} from './workspace_lifecycle.mjs';

const path = '/api/oa-daily-digest';
const unavailable = '__unavailable__';
const validTime = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
const shanghaiTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23',
});
const lastStatus = {
  published:'已生成简报', partial:'已生成简报，Telegram 私聊不可用', reused:'当日简报已生成',
  endpoint_unavailable:'所选收件渠道不可用', permission_unavailable:'OA 查询权限不可用', failed:'简报生成失败',
};

function localTime(value) {
  const ms = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(ms) ? shanghaiTime.format(ms) : '';
}

export function createDailyDigestSettings({document, $, getScope, request}) {
  const field = name => $(`#oa-digest-${name}`);
  const disclosure = field('settings'), summary = field('summary'), form = field('form');
  const enabled = field('enabled'), time = field('time'), workspace = field('workspace'), telegram = field('telegram');
  const telegramNote = field('telegram-note'), runs = field('runs'), status = field('status');
  const save = field('save'), reread = field('reload');
  let panelScope, saved, baseline, pending, needsRead = false;

  const current = scope => panelScope === scope && scope.current();
  const values = () => ({enabled:enabled.checked, time:time.value, workspace:workspace.checked,
    telegramEndpointId:telegram.value || null});
  const edits = () => baseline ? Object.fromEntries(Object.entries(values()).filter(([key, value]) => value !== baseline[key])) : {};
  const isEdited = () => Object.keys(edits()).length > 0;

  function updateControls() {
    const canConfigure = saved?.canConfigure === true;
    const canStop = saved?.enabled === true && !enabled.checked;
    const busy = Boolean(pending);
    enabled.disabled = busy || !canConfigure && !saved?.enabled;
    time.disabled = workspace.disabled = busy || !canConfigure;
    telegram.disabled = busy || !canConfigure || !saved.telegramOptions.length && telegram.value !== unavailable;
    save.disabled = busy || needsRead || !isEdited() || !canConfigure && !canStop;
    reread.disabled = busy;
    form.setAttribute('aria-busy', String(busy));
    form.hidden = !saved || !canConfigure && !saved.enabled;
    summary.textContent = saved?.enabled ? `已开启 · 每天 ${saved.time}` : saved ? '已停用' : pending === 'load' ? '正在读取' : '';
    telegramNote.textContent = telegram.value === unavailable
      ? '此前选择的私聊不可用，请重新选择，或取消发送到 Telegram。'
      : !saved?.telegramOptions.length ? '当前没有已绑定的 Telegram 私聊。' : '';
    telegramNote.hidden = !telegramNote.textContent;
  }

  function message(value, {retry = false} = {}) {
    status.textContent = value;
    reread.hidden = !retry;
  }

  function option(value, label, disabled = false) {
    const item = document.createElement('option');
    item.value = value;
    item.textContent = label;
    item.disabled = disabled;
    return item;
  }

  function showSettings(value, draft = {}) {
    if (!Number.isSafeInteger(value?.version) || !validTime.test(value?.time) || !Array.isArray(value?.telegramOptions)) {
      throw new Error('Invalid daily digest settings');
    }
    saved = value;
    const options = value.telegramOptions;
    const target = id => id && options.some(item => item.id === id) ? id : id ? unavailable : null;
    baseline = {enabled:value.enabled, time:value.time, workspace:value.workspace,
      telegramEndpointId:value.telegramEndpointId && value.telegramAvailable === false ? unavailable : target(value.telegramEndpointId)};
    const shown = {...baseline, ...(value.canConfigure ? draft : {})};
    const selected = target(shown.telegramEndpointId);
    const entries = [option('', '不发送到 Telegram'), ...options.map(item => option(item.id, item.label))];
    if (selected === unavailable) entries.push(option(unavailable, '此前选择的私聊不可用', true));
    telegram.replaceChildren(...entries);
    telegram.value = selected || '';
    enabled.checked = shown.enabled;
    time.value = shown.time;
    workspace.checked = shown.workspace;
    const next = localTime(value.nextRunAt), last = localTime(value.lastRunAt);
    runs.textContent = [next && `下次发送 ${next}（北京时间）`,
      last && `上次执行 ${last}（北京时间）${lastStatus[value.lastStatus] ? `，${lastStatus[value.lastStatus]}` : ''}`]
      .filter(Boolean).join('　');
    runs.hidden = !runs.textContent;
    updateControls();
  }

  function loseAccess(error) {
    if (error?.status !== 401 && error?.status !== 403) return false;
    saved = baseline = null;
    enabled.checked = workspace.checked = false;
    time.value = telegram.value = '';
    telegram.replaceChildren();
    runs.textContent = '';
    runs.hidden = true;
    message(error.status === 401 ? '登录已失效，请重新登录。' : '当前账号无法读取简报设置。', {retry:true});
    return true;
  }

  async function refresh({review = false} = {}) {
    const scope = panelScope;
    if (!scope?.current() || pending || !review && (needsRead || isEdited())) return;
    pending = 'load';
    updateControls();
    if (!saved) message('正在读取简报设置。');
    try {
      const result = await request(path, scope, {timeoutMs:15000});
      if (!current(scope)) return;
      showSettings(result, edits());
      needsRead = false;
      message(result.canConfigure ? isEdited() ? '已读取当前设置，草稿仍待保存。' : ''
        : result.enabled ? '当前账号没有 OA 查询权限，仍可停用简报。' : '当前账号没有 OA 简报设置权限。');
    } catch (error) {
      if (!current(scope) || isCancellation(error)) return;
      if (!loseAccess(error)) message('暂时无法读取简报设置，请重试。', {retry:true});
    } finally {
      if (current(scope)) { pending = null; updateControls(); }
    }
  }

  async function submit(event) {
    event.preventDefault();
    const scope = panelScope;
    if (!scope?.current() || save.disabled) return;
    const draft = values();
    if (!validTime.test(draft.time)) { message('请选择有效的发送时间。'); time.focus(); return; }
    if (draft.enabled && draft.telegramEndpointId === unavailable) {
      message('请重新选择 Telegram 私聊，或取消发送到 Telegram。'); telegram.focus(); return;
    }
    if (draft.enabled && !draft.workspace && !draft.telegramEndpointId) {
      message('请至少选择一个接收渠道。'); workspace.focus(); return;
    }
    const body = {...draft, expectedVersion:saved.version};
    if (body.telegramEndpointId === unavailable) body.telegramEndpointId = saved.telegramEndpointId;
    pending = 'save';
    updateControls();
    message('正在保存简报设置。');
    try {
      const result = await request(path, scope, {method:'POST', csrf:true, body, timeoutMs:15000});
      if (!current(scope)) return;
      showSettings(result);
      message(result.enabled ? '简报设置已保存。' : '简报已停用，发送设置已保存。');
    } catch (error) {
      if (!current(scope) || isCancellation(error)) return;
      if (loseAccess(error)) return;
      needsRead = true;
      const notice = error?.code === 'DIGEST_TELEGRAM_UNAVAILABLE'
        ? '所选 Telegram 私聊已不可用，请重新读取并选择收件渠道。'
        : error?.status === 409 ? '简报设置已在其他页面更新，请重新读取后保存。'
          : '暂未确认保存，请重新读取简报设置。';
      message(notice, {retry:true});
    } finally {
      if (current(scope)) { pending = null; updateControls(); }
    }
  }

  function deactivate() {
    if (pending === 'save') {
      needsRead = true;
      message('暂未确认保存，请重新读取简报设置。', {retry:true});
    }
    panelScope?.dispose();
    panelScope = pending = null;
    updateControls();
  }

  function activate() {
    if (panelScope?.current()) return;
    panelScope = getScope().replace('oa-digest-settings');
    return refresh();
  }

  function reset() {
    deactivate();
    saved = baseline = null;
    needsRead = false;
    disclosure.open = false;
    enabled.checked = workspace.checked = false;
    time.value = telegram.value = '';
    telegram.replaceChildren();
    runs.textContent = '';
    runs.hidden = true;
    message('');
    updateControls();
  }

  form.addEventListener('submit', submit);
  form.addEventListener('input', () => {
    if (!needsRead && !pending && saved?.canConfigure) message('');
    updateControls();
  });
  reread.addEventListener('click', () => refresh({review:true}));
  reset();
  return {activate, deactivate, refresh, reset};
}
