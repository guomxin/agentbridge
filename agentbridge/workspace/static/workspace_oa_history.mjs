import { reconcileChildren } from "./workspace_dom.mjs";
import { isCancellation } from "./workspace_lifecycle.mjs";

const SCHEMA = "agentbridge.oa_history_query.v1";
const WORKING = new Set(["scanning", "details"]);
const STATES = new Set([...WORKING, "complete", "partial"]);
const FLOW_LABELS = { running: "流转中", active: "流转中", pending: "处理中",
  completed: "已结束", finished: "已结束", ended: "已结束", revoked: "已撤销",
  canceled: "已取消", terminated: "已终止", unknown: "未读到" };
const ENDED = new Set(["completed", "finished", "ended", "revoked", "canceled", "terminated"]);
const PARTIAL_REASONS = {
  source_timeout: "读取 OA 来源超时", source_read_failed: "未能继续读取 OA 来源",
  source_row_budget: "已达到本次查询的事项读取上限", detail_item_budget: "已达到本次查询的详情核对上限",
  execution_time_budget: "已达到本次查询的总用时上限", source_changed: "读取期间来源列表发生变化",
  source_metadata_invalid: "未能核实来源的分页或总数", source_duplicate_ids: "来源出现重复事项，无法确认列表完整",
  source_count_mismatch: "已读事项数与来源报告的总数不一致", source_date_unverified: "未能核实来源日期",
  source_date_filter_mismatch: "来源日期与查询条件不一致", detail_fields_unavailable: "部分当前状态或节点字段未读到",
};
const copy = value => typeof value === "string" && value.trim() ? value.trim() : "";
const count = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : "未确认";
const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const valid = value => object(value) && value.schema_version === SCHEMA && copy(value.query_id)
  && ["done", "sent"].includes(value.collection) && STATES.has(value.state)
  && Array.isArray(value.items) && value.items.every(object);
const unavailable = value => object(value) && value.schema_version === SCHEMA && copy(value.query_id)
  && ["unavailable", "error"].includes(value.state);
const pageFrom = value => value?.oaHistoryQuery ?? value;
const working = value => WORKING.has(value?.state);
const nextCursor = value => value?.has_more === true && copy(value.next_cursor) ? value.next_cursor : null;
const olderWork = (value, latest) => working(value) && latest && (!working(latest)
  || ["scanned_count", "details_checked"].some(key => Number.isSafeInteger(value.progress?.[key])
    && Number.isSafeInteger(latest.progress?.[key]) && value.progress[key] < latest.progress[key])
  || value.state === "scanning" && latest.state === "details");

export function oaHistoryPresentation(value) {
  if (unavailable(value)) return {title: "OA 历史查询", label: value.state === "error" ? "读取失败" : "暂不可读", statusClass: "failed",
    message: /SESSION|LOGIN|AUTHORIZATION|PERMISSION/.test(copy(value.error?.code))
      ? "当前登录或权限已变化，旧结果已清除。请确认登录与权限后重新查询。"
      : "当前无法读取该查询，旧结果已清除。请重试或重新查询。"};
  if (!valid(value)) return null;
  const complete = value.state === "complete" && value.coverage?.status === "complete";
  const title = value.collection === "done" ? "OA 已办历史查询" : "OA 已发历史查询";
  if (value.state === "scanning") return {title, label: "读取中", statusClass: "running", message: "正在读取符合条件的历史事项，尚未查全。"};
  if (value.state === "details") return {title, label: "核对中", statusClass: "running", message: "正在核对匹配事项的当前状态和流程节点，详情尚未读完。"};
  if (complete) return {title, label: "已查全", statusClass: "succeeded", message: "本次查询已完成；有后续结果页时请翻页查看。"};
  const scanComplete = value.coverage?.scan_complete === true;
  const reason = PARTIAL_REASONS[value.completion_reason || value.coverage?.completionReason];
  const scope = scanComplete ? "事项列表已查全，部分详情或状态字段未读到。" : "本次查询尚未查全，以下为已读取的部分结果。";
  return {title, label: scanComplete ? "详情不完整" : "未查全", statusClass: "partially_succeeded",
    message: `${scope}${reason ? `停止原因：${reason}。` : "停止原因尚未确认。"}`};
}

