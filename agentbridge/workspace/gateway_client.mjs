import {
  createHash,
  createPrivateKey,
  generateKeyPairSync,
  randomUUID,
  sign,
} from "node:crypto";
import {
  chmodSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname } from "node:path";
import { normalizeGatewayEvent } from "./gateway_events.mjs";
import {
  canRecoverStartup,
  recoveryIdempotencyKey,
  sessionRunState,
} from "./gateway_run_guard.mjs";


const gatewayUrl = requiredEnv("AB_GATEWAY_URL");
const gatewayToken = requiredEnv("AB_GATEWAY_TOKEN");
const identityPath = requiredEnv("AB_GATEWAY_IDENTITY_PATH");
const input = JSON.parse(readFileSync(0, "utf8"));
const timeoutMs = clampInteger(input.timeoutMs, 1_000, 300_000, 30_000);
const mode = ["stream", "send-stream"].includes(input.mode)
  ? input.mode
  : "call";
const streamingMode = mode !== "call";
const method =
  mode === "call" ? requiredText(input.method, "method", 160) : null;
const params = isRecord(input.params) ? input.params : {};
const streamSessionKey =
  streamingMode
    ? requiredText(input.sessionKey, "sessionKey", 1_024)
    : null;
const endpointKey =
  mode === "send-stream"
    ? requiredText(input.endpointKey, "endpointKey", 768)
    : null;
const grant =
  mode === "send-stream"
    ? requiredText(input.grant, "grant", 256)
    : null;
const message =
  mode === "send-stream"
    ? requiredText(input.message, "message", 20_000)
    : null;
const attachments =
  mode === "send-stream" ? normalizeChatAttachments(input.attachments) : [];
const idempotencyKey =
  mode === "send-stream"
    ? requiredText(input.idempotencyKey, "idempotencyKey", 128)
    : null;
const preflightAbort =
  mode === "send-stream" && input.preflightAbort !== false;
const acceptTimeoutMs =
  mode === "send-stream"
    ? clampInteger(input.acceptTimeoutMs, 5_000, 60_000, 20_000)
    : timeoutMs;
const startupProgressTimeoutMs =
  mode === "send-stream"
    ? clampInteger(input.startupProgressTimeoutMs, 5_000, 60_000, 15_000)
    : timeoutMs;
const startupActiveGraceMs =
  mode === "send-stream"
    ? clampInteger(input.startupActiveGraceMs, 15_000, 240_000, 90_000)
    : timeoutMs;
const sessionIdleTimeoutMs =
  mode === "send-stream"
    ? clampInteger(input.sessionIdleTimeoutMs, 1_000, 30_000, 15_000)
    : timeoutMs;
const sessionIdlePollMs =
  mode === "send-stream"
    ? clampInteger(input.sessionIdlePollMs, 100, 2_000, 250)
    : 250;
const identity = loadOrCreateIdentity(identityPath);
const scopes = ["operator.read", "operator.write"];

if (typeof WebSocket !== "function") {
  fail("WEBSOCKET_UNAVAILABLE", "Node.js WebSocket support is unavailable.");
}

let settled = false;
let connectSent = false;
let methodSent = false;
let connected = false;
let streamRunId = mode === "send-stream" ? idempotencyKey : null;
let streamAccepted = false;
let streamHadProgress = false;
let streamHadToolActivity = false;
let streamAcceptedAtMs = null;
let streamFirstProgressAtMs = null;
let recoveryUsed = false;
let recoveryAttempt = 0;
let currentIdempotencyKey = idempotencyKey;
let recoveredFromRunId = null;
let abortSent = false;
let abortTimer = null;
let abortPurpose = null;
let startupTimer = null;
let sessionStateTimer = null;
let sessionStateRequestId = null;
let sessionStatePurpose = null;
let recoveryEvidenceTimer = null;
let recoveryEvidenceRequestId = null;
let sessionIdleDeadline = 0;
let requestStage = "connect";
let preflightFallbackSent = false;
const connectId = `connect-${randomUUID()}`;
const preflightAbortRequestId = `preflight-abort-${randomUUID()}`;
const preflightAbortFallbackRequestId =
  `preflight-abort-fallback-${randomUUID()}`;
let bindRequestId = `bind-${randomUUID()}`;
let bindRetryCount = 0;
let bindRetryTimer = null;
let requestId = `rpc-${randomUUID()}`;
let abortRequestId = `abort-${randomUUID()}`;
const socket = new WebSocket(gatewayUrl);
let timer = setTimeout(
  () => handleRequestTimeout(),
  acceptTimeoutMs,
);

