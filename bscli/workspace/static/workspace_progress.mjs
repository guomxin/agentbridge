
export function createChatProgress({ document, state, $, scrollChat, scheduleChatRefresh, renderMarkdown, attachDispatchCancel }) {
  function ensureLiveMessage(runId) {
    let live = state.liveMessages.get(runId);
    if (live?.item?.isConnected) return live;
    const item = document.createElement("article");
    item.className = "message assistant live-message";
    const progress = document.createElement("div");
    progress.className = "live-progress";
    const text = document.createElement("div");
    text.className = "live-text";
    const actions = document.createElement("div");
    actions.className = "live-actions";
    item.append(progress, text, actions);
    $("#chat-messages").append(item);
    live = {
      item,
      progress,
      text,
      actions,
      progressRow: null,
      progressDetails: null,
      progressDescription: null,
      requestMessage: null,
    };
    state.liveMessages.set(runId, live);
    scrollChat();
    return live;
  }

  function adoptLiveMessage(previousRunId, runId, requestMessage) {
    if (previousRunId === runId) return ensureLiveMessage(runId);
    const previous = state.liveMessages.get(previousRunId);
    const existing = state.liveMessages.get(runId);
    if (existing?.item?.isConnected) return existing;
    if (!previous?.item?.isConnected) return ensureLiveMessage(runId);
    state.liveMessages.delete(previousRunId);
    previous.requestMessage = requestMessage;
    state.liveMessages.set(runId, previous);
    return previous;
  }

  function handleChatProgress(payload) {
    if (!payload.runId) return;
    if (payload.kind === "preamble" && payload.text) {
      const live = ensureLiveMessage(payload.runId);
      if (!live.progressDetails) {
        const details = document.createElement("details");
        details.className = "live-progress-details";
        const summary = document.createElement("summary");
        summary.textContent = "查看处理说明";
        const description = document.createElement("div");
        description.className = "live-progress-description";
        details.append(summary, description);
        live.progress.append(details);
        live.progressDetails = details;
        live.progressDescription = description;
      }
      // Preamble events carry cumulative text. Replace it in place; never add
      // a progress step (or force a scroll) for every streamed fragment.
      renderMarkdown(live.progressDescription, payload.text);
      addLiveProgress(payload.runId, "正在梳理处理步骤", "active");
      return;
    }
    if (payload.kind === "lifecycle" && payload.phase === "end") {
      // The agent turn ending does not mean the business approval has completed.
      addLiveProgress(payload.runId, "正在整理处理结果", "active");
      return;
    }
    if (payload.label) {
      const complete =
        payload.phase === "result" || payload.phase === "end";
      addLiveProgress(
        payload.runId,
        complete
          ? payload.label.replace(/^正在/, "已完成")
          : payload.label,
        complete
          ? "complete"
          : payload.phase === "error" || payload.phase === "aborted"
            ? "failed"
            : "active",
      );
    }
  }

  function addLiveProgress(runId, label, status) {
    const live = ensureLiveMessage(runId);
    let row = live.progressRow;
    if (!row) {
      row = document.createElement("div");
      row.className = "live-progress-row";
      row.setAttribute("role", "status");
      row.setAttribute("aria-live", "polite");
      row.setAttribute("aria-atomic", "true");
      const dot = document.createElement("span");
      dot.className = "live-progress-dot";
      const copy = document.createElement("span");
      row.append(dot, copy);
      live.progress.prepend(row);
      live.progressRow = row;
    }
    if (row.className === `live-progress-row ${status}` &&
        row.lastElementChild.textContent === label) return;
    row.className = `live-progress-row ${status}`;
    row.lastElementChild.textContent = label;
    scrollChat();
  }

  function handleChatDelta(payload) {
    if (!payload.runId) return;
    const live = ensureLiveMessage(payload.runId);
    if (typeof payload.text === "string") {
      renderMarkdown(live.text, payload.text);
    }
    if (payload.state === "final") {
      live.progress.replaceChildren();
      live.actions.replaceChildren();
      live.item.classList.remove("live-message");
      state.liveMessages.delete(payload.runId);
      scheduleChatRefresh(500, 1);
    }
    scrollChat();
  }

  function restoreActiveDispatches(items) {
    const activeKeys = new Set();
    const labels = {
      queued: "请求已保存，正在连接智能体",
      waiting_host: "智能体连接正在恢复，恢复后会自动继续",
      dispatching: "正在交给智能体处理",
      reconciling_acceptance: "正在确认智能体是否已接收",
      accepted: "智能体已开始处理",
    };
    items.forEach((dispatch) => {
      const key = dispatch.runId || dispatch.idempotencyKey;
      if (!key) return;
      activeKeys.add(key);
      const live = ensureLiveMessage(key);
      live.dispatchId = dispatch.dispatchId;
      live.restoredDispatch = true;
      live.requestMessage = dispatch.requestMessage || null;
      addLiveProgress(
        key,
        dispatch.state === "accepted" && dispatch.lastErrorCode === "HOST_RUN_RESULT_PENDING"
          ? "请求已接收，正在恢复结果；不会重复执行"
          : labels[dispatch.state] || "正在继续原请求",
        "active",
      );
      if (["queued", "waiting_host"].includes(dispatch.state)) {
        attachDispatchCancel(key, dispatch.dispatchId);
      } else {
        live.actions.replaceChildren();
      }
    });
    state.liveMessages.forEach((live, key) => {
      if (!live?.restoredDispatch || activeKeys.has(key)) return;
      live.item.remove();
      state.liveMessages.delete(key);
    });
  }

  return { ensureLiveMessage, adoptLiveMessage, handleChatProgress, addLiveProgress, handleChatDelta, restoreActiveDispatches };
}
