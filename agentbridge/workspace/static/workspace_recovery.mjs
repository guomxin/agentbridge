import { isCancellation } from "./workspace_lifecycle.mjs";

const ACK_VERSION = "oa_unknown_new_attempt_v1";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PARTIAL_DONE_NOTICE = "已办记录仅部分覆盖，未找到记录不能证明上次未提交。";
const RISK_ACK = "我已核对原事项，理解原提交结果仍未知、可能仍在处理，重新办理存在重复处理风险。我仅要求准备新表单，并会重新核对材料、填写意见后再明确确认。";
const reasons = {
  still_pending: "原事项仍在 OA 待办中，但这不能排除原提交仍在处理。",
  pending_partial: "待办列表不完整，暂不能安全准备新的办理。",
  done_partial: "已办核验范围不完整，暂不能确认原事项结果。",
  identity_changed: "当前 OA 身份与原办理不一致，请先核对登录身份。",
  material_changed: "原事项材料已变化，请先到 OA 核对。",
  target_changed: "原事项或当前审批节点已变化，请先到 OA 核对。",
  pending_coverage_incomplete: "待办列表不完整，暂不能安全准备新的办理。",
  target_identity_changed: "原事项或当前审批节点已变化，请先到 OA 核对。",
  conflicting_or_completed_evidence: "当前记录存在冲突或已有完成证据，请到 OA 核对原事项。",
};

