import { taskPrimaryAction, TERMINAL_TASK_STATES } from './workspace_results.mjs';

// Browser storage is a reading preference, never a source of action authority.
export function createWorkspaceViewState({storage, getAccountId, now = Date.now}) {
  const key = () => getAccountId() ? `agentbridge-workspace-view:v1:${getAccountId()}` : null;
  const read = () => {
    try {
      const value = JSON.parse(storage?.getItem(key()) || 'null');
      return value && value.version === 1 && now() - value.savedAt < 86400000 && value.savedAt <= now()
        ? value : {version:1, sources:{}};
    } catch { return {version:1, sources:{}}; }
  };
  const write = value => { try { if (key()) storage?.setItem(key(), JSON.stringify({...value, savedAt:now()})); } catch {} };
  const source = value => ({search:String(value?.search || '').slice(0,160), type:String(value?.type || '').slice(0,120),
    onlySelected:value?.onlySelected === true,
    selected:Array.isArray(value?.selected) ? value.selected.filter(item => typeof item === 'string' && item.length <= 256).slice(0,100) : []});
  return {
    getSource(id) { return source(read().sources?.[id]); },
    setSource(id, value) {
      if (typeof id !== 'string' || id.length > 300) return;
      const record = read(), entries = Object.entries(record.sources || {}).filter(([name]) => name !== id).slice(-63);
      record.sources = Object.fromEntries([...entries,[id,source(value)]]); write(record);
    },
    reading() { const value = read().reading; return value && Number.isFinite(value.top) && value.top >= 0 ? value : null; },
    saveReading(value) {
      const record = read(); record.reading = {top:Math.max(0,Number(value.top) || 0), atBottom:value.atBottom === true,
        anchor:typeof value.anchor === 'string' ? value.anchor.slice(0,300) : '',offset:Number(value.offset) || 0}; write(record);
    },
    clear() { try { if (key()) storage?.removeItem(key()); } catch {} },
  };
}

export function createReadingScroll({container, notice, save = () => {}}) {
  let following = true;
  const snapshot = () => {
    const anchor = [...(container.children || [])].filter(node => node.dataset?.timelineKey &&
      Number.isFinite(node.offsetTop) && node.offsetTop <= container.scrollTop).at(-1);
    return {top:container.scrollTop,atBottom:following,anchor:anchor?.dataset.timelineKey || '',offset:anchor ? container.scrollTop-anchor.offsetTop : 0};
  };
  const onScroll = () => {
    following = container.scrollHeight - container.scrollTop - container.clientHeight < 120;
    if (following) notice.hidden = true;
    save(snapshot());
  };
  const update = ({force = false} = {}) => {
    if (force || following) { following = true; container.scrollTop = container.scrollHeight; notice.hidden = true; }
    else notice.hidden = false;
  };
  container.addEventListener('scroll', onScroll);
  const showLatest = () => { update({force:true}); save(snapshot()); };
  notice.addEventListener('click', showLatest);
  return {update, snapshot, restore(value) {
    if (!value || value.atBottom) update({force:true});
    else {
      const anchor = [...(container.children || [])].find(node => value.anchor && node.dataset?.timelineKey === value.anchor);
      const top = anchor && Number.isFinite(anchor.offsetTop) ? anchor.offsetTop + value.offset : value.top;
      following = false; container.scrollTop = Math.min(top, Math.max(0,container.scrollHeight-container.clientHeight)); notice.hidden = true;
    }
  }, dispose() { container.removeEventListener?.('scroll',onScroll); notice.removeEventListener?.('click',showLatest); }};
}

// Only an explicit current-page selection creates this short-lived navigation
// intent. No request, form URL or navigation intent is persisted or replayed.
export function createPreparationHandoff({getScope, read, navigate, openResult, now = Date.now}) {
  let cancelCurrent = null;
  return {cancel() { cancelCurrent?.(); }, start(taskId, {detail, automatic = false, onProgress = () => {}} = {}) {
    cancelCurrent?.();
    const scope = getScope().replace('preparation-handoff'), started = now();
    let finished = false;
    const finish = message => { if (finished) return; finished = true; onProgress(message, false); scope.dispose(); };
    const cancel = () => finish('已停止自动打开；准备中的任务会继续，可随时点击继续办理。');
    cancelCurrent = cancel;
    scope.defer(() => { if (!finished) { finished = true; onProgress('准备任务已保留，请在原卡继续办理。',false); } });
    async function inspect(value) {
      try {
        const result = value || await read(taskId,scope);
        if (!scope.current() || finished) return;
        if (result?.task?.task_id !== taskId) throw new Error('办理状态尚未确认，请在原卡继续办理。');
        const action = taskPrimaryAction(result);
        if (action.kind === 'form') {
          finish('表单已准备好，正在打开核对页面。'); navigate(action.url); return;
        }
        if (TERMINAL_TASK_STATES.has(result.task.status) || ['probe','open_recovery'].includes(action.kind)) {
          finish('本次准备已有结果，请查看原任务；不会自动重新提交。');
          if (!automatic) await openResult(taskId);
          return;
        }
        if (result.preparationHealth?.state === 'authentication_required' || result.preparationHealth?.state === 'expired') { finish(result.preparationHealth.message); return; }
        if (now()-started >= 60000) { finish('材料准备仍在进行，可稍后点击继续办理；无需重新选择。'); return; }
        onProgress('正在准备材料；表单就绪后会直接打开。可取消自动打开，任务仍会继续。',true);
        scope.timeout(() => inspect(),2000);
      } catch (error) {
        if (scope.current()) finish(error?.message || '暂时无法读取准备状态，请点击继续办理。');
      }
    }
    onProgress('正在读取本次办理状态…',true);
    void inspect(detail);
    return cancel;
  }};
}