function field(row, name) {
  return Array.isArray(row.unavailable_fields) && row.unavailable_fields.includes(name) ? null : row[name];
}
function names(value) {
  return Array.isArray(value) ? value.map(copy).filter(Boolean) : [];
}
function timeText(value) {
  const raw = copy(value);
  if (!raw) return "未读到";
  if (/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(raw)) {
    const stamp = Date.parse(raw);
    if (Number.isFinite(stamp)) return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).format(stamp);
  }
  return raw;
}
function rowValues(row) {
  const handlers = names(field(row, "current_handlers"));
  const nodes = field(row, "current_nodes");
  const nodeText = Array.isArray(nodes) ? nodes.filter(object).map(node => {
    const name = copy(node.name), assigned = names(node.handlers);
    return name ? `${name}${assigned.length ? `（${assigned.join("、")}）` : ""}` : "";
  }).filter(Boolean).join("；") : "";
  const ended = ENDED.has(row.workflow_state);
  const state = field(row, "workflow_state_text");
  const stateText = copy(state) || FLOW_LABELS[field(row, "workflow_state")] || "未读到";
  return [copy(row.title) || "标题未读到", timeText(field(row, "initiated_at")),
    timeText(field(row, "processed_at")), stateText,
    nodeText || (ended && Array.isArray(nodes) && !nodes.length ? "无当前节点（流程已结束）" : "未读到"),
    handlers.join("、") || (ended && Array.isArray(field(row, "current_handlers")) ? "无当前处理人（流程已结束）" : "未读到"),
    timeText(field(row, "observed_at"))];
}

