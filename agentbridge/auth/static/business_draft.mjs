/** Trusted-page state only. Business values never leave this origin or persist in browser storage. */
export const DRAFT_FIELDS = ["log_date", "hours", "project", "content"];
const LOCKED_STATES = new Set(["approved", "finalized", "expired", "superseded", "canceled", "unavailable"]);
const isLocked = state => Boolean(state.receipt) || LOCKED_STATES.has(state.state);
const hasReviewChanged = state => [state.reviewChanged, state.preview?.reviewChanged,
  state.preview?.summary?.reviewChanged, state.ui?.review?.reviewChanged].some(value => value === true);
export const localDateTime = value => String(value ?? "").replace(" ", "T")
  .replace(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}):00(?:\.0+)?$/, "$1");

/** One business field may be a native input, a select, or an explicit radio choice. */
export function bindDraftField(root, definition = {}) {
  if (!root) throw new Error("办理字段缺失，请从原任务重新打开。");
  const grouped = definition.control === "segmented";
  const controls = grouped ? [...root.querySelectorAll('input[type="radio"]')] : [root];
  if (!controls.length) throw new Error("办理选项缺失，请从原任务重新打开。");
  return {
    get value() { return grouped ? controls.find(control => control.checked)?.value || "" : String(root.value ?? ""); },
    set value(value) {
      const next = String(value ?? "");
      if (grouped) controls.forEach(control => { control.checked = control.value === next; });
      else root.value = next;
    },
    get validity() { return {valid: controls.every(control => control.validity?.valid !== false)}; },
    get validationMessage() { return controls.find(control => control.validity?.valid === false)?.validationMessage || ""; },
    checkValidity() { return controls.every(control => control.checkValidity()); },
    set disabled(value) { controls.forEach(control => { control.disabled = value; }); },
    setAttribute(name, value) { root.setAttribute(name, value); controls.forEach(control => control.setAttribute(name, value)); },
  };
}

function decisionKey() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(24)), value => value.toString(16).padStart(2, "0")).join("");
}

export function safeFlowUrl(value, origin) {
  try {
    const url = new URL(value, origin);
    if (url.origin !== origin || url.username || url.password || url.search || url.hash ||
        !/^\/flow\/authorize\/[A-Za-z0-9_-]{32,128}(?:\/present\/[A-Za-z0-9_-]{32,128})?$/.test(url.pathname)) return null;
    return url;
  } catch { return null; }
}

