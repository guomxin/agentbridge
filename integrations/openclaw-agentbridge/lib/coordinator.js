import { LOGIN_READ_TOOLS, LOGIN_CONTINUATION_TTL_MS,
  normalizeReadContinuation, inferReadContinuation, isFreshContinuation, isLoginRequiredPayload, loginToolForReadTool, formatReadContinuation
} from "./read-continuation.js";
import { randomUUID } from "node:crypto";
import { mkdir, rename, unlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { interactionResumeAllowed, resumeClaimAllowed } from "./recovery-policy.js";

import {
  appendPresentationLinks,
  buildPresentation,
  collectPublicInteractionReferences,
  isInteractionExpired,
  isPrivateSessionKey,
  processToolResult,
} from "./interaction.js";
import {
  collectTaskReferences,
  hostContextMeta,
  isIndependentTaskEntryTool,
  TASK_CONTEXT_META_KEY,
} from "./proxy-tools.js";

const TERMINAL_STATES = new Set([
  "declined",
  "expired",
  "failed",
  "superseded",
]);
const TERMINAL_TASK_EVENTS = new Set([
  "task.completed",
  "task.failed",
  "task.canceled",
]);
const MAX_INTERACTIONS = 100;
const MAX_TERMINAL_TASKS = 1000;
const MAX_POLL_ERRORS = 5;
const MAX_TOOL_BINDINGS = 1000;
const TOOL_BINDING_TTL_MS = 5 * 60 * 1000;
const PLANNING_REPAIR_TTL_MS = 5 * 60 * 1000;
const TERMINAL_TASK_TTL_MS = 30 * 60 * 1000;
const MAX_HYDRATION_REFERENCES = 3;
const QUIET_COMPANION_TASK_EVENTS = new Set([
  "task.created",
  "task.operation.linked",
  "task.operation.running",
  "task.operation.requires_user_action",
  "task.interaction.waiting",
  "task.interaction.completed",
  "task.completed",
  "task.artifact.delivery",
  "plan.proposed",
  "plan.validated",
  "plan.started",
  "plan.step.started",
  "plan.step.succeeded",
  "plan.step.resumed",
  "plan.step.recovered",
  "plan.step.waiting",
  "plan.authorization.waiting",
]);
const PULL_BASED_CHANNELS = new Set(["web", "webchat"]);
const MAX_NOTIFICATION_IDLE_INTERVAL_MS = 10_000;
const PREPARED_DOCUMENT_NATIVE_ATTEMPTS = 2;
const PREPARED_DOCUMENT_RETRY_DELAY_MS = 1_000;
const PREPARED_DOCUMENT_RECEIPT_TTL_MS = 10 * 60 * 1000;
const MAX_PREPARED_DOCUMENT_RECEIPTS = 1_000;
export function createInteractionSharedState() {
  return {
    id:
      globalThis.crypto?.randomUUID?.() ||
      `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    records: new Map(),
    sessionRoutes: new Map(),
    directDeliveries: new Map(),
    loginContinuations: new Map(),
    recentUserMessages: new Map(),
    planningRepairs: new Map(),
    documentDeliveries: new Map(),
    documentDeliveryReceipts: new Map(),
    taskContinuations: new Map(),
    taskContinuationChoices: new Map(),
    independentTaskBindings: new Map(),
    resumeClaims: new Map(),
    terminalTasks: new Map(),
    identitySessionBindings: new Map(),
    identitySessionEndpoints: new Map(),
  };
}

export class InteractionCoordinator {
  constructor({
    api,
    config,
    mcpClient = null,
    mcpClientResolver = null,
    sharedState = createInteractionSharedState(),
    sleep = defaultSleep,
    notificationSleep = backgroundSleep,
    now = Date.now,
    fetchImpl = globalThis.fetch,
    saveMediaBufferImpl = saveOpenClawMediaBuffer,
    interactionPresenter = null,
  }) {
    this.api = api;
    this.config = config;
    this.mcpClient = mcpClient;
    this.mcpClientResolver = mcpClientResolver;
    this.sleep = sleep;
    this.notificationSleep = notificationSleep;
    this.now = now;
    this.fetchImpl = fetchImpl;
    this.saveMediaBufferImpl = saveMediaBufferImpl;
    this.interactionPresenter = interactionPresenter;
    this.sharedStateId = sharedState.id || "isolated";
    this.records = sharedState.records;
    this.polls = new Map();
    this.abortControllers = new Map();
    this.notificationController = null;
    this.notificationPump = null;
    this.toolBindings = new Map();
    this.sessionRoutes = sharedState.sessionRoutes;
    this.directDeliveries = sharedState.directDeliveries;
    this.loginContinuations =
      sharedState.loginContinuations || (sharedState.loginContinuations = new Map());
    this.recentUserMessages =
      sharedState.recentUserMessages || (sharedState.recentUserMessages = new Map());
    this.planningRepairs =
      sharedState.planningRepairs || (sharedState.planningRepairs = new Map());
    this.terminalPlanFailures =
      sharedState.terminalPlanFailures || (sharedState.terminalPlanFailures = new Map());
    this.documentDeliveries =
      sharedState.documentDeliveries || (sharedState.documentDeliveries = new Map());
    this.documentDeliveryReceipts =
      sharedState.documentDeliveryReceipts ||
      (sharedState.documentDeliveryReceipts = new Map());
    this.taskContinuations =
      sharedState.taskContinuations || (sharedState.taskContinuations = new Map());
    this.taskContinuationChoices =
      sharedState.taskContinuationChoices ||
      (sharedState.taskContinuationChoices = new Map());
    this.independentTaskBindings =
      sharedState.independentTaskBindings ||
      (sharedState.independentTaskBindings = new Map());
    this.resumeClaims =
      sharedState.resumeClaims || (sharedState.resumeClaims = new Map());
    this.terminalTasks =
      sharedState.terminalTasks || (sharedState.terminalTasks = new Map());
  }

  recordUserMessage(event, context) {
    const sessionKey = event.sessionKey || context.sessionKey;
    const rawText = extractUserMessageText(event);
    if (!isPrivateSessionKey(sessionKey) || !rawText) {
      return;
    }
    const text = rawText.trim().slice(0, 1000);
    if (!text) {
      return;
    }
    // A continuation applies to one inbound turn. The prompt hook may bind a
    // fresh server-backed selection before this turn reaches any business tool.
    this.taskContinuations.delete(sessionKey);
    this.independentTaskBindings.delete(sessionKey);
    this.planningRepairs.delete(sessionKey);
    this.terminalPlanFailures.delete(sessionKey);
    this.recentUserMessages.set(sessionKey, {
      text,
      intentText: rawText.trim().slice(0, 20_000),
      capturedAt: this.now(),
      taskRunRef: `turn:${randomUUID()}`,
    });
  }

  bindWorkspaceTurn(sessionKey, turnRef, message = null) {
    if (!isPrivateSessionKey(sessionKey)) {
      return false;
    }
    const normalizedTurnRef = safeRoutePart(turnRef);
    if (!normalizedTurnRef) {
      return false;
    }
    this.taskContinuations.delete(sessionKey);
    this.independentTaskBindings.delete(sessionKey);
    this.planningRepairs.delete(sessionKey);
    this.terminalPlanFailures.delete(sessionKey);
    const normalizedMessage = safeMessageText(message, 1000);
    this.recentUserMessages.set(sessionKey, {
      text: normalizedMessage || null,
      intentText: safeMessageText(message, 20_000) || null,
      capturedAt: this.now(),
      taskRunRef: `workspace:${normalizedTurnRef}`,
    });
    return true;
  }

  currentUserMessageForSession(sessionKey) {
    this.prune();
    const record = this.recentUserMessages.get(sessionKey);
    return record?.intentText || record?.text || null;
  }

  normalizeBusinessToolArguments({ sessionKey, toolName, params }) {
    if (toolName !== "smartlight_alarm_list") {
      return params;
    }
    const message = this.recentUserMessages.get(sessionKey)?.text;
    return normalizeSmartlightAlarmListArguments(params, message);
  }

  bindDeliveryRoute({ sessionKey, channel, to, accountId, threadId }) {
    if (!isPrivateSessionKey(sessionKey)) {
      return false;
    }
    const normalizedChannel = safeRoutePart(channel);
    const normalizedTo = safeRoutePart(to);
    if (!normalizedChannel || !normalizedTo) {
      return false;
    }
    this.sessionRoutes.set(sessionKey, {
      channel: normalizedChannel,
      to: normalizedTo,
      accountId: safeRoutePart(accountId) || null,
      threadId: normalizeThreadId(threadId),
    });
    return true;
  }

  deliveryChannelForSession(sessionKey) {
    return this.sessionRoutes.get(sessionKey)?.channel || null;
  }

  isPullBasedSession(sessionKey) {
    const channel = String(
      this.sessionRoutes.get(sessionKey)?.channel || "",
    ).toLowerCase();
    return PULL_BASED_CHANNELS.has(channel);
  }

  redundantInteractionGet({ sessionKey, runId, toolCallId, interactionId }) {
    this.prune();
    const normalizedInteractionId = safeRoutePart(interactionId);
    const normalizedToolCallId = normalizeToolCallId(toolCallId);
    const binding = normalizedToolCallId
      ? this.toolBindings.get(normalizedToolCallId)
      : null;
    const activeRunId = safeRoutePart(runId || binding?.runId);
    const record = normalizedInteractionId
      ? this.records.get(normalizedInteractionId)
      : null;
    if (
      !record ||
      !isPrivateSessionKey(sessionKey) ||
      record.sessionKey !== sessionKey ||
      !activeRunId ||
      record.runId !== activeRunId ||
      !["pending", "processing"].includes(record.interaction.state)
    ) {
      return null;
    }
    this.api.logger.info(
      "AgentBridge suppressed a redundant same-run interaction_get; the host already has the card",
    );
    return {
      status: "host_handled",
      interactionId: normalizedInteractionId,
      interactionState: record.interaction.state,
      message:
        "The host already has this trusted interaction. Do not fetch it again in this run; briefly ask the user to use the displayed card.",
    };
  }

  deliverySessionKeyForRoute({ channel, to, accountId }) {
    const normalizedChannel = safeRoutePart(channel);
    const normalizedTo = safeRoutePart(to);
    const normalizedAccountId = safeRoutePart(accountId);
    if (!normalizedChannel || !normalizedTo) {
      return null;
    }
    const matches = [...this.sessionRoutes.entries()].filter(
      ([, route]) =>
        route.channel === normalizedChannel &&
        route.to === normalizedTo &&
        (!normalizedAccountId ||
          !route.accountId ||
          route.accountId === normalizedAccountId),
    );
    return matches.length === 1 ? matches[0][0] : null;
  }

  bindToolCall(event, context) {
    const toolCallId = normalizeToolCallId(event.toolCallId);
    if (!toolCallId) {
      return;
    }
    this.toolBindings.set(toolCallId, {
      sessionKey: context.sessionKey || null,
      runId: event.runId || context.runId || null,
      capturedAt: this.now(),
      readContinuation: normalizeReadContinuation(
        event.toolName,
        event.params,
        this.now(),
      ),
    });
    this.pruneToolBindings();
  }

  taskRunRefForToolCall(toolCallId, sessionKey, toolName = null) {
    this.prunePlanningRepairs();
    const normalized = normalizeToolCallId(toolCallId);
    const binding = normalized ? this.toolBindings.get(normalized) : null;
    // Deferred native tools may execute without a before_tool_call binding.
    // Workspace's server-validated turn (or the channel's inbound message)
    // still supplies the shared user-turn identity; never use a tool-call ID.
    if (toolName === "agentbridge_skill_get") {
      if (binding?.sessionKey && binding.sessionKey !== sessionKey) return null;
      return this.recentUserMessages.get(sessionKey)?.taskRunRef || binding?.runId || null;
    }
    if (!binding || (binding.sessionKey && binding.sessionKey !== sessionKey)) {
      return normalized;
    }
    const recent = this.recentUserMessages.get(sessionKey);
    const planningRepair = this.planningRepairs.get(sessionKey);
    if (
      toolName === "agentbridge_task_plan_prepare" &&
      planningRepair?.taskRunRef
    ) {
      return planningRepair.taskRunRef;
    }
    if (
      !isIndependentTaskEntryTool(toolName) &&
      recent?.taskRunRef
    ) {
      return recent.taskRunRef;
    }
    return binding.runId || normalized;
  }

  taskScopeForBusinessCall(sessionKey, toolName) {
    this.prunePlanningRepairs();
    return toolName === "agentbridge_task_plan_prepare" &&
      this.planningRepairs.has(sessionKey) ? "independent" : null;
  }

  deliverPreparedDocumentResult(event, context) {
    const payload = trustedAgentBridgeStructuredContent(
      event.result,
      this.config.mcpServerName,
    );
    const files = normalizePreparedDocuments(
      payload,
      this.config.allowedCardOrigins,
    );
    if (files.length === 0) {
      return null;
    }
    const toolCallId = normalizeToolCallId(event.toolCallId);
    const binding = toolCallId ? this.toolBindings.get(toolCallId) : null;
    const sessionKey = binding?.sessionKey || context.sessionKey;
    const taskId = taskIdFromToolResult(event.result);
    const deliveryRef = `tool-result:${toolCallId || randomUUID()}`;
    if (!isPrivateSessionKey(sessionKey)) {
      this.api.logger.warn(
        "AgentBridge prepared document withheld because no private session binding was available",
      );
      return Promise.resolve(
        preparedDocumentDeliveryReport(files, [], {
          state: "failed",
          errorCode: "PRIVATE_SESSION_REQUIRED",
        }),
      );
    }
    const previous = this.documentDeliveries.get(sessionKey) || Promise.resolve();
    const route = this.sessionRoutes.get(sessionKey);
    const endpointMode = this.isPullBasedSession(sessionKey)
      ? "workspace_download"
      : "native_channel";
    const delivery = previous
      .catch(() => undefined)
      .then(async () => {
        const receipts = [];
        for (const file of files) {
          receipts.push(
            await this.deliverPreparedDocumentDirect(sessionKey, file, {
              deliveryRef,
            }),
          );
        }
        const report = preparedDocumentDeliveryReport(files, receipts, {
          channel: route?.channel || "unknown",
          endpointMode,
        });
        await this.reportPreparedDocumentDelivery({
          sessionKey,
          taskId,
          deliveryRef,
          report,
        });
        return report;
      });
    this.documentDeliveries.set(sessionKey, delivery);
    return delivery.finally(() => {
      if (this.documentDeliveries.get(sessionKey) === delivery) {
        this.documentDeliveries.delete(sessionKey);
      }
    });
  }

  async captureNativeToolResult(event, context) {
    const processed = processToolResult(event.result, this.config.allowedCardOrigins);
    if (!processed.sanitized) {
      const payload = trustedAgentBridgeStructuredContent(event.result, this.config.mcpServerName);
      if (isLoginRequiredPayload(payload)) {
        // Native hosts may never invoke the legacy result middleware. Start
        // authentication here while the failed read still owns its task.
        // The native proxy supplies the actual call arguments even if the
        // host also omitted before_tool_call.
        if (!this.toolBindings.has(normalizeToolCallId(event.toolCallId))) {
          this.bindToolCall(event, context);
        }
        const captured = await this.captureToolResult(event, context);
        return captured?.result || event.result;
      }
      return event.result;
    }
    // Retain the binding for middleware handling of any non-card result data.
    const binding = this.toolBindings.get(normalizeToolCallId(event.toolCallId));
    const sessionKey = binding?.sessionKey || context.sessionKey;
    const interactions =
      (await this.presentInteractions(processed.interactions, sessionKey)) ||
      processed.interactions;
    this.captureInteractions(interactions, binding, context, taskIdFromToolResult(event.result));
    return processed.result;
  }

  captureToolResult(event, context) {
    const binding = this.takeToolBinding(event.toolCallId);
    const taskId = taskIdFromToolResult(event.result);
    const processed = processToolResult(
      event.result,
      this.config.allowedCardOrigins,
    );
    if (processed.sanitized) {
      const sessionKey = binding?.sessionKey || context.sessionKey;
      const presented = this.presentInteractions(
        processed.interactions,
        sessionKey,
      );
      if (presented) {
        return presented.then((interactions) => {
          this.captureInteractions(interactions, binding, context, taskId);
          return { result: processed.result };
        });
      }
      this.captureInteractions(processed.interactions, binding, context, taskId);
      return { result: processed.result };
    }

    const publicPayload = trustedAgentBridgeStructuredContent(
      event.result,
      this.config.mcpServerName,
    );
    if (!publicPayload) {
      return undefined;
    }
    const failedPlan = publicPayload.plan || publicPayload.result?.plan;
    if (failedPlan?.state === "failed" && failedPlan.terminalReason === "PLAN_SOURCE_INCOMPLETE") {
      const sessionKey = binding?.sessionKey || context.sessionKey;
      if (isPrivateSessionKey(sessionKey)) {
        this.terminalPlanFailures.set(sessionKey, {
          planId: failedPlan.planId,
          turnRef: this.recentUserMessages.get(sessionKey)?.taskRunRef || null,
          runId: binding?.runId || context.runId || null,
          expiresAt: this.now() + PLANNING_REPAIR_TTL_MS,
        });
      }
    }
    const planningRepair = this.rememberPlanningRepair(
      publicPayload,
      binding,
      context,
      taskId,
    );
    if (planningRepair) {
      return {
        result: planningRepairRequiredResult(event.result, publicPayload),
      };
    }
    this.rememberLoginContinuation(publicPayload, binding, context);
    const loginStart = this.autoStartLoginForRead(
      publicPayload,
      binding,
      context,
      taskId,
      event.result,
    );
    if (loginStart) {
      return loginStart;
    }
    const references = collectPublicInteractionReferences(publicPayload).slice(
      0,
      MAX_HYDRATION_REFERENCES,
    );
    if (references.length === 0) {
      return undefined;
    }
    return this.hydratePublicInteractionReferences(
      references,
      binding,
      context,
      taskId,
    );
  }

  terminalPlanFailureForCall({ sessionKey, runId, toolCallId, toolName }) {
    const failure = this.terminalPlanFailures.get(sessionKey);
    if (!failure) {
      this.prunePlanningRepairs();
      if (this.planningRepairs.has(sessionKey) && toolName !== "agentbridge_task_plan_prepare") {
        return { status: "planning_control", error: { code: "PLAN_REQUIRED",
          message: "本轮已要求使用持久计划，不能改用独立查询或填写卡绕过；请使用计划目录和计划入口继续。" } };
      }
      return null;
    }
    if (failure.expiresAt <= this.now()) {
      this.terminalPlanFailures.delete(sessionKey);
      return null;
    }
    const currentRun = this.toolBindings.get(normalizeToolCallId(toolCallId))?.runId || runId;
    const sameTurn = failure.turnRef
      ? failure.turnRef === this.recentUserMessages.get(sessionKey)?.taskRunRef
      : failure.runId && failure.runId === currentRun;
    if (!sameTurn) return null;
    return {
      status: "failed", planId: failure.planId,
      error: {
        code: "PLAN_SOURCE_INCOMPLETE",
        message: "计划因来源不完整已安全停止，未进入业务写入。本轮不得绕过计划重新调用来源或写入工具，请报告原计划结果。",
      },
      nextAction: { type: "report_plan_failure", doNotRetryAtomicTools: true, businessWriteOccurred: false },
    };
  }

  rememberLoginContinuation(payload, binding, context) {
    if (!isLoginRequiredPayload(payload) || !binding?.readContinuation) {
      return;
    }
    const sessionKey = binding.sessionKey || context.sessionKey;
    if (!isPrivateSessionKey(sessionKey)) {
      return;
    }
    this.loginContinuations.set(sessionKey, binding.readContinuation);
  }

  rememberPlanningRepair(payload, binding, context, sourceTaskId = null) {
    if (payload?.error?.code !== "PLAN_REQUIRED") {
      return false;
    }
    const sessionKey = binding?.sessionKey || context.sessionKey;
    if (!isPrivateSessionKey(sessionKey)) {
      return false;
    }
    const existing = this.planningRepairs.get(sessionKey);
    if (existing && existing.expiresAt > this.now()) {
      return true;
    }
    const recent = this.recentUserMessages.get(sessionKey);
    const baseRef =
      recent?.taskRunRef ||
      safeRoutePart(binding?.runId) ||
      `repair:${randomUUID()}`;
    this.planningRepairs.set(sessionKey, {
      taskRunRef: `${baseRef}:plan-repair`.slice(0, 256),
      sourceTaskId: safeRoutePart(sourceTaskId),
      capturedAt: this.now(),
      expiresAt: this.now() + PLANNING_REPAIR_TTL_MS,
    });
    this.api.logger.info(
      "AgentBridge prepared a fresh host task reference for one composed-plan repair",
    );
    return true;
  }

  autoStartLoginForRead(
    payload,
    binding,
    context,
    taskId = null,
    originalResult = null,
  ) {
    if (!isLoginRequiredPayload(payload) || !binding?.readContinuation) {
      return null;
    }
    const sessionKey = binding.sessionKey || context.sessionKey;
    if (!isPrivateSessionKey(sessionKey)) {
      return null;
    }
    const loginTool = loginToolForReadTool(binding.readContinuation.toolName);
    const mcpClient = this.clientForSession(sessionKey);
    if (!loginTool || !mcpClient) {
      return null;
    }
    return (async () => {
      let response;
      try {
        response = await mcpClient.callTool(
          loginTool,
          {},
          {
            meta: {
              ...hostContextMeta(),
              ...(taskId
                ? {
                    [TASK_CONTEXT_META_KEY]: { taskId },
                  }
                : {}),
            },
          },
        );
      } catch (error) {
        this.api.logger.warn(
          `AgentBridge automatic trusted login start failed: ${safeErrorCode(error)}`,
        );
        return undefined;
      }
      await this.observeTaskResponse({ taskId, mcpClient }, response);
      const processed = processToolResult(
        response,
        this.config.allowedCardOrigins,
      );
      if (processed.interactions.length === 0) {
        this.api.logger.warn(
          "AgentBridge automatic trusted login start returned no interaction",
        );
        return undefined;
      }
      const presented =
        (await this.presentInteractions(
          processed.interactions,
          sessionKey,
        )) || processed.interactions;
      this.captureInteractions(presented, binding, context, taskId);
      this.api.logger.info(
        `AgentBridge automatically started trusted login after a read required authentication (tool=${loginTool})`,
      );
      return {
        result: trustedLoginStartedResult(originalResult, loginTool),
      };
    })();
  }

  consumeLoginContinuation(sessionKey) {
    const remembered = this.loginContinuations.get(sessionKey);
    if (isFreshContinuation(remembered, this.now())) {
      this.loginContinuations.delete(sessionKey);
      return remembered;
    }
    this.loginContinuations.delete(sessionKey);
    const recent = this.recentUserMessages.get(sessionKey);
    if (!recent || this.now() - recent.capturedAt > LOGIN_CONTINUATION_TTL_MS) {
      return null;
    }
    return inferReadContinuation(recent.text, this.now());
  }

  captureInteractions(interactions, binding, context, taskId = null) {
    const sessionKey = binding?.sessionKey || context.sessionKey;
    const runId = binding?.runId || context.runId;
    const privateSession = isPrivateSessionKey(sessionKey);
    for (const interaction of interactions) {
      if (!privateSession) {
        this.api.logger.warn(
          sessionKey
            ? "AgentBridge interaction withheld because the OpenClaw session is not private"
            : "AgentBridge interaction withheld because no private session binding was available",
        );
        continue;
      }
      const readContinuation =
        interaction.type === "credential"
          ? this.consumeLoginContinuation(sessionKey)
          : null;
      const record = this.upsert({
        interaction,
        sessionKey,
        runId,
        readContinuation,
        taskId,
      });
      this.api.logger.info(
        `AgentBridge interaction captured for private session (type=${interaction.type}, state=${interaction.state})`,
      );
      this.startPolling(record);
    }
  }

  async hydratePublicInteractionReferences(
    references,
    binding,
    context,
    taskId = null,
  ) {
    const sessionKey = binding?.sessionKey || context.sessionKey;
    if (!isPrivateSessionKey(sessionKey)) {
      this.api.logger.warn(
        sessionKey
          ? "AgentBridge interaction reference withheld because the OpenClaw session is not private"
          : "AgentBridge interaction reference withheld because no private session binding was available",
      );
      return undefined;
    }
    const mcpClient = this.clientForSession(sessionKey);
    if (!mcpClient) {
      this.api.logger.warn(
        "AgentBridge interaction metadata recovery is unavailable because MCP endpoint authentication could not be resolved",
      );
      return undefined;
    }

    const interactions = [];
    for (const reference of references) {
      let response;
      try {
        response = await mcpClient.callTool(
          "agentbridge_interaction_get",
          { interaction_id: reference.interactionId },
        );
      } catch (error) {
        this.api.logger.warn(
          `AgentBridge interaction metadata recovery failed: ${safeErrorCode(error)}`,
        );
        continue;
      }
      const processed = processToolResult(
        response,
        this.config.allowedCardOrigins,
      );
      const interaction = processed.interactions.find(
        (item) =>
          item.interactionId === reference.interactionId &&
          item.type === reference.type &&
          ["pending", "processing"].includes(item.state) &&
          !isInteractionExpired(item, this.now()),
      );
      if (interaction) {
        interactions.push(interaction);
      }
    }
    if (interactions.length === 0) {
      this.api.logger.warn(
        "AgentBridge interaction metadata recovery returned no active trusted interaction",
      );
      return undefined;
    }
    const presented =
      (await this.presentInteractions(interactions, sessionKey)) || interactions;
    this.captureInteractions(presented, binding, context, taskId);
    return undefined;
  }

  presentInteractions(interactions, sessionKey) {
    if (
      typeof this.interactionPresenter !== "function" ||
      !isPrivateSessionKey(sessionKey) ||
      !interactions.some(
        (interaction) => interaction.type === "execution_authorization",
      )
    ) {
      return null;
    }
    return Promise.all(
      interactions.map(async (interaction) => {
        if (interaction.type !== "execution_authorization") {
          return interaction;
        }
        try {
          return (
            (await this.interactionPresenter(interaction, sessionKey)) ||
            interaction
          );
        } catch (error) {
          this.api.logger.warn(
            `AgentBridge endpoint presentation failed; original card retained (${safeErrorCode(error)})`,
          );
          return interaction;
        }
      }),
    );
  }

  activeTaskForSession(sessionKey) {
    this.prune();
    if (!isPrivateSessionKey(sessionKey)) {
      return null;
    }
    const matches = [...this.records.values()]
      .filter(
        (record) =>
          record.sessionKey === sessionKey &&
          record.taskId &&
          ["pending", "processing"].includes(record.interaction.state),
      )
      .sort((left, right) => right.capturedAt - left.capturedAt);
    return (
      matches[0]?.taskId ||
      this.taskContinuationForSession(sessionKey)?.taskId ||
      null
    );
  }

  bindTaskContinuation({
    sessionKey,
    taskId,
    taskStatus,
    executionMode,
    allowNewOperation,
    expiresAt,
  }) {
    if (!isPrivateSessionKey(sessionKey)) {
      return null;
    }
    const normalizedTaskId = safeRoutePart(taskId);
    if (!normalizedTaskId) {
      return null;
    }
    const serverExpiry = Date.parse(String(expiresAt || ""));
    const localExpiry = this.now() + 6 * 60 * 60 * 1000;
    const record = {
      taskId: normalizedTaskId,
      taskStatus: safeRoutePart(taskStatus) || "unknown",
      executionMode: safeRoutePart(executionMode) || "observe_only",
      allowNewOperation: allowNewOperation === true,
      expiresAt: Number.isFinite(serverExpiry)
        ? Math.min(serverExpiry, localExpiry)
        : localExpiry,
      capturedAt: this.now(),
    };
    this.taskContinuations.set(sessionKey, record);
    return record;
  }

  taskContinuationForSession(sessionKey) {
    this.prune();
    const record = this.taskContinuations.get(sessionKey) || null;
    return record && record.expiresAt > this.now() ? record : null;
  }

  bindTaskContinuationChoice(sessionKey, { ttlMs = 10 * 60 * 1000 } = {}) {
    if (!isPrivateSessionKey(sessionKey)) {
      return null;
    }
    const record = {
      capturedAt: this.now(),
      expiresAt: this.now() + ttlMs,
    };
    this.taskContinuationChoices.set(sessionKey, record);
    return record;
  }

  taskContinuationChoiceForSession(sessionKey) {
    this.prune();
    const record = this.taskContinuationChoices.get(sessionKey) || null;
    return record && record.expiresAt > this.now() ? record : null;
  }

  clearTaskContinuationChoice(sessionKey) {
    this.taskContinuationChoices.delete(sessionKey);
  }

  taskIdForBusinessCall(sessionKey, toolName = null) {
    if (isIndependentTaskEntryTool(toolName)) {
      // A governed write is a new user-visible job even when it follows a
      // lookup or references a resource produced by a previous task.
      this.taskContinuations.delete(sessionKey);
      const binding = this.independentTaskBindings.get(sessionKey);
      return binding?.toolName === toolName ? binding.taskId : null;
    }
    const continuation = this.taskContinuationForSession(sessionKey);
    if (continuation) {
      return continuation.allowNewOperation ? continuation.taskId : null;
    }
    // A pending card only proves that this private session owns an unfinished
    // interaction. It does not prove that a later inbound user turn belongs to
    // the same host task. Same-turn tools converge through taskRunRefForToolCall
    // and agentbridge_host_task_ensure; cross-turn reuse must be explicit.
    return null;
  }

  bindIndependentTask(sessionKey, toolName, taskId) {
    if (
      !isPrivateSessionKey(sessionKey) ||
      !isIndependentTaskEntryTool(toolName)
    ) {
      return false;
    }
    const normalizedTaskId = safeRoutePart(taskId);
    if (!normalizedTaskId) {
      return false;
    }
    this.independentTaskBindings.set(sessionKey, {
      toolName,
      taskId: normalizedTaskId,
    });
    return true;
  }

  async restoreRecoveredInteraction({
    taskId,
    interaction,
    sessionKey,
    runId = null,
    mcpClient = null,
  }) {
    if (
      !isPrivateSessionKey(sessionKey) ||
      !taskId ||
      !interaction ||
      this.isTaskTerminal(taskId)
    ) {
      return false;
    }
    const existing = this.records.get(interaction.interactionId);
    const alreadyDelivered = Boolean(
      existing?.delivered &&
        existing.sessionKey === sessionKey &&
        existing.taskId === taskId,
    );
    const presented =
      (await this.presentInteractions([interaction], sessionKey)) || [
        interaction,
      ];
    const record = this.upsert({
      interaction: presented[0],
      sessionKey,
      runId,
      taskId,
    });
    record.mcpClient ||= mcpClient;
    if (interactionResumeAllowed(record.interaction)) {
      if (!resumeClaimAllowed({ claimed: Boolean(record.resumeStarted || this.resumeClaims.has(interaction.interactionId)) })) {
        return true;
      }
      return await this.resume(record, new AbortController().signal);
    }
    this.startPolling(record);
    if (!alreadyDelivered) {
      return await this.deliverInteractionsDirect(sessionKey, presented);
    }
    return true;
  }
  takeForDelivery({ sessionKey }) {
    this.prune();
    if (!isPrivateSessionKey(sessionKey)) {
      return [];
    }
    const matches = [...this.records.values()].filter(
      (record) => record.sessionKey === sessionKey && !record.delivered,
    );
    for (const record of matches) {
      record.delivered = true;
    }
    return matches.map((record) => record.interaction);
  }

  pendingForSession(sessionKey) {
    this.prune();
    if (!isPrivateSessionKey(sessionKey)) {
      return [];
    }
    return [...this.records.values()]
      .filter(
        (record) =>
          record.sessionKey === sessionKey &&
          ["pending", "processing"].includes(record.interaction.state),
      )
      .sort((left, right) => right.capturedAt - left.capturedAt)
      .slice(0, 3)
      .map((record) => record.interaction);
  }

  statusForSession(sessionKey) {
    this.prune();
    const privateSession = isPrivateSessionKey(sessionKey);
    const records = privateSession
      ? [...this.records.values()].filter((record) => record.sessionKey === sessionKey)
      : [];
    return {
      privateSession,
      allowedOriginCount: this.config.allowedCardOrigins.length,
      mcpPollingConfigured: Boolean(
        this.clientForSession(sessionKey) && this.config.autoPoll,
      ),
      pendingCount: records.filter((record) =>
        ["pending", "processing"].includes(record.interaction.state),
      ).length,
      activePollCount: records.filter((record) => this.polls.has(record.interaction.interactionId)).length,
      wakeAgentOnComplete: this.config.wakeAgentOnComplete,
    };
  }

  hostRuntimeCounts() {
    this.prune();
    const activeRecords = [...this.records.values()].filter((record) =>
      ["pending", "processing"].includes(record.interaction.state),
    );
    const taskIds = new Set(
      activeRecords.map((record) => record.taskId).filter(Boolean),
    );
    return {
      activeTaskCount: taskIds.size,
      waitingInteractionCount: activeRecords.length,
    };
  }

  clientForSession(sessionKey) {
    return this.mcpClientResolver?.(sessionKey) || this.mcpClient;
  }

  isDirectDeliveryActive(sessionKey) {
    return (this.directDeliveries.get(sessionKey) || 0) > 0;
  }

  removeSession(sessionKey) {
    for (const [interactionId, record] of this.records) {
      if (record.sessionKey === sessionKey) {
        this.abortControllers.get(interactionId)?.abort();
        this.records.delete(interactionId);
        this.resumeClaims.delete(interactionId);
      }
    }
    for (const [toolCallId, binding] of this.toolBindings) {
      if (binding.sessionKey === sessionKey) {
        this.toolBindings.delete(toolCallId);
      }
    }
    this.loginContinuations.delete(sessionKey);
    this.recentUserMessages.delete(sessionKey);
    this.planningRepairs.delete(sessionKey);
    this.terminalPlanFailures.delete(sessionKey);
    this.taskContinuations.delete(sessionKey);
    this.taskContinuationChoices.delete(sessionKey);
    this.independentTaskBindings.delete(sessionKey);
    this.sessionRoutes.delete(sessionKey);
    this.directDeliveries.delete(sessionKey);
    this.documentDeliveries.delete(sessionKey);
    for (const key of this.documentDeliveryReceipts.keys()) {
      if (key.startsWith(`${sessionKey}\u0000`)) {
        this.documentDeliveryReceipts.delete(key);
      }
    }
  }

  stopAll() {
    this.notificationController?.abort();
    this.notificationController = null;
    this.notificationPump = null;
    for (const controller of this.abortControllers.values()) {
      controller.abort();
    }
    this.abortControllers.clear();
    this.polls.clear();
    this.toolBindings.clear();
    this.loginContinuations.clear();
    this.recentUserMessages.clear();
    this.planningRepairs.clear();
    this.terminalPlanFailures.clear();
    this.taskContinuations.clear();
    this.taskContinuationChoices.clear();
    this.independentTaskBindings.clear();
    this.sessionRoutes.clear();
    this.directDeliveries.clear();
    this.documentDeliveries.clear();
    this.documentDeliveryReceipts.clear();
  }

  startNotificationPump(identityRouter, { intervalMs = 2000 } = {}) {
    if (
      this.notificationController ||
      !identityRouter?.enabled
    ) {
      return false;
    }
    const controller = new AbortController();
    this.notificationController = controller;
    this.notificationPump = this.runNotificationPump(
      identityRouter,
      controller.signal,
      intervalMs,
    ).catch((error) => {
      if (!controller.signal.aborted) {
        this.api.logger.warn(
          `AgentBridge notification pump stopped: ${safeErrorCode(error)}`,
        );
      }
    });
    return true;
  }

  async runNotificationPump(identityRouter, signal, intervalMs) {
    await Promise.all(
      identityRouter.configuredIdentities().map(({ binding, client }) =>
        this.runEndpointNotificationPump(
          identityRouter,
          binding,
          client,
          signal,
          intervalMs,
        ),
      ),
    );
  }

  async runEndpointNotificationPump(
    identityRouter,
    binding,
    client,
    signal,
    intervalMs,
  ) {
    let idleRounds = 0;
    while (!signal.aborted) {
      const notificationCount = await this.deliverEndpointNotifications(
        identityRouter,
        binding,
        client,
        signal,
      );
      if (signal.aborted) {
        return;
      }
      idleRounds = notificationCount > 0 ? 0 : idleRounds + 1;
      await this.notificationSleep(
        notificationPumpDelay(intervalMs, idleRounds),
        signal,
      );
    }
  }

  async deliverEndpointNotifications(
    identityRouter,
    binding,
    client,
    signal,
  ) {
    let response;
    try {
      response = await client.callTool(
        "agentbridge_host_notification_claim",
        {
          agent_host: "openclaw",
          endpoint_key: binding.key,
          limit: 10,
          lease_seconds: 30,
        },
        { signal, meta: hostContextMeta() },
      );
    } catch (error) {
      if (!signal.aborted) {
        this.api.logger.debug?.(
          `AgentBridge endpoint notification claim unavailable (${safeErrorCode(error)})`,
        );
      }
      return 0;
    }
    const endpoint = response?.endpoint;
    const notifications = Array.isArray(response?.notifications)
      ? response.notifications
      : [];
    for (const notification of notifications) {
      const eventType = safeRoutePart(notification?.event?.eventType);
      if (TERMINAL_TASK_EVENTS.has(eventType)) {
        this.markTaskTerminal(
          notification?.task?.taskId,
          notification?.event?.payload?.status || eventType,
        );
      }
    }
    for (const notification of notifications) {
      let delivered = false;
      const sessionKey = endpoint?.conversationRef;
      const route = endpoint?.route;
      const recovered = await this.restoreInteractionFromNotification(
        notification,
        client,
        { identityRouter, binding, endpoint },
      );
      if (recovered === false) {
        this.api.logger.warn("AgentBridge interaction notification retained for recovery retry");
      } else if (
        notification?.deliveryMode === "origin_handled" ||
        notification?.deliveryMode === "no_op"
      ) {
        delivered = true;
      } else if (isPullBasedEndpoint(endpoint, route, binding)) {
        // Agent Workspace reads the same Task Hub event stream directly.
        delivered = true;
      } else if (
        notification?.deliveryMode === "status" &&
        (QUIET_COMPANION_TASK_EVENTS.has(notification?.event?.eventType) ||
          isSimpleReadSuccessNotification(notification))
      ) {
        // Keep companion chats focused on actionable cards and terminal results.
        delivered = true;
      } else if (
        isPrivateSessionKey(sessionKey) &&
        route &&
        typeof route === "object" &&
        !Array.isArray(route) &&
        identityRouter.restoreSessionBinding({
          sessionKey,
          bindingKey: binding.key,
        })
      ) {
        this.bindDeliveryRoute({
          sessionKey,
          channel: route.channel || binding.channel,
          to: route.to || binding.senderId,
          accountId: route.accountId || binding.accountId,
          threadId: route.threadId,
        });
        if (
          notification.deliveryMode === "trusted_interaction" &&
          notification.interaction
        ) {
          delivered = await this.deliverInteractionsDirect(
            sessionKey,
            [notification.interaction],
          );
        } else if (
          notification.deliveryMode === "artifact" &&
          notification.artifact
        ) {
          const receipt = await this.deliverPreparedDocumentDirect(
            sessionKey,
            notification.artifact,
            { deliveryRef: notification.deliveryId },
          );
          delivered = receipt.delivered;
          await this.reportPreparedDocumentDelivery({
            sessionKey,
            taskId: notification.task?.taskId,
            deliveryRef: notification.deliveryId,
            mcpClient: client,
            report: preparedDocumentDeliveryReport(
              [notification.artifact],
              [receipt],
            ),
          });
        } else if (
          notification.deliveryMode === "timeline_message" &&
          typeof notification.message === "string" &&
          Array.isArray(notification.attachments) &&
          notification.attachments.length > 0
        ) {
          delivered = await this.deliverTimelineMessageDirect(
            sessionKey,
            notification.message,
            notification.attachments,
          );
        } else if (
          ["status", "timeline_message", "plan_result"].includes(
            notification.deliveryMode,
          ) &&
          typeof notification.message === "string"
        ) {
          delivered = await this.deliverTextDirect(
            sessionKey,
            notification.message,
          );
        }
      }
      try {
        const deferUntilActivity =
          !delivered && recovered !== false &&
          isActivityGatedDirectEndpoint(endpoint, route, binding);
        await client.callTool(
          "agentbridge_host_notification_ack",
          {
            agent_host: "openclaw",
            endpoint_key: binding.key,
            delivery_id: notification.deliveryId,
            succeeded: delivered,
            retry_after_seconds: 5,
            defer_until_activity: deferUntilActivity,
          },
          { signal, meta: hostContextMeta() },
        );
      } catch (error) {
        if (!signal.aborted) {
          this.api.logger.warn(
            `AgentBridge endpoint notification acknowledgement failed (${safeErrorCode(error)})`,
          );
        }
      }
    }
    return notifications.length;
  }

  async restoreInteractionFromNotification(notification, client, { identityRouter, binding, endpoint }) {
    const taskId = safeRoutePart(notification?.task?.taskId);
    if (!taskId || this.isTaskTerminal(taskId)) {
      return null;
    }
    const sessionKey = safeRoutePart(
      notification.task.activeConversationRef,
    );
    const workspaceSession = isWorkspaceSessionKey(sessionKey);
    const originHandled = notification.deliveryMode === "origin_handled";
    if (!workspaceSession && !originHandled) return null;
    let interaction = notification.interaction || null;
    const eventType = safeRoutePart(notification?.event?.eventType);
    const eventInteractionId = safeRoutePart(
      notification?.event?.payload?.interactionId,
    );
    if (!interaction && !["task.interaction.waiting", "task.interaction.completed"].includes(eventType)) {
      return null;
    }
    if (
      !isPrivateSessionKey(sessionKey) ||
      (!workspaceSession && sessionKey !== endpoint?.conversationRef) ||
      !identityRouter.restoreSessionBinding({ sessionKey, bindingKey: binding.key })
    ) {
      return false;
    }
    const existing = this.records.get(interaction?.interactionId || eventInteractionId);
    if (existing && (existing.sessionKey !== sessionKey || (existing.taskId && existing.taskId !== taskId))) {
      return false;
    }
    if (existing?.resumeStarted || this.resumeClaims.has(eventInteractionId)) return true;
    if (
      !interaction &&
      eventInteractionId &&
      ["task.interaction.waiting", "task.interaction.completed"].includes(
        eventType,
      )
    ) {
      try {
        const response = await client.callTool(
          "agentbridge_interaction_get",
          { interaction_id: eventInteractionId },
          { meta: hostContextMeta() },
        );
        const processed = processToolResult(
          response,
          this.config.allowedCardOrigins,
        );
        interaction = processed.interactions.find(
          (item) => item.interactionId === eventInteractionId,
        );
      } catch (error) {
        this.api.logger.warn(
          `AgentBridge interaction notification recovery failed: ${safeErrorCode(error)}`,
        );
        return false;
      }
    }
    if (this.isTaskTerminal(taskId)) {
      return true;
    }
    const resumableCompleted = interactionResumeAllowed(interaction);
    if (!interaction) return false;
    if (!resumableCompleted && !["pending", "processing"].includes(interaction.state)) return true;
    if (!workspaceSession && !endpoint?.route) return false;
    this.bindDeliveryRoute({
      sessionKey,
      channel: workspaceSession ? "webchat" : endpoint.route.channel || binding.channel,
      to: workspaceSession ? notification.task.originEndpointId || sessionKey : endpoint.route.to || binding.senderId,
      accountId: workspaceSession ? null : endpoint.route.accountId || binding.accountId,
      threadId: workspaceSession ? null : endpoint.route.threadId,
    });
    const restored = await this.restoreRecoveredInteraction({
      taskId,
      interaction,
      sessionKey,
      mcpClient: client,
    });
    if (restored) {
      this.api.logger.info(
        "AgentBridge restored private interaction coordination from endpoint notification",
      );
    }
    return restored;
  }

  async waitForIdle() {
    await Promise.allSettled([
      ...this.polls.values(),
      ...(this.timelinePublisher
        ? [this.timelinePublisher.waitForIdle()]
        : []),
    ]);
  }

  upsert({
    interaction,
    sessionKey,
    runId,
    readContinuation = null,
    taskId = null,
  }) {
    const existing = this.records.get(interaction.interactionId);
    if (existing) {
      existing.interaction = interaction;
      existing.sessionKey = sessionKey || existing.sessionKey;
      existing.runId = runId || existing.runId;
      existing.taskId ||= taskId;
      existing.readContinuation ||= readContinuation;
      existing.mcpClient ||= this.clientForSession(existing.sessionKey);
      return existing;
    }
    const record = {
      interaction,
      sessionKey,
      runId,
      taskId,
      readContinuation,
      mcpClient: this.clientForSession(sessionKey),
      delivered: false,
      continuationQueued: false,
      resumeStarted: false,
      capturedAt: this.now(),
    };
    this.records.set(interaction.interactionId, record);
    this.prune();
    return record;
  }

  startPolling(record) {
    if (
      !this.config.autoPoll ||
      !record.mcpClient ||
      !["pending", "processing"].includes(record.interaction.state) ||
      this.polls.has(record.interaction.interactionId)
    ) {
      return;
    }
    const controller = new AbortController();
    this.abortControllers.set(record.interaction.interactionId, controller);
    const promise = this.poll(record, controller.signal)
      .catch((error) => {
        if (!controller.signal.aborted) {
          this.api.logger.warn(
            `AgentBridge interaction polling stopped: ${safeErrorCode(error)}`,
          );
        }
      })
      .finally(() => {
        this.polls.delete(record.interaction.interactionId);
        this.abortControllers.delete(record.interaction.interactionId);
      });
    this.polls.set(record.interaction.interactionId, promise);
  }

  async poll(record, signal) {
    const deadline = Math.min(
      this.now() + this.config.maxPollSeconds * 1000,
      interactionDeadline(record.interaction) ?? Number.POSITIVE_INFINITY,
    );
    let consecutiveErrors = 0;

    while (!signal.aborted && this.now() < deadline) {
      const intervalMs = consecutiveErrors >= MAX_POLL_ERRORS
        ? Math.max(this.config.pollIntervalSeconds * 1000, 30_000)
        : this.config.pollIntervalSeconds * 1000;
      await this.sleep(Math.min(intervalMs, Math.max(0, deadline - this.now())), signal);
      if (signal.aborted) {
        return;
      }
      if (this.now() >= deadline) break;
      let response;
      try {
        response = await record.mcpClient.callTool(
          "agentbridge_interaction_get",
          { interaction_id: record.interaction.interactionId },
          { signal },
        );
        if (consecutiveErrors >= MAX_POLL_ERRORS) {
          this.api.logger.info("AgentBridge trusted interaction polling recovered");
        }
        consecutiveErrors = 0;
      } catch (error) {
        consecutiveErrors += 1;
        if (consecutiveErrors >= MAX_POLL_ERRORS) {
          if (error?.retryable === true) {
            if (consecutiveErrors === MAX_POLL_ERRORS) {
              this.api.logger.warn(
                `AgentBridge trusted interaction polling is backing off (${safeErrorCode(error)}; transport=${safeErrorCode({ code: error.transportCode })})`,
              );
            }
            continue;
          }
          await this.notify(record, "poll_failed", safeErrorCode(error));
          return;
        }
        continue;
      }

      const processed = processToolResult(
        response,
        this.config.allowedCardOrigins,
      );
      const current = processed.interactions.find(
        (item) => item.interactionId === record.interaction.interactionId,
      );
      if (!current) {
        continue;
      }
      record.interaction = current;
      if (TERMINAL_STATES.has(current.state)) {
        await this.notify(record, current.state, null);
        return;
      }
      if (current.state !== "completed") {
        continue;
      }
      if (!interactionResumeAllowed(current)) {
        await this.notify(record, "completed", null);
        return;
      }
      await this.resume(record, signal);
      return;
    }
    if (!signal.aborted) {
      await this.notify(record, "poll_expired", null);
    }
  }

  async resume(record, signal) {
    const interactionId = record.interaction.interactionId;
    if (!resumeClaimAllowed({
      taskTerminal: Boolean(record.taskId && this.isTaskTerminal(record.taskId)),
      claimed: Boolean(record.resumeStarted || this.resumeClaims.has(interactionId)),
    })) {
      return false;
    }
    record.resumeStarted = true;
    this.resumeClaims.set(interactionId, {
      taskId: record.taskId || null,
      claimedAt: this.now(),
    });
    let response;
    let resumeDispatched = false;
    try {
      let coordinatorLease = null;
      if (record.taskId) {
        const leaseResponse = await record.mcpClient.callTool(
          "agentbridge_host_coordinator_lease_acquire",
          {
            task_id: record.taskId,
            lease_seconds: 600,
            takeover: false,
            expected_version: null,
          },
          { signal, meta: hostContextMeta() },
        );
        if (
          leaseResponse?.status === "ignored" &&
          leaseResponse?.reason === "task_terminal"
        ) {
          this.markTaskTerminal(
            record.taskId,
            leaseResponse.taskStatus || "terminal",
          );
          this.api.logger.info(
            "AgentBridge ignored stale interaction recovery because the parent task is terminal",
          );
          return true;
        }
        if (this.isTaskTerminal(record.taskId)) {
          return true;
        }
        coordinatorLease = leaseResponse?.coordinatorLease || null;
        if (
          !coordinatorLease ||
          coordinatorLease.hostInstanceId !== "openclaw-gateway"
        ) {
          const leaseError = new Error(
            "AgentBridge did not grant the OpenClaw task coordinator lease",
          );
          leaseError.code = "HOST_COORDINATOR_LEASE_CONFLICT";
          throw leaseError;
        }
        record.coordinatorLeaseVersion = coordinatorLease.version;
      }
      resumeDispatched = true;
      response = await record.mcpClient.callTool(
        "agentbridge_interaction_resume",
        {
          interaction_id: record.interaction.interactionId,
          idempotency_key: `openclaw:${record.interaction.interactionId}`,
        },
        {
          signal,
          meta: {
            ...hostContextMeta(),
            ...(record.taskId
              ? {
                [TASK_CONTEXT_META_KEY]: {
                  taskId: record.taskId,
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
        },
      );
    } catch (error) {
      if (signal.aborted || (record.taskId && this.isTaskTerminal(record.taskId))) {
        return true;
      }
      // A lost resume reply may follow a business write. Outbox recovery must
      // not turn that uncertainty into a second execution attempt.
      if (!resumeDispatched) {
        record.resumeStarted = false;
        this.resumeClaims.delete(interactionId);
      }
      await this.notify(record, "resume_failed", safeErrorCode(error));
      return resumeDispatched;
    }

    if (response?.nextAction?.type === "original_request_completed") {
      if (record.taskId) {
        this.markTaskTerminal(record.taskId, response.taskStatus || safeStatus(response));
      }
      this.api.logger.info("AgentBridge persisted login read continuation and feedback centrally");
      return true;
    }

    const processed = processToolResult(
      response,
      this.config.allowedCardOrigins,
    );
    let nextInteractions = processed.interactions.filter(
      (item) => item.interactionId !== record.interaction.interactionId,
    );
    nextInteractions =
      (await this.presentInteractions(
        nextInteractions,
        record.sessionKey,
      )) || nextInteractions;
    for (const interaction of nextInteractions) {
      const next = this.upsert({
        interaction,
        sessionKey: record.sessionKey,
        runId: null,
        taskId: record.taskId,
      });
      this.startPolling(next);
    }
    await this.notify(
      record,
      nextInteractions.length > 0 ? "next_interaction_required" : safeStatus(response),
      safeResponseErrorCode(response),
      nextInteractions,
      {
        resumeOriginalRequest: shouldResumeOriginalRequest(
          record,
          response,
          nextInteractions,
        ),
        response,
      },
    );
    return true;
  }

  async notify(
    record,
    status,
    errorCode,
    nextInteractions = [],
    { resumeOriginalRequest = false, response = null } = {},
  ) {
    if (!record.sessionKey) {
      return;
    }
    if (
      nextInteractions.length > 0 &&
      (await this.deliverInteractionsDirect(record.sessionKey, nextInteractions))
    ) {
      return;
    }
    if (resumeOriginalRequest && this.config.wakeAgentOnComplete) {
      if (record.continuationQueued) {
        return;
      }
      record.continuationQueued = true;
      const pendingBeforeStatus = this.undeliveredPendingFor(record);
      if (
        pendingBeforeStatus.length > 0 &&
        (await this.deliverInteractionsDirect(
          record.sessionKey,
          pendingBeforeStatus,
        ))
      ) {
        return;
      }
      if (
        record.readContinuation &&
        (await this.replayReadContinuation(record))
      ) {
        return;
      }
      await this.deliverStatusDirect(record.sessionKey, status, errorCode, response);
      const pendingAfterStatus = this.undeliveredPendingFor(record);
      if (
        pendingAfterStatus.length > 0 &&
        (await this.deliverInteractionsDirect(
          record.sessionKey,
          pendingAfterStatus,
        ))
      ) {
        return;
      }
      this.api.runtime.system.enqueueSystemEvent(
        [
          "AgentBridge 登录已完成。",
          "继续处理触发本次登录的原始用户请求，并重新调用所需工具取得最新结果。",
          "除非实时会话检查再次明确要求登录，否则不要重复调用登录工具。",
          "不要索取或复述密码、业务字段、授权内容或可信卡片 URL。",
        ].join(""),
        {
          sessionKey: record.sessionKey,
          contextKey: `agentbridge:continue:${record.interaction.interactionId}`,
        },
      );
      await this.wakeAgent(
        record.sessionKey,
        "hook:agentbridge-login-completed",
      );
      const pendingAfterContinuation = this.undeliveredPendingFor(record, {
        allowLaterRun: true,
      });
      if (
        pendingAfterContinuation.length > 0 &&
        (await this.deliverInteractionsDirect(
          record.sessionKey,
          pendingAfterContinuation,
        ))
      ) {
        return;
      }
      this.api.logger.info(
        "AgentBridge original request continuation queued after login",
      );
      return;
    }
    if (
      nextInteractions.length === 0 &&
      (await this.deliverStatusDirect(record.sessionKey, status, errorCode, response))
    ) {
      return;
    }
    const suffix = errorCode ? `，错误码 ${errorCode}` : "";
    this.api.runtime.system.enqueueSystemEvent(
      `AgentBridge 可信交互宿主事件：${status}${suffix}。不要向用户索取密码、业务字段或授权内容。`,
      {
        sessionKey: record.sessionKey,
        contextKey: `agentbridge:${record.interaction.interactionId}`,
      },
    );
    if (this.config.wakeAgentOnComplete) {
      await this.wakeAgent(record.sessionKey);
    }
  }

  async replayReadContinuation(record) {
    const continuation = record.readContinuation;
    if (!continuation || !record.mcpClient) {
      return false;
    }
    const descriptor = LOGIN_READ_TOOLS.get(continuation.toolName);
    if (descriptor?.kind?.startsWith("smartlight_")) {
      // Smartlight read results have several domain-specific schemas. Let the
      // model resume the original turn instead of flattening them as OA lists.
      return false;
    }
    let response;
    try {
      response = await record.mcpClient.callTool(
        continuation.toolName,
        continuation.arguments,
        {
          meta: {
            ...hostContextMeta(),
            ...(record.taskId ? {
                [TASK_CONTEXT_META_KEY]: {
                  taskId: record.taskId,
                  ...(record.coordinatorLeaseVersion ? {
                    coordinatorLeaseVersion: String(record.coordinatorLeaseVersion),
                  } : {}),
                },
              } : {}),
          },
        },
      );
      await this.observeTaskResponse(record, response);
    } catch (error) {
      const system =
        LOGIN_READ_TOOLS.get(continuation.toolName)?.system || "下游系统";
      const text =
        `${system} 登录已完成，但 AgentBridge 自动继续原读取请求失败` +
        `（错误码：${safeErrorCode(error)}）。请重新发送一次读取请求。`;
      return this.deliverReadContinuation(record, text, null);
    }

    if (safeStatus(response) !== "succeeded") {
      const code = safeResponseErrorCode(response) || safeStatus(response);
      const system =
        LOGIN_READ_TOOLS.get(continuation.toolName)?.system || "下游系统";
      const text =
        `${system} 登录已完成，但原读取请求仍然失败` +
        `（错误码：${safeCode(code)}）。请重新发送一次读取请求。`;
      return this.deliverReadContinuation(record, text, response);
    }

    const text = formatReadContinuation(continuation, response);
    const delivered = await this.deliverReadContinuation(record, text, response);
    if (delivered) {
      this.api.logger.info(
        `AgentBridge read request continued after login (tool=${continuation.toolName})`,
      );
    }
    return delivered;
  }

  async deliverReadContinuation(record, text, response) {
    if (await this.deliverTextDirect(record.sessionKey, text)) {
      return true;
    }
    const serialized = JSON.stringify(response || {}).slice(0, 12000);
    this.api.runtime.system.enqueueSystemEvent(
      [
        text,
        `Continued AgentBridge read tool: ${record.readContinuation.toolName}.`,
        `Structured result: ${serialized}`,
        "Answer the original user request from this result. Do not call the login tool again.",
      ].join("\n"),
      {
        sessionKey: record.sessionKey,
        contextKey: `agentbridge:read-continuation:${record.interaction.interactionId}`,
      },
    );
    await this.wakeAgent(
      record.sessionKey,
      "hook:agentbridge-read-continuation-completed",
    );
    return true;
  }

  async observeTaskResponse(record, response) {
    if (!record.taskId || !record.mcpClient) {
      return;
    }
    const references = collectTaskReferences(response);
    if (
      references.operationIds.length === 0 &&
      references.interactionIds.length === 0
    ) {
      return;
    }
    try {
      await record.mcpClient.callTool(
        "agentbridge_host_task_observe",
        {
          agent_host: "openclaw",
          task_id: record.taskId,
          operation_ids: references.operationIds,
          interaction_ids: references.interactionIds,
        },
        { meta: hostContextMeta() },
      );
    } catch (error) {
      this.api.logger.warn(
        `AgentBridge continued task observation failed: ${safeErrorCode(error)}`,
      );
    }
  }

  async materializePreparedDocument(file) {
    if (file.protectedCsv) {
      const body = Buffer.from(file.contentBase64, "base64");
      const saved = await this.saveMediaBufferImpl(body, "text/csv", "inbound", 65536, file.filename, file.filename);
      if (!saved?.path) throw new Error("protected CSV was not saved to the media store");
      return saved.path;
    }
    if (typeof this.fetchImpl !== "function") {
      throw new Error("prepared document fetch is unavailable");
    }
    const response = await this.fetchImpl(file.mediaUrl, {
      headers: { Accept: file.contentType },
      signal: AbortSignal.timeout(45_000),
    });
    if (!response?.ok) {
      throw new Error(`prepared document fetch failed: HTTP ${response?.status || 0}`);
    }
    const declaredSize = Number(response.headers?.get?.("content-length") || 0);
    const maximumSize = 25 * 1024 * 1024;
    if (declaredSize > maximumSize) {
      throw new Error("prepared document exceeds the media size limit");
    }
    const body = Buffer.from(await response.arrayBuffer());
    if (!body.length || body.length > maximumSize) {
      throw new Error("prepared document body is invalid");
    }
    const contentType =
      String(response.headers?.get?.("content-type") || "")
        .split(";", 1)[0]
        .trim()
        .toLowerCase() || file.contentType;
    if (
      ![
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/csv",
      ].includes(
        contentType,
      )
    ) {
      throw new Error("prepared document content type is unsupported");
    }
    const saved = await this.saveMediaBufferImpl(
      body,
      contentType,
      "inbound",
      maximumSize,
      file.filename,
      file.filename,
    );
    if (!saved?.path) {
      throw new Error("prepared document was not saved to the OpenClaw media store");
    }
    return saved.path;
  }

  async deliverPreparedDocumentDirect(
    sessionKey,
    file,
    { deliveryRef = null } = {},
  ) {
    this.prunePreparedDocumentReceipts();
    const receiptKey = preparedDocumentReceiptKey(
      sessionKey,
      deliveryRef,
      file,
    );
    const existing = receiptKey
      ? this.documentDeliveryReceipts.get(receiptKey)
      : null;
    if (existing) {
      return existing.promise;
    }
    const promise = this.performPreparedDocumentDelivery(sessionKey, file);
    if (receiptKey) {
      this.documentDeliveryReceipts.set(receiptKey, {
        promise,
        capturedAt: this.now(),
      });
    }
    const receipt = await promise;
    if (receiptKey && !receipt.delivered) {
      this.documentDeliveryReceipts.delete(receiptKey);
    }
    return receipt;
  }

  async performPreparedDocumentDelivery(sessionKey, file) {
    const route = this.sessionRoutes.get(sessionKey);
    if (!route) {
      this.api.logger.warn(
        "AgentBridge prepared document delivery unavailable because the private session route is missing",
      );
      return preparedDocumentReceipt(file, "failed", 0, "ROUTE_MISSING");
    }
    if (this.isPullBasedSession(sessionKey) && (!file.protectedCsv || file.artifactId)) {
      this.api.logger.info(
        `AgentBridge prepared document exposed through the pull-based task card (channel=${route.channel}, filename=${safeCode(file.filename)})`,
      );
      return preparedDocumentReceipt(
        file,
        "fallback_link_sent",
        0,
        null,
      );
    }
    const text = `AgentBridge 文件已准备完成：${file.filename}`;
    let localMediaPath = null;
    let errorCode = "ATTACHMENT_DELIVERY_FAILED";
    try {
      localMediaPath = await this.materializePreparedDocument(file);
    } catch (error) {
      errorCode = safeErrorCode(error);
      this.api.logger.warn(
        `AgentBridge prepared document materialization failed: ${errorCode}`,
      );
    }
    let attemptCount = 0;
    while (localMediaPath && attemptCount < PREPARED_DOCUMENT_NATIVE_ATTEMPTS) {
      attemptCount += 1;
      try {
        const delivered = await this.sendRoutePayload(sessionKey, route, {
          text,
          mediaUrl: localMediaPath,
          forceDocument: true,
        });
        if (delivered) {
          this.api.logger.info(
            `AgentBridge prepared document delivered directly (channel=${route.channel}, filename=${safeCode(file.filename)}, attempts=${attemptCount})`,
          );
          return preparedDocumentReceipt(
            file,
            "attachment_sent",
            attemptCount,
            null,
          );
        }
        errorCode = "ATTACHMENT_DELIVERY_UNAVAILABLE";
        break;
      } catch (error) {
        errorCode = safeErrorCode(error);
        const retryable = isRetryablePreparedDocumentDeliveryError(error);
        this.api.logger.warn(
          `AgentBridge prepared document attachment delivery failed (attempt=${attemptCount}/${PREPARED_DOCUMENT_NATIVE_ATTEMPTS}, code=${errorCode})`,
        );
        if (
          !retryable ||
          attemptCount >= PREPARED_DOCUMENT_NATIVE_ATTEMPTS
        ) {
          break;
        }
        await this.sleep(PREPARED_DOCUMENT_RETRY_DELAY_MS);
      }
    }
    if (file.protectedCsv) {
      return preparedDocumentReceipt(file, "failed", attemptCount, errorCode);
    }
    const fallback = [
      `OA 证书“${file.filename}”已准备好，但附件上传失败。`,
      "可在链接有效期内直接下载：",
      file.mediaUrl,
    ].join("\n");
    try {
      if (await this.sendRoutePayload(sessionKey, route, { text: fallback })) {
        this.api.logger.info(
          `AgentBridge prepared document delivered as a fallback link (channel=${route.channel}, filename=${safeCode(file.filename)})`,
        );
        return preparedDocumentReceipt(
          file,
          "fallback_link_sent",
          attemptCount,
          errorCode,
        );
      }
    } catch (error) {
      errorCode = safeErrorCode(error);
      this.api.logger.warn(
        `AgentBridge prepared document fallback delivery failed: ${errorCode}`,
      );
    }
    return preparedDocumentReceipt(file, "failed", attemptCount, errorCode);
  }

  async reportPreparedDocumentDelivery({
    sessionKey,
    taskId,
    deliveryRef,
    report,
    mcpClient = null,
  }) {
    if (!taskId || !report?.files?.some((file) => file.artifactId)) {
      return false;
    }
    const deliveryClient = mcpClient || this.clientForSession(sessionKey);
    if (!deliveryClient) {
      return false;
    }
    const channel = this.sessionRoutes.get(sessionKey)?.channel || "unknown";
    try {
      await deliveryClient.callTool(
        "agentbridge_host_artifact_delivery_report",
        {
          agent_host: "openclaw",
          task_id: taskId,
          delivery_ref: deliveryRef,
          channel,
          files: report.files
            .filter((file) => file.artifactId)
            .map((file) => ({
              artifact_id: file.artifactId,
              state: file.state,
              attempt_count: file.attemptCount,
              error_code: file.errorCode,
            })),
        },
        { meta: hostContextMeta() },
      );
      return true;
    } catch (error) {
      this.api.logger.warn(
        `AgentBridge prepared document delivery report failed: ${safeErrorCode(error)}`,
      );
      return false;
    }
  }

  async deliverTimelineMessageDirect(sessionKey, text, attachments) {
    const route = this.sessionRoutes.get(sessionKey);
    if (!route) {
      this.api.logger.warn(
        "AgentBridge timeline media delivery unavailable because the private session route is missing",
      );
      return false;
    }
    const files = normalizeTimelineAttachments(
      attachments,
      this.config.allowedCardOrigins,
    );
    if (files.length === 0) {
      return this.deliverTextDirect(sessionKey, text);
    }

    let adapter;
    try {
      adapter = await this.api.runtime.channel.outbound.loadAdapter(
        route.channel,
      );
    } catch (error) {
      this.api.logger.warn(
        `AgentBridge timeline media adapter unavailable: ${safeErrorCode(error)}`,
      );
    }
    if (!adapter?.sendPayload) {
      return this.deliverTimelineAttachmentLinks(sessionKey, text, files);
    }

    let materialized;
    try {
      materialized = await Promise.all(
        files.map(async (file) => ({
          ...file,
          localMediaPath: await this.materializePreparedDocument(file),
        })),
      );
    } catch (error) {
      this.api.logger.warn(
        `AgentBridge timeline media materialization failed: ${safeErrorCode(error)}`,
      );
      return this.deliverTimelineAttachmentLinks(sessionKey, text, files);
    }

    for (const [index, file] of materialized.entries()) {
      try {
        const delivered = await this.sendRoutePayload(sessionKey, route, {
          text:
            index === 0
              ? text
              : `附加图片 ${index + 1}/${materialized.length}：${file.filename}`,
          mediaUrl: file.localMediaPath,
        });
        if (delivered) {
          continue;
        }
      } catch (error) {
        this.api.logger.warn(
          `AgentBridge timeline media upload failed: ${safeErrorCode(error)}`,
        );
      }
      return this.deliverTimelineAttachmentLinks(sessionKey, text, files);
    }
    this.api.logger.info(
      `AgentBridge timeline media delivered directly (channel=${route.channel}, count=${materialized.length})`,
    );
    return true;
  }

  async deliverTimelineAttachmentLinks(sessionKey, text, files) {
    const fallback = [
      text,
      "附加图片：",
      ...files.map(
        (file, index) =>
          `${index + 1}. ${file.filename}\n${file.mediaUrl}`,
      ),
    ].join("\n");
    try {
      return await this.deliverTextDirect(sessionKey, fallback);
    } catch (error) {
      this.api.logger.warn(
        `AgentBridge timeline media fallback failed: ${safeErrorCode(error)}`,
      );
      return false;
    }
  }

  async deliverTextDirect(sessionKey, text) {
    const route = this.sessionRoutes.get(sessionKey);
    if (!route) {
      this.api.logger.warn(
        "AgentBridge direct read continuation unavailable because the private session route is missing",
      );
      return false;
    }
    try {
      if (!(await this.sendRoutePayload(sessionKey, route, { text }))) {
        return false;
      }
      return true;
    } catch (error) {
      this.api.logger.warn(
        `AgentBridge direct read continuation failed: ${safeErrorCode(error)}`,
      );
      return false;
    }
  }

  async deliverInteractionsDirect(sessionKey, interactions) {
    const route = this.sessionRoutes.get(sessionKey);
    if (!route) {
      this.api.logger.warn(
        "AgentBridge direct card delivery unavailable because the private session route is missing",
      );
      return false;
    }
    if (this.isPullBasedSession(sessionKey)) {
      const deliveredIds = new Set(
        interactions.map((interaction) => interaction.interactionId),
      );
      for (const item of this.records.values()) {
        if (
          item.sessionKey === sessionKey &&
          deliveredIds.has(item.interaction.interactionId)
        ) {
          item.delivered = true;
        }
      }
      this.api.logger.info(
        `AgentBridge trusted card is available through the pull-based task stream (channel=${route.channel}, count=${interactions.length})`,
      );
      return true;
    }
    try {
      const presentation = buildPresentation(interactions, route.channel);
      if (!presentation) {
        return false;
      }
      const text = "请处理下面的 AgentBridge 安全卡片。";
      const initialPayload = { text, presentation };
      if (
        !(await this.sendRoutePayload(
          sessionKey,
          route,
          initialPayload,
          presentation,
        ))
      ) {
        this.api.logger.warn(
          `AgentBridge direct card delivery unavailable for channel ${route.channel}`,
        );
        return false;
      }
      const deliveredIds = new Set(
        interactions.map((interaction) => interaction.interactionId),
      );
      for (const item of this.records.values()) {
        if (
          item.sessionKey === sessionKey &&
          deliveredIds.has(item.interaction.interactionId)
        ) {
          item.delivered = true;
        }
      }
      this.api.logger.info(
        `AgentBridge next trusted card delivered directly (channel=${route.channel}, count=${interactions.length})`,
      );
      return true;
    } catch (error) {
      this.api.logger.warn(
        `AgentBridge direct card delivery failed: ${safeErrorCode(error)}`,
      );
      return false;
    }
  }

  async deliverStatusDirect(sessionKey, status, errorCode, response = null) {
    const route = this.sessionRoutes.get(sessionKey);
    if (!route) {
      this.api.logger.warn(
        "AgentBridge direct status delivery unavailable because the private session route is missing",
      );
      return false;
    }
    if (this.isPullBasedSession(sessionKey)) {
      this.api.logger.info(
        `AgentBridge trusted status is available through the pull-based task stream (channel=${route.channel}, status=${safeCode(status)})`,
      );
      return true;
    }
    const text = safeStatusMessage(status, errorCode, response);
    try {
      if (!(await this.sendRoutePayload(sessionKey, route, { text }))) {
        this.api.logger.warn(
          `AgentBridge direct status delivery unavailable for channel ${route.channel}`,
        );
        return false;
      }
      this.api.logger.info(
        `AgentBridge trusted interaction status delivered directly (channel=${route.channel}, status=${safeCode(status)})`,
      );
      return true;
    } catch (error) {
      this.api.logger.warn(
        `AgentBridge direct status delivery failed: ${safeErrorCode(error)}`,
      );
      return false;
    }
  }

  async sendRoutePayload(sessionKey, route, initialPayload, presentation = null) {
    const depth = this.directDeliveries.get(sessionKey) || 0;
    this.directDeliveries.set(sessionKey, depth + 1);
    try {
      const adapter = await this.api.runtime.channel.outbound.loadAdapter(
        route.channel,
      );
      if (!adapter?.sendPayload && !adapter?.sendText) {
        return false;
      }
      const text =
        typeof initialPayload.text === "string" ? initialPayload.text : "";
      const baseContext = {
        cfg: this.api.config,
        to: route.to,
        text,
        ...(route.accountId ? { accountId: route.accountId } : {}),
        ...(route.threadId !== null ? { threadId: route.threadId } : {}),
      };
      const payload =
        presentation && adapter.renderPresentation
          ? await adapter.renderPresentation({
              payload: initialPayload,
              presentation,
              ctx: { ...baseContext, payload: initialPayload },
            })
          : presentation
            ? appendPresentationLinks(initialPayload, presentation)
            : initialPayload;
      if (!payload) {
        return false;
      }
      if (!adapter.sendPayload) {
        await adapter.sendText({
          ...baseContext,
          text: typeof payload.text === "string" ? payload.text : text,
        });
        return true;
      }
      await adapter.sendPayload({
        ...baseContext,
        text: typeof payload.text === "string" ? payload.text : text,
        payload,
      });
      return true;
    } finally {
      if (depth === 0) {
        this.directDeliveries.delete(sessionKey);
      } else {
        this.directDeliveries.set(sessionKey, depth);
      }
    }
  }

  undeliveredPendingFor(record, { allowLaterRun = false } = {}) {
    this.prune();
    return [...this.records.values()]
      .filter(
        (candidate) =>
          candidate !== record &&
          candidate.sessionKey === record.sessionKey &&
          candidate.delivered === false &&
          ["pending", "processing"].includes(candidate.interaction.state) &&
          (allowLaterRun
            ? candidate.capturedAt >= record.capturedAt
            : record.runId && candidate.runId
              ? candidate.runId === record.runId
              : candidate.capturedAt >= record.capturedAt),
      )
      .sort((left, right) => left.capturedAt - right.capturedAt)
      .slice(0, 3)
      .map((candidate) => candidate.interaction);
  }

  async wakeAgent(sessionKey, reason = "hook:agentbridge-interaction-updated") {
    const options = {
      // OpenClaw infers hook wake semantics from this prefix when the plugin
      // runtime's runHeartbeatOnce facade cannot forward an explicit source.
      reason,
      sessionKey,
      heartbeat: { target: "last" },
    };
    if (typeof this.api.runtime.system.runHeartbeatOnce === "function") {
      try {
        const result = await this.api.runtime.system.runHeartbeatOnce(options);
        if (result?.status === "ran") {
          this.api.logger.info("AgentBridge completion heartbeat ran immediately");
          return;
        }
        this.api.logger.warn(
          `AgentBridge immediate heartbeat did not run: ${safeCode(result?.reason || result?.status || "UNKNOWN")}`,
        );
      } catch (error) {
        this.api.logger.warn(
          `AgentBridge immediate heartbeat failed: ${safeErrorCode(error)}`,
        );
      }
    }
    this.api.runtime.system.requestHeartbeat({
      source: "hook",
      intent: "event",
      reason: options.reason,
      sessionKey,
      heartbeat: options.heartbeat,
    });
    this.api.logger.info("AgentBridge completion heartbeat queued as fallback");
  }

  prune() {
    this.pruneToolBindings();
    this.prunePreparedDocumentReceipts();
    this.prunePlanningRepairs();
    const continuationCutoff = this.now() - LOGIN_CONTINUATION_TTL_MS;
    for (const [sessionKey, continuation] of this.loginContinuations) {
      if (continuation.capturedAt <= continuationCutoff) {
        this.loginContinuations.delete(sessionKey);
      }
    }
    for (const [sessionKey, message] of this.recentUserMessages) {
      if (message.capturedAt <= continuationCutoff) {
        this.recentUserMessages.delete(sessionKey);
      }
    }
    for (const [sessionKey, continuation] of this.taskContinuations) {
      if (continuation.expiresAt <= this.now()) {
        this.taskContinuations.delete(sessionKey);
      }
    }
    for (const [sessionKey, choice] of this.taskContinuationChoices) {
      if (choice.expiresAt <= this.now()) {
        this.taskContinuationChoices.delete(sessionKey);
      }
    }
    for (const [interactionId, record] of this.records) {
      if (isInteractionExpired(record.interaction, this.now())) {
        this.abortControllers.get(interactionId)?.abort();
        this.records.delete(interactionId);
        this.resumeClaims.delete(interactionId);
      }
    }
    while (this.records.size > MAX_INTERACTIONS) {
      const oldest = this.records.keys().next().value;
      if (!oldest) {
        break;
      }
      this.abortControllers.get(oldest)?.abort();
      this.records.delete(oldest);
      this.resumeClaims.delete(oldest);
    }
    const terminalTaskCutoff = this.now() - TERMINAL_TASK_TTL_MS;
    for (const [taskId, terminal] of this.terminalTasks) {
      if (terminal.capturedAt <= terminalTaskCutoff) {
        this.terminalTasks.delete(taskId);
      }
    }
    while (this.terminalTasks.size > MAX_TERMINAL_TASKS) {
      const oldest = this.terminalTasks.keys().next().value;
      if (!oldest) {
        break;
      }
      this.terminalTasks.delete(oldest);
    }
  }

  prunePlanningRepairs() {
    const current = this.now();
    for (const [sessionKey, repair] of this.planningRepairs) {
      if (repair.expiresAt <= current) {
        this.planningRepairs.delete(sessionKey);
      }
    }
  }

  isTaskTerminal(taskId) {
    return Boolean(taskId && this.terminalTasks.has(taskId));
  }

  markTaskTerminal(taskId, status = "terminal") {
    const normalizedTaskId = safeRoutePart(taskId);
    if (!normalizedTaskId) {
      return false;
    }
    this.terminalTasks.set(normalizedTaskId, {
      status: safeRoutePart(status) || "terminal",
      capturedAt: this.now(),
    });
    for (const [interactionId, record] of this.records) {
      if (record.taskId !== normalizedTaskId) {
        continue;
      }
      this.abortControllers.get(interactionId)?.abort();
      this.records.delete(interactionId);
      this.resumeClaims.delete(interactionId);
    }
    for (const [sessionKey, binding] of this.independentTaskBindings) {
      if (binding.taskId === normalizedTaskId) {
        this.independentTaskBindings.delete(sessionKey);
      }
    }
    return true;
  }

  prunePreparedDocumentReceipts() {
    const cutoff = this.now() - PREPARED_DOCUMENT_RECEIPT_TTL_MS;
    for (const [key, receipt] of this.documentDeliveryReceipts) {
      if (receipt.capturedAt <= cutoff) {
        this.documentDeliveryReceipts.delete(key);
      }
    }
    while (
      this.documentDeliveryReceipts.size > MAX_PREPARED_DOCUMENT_RECEIPTS
    ) {
      const oldest = this.documentDeliveryReceipts.keys().next().value;
      if (!oldest) {
        break;
      }
      this.documentDeliveryReceipts.delete(oldest);
    }
  }

  takeToolBinding(toolCallId) {
    const normalized = normalizeToolCallId(toolCallId);
    if (!normalized) {
      return null;
    }
    const binding = this.toolBindings.get(normalized) || null;
    this.toolBindings.delete(normalized);
    return binding;
  }

  pruneToolBindings() {
    const cutoff = this.now() - TOOL_BINDING_TTL_MS;
    for (const [toolCallId, binding] of this.toolBindings) {
      if (binding.capturedAt <= cutoff) {
        this.toolBindings.delete(toolCallId);
      }
    }
    while (this.toolBindings.size > MAX_TOOL_BINDINGS) {
      const oldest = this.toolBindings.keys().next().value;
      if (!oldest) {
        break;
      }
      this.toolBindings.delete(oldest);
    }
  }
}

function normalizeSmartlightAlarmListArguments(params, message) {
  const normalized =
    params && typeof params === "object" && !Array.isArray(params)
      ? { ...params }
      : {};
  const text = safeMessageText(message, 1000);
  if (!text) {
    return normalized;
  }
  const explicitActivity =
    /last_activity/iu.test(text) ||
    /(?:最近|最新|最后|末次).{0,8}(?:活动|变化|更新|处理)/u.test(text) ||
    /(?:活动|变化|更新|处理).{0,8}(?:时间|最近|最新|最后|末次)/u.test(text);
  if (explicitActivity) {
    normalized.sort_by = "last_activity";
    return normalized;
  }
  const explicitOccurrence =
    /occurred_at/iu.test(text) ||
    /(?:首次|最近|最新).{0,6}发生/u.test(text) ||
    /发生时间/u.test(text);
  const genericLatest =
    /(?:最近|最新).{0,8}(?:RTU)?告警/u.test(text) ||
    /(?:最近|最新).{0,6}(?:一|1)条/u.test(text);
  if (explicitOccurrence || genericLatest) {
    normalized.sort_by = "occurred_at";
  }
  return normalized;
}

function isSimpleReadSuccessNotification(notification) {
  return (
    notification?.event?.eventType === "task.operation.succeeded" &&
    notification?.event?.payload?.capabilityEffect === "read"
  );
}

function safeMessageText(value, maximum) {
  if (typeof value !== "string" && typeof value !== "number") {
    return null;
  }
  const normalized = String(value).trim();
  return normalized ? normalized.slice(0, maximum) : null;
}

export function presentationForRecords(interactions, channel) {
  return buildPresentation(interactions, channel);
}

function interactionDeadline(interaction) {
  if (!interaction.expiresAt) {
    return null;
  }
  const value = Date.parse(interaction.expiresAt);
  return Number.isFinite(value) ? value : null;
}

function trustedAgentBridgeStructuredContent(result, serverName) {
  const details = result?.details;
  if (
    !details ||
    typeof details !== "object" ||
    Array.isArray(details) ||
    details.mcpServer !== serverName ||
    typeof details.mcpTool !== "string" ||
    !details.mcpTool.trim()
  ) {
    return null;
  }
  const structuredContent =
    result?.structuredContent ??
    details?.structuredContent ??
    structuredContentFromToolText(result?.content);
  if (
    !structuredContent ||
    typeof structuredContent !== "object" ||
    Array.isArray(structuredContent)
  ) {
    return null;
  }
  return structuredContent;
}

function structuredContentFromToolText(content) {
  if (!Array.isArray(content)) {
    return null;
  }
  for (const item of content) {
    if (item?.type !== "text" || typeof item.text !== "string") {
      continue;
    }
    try {
      const parsed = JSON.parse(item.text);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed;
      }
    } catch {
      // Only exact JSON tool payloads are eligible for the trusted fallback.
    }
  }
  return null;
}

function trustedLoginStartedResult(originalResult, loginTool) {
  const details = originalResult?.details;
  const trustedDetails =
    details && typeof details === "object" && !Array.isArray(details)
      ? {
          mcpServer: details.mcpServer,
          mcpTool: details.mcpTool,
          ...(typeof details.agentbridgeTaskId === "string"
            ? { agentbridgeTaskId: details.agentbridgeTaskId }
            : {}),
        }
      : undefined;
  return {
    content: [
      {
        type: "text",
        text: [
          "AgentBridge has already opened the trusted login card for this read request.",
          "Briefly ask the user to complete the displayed card; the original read will continue automatically after successful login.",
          "Do not call the login tool again, ask whether to open a card, or request credentials in chat.",
          `Internal login tool: ${loginTool}.`,
        ].join(" "),
      },
    ],
    ...(trustedDetails ? { details: trustedDetails } : {}),
    isError: false,
  };
}

function planningRepairRequiredResult(originalResult, payload) {
  return {
    ...originalResult,
    content: [
      {
        type: "text",
        text: [
          "AgentBridge requires this turn to continue as one durable composed task plan.",
          "Do not retry the blocked business capability or synthesize from earlier conversation data.",
          "Call agentbridge_task_plan_catalog now, then call agentbridge_task_plan_prepare with proposal v2.",
          "For a read-only multi-source result, end with a catalog-declared result-projection transform and bind every business source into it.",
          "If plan preparation or execution fails, report that plan failure instead of reconstructing a result from operation history.",
          "A fresh planning task is reserved for this one repair.",
        ].join(" "),
      },
    ],
    structuredContent: payload,
    isError: false,
  };
}

function taskIdFromToolResult(result) {
  const taskId = result?.details?.agentbridgeTaskId;
  if (typeof taskId !== "string") {
    return null;
  }
  const normalized = taskId.trim();
  return normalized.length >= 16 && normalized.length <= 128
    ? normalized
    : null;
}

async function saveOpenClawMediaBuffer(
  buffer,
  contentType,
  _subdir,
  maximumSize,
  originalFilename,
) {
  if (!Buffer.isBuffer(buffer) || !buffer.length || buffer.length > maximumSize) {
    throw new Error("prepared document media buffer is invalid");
  }
  const stateDir = resolveOpenClawStateDir();
  const inboundDir = path.join(stateDir, "media", "inbound");
  await mkdir(inboundDir, { recursive: true, mode: 0o700 });
  const rawStem = path.basename(String(originalFilename || "agentbridge-file"), path.extname(String(originalFilename || "")));
  const stem = rawStem
    .replace(/[^\p{L}\p{N}._-]+/gu, "_")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "")
    .slice(0, 60) || "agentbridge-file";
  const extension = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "text/csv": ".csv",
  }[contentType];
  if (!extension) {
    throw new Error("prepared document content type is unsupported");
  }
  const id = `${stem}---${randomUUID()}${extension}`;
  const finalPath = path.join(inboundDir, id);
  const temporaryPath = `${finalPath}.tmp`;
  try {
    await writeFile(temporaryPath, buffer, { flag: "wx", mode: 0o600 });
    await rename(temporaryPath, finalPath);
  } catch (error) {
    await unlink(temporaryPath).catch(() => undefined);
    throw error;
  }
  return { id, path: finalPath, size: buffer.length, contentType };
}

function resolveOpenClawStateDir() {
  const home = process.env.OPENCLAW_HOME?.trim() || os.homedir();
  const expand = (value) =>
    value.startsWith("~") ? path.join(home, value.slice(1).replace(/^[\\/]+/, "")) : value;
  const stateOverride = process.env.OPENCLAW_STATE_DIR?.trim();
  if (stateOverride) {
    return path.resolve(expand(stateOverride));
  }
  const configPath = process.env.OPENCLAW_CONFIG_PATH?.trim();
  if (configPath) {
    return path.dirname(path.resolve(expand(configPath)));
  }
  return path.join(home, ".openclaw");
}

function normalizeTimelineAttachments(values, allowedOrigins) {
  if (!Array.isArray(values)) {
    return [];
  }
  return values
    .slice(0, 4)
    .map((value) => normalizeTimelineAttachment(value, allowedOrigins))
    .filter(Boolean)
    .sort((left, right) => left.ordinal - right.ordinal);
}

function normalizeTimelineAttachment(value, allowedOrigins) {
  if (!value || typeof value !== "object") {
    return null;
  }
  const filename = String(value.fileName || "").trim();
  const mediaUrl = String(value.mediaUrl || "").trim();
  if (!filename || !mediaUrl || value.type !== "image") {
    return null;
  }
  try {
    const parsed = new URL(mediaUrl);
    const trustedOrigins = new Set(allowedOrigins || []);
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      !trustedOrigins.has(parsed.origin) ||
      !/^\/media\/[A-Za-z0-9_-]{32,128}\/file$/.test(parsed.pathname) ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash
    ) {
      return null;
    }
  } catch {
    return null;
  }
  const contentType = String(value.mimeType || "").trim().toLowerCase();
  if (!["image/jpeg", "image/png", "image/webp"].includes(contentType)) {
    return null;
  }
  return {
    filename: filename.slice(0, 120),
    mediaUrl,
    contentType,
    ordinal: Number.isFinite(Number(value.ordinal))
      ? Number(value.ordinal)
      : 0,
  };
}

function normalizePreparedDocuments(payload, allowedOrigins) {
  const csv = payload?.status === "succeeded" ? payload.result : null;
  if (csv?.schemaVersion === "agentbridge.protected_csv_delivery.v1") {
    const file = csv.file;
    if (!/^[0-9a-f]{32}$/.test(csv.report_id || "") || !/^[a-z][a-z0-9_-]{0,63}-[0-9]{8}-[0-9]{6}\.csv$/.test(file?.filename || "")
        || file.content_type !== "text/csv; charset=utf-8"
        || typeof file.content_base64 !== "string" || file.content_base64.length > 13981016
        || !/^[A-Za-z0-9+/]+={0,2}$/.test(file.content_base64)) return [];
    const bytes = Buffer.from(file.content_base64, "base64");
    if (!bytes.length || bytes.length > 10485760 || bytes.toString("base64") !== file.content_base64) return [];
    return [{filename: file.filename, contentType: "text/csv", protectedCsv: true,
             contentBase64: file.content_base64, downloadId: csv.report_id, artifactId: normalizeOpaqueId(csv.artifact_id)}];
  }
  if (payload?.schemaVersion === "agentbridge.document_delivery.v1") {
    const file = normalizePreparedDocumentFile(payload.file, allowedOrigins);
    return payload.status === "succeeded" && file ? [file] : [];
  }
  if (
    payload?.schemaVersion !== "agentbridge.document_delivery_batch.v1" ||
    !["succeeded", "partial"].includes(payload.status) ||
    !Array.isArray(payload.files)
  ) {
    return [];
  }
  return payload.files
    .slice(0, 20)
    .map((file) => normalizePreparedDocumentFile(file, allowedOrigins))
    .filter(Boolean);
}

function normalizePreparedDocumentFile(file, allowedOrigins) {
  if (!file || typeof file !== "object") {
    return null;
  }
  const filename = String(file.filename || "").trim();
  const mediaUrl = String(file.mediaUrl || "").trim();
  if (!filename || !mediaUrl) {
    return null;
  }
  try {
    const parsed = new URL(mediaUrl);
    const trustedOrigins = new Set(allowedOrigins || []);
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      !trustedOrigins.has(parsed.origin) ||
      !/^\/download\/[A-Za-z0-9_-]{32,128}\/file$/.test(parsed.pathname) ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash
    ) {
      return null;
    }
  } catch {
    return null;
  }
  const contentType = String(file.contentType || "").trim().toLowerCase();
  if (
    !["application/pdf", "image/jpeg", "image/png", "text/csv"].includes(
      contentType,
    )
  ) {
    return null;
  }
  return {
    filename: filename.slice(0, 240),
    mediaUrl,
    contentType,
    artifactId: normalizeOpaqueId(file.artifactId),
    downloadId: normalizeOpaqueId(file.downloadId),
  };
}

function extractUserMessageText(event) {
  if (typeof event?.text === "string" && event.text.trim()) {
    return event.text;
  }
  if (typeof event?.content === "string") {
    return event.content;
  }
  if (!Array.isArray(event?.content)) {
    return null;
  }
  const text = event.content
    .map((item) => {
      if (typeof item === "string") {
        return item;
      }
      if (!item || typeof item !== "object") {
        return "";
      }
      if (typeof item.text === "string") {
        return item.text;
      }
      return typeof item.content === "string" ? item.content : "";
    })
    .filter(Boolean)
    .join("\n")
    .trim();
  return text || null;
}

function normalizeOpaqueId(value) {
  const normalized = typeof value === "string" ? value.trim() : "";
  return /^[A-Za-z0-9_-]{16,128}$/.test(normalized) ? normalized : null;
}

function preparedDocumentReceipt(
  file,
  state,
  attemptCount,
  errorCode,
) {
  return {
    artifactId: normalizeOpaqueId(file?.artifactId),
    downloadId: normalizeOpaqueId(file?.downloadId),
    filename: String(file?.filename || "").slice(0, 240),
    state,
    delivered: ["attachment_sent", "fallback_link_sent"].includes(state),
    attemptCount: Math.max(0, Number(attemptCount) || 0),
    errorCode: errorCode ? safeCode(errorCode) : null,
  };
}

function preparedDocumentDeliveryReport(files, receipts, override = {}) {
  const outcomes = files.map(
    (file, index) =>
      receipts[index] ||
      preparedDocumentReceipt(
        file,
        override.state || "failed",
        0,
        override.errorCode || "DELIVERY_RESULT_MISSING",
      ),
  );
  const attachmentSentCount = outcomes.filter(
    (file) => file.state === "attachment_sent",
  ).length;
  const fallbackLinkSentCount = outcomes.filter(
    (file) => file.state === "fallback_link_sent",
  ).length;
  const failedCount = outcomes.filter((file) => file.state === "failed").length;
  const deliveredCount = attachmentSentCount + fallbackLinkSentCount;
  const state =
    failedCount === 0
      ? "delivered"
      : deliveredCount > 0
        ? "partial"
        : "failed";
  const endpointMode = override.endpointMode || "native_channel";
  const parts = [`${outcomes.length} 份文件已准备`];
  if (endpointMode === "workspace_download") {
    parts.push(`当前网页任务卡已生成 ${fallbackLinkSentCount} 个下载入口`);
  } else {
    if (attachmentSentCount > 0) {
      parts.push(`${attachmentSentCount} 份已作为附件发送`);
    }
    if (fallbackLinkSentCount > 0) {
      parts.push(`${fallbackLinkSentCount} 份附件上传失败，已改发下载链接`);
    }
  }
  if (failedCount > 0) parts.push(`${failedCount} 份未能送达`);
  return {
    mode: outcomes.length === 1 ? "direct_attachment" : "direct_attachment_batch",
    oneFilePerMessage: true,
    handledByHost: true,
    endpointMode,
    channel: override.channel || "unknown",
    state,
    completionMeaning: "endpoint_delivery_reported",
    preparedCount: outcomes.length,
    attachmentSentCount,
    fallbackLinkSentCount,
    failedCount,
    files: outcomes,
    userMessage: `${parts.join("，")}。`,
  };
}

function preparedDocumentReceiptKey(sessionKey, deliveryRef, file) {
  const normalizedRef = safeRoutePart(deliveryRef);
  const fileRef =
    normalizeOpaqueId(file?.artifactId) ||
    normalizeOpaqueId(file?.downloadId);
  if (!normalizedRef || !fileRef) {
    return null;
  }
  return `${sessionKey}\u0000${normalizedRef}\u0000${fileRef}`;
}

function isRetryablePreparedDocumentDeliveryError(error) {
  const evidence = [error?.code, error?.name, error?.message]
    .filter(Boolean)
    .join(" ")
    .toUpperCase();
  return [
    "HTTPERROR",
    "FETCH FAILED",
    "NETWORK",
    "TIMEOUT",
    "ETIMEDOUT",
    "ECONNRESET",
    "ECONNREFUSED",
    "EHOSTUNREACH",
    "ENETUNREACH",
    "SOCKET",
    "429",
    "HTTP 5",
  ].some((token) => evidence.includes(token));
}

function shouldResumeOriginalRequest(record, response, nextInteractions) {
  return Boolean(
    record?.interaction?.type === "credential" &&
      nextInteractions.length === 0 &&
      safeStatus(response) === "succeeded" &&
      response?.nextAction?.type === "retry_original_request",
  );
}

function safeStatus(response) {
  const status = String(response?.status ?? "completed")
    .toLowerCase()
    .replace(/[^a-z0-9_.-]/g, "_")
    .slice(0, 80);
  return status || "completed";
}

function safeResponseErrorCode(response) {
  return response?.error?.code ? safeCode(response.error.code) : null;
}

function safeBusinessRuleMessage(response) {
  const message = response?.error?.message;
  if (typeof message !== "string") {
    return "";
  }
  return message
    .replace(/<[^>]*>/g, "")
    .replace(/https?:\/\/\S+/gi, "[\u94fe\u63a5\u5df2\u9690\u85cf]")
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 500);
}

function safeErrorCode(error) {
  return safeCode(error?.code || error?.name || "UNKNOWN_ERROR");
}

function safeCode(value) {
  return String(value)
    .toUpperCase()
    .replace(/[^A-Z0-9_.-]/g, "_")
    .slice(0, 80);
}

function safeSucceededMessage(response) {
  const batch = response?.batch || response?.result?.batch;
  if (batch?.state === "succeeded") {
    const total = Number(batch.totalCount) || 0;
    const kind = batch.capability === "oa.workflow.pending.batch.prepare" ? "待办事项" : "补签申请";
    return `OA ${kind}已全部处理完成，共 ${total} 条。`;
  }
  const result = response?.result;
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    return "AgentBridge 已完成本次安全操作。";
  }
  if (result.meeting_created === true && result.meeting_sent === true) {
    return "OA 会议已创建并发送。";
  }
  if (
    result.business_intent === "submit_business_trip_request" &&
    result.workflow_submitted === true
  ) {
    return "OA 出差申请已提交审批。";
  }
  if (
    result.business_intent === "submit_leave_request" &&
    result.workflow_submitted === true
  ) {
    return "OA 请假申请已提交审批。";
  }
  if (
    result.business_intent === "revoke_sent_workflow" &&
    result.workflow_revoked === true
  ) {
    return "OA 已发流程已撤销。";
  }
  if (result.pending_action_processed === true) {
    const subjects = {
      efficiency_data: "OA \u6548\u80fd\u6570\u636e\u6d41\u7a0b",
      travel_expense: "OA \u5dee\u65c5\u8d39\u5ba1\u6279\u62a5\u9500\u5355",
      business_trip: "OA \u51fa\u5dee\u7533\u8bf7\u5355",
      labor_contract_renewal: "OA \u52b3\u52a8\u5408\u540c\u7eed\u7b7e\u8868",
      intellectual_property_declaration: "OA \u77e5\u8bc6\u4ea7\u6743\u7533\u62a5\u5ba1\u6279\u5355",
      overtime: "OA \u52a0\u73ed\u7533\u8bf7\u5ba1\u6838\u5355",
      leave: "OA \u8bf7\u5047\u7533\u8bf7\u5355",
      resignation: "OA \u79bb\u804c\u7533\u8bf7\u5355",
      work_handover: "OA 工作交接单",
      flight_application: "OA 乘坐飞机申请单",
      attendance_confirmation: "OA \u6708\u5ea6\u8003\u52e4\u786e\u8ba4\u5355",
      weekly_report: "OA \u5468\u62a5\u53d1\u9001\u6d41\u7a0b",
      standard_collaboration: "OA \u666e\u901a\u534f\u540c\u4e8b\u9879",
    };
    const subject = subjects[result.workflow_profile] || "OA \u5f85\u529e\u4e8b\u9879";
    const action =
      result.action_kind === "acknowledgement"
        ? "\u5df2\u9605\u529e"
        : result.action_kind === "confirmation"
          ? "\u5df2\u786e\u8ba4\u5e76\u63d0\u4ea4"
          : "\u5df2\u5ba1\u6279\u901a\u8fc7";
    return `${subject}${action}\u3002`;
  }
  if (
    result.status === "created" &&
    result.workLog &&
    typeof result.workLog === "object"
  ) {
    return "泰华工作日志已提交。";
  }
  if (
    result.status === "updated" &&
    result.alarm &&
    typeof result.alarm === "object"
  ) {
    return "照明 RTU 告警备注已修改并回读确认。";
  }
  if (result.action === "submit_work_area") {
    return result.status === "already_completed"
      ? "照明 RTU 告警已经提交工区，本次未重复写入。"
      : "照明 RTU 告警已提交工区并回读确认。";
  }
  if (result.action === "revoke_work_area") {
    return result.status === "already_completed"
      ? "照明 RTU 告警当前未提交工区，本次未重复写入。"
      : "照明 RTU 告警工区提交已撤回并回读确认。";
  }
  if (result.action === "dispose") {
    return result.status === "already_completed"
      ? "照明 RTU 告警已经处置，本次未重复写入。"
      : "照明 RTU 告警已标记为已处置并回读确认。";
  }
  if (result.draft_saved === true && result.workflow_submitted === false) {
    return "OA 待发草稿已保存，未提交审批。";
  }
  if (result.workflow_approved === true) {
    return "OA 补签申请已审批通过。";
  }
  return "AgentBridge 已完成本次安全操作。";
}

function safeStatusMessage(status, errorCode, response = null) {
  const code = errorCode ? `（错误码：${safeCode(errorCode)}）` : "";
  const batch = response?.batch || response?.result?.batch;
  if (batch && ["failed", "outcome_unknown", "partially_succeeded", "canceled", "expired"].includes(batch.state)) {
    const done = Number(batch.succeededCount) || 0;
    const total = Number(batch.totalCount) || 0;
    const unknown = batch.state === "outcome_unknown";
    return `OA 批量处理已停止，已完成 ${done}/${total} 条。${unknown ? "当前条结果未知，请先到 OA 核对，不会自动重试。" : "后续事项未继续执行，请核对当前条的失败或取消原因。"}${code}`;
  }
  switch (safeStatus({ status })) {
    case "succeeded":
      return safeSucceededMessage(response);
    case "already_resumed":
      return "AgentBridge 已完成本次安全操作，无需重复处理。";
    case "declined":
      return "你已拒绝本次 AgentBridge 安全操作，系统未继续执行。";
    case "expired":
      return "本次 AgentBridge 安全交互已过期，请在智能体中重新发起。";
    case "superseded":
      return "本次 AgentBridge 安全交互已被新的请求替代。";
    case "completed":
      return "AgentBridge 已收到安全页面的处理结果。";
    case "poll_expired":
      return "AgentBridge 等待安全交互完成已超时，请在智能体中重新发起。";
    case "poll_failed":
      return `AgentBridge 暂时无法查询安全交互状态${code}。`;
    case "resume_failed":
      return `AgentBridge 未能继续执行本次安全操作${code}。`;
    case "unknown":
      if (safeCode(errorCode) === "RESULT_UNKNOWN") {
        return "业务系统写操作的最终结果未能确认。AgentBridge 已停止且不会自动重试，请先到对应业务系统中核对实际结果后再决定下一步（错误码：RESULT_UNKNOWN）。";
      }
      return "AgentBridge 无法确认本次安全操作的最终状态" + code + "。";
    case "failed":
      if (
        [
          "OA_BUSINESS_RULE_REJECTED",
          "TAIHUA_BUSINESS_RULE_REJECTED",
          "SMARTLIGHT_BUSINESS_RULE_REJECTED",
        ].includes(
          safeCode(errorCode),
        )
      ) {
        const reason = safeBusinessRuleMessage(response);
        const normalizedCode = safeCode(errorCode);
        const taihua = normalizedCode === "TAIHUA_BUSINESS_RULE_REJECTED";
        const smartlight = normalizedCode === "SMARTLIGHT_BUSINESS_RULE_REJECTED";
        const system = smartlight ? "照明系统" : taihua ? "泰华日志系统" : "OA";
        const action = smartlight ? "RTU 告警操作" : taihua ? "工作日志" : "申请";
        return reason
          ? `${system} 未提交本次${action}：${reason}${code}。`
          : `${system} 根据业务规则拒绝了本次${action}${code}。`;
      }
      return `AgentBridge 未能完成本次安全操作${code}。`;
    default:
      return `AgentBridge 安全交互状态已更新：${safeCode(status)}${code}。`;
  }
}

function isPullBasedEndpoint(endpoint, route, binding) {
  const clientType = String(endpoint?.clientType || "").trim().toLowerCase();
  const channel = String(
    route?.channel || binding?.channel || "",
  ).trim().toLowerCase();
  return clientType === "web" || PULL_BASED_CHANNELS.has(channel);
}

function isActivityGatedDirectEndpoint(endpoint, route, binding) {
  const clientType = String(endpoint?.clientType || "").trim().toLowerCase();
  const channel = String(
    route?.channel || binding?.channel || "",
  ).trim().toLowerCase();
  return [clientType, channel].some((value) =>
    ["openclaw-weixin", "wechat", "weixin"].includes(value),
  );
}

function normalizeToolCallId(value) {
  if (typeof value !== "string") {
    return null;
  }
  const normalized = value.trim();
  return normalized ? normalized.slice(0, 256) : null;
}

function safeRoutePart(value) {
  if (typeof value !== "string" && typeof value !== "number") {
    return null;
  }
  const normalized = String(value).trim();
  return normalized ? normalized.slice(0, 512) : null;
}

function isWorkspaceSessionKey(sessionKey) {
  return Boolean(
    typeof sessionKey === "string" &&
      /^agent:[^:]+:agentbridge-workspace:direct:/i.test(sessionKey.trim()),
  );
}

function normalizeThreadId(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  return safeRoutePart(value);
}

function defaultSleep(milliseconds, signal) {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(resolve, milliseconds);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

function backgroundSleep(milliseconds, signal) {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(resolve, milliseconds);
    timer.unref?.();
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

export function notificationPumpDelay(
  intervalMs,
  idleRounds,
  maximumMs = MAX_NOTIFICATION_IDLE_INTERVAL_MS,
) {
  const base = Math.max(250, Number(intervalMs) || 0);
  const maximum = Math.max(base, Number(maximumMs) || base);
  const exponent = Math.min(Math.max(Number(idleRounds) - 1, 0), 8);
  return Math.min(maximum, base * 2 ** exponent);
}
