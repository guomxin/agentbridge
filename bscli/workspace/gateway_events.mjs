const MAX_CHAT_TEXT = 50_000;
const MAX_PROGRESS_TEXT = 2_000;

export function normalizeGatewayEvent(
  frame,
  expectedSessionKey,
  expectedRunId = null,
) {
  if (
    !isRecord(frame) ||
    frame.type !== "event" ||
    !["agent", "chat"].includes(frame.event) ||
    !isRecord(frame.payload) ||
    frame.payload.sessionKey !== expectedSessionKey ||
    (expectedRunId && frame.payload.runId !== expectedRunId)
  ) {
    return null;
  }
  return frame.event === "chat"
    ? normalizeChatEvent(frame.payload)
    : normalizeAgentEvent(frame.payload);
}

function normalizeChatEvent(payload) {
  const state = boundedChoice(
    payload.state,
    ["delta", "final", "error", "aborted"],
  );
  const runId = boundedText(payload.runId, 256);
  if (!state || !runId) {
    return null;
  }
  const messageText = messageTextFrom(payload.message);
  const errorText =
    state === "error" ? boundedText(payload.errorMessage, 2_000) : null;
  const text = boundedText(messageText || errorText, MAX_CHAT_TEXT);
  if (!text && state === "delta") {
    return null;
  }
  return {
    type: "chat",
    runId,
    state,
    ...(Number.isSafeInteger(payload.seq) ? { seq: payload.seq } : {}),
    ...(text ? { text } : {}),
    ...(state === "final" ? { replace: true } : {}),
    ...(state !== "final" && typeof payload.replace === "boolean"
      ? { replace: payload.replace }
      : {}),
  };
}

function normalizeAgentEvent(payload) {
  const runId = boundedText(payload.runId, 256);
  const stream = boundedChoice(
    payload.stream,
    ["item", "tool", "lifecycle"],
  );
  const data = isRecord(payload.data) ? payload.data : {};
  if (!runId || !stream) {
    return null;
  }
  if (stream === "item" && data.kind === "preamble") {
    const text = boundedText(data.progressText, MAX_PROGRESS_TEXT);
    return text
      ? {
          type: "progress",
          runId,
          kind: "preamble",
          text,
        }
      : null;
  }
  if (stream === "tool") {
    const phase = boundedChoice(data.phase, ["start", "update", "result"]);
    if (!phase) {
      return null;
    }
    return {
      type: "progress",
      runId,
      kind: "tool",
      phase,
      label: toolLabel(data.name),
    };
  }
  if (stream === "lifecycle") {
    const phase = boundedChoice(
      data.phase,
      ["start", "end", "error", "aborted"],
    );
    return phase
      ? {
          type: "progress",
          runId,
          kind: "lifecycle",
          phase,
          label: lifecycleLabel(phase),
        }
      : null;
  }
  return null;
}

function messageTextFrom(message) {
  if (!isRecord(message) || message.role !== "assistant") {
    return "";
  }
  if (typeof message.content === "string") {
    return message.content;
  }
  if (!Array.isArray(message.content)) {
    return "";
  }
  return message.content
    .filter(
      (item) =>
        isRecord(item) &&
        ["text", "input_text", "output_text"].includes(item.type) &&
        typeof item.text === "string",
    )
    .map((item) => item.text)
    .join("\n");
}

function toolLabel(value) {
  const name = boundedText(value, 160)?.toLowerCase() || "";
  if (name === "agentbridge_identity_status") {
    return "正在确认 AgentBridge 用户身份";
  }
  if (name === "oa_session_status") {
    return "正在检查 OA 登录状态";
  }
  if (name.startsWith("oa_")) {
    return "正在调用 OA 能力";
  }
  if (name.startsWith("taihua_")) {
    return "正在调用日志系统";
  }
  if (name.startsWith("yuque_")) {
    return "正在查询部门信息库";
  }
  if (name.startsWith("agentbridge_host_")) {
    return "正在同步任务状态";
  }
  return "正在调用业务能力";
}

function lifecycleLabel(phase) {
  return {
    start: "智能体开始处理",
    end: "智能体处理完成",
    error: "智能体处理失败",
    aborted: "智能体处理已停止",
  }[phase];
}

function boundedChoice(value, choices) {
  return typeof value === "string" && choices.includes(value) ? value : null;
}

function boundedText(value, maximum) {
  if (typeof value !== "string" && typeof value !== "number") {
    return null;
  }
  const normalized = String(value).replaceAll("\0", "").trim();
  return normalized ? normalized.slice(0, maximum) : null;
}

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