// In-memory, account-scoped paging shared by every task for one trusted query.
// Work cursors advance the query on the same result page; delivery cursors turn
// result pages. Neither path prepares, authorizes, or submits an OA business.
export function createOaHistoryResults({ document, state = {}, getScope, loadHistoryPage }) {
  const models = state.oaHistoryQueries ||= new Map();
  const queries = new Map(), versions = new Map(), blockedQueries = new Map();
  const roots = new WeakMap();
  const node = (tag, className = "", text = "") => {
    const value = document.createElement(tag); value.className = className; value.textContent = text; return value;
  };
  const current = model => !model.disposed && model.taskIds.size > 0 &&
    (model.queryId ? queries.get(model.queryId) === model : models.get(model.taskId) === model) &&
    (!model.scope || model.scope.current());
  function clear(model, notice = "", {blocked = false} = {}) {
    model.generation++; model.pages = []; model.index = 0; model.projection = null;
    model.failureProjection = blocked && model.queryId ? {schema_version: SCHEMA,
      query_id: model.queryId, state: "unavailable", error: {code: "OA_HISTORY_UNAVAILABLE"}} : null;
    model.pending = false; model.notice = notice;
    model.firstSignature = null;
    model.blocked = blocked;
    if (blocked && model.queryId) blockedQueries.set(model.queryId, {scope: model.scope, projection: model.failureProjection});
    for (const view of model.views) { view.rows.clear(); view.body.replaceChildren(); }
  }
  function dispose(model) {
    clear(model); model.disposed = true;
    for (const view of model.views) { view.root.replaceChildren(); view.root.hidden = true; roots.delete(view.root); }
    model.views.clear();
    for (const taskId of model.taskIds) if (models.get(taskId) === model) { models.delete(taskId); versions.delete(taskId); }
    model.taskIds.clear();
    if (queries.get(model.queryId) === model) queries.delete(model.queryId);
  }
  function modelFor(taskId, queryId) {
    const previous = models.get(taskId);
    let model = queryId ? queries.get(queryId) : previous?.queryId === null ? previous : null;
    if (!model) {
      model = {taskId, taskIds: new Set(), queryId, pages: [], index: 0, projection: null, views: new Set(),
        generation: 0, pending: false, notice: "", scope: getScope?.(), disposed: false, blocked: false};
      const blocked = blockedQueries.get(queryId);
      if (blocked && (!blocked.scope || blocked.scope.current())) {
        model.blocked = true; model.failureProjection = blocked.projection;
      }
      if (queryId) queries.set(queryId, model);
      model.scope?.defer(() => {
        dispose(model);
        if (blockedQueries.get(queryId)?.scope === model.scope) blockedQueries.delete(queryId);
      });
    }
    if (previous && previous !== model) {
      previous.taskIds.delete(taskId);
      if (previous.taskId === taskId) previous.taskId = previous.taskIds.values().next().value || null;
      if (previous.requestTaskId === taskId) { previous.generation++; previous.pending = false; }
      for (const view of [...previous.views]) if (view.taskId === taskId) {
        previous.views.delete(view); view.model = model; model.views.add(view);
      }
      // Preserve an access-loss tombstone while this account scope is alive.
      if (!previous.taskIds.size && !previous.blocked) dispose(previous);
      else update(previous);
    }
    model.taskIds.add(taskId); model.taskId ||= taskId; models.set(taskId, model);
    return model;
  }
  function create(root, model, taskId) {
    root.className = "oa-history-panel";
    const heading = node("strong", "oa-history-heading");
    const range = node("p", "oa-history-range"), progress = node("p", "oa-history-progress");
    const explanation = node("p", "oa-history-explanation", "当前状态以各行读取时间为准，不代表所选日期当时的历史状态；时间按上海时区展示。");
    const notice = node("p", "oa-history-notice"); notice.setAttribute("role", "status"); notice.setAttribute("aria-live", "polite");
    const empty = node("p", "oa-history-empty");
    const scroll = node("div", "oa-history-scroll"); scroll.setAttribute("tabindex", "0");
    scroll.setAttribute("role", "region"); scroll.setAttribute("aria-label", "OA 历史查询结果，可横向滚动");
    const table = node("table", "oa-history-table"), head = node("thead"), header = node("tr"), body = node("tbody");
    for (const label of ["标题", "提交／发起时间", "我的处理时间", "流程状态", "当前节点", "当前处理人", "读取时间"]) {
      const cell = node("th", "", label); cell.setAttribute("scope", "col"); header.append(cell);
    }
    head.append(header); table.append(head, body); scroll.append(table);
    const navigation = node("div", "oa-history-navigation"); navigation.setAttribute("aria-label", "OA 历史查询分页");
    const previous = node("button", "secondary", "上一页"), next = node("button", "secondary", "下一页");
    previous.type = next.type = "button";
    const position = node("span", "oa-history-page");
    previous.addEventListener("click", () => {
      const model = view.model;
      if (previous.disabled || !current(model) || model.pending || model.index <= 0) return;
      model.index--; model.notice = ""; update(model);
    });
    next.addEventListener("click", () => { if (!next.disabled) void advance(view.model); });
    navigation.append(previous, position, next);
    root.append(heading, range, progress, explanation, notice, empty, scroll, navigation);
    const view = {root, heading, range, progress, explanation, notice, empty, scroll, body, navigation, previous, next, position, rows: new Map(), model, taskId};
    model.views.add(view); roots.set(root, view); return view;
  }
  function update(model) {
    if (!current(model)) return;
    const page = model.pages[model.index], projection = model.projection || model.failureProjection;
    const presentation = oaHistoryPresentation(projection);
    for (const view of model.views) {
      view.onPresentation?.(presentation);
      view.root.hidden = !presentation && !model.notice;
      view.root.setAttribute("aria-busy", String(model.pending));
      view.heading.textContent = presentation ? `${presentation.title} · ${presentation.label}` : "OA 历史查询";
      for (const part of [view.range, view.progress, view.explanation, view.navigation]) part.hidden = !valid(projection);
      view.scroll.hidden = !page?.items?.length;
      view.empty.hidden = !valid(projection) || Boolean(page?.items?.length);
      if (!valid(projection)) { view.notice.textContent = model.notice || presentation?.message || ""; continue; }
      const range = projection.temporal?.absoluteRange;
      const basis = {processed_at: "本人处理日期", initiated_at: "提交／发起日期"}[projection.temporal?.dateBasis] || "日期口径未确认";
      const precision = projection.temporal?.precision === "day" ? "按天，包含起止日" : "时间精度未确认";
      view.range.textContent = `${basis}：${copy(range?.start) || "不限"} 至 ${copy(range?.end) || "不限"}（${precision}）`;
      const progress = projection.progress || {};
      view.progress.textContent = `来源总数 ${count(progress.source_total)} · 已扫描 ${count(progress.scanned_count)} · 匹配 ${count(progress.matched_count)} · 已核对详情 ${count(progress.details_checked)} · 详情未读到 ${count(progress.details_failed)}`;
      view.notice.textContent = model.notice || presentation.message;
      if (projection.has_more === true && !nextCursor(projection) && !model.notice) view.notice.textContent += " 续读位置尚未返回，请重新查看任务进度。";
      view.empty.textContent = projection.state === "scanning" ? "正在扫描历史事项，结果准备中。"
        : projection.state === "details" ? "正在读取匹配事项的详情，结果准备中。"
        : projection.state === "complete" && projection.coverage?.status === "complete" && progress.matched_count === 0
          ? "该范围内没有匹配事项。" : "本页尚无可展示事项，不能据此认定没有匹配记录。";
      const items = page?.items || [], desired = [], retained = new Set();
      items.forEach((item, ordinal) => {
        const key = `${copy(item.affair_id)}:${ordinal}`; retained.add(key);
        let row = view.rows.get(key);
        if (!row) { const root = node("tr"), cells = Array.from({length: 7}, () => node("td")); root.append(...cells); row = {root, cells}; view.rows.set(key, row); }
        rowValues(item).forEach((value, index) => { if (row.cells[index].textContent !== value) row.cells[index].textContent = value; });
        desired.push(row.root);
      });
      for (const key of view.rows.keys()) if (!retained.has(key)) view.rows.delete(key);
      reconcileChildren(view.body, desired);
      view.previous.hidden = working(projection);
      view.previous.disabled = model.pending || model.index === 0;
      view.position.textContent = working(projection) ? "结果准备中" : `第 ${model.index + 1} 页 · 本页 ${items.length} 项`;
      view.next.hidden = !nextCursor(page) && model.index + 1 >= model.pages.length;
      view.next.disabled = model.pending || typeof loadHistoryPage !== "function" || (!nextCursor(page) && model.index + 1 >= model.pages.length);
      view.next.textContent = model.pending ? "正在读取…" : working(page) ? "继续读取" : "下一页";
    }
  }
  async function advance(model) {
    if (!current(model) || model.pending || typeof loadHistoryPage !== "function") return;
    if (model.index + 1 < model.pages.length) { model.index++; model.notice = ""; update(model); return; }
    const page = model.pages[model.index], cursor = nextCursor(page);
    if (!cursor) return;
    const queryId = model.queryId, generation = model.generation, index = model.index, taskId = model.taskId;
    model.requestTaskId = taskId;
    model.pending = true; model.notice = ""; update(model);
    const stillCurrent = () => current(model) && model.generation === generation && model.queryId === queryId;
    try {
      const response = pageFrom(await loadHistoryPage(taskId, cursor));
      if (!stillCurrent()) return;
      if (unavailable(response) && response.query_id === queryId) {
        clear(model, "", {blocked: true}); model.failureProjection = response; update(model); return;
      }
      if (!valid(response) || response.query_id !== queryId || response.collection !== page.collection) {
        throw new Error("OA_HISTORY_PAGE_MISMATCH");
      }
      if (olderWork(response, model.projection)) return;
      if (working(page)) { model.pages = [response]; model.index = 0; }
      else { model.pages[index + 1] = response; model.index = index + 1; }
      model.projection = response; model.notice = "";
    } catch (error) {
      if (!stillCurrent() || isCancellation(error)) return;
      if ([401, 403, 404, 410].includes(error?.status)) {
        clear(model, [401, 403].includes(error.status)
          ? "当前无权读取该查询，已清除旧结果。请确认登录与权限后重新查询。"
          : "该查询已过期或不可读取，已清除旧结果。请重新查询。", {blocked: true});
        update(model);
      } else model.notice = error?.message === "OA_HISTORY_PAGE_MISMATCH"
        ? "返回结果与当前查询不一致，已保留当前页，请重试。"
        : "本次读取未成功，当前页已保留。请重试同一次读取。";
    } finally { if (stillCurrent()) { model.pending = false; model.requestTaskId = null; update(model); } }
  }
  return {
    render(root, result, onPresentation) {
      const taskId = result?.task?.task_id, projection = result?.oaHistoryQuery;
      if (!taskId || !valid(projection) && !unavailable(projection) && !models.has(taskId)) { root.hidden = true; return; }
      const version = Number.isSafeInteger(result.task.version) ? result.task.version : null, previousVersion = versions.get(taskId);
      if (previousVersion !== undefined && version !== null && version < previousVersion) return;
      const previous = models.get(taskId);
      if (valid(projection) && previous?.queryId === null && previous.blocked &&
          (version === null || previousVersion === undefined || version <= previousVersion)) return;
      if (version !== null) versions.set(taskId, version);
      const model = modelFor(taskId, valid(projection) || unavailable(projection) ? projection.query_id : null);
      const old = roots.get(root);
      if (old && (old.model !== model || old.taskId !== taskId)) { old.model.views.delete(old); roots.delete(root); root.replaceChildren(); }
      if (!roots.has(root)) create(root, model, taskId);
      if (typeof onPresentation === "function") roots.get(root).onPresentation = onPresentation;
      if (unavailable(projection)) {
        clear(model, "", {blocked: true}); model.failureProjection = projection;
      } else if (!valid(projection)) {
        clear(model, "该任务当前未提供可读取的历史查询结果，已清除旧结果。", {blocked: true});
      } else if (!model.blocked) {
        // Task refreshes carry the first page. Do not replace a later report
        // page or reapply an earlier work cursor after manual continuation.
        const signature = JSON.stringify(projection);
        if (!olderWork(projection, model.projection) && signature !== model.firstSignature) {
          model.firstSignature = signature;
          model.pages[0] = projection;
          model.projection = projection;
          if (!model.pending) model.notice = "";
        }
      }
      update(model);
    },
    detach(root) { const view = roots.get(root); if (view) view.model.views.delete(view); roots.delete(root); },
    retire(taskId) {
      const model = models.get(taskId); versions.delete(taskId);
      if (!model) return;
      models.delete(taskId); model.taskIds.delete(taskId);
      for (const view of [...model.views]) if (view.taskId === taskId) {
        view.root.replaceChildren(); view.root.hidden = true; roots.delete(view.root); model.views.delete(view);
      }
      if (model.taskId === taskId) model.taskId = model.taskIds.values().next().value || null;
      if (!model.taskIds.size) dispose(model);
      else {
        if (model.requestTaskId === taskId) { model.generation++; model.pending = false; model.requestTaskId = null; }
        update(model);
      }
    },
    reset() {
      for (const model of new Set([...models.values(), ...queries.values()])) dispose(model);
      models.clear(); queries.clear(); versions.clear(); blockedQueries.clear();
    },
  };
}
