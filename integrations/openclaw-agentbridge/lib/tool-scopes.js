// Presentation/execution bounds only. Central still checks current grants,
// exact Skill binding, frozen materials, lease and user authorization.
const OA_PENDING_PREPARES = [
  "oa_missed_punch_approval_prepare", "oa_efficiency_data_approval_prepare", "oa_travel_expense_approval_prepare",
  "oa_business_trip_approval_prepare", "oa_business_settlement_approval_prepare", "oa_labor_contract_renewal_approval_prepare",
  "oa_intellectual_property_declaration_approval_prepare", "oa_overtime_approval_prepare", "oa_leave_approval_prepare",
  "oa_intellectual_property_reward_acknowledgement_prepare",
  "oa_resignation_approval_prepare", "oa_work_handover_approval_prepare", "oa_flight_application_approval_prepare",
  "oa_attendance_confirmation_prepare", "oa_weekly_report_acknowledgement_prepare",
  "oa_quarterly_performance_acknowledgement_prepare", "oa_standard_collaboration_approval_prepare",
];
const OA_PENDING_READS = ["oa_workflow_pending_list", "oa_workflow_detail_get", "oa_workflow_opinions_list",
  "oa_session_status", "oa_session_login", "agentbridge_interaction_get", "agentbridge_task_get", "agentbridge_task_cancel",
  "agentbridge_operation_get", "agentbridge_skill_catalog", "agentbridge_skill_get", "agentbridge_skill_authoring",
  "agentbridge_task_plan_catalog",
  "agentbridge_identity_status", "agentbridge_server_profile"];
const OA_PENDING_BATCHES = ["oa_workflow_pending_batch_prepare", "oa_missed_punch_approval_batch_prepare"];

export function staticToolScope(name) {
  return name === "oa_pending" ? new Set([...OA_PENDING_READS, ...OA_PENDING_PREPARES, ...OA_PENDING_BATCHES]) : null;
}

export function boundToolScope(binding) {
  // New snapshots carry an exact, server-normalized public capability list.
  // Legacy snapshots retain their historical scope and immutable hash.
  if (binding?.execution && binding.execution.legacy !== true) {
    const capabilities = Array.isArray(binding.execution.capabilities) ? binding.execution.capabilities : [];
    const scope = new Set(["agentbridge_interaction_get", "agentbridge_task_get", "agentbridge_task_cancel",
      "agentbridge_operation_get", "agentbridge_skill_catalog", "agentbridge_skill_get", "agentbridge_skill_authoring",
      "agentbridge_task_plan_catalog", "agentbridge_task_plan_get", "agentbridge_task_plan_cancel",
      "agentbridge_identity_status", "agentbridge_server_profile"]);
    for (const capability of capabilities) {
      if (typeof capability !== "string") continue;
      if (capability.startsWith("database.")) {
        scope.add("database_capabilities"); scope.add("database_execute");
      } else {
        scope.add(capability.replaceAll(".", "_"));
        const system = capability.split(".")[0];
        scope.add(`${system}_session_status`); scope.add(`${system}_session_login`);
        scope.add("agentbridge_task_plan_prepare");
      }
    }
    return scope;
  }
  if (binding?.skillId !== "oa-pending" || !["single", "batch"].includes(binding.profile)) return null;
  return new Set([...OA_PENDING_READS, ...OA_PENDING_PREPARES, ...(binding.profile === "batch" ? OA_PENDING_BATCHES : [])]);
}
