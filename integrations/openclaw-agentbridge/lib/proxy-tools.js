import {
  AGENTBRIDGE_TOOL_CATALOG,
  AGENTBRIDGE_USER_TURN_SOURCE_TOOLS,
} from "./tool-catalog.js";
import { extractToolPayload } from "./mcp-client.js";
import { skillBindingMeta, rememberSkillBinding, skillRunKey, draftSampleGuard } from "./business-skills.js";
import {
  HOST_CONTEXT_META_KEY,
  TASK_CONTEXT_META_KEY,
  hostContextMeta,
} from "./host-contract.js";

import { transportRecoveryStrategy } from "./recovery-policy.js";

export { HOST_CONTEXT_META_KEY, TASK_CONTEXT_META_KEY, hostContextMeta };

export const IDENTITY_STATUS_TOOL_NAME = "agentbridge_identity_status";
export const AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES = Object.freeze([
  "agentbridge_task_plan_prepare",
  "agentbridge_task_plan_cancel",
  "agentbridge_task_cancel",
  "oa_efficiency_data_approval_prepare",
  "oa_travel_expense_approval_prepare",
  "oa_business_trip_approval_prepare",
  "oa_labor_contract_renewal_approval_prepare",
  "oa_intellectual_property_declaration_approval_prepare",
  "oa_overtime_approval_prepare",
  "oa_leave_approval_prepare",
  "oa_resignation_approval_prepare",
  "oa_work_handover_approval_prepare",
  "oa_flight_application_approval_prepare",
  "oa_attendance_confirmation_prepare",
  "oa_weekly_report_acknowledgement_prepare",
  "oa_standard_collaboration_approval_prepare",
  "oa_workflow_revoke_prepare",
  "oa_business_trip_prepare",
  "oa_business_trip_submit_prepare",
  "oa_leave_prepare",
  "oa_leave_submit_prepare",
  "oa_missed_punch_prepare",
  "oa_missed_punch_approval_prepare",
  "oa_missed_punch_approval_batch_prepare",
  "oa_workflow_pending_batch_prepare",
  "oa_meeting_room_application_prepare",
  "oa_meeting_room_application_cancel_prepare",
  "oa_meeting_create_prepare",
  "yuque_session_login",
  "taihua_work_log_create_prepare",
  "taihua_session_login",
  "smartlight_alarm_remark_update_prepare",
  "smartlight_alarm_work_area_submit_prepare",
  "smartlight_alarm_work_area_revoke_prepare",
  "smartlight_rtu_alarm_dispose_prepare",
  "smartlight_session_login",
  "oa_session_login",
]);
const AGENTBRIDGE_GOVERNED_ENTRY_TOOLS = new Set(
  AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES,
);
export const AGENTBRIDGE_INDEPENDENT_TASK_ENTRY_TOOL_NAMES = Object.freeze(
  AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES.filter(
    (name) => name.endsWith("_prepare") && name !== "agentbridge_task_plan_prepare",
  ),
);
const AGENTBRIDGE_INDEPENDENT_TASK_ENTRY_TOOLS = new Set(
  AGENTBRIDGE_INDEPENDENT_TASK_ENTRY_TOOL_NAMES,
);
const TASK_FINALIZATION_TIMEOUT_MS = 3_000;
const INDEPENDENT_READ_TASK_TOOLS = new Set([
  ...AGENTBRIDGE_TOOL_CATALOG.filter(
    (tool) => isTaskEligibleTool(tool.name) &&
      tool.annotations?.readOnlyHint === true &&
      !AGENTBRIDGE_USER_TURN_SOURCE_TOOLS.includes(tool.name),
  ).map((tool) => tool.name),
]);
const SAFE_MCP_RETRY_DELAYS_MS = Object.freeze([500, 2_000]);
const UNREFERENCED_FAILURE_STATUSES = new Set([
  "canceled",
  "deferred",
  "error",
  "expired",
  "failed",
  "not_found",
  "outcome_unknown",
  "pending",
  "processing",
  "rejected",
  "requires_user_action",
  "running",
  "unknown",
  "waiting_user",
]);
const AGENTBRIDGE_AGENT_FACING_TOOL_CATALOG = Object.freeze(
  AGENTBRIDGE_TOOL_CATALOG.filter(
    (descriptor) =>
      descriptor.name === "agentbridge_skill_authoring" || descriptor.annotations?.readOnlyHint === true ||
      AGENTBRIDGE_GOVERNED_ENTRY_TOOLS.has(descriptor.name),
  ),
);
export const AGENTBRIDGE_AGENT_FACING_TOOL_NAMES = Object.freeze([
  IDENTITY_STATUS_TOOL_NAME,
  ...AGENTBRIDGE_AGENT_FACING_TOOL_CATALOG.map((tool) => tool.name),
]);
export const AGENTBRIDGE_PROXY_TOOL_NAMES = AGENTBRIDGE_AGENT_FACING_TOOL_NAMES;

