const state = {
  account: null,
  activeView: "chat",
  tasks: [],
  selectedTaskId: null,
  enrollmentTimer: null,
  chatTimer: null,
  taskListTimer: null,
  eventSource: null,
  timelineReconnectTimer: null,
  timelineReconcileTimer: null,
  timelineReconcileActive: false,
  gatewayStatusTimer: null,
  gatewayStatusPolling: false,
  gatewayStatusCheckActive: false,
  clientVersionTimer: null,
  clientVersionCheckActive: false,
  timelineCursor: 0,
  chatTimelineOldestSequence: 0,
  chatTimelineHasOlder: false,
  chatTimelineLoadingOlder: false,
  liveMessages: new Map(),
  activeStreams: new Map(),
  historyMessages: new Map(),
  localMessages: new Map(),
  syncedMessages: new Map(),
  taskCards: new Map(),
  skillCards: new Map(),
  taskCardMeta: new Map(),
  queryGroupOpen: new Map(),
  taskSyncTimers: new Map(),
  toasts: new Map(),
  composerAttachments: [],
};

const MAX_COMPOSER_IMAGES = 4;
const MAX_COMPOSER_IMAGE_BYTES = 6 * 1024 * 1024;
const MAX_COMPOSER_IMAGES_TOTAL_BYTES = 12 * 1024 * 1024;
const GATEWAY_STATUS_ONLINE_POLL_MS = 30000;
const GATEWAY_STATUS_OFFLINE_POLL_MS = 5000;
const SUPPORTED_COMPOSER_IMAGE_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
]);

const CLIENT_VERSION =
  document.querySelector('meta[name="agentbridge-workspace-version"]')
    ?.content || "";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

document.addEventListener("DOMContentLoaded", bootstrap);

async function bootstrap() {
  bindActions();
  try {
    const session = await api("/api/session");
    if (session.authenticated) {
      enterWorkspace(session.account);
    } else {
      showAuth();
      await restoreEnrollment();
    }
  } catch (error) {
    showAuth();
    showAuthError(error.message);
  }
}

function bindActions() {
  $("#login-tab").addEventListener("click", () => switchAuth("login"));
  $("#enroll-tab").addEventListener("click", () => switchAuth("enroll"));
  $("#login-form").addEventListener("submit", login);
  $("#start-link").addEventListener("click", startEnrollment);
  $("#enroll-complete").addEventListener("submit", completeEnrollment);
  $("#logout-button").addEventListener("click", logout);
  $("#chat-form").addEventListener("submit", sendChat);
  $("#attach-image").addEventListener("click", () => {
    $("#image-input").click();
  });
  $("#image-input").addEventListener("change", async (event) => {
    await addComposerFiles(event.currentTarget.files);
    event.currentTarget.value = "";
  });
  const messageInput = $("#chat-form textarea[name='message']");
  messageInput.addEventListener("paste", handleComposerPaste);
  const composer = $("#chat-form");
  composer.addEventListener("dragover", handleComposerDragOver);
  composer.addEventListener("dragleave", handleComposerDragLeave);
  composer.addEventListener("drop", handleComposerDrop);
  $("#refresh-chat").addEventListener("click", () => {
    loadChat();
    loadGatewayStatus();
  });
  $("#refresh-tasks").addEventListener("click", loadTasks);
  $("#active-only").addEventListener("change", loadTasks);
  $("#refresh-endpoints").addEventListener("click", loadEndpoints);
  $("#refresh-skills").addEventListener("click", loadSkills);
  $$(".nav-item").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  document.addEventListener("visibilitychange", refreshWorkspaceState);
  window.addEventListener("focus", refreshWorkspaceState);
  $("#image-viewer-close").addEventListener("click", closeImageViewer);
  $("#image-viewer-backdrop").addEventListener("click", closeImageViewer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#image-viewer").hidden) {
      closeImageViewer();
    }
  });
}

function showAuth() {
  $("#auth-view").hidden = false;
  $("#app-view").hidden = true;
}

function switchAuth(mode) {
  const login = mode === "login";
  $("#login-tab").classList.toggle("active", login);
  $("#enroll-tab").classList.toggle("active", !login);
  $("#login-tab").setAttribute("aria-selected", String(login));
  $("#enroll-tab").setAttribute("aria-selected", String(!login));
  $("#login-form").hidden = !login;
  $("#enroll-flow").hidden = login;
  showAuthError("");
}

async function login(event) {
  event.preventDefault();
  const loginForm = event.currentTarget;
  const form = new FormData(loginForm);
  setBusy(loginForm, true);
  showAuthError("");
  try {
    const result = await api("/api/login", {
      method: "POST",
      body: {
        username: form.get("username"),
        password: form.get("password"),
      },
    });
    enterWorkspace(result.account);
  } catch (error) {
    showAuthError(friendlyError(error));
  } finally {
    setBusy(loginForm, false);
  }
}

async function startEnrollment() {
  const button = $("#start-link");
  button.disabled = true;
  showAuthError("");
  try {
    const result = await api("/api/enrollment/start", {
      method: "POST",
      body: {},
    });
    renderEnrollmentPending(result);
    pollEnrollment();
  } catch (error) {
    showAuthError(friendlyError(error));
    button.disabled = false;
  }
}

async function restoreEnrollment() {
  try {
    const result = await api("/api/enrollment/status");
    if (!result.active) return;
    switchAuth("enroll");
    if (result.state === "confirmed") {
      renderEnrollmentConfirmed();
    } else if (result.state === "pending") {
      $("#enroll-start").hidden = false;
      $("#enroll-pending").hidden = true;
      $("#start-link").disabled = false;
      showAuthError("页面已刷新，请重新生成配对码。");
    }
  } catch {}
}

function renderEnrollmentPending(result) {
  $("#enroll-start").hidden = true;
  $("#enroll-pending").hidden = false;
  $("#enroll-complete").hidden = true;
  $("#link-code").textContent = result.linkCode;
  $("#link-command").textContent = `/agentbridge link ${result.linkCode}`;
  $("#link-status").textContent = "等待可信端确认";
}

function pollEnrollment() {
  clearInterval(state.enrollmentTimer);
  state.enrollmentTimer = setInterval(async () => {
    try {
      const result = await api("/api/enrollment/status");
      if (result.state === "confirmed") {
        clearInterval(state.enrollmentTimer);
        renderEnrollmentConfirmed();
      } else if (["expired", "consumed"].includes(result.state)) {
        clearInterval(state.enrollmentTimer);
        $("#link-status").textContent = "配对码已失效";
        $("#start-link").disabled = false;
      }
    } catch {}
  }, 1500);
}

function renderEnrollmentConfirmed() {
  $("#enroll-pending").hidden = true;
  $("#enroll-complete").hidden = false;
}

async function completeEnrollment(event) {
  event.preventDefault();
  const enrollmentForm = event.currentTarget;
  const form = new FormData(enrollmentForm);
  const password = String(form.get("password") || "");
  if (password !== String(form.get("passwordConfirm") || "")) {
    showAuthError("两次输入的密码不一致。");
    return;
  }
  setBusy(enrollmentForm, true);
  try {
    const result = await api("/api/enrollment/complete", {
      method: "POST",
      body: {
        username: form.get("username"),
        password,
      },
    });
    enterWorkspace(result.account);
  } catch (error) {
    showAuthError(friendlyError(error));
  } finally {
    setBusy(enrollmentForm, false);
  }
}

function enterWorkspace(account) {
  clearInterval(state.enrollmentTimer);
  stopWorkspaceObservers();
  state.account = account;
  $("#auth-view").hidden = true;
  $("#app-view").hidden = false;
  $("#account-name").textContent = account.username;
  switchView("chat");
  loadTasks();
  loadChat().finally(() => {
    openTimelineStream();
    startWorkspaceObservers();
    loadGatewayStatus();
  });
}

async function logout() {
  try {
    await api("/api/logout", { method: "POST", body: {}, csrf: true });
  } catch {}
  stopWorkspaceObservers();
  clearTimeout(state.chatTimer);
  clearTimeout(state.taskListTimer);
  state.taskSyncTimers.forEach((timer) => clearTimeout(timer));
  state.taskSyncTimers.clear();
  state.historyMessages.clear();
  state.localMessages.clear();
  state.syncedMessages.clear();
  state.taskCards.clear();
  state.skillCards.clear();
  state.taskCardMeta.clear();
  state.queryGroupOpen.clear();
  state.composerAttachments = [];
  state.timelineCursor = 0;
  state.chatTimelineOldestSequence = 0;
  state.chatTimelineHasOlder = false;
  state.chatTimelineLoadingOlder = false;
  state.account = null;
  location.reload();
}

function switchView(view) {
  state.activeView = view;
  $$(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === view);
  });
  $$(".workspace-view").forEach((item) => {
    item.hidden = item.id !== `view-${view}`;
  });
  if (view === "tasks") {
    $("#task-detail").classList.add("mobile-empty");
    loadTasks();
  }
  if (view === "endpoints") loadEndpoints();
  if (view === "skills") loadSkills();
}

async function loadSkills() {
  await loadSkillDrafts();
  const container = $("#skill-list");
  container.replaceChildren();
  try {
    const result = await api("/api/skills");
    if (!result.items.length) container.textContent = "尚未分配业务助手，请联系管理员配置。原有查询仍可在对话中使用。";
    for (const item of result.items) {
      const card = document.createElement("article");
      card.className = "skill-card";
      const title = document.createElement("h3"); title.textContent = item.name;
      const text = document.createElement("p"); text.textContent = item.description;
      const note = document.createElement("p");
      const eligible = Object.values(item.profiles).some(p => p.available || p.needs_source);
      note.textContent = item.status === "disabled" ? "已停用" : !eligible ? Object.values(item.profiles).flatMap(p => p.missing).join("；") : item.status === "trial" ? "试用 · 结果请结合来源核对" : "可用";
      const button = document.createElement("button"); button.type = "button"; button.className = "secondary"; button.textContent = "使用此助手";
      button.disabled = item.status === "disabled" || !eligible;
      button.addEventListener("click", () => {
        switchView("chat");
        const input = $("#chat-form textarea[name='message']");
        input.value = `请使用“${item.name}”（${item.id}）助手，`;
        input.focus();
      });
      card.append(title, text, note, button); addSkillFeedback(card,item); container.append(card);
    }
  } catch { container.textContent = "业务助手暂时无法加载，请稍后刷新。"; }
}

