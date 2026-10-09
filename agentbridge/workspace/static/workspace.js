import { createLifecycle, isCancellation } from "./workspace_lifecycle.mjs";
import { createWorkspaceRequests } from "./workspace_request.mjs";
import { createResultView, formatTime, parseTimestampMilliseconds, textHash, historyMessageKey, endpointType } from "./workspace_results.mjs";
import { createTaskCards } from "./workspace_cards.mjs";
import { createChatProgress } from "./workspace_progress.mjs";
import { createChatStream } from "./workspace_stream.mjs";
import { createComposer } from "./workspace_forms.mjs";
import { createSkillForms } from "./workspace_skills.mjs";

const state = {
  account: null,
  activeView: "chat",
  tasks: [],
  selectedTaskId: null,
  taskDetailRequest: 0,
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

const GATEWAY_STATUS_ONLINE_POLL_MS = 30000;
const GATEWAY_STATUS_OFFLINE_POLL_MS = 5000;
const CLIENT_VERSION =
  document.querySelector('meta[name="agentbridge-workspace-version"]')
    ?.content || "";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const lifecycle = createLifecycle();
let sessionScope = lifecycle.scope();
let viewScope = sessionScope.child();
const { api, fetchChatStreamResponse } = createWorkspaceRequests({
  document, getScope: () => sessionScope,
  addLiveProgress: (...args) => addLiveProgress(...args),
});
const { renderMarkdown, groupQueryCards, renderTaskPlan, appendArtifactList } =
  createResultView({ document, state, reissueArtifact });
const { ensureLiveMessage, adoptLiveMessage, handleChatProgress, addLiveProgress,
  handleChatDelta, restoreActiveDispatches } = createChatProgress({
  document, state, $, scrollChat, scheduleChatRefresh, renderMarkdown, attachDispatchCancel,
});
const { consumeChatStream } = createChatStream({
  state, getScope: () => sessionScope, fetchChatStreamResponse, adoptLiveMessage,
  addLiveProgress, ensureLiveMessage, handleChatProgress, handleChatDelta,
  attachDispatchCancel, agentFailureMessage, renderRunFailure,
});
const { upsertTaskCard, renderTasks, renderTaskDetail, renderSkillCard } = createTaskCards({
  document, state, $, renderChatTimeline, scrollChat, setTimelineNode,
  loadTaskDetail, switchView, continueTask, emptyState, appendArtifactList, renderTaskPlan,
});
const { handleComposerPaste, handleComposerDragOver, handleComposerDragLeave,
  handleComposerDrop, addComposerFiles, clearComposerAttachments, sendChat } = createComposer({
  document, state, $, toast, getScope: () => sessionScope, executeChatMessage,
});
const { loadSkillDrafts, addSkillFeedback } = createSkillForms({
  document, state, $, api, switchView, getScope: () => viewScope,
});

document.addEventListener("DOMContentLoaded", bootstrap);
window.addEventListener("pagehide", () => {
  stopWorkspaceObservers();
  sessionScope.dispose();
});
window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  if (state.account) enterWorkspace(state.account);
  else {
    sessionScope = lifecycle.scope();
    viewScope = sessionScope.child();
    restoreEnrollment();
  }
});

function scopedApi(scope) {
  return (path, options = {}) => api(path, { ...options, scope });
}