export function isIndependentTaskEntryTool(name) {
  return AGENTBRIDGE_INDEPENDENT_TASK_ENTRY_TOOLS.has(String(name || ""));
}

export function createAgentBridgeProxyTools({
  context,
  identityRouter,
  serverName,
  taskIdResolver = null,
  taskIdBinder = null,
  taskRunRefResolver = null,
  taskScopeResolver = null,
  taskContinuationResolver = null,
  interactionGetGuard = null,
  terminalPlanGuard = null,
  argumentNormalizer = null,
  trustedResultHandler = null,
  logger = null,
}) {
  const identity = identityRouter.resolveToolContext(context);
  const workspaceSession = isWorkspaceSession(context.sessionKey);
  const resolveIdentity = async (signal) => {
    if (identity.bound) {
      return identity;
    }
    if (!workspaceSession) {
      return identity;
    }
    return identityRouter.resolveWorkspaceSession(
      context.sessionKey,
      { signal },
    );
  };
  const statusTool = createIdentityStatusTool(resolveIdentity);
  if (!identity.bound && !workspaceSession) {
    return [statusTool];
  }
  const allowedToolNames = identity.bound
    ? identityRouter.allowedToolNamesForBinding?.(identity.binding)
    : null;
  const visibleCatalog = allowedToolNames
    ? AGENTBRIDGE_AGENT_FACING_TOOL_CATALOG.filter((descriptor) =>
        allowedToolNames.has(descriptor.name),
      )
    : AGENTBRIDGE_AGENT_FACING_TOOL_CATALOG;
  return [
    statusTool,
    ...visibleCatalog.map((descriptor) =>
      createProxyTool({
        descriptor,
        resolveIdentity,
        identityRouter,
        context,
        serverName,
        taskIdResolver,
        taskIdBinder,
        taskRunRefResolver,
        taskScopeResolver,
        taskContinuationResolver,
        interactionGetGuard,
        terminalPlanGuard,
        argumentNormalizer,
        trustedResultHandler,
        logger,
      }),
    ),
  ];
}

function isWorkspaceSession(sessionKey) {
  return (
    typeof sessionKey === "string" &&
    /^agent:[^:]+:agentbridge-workspace:direct:/i.test(sessionKey.trim())
  );
}

function createIdentityStatusTool(resolveIdentity) {
  return {
    name: IDENTITY_STATUS_TOOL_NAME,
    label: "AgentBridge Identity Status",
    description:
      "Check whether this private conversation has a provisioned AgentBridge identity. " +
      "Use this when OA tools are unavailable; never ask the user for an MCP token.",
    parameters: {
      type: "object",
      properties: {},
      additionalProperties: false,
    },
    execute: async (_toolCallId, _rawParams, signal) => {
      const identity = await resolveIdentity(signal);
      return jsonToolResult({
        status: identity.bound ? "bound" : "unbound",
        identityLabel: identity.binding?.label || null,
        reason: identity.reason,
        nextAction: identity.bound
          ? null
          : "Ask the AgentBridge administrator to provision this client identity.",
      });
    },
  };
}

