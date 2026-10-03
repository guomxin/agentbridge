"use strict";

const state = { account: null, view: "overview", modalAction: null, coordinationTab: "tasks", governanceTab: "events", incidents: [], selectedIncident: null, detailRevision: 0, viewRevision: 0, readController: null };
const titles = {
  overview: ["CONTROL PLANE", "运行总览"],
  skillReviews: ["SKILL PUBLICATION", "助手发布审批"],
  users: ["IDENTITY", "用户与令牌"],
  databases: ["DATA SOURCES", "数据库"],
  sessions: ["DOWNSTREAM", "系统会话"],
  capabilities: ["GOVERNANCE", "能力与策略"],
  operations: ["EXECUTION", "操作记录"],
  interactions: ["TRUSTED UX", "可信交互"],
  traces: ["END TO END", "链路追踪"],
  incidents: ["RELIABILITY", "事件治理"],
  coordination: ["TASK HUB", "多端任务"],
  runtime: ["RUNTIME", "系统运行"],
  audit: ["ADMIN AUDIT", "管理审计"],
};
const statusText = {
  active: "有效", inactive: "未活动", revoked: "已撤销", expired: "已过期", quarantined: "已隔离",
  awaiting_login: "待登录", new: "未登录", succeeded: "成功", partially_succeeded: "部分成功", failed: "失败", completed: "已完成",
  validated: "已校验", queued: "等待执行", skipped: "已跳过",
  unknown: "结果未知", outcome_unknown: "结果未知", requires_user_action: "用户交互节点", waiting_user: "等待用户",
  awaiting_user: "当前等待用户", user_action_completed: "用户已处理", resumed: "已续办",
  user_action_expired: "交互已过期", user_action_rejected: "用户已拒绝", user_action_superseded: "已被替换",
  user_action_failed: "交互失败", user_action_handoff: "已转交用户",
  running: "执行中", canceled: "已取消", cancelled: "已取消", pending: "待处理", delivering: "投递中", deferred: "等待端点活动", acknowledged: "已送达",
  submitted: "已填写", approved: "已授权", rejected: "已拒绝", consumed: "已使用", superseded: "已替换",
  paused: "已暂停", available: "可用", selected: "已选择", awaiting_selection: "待选择",
  observe_only: "只读接续", resume: "恢复执行", follow_up: "后续操作", pull: "网页拉取", direct: "聊天直推",
  eligible: "保活中", outside_lease: "未保活（超期）", activity_unknown: "活动未知", not_configured: "未配置",
  ready: "同步就绪", waiting_activity: "等待微信活动",
  waiting: "等待中", open: "待处理", investigating: "调查中", resolved: "已解决", suppressed: "已抑制", archived: "已归档",
  healthy: "健康", unavailable: "不可用", meeting: "达标", breached: "未达标", insufficient_data: "数据不足",
};
const statusClass = value => ["active", "succeeded", "approved", "submitted", "completed", "acknowledged", "eligible", "selected", "available", "healthy", "meeting", "resolved", "archived"].includes(value) ? "ok" :
  ["failed", "unknown", "outcome_unknown", "expired", "quarantined", "revoked", "rejected", "user_action_failed", "unavailable", "breached"].includes(value) ? "bad" :
  ["pending", "partially_succeeded", "delivering", "deferred", "waiting_activity", "awaiting_login", "awaiting_user", "waiting_user", "waiting", "paused", "awaiting_selection", "outside_lease", "user_action_expired", "user_action_rejected", "open", "acknowledged", "investigating"].includes(value) ? "warn" :
  ["running", "resume", "follow_up", "pull", "direct"].includes(value) ? "info" : "neutral";