function resetWorkspaceState() {
  for (const name of ["liveMessages", "activeStreams", "historyMessages", "localMessages",
    "syncedMessages", "taskCards", "skillCards", "taskCardMeta", "queryGroupOpen", "taskSyncTimers", "toasts"]) {
    state[name].clear();
  }
  state.tasks = [];
  state.selectedTaskId = null;
  state.taskDetailRequest += 1;
  state.composerAttachments = [];
  state.timelineCursor = 0;
  state.chatTimelineOldestSequence = 0;
  state.chatTimelineHasOlder = false;
  state.chatTimelineLoadingOlder = false;
  state.gatewayStatusCheckActive = false;
  for (const selector of ["#chat-messages", "#task-list", "#task-detail", "#endpoint-list",
    "#skill-list", "#skill-authoring", "#composer-attachments", "#toast-region"]) $(selector).replaceChildren();
  $("#chat-form textarea[name='message']").value = "";
  $("#composer-attachments").hidden = true;
  setBusy($("#chat-form"), false);
  closeImageViewer();
}

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
    if (isCancellation(error)) return;
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
    const input = event.currentTarget;
    await addComposerFiles(input.files);
    input.value = "";
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
    if (isCancellation(error)) return;
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
    if (isCancellation(error)) return;
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
  lifecycle.clearTimer(state.enrollmentTimer);
  state.enrollmentTimer = sessionScope.interval(async () => {
    try {
      const result = await api("/api/enrollment/status");
      if (result.state === "confirmed") {
        lifecycle.clearTimer(state.enrollmentTimer);
        renderEnrollmentConfirmed();
      } else if (["expired", "consumed"].includes(result.state)) {
        lifecycle.clearTimer(state.enrollmentTimer);
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
    if (isCancellation(error)) return;
    showAuthError(friendlyError(error));
  } finally {
    setBusy(enrollmentForm, false);
  }
}

function enterWorkspace(account) {
  stopWorkspaceObservers();
  sessionScope.dispose();
  sessionScope = lifecycle.scope();
  viewScope = sessionScope.child();
  const accountScope = sessionScope;
  resetWorkspaceState();
  state.account = account;
  $("#auth-view").hidden = true;
  $("#app-view").hidden = false;
  $("#account-name").textContent = account.username;
  switchView("chat");
  loadTasks();
  loadChat().finally(() => {
    if (!accountScope.current()) return;
    openTimelineStream();
    startWorkspaceObservers();
    loadGatewayStatus();
  });
}

async function logout() {
  stopWorkspaceObservers();
  sessionScope.dispose();
  sessionScope = lifecycle.scope();
  viewScope = sessionScope.child();
  state.account = null;
  resetWorkspaceState();
  showAuth();
  setBusy($("#login-form"), true);
  setBusy($("#enroll-complete"), true);
  try {
    await api("/api/logout", { method: "POST", body: {}, csrf: true });
  } catch {}
  location.reload();
}

function switchView(view) {
  if (view !== state.activeView) {
    viewScope.dispose();
    viewScope = sessionScope.child();
  }
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
  const scope = viewScope.replace("skills-list");
  const api = scopedApi(scope);
  await loadSkillDrafts();
  if (!scope.current()) return;
  const container = $("#skill-list");
  container.replaceChildren();
  try {
    const result = await api("/api/skills");
    scope.assertCurrent();
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
  } catch (error) {
    if (isCancellation(error)) return; if (isCancellation(error)) return; container.textContent = "业务助手暂时无法加载，请稍后刷新。"; }
}

async function loadGatewayStatus() {
  const scope = sessionScope;
  const api = scopedApi(scope);
  if (
    !state.account ||
    !state.gatewayStatusPolling ||
    state.gatewayStatusCheckActive
  ) {
    return;
  }
  const element = $("#gateway-state");
  const dot = element.querySelector(".status-dot");
  lifecycle.clearTimer(state.gatewayStatusTimer);
  state.gatewayStatusTimer = null;
  state.gatewayStatusCheckActive = true;
  let available = false;
  try {
    const result = await api("/api/gateway");
    scope.assertCurrent();
    available = Boolean(result.available);
    dot.className = `status-dot ${available ? "online" : "offline"}`;
    element.title = available
      ? `OpenClaw ${result.version || "已连接"}`
      : `OpenClaw 不可用：${result.code}`;
  } catch {
    if (!scope.current()) return;
    dot.className = "status-dot offline";
  } finally {
    if (!scope.current()) return;
    state.gatewayStatusCheckActive = false;
    if (state.account && state.gatewayStatusPolling) {
      state.gatewayStatusTimer = sessionScope.timeout(
        loadGatewayStatus,
        available
          ? GATEWAY_STATUS_ONLINE_POLL_MS
          : GATEWAY_STATUS_OFFLINE_POLL_MS,
      );
    }
  }
}

async function loadChat() {
  const scope = sessionScope.replace("chat-load");
  const api = scopedApi(scope);
  const container = $("#chat-messages");
  try {
    const [history, taskTimeline, chatTimeline, dispatches] = await Promise.all([
      api("/api/chat/history?limit=120"),
      api("/api/timeline?entry_type=task_event&limit=240"),
      api("/api/timeline?entry_type=chat_message&limit=500"),
      api("/api/chat/dispatches?active_only=true&limit=20"),
    ]);
    scope.assertCurrent();
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
    await hydrateTaskCards({ render: false, scope });
    scope.assertCurrent();
    renderChatTimeline();
    container.scrollTop = container.scrollHeight;
  } catch (error) {
    if (isCancellation(error)) return;
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
  const scope = sessionScope;
  const api = scopedApi(scope);
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
    scope.assertCurrent();
    const items = Array.isArray(result.items) ? result.items : [];
    items.forEach((entry) => ingestTimelineEntry(entry, false));
    const oldest = Number(result.oldestSequence) || 0;
    if (oldest) state.chatTimelineOldestSequence = oldest;
    state.chatTimelineHasOlder = Boolean(result.hasMore);
  } catch (error) {
    if (isCancellation(error)) return;
    toast(`加载更早消息失败：${friendlyError(error)}`, true);
  } finally {
    if (!scope.current()) return;
    state.chatTimelineLoadingOlder = false;
    renderChatTimeline();
    container.scrollTop = previousTop + container.scrollHeight - previousHeight;
  }
}

async function executeChatMessage(
  message,
  form = $("#chat-form"),
  attachments = [],
) {
  const scope = sessionScope;
  const api = scopedApi(scope);
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
    scope.assertCurrent();
    scheduleChatRefresh(500, 1);
  } catch (error) {
    if (isCancellation(error)) return;
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
    if (!scope.current()) return;
    setBusy(form, false);
    form.elements.message?.focus();
  }
}

function scheduleChatRefresh(delay, attempts) {
  const scope = sessionScope;
  lifecycle.clearTimer(state.chatTimer);
  state.chatTimer = sessionScope.timeout(async () => {
    await loadChat();
    if (scope.current() && attempts > 1) scheduleChatRefresh(1800, attempts - 1);
  }, delay);
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
    if (isCancellation(error)) return;
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

async function hydrateSkillCards({ render = true, scope = sessionScope } = {}) {
  const api = scopedApi(scope);
  const account = state.account;
  try {
    const result = await api("/api/skills/history");
    if (!scope.current() || state.account !== account) return;
    for (const event of result.items || []) {
      if (!state.skillCards.has(event.event_id)) {
        state.skillCards.set(event.event_id, renderSkillCard(event));
      }
    }
    if (render) renderChatTimeline();
  } catch { /* Keep existing evidence; absence of a response is not a failed Skill. */ }
}

async function hydrateTaskCards({ render = true, scope = sessionScope } = {}) {
  const api = scopedApi(scope);
  await hydrateSkillCards({ render: false, scope });
  if (!scope.current()) return;
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
    scope.assertCurrent();
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
  lifecycle.clearTimer(state.taskSyncTimers.get(taskId));
  state.taskSyncTimers.set(
    taskId,
    sessionScope.timeout(async () => {
      state.taskSyncTimers.delete(taskId);
      await syncTaskCard(taskId);
    }, 220),
  );
  lifecycle.clearTimer(state.taskListTimer);
  state.taskListTimer = sessionScope.timeout(loadTasks, 260);
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
  const detailRequest = state.selectedTaskId === taskId ? ++state.taskDetailRequest : null;
  const scope = sessionScope.replace(`task:${taskId}`);
  const api = scopedApi(scope);
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    scope.assertCurrent();
    upsertTaskCard(result);
    scope.assertCurrent();
    if (state.selectedTaskId === taskId && detailRequest === state.taskDetailRequest) renderTaskDetail(result);
  } catch (error) {
    if (isCancellation(error)) return;
    if (error.code !== "TASK_NOT_FOUND") {
      toast(
        friendlyError(error),
        true,
        `task:${taskId}:${error.code || "sync"}`,
      );
    }
  }
}

async function loadTasks() {
  const scope = sessionScope.replace("task-list");
  const api = scopedApi(scope);
  try {
    const activeOnly = $("#active-only").checked;
    const result = await api(
      `/api/tasks?active_only=${activeOnly}&limit=150`,
    );
    scope.assertCurrent();
    state.tasks = result.items;
    renderTasks();
    const active = result.items.filter((task) =>
      ["active", "waiting_user", "running"].includes(task.status),
    ).length;
    $("#active-task-count").textContent = String(active);
  } catch (error) {
    if (isCancellation(error)) return;
    toast(friendlyError(error), true);
  }
}

async function loadTaskDetail(taskId) {
  const scope = viewScope.replace("task-detail");
  const api = scopedApi(scope);
  state.selectedTaskId = taskId;
  const detailRequest = ++state.taskDetailRequest;
  renderTasks();
  const detail = $("#task-detail");
  detail.classList.remove("mobile-empty");
  detail.replaceChildren(emptyState("正在读取任务"));
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    scope.assertCurrent();
    if (state.selectedTaskId === taskId && detailRequest === state.taskDetailRequest) renderTaskDetail(result);
  } catch (error) {
    if (isCancellation(error)) return;
    if (detailRequest === state.taskDetailRequest) detail.replaceChildren(emptyState(friendlyError(error)));
  }
}

