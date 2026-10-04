export const statusText = {
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
export const statusClass = value => ["active", "succeeded", "approved", "submitted", "completed", "acknowledged", "eligible", "selected", "available", "healthy", "meeting", "resolved", "archived"].includes(value) ? "ok" :
  ["failed", "unknown", "outcome_unknown", "expired", "quarantined", "revoked", "rejected", "user_action_failed", "unavailable", "breached"].includes(value) ? "bad" :
  ["pending", "partially_succeeded", "delivering", "deferred", "waiting_activity", "awaiting_login", "awaiting_user", "waiting_user", "waiting", "paused", "awaiting_selection", "outside_lease", "user_action_expired", "user_action_rejected", "open", "acknowledged", "investigating"].includes(value) ? "warn" :
  ["running", "resume", "follow_up", "pull", "direct"].includes(value) ? "info" : "neutral";

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[character]);
}
export function fmtTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? escapeHtml(value) : date.toLocaleString("zh-CN", { hour12: false });
}
export function fmtBytes(value) {
  const size = Number(value || 0);
  if (!Number.isFinite(size) || size <= 0) return "--";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
export function fmtDuration(value) {
  const duration = Number(value || 0);
  if (!Number.isFinite(duration) || duration <= 0) return "--";
  if (duration < 1000) return `${Math.round(duration)} ms`;
  if (duration < 60000) return `${(duration / 1000).toFixed(1)} 秒`;
  return `${(duration / 60000).toFixed(1)} 分`;
}
export function fmtMetric(metric) {
  if (metric.value == null) return "--";
  if (metric.metricKey.endsWith("_rate") || metric.metricKey.endsWith("_coverage")) return `${(metric.value * 100).toFixed(1)}%`;
  if (metric.metricKey.endsWith("_ms")) return fmtDuration(metric.value);
  return Number(metric.value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}
export function boundaryLabel(value) {
  const labels = { B0_NO_EFFECT: "B0 无副作用", B1_READ_ONLY: "B1 只读", B2_INTERACTION_CREATED: "B2 已创建交互", B3_PREPARED_AUTHORIZED: "B3 已准备/授权", B4_COMMIT_ATTEMPTED: "B4 已尝试提交", B5_VERIFIED: "B5 已权威核验" };
  return labels[value] || value || "--";
}
export function shortId(value) { return value ? `${escapeHtml(value.slice(0, 8))}…` : "--"; }
export function badge(value) { return `<span class="status ${statusClass(value)}">${escapeHtml(statusText[value] || value || "未知")}</span>`; }
export function sessionStateBadge(session) {
  if (session.state === "active" && session.session_state_basis === "last_confirmed") {
    return `<span class="status warn" title="当前没有实时校验；这是最后一次确认成功时保存的状态">上次确认有效</span>`;
  }
  return badge(session.state);
}
export function empty(message) { return `<div class="empty">${escapeHtml(message)}</div>`; }
export function table(headers, rows, filterable = false) {
  if (!rows.length) return empty("暂无记录");
  return `<div class="table-shell"><table${filterable ? " data-filter-table" : ""}><thead><tr>${headers.map(item => `<th>${escapeHtml(item)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}
export function filterRow(searchText, status, cells) {
  return `<tr data-filter-text="${escapeHtml(String(searchText).toLowerCase())}" data-filter-status="${escapeHtml(status || "")}">${cells}</tr>`;
}
export function filteredTable(headers, rows, placeholder, statuses = []) {
  if (!rows.length) return empty("暂无记录");
  const options = statuses.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(statusText[value] || value)}</option>`).join("");
  return `<section class="data-block" data-filter-scope><div class="filters"><label class="filter-field"><span>搜索</span><input type="search" data-filter-search placeholder="${escapeHtml(placeholder)}"></label>${statuses.length ? `<label class="filter-field compact"><span>状态</span><select data-filter-status><option value="">全部状态</option>${options}</select></label>` : ""}<span class="filter-count" data-filter-count>${rows.length} 条</span></div>${table(headers, rows, true)}<div class="filter-empty hidden" data-filter-empty>没有匹配记录</div></section>`;
}

export function scopeBadges(scopes) { return `<div class="scope-pills">${scopes.map(scope => `<span>${escapeHtml(scope)}</span>`).join("")}</div>`; }
export function rowMenuButton(label, items) {
  if (!items.length) return "--";
  return `<button type="button" class="row-menu-trigger" data-row-actions="${escapeHtml(JSON.stringify(items))}" aria-label="${escapeHtml(label)}操作" aria-haspopup="dialog" aria-expanded="false">操作 <span aria-hidden="true">⌄</span></button>`;
}

export function skillReviewQualityHtml(q){
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