function createProxyTool({
  descriptor,
  resolveIdentity,
  identityRouter,
  context,
  serverName,
  taskIdResolver,
  taskIdBinder,
  taskRunRefResolver,
  taskScopeResolver,
  taskContinuationResolver,
  interactionGetGuard,
  terminalPlanGuard,
  argumentNormalizer,
  trustedResultHandler,
  logger,
}) {
  return {
    name: descriptor.name,
    label: descriptor.title || descriptor.name,
    description: descriptor.description || descriptor.name,
    parameters: descriptor.inputSchema || emptyObjectSchema(),
    ...(descriptor.annotations ? { annotations: descriptor.annotations } : {}),
    execute: async (toolCallId, rawParams, signal) => {
      const identity = await resolveIdentity(signal);
      if (!identity.bound) {
        return jsonToolResult({
          status: "unbound",
          reason: identity.reason,
          error: {
            code: "IDENTITY_NOT_PROVISIONED",
            message:
              "This AgentBridge client identity is not provisioned.",
          },
        });
      }
      const normalizedParams = normalizeParams(rawParams);
      const skillRun = taskRunRefResolver?.(toolCallId, context.sessionKey, "agentbridge_skill_get");
      const skillContext = { ...context, runId: (skillRun && skillRun !== toolCallId ? skillRun : null) || context.runId };
      const sampleBlocked = draftSampleGuard(identityRouter, skillContext, identity, descriptor.name, normalizedParams);
      if (sampleBlocked) return jsonToolResult(sampleBlocked);
      if (descriptor.name === "agentbridge_skill_get" && !skillRunKey(skillContext, identity)) {
        return jsonToolResult({ status: "rejected", error: { code: "SKILL_RUN_CONTEXT_REQUIRED", message: "当前宿主缺少独立回合上下文，无法安全加载业务助手。" } });
      }
      if (isTaskEligibleTool(descriptor.name)) {
        const stopped = terminalPlanGuard?.({ sessionKey: context.sessionKey, runId: context.runId, toolCallId, toolName: descriptor.name });
        if (stopped) return jsonToolResult(stopped);
      }
      const params =
        argumentNormalizer?.({
          sessionKey: context.sessionKey,
          toolName: descriptor.name,
          params: normalizedParams,
        }) || normalizedParams;
      const callParams = paramsWithStableIdempotencyKey({
        descriptor,
        params,
        toolCallId,
        runId: context.runId,
      });
      if (descriptor.name === "agentbridge_interaction_get") {
        const guarded = interactionGetGuard?.({
          sessionKey: context.sessionKey,
          runId: context.runId,
          toolCallId,
          interactionId: params.interaction_id,
        });
        if (guarded) {
          return {
            ...jsonToolResult(guarded),
            details: {
              mcpServer: serverName,
              mcpTool: descriptor.name,
              structuredContent: guarded,
            },
          };
        }
      }
      const continuation = taskContinuationResolver?.(context.sessionKey);
      if (descriptor.name === "agentbridge_task_cancel" && continuation?.taskId &&
          params.task_id !== continuation.taskId) {
        return jsonToolResult({ status: "rejected", taskId: continuation.taskId,
          error: { code: "TASK_CANCEL_TARGET_MISMATCH",
            message: "The requested cancellation does not match the task selected for this turn. Cancel only the selected task; do not claim success or use a historical task ID." } });
      }
      if (descriptor.name === "agentbridge_task_plan_cancel" && continuation?.taskId) {
        const response = await identity.client.callTool("agentbridge_task_plan_get",
          { plan_id: params.plan_id }, { signal, meta: hostContextMeta() });
        const plan = (response?.result || response)?.plan;
        if (plan?.planId !== params.plan_id || plan?.taskId !== continuation.taskId) {
          return jsonToolResult({ status: "rejected", taskId: continuation.taskId,
            error: { code: "TASK_CANCEL_TARGET_MISMATCH",
              message: "The plan does not belong to the selected task. Nothing was canceled; use the selected task ID with agentbridge_task_cancel." } });
        }
      }
      if (
        continuation &&
        continuation.allowNewOperation !== true &&
        isTaskEligibleTool(descriptor.name)
      ) {
        return jsonToolResult({
          status: "continuation_blocked",
          taskId: continuation.taskId,
          taskStatus: continuation.taskStatus,
          executionMode: continuation.executionMode,
          error: {
            code: "TASK_CONTINUATION_OBSERVE_ONLY",
            message:
              "The selected task is waiting, running, or terminal. Use the supplied task snapshot and existing trusted interaction; do not start another business operation.",
          },
        });
      }
      const resolution = await resolveTaskId({
        descriptor,
        identity,
        identityRouter,
        context,
        toolCallId,
        taskIdResolver,
        taskIdBinder,
        taskRunRefResolver,
        taskScopeResolver,
        logger,
      });
      if (resolution?.planningControl) {
        return {
          ...jsonToolResult(resolution.planningControl),
          details: { mcpServer: serverName, mcpTool: descriptor.name,
            agentbridgeTaskId: resolution.taskId,
            structuredContent: resolution.planningControl },
        };
      }
      const taskId = resolution?.taskId || null;
      const coordinatorLease = taskId
        ? await renewCoordinatorLease({
            client: identity.client,
            taskId,
            logger,
            signal,
          })
        : null;
      let result;
      const transportRecovery = {};
      const retry = safeTransportRetryPolicy({
        descriptor,
        logger,
        transportRecovery,
      });
      try {
        result = await identity.client.callToolResult(
          descriptor.name,
          callParams,
          {
            signal,
            meta: {
              ...hostContextMeta(),
              ...skillBindingMeta(identityRouter, skillContext, identity),
              ...(taskId
                ? {
                  [TASK_CONTEXT_META_KEY]: {
                    taskId,
                    hostRunId: boundedText(toolCallId, 256),
                    toolCallId: boundedText(toolCallId, 256),
                    ...(coordinatorLease?.version
                      ? {
                          coordinatorLeaseVersion: String(
                            coordinatorLease.version,
                          ),
                        }
                      : {}),
                  },
                }
                : {}),
            },
            ...(retry ? { retry } : {}),
          },
        );
      } catch (error) {
        if (taskId) {
          await finishHostTask({
            client: identity.client,
            taskId,
            outcome: {
              status: "failed",
              errorCode: safeErrorCode(error),
              message: safeErrorMessage(error),
            },
            logger,
            causationRef: boundedText(toolCallId, 256),
          });
        }
        throw error;
      }
      if (descriptor.name === "agentbridge_skill_get") {
        rememberSkillBinding(identityRouter, skillContext, identity, extractToolPayload(result));
      }
      if (["agentbridge_task_cancel", "agentbridge_task_plan_cancel"].includes(descriptor.name)) {
        const payload = extractToolPayload(result);
        if (!result.isError && !payload?.error && !["failed", "rejected", "not_found"].includes(payload?.status)) {
          const target = payload.plan || payload.task;
          const idMatches = descriptor.name === "agentbridge_task_cancel"
            ? target?.taskId === params.task_id
            : target?.planId === params.plan_id;
          const selectionMatches = !continuation?.taskId || target?.taskId === continuation.taskId;
          const cancellationPending = descriptor.name === "agentbridge_task_cancel"
            && payload.status === "running" && payload.cancellation_pending === true
            && ["active", "running"].includes(target?.status);
          if (!idMatches || !selectionMatches || (!cancellationPending && (target?.state || target?.status) !== "canceled")) {
            return jsonToolResult({ status: "unconfirmed", requestedTaskId: params.task_id || null,
              requestedPlanId: params.plan_id || null,
              error: { code: "TASK_CANCEL_RESULT_UNCONFIRMED",
                message: "Cancellation response did not confirm the exact selected target in canceled state. Do not report success or retry automatically; inspect the authoritative task state." } });
          }
        }
      }
      if (taskId) {
        const hasReferences = descriptor.name === "agentbridge_task_plan_prepare"
          ? true
          : await observeTaskResult({
              client: identity.client,
              taskId,
              result,
              logger,
              signal,
            });
        if (!hasReferences) {
          await finishHostTask({
            client: identity.client,
            taskId,
            outcome: taskOutcomeForUnreferencedResult(result),
            logger,
            causationRef: boundedText(toolCallId, 256),
          });
        }
      }
      const nativeResult = {
        ...result,
        details: {
          mcpServer: serverName,
          mcpTool: descriptor.name,
          ...(taskId ? { agentbridgeTaskId: taskId } : {}),
          ...(transportRecovery.recovered
            ? {
                agentbridgeTransportRecovery: {
                  attempts: transportRecovery.attempts,
                  transportCode: transportRecovery.transportCode,
                },
              }
            : {}),
        },
      };
      // The embedded host forwards only content/details to result middleware.
      return trustedResultHandler
        ? await trustedResultHandler(
            { toolCallId, toolName: descriptor.name, params: callParams, result: nativeResult },
            context,
          )
        : nativeResult;
    },
  };
}

