/** Trusted-page state only. Business values never leave this origin or persist in browser storage. */
export const DRAFT_FIELDS = ["log_date", "hours", "project", "content"];
const LOCKED_STATES = new Set(["approved", "finalized", "expired", "superseded", "canceled", "unavailable"]);
const isLocked = state => Boolean(state.receipt) || LOCKED_STATES.has(state.state);
const hasReviewChanged = state => [state.reviewChanged, state.preview?.reviewChanged,
  state.preview?.summary?.reviewChanged, state.ui?.review?.reviewChanged].some(value => value === true);
export const localDateTime = value => String(value ?? "").replace(" ", "T")
  .replace(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}):00(?:\.0+)?$/, "$1");

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

  const allowed = action => !permanentlyLocked && current.allowedActions.includes(action);
  const optionsFresh = () => viewKind !== "meeting_room" || Boolean(current.options &&
    current.options.revision === current.revision && Date.parse(current.options.expiresAt) > now() &&
    ["start_time", "end_time"].every(name => localDateTime(current.options.dependsOn?.[name]) === localDateTime(readValues()[name])));
  const roomValid = () => viewKind !== "meeting_room" || optionsFresh() &&
    Boolean(current.options.items?.some(item => item.value === readValues().room && item.available));
  const ready = () => saved === edit && !conflict && !saving && !previewing && !optionsLoading && !deciding && roomValid() &&
    allowed("approve") && current.preview?.revision === current.revision &&
    Date.parse(current.preview.expiresAt) > now();
  const view = () => ({current, dirty: saved !== edit, saving, previewing, optionsLoading,
    optionsFresh: optionsFresh(), reviewChanged, conflict, message,
    fieldErrors, locked: permanentlyLocked || deciding, uncertain: deciding && !permanentlyLocked,
    canApprove: ready(), canEdit: allowed("save") && !deciding,
    canRetry: !conflict && !permanentlyLocked && Boolean(message) && !saving && !previewing && !optionsLoading});
  function emit() { render(view()); }
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
    if (previewing || saving || optionsLoading || conflict || deciding || !allowed("preview") || saved !== edit || !valid() || !roomValid() ||
        (!force && (previewAttempt === edit || ready()))) return;
    const revision = current.revision;
    const snapshot = edit;
    previewAttempt = snapshot;
    previewing = true;
    message = "";
    fieldErrors = {};
    emit();
    try {
      const result = await request("preview", {expectedRevision: revision});
      // Receipt/locking is authoritative even when this preview itself became obsolete.
      if (isLocked(result)) accept(result);
      else if (snapshot === edit && revision === current.revision) accept(result);
    } catch (error) {
      if (snapshot === edit && revision === current.revision) fail(error);
    } finally {
      previewing = false;
      emit();
      if (snapshot !== edit && saved === edit && !conflict) void preview();
    }
  }
  async function loadOptions(force = false) {
    if (viewKind !== "meeting_room" || optionsLoading || previewing || saving || conflict || deciding ||
        !allowed("options") || saved !== edit || !optionsValid() ||
        (!force && (optionsFresh() || optionsAttempt === current.revision))) return;
    const revision = current.revision;
    const snapshot = edit;
    optionsAttempt = revision;
    optionsLoading = true;
    message = "";
    emit();
    try {
      const result = await request("options", {expectedRevision: revision});
      if (isLocked(result)) accept(result);
      else if (snapshot === edit && revision === current.revision) accept(result);
    } catch (error) {
      if (snapshot === edit && revision === current.revision) fail(error);
    } finally {
      optionsLoading = false;
      emit();
      if (snapshot !== edit && saved === edit && !conflict) void prepare();
    }
  }
  async function prepare(force = false) {
    await loadOptions(force);
    await preview(force);
  }
  async function save() {
    if (saving || conflict || deciding || !allowed("save")) return;
    if (saved === edit) { await prepare(); return; }
    saving = true;
    message = "";
    fieldErrors = {};
    emit();
    let succeeded = false;
    try {
      while (saved !== edit && !conflict && !permanentlyLocked) {
        const snapshot = edit;
        const values = Object.fromEntries(fieldNames.map(name => [name, String(readValues()[name] ?? "")]));
        const result = await request("values", {expectedRevision: current.revision, values});
        if (accept(result)) saved = snapshot;
        if (permanentlyLocked) break;
      }
      succeeded = true;
    } catch (error) { fail(error); }
    finally { saving = false; emit(); }
    if (succeeded) await prepare();
  }
  async function poll() {
    if (saving || permanentlyLocked) return;
    const previousPreview = current.preview;
    try {
      const result = await request("state");
      if (saving || result.version < current.version) return;
      const changed = result.revision !== current.revision;
      if (accept(result) && changed && !permanentlyLocked) {
        conflict = current;
        message = "另一页面更新了草稿。本页输入已保留，请核对差异后继续。";
      }
    } catch (error) {
      if (error.status === 403 || error.status === 410) fail(error);
      else if (deciding) message = "正在确认本次提交的状态，已填写内容不会重发。";
    }
    emit();
    const expired = current.preview ? Date.parse(current.preview.expiresAt) <= now() :
      Boolean(previousPreview && ["editable", "editing"].includes(current.state));
    if (!message && !conflict && (expired || viewKind === "meeting_room" && !optionsFresh())) await prepare(true);
  }
  async function approve(retry = false) {
    if (permanentlyLocked || (deciding && !retry) || (!decision && !ready())) return;
    if (!decision) decision = {expectedRevision: current.revision,
      previewId: current.preview.previewId, requestKey: makeKey()};
    deciding = true;
    message = "";
    emit();
    try {
      const result = await request("approve", decision);
      accept(result);
      if (!result.receipt) throw new Error("批准回执暂未返回，正在查询本次提交状态。 ");
    } catch (error) {
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
    emit();
  }
  return {
    state: view,
    edit() {
      if (!allowed("save") || deciding) return;
      edit += 1;
      message = "";
      fieldErrors = {};
      emit();
    },
    save, preview, loadOptions, poll, approve,
    async retry() {
      if (decision) await approve(true);
      else {
        previewAttempt = -1; optionsAttempt = -1;
        if (saved === edit) await prepare(true); else await save();
      }
    },
    async adoptServer() {
      if (!conflict || permanentlyLocked || deciding) return;
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
      if (!conflict || permanentlyLocked || deciding) return;
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

export function reviewSections(review) {
  const overview = [], groups = new Map();
  const addGroup = (name, item) => { if (!groups.has(name)) groups.set(name, []); groups.get(name).push(item); };
  const normalize = fields => (Array.isArray(fields) ? fields : []).filter(item => item && typeof item === "object")
    .map(item => ({label: String(item.label ?? "").trim(), value: String(item.value ?? "").trim()}))
    .filter(item => item.label && item.value);
  for (const item of normalize(review?.fields)) {
    const group = /^(航段\d+|工作明细\d+|财务明细\d+)\s*·/.exec(item.label);
    const long = item.value.length > 180 || item.value.split("\n").length > 3;
    if (group) addGroup(group[1], item);
    else if (KEY_REVIEW_LABELS.has(item.label)) {
      overview.push(long ? {...item, value: item.value.slice(0, 160).replaceAll("\n", " ") + "…（全文见下方）"} : item);
      if (long) addGroup(item.label + " · 全文", item);
    } else addGroup(long ? item.label : "其他申请信息", item);
  }
  if (!overview.length && groups.has("其他申请信息")) overview.push(...groups.get("其他申请信息").splice(0, 6));
  for (const group of Array.isArray(review?.groups) ? review.groups : []) {
    for (const item of normalize(group?.fields)) addGroup(String(group.title || "申请明细"), item);
  }
  return {overview, groups: [...groups].filter(([, fields]) => fields.length)};
}

function appendReviewRows(document, target, fields, wrapped = true) {
  for (const item of fields) {
    const row = wrapped ? document.createElement("div") : target;
    if (wrapped) row.className = "review-row";
    const label = document.createElement("dt"), value = document.createElement("dd");
    label.textContent = String(item.label ?? ""); value.textContent = String(item.value ?? "");
    row.append(label, value);
    if (wrapped) target.append(row);
  }
}

function renderEvidence(document, target, review) {
  const signature = JSON.stringify(review);
  if (target.dataset.signature === signature) return;
  const open = new Set([...target.querySelectorAll("details[open]")].map(item => item.dataset.group));
  const {overview, groups} = reviewSections(review);
  const section = document.createElement("section"); section.className = "review";
  section.setAttribute("aria-labelledby", "draft-evidence-title");
  const kicker = document.createElement("p"); kicker.className = "review-kicker"; kicker.textContent = "审批依据";
  const heading = document.createElement("h2"); heading.id = "draft-evidence-title"; heading.textContent = review?.title || "审批详情";
  const effect = document.createElement("p"); effect.className = "review-effect"; effect.textContent = review?.effect || "";
  const facts = document.createElement("dl"); facts.className = "review-key-facts";
  appendReviewRows(document, facts, overview); section.append(kicker, heading, effect, facts);
  for (const [name, fields] of groups) {
    const detail = document.createElement("details"); detail.className = "review-group";
    detail.dataset.group = name; detail.open = open.has(name);
    const summary = document.createElement("summary"); summary.textContent = `${name} · ${fields.length} 项`;
    const rows = document.createElement("dl"); appendReviewRows(document, rows, fields);
    detail.append(summary, rows); section.append(detail);
  }
  target.replaceChildren(section); target.dataset.signature = signature;
}

export function mountBusinessDraft(config, document, window) {
  const form = document.getElementById("draft-form");
  const fieldNames = config.fieldNames || DRAFT_FIELDS;
  const viewKind = config.viewKind || config.state.ui?.viewKind || "work_log";
  const fields = Object.fromEntries(fieldNames.map(name => [name, document.getElementById(name)]));
  const node = id => document.getElementById(id);
  const readValues = () => Object.fromEntries(fieldNames.map(name => [name, fields[name].value]));
  const setText = (id, value) => { node(id).textContent = String(value ?? ""); };
  const touched = new Set();
  const roomNodes = new Map();
  const roomLabels = new Map();
  let lastOptions = config.state.options;
  let saveTimer;
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
      const summary = view.canApprove ? preview?.summary : null;
      if (viewKind === "approval") renderEvidence(document, node("draft-evidence"), preview?.summary || view.current.ui?.review || {});
      node("draft-review").hidden = !summary;
      if (summary) {
        setText("review-effect", summary.effect || view.current.ui?.effect || "正式创建并提交一条个人工作日志");
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
  let stopped = false;
  window.addEventListener("pagehide", () => { stopped = true; window.clearTimeout(saveTimer); });
  async function poll() {
    if (stopped) return;
    await controller.poll();
    if (!stopped) window.setTimeout(poll, 2500);
  }
  controller.start();
  window.setTimeout(poll, 2500);
  return controller;
}
