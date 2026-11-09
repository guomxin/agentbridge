import assert from "node:assert/strict";
import test from "node:test";

import { InteractionCoordinator } from "../lib/coordinator.js";
import { resolvePluginConfig } from "../lib/config.js";
import { createAgentBridgeProxyTools } from "../lib/proxy-tools.js";

const SESSION = "agent:main:agentbridge-workspace:direct:account-a";
const OTHER_SESSION = "agent:main:agentbridge-workspace:direct:account-b";
const SOURCE_TASK = "source-task-1234567890";
const REPAIR_TASK = "repair-task-1234567890";
const TTL = 5 * 60 * 1000;
const control = () => ({ status: "planning_control", taskId: SOURCE_TASK,
  error: { code: "PLAN_REQUIRED", message: "Use a durable plan" } });
const jsonResult = payload => ({ structuredContent: payload,
  content: [{ type: "text", text: JSON.stringify(payload) }] });
const payloadOf = result => result.structuredContent || result.details?.structuredContent ||
  JSON.parse(result.content[0].text);

function harness({ beforeHook = false, legacyMiddleware = false, businessControl = false } = {}) {
  let now = 1_800_000_000_000;
  let scope = "identity-a";
  const logger = { info() {}, warn() {}, error() {} };
  const coordinator = new InteractionCoordinator({ api: { logger },
    config: resolvePluginConfig({ autoPoll: false, syncTimeline: false }),
    now: () => now, toolIdentityResolver: key => key === SESSION ? scope : "identity-b" });
  coordinator.bindWorkspaceTurn(SESSION, "turn-1");
  const context = { sessionKey: SESSION, runId: "model-run-1" };
  const calls = [];
  const preparedKeys = new Map();
  let sourceReads = 0;
  let beforeBlockedReturn = null;
  let planResult = null;
  const client = {
    async callTool(name, args) {
      calls.push({ name, args });
      if (name === "agentbridge_host_task_ensure") {
        if (args.tool_name === "oa_workflow_history_next") {
          assert.equal(args.task_scope, "independent");
          return { task: { taskId: "cursor-task-1234567890" } };
        }
        if (args.tool_name === "agentbridge_task_plan_prepare") {
          return args.task_scope === "independent" ? { task: { taskId: REPAIR_TASK } } : control();
        }
        if (sourceReads && !businessControl) {
          await beforeBlockedReturn?.();
          return control();
        }
        return { task: { taskId: SOURCE_TASK } };
      }
      if (name === "agentbridge_host_coordinator_lease_acquire") {
        return { status: "succeeded", coordinatorLease: { hostInstanceId: "openclaw-gateway", version: 1 } };
      }
      if (name === "agentbridge_host_task_observe") return { status: "succeeded" };
      throw new Error(`Unexpected call: ${name}`);
    },
    async callToolResult(name, args) {
      calls.push({ name, args });
      if (name === "oa_workflow_done_list") {
        sourceReads++;
        if (sourceReads > 1) return jsonResult(control());
        return jsonResult({ status: "succeeded", operationId: "source-operation-1234567890",
          result: { count: 13, total: 1125, coverage: { status: "partial", scannedCount: 50 } } });
      }
      if (name === "oa_workflow_history_next") return jsonResult({ status: "succeeded",
        operationId: "cursor-operation-1234567890", result: { state: "complete", items: [] } });
      assert.equal(name, "agentbridge_task_plan_prepare");
      if (planResult) return jsonResult(planResult);
      const prior = preparedKeys.get(args.idempotency_key);
      const proposal = JSON.stringify({ ...args, idempotency_key: undefined });
      if (prior && prior !== proposal) return jsonResult({ status: "failed", error: { code: "PLAN_CONFLICT" } });
      preparedKeys.set(args.idempotency_key, proposal);
      return jsonResult({ status: "succeeded", reused: Boolean(prior) });
    },
  };
  const tools = createAgentBridgeProxyTools({ context, serverName: "agentbridge", logger,
    identityRouter: { resolveToolContext: () => ({ bound: true, binding: {}, client }),
      endpointKeyForSession: () => "workspace:account-a" },
    taskRunRefResolver: (...args) => coordinator.taskRunRefForToolCall(...args),
    taskScopeResolver: (...args) => coordinator.taskScopeForBusinessCall(...args),
    planningContextFactory: request => coordinator.capturePlanningCall(request),
    planningRepairResolver: (...args) => coordinator.planningRepairContextForCall(...args),
    terminalPlanGuard: request => coordinator.terminalPlanFailureForCall(request),
    trustedResultHandler: (event, ctx) => coordinator.captureNativeToolResult(event, ctx),
  });
  async function execute(name, id, params = {}) {
    if (beforeHook) coordinator.bindToolCall({ toolCallId: id, toolName: name, params, ...context }, context);
    const result = await tools.find(tool => tool.name === name).execute(id, params);
    if (legacyMiddleware) coordinator.captureToolResult({ toolCallId: id, toolName: name,
      result: { content: result.content, details: result.details } }, { runtime: "openclaw" });
    return result;
  }
  const read = (id, params) => execute("oa_workflow_done_list", id, params);
  async function block() {
    await read("first-read", { keyword: "业务结算单", limit: 1000 });
    return read("expanded-read", { keyword: "业务结算单", start_date: "2000-01-01", end_date: "2026-11-09", limit: 1000 });
  }
  return { coordinator, context, calls, execute, read, block, preparedKeys,
    setTime: value => { now = value; }, now: () => now,
    changeIdentity: () => { scope = "replacement-identity"; },
    beforeBlockedReturn: callback => { beforeBlockedReturn = callback; },
    setPlanResult: value => { planResult = value; },
  };
}