async function loadGatewayStatus() {
  if (
    !state.account ||
    !state.gatewayStatusPolling ||
    state.gatewayStatusCheckActive
  ) {
    return;
  }
  const element = $("#gateway-state");
  const dot = element.querySelector(".status-dot");
  clearTimeout(state.gatewayStatusTimer);
  state.gatewayStatusTimer = null;
  state.gatewayStatusCheckActive = true;
  let available = false;
  try {
    const result = await api("/api/gateway");
    available = Boolean(result.available);
    dot.className = `status-dot ${available ? "online" : "offline"}`;
    element.title = available
      ? `OpenClaw ${result.version || "已连接"}`
      : `OpenClaw 不可用：${result.code}`;
  } catch {
    dot.className = "status-dot offline";
  } finally {
    state.gatewayStatusCheckActive = false;
    if (state.account && state.gatewayStatusPolling) {
      state.gatewayStatusTimer = setTimeout(
        loadGatewayStatus,
        available
          ? GATEWAY_STATUS_ONLINE_POLL_MS
          : GATEWAY_STATUS_OFFLINE_POLL_MS,
      );
    }
  }
}

async function loadChat() {
  const container = $("#chat-messages");
  try {
    const [history, taskTimeline, chatTimeline, dispatches] = await Promise.all([
      api("/api/chat/history?limit=120"),
      api("/api/timeline?entry_type=task_event&limit=240"),
      api("/api/timeline?entry_type=chat_message&limit=500"),
      api("/api/chat/dispatches?active_only=true&limit=20"),
    ]);
    state.historyMessages.clear();
    history.messages.forEach((message, index) => {
      const key = historyMessageKey(message, index);
      const item = messageElement(message);
      setTimelineNode(item, {
        key,
        createdAt: message.timestamp,
        order: index,
      });
      state.historyMessages.set(key, item);
    });
    state.localMessages.clear();
    taskTimeline.items.forEach((entry) => ingestTimelineEntry(entry, false));
    chatTimeline.items.forEach((entry) => ingestTimelineEntry(entry, false));
    state.chatTimelineOldestSequence = Math.min(
      ...[
        Number(chatTimeline.oldestSequence) || 0,
        ...[...state.syncedMessages.values()].map(
          (item) => Number(item.dataset.timelineOrder) || 0,
        ),
      ].filter((sequence) => sequence > 0),
    );
    if (!Number.isFinite(state.chatTimelineOldestSequence)) {
      state.chatTimelineOldestSequence = 0;
    }
    state.chatTimelineHasOlder = Boolean(chatTimeline.hasMore);
    state.timelineCursor = Math.max(
      state.timelineCursor,
      Number(taskTimeline.cursor) || 0,
      Number(chatTimeline.cursor) || 0,
    );
    dismissTerminalLiveMessages();
    restoreActiveDispatches(dispatches.items || []);
    await hydrateTaskCards({ render: false });
    renderChatTimeline();
    container.scrollTop = container.scrollHeight;
  } catch (error) {
    if (container.childElementCount === 0) {
      container.replaceChildren(
        messageElement({
          role: "system",
          text: `智能体暂时不可用：${friendlyError(error)}`,
        }),
      );
    }
  }
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

function messageElement(message) {
  const item = document.createElement("article");
  item.className = `message ${message.role}`;
  item.dataset.messageRole = String(message.role || "");
  item.dataset.messageTextHash = textHash(message.text);
  const text = document.createElement("div");
  if (message.role === "assistant") renderMarkdown(text, message.text);
  else text.textContent = message.text;
  item.append(text);
  if (Array.isArray(message.images) && message.images.length > 0) {
    const images = document.createElement("div");
    images.className = "message-image-list";
    message.images.forEach((image) => {
      const source = image.dataUrl || image.mediaUrl;
      const downloadSource = image.downloadUrl || source;
      const fileName = image.fileName || "附加图片";
      const open = document.createElement("button");
      open.type = "button";
      open.className = "message-image-button";
      open.title = `放大查看 ${fileName}`;
      open.setAttribute("aria-label", `放大查看 ${fileName}`);
      const preview = document.createElement("img");
      preview.src = source;
      preview.alt = fileName;
      preview.loading = "lazy";
      preview.addEventListener("error", () => {
        preview.classList.add("unavailable");
        preview.alt = `${fileName}（已不可用）`;
        open.disabled = true;
      });
      open.addEventListener("click", () => {
        openImageViewer({ source, downloadSource, fileName });
      });
      open.append(preview);
      images.append(open);
    });
    item.append(images);
  }
  if (message.timestamp || message.sourceLabel) {
    const time = document.createElement("time");
    time.className = "message-meta";
    time.textContent = [
      message.sourceLabel,
      message.timestamp ? formatTime(message.timestamp) : null,
    ].filter(Boolean).join(" · ");
    item.append(time);
  }
  return item;
}

function openImageViewer({ source, downloadSource, fileName }) {
  if (!source) return;
  const viewer = $("#image-viewer");
  const image = $("#image-viewer-image");
  const caption = $("#image-viewer-caption");
  const download = $("#image-viewer-download");
  image.src = source;
  image.alt = fileName;
  caption.textContent = fileName;
  download.href = downloadSource || source;
  download.download = fileName;
  download.setAttribute("aria-label", `下载原图 ${fileName}`);
  viewer.hidden = false;
  document.body.classList.add("image-viewer-open");
  $("#image-viewer-close").focus();
}

function closeImageViewer() {
  const viewer = $("#image-viewer");
  if (viewer.hidden) return;
  viewer.hidden = true;
  $("#image-viewer-image").removeAttribute("src");
  $("#image-viewer-download").removeAttribute("href");
  document.body.classList.remove("image-viewer-open");
}

function historyMessageKey(message, index) {
  if (message.id) return `history:${message.id}`;
  return [
    "history",
    message.role,
    message.timestamp || index,
    textHash(message.text),
  ].join(":");
}

function textHash(value) {
  let hash = 2166136261;
  for (const character of String(value || "")) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16);
}

function setTimelineNode(node, { key, createdAt, order = 0 }) {
  const parsed = parseTimestampMilliseconds(createdAt);
  node.dataset.timelineKey = String(key);
  node.dataset.timelineAt = String(
    Number.isFinite(parsed) ? parsed : Number(order) || 0,
  );
  node.dataset.timelineOrder = String(Number(order) || 0);
}

function ingestTimelineEntry(entry, render = true) {
  const sequence = Number(entry?.sequence) || 0;
  state.timelineCursor = Math.max(state.timelineCursor, sequence);
  if (entry?.entry_type === "chat_message") {
    if (entry.source?.is_origin) {
      reconcileOriginChatMessage(entry, render);
      return;
    }
    if (!entry.entry_id || !entry.text) return;
    let item = state.syncedMessages.get(entry.entry_id);
    if (!item) {
      item = messageElement({
        role: entry.role === "user" ? "user" : "assistant",
        text: entry.text,
        images: timelineEntryImages(entry),
        timestamp: entry.created_at,
        sourceLabel: entry.source?.display_label || "其他端",
      });
      state.syncedMessages.set(entry.entry_id, item);
    }
    setTimelineNode(item, {
      key: `timeline:${entry.entry_id}`,
      createdAt: entry.created_at,
      order: sequence,
    });
    if (render) {
      renderChatTimeline();
      scrollChat();
    }
    return;
  }
  if (entry?.entry_type === "task_event") {
    const taskId = entry.task_id || entry.payload?.taskId;
    const eventType = entry.payload?.eventType;
    const interactionId = entry.payload?.payload?.interactionId;
    const cardKey = interactionId
      ? `${taskId}:interaction:${interactionId}`
      : `${taskId}:summary`;
    if (taskId && !state.taskCardMeta.has(cardKey)) {
      state.taskCardMeta.set(cardKey, {
        createdAt: entry.created_at,
        sequence,
      });
    }
    if (render) scheduleTaskSync(taskId, eventType);
  }
}