export function createDraftController({initial, request, readValues, writeValues, render,
  valid = () => true, optionsValid = () => true, fieldNames = DRAFT_FIELDS,
  viewKind = initial.ui?.viewKind || "work_log", makeKey = decisionKey, onReceipt = () => {}, now = Date.now}) {
  let current = initial;
  let edit = 0;
  let saved = 0;
  let saving = false;
  let previewing = false;
  let previewAttempt = -1;
  let conflict = null;
  let message = "";
  let fieldErrors = {};
  let decision = null;
  let deciding = false;
  let permanentlyLocked = isLocked(initial);
  let receiptDelivered = false;
  let optionsLoading = false;
  let optionsAttempt = -1;
  let reviewChanged = hasReviewChanged(initial);
  let active = true, lifecycle = 0, restoring = false;
  const live = epoch => active && lifecycle === epoch;

  const allowed = action => !permanentlyLocked && current.allowedActions.includes(action);
  const optionsFresh = () => viewKind !== "meeting_room" || Boolean(current.options &&
    current.options.revision === current.revision && Date.parse(current.options.expiresAt) > now() &&
    ["start_time", "end_time"].every(name => localDateTime(current.options.dependsOn?.[name]) === localDateTime(readValues()[name])));
  const roomValid = () => viewKind !== "meeting_room" || optionsFresh() &&
    Boolean(current.options.items?.some(item => item.value === readValues().room && item.available));
  const materialReady = () => {
    const review = current.preview?.summary || current.ui?.review;
    return review?.truncated !== true && (!review?.completeness || review.completeness.status === "complete");
  };
  const ready = () => active && !restoring && saved === edit && !conflict && !saving && !previewing && !optionsLoading && !deciding && roomValid() &&
    materialReady() && allowed("approve") && current.preview?.revision === current.revision &&
    Date.parse(current.preview.expiresAt) > now();
  const view = () => ({current, dirty: saved !== edit, saving, previewing, optionsLoading,
    optionsFresh: optionsFresh(), materialReady: materialReady(), reviewChanged, conflict, message,
    fieldErrors, locked: permanentlyLocked || deciding, uncertain: deciding && !permanentlyLocked,
    canApprove: ready(), canEdit: active && allowed("save") && !deciding,
    canRetry: !conflict && !permanentlyLocked && Boolean(message) && !saving && !previewing && !optionsLoading});
  function emit() { if (active) render(view()); }
  function accept(next) {
    if (!next || !Number.isInteger(next.version) || !Number.isInteger(next.revision) ||
        !Array.isArray(next.allowedActions)) throw new Error("invalid draft response");
    if (next.version < current.version) return false;
    current = next;
    reviewChanged ||= hasReviewChanged(next);
    if (isLocked(next)) {
      permanentlyLocked = true;
      conflict = null;
      if (next.receipt && !receiptDelivered) {
        receiptDelivered = true;
        if (onReceipt(next.receipt) === false) message = "本次确认已记录，请返回原任务查看办理状态。";
      }
    }
    return true;
  }
  function fail(error) {
    message = error.message || "连接暂不可用，本页内容已保留。";
    fieldErrors = error.fieldErrors || {};
    if (error.current && error.current.version >= current.version) {
      accept(error.current);
      if (!permanentlyLocked && error.code === "DRAFT_REVISION_CONFLICT") conflict = current;
    }
    if (error.status === 403 || error.status === 410) permanentlyLocked = true;
  }
  async function preview(force = false) {
    if (!active || restoring || previewing || saving || optionsLoading || conflict || deciding || !allowed("preview") || saved !== edit || !valid() || !roomValid() ||
        (!force && (previewAttempt === edit || ready()))) return;
    const epoch = lifecycle;
    const revision = current.revision;
    const snapshot = edit;
    previewAttempt = snapshot;
    previewing = true;
    message = "";
    fieldErrors = {};
    emit();
    try {
      const result = await request("preview", {expectedRevision: revision});
      if (!live(epoch)) return;
      // Receipt/locking is authoritative even when this preview itself became obsolete.
      if (isLocked(result)) accept(result);
      else if (snapshot === edit && revision === current.revision) accept(result);
    } catch (error) {
      if (live(epoch) && snapshot === edit && revision === current.revision) fail(error);
    } finally {
      previewing = false;
      if (live(epoch)) {
        emit();
        if (snapshot !== edit && saved === edit && !conflict) void preview();
      }
    }
  }
  async function loadOptions(force = false) {
    if (!active || restoring || viewKind !== "meeting_room" || optionsLoading || previewing || saving || conflict || deciding ||
        !allowed("options") || saved !== edit || !optionsValid() ||
        (!force && (optionsFresh() || optionsAttempt === current.revision))) return;
    const epoch = lifecycle;
    const revision = current.revision;
    const snapshot = edit;
    optionsAttempt = revision;
    optionsLoading = true;
    message = "";
    emit();
    try {
      const result = await request("options", {expectedRevision: revision});
      if (!live(epoch)) return;
      if (isLocked(result)) accept(result);
      else if (snapshot === edit && revision === current.revision) accept(result);
    } catch (error) {
      if (live(epoch) && snapshot === edit && revision === current.revision) fail(error);
    } finally {
      optionsLoading = false;
      if (live(epoch)) {
        emit();
        if (snapshot !== edit && saved === edit && !conflict) void prepare();
      }
    }
  }
  async function prepare(force = false) {
    const epoch = lifecycle;
    await loadOptions(force);
    if (live(epoch)) await preview(force);
  }
  async function save() {
    if (!active || restoring || saving || conflict || deciding || !allowed("save")) return;
    const epoch = lifecycle;
    if (saved === edit) { await prepare(); return; }
    saving = true;
    message = "";
    fieldErrors = {};
    emit();
    let succeeded = false;
    try {
      while (live(epoch) && saved !== edit && !conflict && !permanentlyLocked) {
        const snapshot = edit;
        const values = Object.fromEntries(fieldNames.map(name => [name, String(readValues()[name] ?? "")]));
        const result = await request("values", {expectedRevision: current.revision, values});
        if (!live(epoch)) return;
        if (accept(result)) saved = snapshot;
        if (permanentlyLocked) break;
      }
      succeeded = true;
    } catch (error) { if (live(epoch)) fail(error); }
    finally { saving = false; if (live(epoch)) emit(); }
    if (live(epoch) && succeeded) await prepare();
  }
  async function poll({restore = false} = {}) {
    if (!active) return;
    if (!restore && restoring) { emit(); return; }
    if (!restore && (saving || permanentlyLocked)) return;
    const epoch = lifecycle;
    const previousPreview = current.preview;
    try {
      const result = await request("state");
      if (!live(epoch) || (!restore && saving) || result.version < current.version) return;
      const changed = result.revision !== current.revision;
      if (accept(result) && changed && !permanentlyLocked) {
        conflict = current;
        message = "另一页面更新了草稿。本页输入已保留，请核对差异后继续。";
      }
      if (restore && deciding && !current.receipt) {
        message = "正在查询原回执；如需恢复本次确认，将使用原请求，不会新建提交。";
      }
    } catch (error) {
      if (!live(epoch)) return;
      if (error.status === 403 || error.status === 410) fail(error);
      else if (deciding) message = "正在确认本次提交的状态，已填写内容不会重发。";
      else if (restore) {
        // A restoration must establish the current permission and revision
        // before a local edit can be saved or a decision can be made.
        message = "暂时无法核对最新草稿，本页输入已保留，请重新检查后继续。";
        return false;
      }
    }
    if (!live(epoch)) return;
    emit();
    const expired = current.preview ? Date.parse(current.preview.expiresAt) <= now() :
      Boolean(previousPreview && ["editable", "editing"].includes(current.state));
    if (!message && !conflict && (expired || viewKind === "meeting_room" && !optionsFresh())) await prepare(true);
    return true;
  }
  async function approve(retry = false) {
    if (!active || restoring || permanentlyLocked || (deciding && !retry) || (!decision && !ready())) return;
    const epoch = lifecycle;
    if (!decision) decision = {expectedRevision: current.revision,
      previewId: current.preview.previewId, requestKey: makeKey()};
    deciding = true;
    message = "";
    emit();
    try {
      const result = await request("approve", decision);
      if (!live(epoch)) return;
      accept(result);
      if (!result.receipt) throw new Error("批准回执暂未返回，正在查询本次提交状态。 ");
    } catch (error) {
      if (!live(epoch)) return;
      if (error.status >= 400 && error.status < 500) {
        deciding = false;
        decision = null;
        fail(error);
        // A changed-material response already contains a newly bound preview.
        // Keep it visible, but only another explicit click may approve it.
        if (error.code === "DRAFT_REVIEW_CHANGED" && current.preview?.revision === current.revision) {
          reviewChanged = true;
          previewAttempt = edit;
        } else {
          previewAttempt = -1;
          if (!permanentlyLocked) current = {...current, preview: null};
        }
      } else {
        message = "提交应答暂未确认。正在查询原回执；可恢复本次确认，不会新建提交。";
        await poll();
      }
    }
    if (live(epoch)) emit();
  }
  async function restorePage() {
    active = true;
    const epoch = ++lifecycle;
    restoring = true;
    emit();
    const fresh = await poll({restore: true});
    if (!live(epoch)) return;
    restoring = fresh !== true;
    // Unsent edits remain in the same DOM. A known revision is required
    // before autosaving; an uncertain decision is only read, never replayed.
    if (fresh === true && !conflict && !permanentlyLocked && !deciding) {
      previewAttempt = -1;
      optionsAttempt = -1;
      await save();
    }
    emit();
  }
  return {
    state: view,
    edit() {
      if (!active || !allowed("save") || deciding) return;
      edit += 1;
      message = "";
      fieldErrors = {};
      emit();
    },
    save, preview, loadOptions, poll, approve,
    suspend() { active = false; lifecycle++; },
    restore: restorePage,
    async retry() {
      if (!active) return;
      if (restoring) { await restorePage(); return; }
      if (decision) await approve(true);
      else {
        previewAttempt = -1; optionsAttempt = -1;
        if (saved === edit) await prepare(true); else await save();
      }
    },
    async adoptServer() {
      if (!active || restoring || !conflict || permanentlyLocked || deciding) return;
      writeValues(current.values);
      saved = ++edit;
      conflict = null;
      message = "";
      previewAttempt = -1;
      optionsAttempt = -1;
      emit();
      await prepare();
    },
    async keepLocal() {
      if (!active || restoring || !conflict || permanentlyLocked || deciding) return;
      conflict = null;
      edit += 1;
      message = "";
      await save();
    },
    start() { if (initial.receipt) accept(initial); emit(); if (!initial.receipt) void prepare(); },
  };
}