for (const beforeHook of [false, true]) {
  for (const legacyMiddleware of [false, true]) {
    test(`native planning recovery has one task and plan (before hook=${beforeHook}, legacy=${legacyMiddleware})`, async () => {
      const h = harness({ beforeHook, legacyMiddleware });
      const blocked = await h.block();
      assert.equal(blocked.structuredContent.error.code, "PLAN_REQUIRED");
      assert.match(blocked.content[0].text, /fresh planning task/);
      assert.equal(h.calls.filter(call => call.name === "oa_workflow_done_list").length, 1);
      const original = h.coordinator.planningRepairs.get(SESSION);
      assert.equal(original.sourceTaskId, SOURCE_TASK);
      const proposal = { schema_version: "agentbridge.task-plan.proposal.v2", goal: "只读全部已办", steps: [] };
      const first = await h.execute("agentbridge_task_plan_prepare", "prepare-1", proposal);
      const retry = await h.execute("agentbridge_task_plan_prepare", "prepare-2", {
        ...proposal, idempotency_key: "model-supplied-new-key-must-not-fork-the-repair",
      });
      assert.equal(first.structuredContent.status, "succeeded");
      assert.equal(retry.structuredContent.reused, true);
      const ensures = h.calls.filter(call => call.name === "agentbridge_host_task_ensure" && call.args.tool_name === "agentbridge_task_plan_prepare");
      assert.deepEqual(ensures.map(call => call.args.task_scope), ["independent", "independent"]);
      assert.equal(new Set(ensures.map(call => call.args.host_task_key)).size, 1);
      assert.match(ensures[0].args.host_task_key, /:plan-repair$/);
      assert.equal(ensures[0].args.planning_repair_source_task_id, SOURCE_TASK);
      assert.equal(ensures[0].args.planning_task_key, `${SESSION}|workspace:turn-1`);
      assert.equal(h.preparedKeys.size, 1);
      assert.equal(h.coordinator.planningRepairs.get(SESSION), original);
      const conflict = await h.execute("agentbridge_task_plan_prepare", "changed-plan", { ...proposal, goal: "different proposal" });
      assert.equal(conflict.structuredContent.error.code, "PLAN_CONFLICT");
      assert.equal(h.preparedKeys.size, 1);
    });
  }
}

test("native business-result PLAN_REQUIRED uses the same repair path as preflight", async () => {
  const h = harness({ businessControl: true });
  const result = await h.block();
  assert.equal(result.structuredContent.error.code, "PLAN_REQUIRED");
  assert.equal(h.coordinator.taskScopeForBusinessCall(SESSION, "agentbridge_task_plan_prepare"), "independent");
  assert.equal((await h.execute("agentbridge_task_plan_prepare", "plan")).structuredContent.status, "succeeded");
});