socket.addEventListener("message", (event) => {
  let frame;
  try {
    frame = JSON.parse(String(event.data));
  } catch {
    return;
  }
  if (
    frame?.type === "event" &&
    frame.event === "connect.challenge" &&
    !connectSent
  ) {
    const nonce = requiredText(frame?.payload?.nonce, "nonce", 512);
    connectSent = true;
    socket.send(
      JSON.stringify({
        type: "req",
        id: connectId,
        method: "connect",
        params: connectParams(nonce),
      }),
    );
    return;
  }
  if (frame?.type === "event" && connected && streamingMode) {
    const normalized = normalizeGatewayEvent(
      frame,
      streamSessionKey,
      streamRunId,
    );
    if (normalized) {
      if (isAbortEcho(normalized)) return;
      recordStreamProgress();
      clearStartupTimer();
      if (normalized.type === "progress" && normalized.kind === "tool") {
        streamHadToolActivity = true;
      }
      if (
        normalized.type === "chat" &&
        ["final", "error", "aborted"].includes(normalized.state)
      ) {
        if (normalized.state === "aborted") {
          normalized.hadToolActivity = streamHadToolActivity;
          normalized.safeToRetry = !streamHadToolActivity;
          normalized.text = streamHadToolActivity
            ? "智能体运行被外部停止；任务已经开始调用业务工具，请先核对业务系统状态。"
            : "智能体运行被外部停止，尚未调用业务工具，可以安全地重新发送。";
        }
      }
      process.stdout.write(`${JSON.stringify(normalized)}\n`);
      if (
        normalized.type === "chat" &&
        ["final", "error", "aborted"].includes(normalized.state)
      ) {
        finish();
      }
    }
    return;
  }
  if (frame?.type !== "res") {
    return;
  }
  if (frame.id === connectId) {
    if (!frame.ok) {
      finishError(
        safeCode(frame?.error?.details?.code || frame?.error?.code),
        safeMessage(frame?.error?.message),
        frame?.error?.details,
      );
      return;
    }
    connected = true;
    if (mode === "stream") {
      process.stdout.write(`${JSON.stringify({ type: "ready" })}\n`);
    } else if (mode === "send-stream") {
      if (preflightAbort) {
        requestPreflightAbort(true);
      } else {
        requestWorkspaceBind();
      }
    } else if (!methodSent) {
      methodSent = true;
      socket.send(
        JSON.stringify({
          type: "req",
          id: requestId,
          method,
          params,
        }),
      );
    }
    return;
  }
  if (
    mode === "send-stream" &&
    [preflightAbortRequestId, preflightAbortFallbackRequestId].includes(
      frame.id,
    )
  ) {
    if (!frame.ok) {
      const code = safeCode(
        frame?.error?.details?.code || frame?.error?.code,
      );
      if (
        frame.id === preflightAbortRequestId &&
        code === "INVALID_REQUEST" &&
        !preflightFallbackSent
      ) {
        preflightFallbackSent = true;
        requestPreflightAbort(false);
        return;
      }
      finishError(
        code,
        safeMessage(frame?.error?.message),
        stageDetails(),
      );
      return;
    }
    beginSessionIdleWait("preflight");
    return;
  }
  if (
    mode === "send-stream" &&
    sessionStateRequestId &&
    frame.id === sessionStateRequestId
  ) {
    handleSessionStateResponse(frame);
    return;
  }
  if (
    mode === "send-stream" &&
    recoveryEvidenceRequestId &&
    frame.id === recoveryEvidenceRequestId
  ) {
    handleRecoveryEvidenceResponse(frame);
    return;
  }
  if (frame.id === bindRequestId && mode === "send-stream") {
    if (!frame.ok) {
      const code = safeCode(
        frame?.error?.details?.code || frame?.error?.code,
      );
      if (code === "FORBIDDEN" && bindRetryCount < 1) {
        bindRetryCount += 1;
        requestStage = "bind_retry_wait";
        bindRetryTimer = setTimeout(() => {
          bindRetryTimer = null;
          bindRequestId = `bind-${randomUUID()}`;
          requestWorkspaceBind();
        }, 250);
        return;
      }
      finishError(
        code,
        safeMessage(frame?.error?.message),
        {
          ...(isRecord(frame?.error?.details) ? frame.error.details : {}),
          ...stageDetails(),
        },
      );
      return;
    }
    requestChatSend();
    return;
  }
  if (frame.id === abortRequestId && mode === "send-stream") {
    if (abortTimer) {
      clearTimeout(abortTimer);
      abortTimer = null;
    }
    handleAbortResponse(frame);
    return;
  }
  if (frame.id !== requestId) {
    return;
  }
  if (!frame.ok) {
    finishError(
      safeCode(frame?.error?.details?.code || frame?.error?.code),
      safeMessage(frame?.error?.message),
      frame?.error?.details,
    );
    return;
  }
  if (mode === "send-stream") {
    const payload = isRecord(frame.payload) ? frame.payload : {};
    streamRunId =
      requiredText(
        payload.runId || payload.run_id || currentIdempotencyKey,
        "runId",
        256,
      );
    streamAccepted = true;
    streamAcceptedAtMs = Date.now();
    requestStage = recoveryUsed ? "run_recovered" : "run";
    clearTimeout(timer);
    timer = setTimeout(() => handleRequestTimeout(), timeoutMs);
    armStartupTimer();
    process.stdout.write(
      `${JSON.stringify({
        type: "accepted",
        runId: streamRunId,
        status:
          typeof payload.status === "string"
            ? payload.status.slice(0, 80)
            : "accepted",
        attempt: recoveryAttempt,
        ...(recoveredFromRunId ? { recoveredFromRunId } : {}),
      })}\n`,
    );
    return;
  }
  finish({ ok: true, payload: frame.payload ?? null });
});

