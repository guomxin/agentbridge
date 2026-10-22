import { createAgentBridgeMcpClient } from "../integrations/openclaw-agentbridge/lib/mcp-client.js";

const CHECKS = new Map([
  ["SessionStatus", { tool: "oa_session_status", arguments: {}, kind: "session" }],
  [
    "TaihuaSessionStatus",
    { tool: "taihua_session_status", arguments: {}, kind: "session" },
  ],
  [
    "SmartlightSessionStatus",
    { tool: "smartlight_session_status", arguments: {}, kind: "session" },
  ],
  [
    "SmartlightOverview",
    { tool: "smartlight_system_overview", arguments: {}, kind: "smartlightOverview" },
  ],
  [
    "SmartlightRuntimeOverview",
    {
      tool: "smartlight_runtime_overview",
      arguments: {},
      kind: "smartlightRuntimeOverview",
    },
  ],
  [
    "SmartlightRtuStatus",
    {
      tool: "smartlight_rtu_status_list",
      arguments: { state: "offline", page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightLampStatus",
    {
      tool: "smartlight_lamp_status_list",
      arguments: { controller_state: "offline", page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightLampAlarms",
    {
      tool: "smartlight_lamp_alarm_list",
      arguments: { last_days: 30, page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightLampAlarmAnalysis",
    {
      tool: "smartlight_lamp_alarm_analysis",
      arguments: { last_days: 30, top_n: 5 },
      kind: "smartlightAnalysis",
    },
  ],
  [
    "SmartlightRtuSurvey",
    {
      tool: "smartlight_rtu_survey_records",
      arguments: { rtu_id: "" },
      kind: "smartlightSurvey",
    },
  ],
  [
    "SmartlightLampPosts",
    {
      tool: "smartlight_lamppost_list",
      arguments: { page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightAlarms",
    {
      tool: "smartlight_alarm_list",
      arguments: { sort_by: "occurred_at", page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightAlarmRemark",
    {
      tool: "smartlight_alarm_remark_get",
      arguments: { alarm_id: "" },
      kind: "smartlightAlarmRemark",
    },
  ],
  [
    "SmartlightInspectionTasks",
    {
      tool: "smartlight_inspection_task_list",
      arguments: { page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightInspectionRunning",
    {
      tool: "smartlight_inspection_task_list",
      arguments: { state: 2, page: 1, size: 3 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightLeakage",
    {
      tool: "smartlight_leakage_summary",
      arguments: {
        last_days: 30,
        page: 1,
        size: 5,
      },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightCabinets",
    {
      tool: "smartlight_asset_search",
      arguments: { asset_type: "cabinet", page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightRtus",
    {
      tool: "smartlight_asset_search",
      arguments: { asset_type: "rtu", page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightAssetDetail",
    {
      tool: "smartlight_asset_detail",
      arguments: { asset_type: "rtu", asset_id: "" },
      kind: "smartlightAssetDetail",
    },
  ],
  [
    "SmartlightInspectionDetail",
    {
      tool: "smartlight_inspection_task_detail",
      arguments: { task_id: "" },
      kind: "smartlightInspectionDetail",
    },
  ],
  [
    "SmartlightAlarmAnalysis",
    {
      tool: "smartlight_alarm_analysis",
      arguments: { last_days: 30, top_n: 5 },
      kind: "smartlightAnalysis",
    },
  ],
  [
    "SmartlightLeakageAnalysis",
    {
      tool: "smartlight_leakage_analysis",
      arguments: { last_days: 30, top_n: 5 },
      kind: "smartlightAnalysis",
    },
  ],
  [
    "SmartlightEnergyRecords",
    {
      tool: "smartlight_energy_record_list",
      arguments: { last_days: 7, page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightEnergyAnalysis",
    {
      tool: "smartlight_energy_analysis",
      arguments: { last_days: 7, top_n: 5 },
      kind: "smartlightAnalysis",
    },
  ],
  [
    "SmartlightLampSurvey",
    {
      tool: "smartlight_lamp_survey_records",
      arguments: { page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightRtuLeakageAlarms",
    {
      tool: "smartlight_rtu_leakage_alarm_list",
      arguments: { last_days: 30, page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightRtuLeakageAnalysis",
    {
      tool: "smartlight_rtu_leakage_analysis",
      arguments: { last_days: 30, top_n: 5 },
      kind: "smartlightAnalysis",
    },
  ],
  [
    "SmartlightOffHoursCurrent",
    {
      tool: "smartlight_off_hours_current_list",
      arguments: { page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightInspectionLogs",
    {
      tool: "smartlight_inspection_log_list",
      arguments: { last_days: 30, page: 1, size: 5 },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightMaintenanceRecords",
    {
      tool: "smartlight_maintenance_record_list",
      arguments: {
        start_date: "2024-08-30",
        end_date: "2024-09-02",
        page: 1,
        size: 5,
      },
      kind: "smartlightList",
    },
  ],
  [
    "SmartlightReport",
    {
      tool: "smartlight_report_export",
      arguments: { report_type: "alarm_analysis", last_days: 30, top_n: 5 },
      kind: "smartlightReport",
    },
  ],
  [
    "OaPendingRead",
    { tool: "oa_workflow_pending_list", arguments: { limit: 1 }, kind: "list" },
  ],
  [
    "OaPendingInspect",
    {
      tool: "oa_workflow_pending_list",
      arguments: { limit: 100 },
      kind: "pendingInspect",
    },
  ],
  [
    "OaMeetingRooms",
    {
      tool: "oa_meeting_room_availability_list",
      arguments: { limit: 100 },
      kind: "list",
    },
  ],
  [
    "OaMeetingRoomApplications",
    {
      tool: "oa_meeting_room_my_applications_list",
      arguments: { limit: 10 },
      kind: "list",
    },
  ],
  [
    "OaAddressbookOrganization",
    {
      tool: "oa_addressbook_organization_tree",
      arguments: { limit: 20 },
      kind: "addressbookList",
    },
  ],
  [
    "OaAddressbookPersonSearch",
    {
      tool: "oa_addressbook_person_search",
      arguments: { query: "辛国茂", search_type: "name", limit: 5 },
      kind: "addressbookList",
    },
  ],
  [
    "OaAddressbookGroups",
    {
      tool: "oa_addressbook_group_list",
      arguments: {},
      kind: "addressbookList",
    },
  ],
  [
    "OaAddressbookPrivateContacts",
    {
      tool: "oa_addressbook_private_contact_search",
      arguments: { limit: 5 },
      kind: "addressbookList",
    },
  ],
  [
    "CertificateSearch",
    {
      tool: "oa_certificate_search",
      arguments: {
        name: "一种基于动态温差补偿的光敏值修正方法及系统",
        document_type: "patent_certificate",
        limit: 5,
      },
      kind: "documentSearch",
    },
  ],
  [
    "TaihuaMyLogs",
    { tool: "taihua_work_log_my_list", arguments: { limit: 1 }, kind: "list" },
  ],
  [
    "YuqueSessionStatus",
    { tool: "yuque_session_status", arguments: {}, kind: "session" },
  ],
  [
    "YuquePublicBooks",
    { tool: "yuque_public_books_list", arguments: {}, kind: "list" },
  ],
  [
    "YuqueDocumentCatalog",
    {
      tool: "yuque_document_catalog",
      arguments: { book: "", limit: 20 },
      kind: "list",
    },
  ],
  [
    "YuqueDocumentSearch",
    {
      tool: "yuque_document_search",
      arguments: { query: "AI", book: "", page: 1, limit: 20 },
      kind: "list",
    },
  ],
  [
    "YuqueDocumentRead",
    {
      tool: "yuque_document_read",
      arguments: { document: "", book: "", max_chars: 4000 },
      kind: "yuqueDocument",
    },
  ],
  ["LoginReuse", { tool: "oa_session_login", arguments: {}, kind: "login" }],
  [
    "YuqueLoginReuse",
    { tool: "yuque_session_login", arguments: {}, kind: "login" },
  ],
  [
    "CrossEndpointContext",
    {
      tool: "agentbridge_host_cross_endpoint_context",
      arguments: {
        agent_host: "openclaw",
        endpoint_key: "",
        max_age_minutes: 360,
        limit: 12,
      },
      kind: "crossEndpointContext",
    },
  ],
  [
    "TaskContinuation",
    {
      tool: "agentbridge_host_task_continuation_resolve",
      arguments: {
        agent_host: "openclaw",
        endpoint_key: "",
        ordinal: null,
        source_client_type: "web",
        cross_endpoint_only: true,
        prefer_active: true,
        reuse_selected: false,
        allow_follow_up: false,
        max_age_minutes: 1_440,
        limit: 8,
      },
      kind: "taskContinuation",
    },
  ],
]);

const REQUIRED_RELEASE_TOOLS = [
  "agentbridge_host_task_ensure",
  "agentbridge_host_task_observe",
  "agentbridge_host_task_recovery_list",
  "agentbridge_host_task_list",
  "agentbridge_host_task_continuation_resolve",
  "agentbridge_host_cross_endpoint_context",
  "agentbridge_host_interaction_present",
  "agentbridge_host_notification_claim",
  "agentbridge_host_notification_ack",
  "oa_certificate_search",
  "oa_certificate_prepare_download",
  "oa_certificate_prepare_downloads",
  "oa_business_trip_prepare",
  "oa_business_trip_save_draft",
  "oa_business_trip_submit_prepare",
  "oa_business_trip_submit",
  "oa_leave_prepare",
  "oa_leave_save_draft",
  "oa_leave_submit_prepare",
  "oa_leave_submit",
  "oa_workflow_revoke_prepare",
  "oa_workflow_revoke",
  "oa_missed_punch_prepare",
  "oa_missed_punch_save_draft",
  "oa_missed_punch_approval_prepare",
  "oa_missed_punch_approval_batch_prepare",
  "oa_workflow_pending_batch_prepare",
  "oa_missed_punch_approve",
  "oa_efficiency_data_approval_prepare",
  "oa_efficiency_data_approve",
  "oa_travel_expense_approval_prepare",
  "oa_travel_expense_approve",
  "oa_business_trip_approval_prepare",
  "oa_business_trip_approve",
  "oa_labor_contract_renewal_approval_prepare",
  "oa_labor_contract_renewal_approve",
  "oa_intellectual_property_declaration_approval_prepare",
  "oa_intellectual_property_declaration_approve",
  "oa_intellectual_property_reward_acknowledgement_prepare",
  "oa_intellectual_property_reward_acknowledge",
  "oa_overtime_approval_prepare",
  "oa_overtime_approve",
  "oa_leave_approval_prepare",
  "oa_leave_approve",
  "oa_resignation_approval_prepare",
  "oa_work_handover_approval_prepare",
  "oa_flight_application_approval_prepare",
  "oa_resignation_approve",
  "oa_work_handover_approve",
  "oa_flight_application_approve",
  "oa_attendance_confirmation_prepare",
  "oa_attendance_confirm",
  "oa_weekly_report_acknowledgement_prepare",
  "oa_weekly_report_acknowledge",
  "oa_standard_collaboration_approval_prepare",
  "oa_standard_collaboration_approve",
  "oa_meeting_room_availability_list",
  "oa_meeting_room_my_applications_list",
  "oa_meeting_room_application_prepare",
  "oa_meeting_room_application_create",
  "oa_meeting_room_application_cancel_prepare",
  "oa_meeting_room_application_cancel",
  "oa_meeting_create_prepare",
  "oa_meeting_create",
  "taihua_work_log_my_list",
  "taihua_work_log_team_list",
  "taihua_project_search",
  "taihua_work_log_create_prepare",
  "taihua_work_log_create",
  "taihua_session_status",
  "taihua_session_login",
  "smartlight_system_overview",
  "smartlight_runtime_overview",
  "smartlight_rtu_status_list",
  "smartlight_lamp_status_list",
  "smartlight_lamp_alarm_list",
  "smartlight_lamp_alarm_analysis",
  "smartlight_rtu_survey_records",
  "smartlight_lamppost_list",
  "smartlight_alarm_list",
  "smartlight_alarm_remark_get",
  "smartlight_inspection_task_list",
  "smartlight_leakage_summary",
  "smartlight_asset_search",
  "smartlight_asset_detail",
  "smartlight_alarm_analysis",
  "smartlight_inspection_task_detail",
  "smartlight_leakage_analysis",
  "smartlight_energy_record_list",
  "smartlight_energy_analysis",
  "smartlight_lamp_survey_records",
  "smartlight_rtu_leakage_alarm_list",
  "smartlight_rtu_leakage_analysis",
  "smartlight_off_hours_current_list",
  "smartlight_inspection_log_list",
  "smartlight_maintenance_record_list",
  "smartlight_report_export",
  "smartlight_alarm_remark_update_prepare",
  "smartlight_alarm_remark_update",
  "smartlight_alarm_work_area_submit_prepare",
  "smartlight_alarm_work_area_submit",
  "smartlight_alarm_work_area_revoke_prepare",
  "smartlight_alarm_work_area_revoke",
  "smartlight_rtu_alarm_dispose_prepare",
  "smartlight_rtu_alarm_dispose",
  "smartlight_session_status",
  "smartlight_session_login",
  "yuque_public_books_list",
  "yuque_document_catalog",
  "yuque_document_search",
  "yuque_document_read",
  "yuque_session_status",
  "yuque_session_login",
];

function argument(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

function safeCode(value, fallback = "MCP_SMOKE_FAILED") {
  const normalized = String(value ?? "")
    .toUpperCase()
    .replace(/[^A-Z0-9_.-]/g, "_")
    .slice(0, 80);
  return normalized || fallback;
}

async function readStdin() {
  let input = "";
  process.stdin.setEncoding("utf8");
  for await (const chunk of process.stdin) {
    input += chunk;
  }
  return input;
}

try {
  const checkName = argument("--check", "SessionStatus");
  const serverName = argument("--server-name", "agentbridge");
  const identityLabel = argument("--identity-label", null);
  const check = CHECKS.get(checkName);
  if (
    !check &&
    !["Release", "ToolCatalog", "WorkflowCollections"].includes(checkName)
  ) {
    throw Object.assign(new Error("Unsupported smoke check"), { code: "INVALID_CHECK" });
  }
  if (checkName === "CertificateSearch") {
    const namesBase64 = argument("--certificate-names-base64", null);
    let names = null;
    if (namesBase64) {
      names = JSON.parse(Buffer.from(namesBase64, "base64").toString("utf8"));
      if (!Array.isArray(names) || names.length < 1 || names.length > 20) {
        throw Object.assign(new Error("Invalid certificate names"), {
          code: "INVALID_CERTIFICATE_NAMES",
        });
      }
    }
    check.arguments = {
      ...check.arguments,
      name: names ? null : argument("--certificate-name", check.arguments.name),
      names,
      document_type: argument(
        "--certificate-document-type",
        check.arguments.document_type,
      ),
    };
  }
  if (checkName === "OaAddressbookOrganization") {
    const keyword = argument("--addressbook-query", "").trim();
    check.arguments = { keyword: keyword || undefined, limit: 200 };
  }
  if (checkName === "OaAddressbookPersonSearch") {
    const query = argument("--addressbook-query", "辛国茂").trim();
    if (!query) {
      throw Object.assign(new Error("Addressbook query is required"), {
        code: "ADDRESSBOOK_QUERY_REQUIRED",
      });
    }
    check.arguments = { query, search_type: "name", limit: 5 };
  }
  if (checkName === "SmartlightAssetDetail") {
    const assetId = argument("--smartlight-asset-id", "").trim();
    const assetType = argument("--smartlight-asset-type", "rtu").trim();
    if (!assetId || !["cabinet", "rtu", "lamppost"].includes(assetType)) {
      throw Object.assign(new Error("Smartlight asset detail arguments are invalid"), {
        code: "SMARTLIGHT_ASSET_ARGUMENTS_INVALID",
      });
    }
    check.arguments = { asset_type: assetType, asset_id: assetId };
  }
  if (checkName === "SmartlightAlarmRemark") {
    const alarmId = argument("--smartlight-alarm-id", "").trim();
    if (!alarmId) {
      throw Object.assign(new Error("Smartlight alarm ID is required"), {
        code: "SMARTLIGHT_ALARM_ID_REQUIRED",
      });
    }
    check.arguments = { alarm_id: alarmId };
  }
  if (checkName === "SmartlightRtuSurvey") {
    const rtuId = argument("--smartlight-rtu-id", "").trim();
    const rtuKeyword = argument("--smartlight-rtu-keyword", "").trim();
    const startTime = argument("--smartlight-start-time", "").trim();
    const endTime = argument("--smartlight-end-time", "").trim();
    if (!rtuId && !rtuKeyword) {
      throw Object.assign(new Error("Smartlight RTU ID or keyword is required"), {
        code: "SMARTLIGHT_RTU_ARGUMENTS_INVALID",
      });
    }
    if (Boolean(startTime) !== Boolean(endTime)) {
      throw Object.assign(new Error("Smartlight survey time range is incomplete"), {
        code: "SMARTLIGHT_SURVEY_RANGE_INVALID",
      });
    }
    check.arguments = {};
    if (rtuId) check.arguments.rtu_id = rtuId;
    if (rtuKeyword) check.arguments.rtu_keyword = rtuKeyword;
    if (startTime) {
      check.arguments.start_time = startTime;
      check.arguments.end_time = endTime;
    }
  }
  if (checkName === "SmartlightInspectionDetail") {
    const taskId = argument("--smartlight-task-id", "").trim();
    const detailDate = argument("--smartlight-detail-date", "").trim();
    if (!taskId) {
      throw Object.assign(new Error("Smartlight task ID is required"), {
        code: "SMARTLIGHT_TASK_ID_REQUIRED",
      });
    }
    check.arguments = { task_id: taskId };
    if (detailDate) check.arguments.detail_date = detailDate;
  }
  if (checkName === "SmartlightReport") {
    const reportType = argument(
      "--smartlight-report-type",
      "alarm_analysis",
    ).trim();
    const assetType = argument("--smartlight-asset-type", "rtu").trim();
    const taskId = argument("--smartlight-task-id", "").trim();
    const detailDate = argument("--smartlight-detail-date", "").trim();
    if (
      ![
        "alarm_analysis",
        "lamp_alarm_analysis",
        "leakage_analysis",
        "asset_inventory",
        "inspection_progress",
        "energy_records",
        "energy_analysis",
        "lamp_survey_records",
        "rtu_leakage_alarms",
        "rtu_leakage_analysis",
        "inspection_logs",
        "maintenance_records",
      ].includes(reportType)
    ) {
      throw Object.assign(new Error("Smartlight report type is invalid"), {
        code: "SMARTLIGHT_REPORT_TYPE_INVALID",
      });
    }
    check.arguments = { report_type: reportType };
    if (
      [
        "alarm_analysis",
        "lamp_alarm_analysis",
        "leakage_analysis",
        "rtu_leakage_analysis",
      ].includes(reportType)
    ) {
      check.arguments.last_days = 30;
      check.arguments.top_n = 5;
    } else if (reportType === "energy_analysis") {
      check.arguments.last_days = 7;
      check.arguments.top_n = 5;
    } else if (reportType === "energy_records") {
      check.arguments.last_days = 7;
    } else if (reportType === "lamp_survey_records") {
      check.arguments.last_days = 1;
    } else if (reportType === "rtu_leakage_alarms") {
      check.arguments.last_days = 30;
    } else if (reportType === "inspection_logs") {
      check.arguments.last_days = 30;
    } else if (reportType === "maintenance_records") {
      check.arguments.start_date = "2024-08-30";
      check.arguments.end_date = "2024-09-02";
    } else if (reportType === "asset_inventory") {
      if (!["cabinet", "rtu", "lamppost"].includes(assetType)) {
        throw Object.assign(new Error("Smartlight asset type is invalid"), {
          code: "SMARTLIGHT_ASSET_ARGUMENTS_INVALID",
        });
      }
      check.arguments.asset_type = assetType;
    } else {
      if (!taskId) {
        throw Object.assign(new Error("Smartlight task ID is required"), {
          code: "SMARTLIGHT_TASK_ID_REQUIRED",
        });
      }
      check.arguments.task_id = taskId;
      if (detailDate) check.arguments.detail_date = detailDate;
    }
  }
  if (
    ["YuqueDocumentCatalog", "YuqueDocumentSearch", "YuqueDocumentRead"].includes(
      checkName,
    )
  ) {
    check.arguments = {
      ...check.arguments,
      book: argument("--yuque-book", check.arguments.book),
    };
  }
  if (checkName === "YuqueDocumentSearch") {
    check.arguments = {
      ...check.arguments,
      query: argument("--yuque-query", check.arguments.query),
    };
  }
  if (checkName === "YuqueDocumentRead") {
    const document = argument("--yuque-document", "");
    const rowOffset = Number(argument("--yuque-row-offset", "0"));
    const maxRows = Number(argument("--yuque-max-rows", "100"));
    const maxChars = Number(argument("--yuque-max-chars", "4000"));
    if (!document) {
      throw Object.assign(new Error("Yuque document is required"), {
        code: "YUQUE_DOCUMENT_REQUIRED",
      });
    }
    if (!Number.isInteger(maxChars) || maxChars < 500 || maxChars > 50000) {
      throw Object.assign(new Error("Yuque max chars is invalid"), {
        code: "YUQUE_MAX_CHARS_INVALID",
      });
    }
    if (!Number.isInteger(rowOffset) || rowOffset < 0 || rowOffset > 100000) {
      throw Object.assign(new Error("Yuque row offset is invalid"), {
        code: "YUQUE_ROW_OFFSET_INVALID",
      });
    }
    if (!Number.isInteger(maxRows) || maxRows < 1 || maxRows > 500) {
      throw Object.assign(new Error("Yuque max rows is invalid"), {
        code: "YUQUE_MAX_ROWS_INVALID",
      });
    }
    check.arguments = {
      ...check.arguments,
      document,
      row_offset: rowOffset,
      max_rows: maxRows,
      max_chars: maxChars,
    };
  }
  if (["CrossEndpointContext", "TaskContinuation"].includes(checkName)) {
    const endpointKey = argument("--endpoint-key", "").trim();
    if (!endpointKey) {
      throw Object.assign(new Error("Endpoint key is required"), {
        code: "ENDPOINT_KEY_REQUIRED",
      });
    }
    check.arguments = {
      ...check.arguments,
      endpoint_key: endpointKey,
    };
  }
  if (checkName === "TaskContinuation") {
    const ordinal = Number(argument("--task-ordinal", "0"));
    if (!Number.isInteger(ordinal) || ordinal < 0 || ordinal > 20) {
      throw Object.assign(new Error("Task ordinal is invalid"), {
        code: "TASK_ORDINAL_INVALID",
      });
    }
    const sourceClientType = argument(
      "--source-client-type",
      check.arguments.source_client_type,
    ).trim();
    check.arguments = {
      ...check.arguments,
      ordinal: ordinal || null,
      source_client_type: sourceClientType || null,
      cross_endpoint_only: Boolean(sourceClientType),
    };
  }

  const server = JSON.parse(await readStdin());
  if (
    typeof server?.url !== "string" ||
    typeof server?.headers?.Authorization !== "string" ||
    !server.headers.Authorization.startsWith("Bearer ")
  ) {
    throw Object.assign(new Error("Resolved MCP configuration is incomplete"), {
      code: "INVALID_MCP_CONFIG",
    });
  }

  const client = createAgentBridgeMcpClient({
    hostConfig: { mcp: { servers: { [serverName]: server } } },
    serverName,
  });
  if (!client) {
    throw Object.assign(new Error("MCP client was not created"), {
      code: "MCP_CLIENT_NOT_CREATED",
    });
  }

  if (checkName === "ToolCatalog") {
    const tools = await client.listTools();
    const rawNames = tools
      .map((tool) => String(tool?.name ?? ""))
      .filter(Boolean)
      .sort();
    const profilePayload = await client.callTool(
      "agentbridge_host_identity_profile",
      { agent_host: "openclaw" },
      {
        meta: {
          "io.agentbridge/host": {
            version: "1",
            agentHost: "openclaw",
          },
        },
      },
    );
    if (profilePayload?.isError || profilePayload?.error) {
      throw Object.assign(new Error("Host identity profile read failed"), {
        code:
          profilePayload?.error?.code || "HOST_IDENTITY_PROFILE_FAILED",
      });
    }
    const profile = profilePayload?.result ?? profilePayload;
    const allowedNames = Array.isArray(
      profile?.agentToolAccess?.allowedToolNames,
    )
      ? profile.agentToolAccess.allowedToolNames
          .map((name) => String(name))
          .filter(Boolean)
          .sort()
      : null;
    if (!allowedNames) {
      throw Object.assign(new Error("Host identity profile omitted tool access"), {
        code: "HOST_IDENTITY_PROFILE_CONTRACT_MISMATCH",
      });
    }
    const smartlightTools = allowedNames.filter((name) =>
      name.startsWith("smartlight_"),
    );
    const expectedSmartlightTools = new Set([
      "smartlight_system_overview",
      "smartlight_runtime_overview",
      "smartlight_rtu_status_list",
      "smartlight_lamp_status_list",
      "smartlight_lamp_alarm_list",
      "smartlight_lamp_alarm_analysis",
      "smartlight_rtu_survey_records",
      "smartlight_lamppost_list",
      "smartlight_alarm_list",
      "smartlight_alarm_remark_get",
      "smartlight_inspection_task_list",
      "smartlight_leakage_summary",
      "smartlight_asset_search",
      "smartlight_asset_detail",
      "smartlight_alarm_analysis",
      "smartlight_inspection_task_detail",
      "smartlight_leakage_analysis",
      "smartlight_energy_record_list",
      "smartlight_energy_analysis",
      "smartlight_lamp_survey_records",
      "smartlight_rtu_leakage_alarm_list",
      "smartlight_rtu_leakage_analysis",
      "smartlight_off_hours_current_list",
      "smartlight_inspection_log_list",
      "smartlight_maintenance_record_list",
      "smartlight_report_export",
      "smartlight_alarm_remark_update_prepare",
      "smartlight_alarm_work_area_submit_prepare",
      "smartlight_alarm_work_area_revoke_prepare",
      "smartlight_session_status",
      "smartlight_session_login",
    ]);
    process.stdout.write(
      JSON.stringify({
        status: "succeeded",
        check: checkName,
        identityLabel,
        rawToolCount: rawNames.length,
        allowedToolCount: allowedNames.length,
        smartlightTools,
        smartlightUnexpectedTools: smartlightTools.filter(
          (name) => !expectedSmartlightTools.has(name),
        ),
      }) + "\n",
    );
  } else if (checkName === "WorkflowCollections") {
    const definitions = [
      ["pending", "oa_workflow_pending_list", "section_api"],
      ["sent", "oa_workflow_sent_list", "history_page_grid"],
      ["done", "oa_workflow_done_list", "history_page_grid"],
      ["tracked", "oa_workflow_tracked_list", "tracked_page_grid"],
    ];
    const collections = {};
    const idSets = {};
    for (const [collection, tool, expectedSource] of definitions) {
      const payload = await client.callTool(tool, { limit: 100 });
      if (payload?.error) {
        throw Object.assign(new Error("Workflow collection read failed"), {
          code: payload.error.code || "WORKFLOW_COLLECTION_READ_FAILED",
        });
      }
      const result = payload?.result ?? payload;
      if (result?.collection !== collection || result?.source !== expectedSource) {
        throw Object.assign(new Error("Workflow collection contract mismatch"), {
          code: "WORKFLOW_COLLECTION_CONTRACT_MISMATCH",
        });
      }
      const ids = (result?.items ?? [])
        .map((item) => String(item?.affair_id ?? ""))
        .filter(Boolean);
      idSets[collection] = new Set(ids);
      collections[collection] = {
        source: result.source,
        loaded: Number(result.count ?? ids.length),
        total: Number(result.total ?? ids.length),
        page: Number(result.page ?? 1),
      };
    }
    const overlap = (left, right) =>
      [...idSets[left]].filter((value) => idSets[right].has(value)).length;
    process.stdout.write(
      JSON.stringify({
        status: "succeeded",
        check: checkName,
        collections,
        overlaps: {
          sentDone: overlap("sent", "done"),
          sentTracked: overlap("sent", "tracked"),
          doneTracked: overlap("done", "tracked"),
        },
      }) + "\n",
    );
  } else {
    let toolCount = null;
    if (checkName === "Release") {
      const tools = await client.listTools();
      const names = new Set(tools.map((tool) => tool?.name).filter(Boolean));
      const missing = REQUIRED_RELEASE_TOOLS.filter((name) => !names.has(name));
      if (missing.length) {
        throw Object.assign(new Error("Release MCP tool catalog is incomplete"), {
          code: "MCP_TOOL_CATALOG_INCOMPLETE",
        });
      }
      toolCount = tools.length;
    }

    const effectiveCheck = check ?? {
      kind: "release",
      tool: null,
      arguments: {},
    };
    const hostTaskId = argument("--host-task-id", "").trim();
    if (hostTaskId && (hostTaskId.length < 16 || hostTaskId.length > 128)) {
      throw Object.assign(new Error("Host task ID is invalid"), {
        code: "HOST_TASK_ID_INVALID",
      });
    }
    const requestMeta = hostTaskId
      ? {
          "io.agentbridge/task": {
            taskId: hostTaskId,
          },
        }
      : ["crossEndpointContext", "taskContinuation"].includes(
            effectiveCheck.kind,
          )
        ? {
            "io.agentbridge/host": {
              version: "1",
              agentHost: "openclaw",
            },
          }
        : null;
    const payload = effectiveCheck.kind === "release"
      ? { result: {} }
      : await client.callTool(
          effectiveCheck.tool,
          effectiveCheck.arguments,
          requestMeta ? { meta: requestMeta } : undefined,
        );
    if (payload?.isError) {
      throw Object.assign(new Error("AgentBridge MCP tool returned an error"), {
        code: "MCP_TOOL_ERROR",
      });
    }
    const errorCode = payload?.error?.code ? safeCode(payload.error.code) : null;
    let result = payload?.result ?? payload;
    if (effectiveCheck.kind === "session" && (payload?.error || errorCode)) {
      throw Object.assign(new Error("Session status check failed"), {
        code: errorCode || "SESSION_STATUS_CHECK_FAILED",
      });
    }
    const expectedText = argument("--expected-text", "");
    if (effectiveCheck.kind === "pendingInspect") {
      const items = Array.isArray(result?.items) ? result.items : [];
      const selected = expectedText
        ? items.find((item) => String(item?.title ?? "").includes(expectedText))
        : items[0];
      if (!selected?.affair_id) {
        throw Object.assign(new Error("Pending workflow was not found"), {
          code: "PENDING_WORKFLOW_NOT_FOUND",
        });
      }
      const detailPayload = await client.callTool("oa_workflow_detail_get", {
        collection: "pending",
        affair_id: String(selected.affair_id),
        text_limit: 20_000,
      });
      if (detailPayload?.error) {
        throw Object.assign(new Error("Pending workflow detail read failed"), {
          code: detailPayload.error.code || "PENDING_WORKFLOW_DETAIL_FAILED",
        });
      }
      const detail = detailPayload?.result ?? detailPayload;
      result = {
        selected,
        detail,
      };
    }
    const summary =
      effectiveCheck.kind === "release"
        ? {
            status: "succeeded",
            check: checkName,
            identityLabel,
            toolCount,
            requiredReleaseToolsPresent: true,
            businessSessionCheck: false,
          }
      : effectiveCheck.kind === "login"
        ? {
            status: "succeeded",
            check: checkName,
            identityLabel,
            operationStatus: String(payload?.status ?? "unknown").slice(0, 80),
            reused: Boolean(payload?.reused),
            hasInteraction: Boolean(payload?.interaction),
            nextAction: payload?.nextAction?.type
              ? String(payload.nextAction.type).slice(0, 80)
              : null,
            errorCode,
          }
        : effectiveCheck.kind === "documentSearch"
          ? certificateSearchSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "list"
          ? {
              status: "succeeded",
              check: checkName,
              identityLabel,
              itemCount: Number(result?.count ?? result?.items?.length ?? 0),
              total: Number(result?.total ?? result?.count ?? result?.items?.length ?? 0),
              errorCode,
            }
          : effectiveCheck.kind === "addressbookList"
          ? addressbookListSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "smartlightOverview"
          ? smartlightOverviewSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "smartlightRuntimeOverview"
          ? smartlightRuntimeOverviewSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "smartlightList"
          ? smartlightListSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "smartlightSurvey"
          ? smartlightSurveySummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "smartlightAnalysis"
          ? smartlightAnalysisSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "smartlightAssetDetail"
          ? smartlightAssetDetailSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "smartlightAlarmRemark"
          ? smartlightAlarmRemarkSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "smartlightInspectionDetail"
          ? smartlightInspectionDetailSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "smartlightReport"
          ? await smartlightReportSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "pendingInspect"
          ? {
              status: "succeeded",
              check: checkName,
              identityLabel,
              selected: result.selected,
              detail: result.detail,
              errorCode,
            }
          : effectiveCheck.kind === "yuqueDocument"
          ? yuqueDocumentSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : effectiveCheck.kind === "crossEndpointContext"
          ? crossEndpointContextSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
              expectedText,
            })
          : effectiveCheck.kind === "taskContinuation"
          ? taskContinuationSummary({
              payload,
              result,
              checkName,
              identityLabel,
              errorCode,
            })
          : {
              status: "succeeded",
              check: checkName,
              identityLabel,
              toolCount,
              requiredReleaseToolsPresent: checkName === "Release" ? true : null,
              sessionStatus: String(result?.status ?? "unknown").slice(0, 80),
              sessionId: result?.sessionId ?? null,
              systemId: result?.systemId ?? null,
              userSubject: result?.userSubject ?? null,
              downstreamPrincipalRef: result?.downstreamPrincipalRef ?? null,
              keepaliveState: result?.keepaliveState ?? null,
              lastActivityAt: result?.lastActivityAt ?? null,
              lastKeepaliveAt: result?.lastKeepaliveAt ?? null,
              checkedAt: result?.checkedAt ?? payload?.checkedAt ?? null,
              errorCode,
            };
    process.stdout.write(JSON.stringify(summary) + "\n");
  }
} catch (error) {
  process.stdout.write(
    JSON.stringify({ status: "failed", errorCode: safeCode(error?.code) }) + "\n",
  );
  process.exitCode = 1;
}

function smartlightListSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode) {
    throw Object.assign(new Error("Smartlight read failed"), {
      code: errorCode || "SMARTLIGHT_READ_FAILED",
    });
  }
  if (!result || !Array.isArray(result.items)) {
    throw Object.assign(new Error("Smartlight list contract mismatch"), {
      code: "SMARTLIGHT_LIST_CONTRACT_MISMATCH",
    });
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    itemCount: Number(result.count ?? result.items.length),
    total: Number(
      result.total ?? result.downstreamTotal ?? result.count ?? result.items.length,
    ),
    summary: result.summary ?? null,
    dateRange: result.dateRange ?? null,
    rangeSummary: result.rangeSummary ?? null,
    summaryScope: result.summaryScope ?? null,
    sort: result.sort ?? null,
    latestGroup: result.latestGroup ?? null,
    filters: result.filters ?? null,
    firstItem: result.items[0] ?? null,
    errorCode: null,
  };
}

function addressbookListSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode) {
    throw Object.assign(new Error("OA addressbook read failed"), {
      code: errorCode || "OA_ADDRESSBOOK_READ_FAILED",
    });
  }
  if (!result || !Array.isArray(result.items)) {
    throw Object.assign(new Error("OA addressbook list contract mismatch"), {
      code: "OA_ADDRESSBOOK_LIST_CONTRACT_MISMATCH",
    });
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    itemCount: Number(result.count ?? result.items.length),
    total: Number(result.total ?? result.source_total ?? result.items.length),
    organization: result.organization ?? null,
    firstItem: result.items[0] ?? null,
    errorCode: null,
  };
}

function smartlightOverviewSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode) {
    throw Object.assign(new Error("Smartlight overview failed"), {
      code: errorCode || "SMARTLIGHT_OVERVIEW_FAILED",
    });
  }
  if (!result?.principal || result?.cabinetTotal == null || result?.lampPostTotal == null) {
    throw Object.assign(new Error("Smartlight overview contract mismatch"), {
      code: "SMARTLIGHT_OVERVIEW_CONTRACT_MISMATCH",
    });
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    cabinetTotal: Number(result.cabinetTotal),
    lampPostTotal: Number(result.lampPostTotal),
    lampPostCounts: result.lampPostCounts ?? null,
    observedPrincipal: result.principal.name ?? null,
    errorCode: null,
  };
}

function smartlightRuntimeOverviewSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode) {
    throw Object.assign(new Error("Smartlight runtime overview failed"), {
      code: errorCode || "SMARTLIGHT_RUNTIME_OVERVIEW_FAILED",
    });
  }
  if (
    result?.scope !== "authenticated_user_runtime_pages" ||
    result?.rtu?.total == null ||
    result?.singleLamp?.controllerTotal == null ||
    !result?.observedAt
  ) {
    throw Object.assign(new Error("Smartlight runtime overview contract mismatch"), {
      code: "SMARTLIGHT_RUNTIME_OVERVIEW_CONTRACT_MISMATCH",
    });
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    observedAt: result.observedAt,
    rtu: result.rtu,
    singleLamp: result.singleLamp,
    errorCode: null,
  };
}

function smartlightSurveySummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode) {
    throw Object.assign(new Error("Smartlight RTU survey failed"), {
      code: errorCode || "SMARTLIGHT_RTU_SURVEY_FAILED",
    });
  }
  if (
    result?.resolved !== true ||
    !result?.rtuId ||
    !Array.isArray(result?.items) ||
    !result?.dateRange
  ) {
    throw Object.assign(new Error("Smartlight RTU survey contract mismatch"), {
      code: "SMARTLIGHT_RTU_SURVEY_CONTRACT_MISMATCH",
    });
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    rtuId: result.rtuId,
    itemCount: Number(result.count ?? result.items.length),
    total: Number(result.total ?? result.items.length),
    dateRange: result.dateRange,
    firstItem: result.items[0] ?? null,
    errorCode: null,
  };
}

function smartlightAnalysisSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode) {
    throw Object.assign(new Error("Smartlight analysis failed"), {
      code: errorCode || "SMARTLIGHT_ANALYSIS_FAILED",
    });
  }
  if (
    !result ||
    result.analyzedCount == null ||
    result.downstreamTotal == null ||
    result.truncated == null
  ) {
    throw Object.assign(new Error("Smartlight analysis contract mismatch"), {
      code: "SMARTLIGHT_ANALYSIS_CONTRACT_MISMATCH",
    });
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    analyzedCount: Number(result.analyzedCount),
    downstreamTotal: Number(result.downstreamTotal),
    truncated: Boolean(result.truncated),
    dateRange: result.dateRange ?? null,
    dailyTrend: result.dailyTrend ?? null,
    errorCode: null,
  };
}

function smartlightAssetDetailSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode || result?.found !== true || !result?.detail) {
    throw Object.assign(new Error("Smartlight asset detail failed"), {
      code: errorCode || "SMARTLIGHT_ASSET_DETAIL_FAILED",
    });
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    assetType: result.assetType ?? null,
    assetId: result.assetId ?? null,
    code: result.detail.code ?? null,
    name: result.detail.name ?? null,
    relayTotal: result.relayTotal ?? null,
    firstRelay: result.relays?.[0] ?? null,
    errorCode: null,
  };
}

function smartlightAlarmRemarkSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (
    payload?.error ||
    errorCode ||
    !result ||
    typeof result.alarmId !== "string" ||
    typeof result.hasRemark !== "boolean"
  ) {
    throw Object.assign(new Error("Smartlight alarm remark failed"), {
      code: errorCode || "SMARTLIGHT_ALARM_REMARK_FAILED",
    });
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    alarmId: result.alarmId,
    hasRemark: result.hasRemark,
    remark: result.remark ?? null,
    createUser: result.createUser ?? null,
    createTime: result.createTime ?? null,
    errorCode: null,
  };
}

function smartlightInspectionDetailSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode || result?.found !== true || !Array.isArray(result?.days)) {
    throw Object.assign(new Error("Smartlight inspection detail failed"), {
      code: errorCode || "SMARTLIGHT_INSPECTION_DETAIL_FAILED",
    });
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    taskId: result.taskId ?? null,
    taskName: result.task?.taskName ?? null,
    dailyCount: Number(result.dailyCount ?? result.days.length),
    firstDay: result.days[0] ?? null,
    detailDateFound: result.detailDateFound ?? null,
    clockinCount: result.clockinCount ?? null,
    errorCode: null,
  };
}

async function smartlightReportSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode || result?.status !== "succeeded") {
    throw Object.assign(new Error("Smartlight report export failed"), {
      code: errorCode || "SMARTLIGHT_REPORT_EXPORT_FAILED",
    });
  }
  const file = result?.file;
  const report = result?.report;
  if (
    result?.schemaVersion !== "agentbridge.document_delivery.v1" ||
    file?.contentType !== "text/csv" ||
    typeof file?.mediaUrl !== "string" ||
    !file.mediaUrl.startsWith("https://") ||
    !String(file?.filename || "").toLowerCase().endsWith(".csv") ||
    !report?.reportType
  ) {
    throw Object.assign(new Error("Smartlight report contract mismatch"), {
      code: "SMARTLIGHT_REPORT_CONTRACT_MISMATCH",
    });
  }
  const response = await fetch(file.mediaUrl, {
    headers: { Accept: "text/csv" },
    signal: AbortSignal.timeout(45_000),
  });
  if (!response.ok) {
    throw Object.assign(new Error("Smartlight report download failed"), {
      code: `SMARTLIGHT_REPORT_DOWNLOAD_HTTP_${response.status}`,
    });
  }
  const contentType = String(response.headers.get("content-type") || "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase();
  const body = Buffer.from(await response.arrayBuffer());
  if (
    contentType !== "text/csv" ||
    body.length < 3 ||
    body[0] !== 0xef ||
    body[1] !== 0xbb ||
    body[2] !== 0xbf
  ) {
    throw Object.assign(new Error("Smartlight report file is invalid"), {
      code: "SMARTLIGHT_REPORT_FILE_INVALID",
    });
  }
  const expiresAt = Date.parse(String(file.expiresAt || ""));
  const remainingSeconds = Math.round((expiresAt - Date.now()) / 1000);
  if (!Number.isFinite(expiresAt) || remainingSeconds < 1_680 || remainingSeconds > 1_800) {
    throw Object.assign(new Error("Smartlight report TTL is invalid"), {
      code: "SMARTLIGHT_REPORT_TTL_INVALID",
    });
  }
  const text = body.subarray(3).toString("utf8");
  const lineCount = text ? text.split(/\r?\n/).filter(Boolean).length : 0;
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    reportType: report.reportType,
    rowCount: Number(report.rowCount ?? 0),
    truncated: Boolean(report?.metadata?.truncated),
    filename: file.filename,
    contentType,
    byteSize: body.length,
    csvLineCount: lineCount,
    hasUtf8Bom: true,
    remainingSeconds,
    errorCode,
  };
}

function taskContinuationSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode) {
    throw Object.assign(new Error("Task continuation resolution failed"), {
      code: errorCode || "TASK_CONTINUATION_FAILED",
    });
  }
  const status = String(result?.status ?? "unknown").slice(0, 80);
  if (!["selected", "ambiguous"].includes(status)) {
    throw Object.assign(new Error("No task continuation candidate was found"), {
      code: "TASK_CONTINUATION_NOT_FOUND",
    });
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    resolutionStatus: status,
    candidateCount: Number(result?.count ?? result?.candidates?.length ?? 0),
    taskId: result?.task?.taskId ?? null,
    taskStatus: result?.task?.status ?? null,
    continuationState: result?.continuation?.state ?? null,
    executionMode: result?.continuation?.executionMode ?? null,
    allowNewOperation: result?.continuation?.allowNewOperation ?? null,
    operationStatus: result?.snapshot?.summary?.operation?.status ?? null,
    interactionState: result?.snapshot?.summary?.interaction?.state ?? null,
    errorCode: null,
  };
}

function crossEndpointContextSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
  expectedText,
}) {
  if (payload?.error || errorCode) {
    throw Object.assign(new Error("Cross-endpoint context read failed"), {
      code: errorCode || "CROSS_ENDPOINT_CONTEXT_READ_FAILED",
    });
  }
  const entries = Array.isArray(result?.entries) ? result.entries : [];
  const combinedText = entries.map((entry) => String(entry?.text ?? "")).join("\n");
  if (expectedText && !combinedText.includes(expectedText)) {
    throw Object.assign(new Error("Expected cross-endpoint context was not found"), {
      code: "CROSS_ENDPOINT_CONTEXT_EXPECTED_TEXT_MISSING",
    });
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    entryCount: entries.length,
    expectedTextMatched: expectedText ? true : null,
    roles: [...new Set(entries.map((entry) => String(entry?.role ?? "")))].filter(Boolean),
    sourceClientTypes: [
      ...new Set(entries.map((entry) => String(entry?.source?.clientType ?? ""))),
    ].filter(Boolean),
    newestSequence: entries.length
      ? Number(entries.at(-1)?.sequence ?? 0) || null
      : null,
    errorCode: null,
  };
}

function yuqueDocumentSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode) {
    throw Object.assign(new Error("Yuque document read failed"), {
      code: errorCode || "YUQUE_DOCUMENT_READ_FAILED",
    });
  }
  const content = String(result?.content ?? "");
  const structure = result?.structure ?? {};
  const sheets = Array.isArray(structure?.sheets) ? structure.sheets : [];
  const tables = Array.isArray(structure?.tables) ? structure.tables : [];
  const images = Array.isArray(structure?.images) ? structure.images : [];
  const attachments = Array.isArray(structure?.attachments)
    ? structure.attachments
    : [];
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    title: String(result?.document?.title ?? "").slice(0, 300),
    book: String(result?.document?.book?.name ?? "").slice(0, 300),
    contentCharacters: content.length,
    contentFormat: String(result?.contentFormat ?? "").slice(0, 100),
    structureKind: String(structure?.kind ?? "").slice(0, 100),
    sheetCount: sheets.length,
    tableCount: tables.length,
    imageCount: images.length,
    attachmentCount: attachments.length,
    rowsReturned: sheets.reduce(
      (total, sheet) => total + Number(sheet?.returnedRows ?? 0),
      0,
    ),
    hasMoreRows: sheets.some((sheet) => Boolean(sheet?.hasMore)),
    truncated: Boolean(result?.truncated),
    redactionApplied: Boolean(result?.redaction?.applied),
    redactionCount: Number(result?.redaction?.count ?? 0),
    errorCode: null,
  };
}

function certificateSearchSummary({
  payload,
  result,
  checkName,
  identityLabel,
  errorCode,
}) {
  if (payload?.error || errorCode) {
    throw Object.assign(new Error("Certificate search failed"), {
      code: errorCode || "CERTIFICATE_SEARCH_FAILED",
    });
  }
  const items = Array.isArray(result?.items) ? result.items : [];
  if (!items.length) {
    throw Object.assign(new Error("Certificate search returned no matches"), {
      code: "CERTIFICATE_SEARCH_EMPTY",
    });
  }
  for (const item of items) {
    if (
      "_download_reference" in item ||
      "resource_id" in item ||
      "source_id" in item
    ) {
      throw Object.assign(new Error("Certificate search leaked OA identifiers"), {
        code: "CERTIFICATE_SEARCH_IDENTIFIER_LEAK",
      });
    }
    if (!String(item?.download_url ?? "").startsWith("https://")) {
      throw Object.assign(new Error("Certificate search download URL is invalid"), {
        code: "CERTIFICATE_SEARCH_URL_INVALID",
      });
    }
  }
  return {
    status: "succeeded",
    check: checkName,
    identityLabel,
    matchCount: items.length,
    exactMatches: items.filter((item) => item?.match_kind === "exact").length,
    matchedQueries: Array.isArray(result?.matched_queries)
      ? result.matched_queries.map(String)
      : [],
    unmatchedQueries: Array.isArray(result?.unmatched_queries)
      ? result.unmatched_queries.map(String)
      : [],
    titles: items.map((item) => String(item?.title ?? "")).filter(Boolean),
    firstDownloadUrl: items[0].download_url,
    expiresAt: items[0].download_expires_at ?? null,
    errorCode: null,
  };
}