async function renewCoordinatorLease({
  client,
  taskId,
  logger,
  signal,
}) {
  try {
    const response = await client.callTool(
      "agentbridge_host_coordinator_lease_acquire",
      {
        task_id: taskId,
        lease_seconds: 600,
        takeover: false,
        expected_version: null,
      },
      { signal, meta: hostContextMeta() },
    );
    const lease = response?.coordinatorLease;
    if (!lease || lease.hostInstanceId !== "openclaw-gateway") {
      const error = new Error(
        "AgentBridge did not grant the OpenClaw task coordinator lease",
      );
      error.code = "HOST_COORDINATOR_LEASE_CONFLICT";
      throw error;
    }
    return lease;
  } catch (error) {
    logger?.warn?.(
      `AgentBridge task coordinator lease unavailable (${safeErrorCode(error)})`,
    );
    throw error;
  }
}

function paramsWithStableIdempotencyKey({
  descriptor,
  params,
  toolCallId,
  runId,
}) {
  if (
    !AGENTBRIDGE_GOVERNED_ENTRY_TOOLS.has(descriptor.name) ||
    !Object.hasOwn(descriptor.inputSchema?.properties || {}, "idempotency_key") ||
    boundedText(params.idempotency_key, 256)
  ) {
    return params;
  }
  const callRef =
    boundedText(toolCallId, 240) || boundedText(runId, 240);
  if (!callRef) {
    return params;
  }
  return {
    ...params,
    idempotency_key: boundedText(`openclaw:${callRef}`, 256),
  };
}