function handleRequestTimeout() {
  if (mode === "stream" && connected) {
    finish();
    return;
  }
  if (mode === "send-stream" && connected && streamAccepted) {
    requestStreamAbort("timeout");
    return;
  }
  finishError(
    "GATEWAY_TIMEOUT",
    "OpenClaw Gateway request timed out.",
    stageDetails(),
  );
}

function requestPreflightAbort(preserveSideRuns) {
  requestStage = "preflight_abort";
  socket.send(
    JSON.stringify({
      type: "req",
      id: preserveSideRuns
        ? preflightAbortRequestId
        : preflightAbortFallbackRequestId,
      method: "chat.abort",
      params: {
        sessionKey: streamSessionKey,
        ...(preserveSideRuns ? { preserveSideRuns: true } : {}),
      },
    }),
  );
}

function requestWorkspaceBind() {
  if (settled) return;
  requestStage = "bind";
  socket.send(
    JSON.stringify({
      type: "req",
      id: bindRequestId,
      method: "agentbridge.workspace.bind",
      params: {
        sessionKey: streamSessionKey,
        endpointKey,
        grant,
        turnRef: idempotencyKey,
        message,
      },
    }),
  );
}

function requestChatSend() {
  requestStage = recoveryUsed ? "send_accept_recovery" : "send_accept";
  requestId = `rpc-${randomUUID()}`;
  clearTimeout(timer);
  timer = setTimeout(() => handleRequestTimeout(), acceptTimeoutMs);
  socket.send(
    JSON.stringify({
      type: "req",
      id: requestId,
      method: "chat.send",
      params: {
        sessionKey: streamSessionKey,
        message,
        ...(attachments.length > 0 ? { attachments } : {}),
        deliver: false,
        idempotencyKey: currentIdempotencyKey,
        timeoutMs,
      },
    }),
  );
}

function beginSessionIdleWait(purpose) {
  clearSessionStateTimer();
  sessionStatePurpose = purpose;
  sessionIdleDeadline = Date.now() + sessionIdleTimeoutMs;
  requestSessionState();
}

function requestSessionState() {
  if (settled || !sessionStatePurpose) return;
  clearSessionStateTimer();
  requestStage =
    sessionStatePurpose === "startup_probe"
      ? "startup_probe"
      : `wait_idle_${sessionStatePurpose}`;
  sessionStateRequestId = `session-state-${randomUUID()}`;
  socket.send(
    JSON.stringify({
      type: "req",
      id: sessionStateRequestId,
      method: "sessions.list",
      params: {
        limit: 20,
        search: streamSessionKey,
        includeGlobal: true,
        includeUnknown: true,
        archived: false,
      },
    }),
  );
  sessionStateTimer = setTimeout(
    handleSessionStateTimeout,
    Math.min(5_000, sessionIdleTimeoutMs),
  );
}