const KEY_REVIEW_LABELS = new Set(["事项", "申请人", "发起人", "姓名", "请假类型", "开始时间", "结束时间",
  "请假天数", "请假小时", "本次操作", "应付金额合计", "员工与月份", "移交人", "交接日期",
  "事由", "出差事由", "加班事由", "OA 当前确认结论", "日期", "发起日期"]);

// These layouts select existing evidence; they never calculate or invent business facts.
export const REVIEW_DESIGNS = {
  missed_punch: {title: "补签申请", family: "time", keys: ["申请人", "开始时间", "补签原因", "事由说明"]},
  efficiency_data: {title: "效能数据材料", family: "document", keys: []},
  travel_expense: {title: "差旅报销材料", family: "expense", keys: ["姓名", "应付金额合计", "费用归算类型", "费用归属部门", "费用归属事项", "关联出差申请单"]},
  business_trip: {title: "出差行程", family: "journey", keys: ["申请人", "出差始发地", "出差目的地", "出差开始时间", "出差结束时间", "出差工具", "出差天数", "出差小时数", "出差事由"]},
  labor_contract_renewal: {title: "劳动合同续签材料", family: "personnel", keys: ["员工信息", "合同期限", "综合评价", "续签建议", "综合评价指导意见"]},
  intellectual_property_declaration: {title: "知识产权申报材料", family: "ownership", keys: ["申报名称", "知识产权类型", "申请人", "申请部门", "权属类型", "权属单位", "发明/设计人", "申请用途"]},
  overtime: {title: "加班申请", family: "time", keys: ["姓名", "申请开始时间", "实际开始时间", "加班事由"]},
  leave: {title: "请假申请", family: "time", keys: ["申请人", "请假类型", "开始时间", "结束时间", "请假天数", "请假小时", "事由"]},
  resignation: {title: "离职申请材料", family: "personnel", keys: ["员工", "所属部门", "岗位", "入职时间", "申请离职时间", "离职原因"]},
  work_handover: {title: "工作与财务交接", family: "handover", keys: ["移交人", "移交人部门", "交接日期", "交接类别", "监交人"]},
  flight_application: {title: "乘机申请与航段", family: "journey", keys: ["申请人", "部门", "申请日期"]},
  attendance_confirmation: {title: "月度考勤核对", family: "attendance", keys: ["员工与月份", "出勤统计", "OA 当前确认结论"]},
  weekly_report: {title: "周报阅读", family: "document", keys: ["周报名称", "年度", "本周说明"]},
  quarterly_performance: {title: "季度绩效知会材料", family: "personnel", keys: ["考评人", "部门名称", "复核截止日期", "A等级占比", "B等级占比"]},
  standard_collaboration: {title: "协同事项材料", family: "document", keys: []},
};

