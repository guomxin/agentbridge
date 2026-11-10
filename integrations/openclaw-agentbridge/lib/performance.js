import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";
import { performance } from "node:perf_hooks";

export const TIMING_META_KEY = "io.agentbridge/timing";
const callContext = new AsyncLocalStorage();
const MAX_IDENTITIES = 256;
const MAX_EVENTS_PER_IDENTITY = 256;
const MAX_RUNS = 1024;
const MAX_AGE_MS = 30 * 60_000;
const SYNC_STAGES = new Set(["host.before_tool_call", "host.tool_factory"]);
const STAGES = new Set([
  "host.prompt_build", "host.model_started", "host.model_call",
  "host.llm_input", "host.llm_output", "host.tool_call",
  "host.mcp_request", "host.mcp_transport", "host.agent_end",
  "host.reply_sending",
  ...SYNC_STAGES,
]);
const IDS = ["requestId", "dispatchId", "runId", "taskId", "toolCallId", "operationId", "callId"];
const LABELS = ["toolName", "provider", "model"];
const NUMBERS = ["durationMs", "timeToFirstByteMs", "requestPayloadBytes", "responseStreamBytes",
  "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "attempt"];

// No prompt, tool arguments, response body, session key or endpoint is accepted
// by the event serializer. A full queue drops telemetry, never business work.
export function createHostPerformance({ identityRouter, sharedState = {},
  now = () => Date.now(), monotonic = () => performance.now() } = {}) {
  const state = sharedState.performanceState ||= {
    queues: new Map(), turns: new Map(), runs: new Map(), dropped: 0,
  };

  function trim() {
    const cutoff = now() - MAX_AGE_MS;
    for (const map of [state.turns, state.runs]) {
      for (const [key, value] of map) if (value.at < cutoff) map.delete(key);
      while (map.size > MAX_RUNS) map.delete(map.keys().next().value);
    }
  }

  function contextFor(context = {}) {
    trim();
    const sessionKey = context.sessionKey;
    const runId = safeId(context.runId);
    const runKey = JSON.stringify([sessionKey, runId]);
    let run = runId ? state.runs.get(runKey) : null;
    if (!run && runId) {
      run = { requestId: safeId(context.requestId) || state.turns.get(sessionKey)?.requestId, at: now() };
      state.runs.set(runKey, run);
      while (state.runs.size > MAX_RUNS) state.runs.delete(state.runs.keys().next().value);
    }
    return { sessionKey, requestId: run?.requestId || (!runId ? safeId(context.requestId) || state.turns.get(sessionKey)?.requestId : undefined),
      runId, toolCallId: safeId(context.toolCallId), taskId: safeId(context.taskId),
      toolName: safeLabel(context.toolName) };
  }

  function enqueue(stage, details, current, bindingKey) {
    const event = { eventId: randomUUID(), stage,
      kind: Object.hasOwn(details, "durationMs") ? "span" : "event",
      observedAt: new Date(now()).toISOString() };
    if (event.kind === "span") event.durationClock = details.durationClock === "host.wall" ? "host.wall" : "host.monotonic";
    for (const field of IDS) { const value = safeId(current[field]); if (value) event[field] = value; }
    for (const field of LABELS) { const value = safeLabel(current[field]); if (value) event[field] = value; }
    for (const field of NUMBERS) {
      const value = current[field];
      if (Number.isFinite(value) && value >= 0 && value <= 1e12) event[field] = Math.round(value * 1000) / 1000;
    }
    if (["started", "completed", "error", "canceled"].includes(current.outcome)) event.outcome = current.outcome;
    let queue = state.queues.get(bindingKey);
    if (!queue && state.queues.size < MAX_IDENTITIES) state.queues.set(bindingKey, queue = []);
    if (!queue || queue.length >= MAX_EVENTS_PER_IDENTITY) { state.dropped += 1; return; }
    queue.push(event);
    try { state.onPending?.(); } catch { /* Reporting never affects execution. */ }
  }

  function record(stage, details = {}, context = {}) {
    try {
    // Synchronous phases must use startSync: the legacy context resolver can
    // inherit the newest turn, and cannot attest identity at both boundaries.
    if (!STAGES.has(stage) || SYNC_STAGES.has(stage)) return;
    const current = { ...contextFor(context), ...details };
    if (!current.requestId && !current.dispatchId && !current.runId) return;
    const bindingKey = identityRouter?.sessionBindings?.get(current.sessionKey)
      || identityRouter?.resolveToolContext?.({ sessionKey: current.sessionKey })?.binding?.key;
    if (!bindingKey || bindingKey === "conflict") return;
    enqueue(stage, details, current, bindingKey);
    } catch { /* An observer must never replace a business result or error. */ }
  }

  function strictContext(context) {
    if (!context || typeof context.sessionKey !== "string" || !context.sessionKey || !safeId(context.runId)) return null;
    const value = { sessionKey: context.sessionKey, runId: context.runId };
    for (const field of IDS) {
      if (context[field] == null) continue;
      const id = safeId(context[field]);
      if (!id) return null;
      value[field] = id;
    }
    const toolName = safeLabel(context.toolName);
    if (toolName) value.toolName = toolName;
    return value;
  }

  function strictIdentity(sessionKey) {
    const pinned = identityRouter?.sessionBindings?.get(sessionKey);
    if (pinned === "conflict") return null;
    const identity = identityRouter?.resolveToolContext?.({ sessionKey });
    const key = identity?.bound && identity.binding?.key;
    if (typeof key !== "string" || !key || key === "conflict" || (pinned && pinned !== key)) return null;
    const namespace = identityRouter.catalogCacheKeyForBinding
      ? identityRouter.catalogCacheKeyForBinding(identity.binding) : key;
    if (!namespace) return null;
    return { key, namespace, endpoint: identityRouter.endpointKeyForSession?.(sessionKey) ?? null };
  }

  function startSync(context, stage) {
    try {
      if (!SYNC_STAGES.has(stage)) return null;
      // Never resolve through contextFor or state.turns. Missing run correlation
      // is a missing sample, rather than evidence for the newest user request.
      const captured = strictContext(context);
      const identity = captured && strictIdentity(captured.sessionKey);
      const started = monotonic();
      if (!captured || !identity || !Number.isFinite(started)) return null;
      let finished = false;
      return (outcome, finalContext) => {
        if (finished) return;
        finished = true;
        try {
          const current = strictContext(finalContext);
          const owner = current && strictIdentity(current.sessionKey);
          if (!current || !owner || JSON.stringify(current) !== JSON.stringify(captured)
              || JSON.stringify(owner) !== JSON.stringify(identity)) return;
          const elapsed = monotonic() - started;
          if (!Number.isFinite(elapsed) || elapsed < 0) return;
          const details = { durationMs: elapsed, outcome };
          enqueue(stage, details, { ...captured, ...details }, identity.key);
        } catch { /* Lost diagnostic context or clocks never change execution. */ }
      };
    } catch { return null; }
  }

  return {
    bindTurn(sessionKey, requestId) {
      const id = safeId(requestId);
      if (typeof sessionKey === "string" && id) {
        trim(); state.turns.set(sessionKey, { requestId: id, at: now() });
        while (state.turns.size > MAX_RUNS) state.turns.delete(state.turns.keys().next().value);
      }
    },
    forgetSession(sessionKey) { state.turns.delete(sessionKey); },
    onPending(callback) { state.onPending = callback; },
    record,
    startSync,
    async run(context, callback, stage = null) {
      const resolved = contextFor(context);
      const start = monotonic();
      let outcome = "completed";
      return callContext.run({ recorder: this, context: resolved }, async () => {
        try { return await callback(); }
        catch (error) { outcome = "error"; throw error; }
        finally { if (stage) record(stage, { durationMs: Math.max(monotonic() - start, 0), outcome }, resolved); }
      });
    },
    take(bindingKey, limit = 32) {
      const queue = state.queues.get(bindingKey);
      const result = queue?.splice(0, Math.max(0, Math.min(limit, 32))) || [];
      if (queue?.length === 0) state.queues.delete(bindingKey);
      return result;
    },
    // Reporting failures drop the bounded batch instead of replaying stale data.
    status() { return { queued: [...state.queues.values()].reduce((sum, list) => sum + list.length, 0), dropped: state.dropped }; },
  };
}

// Prepare/finalize observation outside the callback's exception handling. A
// broken recorder must never cause a fallback that executes business twice.
export function observeHostSync(recorder, context, callback, stage, finalContext = context) {
  let finish;
  try { finish = recorder?.startSync?.(typeof context === "function" ? context() : context, stage); } catch {}
  let outcome = "completed";
  try { return callback(); }
  catch (error) { outcome = "error"; throw error; }
  finally {
    try {
      if (typeof finish === "function") finish(outcome, typeof finalContext === "function" ? finalContext() : finalContext);
    } catch { /* Best effort, including custom recorders supplied by the host. */ }
  }
}

export function timingContext() { return callContext.getStore(); }

export function timingMetadata(meta) {
  const context = timingContext()?.context;
  if (!context) return meta;
  const value = {};
  for (const field of ["requestId", "dispatchId", "runId", "taskId", "toolCallId"]) {
    const id = safeId(context[field]); if (id) value[field] = id;
  }
  const taskId = safeId(meta?.["io.agentbridge/task"]?.taskId);
  if (taskId) value.taskId = taskId;
  return Object.keys(value).length ? { ...meta, [TIMING_META_KEY]: value } : meta;
}

export function recordCurrentTiming(stage, details = {}, overrides = {}) {
  const scope = timingContext();
  try { scope?.recorder.record(stage, details, { ...scope.context, ...overrides }); } catch { /* Best effort. */ }
}

function safeId(value) {
  return typeof value === "string" && /^[A-Za-z0-9_.:@/-]{1,256}$/u.test(value) ? value : undefined;
}
function safeLabel(value) {
  return typeof value === "string" && /^[A-Za-z0-9_.:/-]{1,160}$/u.test(value) ? value : undefined;
}