function handleSessionStateResponse(frame) {
  const purpose = sessionStatePurpose;
  sessionStateRequestId = null;
  clearSessionStateTimer();
  if (!purpose) return;
  if (!frame.ok) {
    if (purpose === "startup_probe") {
      sessionStatePurpose = null;
      requestStreamAbort(
        recoveryUsed ? "startup_final" : "startup_recovery",
      );
      return;
    }
    finishError(
      "GATEWAY_SESSION_STATE_UNAVAILABLE",
      "OpenClaw session state could not be confirmed before starting the run.",
      stageDetails(),
    );
    return;
  }
  const payload = isRecord(frame.payload) ? frame.payload : {};
  const runState = sessionRunState(payload, streamSessionKey, streamRunId);
  if (purpose === "startup_probe") {
    sessionStatePurpose = null;
    handleStartupProbe(runState);
    return;
  }
  if (!runState.active) {
    sessionStatePurpose = null;
    if (purpose === "preflight") {
      requestWorkspaceBind();
    } else {
      requestRecoveryEvidence();
    }
    return;
  }
  if (Date.now() >= sessionIdleDeadline) {
    finishError(
      "GATEWAY_SESSION_NOT_IDLE",
      "The previous OpenClaw run did not release the session in time.",
      {
        ...stageDetails(),
        activeRunIds: runState.activeRunIds,
        waitPurpose: purpose,
      },
    );
    return;
  }
  sessionStateTimer = setTimeout(requestSessionState, sessionIdlePollMs);
}

function handleSessionStateTimeout() {
  sessionStateTimer = null;
  sessionStateRequestId = null;
  if (sessionStatePurpose === "startup_probe") {
    sessionStatePurpose = null;
    requestStreamAbort(
      recoveryUsed ? "startup_final" : "startup_recovery",
    );
    return;
  }
  finishError(
    "GATEWAY_SESSION_STATE_UNAVAILABLE",
    "OpenClaw session state did not respond before starting the run.",
    stageDetails(),
  );
}

function armStartupTimer(delayMs = startupProgressTimeoutMs) {
  clearStartupTimer();
  startupTimer = setTimeout(() => {
    startupTimer = null;
    if (settled || streamHadProgress || streamHadToolActivity) return;
    sessionStatePurpose = "startup_probe";
    requestSessionState();
  }, delayMs);
}

function clearStartupTimer() {
  if (startupTimer) {
    clearTimeout(startupTimer);
    startupTimer = null;
  }
}

function clearSessionStateTimer() {
  if (sessionStateTimer) {
    clearTimeout(sessionStateTimer);
    sessionStateTimer = null;
  }
}

function clearRecoveryEvidenceTimer() {
  if (recoveryEvidenceTimer) {
    clearTimeout(recoveryEvidenceTimer);
    recoveryEvidenceTimer = null;
  }
}

function handleStartupProbe(runState) {
  if (streamHadProgress || streamHadToolActivity) return;
  const acceptedElapsedMs = elapsedSince(streamAcceptedAtMs) ?? 0;
  if (
    runState.currentRunActive &&
    acceptedElapsedMs < startupActiveGraceMs
  ) {
    requestStage = recoveryUsed ? "run_recovered_queued" : "run_queued";
    process.stdout.write(
      `${JSON.stringify({
        type: "progress",
        runId: streamRunId,
        kind: "system",
        phase: "queued",
        label: "\u667a\u80fd\u4f53\u7e41\u5fd9\uff0c\u5df2\u6392\u961f",
        acceptedElapsedMs,
      })}\n`,
    );
    armStartupTimer(
      Math.max(1_000, startupActiveGraceMs - acceptedElapsedMs),
    );
    return;
  }
  if (!runState.active) {
    requestRecoveryEvidence();
    return;
  }
  const recover = canRecoverStartup({
    progressObserved: streamHadProgress,
    toolActivity: streamHadToolActivity,
    recoveryUsed,
  });
  requestStreamAbort(recover ? "startup_recovery" : "startup_final");
}

function requestRecoveryEvidence() {
  clearRecoveryEvidenceTimer();
  requestStage = "recovery_evidence";
  recoveryEvidenceRequestId = `recovery-evidence-${randomUUID()}`;
  socket.send(
    JSON.stringify({
      type: "req",
      id: recoveryEvidenceRequestId,
      method: "chat.history",
      params: {
        sessionKey: streamSessionKey,
        limit: 200,
      },
    }),
  );
  recoveryEvidenceTimer = setTimeout(
    handleRecoveryEvidenceTimeout,
    5_000,
  );
}

