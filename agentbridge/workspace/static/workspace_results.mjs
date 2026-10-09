// Pure projections shared by timeline cards and task details.
export function completedInteractionPresentation(interaction) {
  if (interaction?.state !== "completed") return null;
  if (interaction.type === "business_input") {
    return {
      label: "字段已提交",
      message: "字段核对已完成，不代表业务已提交；请查看后续授权卡和任务进度。",
    };
  }
  if (interaction.type === "credential") {
    return {
      label: "认证已完成",
      message: "认证步骤已完成，原任务结果请查看后续消息和任务进度。",
    };
  }
  return null;
}

export function taskCardStatusForInteraction(state, fallback) {
  if (fallback === "canceled") return "canceled";
  return (
    {
      pending: "waiting_user",
      processing: "running",
      completed: "succeeded",
      declined: "canceled",
      expired: "expired",
      failed: "failed",
      superseded: "superseded",
    }[state] || fallback
  );
}

export function taskCardStatusMessage(status, summary = null) {
  const batch = summary?.batch;
  if (batch && Number.isInteger(batch.totalCount) && batch.totalCount > 0) {
    const done = Number(batch.succeededCount) || 0;
    const failed = Number(batch.failedCount) || 0;
    const remaining = Math.max(0, batch.totalCount - done - failed - (Number(batch.skippedCount) || 0));
    const progress = `已完成 ${done}/${batch.totalCount} 条，剩余 ${remaining} 条。`;
    if (batch.state === "succeeded") return `批量事项全部完成，共 ${batch.totalCount} 条。`;
    if (["running", "waiting_user"].includes(batch.state)) return `${progress}正在处理第 ${batch.currentOrdinal} 条。`;
    return `${progress}批次已停止，请核对当前事项的结果或失败原因。`;
  }
  const deliveryMessage = taskCardArtifactDeliveryMessage(summary);
  if (deliveryMessage) return deliveryMessage;
  return (
    {
      active: "任务已创建，等待智能体继续处理。",
      waiting_user: "任务正在等待你的填写或确认。",
      running: "智能体正在执行已确认的操作。",
      succeeded: "任务已经完成。",
      partially_succeeded: "部分事项已完成，批次已停止，请查看进度。",
      failed: "任务未能完成，请查看进度了解原因。",
      outcome_unknown: "最终结果未能确认，请先到业务系统核对。",
      canceled: "任务已取消。",
      expired: "任务交互已过期，请重新发起。",
      superseded: "任务已被更新的可信交互替换。",
    }[status] || "任务状态已更新。"
  );
}

export function taskCardArtifactDeliveryMessage(summary) {
  const aggregate = summary?.artifactDeliveryAggregate;
  if (
    aggregate?.completionMeaning === "cross_endpoint_delivery_reported" &&
    String(aggregate.userMessage || "").trim()
  ) {
    return String(aggregate.userMessage).trim();
  }
  const delivery = summary?.artifactDelivery;
  if (!delivery || typeof delivery !== "object") return null;
  if (
    delivery.completionMeaning !== "endpoint_delivery_reported" ||
    !Number.isInteger(delivery.preparedCount)
  ) {
    return null;
  }
  const message = String(delivery.userMessage || "").trim();
  if (message) return message;
  const prepared = Math.max(0, delivery.preparedCount || 0);
  const attached = Math.max(0, delivery.attachmentSentCount || 0);
  const fallback = Math.max(0, delivery.fallbackLinkSentCount || 0);
  const failed = Math.max(0, delivery.failedCount || 0);
  const parts = [
    `${prepared} 份文件已准备`,
    `${attached} 份已作为附件发送`,
  ];
  if (fallback) parts.push(`${fallback} 份已改发下载链接`);
  if (failed) parts.push(`${failed} 份未能送达`);
  return `${parts.join("，")}。`;
}