async function continueTask(task, button) {
  const scope = sessionScope;
  const api = scopedApi(scope);
  if (!task?.task_id || button.disabled) return;
  button.disabled = true;
  try {
    const result = await api(
      `/api/tasks/${encodeURIComponent(task.task_id)}/continue`,
      { method: "POST", body: {}, csrf: true },
    );
    scope.assertCurrent();
    switchView("chat");
    await executeChatMessage(result.message);
  } catch (error) {
    if (isCancellation(error)) return;
    toast(friendlyError(error), true, `task:${task.task_id}:continue`);
  } finally {
    button.disabled = false;
  }
}

async function loadEndpoints() {
  const scope = viewScope.replace("endpoints");
  const api = scopedApi(scope);
  const body = $("#endpoint-list");
  try {
    const result = await api("/api/endpoints");
    scope.assertCurrent();
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
    if (isCancellation(error)) return;
    toast(friendlyError(error), true);
  }
}

function startWorkspaceObservers() {
  lifecycle.clearTimer(state.timelineReconcileTimer);
  lifecycle.clearTimer(state.clientVersionTimer);
  lifecycle.clearTimer(state.gatewayStatusTimer);
  state.gatewayStatusPolling = true;
  state.timelineReconcileTimer = sessionScope.interval(reconcileTimeline, 10000);
  state.clientVersionTimer = sessionScope.interval(checkClientVersion, 30000);
  loadGatewayStatus();
}