function renderChatTimeline() {
  const container = $("#chat-messages");
  const stable = [
    ...state.historyMessages.values(),
    ...state.localMessages.values(),
    ...state.syncedMessages.values(),
    ...state.taskCards.values(),
    ...state.skillCards.values(),
  ].filter(Boolean);
  stable.sort((left, right) => {
    const time =
      Number(left.dataset.timelineAt) - Number(right.dataset.timelineAt);
    if (time !== 0) return time;
    const order =
      Number(left.dataset.timelineOrder) -
      Number(right.dataset.timelineOrder);
    if (order !== 0) return order;
    return String(left.dataset.timelineKey).localeCompare(
      String(right.dataset.timelineKey),
    );
  });
  const live = [...state.liveMessages.values()]
    .map((item) => item.item)
    .filter((item) => item?.isConnected);
  if (stable.length === 0 && live.length === 0) {
    container.replaceChildren(
      messageElement({
        role: "system",
        text: "开始一个新的工作会话",
      }),
    );
    return;
  }
  const olderControl = state.chatTimelineHasOlder
    ? [olderChatControl()]
    : [];
  container.replaceChildren(...olderControl, ...groupQueryCards(stable), ...live);
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

function olderChatControl() {
  const wrapper = document.createElement("div");
  wrapper.className = "chat-history-control";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary compact";
  button.disabled = state.chatTimelineLoadingOlder;
  button.textContent = state.chatTimelineLoadingOlder
    ? "正在加载..."
    : "加载更早消息";
  button.addEventListener("click", loadOlderChatMessages);
  wrapper.append(button);
  return wrapper;
}

async function loadOlderChatMessages() {
  const before = Number(state.chatTimelineOldestSequence) || 0;
  if (!before || state.chatTimelineLoadingOlder) return;
  const container = $("#chat-messages");
  const previousHeight = container.scrollHeight;
  const previousTop = container.scrollTop;
  state.chatTimelineLoadingOlder = true;
  renderChatTimeline();
  try {
    const result = await api(
      `/api/timeline?entry_type=chat_message&before=${encodeURIComponent(before)}&limit=500`,
    );
    const items = Array.isArray(result.items) ? result.items : [];
    items.forEach((entry) => ingestTimelineEntry(entry, false));
    const oldest = Number(result.oldestSequence) || 0;
    if (oldest) state.chatTimelineOldestSequence = oldest;
    state.chatTimelineHasOlder = Boolean(result.hasMore);
  } catch (error) {
    toast(`加载更早消息失败：${friendlyError(error)}`, true);
  } finally {
    state.chatTimelineLoadingOlder = false;
    renderChatTimeline();
    container.scrollTop = previousTop + container.scrollHeight - previousHeight;
  }
}

async function handleComposerPaste(event) {
  const files = [...(event.clipboardData?.items || [])]
    .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
    .map((item) => item.getAsFile())
    .filter(Boolean);
  if (files.length === 0) return;
  event.preventDefault();
  await addComposerFiles(files);
}

function handleComposerDragOver(event) {
  if (![...(event.dataTransfer?.items || [])].some(
    (item) => item.kind === "file" && item.type.startsWith("image/"),
  )) {
    return;
  }
  event.preventDefault();
  event.currentTarget.classList.add("drag-active");
}

function handleComposerDragLeave(event) {
  if (!event.currentTarget.contains(event.relatedTarget)) {
    event.currentTarget.classList.remove("drag-active");
  }
}

async function handleComposerDrop(event) {
  event.currentTarget.classList.remove("drag-active");
  const files = [...(event.dataTransfer?.files || [])].filter((file) =>
    file.type.startsWith("image/"),
  );
  if (files.length === 0) return;
  event.preventDefault();
  await addComposerFiles(files);
}

async function addComposerFiles(fileList) {
  const available = MAX_COMPOSER_IMAGES - state.composerAttachments.length;
  if (available <= 0) {
    toast("一次最多添加 4 张图片。", true, "image-limit");
    return;
  }
  const files = [...(fileList || [])];
  if (files.length > available) {
    toast("一次最多添加 4 张图片。", true, "image-limit");
  }
  let totalBytes = state.composerAttachments.reduce(
    (total, attachment) => total + Number(attachment.size || 0),
    0,
  );
  for (const file of files.slice(0, available)) {
    const mimeType = normalizedImageType(file);
    if (!SUPPORTED_COMPOSER_IMAGE_TYPES.has(mimeType)) {
      toast("仅支持 JPEG、PNG 和 WebP 图片。", true, "image-type");
      continue;
    }
    if (!file.size || file.size > MAX_COMPOSER_IMAGE_BYTES) {
      toast("单张图片不能超过 6 MB。", true, "image-size");
      continue;
    }
    if (totalBytes + file.size > MAX_COMPOSER_IMAGES_TOTAL_BYTES) {
      toast("图片总大小不能超过 12 MB。", true, "image-total-size");
      continue;
    }
    try {
      state.composerAttachments.push(
        await readComposerImage(file, mimeType),
      );
      totalBytes += file.size;
    } catch {
      toast("图片读取失败，请重新选择。", true, "image-read");
    }
  }
  renderComposerAttachments();
}

function normalizedImageType(file) {
  const supplied = String(file?.type || "").toLowerCase();
  if (supplied === "image/jpg") return "image/jpeg";
  if (supplied) return supplied;
  const name = String(file?.name || "").toLowerCase();
  if (/\.jpe?g$/.test(name)) return "image/jpeg";
  if (/\.png$/.test(name)) return "image/png";
  if (/\.webp$/.test(name)) return "image/webp";
  return "";
}

function readComposerImage(file, mimeType) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("error", () => reject(reader.error));
    reader.addEventListener("load", () => {
      const dataUrl = String(reader.result || "");
      const separator = dataUrl.indexOf(",");
      const content = separator >= 0 ? dataUrl.slice(separator + 1) : "";
      if (!content) {
        reject(new Error("image content is empty"));
        return;
      }
      resolve({
        id: crypto.randomUUID(),
        fileName: String(file.name || "pasted-image").slice(0, 120),
        mimeType,
        content,
        dataUrl,
        size: file.size,
      });
    });
    reader.readAsDataURL(file);
  });
}

function renderComposerAttachments() {
  const container = $("#composer-attachments");
  container.replaceChildren();
  state.composerAttachments.forEach((attachment) => {
    const preview = document.createElement("div");
    preview.className = "attachment-preview";
    const image = document.createElement("img");
    image.src = attachment.dataUrl;
    image.alt = attachment.fileName;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attachment-remove";
    remove.title = `移除 ${attachment.fileName}`;
    remove.setAttribute("aria-label", remove.title);
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      state.composerAttachments = state.composerAttachments.filter(
        (item) => item.id !== attachment.id,
      );
      renderComposerAttachments();
    });
    preview.append(image, remove);
    container.append(preview);
  });
  container.hidden = state.composerAttachments.length === 0;
}

function clearComposerAttachments() {
  state.composerAttachments = [];
  renderComposerAttachments();
}

async function sendChat(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const textarea = form.elements.message;
  const attachments = state.composerAttachments.map((item) => ({ ...item }));
  const message =
    textarea.value.trim() ||
    (attachments.length > 0 ? "请处理附加图片中的内容。" : "");
  if (!message) return;
  textarea.value = "";
  clearComposerAttachments();
  await executeChatMessage(message, form, attachments);
}

async function executeChatMessage(
  message,
  form = $("#chat-form"),
  attachments = [],
) {
  dismissTerminalLiveMessages();
  const idempotencyKey = crypto.randomUUID();
  const local = messageElement({
    role: "user",
    text: message,
    images: attachments,
  });
  setTimelineNode(local, {
    key: `local:${idempotencyKey}`,
    createdAt: new Date().toISOString(),
    order: Number.MAX_SAFE_INTEGER - 1,
  });
  state.localMessages.set(idempotencyKey, local);
  renderChatTimeline();
  scrollChat();
  setBusy(form, true);
  const live = ensureLiveMessage(idempotencyKey);
  live.requestMessage = message;
  addLiveProgress(idempotencyKey, "正在连接智能体", "active");
  try {
    await consumeChatStream({ message, idempotencyKey, attachments });
    scheduleChatRefresh(500, 1);
  } catch (error) {
    if (!error.rendered) {
      const text = friendlyError(error);
      renderRunFailure(
        error.runId || idempotencyKey,
        text,
        error.safeToRetry === true,
        message,
        attachments,
      );
      toast(text, true, error.code || "chat-failed");
    }
  } finally {
    setBusy(form, false);
    form.elements.message?.focus();
  }
}

function scheduleChatRefresh(delay, attempts) {
  clearTimeout(state.chatTimer);
  state.chatTimer = setTimeout(async () => {
    await loadChat();
    if (attempts > 1) scheduleChatRefresh(1800, attempts - 1);
  }, delay);
}

async function consumeChatStream({ message, idempotencyKey, attachments = [] }) {
  const activeStream = {
    controller: new AbortController(),
    requestMessage: message,
    requestAttachments: attachments,
    runIds: new Set(),
    terminal: false,
    timelineCompleted: false,
  };
  registerActiveStream(activeStream, idempotencyKey);
  let runId = idempotencyKey;
  let hadToolActivity = false;
  let terminalFailure = null;
  let streamFailure = null;
  let reader = null;
  try {
    const response = await fetchChatStreamResponse({
      message,
      idempotencyKey,
      attachments,
      activeStream,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const error = new Error(
        payload?.error?.message || payload?.error?.code || "请求失败",
      );
      error.code = payload?.error?.code;
      throw error;
    }
    if (!response.body) {
      const error = new Error("浏览器不支持流式响应");
      error.code = "STREAM_UNAVAILABLE";
      throw error;
    }
    reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let stopReading = false;
    readLoop:
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = done ? "" : blocks.pop() || "";
      for (const block of blocks) {
        const event = parseSseBlock(block);
        if (!event) continue;
        if (event.name === "accepted" && event.data.runId) {
          const previousRunId = runId;
          runId = event.data.runId;
          registerActiveStream(activeStream, runId);
          adoptLiveMessage(previousRunId, runId, message);
          addLiveProgress(runId, "请求已交给智能体", "active");
          ensureLiveMessage(runId).actions.replaceChildren();
        } else if (event.name === "progress") {
          if (event.data.kind === "tool") hadToolActivity = true;
          handleChatProgress(event.data);
          if (["queued", "waiting_host"].includes(event.data.phase)) {
            attachDispatchCancel(runId, event.data.dispatchId);
          } else if (
            ["dispatching", "reconciling_acceptance"].includes(
              event.data.phase,
            )
          ) {
            ensureLiveMessage(runId).actions.replaceChildren();
          }
        } else if (event.name === "chat") {
          if (["final", "error", "aborted"].includes(event.data.state)) {
            activeStream.terminal = true;
            stopReading = true;
          }
          if (["error", "aborted"].includes(event.data.state)) {
            terminalFailure = event.data;
          } else {
            handleChatDelta(event.data);
          }
        } else if (event.name === "stream-error") {
          streamFailure = event.data;
          stopReading = true;
        }
        if (stopReading) break readLoop;
      }
      if (done) break;
    }
    if (stopReading && reader) {
      await reader.cancel().catch(() => {});
    }
    if (streamFailure) {
      const code = streamFailure.code || "GATEWAY_STREAM_FAILED";
      const error = new Error(code);
      error.code = code;
      error.runId = runId;
      error.details = streamFailure.details || {};
      error.safeToRetry = streamFailure.safeToRetry === true;
      throw error;
    }
    if (terminalFailure) {
      const effectiveToolActivity =
        hadToolActivity || terminalFailure.hadToolActivity === true;
      const safeToRetry =
        typeof terminalFailure.safeToRetry === "boolean"
          ? terminalFailure.safeToRetry
          : terminalFailure.state === "error" && !effectiveToolActivity;
      const text = agentFailureMessage(
        terminalFailure.text,
        safeToRetry,
        terminalFailure.state,
      );
      renderRunFailure(runId, text, safeToRetry, message, attachments);
      const error = new Error(text);
      error.code =
        terminalFailure.state === "aborted"
          ? "AGENT_RUN_ABORTED"
          : "AGENT_RUN_FAILED";
      error.runId = runId;
      error.rendered = true;
      throw error;
    }
  } catch (error) {
    if (
      activeStream.timelineCompleted &&
      activeStream.controller.signal.aborted
    ) {
      return;
    }
    throw error;
  } finally {
    unregisterActiveStream(activeStream);
  }
}