const $ = selector => document.querySelector(selector);
const content = $("#content");
const modal = $("#modal");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[character]);
}
function fmtTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? escapeHtml(value) : date.toLocaleString("zh-CN", { hour12: false });
}
function fmtBytes(value) {
  const size = Number(value || 0);
  if (!Number.isFinite(size) || size <= 0) return "--";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
function fmtDuration(value) {
  const duration = Number(value || 0);
  if (!Number.isFinite(duration) || duration <= 0) return "--";
  if (duration < 1000) return `${Math.round(duration)} ms`;
  if (duration < 60000) return `${(duration / 1000).toFixed(1)} 秒`;
  return `${(duration / 60000).toFixed(1)} 分`;
}
function fmtMetric(metric) {
  if (metric.value == null) return "--";
  if (metric.metricKey.endsWith("_rate") || metric.metricKey.endsWith("_coverage")) return `${(metric.value * 100).toFixed(1)}%`;
  if (metric.metricKey.endsWith("_ms")) return fmtDuration(metric.value);
  return Number(metric.value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}
function boundaryLabel(value) {
  const labels = { B0_NO_EFFECT: "B0 无副作用", B1_READ_ONLY: "B1 只读", B2_INTERACTION_CREATED: "B2 已创建交互", B3_PREPARED_AUTHORIZED: "B3 已准备/授权", B4_COMMIT_ATTEMPTED: "B4 已尝试提交", B5_VERIFIED: "B5 已权威核验" };
  return labels[value] || value || "--";
}
function shortId(value) { return value ? `${escapeHtml(value.slice(0, 8))}…` : "--"; }
function badge(value) { return `<span class="status ${statusClass(value)}">${escapeHtml(statusText[value] || value || "未知")}</span>`; }
function sessionStateBadge(session) {
  if (session.state === "active" && session.session_state_basis === "last_confirmed") {
    return `<span class="status warn" title="当前没有实时校验；这是最后一次确认成功时保存的状态">上次确认有效</span>`;
  }
  return badge(session.state);
}
function empty(message) { return `<div class="empty">${escapeHtml(message)}</div>`; }
function table(headers, rows, filterable = false) {
  if (!rows.length) return empty("暂无记录");
  return `<div class="table-shell"><table${filterable ? " data-filter-table" : ""}><thead><tr>${headers.map(item => `<th>${escapeHtml(item)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}
function filterRow(searchText, status, cells) {
  return `<tr data-filter-text="${escapeHtml(String(searchText).toLowerCase())}" data-filter-status="${escapeHtml(status || "")}">${cells}</tr>`;
}
function filteredTable(headers, rows, placeholder, statuses = []) {
  if (!rows.length) return empty("暂无记录");
  const options = statuses.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(statusText[value] || value)}</option>`).join("");
  return `<section class="data-block" data-filter-scope><div class="filters"><label class="filter-field"><span>搜索</span><input type="search" data-filter-search placeholder="${escapeHtml(placeholder)}"></label>${statuses.length ? `<label class="filter-field compact"><span>状态</span><select data-filter-status><option value="">全部状态</option>${options}</select></label>` : ""}<span class="filter-count" data-filter-count>${rows.length} 条</span></div>${table(headers, rows, true)}<div class="filter-empty hidden" data-filter-empty>没有匹配记录</div></section>`;
}
function applyFilter(control) {
  const scope = control.closest("[data-filter-scope]");
  if (!scope) return;
  const search = (scope.querySelector("[data-filter-search]")?.value || "").trim().toLowerCase();
  const status = scope.querySelector("[data-filter-status]")?.value || "";
  const category = scope.querySelector("[data-incident-category]")?.value || "";
  let visible = 0;
  scope.querySelectorAll("tbody tr").forEach(row => {
    const matches = (!search || row.dataset.filterText.includes(search)) && (!status || row.dataset.filterStatus === status) && (!category || row.dataset.incidentCategory === category);
    row.classList.toggle("hidden", !matches);
    if (matches) visible += 1;
  });
  const count = scope.querySelector("[data-filter-count]");
  if (count) count.textContent = `${visible} 条`;
  scope.querySelector("[data-filter-empty]")?.classList.toggle("hidden", visible !== 0);
  const selected = scope.querySelector('[data-incident-detail][aria-pressed="true"]');
  if (selected?.closest("tr").classList.contains("hidden")) {
    state.selectedIncident = null;
    state.detailRevision++;
    selected.setAttribute("aria-pressed", "false");
    selected.closest("tr").classList.remove("selected");
    $("#incident-detail").innerHTML = empty("选择筛选结果中的事件查看证据");
  }
}
function scopeBadges(scopes) { return `<div class="scope-pills">${scopes.map(scope => `<span>${escapeHtml(scope)}</span>`).join("")}</div>`; }
function rowMenuButton(label, items) {
  if (!items.length) return "--";
  return `<button type="button" class="row-menu-trigger" data-row-actions="${escapeHtml(JSON.stringify(items))}" aria-label="${escapeHtml(label)}操作" aria-haspopup="dialog" aria-expanded="false">操作 <span aria-hidden="true">⌄</span></button>`;
}
let activeMenuTrigger = null;
function closeRowActions(restoreFocus = false) {
  const menu = content.querySelector("#row-action-menu");
  if (menu?.matches(":popover-open")) menu.hidePopover();
  if (activeMenuTrigger) {
    activeMenuTrigger.setAttribute("aria-expanded", "false");
    if (restoreFocus && activeMenuTrigger.isConnected) activeMenuTrigger.focus();
  }
  activeMenuTrigger = null;
}
function openRowActions(trigger) {
  const wasOpen = activeMenuTrigger === trigger && content.querySelector("#row-action-menu")?.matches(":popover-open");
  closeRowActions(wasOpen);
  if (wasOpen) return;
  let menu = content.querySelector("#row-action-menu");
  if (!menu) {
    menu = document.createElement("div");
    menu.id = "row-action-menu";
    menu.className = "row-action-menu";
    menu.setAttribute("popover", "auto");
    menu.setAttribute("role", "dialog");
    menu.addEventListener("toggle", event => {
      if (event.newState === "closed" && !menu.matches(":popover-open")) closeRowActions();
    });
    content.append(menu);
  }
  const items = JSON.parse(trigger.dataset.rowActions);
  menu.setAttribute("aria-label", trigger.getAttribute("aria-label"));
  menu.innerHTML = items.map(item => `<button type="button" class="row-menu-item${item.danger ? " danger-text" : ""}" ${Object.entries(item.attrs).map(([key, value]) => `${key}="${escapeHtml(value)}"`).join(" ")}>${escapeHtml(item.label)}</button>`).join("");
  menu.showPopover();
  const anchor = trigger.getBoundingClientRect();
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  menu.style.left = `${Math.max(12, Math.min(anchor.right - width, window.innerWidth - width - 12))}px`;
  menu.style.top = `${anchor.bottom + height + 8 > window.innerHeight ? Math.max(12, anchor.top - height - 6) : anchor.bottom + 6}px`;
  activeMenuTrigger = trigger;
  trigger.setAttribute("aria-expanded", "true");
  trigger.setAttribute("aria-controls", menu.id);
  menu.querySelector("button")?.focus();
}
function csrfToken() {
  const prefix = "agentbridge_admin_csrf=";
  const item = document.cookie.split(";").map(value => value.trim()).find(value => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}
async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { ...(options.headers || {}) };
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["X-AgentBridge-CSRF"] = csrfToken();
  }
  const signal = method === "GET" ? state.readController?.signal : undefined;
  const response = await fetch(path, { signal, ...options, method, headers, credentials: "same-origin" });
  const data = await response.json().catch(() => ({}));
  signal?.throwIfAborted();
  if (!response.ok) {
    const error = new Error(data.error?.message || `请求失败 (${response.status})`);
    error.code = data.error?.code || "REQUEST_FAILED";
    error.status = response.status;
    throw error;
  }
  return data;
}
function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add("hidden"), 3600);
}
function showLogin() {
  closeRowActions();
  state.readController?.abort();
  state.viewRevision++;
  state.detailRevision++;
  state.incidents = [];
  state.selectedIncident = null;
  state.view = "overview";
  state.governanceTab = "events";
  state.coordinationTab = "tasks";
  content.innerHTML = "";
  $("#refresh-button").disabled = false;
  content.setAttribute("aria-busy", "false");
  closeModal();
  $("#modal-body").replaceChildren();
  $("#account-menu").classList.add("hidden");
  $("#account-button").setAttribute("aria-expanded", "false");
  clearTimeout(toast.timer);
  $("#toast").textContent = "";
  $("#toast").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
  $("#app").classList.add("hidden");
  state.account = null;
}
function showApp(account) {
  state.account = account;
  $("#login-view").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#account-name").textContent = account.username;
  $("#account-initial").textContent = account.username.slice(0, 1).toUpperCase();
  $("#password-banner").classList.toggle("hidden", !account.must_change_password);
}
async function initialize() {
  try {
    const session = await api("/api/session");
    if (!session.authenticated) { showLogin(); return; }
    showApp(session.account);
    if (session.account.must_change_password) openPasswordModal(true);
    else await loadView(state.view);
  } catch (error) { showLogin(); }
}

async function loadView(view) {
  if (!state.account || state.account.must_change_password) return;
  if (!titles[view]) return;
  closeRowActions();
  state.readController?.abort();
  state.readController = new AbortController();
  const revision = ++state.viewRevision;
  state.detailRevision++;
  state.view = view;
  setNavigation(false);
  document.querySelectorAll("#nav button").forEach(button => {
    button.classList.toggle("active", button.dataset.view === view);
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  content.setAttribute("aria-busy", "true");
  $("#freshness").textContent = "正在刷新";
  $("#refresh-button").disabled = true;
  $("#view-kicker").textContent = titles[view][0];
  $("#view-title").textContent = titles[view][1];
  content.innerHTML = '<div class="loading">正在读取中心状态</div>';
  try {
    const renderers = {
      skillReviews: renderSkillReviews, overview: renderOverview, databases: renderDatabases, users: renderUsers, sessions: renderSessions, capabilities: renderCapabilities,
      operations: renderOperations, interactions: renderInteractions, traces: renderTraces,
      incidents: renderIncidents, coordination: renderCoordination, runtime: renderRuntime, audit: renderAudit,
    };
    await renderers[view]();
    if (revision !== state.viewRevision) return;
    content.querySelectorAll(".table-shell").forEach(table => { table.tabIndex = 0; table.setAttribute("aria-label", "数据表格"); });
    $("#freshness").textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
  } catch (error) {
    if (revision !== state.viewRevision || error.name === "AbortError") return;
    $("#freshness").textContent = "刷新失败";
    if (error.status === 401) { showLogin(); return; }
    if (error.code === "PASSWORD_CHANGE_REQUIRED") { openPasswordModal(true); return; }
    content.innerHTML = empty(`读取失败：${error.message}`) + '<button class="button secondary section-spaced" data-open-view="' + view + '">重新读取</button>';
  } finally {
    if (revision === state.viewRevision) {
      content.setAttribute("aria-busy", "false");
      $("#refresh-button").disabled = false;
    }
  }
}

function metric(label, value, hint, extra = "", iconName = "") { return `<div class="metric ${extra}">${iconName ? icon(iconName, "metric-icon") : ""}<span class="label">${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><span class="hint">${escapeHtml(hint)}</span></div>`; }
function icon(name, extra = "") {
  return `<img class="ui-icon ${extra}" src="/assets/icon-${name}.svg" width="20" height="20" alt="">`;
}
function attentionLink(view, label, count, hint, name = "triangle-alert") {
  return `<button class="attention-link" data-open-view="${view}">${icon(name)}<span><strong>${escapeHtml(label)}</strong><small>${escapeHtml(hint)}</small></span><b>${escapeHtml(count)}</b>${icon("chevron-right")}</button>`;
}
async function renderOverview() {
  const data = await api("/api/overview");
  $("#sidebar-release").textContent = data.runtime.release_id;
  const summary = data.summary;
  const sessionAttention = data.systems.reduce((total, system) => total + system.attention_sessions, 0);
  const attention = [
    summary.open_incidents && attentionLink("incidents", "开放事件", summary.open_incidents, `P0/P1 · ${summary.critical_incidents}`, "shield-alert"),
    sessionAttention && attentionLink("sessions", "会话需要关注", sessionAttention, "以会话记录为准，未做实时检查", "key-round"),
    summary.outstanding_deliveries && attentionLink("coordination", "投递等待确认", summary.outstanding_deliveries, "查看端点与投递回执", "send"),
    (summary.waiting_host_dispatches + summary.acceptance_unknown_dispatches) && attentionLink("coordination", "宿主受理等待", summary.active_host_dispatches, `连接恢复 ${summary.waiting_host_dispatches} / 受理不确定 ${summary.acceptance_unknown_dispatches}`, "bot"),
    summary.failed_operations_24h && attentionLink("operations", "近 24 小时异常操作", summary.failed_operations_24h, "包含失败及结果未知", "activity"),
    summary.paused_policies && attentionLink("capabilities", "生效中的写暂停", summary.paused_policies, "查看暂停范围和原因", "shield-check"),
    summary.isolation_violations && attentionLink("coordination", "隔离异常", summary.isolation_violations, "检查用户、任务与端点关联", "users"),
  ].filter(Boolean).join("");
  content.innerHTML = `
    <div class="metric-grid overview-metrics">
      ${metric("活动任务", summary.active_tasks, `等待用户 ${summary.waiting_tasks}`, "", "list-checks")}
      ${metric("24 小时操作", summary.operations_24h, `异常 ${summary.failed_operations_24h} · 最近最多 500 条记录`, "", "activity")}
      ${metric("待投递", summary.outstanding_deliveries, "尚未确认送达", summary.outstanding_deliveries ? "alert" : "", "send")}
      ${metric("开放事件", summary.open_incidents, `P0/P1 · ${summary.critical_incidents}`, summary.open_incidents ? "alert" : "", "shield-alert")}
    </div>
    <section class="architecture" aria-label="接入与执行路径">
      <div class="architecture-head"><h2>接入与执行路径</h2><small>架构关系 · 非实时链路</small></div>
      <div class="architecture-grid">
        <div class="architecture-node"><h3>01 / 客户端</h3><ul><li>${icon("monitor")}Workspace</li><li>${icon("send")}Telegram</li><li>${icon("handshake")}微信</li></ul></div>
        <span class="path-arrow" aria-hidden="true">→</span>
        <div class="architecture-node"><h3>02 / 智能体宿主</h3><ul><li>${icon("bot")}OpenClaw</li><li>${icon("shield-check")}Reference Host</li></ul><small>Reference Host 用于协议验证</small></div>
        <span class="path-arrow" aria-hidden="true">→</span>
        <div class="architecture-node central"><h3>03 / AgentBridge 中心</h3><ul><li>${icon("users")}身份与权限</li><li>${icon("workflow")}Task Hub</li><li>${icon("handshake")}可信交互</li><li>${icon("shield-check")}受控执行</li></ul></div>
        <span class="path-arrow" aria-hidden="true">→</span>
        <div class="architecture-node"><h3>04 / 已配置业务系统</h3><ul>${data.systems.filter(system => system.configured).map(system => `<li>${icon("database")}${escapeHtml(system.label)}</li>`).join("") || "<li>未配置</li>"}</ul></div>
      </div>
      <div class="write-path"><span>业务写入边界</span>准备 <b>→</b> 用户授权 <b>→</b> 提交 <b>→</b> 权威回读</div>
    </section>
    <div class="governance-strip">
      <span>接入用户<b>${summary.users}</b></span><span>有效令牌<b>${summary.active_tokens}</b></span><span>活动端点<b>${summary.active_endpoints}</b></span><span>写暂停<b>${summary.paused_policies}</b></span><span>隔离异常<b>${summary.isolation_violations}</b></span>
    </div>
    <div class="split">
      <section class="panel"><div class="panel-head"><h3>系统会话</h3><span>注册表快照，不代表实时在线</span></div><div class="system-list">${data.systems.map(system => `
        <div class="system-row"><div class="system-name"><strong>${escapeHtml(system.label)}</strong><span>${escapeHtml(system.system_id)}</span></div><div class="system-stat"><strong>${system.active_sessions}</strong><span>上次有效</span></div><div class="system-stat ${system.attention_sessions ? "attention" : ""}"><strong>${system.attention_sessions}</strong><span>需关注</span></div><div class="system-stat"><strong>${system.total_sessions}</strong><span>总计</span></div></div>`).join("")}</div><button class="attention-link" data-open-view="sessions"><span>查看用户 × 系统会话</span>${icon("chevron-right")}</button></section>
      <section class="panel"><div class="panel-head"><h3>需关注</h3><span>快照 ${fmtTime(data.generated_at)}</span></div><div class="attention-list">${attention || empty("当前统计未发现待关注项")}</div></section>
    </div>
    ${data.paused_policies.length ? `<div class="view-head section-spaced"><h2>生效中的写暂停</h2></div>${table(["范围", "对象", "原因"], data.paused_policies.map(policy => `<tr><td>${escapeHtml(policy.scope_type)}</td><td>${escapeHtml(policy.scope_value)}</td><td>${escapeHtml(policy.reason)}</td></tr>`))}` : ""}
    <div class="view-head section-spaced-sm"><div><h2>最近操作</h2><p>仅运行元数据，不展示业务字段和值。</p></div><button class="button secondary small" data-open-view="operations">全部操作</button></div>${operationTable(data.recent_operations, false)}`;
}

function principalBindingSummary(user) {
  return Object.entries(user.principal_bindings || {}).map(([system, binding]) => `${escapeHtml(system)}: ${escapeHtml(binding.verified || binding.expected || "--")}`).join("<br>") || "--";
}

async function renderDatabases() {
  const data = await api("/api/database-sources");
  const admin = state.account.role === "admin";
  const rows = data.items.map(r => {
    const c = r.active || r.draft;
    const status = {enabled:"已启用",disabled:"已停用",draft:"草稿"}[r.state];
    const check = {passed:"预检通过",failed:"预检失败",pending:"待预检",migrated:"原配置已迁入"}[r.preflight?.status] || "待预检";
    const items = [
      { label: admin ? "配置数据源" : "查看数据源", attrs: { "data-source-edit": r.source_id } },
      { label: "账号授权", attrs: { "data-source-grants": r.source_id } },
      ...(admin ? [
        { label: "只读预检", attrs: { "data-source-action": "preflight", "data-source-id": r.source_id } },
        { label: r.state === "enabled" ? "停用数据源" : "启用数据源", attrs: { "data-source-action": r.state === "enabled" ? "disable" : "enable", "data-source-id": r.source_id }, danger: r.state === "enabled" },
      ] : []),
    ];
    return filterRow(`${c.name} ${r.source_id}`, r.state, `<td><strong>${escapeHtml(c.name)}</strong><div class="muted">${escapeHtml(r.source_id)}</div></td><td>PostgreSQL<div class="muted">${c.template_pack === "taihua_logs" ? "日志系统" : "通用只读"}</div></td><td>${status}<div class="muted">${check} · 版本 ${r.revision}</div>${r.preflight?.code ? `<p class="form-error">${escapeHtml(r.preflight.code)}</p>` : ""}</td><td>${c.allowed_relations.length} 个开放对象</td><td class="row-action-cell">${rowMenuButton(`${c.name} ${r.source_id}数据源`, items)}</td>`);
  });
  content.innerHTML = `<div class="toolbar"><div><strong>独立数据源</strong><p class="muted">先配置与预检，再启用并按账号授权。保存授权无需更换 Token。</p></div>${admin ? '<button class="button primary" data-source-new>接入数据库</button>' : ""}</div>${filteredTable(["数据源","引擎与能力包","状态","开放范围","操作"],rows,"搜索数据源")}`;
}

async function sourceGrantUser(sourceId) {
  const users = await api("/api/users");
  openModal({title: `${sourceId} · 选择中央账号`,submit:"查看能力",body:`<label>中央账号<select name="user">${users.items.map(u => `<option value="${escapeHtml(u.user_subject)}">${escapeHtml(u.user_subject)}</option>`).join("")}</select></label>`,action:async form => { const u = form.get("user"); closeModal(); await openDatabaseGrants(u,sourceId); }});
}

async function sourceAction(sourceId, action) {
  const data = await api("/api/database-sources"); const r = data.items.find(x => x.source_id === sourceId);
  if (!r) throw new Error("数据源已变化，请刷新");
  openModal({title: `${{preflight:"只读预检",enable:"启用数据源",disable:"停用数据源"}[action]} · ${(r.draft || r.active).name}`,submit:"执行",body:`<p>${action === "preflight" ? "检查当前草稿的连接、只读权限、开放对象和模板结构，不读取业务正文。" : action === "disable" ? "停用后此源的查询和下载立即不可用。" : "仅启用通过预检的当前草稿。不会自动为账号授权。"}</p>${reasonField()}`,action:async form => {
    const result = await api("/api/database-sources",{method:"POST",body:JSON.stringify({source_id:sourceId,action,expected_revision:r.revision,reason:form.get("reason")})});
    closeModal(); toast(result.preflight?.status === "failed" ? "预检未通过，请查看原因" : "数据源状态已保存"); await loadView("databases");
  }});
}

async function openSource(sourceId) {
  const data = await api("/api/database-sources");
  const r = data.items.find(x => x.source_id === sourceId);
  const c = r?.draft || {source_id:"",name:"",description:"",engine:"postgresql",host:"",port:5432,dbname:"",username:"",sslmode:"verify-full",allowed_relations:[],template_pack:"generic",statement_timeout_ms:10000,export_rows:10000,export_bytes:10485760};
  const admin = state.account.role === "admin";
  const field = (label,key,type="text") => `<label>${label}<input name="${key}" type="${type}" value="${escapeHtml(c[key])}" ${key === "source_id" && r ? "readonly" : ""} required></label>`;
  openModal({title:r ? `配置 · ${c.name}` : "接入数据库",submit:admin ? "保存草稿" : "关闭",body:`<fieldset ${admin ? "" : "disabled"} class="source-fields"><p class="database-grant-note">第一阶段支持 PostgreSQL。草稿保存后，运行只读预检并启用；新库默认没有账号权限。</p><div class="source-grid">${field("唯一标识（英文）","source_id")}${field("显示名称","name")}${field("主机","host")}${field("端口","port","number")}${field("数据库名","dbname")}${field("只读用户名","username")}</div><label>连接密码<input name="password" type="password" autocomplete="new-password" ${r ? 'placeholder="留空保留现有凭据"' : 'required'}></label><label>TLS<select name="sslmode"><option value="verify-full" ${c.sslmode === "verify-full" ? "selected" : ""}>验证证书与主机名</option>${sourceId === "taihua_primary" ? '<option value="disable" '+(c.sslmode === "disable" ? "selected" : "")+'>既有日志系统内网配置</option>' : ""}</select></label><label>开放表或视图（每行 schema.table）<textarea name="allowed_relations" required rows="5">${escapeHtml(c.allowed_relations.join("\n"))}</textarea></label><label>能力包<select name="template_pack"><option value="generic" ${c.template_pack === "generic" ? "selected" : ""}>通用只读查询</option><option value="taihua_logs" ${c.template_pack === "taihua_logs" ? "selected" : ""}>日志系统（需匹配五视图结构）</option></select></label><label>业务说明<textarea name="description">${escapeHtml(c.description)}</textarea></label><details><summary>查询与导出预算</summary><div class="source-grid">${field("SQL 超时（毫秒，最多 30000）","statement_timeout_ms","number")}${field("导出最多行数（最多 100000）","export_rows","number")}${field("导出字节上限（最多 10485760）","export_bytes","number")}</div></details>${admin ? reasonField() : ""}</fieldset>`,action:async form => {
    if (!admin) {closeModal();return;}
    const config = {...c};
    for (const key of ["source_id","name","host","dbname","username","sslmode","template_pack","description"]) config[key]=form.get(key);
    for (const key of ["port","statement_timeout_ms","export_rows","export_bytes"]) config[key]=Number(form.get(key));
    config.allowed_relations=String(form.get("allowed_relations")).split(/\r?\n/).map(v=>v.trim()).filter(Boolean);
    const body={source_id:config.source_id,action:"save",config,expected_revision:r?.revision || 0,reason:form.get("reason")};
    if (form.get("password")) body.password=form.get("password");
    await api("/api/database-sources",{method:"POST",body:JSON.stringify(body)});closeModal();toast("草稿已保存，请运行只读预检");await loadView("databases");
  }});
}

async function renderUsers() {
  const [users, tokens] = await Promise.all([api("/api/users"), api("/api/tokens")]);
  const admin = state.account.role === "admin";
  const userRows = users.items.map(user => {
    const items = [
      { label: admin ? "配置业务能力" : "查看业务能力", attrs: { "data-user-grants": user.user_subject } },
      { label: admin ? "配置业务助手" : "查看业务助手", attrs: { "data-skill-user": user.user_subject } },
      { label: admin ? "配置数据库能力" : "查看数据库能力", attrs: { "data-database-grants": user.user_subject } },
      ...(admin ? [{ label: "暂停写入", attrs: { "data-pause-user": user.user_subject }, danger: true }] : []),
    ];
    return filterRow(`${user.user_subject} ${Object.values(user.principal_bindings || {}).flatMap(value => [value.expected, value.verified]).join(" ")}`, "", `<td><strong>${escapeHtml(user.user_subject)}</strong></td><td>${principalBindingSummary(user)}</td><td>${user.active_token_count} / ${user.token_count}</td><td>${Object.entries(user.sessions).map(([system, value]) => `${escapeHtml(system)} ${badge(value)}`).join(" ") || "--"}</td><td><span class="muted">${user.database_grants?.capabilities?.length || 0} 项；按源配置</span></td><td class="row-action-cell">${rowMenuButton(`${user.user_subject}用户`, items)}</td>`);
  });
  const tokenRows = tokens.items.map(token => {
    const displayState = token.state === "active" && new Date(token.expires_at).getTime() <= Date.now() ? "expired" : token.state;
    const items = admin && token.state === "active" ? [
      { label: "管理员续期", attrs: { "data-renew-token": token.token_id, "data-expires-at": token.expires_at, "data-edit-revision": token.edit_revision } },
      { label: "撤销令牌", attrs: { "data-revoke-token": token.token_id }, danger: true },
    ] : [];
    return filterRow(`${token.token_id} ${token.label} ${token.user_subject}`, displayState, `<td class="code">${shortId(token.token_id)}</td><td>${escapeHtml(token.label || "未命名")}</td><td>${escapeHtml(token.user_subject)}</td><td>${badge(displayState)}</td><td>${fmtTime(token.expires_at)}</td><td class="row-action-cell">${rowMenuButton(`${token.label || "未命名"} ${token.token_id.slice(0, 8)}令牌`, items)}</td>`);
  });
  content.innerHTML = `<div class="toolbar"><div><strong>身份绑定</strong><button class="button" data-skill-global>业务助手全局状态</button><div class="muted">令牌密钥只在签发时显示一次；各系统主体独立绑定。</div></div>${state.account.role === "admin" ? '<button class="button primary" data-issue-token>签发令牌</button>' : ""}</div>
    <p class="muted">数据库能力按中央账号和数据源配置，保存即生效，无需重启 OpenClaw 或重新签发 Token。</p>
    ${filteredTable(["用户标识", "各系统下游主体", "有效 / 全部令牌", "系统会话", "数据库能力", "操作"], userRows, "搜索用户或下游主体")}
    <div class="view-head section-spaced"><div><h2>MCP Token</h2><p>业务权限按用户配置；令牌用于认证、续期和撤销。管理员看不到已签发密钥。</p></div></div>
    ${filteredTable(["Token ID", "标签", "用户", "状态", "到期时间", "操作"], tokenRows, "搜索 Token 或用户", ["active", "expired", "revoked"])} `;
}

async function renderSessions() {
  const data = await api("/api/sessions");
  const rows = data.items.map(session => {
    const latest = session.latest_event || {};
    const latestSummary = [latest.source, latest.reason].filter(Boolean).join(" · ");
    const items = [{ label: "会话历史", attrs: { "data-session-history": session.session_id } }];
    if (state.account.role === "admin") {
      items.push({ label: "实时检查", attrs: { "data-check-session": session.session_id } });
      items.push({ label: "修改绑定", attrs: { "data-rebind-session": session.session_id, "data-expected-principal": session.expected_principal_ref || "" } });
      if (session.state === "active") items.push({ label: "使会话失效", attrs: { "data-invalidate-session": session.session_id }, danger: true });
    }
    return filterRow(`${session.user_subject} ${session.system_id} ${session.expected_principal_ref} ${session.downstream_principal_ref} ${session.last_error} ${latestSummary}`, session.state, `<td>${escapeHtml(session.user_subject)}</td><td>${escapeHtml(session.system_id)}</td><td>${escapeHtml(session.expected_principal_ref || "--")}</td><td>${escapeHtml(session.downstream_principal_ref || "--")}</td><td>${sessionStateBadge(session)}</td><td title="${escapeHtml(session.keepalive_explanation || "")}">${badge(session.keepalive_state || "not_configured")}</td><td>${fmtTime(session.last_user_activity_at)}</td><td>${fmtTime(session.last_keepalive_at)}</td><td>${fmtTime(session.keepalive_eligible_until)}</td><td class="truncate" title="${escapeHtml(latestSummary || session.last_error || "")}">${escapeHtml(latestSummary || session.last_error || "--")}</td><td class="row-action-cell">${rowMenuButton(`${session.user_subject} ${session.system_id}会话`, items)}</td>`);
  });
  content.innerHTML = `<div class="view-head"><div><h2>用户 × 系统会话矩阵</h2><p>“上次确认有效”表示注册表没有发现失效，但当前未做实时保证；“未保活（超期）”表示超过最近用户活动租约，后台已主动停止保活。实时检查不会延长租约。</p></div></div>${filteredTable(["用户", "系统", "预期主体", "已验证主体", "登录状态", "保活资格", "最近用户活动", "最近保活", "自动保活资格至", "最近事件", "操作"], rows, "搜索用户、系统、主体或事件", ["active", "expired", "awaiting_login", "new", "quarantined"])}`;
}

async function renderCapabilities() {
  const [capabilities, policies] = await Promise.all([api("/api/capabilities"), api("/api/policies")]);
  const policyRows = policies.items.map(policy => filterRow(`${policy.scope_type} ${policy.scope_value} ${policy.reason} ${policy.updated_by}`, policy.state, `<td>${escapeHtml(policy.scope_type)}</td><td class="code">${escapeHtml(policy.scope_value)}</td><td>${escapeHtml(policy.capability_version)}</td><td>${badge(policy.state)}</td><td class="truncate" title="${escapeHtml(policy.reason)}">${escapeHtml(policy.reason)}</td><td>${escapeHtml(policy.updated_by)}</td><td>${fmtTime(policy.updated_at)}</td><td><div class="actions">${state.account.role === "admin" && policy.state === "paused" ? `<button class="button secondary small" data-resume-policy="${policy.policy_id}">恢复</button>` : ""}</div></td>`));
  const capabilityRows = capabilities.items.map(item => { const capabilityState = item.effect === "read" ? "available" : item.paused_by.length ? "paused" : "active"; return filterRow(`${item.name} ${item.system} ${item.effect}`, capabilityState, `<td class="code">${escapeHtml(item.name)}</td><td>${escapeHtml(item.system)}</td><td>${escapeHtml(item.effect)}</td><td>${escapeHtml(item.version)}</td><td>${badge(capabilityState)}</td><td><div class="actions">${state.account.role === "admin" && item.effect !== "read" ? `<button class="button secondary small" data-pause-capability="${escapeHtml(item.name)}" data-version="${escapeHtml(item.version)}">暂停</button>` : ""}</div></td>`); });
  content.innerHTML = `<div class="toolbar"><div><strong>写操作控制</strong><div class="muted">可按全局、系统、用户或具体能力暂停，不影响读取能力。</div></div>${state.account.role === "admin" ? '<div class="actions"><button class="button secondary" data-pause-system="oa">暂停 OA</button><button class="button secondary" data-pause-system="taihua">暂停日志系统</button><button class="button danger" data-global-pause>全局暂停写入</button></div>' : ""}</div>
    ${filteredTable(["范围", "对象", "版本", "状态", "原因", "操作人", "更新时间", ""], policyRows, "搜索策略对象、原因或操作人", ["paused", "active"])}
    <div class="view-head section-spaced"><div><h2>能力目录</h2><p>暂停策略在准备与最终提交边界都会重新检查。</p></div></div>${filteredTable(["能力", "系统", "效果", "版本", "状态", ""], capabilityRows, "搜索能力、系统或效果", ["available", "active", "paused"])}`;
}

async function renderOperations() {
  const data = await api("/api/operations?limit=300");
  content.innerHTML = `<div class="view-head"><div><h2>能力执行记录</h2><p>不展示输入摘要、结果正文和业务字段。</p></div></div>${operationTable(data.items, true)}`;
}
function operationTable(items, filterable = true) {
  const rows = items.map(item => { const effective = item.effective_status || item.status; return filterRow(`${item.operation_id} ${item.user_subject} ${item.capability_name} ${item.status} ${item.interaction_type} ${item.interaction_state} ${item.error_code} ${item.error_message}`, effective, `<td class="code">${shortId(item.operation_id)}</td><td>${escapeHtml(item.user_subject)}</td><td class="code">${escapeHtml(item.capability_name)}</td><td>${badge(effective)}</td><td>${item.interaction_state ? badge(item.interaction_state) : "--"}</td><td class="truncate" title="${escapeHtml(item.error_message || "")}">${escapeHtml(item.error_code || "--")}</td><td>${fmtTime(item.created_at)}</td><td>${fmtTime(item.finished_at)}</td>`); });
  const headers = ["Operation ID", "用户", "能力", "有效状态", "交互状态", "错误", "开始", "结束"];
  return filterable ? filteredTable(headers, rows, "搜索操作、用户、能力或错误", ["awaiting_user", "user_action_completed", "resumed", "user_action_expired", "user_action_rejected", "user_action_superseded", "user_action_handoff", "succeeded", "failed", "running", "unknown"]) : table(headers, rows);
}

async function renderInteractions() {
  const data = await api("/api/interactions?limit=300");
  const rows = data.items.map(item => filterRow(`${item.interaction_id} ${item.user_subject} ${item.system_id} ${item.interaction_type} ${item.title}`, item.state, `<td class="code">${shortId(item.interaction_id)}</td><td>${escapeHtml(item.user_subject)}</td><td>${escapeHtml(item.system_id)}</td><td>${escapeHtml(item.interaction_type)}</td><td class="truncate">${escapeHtml(item.title)}</td><td>${badge(item.state)}</td><td>${fmtTime(item.created_at)}</td><td>${fmtTime(item.expires_at)}</td>`));
  content.innerHTML = `<div class="view-head"><div><h2>可信交互时间线</h2><p>只看交互状态；不展示卡片 URL、字段值、密码或授权计划。</p></div></div>${filteredTable(["Interaction ID", "用户", "系统", "类型", "标题", "状态", "创建", "到期"], rows, "搜索交互、用户、系统或标题", ["pending", "submitted", "approved", "rejected", "expired", "consumed", "superseded"])}`;
}

async function renderTraces() {
  const data = await api("/api/traces?limit=400");
  const rows = data.items.map(item => filterRow(
    `${item.trace_id} ${item.request_id} ${item.user_subject} ${item.task_id} ${item.host_type} ${item.host_instance_id} ${item.system_id} ${item.capability_name} ${item.status} ${item.side_effect_boundary}`,
    item.status,
    `<td class="code">${shortId(item.trace_id)}</td><td>${escapeHtml(item.user_subject)}</td><td>${escapeHtml(item.host_type || "--")}</td><td>${escapeHtml(item.request_kind || "--")}</td><td>${escapeHtml(item.system_id || "--")}</td><td class="truncate" title="${escapeHtml(item.capability_name || "")}">${escapeHtml(item.capability_name || "--")}</td><td>${badge(item.status)}</td><td><span class="boundary">${escapeHtml(boundaryLabel(item.side_effect_boundary))}</span></td><td>${item.stage_count || 0}</td><td>${fmtDuration(item.recorded_duration_ms)}</td><td>${fmtTime(item.started_at)}</td><td><button class="button secondary small" data-trace-detail="${escapeHtml(item.trace_id)}">详情</button></td>`,
  ));
  content.innerHTML = `<div class="view-head"><div><h2>端到端执行链路</h2><p>从宿主接收、MCP、能力、下游适配、可信交互到投递回执；不记录密码、Cookie、业务字段或下载地址。</p></div><button class="button secondary" data-open-view="incidents">查看异常事件</button></div>${filteredTable(["Trace ID", "用户", "宿主", "类型", "系统", "能力", "状态", "副作用边界", "阶段", "记录耗时", "开始", ""], rows, "搜索 Trace、用户、任务、系统或能力", ["active", "waiting", "succeeded", "failed", "unknown", "cancelled"])}`;
}

async function openTraceDetail(traceId) {
  const data = await api(`/api/traces/${traceId}`);
  const trace = data.trace;
  const spanRows = data.spans.map((span, index) => `<tr><td>${index + 1}</td><td>${fmtTime(span.started_at)}</td><td class="code">${escapeHtml(span.stage)}</td><td>${badge(span.status)}</td><td>${fmtDuration(span.duration_ms)}</td><td><span class="boundary">${escapeHtml(boundaryLabel(span.side_effect_boundary))}</span></td><td class="truncate" title="${escapeHtml(span.error_code || "")}">${escapeHtml(span.error_code || "--")}</td></tr>`);
  const incidentRows = data.incidents.map(item => `<tr><td>${escapeHtml(item.severity)}</td><td>${escapeHtml(item.title)}</td><td>${badge(item.state)}</td><td>${fmtTime(item.last_seen_at)}</td></tr>`);
  openModal({
    kicker: "TRACE DETAIL",
    title: `链路 ${trace.trace_id.slice(0, 8)}`,
    submit: "关闭",
    body: `<dl class="detail-grid"><div><dt>用户</dt><dd>${escapeHtml(trace.user_subject)}</dd></div><div><dt>宿主</dt><dd>${escapeHtml(trace.host_type)} / ${escapeHtml(trace.host_instance_id || "--")}</dd></div><div><dt>任务</dt><dd class="code">${escapeHtml(trace.task_id || "--")}</dd></div><div><dt>能力</dt><dd class="code">${escapeHtml(trace.capability_name || "--")}</dd></div><div><dt>状态</dt><dd>${badge(trace.status)}</dd></div><div><dt>最远边界</dt><dd>${escapeHtml(boundaryLabel(trace.side_effect_boundary))}</dd></div><div><dt>开始</dt><dd>${fmtTime(trace.started_at)}</dd></div><div><dt>结束</dt><dd>${fmtTime(trace.finished_at)}</dd></div></dl><h3 class="modal-section-title">阶段时间线</h3>${table(["序", "时间", "阶段", "状态", "耗时", "副作用", "错误"], spanRows)}<h3 class="modal-section-title">关联事件</h3>${table(["级别", "事件", "状态", "最近出现"], incidentRows)}`,
    action: async () => closeModal(),
  });
}

function incidentStateBadge(value) {
  const labels = { open: "待处理", acknowledged: "已确认", investigating: "调查中", resolved: "已解决", suppressed: "已抑制" };
  return `<span class="status ${statusClass(value)}">${escapeHtml(labels[value] || value || "未知")}</span>`;
}
function severityBadge(value) {
  const cls = ["P0", "P1"].includes(value) ? "bad" : value === "P2" ? "warn" : "neutral";
  return `<span class="status ${cls}">${escapeHtml(value || "--")}</span>`;
}

function incidentActions(item) {
  if (state.account.role !== "admin" || !["open", "acknowledged", "investigating"].includes(item.state)) return "";
  const actions = [["acknowledge", "确认"], ["investigate", "调查"], ["resolve", "解决"], ["suppress", "抑制"]];
  return `<div class="actions">${actions.filter(([action]) => !(action === "acknowledge" && item.state !== "open") && !(action === "investigate" && item.state === "investigating")).map(([action, label]) => `<button class="button secondary small" data-incident-action="${action}" data-incident-id="${escapeHtml(item.incident_id)}">${label}</button>`).join("")}</div>`;
}
function incidentCategory(item) {
  if (["resolved", "suppressed"].includes(item.state)) return "closed";
  const occurredAt = Date.parse(item.operation_created_at || item.first_seen_at);
  return item.actionability === "manual_reconciliation" && Number.isFinite(occurredAt) && Date.now() - occurredAt > 86400000 ? "historical" : "current";
}
async function renderIncidents(evaluate = false) {
  const [governance, incidents] = await Promise.all([api(`/api/governance${evaluate ? "?evaluate=true" : ""}`), api("/api/incidents?limit=500")]);
  state.incidents = incidents.items;
  const summary = governance.summary;
  const incidentRows = incidents.items.map(item => filterRow(
    `${item.incident_id} ${item.rule_id} ${item.title} ${item.severity} ${item.symptom_code} ${item.root_cause_code} ${item.user_subject} ${item.system_id} ${item.object_id}`, item.state,
    `<td>${severityBadge(item.severity)}</td><td><button class="incident-select" data-incident-detail="${escapeHtml(item.incident_id)}" aria-pressed="false">${escapeHtml(item.title)}</button><br><span class="muted code">${escapeHtml(item.rule_id)}</span></td><td>${escapeHtml(item.system_id || item.host_type || "--")}</td><td>${incidentStateBadge(item.state)}</td><td>${fmtTime(item.operation_created_at)}</td><td>${fmtTime(item.last_seen_at)}</td>`
  ).replace("<tr ", `<tr data-incident-category="${incidentCategory(item)}" `));
  const sloRows = governance.slo.metrics.map(item => `<tr><td class="code">${escapeHtml(item.metricKey)}</td><td>${item.sampleCount}</td><td><strong>${escapeHtml(fmtMetric(item))}</strong></td><td>${item.target == null ? "--" : escapeHtml(fmtMetric({ ...item, value: item.target }))}</td><td>${badge(item.status)}</td></tr>`);
  const signalRows = governance.recent_signals.map(item => `<tr><td>${fmtTime(item.observed_at)}</td><td class="code">${escapeHtml(item.signal_type)}</td><td>${escapeHtml(item.source)}</td><td>${badge(item.status)}</td><td>${escapeHtml(item.user_subject || "--")}</td><td>${escapeHtml(item.system_id || item.host_type || "--")}</td></tr>`);
  const recoveryRows = governance.recent_recoveries.map(item => `<tr><td>${fmtTime(item.created_at)}</td><td class="code">${escapeHtml(item.action_type)}</td><td>${escapeHtml(item.target_type)}</td><td class="code">${shortId(item.target_id)}</td><td>${escapeHtml(item.actor)}</td><td>${badge(item.status)}</td><td>${escapeHtml(item.error_code || "--")}</td></tr>`);
  const observationRows = (governance.observations || []).map(item => `<tr><td><strong>${escapeHtml(item.name)}</strong><br><span class="muted code">${shortId(item.observation_id)}</span></td><td>${badge(item.state)}</td><td>${fmtTime(item.started_at)}</td><td>${fmtTime(item.ends_at)}</td><td>${item.snapshot_count}</td><td>${fmtTime(item.last_snapshot_at)}</td><td>${escapeHtml(item.created_by)}</td></tr>`);
  const tabs = [["events", "事件"], ["observations", "影子观察"], ["slo", "SLO"], ["signals", "最近信号"], ["recoveries", "恢复账本"]];
  content.innerHTML = `
    <div class="metric-grid">${metric("未结案事件", summary.open_incidents, "包含当前异常和历史待核对", summary.open_incidents ? "alert" : "", "shield-alert")}${metric("P0/P1", summary.critical_incidents, "未结案事件中的严重级别", summary.critical_incidents ? "alert" : "")}${metric("活动链路", summary.active_traces, "执行中或等待用户")}${metric("影子观察", summary.active_observations || 0, `最近采样 ${fmtTime(summary.last_observation_at)}`)}</div>
    <div class="toolbar section-spaced"><div><strong>异常事件与可靠性</strong><div class="muted">只读运行证据；处置事件不会重试业务写入。</div></div>${state.account.role === "admin" ? '<button class="button secondary" data-evaluate-runtime>立即评估</button>' : ""}</div>
    <div class="segmented" role="tablist" aria-label="事件治理视图">${tabs.map(([key, label]) => `<button role="tab" id="governance-tab-${key}" aria-controls="governance-panel-${key}" data-governance-tab="${key}">${label}</button>`).join("")}</div>
    <section id="governance-panel-events" data-governance-panel="events" role="tabpanel" aria-labelledby="governance-tab-events"><div class="incident-layout">
      ${filteredTable(["级别", "事件", "系统/宿主", "状态", "关联操作发起", "最近检测"], incidentRows, "搜索事件、级别、用户或系统", ["open", "acknowledged", "investigating", "resolved", "suppressed"])}
      <aside id="incident-detail" class="incident-detail" aria-label="选中事件详情">${empty("选择事件查看证据")}</aside>
    </div></section>
    <section id="governance-panel-observations" data-governance-panel="observations" role="tabpanel" aria-labelledby="governance-tab-observations"><div class="view-head"><div><h2>影子观察</h2><p>按小时采集中央账本与 SLO，不调用业务系统。</p></div></div>${table(["观察批次", "状态", "开始", "计划结束", "快照", "最近采样", "发起人"], observationRows)}</section>
    <section id="governance-panel-slo" data-governance-panel="slo" role="tabpanel" aria-labelledby="governance-tab-slo"><div class="view-head"><div><h2>24 小时 SLO</h2><p>零隔离异常、写后核验覆盖率与关键阶段延迟。</p></div></div>${table(["指标", "样本", "当前值", "目标", "状态"], sloRows)}</section>
    <section id="governance-panel-signals" data-governance-panel="signals" role="tabpanel" aria-labelledby="governance-tab-signals"><div class="view-head"><h2>最近信号</h2></div>${table(["时间", "信号", "来源", "状态", "用户", "系统/宿主"], signalRows)}</section>
    <section id="governance-panel-recoveries" data-governance-panel="recoveries" role="tabpanel" aria-labelledby="governance-tab-recoveries"><div class="view-head"><div><h2>受控恢复账本</h2><p>动作记录包含操作者与结果，失败不会静默重复。</p></div></div>${table(["时间", "动作", "对象", "对象 ID", "操作者", "状态", "错误"], recoveryRows)}</section>`;
  selectGovernanceTab(state.governanceTab);
  const scope = content.querySelector(".incident-layout [data-filter-scope]");
  const counts = incidents.items.reduce((result, item) => { result[incidentCategory(item)]++; return result; }, { current: 0, historical: 0, closed: 0 });
  scope.querySelector(".filters").insertAdjacentHTML("afterbegin", `<label class="filter-field"><span>分类</span><select data-incident-category><option value="current">当前异常 (${counts.current})</option><option value="historical">历史待核对 (${counts.historical})</option><option value="closed">已结案 (${counts.closed})</option><option value="">全部 (${incidents.items.length})</option></select></label><p class="incident-scope-note muted">当前列表最多 500 条；历史待核对指超过 24 小时的未结案结果未知事项。后台检测不代表新业务发生。</p>`);
  applyFilter(scope.querySelector("[data-incident-category]"));
  const currentItems = incidents.items.filter(item => incidentCategory(item) === "current");
  const selected = currentItems.find(item => item.incident_id === state.selectedIncident) || currentItems[0];
  if (selected) await selectIncident(selected.incident_id);
}
function selectGovernanceTab(tab) {
  state.governanceTab = tab;
  content.querySelectorAll("[data-governance-tab]").forEach(button => {
    const selected = button.dataset.governanceTab === tab;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  content.querySelectorAll("[data-governance-panel]").forEach(panel => panel.classList.toggle("hidden", panel.dataset.governancePanel !== tab));
}
async function selectIncident(id) {
  const item = state.incidents.find(entry => entry.incident_id === id);
  const panel = $("#incident-detail");
  if (!item || !panel) return;
  const revision = ++state.detailRevision;
  state.selectedIncident = id;
  content.querySelectorAll("[data-incident-detail]").forEach(button => {
    const selected = button.dataset.incidentDetail === id;
    button.setAttribute("aria-pressed", String(selected));
    button.closest("tr").classList.toggle("selected", selected);
  });
  panel.innerHTML = `
    <h3>${escapeHtml(item.title)}</h3>${severityBadge(item.severity)} ${incidentStateBadge(item.state)}
    <dl class="detail-grid section-spaced-sm">
      <div><dt>用户</dt><dd>${escapeHtml(item.user_subject || "--")}</dd></div><div><dt>系统 / 宿主</dt><dd>${escapeHtml(item.system_id || item.host_type || "--")}</dd></div>
      <div><dt>关联操作发起时间</dt><dd>${fmtTime(item.operation_created_at)}</dd></div><div><dt>首次检测到</dt><dd>${fmtTime(item.first_seen_at)}</dd></div>
      <div><dt>最近检测到</dt><dd>${fmtTime(item.last_seen_at)}</dd></div><div><dt>累计检测次数（非业务次数）</dt><dd>${escapeHtml(item.occurrence_count)}</dd></div>
      <div><dt>症状</dt><dd>${escapeHtml(item.symptom_code || "--")}</dd></div><div><dt>根因记录</dt><dd>${escapeHtml(item.root_cause_code || "尚未确定")}</dd></div>
      <div><dt>事件 ID</dt><dd class="code">${escapeHtml(item.incident_id)}</dd></div><div><dt>关联对象 · ${escapeHtml(item.object_type || "--")}</dt><dd class="code">${escapeHtml(item.object_id || "--")}</dd></div>
    </dl>
    ${item.recommended_action ? `<p class="evidence-notice">${escapeHtml(item.recommended_action)}</p>` : ""}
    <h4>执行证据</h4><div id="incident-evidence">${item.trace_id ? '<p class="muted">正在读取关联链路</p>' : '<p class="muted">没有关联链路，无法确认执行阶段。</p>'}</div>
    ${item.trace_id ? `<button class="button secondary small" data-trace-detail="${escapeHtml(item.trace_id)}">查看完整链路</button>` : ""}
    <h4>治理处置</h4><p class="muted">最近更新：${fmtTime(item.updated_at)}<br>解决治理事件不等于业务操作成功。结果未知的业务写入不会自动重试。</p>${item.disposition_reason ? `<p class="evidence-notice">结案记录：${escapeHtml(item.disposition_reason)}<br>操作者：${escapeHtml(item.disposition_actor || "--")}</p>` : ""}${incidentActions(item)}`;
  if (!item.trace_id) return;
  try {
    const data = await api(`/api/traces/${encodeURIComponent(item.trace_id)}`);
    if (revision !== state.detailRevision || !panel.isConnected) return;
    const spans = data.spans || [];
    $("#incident-evidence").innerHTML = `<p><span class="boundary">${escapeHtml(boundaryLabel(data.trace.side_effect_boundary))}</span></p>
      ${spans.length ? `<ol class="evidence-timeline">${spans.slice(-12).map(span => `<li><strong>${escapeHtml(span.stage)}</strong><small>${fmtTime(span.started_at)} · ${fmtDuration(span.duration_ms)}</small>${badge(span.status)}<small>${escapeHtml(boundaryLabel(span.side_effect_boundary))}</small></li>`).join("")}</ol><p class="muted">展示最近 ${Math.min(spans.length, 12)} 个已记录阶段，共 ${spans.length} 个。</p>` : '<p class="muted">链路尚无阶段记录。</p>'}`;
  } catch (error) {
    if (revision !== state.detailRevision || !panel.isConnected || error.name === "AbortError") return;
    $("#incident-evidence").innerHTML = `<p class="form-error">证据读取失败：${escapeHtml(error.message)}</p><button class="button secondary small" data-incident-detail="${escapeHtml(id)}">重新读取证据</button>`;
  }
}

async function renderCoordination() {
  const data = await api("/api/coordination?limit=300");
  data.artifacts = Array.isArray(data.artifacts) ? data.artifacts : [];
  data.plans = Array.isArray(data.plans) ? data.plans : [];
  const summary = data.summary;
  const taskRows = data.tasks.map(item => filterRow(`${item.task_id} ${item.user_subject} ${item.title} ${item.origin_client_type} ${item.origin_label} ${item.current_operation_id} ${item.current_interaction_id}`, item.status, `<td class="code">${shortId(item.task_id)}</td><td>${escapeHtml(item.user_subject)}</td><td class="truncate" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</td><td>${badge(item.status)}</td><td>${escapeHtml(item.origin_label || item.origin_client_type || "--")}</td><td class="code">${shortId(item.current_operation_id)}</td><td class="code">${shortId(item.current_interaction_id)}</td><td>${fmtTime(item.updated_at)}</td><td>${fmtTime(item.finished_at)}</td>`));
  const planRows = data.plans.map(item => { const completed = Number(item.step_counts?.succeeded || 0) + Number(item.step_counts?.skipped || 0); const range = item.temporal_range ? `${item.temporal_range.start || "?"} 至 ${item.temporal_range.end || "?"}` : "--"; const result = item.result_kind ? `${item.result_kind} · ${item.included_count || 0}/${item.excluded_count || 0}` : "--"; return filterRow(`${item.plan_id} ${item.task_id} ${item.user_subject} ${item.current_step_key} ${(item.systems || []).join(" ")} ${item.effect_outcome} ${item.coverage_status} ${range} ${item.terminal_reason}`, item.state, `<td class="code">${shortId(item.plan_id)}</td><td>${escapeHtml(item.user_subject)}</td><td>${badge(item.state)}</td><td>${escapeHtml(item.schema_version || `v${item.revision}`)}</td><td>${escapeHtml((item.systems || []).join(" → ") || "--")}</td><td>${completed}/${item.step_count}</td><td class="code">${escapeHtml(item.current_step_key || "--")}</td><td>${escapeHtml(item.effect_outcome || "--")}</td><td>${escapeHtml(item.coverage_status || "--")}</td><td>${escapeHtml(range)}</td><td>${escapeHtml(result)}</td><td>${item.write_sink_count}</td><td>${escapeHtml(item.terminal_reason || "--")}</td><td>${fmtTime(item.updated_at)}</td>`); });
  const endpointRows = data.endpoints.map(item => filterRow(`${item.endpoint_id} ${item.user_subject} ${item.client_type} ${item.label} ${item.capabilities.join(" ")} ${item.delivery_state}`, item.state, `<td class="code">${shortId(item.endpoint_id)}</td><td>${escapeHtml(item.user_subject)}</td><td>${escapeHtml(item.client_type)}</td><td>${badge(item.delivery_mode)}</td><td>${escapeHtml(item.label || "--")}</td><td>${scopeBadges(item.capabilities)}</td><td>${badge(item.state)}</td><td>${badge(item.delivery_state)}${item.deferred_delivery_count ? ` <span class="muted">${item.deferred_delivery_count}</span>` : ""}</td><td>${fmtTime(item.last_seen_at)}</td>`));
  const continuationRows = data.continuations.map(item => filterRow(`${item.endpoint_id} ${item.user_subject} ${item.client_type} ${item.selected_task_id} ${item.reason}`, item.state, `<td class="code">${shortId(item.endpoint_id)}</td><td>${escapeHtml(item.user_subject)}</td><td>${escapeHtml(item.client_type || "--")}</td><td>${badge(item.state)}</td><td>${badge(item.execution_mode)}</td><td class="code">${shortId(item.selected_task_id)}</td><td>${item.candidate_count}</td><td>${escapeHtml(item.reason || "--")}</td><td>${fmtTime(item.expires_at)}</td>`));
  const deliveryRows = data.deliveries.map(item => { const actions = state.account.role === "admin" ? `<div class="actions">${["failed", "deferred", "archived"].includes(item.state) ? `<button class="button secondary small" data-recovery-action="retry_delivery" data-recovery-target="${item.delivery_id}">重试</button>` : ""}${["failed", "acknowledged"].includes(item.state) ? `<button class="button secondary small" data-recovery-action="archive_delivery" data-recovery-target="${item.delivery_id}">归档</button>` : ""}</div>` : ""; return filterRow(`${item.delivery_id} ${item.task_id} ${item.endpoint_id} ${item.user_subject} ${item.event_type}`, item.state, `<td class="code">${shortId(item.delivery_id)}</td><td>${escapeHtml(item.user_subject)}</td><td>${escapeHtml(item.event_type || item.payload_type)}</td><td class="code">${shortId(item.endpoint_id)}</td><td>${badge(item.state)}</td><td>${item.attempt_count}</td><td>${fmtTime(item.next_attempt_at)}</td><td>${fmtTime(item.acknowledged_at)}</td><td>${fmtTime(item.updated_at)}</td><td>${actions}</td>`); });
  const artifactRows = data.artifacts.map(item => filterRow(`${item.artifact_id} ${item.task_id} ${item.user_subject} ${item.filename} ${item.artifact_type} ${item.content_type}`, item.state, `<td class="code">${shortId(item.artifact_id)}</td><td>${escapeHtml(item.user_subject)}</td><td class="truncate" title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</td><td>${escapeHtml(item.artifact_type)}</td><td>${escapeHtml(item.content_type)}</td><td>${fmtBytes(item.byte_size)}</td><td>${badge(item.state)}</td><td class="code">${shortId(item.task_id)}</td><td>${fmtTime(item.created_at)}</td><td>${fmtTime(item.expires_at)}</td>`));
  const violationRows = Object.entries(data.isolation?.violations || {}).map(([name, count]) => `<tr><td class="code">${escapeHtml(name)}</td><td>${count}</td><td>${badge(count === 0 ? "succeeded" : "failed")}</td></tr>`);
  content.innerHTML = `<div class="metric-grid">
    ${metric("涉及用户", summary.users, "Task Hub 用户")}
    ${metric("活动任务", summary.active_tasks, "尚未进入终态")}
    ${metric("等待用户", summary.waiting_tasks, "卡片或确认节点")}
    ${metric("活动计划", summary.active_plans || 0, `等待用户 ${summary.waiting_plans || 0}`)}
    ${metric("活动端点", summary.active_endpoints, "网页与聊天通道")}
    ${metric("投递模式", `${summary.pull_endpoints} / ${summary.direct_endpoints}`, "拉取 / 直推")}
    ${metric("活动接续", summary.active_continuations, "跨端任务上下文")}
    ${metric("可取用文件", summary.ready_artifacts, `${summary.expired_artifacts} 个链接已过期`)}
    ${metric("待投递", summary.outstanding_deliveries, `等待活动 ${summary.deferred_deliveries} / 历史失败 ${summary.failed_deliveries}`, summary.outstanding_deliveries ? "alert" : "")}
    ${metric("宿主受理等待", summary.active_host_dispatches, `连接恢复 ${summary.waiting_host_dispatches} / 受理不确定 ${summary.acceptance_unknown_dispatches}`, summary.waiting_host_dispatches || summary.acceptance_unknown_dispatches ? "alert" : "")}
    ${metric("隔离完整性", summary.isolation_violations ? "异常" : "通过", `${summary.isolation_violations} 项异常`, summary.isolation_violations ? "alert" : "")}
  </div>
  <div class="view-head section-spaced"><div><h2>任务协调明细</h2><p>只显示治理元数据；聊天正文、卡片链接、通道路由和业务字段不会进入此页面。</p></div></div>
  <div class="segmented" role="tablist" aria-label="多端任务视图">
    <button role="tab" data-coordination-tab="tasks">任务 <span>${data.tasks.length}</span></button>
    <button role="tab" data-coordination-tab="plans">计划 <span>${data.plans.length}</span></button>
    <button role="tab" data-coordination-tab="endpoints">端点 <span>${data.endpoints.length}</span></button>
    <button role="tab" data-coordination-tab="continuations">接续 <span>${data.continuations.length}</span></button>
    <button role="tab" data-coordination-tab="deliveries">投递 <span>${data.deliveries.length}</span></button>
    <button role="tab" data-coordination-tab="artifacts">文件 <span>${data.artifacts.length}</span></button>
    <button role="tab" data-coordination-tab="isolation">隔离 <span>${summary.isolation_violations}</span></button>
  </div>
  <section data-coordination-panel="tasks">${filteredTable(["Task ID", "用户", "任务", "状态", "发起端", "当前操作", "当前交互", "更新", "结束"], taskRows, "搜索任务、用户、端点或关联 ID", ["active", "waiting_user", "running", "completed", "partially_succeeded", "failed", "outcome_unknown", "canceled", "superseded"])}</section>
  <section data-coordination-panel="plans">${filteredTable(["Plan ID", "用户", "状态", "协议", "系统", "步骤", "当前步骤", "效果结果", "覆盖", "时间范围", "结果摘要", "写终点", "终止原因", "更新"], planRows, "搜索计划、用户、系统、结果、覆盖或终止原因", ["validated", "running", "waiting_user", "succeeded", "failed", "outcome_unknown", "canceled"])}</section>
  <section data-coordination-panel="endpoints">${filteredTable(["Endpoint ID", "用户", "客户端", "投递方式", "标签", "能力", "状态", "同步状态", "最近活动"], endpointRows, "搜索端点、用户、客户端或能力", ["active", "inactive"])}</section>
  <section data-coordination-panel="continuations">${filteredTable(["Endpoint ID", "用户", "客户端", "状态", "执行模式", "选中任务", "候选", "原因", "到期"], continuationRows, "搜索用户、端点、任务或原因", ["selected", "awaiting_selection", "expired", "canceled"])}</section>
  <section data-coordination-panel="deliveries">${filteredTable(["Delivery ID", "用户", "事件", "Endpoint ID", "状态", "尝试", "下次重试", "确认", "更新", ""], deliveryRows, "搜索投递、用户、事件或端点", ["pending", "delivering", "deferred", "acknowledged", "failed", "archived"])}</section>
  <section data-coordination-panel="artifacts">${filteredTable(["Artifact ID", "用户", "文件", "类型", "内容类型", "大小", "状态", "Task ID", "创建", "到期"], artifactRows, "搜索文件、用户、任务或类型", ["ready", "expired"])}</section>
  <section data-coordination-panel="isolation">${table(["检查项", "异常数", "结果"], violationRows)}</section>`;
  selectCoordinationTab(state.coordinationTab);
}
function selectCoordinationTab(tab) {
  state.coordinationTab = tab;
  content.querySelectorAll("[data-coordination-tab]").forEach(button => {
    const selected = button.dataset.coordinationTab === tab;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
  });
  content.querySelectorAll("[data-coordination-panel]").forEach(panel => panel.classList.toggle("hidden", panel.dataset.coordinationPanel !== tab));
}

async function renderRuntime() {
  const [data, accounts] = await Promise.all([api("/api/runtime"), api("/api/admin-accounts")]);
  const accountRows = accounts.items.map(account => filterRow(`${account.username} ${account.role}`, account.state, `<td>${escapeHtml(account.username)}</td><td>${escapeHtml(account.role)}</td><td>${badge(account.state)}</td><td>${account.must_change_password ? badge("pending") : badge("active")}</td><td>${fmtTime(account.last_login_at)}</td><td>${fmtTime(account.created_at)}</td>`));
  const taskHub = data.coordination?.task_hub || { summary: {}, isolation: { passed: false, violations: {} } };
  const hostControl = data.coordination?.host_control || { operations: [], recent_slow_calls: [], slow_after_ms: 1000 };
  const agentHosts = data.coordination?.agent_hosts || { summary: {}, registrations: [], compatibility: [], leases: [] };
  const workspaceGateway = data.workspace_gateway || { configured: false };
  const governance = data.governance || { active_traces: 0, open_incidents: 0, critical_incidents: 0, running_recoveries: 0 };
  const gatewayHasCurrentError = Boolean(
    workspaceGateway.last_error_code &&
    (!workspaceGateway.last_success_at ||
      new Date(workspaceGateway.last_error_at).getTime() >
        new Date(workspaceGateway.last_success_at).getTime()),
  );
  const operationRows = hostControl.operations.map(operation => filterRow(`${operation.operation_name}`, operation.error_count ? "failed" : operation.slow_count ? "running" : "succeeded", `<td class="code">${escapeHtml(operation.operation_name)}</td><td>${operation.call_count}</td><td>${operation.slow_count}</td><td>${operation.error_count}</td><td>${operation.average_elapsed_ms} ms</td><td>${operation.max_elapsed_ms} ms</td><td>${fmtTime(operation.last_called_at)}</td>`));
  const slowRows = hostControl.recent_slow_calls.map(call => filterRow(`${call.user_subject} ${call.operation_name} ${call.error_code}`, call.error_code ? "failed" : "running", `<td>${fmtTime(call.called_at)}</td><td>${escapeHtml(call.user_subject)}</td><td class="code">${escapeHtml(call.operation_name)}</td><td>${call.elapsed_ms} ms</td><td>${escapeHtml(call.error_code || "--")}</td>`));
  const violationRows = Object.entries(taskHub.isolation?.violations || {}).map(([name, count]) => `<tr><td class="code">${escapeHtml(name)}</td><td>${count}</td><td>${badge(count === 0 ? "succeeded" : "failed")}</td></tr>`);
  const hostRows = agentHosts.registrations.map(item => {
    const runtime = item.runtime || {};
    return `<tr><td>${escapeHtml(item.userSubject)}</td><td class="code">${escapeHtml(item.agentHost)}</td><td class="code">${escapeHtml(item.hostVersion)}</td><td class="code">${escapeHtml(item.hostInstanceId)}</td><td>${badge(item.acceptedLevel)}</td><td>${badge(runtime.status || "unknown")}</td><td>${runtime.activeTaskCount ?? "--"}</td><td>${runtime.waitingInteractionCount ?? "--"}</td><td>${fmtTime(runtime.observedAt || item.lastSeenAt)}</td></tr>`;
  });
  const compatibilityRows = agentHosts.compatibility.map(item => `<tr><td class="code">${escapeHtml(item.agentHost)}</td><td class="code">${escapeHtml(item.hostVersion)}</td><td>${badge(item.acceptedLevel)}</td><td class="code">${escapeHtml(item.evidenceDigest)}</td><td>${fmtTime(item.approvedAt)}</td></tr>`);
  content.innerHTML = `<div class="metric-grid">
    ${metric("发布版本", data.release_id, "当前服务构建")}${metric("启动时间", fmtTime(data.started_at), "中心进程")}${metric("管理 API", data.admin_api, "独立认证域")}${metric("数据库", data.database, "SQLite WAL")}
    ${metric("保活租约", data.session_keepalive.activity_lease_seconds ? `${Math.round(data.session_keepalive.activity_lease_seconds / 86400)} 天` : "关闭", "仅真实活动续租")}${metric("活动端点", taskHub.summary.active_endpoints ?? "--", `${taskHub.summary.users ?? 0} 个用户`)}${metric("待投递", taskHub.summary.outstanding_deliveries ?? "--", `失败 ${taskHub.summary.failed_deliveries ?? 0}`)}${metric("宿主受理等待", taskHub.summary.active_host_dispatches ?? 0, `连接恢复 ${taskHub.summary.waiting_host_dispatches ?? 0} / 不确定 ${taskHub.summary.acceptance_unknown_dispatches ?? 0}`, taskHub.summary.waiting_host_dispatches || taskHub.summary.acceptance_unknown_dispatches ? "alert" : "")}${metric("隔离完整性", taskHub.isolation?.passed ? "通过" : "异常", `${taskHub.summary.isolation_violation_count ?? "--"} 项异常`, taskHub.isolation?.passed ? "" : "alert")}
    ${metric("活动链路", governance.active_traces, "执行中或等待用户")}${metric("开放事件", governance.open_incidents, `${governance.critical_incidents} 个 P0/P1`, governance.critical_incidents ? "alert" : "")}${metric("恢复动作", governance.running_recoveries, "当前执行中")}${metric("追踪阶段", governance.span_count ?? 0, `最近信号 ${fmtTime(governance.last_signal_at)}`)}
    ${metric("Workspace Gateway", workspaceGateway.configured ? workspaceGateway.target : "未配置", workspaceGateway.last_success_at ? `最近连通 ${fmtTime(workspaceGateway.last_success_at)}` : "尚无成功连接")}${metric("Gateway 最近错误", workspaceGateway.last_error_code || "无", workspaceGateway.last_error_at ? `${fmtTime(workspaceGateway.last_error_at)}${gatewayHasCurrentError ? "" : " · 已恢复"}` : "未记录错误", gatewayHasCurrentError ? "alert" : "")}
    ${metric("宿主登记", agentHosts.summary.registrations ?? 0, `${agentHosts.summary.hostInstances ?? 0} 个实例`)}${metric("宿主健康", agentHosts.summary.healthyRegistrations ?? 0, `兼容版本 ${agentHosts.summary.compatibilityVersions ?? 0}`)}${metric("协调 Lease", agentHosts.summary.activeLeases ?? 0, `过期 ${agentHosts.summary.expiredLeases ?? 0}`)}
  </div>
  <div class="view-head section-spaced"><div><h2>智能体宿主</h2><p>按 Token 主体展示兼容等级、实例健康和活动任务；不显示 Token、字段值或短期链接。</p></div></div>${table(["用户", "宿主", "版本", "实例", "等级", "健康", "活动任务", "等待用户", "最近上报"], hostRows)}
  <div class="view-head section-spaced"><div><h2>宿主兼容基线</h2><p>宿主版本变化后必须重新通过共享契约验收，旧版本结论不会自动继承。</p></div></div>${table(["宿主", "版本", "等级", "证据", "批准时间"], compatibilityRows)}
  <div class="view-head section-spaced"><div><h2>已配置系统</h2><p>控制台只报告状态，不提供 systemd 重启或业务代操作。</p></div></div>${table(["系统 ID", "名称", "状态"], data.systems.map(system => `<tr><td class="code">${escapeHtml(system.system_id)}</td><td>${escapeHtml(system.label)}</td><td>${badge(system.configured ? "active" : "failed")}</td></tr>`))}
  <div class="view-head section-spaced"><div><h2>隔离完整性</h2><p>所有任务、时间线和通知都必须与所属用户和端点一致。</p></div><button class="button secondary small" data-open-view="coordination">打开多端任务</button></div>${table(["检查项", "异常数", "结果"], violationRows)}
  <div class="view-head section-spaced"><div><h2>协调调用即时耗时</h2><p>慢调用阈值 ${hostControl.slow_after_ms} ms；此即时统计随中心进程重启清零，持久化历史请查看链路追踪。</p></div><button class="button secondary small" data-open-view="traces">打开链路追踪</button></div>${filteredTable(["调用", "次数", "慢调用", "错误", "平均", "最大", "最近调用"], operationRows, "搜索调用名称", ["succeeded", "running", "failed"])}
  <div class="view-head section-spaced"><div><h2>最近慢调用</h2><p>仅保留工具名、用户标识、耗时和错误类型。</p></div></div>${filteredTable(["时间", "用户", "调用", "耗时", "错误"], slowRows, "搜索用户、调用或错误", ["running", "failed"])}
  <div class="toolbar section-spaced"><div><strong>管理账户</strong><div class="muted">管理员可执行控制动作，审计员仅可查看。</div></div>${state.account.role === "admin" ? '<button class="button primary" data-create-admin>新建账户</button>' : ""}</div>${filteredTable(["用户名", "角色", "状态", "初始密码", "最近登录", "创建时间"], accountRows, "搜索管理账户", ["active", "revoked"])}`;
}
async function renderAudit() {
  const data = await api("/api/audit?limit=400");
  const rows = data.items.map(item => { const result = item.result === "succeeded" ? "succeeded" : "failed"; return filterRow(`${item.actor_username} ${item.action} ${item.target_type} ${item.target_id} ${item.reason} ${item.request_ip}`, result, `<td>${fmtTime(item.created_at)}</td><td>${escapeHtml(item.actor_username || "匿名")}</td><td class="code">${escapeHtml(item.action)}</td><td>${escapeHtml(item.target_type || "--")}</td><td class="code">${shortId(item.target_id)}</td><td class="truncate" title="${escapeHtml(item.reason || "")}">${escapeHtml(item.reason || "--")}</td><td>${badge(result)}</td><td>${escapeHtml(item.request_ip || "--")}</td>`); });
  content.innerHTML = `<div class="view-head"><div><h2>独立管理审计</h2><p>管理事件仅追加写入，包含操作人、来源、原因和前后状态。</p></div></div>${filteredTable(["时间", "管理员", "动作", "对象类型", "对象 ID", "原因", "结果", "来源 IP"], rows, "搜索管理员、动作、对象或原因", ["succeeded", "failed"])}`;
}

async function openSkillConfig(userSubject = null) {
  const config = await api(`/api/skills${userSubject ? `?user=${encodeURIComponent(userSubject)}` : ""}`);
  const editable = state.account.role === "admin";
  const labels = { review: "复核", track: "追踪", search: "检索", preview: "仅预览", fill: "填写日志", single: "单条办理", batch: "批量办理" };
  const rows = config.items.map(item => {
    const selected = config.value[item.id];
    if (!userSubject) return `<label>${escapeHtml(item.name)} · ${escapeHtml(item.version)}<select name="status:${item.id}">${["enabled", "trial", "disabled"].map(status => `<option value="${status}" ${(selected || item.default_status) === status ? "selected" : ""}>${{enabled:"可用",trial:"试用",disabled:"停用"}[status]}</option>`).join("")}</select></label>`;
    const reasons = Object.values(item.profiles).flatMap(p => p.missing);
    return `<fieldset class="scope-group"><legend><label class="check"><input type="checkbox" name="skill" value="${item.id}" ${selected ? "checked" : ""}>${escapeHtml(item.name)}</label></legend><p>${escapeHtml(item.description)}</p><p class="muted">${escapeHtml(item.status === "trial" ? "试用" : item.status === "disabled" ? "全局已停用" : "可用")} · ${escapeHtml(reasons.join("；") || "执行时再次检查权限与所选来源")}</p><div class="checkbox-grid">${item.profile_choices.map(p => `<label class="check"><input type="checkbox" name="profiles:${item.id}" value="${p}" ${selected?.profiles.includes(p) ? "checked" : ""}>${labels[p] || escapeHtml(p)}</label>`).join("")}</div>${item.requires_source ? `<p class="muted">执行时从用户已获准的数据源中选择；只有一个适用来源时自动选择。</p>` : ""}<label>输出详细程度<select name="detail:${item.id}">${["brief","standard","detailed"].map(d => `<option value="${d}" ${(selected?.detail || "standard") === d ? "selected" : ""}>${{brief:"简要",standard:"标准",detailed:"详细"}[d]}</option>`).join("")}</select></label></fieldset>`;
  }).join("");
  openModal({title:userSubject ? `业务助手 · ${userSubject}` : "业务助手全局状态", submit:editable ? "保存" : "关闭",
    body:`<p>业务助手不增加业务权限。保存后下次调用生效，无需换发令牌；已提交操作继续核验。</p><fieldset ${editable ? "" : "disabled"}>${rows}</fieldset>${editable ? reasonField() : ""}`,
    action:async form => {
      if (!editable) { closeModal(); return; }
      const value = {};
      for (const item of config.items) {
        if (!userSubject) value[item.id] = form.get(`status:${item.id}`);
        else {
          const profiles = form.getAll(`profiles:${item.id}`);
          if (profiles.length) value[item.id] = {profiles, source_id:"", detail:form.get(`detail:${item.id}`)};
        }
      }
      await api("/api/skills", {method:"POST",body:JSON.stringify({user_subject:userSubject,value,expected_revision:config.revision,reason:form.get("reason")})});
      closeModal(); toast("业务助手配置已保存");
    }});
  if (userSubject) {
    for (const group of $("#modal-body").querySelectorAll(".scope-group")) {
      const parent = group.querySelector('input[name="skill"]');
      const children = [...group.querySelectorAll('input[name^="profiles:"]')];
      const sync = () => {
        const count = children.filter(child => child.checked).length;
        parent.checked = count === children.length && count > 0;
        parent.indeterminate = count > 0 && count < children.length;
      };
      parent.addEventListener("change", () => {
        for (const child of children) child.checked = parent.checked;
        sync();
      });
      for (const child of children) child.addEventListener("change", sync);
      sync();
    }
  }
}

async function openUserGrants(userSubject) {
  const config = await api(`/api/user-grants?user=${encodeURIComponent(userSubject)}`);
  const editable = state.account.role === "admin";
  const selected = new Set(config.permissions);
  const groups = ["oa", "smartlight", "taihua", "yuque"].map(system => {
    const labels = { oa: "致远 OA", smartlight: "照明系统", taihua: "日志系统", yuque: "语雀" };
    const items = config.available.filter(item => item.system === system);
    return `<fieldset class="scope-group"><legend>${labels[system]}</legend><div class="checkbox-grid">${items.map(item => `<label class="check"><input type="checkbox" name="business_permission" value="${escapeHtml(item.id)}" ${selected.has(item.id) ? "checked" : ""} ${editable && item.grantable ? "" : "disabled"}><span><strong>${escapeHtml(item.label)}${item.grantable ? "" : "（待开放）"}</strong><small>${escapeHtml(item.id)}</small></span></label>`).join("")}</div></fieldset>`;
  }).join("");
  const grantNote = `<p class="muted">${config.configured ? "" : "尚未配置业务能力，默认无业务权限。"}保存后所有有效令牌下次调用生效，无需换发或重启。数据库能力单独配置。</p>`;
  openModal({ title: `${userSubject} · 业务能力`, submit: editable ? "保存用户授权" : "关闭", body: `${grantNote}${groups}<p class="muted">当前版本：${config.revision}。空列表撤销此用户全部非数据库业务能力。</p>${editable ? reasonField() : ""}`,
    action: async form => { if (!editable) { closeModal(); return; } await api("/api/user-grants", { method: "POST", body: JSON.stringify({ user_subject: config.user_subject, permissions: form.getAll("business_permission"), expected_revision: config.revision, reason: form.get("reason") }) }); closeModal(); toast("用户业务授权已保存"); await loadView("users"); }
  });
}
async function openDatabaseGrants(userSubject, sourceId) {
  if (!sourceId) {
    const sources = await api("/api/database-sources");
    if (!sources.items.length) { toast("请先在数据库页面接入数据源"); return; }
    openModal({title: `${userSubject} · 选择数据源`, submit: "查看能力", body: `<label>数据源<select name="source">${sources.items.map(r => `<option value="${escapeHtml(r.source_id)}">${escapeHtml((r.active || r.draft).name)} · ${escapeHtml(r.source_id)}</option>`).join("")}</select></label>`, action: async form => { const id = form.get("source"); closeModal(); await openDatabaseGrants(userSubject, id); }}); return;
  }
  const request = state.databaseGrantRequest = (state.databaseGrantRequest || 0) + 1;
  const config = await api(`/api/database-grants?user=${encodeURIComponent(userSubject)}&source=${encodeURIComponent(sourceId)}`);
  if (request !== state.databaseGrantRequest || !["users", "databases"].includes(state.view) || !state.account) return;
  const editable = state.account.role === "admin";
  const selected = new Set(config.capabilities);
  const labels = { "database.schema": "数据库结构", "database.directory": "人员、部门与项目目录", "database.logs.query": "日志正文查询", "database.logs.analyze": "日志统计分析", "database.logs.content_analyze": "日志内容分析", "database.comments.analyze": "日志评论分析", "database.free.read": "自由只读 SQL", "database.report.export": "CSV 导出与下载" };
  const choices = advanced => config.available.filter(item => item.advanced === advanced).map(item => `<label class="check database-capability"><input type="checkbox" name="database_capability" value="${escapeHtml(item.name)}" ${selected.has(item.name) ? "checked" : ""} ${editable ? "" : "disabled"}><span><strong>${escapeHtml(labels[item.name] || item.name)}</strong><small>${escapeHtml(item.description)}</small></span></label>`).join("");
  openModal({ title: `${userSubject} · ${sourceId}`, submit: editable ? "保存授权" : "关闭",
    body: `<div class="database-grant-note"><strong>保存即生效</strong><p>继续使用原 Token，无需重启 OpenClaw。只影响此中央账号在当前数据源的能力。</p></div><fieldset class="scope-group"><legend>常规只读能力</legend><div class="checkbox-grid">${choices(false)}</div></fieldset><fieldset class="scope-group database-advanced"><legend>高级能力</legend>${choices(true)}<p class="muted">可查询当前源的全部开放对象，可能包含评论。请独立选择此项。</p></fieldset><p class="muted">全部取消只撤销当前数据源权限，其他数据源不受影响。当前配置版本：${config.revision}</p>${editable ? reasonField() : '<p class="muted">审计员仅可查看。</p>'}`,
    action: async form => {
      if (!editable) { closeModal(); return; }
      await api("/api/database-grants", { method: "POST", body: JSON.stringify({ user_subject: config.user_subject, source_id: sourceId,
        capabilities: form.getAll("database_capability"), expected_revision: config.revision, reason: form.get("reason") }) });
      closeModal(); toast("数据库授权已生效；原 Token 继续使用，无需重启"); await loadView(state.view);
    }
  });
}

function openModal({ kicker = "管理操作", title, body, submit = "确认", danger = false, action, locked = false }) {
  $("#modal-kicker").textContent = kicker; $("#modal-title").textContent = title; $("#modal-body").innerHTML = body;
  $("#modal-submit").textContent = submit; $("#modal-submit").className = `button ${danger ? "danger" : "primary"}`;
  $("#modal-cancel").classList.toggle("hidden", locked); $("#modal-close").classList.toggle("hidden", locked);
  $("#modal-error").textContent = ""; state.modalAction = action; modal.showModal();
}
function closeModal() { if (modal.open) modal.close(); state.modalAction = null; }
function reasonField(label = "操作原因") { return `<label>${escapeHtml(label)}<textarea name="reason" maxlength="500" required placeholder="说明本次管理操作的原因，将写入审计日志"></textarea></label>`; }
function openPasswordModal(locked = false) {
  openModal({ kicker: "账户安全", title: locked ? "修改初始密码" : "修改密码", submit: "更新密码", locked,
    body: `<label>当前密码<input name="current_password" type="password" autocomplete="current-password" required></label><label>新密码<input name="new_password" type="password" autocomplete="new-password" minlength="12" required></label><label>确认新密码<input name="confirm_password" type="password" autocomplete="new-password" minlength="12" required></label><p class="muted">至少 12 位，并使用大写、小写、数字、符号中的三类。</p>`,
    action: async form => { if (form.get("new_password") !== form.get("confirm_password")) throw new Error("两次输入的新密码不一致"); const result = await api("/api/account/password", { method: "POST", body: JSON.stringify({ current_password: form.get("current_password"), new_password: form.get("new_password") }) }); state.account = result.account; $("#password-banner").classList.add("hidden"); closeModal(); toast("密码已更新"); await loadView(state.view); }
  });
}
function openAdminAccount() {
  openModal({ title: "新建管理账户", submit: "创建账户", body: `<label>用户名<input name="username" pattern="[A-Za-z][A-Za-z0-9_.-]{2,63}" required></label><label>角色<select name="role"><option value="auditor">审计员（只读）</option><option value="admin">管理员</option></select></label>${reasonField()}`, action: async form => {
    const account = await api("/api/admin-accounts", { method: "POST", body: JSON.stringify({ username: form.get("username"), role: form.get("role"), reason: form.get("reason") }) });
    openModal({ kicker: "只显示一次", title: "账户已创建", submit: "完成", body: `<p>${escapeHtml(account.username)} 首次登录后必须修改初始密码。</p><div id="issued-secret" class="secret-box">${escapeHtml(account.bootstrap_password)}</div><button type="button" class="button secondary" data-copy-secret>复制初始密码</button>`, action: async () => { closeModal(); await loadView("runtime"); } });
  } });
}
function openIssueToken() {
  openModal({ title: "签发 MCP Token", submit: "签发", body: `<label>用户标识<input name="user_subject" required></label><div><strong>各系统预期主体</strong><p class="muted">已有绑定可以留空；首次开通某个系统时必须填写该系统识别到的账号或主体。</p></div><label>OA 主体<input name="principal_oa" maxlength="256"></label><label>日志系统主体<input name="principal_taihua" maxlength="256"></label><label>语雀主体<input name="principal_yuque" maxlength="256"></label><label>标签<input name="label" maxlength="120"></label><label>有效小时数<input name="ttl_hours" type="number" min="1" max="2160" value="720" required></label><p class="muted">令牌仅用于身份认证。已有用户沿用当前授权；新用户默认无业务能力，签发后请在用户操作中配置业务能力和数据库能力。</p>${reasonField()}`,
    action: async form => { const principal_bindings = Object.fromEntries(["oa", "taihua", "yuque"].map(system => [system, String(form.get(`principal_${system}`) || "").trim()]).filter(([, principal]) => principal)); const issued = await api("/api/tokens", { method: "POST", body: JSON.stringify({ user_subject: form.get("user_subject"), principal_bindings, label: form.get("label"), ttl_hours: Number(form.get("ttl_hours")), reason: form.get("reason") }) }); openModal({ kicker: "只显示一次", title: "Token 已签发", submit: "完成", body: `<p>请将密钥配置到可信 MCP 客户端。关闭后无法再次查看。</p><div id="issued-secret" class="secret-box">${escapeHtml(issued.token_secret)}</div><button type="button" class="button secondary" data-copy-secret>复制密钥</button>`, action: async () => { closeModal(); await loadView("users"); } }); }
  });
}
function openRenewToken(target) {
  const requestId = crypto.randomUUID();
  const tokenId = target.dataset.renewToken;
  const priorExpiry = target.dataset.expiresAt;
  const revision = Number(target.dataset.editRevision);
  const defaultExpiry = new Date(Math.min(Date.now() + 89 * 86400000, Math.max(Date.now() + 30 * 86400000, new Date(priorExpiry).getTime() + 86400000)));
  const localValue = new Date(defaultExpiry.getTime() - defaultExpiry.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  openModal({ title: "管理员续期", submit: "续期", body: `<p>当前到期：${escapeHtml(fmtTime(priorExpiry))}。原 Token 密钥不变，客户端无需重新配置。</p><label>新的到期时间<input name="new_expires_at" type="datetime-local" value="${localValue}" required></label>${reasonField()}`,
    action: async form => { const value = String(form.get("new_expires_at") || ""); await api(`/api/tokens/${tokenId}/renew`, { method: "POST", body: JSON.stringify({ new_expires_at: new Date(value).toISOString(), expected_revision: revision, request_id: requestId, reason: form.get("reason") }) }); closeModal(); toast("Token 已续期，原凭证继续使用"); await loadView("users"); }
  });
}
function openRebindSession(target) {
  openModal({ title: "修改系统身份绑定", submit: "修改并清除登录态", danger: true, body: `<label>预期下游主体<input name="expected_principal_ref" maxlength="256" value="${escapeHtml(target.dataset.expectedPrincipal || "")}" required></label><p class="muted">只影响这一用户在当前系统中的会话。修改后必须重新登录。</p>${reasonField()}`, action: async form => { await api(`/api/sessions/${target.dataset.rebindSession}/rebind`, { method: "POST", body: JSON.stringify({ expected_principal_ref: form.get("expected_principal_ref"), reason: form.get("reason") }) }); closeModal(); toast("系统身份绑定已更新"); await loadView("sessions"); } });
}
function openPause({ scopeType, scopeValue, version = "*", title }) {
  openModal({ title, submit: "暂停写入", danger: true, body: `<p>读取能力不受影响；新的准备动作和已经授权但尚未提交的写操作都会被阻断。</p>${reasonField()}`, action: async form => { await api("/api/policies/pause", { method: "POST", body: JSON.stringify({ scope_type: scopeType, scope_value: scopeValue, capability_version: version, reason: form.get("reason") }) }); closeModal(); toast("写暂停策略已生效"); await loadView(state.view); } });
}
function openReasonAction({ title, submit, danger = false, request }) {
  openModal({ title, submit, danger, body: reasonField(), action: async form => { await request(form.get("reason")); closeModal(); toast(`${title}已完成`); await loadView(state.view); } });
}
function openRuntimeRecovery({ actionType, targetId, title, submit, danger = false }) {
  openReasonAction({ title, submit, danger, request: reason => api("/api/recovery", { method: "POST", body: JSON.stringify({ action_type: actionType, target_id: targetId, reason, idempotency_key: crypto.randomUUID() }) }) });
}
async function openSessionHistory(sessionId) {
  const data = await api(`/api/sessions/${sessionId}/events?limit=100`);
  const rows = data.items.map(item => `<tr><td>${fmtTime(item.created_at)}</td><td>${escapeHtml(item.source)}</td><td>${escapeHtml(item.event_type)}</td><td>${escapeHtml(item.previous_state || "--")} → ${escapeHtml(item.new_state || "--")}</td><td class="truncate" title="${escapeHtml(item.reason || "")}">${escapeHtml(item.reason || "--")}</td></tr>`);
  openModal({ kicker: "SESSION HISTORY", title: "会话状态历史", submit: "关闭", body: `<p class="muted">保留失效、延期检查、重新登录等原因；后续成功登录不会覆盖旧记录。</p>${table(["时间", "来源", "事件", "状态变化", "原因"], rows)}`, action: async () => closeModal() });
}

$("#login-form").addEventListener("submit", async event => {
  event.preventDefault(); $("#login-error").textContent = ""; const loginForm = event.currentTarget; const form = new FormData(loginForm);
  try { const result = await api("/api/login", { method: "POST", body: JSON.stringify({ username: form.get("username"), password: form.get("password") }) }); loginForm.reset(); showApp(result.account); if (result.account.must_change_password) openPasswordModal(true); else await loadView("overview"); }
  catch (error) { $("#login-error").textContent = error.message; }
});
$("#nav").addEventListener("click", event => { const button = event.target.closest("button[data-view]"); if (button) loadView(button.dataset.view); });
$("#refresh-button").addEventListener("click", () => loadView(state.view));
$("#account-button").addEventListener("click", () => {
  const expanded = $("#account-menu").classList.contains("hidden");
  $("#account-menu").classList.toggle("hidden", !expanded);
  $("#account-button").setAttribute("aria-expanded", String(expanded));
});
$("#change-password-button").addEventListener("click", () => { $("#account-menu").classList.add("hidden"); openPasswordModal(false); });
$("#banner-password-button").addEventListener("click", () => openPasswordModal(true));
$("#logout-button").addEventListener("click", async () => { try { await api("/api/logout", { method: "POST", body: "{}" }); } finally { showLogin(); } });
$("#modal-close").addEventListener("click", closeModal); $("#modal-cancel").addEventListener("click", closeModal);
$("#modal-form").addEventListener("submit", async event => { event.preventDefault(); if (!state.modalAction) return; $("#modal-error").textContent = ""; $("#modal-submit").disabled = true; try { await state.modalAction(new FormData(event.currentTarget)); } catch (error) { $("#modal-error").textContent = error.message; } finally { $("#modal-submit").disabled = false; } });
content.addEventListener("input", event => { if (event.target.matches("[data-filter-search]")) applyFilter(event.target); });
content.addEventListener("change", event => { if (event.target.matches("[data-filter-status], [data-incident-category]")) applyFilter(event.target); });
content.addEventListener("click", async event => {
  try {
  const governanceTab = event.target.closest("[data-governance-tab]"); if (governanceTab) { selectGovernanceTab(governanceTab.dataset.governanceTab); return; }
  const tab = event.target.closest("[data-coordination-tab]"); if (tab) { selectCoordinationTab(tab.dataset.coordinationTab); return; }
  const target = event.target.closest("button"); if (!target) return;
  if (target.hasAttribute("data-row-actions")) { openRowActions(target); return; }
  if (target.closest("#row-action-menu")) closeRowActions();
  if (target.dataset.openView) loadView(target.dataset.openView);
  else if (target.dataset.incidentDetail) await selectIncident(target.dataset.incidentDetail);
  else if (target.dataset.traceDetail) await openTraceDetail(target.dataset.traceDetail);
  else if (target.dataset.incidentAction) openReasonAction({ title: `${target.textContent.trim()}运行事件`, submit: target.textContent.trim(), danger: target.dataset.incidentAction === "suppress", request: reason => api(`/api/incidents/${target.dataset.incidentId}/${target.dataset.incidentAction}`, { method: "POST", body: JSON.stringify({ reason }) }) });
  else if (target.matches("[data-evaluate-runtime]")) openRuntimeRecovery({ actionType: "evaluate_runtime", targetId: "runtime", title: "立即评估运行状态", submit: "开始评估" });
  else if (target.dataset.recoveryAction) openRuntimeRecovery({ actionType: target.dataset.recoveryAction, targetId: target.dataset.recoveryTarget, title: target.dataset.recoveryAction === "retry_delivery" ? "重新投递通知" : "归档投递记录", submit: target.dataset.recoveryAction === "retry_delivery" ? "重新投递" : "归档", danger: target.dataset.recoveryAction === "archive_delivery" });
  else if (target.matches("[data-issue-token]")) openIssueToken();
  else if (target.matches("[data-create-admin]")) openAdminAccount();
  else if (target.hasAttribute("data-source-new")) await openSource();
  else if (target.dataset.sourceEdit) await openSource(target.dataset.sourceEdit);
  else if (target.dataset.sourceAction) await sourceAction(target.dataset.sourceId, target.dataset.sourceAction);
  else if (target.dataset.sourceGrants) await sourceGrantUser(target.dataset.sourceGrants);
  else if (target.dataset.userGrants) await openUserGrants(target.dataset.userGrants);
  else if (target.dataset.skillUser) await openSkillConfig(target.dataset.skillUser);
  else if (target.hasAttribute("data-skill-global")) await openSkillConfig();
  else if (target.dataset.databaseGrants) await openDatabaseGrants(target.dataset.databaseGrants);
  else if (target.dataset.pauseUser) openPause({ scopeType: "user", scopeValue: target.dataset.pauseUser, title: `暂停 ${target.dataset.pauseUser} 的写入` });
  else if (target.dataset.pauseCapability) openPause({ scopeType: "capability", scopeValue: target.dataset.pauseCapability, version: target.dataset.version, title: "暂停具体能力" });
  else if (target.dataset.pauseSystem) openPause({ scopeType: "system", scopeValue: target.dataset.pauseSystem, title: "暂停系统写入" });
  else if (target.matches("[data-global-pause]")) openPause({ scopeType: "global", scopeValue: "*", title: "全局暂停所有写入" });
  else if (target.dataset.renewToken) openRenewToken(target);
  else if (target.dataset.revokeToken) openReasonAction({ title: "撤销 Token", submit: "撤销", danger: true, request: reason => api(`/api/tokens/${target.dataset.revokeToken}/revoke`, { method: "POST", body: JSON.stringify({ reason }) }) });
  else if (target.dataset.checkSession) openReasonAction({ title: "实时检查会话", submit: "检查", request: reason => api(`/api/sessions/${target.dataset.checkSession}/check`, { method: "POST", body: JSON.stringify({ reason }) }) });
  else if (target.dataset.sessionHistory) await openSessionHistory(target.dataset.sessionHistory);
  else if (target.dataset.rebindSession) openRebindSession(target);
  else if (target.dataset.invalidateSession) openReasonAction({ title: "使会话失效", submit: "确认失效", danger: true, request: reason => api(`/api/sessions/${target.dataset.invalidateSession}/invalidate`, { method: "POST", body: JSON.stringify({ reason }) }) });
  else if (target.dataset.resumePolicy) openReasonAction({ title: "恢复写入", submit: "恢复", request: reason => api(`/api/policies/${target.dataset.resumePolicy}/resume`, { method: "POST", body: JSON.stringify({ reason }) }) });
  } catch (error) {
    if (error.name === "AbortError") return;
    if (error.status === 401) { showLogin(); return; }
    toast(`操作未完成：${error.message}`, true);
  }
});
modal.addEventListener("click", event => { if (event.target.matches("[data-copy-secret]")) navigator.clipboard.writeText($("#issued-secret").textContent).then(() => toast("密钥已复制"), () => toast("复制失败，请手动选择", true)); });
function setNavigation(open) {
  $("#app").classList.toggle("navigation-open", open);
  $("#navigation-toggle").setAttribute("aria-expanded", String(open));
  $("#navigation-toggle").setAttribute("aria-label", open ? "收起导航" : "展开导航");
  $("#navigation-toggle").title = open ? "收起导航" : "展开导航";
}
$("#navigation-toggle").addEventListener("click", () => setNavigation(!$("#app").classList.contains("navigation-open")));
document.addEventListener("click", event => {
  if (!event.target.closest("#account-button, #account-menu")) {
    $("#account-menu").classList.add("hidden");
    $("#account-button").setAttribute("aria-expanded", "false");
  }
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") { closeRowActions(true); setNavigation(false); $("#account-menu").classList.add("hidden"); $("#account-button").setAttribute("aria-expanded", "false"); }
  const tab = event.target.closest("[data-governance-tab]");
  if (tab && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const tabs = [...content.querySelectorAll("[data-governance-tab]")];
    const index = tabs.indexOf(tab);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    selectGovernanceTab(tabs[next].dataset.governanceTab);
    tabs[next].focus();
  }
});
initialize();


async function renderSkillReviews() {
  const result=await api("/api/skill-reviews");
  const labels={submitted:"待审批",published:"已发布",changes_requested:"需修改",rejected:"已拒绝",withdrawn:"已撤回"};
  content.innerHTML=`<p>普通用户的个人发布与共享发布均在此审批。通过后立即生效，不授予业务权限。请核对固定修订、版本差异、使用范围及评测报告。</p>`+table(["申请人","助手与修订","范围","状态","提交时间","操作"],result.items.map(r=>`<tr><td>${escapeHtml(r.owner_subject)}</td><td>${escapeHtml(r.name||'历史助手')} / ${r.draft_revision}</td><td>${escapeHtml(r.audience.join("、"))}</td><td>${escapeHtml(labels[r.state]||r.state)}</td><td>${fmtTime(r.created_at)}</td><td><button class="button secondary" data-skill-review="${escapeHtml(r.request_id)}">查看申请</button></td></tr>`));
  if(result.metrics){const ratings={useful:'有帮助',incorrect:'结果有误',not_applicable:'不适用'};content.insertAdjacentHTML('beforeend',`<details><summary>版本反馈与后台任务</summary><p>用户反馈不等于独立成功率，加载数量不作为质量证明。</p>${result.metrics.feedback.map(r=>`<p>${escapeHtml(r.skill_id)} · ${escapeHtml(r.version)} · ${ratings[r.rating]} ${r.count} 次</p>`).join('')||'<p>暂无版本反馈。</p>'}${result.metrics.jobs.map(r=>`<p>${r.kind==='evaluation'?'评测':'生成'} · ${escapeHtml(r.state)} ${r.count} 项</p>`).join('')}</details>`);}
  content.querySelectorAll("[data-skill-review]").forEach(b=>b.onclick=()=>openSkillReview(b.dataset.skillReview).catch(e=>toast(e.message,true)));
}
async function openSkillReview(id) {
  const r=await api("/api/skill-reviews?id="+encodeURIComponent(id));const m=r.bundle.manifest;
  const canReview=state.account.role==="admin"&&r.state==="submitted";
  openModal({title:`${m.name} · 修订 ${r.draft_revision}`,submit:canReview?"提交审批决定":"关闭",body:`
    <p>${escapeHtml(m.description)}</p><p>申请人：${escapeHtml(r.owner_subject)} · 使用范围：${escapeHtml(r.audience.join("、"))}</p><p>发布说明：${escapeHtml(r.reason)}</p>
    <p>适用：${escapeHtml(m.selection.use_when)}<br>不适用：${escapeHtml(m.selection.not_for)}<br>输出：${escapeHtml(m.selection.output)}</p>
    ${skillReviewQualityHtml(r.quality)}
    <details open><summary>方法与依赖</summary><pre class="skill-review-text">${escapeHtml(JSON.stringify(m.profiles,null,2))}\n${escapeHtml(r.bundle.resources["SKILL.md"])}</pre></details>
    ${Object.entries(r.bundle.resources).filter(([k])=>k!=="SKILL.md").map(([k,v])=>`<details><summary>${escapeHtml(k)}</summary><pre class="skill-review-text">${escapeHtml(v)}</pre></details>`).join("")}
    <h3>样例结果（模型生成，待人工复核）</h3>${r.tests.map(t=>`<details open><summary>${escapeHtml(t.profile)} · ${escapeHtml(t.prompt)}</summary><pre class="skill-review-text">${escapeHtml(t.output)}</pre></details>`).join("")}
    ${canReview?`<label>决定<select name="decision"><option value="changes_requested">退回修改</option><option value="approve">通过并发布</option><option value="rejected">拒绝</option></select></label><label><input type="checkbox" name="reviewed_tests">我已检查方法、依赖、脱敏、发布范围和全部样例结果</label>${reasonField("审批意见")}`:`<p>${escapeHtml(r.decision_reason||r.state)}</p>`}`,
    action:async f=>{if(canReview)await api("/api/skill-reviews",{method:"POST",body:JSON.stringify({request_id:id,decision:f.get("decision"),reviewed_tests:f.get("reviewed_tests")==="on",reason:f.get("reason"),manual_quality_reason:f.get("manual_quality_reason")||null})});closeModal();await loadView("skillReviews");}});
}

function skillReviewQualityHtml(q){
  if(!q)return '<p>历史申请未记录独立评测及版本差异。</p>';
  const report=q.evaluation;const labels={candidate:'候选版本',published:'线上版本',without_skill:'不使用助手'};
  return `<h3>固定修订质量依据</h3><p>${escapeHtml(q.diagnostics.message)}</p>${q.diagnostics.checks.filter(c=>c.level==='warning').map(c=>`<p>待复核：${escapeHtml(c.message)}</p>`).join('')}
    <details><summary>版本差异（${q.diff.length} 项）</summary>${q.diff.map(c=>`<p>${escapeHtml(c.field)}</p><pre class="skill-review-text">${escapeHtml(c.diff??JSON.stringify({原来:c.before,现在:c.after},null,2))}</pre>`).join('')}</details>
    <p>新增使用用户：${escapeHtml(q.audience_diff.added.join('、')||'无')}；移除：${escapeHtml(q.audience_diff.removed.join('、')||'无')}</p>
    ${report?`<p><strong>${report.passed?'独立断言通过，语义仍需人工复核':'独立评测未通过，不能批准'}</strong></p><p>${escapeHtml(report.limitations)}</p>
    ${Object.entries(report.scores).map(([k,v])=>`<p>${labels[k]}：${v.passed}/${v.total}</p>`).join('')}
    ${report.rows.map(r=>`<details><summary>${labels[r.variant]} · ${escapeHtml(r.prompt||'模式不可用')} · ${r.passed?'通过':'未通过'}</summary><pre class="skill-review-text">${escapeHtml(r.output||r.skipped)}</pre><p>${escapeHtml(r.model||'')}</p></details>`).join('')}`:
    `<p>尚无独立评测，仅有人工样例。批准时须说明人工验证方法；此说明不会被标为独立评测通过。</p><label>人工验证说明<textarea name="manual_quality_reason" maxlength="1000">${escapeHtml(q.manual_quality_reason||'')}</textarea></label>`}`;
}