function safeTransportRetryPolicy({
  descriptor,
  logger,
  transportRecovery,
}) {
  if (!supportsSafeTransportRetry(descriptor)) {
    return null;
  }
  return {
    delaysMs: SAFE_MCP_RETRY_DELAYS_MS,
    onRetry({ attempt, nextAttempt, delayMs, error }) {
      const transportCode = safeTransportCode(error);
      logger?.warn?.(
        `AgentBridge MCP transport retry tool=${descriptor.name} ` +
          `attempt=${attempt} nextAttempt=${nextAttempt} ` +
          `delayMs=${delayMs} cause=${transportCode}`,
      );
    },
    onRecovered({ attempts, lastError }) {
      const transportCode = safeTransportCode(lastError);
      transportRecovery.recovered = true;
      transportRecovery.attempts = attempts;
      transportRecovery.transportCode = transportCode;
      logger?.info?.(
        `AgentBridge MCP transport recovered tool=${descriptor.name} ` +
          `attempts=${attempts} cause=${transportCode}`,
      );
    },
  };
}

function supportsSafeTransportRetry(descriptor) {
  const read = descriptor.annotations?.readOnlyHint === true;
  const callClass = read ? "read" :
    AGENTBRIDGE_GOVERNED_ENTRY_TOOLS.has(descriptor.name) ? "prepare" : "unsafe";
  return transportRecoveryStrategy(callClass).startsWith("bounded_retry") &&
    descriptor.annotations?.idempotentHint === true &&
    (read || descriptor.annotations?.destructiveHint !== true);
}

function safeTransportCode(error) {
  const value =
    error?.transportCode ||
    error?.cause?.code ||
    error?.code ||
    error?.name ||
    "UNKNOWN_TRANSPORT_ERROR";
  return String(value)
    .toUpperCase()
    .replace(/[^A-Z0-9_.-]/g, "_")
    .slice(0, 80);
}