export function displayTaskTitle(value) {
  const title = String(value || "").trim();
  return (
    {
      "Prepare OA Efficiency-Data Approval": "OA 效能数据审批",
      "Prepare OA Travel-Expense Approval": "OA 差旅费审批",
      "Prepare OA Labor-Contract Renewal Approval": "OA 劳动合同续签审批",
      "Prepare OA Intellectual-Property Declaration Approval": "OA 知识产权申报审批",
      "Prepare OA Weekly-Report Acknowledgement": "OA 周报阅办",
      "Prepare OA Standard-Collaboration Approval": "OA 普通事项审批",
      "Prepare OA Workflow Revoke": "OA 流程撤销",
      "Prepare OA Business Trip Draft": "OA 出差申请草稿",
      "Prepare OA Business Trip Submission": "OA 出差申请提交",
      "Prepare OA Leave Draft": "OA 请假申请草稿",
      "Prepare OA Leave Submission": "OA 请假申请提交",
      "Prepare OA Missed-Punch Draft": "OA 补签申请草稿",
      "Prepare OA Missed-Punch Approval": "OA 补签申请审批",
      "Prepare OA Meeting Creation": "OA 会议创建",
      "Prepare and Deliver One OA Certificate Scan": "OA 证书文件交付",
      "Prepare and Deliver OA Certificate Scans": "OA 证书文件批量交付",
      "Search OA Certificate Scans": "OA 证书查询与下载",
      "导出照明系统 CSV 报告": "照明系统报告导出",
      "Prepare Taihua Work Log": "工作日志提交",
    }[title] ||
    title ||
    "AgentBridge 任务"
  );
}

export function skillProfileLabel(profile) {
  return ({review: "总结与复核", track: "进展追踪", search: "相似案例检索",
    preview: "草稿预览", fill: "准备填写", single: "单项办理", batch: "批量办理"})[profile] || profile || "";
}

export function taskPlanProgress(plan) {
  const steps = Array.isArray(plan?.steps) ? plan.steps : [];
  const completed = steps.filter((step) =>
    ["succeeded", "skipped"].includes(step.state),
  ).length;
  const current = steps.find((step) => step.stepKey === plan.currentStepKey);
  const suffix = current ? ` · ${current.title || current.stepKey}` : "";
  return `${completed}/${steps.length} 步${suffix}`;
}

export function taskPlanFailurePresentation(plan) {
  if (plan?.terminalReason !== "PLAN_SOURCE_INCOMPLETE") return null;
  const sources = plan.resultProjection?.result?.source_summaries || [];
  const incomplete = sources.filter(source => source.status !== "complete");
  const names = { done: "OA 已办", sent: "OA 已发" };
  const detail = incomplete.map(source => {
    const coverage = source.coverage || {};
    const total = Number.isInteger(coverage.sourceQueryTotal)
      ? ` / 筛选命中 ${coverage.sourceQueryTotal} 条` : "";
    return `${names[source.collection] || source.collection}：已读 ${source.scanned_count ?? 0} 条${total}`;
  }).join("；");
  return {
    label: "已安全停止",
    message: `${detail ? `${detail}。` : ""}来源未完整覆盖请求范围，未进入业务写入。`,
  };
}