async function fetchChatStreamResponse({
  message,
  idempotencyKey,
  attachments,
  activeStream,
}) {
  const request = {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-AgentBridge-CSRF": cookieValue("agentbridge_workspace_csrf"),
    },
    credentials: "same-origin",
    signal: activeStream.controller.signal,
    body: JSON.stringify({
      message,
      idempotencyKey,
      attachments: attachments.map((item) => ({
        type: "image",
        mimeType: item.mimeType,
        fileName: item.fileName,
        content: item.content,
      })),
    }),
  };
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      return await fetch("/api/chat/send-stream", request);
    } catch (error) {
      const canRetry =
        attempt === 0 &&
        error instanceof TypeError &&
        !activeStream.controller.signal.aborted;
      if (!canRetry) throw error;
      addLiveProgress(
        idempotencyKey,
        "\u7f51\u7edc\u8fde\u63a5\u77ed\u6682\u4e2d\u65ad\uff0c\u6b63\u5728\u6062\u590d",
        "active",
      );
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
  }
  throw new TypeError("chat stream connection failed");
}

function registerActiveStream(activeStream, runId) {
  if (!runId) return;
  activeStream.runIds.add(runId);
  state.activeStreams.set(runId, activeStream);
}

function unregisterActiveStream(activeStream) {
  activeStream.runIds.forEach((runId) => {
    if (state.activeStreams.get(runId) === activeStream) {
      state.activeStreams.delete(runId);
    }
  });
}

function reconcileOriginChatMessage(entry, render = true) {
  if (!entry?.text) return;
  const messageKey = String(entry.message_key || "");
  if (entry.role === "user") {
    const images = timelineEntryImages(entry);
    if (!entry.entry_id) return;
    const localPrefix = "workspace:user:";
    if (messageKey.startsWith(localPrefix)) {
      state.localMessages.delete(messageKey.slice(localPrefix.length));
    }
    removeMatchingHistoryMessage(entry);
    let item = state.syncedMessages.get(entry.entry_id);
    if (!item) {
      item = messageElement({
        role: "user",
        text: entry.text,
        images,
        timestamp: entry.created_at,
      });
      state.syncedMessages.set(entry.entry_id, item);
    }
    setTimelineNode(item, {
      key: `timeline:${entry.entry_id}`,
      createdAt: entry.created_at,
      order: Number(entry.sequence) || 0,
    });
    if (render) renderChatTimeline();
    return;
  }
  if (entry.role !== "assistant") return;
  const failurePrefix = "workspace:assistant:error:";
  const finalPrefix = "workspace:assistant:";
  const failed = messageKey.startsWith(failurePrefix);
  const runId = failed
    ? messageKey.slice(failurePrefix.length)
    : messageKey.startsWith(finalPrefix)
      ? messageKey.slice(finalPrefix.length)
      : "";
  const activeStream = state.activeStreams.get(runId);
  if (activeStream) {
    if (!activeStream.terminal) {
      activeStream.terminal = true;
      activeStream.timelineCompleted = true;
      if (failed) {
        renderRunFailure(
          runId,
          entry.text,
          false,
          activeStream.requestMessage,
          activeStream.requestAttachments,
        );
      } else {
        handleChatDelta({ runId, state: "final", text: entry.text });
      }
      activeStream.controller.abort();
    }
    return;
  }
  const restoredLive = state.liveMessages.get(runId);
  if (restoredLive?.item?.isConnected) {
    restoredLive.item.remove();
    state.liveMessages.delete(runId);
  }
  if (!entry.entry_id) return;
  removeMatchingHistoryMessage(entry);
  let item = state.syncedMessages.get(entry.entry_id);
  if (!item) {
    item = messageElement({
      role: "assistant",
      text: entry.text,
      timestamp: entry.created_at,
    });
    state.syncedMessages.set(entry.entry_id, item);
  }
  setTimelineNode(item, {
    key: `timeline:${entry.entry_id}`,
    createdAt: entry.created_at,
    order: Number(entry.sequence) || 0,
  });
  if (render) renderChatTimeline();
}

function timelineEntryImages(entry) {
  const attachments = Array.isArray(entry?.payload?.attachments)
    ? entry.payload.attachments
    : [];
  return attachments
    .filter(
      (attachment) =>
        attachment?.type === "image" &&
        typeof attachment.mediaUrl === "string" &&
        attachment.mediaUrl,
    )
    .sort(
      (left, right) =>
        Number(left.ordinal || 0) - Number(right.ordinal || 0),
    )
    .map((attachment) => ({
      mediaUrl: attachment.mediaUrl,
      downloadUrl: attachment.attachmentId
        ? `/api/timeline/attachments/${encodeURIComponent(attachment.attachmentId)}/download`
        : attachment.mediaUrl,
      fileName: attachment.fileName || "附加图片",
      mimeType: attachment.mimeType,
    }));
}

function removeMatchingHistoryMessage(entry) {
  const targetHash = textHash(entry.text);
  const targetAt = parseTimestampMilliseconds(entry.created_at);
  for (const [key, item] of state.historyMessages) {
    if (
      item.dataset.messageRole !== String(entry.role || "") ||
      item.dataset.messageTextHash !== targetHash
    ) {
      continue;
    }
    const historyAt = Number(item.dataset.timelineAt);
    if (
      Number.isFinite(targetAt) &&
      Number.isFinite(historyAt) &&
      Math.abs(historyAt - targetAt) > 5 * 60 * 1000
    ) {
      continue;
    }
    state.historyMessages.delete(key);
    return;
  }
}

function parseSseBlock(block) {
  let name = "message";
  const data = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) {
      name = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      data.push(line.slice(5).trimStart());
    }
  }
  if (data.length === 0) return null;
  try {
    const value = JSON.parse(data.join("\n"));
    return value && typeof value === "object"
      ? { name, data: value }
      : null;
  } catch {
    return null;
  }
}

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

function renderRunFailure(
  runId,
  text,
  canRetry,
  requestMessage,
  requestAttachments = [],
) {
  const live = ensureLiveMessage(runId);
  live.item.classList.add("failed");
  live.text.textContent = text;
  live.actions.replaceChildren();
  addLiveProgress(runId, "处理未完成", "failed");
  if (canRetry && requestMessage) {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "secondary retry-command";
    retry.textContent = "重新发送";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      await executeChatMessage(
        requestMessage,
        $("#chat-form"),
        requestAttachments,
      );
    });
    live.actions.append(retry);
  }
  scrollChat();
}

function attachDispatchCancel(runId, dispatchId) {
  if (!runId || !dispatchId) return;
  const live = ensureLiveMessage(runId);
  if (live.cancelDispatchId === dispatchId && live.actions.childElementCount) {
    return;
  }
  live.cancelDispatchId = dispatchId;
  live.actions.replaceChildren();
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "secondary retry-command";
  cancel.textContent = "取消本次请求";
  cancel.addEventListener("click", async () => {
    cancel.disabled = true;
    try {
      await api(
        `/api/chat/dispatches/${encodeURIComponent(dispatchId)}/cancel`,
        { method: "POST", body: {}, csrf: true },
      );
      addLiveProgress(runId, "已取消等待", "failed");
    } catch (error) {
      cancel.disabled = false;
      toast(friendlyError(error), true, `dispatch:${dispatchId}:cancel`);
    }
  });
  live.actions.append(cancel);
}

function dismissTerminalLiveMessages() {
  state.liveMessages.forEach((live, runId) => {
    if (!live?.item?.classList.contains("failed")) return;
    live.item.remove();
    state.liveMessages.delete(runId);
  });
}

function agentFailureMessage(value, safeToRetry, state) {
  const text = String(value || "").trim();
  if (state === "aborted" && text) return text;
  if (state === "aborted" && safeToRetry) {
    return "智能体处理已停止，尚未调用业务工具，可以安全地重新发送。";
  }
  if (state === "aborted") {
    return "智能体处理已停止；请先核对业务系统状态再决定是否重试。";
  }
  const networkFailure =
    /network|connection|econnreset|before producing a reply/i.test(text);
  if (networkFailure && safeToRetry) {
    return "智能体网络连接中断，尚未调用业务工具。可安全地重新发送这条指令。";
  }
  if (networkFailure) {
    return "智能体网络连接中断。由于任务已经开始调用业务工具，请先查看任务状态再决定是否重试。";
  }
  return text || "智能体未能完成本次处理。";
}