function handleRecoveryEvidenceResponse(frame) {
  recoveryEvidenceRequestId = null;
  clearRecoveryEvidenceTimer();
  if (!frame.ok) {
    finishRecoveryEvidenceUnavailable();
    return;
  }
  const evidence = recoveryEvidenceForRun(frame.payload, streamRunId);
  if (evidence.toolActivity) {
    streamHadToolActivity = true;
    finishError(
      "GATEWAY_START_RECOVERY_BLOCKED_TOOL_ACTIVITY",
      "The stalled OpenClaw run reached a business tool, so it was not replayed.",
      {
        ...timeoutDetails(abortSent),
        promptObserved: evidence.promptObserved,
        safeToRetry: false,
      },
    );
    return;
  }
  startRecoveryAttempt();
}

function handleRecoveryEvidenceTimeout() {
  recoveryEvidenceTimer = null;
  recoveryEvidenceRequestId = null;
  finishRecoveryEvidenceUnavailable();
}

function finishRecoveryEvidenceUnavailable() {
  finishError(
    "GATEWAY_START_RECOVERY_EVIDENCE_UNAVAILABLE",
    "The stalled OpenClaw run was stopped, but tool activity could not be verified.",
    {
      ...timeoutDetails(abortSent),
      safeToRetry: false,
    },
  );
}

function startRecoveryAttempt() {
  recoveredFromRunId = streamRunId;
  recoveryUsed = true;
  recoveryAttempt += 1;
  currentIdempotencyKey = recoveryIdempotencyKey(
    idempotencyKey,
    recoveryAttempt,
  );
  streamRunId = currentIdempotencyKey;
  streamAccepted = false;
  streamHadProgress = false;
  streamHadToolActivity = false;
  streamAcceptedAtMs = null;
  streamFirstProgressAtMs = null;
  abortSent = false;
  abortPurpose = null;
  abortRequestId = `abort-${randomUUID()}`;
  process.stdout.write(
    `${JSON.stringify({
      type: "progress",
      runId: recoveredFromRunId,
      kind: "system",
      phase: "recovery",
      label: "\u667a\u80fd\u4f53\u542f\u52a8\u5ef6\u8fdf\uff0c\u6b63\u5728\u81ea\u52a8\u6062\u590d",
      attempt: recoveryAttempt,
    })}\n`,
  );
  requestChatSend();
}

function requestStreamAbort(purpose = "timeout") {
  if (settled || abortSent) return;
  clearStartupTimer();
  clearTimeout(timer);
  abortSent = true;
  abortPurpose = purpose;
  abortRequestId = `abort-${randomUUID()}`;
  requestStage = `abort_${purpose}`;
  try {
    socket.send(
      JSON.stringify({
        type: "req",
        id: abortRequestId,
        method: "chat.abort",
        params: {
          sessionKey: streamSessionKey,
          runId: streamRunId,
        },
      }),
    );
  } catch {
    finishAbortError(false);
    return;
  }
  abortTimer = setTimeout(
    () => finishAbortError(false),
    10_000,
  );
}

function handleAbortResponse(frame) {
  if (!frame.ok) {
    finishAbortError(false);
    return;
  }
  const payload = isRecord(frame.payload) ? frame.payload : {};
  const abortConfirmed =
    payload.aborted === true &&
    (!Array.isArray(payload.runIds) || payload.runIds.includes(streamRunId));
  if (abortPurpose === "startup_recovery" && abortConfirmed) {
    beginSessionIdleWait("recovery");
    return;
  }
  finishAbortError(abortConfirmed);
}

function finishAbortError(aborted) {
  const startup = abortPurpose?.startsWith("startup_") === true;
  finishError(
    startup
      ? aborted
        ? "GATEWAY_START_STALLED_ABORTED"
        : "GATEWAY_START_STALLED_ABORT_UNCONFIRMED"
      : aborted
        ? "GATEWAY_RUN_TIMEOUT_ABORTED"
        : "GATEWAY_RUN_TIMEOUT_ABORT_UNCONFIRMED",
    startup
      ? aborted
        ? "The stalled OpenClaw startup was stopped."
        : "The stalled OpenClaw startup could not be confirmed as stopped."
      : aborted
        ? "The timed-out OpenClaw run was stopped."
        : "The timed-out OpenClaw run could not be confirmed as stopped.",
    timeoutDetails(aborted),
  );
}