for (const change of ["turn", "identity", "ended-run"]) {
  test(`late native planning result cannot bind after ${change} changes`, async () => {
    const h = harness({ beforeHook: true, legacyMiddleware: true });
    h.beforeBlockedReturn(() => {
      if (change === "turn") h.coordinator.bindWorkspaceTurn(SESSION, "turn-2");
      else if (change === "identity") h.changeIdentity();
      else h.coordinator.releaseToolRun(SESSION, h.context.runId);
    });
    const result = await h.block();
    assert.equal(payloadOf(result).error.code, "PLAN_REQUIRED");
    assert.equal(h.coordinator.planningRepairs.size, 0);
    assert.doesNotMatch(result.content[0].text, /fresh planning task/);
  });
}

test("duplicate native and legacy receipts preserve TTL; an expired call cannot recreate recovery", async () => {
  const h = harness({ beforeHook: true });
  const result = await h.block();
  const repair = h.coordinator.planningRepairs.get(SESSION);
  const event = { toolCallId: "expanded-read", result };
  h.setTime(h.now() + TTL - 1);
  await h.coordinator.captureNativeToolResult(event, h.context);
  h.coordinator.captureToolResult(event, h.context);
  assert.equal(h.coordinator.planningRepairs.get(SESSION), repair);
  assert.equal(repair.expiresAt, repair.capturedAt + TTL);
  h.setTime(h.now() + 2);
  assert.equal(h.coordinator.taskScopeForBusinessCall(SESSION, "agentbridge_task_plan_prepare"), null);
  await h.coordinator.captureNativeToolResult(event, h.context);
  h.coordinator.captureToolResult(event, h.context);
  assert.equal(h.coordinator.planningRepairs.size, 0);
});

test("repair does not cross user, turn, identity, or reused call IDs", async () => {
  const h = harness();
  await h.block();
  h.coordinator.bindWorkspaceTurn(OTHER_SESSION, "other-turn");
  assert.equal(h.coordinator.taskScopeForBusinessCall(OTHER_SESSION, "agentbridge_task_plan_prepare"), null);
  const conflicted = h.coordinator.capturePlanningCall({ toolCallId: "expanded-read", sessionKey: OTHER_SESSION, runId: "other-run" });
  assert.equal(conflicted.isCurrent(), false);
  assert.equal(h.coordinator.planningIdempotencyForCall(OTHER_SESSION, "agentbridge_task_plan_prepare", conflicted), null);
  h.changeIdentity();
  assert.equal(h.coordinator.taskScopeForBusinessCall(SESSION, "agentbridge_task_plan_prepare"), null);
  assert.equal(h.coordinator.terminalPlanFailureForCall({ sessionKey: SESSION, toolName: "oa_workflow_done_list" }), null);
  h.coordinator.bindWorkspaceTurn(SESSION, "new-turn");
  assert.equal(h.coordinator.planningRepairs.size, 0);
});

test("native incomplete-source plan remains terminal for this turn", async () => {
  const h = harness({ legacyMiddleware: true });
  await h.block();
  h.setPlanResult({ status: "failed", plan: { planId: "incomplete-plan", state: "failed", terminalReason: "PLAN_SOURCE_INCOMPLETE" } });
  await h.execute("agentbridge_task_plan_prepare", "failed-plan");
  const before = h.calls.length;
  for (const name of ["oa_workflow_done_list", "oa_workflow_history_next", "agentbridge_task_plan_prepare", "taihua_work_log_create_prepare"]) {
    const stopped = await h.execute(name, `retry-${name}`);
    assert.equal(payloadOf(stopped).error.code, "PLAN_SOURCE_INCOMPLETE");
  }
  assert.equal(h.calls.length, before);
  assert.equal(h.coordinator.terminalPlanFailureForCall({ sessionKey: OTHER_SESSION }), null);
  h.coordinator.bindWorkspaceTurn(SESSION, "new-turn");
  assert.equal(h.coordinator.terminalPlanFailureForCall({ sessionKey: SESSION }), null);
});

test("a repaired read-only history report can continue its owned cursor but cannot start a new source", async () => {
  const h = harness();
  await h.block();
  await h.execute("agentbridge_task_plan_prepare", "read-only-report");
  const next = await h.execute("oa_workflow_history_next", "continue-report", { cursor: "owned-opaque-cursor" });
  assert.equal(payloadOf(next).status, "succeeded");
  const call = h.calls.find(entry => entry.name === "oa_workflow_history_next");
  assert.deepEqual(call.args, { cursor: "owned-opaque-cursor" });
  const before = h.calls.length;
  const fresh = await h.execute("oa_workflow_history_query", "new-source", { collection: "done", temporal: { kind: "all" } });
  assert.equal(payloadOf(fresh).error.code, "PLAN_REQUIRED");
  assert.equal(h.calls.length, before);
});

