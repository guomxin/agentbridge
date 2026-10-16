import { createHash } from "node:crypto";


export function sessionRunState(payload, sessionKey, runId = null) {
  const sessions = Array.isArray(payload?.sessions) ? payload.sessions : [];
  const session = sessions.find((item) => item?.key === sessionKey) ?? null;
  if (!session) {
    return {
      known: false,
      active: false,
      currentRunActive: false,
      activeRunIds: [],
    };
  }
  const activeRunIds = Array.isArray(session.activeRunIds)
    ? session.activeRunIds
        .filter((value) => typeof value === "string" && value.trim())
        .map((value) => value.trim())
    : [];
  return {
    known: true,
    active: session.hasActiveRun === true || activeRunIds.length > 0,
    currentRunActive: Boolean(runId && activeRunIds.includes(runId)),
    activeRunIds,
  };
}

export function recoveryIdempotencyKey(baseKey, attempt) {
  const normalized = String(baseKey || "workspace-run").trim();
  const suffix = `-recovery-${attempt}-${createHash("sha256")
    .update(`${normalized}:${attempt}`)
    .digest("hex")
    .slice(0, 12)}`;
  return `${normalized.slice(0, Math.max(1, 128 - suffix.length))}${suffix}`;
}

export function canRecoverStartup({
  progressObserved,
  toolActivity,
  recoveryUsed,
}) {
  return (
    !progressObserved &&
    !toolActivity &&
    !recoveryUsed
  );
}

export function historyEvidenceForRun(payload, runId, sessionKey = null) {
  const empty = () => ({ prompt_observed: false, had_tool_activity: false,
    final_text: "", observed_run_id: "" });
  const messages = payload?.messages;
  if (!Array.isArray(messages) || (sessionKey && payload.sessionKey != null && payload.sessionKey !== "" && payload.sessionKey !== sessionKey)) return empty();
  let anchor = -1;
  let observedRunId = "";
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!message || message.role !== "user") continue;
    if (message.idempotencyKey === `${runId}:user` || message.runId === runId) {
      // runId here is an OpenClaw transcript run, never a native SDK turnId.
      if (message.runId != null && message.runId !== "" && message.runId !== runId) return empty();
      anchor = index;
      observedRunId = message.runId || runId;
      break;
    }
  }
  if (anchor < 0) return empty();
  let hadToolActivity = false;
  let finalText = "";
  for (const message of messages.slice(anchor + 1)) {
    if (!message || typeof message !== "object" || Array.isArray(message)) continue;
    if (message.role === "user") break;
    if (message.runId != null && message.runId !== "" && message.runId !== runId) continue;
    const tool = ["tool", "toolResult"].includes(message.role) || typeof message.toolCallId === "string"
      || typeof message.toolName === "string" || (Array.isArray(message.content) && message.content.some(item =>
        item && ["tool", "toolCall", "toolResult", "tool_use", "tool_result"].includes(item.type)));
    hadToolActivity ||= tool;
    if (tool) finalText = "";
    const stop = Object.hasOwn(message, "stopReason") ? message.stopReason : message.stop_reason;
    if (message.role === "assistant" && !tool && ["stop", "end_turn"].includes(stop)) {
      const text = typeof message.content === "string" ? message.content.trim()
        : Array.isArray(message.content) ? message.content.filter(item => item && ["text", "output_text"].includes(item.type)
          && typeof item.text === "string" && item.text.trim()).map(item => item.text.trim()).join("\n").trim() : "";
      if (text) finalText = text;
    }
  }
  return { prompt_observed: true, had_tool_activity: hadToolActivity, final_text: finalText, observed_run_id: observedRunId };
}
