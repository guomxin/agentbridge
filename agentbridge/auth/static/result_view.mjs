// The bearer and CSRF proof stay within this trusted same-origin document.
export function installResultView({ document, window, fetch, initial, setTimeout, clearTimeout, now = Date.now }) {
  const byId = (id) => document.getElementById(id);
  const root = byId('result-groups');
  const status = byId('result-status');
  const prepare = byId('result-prepare');
  const recover = byId('result-recover');
  const resume = byId('result-resume');
  const refresh = byId('result-refresh'), queryRecover = byId('result-query-recover');
  const search = byId('result-search'), typeFilter = byId('result-type'), selectedOnly = byId('result-selected-only');
  let state = initial.state, selection = new Set(), operationId = null;
  let pending = null, busy = false, stopped = false, epoch = 0, timer = null, readController = null;
  let readTicket = 0, navigated = false, choices = [], resumeButtons = [];
  let unavailable = false, recoverRead = false, waitingSince = null;
  let queryPending = null, queryFinished = false, queryRecoveryNeeded = false, groupActions = [], queryTimer = null, expiryTimer = null;
  const requests = new Set();
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const terminalStates = new Set(['unknown', 'outcome_unknown', 'failed', 'canceled', 'expired',
    'succeeded', 'completed', 'partially_succeeded', 'superseded', 'skipped']);
  const activeStates = new Set(['active', 'running', 'waiting_user', 'paused']);
  const permanentError = error => error.status >= 400 && error.status < 500 && ![408, 425, 429].includes(error.status);
  const storageKey = `agentbridge-result-view:${initial.path}`;
  const queryStorageKey = `${storageKey}:query`;
  const say = (text) => { status.textContent = text; };
  const save = () => { try { window.sessionStorage.setItem(storageKey, JSON.stringify(pending)); } catch {} };
  const saveQuery = () => { try { window.sessionStorage.setItem(queryStorageKey, JSON.stringify(queryPending)); } catch {} };
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
  try { queryPending = JSON.parse(window.sessionStorage.getItem(queryStorageKey) || 'null'); } catch {}
  if (queryPending && (typeof queryPending !== 'object' || Array.isArray(queryPending)
      || Object.keys(queryPending).some(key => !['taskId', 'body'].includes(key))
      || !queryPending.body || Object.keys(queryPending.body).join(',') !== 'requestKey'
      || !/^[A-Za-z0-9_-]{12,128}$/.test(queryPending.body.requestKey || '')
      || queryPending.taskId !== undefined && !uuid.test(queryPending.taskId))) queryPending = null;
  // A selection being prepared always owns this page. Never start a read shortcut
  // beside an unresolved business preparation, including altered browser storage.
  if (pending && queryPending) queryPending = null;
  queryRecoveryNeeded = Boolean(queryPending && !queryPending.taskId);
  function element(tag, text) { const e = document.createElement(tag); if (text) e.textContent = text; return e; }
  const localTime = value => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '未提供' : date.toLocaleString('zh-CN', {hour12: false});
  };
  const stateLabel = value => ({active:'等待准备',running:'正在办理',waiting_user:'等待核对',paused:'已暂停',
    succeeded:'已核验完成',completed:'任务已结束',partially_succeeded:'部分已完成',failed:'未完成',
    canceled:'已取消',expired:'已过期',superseded:'已被替代',skipped:'已跳过',unknown:'结果待核实',outcome_unknown:'结果待核实'}[value] || '状态待核对');
  const blocked = () => unavailable || navigated || busy || Boolean(pending) || Boolean(queryPending);
  const sourceExpiry = value => value === undefined ? Infinity : new Date(value).getTime();
  const eligible = choice => choice.enabled && choice.expiresAt > now();
  function matches(choice) {
    const term = String(search?.value || '').trim().toLocaleLowerCase();
    return (!term || choice.search.includes(term)) && (!typeFilter?.value || choice.type === typeFilter.value)
      && (!selectedOnly?.checked || operationId === choice.operationId && selection.has(choice.targetKey));
  }
  function count() {
    clearTimeout(expiryTimer);
    // Expiry only changes new selections. A frozen preparation still recovers
    // with its original request key, even after its source has aged out.
    if (!pending) for (const key of selection) {
      if (!choices.some(choice => choice.operationId === operationId && choice.targetKey === key && eligible(choice))) selection.delete(key);
    }
    const visible = choices.filter(matches), enabled = visible.filter(eligible);
    byId('result-count').textContent = `显示 ${visible.length} 项，其中可办 ${enabled.length} 项、不可直接办理 ${visible.length - enabled.length} 项。已选择 ${selection.size} 项；每项仍需单独确认。`;
    prepare.disabled = blocked() || selection.size === 0;
    prepare.textContent = selection.size === 1 ? '核对这 1 项' : selection.size ? `逐项核对 ${selection.size} 项` : '请选择待办';
    for (const choice of choices) {
      choice.input.disabled = blocked() || !eligible(choice);
      choice.input.checked = operationId === choice.operationId && selection.has(choice.targetKey);
      choice.detail.textContent = choice.enabled && !eligible(choice) ? '原查询选择已过期，请查询最新待办。已准备的事项仍可继续。' : choice.description;
      const visible = matches(choice);
      choice.label.hidden = !visible; choice.detail.hidden = !visible;
    }
    for (const button of resumeButtons) button.disabled = blocked();
    for (const entry of groupActions) {
      const visible = choices.filter(choice => choice.operationId === entry.operationId && matches(choice));
      const available = visible.filter(eligible);
      entry.select.textContent = `选择当前可办 ${available.length} 项`;
      entry.select.disabled = blocked() || !available.length;
      entry.empty.hidden = visible.length > 0;
    }
    if (refresh) {
      refresh.disabled = unavailable || navigated || busy || Boolean(pending) || Boolean(queryPending) && !queryFinished;
      refresh.textContent = queryFinished ? '再次查询最新待办' : '查询最新待办';
    }
    if (queryRecover) queryRecover.disabled = unavailable || busy;
    if (!stopped && !unavailable && !navigated && !pending && !queryPending) {
      const next = Math.min(...choices.filter(eligible).map(choice => choice.expiresAt));
      if (Number.isFinite(next)) expiryTimer = setTimeout(count, Math.max(1, Math.min(next - now(), 2147483647)));
    }
  }
  function render() {
    byId('result-title').textContent = state.title || '选择待办并核对办理';
    root.replaceChildren(); resume.replaceChildren();
    choices = []; resumeButtons = []; groupActions = [];
    if (byId('result-validity')) byId('result-validity').textContent = `此入口可使用至 ${localTime(state.expiresAt)}。查询结果有各自的选择有效期；已准备草稿按其自身有效期办理。`;
    if (typeFilter) {
      const selected = typeFilter.value || '';
      const types = new Map((state.groups || []).flatMap(group => (group.targets || []).map(target =>
        [target.workflowType || target.workflowLabel || 'other', target.workflowLabel || '其他事项'])));
      typeFilter.replaceChildren(element('option', '全部类型'));
      typeFilter.children[0].value = '';
      for (const [key, label] of types) { const option = element('option', label); option.value = key; typeFilter.append(option); }
      typeFilter.value = types.has(selected) ? selected : '';
    }
    for (const group of state.groups || []) {
      const section = element('section'); section.append(element('h2', group.title || 'OA 待办'));
      section.append(element('p', `来源查询时间：${localTime(group.fetchedAt)}；办理选择有效至：${localTime(group.expiresAt)}`));
      const select = element('button'); select.type = 'button'; select.className = 'result-select-current';
      const empty = element('p', '当前筛选没有匹配事项。'); empty.hidden = true;
      groupActions.push({operationId: group.operationId, select, empty});
      select.addEventListener('click', () => {
        if (stopped || blocked()) return;
        const current = choices.filter(choice => choice.operationId === group.operationId && eligible(choice) && matches(choice));
        if (!current.length) return;
        if (operationId !== group.operationId) { operationId = group.operationId; selection.clear(); }
        for (const choice of current) selection.add(choice.targetKey);
        count();
      });
      section.append(select, empty);
      for (const target of group.targets || []) {
        const label = element('label'); label.className = 'result-choice';
        const input = element('input'); input.type = 'checkbox'; input.disabled = !target.enabled;
        const description = target.enabled ? [target.sender, target.date].filter(Boolean).join(' · ') : target.disabledReason || '此项暂不可直接办理，请回原聊天核对。';
        const detail = element('p', description);
        const choice = {input, label, detail, description, expiresAt: sourceExpiry(group.expiresAt), operationId: group.operationId, targetKey: target.targetKey, enabled: target.enabled === true,
          type: target.workflowType || target.workflowLabel || 'other', search: [target.title, target.workflowLabel, target.sender, target.date].filter(Boolean).join(' ').toLocaleLowerCase()};
        choices.push(choice);
        input.checked = operationId === group.operationId && selection.has(target.targetKey);
        input.addEventListener('change', () => {
          if (input.disabled || !eligible(choice) || blocked() || stopped) { count(); return; }
          if (operationId !== group.operationId) { operationId = group.operationId; selection.clear(); }
          if (input.checked) selection.add(target.targetKey); else selection.delete(target.targetKey);
          count();
        });
        label.append(input, element('span', `${target.workflowLabel || '待办'} · ${target.title || '无标题'}`));
        section.append(label, detail);
      }
      root.append(section);
    }
    if (!(state.groups || []).length) root.append(element('p', state.message || '当前没有可新选的待办，请回到原聊天重新查询。'));
    for (const item of state.preparations || []) {
      if (!item.taskId) continue;
      const terminal = item.terminal === true || terminalStates.has(item.state);
      const titles = Array.isArray(item.selectedTitles) ? item.selectedTitles.filter(value => typeof value === 'string') : [];
      const title = titles.length ? titles.join('、') : item.title || '原办理任务';
      const batch = item.batch;
      const progress = batch && Number.isInteger(batch.totalCount) ? ` · 已完成 ${batch.succeededCount || 0}/${batch.totalCount} 项${!terminal && Number.isInteger(batch.currentIndex) ? `，当前第 ${batch.currentIndex} 项` : ''}` : '';
      const button = element('button', `${terminal ? '查看结果' : '继续办理'} · ${title} · ${stateLabel(item.state)}${progress}`); button.type = 'button';
      button.addEventListener('click', () => {
        if (stopped || blocked() || !uuid.test(item.taskId)) return;
        waitingSince = now(); pending = { taskId: item.taskId }; save(); count(); poll();
      });
      resumeButtons.push(button);
      resume.append(button);
    }
    recover.hidden = unavailable || (!recoverRead && (!pending?.body || Boolean(pending.taskId)));
    recover.textContent = recoverRead ? '重新读取原办理状态' : '恢复同一准备请求';
    if (queryRecover) queryRecover.hidden = unavailable || !queryRecoveryNeeded;
    count();
  }
  async function request(action, body, taskId, query = false) {
    const requestEpoch = epoch;
    const controller = new AbortController();
    if (action === 'state') { readController?.abort(); readController = controller; }
    let timeout, progress, finished = false, rejectCanceled, responseStatus;
    const started = now();
    const cancel = () => { controller.abort(); rejectCanceled?.(Object.assign(new Error('页面已离开'), {name: 'AbortError'})); };
    requests.add(cancel);
    const tick = () => {
      if (finished || stopped || epoch !== requestEpoch) return;
      const seconds = Math.floor((now() - (waitingSince ?? started)) / 1000);
      if (seconds >= 5) say(`正在${action === 'prepare' ? '准备所选事项' : action === 'refresh' || query ? '查询最新待办' : '读取原办理状态'}，已等待 ${seconds} 秒。稍慢，请保留本页；无需重复提交。`);
      progress = setTimeout(tick, 1000);
    };
    progress = setTimeout(tick, 1000);
    try {
      const responseValue = (async () => {
        const response = await fetch(`${initial.path}/${action}${taskId ? `?${query ? 'query' : 'task'}=${encodeURIComponent(taskId)}` : ''}`, {
          method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
          headers: { 'X-AgentBridge-Result-Token': initial.csrfToken, ...(body ? { 'Content-Type': 'application/json' } : {}) },
          ...(body ? { body: JSON.stringify(body) } : {}), signal: controller.signal,
        });
        responseStatus = response.status;
        let value;
        try { value = await response.json(); }
        catch { throw Object.assign(new Error('暂时无法读取办理状态，请从原聊天核对。'), {status: response.ok ? undefined : response.status}); }
        if (!response.ok) throw Object.assign(new Error(value.error?.message || '办理状态暂不可用，请从原聊天核对。'),
          {status: response.status, code: value.error?.code});
        return value;
      })();
      const value = await Promise.race([responseValue, new Promise((_, reject) => {
        rejectCanceled = reject;
        timeout = setTimeout(() => {
          controller.abort();
          const denied = [401, 403, 404, 410].includes(responseStatus);
          reject(Object.assign(new Error(denied ? '办理入口或页面权限已失效。' : '等待应答已超时，原任务可能仍在处理。'),
            {code: 'REQUEST_TIMEOUT', ...(denied ? {status: responseStatus} : {})}));
        }, action === 'state' ? 15000 : 30000);
      })]);
      return stopped || epoch !== requestEpoch ? null : value;
    } finally {
      finished = true; clearTimeout(timeout); clearTimeout(progress); requests.delete(cancel);
      if (readController === controller) readController = null;
    }
  }
  function failed(error, {read = false} = {}) {
    if (permanentError(error)) {
      unavailable = true; clearTimeout(timer); clearTimeout(queryTimer); recover.hidden = true;
      if (queryRecover) queryRecover.hidden = true;
      say(`${error.message} 请关闭本页，回原 Telegram 聊天查看已有任务或重新查询。关闭页面不会取消已开始的任务。`);
      count();
    } else if (read) {
      recoverRead = true; recover.hidden = false; recover.disabled = false;
      recover.textContent = '重新读取原办理状态'; say(`${error.message} 可重新读取原状态；不会另建任务。`);
    }
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
  function safeResultUrl(value) {
    try {
      const url = new URL(value, window.location.href);
      if (url.origin !== window.location.origin || url.username || url.password || url.search || url.hash
          || !/^\/results\/[A-Za-z0-9_-]{43}$/.test(url.pathname)) return null;
      url.hash = window.location.hash; return url.href;
    } catch { return null; }
  }
  function queryFailed(error) {
    if (permanentError(error)) { failed(error); return; }
    queryRecoveryNeeded = true;
    if (queryRecover) queryRecover.hidden = false;
    say(`${error.message} 可恢复原查询；不会重复办理任何事项。`); count();
  }
  function queryEnded(value) {
    queryFinished = true; queryRecoveryNeeded = false; selection.clear(); operationId = null;
    if (queryRecover) queryRecover.hidden = true;
    if (value.state === 'succeeded') {
      const targets = (value.groups || []).flatMap(group => group.targets || []);
      // A result without a newly issued ticket is read-only. Its source keys
      // must never be passed to this page's original prepare endpoint.
      state = {...state, groups: [], message: `已查询，发现 ${targets.length} 项；当前没有可直接办理的事项。`};
      render();
      for (const target of targets) root.append(element('p', `${target.workflowLabel || '待办'} · ${target.title || '无标题'}：${target.disabledReason || '请回原聊天核对办理方式。'}`));
      say(`已查询，发现 ${targets.length} 项；当前没有可直接办理的事项。`);
    } else say(`本次查询${stateLabel(value.state)}。${value.message || '请回原聊天查看查询状态。'}`);
    count();
  }
  async function acceptQuery(value, context, resultView = null) {
    if (!context.current()) return;
    if (!activeStates.has(value?.state) && !terminalStates.has(value?.state)) {
      throw new Error('暂时无法识别原查询状态，请回原聊天核对。');
    }
    if (value.taskId && value.taskId !== context.taskId) throw new Error('原查询回执不一致，请回原聊天核对。');
    queryRecoveryNeeded = false;
    if (queryRecover) queryRecover.hidden = true;
    if (value.state === 'succeeded' && value.selectionAvailable === true) {
      let view = resultView;
      if (!view) {
        // Same original read request, now retrieving its fresh opaque ticket.
        const receipt = await request('refresh', queryPending.body);
        if (!receipt || !context.current()) return;
        if (receipt.taskId !== context.taskId) throw new Error('原查询回执不一致，请回原聊天核对。');
        view = receipt.resultView;
      }
      const url = safeResultUrl(view?.presentation?.url);
      if (!url) throw new Error('新的待办入口暂不可用，请恢复原查询或回原聊天核对。');
      queryPending = null; saveQuery(); navigated = true; window.location.replace(url); return;
    }
    if (value.terminal === true || terminalStates.has(value.state)) { queryEnded(value); return; }
    const label = value.state === 'running' ? '正在读取 OA 待办' : value.state === 'waiting_user' ? '查询需要你继续，请查看原聊天的登录或核对提示' : '查询已排队，等待执行';
    const seconds = waitingSince === null ? 0 : Math.floor((now() - waitingSince) / 1000);
    say(`${label}${seconds >= 5 ? `，已等待 ${seconds} 秒` : ''}。此步骤只查询，不会办理事项。`);
    queryTimer = setTimeout(pollQuery, 1000);
  }
  async function pollQuery() {
    if (stopped || unavailable || navigated || !queryPending?.taskId || queryFinished) return;
    clearTimeout(queryTimer);
    const taskId = queryPending.taskId, queryEpoch = epoch, ticket = ++readTicket;
    const current = () => !stopped && epoch === queryEpoch && ticket === readTicket && queryPending?.taskId === taskId;
    try {
      const value = await request('state', null, taskId, true);
      if (value && current()) await acceptQuery(value, {taskId, current});
    } catch (error) { if (current() && error.name !== 'AbortError') queryFailed(error); }
  }
  async function submitQuery({fresh = false} = {}) {
    if (stopped || unavailable || busy || pending || navigated) return;
    if (fresh) {
      if (queryPending && !queryFinished) return;
      queryPending = {body: {requestKey: window.crypto.randomUUID()}};
      queryFinished = false; waitingSince = now(); saveQuery();
    }
    if (!queryPending || queryFinished) return;
    const original = queryPending, queryEpoch = epoch;
    const current = () => !stopped && epoch === queryEpoch && queryPending === original;
    busy = true; queryRecoveryNeeded = false;
    if (queryRecover) queryRecover.hidden = true;
    count(); say('正在请求最新 OA 待办；此步骤只查询，不会办理事项。');
    try {
      const value = await request('refresh', original.body);
      if (!value || !current()) return;
      if (!uuid.test(value.taskId || '')) throw new Error('未取得原查询回执，请恢复同一查询。');
      if (original.taskId && original.taskId !== value.taskId) throw new Error('原查询回执不一致，请回原聊天核对。');
      original.taskId = value.taskId; saveQuery();
      await acceptQuery(value.query, {taskId: value.taskId, current}, value.resultView);
    } catch (error) { if (current() && error.name !== 'AbortError') queryFailed(error); }
    finally { if (epoch === queryEpoch) { busy = false; count(); } }
  }
  async function poll() {
    if (stopped || unavailable || navigated || !pending?.taskId) return;
    const taskId = pending.taskId, pollEpoch = epoch, ticket = ++readTicket;
    const current = () => !stopped && epoch === pollEpoch && ticket === readTicket && pending?.taskId === taskId;
    clearTimeout(timer);
    try {
      const value = await request('state', null, taskId);
      if (!value || !current()) return;
      recoverRead = false; recover.hidden = true;
      if (!activeStates.has(value.state) && !terminalStates.has(value.state)) {
        say('暂时无法识别原任务状态，请回原聊天核对；不要重复提交。');
        return;
      }
      const elapsed = waitingSince === null ? 0 : Math.floor((now() - waitingSince) / 1000);
      say((value.message || value.title || '正在准备材料，请稍候。') + (elapsed >= 5 ? ` 已等待 ${elapsed} 秒。` : ''));
      const url = safeAction(value.interaction);
      const terminal = value.terminal === true || terminalStates.has(value.state);
      if (url && !terminal) { navigated = true; window.location.replace(url); return; }
      if (terminal) {
        recover.hidden = true;
        say(value.message || value.title || '任务已结束，请回原聊天查看核验结果。');
        if (terminalStates.has(value.state) && !['unknown', 'outcome_unknown'].includes(value.state)) {
          const back = element('button', '返回待办选择'); back.type = 'button';
          back.addEventListener('click', () => {
            if (!current()) return;
            pending = null; waitingSince = null; save(); selection.clear(); operationId = null;
            restore(); // A fresh read, never a repeated preparation or decision.
          });
          resume.replaceChildren(back);
        }
        return;
      }
      timer = setTimeout(poll, 1000);
    } catch (error) { if (current() && error.name !== 'AbortError') failed(error, {read: true}); }
  }
  async function submit() {
    if (busy || stopped || unavailable || navigated || queryPending) return;
    if (!pending) {
      count();
      if (!operationId || !selection.size) return;
      waitingSince = now(); pending = { body: { operationId, targetKeys: [...selection], requestKey: window.crypto.randomUUID() } }; save();
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
      if (!stopped && submitEpoch === epoch) {
        if (error.status === 409 && error.code === 'ACTION_SOURCE_EXPIRED' && !pending?.taskId) {
          // This exact rejection happens before a task is queued. Refresh the
          // original ticket read-only; it remains usable for new read queries.
          pending = null; save(); selection.clear(); operationId = null; recoverRead = false;
          say('原查询选择已过期，请查询最新待办；已准备的事项仍可继续。');
          await restore();
        } else if (permanentError(error)) failed(error);
        else { say(`${error.message} 如未取得回执，请恢复同一准备请求，不要另建重复任务。`); recover.hidden = false; }
      }
    } finally { if (submitEpoch === epoch) { busy = false; if (!stopped) { recover.disabled = false; count(); } } }
  }
  async function restore() {
    if (unavailable) return;
    stopped = false; epoch++; busy = !pending?.taskId; recover.disabled = busy;
    if (queryPending && !queryPending.taskId) queryRecoveryNeeded = true;
    navigated = false; readTicket++; clearTimeout(timer); clearTimeout(queryTimer); readController?.abort();
    const restoreEpoch = epoch;
    render();
    if (queryPending?.taskId) { queryFinished = false; busy = false; count(); pollQuery(); return; }
    if (pending?.taskId) { poll(); return; }
    let refreshed = false;
    try { const value = await request('state'); if (value) { state = value; refreshed = true; } }
    catch (error) { if (!stopped && restoreEpoch === epoch && error.name !== 'AbortError') failed(error, {read: true}); }
    finally { if (!stopped && restoreEpoch === epoch) { busy = !refreshed; recover.disabled = unavailable || busy && !recoverRead; render(); } }
  }
  prepare.addEventListener('click', submit); recover.addEventListener('click', () => recoverRead ? restore() : submit());
  refresh?.addEventListener('click', () => submitQuery({fresh: true}));
  queryRecover?.addEventListener('click', () => {
    if (stopped || unavailable || busy || pending) return;
    if (queryPending?.taskId) { queryFinished = false; pollQuery(); } else submitQuery();
  });
  search?.addEventListener('input', count); typeFilter?.addEventListener('change', count); selectedOnly?.addEventListener('change', count);
  window.addEventListener('pagehide', () => { stopped = true; epoch++; clearTimeout(timer); clearTimeout(queryTimer); clearTimeout(expiryTimer); for (const cancel of requests) cancel(); });
  window.addEventListener('pageshow', (event) => { if (event.persisted || stopped) restore(); });
  render(); if (pending?.taskId) poll(); else if (queryPending?.taskId) pollQuery();
  return { restore, safeAction, safeResultUrl };
}

if (typeof document !== 'undefined' && document.getElementById('result-data')) {
  installResultView({ document, window, fetch: window.fetch.bind(window),
    initial: JSON.parse(document.getElementById('result-data').textContent),
    setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window) });
}