export function reviewSections(review, kind = "") {
  const overview = [], groups = new Map();
  const design = Object.hasOwn(REVIEW_DESIGNS, kind) ? REVIEW_DESIGNS[kind] : null;
  const keys = design ? new Set(["事项", "发起人", "日期", ...design.keys]) : KEY_REVIEW_LABELS;
  const addGroup = (name, item) => { if (!groups.has(name)) groups.set(name, []); groups.get(name).push(item); };
  const normalize = fields => (Array.isArray(fields) ? fields : []).filter(item => item && typeof item === "object")
    .map(item => ({...item, label: String(item.label ?? "").trim(), value: String(item.value ?? "").trim()}))
    .filter(item => item.label && item.value);
  for (const item of normalize(review?.fields)) {
    const group = /^(航段\d+|工作明细\d+|财务明细\d+)\s*·/.exec(item.label);
    const long = item.value.length > 180 || item.value.split("\n").length > 3;
    if (group) addGroup(group[1], item);
    else if (keys.has(item.label)) {
      overview.push(long ? {...item, reading: undefined, value: item.value.slice(0, 160).replaceAll("\n", " ") + "…（全文见下方）"} : item);
      if (long) addGroup(item.label + " · 全文", item);
    } else addGroup(long ? item.label : "其他申请信息", item);
  }
  if (!overview.length && groups.has("其他申请信息")) overview.push(...groups.get("其他申请信息").splice(0, 6));
  for (const group of Array.isArray(review?.groups) ? review.groups : []) {
    for (const item of normalize(group?.fields)) addGroup(String(group.title || "申请明细"), item);
  }
  return {overview, groups: [...groups].filter(([, fields]) => fields.length)};
}

const READING_TAGS = new Set(["p", "h2", "br", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "th", "td",
  "strong", "em", "blockquote", "pre", "code", "span"]);

