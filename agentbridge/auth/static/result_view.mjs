// The bearer and CSRF proof stay within this trusted same-origin document.
export function installResultView({ document, window, fetch, initial, setTimeout, clearTimeout }) {
  const byId = (id) => document.getElementById(id);
  const root = byId('result-groups');
  const status = byId('result-status');
  const prepare = byId('result-prepare');
  const recover = byId('result-recover');
  const resume = byId('result-resume');
  let state = initial.state, selection = new Set(), operationId = null;
  let pending = null, busy = false, stopped = false, epoch = 0, timer = null, readController = null;
  let readTicket = 0, navigated = false, choices = [], resumeButtons = [];
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const terminalStates = new Set(['unknown', 'outcome_unknown', 'failed', 'canceled', 'expired',
    'succeeded', 'completed', 'partially_succeeded']);
  const storageKey = `agentbridge-result-view:${initial.path}`;
  const say = (text) => { status.textContent = text; };
  const save = () => { try { window.sessionStorage.setItem(storageKey, JSON.stringify(pending)); } catch {} };
  try { pending = JSON.parse(window.sessionStorage.getItem(storageKey) || 'null'); } catch {}
  if (pending) {
    const body = pending.body;
    const bodyValid = body === undefined || (body && typeof body === 'object' && !Array.isArray(body)
      && Object.keys(body).sort().join(',') === 'operationId,requestKey,targetKeys'
      && /^[A-Za-z0-9_-]{1,128}$/.test(body.operationId || '')
      && /^[A-Za-z0-9_-]{12,128}$/.test(body.requestKey || '')
      && Array.isArray(body.targetKeys) && body.targetKeys.length > 0 && body.targetKeys.length <= 100
      && body.targetKeys.every(key => typeof key === 'string' && /^[0-9a-f]{64}$/.test(key))
      && new Set(body.targetKeys).size === body.targetKeys.length);
    if (typeof pending !== 'object' || Array.isArray(pending)
        || Object.keys(pending).some(key => !['taskId', 'body'].includes(key))
        || (pending.taskId !== undefined && !uuid.test(pending.taskId))
        || (!pending.taskId && !body) || !bodyValid) pending = null;
  }
  function element(tag, text) { const e = document.createElement(tag); if (text) e.textContent = text; return e; }
  function count() {
    byId('result-count').textContent = `已选择 ${selection.size} 项；每项仍需单独确认。`;
    prepare.disabled = busy || Boolean(pending) || selection.size === 0;
    prepare.textContent = selection.size === 1 ? '核对这 1 项' : selection.size ? `逐项核对 ${selection.size} 项` : '请选择待办';
    for (const choice of choices) {
      choice.input.disabled = busy || Boolean(pending) || !choice.enabled;
      choice.input.checked = operationId === choice.operationId && selection.has(choice.targetKey);
    }
    for (const button of resumeButtons) button.disabled = busy || Boolean(pending);
  }
  function render() {
    byId('result-title').textContent = state.title || '选择待办并核对办理';
    root.replaceChildren(); resume.replaceChildren();
    choices = []; resumeButtons = [];
    for (const group of state.groups || []) {
      const section = element('section'); section.append(element('h2', group.title || 'OA 待办'));
      section.append(element('p', `来源查询时间：${group.fetchedAt || '未提供'}；办理选择有效至：${group.expiresAt || '未提供'}`));
      for (const target of group.targets || []) {
        const label = element('label'); label.className = 'result-choice';
        const input = element('input'); input.type = 'checkbox'; input.disabled = !target.enabled;
        choices.push({input, operationId: group.operationId, targetKey: target.targetKey, enabled: target.enabled === true});
        input.checked = operationId === group.operationId && selection.has(target.targetKey);
        input.addEventListener('change', () => {
          if (input.disabled || busy || pending || stopped) return;
          if (operationId !== group.operationId) { operationId = group.operationId; selection.clear(); }
          if (input.checked) selection.add(target.targetKey); else selection.delete(target.targetKey);
          count();
        });
        label.append(input, element('span', `${target.workflowLabel || '待办'} · ${target.title || '无标题'}`));
        section.append(label);
        section.append(element('p', target.enabled ? [target.sender, target.date].filter(Boolean).join(' · ') : target.disabledReason || '此项暂不可直接办理，请回原聊天核对。'));
      }
      root.append(section);
    }
    if (!(state.groups || []).length) root.append(element('p', state.message || '当前没有可新选的待办，请回到原聊天重新查询。'));
    for (const item of state.preparations || []) {
      if (!item.taskId) continue;
      const button = element('button', '继续原办理任务'); button.type = 'button';
      button.addEventListener('click', () => {
        if (stopped || busy || pending || !uuid.test(item.taskId)) return;
        pending = { taskId: item.taskId }; save(); count(); poll();
      });
      resumeButtons.push(button);
      resume.append(button);
    }
    recover.hidden = !pending?.body || Boolean(pending.taskId);
    count();
  }
  async function request(action, body, taskId) {
    const requestEpoch = epoch;
    const controller = action === 'state' ? new AbortController() : null;
    if (controller) { readController?.abort(); readController = controller; }
    const response = await fetch(`${initial.path}/${action}${taskId ? `?task=${encodeURIComponent(taskId)}` : ''}`, {
      method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
      headers: { 'X-AgentBridge-Result-Token': initial.csrfToken, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}), ...(controller ? { signal: controller.signal } : {}),
    });
    const value = await response.json();
    if (stopped || epoch !== requestEpoch) return null;
    if (!response.ok) throw new Error(value.error?.message || '办理状态暂不可用，请从原聊天核对。');
    return value;
  }
  function safeAction(interaction) {
    if (!interaction || !['pending', 'processing'].includes(interaction.state)) return null;
    try {
      const url = new URL(interaction.presentation.url, window.location.href);
      if (url.origin !== window.location.origin || url.username || url.password || url.search || url.hash
          || !/^\/flow\/(input|authorize)\/[A-Za-z0-9_-]{32,128}(?:\/present\/[A-Za-z0-9_-]{32,128})?$/.test(url.pathname)) return null;
      url.hash = window.location.hash; return url.href;
    } catch { return null; }
  }
  async function poll() {
    if (stopped || navigated || !pending?.taskId) return;
    const taskId = pending.taskId, pollEpoch = epoch, ticket = ++readTicket;
    const current = () => !stopped && epoch === pollEpoch && ticket === readTicket && pending?.taskId === taskId;
    clearTimeout(timer);
    try {
      const value = await request('state', null, taskId);
      if (!value || !current()) return;
      say(value.message || value.title || '正在准备材料，请稍候。');
      const url = safeAction(value.interaction);
      const terminal = value.terminal === true || terminalStates.has(value.state);
      if (url && !terminal) { navigated = true; window.location.replace(url); return; }
      if (terminal) {
        recover.hidden = true;
        say(value.message || value.title || '任务已结束，请回原聊天查看核验结果。');
        if (['succeeded', 'failed', 'canceled', 'expired'].includes(value.state)) {
          const back = element('button', '返回待办选择'); back.type = 'button';
          back.addEventListener('click', () => {
            if (!current()) return;
            pending = null; save(); selection.clear(); operationId = null;
            restore(); // A fresh read, never a repeated preparation or decision.
          });
          resume.replaceChildren(back);
        }
        return;
      }
      timer = setTimeout(poll, 1000);
    } catch (error) { if (current() && error.name !== 'AbortError') { say(error.message); timer = setTimeout(poll, 3000); } }
  }
  async function submit() {
    if (busy || stopped) return;
    if (!pending) {
      if (!operationId || !selection.size) return;
      pending = { body: { operationId, targetKeys: [...selection], requestKey: window.crypto.randomUUID() } }; save();
    }
    if (!pending.body || pending.taskId) return;
    const submitEpoch = epoch;
    busy = true; count(); recover.disabled = true; say('正在准备所选事项；尚未授权或提交。');
    try {
      const value = await request('prepare', pending.body);
      if (!value) return;
      if (typeof value.taskId !== 'string' || !uuid.test(value.taskId)) throw new Error('未取得原准备任务，请恢复同一请求核对。');
      pending.taskId = value.taskId; save(); recover.hidden = true; await poll();
    } catch (error) {
      if (!stopped && submitEpoch === epoch) { say(`${error.message} 如未取得回执，请恢复同一准备请求，不要另建重复任务。`); recover.hidden = false; }
    } finally { if (submitEpoch === epoch) { busy = false; if (!stopped) { recover.disabled = false; count(); } } }
  }
  async function restore() {
    stopped = false; epoch++; busy = !pending?.taskId; recover.disabled = busy;
    navigated = false; readTicket++; clearTimeout(timer); readController?.abort();
    const restoreEpoch = epoch;
    render();
    if (pending?.taskId) { poll(); return; }
    let refreshed = false;
    try { const value = await request('state'); if (value) { state = value; refreshed = true; } }
    catch (error) { if (!stopped && restoreEpoch === epoch && error.name !== 'AbortError') say(error.message); }
    finally { if (!stopped && restoreEpoch === epoch) { busy = !refreshed; recover.disabled = busy; render(); } }
  }
  prepare.addEventListener('click', submit); recover.addEventListener('click', submit);
  window.addEventListener('pagehide', () => { stopped = true; epoch++; clearTimeout(timer); readController?.abort(); });
  window.addEventListener('pageshow', (event) => { if (event.persisted || stopped) restore(); });
  render(); if (pending?.taskId) poll();
  return { restore, safeAction };
}

if (typeof document !== 'undefined' && document.getElementById('result-data')) {
  installResultView({ document, window, fetch: window.fetch.bind(window),
    initial: JSON.parse(document.getElementById('result-data').textContent),
    setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window) });
}
