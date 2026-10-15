type JsonRecord = Record<string, unknown>;
export type Tone = "neutral" | "working" | "success" | "error";

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord : null;
}

export function interactionActionLabel(type: string, display: unknown): string {
  const view = record(record(display)?.businessView);
  const labels: Record<string, [string, string]> = {
    "taihua.work_log.create": ["work_log", "填写工作日志"],
    "oa.meeting_room.application.create": ["meeting_room", "选择会议室"],
    "oa.meeting.create": ["meeting_room", "安排会议"],
    "oa.meeting_room.application.cancel": ["form", "核对撤销申请"],
    "oa.workflow.revoke": ["form", "核对流程撤销"],
    "oa.business_trip.save_draft": ["form", "填写出差草稿"],
    "oa.business_trip.submit": ["form", "填写出差申请"],
    "oa.leave.save_draft": ["form", "填写请假草稿"],
    "oa.leave.submit": ["form", "填写请假申请"],
    "oa.missed_punch.save_draft": ["form", "填写补签草稿"],
    "smartlight.alarm.remark.update": ["form", "编辑告警备注"],
    "oa.missed_punch.approval": ["approval", "核对补签材料"],
    "oa.efficiency_data.approval": ["approval", "核对效能数据"],
    "oa.travel_expense.approval": ["approval", "核对差旅报销"],
    "oa.business_trip.approval": ["approval", "核对出差材料"],
    "oa.labor_contract_renewal.approval": ["approval", "核对合同续签"],
    "oa.intellectual_property_declaration.approval": ["approval", "核对知识产权材料"],
    "oa.overtime.approval": ["approval", "核对加班材料"],
    "oa.leave.approval": ["approval", "核对请假材料"],
    "oa.resignation.approval": ["approval", "核对离职材料"],
    "oa.work_handover.approval": ["approval", "核对交接材料"],
    "oa.flight_application.approval": ["approval", "核对乘机申请"],
    "oa.standard_collaboration.approval": ["approval", "核对协同事项"],
    "oa.attendance_confirmation": ["approval", "核对月度考勤"],
    "oa.weekly_report.acknowledgement": ["approval", "核对周报"],
  };
  const known = typeof view?.profileId === "string" && Object.hasOwn(labels, view.profileId) ? labels[view.profileId] : null;
  if (type === "business_input" && view?.schemaVersion === "agentbridge.business-view.v1" &&
      view.profileVersion === "1" && known && view.viewKind === known[0]) return known[1];
  return ({credential: "安全登录", business_input: "填写信息", execution_authorization: "核对并确认"})[type] || "继续处理";
}

/** Interaction completion is not evidence of the business operation's outcome. */
export function operationStatus(result: unknown): string | null {
  const envelope = record(result);
  // Tool transport errors do not establish whether a submitted write took effect.
  if (envelope?.isError === true) return null;
  const structured = record(envelope?.structuredContent);
  if (record(structured?.interaction) || record(record(envelope?._meta)?.["io.agentbridge/interaction"])) return null;
  const status = structured?.status;
  return typeof status === "string" && status.trim() && status !== "completed"
    ? status : null;
}

export function interactionStateLabel(type: string, state: string): string {
  if (state === "completed") {
    return ({credential: "登录已完成", business_input: "信息已提交", execution_authorization: "已授权"})[type]
      || "交互已完成";
  }
  return ({pending: "等待操作", processing: "正在处理", declined: "已取消", expired: "已失效",
    failed: "交互处理失败", superseded: "已被新交互替代"})[state] || state;
}

export function terminalPresentation(result: unknown): {title: string; message: string; label: string; tone: Tone} {
  const status = operationStatus(result);
  if (status === "succeeded") return {
    title: "操作已完成", message: "AgentBridge 已确认业务操作成功。", label: "已完成", tone: "success",
  };
  if (["unknown", "outcome_unknown", "result_unknown"].includes(status || "")) return {
    title: "业务结果待核实", message: "尚未确认业务结果。请先核对原任务，不要重复提交。", label: "结果未知", tone: "error",
  };
  if (status === "failed") return {
    title: "操作未成功", message: "请返回原任务查看失败信息和后续处理方式。", label: "处理失败", tone: "error",
  };
  return {
    title: "操作状态已更新", message: "交互完成不代表业务操作成功，请返回原任务查看核验结果。",
    label: status || "业务结果待确认", tone: "neutral",
  };
}