test("untrusted, unbound, and ordinary native errors do not create a planning repair", async () => {
  const h = harness();
  const planningContext = h.coordinator.capturePlanningCall({ ...h.context, toolCallId: "trusted-call" });
  for (const [callId, server, payload] of [
    ["trusted-call", "foreign-server", control()],
    ["missing-call", "agentbridge", control()],
    ["trusted-call", "agentbridge", { status: "failed", error: { code: "DOWNSTREAM_UNAVAILABLE" } }],
  ]) {
    await h.coordinator.captureNativeToolResult({ toolCallId: callId,
      ...(callId === "trusted-call" ? { planningContext } : {}),
      result: { ...jsonResult(payload), details: { mcpServer: server, mcpTool: "oa_workflow_done_list", agentbridgeTaskId: SOURCE_TASK } },
    }, h.context);
    assert.equal(h.coordinator.planningRepairs.size, 0);
  }
});

test("a plan call that becomes stale during preflight stops before its business invocation", async () => {
  const h = harness();
  await h.block();
  const originalResolver = h.coordinator.taskScopeForBusinessCall.bind(h.coordinator);
  h.coordinator.taskScopeForBusinessCall = (...args) => {
    const scope = originalResolver(...args);
    h.coordinator.bindWorkspaceTurn(SESSION, "next-turn-during-ensure");
    return scope;
  };
  const result = await h.execute("agentbridge_task_plan_prepare", "stale-plan");
  assert.equal(payloadOf(result).error.code, "PLAN_TURN_CONTEXT_CHANGED");
  assert.equal(h.calls.filter(call => call.name === "agentbridge_task_plan_prepare").length, 0);
});

test("first asynchronous identity binding preserves the captured turn and rejects a stale resolved identity", () => {
  let scope = null;
  const c = new InteractionCoordinator({ api: { logger: { info() {} } },
    config: resolvePluginConfig({ autoPoll: false }), toolIdentityResolver: () => scope });
  c.bindWorkspaceTurn(SESSION, "identity-start");
  const request = { toolCallId: "identity-call", sessionKey: SESSION, runId: "identity-run" };
  const pending = c.capturePlanningCall(request);
  scope = "resolved-identity";
  const resolved = c.capturePlanningCall({ ...request, resolvedIdentityScope: scope });
  assert.equal(resolved.turnRef, pending.turnRef);
  assert.equal(resolved.capturedAt, pending.capturedAt);
  assert.equal(resolved.isCurrent(), true);
  assert.equal(pending.isCurrent(), false);
  const oldClient = c.capturePlanningCall({ ...request, resolvedIdentityScope: "stale-client-identity" });
  assert.equal(oldClient.isCurrent(), false);
  c.bindWorkspaceTurn(SESSION, "before-login");
  scope = null;
  const changedRequest = { ...request, toolCallId: "changed-during-login" };
  const original = c.capturePlanningCall(changedRequest);
  c.bindWorkspaceTurn(SESSION, "after-login");
  scope = "resolved-identity";
  const late = c.capturePlanningCall({ ...changedRequest, resolvedIdentityScope: scope });
  assert.equal(late.turnRef, original.turnRef);
  assert.equal(late.isCurrent(), false);
});

test("a conflicting native call ID cannot let a legacy result attach to another user", async () => {
  const h = harness();
  const original = h.coordinator.capturePlanningCall({ ...h.context, toolCallId: "collision" });
  h.coordinator.bindWorkspaceTurn(OTHER_SESSION, "other-turn");
  h.coordinator.capturePlanningCall({ toolCallId: "collision", sessionKey: OTHER_SESSION, runId: "other-run" });
  assert.equal(original.isCurrent(), false);
  const event = { toolCallId: "collision", result: { ...jsonResult(control()), details: {
    mcpServer: "agentbridge", mcpTool: "oa_workflow_done_list", agentbridgeTaskId: SOURCE_TASK,
  } } };
  await h.coordinator.captureNativeToolResult({ ...event, planningContext: original }, h.context);
  h.coordinator.captureToolResult(event, { sessionKey: OTHER_SESSION });
  assert.equal(h.coordinator.planningRepairs.size, 0);
});
