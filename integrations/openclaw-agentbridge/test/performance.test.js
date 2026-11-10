import test from "node:test";
import assert from "node:assert/strict";

import { createHostPerformance, observeHostSync, TIMING_META_KEY } from "../lib/performance.js";
import { createAgentBridgeMcpClient } from "../lib/mcp-client.js";
import { createHostRuntimeReporter } from "../lib/runtime-reporter.js";
import { registerAgentBridgeInteractions } from "../lib/plugin.js";
import { createInteractionSharedState } from "../lib/coordinator.js";

function recorder(options = {}) {
  return createHostPerformance({ identityRouter: {
    sessionBindings: new Map([["session-a", "identity-a"], ["session-b", "identity-b"]]),
  }, ...options });
}

test("timings freeze exact turn association per run and isolate identity queues", async () => {
  let mono = 100;
  const perf = recorder({ now: () => Date.parse("2026-10-16T04:00:00Z"), monotonic: () => mono });
  perf.bindTurn("session-a", "request-original");
  await perf.run({ sessionKey: "session-a", runId: "run-a", toolCallId: "tool-a" }, async () => { mono += 50; }, "host.tool_call");
  perf.bindTurn("session-a", "request-later");
  perf.record("host.model_call", { durationMs: 20, durationClock: "host.wall", prompt: "PRIVATE_BODY" }, { sessionKey: "session-a", runId: "run-a" });
  perf.record("host.model_started", {}, { sessionKey: "session-b", runId: "run-b" });
  const events = perf.take("identity-a");
  assert.equal(events.length, 2);
  assert.equal(events[0].requestId, "request-original");
  assert.equal(events[0].durationMs, 50);
  assert.equal(events[0].durationClock, "host.monotonic");
  assert.equal(events[1].requestId, "request-original");
  assert.equal(events[1].durationClock, "host.wall");
  assert.equal(events[0].dispatchId, undefined);
  assert.equal(perf.take("identity-b")[0].runId, "run-b");
  perf.bindTurn("session-b", "request-other-user");
  perf.record("host.model_started", {}, { sessionKey: "session-b", runId: "run-a" });
  assert.equal(perf.take("identity-b")[0].requestId, "request-other-user");
  assert.equal(/PRIVATE_BODY|session-a|identity-a/u.test(JSON.stringify(events)), false);
});

test("failed diagnostic paths preserve the exact business result or exception", async () => {
  const perf = createHostPerformance({ identityRouter: { resolveToolContext() { throw new Error("diagnostic only"); } } });
  const context = { sessionKey: "session-a", runId: "run-a" };
  const expected = new Error("business original");
  assert.equal(await perf.run(context, async () => "result", "host.prompt_build"), "result");
  await assert.rejects(perf.run(context, async () => { throw expected; }, "host.prompt_build"), error => error === expected);
  assert.equal(perf.status().queued, 0);
});

test("queue, serializer and numeric budgets reject arbitrary details without blocking", () => {
  const perf = recorder();
  const context = { sessionKey: "session-a", runId: "run-a" };
  perf.record("arbitrary stage", {}, context);
  for (let i = 0; i < 300; i += 1) perf.record("host.model_call", {
    durationMs: i, timeToFirstByteMs: Infinity, inputTokens: -1,
    provider: "openai", model: "gpt-5.6-sol", apiKey: "Bearer SECRET_TOKEN",
    prompt: "PRIVATE_BODY", outcome: "completed", operationId: "operation-1",
  }, context);
  assert.deepEqual(perf.status(), { queued: 256, dropped: 44 });
  const batch = perf.take("identity-a", 1000);
  assert.equal(batch.length, 32);
  assert.equal(batch[0].timeToFirstByteMs, undefined);
  assert.equal(batch[0].inputTokens, undefined);
  assert.equal(/PRIVATE_BODY|SECRET_TOKEN|apiKey|prompt/u.test(JSON.stringify(batch)), false);
});