function scrollChat() {
  const container = $("#chat-messages");
  container.scrollTop = container.scrollHeight;
}

function skillProfileLabel(profile) {
  return ({review: "总结与复核", track: "进展追踪", search: "相似案例检索",
    preview: "草稿预览", fill: "准备填写", single: "单项办理", batch: "批量办理"})[profile] || profile || "";
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

async function hydrateSkillCards({ render = true } = {}) {
  const account = state.account;
  try {
    const result = await api("/api/skills/history");
    if (state.account !== account) return;
    for (const event of result.items || []) {
      if (!state.skillCards.has(event.event_id)) {
        state.skillCards.set(event.event_id, renderSkillCard(event));
      }
    }
    if (render) renderChatTimeline();
  } catch { /* Keep existing evidence; absence of a response is not a failed Skill. */ }
}

async function hydrateTaskCards({ render = true } = {}) {
  await hydrateSkillCards({ render: false });
  try {
    const [taskResponse, historyResponse] = await Promise.allSettled([
      api("/api/tasks?active_only=false&limit=30"),
      api("/api/artifacts/history?limit=20"),
    ]);
    if (taskResponse.status !== "fulfilled") {
      throw taskResponse.reason;
    }
    const result = taskResponse.value;
    const artifactHistory = historyResponse.status === "fulfilled"
      ? historyResponse.value.items || []
      : [];
    const active = new Set(["active", "waiting_user", "running"]);
    const recentCutoff = Date.now() - 6 * 60 * 60 * 1000;
    const recentCandidates = result.items
      .filter((task) => {
        const updatedAt = Date.parse(task.updated_at || "");
        return active.has(task.status) ||
          (Number.isFinite(updatedAt) && updatedAt >= recentCutoff);
      })
      .slice(0, 30);
    const recentIds = new Set(
      recentCandidates.map((task) => task.task_id),
    );
    const historicalDetails = new Map(
      artifactHistory
        .filter((detail) => detail?.task?.task_id)
        .map((detail) => [detail.task.task_id, detail]),
    );
    const candidates = [
      ...recentCandidates,
      ...artifactHistory
        .map((detail) => detail.task)
        .filter((task) => !recentIds.has(task.task_id)),
    ].reverse();
    const details = await Promise.allSettled(
      candidates.map((task) => {
        if (!recentIds.has(task.task_id)) {
          return Promise.resolve(historicalDetails.get(task.task_id));
        }
        return api(`/api/tasks/${encodeURIComponent(task.task_id)}`);
      }),
    );
    const candidateIds = new Set(candidates.map((task) => task.task_id));
    details.forEach((result) => {
      if (result.status === "fulfilled") {
        upsertTaskCard(result.value, { render: false });
      }
    });
    state.taskCards.forEach((card, cardKey) => {
      if (!candidateIds.has(card.dataset.taskId || cardKey)) {
        card.remove();
        state.taskCards.delete(cardKey);
        state.taskCardMeta.delete(cardKey);
      }
    });
    if (render) renderChatTimeline();
  } catch {}
}

function scheduleTaskSync(taskId, eventType) {
  if (!taskId) return;
  clearTimeout(state.taskSyncTimers.get(taskId));
  state.taskSyncTimers.set(
    taskId,
    setTimeout(async () => {
      state.taskSyncTimers.delete(taskId);
      await syncTaskCard(taskId);
    }, 220),
  );
  clearTimeout(state.taskListTimer);
  state.taskListTimer = setTimeout(loadTasks, 260);
  if (
    ["task.operation.failed", "task.operation.outcome_unknown"].includes(
      eventType,
    )
  ) {
    toast(
      eventType === "task.operation.outcome_unknown"
        ? "一项业务操作的最终结果需要核对。"
        : "一项业务操作执行失败。",
      true,
      `task:${taskId}:failure`,
    );
  }
}

async function syncTaskCard(taskId) {
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    upsertTaskCard(result);
    if (state.selectedTaskId === taskId) renderTaskDetail(result);
  } catch (error) {
    if (error.code !== "TASK_NOT_FOUND") {
      toast(
        friendlyError(error),
        true,
        `task:${taskId}:${error.code || "sync"}`,
      );
    }
  }
}

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