function timeoutDetails(aborted) {
  return {
    runId: streamRunId,
    abortRequested: abortSent,
    aborted,
    abortPurpose,
    recoveryUsed,
    recoveryAttempt,
    hadProgress: streamHadProgress,
    hadToolActivity: streamHadToolActivity,
    safeToRetry: aborted && !streamHadToolActivity,
    acceptedElapsedMs: elapsedSince(streamAcceptedAtMs),
    firstProgressElapsedMs: elapsedSince(streamFirstProgressAtMs),
  };
}

function stageDetails() {
  return {
    stage: requestStage,
    accepted: streamAccepted,
    recoveryUsed,
    recoveryAttempt,
    hadProgress: streamHadProgress,
    hadToolActivity: streamHadToolActivity,
    acceptedElapsedMs: elapsedSince(streamAcceptedAtMs),
  };
}

function isAbortEcho(event) {
  if (!abortSent) return false;
  return (
    (event.type === "chat" && event.state === "aborted") ||
    (event.type === "progress" &&
      event.kind === "lifecycle" &&
      event.phase === "aborted")
  );
}

function recordStreamProgress() {
  streamHadProgress = true;
  if (streamFirstProgressAtMs !== null) return;
  streamFirstProgressAtMs = Date.now();
  clearTimeout(timer);
  timer = setTimeout(() => handleRequestTimeout(), timeoutMs);
}

function recoveryEvidenceForRun(payload, runId) {
  const messages = Array.isArray(payload?.messages) ? payload.messages : [];
  const promptKey = `${runId}:user`;
  let promptIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = isRecord(messages[index]) ? messages[index] : {};
    if (
      message.role === "user" &&
      (message.idempotencyKey === promptKey || message.runId === runId)
    ) {
      promptIndex = index;
      break;
    }
  }
  if (promptIndex < 0) {
    return { promptObserved: false, toolActivity: false };
  }
  for (let index = promptIndex + 1; index < messages.length; index += 1) {
    const message = isRecord(messages[index]) ? messages[index] : {};
    if (message.role === "user") break;
    if (messageHasToolActivity(message)) {
      return { promptObserved: true, toolActivity: true };
    }
  }
  return { promptObserved: true, toolActivity: false };
}

function messageHasToolActivity(message) {
  if (
    ["tool", "toolResult"].includes(message.role) ||
    typeof message.toolCallId === "string" ||
    typeof message.toolName === "string"
  ) {
    return true;
  }
  if (!Array.isArray(message.content)) return false;
  return message.content.some((item) => {
    if (!isRecord(item)) return false;
    return [
      "tool",
      "toolCall",
      "toolResult",
      "tool_use",
      "tool_result",
    ].includes(item.type);
  });
}

function elapsedSince(startedAtMs) {
  return Number.isFinite(startedAtMs)
    ? Math.max(0, Date.now() - startedAtMs)
    : null;
}

socket.addEventListener("error", () => {
  finishError(
    "GATEWAY_CONNECTION_FAILED",
    "Could not connect to the OpenClaw Gateway.",
    stageDetails(),
  );
});

socket.addEventListener("close", (event) => {
  if (!settled) {
    finishError(
      "GATEWAY_CONNECTION_CLOSED",
      safeMessage(event.reason || "OpenClaw Gateway closed the connection."),
      stageDetails(),
    );
  }
});

function connectParams(nonce) {
  const signedAt = Date.now();
  const clientId = "gateway-client";
  const clientMode = "backend";
  const role = "operator";
  const platform = "linux";
  const deviceFamily = "server";
  const payload = [
    "v3",
    identity.deviceId,
    clientId,
    clientMode,
    role,
    scopes.join(","),
    String(signedAt),
    gatewayToken,
    nonce,
    platform,
    deviceFamily,
  ].join("|");
  const signature = sign(
    null,
    Buffer.from(payload, "utf8"),
    createPrivateKey(identity.privateKeyPem),
  ).toString("base64url");
  return {
    minProtocol: 4,
    maxProtocol: 4,
    client: {
      id: clientId,
      displayName: "AgentBridge Workspace",
      version: "0.1.0",
      platform,
      deviceFamily,
      mode: clientMode,
    },
    caps: ["tool-events"],
    role,
    scopes,
    device: {
      id: identity.deviceId,
      publicKey: identity.publicKey,
      signature,
      signedAt,
      nonce,
    },
    auth: { token: gatewayToken },
    locale: "zh-CN",
    userAgent: "agentbridge-workspace/0.1.0",
  };
}