test("MCP spans distinguish control transport and full retry duration without serial extra calls", async () => {
  const perf = recorder();
  perf.bindTurn("session-a", "request-a");
  const requests = [];
  const client = createAgentBridgeMcpClient({ endpoint: { url: "https://bridge.test/mcp", timeoutSeconds: 5 },
    tokenEnv: "TEST_TOKEN", env: { TEST_TOKEN: "SECRET_TOKEN" },
    fetchImpl: async (_url, options) => {
      requests.push(JSON.parse(options.body));
      if (requests.length === 1) return new Response("unavailable", { status: 503 });
      return new Response(JSON.stringify({ jsonrpc: "2.0", id: "id", result: {
        structuredContent: { status: "succeeded", operationId: "operation-a", privateBody: "PRIVATE_BODY" },
      } }));
    },
  });
  const result = await perf.run({ sessionKey: "session-a", runId: "run-a", toolCallId: "tool-a" }, () => client.callTool(
    "oa_workflow_pending_list", { privateFilter: "PRIVATE_ARGUMENT" }, { meta: { "io.agentbridge/task": { taskId: "task-a" } },
      retry: { delaysMs: [1], sleep: async () => {} } }));
  assert.equal(result.status, "succeeded");
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[0].params._meta[TIMING_META_KEY], { requestId: "request-a", runId: "run-a", toolCallId: "tool-a", taskId: "task-a" });
  const events = perf.take("identity-a");
  assert.equal(events.length, 3);
  assert.deepEqual(events.map(item => item.stage), ["host.mcp_transport", "host.mcp_transport", "host.mcp_request"]);
  assert.deepEqual(events.map(item => item.outcome), ["error", "completed", "completed"]);
  assert.equal(events[2].attempt, 2);
  assert.equal(events[2].operationId, "operation-a");
  assert.equal(events[2].taskId, "task-a");
  assert.equal(events[2].durationMs >= events[1].durationMs, true);
  assert.equal(/PRIVATE_BODY|PRIVATE_ARGUMENT|SECRET_TOKEN|https:/u.test(JSON.stringify(events)), false);
});

test("runtime batches use the existing owning identity channel and failures never retry work", async () => {
  const perf = recorder();
  for (let i = 0; i < 40; i += 1) perf.record("host.model_started", {}, { sessionKey: "session-a", runId: "run-a" });
  const calls = [];
  const reporter = createHostRuntimeReporter({ performanceRecorder: perf,
    identityRouter: { configuredIdentities: () => ["identity-a", "identity-b"].map(key => ({ binding: { key }, client: {
      async callTool(name, params) { calls.push({ key, name, params }); return { status: "failed", error: { code: "TEMPORARY" } }; },
    } })) },
  });
  await reporter.collectOnce();
  assert.equal(calls[0].params.snapshot.timingEvents.length, 32);
  assert.equal(calls[1].params.snapshot.timingEvents, undefined);
  assert.equal(perf.status().queued, 8);
  assert.equal(calls.every(item => item.name === "agentbridge_host_runtime_snapshot"), true);
});

test("diagnostic transport does not recursively create timings through inherited async context", async () => {
  const perf = recorder();
  let body;
  const client = createAgentBridgeMcpClient({ endpoint: { url: "https://bridge.test/mcp", timeoutSeconds: 5 },
    tokenEnv: "TEST_TOKEN", env: { TEST_TOKEN: "SECRET_TOKEN" }, fetchImpl: async (_url, options) => {
      body = JSON.parse(options.body);
      return new Response(JSON.stringify({ jsonrpc: "2.0", id: "id", result: { structuredContent: { status: "succeeded" } } }));
    },
  });
  await perf.run({ sessionKey: "session-a", runId: "run-a" }, async () => {
    await new Promise(resolve => setTimeout(resolve, 1));
    await client.callTool("agentbridge_host_runtime_snapshot", { snapshot: { status: "healthy" } });
  });
  assert.equal(perf.status().queued, 0);
  assert.equal(body.params._meta, undefined);
});

test("2026.7.1 native model hooks record TTFB and usage, never synthesize first token", () => {
  const sharedState = {};
  const hooks = {};
  const identityRouter = { enabled: true, sessionBindings: new Map([["session-a", "identity-a"]]),
    resolveToolContext: () => ({ bound: false }), removeSession() {} };
  const api = { pluginConfig: { autoPoll: false, syncTimeline: false }, config: {},
    logger: { info() {}, warn() {} }, registerTool() {}, registerAgentToolResultMiddleware() {},
    registerCommand() {}, registerGatewayMethod() {}, on(name, callback) { hooks[name] = callback; },
  };
  registerAgentBridgeInteractions(api, { sharedState, identityRouter });
  const context = { sessionKey: "session-a", runId: "run-a" };
  hooks.model_call_started({ callId: "model-1", provider: "openai", model: "gpt-5.6-sol" }, context);
  hooks.model_call_ended({ callId: "model-1", durationMs: 1200, timeToFirstByteMs: 350,
    outcome: "completed", provider: "openai", model: "gpt-5.6-sol", prompt: "PRIVATE_BODY" }, context);
  hooks.llm_output({ usage: { input: 100, output: 20, cacheRead: 80 }, assistantTexts: ["PRIVATE_BODY"] }, context);
  const events = sharedState.hostPerformance.take("identity-a");
  assert.equal(events[1].timeToFirstByteMs, 350);
  assert.equal(events[1].durationMs, 1200);
  assert.equal(events[1].durationClock, "host.wall");
  assert.equal(events[2].cacheReadTokens, 80);
  assert.equal(/PRIVATE_BODY|firstToken|ttft/iu.test(JSON.stringify(events)), false);
});

