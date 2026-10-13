/** Trusted-page state only. Business values never leave this origin or persist in browser storage. */
export const DRAFT_FIELDS = ["log_date", "hours", "project", "content"];
const LOCKED_STATES = new Set(["approved", "finalized", "expired", "superseded", "canceled", "unavailable"]);
const isLocked = state => Boolean(state.receipt) || LOCKED_STATES.has(state.state);

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
  valid = () => true, makeKey = decisionKey, onReceipt = () => {}, now = Date.now}) {
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

  const allowed = action => !permanentlyLocked && current.allowedActions.includes(action);
  const ready = () => saved === edit && !conflict && !saving && !previewing && !deciding &&
    allowed("approve") && current.preview?.revision === current.revision &&
    Date.parse(current.preview.expiresAt) > now();
  const view = () => ({current, dirty: saved !== edit, saving, previewing, conflict, message,
    fieldErrors, locked: permanentlyLocked || deciding, uncertain: deciding && !permanentlyLocked,
    canApprove: ready(), canEdit: allowed("save") && !deciding,
    canRetry: !conflict && !permanentlyLocked && Boolean(message) && !saving && !previewing});
  function emit() { render(view()); }
  function accept(next) {
    if (!next || !Number.isInteger(next.version) || !Number.isInteger(next.revision) ||
        !Array.isArray(next.allowedActions)) throw new Error("invalid draft response");
    if (next.version < current.version) return false;
    current = next;
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
    if (previewing || saving || conflict || deciding || !allowed("preview") || saved !== edit || !valid() ||
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
  async function save() {
    if (saving || conflict || deciding || !allowed("save")) return;
    if (saved === edit) { await preview(); return; }
    saving = true;
    message = "";
    fieldErrors = {};
    emit();
    let succeeded = false;
    try {
      while (saved !== edit && !conflict && !permanentlyLocked) {
        const snapshot = edit;
        const values = Object.fromEntries(DRAFT_FIELDS.map(name => [name, String(readValues()[name] ?? "")]));
        const result = await request("values", {expectedRevision: current.revision, values});
        if (accept(result)) saved = snapshot;
        if (permanentlyLocked) break;
      }
      succeeded = true;
    } catch (error) { fail(error); }
    finally { saving = false; emit(); }
    if (succeeded) await preview();
  }
  async function poll() {
    if (saving || permanentlyLocked) return;
    const revision = current.revision;
    const previousPreview = current.preview;
    try {
      const result = await request("state");
      if (saving || result.version < current.version) return;
      const changed = result.revision !== revision;
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
    if (!message && !conflict && expired) await preview(true);
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
        // An explicit rejection invalidates the displayed preview until refreshed.
        previewAttempt = -1;
        if (!permanentlyLocked) current = {...current, preview: null};
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
    save, preview, poll, approve,
    async retry() { if (decision) await approve(true); else { previewAttempt = -1; await save(); } },
    async adoptServer() {
      if (!conflict || permanentlyLocked || deciding) return;
      writeValues(current.values);
      saved = ++edit;
      conflict = null;
      message = "";
      previewAttempt = -1;
      emit();
      await preview();
    },
    async keepLocal() {
      if (!conflict || permanentlyLocked || deciding) return;
      conflict = null;
      edit += 1;
      message = "";
      await save();
    },
    start() { if (initial.receipt) accept(initial); emit(); if (!initial.receipt) void preview(); },
  };
}

export function mountBusinessDraft(config, document, window) {
  const form = document.getElementById("draft-form");
  const fields = Object.fromEntries(DRAFT_FIELDS.map(name => [name, document.getElementById(name)]));
  const node = id => document.getElementById(id);
  const readValues = () => Object.fromEntries(DRAFT_FIELDS.map(name => [name, fields[name].value]));
  const setText = (id, value) => { node(id).textContent = String(value ?? ""); };
  const touched = new Set();
  let saveTimer;
  const controller = createDraftController({
    initial: config.state, readValues,
    writeValues(values) { for (const name of DRAFT_FIELDS) fields[name].value = String(values[name] ?? ""); },
    valid: () => form.checkValidity(),
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
      for (const name of DRAFT_FIELDS) {
        fields[name].disabled = !view.canEdit;
        const error = view.fieldErrors[name] || (touched.has(name) && !fields[name].validity.valid ? fields[name].validationMessage : "");
        fields[name].setAttribute("aria-invalid", error ? "true" : "false");
        setText(`error-${name}`, error);
      }
      node("draft-submit").disabled = !view.canApprove;
      node("draft-retry").hidden = !view.canRetry;
      setText("draft-retry", view.uncertain ? "恢复本次确认" : "重新检查");
      setText("draft-error", view.message);
      node("draft-error").hidden = !view.message;
      setText("draft-status", view.locked ? (view.current.receipt ? "已确认，正在接续办理" : view.uncertain ? "正在确认提交状态" : "此草稿已停止编辑") :
        view.conflict ? "需要核对另一页面的修改" : view.saving ? "正在保存草稿" : view.dirty ? "有修改，正在准备检查" :
        view.previewing ? "正在解析项目并检查重复日志" : view.canApprove ? "已检查，可以确认提交" : "填写完整后自动检查");
      node("draft-conflict").hidden = !view.conflict;
      if (view.conflict) {
        const local = readValues();
        for (const name of DRAFT_FIELDS) {
          setText(`conflict-local-${name}`, local[name] || "（空）");
          setText(`conflict-server-${name}`, view.current.values[name] || "（空）");
          node(`conflict-row-${name}`).hidden = local[name] === String(view.current.values[name] ?? "");
        }
      }
      const summary = view.canApprove ? view.current.preview?.summary : null;
      node("draft-review").hidden = !summary;
      if (summary) {
        setText("review-project", summary.project?.name || "未关联项目");
        setText("review-project-code", summary.project ? summary.project.code || "未提供编码" : "—");
        setText("review-duplicate", summary.duplicateCheck?.message || "未发现日期、工时、项目和内容完全相同的日志");
        setText("review-effect", summary.effect || "正式创建并提交一条个人工作日志");
        const normalized = Array.isArray(summary.fields) ? summary.fields : [];
        setText("review-content", normalized.find(item => item.label === "日志内容")?.value ?? readValues().content);
      }
    },
  });
  form.addEventListener("input", event => {
    if (DRAFT_FIELDS.includes(event.target.name)) touched.add(event.target.name);
    controller.edit();
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => void controller.save(), 450);
  });
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