function completedInteractionPresentation(interaction) {
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

function taskCardStatusForInteraction(state, fallback) {
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

function taskCardStatusMessage(status, summary = null) {
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

function taskCardArtifactDeliveryMessage(summary) {
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

function displayTaskTitle(value) {
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

async function loadTasks() {
  try {
    const activeOnly = $("#active-only").checked;
    const result = await api(
      `/api/tasks?active_only=${activeOnly}&limit=150`,
    );
    state.tasks = result.items;
    renderTasks();
    const active = result.items.filter((task) =>
      ["active", "waiting_user", "running"].includes(task.status),
    ).length;
    $("#active-task-count").textContent = String(active);
  } catch (error) {
    toast(friendlyError(error), true);
  }
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

async function loadTaskDetail(taskId) {
  state.selectedTaskId = taskId;
  renderTasks();
  const detail = $("#task-detail");
  detail.classList.remove("mobile-empty");
  detail.replaceChildren(emptyState("正在读取任务"));
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    renderTaskDetail(result);
  } catch (error) {
    detail.replaceChildren(emptyState(friendlyError(error)));
  }
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

async function continueTask(task, button) {
  if (!task?.task_id || button.disabled) return;
  button.disabled = true;
  try {
    const result = await api(
      `/api/tasks/${encodeURIComponent(task.task_id)}/continue`,
      { method: "POST", body: {}, csrf: true },
    );
    switchView("chat");
    await executeChatMessage(result.message);
  } catch (error) {
    toast(friendlyError(error), true, `task:${task.task_id}:continue`);
  } finally {
    button.disabled = false;
  }
}

function addDetail(list, term, value) {
  const dt = document.createElement("dt");
  const dd = document.createElement("dd");
  dt.textContent = term;
  dd.textContent = value || "—";
  list.append(dt, dd);
}

async function loadEndpoints() {
  const body = $("#endpoint-list");
  try {
    const result = await api("/api/endpoints");
    body.replaceChildren();
    result.items.forEach((endpoint) => {
      const row = document.createElement("tr");
      const label = endpoint.display_label;
      row.innerHTML = `
        <td></td>
        <td>${endpointType(endpoint.client_type)}</td>
        <td><span class="state-label"><span class="status-dot ${endpoint.state === "active" ? "online" : "offline"}"></span>${endpoint.state === "active" ? "有效" : "已停用"}</span></td>
        <td>${formatTime(endpoint.last_seen_at)}</td>`;
      row.firstElementChild.textContent = label;
      body.append(row);
    });
    if (result.items.length === 0) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 4;
      cell.append(emptyState("暂无关联端点"));
      row.append(cell);
      body.append(row);
    }
  } catch (error) {
    toast(friendlyError(error), true);
  }
}

function startWorkspaceObservers() {
  clearInterval(state.timelineReconcileTimer);
  clearInterval(state.clientVersionTimer);
  clearTimeout(state.gatewayStatusTimer);
  state.gatewayStatusPolling = true;
  state.timelineReconcileTimer = setInterval(reconcileTimeline, 10000);
  state.clientVersionTimer = setInterval(checkClientVersion, 30000);
  loadGatewayStatus();
}

function stopWorkspaceObservers() {
  state.eventSource?.close();
  state.eventSource = null;
  clearTimeout(state.timelineReconnectTimer);
  clearTimeout(state.gatewayStatusTimer);
  clearInterval(state.timelineReconcileTimer);
  clearInterval(state.clientVersionTimer);
  state.timelineReconnectTimer = null;
  state.gatewayStatusTimer = null;
  state.timelineReconcileTimer = null;
  state.clientVersionTimer = null;
  state.gatewayStatusPolling = false;
  state.timelineReconcileActive = false;
  state.clientVersionCheckActive = false;
}

function refreshWorkspaceState() {
  if (!state.account || document.visibilityState === "hidden") return;
  reconcileTimeline();
  checkClientVersion();
  loadGatewayStatus();
  if (!state.eventSource) openTimelineStream();
}

async function reconcileTimeline() {
  if (!state.account || state.timelineReconcileActive) return;
  state.timelineReconcileActive = true;
  try {
    for (let page = 0; page < 3; page += 1) {
      const after = Math.max(Number(state.timelineCursor) || 0, 0);
      const result = await api(
        `/api/timeline?after=${encodeURIComponent(after)}&limit=200`,
      );
      const items = Array.isArray(result.items) ? result.items : [];
      items.forEach((entry) => ingestTimelineEntry(entry));
      if (items.length < 200) {
        state.timelineCursor = Math.max(
          state.timelineCursor,
          Number(result.cursor) || 0,
        );
        break;
      }
    }
  } catch {
  } finally {
    state.timelineReconcileActive = false;
    await hydrateSkillCards();
  }
}

async function checkClientVersion() {
  if (!CLIENT_VERSION || state.clientVersionCheckActive) return;
  state.clientVersionCheckActive = true;
  try {
    const result = await api("/api/client-version");
    const runActive = state.activeStreams.size > 0;
    if (result.version && result.version !== CLIENT_VERSION && !runActive) {
      location.reload();
    }
  } catch {
  } finally {
    state.clientVersionCheckActive = false;
  }
}

function openTimelineStream() {
  if (!state.account) return;
  clearTimeout(state.timelineReconnectTimer);
  state.timelineReconnectTimer = null;
  state.eventSource?.close();
  const query = state.timelineCursor
    ? `?after=${encodeURIComponent(state.timelineCursor)}`
    : "";
  const source = new EventSource(`/api/timeline/stream${query}`);
  source.addEventListener("cursor", (event) => {
    state.timelineCursor = Math.max(
      state.timelineCursor,
      Number(event.lastEventId) || 0,
    );
  });
  source.addEventListener("timeline", (event) => {
    let payload = {};
    try {
      payload = JSON.parse(event.data || "{}");
    } catch {
      payload = {};
    }
    state.timelineCursor = Math.max(
      state.timelineCursor,
      Number(event.lastEventId) || Number(payload.sequence) || 0,
    );
    ingestTimelineEntry(payload);
  });
  source.onerror = () => {
    if (state.eventSource !== source) return;
    source.close();
    state.eventSource = null;
    clearTimeout(state.timelineReconnectTimer);
    state.timelineReconnectTimer = setTimeout(openTimelineStream, 5000);
  };
  state.eventSource = source;
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (options.csrf) {
    headers["X-AgentBridge-CSRF"] = cookieValue(
      "agentbridge_workspace_csrf",
    );
  }
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    credentials: "same-origin",
    body:
      options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      payload?.error?.message || payload?.error?.code || "请求失败",
    );
    error.code = payload?.error?.code;
    throw error;
  }
  return payload;
}

function cookieValue(name) {
  const prefix = `${name}=`;
  return (
    document.cookie
      .split(";")
      .map((item) => item.trim())
      .find((item) => item.startsWith(prefix))
      ?.slice(prefix.length) || ""
  );
}

function setBusy(form, busy) {
  [...form.querySelectorAll("button, input, textarea")].forEach((element) => {
    element.disabled = busy;
  });
}

function showAuthError(message) {
  $("#auth-error").textContent = message || "";
}

function friendlyError(error) {
  if (error.code === "GATEWAY_RUN_TIMEOUT_ABORTED") {
    return error.details?.hadToolActivity === true
      ? "智能体运行超时，已停止后续处理；任务已经调用业务工具，请先核对业务系统结果。"
      : "智能体运行超时，已安全中止；本次尚未调用业务系统，可以重新发送。";
  }
  const labels = {
    LOGIN_FAILED: "用户名或密码不正确。",
    LOGIN_RATE_LIMITED: "登录尝试过多，请稍后再试。",
    WORKSPACE_LINK_INVALID: "配对尚未完成或已经失效。",
    WORKSPACE_CONFLICT: "该身份或用户名已经绑定网页账号。",
    IDEMPOTENCY_PAYLOAD_MISMATCH:
      "同一次请求的内容发生变化，系统没有覆盖原请求。",
    WORKSPACE_HOST_QUEUE_CONVERSATION_LIMIT:
      "当前会话已有 3 条请求等待智能体受理，请稍后再发。",
    WORKSPACE_HOST_QUEUE_USER_LIMIT:
      "当前账号等待智能体受理的请求较多，请稍后再发。",
    HOST_DISPATCH_CANCEL_NOT_ALLOWED:
      "请求已经开始交给智能体，当前不能再取消等待。",
    GATEWAY_NOT_CONFIGURED: "智能体连接尚未配置。",
    WORKSPACE_RUN_IN_PROGRESS:
      "\u4e0a\u4e00\u6761\u7f51\u9875\u4efb\u52a1\u4ecd\u5728\u5904\u7406\uff0c\u672c\u6b21\u8bf7\u6c42\u6ca1\u6709\u6392\u961f\u3002",
    GATEWAY_RUN_TIMEOUT_ABORTED:
      "\u667a\u80fd\u4f53\u8fd0\u884c\u8d85\u65f6\uff0c\u5df2\u505c\u6b62\u540e\u7eed\u5904\u7406\u3002",
    GATEWAY_RUN_TIMEOUT_ABORT_UNCONFIRMED:
      "OpenClaw \u6682\u65f6\u65e0\u54cd\u5e94\uff0c\u672c\u6b21\u8bf7\u6c42\u5df2\u505c\u6b62\u7ee7\u7eed\u6392\u961f\u3002",
    GATEWAY_SESSION_NOT_IDLE:
      "\u4e0a\u4e00\u6761\u667a\u80fd\u4f53\u4efb\u52a1\u672a\u80fd\u53ca\u65f6\u7ed3\u675f\uff0c\u672c\u6b21\u8bf7\u6c42\u672a\u8fdb\u5165\u4e1a\u52a1\u7cfb\u7edf\u3002",
    GATEWAY_SESSION_STATE_UNAVAILABLE:
      "\u6682\u65f6\u65e0\u6cd5\u786e\u8ba4\u667a\u80fd\u4f53\u4f1a\u8bdd\u662f\u5426\u7a7a\u95f2\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\u3002",
    GATEWAY_START_STALLED_ABORTED:
      "\u667a\u80fd\u4f53\u81ea\u52a8\u6062\u590d\u540e\u4ecd\u672a\u80fd\u542f\u52a8\uff0c\u5df2\u5b89\u5168\u4e2d\u6b62\u3002",
    GATEWAY_START_STALLED_ABORT_UNCONFIRMED:
      "\u667a\u80fd\u4f53\u542f\u52a8\u72b6\u6001\u65e0\u6cd5\u786e\u8ba4\uff0c\u5df2\u505c\u6b62\u81ea\u52a8\u6062\u590d\u3002",
    GATEWAY_START_RECOVERY_BLOCKED_TOOL_ACTIVITY:
      "智能体启动异常，但本轮已经触碰业务工具；已停止自动重放，请先核对业务系统状态。",
    GATEWAY_START_RECOVERY_EVIDENCE_UNAVAILABLE:
      "智能体启动异常，且无法确认是否已调用业务工具；已停止自动重放。",
    GATEWAY_TIMEOUT:
      "OpenClaw \u6682\u65f6\u65e0\u54cd\u5e94\uff0c\u672c\u6b21\u8bf7\u6c42\u672a\u7ee7\u7eed\u8fdb\u5165\u4e1a\u52a1\u7cfb\u7edf\u3002",
    PAIRING_REQUIRED: "AgentBridge 服务器尚未获准连接 OpenClaw。",
    AUTHENTICATION_REQUIRED: "网页会话已失效，请重新登录。",
  };
  return labels[error.code] || error.message || "操作没有完成。";
}

function toast(message, isError = false, key = message) {
  const normalizedKey = String(key || message);
  let item = state.toasts.get(normalizedKey);
  if (!item?.isConnected) {
    item = document.createElement("div");
    state.toasts.set(normalizedKey, item);
    $("#toast-region").append(item);
  }
  item.className = `toast${isError ? " error" : ""}`;
  item.textContent = message;
  clearTimeout(item.dismissTimer);
  while ($("#toast-region").childElementCount > 2) {
    const oldest = $("#toast-region").firstElementChild;
    state.toasts.forEach((value, toastKey) => {
      if (value === oldest) state.toasts.delete(toastKey);
    });
    oldest?.remove();
  }
  item.dismissTimer = setTimeout(() => {
    item.remove();
    state.toasts.delete(normalizedKey);
  }, 5200);
}

function emptyState(text) {
  const item = document.createElement("div");
  item.className = "empty-state";
  item.textContent = text;
  return item;
}

function formatTime(value) {
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

function parseTimestampMilliseconds(value) {
  const text = String(value ?? "").trim();
  if (!text) return Number.NaN;
  if (/^\d{10,13}$/.test(text)) {
    const numeric = Number(text);
    return numeric < 100_000_000_000 ? numeric * 1_000 : numeric;
  }
  return Date.parse(text);
}

function statusLabel(status) {
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

function eventLabel(type) {
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

function taskPlanProgress(plan) {
  const steps = Array.isArray(plan?.steps) ? plan.steps : [];
  const completed = steps.filter((step) =>
    ["succeeded", "skipped"].includes(step.state),
  ).length;
  const current = steps.find((step) => step.stepKey === plan.currentStepKey);
  const suffix = current ? ` · ${current.title || current.stepKey}` : "";
  return `${completed}/${steps.length} 步${suffix}`;
}

function taskPlanFailurePresentation(plan) {
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

async function reissueArtifact(taskId, artifactId, button) {
  if (!taskId || !artifactId || button.disabled) return;
  button.disabled = true;
  button.textContent = "正在重新获取";
  try {
    const result = await api(
      `/api/tasks/${encodeURIComponent(taskId)}/artifacts/` +
        `${encodeURIComponent(artifactId)}/reissue`,
      { method: "POST", body: {}, csrf: true },
    );
    upsertTaskCard(result);
    if (state.selectedTaskId === taskId) renderTaskDetail(result);
    toast(
      "新的下载链接已生成，30 分钟内有效。",
      false,
      `artifact:${artifactId}:reissued`,
    );
  } catch (error) {
    toast(
      friendlyError(error),
      true,
      `artifact:${artifactId}:${error.code || "reissue"}`,
    );
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = "重新生成下载";
    }
  }
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (!Number.isFinite(size) || size <= 0) return "未知大小";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function interactionLabel(type) {
  return (
    {
      credential: "需要完成安全登录",
      business_input: "需要补充业务信息",
      execution_authorization: "需要核对并确认",
    }[type] || "需要用户处理"
  );
}

function interactionActionLabel(type) {
  return (
    {
      credential: "安全登录",
      business_input: "填写信息",
      execution_authorization: "核对并确认",
    }[type] || "继续处理"
  );
}

function endpointType(type) {
  return (
    {
      telegram: "Telegram",
      "openclaw-weixin": "微信",
      web: "网页",
    }[type] || type
  );
}

function escapeClass(value) {
  return String(value || "").replace(/[^a-z0-9_-]/gi, "");
}


function draftChat(message) {
  switchView("chat");
  const input=$("#chat-form textarea[name='message']"); input.value=message; input.focus();
}
const draftAction=(action,data={})=>api("/api/skill-drafts",{method:"POST",csrf:true,body:{action,data}});
const draftStatus={submitted:"待审批",published:"已发布",changes_requested:"需修改",rejected:"已拒绝",withdrawn:"已撤回",editing:"草稿",archived:"已归档"};
async function loadSkillDrafts() {
  const root=$("#skill-authoring"); if (!root) return;
  try {
    const data=await api("/api/skill-drafts");
    root.innerHTML=`<h3>我的草稿</h3><p>从需求或当前对话提炼方法，先试用，再提交管理控制台审批。草稿仅自己可见。</p>
      <div class="draft-actions"><button id="draft-generate" class="secondary">描述需求生成</button><button id="draft-extract" class="secondary">总结当前对话</button><button id="draft-import" class="secondary">导入草稿</button></div>
      <label class="draft-auto"><input type="checkbox" id="draft-auto" ${data.preferences.value.auto_draft?"checked":""}> 自动沉淀有价值的交互（仅私有草稿，不自动发布）</label>
      <div id="skill-workbench-home"></div><p id="draft-error" role="status"></p><div id="draft-list"></div><div id="draft-detail"></div><h3>可用助手</h3>`;
    $("#draft-generate").onclick=()=>{$("#skill-generate-form").hidden=false;$("#skill-generate-material").focus();};
    renderSkillWorkbenchHome(data);
    $("#draft-extract").onclick=()=>draftChat("把刚才的过程做成助手草稿");
    $("#draft-auto").onchange=async e=>{try { await draftAction("preferences",{value:{auto_draft:e.target.checked},expected_revision:data.preferences.revision}); await loadSkillDrafts(); } catch(err){$("#draft-error").textContent=err.message;e.target.checked=!e.target.checked;} };
    $("#draft-import").onclick=()=>{
      const input=document.createElement("input");input.type="file";input.accept="application/json,.json";
      input.onchange=async()=>{try {const f=input.files[0];if(!f)return;if(f.size>100000)throw new Error("文件不能超过100KB");const v=JSON.parse(await f.text());if(v.format==="agentskills.files.v1")await draftAction("import_standard",{package:v,request_key:crypto.randomUUID()});else if(v.format==="agentbridge.skill-draft.v1")await draftAction("save",{proposal:v.proposal,request_key:crypto.randomUUID(),provenance:{kind:"import"}});else throw new Error("不支持的草稿格式");await loadSkillDrafts();}catch(err){$("#draft-error").textContent=err.message;}};input.click();
    };
    const list=$("#draft-list");
    if(!data.items.length)list.textContent="还没有草稿。可以直接在对话中说：做个周报助手。";
    for(const d of data.items){const b=document.createElement("button");b.className="secondary";b.textContent=`${d.name} · 修订 ${d.revision} · ${draftStatus[d.review_state||d.state]||d.state}${d.published_version?" · 线上 "+d.published_version:" · 尚未发布"}`;b.onclick=()=>showSkillDraft(d.draft_id);list.append(b);}
  } catch(e){root.textContent=e.message;}
}
async function showSkillDraft(id) {
  const root=$("#draft-detail");
  try {
    const d=await api("/api/skill-drafts?id="+encodeURIComponent(id));const m=d.bundle.manifest;
    root.innerHTML=`<form id="draft-editor" class="draft-editor"><h3>${escapeHtml(m.name)}</h3><p>修订 ${d.revision} · 来源：${escapeHtml(d.provenance.kind)} · 仅保存方法，勿包含业务原文或凭据。</p>
      <label>名称<input name="name" required maxlength="120" value="${escapeHtml(m.name)}"></label>
      <label>简介<textarea name="description" required maxlength="500">${escapeHtml(m.description)}</textarea></label>
      ${[["use_when","适用场景"],["not_for","不适用场景"],["output","输出结果"]].map(([k,t])=>`<label>${t}<textarea name="${k}" required maxlength="500">${escapeHtml(m.selection[k])}</textarea></label>`).join("")}
      <label>处理方法<textarea name="instructions" required maxlength="40000" rows="12">${escapeHtml(d.bundle.resources["SKILL.md"])}</textarea></label>
      <div class="draft-actions"><button type="submit" ${d.state==="archived"?"disabled":""}>保存新修订</button><button type="button" id="draft-test" class="secondary">对话试用</button><button type="button" id="draft-export" class="secondary">导出</button><button type="button" id="draft-archive" class="secondary">归档</button></div></form>
      <div class="draft-actions"><label>历史修订<select id="draft-revision">${d.revisions.map(v=>`<option value="${v.revision}">修订 ${v.revision}</option>`).join("")}</select></label><button type="button" id="draft-restore" class="secondary">恢复为新草稿修订</button></div><p>恢复不影响已发布版本；重新试用并审批后才会生效。</p>
      <h4>样例试运行</h4><p>以下为模型样例，未经独立验证，需人工核对。</p>
      ${d.tests.map(t=>`<details><summary>修订 ${t.revision} · ${escapeHtml(t.profile)} · ${escapeHtml(t.status)}</summary><pre>${escapeHtml(t.prompt)}\n${escapeHtml(t.output||"等待样例结果")}</pre></details>`).join("")||"暂无样例"}
      <div id="skill-quality-panel"></div><form id="draft-submit" class="draft-editor"><h4>申请发布当前修订</h4><fieldset id="skill-recipients"><legend>使用范围（默认仅自己）</legend></fieldset><label>发布说明<textarea name="reason" required maxlength="1000"></textarea></label><button type="submit">提交审批</button></form>
      <h4>发布申请</h4><div id="draft-requests"></div><p id="draft-detail-error" role="status"></p>`;
    const error=e=>{$("#draft-detail-error").textContent=e.message;};
    $("#draft-editor").onsubmit=async e=>{e.preventDefault();try{const f=new FormData(e.target);const exported=await draftAction("export",{draft_id:id});const proposal={...exported.proposal,name:f.get("name"),description:f.get("description"),instructions:f.get("instructions"),selection:Object.fromEntries(["use_when","not_for","output"].map(k=>[k,f.get(k)]))};await draftAction("save",{draft_id:id,expected_revision:d.revision,request_key:crypto.randomUUID(),proposal,provenance:{kind:"revision"}});await loadSkillDrafts();await showSkillDraft(id);}catch(e){error(e);}};
    $("#draft-restore").onclick=async()=>{try{await draftAction("restore",{draft_id:id,expected_revision:d.revision,target_revision:Number($("#draft-revision").value),request_key:crypto.randomUUID()});await loadSkillDrafts();await showSkillDraft(id);}catch(e){error(e);}};
    $("#draft-test").onclick=()=>draftChat(`试用“${m.name}”草稿（${id}），用一个简短的合成例子`);
    $("#draft-export").onclick=async()=>{try{const result=await draftAction("export",{draft_id:id});const url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download="skill-draft.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){error(e);}};
    $("#draft-archive").onclick=async()=>{try{await draftAction("archive",{draft_id:id,expected_revision:d.revision});await loadSkillDrafts();}catch(e){error(e);}};
    $("#draft-submit").onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const audience=f.getAll("audience");try{await draftAction("submit",{draft_id:id,expected_revision:d.revision,request_key:crypto.randomUUID(),reason:f.get("reason"),...(audience.length?{audience}:{})});await showSkillDraft(id);}catch(e){error(e);}};
    await renderSkillQuality(d);
    for(const r of d.requests){const p=document.createElement("p");p.textContent=`修订 ${r.draft_revision} · ${draftStatus[r.state]||r.state} ${r.published_version||""} ${r.decision_reason||""}`;if(r.state==="submitted"){const b=document.createElement("button");b.textContent="撤回申请";b.onclick=async()=>{try{await draftAction("withdraw",{request_id:r.request_id});await showSkillDraft(id);}catch(e){error(e);}};p.append(b);}$("#draft-requests").append(p);}
  } catch(e){root.textContent=e.message;}
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
}

function skillReportHtml(report) {
  if (!report) return '<p>尚无独立评测。快速试用仅供人工复核。</p>';
  const names={candidate:'候选版本',published:'线上版本',without_skill:'不使用助手'};
  return `<p><strong>${report.passed?'所列断言通过':'存在未通过断言'}</strong> · 仍需人工检查语义正确性</p>
    <p>${escapeHtml(report.limitations)}</p>
    <div class="draft-actions">${Object.entries(report.scores).map(([k,v])=>`<span>${names[k]}：${v.passed}/${v.total}</span>`).join('')}</div>
    ${report.rows.map(r=>`<details><summary>${names[r.variant]} · ${escapeHtml(r.prompt||'不支持的模式')} · ${r.passed?'通过':'未通过'}</summary><pre>${escapeHtml(r.output||r.skipped)}</pre><p>${escapeHtml(r.model||'')} · ${r.elapsed_ms||0} 毫秒</p>${(r.checks||[]).map(c=>`<p>${c.passed?'✓':'×'} ${c.rule==='contains'?'应包含':c.rule==='excludes'?'不应包含':'触发判断'}：${escapeHtml(c.value||'')}</p>`).join('')}</details>`).join('')}`;
}

function renderSkillWorkbenchHome(data) {
  const root=$('#skill-workbench-home');
  const selected=data.scopes?.items.some(x=>x.scope===data.scope&&x.enabled);
  root.innerHTML=`<label class="draft-auto"><input id="skill-auto-scope" type="checkbox" ${selected?'checked':''}>允许从此工作台的新交互后台提炼（还需开启上方总开关）</label>
    <form id="skill-generate-form" class="draft-editor" hidden><label>想生成什么助手？<textarea id="skill-generate-material" required maxlength="16000" placeholder="做个周报助手，只整理我给的文字。"></textarea></label><button>后台生成草稿</button><p>可填写需求或粘贴选定过程。方法保存为私有草稿；偏好和事实单独提示。</p></form>
    <details><summary>后台生成与评测</summary><button id="skill-refresh-jobs" type="button" class="secondary">刷新任务</button><div id="skill-jobs"></div></details>`;
  const fail=e=>{$('#draft-error').textContent=e.message;};
  $('#skill-auto-scope').onchange=async e=>{try{await draftAction('scopes',{scope:data.scope,enabled:e.target.checked});}catch(err){e.target.checked=!e.target.checked;fail(err);}};
  $('#skill-generate-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{await draftAction('generate',{material:$('#skill-generate-material').value,request_key:crypto.randomUUID()});$('#draft-error').textContent='已加入后台生成队列，可继续其他工作。';await refreshSkillJobs();}catch(err){fail(err);}finally{button.disabled=false;}};
  $('#skill-refresh-jobs').onclick=()=>refreshSkillJobs().catch(fail);
  renderSkillJobs(data.jobs?.items||[]);
}

async function refreshSkillJobs(){const data=await draftAction('jobs');renderSkillJobs(data.items);}
function renderSkillJobs(items) {
  const root=$('#skill-jobs');if(!root)return;
  clearTimeout(state.skillJobTimer);
  if(state.activeView==='skills'&&items.some(j=>['queued','running'].includes(j.state)))state.skillJobTimer=setTimeout(()=>refreshSkillJobs().catch(e=>{$('#draft-error').textContent=e.message;}),3000);
  const labels={queued:'排队中',running:'执行中',succeeded:'已完成',failed:'失败',canceled:'已取消'};
  const kinds={method:'可复用方法',preference:'个人偏好',fact:'业务事实',none:'无需沉淀'};
  root.innerHTML=items.map(j=>`<article class="skill-job"><p><strong>${j.kind==='evaluation'?'独立评测':'方法提炼'}</strong> · ${labels[j.state]}${j.kind==='evaluation'?` · 已完成 ${j.progress} 项`:''}</p>
    ${j.result?`<p>${escapeHtml(kinds[j.result.classification]||'')} ${escapeHtml(j.result.summary||j.result.message||'')}</p>`:''}
    ${j.error?`<p role="status">${escapeHtml(j.error)}</p>`:''}
    ${j.result?.draft_id?`<button type="button" class="secondary" data-open-draft="${escapeHtml(j.result.draft_id)}">查看草稿</button>`:''}
    ${(j.result?.similar||[]).map(x=>`<button type="button" class="secondary" data-open-draft="${escapeHtml(x.draft_id)}">相似草稿：${escapeHtml(x.name)}</button>`).join('')}
    ${j.result?.candidate?`<details><summary>查看候选方法并采用</summary><pre>${escapeHtml(j.result.candidate.instructions)}</pre><button type="button" class="secondary" data-adopt-job="${j.job_id}">另存为新草稿</button>${(j.result.similar||[]).map(x=>`<button type="button" class="secondary" data-adopt-job="${j.job_id}" data-adopt-target="${escapeHtml(x.draft_id)}">更新 ${escapeHtml(x.name)}</button>`).join('')}</details>`:''}
    ${['queued','running'].includes(j.state)?`<button type="button" class="secondary" data-job-action="cancel_job" data-job="${j.job_id}">取消</button>`:''}
    ${j.state==='failed'&&j.attempts<3?`<button type="button" class="secondary" data-job-action="retry_job" data-job="${j.job_id}">重试</button>`:''}</article>`).join('')||'<p>暂无后台任务。</p>';
  root.querySelectorAll('[data-open-draft]').forEach(b=>b.onclick=()=>showSkillDraft(b.dataset.openDraft));
  root.querySelectorAll('[data-adopt-job]').forEach(b=>b.onclick=async()=>{try{const target=b.dataset.adoptTarget;const d=target?await draftAction('get',{draft_id:target}):null;const saved=await draftAction('adopt',{job_id:b.dataset.adoptJob,request_key:crypto.randomUUID(),...(d?{draft_id:target,expected_revision:d.revision}:{})});await loadSkillDrafts();await showSkillDraft(saved.draft_id);}catch(e){$('#draft-error').textContent=e.message;}});
  root.querySelectorAll('[data-job-action]').forEach(b=>b.onclick=async()=>{try{await draftAction(b.dataset.jobAction,{job_id:b.dataset.job});await refreshSkillJobs();}catch(e){$('#draft-error').textContent=e.message;}});
}

async function renderSkillQuality(d) {
  const [quality, recipients]=await Promise.all([draftAction('inspect',{draft_id:d.draft_id}),draftAction('recipients')]);
  const root=$('#skill-quality-panel');if(!root)return;
  root.innerHTML=`<h4>方法检查与版本对照</h4><p>当前草稿修订 ${d.revision} · ${quality.published_version?'线上 '+escapeHtml(quality.published_version):'尚未发布'}</p>
    <p>${escapeHtml(quality.diagnostics.message)}</p>${quality.diagnostics.checks.map(c=>`<p>${c.level==='warning'?'待完善':'已识别'}：${escapeHtml(c.message)}</p>`).join('')}
    <details><summary>与线上版本的差异（${quality.diff.length} 项）</summary>${quality.diff.map(c=>`<p>${escapeHtml(c.field)}</p><pre>${escapeHtml(c.diff??JSON.stringify({原来:c.before,现在:c.after},null,2))}</pre>`).join('')}</details>
    <button id="skill-standard-export" class="secondary" type="button">导出标准 Skill 文件包</button>
    <h4>独立评测</h4><p>仅使用合成文字，无业务工具。输入简短问题及可检查的期望；断言不会提供给被测模型。</p>
    ${skillReportHtml(quality.evaluation)}
    <form id="skill-eval-form" class="draft-editor"><div id="skill-eval-cases"></div><div class="draft-actions"><button id="skill-add-case" type="button" class="secondary">添加样本</button><label>每项重复<select name="repeats"><option>1</option><option>2</option><option>3</option></select></label><button>启动独立评测</button></div></form>`;
  $('#skill-recipients').innerHTML='<legend>使用范围（默认仅自己）</legend>'+recipients.items.map((x,i)=>`<label><input type="checkbox" name="audience" value="${escapeHtml(x.subject)}" ${i===0?'checked':''}>${escapeHtml(x.name)}</label>`).join('');
  const add=()=>{
    const cases=$('#skill-eval-cases');if(cases.children.length>=12)return;
    const node=document.createElement('fieldset');node.innerHTML=`<legend>样本 ${cases.children.length+1}</legend><label>问题<textarea name="prompt" required maxlength="3000" placeholder="整理成周报：计划修复登录问题。"></textarea></label>
      <label>模式<select name="profile">${Object.keys(d.bundle.manifest.profiles).map(p=>`<option>${escapeHtml(p)}</option>`).join('')}</select></label>
      <label>测试内容<select name="kind"><option value="output">回答质量</option><option value="trigger_true">应该使用此助手</option><option value="trigger_false">不应使用此助手</option></select></label>
      <label>应包含（每行一项）<textarea name="contains" placeholder="计划"></textarea></label><label>不应包含（每行一项）<textarea name="excludes" placeholder="已修复"></textarea></label><button type="button" class="secondary">移除此样本</button>`;
    node.querySelector('button').onclick=()=>node.remove();cases.append(node);
  };
  add();$('#skill-add-case').onclick=add;
  $('#skill-eval-form').onsubmit=async e=>{e.preventDefault();try{const cases=[...$('#skill-eval-cases').children].map(n=>{const kind=n.querySelector('[name=kind]').value;return {prompt:n.querySelector('[name=prompt]').value,profile:n.querySelector('[name=profile]').value,kind:kind==='output'?'output':'trigger',...(kind==='output'?{}:{expected_trigger:kind==='trigger_true'}),...Object.fromEntries(['contains','excludes'].map(k=>[k,n.querySelector(`[name=${k}]`).value.split('\n').map(v=>v.trim()).filter(Boolean)]))};});await draftAction('evaluate',{draft_id:d.draft_id,expected_revision:d.revision,cases,repeats:Number(new FormData(e.target).get('repeats')),request_key:crypto.randomUUID()});$('#draft-detail-error').textContent='评测已排队，请在后台任务查看进度；完成后重新打开草稿查看对照。';await refreshSkillJobs();}catch(err){$('#draft-detail-error').textContent=err.message;}};
  $('#skill-standard-export').onclick=async()=>{try{const pack=await draftAction('export_standard',{draft_id:d.draft_id});const url=URL.createObjectURL(new Blob([JSON.stringify(pack,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='skill-files.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){$('#draft-detail-error').textContent=e.message;}};
}

function addSkillFeedback(card,item) {
  const details=document.createElement('details');details.innerHTML=`<summary>反馈此版本的效果</summary><p>版本 ${escapeHtml(item.version)} · 反馈仅用于改进方法</p><select aria-label="效果评价"><option value="useful">有帮助</option><option value="incorrect">结果有误</option><option value="not_applicable">不适用</option></select><textarea aria-label="纠正说明" maxlength="1000" placeholder="哪里需要改进？"></textarea><button type="button" class="secondary">保存反馈</button><p role="status"></p>`;
  details.querySelector('button').onclick=async()=>{try{await draftAction('feedback',{skill_id:item.id,version:item.version,profile:Object.keys(item.profiles)[0],rating:details.querySelector('select').value,comment:details.querySelector('textarea').value});details.querySelector('[role=status]').textContent='已保存反馈。';}catch(e){details.querySelector('[role=status]').textContent=e.message;}};
  card.append(details);
}