function syncRecorder(options = {}) {
  let owner = "identity-a", namespace = "namespace-a", endpoint = "endpoint-a";
  const identityRouter = { enabled: true, sessionBindings: new Map([["session-a", owner]]),
    resolveToolContext: () => ({ bound: true, binding: { key: owner }, client: {} }),
    catalogCacheKeyForBinding: () => namespace, endpointKeyForSession: () => endpoint };
  const perf = createHostPerformance({ identityRouter, ...options });
  return { perf, identityRouter,
    changeOwner(value) { owner = value; identityRouter.sessionBindings.set("session-a", value); },
    changeNamespace(value) { namespace = value; }, changeEndpoint(value) { endpoint = value; } };
}

test("synchronous phases preserve undefined, block, arrays, promises and original errors", () => {
  let mono = 10, calls = 0;
  const { perf } = syncRecorder({ monotonic: () => mono });
  const context = { sessionKey: "session-a", runId: "run-a", toolCallId: "call-a", toolName: "database_execute" };
  const values = [undefined, { block: true, blockReason: "unchanged" }, [{ name: "tool" }], Promise.resolve("same-promise")];
  for (const value of values) assert.equal(observeHostSync(perf, context, () => {
    calls++; mono += 2; return value;
  }, "host.tool_factory"), value);
  const original = new Error("original business error");
  assert.throws(() => observeHostSync(perf, context, () => { calls++; mono += 3; throw original; },
    "host.before_tool_call"), error => error === original);
  assert.equal(calls, 5);
  const events = perf.take("identity-a");
  assert.deepEqual(events.map(event => event.durationMs), [2, 2, 2, 2, 3]);
  assert.equal(events.at(-1).outcome, "error");
  assert.equal(events.every(event => event.durationClock === "host.monotonic"), true);
});

test("clock, recorder and correlation failures never run the synchronous callback twice", () => {
  let calls = 0;
  const context = { sessionKey: "session-a", runId: "run-a" };
  const original = new Error("original");
  const broken = [
    syncRecorder({ monotonic() { throw new Error("clock"); } }).perf,
    syncRecorder({ now() { throw new Error("wall-clock"); } }).perf,
    syncRecorder({ monotonic: (() => { let n = 0; return () => { if (n++) throw new Error("finish clock"); return 1; }; })() }).perf,
    { startSync() { throw new Error("recorder start"); } },
    { startSync() { return () => { throw new Error("recorder finish"); }; } },
  ];
  for (const perf of broken) {
    const value = {};
    assert.equal(observeHostSync(perf, context, () => { calls++; return value; }, "host.tool_factory"), value);
    assert.throws(() => observeHostSync(perf, context, () => { calls++; throw original; },
      "host.before_tool_call"), error => error === original);
  }
  assert.equal(observeHostSync(broken[0], () => { throw new Error("context"); },
    () => { calls++; return "once"; }, "host.tool_factory"), "once");
  assert.equal(calls, broken.length * 2 + 1);
});

test("new synchronous phases never infer a request from the latest turn or an unknown run", () => {
  const { perf } = syncRecorder();
  perf.bindTurn("session-a", "old-request");
  perf.record("host.model_started", {}, { sessionKey: "session-a", runId: "old-run" });
  perf.take("identity-a");
  perf.bindTurn("session-a", "new-request");
  for (const runId of ["old-run", "unknown-run"]) observeHostSync(perf, { sessionKey: "session-a", runId },
    () => {}, "host.tool_factory");
  observeHostSync(perf, { sessionKey: "session-a", requestId: "explicit-without-run" }, () => {}, "host.tool_factory");
  perf.record("host.before_tool_call", { durationMs: 1 }, { sessionKey: "session-a", runId: "unknown-run" });
  observeHostSync(perf, { sessionKey: "session-a", runId: "known-run", requestId: "explicit-request" },
    () => {}, "host.before_tool_call");
  const events = perf.take("identity-a");
  assert.equal(events.length, 3);
  assert.deepEqual(events.slice(0, 2).map(event => event.requestId), [undefined, undefined]);
  assert.equal(events[2].requestId, "explicit-request");
  assert.doesNotMatch(JSON.stringify(events), /new-request|old-request|explicit-without-run/);
});