// The server projects source HTML into a closed reading tree. Never parse HTML
// here or copy source attributes: even an invalid projection stays inert text.
export function appendReading(document, target, text, reading) {
  if (!reading) { target.textContent = String(text ?? ""); return; }
  let budget = 20000;
  const valid = (item, depth = 0) => {
    if (--budget < 0 || depth > 64) return false;
    if (typeof item === "string") return true;
    if (!item || !READING_TAGS.has(item.tag) || !Array.isArray(item.children)) return false;
    if (["colspan", "rowspan"].some(key => item[key] !== undefined &&
      (!["td", "th"].includes(item.tag) || !Number.isInteger(item[key]) || item[key] < 1 || item[key] > 100))) return false;
    return item.children.every(child => valid(child, depth + 1));
  };
  if (reading.version !== 1 || !Array.isArray(reading.nodes) || !reading.nodes.every(item => valid(item)) ||
      !Array.isArray(reading.notices) || !reading.notices.every(item => typeof item === "string")) {
    target.textContent = String(text ?? "");
    return;
  }
  const root = document.createElement("div"); root.className = "material-reading";
  const append = (parent, item) => {
    if (typeof item === "string") {
      const span = document.createElement("span"); span.textContent = item; parent.append(span); return;
    }
    const element = document.createElement(item.tag);
    for (const key of ["colspan", "rowspan"]) if (item[key] !== undefined) element.setAttribute(key, String(item[key]));
    for (const child of item.children) append(element, child);
    if (item.tag === "table") {
      const scroll = document.createElement("div"); scroll.className = "reading-table";
      scroll.tabIndex = 0; scroll.setAttribute("aria-label", "正文表格"); scroll.append(element); parent.append(scroll);
    } else parent.append(element);
  };
  for (const item of reading.nodes) append(root, item);
  for (const notice of reading.notices) {
    const note = document.createElement("p"); note.className = "reading-notice";
    note.textContent = notice; root.append(note);
  }
  target.append(root);
}

function appendReviewRows(document, target, fields, wrapped = true) {
  for (const item of fields) {
    const row = wrapped ? document.createElement("div") : target;
    if (wrapped) row.className = "review-row";
    const label = document.createElement("dt"), value = document.createElement("dd");
    label.textContent = String(item.label ?? ""); appendReading(document, value, item.value, item.reading);
    row.append(label, value);
    if (wrapped) target.append(row);
  }
}