async function resolveTaskId({
  descriptor,
  identity,
  identityRouter,
  context,
  toolCallId,
  taskIdResolver,
  taskIdBinder,
  taskRunRefResolver,
  taskScopeResolver,
  logger,
}) {
  if (!isTaskEligibleTool(descriptor.name)) {
    return null;
  }
  const sessionKey = boundedText(context.sessionKey, 1024);
  if (!sessionKey) {
    return null;
  }
  // Atomic reads can follow a completed task, including pagination and details.
  // Keep each call separate while preserving the shared planning turn below.
  const independentReadCall = INDEPENDENT_READ_TASK_TOOLS.has(descriptor.name);
  const resumedTaskId = boundedText(
    taskIdResolver?.(sessionKey, descriptor.name),
    128,
  );
  if (resumedTaskId) {
    return { taskId: resumedTaskId };
  }
  const independentTaskEntry = isIndependentTaskEntryTool(descriptor.name);
  const sharedTurnRef = boundedText(
        taskRunRefResolver?.(toolCallId, sessionKey, independentTaskEntry ? null : descriptor.name),
        256,
      );
  const runRef = independentReadCall ? boundedText(toolCallId, 256) :
    (!independentTaskEntry && sharedTurnRef) ||
    boundedText(context.runId, 256) ||
    boundedText(toolCallId, 256);
  if (!runRef) {
    return null;
  }
  const binding = identity.binding;
  const workspaceSession = isWorkspaceSession(sessionKey);
  const endpointKey = identityRouter.endpointKeyForSession(sessionKey);
  if (!endpointKey) {
    logger?.warn?.(
      "AgentBridge task creation skipped because the session endpoint is not bound",
    );
    return null;
  }
  const delivery =
    context.deliveryContext &&
    typeof context.deliveryContext === "object" &&
    !Array.isArray(context.deliveryContext)
      ? context.deliveryContext
      : {};
  try {
    const response = await identity.client.callTool(
      "agentbridge_host_task_ensure",
      {
        agent_host: "openclaw",
        host_task_key: boundedText(`${sessionKey}|${runRef}`, 1024),
        tool_name: descriptor.name,
        planning_task_key: boundedText(`${sessionKey}|${sharedTurnRef || runRef}`, 1024),
        endpoint_key: endpointKey,
        client_type: workspaceSession ? "web" : binding.channel,
        external_subject: workspaceSession
          ? workspaceSubject(endpointKey)
          : binding.senderId,
        conversation_ref: sessionKey,
        title: boundedText(descriptor.title || descriptor.name, 240),
        account_id: workspaceSession
          ? workspaceSubject(endpointKey)
          : binding.accountId,
        label: workspaceSession ? "Agent Workspace" : binding.label,
        route: workspaceSession
          ? {}
          : {
              channel: binding.channel,
              to: boundedText(delivery.to, 768) || binding.senderId,
              accountId:
                boundedText(delivery.accountId, 512) || binding.accountId,
              threadId: boundedText(delivery.threadId, 512),
            },
        capabilities: workspaceSession
          ? [
              "workspace.chat",
              "workspace.task.read",
              "workspace.interaction.open",
            ]
          : ["direct_status", "trusted_interaction"],
        task_scope: workspaceSession
          ? independentReadCall || independentTaskEntry ||
            taskScopeResolver?.(sessionKey, descriptor.name) === "independent"
            ? "independent"
            : "user_turn"
          : "host_run",
      },
      { meta: hostContextMeta() },
    );
    if (response?.error?.code === "PLAN_REQUIRED") {
      return { taskId: response.taskId || null, planningControl: response };
    }
    const taskId = boundedText(response?.task?.taskId, 128);
    if (!taskId) {
      logger?.warn?.(
        "AgentBridge task creation returned no task ID; business call stopped",
      );
      return taskContextUnavailable();
    } else {
      if (!independentReadCall) taskIdBinder?.(sessionKey, descriptor.name, taskId);
    }
    return { taskId };
  } catch (error) {
    logger?.warn?.(
      `AgentBridge task creation unavailable; business call stopped (${safeErrorCode(error)})`,
    );
    return taskContextUnavailable();
  }
}

function taskContextUnavailable() {
  return { taskId: null, planningControl: { status: "failed", error: {
    code: "TASK_CONTEXT_UNAVAILABLE",
    message: "任务上下文暂不可用，本次请求未进入业务系统。请稍后重试。",
  } } };
}

function workspaceSubject(endpointKey) {
  return boundedText(String(endpointKey).slice("workspace:".length), 768);
}

async function observeTaskResult({ client, taskId, result, logger, signal }) {
  const references = collectTaskReferences(extractToolPayload(result));
  if (
    references.operationIds.length === 0 &&
    references.interactionIds.length === 0
  ) {
    return false;
  }
  try {
    await client.callTool(
      "agentbridge_host_task_observe",
      {
        agent_host: "openclaw",
        task_id: taskId,
        operation_ids: references.operationIds,
        interaction_ids: references.interactionIds,
      },
      { signal, meta: hostContextMeta() },
    );
  } catch (error) {
    logger?.warn?.(
      `AgentBridge task observation unavailable; business result preserved (${safeErrorCode(error)})`,
    );
  }
  return true;
}