test("synchronous samples are dropped on identity, namespace, endpoint or final call changes", () => {
  for (const change of [h => h.changeOwner("identity-b"), h => h.changeOwner("conflict"),
                       h => h.changeNamespace("namespace-b"), h => h.changeEndpoint("endpoint-b")]) {
    const h = syncRecorder();
    const context = { sessionKey: "session-a", runId: "run-a" };
    assert.equal(observeHostSync(h.perf, context, () => { change(h); return "result"; }, "host.tool_factory"), "result");
    assert.equal(h.perf.status().queued, 0);
  }
  const { perf, identityRouter } = syncRecorder();
  const context = { sessionKey: "session-a", runId: "run-a", toolCallId: "exact-call" };
  observeHostSync(perf, context, () => {}, "host.before_tool_call", { ...context, runId: "new-run" });
  observeHostSync(perf, context, () => {}, "host.before_tool_call", () => null);
  identityRouter.resolveToolContext = () => ({ bound: false });
  observeHostSync(perf, context, () => {}, "host.tool_factory");
  assert.equal(perf.status().queued, 0);
});

test("synchronous diagnostics share bounded queues and never serialize business fields", () => {
  const { perf } = syncRecorder();
  perf.onPending(() => { throw new Error("reporter failed"); });
  const context = { sessionKey: "session-a", runId: "run-a", toolName: "database_execute",
    prompt: "PRIVATE_PROMPT", arguments: { password: "PRIVATE_PASSWORD" }, url: "https://private.test/" };
  let calls = 0;
  for (let i = 0; i < 300; i++) observeHostSync(perf, context, () => { calls++; }, "host.tool_factory");
  assert.equal(calls, 300);
  assert.deepEqual(perf.status(), { queued: 256, dropped: 44 });
  assert.doesNotMatch(JSON.stringify(perf.take("identity-a")), /PRIVATE_|https:|password|prompt|session-a/);
});

function synchronousPlugin() {
  const h = syncRecorder(), hooks = {}, sharedState = { ...createInteractionSharedState(), hostPerformance: h.perf };
  let factory;
  const api = { pluginConfig: { autoPoll: false, syncTimeline: false }, config: {},
    logger: { info() {}, warn() {} }, registerTool(value) { factory = value; }, registerAgentToolResultMiddleware() {},
    registerCommand() {}, registerGatewayMethod() {}, on(name, callback) { hooks[name] = callback; } };
  const coordinator = registerAgentBridgeInteractions(api, { sharedState, identityRouter: h.identityRouter });
  return { ...h, hooks, coordinator, factory };
}

test("real before-tool guards and lazy factories remain synchronous and report exact observed calls", () => {
  const h = synchronousPlugin();
  const context = { sessionKey: "session-a", runId: "run-a" };
  assert.equal(h.hooks.before_tool_call({ toolCallId: "read-call", toolName: "database_execute", runId: "run-a", params: {} }, context), undefined);
  const blocked = h.hooks.before_tool_call({ toolCallId: "blocked-call", toolName: "agentbridge__read", runId: "run-a", params: {} }, context);
  assert.equal(blocked.block, true);
  assert.equal(blocked.then, undefined);
  const tools = h.factory(context);
  assert.equal(Array.isArray(tools), true);
  const events = h.perf.take("identity-a");
  assert.deepEqual(events.map(event => event.stage), ["host.before_tool_call", "host.before_tool_call", "host.tool_factory"]);
  assert.equal(events[0].toolCallId, "read-call");
  assert.equal(events.every(event => event.runId === "run-a"), true);
  const original = new Error("original guard error");
  h.coordinator.bindToolCall = () => { throw original; };
  assert.throws(() => h.hooks.before_tool_call({ toolCallId: "failed-call", runId: "run-a" }, context), error => error === original);
});

test("plugin phases omit absent runs, conflicting calls, ended runs and event/context disagreement", () => {
  const h = synchronousPlugin();
  const context = { sessionKey: "session-a", runId: "run-a" };
  h.hooks.before_tool_call({ toolCallId: "same-call", runId: "run-a", params: {} }, context);
  h.perf.take("identity-a");
  // Reusing the exact tool call in another run is rejected for diagnostics.
  h.hooks.before_tool_call({ toolCallId: "same-call", runId: "run-b", params: {} }, { ...context, runId: "run-b" });
  h.factory({ ...context, toolCallId: "same-call" });
  h.factory({ sessionKey: "session-a" });
  h.hooks.before_tool_call({ toolCallId: "no-run", params: {} }, { sessionKey: "session-a" });
  h.hooks.before_tool_call({ toolCallId: "different-session", sessionKey: "session-b", runId: "run-a", params: {} }, context);
  h.hooks.before_tool_call({ toolCallId: "different-run", runId: "run-b", params: {} }, context);
  h.coordinator.releaseToolRun("session-a", "run-a");
  h.factory(context);
  assert.equal(h.perf.status().queued, 0);
});
