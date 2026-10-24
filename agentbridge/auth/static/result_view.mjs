// The bearer and CSRF proof stay within this trusted same-origin document.
import {renderBatchChecklist} from './batch_view.mjs';
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
  let readTicket = 0, navigated = false, choices = [], resumeButtons = [], remainingButtons = [];
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
  const recoveryStorageKey = `${storageKey}:recovery`;
  const recoveryModels = new Map();
  let recoveryBusy = false;
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
        || Object.keys(pending).some(key => !['taskId', 'body', 'openRequestKey'].includes(key))
        || pending.openRequestKey !== undefined && !/^[A-Za-z0-9_-]{12,128}$/.test(pending.openRequestKey)
        || (pending.taskId !== undefined && !uuid.test(pending.taskId))
        || (!pending.taskId && !body) || !bodyValid) pending = null;
  }
  try { queryPending = JSON.parse(window.sessionStorage.getItem(queryStorageKey) || 'null'); } catch {}
  if (queryPending && (typeof queryPending !== 'object' || Array.isArray(queryPending)
      || Object.keys(queryPending).some(key => !['taskId', 'body','action'].includes(key))
      || queryPending.action !== undefined && queryPending.action !== 'remaining-query'
      || !queryPending.body || (queryPending.action === 'remaining-query'
        ? Object.keys(queryPending.body).sort().join(',') !== 'expectedBatchVersion,requestKey,taskId' || !uuid.test(queryPending.body.taskId || '') || !Number.isSafeInteger(queryPending.body.expectedBatchVersion) || queryPending.body.expectedBatchVersion<1
        : Object.keys(queryPending.body).join(',') !== 'requestKey')
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
  const blocked = () => unavailable || navigated || busy || recoveryBusy || Boolean(pending) || Boolean(queryPending);
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
    for (const button of remainingButtons) button.disabled = blocked();
    for (const entry of groupActions) {
      const visible = choices.filter(choice => choice.operationId === entry.operationId && matches(choice));
      const available = visible.filter(eligible);
      entry.select.textContent = `选择当前可办 ${available.length} 项`;
      entry.select.disabled = blocked() || !available.length;
      entry.empty.hidden = visible.length > 0;
    }
    if (refresh) {
      refresh.disabled = unavailable || navigated || busy || recoveryBusy || Boolean(pending) || Boolean(queryPending) && !queryFinished;
      refresh.textContent = queryFinished ? '再次查询最新待办' : '查询最新待办';
    }
    if (queryRecover) queryRecover.disabled = unavailable || busy;
    if (!stopped && !unavailable && !navigated && !pending && !queryPending) {
      const next = Math.min(...choices.filter(eligible).map(choice => choice.expiresAt));
      if (Number.isFinite(next)) expiryTimer = setTimeout(count, Math.max(1, Math.min(next - now(), 2147483647)));
    }
    for (const model of recoveryModels.values()) updateRecovery(model);
  }
  function render() {
    for (const model of recoveryModels.values()) { clearTimeout(model.timer); model.view = null; }
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
    remainingButtons=[];
    for (const group of state.groups || []) {
      const section = element('section'); section.append(element('h2', group.title || 'OA 待办'));
      if(group.remaining) {
        const remaining=group.remaining;
        section.append(element('p',`本轮仅核对原批次剩余 ${remaining.requestedCount || 0} 项：匹配 ${remaining.matchedCount || 0} 项，当前待办未找到 ${remaining.missingCount || 0} 项，资格变化 ${remaining.changedCount || 0} 项。未找到不代表已成功；只有仍匹配的事项可重新选择。`));
        for(const item of (remaining.items || []).slice(0,100)) section.append(element('p',`原第 ${item.ordinal} 项 · ${item.title || '事项'} · ${{matched:'可重新核对',missing:'未在当前待办找到，不代表办理成功',changed:'当前资格已变化，暂不可直接办理'}[item.state] || '待核对'}`));
      }
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
      const summaryPrefix = `${terminal ? '查看结果' : '继续办理'} · ${title} · `;
      const button = element('button', `${summaryPrefix}${recoveryResultLabel(item.oaPendingRecovery, stateLabel(item.state))}${progress}`); button.type = 'button';
      button.addEventListener('click', () => {
        if (stopped || blocked() || !uuid.test(item.taskId)) return;
        waitingSince = now(); pending = { taskId: item.taskId }; save(); count(); poll();
      });
      resumeButtons.push(button);
      resume.append(button);
      if(batch?.items?.length) {const checklist=element('section');resume.append(checklist);renderBatchChecklist(document,checklist,batch);}
      const remaining=item.oaBatchRemaining;
      if(remaining?.available===true && Number.isSafeInteger(remaining.batchVersion) && remaining.batchVersion>0 && Number.isSafeInteger(remaining.remainingCount) && remaining.remainingCount>0) {
        const explanation=element('p','仅重新查询这些未开始的原事项。已成功项不重提；结果待核对项需单独核对，后续仍须逐项确认。');
        const query=element('button',`重新查询并核对剩余 ${remaining.remainingCount} 项`);query.type='button';remainingButtons.push(query);
        query.addEventListener('click',()=>{
          if(stopped || blocked() || !uuid.test(item.taskId)) return;
          queryPending={action:'remaining-query',body:{taskId:item.taskId,requestKey:window.crypto.randomUUID(),expectedBatchVersion:remaining.batchVersion}};
          queryFinished=false;waitingSince=now();saveQuery();void submitQuery();
        });resume.append(explanation,query);
      }
      renderRecovery(item, resume, {button,prefix:summaryPrefix,suffix:progress,fallback:stateLabel(item.state)});
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
      if (seconds >= 5) say(`正在${action === 'prepare' ? '准备所选事项' : action === 'recovery-probe' ? '只读核对原事项' : action === 'recovery-prepare' ? '准备新的办理表单' : action === 'remaining-query' ? '查询原批次剩余事项' : action === 'refresh' || query ? '查询最新待办' : '读取原办理状态'}，已等待 ${seconds} 秒。稍慢，请保留本页；无需重复提交。`);
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
      if (url.origin !== window.location.origin || url.username || url.password || (url.search && url.search !== `?result=${initial.path.split('/').at(-1)}`) || url.hash
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
  const recoveryStates = new Set(['not_checked', 'verified_complete', 'unresolved_still_pending', 'blocked',
    'recovery_in_progress', 'recovery_succeeded', 'recovery_failed', 'recovery_unknown']);
  const recoveryReasons = {
    login_required:'请先在原聊天恢复 OA 登录，再只读核对原事项。', identity_changed:'当前 OA 身份与原办理不一致，请先核对登录身份。',
    permission_required:'当前身份暂不能核对原事项，请先恢复所需权限。',
    read_budget_exceeded:'本轮核对已到时间上限，尚不能确认结果；可以再次只读核对，不会重新提交。',
    done_scan_unproven:'已办列表没有完整覆盖证据，尚不能确认结果；可以再次只读核对原事项。',
    material_changed:'原事项材料已变化，请先到 OA 核对。', target_changed:'原事项或审批节点已变化，请先到 OA 核对。',
    target_identity_changed:'原事项或审批节点已变化，请先到 OA 核对。',
    conflicting_or_completed_evidence:'已有完成或待办、已办相互冲突的证据，请先到 OA 核对原事项。',
    pending_coverage_incomplete:'待办列表尚未完整读取，暂不能准备新的办理。',
    recovery_chain_unsupported:'后续办理结果也待核对，请到 OA 核对，不能连续创建新的恢复任务。',
    recovery_evidence_invalid:'后续任务的业务结果证据尚不足，请先核对原事项。',
  };
  function persistRecovery() {
    try { window.sessionStorage.setItem(recoveryStorageKey, JSON.stringify([...recoveryModels.values()]
      .filter(model => model.command).slice(-16).map(model => ({taskId:model.taskId, command:model.command})))); } catch {}
  }
  function safeRecoveryCommand(value, taskId) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || !['recovery-probe','recovery-prepare','recovery-open','open'].includes(value.action)) return false;
    const body = value.body;
    if (!body || body.taskId !== taskId || !/^[A-Za-z0-9_-]{12,128}$/.test(body.requestKey || '')) return false;
    const keys = Object.keys(body).sort().join(',');
    return value.action !== 'recovery-prepare' ? keys === 'requestKey,taskId' :
      keys === 'acknowledgement,assessmentHash,assessmentId,expectedRevision,requestKey,taskId'
      && uuid.test(body.assessmentId || '') && /^sha256:[0-9a-f]{64}$/.test(body.assessmentHash || '')
      && Number.isSafeInteger(body.expectedRevision) && body.expectedRevision > 0
      && body.acknowledgement === 'oa_unknown_new_attempt_v1';
  }
  try {
    const saved = JSON.parse(window.sessionStorage.getItem(recoveryStorageKey) || '[]');
    if (Array.isArray(saved) && saved.length <= 16) for (const entry of saved) {
      if (uuid.test(entry?.taskId || '') && safeRecoveryCommand(entry.command, entry.taskId)) {
        recoveryModels.set(entry.taskId, {taskId:entry.taskId, command:entry.command, assessment:null, acknowledged:false});
      }
    }
  } catch {}
  const freshAssessment = value => value && value.status === 'unresolved_still_pending' && value.canExecute === false
    && value.canPrepare === true && uuid.test(value.assessmentId || '') && /^sha256:[0-9a-f]{64}$/.test(value.assessmentHash || '')
    && Number.isSafeInteger(value.revision) && value.revision > 0 && Date.parse(value.expiresAt) > now();
  function recoveryResultLabel(projection, fallback) {
    if (!projection || !uuid.test(projection.originalTaskId || '') || projection.canExecute !== false || projection.canPrepare !== false
        || !['verified_complete','recovery_succeeded'].includes(projection.status)) return fallback;
    const expiry = Date.parse(projection.expiresAt);
    const stale = projection.stale === true || Number.isFinite(expiry) && expiry <= now()
      || projection.status === 'verified_complete' && !Number.isFinite(expiry);
    return stale ? '上次核对已过期' : projection.status === 'recovery_succeeded' ? '后续办理已完成' : '原事项已核验完成';
  }
  function renderRecovery(value, target, summary = null) {
    const projection = value?.oaPendingRecovery;
    if (!projection || !uuid.test(projection.originalTaskId || '') || !recoveryStates.has(projection.status)
        || projection.canExecute !== false || projection.canPrepare !== false) return;
    const taskId = projection.originalTaskId;
    let model = recoveryModels.get(taskId);
    if (!model) { model = {taskId, assessment:null, acknowledged:false, command:null}; recoveryModels.set(taskId, model); }
    model.projection = projection;
    clearTimeout(model.timer);
    const panel = element('section'); panel.className = 'result-recovery';
    panel.append(element('h3', '核对原事项与后续办理'));
    const current = element('p'), history = element('p', '原尝试的结果仍保留为待核对；只读核对和准备新表单都不会重复提交。');
    history.className = 'muted';
    const notice = element('p'); notice.setAttribute?.('role','status');
    const probe = element('button'), risk = element('label'), ack = element('input'), prepareNew = element('button'), open = element('button');
    probe.type = prepareNew.type = open.type = 'button'; ack.type = 'checkbox'; risk.className = 'result-risk';
    risk.append(ack, element('span', '我已核对原事项，理解原提交结果仍未知、可能仍在处理，重新办理存在重复处理风险。我仅要求准备新表单，并会重新核对材料、填写意见后再明确确认。'));
    panel.append(current, history, notice, probe, risk, prepareNew, open); target.append(panel);
    model.view = {panel,current,history,notice,probe,risk,ack,prepareNew,open,summary};
    probe.addEventListener('click', () => runRecovery(model, 'recovery-probe'));
    ack.addEventListener('change', () => { if (!ack.disabled && freshAssessment(model.assessment)) model.acknowledged = ack.checked === true; updateRecovery(model); });
    prepareNew.addEventListener('click', () => runRecovery(model, 'recovery-prepare'));
    open.addEventListener('click', () => ['recovery_succeeded','recovery_failed','recovery_unknown'].includes(model.projection?.status)
      ? refreshRecovery(model) : runRecovery(model, 'recovery-open'));
    updateRecovery(model);
  }
  function updateRecovery(model) {
    if (!model.view) return;
    clearTimeout(model.timer);
    const p = model.projection, v = model.view;
    if (!p) return;
    if (v.summary) v.summary.button.textContent = `${v.summary.prefix}${recoveryResultLabel(p, v.summary.fallback)}${v.summary.suffix}`;
    const expires = Date.parse(model.assessment?.expiresAt || p.expiresAt);
    const stale = p.stale === true || Number.isFinite(expires) && expires <= now();
    const completed = ['verified_complete','recovery_succeeded'].includes(p.status) && !stale;
    const ready = freshAssessment(model.assessment);
    if (!ready && !model.command) model.acknowledged = false;
    const canProbe = !completed && (p.canProbe === true || stale && p.availability === 'can_probe');
    const unavailableHere = stopped || unavailable || navigated || recoveryBusy || busy || Boolean(queryPending)
      || Boolean(pending && pending.taskId !== model.taskId);
    const labels = {
      not_checked:'尚未核对原事项的当前结果。', verified_complete:stale ? '上次核对曾确认原事项已完成；该证据现已过期或当前身份不可核对。' : '已根据本次 OA 记录核验原事项完成，无需重新办理。',
      unresolved_still_pending:'原事项仍在 OA 待办中，但这不能排除原提交仍在处理。',
      blocked:'当前证据不足，暂不能准备新的办理。', recovery_in_progress:'后续办理正在进行，请查看已有任务，避免重复准备。',
      recovery_succeeded:stale ? '此前后续办理已核验完成；当前身份或会话需重新核对。' : '后续办理已核验完成。',
      recovery_failed:'后续办理尚未完成，请查看已有任务的处理状态。', recovery_unknown:'后续办理结果仍待核对，请到 OA 核对，勿再次提交。',
    };
    v.current.textContent = labels[p.status] + (p.observedAt ? ` 核验时间：${localTime(p.observedAt)}。` :
      p.recoveryUpdatedAt ? ` 后续任务更新时间：${localTime(p.recoveryUpdatedAt)}（不是核验时间）。` : '');
    v.notice.textContent = model.notice || recoveryReasons[p.reason] || '';
    if (!model.command && model.assessment && !ready && model.assessment.canPrepare === true) v.notice.textContent = '本次核对已到期，请重新只读核对原事项。';
    if (ready && !model.command) v.notice.textContent += ` 本次核对有效至 ${localTime(model.assessment.expiresAt)}。${model.assessment.doneCoverage !== 'complete' ? '已办记录仅部分覆盖，未找到记录不能证明上次未提交。' : ''}`;
    v.probe.textContent = model.command?.action === 'recovery-probe' ? '恢复本次只读核对' : '只读核对原事项';
    v.probe.hidden = !canProbe && model.command?.action !== 'recovery-probe';
    v.probe.disabled = unavailableHere || Boolean(model.command && model.command.action !== 'recovery-probe');
    v.risk.hidden = !ready || Boolean(model.command); v.ack.checked = model.acknowledged;
    v.ack.disabled = unavailableHere || !ready || Boolean(model.command);
    v.prepareNew.hidden = !ready && model.command?.action !== 'recovery-prepare';
    v.prepareNew.textContent = model.command?.action === 'recovery-prepare' ? '恢复本次新表单准备' : model.assessment?.childPreparation === 'retryable' ? '恢复同一任务材料准备' : '准备全新办理表单';
    v.prepareNew.disabled = unavailableHere || Boolean(model.command && model.command.action !== 'recovery-prepare')
      || (!model.command && (!ready || !model.acknowledged));
    v.open.hidden = !uuid.test(p.recoveryTaskId || '') && model.command?.action !== 'recovery-open';
    v.open.textContent = ['recovery_succeeded','recovery_failed','recovery_unknown'].includes(p.status) ? '刷新后续办理结果' : '查看后续办理表单';
    v.open.disabled = unavailableHere || Boolean(model.command && model.command.action !== 'recovery-open');
    if (!stopped && Number.isFinite(expires) && expires > now()) model.timer = setTimeout(() => updateRecovery(model), Math.min(2147483647, expires - now() + 1));
  }
  async function refreshRecovery(model) {
    if (stopped || unavailable || navigated || recoveryBusy || busy || queryPending || pending && pending.taskId !== model.taskId) return;
    const requestEpoch = epoch;
    recoveryBusy = true; model.assessment = null; model.acknowledged = false;
    count(); for (const item of recoveryModels.values()) updateRecovery(item);
    try {
      const result = await request('state', null, model.taskId);
      if (!result || stopped || requestEpoch !== epoch) return;
      if (result.oaPendingRecovery?.originalTaskId !== model.taskId || !recoveryStates.has(result.oaPendingRecovery.status)
          || result.oaPendingRecovery.canExecute !== false || result.oaPendingRecovery.canPrepare !== false) throw Error('暂未取得原事项的最新结果。');
      model.projection = result.oaPendingRecovery; model.notice = '';
      if (model.command?.action === 'recovery-open') { model.command = null; persistRecovery(); }
    } catch (error) { if (!stopped && requestEpoch === epoch && error.name !== 'AbortError') model.notice = error.message; }
    finally { if (requestEpoch === epoch) { recoveryBusy = false; count(); for (const item of recoveryModels.values()) updateRecovery(item); } }
  }
  async function runRecovery(model, action) {
    if (stopped || unavailable || navigated || recoveryBusy || busy || queryPending || pending && pending.taskId !== model.taskId) return;
    if (model.command && model.command.action !== action) return;
    updateRecovery(model);
    const control = action === 'recovery-probe' ? model.view?.probe : action === 'recovery-prepare' ? model.view?.prepareNew : model.view?.open;
    if (!control || control.disabled || control.hidden) return;
    if (!model.command) {
      const body = {taskId:model.taskId,requestKey:window.crypto.randomUUID()};
      if (action === 'recovery-prepare') {
        if (!model.acknowledged || !freshAssessment(model.assessment)) return;
        Object.assign(body, {assessmentId:model.assessment.assessmentId, assessmentHash:model.assessment.assessmentHash,
          expectedRevision:model.assessment.revision, acknowledgement:'oa_unknown_new_attempt_v1'});
      }
      model.command = {action,body}; persistRecovery();
    }
    const operationEpoch = epoch, command = model.command;
    recoveryBusy = true; model.notice = action === 'recovery-probe' ? '正在只读核对原事项，不会重新提交。' :
      action === 'recovery-prepare' ? '正在准备新的办理表单；仍需重新核对、填写和明确确认。' : '正在读取本端的已有办理入口。';
    count(); for (const item of recoveryModels.values()) updateRecovery(item);
    try {
      const result = await request(action, command.body);
      if (!result || stopped || operationEpoch !== epoch) return;
      if (result.taskId !== model.taskId || result.oaPendingRecovery?.originalTaskId !== model.taskId
          || !recoveryStates.has(result.oaPendingRecovery.status) || result.oaPendingRecovery.canExecute !== false
          || result.oaPendingRecovery.canPrepare !== false) throw Error('原事项回执尚未确认，请恢复同一请求。');
      if (action === 'recovery-probe') {
        const assessment = result.assessment;
        if (!assessment || !['verified_complete','unresolved_still_pending','blocked'].includes(assessment.status) || assessment.canExecute !== false) throw Error('核对回执尚未确认，请恢复同一核对。');
        model.assessment = assessment; model.acknowledged = false;
      } else if (action === 'recovery-prepare') { model.assessment = null; model.acknowledged = false; }
      model.projection = result.oaPendingRecovery; model.command = null; model.notice = ''; persistRecovery();
      if (action === 'recovery-open') {
        const url = safeAction(result.interaction);
        if (url && !['recovery_succeeded','recovery_unknown'].includes(model.projection.status)) {
          navigated = true; window.location.replace(url);
        } else model.notice = ['recovery_succeeded','recovery_unknown'].includes(model.projection.status) ?
          '已显示该任务的核验结果；历史尝试与后续任务分别保留。' : '当前表单尚未就绪，请稍后查看已有后续任务，不会重复准备。';
      }
    } catch (error) {
      if (!stopped && operationEpoch === epoch && error.name !== 'AbortError') {
        model.notice = error.message || '回执尚未确认，请恢复同一请求。';
        if (permanentError(error)) { model.command = null; model.assessment = null; model.acknowledged = false; persistRecovery(); }
      }
    } finally {
      if (operationEpoch === epoch) { recoveryBusy = false; count(); for (const item of recoveryModels.values()) updateRecovery(item); }
    }
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
    if (value.state === 'succeeded' && (value.selectionAvailable === true || value.entryAvailable === true)) {
      let view = resultView;
      if (!view) {
        // Same original read request, now retrieving its fresh opaque ticket.
        const receipt = await request(queryPending.action || 'refresh', queryPending.body);
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
    count(); say(original.action === 'remaining-query' ? '正在重新查询原批次尚未开始的事项；此步骤不会准备或提交业务。' : '正在请求最新 OA 待办；此步骤只查询，不会办理事项。');
    try {
      const value = await request(original.action || 'refresh', original.body);
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
      if ((url || value.canOpen === true) && !terminal) {
        // A user-selected task may open its existing local presentation through
        // this explicit, idempotent endpoint. It also binds the return source.
        pending.openRequestKey ||= window.crypto.randomUUID(); save();
        const opened = await request('open', {taskId,requestKey:pending.openRequestKey});
        if (!current() || !opened) return;
        const destination = opened.taskId === taskId ? safeAction(opened.interaction) : null;
        if (!destination) throw Error('本端办理入口尚未就绪，请重新读取原办理状态。');
        // The acknowledged open is complete. A later source Back must show the
        // list instead of replaying this task selection and reopening the form.
        pending = null; save();
        navigated = true; window.location.replace(destination); return;
      }
      if (terminal) {
        recover.hidden = true;
        say(value.message || value.title || '任务已结束，请回原聊天查看核验结果。');
        if (terminalStates.has(value.state)) {
          const back = element('button', ['unknown', 'outcome_unknown'].includes(value.state)
            ? '返回待办列表（此项仍待核对）' : '返回待办选择'); back.type = 'button';
          back.addEventListener('click', () => {
            if (!current()) return;
            pending = null; waitingSince = null; save(); selection.clear(); operationId = null;
            restore(); // A fresh read, never a repeated preparation or decision.
          });
          for (const model of recoveryModels.values()) { clearTimeout(model.timer); model.view = null; }
          resume.replaceChildren(back);
          renderRecovery(value, resume);
        }
        return;
      }
      timer = setTimeout(poll, 1000);
    } catch (error) {
      if (current() && error.name !== 'AbortError') {
        if (['ACTION_SOURCE_UNAVAILABLE','ACTION_REQUEST_INVALID'].includes(error.code)) {
          delete pending.openRequestKey; save(); recoverRead = true; recover.hidden = false; recover.disabled = false;
          recover.textContent = '重新读取原办理状态'; say(`${error.message} 可以重新读取原任务，系统不会另建办理或重复提交。`);
        } else failed(error, {read: true});
      }
    }
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
    stopped = false; epoch++; busy = !pending?.taskId; recoveryBusy = false; recover.disabled = busy;
    for (const model of recoveryModels.values()) { model.assessment = null; model.acknowledged = false; clearTimeout(model.timer); }
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
  window.addEventListener('pagehide', () => { stopped = true; epoch++; clearTimeout(timer); clearTimeout(queryTimer); clearTimeout(expiryTimer);
    for (const model of recoveryModels.values()) { clearTimeout(model.timer); model.assessment = null; model.acknowledged = false; }
    for (const cancel of requests) cancel(); });
  window.addEventListener('pageshow', (event) => { if (event.persisted || stopped) restore(); });
  document.addEventListener?.('agentbridge:native-resume', () => { if (!stopped) restore(); });
  render(); if (pending?.taskId) poll(); else if (queryPending?.taskId) pollQuery();
  return { restore, safeAction, safeResultUrl };
}

if (typeof document !== 'undefined' && document.getElementById('result-data')) {
  installResultView({ document, window, fetch: window.fetch.bind(window),
    initial: JSON.parse(document.getElementById('result-data').textContent),
    setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window) });
}
