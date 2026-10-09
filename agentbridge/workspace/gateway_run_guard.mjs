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