function loadOrCreateIdentity(path) {
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    if (
      parsed?.version === 1 &&
      typeof parsed.deviceId === "string" &&
      typeof parsed.publicKey === "string" &&
      typeof parsed.privateKeyPem === "string"
    ) {
      return parsed;
    }
  } catch {}
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const publicDer = publicKey.export({ type: "spki", format: "der" });
  const publicRaw = publicDer.subarray(publicDer.length - 32);
  const stored = {
    version: 1,
    deviceId: createHash("sha256").update(publicRaw).digest("hex"),
    publicKey: publicRaw.toString("base64url"),
    privateKeyPem: privateKey.export({ type: "pkcs8", format: "pem" }),
    createdAt: new Date().toISOString(),
  };
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${process.pid}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(stored)}\n`, { mode: 0o600 });
  chmodSync(temporary, 0o600);
  renameSync(temporary, path);
  return stored;
}

function finish(payload) {
  if (settled) return;
  settled = true;
  clearTimeout(timer);
  if (abortTimer) {
    clearTimeout(abortTimer);
    abortTimer = null;
  }
  clearStartupTimer();
  clearSessionStateTimer();
  clearRecoveryEvidenceTimer();
  if (bindRetryTimer) {
    clearTimeout(bindRetryTimer);
    bindRetryTimer = null;
  }
  if (streamingMode) {
    process.stdout.write(`${JSON.stringify({ type: "eof" })}\n`);
  } else {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
  }
  try {
    socket.close();
  } catch {}
}

function finishError(code, message, details = undefined) {
  const error = {
    code: safeCode(code),
    message: safeMessage(message),
    ...(isRecord(details) ? { details } : {}),
  };
  if (streamingMode) {
    if (!settled) {
      process.stdout.write(
        `${JSON.stringify({ type: "error", error })}\n`,
      );
    }
    finish();
  } else {
    finish({ ok: false, error });
  }
}

function fail(code, message) {
  process.stdout.write(
    `${JSON.stringify({ ok: false, error: { code, message } })}\n`,
  );
  process.exit(2);
}

function requiredEnv(name) {
  const value = process.env[name]?.trim();
  if (!value) fail("GATEWAY_CONFIG_INVALID", `${name} is required.`);
  return value;
}

function requiredText(value, name, maximum) {
  const normalized =
    typeof value === "string" || typeof value === "number"
      ? String(value).trim()
      : "";
  if (!normalized || normalized.length > maximum) {
    fail("GATEWAY_REQUEST_INVALID", `${name} is invalid.`);
  }
  return normalized;
}

function normalizeChatAttachments(values) {
  if (values === undefined || values === null) return [];
  if (!Array.isArray(values) || values.length > 4) {
    fail("GATEWAY_REQUEST_INVALID", "attachments are invalid.");
  }
  return values.map((value, index) => {
    if (!isRecord(value)) {
      fail("GATEWAY_REQUEST_INVALID", "attachment is invalid.");
    }
    const mimeType = requiredText(value.mimeType, "attachment mimeType", 80)
      .toLowerCase();
    if (!["image/jpeg", "image/png", "image/webp"].includes(mimeType)) {
      fail("GATEWAY_REQUEST_INVALID", "attachment mimeType is unsupported.");
    }
    return {
      type: "image",
      mimeType,
      fileName:
        requiredText(
          value.fileName || `image-${index + 1}`,
          "attachment fileName",
          120,
        ),
      content: requiredText(
        value.content,
        "attachment content",
        8_500_000,
      ),
    };
  });
}

function safeCode(value) {
  const normalized = String(value || "GATEWAY_REQUEST_FAILED")
    .toUpperCase()
    .replace(/[^A-Z0-9_.-]/g, "_")
    .slice(0, 80);
  return normalized || "GATEWAY_REQUEST_FAILED";
}

function safeMessage(value) {
  return String(value || "OpenClaw Gateway request failed.").slice(0, 500);
}

function clampInteger(value, minimum, maximum, fallback) {
  const number = Number(value);
  return Number.isInteger(number)
    ? Math.min(Math.max(number, minimum), maximum)
    : fallback;
}

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
