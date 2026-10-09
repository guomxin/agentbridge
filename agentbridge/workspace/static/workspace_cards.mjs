import { completedInteractionPresentation, taskCardStatusForInteraction,
  taskCardStatusMessage, displayTaskTitle, skillProfileLabel, taskPlanProgress,
  taskPlanFailurePresentation, interactionLabel, interactionActionLabel,
  escapeClass, formatTime, statusLabel, eventLabel } from "./workspace_results.mjs";

export function createTaskCards({ document, state, $, renderChatTimeline, scrollChat, setTimelineNode, loadTaskDetail, switchView, continueTask, emptyState, appendArtifactList, renderTaskPlan }) {
  function upsertTaskCard(result, { render = true } = {}) {
    const task = result?.task;
    if (!task?.task_id) return;
    const interactions = Array.isArray(result.interactions) &&
      result.interactions.length
      ? result.interactions
      : result.interaction
        ? [result.interaction]
        : [];
    const variants = interactions.length
      ? interactions.map((interaction) => ({
          cardKey: `${task.task_id}:interaction:${interaction.interactionId}`,
          interaction,
        }))
      : [{ cardKey: `${task.task_id}:summary`, interaction: null }];
    const desiredKeys = new Set(variants.map((variant) => variant.cardKey));
    let nearBottom = false;
    variants.forEach((variant, index) => {
      nearBottom = upsertTaskCardVariant(
        result,
        variant.cardKey,
        variant.interaction,
        { showArtifacts: index === variants.length - 1 },
      ) || nearBottom;
    });
    state.taskCards.forEach((card, cardKey) => {
      if (
        card.dataset.taskId === task.task_id &&
        !desiredKeys.has(cardKey)
      ) {
        card.remove();
        state.taskCards.delete(cardKey);
        state.taskCardMeta.delete(cardKey);
      }
    });
    if (render) renderChatTimeline();
    if (nearBottom) scrollChat();
  }

  function upsertTaskCardVariant(
    result,
    cardKey,
    interaction,
    { showArtifacts = true } = {},
  ) {
    const task = result?.task;
    if (!task?.task_id) return;
    const container = $("#chat-messages");
    const nearBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight < 120;
    let card = state.taskCards.get(cardKey);
    if (!card) {
      card = document.createElement("article");
      card.className = "message assistant application-card";
      card.dataset.taskId = task.task_id;
      card.dataset.interactionId = interaction?.interactionId || "";
      state.taskCards.set(cardKey, card);
    }
    const timelineMeta = state.taskCardMeta.get(cardKey) || {
      createdAt: interaction?.linkedAt || task.created_at,
      sequence: 0,
    };
    state.taskCardMeta.set(cardKey, timelineMeta);
    setTimelineNode(card, {
      key: `task:${cardKey}`,
      createdAt:
        timelineMeta.createdAt || interaction?.linkedAt || task.created_at,
      order: timelineMeta.sequence || 0,
    });
    const cardStatus = taskCardStatusForInteraction(
      interaction?.state,
      task.status,
    );
    // Keep independent backend tasks; only group read-only database presentation.
    // Older records have no turn metadata, so the visible user-message boundary
    // is a conservative fallback. Never fold approvals, artifacts or business plans.
    const queryGroup = task.summary?.workspaceQueryGroup;
    const queryTool = ["database_capabilities", "database_execute"].includes(queryGroup?.toolName);
    const legacyQuery = !queryGroup && ["独立数据库能力目录", "执行独立数据库查询与分析"].includes(task.title);
    const canGroup = (queryTool || legacyQuery) && !interaction &&
      !result.artifacts?.length && !result.plan && !task.summary?.batch;
    card.dataset.queryScope = canGroup
      ? `${task.agent_host || ""}|${task.origin_endpoint_id || ""}|${task.active_conversation_ref || ""}`
      : "";
    card.dataset.queryTurn = canGroup ? queryGroup?.turnRef || "" : "";
    card.dataset.queryStatus = cardStatus;
    card.className =
      `message assistant application-card ${escapeClass(cardStatus)}`;
    card.replaceChildren();

    const header = document.createElement("div");
    header.className = "application-card-header";
    const heading = document.createElement("div");
    const eyebrow = document.createElement("span");
    eyebrow.className = "application-card-eyebrow";
    eyebrow.textContent = "AGENTBRIDGE 应用卡";
    const title = document.createElement("strong");
    title.className = "application-card-title";
    title.textContent = interaction?.title || displayTaskTitle(task.title);
    heading.append(eyebrow, title);
    const status = document.createElement("span");
    status.className =
      `application-card-status ${escapeClass(cardStatus)}`;
    const completedInteraction = completedInteractionPresentation(interaction);
    const planFailure = taskPlanFailurePresentation(result.plan);
    status.textContent = planFailure?.label || completedInteraction?.label || statusLabel(cardStatus);
    header.append(heading, status);
    card.append(header);

    const description = document.createElement("p");
    description.className = "application-card-copy";
    const interactionActive =
      interaction &&
      ["pending", "processing"].includes(interaction.state);
    description.textContent = planFailure?.message || completedInteraction?.message || (
      interactionActive
        ? interaction.message || taskCardStatusMessage(cardStatus, task.summary)
        : taskCardStatusMessage(cardStatus, task.summary)
    );
    card.append(description);

    const facts = document.createElement("dl");
    facts.className = "application-card-facts";
    const systemName = interaction?.display?.systemName;
    const effect = interaction?.display?.effect;
    if (systemName) addDetail(facts, "系统", systemName);
    if (effect) addDetail(facts, "影响", effect);
    const latestEvent = result.events?.at(-1);
    if (latestEvent) {
      addDetail(
        facts,
        "最新进展",
        `${eventLabel(latestEvent.event_type)} · ${formatTime(latestEvent.created_at)}`,
      );
    }
    addDetail(facts, "业务助手", result.skill
      ? `${result.skill.name} · ${skillProfileLabel(result.skill.profile)} · ${result.skill.version}${result.skill.revoked ? "（已撤销）" : ""}`
      : "未关联 Skill");
    if (result.plan) {
      addDetail(facts, "计划进度", taskPlanProgress(result.plan));
    }
    if (task.summary?.batch) {
      addDetail(facts, "批量进度", taskCardStatusMessage(cardStatus, task.summary));
    }
    if (facts.childElementCount) card.append(facts);
    if (showArtifacts) {
      appendArtifactList(card, result.artifacts, {
        compact: true,
        taskId: task.task_id,
      });
    }

    const actions = document.createElement("div");
    actions.className = "application-card-actions";
    const url = interaction?.presentation?.url;
    if (
      interactionActive &&
      typeof url === "string" &&
      /^https:\/\//.test(url)
    ) {
      const action = document.createElement("a");
      action.className = "primary";
      action.href = url;
      action.target = "_blank";
      action.rel = "noopener";
      action.textContent = interactionActionLabel(interaction.type);
      actions.append(action);
    }
    const progress = document.createElement("button");
    progress.type = "button";
    progress.className = "secondary";
    progress.textContent = "查看进度";
    progress.addEventListener("click", () => {
      switchView("tasks");
      loadTaskDetail(task.task_id);
    });
    actions.append(progress);
    card.append(actions);
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
    detail.replaceChildren();

    const heading = document.createElement("div");
    heading.className = "detail-heading";
    const back = document.createElement("button");
    back.type = "button";
    back.className = "icon-command mobile-detail-back";
    back.setAttribute("aria-label", "返回任务列表");
    back.title = "返回任务列表";
    back.textContent = "←";
    back.addEventListener("click", () => {
      detail.classList.add("mobile-empty");
    });
    const headingCopy = document.createElement("div");
    const eyebrow = document.createElement("p");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = statusLabel(task.status);
    const title = document.createElement("h2");
    title.textContent = displayTaskTitle(task.title);
    headingCopy.append(eyebrow, title);
    const actions = document.createElement("div");
    actions.className = "detail-heading-actions";
    const continueButton = document.createElement("button");
    continueButton.type = "button";
    continueButton.className = "primary";
    continueButton.textContent = "继续任务";
    continueButton.addEventListener("click", () =>
      continueTask(task, continueButton),
    );
    actions.append(continueButton);
    heading.append(back, headingCopy, actions);
    detail.append(heading);

    const metadata = document.createElement("dl");
    metadata.className = "detail-grid";
    addDetail(metadata, "来源", task.agent_host);
    addDetail(metadata, "创建时间", formatTime(task.created_at));
    addDetail(metadata, "更新时间", formatTime(task.updated_at));
    addDetail(metadata, "业务助手", result.skill
      ? `${result.skill.name} · ${skillProfileLabel(result.skill.profile)} · ${result.skill.version}${result.skill.revoked ? "（已撤销）" : ""}`
      : "未关联 Skill");
    addDetail(metadata, "任务编号", task.task_id);
    detail.append(metadata);

    if (result.plan) {
      detail.append(renderTaskPlan(result.plan));
    }

    const interactions = Array.isArray(result.interactions)
      ? result.interactions
      : result.interaction
        ? [result.interaction]
        : [];
    interactions
      .filter((interaction) => {
        const url = interaction?.presentation?.url;
        return (
          ["pending", "processing"].includes(interaction?.state) &&
          typeof url === "string" &&
          /^https:\/\//.test(url)
        );
      })
      .forEach((interaction) => {
        const url = interaction.presentation.url;
        const band = document.createElement("div");
        band.className = "interaction-band";
        const label = document.createElement("span");
        label.textContent = interaction.title || interactionLabel(interaction.type);
        const link = document.createElement("a");
        link.className = "primary";
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = "打开";
        band.append(label, link);
        detail.append(band);
      });

    appendArtifactList(detail, result.artifacts, { taskId: task.task_id });

    const timeline = document.createElement("div");
    timeline.className = "timeline";
    [...result.events].reverse().forEach((event) => {
      const item = document.createElement("div");
      item.className = "timeline-item";
      const dot = document.createElement("span");
      dot.className = "timeline-dot";
      const copy = document.createElement("div");
      copy.className = "timeline-copy";
      const eventTitle = document.createElement("strong");
      eventTitle.textContent = eventLabel(event.event_type);
      const time = document.createElement("time");
      time.textContent = formatTime(event.created_at);
      copy.append(eventTitle, time);
      item.append(dot, copy);
      timeline.append(item);
    });
    detail.append(timeline);
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

  return { upsertTaskCard, upsertTaskCardVariant, renderTasks, renderTaskDetail, renderSkillCard, addDetail };
}
