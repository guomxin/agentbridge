import { reconcileChildren } from "./workspace_dom.mjs";

// A selected result records preparation intent only. Approval remains on the
// existing trusted page, with one explicit decision for each business item.
export function createActionableResults({ document, state, prepareTargets,
  openPreparation,
  makeRequestKey = () => crypto.randomUUID() }) {
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
        request: null, status: "", group };
      state.actionableGroups.set(key, model);
    }
    model.group = group;
    return model;
  }
  const preparationStates = {
    queued: ["准备中", "查看进度"],
    preparing: ["正在准备材料", "查看进度"],
    waiting_user: ["等待确认", "继续办理"],
    running: ["提交中", "查看进度"],
    succeeded: ["已完成", "查看结果"],
    partially_succeeded: ["部分完成，请查看结果", "查看结果"],
    failed: ["办理失败，请查看原任务；重新办理需重新查询待办。", "查看结果"],
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
      return {taskId, message: known?.[0] || "办理状态待核实，请查看原任务。",
        label: known?.[1] || "查看原任务", state: known ? supplied.state : "unknown"};
    }
    const accepted = model.accepted.get(target.targetKey);
    return accepted ? {taskId: accepted, message: "已发起准备，正在等待办理进度。", label: "查看进度", state: "queued"} : null;
  }
  function canPrepare(model, target) {
    return target.enabled === true && !preparationFor(model, target);
  }
  function openOrPrepare(model, key) {
    if (model.pending || model.request) return;
    const target = model.group.targets.find(item => item.targetKey === key);
    if (!target) return;
    const preparation = preparationFor(model, target);
    if (preparation) {
      if (preparation.taskId && typeof openPreparation === "function") openPreparation(preparation.taskId);
      return;
    }
    if (canPrepare(model, target)) void submit(model, [key]);
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
      for (const key of model.request.targetKeys) {
        const target = model.group.targets.find(item => item.targetKey === key);
        if (!target?.preparation) {
          model.accepted.set(key, result.taskId);
          awaitingProjection = true;
        }
        model.selected.delete(key);
      }
      model.request = null;
      model.status = awaitingProjection ? "已记录准备请求，请在新任务核对材料并确认。" : "";
    } catch (error) {
      model.status = error.message || "准备应答尚未确认，请恢复本次准备。";
      if ([400, 403, 404, 409, 422].includes(error.status)) model.request = null;
    } finally { model.pending = false; update(model); }
  }
  function update(model) {
    const group = model.group;
    const targets = new Map(group.targets.map(t => [t.targetKey, t]));
    for (const key of model.selected) if (!targets.has(key) || !canPrepare(model, targets.get(key))) model.selected.delete(key);
    for (const view of model.views) {
      const children = [];
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
        row.reason.textContent = preparation?.message || target.disabledReason || "";
        row.reason.hidden = !row.reason.textContent;
        row.checkbox.checked = model.selected.has(target.targetKey);
        row.checkbox.disabled = model.pending || Boolean(model.request) || !canPrepare(model, target);
        row.prepare.disabled = model.pending || Boolean(model.request) || (preparation
          ? !preparation.taskId || typeof openPreparation !== "function" : !canPrepare(model, target));
        children.push(row.root);
      }
      reconcileChildren(view.list, children);
      view.status.textContent = model.status;
      view.selectAll.disabled = model.pending || Boolean(model.request) || !group.targets.length ||
        group.targets.some(t => !canPrepare(model, t));
      view.submit.disabled = model.pending || (!model.request && !model.selected.size);
      view.submit.textContent = model.pending ? "正在准备…" : model.request ? "恢复本次准备" : `准备已选事项（${model.selected.size}）`;
    }
  }
  function create(group) {
    const model = modelFor(group);
    const root = node("section", "actionable-results");
    const title = node("h3", "", "选择待办并进入办理");
    const explanation = node("p", "", "选择精确事项后直接准备对应界面；每项仍需核对材料并明确确认。不可选事项不会被自动排除后批量办理。");
    const list = node("div", "actionable-results-list"), actions = node("div", "application-card-actions");
    const selectAll = node("button", "secondary", "选择全部"), submitButton = node("button", "primary");
    selectAll.type = submitButton.type = "button";
    const status = node("p", "actionable-result-status"); status.setAttribute("role", "status");
    selectAll.addEventListener("click", () => {
      if (selectAll.disabled) return;
      model.selected = new Set(model.group.targets.map(t => t.targetKey)); update(model);
    });
    submitButton.addEventListener("click", () => submit(model, [...model.selected]));
    actions.append(selectAll, submitButton); root.append(title, explanation, list, actions, status);
    const view = { root, list, rows: new Map(), selectAll, submit: submitButton, status };
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
      if (view.model.request && !groups.some(group => `${group.sourceTaskId}:${group.operationId}` === key)) {
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
      if (!view.model.pending && !view.model.request) view.model.status = "";
      update(view.model); children.push(view.root);
    }
    for (const [key, view] of container._actionableViews) {
      if (!groups.some(group => `${group.sourceTaskId}:${group.operationId}` === key)) {
        view.model.views.delete(view); container._actionableViews.delete(key);
        if (!view.model.views.size && !view.model.request && !view.model.pending) state.actionableGroups.delete(key);
      }
    }
    reconcileChildren(container, children);
    container.hidden = !children.length;
  }
  return { render };
}