export function formatBytes(value) {
  const size = Number(value || 0);
  if (!Number.isFinite(size) || size <= 0) return "未知大小";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function interactionLabel(type) {
  return (
    {
      credential: "需要完成安全登录",
      business_input: "需要补充业务信息",
      execution_authorization: "需要核对并确认",
    }[type] || "需要用户处理"
  );
}

export function interactionActionLabel(type) {
  return (
    {
      credential: "安全登录",
      business_input: "填写信息",
      execution_authorization: "核对并确认",
    }[type] || "继续处理"
  );
}

export function endpointType(type) {
  return (
    {
      telegram: "Telegram",
      "openclaw-weixin": "微信",
      web: "网页",
    }[type] || type
  );
}

export function escapeClass(value) {
  return String(value || "").replace(/[^a-z0-9_-]/gi, "");
}

export function formatTime(value) {
  if (!value) return "—";
  const parsed = parseTimestampMilliseconds(value);
  if (!Number.isFinite(parsed)) return String(value);
  const date = new Date(parsed);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function parseTimestampMilliseconds(value) {
  const text = String(value ?? "").trim();
  if (!text) return Number.NaN;
  if (/^\d{10,13}$/.test(text)) {
    const numeric = Number(text);
    return numeric < 100_000_000_000 ? numeric * 1_000 : numeric;
  }
  return Date.parse(text);
}

export function statusLabel(status) {
  return (
    {
      active: "进行中",
      validated: "已校验",
      queued: "等待执行",
      pending: "待处理",
      waiting_user: "等待确认",
      running: "执行中",
      succeeded: "已完成",
      skipped: "已跳过",
      partially_succeeded: "部分成功",
      failed: "失败",
      outcome_unknown: "结果待核对",
      canceled: "已取消",
      expired: "已过期",
      superseded: "已被替换",
    }[status] || status
  );
}

export function eventLabel(type) {
  return (
    {
      "task.created": "任务已创建",
      "task.operation.linked": "操作已关联",
      "task.operation.requires_user_action": "操作等待用户处理",
      "task.interaction.waiting": "等待用户处理",
      "task.interaction.completed": "可信交互已完成",
      "task.interaction.expired": "可信交互已过期",
      "task.interaction.failed": "可信交互失败",
      "task.interaction.superseded": "可信交互已更新",
      "task.operation.running": "操作执行中",
      "task.analysis.progress": "正在核验并分析本人日报",
      "task.operation.succeeded": "操作成功",
      "task.operation.failed": "操作失败",
      "task.failed": "任务失败",
      "task.operation.outcome_unknown": "操作结果待核对",
      "task.canceled": "任务已取消",
      "batch.created": "批量任务已创建",
      "batch.candidates.frozen": "候选事项已冻结",
      "batch.item.started": "开始处理当前事项",
      "batch.item.succeeded": "当前事项已完成",
      "batch.item.failed": "当前事项失败",
      "batch.item.outcome_unknown": "当前事项结果待核对",
      "batch.completed": "批量任务已结束",
      "plan.proposed": "跨系统计划已提出",
      "plan.validated": "跨系统计划已校验",
      "plan.started": "跨系统计划已启动",
      "plan.step.started": "计划步骤已开始",
      "plan.step.succeeded": "计划步骤已完成",
      "plan.result.ready": "组合任务结果已生成",
      "plan.step.resumed": "计划步骤已恢复",
      "plan.step.recovered": "重启后已恢复计划步骤",
      "plan.step.waiting": "计划正在等待用户",
      "plan.authorization.waiting": "计划正在等待执行授权",
      "plan.step.failed": "计划步骤失败",
      "plan.outcome_unknown": "计划结果待核对",
      "plan.completed": "跨系统计划已完成",
      "plan.canceled": "跨系统计划已取消",
      "task.artifact.ready": "任务文件已就绪",
      "task.artifact.delivery": "文件投递结果已回报",
      "task.artifact.refreshed": "文件下载已重新生成",
      "task.completed": "任务已完成",
    }[type] || type.replaceAll(".", " / ")
  );
}

export function textHash(value) {
  let hash = 2166136261;
  for (const character of String(value || "")) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16);
}

export function historyMessageKey(message, index) {
  if (message.id) return `history:${message.id}`;
  return [
    "history",
    message.role,
    message.timestamp || index,
    textHash(message.text),
  ].join(":");
}

export function createResultView({ document, state, reissueArtifact }) {
  function renderMarkdown(element, value) {
    const source = String(value ?? "");
    // Keep content readable if a static dependency failed to load.
    element.textContent = source;
    element.classList.remove("markdown-body");
    if (!globalThis.marked?.parse || !globalThis.DOMPurify?.sanitize) return;
    try {
      const html = globalThis.marked.parse(source, { gfm: true, breaks: true, async: false });
      element.innerHTML = globalThis.DOMPurify.sanitize(html, {
        ALLOWED_TAGS: ["p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
          "strong", "em", "del", "blockquote", "ul", "ol", "li", "pre", "code",
          "a", "table", "thead", "tbody", "tr", "th", "td", "input"],
        ALLOWED_ATTR: ["href", "title", "start", "align", "type", "checked", "disabled"],
        ALLOW_DATA_ATTR: false,
        ALLOW_ARIA_ATTR: false,
      });
      element.classList.add("markdown-body");
      element.querySelectorAll("a").forEach((link) => {
        const href = link.getAttribute("href");
        // Do not turn generated links into local API actions or custom protocols.
        if (!href || !/^(https?:\/\/|mailto:)/i.test(href)) {
          link.removeAttribute("href");
        } else {
          link.target = "_blank";
          link.rel = "noopener noreferrer";
        }
      });
      element.querySelectorAll("input").forEach((input) => {
        if (input.type !== "checkbox") input.remove();
        else input.disabled = true;
      });
      element.querySelectorAll("table").forEach((table) => {
        const wrapper = document.createElement("div");
        wrapper.className = "markdown-table-scroll";
        wrapper.tabIndex = 0;
        wrapper.setAttribute("role", "region");
        wrapper.setAttribute("aria-label", "结果表格，可横向滚动");
        table.replaceWith(wrapper);
        wrapper.append(table);
      });
    } catch {
      element.classList.remove("markdown-body");
      element.textContent = source;
    }
  }

  function groupQueryCards(nodes) {
    const output = [];
    const groups = new Map();
    let userBoundary = "";
    for (const node of nodes) {
      if (node.dataset.messageRole === "user") {
        userBoundary = node.dataset.timelineKey;
      }
      const scope = node.dataset.queryScope;
      const turn = node.dataset.queryTurn || (userBoundary && `legacy:${userBoundary}`);
      if (!scope || !turn) {
        output.push(node);
        continue;
      }
      const key = JSON.stringify([scope, turn]);
      let group = groups.get(key);
      if (!group) {
        group = { key, cards: [], element: document.createElement("article") };
        groups.set(key, group);
        output.push(group.element);
      }
      group.cards.push(node);
    }
    for (const { key, cards, element } of groups.values()) {
      element.className = "message assistant application-card query-group";
      const header = document.createElement("div");
      header.className = "application-card-header";
      const title = document.createElement("strong");
      title.className = "application-card-title";
      title.textContent = "数据库查询过程";
      const status = document.createElement("span");
      status.className = "application-card-status";
      const states = cards.map(card => card.dataset.queryStatus);
      const pending = states.filter(value => ["active", "running", "waiting_user"].includes(value)).length;
      const failed = states.filter(value => !["succeeded", "active", "running", "waiting_user"].includes(value)).length;
      status.textContent = failed ? `${failed} 个步骤需关注` : pending ? "查询中" : "查询步骤已结束";
      if (failed) status.classList.add("failed");
      header.append(title, status);
      const details = document.createElement("details");
      details.className = "query-group-details";
      details.open = state.queryGroupOpen.get(key) === true;
      details.addEventListener("toggle", () => state.queryGroupOpen.set(key, details.open));
      const summary = document.createElement("summary");
      summary.textContent = `查看查询步骤（${cards.length}）`;
      details.append(summary, ...cards);
      element.append(header);
      if (failed) {
        const warning = document.createElement("p");
        warning.className = "application-card-copy";
        warning.textContent = "部分步骤失败、取消或结果待核对，请展开查看具体状态。";
        element.append(warning);
      }
      element.append(details);
    }
    return output;
  }

  function renderTaskPlan(plan) {
    const section = document.createElement("section");
    section.className = "task-plan";
    const header = document.createElement("div");
    header.className = "task-plan-header";
    const title = document.createElement("strong");
    title.textContent = "跨系统任务计划";
    const progress = document.createElement("span");
    progress.textContent = taskPlanProgress(plan);
    header.append(title, progress);
    section.append(header);
    const failure = taskPlanFailurePresentation(plan);
    if (failure) {
      const notice = document.createElement("p");
      notice.className = "task-plan-result";
      notice.textContent = `${failure.label}：${failure.message}`;
      section.append(notice);
    }

    const steps = document.createElement("ol");
    steps.className = "task-plan-steps";
    (plan.steps || []).forEach((step) => {
      const item = document.createElement("li");
      item.className = `task-plan-step ${escapeClass(step.state)}`;
      const marker = document.createElement("span");
      marker.className = "task-plan-marker";
      marker.textContent = String(Number(step.ordinal || 0));
      const copy = document.createElement("span");
      const name = document.createElement("strong");
      name.textContent = step.title || step.capabilityName || step.transformName;
      const meta = document.createElement("small");
      meta.textContent = `${step.systemId} · ${statusLabel(step.state)}`;
      copy.append(name, meta);
      item.append(marker, copy);
      steps.append(item);
    });
    section.append(steps);
    if (plan.resultProjection) {
      section.append(renderTaskPlanResult(plan.resultProjection));
    }
    return section;
  }

  function renderTaskPlanResult(projection) {
    const result = projection?.result || {};
    const panel = document.createElement("div");
    panel.className = "task-plan-result";
    const heading = document.createElement("strong");
    heading.textContent = projection?.kind === "private_draft"
      ? "可核对草稿"
      : "来源核对结果";
    panel.append(heading);
    if (typeof result.draft === "string" && result.draft.trim()) {
      const draft = document.createElement("div");
      renderMarkdown(draft, result.draft.trim());
      panel.append(draft);
    }
    const summary = document.createElement("p");
    const included = Number(result.included_count ?? result.item_count ?? 0);
    const excluded = Number(result.excluded_count ?? result.duplicate_count ?? 0);
    const coverage = result.coverage?.status || "unknown";
    summary.textContent = `采用 ${included} 项 · 排除 ${excluded} 项 · 来源${coverage === "complete" ? "完整" : "不完整"}`;
    panel.append(summary);
    return panel;
  }

  function appendArtifactList(
    container,
    artifacts,
    { compact = false, taskId = null } = {},
  ) {
    const items = Array.isArray(artifacts) ? artifacts : [];
    if (!items.length) return;
    const section = document.createElement("section");
    section.className = `task-artifacts${compact ? " compact" : ""}`;
    const heading = document.createElement("strong");
    heading.className = "task-artifacts-heading";
    heading.textContent = "任务文件";
    section.append(heading);
    items.forEach((artifact) => {
      const row = document.createElement("div");
      row.className = "task-artifact";
      const copy = document.createElement("div");
      copy.className = "task-artifact-copy";
      const name = document.createElement("span");
      name.className = "task-artifact-name";
      name.textContent = artifact.filename || "未命名文件";
      const meta = document.createElement("span");
      meta.className = "task-artifact-meta";
      meta.textContent = artifact.state === "ready"
        ? `${formatBytes(artifact.byte_size)} · ${formatTime(artifact.expires_at)} 前可取用`
        : "下载链接已过期";
      copy.append(name, meta);
      row.append(copy);
      if (
        artifact.state === "ready" &&
        typeof artifact.download_url === "string" &&
        (/^https:\/\//.test(artifact.download_url) ||
         (artifact.artifact_type === "database_csv" &&
          /^\/api\/database\/reports\/[a-z][a-z0-9_-]{0,63}\/[0-9a-f]{32}\/download$/.test(artifact.download_url)))
      ) {
        const download = document.createElement("a");
        download.className = "secondary task-artifact-download";
        download.href = artifact.download_url;
        download.target = "_blank";
        download.rel = "noopener";
        download.textContent = "下载";
        row.append(download);
      } else {
        const actions = document.createElement("div");
        actions.className = "task-artifact-actions";
        const state = document.createElement("span");
        state.className = "task-artifact-expired";
        state.textContent = "已过期";
        actions.append(state);
        if (
          ["certificate_scan", "smartlight_report"].includes(
            artifact.artifact_type,
          ) &&
          taskId &&
          artifact.artifact_id
        ) {
          const reissue = document.createElement("button");
          reissue.type = "button";
          reissue.className = "secondary task-artifact-reissue";
          reissue.textContent = "重新生成下载";
          reissue.addEventListener("click", () =>
            reissueArtifact(taskId, artifact.artifact_id, reissue),
          );
          actions.append(reissue);
        }
        row.append(actions);
      }
      section.append(row);
    });
    container.append(section);
  }

  return { renderMarkdown, groupQueryCards, renderTaskPlan, renderTaskPlanResult, appendArtifactList };
}
