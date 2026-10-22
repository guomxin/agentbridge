// These labels mirror the versioned Central profile contract. Never render
// arbitrary entryLabel/submitLabel strings as host-owned action controls.
const BUSINESS_VIEWS = new Map([
  ["oa.meeting.reply", ["form", "核对会议邀请"]],
  ["taihua.work_log.create", ["work_log", "填写工作日志"]],
  ["oa.meeting_room.application.create", ["meeting_room", "选择会议室"]],
  ["oa.meeting.create", ["meeting_room", "安排会议"]],
  ["oa.meeting_room.application.cancel", ["form", "核对撤销申请"]],
  ["oa.workflow.revoke", ["form", "核对流程撤销"]],
  ["oa.business_trip.save_draft", ["form", "填写出差草稿"]],
  ["oa.business_trip.submit", ["form", "填写出差申请"]],
  ["oa.leave.save_draft", ["form", "填写请假草稿"]],
  ["oa.leave.submit", ["form", "填写请假申请"]],
  ["oa.missed_punch.save_draft", ["form", "填写补签草稿"]],
  ["smartlight.alarm.remark.update", ["form", "编辑告警备注"]],
  ["oa.missed_punch.approval", ["approval", "核对补签材料"]],
  ["oa.efficiency_data.approval", ["approval", "核对效能数据"]],
  ["oa.travel_expense.approval", ["approval", "核对差旅报销"]],
  ["oa.business_trip.approval", ["approval", "核对出差材料"]],
  ["oa.business_settlement.approval", ["approval", "核对业务结算单"]],
  ["oa.labor_contract_renewal.approval", ["approval", "核对合同续签"]],
  ["oa.intellectual_property_declaration.approval", ["approval", "核对知识产权材料"]],
  ["oa.intellectual_property_reward.acknowledgement", ["approval", "核对知识产权奖励并阅办"]],
  ["oa.overtime.approval", ["approval", "核对加班材料"]],
  ["oa.leave.approval", ["approval", "核对请假材料"]],
  ["oa.resignation.approval", ["approval", "核对离职材料"]],
  ["oa.work_handover.approval", ["approval", "核对交接材料"]],
  ["oa.flight_application.approval", ["approval", "核对乘机申请"]],
  ["oa.standard_collaboration.approval", ["approval", "核对协同事项"]],
  ["oa.attendance_confirmation", ["approval", "核对月度考勤"]],
  ["oa.weekly_report.acknowledgement", ["approval", "核对周报"]],
  ["oa.quarterly_performance.acknowledgement", ["approval", "核对季度绩效"]],
]);
const ACTION_LABELS = new Map([...BUSINESS_VIEWS].map(([profileId, [, label]]) => [
  profileId === "oa.attendance_confirmation" ? "oa.attendance_confirmation.confirm" :
    profileId.replace(/\.approval$/, ".approve").replace(/\.acknowledgement$/, ".acknowledge"),
  label.replace(/^(?:填写|编辑|选择|安排)/, "核对"),
]));
ACTION_LABELS.set("smartlight.alarm.work_area.submit", "核对工区提交");
ACTION_LABELS.set("smartlight.alarm.work_area.revoke", "核对工区撤回");
ACTION_LABELS.set("smartlight.alarm.dispose", "核对 RTU 告警处置");
const BATCH_STATES = new Set(["running", "waiting_user", "succeeded", "partially_succeeded",
  "failed", "outcome_unknown", "canceled", "expired"]);

export function batchViewForInteraction(interaction) {
  const batch = interaction.display?.batch;
  if (!batch || batch.schemaVersion !== "agentbridge.batch-view.v1" ||
      !Number.isInteger(batch.totalCount) || batch.totalCount < 1 || batch.totalCount > 1000 ||
      !Number.isInteger(batch.currentIndex) || batch.currentIndex < 1 || batch.currentIndex > batch.totalCount ||
      !Number.isInteger(batch.succeededCount) || batch.succeededCount < 0 || batch.succeededCount > batch.totalCount ||
      !BATCH_STATES.has(batch.state)) return null;
  return { totalCount: batch.totalCount, currentIndex: batch.currentIndex,
    succeededCount: batch.succeededCount, state: batch.state };
}

export function interactionCanOpen(interaction) {
  if (interaction.state !== "pending") return false;
  if (!interaction.display?.batch) return true;
  const batch = batchViewForInteraction(interaction);
  return batch && ["running", "waiting_user"].includes(batch.state);
}

export function businessViewForInteraction(interaction) {
  if (interaction.type !== "business_input") return null;
  const view = interaction.display?.businessView;
  if (!view || view.schemaVersion !== "agentbridge.business-view.v1" ||
      view.profileVersion !== "1") return null;
  const known = BUSINESS_VIEWS.get(view.profileId);
  if (!known || known[0] !== view.viewKind) return null;
  return { profileId: view.profileId, viewKind: known[0], entryLabel: known[1] };
}

export function interactionEntryLabel(interaction) {
  const action = interaction.display?.actionView;
  const actionLabel = interaction.type === "execution_authorization" &&
    action?.schemaVersion === "agentbridge.action-view.v1" && ACTION_LABELS.get(action.capabilityId);
  const label = businessViewForInteraction(interaction)?.entryLabel || actionLabel || {
    credential: "安全登录",
    business_input: "填写信息",
    execution_authorization: "核对并确认",
  }[interaction.type] || "继续处理";
  const batch = batchViewForInteraction(interaction);
  return batch ? `第 ${batch.currentIndex}/${batch.totalCount} 项 · ${label}` : label;
}