export function renderEvidence(document, target, review, kind = "") {
  kind ||= review?.kind || "";
  const design = Object.hasOwn(REVIEW_DESIGNS, kind) ? REVIEW_DESIGNS[kind] : null;
  const signature = JSON.stringify([review, kind]);
  if (target.dataset.signature === signature) return;
  const opened = new Map([...target.querySelectorAll("details")].map(item => [item.dataset.group, item.open]));
  const {overview, groups} = reviewSections(review, kind);
  const section = document.createElement("section"); section.className = `review review-${design?.family || "general"}`;
  section.dataset.reviewKind = kind;
  section.setAttribute("aria-labelledby", "draft-evidence-title");
  const kicker = document.createElement("p"); kicker.className = "review-kicker"; kicker.textContent = design?.title || "办理依据";
  const heading = document.createElement("h2"); heading.id = "draft-evidence-title"; heading.textContent = review?.title || design?.title || "办理详情";
  const effect = document.createElement("p"); effect.className = "review-effect"; effect.textContent = review?.effect || "";
  const facts = document.createElement("dl"); facts.className = "review-key-facts";
  const weeklySections = (review?.sections || []).filter(item => ["current-week", "next-week"].includes(item?.id));
  const weeklyValues = new Set(weeklySections.flatMap(item => item.fields || []).map(item => String(item.value || "").trim()));
  const weeklyLabels = new Set(weeklySections.flatMap(item => item.fields || []).map(item => String(item.label || "").trim()));
  const duplicatedWeekly = item => kind === "weekly_report" && (weeklyLabels.has(item.label) || weeklyValues.has(item.value));
  appendReviewRows(document, facts, overview.filter(item => !duplicatedWeekly(item)));
  section.append(kicker, heading, effect, facts);
  for (const [name, fields] of groups) {
    const visibleFields = fields.filter(item => !duplicatedWeekly(item));
    if (!visibleFields.length) continue;
    const detail = document.createElement("details"); detail.className = "review-group";
    detail.dataset.group = name; detail.open = opened.get(name) || false;
    const summary = document.createElement("summary"); summary.textContent = `${name} · ${visibleFields.length} 项`;
    const rows = document.createElement("dl"); appendReviewRows(document, rows, visibleFields);
    detail.append(summary, rows); section.append(detail);
  }
  let weeklyGrid = null;
  for (const material of Array.isArray(review?.sections) ? review.sections : []) {
    if (!material || !["facts", "text", "table"].includes(material.kind)) continue;
    const detail = document.createElement("details"); detail.className = `review-group material-${material.kind}`;
    detail.dataset.group = `material:${String(material.id || material.title || "")}`;
    const primary = ["current-week", "next-week", "handover-work", "handover-finance", "flight-legs"].includes(material.id) ||
      material.id === "body" && design?.family === "document" && (kind !== "weekly_report" || !weeklySections.length);
    detail.open = opened.has(detail.dataset.group) ? opened.get(detail.dataset.group) : primary;
    const summary = document.createElement("summary"); summary.textContent = String(material.title || "原始材料");
    detail.append(summary);
    if (material.kind === "facts") {
      const rows = document.createElement("dl"); appendReviewRows(document, rows, Array.isArray(material.fields) ? material.fields : []); detail.append(rows);
    } else if (material.kind === "text") {
      if (!primary && String(material.text || "").length > 1800) {
        const excerpt = document.createElement("p"); excerpt.className = "material-excerpt";
        excerpt.textContent = String(material.text).slice(0, 240) + "…（展开下方查看完整材料）";
        section.append(excerpt);
      }
      const text = document.createElement(material.reading ? "div" : "pre"); text.className = "material-body"; text.tabIndex = 0;
      appendReading(document, text, material.text, material.reading); detail.append(text);
    } else {
      const columns = (Array.isArray(material.columns) ? material.columns : []).filter(column => typeof column?.key === "string");
      const table = document.createElement("table"), head = document.createElement("thead"), body = document.createElement("tbody");
      const header = document.createElement("tr");
      for (const column of columns) { const cell = document.createElement("th"); cell.scope = "col"; cell.textContent = String(column.label || column.key); header.append(cell); }
      head.append(header);
      for (const row of Array.isArray(material.rows) ? material.rows : []) {
        const line = document.createElement("tr"); line.dataset.rowId = String(row?.id || "");
        for (const column of columns) {
          const cell = document.createElement("td");
          appendReading(document, cell, row?.cells?.[column.key], row?.readings?.[column.key]); line.append(cell);
        }
        body.append(line);
      }
      table.append(head, body); const scroll = document.createElement("div"); scroll.className = "material-table";
      scroll.tabIndex = 0; scroll.setAttribute("aria-label", String(material.title || "原始明细表"));
      scroll.append(table); detail.append(scroll);
    }
    if (kind === "weekly_report" && ["current-week", "next-week"].includes(material.id)) {
      if (!weeklyGrid) { weeklyGrid = document.createElement("div"); weeklyGrid.className = "material-primary-grid"; section.append(weeklyGrid); }
      weeklyGrid.append(detail);
    } else section.append(detail);
  }
  if (Array.isArray(review?.attachments) && review.attachments.length) {
    const attachments = document.createElement("section"); attachments.className = "material-attachments";
    const title = document.createElement("h3"); title.textContent = "附件名称";
    const list = document.createElement("ul");
    for (const attachment of review.attachments) { const item = document.createElement("li"); item.textContent = String(attachment?.name || "未命名附件"); list.append(item); }
    const note = document.createElement("p"); note.textContent = "此处仅列出源页面中的附件名称，附件内容未在本页读取。";
    attachments.append(title, list, note); section.append(attachments);
  }
  if (review?.completeness || review?.truncated === true) {
    const note = document.createElement("p"); note.className = "material-completeness";
    const complete = review?.completeness?.status === "complete" && review?.truncated !== true;
    note.textContent = complete ? "已读取当前受控页面的材料；此状态不包含附件正文或源系统未展开的历史。" :
      "当前材料尚未完整核对，请查看读取状态并补全材料后继续。";
    if (!complete) note.setAttribute("role", "status");
    for (const reason of Array.isArray(review?.completeness?.reasons) ? review.completeness.reasons : []) {
      const detail = document.createElement("span"); detail.textContent = ` ${String(reason)}`; note.append(detail);
    }
    section.append(note);
  }
  target.replaceChildren(section); target.dataset.signature = signature;
}

