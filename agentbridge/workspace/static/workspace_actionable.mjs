import { reconcileChildren } from "./workspace_dom.mjs";
import { formatTime } from "./workspace_results.mjs";

// A selected result records preparation intent only. Approval remains on the
// existing trusted page, with one explicit decision for each business item.
export function createActionableResults({ document, state, prepareTargets,
  openPreparation, refreshQuery, viewState, now = () => Date.now(),
  makeRequestKey = () => crypto.randomUUID(), getScope, timers = globalThis }) {
  state.actionableGroups ||= new Map();
  const node = (tag, className = "", text = "") => {
    const element = document.createElement(tag);
    element.className = className; element.textContent = text;
    return element;
  };
  function modelFor(group) {
    const key = `${group.sourceTaskId}:${group.operationId}`;
    let model = state.actionableGroups.get(key);
    if (!model) {
      model = { selected: new Set(), accepted: new Map(), views: new Set(), pending: false,
        request: null, status: "", group, search: "", type: "", onlySelected: false, refreshRequest: null,
        scope:getScope?.(), disposed:false };
      const stored = viewState?.getSource(key);
      if (stored) Object.assign(model,{...stored,selected:new Set(stored.selected)});
      state.actionableGroups.set(key, model);
      model.scope?.defer(() => { model.disposed = true; clearExpiry(model); });
    }
    model.group = group;
    return model;
  }
  function clearExpiry(model) {
    if (model.scope) model.scope.clearTimer(model.expiryTimer); else timers.clearTimeout(model.expiryTimer);
    model.expiryTimer = null;
  }
  const preparationStates = {
    queued: ["准备中", "查看进度"],
    preparing: ["正在准备材料", "查看进度"],
    waiting_user: ["等待确认", "继续办理"],
    running: ["提交中", "查看进度"],
    succeeded: ["已完成", "查看结果"],
    verified_complete: ["原事项已核验完成", "查看结果"],
    partially_succeeded: ["部分完成，请查看结果", "查看结果"],
    failed: ["办理未完成，请查看原任务的提交与核验说明；不要重复准备或提交。", "查看结果"],
    expired: ["办理已过期，重新办理请重新查询待办。", "查看结果"],
    canceled: ["办理已取消，重新办理请重新查询待办。", "查看结果"],
    outcome_unknown: ["办理结果待核对，请查看原任务，不要重复提交。", "查看结果"],
    superseded: ["原办理已被替换，请查看原任务。", "查看结果"],
    skipped: ["此项未处理，重新办理请重新查询待办。", "查看结果"],
  };
  function preparationFor(model, target) {
    const supplied = target.preparation;
    if (supplied !== undefined && supplied !== null) {
      const known = typeof supplied === "object" && !Array.isArray(supplied) &&
        Object.hasOwn(preparationStates, supplied.state) ? preparationStates[supplied.state] : null;
      const taskId = typeof supplied?.taskId === "string" && supplied.taskId.trim() ? supplied.taskId : null;
      if (known && supplied.state === "verified_complete") {
        const fresh = Number.isFinite(Date.parse(supplied.expiresAt)) && Date.parse(supplied.expiresAt) > now();
        const checked = Number.isFinite(Date.parse(supplied.observedAt)) ? ` 核验时间：${formatTime(supplied.observedAt)}。` : "";
        return {taskId, state:fresh ? "verified_complete" : "outcome_unknown", label:fresh ? "查看结果" : "核对原事项",
          message:fresh ? `原事项已核验完成。${checked}` : `上次核验已完成；该次核对已过期，请查看原任务重新核对当前状态。${checked}`};
      }
      return {taskId, message: known?.[0] || "办理状态待核实，请查看原任务。",
        label: known?.[1] || "查看原任务", state: known ? supplied.state : "unknown"};
    }
    const accepted = model.accepted.get(target.targetKey);
    return accepted ? {taskId: accepted, message: "已发起准备，正在等待办理进度。", label: "查看进度", state: "queued"} : null;
  }
  function canPrepare(model, target) {
    return !expired(model.group) && target.enabled === true && !preparationFor(model, target);
  }
  function expired(group) {
    return Boolean(group.expiresAt) && !(Date.parse(group.expiresAt) > now());
  }
  function visible(model, target) {
    return (!model.type || target.workflowLabel === model.type) &&
      (!model.onlySelected || model.selected.has(target.targetKey)) &&
      (!model.search || [target.title, target.sender, target.workflowLabel].join(" ").toLocaleLowerCase().includes(model.search.toLocaleLowerCase()));
  }
  async function refresh(model) {
    if (model.pending || model.request || typeof refreshQuery !== "function") return;
    model.refreshRequest ||= {requestKey: makeRequestKey()};
    model.pending = true; model.status = "正在重新查询最新待办；原办理任务会保留。"; update(model);
    try {
      const result = await refreshQuery({...model.refreshRequest});
      if (!result?.taskId) throw new Error("查询受理状态尚未确认，请恢复本次查询。");
      model.refreshRequest = null;
      model.status = "最新查询已受理，请查看新的查询卡；原办理任务仍可继续。";
    } catch (error) { model.status = error.message || "暂时无法查询，请恢复同一次查询。"; }
    finally { model.pending = false; update(model); }
  }
  function openOrPrepare(model, key) {
    if (model.pending || model.request) return;
    const target = model.group.targets.find(item => item.targetKey === key);
    if (!target) return;
    const preparation = preparationFor(model, target);
    if (preparation) {
      if (preparation.taskId && typeof openPreparation === "function") handoff(model,preparation.taskId);
      return;
    }
    if (canPrepare(model, target)) void submit(model, [key]);
  }
  function handoff(model,taskId,detail,automatic = false) {
    if (model.disposed || model.scope && !model.scope.current()) return;
    model.cancelHandoff = openPreparation?.(taskId,{detail,automatic,onProgress(message,waiting) {
      model.status = message; model.handoff = waiting; model.handoffNotice = true; update(model);
    }});
  }
  async function submit(model, keys) {
    keys = model.request?.targetKeys || keys;
    if (model.pending || typeof prepareTargets !== "function" || !keys.length) return;
    const enabled = new Set(model.group.targets.filter(t => canPrepare(model, t)).map(t => t.targetKey));
    if (!model.request && keys.some(key => !enabled.has(key))) return;
    model.request ||= { operationId: model.group.operationId, targetKeys: [...keys], requestKey: makeRequestKey() };
    model.pending = true; model.status = "正在准备所选事项…"; update(model);
    try {
      const result = await prepareTargets(model.group.sourceTaskId, { ...model.request,
        targetKeys: [...model.request.targetKeys] });
      if (!result?.taskId) throw new Error("准备应答尚未确认，请恢复本次准备。");
      let awaitingProjection = false;
      let superseded = false;
      for (const key of model.request.targetKeys) {
        const target = model.group.targets.find(item => item.targetKey === key);
        if (target?.preparation && (target.preparation.taskId !== result.taskId ||
            !['queued','preparing','waiting_user','running'].includes(target.preparation.state))) superseded = true;
        if (!target?.preparation) {
          model.accepted.set(key, result.taskId);
          awaitingProjection = true;
        }
        model.selected.delete(key);
      }
      model.request = null;
      model.status = awaitingProjection ? "已记录准备请求，请在新任务核对材料并确认。" : "";
      if (!superseded && typeof openPreparation === 'function') handoff(model,result.taskId,result.detail,true);
    } catch (error) {
      model.status = error.message || "准备应答尚未确认，请恢复本次准备。";
      if ([400, 403, 404, 409, 422].includes(error.status)) model.request = null;
    } finally { model.pending = false; update(model); }
  }
  function update(model) {
    if (model.disposed || model.scope && !model.scope.current()) return;
    const group = model.group;
    clearExpiry(model);
    // A read-only verification has its own short evidence lifetime, which can
    // expire after the source query. Keep both boundaries live on open pages.
    const expiries = [Date.parse(group.expiresAt), ...group.targets.filter(target =>
      target.preparation?.state === "verified_complete").map(target => Date.parse(target.preparation.expiresAt))]
      .filter(value => Number.isFinite(value) && value > now());
    const remaining = expiries.length ? Math.min(...expiries) - now() : NaN;
    if (Number.isFinite(remaining) && remaining > 0 && model.views.size) {
      const expire = () => update(model), delay = Math.min(remaining + 20, 2147483647);
      model.expiryTimer = model.scope ? model.scope.timeout(expire, delay) : timers.setTimeout(expire, delay);
      model.expiryTimer?.unref?.();
    }
    const targets = new Map(group.targets.map(t => [t.targetKey, t]));
    for (const key of model.selected) if (!targets.has(key) || !canPrepare(model, targets.get(key))) model.selected.delete(key);
    viewState?.setSource(`${group.sourceTaskId}:${group.operationId}`,{search:model.search,type:model.type,
      onlySelected:model.onlySelected,selected:[...model.selected]});
    for (const view of model.views) {
      const children = [];
      const types = [...new Set(group.targets.map(t => t.workflowLabel).filter(Boolean))];
      if (view.typeSignature !== types.join("|")) {
        view.type.replaceChildren(node("option", "", "全部类型"), ...types.map(type => {
          const option = node("option", "", type); option.value = type; return option;
        }));
        view.type.children[0].value = ""; view.typeSignature = types.join("|");
      }
      view.search.value = model.search; view.type.value = model.type; view.onlySelected.checked = model.onlySelected;
      for (const target of group.targets) {
        let row = view.rows.get(target.targetKey);
        if (!row) {
          const root = node("div", "actionable-result-row");
          const label = node("label"), checkbox = node("input"), copy = node("span");
          checkbox.type = "checkbox";
          const title = node("strong"), meta = node("small"), reason = node("p");
          copy.append(title, meta); label.append(checkbox, copy);
          const prepare = node("button", "secondary", "核对办理"); prepare.type = "button";
          root.append(label, reason, prepare);
          row = { root, checkbox, title, meta, reason, prepare };
          checkbox.addEventListener("change", () => {
            if (checkbox.disabled) return;
            if (checkbox.checked) model.selected.add(target.targetKey); else model.selected.delete(target.targetKey);
            update(model);
          });
          prepare.addEventListener("click", () => openOrPrepare(model, target.targetKey));
          view.rows.set(target.targetKey, row);
        }
        row.title.textContent = target.title;
        row.meta.textContent = [target.workflowLabel, target.sender, target.date].filter(Boolean).join(" · ");
        const preparation = preparationFor(model, target);
        const label = preparation?.label || "核对办理";
        row.prepare.textContent = label;
        row.prepare.setAttribute("aria-label", `${label}：${target.title}`);
        row.root.dataset.preparationState = preparation?.state || "unprepared";
        row.root.hidden = !visible(model, target);
        row.reason.textContent = preparation?.message || (expired(group) ? "查询已过期，请重新查询最新待办。" : target.disabledReason) || "";
        row.reason.hidden = !row.reason.textContent;
        row.checkbox.checked = model.selected.has(target.targetKey);
        row.checkbox.disabled = model.pending || Boolean(model.request) || !canPrepare(model, target);
        row.prepare.disabled = model.pending || Boolean(model.request) || (preparation
          ? !preparation.taskId || typeof openPreparation !== "function" : !canPrepare(model, target));
        children.push(row.root);
      }
      reconcileChildren(view.list, children);
      view.status.textContent = model.status;
      view.cancelHandoff.hidden = !model.handoff;
      const available = group.targets.filter(t => visible(model, t) && canPrepare(model, t));
      const continuing = group.targets.filter(t => preparationFor(model, t)).length;
      const unavailable = group.targets.filter(t => !canPrepare(model, t) && !preparationFor(model, t)).length;
      view.count.textContent = `共 ${group.targets.length} 项 · 可新办 ${group.targets.filter(t => canPrepare(model, t)).length} 项 · 已有办理 ${continuing} 项 · 当前不可新办 ${unavailable} 项 · 已选 ${model.selected.size} 项`;
      view.empty.hidden = group.targets.some(t => visible(model, t));
      view.selectAll.textContent = `选择当前可办 ${available.length} 项`;
      view.selectAll.disabled = model.pending || Boolean(model.request) || !available.length;
      view.clear.disabled = model.pending || Boolean(model.request) || !model.selected.size;
      view.refresh.hidden = typeof refreshQuery !== "function";
      view.refresh.disabled = model.pending || Boolean(model.request);
      view.refresh.textContent = model.refreshRequest ? "恢复本次查询" : "重新查询待办";
      view.expiry.textContent = expired(group) ? "本次查询已过期；已有办理可查看原任务，新办理请重新查询。" : group.expiresAt ?
        `可选择至 ${new Date(group.expiresAt).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"})}；已有草稿按各自有效期继续。` : "";
      view.expiry.hidden = !view.expiry.textContent;
      view.submit.disabled = model.pending || (!model.request && !model.selected.size);
      view.submit.textContent = model.pending ? "正在准备…" : model.request ? "恢复本次准备" : `准备已选事项（${model.selected.size}）`;
    }
  }
  function create(group) {
    const model = modelFor(group);
    const root = node("section", "actionable-results");
    const title = node("h3", "", "选择待办并进入办理");
    const explanation = node("p", "", "选择要办理的具体事项，每项核对后确认一次。选择当前可办项仅包括当前筛选结果，未选及不可办事项会保留。");
    const filters = node("div", "actionable-result-filters");
    const search = node("input"); search.type = "search"; search.placeholder = "搜索事项或发起人"; search.setAttribute("aria-label", "搜索待办事项");
    const type = node("select"); type.setAttribute("aria-label", "筛选待办类型");
    const onlySelected = node("input"); onlySelected.type = "checkbox";
    const selectedLabel = node("label"); selectedLabel.append(onlySelected, node("span", "", "只看已选"));
    filters.append(search, type, selectedLabel);
    const count = node("p", "actionable-result-count"), expiry = node("p", "actionable-result-expiry");
    const empty = node("p", "", "没有符合筛选条件的事项；可修改搜索或类型。"); empty.hidden = true;
    const list = node("div", "actionable-results-list"), actions = node("div", "application-card-actions");
    const selectAll = node("button", "secondary"), clear = node("button", "secondary", "清除选择"), submitButton = node("button", "primary");
    const refreshButton = node("button", "secondary", "重新查询待办");
    selectAll.type = clear.type = submitButton.type = refreshButton.type = "button";
    const status = node("p", "actionable-result-status"); status.setAttribute("role", "status");
    const cancelHandoff = node('button','secondary','取消自动打开'); cancelHandoff.type = 'button'; cancelHandoff.hidden = true;
    cancelHandoff.addEventListener('click',() => model.cancelHandoff?.());
    selectAll.addEventListener("click", () => {
      if (selectAll.disabled) return;
      for (const target of model.group.targets) if (visible(model, target) && canPrepare(model, target)) model.selected.add(target.targetKey);
      update(model);
    });
    search.addEventListener("input", () => { model.search = search.value.trim(); update(model); });
    type.addEventListener("change", () => { model.type = type.value; update(model); });
    onlySelected.addEventListener("change", () => { model.onlySelected = onlySelected.checked; update(model); });
    clear.addEventListener("click", () => { if (!clear.disabled) { model.selected.clear(); update(model); } });
    refreshButton.addEventListener("click", () => { if (!refreshButton.disabled) void refresh(model); });
    submitButton.addEventListener("click", () => submit(model, [...model.selected]));
    actions.append(selectAll, clear, submitButton, refreshButton,cancelHandoff); root.append(title, explanation, filters, count, expiry, list, empty, actions, status);
    const view = { root, list, rows: new Map(), selectAll, clear, refresh: refreshButton, submit: submitButton, status, search, type, onlySelected, count, expiry, empty,cancelHandoff };
    view.model = model;
    model.views.add(view); update(model);
    return view;
  }
  function render(container, groups = []) {
    if (!Array.isArray(groups)) groups = [];
    container._actionableViews ||= new Map();
    // A lost acknowledgement can still be recovered after source expiry or a
    // permission change. Only the frozen request is sent; Central rechecks it.
    groups = [...groups];
    for (const [key, view] of container._actionableViews) {
      if ((view.model.request || view.model.refreshRequest) && !groups.some(group => `${group.sourceTaskId}:${group.operationId}` === key)) {
        groups.push({ ...view.model.group, targets: view.model.group.targets.map(target => ({ ...target,
          enabled: false, disabledReason: "查询已更新，请恢复原请求或重新查询后办理。" })) });
      }
    }
    const children = [];
    for (const group of groups) {
      const key = `${group.sourceTaskId}:${group.operationId}`;
      let view = container._actionableViews.get(key);
      if (!view) { view = create(group); container._actionableViews.set(key, view); }
      view.model.group = group;
      // Server projections own the durable child state. The accepted map only
      // bridges a returned task ID until its first authoritative projection.
      for (const target of group.targets) if (target.preparation) view.model.accepted.delete(target.targetKey);
      if (!view.model.pending && !view.model.request && !view.model.refreshRequest && !view.model.handoffNotice) view.model.status = "";
      update(view.model); children.push(view.root);
    }
    for (const [key, view] of container._actionableViews) {
      if (!groups.some(group => `${group.sourceTaskId}:${group.operationId}` === key)) {
        view.model.views.delete(view); container._actionableViews.delete(key);
        if (!view.model.views.size) clearExpiry(view.model);
        if (!view.model.views.size && !view.model.request && !view.model.refreshRequest && !view.model.pending) state.actionableGroups.delete(key);
      }
    }
    reconcileChildren(container, children);
    container.hidden = !children.length;
  }
  return { render, reset() {
    for (const model of state.actionableGroups.values()) {model.disposed=true;clearExpiry(model);model.views.clear();}
  } };
}
