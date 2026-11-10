import { isCancellation } from './workspace_lifecycle.mjs';
import { reconcileChildren } from './workspace_dom.mjs';

const STATES = {active:'关注中', paused:'已暂停', stopped:'已停止', completed:'关注已结束', expired:'已到期'};
const FLOW_STATES = {in_progress:'流转中', running:'流转中', active:'流转中', pending:'处理中', ended:'流程已结束',
  completed:'流程已结束', finished:'流程已结束', revoked:'已撤销', canceled:'已取消', terminated:'已终止'};
const text = value => typeof value === 'string' ? value : '';
const names = value => Array.isArray(value) ? value.filter(item => typeof item === 'string' && item.trim()) : [];
const validWatch = value => value && typeof value.watch_id === 'string' && value.watch_id.length > 0
  && value.watch_id.length <= 200 && Number.isSafeInteger(value.version) && value.version >= 0;
const timeFormat = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',
  hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
const timestamp = value => {
  if (!value) return '尚无记录';
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? timeFormat.format(new Date(parsed)) : '时间未核实';
};
const incompleteCheck = watch => ['unknown','incomplete','unavailable','failed'].includes(watch?.last_check_status);
const INTERVALS = [[900,'每 15 分钟'], [3600,'每小时'], [14400,'每 4 小时'], [86400,'每天']];
const RULES = [['nodes','节点变化'], ['handlers','处理人或分工变化'], ['ended','流程结束']];
const object = value => value && typeof value === 'object' && !Array.isArray(value);
function changeSummary(event) {
  const kind = text(event.event_type || event.type).split('.').at(-1), diff = event.diff;
  if (!['changed','completed'].includes(kind) || !object(diff)) return '';
  const summaries = [];
  for (const [rule, label] of RULES) {
    const value = diff[rule];
    if (!object(value) || !Object.hasOwn(value,'before') || !Object.hasOwn(value,'after')
        || JSON.stringify(value.before) === JSON.stringify(value.after)) continue;
    if (rule === 'ended' && typeof value.after === 'boolean') summaries.push(value.after ? '流程已结束' : '流程恢复流转');
    if (rule === 'nodes' && Array.isArray(value.before) && Array.isArray(value.after))
      summaries.push(`当前节点有变化：${names(value.before).join('、') || '无'} → ${names(value.after).join('、') || '无'}`);
    if (rule === 'handlers' && object(value.before) && object(value.after)) {
      const assigned = value => Array.isArray(value.nodes) ? value.nodes.map(item => text(item?.name)
        + (names(item?.handlers).length ? `（${names(item.handlers).join('、')}）` : '')).filter(Boolean).join('；')
        : names(value.current_handlers).join('、');
      summaries.push(`${label}：${assigned(value.before) || '无'} → ${assigned(value.after) || '无'}`);
    }
  }
  return summaries.join('；');
}

function errorMessage(error, mutation = false) {
  if (error.code === 'WATCH_SESSION_UNAVAILABLE') return 'OA 登录暂不可用，本次未取得新状态。请确认登录后刷新关注记录。';
  if (error.code === 'WATCH_READ_CONSENT_REQUIRED') return '本项尚未允许检查时标为已读，当前未执行核验。';
  if (error.status === 401) return '工作台登录已失效，请重新登录。';
  if (error.status === 403) return '当前没有查看或管理这项关注的权限。';
  if (error.status === 404) return '这项关注已不存在或当前不可访问。';
  if (/(?:^|_)VERSION_CONFLICT$/.test(text(error.code))) return '关注记录已更新，请刷新详情后再操作。';
  if (error.status === 409) return (text(error.message).trim() || '本次未能完成操作，已保留上次记录。')
    + ' 请刷新详情核对，系统不会自动重复操作。';
  if (mutation) return '暂未确认本次操作的结果，请刷新详情核对；系统不会自动重复操作。';
  return '读取失败，已保留本账号上次读取的记录。请重试。';
}

