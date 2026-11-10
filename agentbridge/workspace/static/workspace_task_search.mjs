import { isCancellation } from './workspace_lifecycle.mjs';

const fields = {q:'#task-search', status:'#task-status', system_id:'#task-system',
  updated_from:'#task-from', updated_to:'#task-to'};
const defaults = {q:'', status:'all', system_id:'', updated_from:'', updated_to:''};

// Only reading preferences are saved. A fresh authenticated request decides
// which tasks are visible; no cursor or business authority is restored.
export function createTaskSearch({state, $, getScope, request, renderTasks, viewState, onApply = () => {}}) {
  let filters = {...defaults}, cursor = null, pending = false, generation = 0;
  let initialized = false, lastAppend = false, scope = null, changedDuringLoad = false;
  const read = () => Object.fromEntries(Object.entries(fields).map(([name, selector]) =>
    [name, String($(selector).value || '').trim()]));
  const write = value => { for (const [name, selector] of Object.entries(fields)) $(selector).value = value[name] || ''; };
  const message = copy => { $('#task-list-status').textContent = copy; };
  const controls = () => {
    $('#task-more').hidden = !cursor;
    $('#task-more').disabled = pending;
    $('#task-more').textContent = pending && lastAppend ? '正在加载…' : '加载更多';
    $('#task-filters').setAttribute('aria-busy', String(pending));
  };
  const restore = () => {
    filters = {...defaults, ...viewState.taskFilters()};
    write(filters); initialized = true; cursor = null;
    $('#task-retry').hidden = true; message(''); controls();
  };
  async function load({append = false, automatic = false} = {}) {
    if (!initialized) return;
    // Incoming task events must not collapse a list the user has paged through.
    if (automatic && (pending || state.tasks.length > 32 || $('#task-list').scrollTop > 20)) {
      changedDuringLoad = true;
      if (!pending) message('任务有更新，刷新可查看。');
      return;
    }
    if (append && (!cursor || pending)) return;
    scope = getScope().replace('task-search');
    const current = scope, ticket = ++generation;
    const parameters = new URLSearchParams({...filters, limit:'32'});
    if (append) parameters.set('cursor', cursor);
    pending = true; lastAppend = append; changedDuringLoad = false;
    $('#task-retry').hidden = true;
    message(append ? '正在加载更多…' : '正在查找…'); controls();
    try {
      const result = await request(`/api/tasks?${parameters}`, current);
      current.assertCurrent();
      if (ticket !== generation) return;
      const items = append ? [...state.tasks] : [];
      const seen = new Set(items.map(task => task.task_id));
      for (const task of result.items || []) if (!seen.has(task.task_id)) {
        seen.add(task.task_id); items.push(task);
      }
      const list = $('#task-list'), top = append ? list.scrollTop : 0;
      state.tasks = items;
      if (Number.isSafeInteger(result.activeCount)) $('#active-task-count').textContent = String(result.activeCount);
      cursor = result.hasMore ? result.nextCursor?.cursor || null : null;
      renderTasks(); list.scrollTop = top;
      message((items.length ? `已显示 ${items.length} 项${cursor ? '，可继续加载。' : '。'}` : '没有符合条件的任务。')
        + (changedDuringLoad ? '任务有更新，刷新可查看。' : ''));
    } catch (error) {
      if (isCancellation(error) || !current.current() || ticket !== generation) return;
      if (append && error.status === 400) { cursor = null; lastAppend = false; }
      message(error.status === 401 ? '登录已失效，请重新登录。'
        : error.status === 403 ? '暂时没有查看权限。'
        : error.status === 400 ? error.message || '请检查筛选条件后重试。'
        : state.tasks.length ? '加载失败，已保留上次结果。' : '加载失败，请重试。');
      $('#task-retry').hidden = false;
    } finally {
      if (current.current() && ticket === generation) { pending = false; controls(); }
    }
  }
  const apply = () => {
    onApply();
    filters = read(); viewState.saveTaskFilters(filters); cursor = null;
    return load();
  };
  const bind = () => {
    $('#task-filters').addEventListener('submit', event => { event.preventDefault(); apply(); });
    $('#task-status').addEventListener('change', apply);
    $('#task-system').addEventListener('change', apply);
    $('#task-clear').addEventListener('click', () => { write(defaults); apply(); });
    $('#task-more').addEventListener('click', () => load({append:true}));
    $('#task-retry').addEventListener('click', () => load({append:lastAppend}));
    for (const [selector, status] of [['#tasks-waiting','waiting_user'], ['#tasks-review','outcome_unknown'], ['#tasks-failed','failed']]) {
      $(selector).addEventListener('click', () => { $('#task-status').value = status; apply(); });
    }
  };
  return {bind, restore, load, apply, reset() {
    initialized = false; generation++; scope?.dispose(); scope = null;
    cursor = null; pending = false; changedDuringLoad = false; filters = {...defaults};
    $('#active-task-count').textContent = '0';
    write(filters); message(''); $('#task-retry').hidden = true; controls();
  }};
}
