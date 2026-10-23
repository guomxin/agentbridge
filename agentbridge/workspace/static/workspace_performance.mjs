// Diagnostic marks contain identifiers and clocks only; never messages or fields.
const STAGES = new Set(["send", "accepted", "first_tool", "first_text", "host_terminal", "card_rendered", "timeline_received", "chat_loaded",
  "selection_sent", "selection_queued", "batch_selection_sent", "batch_selection_queued", "quick_query_sent", "quick_query_queued",
  "recovery_probe_sent", "recovery_probe_received", "recovery_prepare_sent", "recovery_prepare_received", "form_opened"]);
const dataKey = stage => `timing${stage.replace(/(^|_)([a-z])/g, (_, _sep, c) => c.toUpperCase())}`;
export function clearWorkspaceTimings(clock = globalThis.performance) {
  for (const stage of STAGES) {
    try { clock?.clearMarks?.(`agentbridge.workspace.${stage}`); } catch {}
    const root = globalThis.document?.documentElement;
    if (root?.dataset) delete root.dataset[dataKey(stage)];
  }
}
export function markWorkspaceTiming(stage, identifiers = {}, clock = globalThis.performance) {
  if (!STAGES.has(stage) || !clock?.mark) return;
  const detail = { clockSource: "browser.monotonic", observedAt: Date.now() };
  for (const key of ["requestId", "runId", "taskId", "version", "status"]) {
    const value = identifiers[key];
    if ((typeof value === "string" && /^[A-Za-z0-9_.:-]{1,256}$/.test(value)) ||
        (typeof value === "number" && Number.isFinite(value))) detail[key] = value;
  }
  try {
    const name = `agentbridge.workspace.${stage}`;
    // Only the latest mark per stage survives long sessions.
    clock.clearMarks?.(name);
    clock.mark(name, {detail});
    // A DOM-backed diagnostic is also available to restricted browser tooling.
    const root = globalThis.document?.documentElement;
    if (root?.dataset) root.dataset[dataKey(stage)] = JSON.stringify(detail);
  } catch { /* Unsupported performance APIs cannot interrupt the application. */ }
}