// The controller owns only authenticated projections. Browser storage and old
// query URLs never supply a watch target, authorization or mutation version.
export function createBusinessWatches({document, $, getScope, request, makeKey = () => crypto.randomUUID()}) {
  let active = false, panelScope = null, generation = 0, listTicket = 0, detailTicket = 0;
  let items = new Map(), cursor = null, selectedId = null, detail = null, events = [];
  let listPending = false, detailPending = false, actionPending = false, verified = false;
  let detailVisible = false;
  let lastAppend = false, detailNotice = '', listNotice = '';
  let editor = null, editorPending = false, editorTicket = 0;
  const rowViews = new Map();
  const node = (tag, className = '', value = '') => {
    const element = document.createElement(tag); element.className = className; element.textContent = value; return element;
  };
  const placeholder = node('p', 'empty-state', '选择一项关注查看当前记录');
  const content = node('section', 'business-watch-content');
  const back = node('button', 'secondary mobile-detail-back', '返回关注列表'); back.type = 'button';
  const title = node('h3', 'business-watch-title');
  const state = node('p', 'business-watch-state');
  const notice = node('p', 'business-watch-notice'); notice.setAttribute('role', 'status'); notice.setAttribute('aria-live', 'polite');
  const fields = node('dl', 'business-watch-fields');
  const values = new Map();
  for (const [key, label] of [['workflow','流程状态'], ['nodes','当前节点'], ['handlers','当前处理人'],
    ['observed','状态读取时间'], ['checked','最近检查'], ['next','下次检查'], ['expires','关注到期时间']]) {
    const name = node('dt', '', label), value = node('dd'); values.set(key, value); fields.append(name, value);
  }
  const explanation = node('p', 'business-watch-explanation', '时间按北京时间显示，当前状态以读取时间为准；没有节点进入时间时不估算停留时长。流程结束不代表款项已支付。');
  const controls = node('div', 'business-watch-actions');
  const buttons = {};
  for (const [key, label] of [['check','核验最新状态'], ['pause','暂停关注'], ['resume','恢复关注'], ['stop','停止关注']]) {
    const button = node('button', key === 'check' ? 'primary' : 'secondary', label); button.type = 'button';
    button.dataset.watchAction = key; button.addEventListener('click', () => act(key));
    controls.append(button); buttons[key] = button;
  }
  const refreshDetail = node('button', 'secondary', '刷新详情'); refreshDetail.type = 'button';
  refreshDetail.addEventListener('click', () => selectedId && select(selectedId, {preserve:true}));
  controls.append(refreshDetail);
  const settings = node('button', 'secondary', '关注设置'); settings.type = 'button';
  settings.addEventListener('click', () => openSettings()); controls.append(settings);
  const boundary = node('p', 'business-watch-explanation', '关注及核验只读取流程状态，不办理、催办或撤销 OA 事项。停止关注不改变 OA 流程。');
  const eventTitle = node('h4', '', '最近变化（最多 100 条）'), eventList = node('ul', 'business-watch-events');
  content.append(back, title, state, notice, fields, explanation, controls, boundary, eventTitle, eventList);
  const editorRoot = node('section', 'business-watch-editor'), editorTitle = node('h3', 'business-watch-title');
  const editorTarget = node('p', 'business-watch-title'), editorNotice = node('p', 'business-watch-notice');
  editorNotice.setAttribute('role','status'); editorNotice.setAttribute('aria-live','polite');
  const form = node('form', 'business-watch-form');
  const interval = node('select'); interval.name = 'interval_seconds';
  for (const [value, label] of INTERVALS) { const option = node('option','',label); option.value = String(value); interval.append(option); }
  const days = node('input'); days.type = 'number'; days.name = 'expires_in_days'; days.min = '1'; days.max = '30'; days.step = '1';
  const label = (name, input) => { const element = node('label','business-watch-form-label',name); element.append(input); return element; };
  const daysLabel = label('关注期限（天）',days), expiryNote = node('p','business-watch-explanation');
  const rulesGroup = node('fieldset'), ruleLegend = node('legend','','关注变化'), ruleInputs = new Map(); rulesGroup.append(ruleLegend);
  for (const [value, title] of RULES) {
    const input = node('input'); input.type = 'checkbox'; input.name = 'event_types'; input.value = value; input.checked = false;
    const element = node('label','business-watch-check'); element.append(input,node('span','',title)); rulesGroup.append(element); ruleInputs.set(value,input);
  }
  const endpointsGroup = node('fieldset'), endpointLegend = node('legend','','提醒位置'), endpointsList = node('div','business-watch-endpoints');
  const endpointNote = node('p','business-watch-explanation','不选择外部端点时，仅在 Workspace 查看变化。');
  endpointsGroup.append(endpointLegend,endpointsList,endpointNote); const endpointInputs = new Map();
  const consent = node('input'); consent.type = 'checkbox'; consent.name = 'allow_mark_read'; consent.checked = false;
  const consentLabel = node('label','business-watch-check'); consentLabel.append(consent,node('span','','允许检查时标为已读'));
  const readNotice = node('p','business-watch-explanation','检查可能将事项标为已读。');
  const save = node('button','primary','开始关注'); save.type = 'submit';
  const cancel = node('button','secondary','取消'); cancel.type = 'button'; cancel.addEventListener('click', () => closeEditor());
  const verifyEdit = node('button','secondary','刷新核对'); verifyEdit.type = 'button'; verifyEdit.addEventListener('click', () => refreshEditor());
  const editorActions = node('div','business-watch-actions'); editorActions.append(save,verifyEdit,cancel);
  form.append(label('检查频率',interval),daysLabel,expiryNote,rulesGroup,endpointsGroup,readNotice,consentLabel,editorActions);
  form.addEventListener('submit', event => { event.preventDefault(); return submitEditor(); });
  editorRoot.append(editorTitle,editorTarget,editorNotice,form); editorRoot.hidden = true;
  $('#business-watch-detail').replaceChildren(placeholder, content, editorRoot);
  back.addEventListener('click', () => { detailVisible = false; $('#business-watch-detail').className = 'business-watch-detail mobile-empty'; });

  const current = (scope, epoch) => active && epoch === generation && scope.current();
  const showListNotice = message => { listNotice = message; $('#business-watch-list-status').textContent = message; };
  const merge = watch => {
    if (!validWatch(watch)) throw new Error('Invalid watch projection');
    const previous = items.get(watch.watch_id);
    const value = previous && previous.version > watch.version ? previous : watch;
    items.set(watch.watch_id, value);
    return value;
  };
  function updateControls() {
    const busy = listPending || actionPending || editorPending;
    $('#business-watch-more').hidden = !cursor;
    $('#business-watch-more').disabled = busy;
    $('#business-watch-retry').disabled = busy;
    $('#business-watches-panel').setAttribute('aria-busy', String(listPending));
    if (active) $('#refresh-tasks').disabled = busy || detailPending;
    refreshDetail.disabled = actionPending || detailPending;
    for (const [action, button] of Object.entries(buttons)) {
      const allowed = action === 'check' || action === 'pause' ? detail?.state === 'active'
        : action === 'resume' ? detail?.state === 'paused' : ['active', 'paused'].includes(detail?.state);
      button.hidden = !allowed;
      button.disabled = !allowed || !verified || actionPending || detailPending
        || detail?.background_paused === true && ['check','resume'].includes(action);
      button.textContent = actionPending && button.dataset.running === 'true' ? '正在处理…'
        : ({check:'核验最新状态',pause:'暂停关注',resume:'恢复关注',stop:'停止关注'})[action];
    }
    settings.hidden = !['active','paused'].includes(detail?.state);
    settings.disabled = !verified || actionPending || detailPending || !!editor || detail?.background_paused === true;
    for (const row of rowViews.values()) row.root.disabled = actionPending || !!editor;
    editorRoot.hidden = !editor;
    if (editor) {
      content.hidden = placeholder.hidden = true;
      editorTitle.textContent = editor.mode === 'create' ? '关注后续' : '关注设置';
      editorTarget.textContent = editor.title;
      editorNotice.textContent = editor.notice; editorNotice.hidden = !editor.notice;
      save.textContent = editorPending ? '正在处理…' : editor.mode === 'create' ? '开始关注' : '保存设置';
      save.disabled = editorPending || !editor.loaded || !editor.enabled || editor.needsVerify || editor.sourceInvalid;
      verifyEdit.hidden = !editor.needsVerify; verifyEdit.disabled = editorPending;
      cancel.disabled = editorPending;
      for (const input of [interval,days,consent,...ruleInputs.values(),...endpointInputs.values()])
        input.disabled = editorPending || !editor.loaded || !editor.enabled;
    }
  }
  function renderList() {
    const children = [];
    for (const watch of items.values()) {
      let row = rowViews.get(watch.watch_id);
      if (!row) {
        const root = node('button', 'business-watch-row'); root.type = 'button'; root.dataset.watchId = watch.watch_id;
        const heading = node('strong', 'business-watch-title'), metadata = node('span', 'business-watch-meta');
        root.append(heading, metadata); root.addEventListener('click', () => select(watch.watch_id));
        row = {root, heading, metadata}; rowViews.set(watch.watch_id, row);
      }
      row.root.className = 'business-watch-row' + (watch.watch_id === selectedId ? ' active' : '');
      row.root.setAttribute('aria-current', watch.watch_id === selectedId ? 'true' : 'false');
      row.heading.textContent = text(watch.title) || '标题未读到';
      row.metadata.textContent = `${STATES[watch.state] || '状态待核实'} · 最近检查：${timestamp(watch.last_checked_at)}`;
      children.push(row.root);
    }
    for (const [id, view] of rowViews) if (!items.has(id)) { view.root.remove(); rowViews.delete(id); }
    if (!children.length) children.push(node('p', 'empty-state', listPending ? '正在读取关注事项…' : '暂时没有关注的事项。'));
    reconcileChildren($('#business-watch-list'), children); updateControls();
  }
  function renderDetail() {
    content.hidden = !detail; placeholder.hidden = !!detail;
    if (!detail) {
      title.textContent = state.textContent = notice.textContent = '';
      for (const value of values.values()) value.textContent = '';
      eventList.replaceChildren();
      placeholder.textContent = detailNotice || '选择一项关注查看当前记录'; updateControls(); return;
    }
    title.textContent = text(detail.title) || '标题未读到'; state.textContent = STATES[detail.state] || '状态待核实';
    notice.textContent = detailNotice || (detail.background_paused === true
      ? '维护暂停中，暂不检查、恢复或修改设置；可暂停或停止关注。下方为上次核实的结果。'
      : detail.last_error_code || incompleteCheck(detail) ? '最近检查未取得完整可信状态，下方保留上次核实的结果。' : '');
    notice.hidden = !notice.textContent;
    const snapshot = detail.snapshot && typeof detail.snapshot === 'object' ? detail.snapshot : {};
    const unavailable = new Set(Array.isArray(snapshot.unavailable_fields) ? snapshot.unavailable_fields : []);
    const field = key => unavailable.has(key) ? null : snapshot[key];
    values.get('workflow').textContent = FLOW_STATES[field('workflow_state')] || '未读到';
    const nodes = field('current_nodes');
    values.get('nodes').textContent = Array.isArray(nodes) && nodes.length
      ? nodes.filter(value => value && typeof value === 'object').map(value => text(value.name) + (names(value.handlers).length ? `（${names(value.handlers).join('、')}）` : '')).join('；') || '未读到'
      : Array.isArray(nodes) && ['ended','completed','finished','revoked','canceled','terminated'].includes(field('workflow_state'))
        ? '无当前节点（流程已结束）' : '未读到';
    values.get('handlers').textContent = names(field('current_handlers')).join('、') ||
      (Array.isArray(field('current_handlers')) && ['ended','completed','finished','revoked','canceled','terminated'].includes(field('workflow_state'))
        ? '无当前处理人（流程已结束）' : '未读到');
    values.get('observed').textContent = timestamp(field('observed_at'));
    values.get('checked').textContent = timestamp(detail.last_checked_at);
    values.get('next').textContent = detail.background_paused === true ? '维护暂停中' : detail.state !== 'active' ? '当前不安排检查'
      : detail.next_check_at ? timestamp(detail.next_check_at) : '尚未安排';
    values.get('expires').textContent = timestamp(detail.expires_at);
    const seen = new Set(), records = [];
    for (const event of events) {
      if (!event || typeof event !== 'object') continue;
      const id = event.event_id ?? event.sequence;
      if (id != null && seen.has(id)) continue;
      if (id != null) seen.add(id);
      const summary = changeSummary(event);
      if (summary) records.push(node('li', '', `${timestamp(event.observed_at || event.created_at)} · ${summary}`));
      if (records.length === 100) break;
    }
    eventList.replaceChildren(...(records.length ? records : [node('li', '', '暂无已核实的变化。')]));
    updateControls();
  }
  function closeEditor() {
    if (panelScope?.current()) panelScope.replace('editor').dispose();
    editorTicket++; editor = null; editorPending = false;
    interval.value = days.value = ''; consent.checked = false;
    for (const input of ruleInputs.values()) input.checked = false;
    endpointInputs.clear(); endpointsList.replaceChildren(); editorTarget.textContent = editorNotice.textContent = '';
    renderList(); renderDetail();
  }
  async function startEditor(mode, target) {
    if (!active || actionPending || editorPending) return;
    closeEditor();
    editor = {mode,title:text(target.title) || '标题未读到',targetRef:mode === 'create' ? target.target_ref : null,
      id:mode === 'configure' ? target.watch_id : null,version:target.version,key:mode === 'create' ? makeKey() : null,
      loaded:false,enabled:false,needsVerify:false,sourceInvalid:false,notice:'正在读取关注选项…'};
    const editing = editor, scope = panelScope.replace('editor'), epoch = generation, ticket = ++editorTicket;
    detailVisible = true; $('#business-watch-detail').className = 'business-watch-detail'; renderDetail();
    try {
      const result = await request('/api/business-watches/options',scope,{timeoutMs:15000});
      if (!current(scope,epoch) || ticket !== editorTicket || editor !== editing) return;
      if (typeof result.enabled !== 'boolean' || !Array.isArray(result.endpoints)) throw new Error('Invalid watch options');
      editing.loaded = true; editing.enabled = result.enabled && result.backgroundPaused !== true;
      editing.notice = !result.enabled ? text(result.message) || '当前账号没有持续跟踪权限，请联系管理员了解可用范围。'
        : result.backgroundPaused === true ? '维护暂停中，暂不能新建或修改关注。可返回列表查看已存记录。' : '';
      if (!result.enabled) { updateControls(); return; }
      const defaults = result.defaults || {};
      interval.value = String(mode === 'create' ? defaults.interval_seconds ?? 3600 : target.interval_seconds);
      if (!INTERVALS.some(([value]) => String(value) === interval.value)) interval.value = '3600';
      days.value = mode === 'create' ? String(defaults.expires_in_days ?? 7) : '';
      days.required = mode === 'create'; days.placeholder = mode === 'create' ? '' : '留空保留当前到期时间';
      expiryNote.textContent = mode === 'create' ? '关注 1 至 30 天，到期后停止检查。'
        : `当前到期：${timestamp(target.expires_at)}。期限留空则保留；填写后从保存时起重新计算。`;
      consent.checked = mode === 'configure' && target.allow_mark_read === true;
      readNotice.textContent = text(result.readNotice).trim() || '检查可能将事项标为已读。';
      const rules = mode === 'create' ? defaults.event_types ?? ['nodes','handlers','ended'] : target.event_types ?? [];
      for (const [value,input] of ruleInputs) input.checked = Array.isArray(rules) && rules.includes(value);
      const seen = new Set();
      for (const endpoint of result.endpoints) {
        if (!object(endpoint) || !text(endpoint.endpoint_id) || seen.has(endpoint.endpoint_id)) throw new Error('Invalid endpoint options');
        seen.add(endpoint.endpoint_id);
        const input = node('input'); input.type = 'checkbox'; input.name = 'endpoint_ids'; input.value = endpoint.endpoint_id;
        input.checked = mode === 'configure' && Array.isArray(target.endpoint_ids) && target.endpoint_ids.includes(endpoint.endpoint_id);
        const item = node('label','business-watch-check'); item.append(input,node('span','',text(endpoint.label) || text(endpoint.client_type) || '本人端点'));
        endpointsList.append(item); endpointInputs.set(endpoint.endpoint_id,input);
      }
      if (!endpointInputs.size) endpointsList.append(node('p','business-watch-explanation','暂无可用的本人通知端点。'));
      if (mode === 'configure' && names(target.endpoint_ids).some(id => !seen.has(id)))
        editing.notice = '部分原提醒端点已不可用；保存后仅保留下方选中的可用端点。';
      interval.focus(); updateControls();
    } catch (error) {
      if (isCancellation(error) || !current(scope,epoch) || ticket !== editorTicket || editor !== editing) return;
      editing.loaded = false; editing.notice = '读取关注选项失败，请取消后重试；没有创建或修改关注。';
      updateControls();
    }
  }
  async function createFrom(target) {
    if (!object(target) || !text(target.target_ref).trim() || target.target_ref.length > 8192) return;
    await startEditor('create',target);
  }
  async function openSettings() {
    if (!active || !verified || !detail || detail.background_paused === true || !['active','paused'].includes(detail.state)) return;
    await startEditor('configure',detail);
  }
  async function submitEditor() {
    const editing = editor;
    if (!active || !editing || editorPending || !editing.loaded || !editing.enabled || editing.needsVerify || editing.sourceInvalid) return;
    const selectedRules = [...ruleInputs].filter(([,input]) => input.checked).map(([value]) => value);
    const dayText = String(days.value).trim(), dayValue = dayText ? Number(dayText) : null;
    if (!INTERVALS.some(([value]) => String(value) === interval.value) || !selectedRules.length ||
        (dayValue !== null && (!/^[0-9]+$/.test(dayText) || !Number.isSafeInteger(dayValue) || dayValue < 1 || dayValue > 30)) ||
        (editing.mode === 'create' && (dayValue === null || !consent.checked))) {
      editing.notice = !selectedRules.length ? '请至少选择一种关注变化。' : editing.mode === 'create' && !consent.checked
        ? '请主动确认“允许检查时标为已读”后再开始关注。' : '请选择检查频率，并填写 1 至 30 天的期限。'; updateControls(); return;
    }
    const body = {interval_seconds:Number(interval.value),event_types:selectedRules,
      endpoint_ids:[...endpointInputs].filter(([,input]) => input.checked).map(([id]) => id),allow_mark_read:consent.checked};
    if (dayValue !== null) body.expires_in_days = dayValue;
    if (editing.mode === 'create') { body.target_ref = editing.targetRef; body.idempotency_key = editing.key; }
    else { body.action = 'configure'; body.expected_version = editing.version; }
    const scope = panelScope.replace('editor-save'), epoch = generation;
    editorPending = true; editing.notice = editing.mode === 'create' ? '正在核验来源并开始关注…' : '正在保存关注设置…'; updateControls();
    try {
      const result = await request(editing.mode === 'create' ? '/api/business-watches' : `/api/business-watches/${encodeURIComponent(editing.id)}/update`,
        scope,{method:'POST',csrf:true,body,timeoutMs:editing.mode === 'create' ? 45000 : 15000});
      if (!current(scope,epoch) || editor !== editing) return;
      if (!validWatch(result.watch) || !Object.hasOwn(result.watch,'title') ||
          (editing.mode === 'configure' && (result.watch.watch_id !== editing.id || result.watch.version < editing.version))) throw new Error('Invalid watch receipt');
      const value = merge(result.watch); selectedId = value.watch_id; detail = value; events = Array.isArray(result.events) ? result.events : [];
      verified = value.version === result.watch.version; detailNotice = editing.mode === 'create' ? '已开始关注；最新状态以读取时间为准。' : '关注设置已保存。';
      editorPending = false; closeEditor();
    } catch (error) {
      if (isCancellation(error) || !current(scope,epoch) || editor !== editing) return;
      const denied = error.status === 401 && error.code !== 'WATCH_SESSION_UNAVAILABLE'
        || error.status === 403 && ['WATCH_PRINCIPAL_CHANGED','HTTP403_PERMISSION_DENIED'].includes(error.code);
      if (denied) { editorPending = false; closeEditor(); detailNotice = errorMessage(error); clearAccess(); return; }
      editing.needsVerify = true;
      editing.sourceInvalid = editing.mode === 'create' && ['WATCH_REFERENCE_INVALID','WATCH_SOURCE_UNAVAILABLE'].includes(error.code);
      editing.notice = editing.sourceInvalid ? '来源引用已失效或暂不可核实，请重新查询 OA 历史后再关注。设置已保留，系统不会自动重发。'
        : errorMessage(error,true) + ' 编辑内容已保留。';
    } finally {
      if (current(scope,epoch) && editor === editing) { editorPending = false; updateControls(); }
    }
  }
  async function refreshEditor() {
    const editing = editor;
    if (!active || !editing || editorPending) return;
    if (editing.mode === 'create') {
      await load();
      if (editor === editing) { editing.notice = '已刷新关注列表，请核对是否已创建；本表单不会重复提交。重新创建前请取消并重新查询来源。'; updateControls(); }
      return;
    }
    await select(editing.id,{preserve:true});
    if (editor !== editing) return;
    if (verified && detail?.watch_id === editing.id && detail.background_paused !== true && ['active','paused'].includes(detail.state)) {
      editing.version = detail.version; editing.needsVerify = false;
      editing.notice = '已刷新当前记录，保留了你的编辑内容；请核对后再保存。';
    } else editing.notice = '尚未核实可修改的当前版本，编辑内容已保留。请刷新核对。';
    updateControls();
  }
  function clearAccess(id = null) {
    if (editor) closeEditor();
    if (id) items.delete(id); else items.clear();
    if (!id || selectedId === id) { selectedId = null; detail = null; events = []; verified = false; }
    renderList(); renderDetail();
  }
  async function load({append = false} = {}) {
    if (!active || (append && (!cursor || listPending)) || actionPending) return;
    const scope = panelScope.replace('list'), epoch = generation, ticket = ++listTicket;
    const requestedCursor = append ? cursor : null;
    listPending = true; lastAppend = append; $('#business-watch-retry').hidden = true;
    showListNotice(append ? '正在加载更多…' : '正在读取关注事项…'); updateControls();
    try {
      const result = await request('/api/business-watches' + (requestedCursor ? `?cursor=${encodeURIComponent(requestedCursor)}` : ''), scope, {timeoutMs:15000});
      if (!current(scope, epoch) || ticket !== listTicket) return;
      if (!Array.isArray(result.items) || result.items.some(item => !validWatch(item)) ||
          (result.hasMore && (typeof result.nextCursor !== 'string' || !result.nextCursor))) throw new Error('Invalid watch list');
      const previous = items, next = append ? new Map(items) : new Map();
      for (const watch of result.items) {
        const old = previous.get(watch.watch_id);
        next.set(watch.watch_id, old && old.version > watch.version ? old : watch);
      }
      const top = $('#business-watch-list').scrollTop;
      items = next; cursor = result.hasMore ? result.nextCursor : null;
      renderList(); $('#business-watch-list').scrollTop = append ? top : 0;
      showListNotice(items.size ? `已显示 ${items.size} 项${cursor ? '，可继续加载。' : '。'}` : '暂时没有关注的事项。');
    } catch (error) {
      if (isCancellation(error) || !current(scope, epoch) || ticket !== listTicket) return;
      if (error.status === 403 || (error.status === 401 && error.code !== 'WATCH_SESSION_UNAVAILABLE')) clearAccess();
      if (append && error.status === 400) { cursor = null; lastAppend = false; }
      showListNotice(errorMessage(error)); $('#business-watch-retry').hidden = false;
    } finally {
      if (current(scope, epoch) && ticket === listTicket) { listPending = false; updateControls(); }
    }
  }
  async function select(id, {preserve = false, reveal = true} = {}) {
    if (!active || actionPending || typeof id !== 'string') return;
    const same = selectedId === id;
    selectedId = id; verified = false;
    if (!same || !preserve) { detail = null; events = []; }
    detailNotice = same && detail ? '正在刷新关注记录…' : '正在读取关注详情…'; detailPending = true;
    const scope = panelScope.replace('detail'), epoch = generation, ticket = ++detailTicket;
    detailVisible = reveal;
    $('#business-watch-detail').className = 'business-watch-detail' + (reveal ? '' : ' mobile-empty'); renderList(); renderDetail();
    try {
      const result = await request(`/api/business-watches/${encodeURIComponent(id)}`, scope, {timeoutMs:15000});
      if (!current(scope, epoch) || ticket !== detailTicket || selectedId !== id) return;
      if (!validWatch(result.watch) || result.watch.watch_id !== id || !Array.isArray(result.events)) throw new Error('Invalid watch detail');
      detail = merge(result.watch);
      verified = detail.version === result.watch.version;
      if (verified) events = result.events;
      detailNotice = verified ? '' : '关注记录有更新，请刷新详情后再操作。';
      renderList(); renderDetail();
    } catch (error) {
      if (isCancellation(error) || !current(scope, epoch) || ticket !== detailTicket) return;
      detailNotice = errorMessage(error);
      if ([403,404].includes(error.status) || (error.status === 401 && error.code !== 'WATCH_SESSION_UNAVAILABLE')) clearAccess(id);
      renderDetail();
    } finally {
      if (current(scope, epoch) && ticket === detailTicket) { detailPending = false; updateControls(); }
    }
  }
  async function act(action) {
    const allowed = action === 'check' || action === 'pause' ? detail?.state === 'active'
      : action === 'resume' ? detail?.state === 'paused' : action === 'stop' && ['active','paused'].includes(detail?.state);
    if (!active || !verified || actionPending || detailPending || editor || !allowed
        || detail?.background_paused === true && ['check','resume'].includes(action)) return;
    const id = selectedId, version = detail.version, epoch = generation;
    const scope = panelScope.replace('action'); actionPending = true; verified = false;
    buttons[action].dataset.running = 'true'; detailNotice = action === 'check' ? '正在核验最新流程状态…' : '正在更新关注记录…';
    renderDetail();
    try {
      const result = await request(`/api/business-watches/${encodeURIComponent(id)}/${action === 'check' ? 'check' : 'update'}`, scope,
        {method:'POST', csrf:true, body:{expected_version:version, ...(action === 'check' ? {} : {action})}, timeoutMs:action === 'check' ? 45000 : 15000});
      if (!current(scope, epoch) || selectedId !== id) return;
      if (!validWatch(result.watch) || result.watch.watch_id !== id || result.watch.version < version) throw new Error('Invalid watch receipt');
      if (!Object.hasOwn(result.watch, 'title')) {
        detailNotice = (action === 'stop' ? '关注已停止。' : action === 'pause' ? '关注已暂停。' : '关注记录已更新。')
          + '当前没有读取详情的权限，旧结果已清除。';
        clearAccess(id); return;
      }
      detail = merge(result.watch); verified = detail.version === result.watch.version;
      if (Array.isArray(result.events)) events = result.events;
      detailNotice = verified ? action === 'check' ? incompleteCheck(detail)
        ? '本次未取得完整可信状态，下方保留上次核实的结果。' : '本次核验已结束，流程状态以读取时间为准。' : '关注记录已更新。'
        : '关注记录有更新，请刷新详情后再操作。';
      renderList(); renderDetail();
    } catch (error) {
      if (isCancellation(error) || !current(scope, epoch)) return;
      detailNotice = errorMessage(error, true);
      if ([403,404].includes(error.status) || (error.status === 401 && error.code !== 'WATCH_SESSION_UNAVAILABLE')) clearAccess(id);
      renderDetail();
    } finally {
      if (current(scope, epoch)) {
        actionPending = false; buttons[action].dataset.running = 'false'; updateControls();
      }
    }
  }
  function deactivate() {
    active = false; generation++; listTicket++; detailTicket++; panelScope?.dispose(); panelScope = null;
    listPending = detailPending = actionPending = verified = false;
    if (editor) closeEditor();
    for (const button of Object.values(buttons)) button.dataset.running = 'false';
    $('#refresh-tasks').disabled = false;
  }
  async function activate() {
    deactivate(); active = true; panelScope = getScope().replace('business-watch-panel');
    panelScope.defer(() => { active = false; verified = false; });
    const selected = selectedId, reveal = detailVisible;
    await load();
    if (active && selected && selectedId === selected) await select(selected, {preserve:true, reveal});
  }
  async function refresh() {
    if (!active || actionPending || editorPending) return;
    if (editor) return refreshEditor();
    const selected = selectedId, reveal = detailVisible;
    await load();
    if (active && selected && selectedId === selected) await select(selected, {preserve:true, reveal});
  }
  $('#business-watch-more').addEventListener('click', () => load({append:true}));
  $('#business-watch-retry').addEventListener('click', () => load({append:lastAppend}));
  function reset() {
    deactivate(); items.clear(); rowViews.clear(); cursor = selectedId = detail = null; events = [];
    detailNotice = listNotice = ''; detailVisible = false; $('#business-watch-retry').hidden = true;
    showListNotice(''); renderList(); renderDetail();
    $('#business-watch-detail').className = 'business-watch-detail mobile-empty';
  }
  reset();
  return {activate, deactivate, refresh, load, select, act, reset, createFrom, openSettings, submitEditor, refreshEditor, closeEditor};
}