// This panel can only request a read-only assessment and a fresh draft. The
// reviewed business form remains the sole place for a new execution decision.
export function createOaPendingRecovery({ document, state, probeRecovery, prepareRecovery, openTask,
  now = () => Date.now(), makeRequestKey = () => crypto.randomUUID() }) {
  state.oaPendingRecovery ||= new Map();
  const roots = new WeakMap();
  const node = (tag, className = "", text = "") => {
    const value = document.createElement(tag); value.className = className; value.textContent = text; return value;
  };
  function modelFor(taskId) {
    let model = state.oaPendingRecovery.get(taskId);
    if (!model) {
      model = {taskId, views:new Set(), assessment:null, acknowledgement:false, pending:false,
        probeRequest:null, prepareRequest:null, prepareRetryable:false, recoveryTaskId:null, notice:"", eligible:true};
      state.oaPendingRecovery.set(taskId, model);
    }
    return model;
  }
  const validUntil = assessment => Number.isFinite(Date.parse(assessment?.expiresAt)) && Date.parse(assessment.expiresAt) > now();
  const validAssessment = value => value && ["verified_complete", "unresolved_still_pending", "blocked"].includes(value.status)
    && value.canExecute === false && typeof value.canPrepare === "boolean"
    && (!value.recoveryTaskId || !value.canPrepare || value.childPreparation === "retryable")
    && (value.status !== "unresolved_still_pending" || value.canPrepare !== true ||
      UUID.test(value.assessmentId) && /^sha256:[0-9a-f]{64}$/.test(value.assessmentHash)
      && Number.isSafeInteger(value.revision) && value.revision > 0 && Number.isFinite(Date.parse(value.expiresAt)));

  function update(model) {
    const assessment = model.assessment;
    const expired = Boolean(assessment && assessment.canPrepare && !validUntil(assessment));
    const ready = model.eligible && assessment?.status === "unresolved_still_pending" && assessment.canPrepare === true && !expired;
    const retryableChild = Boolean(model.recoveryTaskId) && assessment?.childPreparation === "retryable";
    for (const view of model.views) {
      view.root.hidden = !model.eligible;
      view.probe.disabled = model.pending || Boolean(model.prepareRequest && !model.prepareRetryable) || !model.eligible;
      view.probe.textContent = model.pending && !model.prepareRequest ? "正在核对原事项…" : model.probeRequest ? "恢复本次核对" : "只读核对原事项";
      view.notice.textContent = model.notice || "先核对原事项的当前状态；本步骤不会重新提交。";
      if (expired && !model.prepareRequest) view.notice.textContent = "本次核对已过期，请重新核对原事项。";
      view.risk.hidden = !(ready || model.prepareRequest) || Boolean(model.recoveryTaskId && !retryableChild);
      view.riskCopy.textContent = `${assessment?.status === "unresolved_still_pending" && assessment.doneCoverage !== "complete" ? PARTIAL_DONE_NOTICE : ""}${RISK_ACK}`;
      view.ack.checked = model.acknowledgement;
      view.ack.disabled = model.pending || Boolean(model.prepareRequest) || !ready;
      view.prepare.hidden = !(ready || model.prepareRequest) || Boolean(model.recoveryTaskId && !retryableChild);
      view.prepare.disabled = model.pending || !model.eligible || (!model.prepareRequest && (!ready || !model.acknowledgement));
      view.prepare.textContent = model.pending && model.prepareRequest ? "正在准备新表单…" : model.prepareRequest ? "恢复本次新表单准备" : retryableChild ? "恢复同一任务材料准备" : "准备全新办理表单";
      view.open.hidden = !model.recoveryTaskId;
      view.open.textContent = retryableChild ? "查看恢复任务进度" : "查看新办理表单";
      view.open.disabled = typeof openTask !== "function";
    }
  }

  async function probe(model) {
    if (model.pending || model.prepareRequest && !model.prepareRetryable || !model.eligible) return;
    if (model.prepareRetryable) { model.prepareRequest = null; model.prepareRetryable = false; }
    model.probeRequest ||= {requestKey:makeRequestKey()};
    model.pending = true; model.assessment = null; model.acknowledgement = false;
    model.notice = "正在只读核对原事项；不会重新提交，请稍候。"; update(model);
    try {
      const result = await probeRecovery(model.taskId, {...model.probeRequest});
      if (!validAssessment(result)) throw Error("核对回执尚未确认，请恢复本次核对。");
      model.assessment = result; model.probeRequest = null;
      model.recoveryTaskId = UUID.test(result.recoveryTaskId) && result.recoveryTaskId !== model.taskId ? result.recoveryTaskId : null;
      if (result.status === "verified_complete") {
        model.notice = "已根据 OA 当前记录核验原事项办理完成，无需重新准备。原任务的历史结果记录会保留。";
      } else if (model.recoveryTaskId && result.childPreparation === "retryable" && result.canPrepare) {
        model.notice = `同一恢复任务的材料尚未准备完成。${result.doneCoverage !== "complete" ? PARTIAL_DONE_NOTICE : ""}原提交结果仍未知；确认风险后仅恢复该任务的材料准备，不会创建第二次恢复任务或提交。`;
      } else if (model.recoveryTaskId) {
        model.notice = "此原事项已有新的办理任务，请查看原有新表单，避免重复准备。原办理结果仍保留为待核对。";
      } else if (result.status === "unresolved_still_pending" && result.canPrepare) {
        model.notice = `原事项仍在 OA 待办中。${result.doneCoverage !== "complete" ? PARTIAL_DONE_NOTICE : ""}原提交结果仍未知；如继续，需要重新核对全部材料，并作出新的最终确认。`;
      } else {
        model.notice = reasons[result.reason] || "当前证据不足，不能安全准备新的办理。请到 OA 核对原事项。";
      }
    } catch (error) {
      if (isCancellation(error)) return;
      model.notice = error.message || "原事项核对尚未确认，请恢复本次核对。";
      if ([400, 403, 404, 409, 422].includes(error.status)) model.probeRequest = null;
    } finally { model.pending = false; update(model); }
  }

  async function prepare(model) {
    if (model.pending || !model.eligible || model.recoveryTaskId && model.assessment?.childPreparation !== "retryable") return;
    if (!model.prepareRequest) {
      if (!model.acknowledgement || !validUntil(model.assessment)
          || model.assessment?.status !== "unresolved_still_pending" || model.assessment.canPrepare !== true) {
        update(model); return;
      }
      model.prepareRequest = {assessmentId:model.assessment.assessmentId, assessmentHash:model.assessment.assessmentHash,
        expectedRevision:model.assessment.revision, requestKey:makeRequestKey(), acknowledgement:ACK_VERSION};
    }
    model.pending = true; model.prepareRetryable = false; model.notice = "正在重新读取材料并准备全新表单；不会提交原事项。"; update(model);
    try {
      const result = await prepareRecovery(model.taskId, {...model.prepareRequest});
      if (result?.canExecute !== false || !["prepared", "existing", "verified_complete"].includes(result.status)
          || result.originalTaskId !== model.taskId || result.assessmentId !== model.prepareRequest.assessmentId
          || result.status !== "verified_complete" && (!UUID.test(result.taskId) || result.taskId === model.taskId)) {
        throw Error("新表单准备回执尚未确认，请恢复本次准备。");
      }
      model.prepareRequest = null; model.prepareRetryable = false; model.acknowledgement = false;
      if (result.status === "verified_complete") {
        model.assessment = {...model.assessment, status:"verified_complete", canPrepare:false};
        model.notice = "核对发现原事项已办理完成，无需新的办理。原任务的历史结果记录会保留。";
      } else {
        model.recoveryTaskId = result.taskId;
        model.assessment = {...model.assessment, childPreparation:"ready", canPrepare:false};
        model.notice = "新的办理任务已准备。请重新核对材料、填写意见，并在新表单作出最终确认；原办理结果仍待核对。";
      }
    } catch (error) {
      if (isCancellation(error)) return;
      model.notice = error.message || "新表单准备尚未确认，请恢复本次准备。";
      if (error.status === 409 && error.code === "OA_RECOVERY_PREPARE_FAILED") {
        model.prepareRetryable = true;
      } else if ([400, 403, 404, 409, 422].includes(error.status)) {
        model.prepareRequest = null; model.assessment = null; model.acknowledgement = false;
      }
    } finally { model.pending = false; update(model); }
  }

  function create(root, model) {
    const heading = node("strong", "", "核对原事项与后续办理");
    const notice = node("p", "oa-recovery-status"); notice.setAttribute("role", "status"); notice.setAttribute("aria-live", "polite");
    const probeButton = node("button", "secondary", "只读核对原事项"); probeButton.type = "button";
    const risk = node("label", "oa-recovery-risk"), ack = node("input"), riskCopy = node("span"); ack.type = "checkbox";
    riskCopy.textContent = RISK_ACK;
    risk.append(ack, riskCopy);
    const prepareButton = node("button", "secondary", "准备全新办理表单"); prepareButton.type = "button";
    const open = node("button", "primary", "查看新办理表单"); open.type = "button";
    root.className = "oa-pending-recovery";
    root.append(heading, notice, probeButton, risk, prepareButton, open);
    probeButton.addEventListener("click", () => { if (!probeButton.disabled) void probe(model); });
    ack.addEventListener("change", () => { if (!ack.disabled) { model.acknowledgement = ack.checked; update(model); } });
    prepareButton.addEventListener("click", () => { if (!prepareButton.disabled) void prepare(model); });
    open.addEventListener("click", () => { if (!open.disabled && model.recoveryTaskId) void openTask(model.recoveryTaskId); });
    const view = {root, notice, probe:probeButton, risk, riskCopy, ack, prepare:prepareButton, open};
    model.views.add(view); roots.set(root, model); return view;
  }

  return {detach(root) {
    const model = roots.get(root);
    if (model) for (const view of model.views) if (view.root === root) model.views.delete(view);
    roots.delete(root);
  }, render(root, result) {
    const task = result?.task;
    const eligible = task?.status === "outcome_unknown" && result?.businessExecution?.readOnlyQuery === "oa_pending"
      && result.oaPendingRecoveryAvailable !== false && typeof probeRecovery === "function" && typeof prepareRecovery === "function";
    if (!eligible && !roots.has(root)) { root.hidden = true; return; }
    const model = modelFor(task.task_id); model.eligible = eligible;
    if (!roots.has(root)) create(root, model);
    update(model);
  }};
}