function stopWorkspaceObservers() {
  state.eventSource?.close();
  state.eventSource = null;
  lifecycle.clearTimer(state.timelineReconnectTimer);
  lifecycle.clearTimer(state.gatewayStatusTimer);
  lifecycle.clearTimer(state.timelineReconcileTimer);
  lifecycle.clearTimer(state.clientVersionTimer);
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
  const scope = sessionScope;
  const api = scopedApi(scope);
  if (!state.account || state.timelineReconcileActive) return;
  state.timelineReconcileActive = true;
  try {
    for (let page = 0; page < 3; page += 1) {
      const after = Math.max(Number(state.timelineCursor) || 0, 0);
      const result = await api(
        `/api/timeline?after=${encodeURIComponent(after)}&limit=200`,
      );
      scope.assertCurrent();
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
    if (!scope.current()) return;
    state.timelineReconcileActive = false;
    await hydrateSkillCards({ scope });
  }
}

async function checkClientVersion() {
  const scope = sessionScope;
  const api = scopedApi(scope);
  if (!CLIENT_VERSION || state.clientVersionCheckActive) return;
  state.clientVersionCheckActive = true;
  try {
    const result = await api("/api/client-version");
    scope.assertCurrent();
    const runActive = state.activeStreams.size > 0;
    if (result.version && result.version !== CLIENT_VERSION && !runActive) {
      location.reload();
    }
  } catch {
  } finally {
    if (!scope.current()) return;
    state.clientVersionCheckActive = false;
  }
}

function openTimelineStream() {
  if (!state.account) return;
  const scope = sessionScope.replace("timeline-stream");
  lifecycle.clearTimer(state.timelineReconnectTimer);
  state.timelineReconnectTimer = null;
  state.eventSource?.close();
  const query = state.timelineCursor
    ? `?after=${encodeURIComponent(state.timelineCursor)}`
    : "";
  const source = new EventSource(`/api/timeline/stream${query}`);
  scope.defer(() => source.close());
  source.addEventListener("cursor", (event) => {
    if (!scope.current() || state.eventSource !== source) return;
    state.timelineCursor = Math.max(
      state.timelineCursor,
      Number(event.lastEventId) || 0,
    );
  });
  source.addEventListener("timeline", (event) => {
    if (!scope.current() || state.eventSource !== source) return;
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
    if (!scope.current() || state.eventSource !== source) return;
    source.close();
    state.eventSource = null;
    lifecycle.clearTimer(state.timelineReconnectTimer);
    state.timelineReconnectTimer = sessionScope.timeout(openTimelineStream, 5000);
  };
  state.eventSource = source;
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
  if (isCancellation(error)) return "";
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
    INVALID_REQUEST: "请求未能提交，请检查图片格式、大小和文字长度。",
    WORKSPACE_INTERNAL_ERROR: "请求提交异常，请刷新查看请求状态；系统不会自动重新发送。",
    WORKSPACE_STREAM_FAILED: "结果连接中断，请刷新查看请求状态；系统不会自动重新发送。",
    WORKSPACE_STREAM_INCOMPLETE: "未收到完整处理结果，请刷新查看请求状态；系统不会自动重新发送。",
  };
  return labels[error.code] || error.message || "操作没有完成。";
}

function toast(message, isError = false, key = message) {
  if (!message) return;
  const normalizedKey = String(key || message);
  let item = state.toasts.get(normalizedKey);
  if (!item?.isConnected) {
    item = document.createElement("div");
    state.toasts.set(normalizedKey, item);
    $("#toast-region").append(item);
  }
  item.className = `toast${isError ? " error" : ""}`;
  item.textContent = message;
  lifecycle.clearTimer(item.dismissTimer);
  while ($("#toast-region").childElementCount > 2) {
    const oldest = $("#toast-region").firstElementChild;
    state.toasts.forEach((value, toastKey) => {
      if (value === oldest) state.toasts.delete(toastKey);
    });
    oldest?.remove();
  }
  item.dismissTimer = sessionScope.timeout(() => {
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

async function reissueArtifact(taskId, artifactId, button) {
  const detailRequest = state.selectedTaskId === taskId ? ++state.taskDetailRequest : null;
  const scope = sessionScope;
  const api = scopedApi(scope);
  if (!taskId || !artifactId || button.disabled) return;
  button.disabled = true;
  button.textContent = "正在重新获取";
  try {
    const result = await api(
      `/api/tasks/${encodeURIComponent(taskId)}/artifacts/` +
        `${encodeURIComponent(artifactId)}/reissue`,
      { method: "POST", body: {}, csrf: true },
    );
    scope.assertCurrent();
    upsertTaskCard(result);
    scope.assertCurrent();
    if (state.selectedTaskId === taskId && detailRequest === state.taskDetailRequest) renderTaskDetail(result);
    toast(
      "新的下载链接已生成，30 分钟内有效。",
      false,
      `artifact:${artifactId}:reissued`,
    );
  } catch (error) {
    if (isCancellation(error)) return;
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
