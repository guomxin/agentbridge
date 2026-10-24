/** Trusted-page state only. Business values never leave this origin or persist in browser storage. */
import {renderBatchChecklist} from './batch_view.mjs';
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
    if (url.origin !== origin || url.username || url.password || (url.search && !/^\?result=[A-Za-z0-9_-]{43}$/.test(url.search)) || url.hash ||
        !/^\/flow\/authorize\/[A-Za-z0-9_-]{32,128}(?:\/present\/[A-Za-z0-9_-]{32,128})?$/.test(url.pathname)) return null;
    return url;
  } catch { return null; }
}

export function safeResultReturn(navigation, origin) {
  const kind = navigation?.back?.kind;
  if (!['result_list','workspace'].includes(kind)) return null;
  try {
    const url = kind === 'workspace' ? new URL(navigation.back.url) : new URL(navigation.back.url, origin);
    if (kind === 'workspace') return ['http:','https:'].includes(url.protocol) && !url.username && !url.password
      && !url.search && !url.hash ? url : null;
    return url.origin === origin && !url.username && !url.password && !url.search && !url.hash
      && /^\/results\/[A-Za-z0-9_-]{43}$/.test(url.pathname) ? url : null;
  } catch { return null; }
}

/** A timeout ends this browser wait, never the server operation or its original decision key. */
export function createDraftTransport({window, apiUrl, csrfToken, onProgress = () => {}, now = Date.now}) {
  const waiting = new Set();
  const limits = {state: 15000, values: 15000, options: 30000, preview: 45000, approve: 30000,
    'content-restore':45000,'content-discard':15000};
  async function request(action, payload) {
    const controller = new AbortController(), started = now();
    let timer, progress, finished = false, cancelWait, responseStatus;
    const entry = {action, cancel: () => { controller.abort(); cancelWait?.(Object.assign(new Error('页面已离开'), {name: 'AbortError'})); }};
    waiting.add(entry);
    const tick = () => {
      if (finished) return;
      onProgress({action, elapsedMs: now() - started, active: true, entry});
      progress = window.setTimeout(tick, 1000);
    };
    progress = window.setTimeout(tick, 1000);
    try {
      const receive = (async () => {
        const response = await window.fetch(`${apiUrl}/${action}`, {
          method: payload ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
          headers: {'X-AgentBridge-Draft-Token': csrfToken, ...(payload ? {'Content-Type': 'application/json'} : {})},
          ...(payload ? {body: JSON.stringify(payload)} : {}),
        });
        responseStatus = response.status;
        let result;
        try { result = await response.json(); }
        catch { throw Object.assign(new Error(response.status === 403 ? '办理权限或页面会话已失效，请从原任务重新打开。' :
          '暂时无法读取办理状态，请保留本页后重试。'), {status: response.ok ? undefined : response.status}); }
        if (!response.ok) throw Object.assign(new Error(result.error?.message || '暂时无法继续，请保留本页后重试。'),
          {status: response.status, code: result.error?.code, fieldErrors: result.error?.fieldErrors, current: result.current});
        return result;
      })();
      return await Promise.race([receive, new Promise((_, reject) => {
        cancelWait = reject;
        timer = window.setTimeout(() => {
          controller.abort();
          const denied = [401, 403, 404, 410].includes(responseStatus);
          reject(Object.assign(new Error(denied ? '办理入口或页面权限已失效，请从原任务重新打开。' :
            action === 'approve' ? '确认应答等待超时，需查询原回执。' :
            '等待应答已超时，本页输入已保留。请重新检查原草稿状态。'),
            {code: 'REQUEST_TIMEOUT', action, ...(denied ? {status: responseStatus} : {})}));
        }, limits[action] || 15000);
      })]);
    } finally {
      finished = true; window.clearTimeout(timer); window.clearTimeout(progress); waiting.delete(entry);
      onProgress({action, elapsedMs: now() - started, active: false, entry});
    }
  }
  return {request, suspend() { for (const entry of waiting) entry.cancel(); }};
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
  let active = true, lifecycle = 0, restoring = false, needsRead = false, decisionInFlight = false;
  let contentBusy = false, contentRequest = null, roomChoiceRequired = false;
  const live = epoch => active && lifecycle === epoch;

  const allowed = action => !permanentlyLocked && current.allowedActions.includes(action);
  const optionsFresh = () => viewKind !== "meeting_room" || Boolean(current.options &&
    current.options.revision === current.revision && Date.parse(current.options.expiresAt) > now() &&
    ["start_time", "end_time"].every(name => localDateTime(current.options.dependsOn?.[name]) === localDateTime(readValues()[name])));
  const roomValid = () => viewKind !== "meeting_room" || !roomChoiceRequired && optionsFresh() &&
    Boolean(current.options.items?.some(item => item.value === readValues().room && item.available));
  const materialReady = () => {
    const review = current.preview?.summary || current.ui?.review;
    return review?.truncated !== true && (!review?.completeness || review.completeness.status === "complete");
  };
  const contentAvailable = () => active && !restoring && !needsRead && !contentBusy && !contentRequest && !conflict && !saving && !previewing && !optionsLoading && !deciding && allowed('save') && Date.parse(current.expiresAt)>now();
  const ready = () => active && !contentBusy && !contentRequest && !restoring && !needsRead && saved === edit && !conflict && !saving && !previewing && !optionsLoading && !deciding && roomValid() &&
    materialReady() && allowed("approve") && current.preview?.revision === current.revision &&
    Date.parse(current.preview.expiresAt) > now();
  const view = () => ({current, dirty: saved !== edit, saving, previewing, optionsLoading,contentBusy,contentPending:contentRequest?.action,roomChoiceRequired,
    canRestoreContent:contentAvailable() && Boolean(current.contentDraft?.restore),
    canDiscardContent:contentAvailable() && Boolean(current.contentDraft?.restore || current.contentDraft?.saved),
    optionsFresh: optionsFresh(), materialReady: materialReady(), reviewChanged, conflict, message,
    fieldErrors, locked: permanentlyLocked || deciding, uncertain: deciding && !permanentlyLocked,
    canApprove: ready(), canEdit: active && allowed("save") && !deciding && !contentBusy && !contentRequest,
    canRetry: !conflict && !permanentlyLocked && (Boolean(message) || needsRead || restoring || contentRequest) && !contentBusy && !saving && !previewing && !optionsLoading && !decisionInFlight});
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
    if ([401, 403, 404, 410].includes(error.status)) permanentlyLocked = true;
    if (error.code === 'REQUEST_TIMEOUT') needsRead = true;
  }
  async function preview(force = false) {
    if (!active || contentBusy || contentRequest || restoring || needsRead || previewing || saving || optionsLoading || conflict || deciding || !allowed("preview") || saved !== edit || !valid() || !roomValid() ||
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
    if (!active || contentBusy || contentRequest || restoring || needsRead || viewKind !== "meeting_room" || optionsLoading || previewing || saving || conflict || deciding ||
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
  async function save({prepareAfter = true} = {}) {
    if (!active || contentBusy || contentRequest || restoring || needsRead || saving || conflict || deciding || !allowed("save")) return;
    const epoch = lifecycle;
    if (saved === edit) { if (prepareAfter) await prepare(); return; }
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
    if (live(epoch) && succeeded && prepareAfter) await prepare();
  }
  async function poll({restore = false} = {}) {
    if (!active) return;
    if (!restore && restoring) { emit(); return; }
    if (contentBusy || !restore && (saving || permanentlyLocked)) return;
    const epoch = lifecycle;
    const previousPreview = current.preview;
    try {
      const result = await request("state");
      if (!live(epoch) || contentBusy || (!restore && saving) || result.version < current.version) return;
      const changed = result.revision !== current.revision;
      if (accept(result) && changed && !permanentlyLocked && !contentRequest) {
        conflict = current;
        message = "另一页面更新了草稿。本页输入已保留，请核对差异后继续。";
      }
      if (restore && deciding && !current.receipt) {
        message = "正在查询原回执；如需恢复本次确认，将使用原请求，不会新建提交。";
      }
    } catch (error) {
      if (!live(epoch)) return;
      if ([401, 403, 404, 410].includes(error.status)) fail(error);
      else if (deciding) { message = "暂时无法读取原回执，请重新检查；已填写内容不会重发。"; emit(); return false; }
      else if (restore) {
        // A restoration must establish the current permission and revision
        // before a local edit can be saved or a decision can be made.
        message = "暂时无法核对最新草稿，本页输入已保留，请重新检查后继续。";
        emit(); return false;
      } else {
        needsRead = true;
        message = error.message || "暂时无法读取最新状态，本页输入已保留，请重新检查后继续。";
        emit(); return false;
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
    if (!active || restoring || permanentlyLocked || decisionInFlight || (deciding && !retry) || (!decision && !ready())) return;
    const epoch = lifecycle;
    if (!decision) decision = {expectedRevision: current.revision,
      previewId: current.preview.previewId, requestKey: makeKey()};
    deciding = true;
    decisionInFlight = true;
    message = "";
    emit();
    try {
      const result = await request("approve", decision);
      if (!live(epoch)) return;
      accept(result);
      if (!result.receipt) throw new Error("批准回执暂未返回，正在查询本次提交状态。 ");
    } catch (error) {
      if (!live(epoch)) return;
      if (error.status >= 400 && error.status < 500 && ![408, 425, 429].includes(error.status)) {
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
    } finally { decisionInFlight = false; }
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
    if (fresh === true) needsRead = false;
    // Unsent edits remain in the same DOM. A known revision is required
    // before autosaving; an uncertain decision is only read, never replayed.
    if (fresh === true && !conflict && !permanentlyLocked && !deciding && !contentRequest) {
      previewAttempt = -1;
      optionsAttempt = -1;
      await save();
    }
    emit();
  }
  async function contentOperation(action, retry = false) {
    if (!active || permanentlyLocked || contentBusy || deciding) return;
    if (!retry) {
      if (!contentAvailable()) return;
      await save({prepareAfter:false});
      if (!contentAvailable() || saved !== edit) return;
      const candidate = action === 'content-restore' ? current.contentDraft?.restore : current.contentDraft?.restore || current.contentDraft?.saved;
      if (!candidate) return;
      contentRequest = {action,payload:{expectedRevision:current.revision,contentId:candidate.contentId,
        contentVersion:candidate.version,requestKey:makeKey()}};
    }
    if (!contentRequest) return;
    const command = contentRequest, epoch = lifecycle;
    contentBusy = true; message = ''; emit();
    let completed = false;
    try {
      const result = await request(command.action,command.payload);
      if (!live(epoch)) return;
      if (accept(result) && !permanentlyLocked) {
        if (command.action === 'content-restore') {
          writeValues(current.values); saved = ++edit;
          roomChoiceRequired = viewKind === 'meeting_room';
          previewAttempt = optionsAttempt = -1; conflict = null;
        }
        message = command.action === 'content-restore' ? (viewKind === 'meeting_room' ? '已恢复填写内容；会议室需重新选择，尚未确认提交。' : '已恢复填写内容；正在重新检查当前材料，尚未确认提交。') : '已删除此事项的保留内容。本页输入不变；后续编辑会再次保存。';
        needsRead = false; completed = true;
      }
      contentRequest = null;
    } catch (error) {
      if (!live(epoch)) return;
      fail(error);
      if (error.status >= 400 && error.status < 500 && ![408,425,429].includes(error.status)) contentRequest = null;
      else message = '草稿操作应答尚未确认。请恢复本次草稿操作；不会确认或提交业务。';
    } finally { contentBusy = false; if (live(epoch)) emit(); }
    if (completed && live(epoch) && command.action === 'content-restore') await prepare(true);
  }
  return {
    state: view,
    edit(name) {
      if (!active || contentBusy || contentRequest || !allowed("save") || deciding) return;
      if (name === 'room') roomChoiceRequired = false;
      edit += 1;
      message = needsRead || restoring ? "本页输入已保留，请重新检查最新草稿后继续。" : "";
      fieldErrors = {};
      emit();
    },
    save, preview, loadOptions, poll, approve,
    restoreContent:() => contentOperation('content-restore'), discardContent:() => contentOperation('content-discard'),
    suspend() { active = false; lifecycle++; },
    restore: restorePage,
    async retry() {
      if (!active || permanentlyLocked) return;
      if (contentRequest) {
        if (await poll({restore:true}) === true && !permanentlyLocked) {needsRead = false; restoring = false; await contentOperation(contentRequest.action,true);}
        return;
      }
      if (restoring || needsRead) { await restorePage(); return; }
      if (decision) {
        // Read the durable original receipt before an explicit same-key recovery.
        // An unavailable read must never turn into another POST.
        if (await poll({restore: true}) === true && !permanentlyLocked) await approve(true);
      }
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
  work_log: {title: "工作日志核对", family: "document", keys: ["日志日期", "工时", "所属项目", "日志内容"]},
  meeting: {title: "会议安排", family: "journey", keys: ["会议主题", "会议室", "开始时间", "结束时间", "参会人"]},
  meeting_room_application: {title: "会议室使用安排", family: "journey", keys: ["用途", "会议室", "开始时间", "结束时间"]},
  meeting_room_cancel: {title: "会议室撤销核对", family: "journey", keys: ["会议室", "开始时间", "结束时间", "撤销原因"]},
  workflow_revoke: {title: "流程撤销核对", family: "document", keys: ["事项", "发起时间", "当前待办人", "撤销附言"]},
  alarm_remark: {title: "告警备注核对", family: "document", keys: ["告警设备", "告警类型", "告警 ID", "原备注", "新备注"]},
  meeting_reply: {title: "会议邀请", family: "document", keys: ["会议主题", "开始时间", "结束时间", "地点", "发起人", "主持人", "记录人", "回复人", "当前回执", "参会意向", "回复说明"]},
  missed_punch: {title: "补签申请", family: "time", keys: ["申请人", "开始时间", "补签原因", "事由说明"]},
  efficiency_data: {title: "效能数据材料", family: "document", keys: ["统计周期", "周期", "部门", "部门名称", "指标名称", "指标值"]},
  travel_expense: {title: "差旅报销材料", family: "expense", keys: ["姓名", "应付金额合计", "费用归算类型", "费用归属部门", "费用归属事项", "关联出差申请单"]},
  business_trip: {title: "出差行程", family: "journey", keys: ["申请人", "出差始发地", "出差目的地", "出差开始时间", "出差结束时间", "出差工具", "出差天数", "出差小时数", "出差事由"]},
  business_settlement: {title: "业务结算单材料", family: "expense", keys: ["流水号", "姓名", "发起部门", "日期", "结算方式", "费用归属部门", "结算实体", "费用归属事项", "应付金额合计", "账户类型", "收款账户名称", "收款账号", "收款账户开户行"]},
  labor_contract_renewal: {title: "劳动合同续签材料", family: "personnel", keys: ["员工信息", "合同期限", "综合评价", "续签建议", "综合评价指导意见"]},
  intellectual_property_declaration: {title: "知识产权申报材料", family: "ownership", keys: ["申报名称", "知识产权类型", "申请人", "申请部门", "权属类型", "权属单位", "发明/设计人", "申请用途"]},
  intellectual_property_reward: {title: "知识产权奖励知会材料", family: "ownership", keys: ["申请人", "工号", "所属部门", "申请日期", "奖项名称", "奖励金额", "奖励事由", "团队负责人", "团队负责人部门"]},
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

// The focus groups only select exact source labels. A title, an image or a
// numeric-looking string is never parsed into a new business fact.
export const REVIEW_FOCUS = {
  work_log: [["日期与工时", ["日志日期", "工时"]], ["项目与内容", ["所属项目", "日志内容"]]],
  meeting: [["会议与参会范围", ["会议主题", "参会人"]], ["时段与场地", ["开始时间", "结束时间", "会议室"]]],
  meeting_room_application: [["用途与场地", ["用途", "会议室"]], ["使用时段", ["开始时间", "结束时间"]]],
  meeting_room_cancel: [["原申请时段", ["会议室", "开始时间", "结束时间"]], ["撤销说明", ["撤销原因"]]],
  workflow_revoke: [["原流程", ["事项", "发起时间", "当前待办人"]], ["撤销说明", ["撤销附言"]]],
  alarm_remark: [["原告警", ["告警设备", "告警类型", "告警 ID"]], ["备注变更", ["原备注", "新备注"]]],
  meeting_reply: [["会议与时段", ["会议主题", "开始时间", "结束时间", "地点"]], ["本人回执", ["回复人", "当前回执", "参会意向", "回复说明"]]],
  efficiency_data: [["统计周期", ["统计周期", "周期"]], ["所属部门", ["部门", "部门名称"]], ["明确提供的指标", ["指标名称", "指标值"]]],
  travel_expense: [["金额与费用归属", ["应付金额合计", "费用归算类型", "费用归属部门", "费用归属事项"]], ["人员与关联申请", ["姓名", "关联出差申请单"]]],
  business_trip: [["行程与交通", ["出差始发地", "出差目的地", "出差工具"]], ["时段与口径", ["出差开始时间", "出差结束时间", "出差天数", "出差小时数"]], ["出差事由", ["出差事由"]]],
  business_settlement: [["账面金额与归属", ["应付金额合计", "费用归属部门", "费用归属事项", "结算实体"]], ["收款对象", ["账户类型", "收款账户名称", "收款账号", "收款账户开户行"]]],
  labor_contract_renewal: [["员工与期限", ["员工信息", "合同期限"]], ["原续签意见", ["综合评价", "续签建议", "综合评价指导意见"]]],
  intellectual_property_declaration: [["申报与权属", ["申报名称", "知识产权类型", "权属类型", "权属单位"]], ["申请主体", ["申请人", "申请部门", "发明/设计人"]]],
  intellectual_property_reward: [["原奖项与金额", ["奖项名称", "奖励金额", "奖励事由"]], ["人员与部门", ["申请人", "所属部门", "团队负责人", "团队负责人部门"]]],
  overtime: [["申请与实际时段", ["申请开始时间", "申请结束时间", "实际开始时间", "实际结束时间"]], ["申请事由", ["姓名", "加班事由"]]],
  leave: [["假别与时段", ["请假类型", "开始时间", "结束时间"]], ["天数与小时口径", ["请假天数", "请假小时"]], ["请假事由", ["申请人", "事由"]]],
  missed_punch: [["补签时段", ["开始时间", "结束时间"]], ["补签说明", ["申请人", "补签原因", "事由说明"]]],
  resignation: [["人员与日期", ["员工", "所属部门", "岗位", "入职时间", "申请离职时间"]], ["原申请说明", ["离职原因"]]],
  work_handover: [["交接责任", ["移交人", "移交人部门", "监交人"]], ["交接安排", ["交接日期", "交接类别"]]],
  flight_application: [["申请人员与日期", ["申请人", "部门", "申请日期"]]],
  attendance_confirmation: [["员工与统计月份", ["员工与月份"]], ["原出勤统计", ["出勤统计"]], ["当前确认结论", ["OA 当前确认结论"]]],
  weekly_report: [["周报与周期", ["周报名称", "年度"]], ["本周说明", ["本周说明"]]],
  quarterly_performance: [["原考评范围", ["考评人", "部门名称", "复核截止日期"]], ["原评级口径", ["A等级占比", "B等级占比"]]],
  standard_collaboration: [["事项与发起人", ["事项", "发起人", "日期"]]],
};

export function reviewSourceFields(review) {
  const result = [];
  const add = (fields, prefix) => (Array.isArray(fields) ? fields : []).forEach((field, index) => {
    if (!field || typeof field !== 'object' || !String(field.label ?? '').trim()) return;
    result.push({...field, label:String(field.label).trim(), value:String(field.value ?? ''), sourceAnchor:`${prefix}-${index}`});
  });
  add(review?.fields, 'review-field');
  (Array.isArray(review?.groups) ? review.groups : []).forEach((group, index) => add(group?.fields, `review-group-${index}-field`));
  (Array.isArray(review?.sections) ? review.sections : []).forEach((section, index) => {
    if (section?.kind === 'facts') add(section.fields, `review-section-${index}-field`);
  });
  return result;
}

export function reviewFocus(review, kind) {
  // Source projections may repeat a fact in overview and original sections.
  // Collapse only identical summary entries; all original rows remain below.
  const seen = new Set();
  const fields = reviewSourceFields(review).filter(field => {
    const key = JSON.stringify([field.label, field.value]);
    if (seen.has(key)) return false;
    seen.add(key); return true;
  });
  return (Object.hasOwn(REVIEW_FOCUS, kind) ? REVIEW_FOCUS[kind] : []).map(([title, labels]) => ({title,
    fields:fields.filter(field => labels.includes(field.label) && field.value.trim()),
    missing:!fields.some(field => labels.includes(field.label) && field.value.trim())}));
}

export function reviewSections(review, kind = "") {
  const overview = [], groups = new Map();
  const design = Object.hasOwn(REVIEW_DESIGNS, kind) ? REVIEW_DESIGNS[kind] : null;
  const keys = design ? new Set(["事项", "发起人", "日期", ...design.keys]) : KEY_REVIEW_LABELS;
  const addGroup = (name, item) => { if (!groups.has(name)) groups.set(name, []); groups.get(name).push(item); };
  const normalize = (fields, prefix) => (Array.isArray(fields) ? fields : []).map((item, index) => item && typeof item === "object"
    ? {...item, sourceAnchor:`${prefix}-${index}`, label: String(item.label ?? "").trim(), value: String(item.value ?? "").trim()} : null).filter(Boolean)
    .filter(item => item.label && item.value);
  for (const item of normalize(review?.fields, 'review-field')) {
    const group = /^(航段\d+|工作明细\d+|财务明细\d+)\s*·/.exec(item.label);
    const long = item.value.length > 180 || item.value.split("\n").length > 3;
    if (group) addGroup(group[1], item);
    else if (keys.has(item.label)) {
      overview.push(long ? {...item, sourceAnchor:undefined, fullAnchor:item.sourceAnchor, reading: undefined, value: item.value.slice(0, 160).replaceAll("\n", " ") + "…（全文见下方）"} : item);
      if (long) addGroup(item.label + " · 全文", item);
    } else addGroup(long ? item.label : "其他申请信息", item);
  }
  if (!overview.length && groups.has("其他申请信息")) overview.push(...groups.get("其他申请信息").splice(0, 6));
  for (const [index, group] of (Array.isArray(review?.groups) ? review.groups : []).entries()) {
    for (const item of normalize(group?.fields, `review-group-${index}-field`)) addGroup(String(group.title || "申请明细"), item);
  }
  return {overview, groups: [...groups].filter(([, fields]) => fields.length)};
}

const READING_TAGS = new Set(["p", "h2", "br", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "th", "td",
  "strong", "em", "blockquote", "pre", "code", "span"]);

// The server projects source HTML into a closed reading tree. Never parse HTML
// here or copy source attributes: even an invalid projection stays inert text.
export function appendReading(document, target, text, reading, onTable = () => {}, prioritizeFormattedBody = false) {
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
      onTable(scroll);
    } else parent.append(element);
  };
  // Only the body section opts in. Bare top-level text comes from the source
  // outside its explicit formatted blocks; it can include useful context as
  // well as page controls. Retain every character without classifying words.
  const plainNodes = reading.nodes.filter(item => typeof item === "string");
  const formattedNodes = reading.nodes.filter(item => typeof item !== "string");
  const split = prioritizeFormattedBody && plainNodes.some(item => item.trim()) &&
    formattedNodes.some(item => ["p", "h2", "ul", "ol", "table", "blockquote", "pre"].includes(item.tag));
  for (const item of split ? formattedNodes : reading.nodes) append(root, item);
  for (const notice of reading.notices) {
    const note = document.createElement("p"); note.className = "reading-notice";
    note.textContent = notice; root.append(note);
  }
  let originalText;
  if (split) {
    originalText = document.createElement("details"); originalText.className = "material-page-text";
    const label = document.createElement("summary"), source = document.createElement("pre");
    label.textContent = "其他原始页面文字（展开查看）";
    source.textContent = plainNodes.join(""); source.tabIndex = 0;
    originalText.append(label, source); root.append(originalText);
  }
  target.append(root);
  return originalText;
}

function appendReviewRows(document, target, fields, wrapped = true, register = () => {}, linkTo = null) {
  for (const item of fields) {
    const row = wrapped ? document.createElement("div") : target;
    if (wrapped) row.className = "review-row";
    if (item.sourceAnchor) register(item.sourceAnchor, row);
    const label = document.createElement("dt"), value = document.createElement("dd");
    label.textContent = String(item.label ?? ""); appendReading(document, value, item.value, item.reading);
    row.append(label, value);
    if (item.fullAnchor && linkTo) value.append(linkTo(item.fullAnchor, '定位完整原字段'));
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
  const originalFields = reviewSourceFields(review);
  const canonical = new Map();
  if (kind === 'weekly_report') {
    for (const field of originalFields.filter(item => item.sourceAnchor.startsWith('review-field-'))) {
      const source = originalFields.find(item => item.sourceAnchor.startsWith('review-section-') && item.label === field.label && item.value === field.value);
      if (source) canonical.set(field.sourceAnchor, source.sourceAnchor);
    }
  }
  const sourceNodes = new Map();
  const register = (id, node) => { node.id = id; if (!Number.isInteger(node.tabIndex) || node.tabIndex < 0) node.tabIndex = -1; sourceNodes.set(id, node); };
  const linkTo = (id, label) => {
    id = canonical.get(id) || id;
    const link = document.createElement('a'); link.href = `#${id}`; link.textContent = label;
    link.addEventListener('click', event => {
      const destination = sourceNodes.get(id) || document.getElementById?.(id);
      if (!destination) return;
      event.preventDefault();
      for (let parent = destination; parent; parent = parent.parentElement || parent.parentNode) {
        if (String(parent.tagName || parent.tag).toLowerCase() === 'details') parent.open = true;
      }
      destination.scrollIntoView?.({block:'start',behavior:'auto'}); destination.focus?.({preventScroll:true});
    });
    return link;
  };
  const focus = document.createElement('section'); focus.className = 'review-focus'; focus.setAttribute('aria-label','材料核对要点');
  for (const group of reviewFocus(review, kind)) {
    const box = document.createElement('div'), title = document.createElement('h3'); title.textContent = group.title; box.append(title);
    if (group.missing) {
      const missing = document.createElement('p'); missing.className = 'review-missing';
      missing.textContent = '原材料未提供独立字段，请查看下方原文或原图。'; box.append(missing);
    } else {
      for (const field of group.fields) {
        const item = document.createElement('p');
        item.textContent = kind === 'weekly_report' ? `${field.label} ` : `${field.label}：${field.value.length > 160 ? field.value.slice(0,160) + '…' : field.value} `;
        item.append(linkTo(field.sourceAnchor, '查看原字段')); box.append(item);
      }
    }
    focus.append(box);
  }
  const directory = document.createElement('nav'); directory.className = 'material-directory'; directory.setAttribute('aria-label','原始材料目录');
  const directoryTitle = document.createElement('h3'); directoryTitle.textContent = '原始材料目录'; directory.append(directoryTitle);
  const directoryItems = document.createElement('div'); directory.append(directoryItems);
  let directoryCount = 0;
  const addDirectory = (id, title) => { directoryItems.append(linkTo(id,title)); directoryCount++; };
  const duplicatedWeekly = item => canonical.has(item.sourceAnchor || item.fullAnchor);
  appendReviewRows(document, facts, overview.filter(item => !duplicatedWeekly(item)), true, register, linkTo);
  section.append(kicker, heading, effect, focus, directory, facts);
  for (const [name, fields] of groups) {
    const visibleFields = fields.filter(item => !duplicatedWeekly(item));
    if (!visibleFields.length) continue;
    const detail = document.createElement("details"); detail.className = "review-group";
    detail.dataset.group = name; detail.open = opened.get(name) || false;
    const summary = document.createElement("summary"); summary.textContent = `${name} · ${visibleFields.length} 项`;
    const rows = document.createElement("dl"); appendReviewRows(document, rows, visibleFields, true, register, linkTo);
    detail.append(summary, rows); section.append(detail);
  }
  let weeklyGrid = null;
  for (const [materialIndex, material] of (Array.isArray(review?.sections) ? review.sections : []).entries()) {
    if (!material || !["facts", "text", "table"].includes(material.kind)) continue;
    const detail = document.createElement("details"); detail.className = `review-group material-${material.kind}`;
    const sectionId = `review-section-${materialIndex}`; register(sectionId, detail);
    const materialCount = material.kind === 'table' ? ` · ${Array.isArray(material.rows) ? material.rows.length : 0} 行` : '';
    addDirectory(sectionId, String(material.title || '原始材料') + materialCount);
    detail.dataset.group = `material:${String(material.id || material.title || "")}`;
    const primary = ["current-week", "next-week", "handover-work", "handover-finance", "flight-legs", "reward-information", "reward-allocations", "settlement-information", "settlement-expenses", "settlement-payments"].includes(material.id) ||
      material.id === "body" && design?.family === "document" && (kind !== "weekly_report" || !weeklySections.length);
    detail.open = opened.has(detail.dataset.group) ? opened.get(detail.dataset.group) : primary;
    const summary = document.createElement("summary"); summary.textContent = String(material.title || "原始材料");
    detail.append(summary);
    if (material.kind === "facts") {
      const rows = document.createElement("dl");
      appendReviewRows(document, rows, (Array.isArray(material.fields) ? material.fields : []).map((field, index) => field && ({...field,sourceAnchor:`${sectionId}-field-${index}`})).filter(Boolean), true, register, linkTo); detail.append(rows);
    } else if (material.kind === "text") {
      if (!primary && String(material.text || "").length > 1800) {
        const excerpt = document.createElement("p"); excerpt.className = "material-excerpt";
        excerpt.textContent = String(material.text).slice(0, 240) + "…（展开下方查看完整材料）";
        section.append(excerpt);
      }
      const text = document.createElement(material.reading ? "div" : "pre"); text.className = "material-body"; text.tabIndex = 0;
      let tableIndex = 0;
      const originalText = appendReading(document, text, material.text, material.reading, table => {
        const id = `${sectionId}-table-${++tableIndex}`; register(id, table); addDirectory(id, `${material.title || '正文'} · 原表 ${tableIndex}`);
      }, material.id === "body");
      if (originalText) {
        originalText.dataset.group = `material-page-text:${materialIndex}`;
        originalText.open = opened.get(originalText.dataset.group) === true;
      }
      detail.append(text);
      if (String(material.text || "").length > 1800) {
        const expand = document.createElement("button"); expand.type = "button"; expand.className = "secondary material-reading-toggle";
        let expanded = false; expand.textContent = "展开长文连续阅读"; expand.setAttribute("aria-expanded", "false");
        expand.addEventListener("click", () => {
          expanded = !expanded; text.className = `material-body${expanded ? " expanded" : ""}`;
          expand.textContent = expanded ? "收起长文阅读区" : "展开长文连续阅读";
          expand.setAttribute("aria-expanded", String(expanded));
        });
        detail.append(expand);
      }
    } else {
      const columns = (Array.isArray(material.columns) ? material.columns : []).filter(column => typeof column?.key === "string");
      const table = document.createElement("table"), head = document.createElement("thead"), body = document.createElement("tbody");
      const header = document.createElement("tr");
      for (const column of columns) { const cell = document.createElement("th"); cell.scope = "col"; cell.textContent = String(column.label || column.key); header.append(cell); }
      head.append(header);
      const rowIndex = document.createElement('details'); rowIndex.className = 'material-row-index';
      rowIndex.dataset.group = `row-index:${materialIndex}`; rowIndex.open = opened.get(rowIndex.dataset.group) || false;
      const rowSummary = document.createElement('summary'), rowLinks = document.createElement('div');
      const sourceRows = Array.isArray(material.rows) ? material.rows : [];
      rowSummary.textContent = `定位原始明细行（${sourceRows.length} 行）`; rowIndex.append(rowSummary,rowLinks);
      for (const [rowNumber, row] of sourceRows.entries()) {
        const line = document.createElement("tr"); line.dataset.rowId = String(row?.id || "");
        const rowId = `${sectionId}-row-${rowNumber}`; register(rowId,line);
        if (rowNumber < 200) {
          const description = columns.slice(0,2).map(column => String(row?.cells?.[column.key] ?? '').slice(0,60)).join(' · ');
          rowLinks.append(linkTo(rowId, `第 ${rowNumber+1} 行${description ? '：'+description : ''}`));
        }
        for (const column of columns) {
          const cell = document.createElement("td");
          appendReading(document, cell, row?.cells?.[column.key], row?.readings?.[column.key]); line.append(cell);
        }
        body.append(line);
      }
      table.append(head, body); const scroll = document.createElement("div"); scroll.className = "material-table";
      scroll.tabIndex = 0; scroll.setAttribute("aria-label", String(material.title || "原始明细表"));
      scroll.append(table);
      if (sourceRows.length > 200) {
        const note = document.createElement('p'); note.textContent = '定位目录列出前 200 行；下方原表保留全部已读取行。'; rowIndex.append(note);
      }
      if (sourceRows.length) detail.append(rowIndex);
      detail.append(scroll);
    }
    if (kind === "weekly_report" && ["current-week", "next-week"].includes(material.id)) {
      if (!weeklyGrid) { weeklyGrid = document.createElement("div"); weeklyGrid.className = "material-primary-grid"; section.append(weeklyGrid); }
      weeklyGrid.append(detail);
    } else section.append(detail);
  }
  const previewIds = new Set();
  for (const preview of Array.isArray(review?.materialPreviews) ? review.materialPreviews.slice(0,128) : []) {
    if (!preview || !/^[a-f0-9]{64}$/.test(preview.id) || previewIds.has(preview.id) ||
        !['image','pdf','archive','file'].includes(preview.kind) || !['available','unavailable'].includes(preview.state)) continue;
    previewIds.add(preview.id);
    addDirectory(`material-preview-${preview.id}`, `${String(preview.name || '未命名材料').slice(0,500)} · ${preview.state === 'available' ? '按需预览' : '暂不支持预览'}`);
  }
  if (!directoryCount) directory.hidden = true;
  if (!focus.childElementCount) focus.hidden = true;
  if (Array.isArray(review?.attachments) && review.attachments.length) {
    const attachments = document.createElement("section"); attachments.className = "material-attachments";
    const title = document.createElement("h3"); title.textContent = "附件名称";
    const list = document.createElement("ul");
    for (const attachment of review.attachments) { const item = document.createElement("li"); item.textContent = String(attachment?.name || "未命名附件"); list.append(item); }
    const note = document.createElement("p"); note.textContent = "此处列出源页面中的附件名称；需要阅读内容时，请打开下方支持的材料预览。未打开或不支持预览的附件内容尚未读取。";
    attachments.append(title, list, note); section.append(attachments);
  }
  if (review?.completeness || review?.truncated === true) {
    const note = document.createElement("p"); note.className = "material-completeness";
    const complete = review?.completeness?.status === "complete" && review?.truncated !== true;
    note.textContent = complete ? "已读取当前受控页面的材料。附件正文请按需打开材料预览；未预览的附件和源系统未展开的历史不在此读取范围内。" :
      "当前材料尚未完整核对，请查看读取状态并补全材料后继续。";
    if (!complete) note.setAttribute("role", "status");
    for (const reason of Array.isArray(review?.completeness?.reasons) ? review.completeness.reasons : []) {
      const detail = document.createElement("span"); detail.textContent = ` ${String(reason)}`; note.append(detail);
    }
    section.append(note);
  }
  target.replaceChildren(section); target.dataset.signature = signature;
}

/** Display-only differences in source material; never changes the reviewed contract. */
export function reviewDifferences(previous, next) {
  const source = review => {
    const entries = [];
    const occurrences = new Map();
    const identity = (scope, label) => {
      const key = JSON.stringify([scope, label]), ordinal = occurrences.get(key) || 0;
      occurrences.set(key, ordinal + 1);
      return JSON.stringify([scope, label, ordinal]);
    };
    for (const field of review?.fields || []) {
      if (["处理意见", "本次操作", "回复说明", "参会意向"].includes(field.label)) continue;
      entries.push([identity("field", String(field.label)), String(field.label), String(field.value ?? "")]);
    }
    for (const group of review?.groups || []) {
      const groupKey = identity("group", String(group.title || "申请明细"));
      for (const field of group.fields || []) {
        entries.push([identity(groupKey, String(field.label)), `${group.title || "申请明细"} · ${field.label}`, String(field.value ?? "")]);
      }
    }
    for (const section of review?.sections || []) {
      const text = section.kind === "text" ? String(section.text || "") : section.kind === "facts" ?
        (section.fields || []).map(field => `${field.label}：${field.value ?? ""}`).join("\n") :
        (section.rows || []).map(row => (section.columns || []).map(column => `${column.label}：${row.cells?.[column.key] ?? ""}`).join("；")).join("\n");
      entries.push([identity("section", String(section.id || section.title || "原始材料")), String(section.title || "原始材料"), text]);
    }
    entries.push(["attachments", "附件清单", (review?.attachments || []).map(item => item.name || "未命名附件").join("\n")]);
    return new Map(entries.map(([key, label, value]) => [key, {label, value}]));
  };
  const before = source(previous), after = source(next), changes = [];
  for (const key of new Set([...before.keys(), ...after.keys()])) {
    if (before.get(key)?.value === after.get(key)?.value) continue;
    const shorten = value => value === undefined ? "（无）" : value.length > 1200 ? value.slice(0, 1200) + "…（节选，请核对下方完整材料）" : value || "（空）";
    changes.push({label: after.get(key)?.label || before.get(key).label,
      before: shorten(before.get(key)?.value), after: shorten(after.get(key)?.value)});
  }
  return {items: changes.slice(0, 20), total: changes.length};
}

export function mountBusinessDraft(config, document, window) {
  const form = document.getElementById("draft-form");
  const fieldNames = config.fieldNames || DRAFT_FIELDS;
  const viewKind = config.viewKind || config.state.ui?.viewKind || "work_log";
  const definitions = config.fields || config.state.ui?.fields || [];
  const fields = Object.fromEntries(fieldNames.map(name => [name,
    bindDraftField(document.getElementById(name), definitions.find(field => field.name === name))]));
  const node = id => document.getElementById(id);
  // Only the initial server-validated source may own this navigation. Polls
  // cannot replace it with a different source or a client-supplied return URL.
  const returnTo = safeResultReturn(config.state.navigation, window.location.origin);
  const workspaceReturn = config.state.navigation?.back?.kind === 'workspace';
  const materials = node("draft-material-previews") && typeof createMaterialPreviews === "function" ?
    createMaterialPreviews({document, window, target:node("draft-material-previews"), apiUrl:config.apiUrl, csrfToken:config.csrfToken}) : null;
  const readValues = () => Object.fromEntries(fieldNames.map(name => [name, fields[name].value]));
  const setText = (id, value) => { node(id).textContent = String(value ?? ""); };
  const touched = new Set();
  const roomNodes = new Map();
  const roomLabels = new Map();
  let lastOptions = config.state.options;
  let saveTimer, pollTimer;
  let stopped = false, pageEpoch = 0, navigationApproved = false;
  const draftNavigation = document.querySelector?.('.draft-nav');
  let navigationObserver = null, navigationClearance = null;
  function measureNavigation() {
    if (stopped || !draftNavigation) return;
    const height = draftNavigation.getBoundingClientRect?.().height ?? draftNavigation.offsetHeight;
    if (!Number.isFinite(height) || height <= 0) return;
    const clearance = `${Math.ceil(height) + 16}px`;
    if (clearance !== navigationClearance) {
      document.documentElement?.style?.setProperty('--draft-nav-clearance', clearance);
      navigationClearance = clearance;
    }
  }
  function observeNavigation() {
    if (!draftNavigation) return;
    measureNavigation();
    if (typeof window.ResizeObserver === 'function') {
      try { navigationObserver = new window.ResizeObserver(measureNavigation); navigationObserver.observe(draftNavigation); }
      catch { navigationObserver?.disconnect(); navigationObserver = null; }
    }
    // Older WebViews still update on viewport changes, font readiness and render.
    window.addEventListener('resize', measureNavigation);
    const epoch = pageEpoch;
    document.fonts?.ready?.then(() => { if (epoch === pageEpoch) measureNavigation(); }, () => {});
  }
  function suspendNavigation() {
    navigationObserver?.disconnect(); navigationObserver = null;
    window.removeEventListener?.('resize', measureNavigation);
  }
  let contentIntent = null;
  let statusCopy = '';
  let displayedReview = config.state.preview?.summary || config.state.ui?.review || {};
  let materialDiff = null;
  const waiting = new Map();
  function paintStatus() {
    const slow = [...waiting.values()].filter(wait => wait.elapsedMs >= 5000)
      .sort((a, b) => b.elapsedMs - a.elapsedMs)[0];
    setText('draft-status', statusCopy + (slow ? ` · 已等待 ${Math.floor(slow.elapsedMs / 1000)} 秒，请保留本页，无需重复提交。` : ''));
  }
  const transport = createDraftTransport({window, apiUrl: config.apiUrl, csrfToken: config.csrfToken,
    onProgress(wait) {
      if (wait.active) waiting.set(wait.entry, wait); else waiting.delete(wait.entry);
      if (!stopped) paintStatus();
    }});
  function edited(name) {
    touched.add(name); controller.edit(name);
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
      entry.input.checked = !view.roomChoiceRequired && fields.room.value === item.value;
      entry.input.disabled = !view.canEdit || !view.optionsFresh || !item.available || view.optionsLoading;
    }
    const selected = options?.selected?.value === fields.room.value ? options.selected : items.find(item => item.value === fields.room.value);
    const unavailable = Boolean(fields.room.value) && (!view.optionsFresh || !items.some(item => item.value === fields.room.value && item.available));
    node("room-selected").hidden = !unavailable;
    setText("room-selected", `原选择：${selected?.label || roomLabels.get(fields.room.value) || "先前选择的会议室"}。${view.optionsFresh ? selected?.reason || "该选择已不可用，请重新选择会议室。" : "新时段尚未确认，旧选择不会自动替换。"}`);
    setText("room-status", view.optionsLoading ? "正在查询此时段的空闲会议室…" : view.roomChoiceRequired ? '已恢复填写内容；会议室需在当前可用房源中重新选择。' : !view.optionsFresh ?
      "请填写有效起止时间；保存后重新查询候选会议室。" : items.some(item => item.available) ?
      "请选择一个可用会议室。时间变化后会重新检查。" : "此时段没有可用会议室，请调整时间。");
  }
  function contentConfirmationAvailable(view) {
    const candidate = contentIntent?.action === 'restore' ? view.current.contentDraft?.restore :
      view.current.contentDraft?.restore || view.current.contentDraft?.saved;
    return Boolean(contentIntent && candidate && candidate.contentId === contentIntent.contentId &&
      candidate.version === contentIntent.version && (contentIntent.action === 'restore' ? view.canRestoreContent : view.canDiscardContent));
  }
  function paintContentConfirmation(view) {
    const panel = node('draft-content-confirmation');
    if (!panel) return;
    panel.hidden = !contentIntent;
    node('draft-content-restore').disabled = !view.canRestoreContent || Boolean(contentIntent);
    node('draft-content-discard').disabled = !view.canDiscardContent || Boolean(contentIntent);
    node('draft-submit').disabled = !view.canApprove || Boolean(contentIntent);
    form.dataset.agentbridgeBusy = String(Boolean(contentIntent) || view.saving || view.previewing || view.optionsLoading || view.uncertain || view.contentBusy || Boolean(view.contentPending));
    if (contentIntent) {
      const restore = contentIntent.action === 'restore', allowed = contentConfirmationAvailable(view);
      setText('draft-content-confirm-title',restore ? '确认恢复填写内容' : '确认删除保留内容');
      setText('draft-content-confirm-message',!allowed ? '草稿或保留内容状态已变化，请取消并重新检查后再选择。' : restore
        ? '当前修改会先保存，再用这份保留内容替换当前输入，并重新读取和核对材料。这一步不会授权或提交业务。'
        : '将删除此事项的全部保留内容。本页已填写的内容不会清空，之后主动编辑会再次保存。这一步不会授权或提交业务。');
      setText('draft-content-confirm',restore ? '确认恢复内容' : '确认删除保留内容');
      node('draft-content-confirm').disabled = !allowed;
    }
    if (window.CustomEvent) document.dispatchEvent(new window.CustomEvent('agentbridge:draft-state'));
  }
  function cancelContentConfirmation() {
    const action = contentIntent?.action; contentIntent = null;
    paintContentConfirmation(controller.state());
    node(action === 'restore' ? 'draft-content-restore' : 'draft-content-discard')?.focus();
  }
  function showContentConfirmation(action) {
    const view = controller.state();
    if (stopped || contentIntent || !(action === 'restore' ? view.canRestoreContent : view.canDiscardContent)) return;
    const candidate = action === 'restore' ? view.current.contentDraft.restore : view.current.contentDraft.restore || view.current.contentDraft.saved;
    contentIntent = {action,contentId:candidate.contentId,version:candidate.version};
    paintContentConfirmation(view); node('draft-content-cancel')?.focus();
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
    request: transport.request,
    onReceipt(receipt) {
      if (stopped) return false;
      const next = safeFlowUrl(receipt.flowUrl, window.location.origin);
      if (!next) return false;
      if (returnTo && !workspaceReturn) next.searchParams.set('result', returnTo.pathname.split('/').at(-1));
      if (!workspaceReturn) next.hash = window.location.hash;
      window.location.replace(next.href);
    },
    render(view) {
      renderBatchChecklist(document,node('draft-batch-list'),view.current.batch);
      const problems = [];
      for (const name of fieldNames) {
        fields[name].disabled = !view.canEdit;
        const error = view.fieldErrors[name] || (touched.has(name) && !fields[name].validity.valid ? fields[name].validationMessage : "");
        fields[name].setAttribute("aria-invalid", error ? "true" : "false");
        setText(`error-${name}`, error);
        const definition = definitions.find(field => field.name === name) || {};
        if (error || definition.required && !fields[name].value.trim() || !fields[name].validity.valid) {
          problems.push({name, label: definition.label || name, error});
        }
        if (node(`count-${name}`)) setText(`count-${name}`, `${fields[name].value.length} / ${definition.max_length} 字`);
      }
      const errors = node("draft-field-summary");
      if (errors) {
        const visible = problems.filter(problem => problem.error), signature = JSON.stringify(visible);
        errors.hidden = !visible.length;
        if (errors.dataset.signature !== signature) {
          errors.replaceChildren();
          const title = document.createElement("strong"); title.textContent = `请检查 ${visible.length} 个填写项`; errors.append(title);
          const list = document.createElement("ul");
          for (const problem of visible) {
            const item = document.createElement("li"), link = document.createElement("a"); link.href = `#${problem.name}`;
            link.textContent = `${problem.label}：${problem.error}`;
            link.addEventListener("click", () => { const field = node(problem.name); (field?.querySelector?.('input') || field)?.focus(); });
            item.append(link); list.append(item);
          }
          errors.append(list); errors.dataset.signature = signature;
        }
      }
      const expiry = node("draft-expiry"), expires = Date.parse(view.current.expiresAt), left = expires - Date.now();
      if (expiry) {
        expiry.hidden = !Number.isFinite(expires) || Boolean(view.current.receipt);
        expiry.className = `draft-expiry${left < 120000 ? " urgent" : ""}`;
        expiry.textContent = left <= 0 ? "本页已到期。输入仍保留在本页，请返回原任务重新打开；系统不会自动提交。" :
          left < 120000 ? `本页约 ${Math.ceil(left / 1000)} 秒后到期，请及时核对；到期后从原任务重新打开。` :
          `本页可办理至 ${new Date(expires).toLocaleTimeString("zh-CN", {hour:"2-digit",minute:"2-digit"})}；填写自动保存，确认后才执行。`;
      }
      renderRooms(view);
      const retained = view.current.contentDraft, contentPanel = node('draft-content');
      if (contentPanel) {
        contentPanel.hidden = !retained || Boolean(view.current.receipt);
        const date = value => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN') : '时间未知';
        setText('draft-content-saved',retained?.saved ? `填写内容已保留：${date(retained.saved.savedAt)}；保留至 ${date(retained.saved.expiresAt)}。` : '当前没有已保留的内容副本。未保存的输入仅留在本页。');
        const candidate = retained?.restore;
        setText('draft-content-candidate',candidate ? `找到${candidate.scope === 'same_item' ? '同一事项' : '本任务'}之前填写的内容（${date(candidate.savedAt)}），保留至 ${date(candidate.expiresAt)}。恢复会替换当前输入，并重新检查当前材料；不会沿用旧授权。` : '内容保留期独立于本页办理有效期。页面或授权到期后，需从原任务重新打开；保留内容不会延长办理权限。');
        node('draft-content-restore').hidden = !candidate;
        node('draft-content-restore').disabled = !view.canRestoreContent;
        node('draft-content-discard').hidden = !candidate && !retained?.saved;
        node('draft-content-discard').disabled = !view.canDiscardContent;
        setText('draft-content-discard','删除此事项的保留内容');
      }
      node("draft-submit").disabled = !view.canApprove;
      node("draft-retry").hidden = !view.canRetry;
      setText("draft-retry", view.contentPending ? '恢复本次草稿操作' : view.uncertain ? "恢复本次确认" : "重新检查");
      setText("draft-error", view.message);
      node("draft-error").hidden = !view.message;
      node("draft-material-change").hidden = !view.reviewChanged;
      statusCopy = view.locked ? (view.current.receipt ? "已确认，正在接续办理" : view.uncertain ? "正在确认提交状态" : "此草稿已停止编辑") :
        view.contentBusy ? (view.contentPending === 'content-restore' ? '正在读取当前材料并恢复填写内容' : '正在删除此事项的保留内容') : view.contentPending ? '草稿操作回执待核对' :
        view.conflict ? "需要核对另一页面的修改" : view.saving ? "正在保存草稿" : view.dirty ? "有修改，正在准备检查" :
        view.optionsLoading ? "正在查询空闲会议室" : view.previewing ? (viewKind === "work_log" ? "正在解析项目并检查重复日志" : "正在核对申请信息") :
        !view.materialReady ? "原始材料尚未完整核对，暂不能确认提交" :
        view.canApprove ? (view.reviewChanged ? "请重新核对更新后的材料，再确认提交" : "已检查，可以确认提交") : "填写完整后自动检查";
      paintStatus();
      if (node("draft-submit-hint")) setText("draft-submit-hint", problems.length ?
        `还需检查 ${problems.length} 项：${problems.map(item => item.label).join("、")}` : statusCopy);
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
      const currentReview = preview?.summary || view.current.ui?.review || {};
      materials?.render(currentReview);
      if (view.reviewChanged) {
        const diff = reviewDifferences(displayedReview, currentReview);
        if (diff.total) materialDiff = diff;
      }
      displayedReview = currentReview;
      const diffTarget = node("draft-material-diff");
      if (diffTarget && view.reviewChanged && diffTarget.dataset.signature !== JSON.stringify(materialDiff)) {
        diffTarget.replaceChildren(); diffTarget.dataset.signature = JSON.stringify(materialDiff);
        if (!materialDiff?.total) { const note = document.createElement("p"); note.textContent = "来源材料绑定已变化，请重新核对下方完整材料。"; diffTarget.append(note); }
        for (const item of materialDiff?.items || []) {
          const detail = document.createElement("details"), label = document.createElement("summary"), before = document.createElement("pre"), after = document.createElement("pre");
          label.textContent = item.label; before.textContent = `之前：${item.before}`; after.textContent = `现在：${item.after}`;
          detail.append(label, before, after); diffTarget.append(detail);
        }
        if (materialDiff?.total > 20) { const note = document.createElement("p"); note.textContent = `共 ${materialDiff.total} 处变化，此处显示前 20 处，请核对下方完整材料。`; diffTarget.append(note); }
      }
      const hasSourceReview = Boolean(currentReview && (currentReview.title || currentReview.fields?.length || currentReview.groups?.length || currentReview.sections?.length));
      if (node('draft-evidence')) node('draft-evidence').hidden = !hasSourceReview;
      if (node('draft-evidence-link')) node('draft-evidence-link').hidden = !hasSourceReview;
      if (hasSourceReview) renderEvidence(document, node("draft-evidence"), currentReview, view.current.ui?.reviewKind || "");
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
      form.dataset.agentbridgeDirty = String(view.dirty && !view.locked);
      form.dataset.agentbridgeBusy = String(view.saving || view.previewing || view.optionsLoading || view.uncertain || view.contentBusy || Boolean(view.contentPending));
      form.dataset.agentbridgeLocked = String(view.locked);
      paintContentConfirmation(view);
      measureNavigation();
      if (window.CustomEvent) document.dispatchEvent(new window.CustomEvent('agentbridge:draft-state'));
    },
  });
  if (returnTo) {
    const back = document.createElement('button'); back.id = 'agentbridge-back'; back.type = 'button';
    back.className = 'secondary draft-return'; back.textContent = workspaceReturn ? '返回 Workspace' : '返回待办列表';
    back.addEventListener('click', () => {
      if (stopped) return;
      const view = controller.state();
      if (view.dirty && !view.locked && (typeof window.confirm !== 'function' ||
          !window.confirm('本页还有未保存的输入。返回可能丢失这些输入，已开始的任务会继续。确认返回？'))) return;
      navigationApproved = true;
      const target = new URL(returnTo.href); if (!workspaceReturn) target.hash = window.location.hash;
      window.location.replace(target.href);
    });
    form.append(back);
  }
  form.addEventListener("input", event => { if (fieldNames.includes(event.target.name)) edited(event.target.name); });
  form.addEventListener("submit", event => { event.preventDefault(); if (!contentIntent) void controller.approve(); });
  node("draft-retry").addEventListener("click", () => void controller.retry());
  node("draft-use-server").addEventListener("click", () => void controller.adoptServer());
  node("draft-keep-local").addEventListener("click", () => void controller.keepLocal());
  node('draft-content-restore')?.addEventListener('click',() => showContentConfirmation('restore'));
  node('draft-content-discard')?.addEventListener('click',() => showContentConfirmation('discard'));
  node('draft-content-cancel')?.addEventListener('click',cancelContentConfirmation);
  node('draft-content-confirmation')?.addEventListener('keydown',event => {
    if (event.key === 'Escape' && contentIntent) { event.preventDefault(); cancelContentConfirmation(); }
  });
  node('draft-content-confirm')?.addEventListener('click',() => {
    if (!contentConfirmationAvailable(controller.state())) return;
    const action = contentIntent.action; contentIntent = null;
    window.clearTimeout(saveTimer); paintContentConfirmation(controller.state());
    void (action === 'restore' ? controller.restoreContent() : controller.discardContent());
  });
  window.addEventListener("beforeunload", event => {
    if (!navigationApproved && controller.state().dirty && !controller.state().locked) { event.preventDefault(); event.returnValue = ""; }
  });
  window.addEventListener("pagehide", () => {
    contentIntent = null;
    stopped = true; pageEpoch++;
    suspendNavigation();
    window.clearTimeout(saveTimer); window.clearTimeout(pollTimer);
    controller.suspend();
    materials?.suspend();
    transport.suspend(); waiting.clear();
  });
  window.addEventListener("pageshow", event => {
    if (!event.persisted || !stopped) return;
    stopped = false;
    const epoch = ++pageEpoch;
    observeNavigation();
    void controller.restore().finally(() => {
      if (!stopped && pageEpoch === epoch && !controller.state().locked) pollTimer = window.setTimeout(poll, 2500);
    });
  });
  document.addEventListener?.('agentbridge:native-resume', () => {
    if (stopped) return;
    window.clearTimeout(pollTimer);
    // Re-read the same draft and receipt; restoration never invokes approve.
    const epoch = ++pageEpoch;
    void controller.restore().finally(() => {
      if (!stopped && pageEpoch === epoch && !controller.state().locked) pollTimer = window.setTimeout(poll, 2500);
    });
  });
  async function poll() {
    if (stopped) return;
    const epoch = pageEpoch;
    const refreshed = await controller.poll();
    if (!stopped && pageEpoch === epoch && refreshed !== false && (!controller.state().locked || controller.state().uncertain)) {
      pollTimer = window.setTimeout(poll, 2500);
    }
  }
  controller.start();
  observeNavigation();
  pollTimer = window.setTimeout(poll, 2500);
  return controller;
}
