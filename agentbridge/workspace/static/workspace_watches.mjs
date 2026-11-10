import { isCancellation } from './workspace_lifecycle.mjs';
import { reconcileChildren } from './workspace_dom.mjs';

const STATES = {active:'关注中', paused:'已暂停', stopped:'已停止', completed:'关注已结束', expired:'已到期'};
const FLOW_STATES = {in_progress:'流转中', running:'流转中', active:'流转中', pending:'处理中', ended:'流程已结束',
  completed:'流程已结束', finished:'流程已结束', revoked:'已撤销', canceled:'已取消', terminated:'已终止'};
const EVENT_LABELS = {created:'开始关注', paused:'暂停关注', resumed:'恢复关注', stopped:'停止关注',
  completed:'关注已结束', expired:'关注已到期', changed:'流程状态有变化', checked:'已核验', configured:'关注设置已更新'};
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
export function createBusinessWatches({document, $, getScope, request}) {
  let active = false, panelScope = null, generation = 0, listTicket = 0, detailTicket = 0;
  let items = new Map(), cursor = null, selectedId = null, detail = null, events = [];
  let listPending = false, detailPending = false, actionPending = false, verified = false;
  let detailVisible = false;
  let lastAppend = false, detailNotice = '', listNotice = '';
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
  const boundary = node('p', 'business-watch-explanation', '关注及核验只读取流程状态，不办理、催办或撤销 OA 事项。停止关注不改变 OA 流程。');
  const eventTitle = node('h4', '', '最近关注记录'), eventList = node('ul', 'business-watch-events');
  content.append(back, title, state, notice, fields, explanation, controls, boundary, eventTitle, eventList);
  $('#business-watch-detail').replaceChildren(placeholder, content);
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
    const busy = listPending || actionPending;
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
      button.disabled = !allowed || !verified || actionPending || detailPending;
      button.textContent = actionPending && button.dataset.running === 'true' ? '正在处理…'
        : ({check:'核验最新状态',pause:'暂停关注',resume:'恢复关注',stop:'停止关注'})[action];
    }
    for (const row of rowViews.values()) row.root.disabled = actionPending;
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
    notice.textContent = detailNotice || (detail.last_error_code || incompleteCheck(detail) ? '最近检查未取得完整可信状态，下方保留上次核实的结果。' : '');
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
    values.get('next').textContent = detail.state !== 'active' ? '当前不安排检查' : detail.next_check_at ? timestamp(detail.next_check_at) : '尚未安排';
    values.get('expires').textContent = timestamp(detail.expires_at);
    const seen = new Set(), records = [];
    for (const event of events) {
      if (!event || typeof event !== 'object') continue;
      const id = event.event_id ?? event.sequence;
      if (id != null && seen.has(id)) continue;
      if (id != null) seen.add(id);
      const kind = text(event.event_type || event.type).split('.').at(-1);
      records.push(node('li', '', `${timestamp(event.created_at || event.observed_at)} · ${text(event.summary) || EVENT_LABELS[kind] || '关注记录已更新'}`));
    }
    eventList.replaceChildren(...(records.length ? records : [node('li', '', '暂无关注记录。')]));
    updateControls();
  }
  function clearAccess(id = null) {
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
    if (!active || !verified || actionPending || detailPending || !allowed) return;
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
    if (!active || actionPending) return;
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
  return {activate, deactivate, refresh, load, select, act, reset};
}