async function finishHostTask({
  client,
  taskId,
  outcome,
  logger,
  causationRef,
}) {
  try {
    await client.callTool(
      "agentbridge_host_task_finish",
      {
        agent_host: "openclaw",
        task_id: taskId,
        outcome: outcome.status,
        reason: outcome.reason || null,
        error_code: outcome.errorCode || null,
        message: outcome.message || null,
        causation_ref: causationRef || null,
      },
      {
        signal: AbortSignal.timeout(TASK_FINALIZATION_TIMEOUT_MS),
        meta: hostContextMeta(),
      },
    );
  } catch (error) {
    logger?.warn?.(
      `AgentBridge task finalization unavailable; stale-task reconciliation will retry (${safeErrorCode(error)})`,
    );
  }
}

export function taskOutcomeForUnreferencedResult(result) {
  const payload = extractToolPayload(result);
  const status = boundedText(payload?.status, 80)?.toLowerCase() || "";
  const error =
    payload?.error && typeof payload.error === "object"
      ? payload.error
      : null;
  const errorCode = boundedText(
    error?.code || payload?.errorCode || (UNREFERENCED_FAILURE_STATUSES.has(status) ? payload?.code : null),
    120,
  );
  const errorMessage = boundedText(
    error?.message || payload?.errorMessage || (UNREFERENCED_FAILURE_STATUSES.has(status) ? payload?.message : null),
    500,
  );
  if (
    result?.isError === true ||
    errorCode ||
    UNREFERENCED_FAILURE_STATUSES.has(status)
  ) {
    return {
      status: "failed",
      errorCode: errorCode || "HOST_RESULT_MISSING_REFERENCE",
      message:
        errorMessage ||
        "The tool did not produce an operation or trusted-interaction reference.",
    };
  }
  return {
    status: "succeeded",
    reason: "host_tool_completed_without_follow_up",
  };
}

export function collectTaskReferences(value) {
  const operationIds = new Set();
  const interactionIds = new Set();
  const seen = new Set();
  const visit = (item, depth) => {
    if (
      depth > 12 ||
      !item ||
      typeof item !== "object" ||
      seen.has(item)
    ) {
      return;
    }
    seen.add(item);
    if (Array.isArray(item)) {
      for (const child of item.slice(0, 100)) {
        visit(child, depth + 1);
      }
      return;
    }
    for (const [key, child] of Object.entries(item)) {
      if (
        key === "operationId" &&
        typeof child === "string" &&
        child.trim()
      ) {
        operationIds.add(child.trim().slice(0, 256));
      } else if (
        key === "interactionId" &&
        typeof child === "string" &&
        child.trim()
      ) {
        interactionIds.add(child.trim().slice(0, 256));
      } else {
        visit(child, depth + 1);
      }
      if (operationIds.size + interactionIds.size >= 40) {
        return;
      }
    }
  };
  visit(value, 0);
  return {
    operationIds: [...operationIds].slice(0, 20),
    interactionIds: [...interactionIds].slice(0, 20),
  };
}

function isTaskEligibleTool(name) {
  return (
    typeof name === "string" &&
    (name === "agentbridge_task_plan_prepare" ||
      !name.startsWith("agentbridge_")) &&
    !name.endsWith("_session_status")
  );
}

function normalizeParams(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value
    : {};
}

function emptyObjectSchema() {
  return { type: "object", properties: {}, additionalProperties: false };
}

function boundedText(value, maximum) {
  if (typeof value !== "string" && typeof value !== "number") {
    return null;
  }
  const normalized = String(value).trim();
  return normalized ? normalized.slice(0, maximum) : null;
}

function safeErrorCode(error) {
  const value = error?.code || error?.name || "TASK_HUB_ERROR";
  return String(value)
    .toUpperCase()
    .replace(/[^A-Z0-9_.-]/g, "_")
    .slice(0, 80);
}

function safeErrorMessage(error) {
  return (
    boundedText(error?.message, 500) ||
    "AgentBridge business tool call failed before producing a result."
  );
}

function jsonToolResult(value) {
  return {
    content: [{ type: "text", text: JSON.stringify(value) }],
    details: { structuredContent: value },
  };
}