export function mountBusinessDraft(config, document, window) {
  const form = document.getElementById("draft-form");
  const fieldNames = config.fieldNames || DRAFT_FIELDS;
  const viewKind = config.viewKind || config.state.ui?.viewKind || "work_log";
  const definitions = config.fields || config.state.ui?.fields || [];
  const fields = Object.fromEntries(fieldNames.map(name => [name,
    bindDraftField(document.getElementById(name), definitions.find(field => field.name === name))]));
  const node = id => document.getElementById(id);
  const readValues = () => Object.fromEntries(fieldNames.map(name => [name, fields[name].value]));
  const setText = (id, value) => { node(id).textContent = String(value ?? ""); };
  const touched = new Set();
  const roomNodes = new Map();
  const roomLabels = new Map();
  let lastOptions = config.state.options;
  let saveTimer, pollTimer;
  let stopped = false, pageEpoch = 0;
  function edited(name) {
    touched.add(name); controller.edit();
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => void controller.save(), 450);
  }
  function renderRooms(view) {
    if (viewKind !== "meeting_room") return;
    if (view.current.options) lastOptions = view.current.options;
    const options = view.current.options || lastOptions;
    const items = Array.isArray(options?.items) ? options.items : [];
    for (const item of items) roomLabels.set(item.value, item.label);
    if (options?.selected) roomLabels.set(options.selected.value, options.selected.label);
    const live = new Set(items.map(item => item.value));
    for (const [value, entry] of roomNodes) if (!live.has(value)) { entry.label.remove(); roomNodes.delete(value); }
    for (const item of items) {
      let entry = roomNodes.get(item.value);
      if (!entry) {
        const label = document.createElement("label"); label.className = "room-option";
        const input = document.createElement("input"); input.type = "radio"; input.name = "room-choice"; input.value = item.value;
        const copy = document.createElement("span"), title = document.createElement("strong"), status = document.createElement("small");
        copy.append(title, status); label.append(input, copy); node("room-options").append(label);
        input.addEventListener("change", () => { if (input.checked && !input.disabled) { fields.room.value = input.value; edited("room"); } });
        entry = {label, input, title, status}; roomNodes.set(item.value, entry);
      }
      entry.title.textContent = item.label;
      entry.status.textContent = !view.optionsFresh ? "时段已变更或检查已到期" : item.available ? "可用" : "当前不可用";
      entry.input.checked = fields.room.value === item.value;
      entry.input.disabled = !view.canEdit || !view.optionsFresh || !item.available || view.optionsLoading;
    }
    const selected = options?.selected?.value === fields.room.value ? options.selected : items.find(item => item.value === fields.room.value);
    const unavailable = Boolean(fields.room.value) && (!view.optionsFresh || !items.some(item => item.value === fields.room.value && item.available));
    node("room-selected").hidden = !unavailable;
    setText("room-selected", `原选择：${selected?.label || roomLabels.get(fields.room.value) || "先前选择的会议室"}。${view.optionsFresh ? selected?.reason || "该选择已不可用，请重新选择会议室。" : "新时段尚未确认，旧选择不会自动替换。"}`);
    setText("room-status", view.optionsLoading ? "正在查询此时段的空闲会议室…" : !view.optionsFresh ?
      "请填写有效起止时间；保存后重新查询候选会议室。" : items.some(item => item.available) ?
      "请选择一个可用会议室。时间变化后会重新检查。" : "此时段没有可用会议室，请调整时间。");
  }
  const controller = createDraftController({
    initial: config.state, readValues, fieldNames, viewKind,
    writeValues(values) {
      for (const name of fieldNames) fields[name].value = ["start_time", "end_time"].includes(name) ?
        localDateTime(values[name]) : String(values[name] ?? "");
    },
    valid: () => form.checkValidity(),
    optionsValid: () => ["start_time", "end_time"].every(name => fields[name]?.value && fields[name].checkValidity()) &&
      fields.start_time.value < fields.end_time.value,
    async request(action, payload) {
      const response = await window.fetch(`${config.apiUrl}/${action}`, {
        method: payload ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
        headers: {"X-AgentBridge-Draft-Token": config.csrfToken,
          ...(payload ? {"Content-Type": "application/json"} : {})},
        ...(payload ? {body: JSON.stringify(payload)} : {}),
      });
      let result;
      try { result = await response.json(); }
      catch {
        const error = new Error(response.status === 403 ? "办理权限或页面会话已失效，请从原任务重新打开。" : "暂时无法读取办理状态，请保留本页后重试。");
        if (!response.ok) error.status = response.status;
        throw error;
      }
      if (!response.ok) {
        const error = new Error(result.error?.message || "暂时无法继续，请保留本页后重试。");
        Object.assign(error, {status: response.status, code: result.error?.code,
          fieldErrors: result.error?.fieldErrors, current: result.current});
        throw error;
      }
      return result;
    },
    onReceipt(receipt) {
      if (stopped) return false;
      const next = safeFlowUrl(receipt.flowUrl, window.location.origin);
      if (!next) return false;
      next.hash = window.location.hash;
      window.location.replace(next.href);
    },
    render(view) {
      for (const name of fieldNames) {
        fields[name].disabled = !view.canEdit;
        const error = view.fieldErrors[name] || (touched.has(name) && !fields[name].validity.valid ? fields[name].validationMessage : "");
        fields[name].setAttribute("aria-invalid", error ? "true" : "false");
        setText(`error-${name}`, error);
      }
      renderRooms(view);
      node("draft-submit").disabled = !view.canApprove;
      node("draft-retry").hidden = !view.canRetry;
      setText("draft-retry", view.uncertain ? "恢复本次确认" : "重新检查");
      setText("draft-error", view.message);
      node("draft-error").hidden = !view.message;
      node("draft-material-change").hidden = !view.reviewChanged;
      setText("draft-status", view.locked ? (view.current.receipt ? "已确认，正在接续办理" : view.uncertain ? "正在确认提交状态" : "此草稿已停止编辑") :
        view.conflict ? "需要核对另一页面的修改" : view.saving ? "正在保存草稿" : view.dirty ? "有修改，正在准备检查" :
        view.optionsLoading ? "正在查询空闲会议室" : view.previewing ? (viewKind === "work_log" ? "正在解析项目并检查重复日志" : "正在核对申请信息") :
        !view.materialReady ? "原始材料尚未完整核对，暂不能确认提交" :
        view.canApprove ? (view.reviewChanged ? "请重新核对更新后的材料，再确认提交" : "已检查，可以确认提交") : "填写完整后自动检查");
      node("draft-conflict").hidden = !view.conflict;
      if (view.conflict) {
        const local = readValues();
        for (const name of fieldNames) {
          const readable = value => !value ? "（空）" : name === "room" && viewKind === "meeting_room" ?
            roomLabels.get(value) || "未在当前候选中找到的会议室" : value;
          setText(`conflict-local-${name}`, readable(local[name]));
          setText(`conflict-server-${name}`, readable(view.current.values[name]));
          node(`conflict-row-${name}`).hidden = local[name] === String(view.current.values[name] ?? "");
        }
      }
      const preview = !view.dirty && view.current.preview?.revision === view.current.revision ? view.current.preview : null;
      const summary = !view.conflict ? preview?.summary : null;
      if (viewKind === "approval" || view.current.ui?.review) renderEvidence(document, node("draft-evidence"),
        preview?.summary || view.current.ui?.review || {}, view.current.ui?.reviewKind || "");
      node("draft-review").hidden = !summary;
      if (summary) {
        setText("review-effect", summary.effect || view.current.ui?.effect || "执行已核对的本次操作");
        const normalized = Array.isArray(summary.fields) ? summary.fields : [];
        if (viewKind === "work_log") {
          setText("review-project", summary.project?.name || "未关联项目");
          setText("review-project-code", summary.project ? summary.project.code || "未提供编码" : "—");
          setText("review-duplicate", summary.duplicateCheck?.message || "未发现日期、工时、项目和内容完全相同的日志");
          setText("review-content", normalized.find(item => item.label === "日志内容")?.value ?? readValues().content);
        } else {
          const shown = viewKind === "approval" ? normalized.filter(item => ["本次操作", "处理意见"].includes(item.label)) : normalized;
          const signature = JSON.stringify(shown);
          if (node("review-fields").dataset.signature !== signature) {
            node("review-fields").replaceChildren(); appendReviewRows(document, node("review-fields"), shown, false);
            node("review-fields").dataset.signature = signature;
          }
        }
      }
    },
  });
  form.addEventListener("input", event => { if (fieldNames.includes(event.target.name)) edited(event.target.name); });
  form.addEventListener("submit", event => { event.preventDefault(); void controller.approve(); });
  node("draft-retry").addEventListener("click", () => void controller.retry());
  node("draft-use-server").addEventListener("click", () => void controller.adoptServer());
  node("draft-keep-local").addEventListener("click", () => void controller.keepLocal());
  window.addEventListener("beforeunload", event => {
    if (controller.state().dirty && !controller.state().locked) { event.preventDefault(); event.returnValue = ""; }
  });
  window.addEventListener("pagehide", () => {
    stopped = true; pageEpoch++;
    window.clearTimeout(saveTimer); window.clearTimeout(pollTimer);
    controller.suspend();
  });
  window.addEventListener("pageshow", event => {
    if (!event.persisted || !stopped) return;
    stopped = false;
    const epoch = ++pageEpoch;
    void controller.restore().finally(() => {
      if (!stopped && pageEpoch === epoch) pollTimer = window.setTimeout(poll, 2500);
    });
  });
  async function poll() {
    if (stopped) return;
    const epoch = pageEpoch;
    await controller.poll();
    if (!stopped && pageEpoch === epoch) pollTimer = window.setTimeout(poll, 2500);
  }
  controller.start();
  pollTimer = window.setTimeout(poll, 2500);
  return controller;
}
