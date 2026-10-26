import { completedInteractionPresentation, taskCardStatusForInteraction,
  taskCardStatusMessage, displayTaskTitle, skillProfileLabel, taskPlanProgress,
  taskPlanFailurePresentation, interactionLabel, interactionActionLabel,
  escapeClass, formatTime, statusLabel, eventLabel, taskInteractions,
  actionableTaskInteractions, batchItemStatusLabel, interactionStateLabel, trustedInteractionUrl,
  TERMINAL_TASK_STATES, businessExecutionPresentation, taskResultTitle, taskPrimaryAction, oaRecoveryPresentation, executionWaitMessage } from "./workspace_results.mjs";

import { reconcileChildren } from "./workspace_dom.mjs";
import { markWorkspaceTiming } from "./workspace_performance.mjs";
import { createActionableResults } from "./workspace_actionable.mjs";
import { createOaPendingRecovery } from "./workspace_recovery.mjs";
import {createBatchRemaining} from './workspace_batch_remaining.mjs';

export function createTaskCards({ document, state = {}, $, renderChatTimeline, scrollChat, setTimelineNode, loadTaskDetail, switchView, continueTask, emptyState, appendArtifactList, renderTaskPlan,
  submitTaskFeedback, prepareTargets, refreshQuery, queryRemaining, probeRecovery, prepareRecovery, openPreparation, viewState, getScope, makeFeedbackRequestKey = () => crypto.randomUUID() }) {
  const views = new WeakMap();
  const detailViews = new WeakMap();
  const clocks = new Map();
  let clockTimer = null, clockScope = null;
  state.taskFeedback ||= new Map();
  const remaining = createBatchRemaining({document,state,queryRemaining,getScope,makeKey:makeFeedbackRequestKey});
  const recovery = createOaPendingRecovery({document, state, probeRecovery, prepareRecovery, getScope,
    makeRequestKey:makeFeedbackRequestKey, openTask: async taskId => {
      if (await switchView("tasks") === false) return;
      return loadTaskDetail(taskId);
    }});
  const actionable = createActionableResults({ document, state, prepareTargets, refreshQuery, getScope, viewState,
    openPreparation: openPreparation || (async taskId => {
      if (await switchView("tasks") === false) return;
      return loadTaskDetail(taskId);
    }) });
  let feedbackViewId = 0;
  const element = (tag, className = "") => {
    const node = document.createElement(tag);
    node.className = className;
    return node;
  };
  const text = (node, value) => {
    const next = String(value ?? "");
    if (node.textContent !== next) node.textContent = next;
  };
  const remainingQueryCopy = result => {
    const scopes=(result.actionableResults || []).map(group=>group.remaining).filter(Boolean);
    if(!scopes.length) return '';
    const count=field=>scopes.reduce((sum,item)=>sum+(Number.isSafeInteger(item[field]) ? item[field] : 0),0);
    return `本轮核对原批次剩余 ${count('requestedCount')} 项：仍匹配 ${count('matchedCount')} 项，当前待办未找到 ${count('missingCount')} 项，资格变化 ${count('changedCount')} 项。仅仍匹配项可重新选择；未找到不代表已成功。`;
  };
  const disclosure = (label, className) => {
    const root = element("details", className);
    const summary = element("summary");
    text(summary, label);
    const body = element("div");
    root.append(summary, body);
    return { root, summary, body, rows: new Map() };
  };
  function updateClock(node, result) {
    const copy = executionWaitMessage(result);
    node.hidden = !copy;
    text(node, copy);
    if (copy) clocks.set(node, result); else clocks.delete(node);
    if (!clocks.size && clockTimer !== null) {
      clockScope?.clearTimer(clockTimer); clockTimer = null;
    }
    if (clocks.size && clockTimer === null && getScope) {
      clockScope = getScope();
      clockScope.defer(() => { clocks.clear(); clockTimer = null; });
      clockTimer = clockScope.interval(() => {
        for (const [root, current] of clocks) {
          if (root.isConnected === false) { clocks.delete(root); continue; }
          text(root, executionWaitMessage(current));
        }
        if (!clocks.size) { clockScope.clearTimer(clockTimer); clockTimer = null; }
      }, 1000);
    }
  }
  function markBusinessProjection(result, root) {
    const execution = result.businessExecution, timing = result.businessTiming;
    if (execution?.actualPhase) markWorkspaceTiming("phase_rendered", {taskId:result.task.task_id,
      operationId:execution.operationId, requestId:execution.requestId, phase:execution.actualPhase, status:result.task.status});
    if (timing?.schemaVersion !== "agentbridge.business-timing.v1") return;
    root.dataset.businessTiming = JSON.stringify(timing);
    for (const item of timing.stages || []) {
      if (item.stage === "form_ready") markWorkspaceTiming("form_ready", {taskId:result.task.task_id, requestId:timing.requestId});
      if (item.stage === "decision_recorded") markWorkspaceTiming("decision_recorded", {taskId:result.task.task_id, operationId:timing.operationId, requestId:timing.requestId});
    }
    if (TERMINAL_TASK_STATES.has(result.task.status)) markWorkspaceTiming("result_rendered", {
      taskId:result.task.task_id, operationId:timing.operationId, requestId:timing.requestId, status:result.task.status});
  }

  const feedbackLabels = { useful: "有帮助", incorrect: "有错误", not_applicable: "不适用" };
  function feedbackModel(taskId) {
    if (!state.taskFeedback.has(taskId)) state.taskFeedback.set(taskId, {
      taskId, available: false, skill: null, rating: "", comment: "", open: false,
      pending: false, request: null, receipt: null, receiptNotice: "", error: "", views: new Set(),
    });
    return state.taskFeedback.get(taskId);
  }
  function createFeedbackView(taskId) {
    const model = feedbackModel(taskId);
    const view = disclosure("反馈业务助手", "business-disclosure task-feedback");
    const identity = element("p", "task-feedback-identity");
    const form = element("form", "task-feedback-form");
    const choices = element("fieldset", "task-feedback-choices"), legend = element("legend");
    text(legend, "这次业务助手是否有帮助？"); choices.append(legend);
    const inputs = new Map(), id = ++feedbackViewId;
    for (const [value, label] of Object.entries(feedbackLabels)) {
      const wrapper = element("label"), input = element("input"), copy = element("span");
      input.type = "radio"; input.name = `task-feedback-${id}`; input.value = value; input.required = true;
      text(copy, label); wrapper.append(input, copy); choices.append(wrapper); inputs.set(value, input);
      input.addEventListener("change", () => {
        if (input.checked && !input.disabled) { model.rating = value; model.error = ""; renderFeedbackModel(model); }
      });
    }
    const commentLabel = element("label"), commentCopy = element("span"), comment = element("textarea");
    text(commentCopy, "补充说明（可选）"); comment.rows = 3; comment.maxLength = 1000;
    comment.placeholder = "例如：哪一步有帮助，或哪里需要改进。";
    commentLabel.append(commentCopy, comment);
    comment.addEventListener("input", () => { model.comment = comment.value; model.error = ""; renderFeedbackModel(model); });
    const save = element("button", "secondary"); save.type = "submit"; text(save, "保存评价");
    const status = element("p", "task-feedback-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const receipt = element("div", "task-feedback-receipt"), receiptTitle = element("strong"), receiptComment = element("p");
    receipt.append(receiptTitle, receiptComment);
    form.append(choices, commentLabel, save); view.body.append(identity, form, status, receipt);
    Object.assign(view, {identity, form, choices, inputs, comment, save, status, receipt, receiptTitle, receiptComment});
    view.root.addEventListener("toggle", () => {
      if (view.root.open === model.open) return;
      model.open = view.root.open; renderFeedbackModel(model);
    });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (model.pending || model.receipt || !model.available || !feedbackLabels[model.rating] ||
          model.comment.length > 1000 || typeof submitTaskFeedback !== "function") return;
      model.request ||= {rating: model.rating, comment: model.comment, requestKey: makeFeedbackRequestKey()};
      model.pending = true; model.error = ""; renderFeedbackModel(model);
      try {
        const response = await submitTaskFeedback(model.taskId, {...model.request});
        if (!response?.feedback?.receipt) throw new Error("评价回执暂未返回，请重试恢复本次评价。");
        model.receipt = response.feedback.receipt; model.available = false; model.request = null;
      } catch (error) {
        if (error.feedback?.receipt) {
          model.receipt = error.feedback.receipt; model.available = false; model.request = null;
          model.receiptNotice = "此任务已有另一份评价，以下为已保存内容。";
        } else {
          model.error = error.message || "保存应答尚未确认，输入已保留，请重试恢复本次评价。";
          if ([400, 422].includes(error.status)) model.request = null;
        }
      } finally { model.pending = false; renderFeedbackModel(model); }
    });
    model.views.add(view);
    return view;
  }
  function renderFeedbackModel(model) {
    for (const view of model.views) {
      view.root.hidden = !model.available && !model.receipt;
      if (view.root.open !== model.open) view.root.open = model.open;
      const skill = model.skill;
      text(view.identity, skill ? `${skill.name} · 版本 ${skill.version} · ${skillProfileLabel(skill.profile)}` : "原任务绑定的业务助手");
      view.form.hidden = Boolean(model.receipt); view.receipt.hidden = !model.receipt;
      const locked = model.pending || Boolean(model.request) || Boolean(model.receipt) || !model.available;
      for (const [rating, input] of view.inputs) { input.checked = model.rating === rating; input.disabled = locked; }
      if (view.comment.value !== model.comment) view.comment.value = model.comment;
      view.comment.disabled = locked;
      view.save.disabled = model.pending || !model.available || !feedbackLabels[model.rating] || model.comment.length > 1000;
      text(view.save, model.pending ? "正在保存…" : model.request ? "重试保存评价" : "保存评价");
      text(view.status, model.receipt ? model.receiptNotice || "此评价已保存。" : model.error);
      view.status.className = `task-feedback-status${model.error && !model.receipt ? " error" : ""}`;
      if (model.receipt) {
        text(view.receiptTitle, `已保存评价 · ${feedbackLabels[model.receipt.rating] || "已记录"}`);
        text(view.receiptComment, model.receipt.comment || "未填写补充说明。");
      }
    }
  }
  function updateFeedback(view, result) {
    const model = feedbackModel(result.task.task_id);
    if (result.skill) model.skill = result.skill;
    model.available = Boolean(TERMINAL_TASK_STATES.has(result.task.status) && result.feedback?.available && result.skill && submitTaskFeedback);
    if (result.feedback?.receipt) {
      model.receipt = result.feedback.receipt; model.request = null; model.available = false;
    }
    renderFeedbackModel(model);
  }

  function updateFacts(view, entries) {
    const nodes = [];
    for (const [label, value] of entries.filter(([, value]) => value)) {
      let row = view.rows.get(label);
      if (!row) {
        row = [element("dt"), element("dd")];
        text(row[0], label);
        view.rows.set(label, row);
      }
      text(row[1], value);
      nodes.push(...row);
    }
    reconcileChildren(view.root, nodes);
    view.root.hidden = !nodes.length;
  }

  function createBusinessView(card, taskId) {
    const header = element("div", "application-card-header");
    const heading = element("div");
    const eyebrow = element("span", "application-card-eyebrow");
    text(eyebrow, "AGENTBRIDGE 应用卡");
    const title = element("strong", "application-card-title");
    heading.append(eyebrow, title);
    const status = element("span", "application-card-status");
    header.append(heading, status);
    const description = element("p", "application-card-copy");
    const wait = element("p", "application-card-copy business-wait");
    const facts = { root: element("dl", "application-card-facts"), rows: new Map() };
    const active = { root: element("div", "business-interactions"), rows: new Map() };
    const batch = disclosure("查看逐项结果", "business-disclosure batch-queue");
    const history = disclosure("办理记录", "business-disclosure interaction-history");
    const executionHistory = disclosure("查看历史执行观察", "business-disclosure execution-history");
    const feedback = createFeedbackView(taskId);
    const artifacts = element("div");
    const query = element("div");
    const recoveryPanel = element("section");
    const remainingPanel = element('section');
    const actions = element("div", "application-card-actions");
    const progress = element("button", "secondary");
    progress.type = "button";
    text(progress, "查看进度");
    progress.addEventListener("click", async () => {
      if (await switchView("tasks") === false) return;
      loadTaskDetail(taskId);
    });
    actions.append(progress);
    card.append(header, description, wait, facts.root, active.root, batch.root,
      history.root, executionHistory.root, artifacts, query, recoveryPanel,remainingPanel, feedback.root, actions);
    return { title, status, description, wait, facts, active, batch, history, executionHistory, artifacts, query, recoveryPanel,remainingPanel, feedback, progress };
  }

  function renderBatch(view, batch) {
    view.root.hidden = !batch;
    if (!batch) return;
    text(view.summary, `查看逐项结果（${batch.totalCount} 项）`);
    const nodes = [];
    for (const item of batch.items || []) {
      let row = view.rows.get(item.itemId);
      if (!row) {
        const root = element("div", "batch-item");
        const ordinal = element("span", "batch-item-ordinal");
        const copy = element("div", "batch-item-copy");
        const title = element("strong");
        const metadata = element("small");
        const result = element("span", "batch-item-result");
        copy.append(title, metadata);
        root.append(ordinal, copy, result);
        row = { root, ordinal, title, metadata, result };
        view.rows.set(item.itemId, row);
      }
      const current = item.ordinal === batch.currentOrdinal;
      row.root.className = `batch-item ${escapeClass(item.state)}${current ? " current" : ""}`;
      row.root.dataset.itemId = item.itemId;
      row.root.dataset.state = item.state;
      row.root.setAttribute("aria-label", `第 ${item.ordinal} 项${current ? "，当前事项" : ""}`);
      text(row.ordinal, item.ordinal);
      text(row.title, item.display?.title || `第 ${item.ordinal} 项`);
      text(row.metadata, [current ? "当前事项" : "", item.display?.sender, item.display?.date].filter(Boolean).join(" · "));
      // Per-item facts never inherit the parent task's unknown/failed state.
      text(row.result, batchItemStatusLabel(item.state));
      nodes.push(row.root);
    }
    reconcileChildren(view.body, nodes);
  }

  function renderInteractions(view, result, primaryInteractionId = null) {
    const nodes = [];
    const active = actionableTaskInteractions(result);
    for (const interaction of active) {
      let row = view.rows.get(interaction.interactionId);
      if (!row) {
        const root = element("section", "business-interaction");
        const title = element("strong");
        const status = element("span", "interaction-step-status");
        const copy = element("p");
        const action = element("a", "primary");
        action.target = "_blank";
        action.rel = "noopener noreferrer";
        root.append(title, status, copy, action);
        row = { root, title, status, copy, action };
        view.rows.set(interaction.interactionId, row);
      }
      row.root.dataset.interactionId = interaction.interactionId;
      text(row.title, interaction.title || interactionLabel(interaction.type));
      text(row.status, interactionStateLabel(interaction));
      text(row.copy, interaction.confirmation?.mode === "single" ?
        "请填写并核对本次事项，在可信页面一次确认提交；提交后将核对业务结果。" :
        interaction.message || "请在可信页面完成当前操作。");
      const url = trustedInteractionUrl(interaction.presentation?.url);
      row.action.hidden = !url || interaction.interactionId === primaryInteractionId;
      if (url) row.action.href = url;
      else row.action.removeAttribute("href");
      text(row.action, interactionActionLabel(interaction.type, interaction.display));
      nodes.push(row.root);
    }
    reconcileChildren(view.root, nodes);
    view.root.hidden = !nodes.length;
    return active;
  }

  function renderHistory(view, result, active) {
    const activeIds = new Set(active.map(item => item.interactionId));
    const recordedInputs = new Set(taskInteractions(result).filter(item => item.type === "business_input" &&
      item.state === "completed" && item.confirmation?.mode === "single" && item.confirmation.state === "recorded")
      .map(item => item.interactionId));
    const items = taskInteractions(result).filter(item => !activeIds.has(item.interactionId) &&
      !(item.type === "execution_authorization" && item.state === "completed" &&
        item.confirmation?.mode === "single" && item.confirmation.state === "recorded" &&
        recordedInputs.has(item.confirmation.sourceInteractionId)));
    text(view.summary, `办理记录（${items.length}）`);
    view.root.hidden = !items.length;
    const nodes = [];
    for (const interaction of items) {
      let row = view.rows.get(interaction.interactionId);
      if (!row) {
        const root = element("div", "interaction-record");
        const title = element("strong");
        const status = element("span", "interaction-step-status");
        const copy = element("p");
        root.append(title, status, copy);
        row = { root, title, status, copy };
        view.rows.set(interaction.interactionId, row);
      }
      text(row.title, interaction.title || interactionLabel(interaction.type));
      text(row.status, interactionStateLabel(interaction));
      const completed = completedInteractionPresentation(interaction);
      text(row.copy, completed?.message || "此记录仅供查看；请按当前事项及任务结果继续办理。");
      nodes.push(row.root);
    }
    reconcileChildren(view.body, nodes);
  }

  function renderExecutionHistory(view, result) {
    const execution = businessExecutionPresentation(result), recovered = oaRecoveryPresentation(result);
    const copy = recovered && (recovered.completed || recovered.recoveryTaskId)
      ? `首次尝试：${statusLabel(result.task.status)}。${execution?.message || ""}${execution?.submission ? ` ${execution.submission}。` : ""}`
      : execution?.historicalSubmission ? `完成核验前的提交观察：${execution.historicalSubmission}。最终结论以当前业务核验结果为准。` : "";
    view.root.hidden = !copy;
    text(view.body, copy);
  }

  function updateArtifacts(view, result, compact) {
    const signature = JSON.stringify(result.artifacts || []);
    if (signature === view.artifactSignature) return;
    view.artifactSignature = signature;
    view.artifacts.replaceChildren();
    appendArtifactList(view.artifacts, result.artifacts, {compact, taskId:result.task.task_id});
  }

  function upsertTaskCard(result, { render = true } = {}) {
    const task = result?.task;
    if (!task?.task_id) return;
    const batchId = result.batch?.batchId || task.summary?.batch?.batchId;
    const cardKey = batchId ? `${task.task_id}:batch:${batchId}` : `${task.task_id}:summary`;
    upsertTaskCardVariant(result, cardKey);
    if (render) renderChatTimeline();
    if (render) scrollChat();
  }

  function upsertTaskCardVariant(result, cardKey) {
    const task = result?.task;
    if (!task?.task_id) return false;
    const container = $("#chat-messages");
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 120;
    let card = state.taskCards.get(cardKey);
    if (!card) {
      // A just-created task can acquire a batch identity without replacing its DOM.
      const previous = [...state.taskCards].find(([, node]) => node.dataset.taskId === task.task_id);
      if (previous) {
        const [oldKey, node] = previous;
        const oldView = views.get(node);
        if (oldView && Number(task.version) < oldView.version) return false;
        card = node;
        state.taskCards.delete(oldKey);
        const meta = state.taskCardMeta.get(oldKey);
        if (meta) state.taskCardMeta.set(cardKey, meta);
        state.taskCardMeta.delete(oldKey);
      } else {
        card = element("article", "message assistant application-card");
        card.dataset.taskId = task.task_id;
      }
      state.taskCards.set(cardKey, card);
    }
    let view = views.get(card);
    if (!view) {
      view = createBusinessView(card, task.task_id);
      views.set(card, view);
    }
    if (Number.isFinite(view.version) && Number(task.version) < view.version) return false;
    const focused = document.activeElement;
    const focusWasInside = focused && card.contains(focused);
    view.version = Number(task.version);
    card.dataset.renderedAt = String(Date.now());
    card.dataset.renderedVersion = String(task.version);
    card.dataset.preparationPending = String(task.summary?.preparation?.state === "pending" && ["active","running","waiting_user"].includes(task.status));
    card.dataset.sourceUpdatedAt = task.updated_at || "";
    markWorkspaceTiming("card_rendered", {taskId:task.task_id, version:Number(task.version), status:task.status});
    markBusinessProjection(result, card);
    updateClock(view.wait, result);
    const timelineMeta = state.taskCardMeta.get(cardKey) || state.taskCardMeta.get(`${task.task_id}:summary`) || {
      createdAt: task.created_at, sequence: 0,
    };
    state.taskCardMeta.set(cardKey, timelineMeta);
    setTimelineNode(card, {key:`task:${cardKey}`, createdAt:timelineMeta.createdAt || task.created_at, order:timelineMeta.sequence || 0});
    const active = actionableTaskInteractions(result);
    const cardStatus = businessExecutionPresentation(result)?.running ? "running" : task.status || taskCardStatusForInteraction(active[0]?.state, "active");
    const queryGroup = task.summary?.workspaceQueryGroup;
    const queryTool = ["database_capabilities", "database_execute"].includes(queryGroup?.toolName);
    const legacyQuery = !queryGroup && ["独立数据库能力目录", "执行独立数据库查询与分析"].includes(task.title);
    const canGroup = (queryTool || legacyQuery) && !taskInteractions(result).length &&
      !result.artifacts?.length && !result.plan && !task.summary?.batch;
    card.dataset.queryScope = canGroup ? `${task.agent_host || ""}|${task.origin_endpoint_id || ""}|${task.active_conversation_ref || ""}` : "";
    card.dataset.queryTurn = canGroup ? queryGroup?.turnRef || "" : "";
    card.dataset.queryStatus = cardStatus;
    card.className = `message assistant application-card ${escapeClass(cardStatus)}`;
    text(view.title, displayTaskTitle(taskResultTitle(result)));
    view.status.className = `application-card-status ${escapeClass(cardStatus)}`;
    const failure = taskPlanFailurePresentation(result.plan);
    const oaQuery = task.title === "List Pending OA Workflows" || task.summary?.preparation?.kind === "oa_pending_query" ||
      Boolean(result.actionableResults?.length);
    card.dataset.oaQueryScope = oaQuery ? `${task.agent_host || ""}|${task.origin_endpoint_id || ""}|${task.active_conversation_ref || ""}` : "";
    const oldSources = result.actionableResults || [];
    card.dataset.oaArchiveable = String(oaQuery && cardStatus === "succeeded" && !active.length && oldSources.length > 0 &&
      oldSources.every(group => Date.parse(group.expiresAt) <= Date.now() && !(group.targets || []).some(target =>
        target.preparation && !["succeeded","completed","verified_complete","failed","outcome_unknown","canceled","expired","superseded","skipped"].includes(target.preparation.state))));
    const recovered = oaRecoveryPresentation(result);
    if (recovered?.completed) {
      card.className = "message assistant application-card succeeded";
      view.status.className = "application-card-status succeeded";
    }
    text(view.status, result.preparationHealth?.label || recovered?.label || failure?.label || (oaQuery && cardStatus === "succeeded" ? "已查询" : statusLabel(cardStatus)));
    const selectedCount = result.actionableResults?.reduce((sum, group) => sum + group.targets.length, 0);
    const scopedQueryCopy=remainingQueryCopy(result);
    if (oaQuery) text(view.title, scopedQueryCopy ? '原批次剩余事项查询' : "OA 待办查询");
    const execution = businessExecutionPresentation(result);
    const statusCopy = oaQuery && cardStatus === "succeeded" ?
      selectedCount ? `本次查询返回 ${selectedCount} 项；具体办理状态见各事项。` : "查询已完成，请查看本次查询结果。" : taskCardStatusMessage(cardStatus, task.summary);
    text(view.description, result.preparationHealth?.message || recovered?.message || failure?.message || scopedQueryCopy || (execution?.message ?
      task.summary?.batch ? `${statusCopy} ${execution.message}` : execution.message : statusCopy));
    const current = active[0] || result.interaction;
    updateFacts(view.facts, [["系统", current?.display?.systemName], ["影响", current?.display?.effect],
      ["办理阶段", recovered?.label || execution?.phase], ["最近核验", recovered?.observedAt ? formatTime(recovered.observedAt) : execution?.observedAt ? formatTime(execution.observedAt) : null],
      ["计划进度", result.plan ? taskPlanProgress(result.plan) : null]]);
    renderInteractions(view.active, result);
    renderBatch(view.batch, result.batch);
    renderHistory(view.history, result, active);
    renderExecutionHistory(view.executionHistory, result);
    updateArtifacts(view, result, true);
    text(view.progress, recovered?.completed || cardStatus === "succeeded" ? "查看办理结果" : cardStatus === "outcome_unknown" ? "查看核验详情" : result.artifactsHasMore ? "查看全部文件与进度" : "查看进度");
    updateFeedback(view.feedback, result);
    actionable.render(view.query, result.actionableResults);
    recovery.render(view.recoveryPanel, result);
    remaining.render(view.remainingPanel,result);
    if (focusWasInside && !card.contains(focused)) view.progress.focus({preventScroll:true});
    return nearBottom;
  }

  function renderTasks() {
    const list = $("#task-list");
    list.replaceChildren();
    if (state.tasks.length === 0) {
      list.append(emptyState("没有符合条件的任务"));
      return;
    }
    state.tasks.forEach((task) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `task-row ${task.task_id === state.selectedTaskId ? "active" : ""}`;
      button.innerHTML = `
        <span class="task-status-bar ${escapeClass(task.status)}"></span>
        <span>
          <span class="task-title"></span>
          <span class="task-meta">
            <span>${statusLabel(task.status)}</span>
            <span>${formatTime(task.updated_at)}</span>
          </span>
        </span>`;
      button.querySelector(".task-title").textContent =
        displayTaskTitle(task.title);
      button.addEventListener("click", () => loadTaskDetail(task.task_id));
      list.append(button);
    });
  }

  function renderTaskDetail(result) {
    const task = result.task;
    const detail = $("#task-detail");
    let view = detailViews.get(detail);
    if (!view || view.taskId !== task.task_id || view.heading.parentNode !== detail) {
      if (view?.feedback) state.taskFeedback.get(view.taskId)?.views.delete(view.feedback);
      if (view?.recoveryPanel) recovery.detach(view.recoveryPanel);
      if (view?.remainingPanel) remaining.detach(view.remainingPanel);
      detail.replaceChildren();
      const heading = element("div", "detail-heading");
      const back = element("button", "icon-command mobile-detail-back");
      back.type = "button";
      back.setAttribute("aria-label", "返回任务列表");
      back.title = "返回任务列表";
      text(back, "←");
      back.addEventListener("click", () => detail.classList.add("mobile-empty"));
      const copy = element("div");
      const status = element("p", "eyebrow");
      const title = element("h2");
      copy.append(status, title);
      const actions = element("div", "detail-heading-actions");
      const resume = element("button", "primary");
      resume.type = "button";
      text(resume, "查看进度");
      resume.addEventListener("click", async () => {
        if (resume.disabled) return;
        const action = view.primaryAction;
        if (action?.kind === "probe") {
          resume.disabled = true;
          try { await recovery.probe(view.taskId); }
          finally { resume.disabled = view.primaryAction?.kind === "unavailable"; }
          return;
        }
        if (action?.kind === "open_task") {
          if (await switchView("tasks") === false) return;
          return loadTaskDetail(action.taskId);
        }
        if (!["progress", "result"].includes(action?.kind)) return;
        const target = action.kind === "progress" && !view.active.root.hidden ? view.active.root : view.outcomeNotice;
        target.setAttribute("tabindex", "-1");
        target.scrollIntoView?.({block:"nearest"}); target.focus({preventScroll:true});
      });
      const resumeLink = element("a", "primary");
      resumeLink.target = "_blank"; resumeLink.rel = "noopener noreferrer";
      resumeLink.addEventListener("click", () => markWorkspaceTiming("form_opened", {taskId:view.taskId}));
      const queryAction = element("button", "secondary"); queryAction.type = "button";
      text(queryAction, "只读查询 OA 待办");
      queryAction.addEventListener("click", async () => {
        if (queryAction.disabled) return;
        if (!view.readOnlyQuery || typeof refreshQuery !== "function") return;
        view.queryPending = true;
        queryAction.disabled = true;
        view.queryRequestKey ||= makeFeedbackRequestKey();
        text(view.outcomeNotice, "正在发起只读查询，本次不会重新提交原事项。");
        try {
          await refreshQuery({requestKey: view.queryRequestKey});
          view.queryRequestKey = null;
          text(view.outcomeNotice, "已发起只读查询。原提交结果仍待核对；查询待办不等于确认办理成功。请查看最新查询卡。");
          if (typeof switchView === "function") await switchView("chat");
        } catch {
          text(view.outcomeNotice, "只读查询的受理状态尚未确认，请重试恢复本次查询；不要重新提交原事项。");
        } finally {
          view.queryPending = false;
          queryAction.disabled = !view.readOnlyQuery;
        }
      });
      actions.append(resume, resumeLink, queryAction);
      heading.append(back, copy, actions);
      const facts = {root:element("dl", "detail-grid"), rows:new Map()};
      const outcomeNotice = element("p", "application-card-copy");
      outcomeNotice.setAttribute("role", "status");
      const wait = element("p", "application-card-copy business-wait");
      const plan = element("div");
      const active = {root:element("div", "business-interactions"), rows:new Map()};
      const batch = disclosure("查看逐项结果", "business-disclosure batch-queue");
      const history = disclosure("办理记录", "business-disclosure interaction-history");
      const executionHistory = disclosure("查看历史执行观察", "business-disclosure execution-history");
      const artifacts = element("div");
      const query = element("div");
      const recoveryPanel = element("section");
      const remainingPanel = element('section');
      const feedback = createFeedbackView(task.task_id);
      const timeline = {root:element("div", "timeline"), rows:new Map()};
      detail.append(heading, facts.root, outcomeNotice, wait, plan, active.root, batch.root, history.root, executionHistory.root, artifacts, query, recoveryPanel,remainingPanel, feedback.root, timeline.root);
      view = {taskId:task.task_id, heading, title, status, resume, resumeLink, queryAction, facts, outcomeNotice, wait, plan, active, batch, history, executionHistory, artifacts, query, recoveryPanel,remainingPanel, feedback, timeline};
      detailViews.set(detail, view);
      detail.dataset.taskId = task.task_id;
    }
    if (Number.isFinite(view.version) && Number(task.version) < view.version) return;
    const focused = document.activeElement;
    const focusWasInside = focused && detail.contains(focused);
    view.version = Number(task.version);
    view.task = task;
    markBusinessProjection(result, detail);
    updateClock(view.wait, result);
    const execution = businessExecutionPresentation(result);
    const recovered = oaRecoveryPresentation(result);
    const primaryAction = taskPrimaryAction(result);
    view.primaryAction = primaryAction;
    text(view.title, displayTaskTitle(taskResultTitle(result)));
    text(view.status, result.preparationHealth?.label || recovered?.label || statusLabel(execution?.running ? "running" : task.status));
    view.readOnlyQuery = execution?.readOnlyQuery === true;
    view.resume.hidden = primaryAction.kind === "form";
    text(view.resume, primaryAction.label);
    view.resume.disabled = primaryAction.kind === "unavailable" || primaryAction.kind === "probe" && typeof probeRecovery !== "function";
    view.resumeLink.hidden = primaryAction.kind !== "form";
    text(view.resumeLink, primaryAction.kind === "form" ? primaryAction.label : "");
    if (primaryAction.kind === "form") view.resumeLink.href = primaryAction.url;
    else view.resumeLink.removeAttribute("href");
    view.queryAction.hidden = !view.readOnlyQuery || Boolean(recovered?.completed);
    view.queryAction.disabled = Boolean(view.queryPending) || !view.readOnlyQuery || typeof refreshQuery !== "function";
    if (!view.queryRequestKey) text(view.outcomeNotice, result.preparationHealth?.message || recovered?.message || remainingQueryCopy(result) || execution?.message || taskCardStatusMessage(task.status, task.summary));
    updateFacts(view.facts, [["来源", task.agent_host], ["创建时间", formatTime(task.created_at)],
      ["更新时间", formatTime(task.updated_at)], ["业务助手", result.skill
        ? `${result.skill.name} · ${skillProfileLabel(result.skill.profile)} · ${result.skill.version}${result.skill.revoked ? "（已撤销）" : ""}` : "未关联 Skill"],
      ["任务编号", task.task_id], ["办理阶段", recovered?.label || execution?.phase], ["提交观察", recovered?.completed || recovered?.recoveryTaskId ? null : execution?.submission],
      ["最近核验", recovered?.observedAt ? formatTime(recovered.observedAt) : execution?.observedAt ? formatTime(execution.observedAt) : null],
      ["状态记录更新", execution?.updatedAt ? formatTime(execution.updatedAt) : null],
      ["批量结果", task.summary?.batch ? taskCardStatusMessage(task.status, task.summary) : null]]);
    const planSignature = JSON.stringify(result.plan || null);
    if (planSignature !== view.planSignature) {
      view.planSignature = planSignature;
      view.plan.replaceChildren();
      if (result.plan) view.plan.append(renderTaskPlan(result.plan));
    }
    const active = renderInteractions(view.active, result, primaryAction.kind === "form" ? primaryAction.interactionId : null);
    renderBatch(view.batch, result.batch);
    renderHistory(view.history, result, active);
    renderExecutionHistory(view.executionHistory, result);
    updateArtifacts(view, result, false);
    updateFeedback(view.feedback, result);
    actionable.render(view.query, result.actionableResults);
    recovery.render(view.recoveryPanel, result);
    remaining.render(view.remainingPanel,result);
    const nodes = [];
    for (const event of [...(result.events || [])].reverse()) {
      const key = event.event_id || `${event.event_type}:${event.created_at}`;
      let row = view.timeline.rows.get(key);
      if (!row) {
        const root = element("div", "timeline-item");
        const dot = element("span", "timeline-dot");
        const copy = element("div", "timeline-copy");
        const title = element("strong");
        const time = element("time");
        copy.append(title, time);
        root.append(dot, copy);
        row = {root, title, time};
        view.timeline.rows.set(key, row);
      }
      text(row.title, eventLabel(event.event_type));
      text(row.time, formatTime(event.created_at));
      nodes.push(row.root);
    }
    reconcileChildren(view.timeline.root, nodes);
    if (focusWasInside && !detail.contains(focused)) view.resume.focus({preventScroll:true});
  }

  function renderSkillCard(event) {
    const card = document.createElement("article");
    card.className = "skill-activity-card";
    const heading = document.createElement("strong");
    heading.textContent = `业务助手 · ${event.name}`;
    const status = document.createElement("span");
    status.className = "skill-activity-status";
    status.textContent = event.status === "succeeded" ? "已加载" : "加载失败";
    const description = document.createElement("p");
    description.textContent = event.status === "succeeded"
      ? `${skillProfileLabel(event.profile)} · 版本 ${event.version}。${event.loaded_resources?.length ? `主说明及必读资料已加载（${event.loaded_resources.length} 个文件）` : "已加载处理规则"}，业务执行结果请查看后续任务。`
      : (event.message || "助手未能加载，本次不能视为已使用该助手。");
    const time = document.createElement("small");
    time.textContent = formatTime(event.created_at);
    card.append(heading, status, description, time);
    setTimelineNode(card, {key: `skill:${event.event_id}`, createdAt: event.created_at});
    return card;
  }

  function addDetail(list, term, value) {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = term;
    dd.textContent = value || "—";
    list.append(dt, dd);
  }

  return { upsertTaskCard, upsertTaskCardVariant, renderTasks, renderTaskDetail, renderSkillCard, addDetail,
    resetTaskCardControllers: () => {
      recovery.reset();remaining.reset();actionable.reset();clocks.clear();
      if (clockTimer !== null) clockScope?.clearTimer(clockTimer);
      clockTimer = null;
    } };
}
