import test from "node:test";
import assert from "node:assert/strict";
import { createAgentBridgeProxyTools } from "../lib/proxy-tools.js";
import { skillCatalog, rememberSkillBinding, boundSkillToolGuard } from "../lib/business-skills.js";
import { AgentBridgeIdentityRouter } from "../lib/identity-router.js";
import { resolvePluginConfig } from "../lib/config.js";
import { createTaskCommandRunner } from "../lib/task-commands.js";
import { InteractionCoordinator } from "../lib/coordinator.js";
import { interaction } from "./fixtures.js";

const taskId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const operationId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const context = { sessionKey: "agent:main:agentbridge-workspace:direct:test-user", runId: "run-1" };
const binding = { key: "owner", channel: "telegram", tokenEnv: "TEST_TOKEN" };
const lease = (extra = {}) => ({ schema: "agentbridge.coordinator-lease.v1", taskId,
  hostInstanceId: "openclaw-gateway", agentHost: "openclaw", state: "active", version: 7,
  expiresAt: new Date(Date.now() + 600_000).toISOString(), ...extra });

function proxy({ responseLease = lease(), receipt = {}, scope = "all" } = {}) {
  const calls = [];
  const client = {
    async callTool(name) {
      calls.push(name);
      if (name === "agentbridge_host_task_ensure") return { task: { taskId }, coordinatorLease: responseLease };
      if (name === "agentbridge_host_coordinator_lease_acquire") return { coordinatorLease: lease() };
      return { status: "succeeded" };
    },
    async callToolResult(name, _args, options) {
      calls.push(name);
      assert.equal(options.meta["io.agentbridge/task"].coordinatorLeaseVersion, "7");
      return { structuredContent: { status: "succeeded", operationId,
        taskObservation: { schemaVersion: "agentbridge.task-observation.v1", taskId, taskVersion: 3,
          taskStatus: "succeeded", operationIds: [operationId], interactionIds: [], ...receipt } } };
    },
  };
  const router = { config: { toolScope: scope }, resolveToolContext: () => ({ bound: true, binding, client }),
    endpointKeyForSession: () => "workspace:test-user" };
  const tools = createAgentBridgeProxyTools({ context, identityRouter: router, serverName: "agentbridge" });
  return { calls, tools, run: () => tools.find(tool => tool.name === "oa_workflow_pending_list").execute("call-1", {}) };
}

test("an exact sufficiently long atomic lease and full observation receipt remove two control round trips", async () => {
  const h = proxy(); await h.run();
  assert.deepEqual(h.calls, ["agentbridge_host_task_ensure", "oa_workflow_pending_list"]);
});
for (const extra of [{ expiresAt: new Date(Date.now() + 60_000).toISOString() }, { taskId: "other-task" },
  { hostInstanceId: "other-host" }, { version: 0 }, { state: "released" }]) {
  test(`an insufficient ensure lease retains the original renewal: ${Object.keys(extra)[0]}`, async () => {
    const h = proxy({ responseLease: lease(extra) }); await h.run();
    assert.deepEqual(h.calls, ["agentbridge_host_task_ensure", "agentbridge_host_coordinator_lease_acquire", "oa_workflow_pending_list"]);
  });
}
for (const receipt of [{ taskId: "other-task" }, { operationIds: [] }, { interactionIds: ["unseen-interaction"] },
  { operationIds: [operationId, operationId] }, { taskVersion: 0 }, { taskStatus: "unknown-status" }, { schemaVersion: "future-schema" }]) {
  test(`an incomplete observation receipt retains original observe: ${JSON.stringify(receipt)}`, async () => {
    const h = proxy({ receipt }); await h.run();
    assert.equal(h.calls.at(-1), "agentbridge_host_task_observe");
  });
}

test("general-purpose tool presentation remains complete; a separate static scope is explicit", () => {
  const all = proxy().tools.map(tool => tool.name);
  const narrow = proxy({ scope: "oa_pending" }).tools.map(tool => tool.name);
  assert.equal(all.includes("smartlight_alarm_remark_update_prepare"), true);
  assert.equal(all.includes("taihua_work_log_create_prepare"), true);
  assert.equal(narrow.includes("smartlight_alarm_remark_update_prepare"), false);
  assert.equal(narrow.includes("oa_missed_punch_approval_prepare"), true);
  assert.equal(narrow.includes("oa_workflow_pending_list"), true);
  assert.equal(narrow.length < all.length, true);
});

test("loaded OA scopes reject unrelated tools, batch under single and another Skill; server binding remains attached", () => {
  const router = { config: {} }, identity = { binding };
  rememberSkillBinding(router, context, identity, { status: "succeeded", binding_id: "fixed-binding", skill_id: "oa-pending", profile: "single" });
  for (const name of ["exec", "taihua_work_log_create_prepare", "oa_workflow_pending_batch_prepare", "agentbridge_task_plan_prepare"]) {
    assert.equal(boundSkillToolGuard(router, context, identity, name, {}).error.code, "SKILL_TOOL_SCOPE_REQUIRED");
  }
  assert.equal(boundSkillToolGuard(router, context, identity, "oa_missed_punch_approval_prepare", {}), null);
  assert.equal(boundSkillToolGuard(router, context, identity, "agentbridge_skill_authoring", { action: "resource", data: { binding_id: "fixed-binding" } }), null);
  assert.ok(boundSkillToolGuard(router, context, identity, "agentbridge_skill_get", { skill_id: "log-review", profile: "review" }));
  assert.equal(boundSkillToolGuard(router, { ...context, runId: "other-run" }, identity, "exec", {}), null);
});

function catalogRouter({ token = "first-test-token", cache = new Map(), versions = new Map() } = {}) {
  return new AgentBridgeIdentityRouter({ config: resolvePluginConfig({ mcpUrl: "https://bridge.test/mcp",
    identityBindings: [{ channel: "telegram", senderId: "1001", tokenEnv: "TEST_TOKEN" }] }),
    hostConfig: {}, env: { TEST_TOKEN: token }, catalogCache: cache, catalogVersions: versions,
    fetchImpl: async () => { throw new Error("must use injected test client"); } });
}
function stampedCatalog(catalogVersion = "a".repeat(64), permissionVersion = "1".repeat(64)) {
  return { items: [{ id: "oa-pending", name: "OA", version: "1" }], catalogVersion, permissionVersion };
}

test("real Central sha256-prefixed catalog stamps produce a cache hit", async () => {
  const router = catalogRouter(); let calls = 0;
  const identity = { binding: router.config.identityBindings[0], client: { async callTool() {
    calls += 1; return stampedCatalog("sha256:" + "a".repeat(64), "sha256:" + "1".repeat(64));
  } } };
  await skillCatalog(identity, { router }); await skillCatalog(identity, { router });
  assert.equal(calls, 1);
});

test("catalog cache singleflight, version invalidation, TTL and credential isolation are independent", async () => {
  const cache = new Map(), versions = new Map();
  const router = catalogRouter({ cache, versions });
  const ownerBinding = router.config.identityBindings[0];
  let count = 0, now = 100, catalog = stampedCatalog();
  let release; const wait = new Promise(resolve => { release = resolve; });
  const identity = { binding: ownerBinding, client: { async callTool() { count += 1; await wait; return catalog; } } };
  const reads = [skillCatalog(identity, { router, now: () => now }), skillCatalog(identity, { router, now: () => now })];
  release(); await Promise.all(reads); assert.equal(count, 1);
  await skillCatalog(identity, { router, now: () => now }); assert.equal(count, 1);
  catalog = stampedCatalog("b".repeat(64)); router.rememberCatalogVersions(ownerBinding, catalog);
  await skillCatalog(identity, { router, now: () => now }); assert.equal(count, 2);
  now += 60_001; await skillCatalog(identity, { router, now: () => now }); assert.equal(count, 3);
  const other = catalogRouter({ token: "second-test-token", cache, versions });
  await skillCatalog({ ...identity, binding: other.config.identityBindings[0] }, { router: other, now: () => now });
  assert.equal(count, 4);
  assert.equal(JSON.stringify([...cache.keys()]).includes("test-token"), false);
});

test("catalog errors never use expired entries and unversioned servers are never cached", async () => {
  const router = catalogRouter();
  let count = 0;
  const identity = { binding: router.config.identityBindings[0], client: { async callTool() { count += 1; return { items: [] }; } } };
  await skillCatalog(identity, { router }); await skillCatalog(identity, { router }); assert.equal(count, 2);
  identity.client.callTool = async () => { throw new Error("offline"); };
  await assert.rejects(skillCatalog(identity, { router }), /offline/);
});

test("prepared commands contain no business arguments and recover through exact endpoint, lease and original task", async () => {
  const calls = [], restored = [];
  let release; const blocked = new Promise(resolve => { release = resolve; });
  const client = { async callTool(name, args, options) {
    calls.push({ name, args, options });
    if (name === "agentbridge_host_coordinator_lease_acquire") { await blocked; return { coordinatorLease: lease() }; }
    assert.equal(options.meta["io.agentbridge/task"].coordinatorLeaseVersion, "7");
    return { status: "requires_user_action", interaction: interaction({ type: "business_input" }) };
  } };
  const identity = { bound: true, binding, client };
  const runner = createTaskCommandRunner({ identityRouter: { resolvePinnedSession: () => identity,
    endpointKeyForSession: () => "workspace:test-user" }, coordinator: { config: { allowedCardOrigins: ["http://10.10.50.213:8780"] },
      async restoreRecoveredInteraction(value) { restored.push(value); } } });
  const command = { taskId, commandKey: "command-1", sessionKey: context.sessionKey, endpointKey: "workspace:test-user" };
  const first = runner.schedule(command); const duplicate = runner.schedule(command);
  assert.equal(duplicate.duplicate, true);
  assert.equal(runner.schedule({ ...command, arguments: {} }).accepted, false);
  release(); await first.promise; await runner.waitForIdle();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].args, { task_id: taskId, command_key: "command-1", session_key: context.sessionKey, endpoint_key: "workspace:test-user" });
  assert.equal(restored[0].taskId, taskId);
});

test("prepared commands never dispatch on conflicting routing or an expired/fenced lease", async () => {
  for (const conflict of ["owner", "endpoint", "lease"]) {
    let preparations = 0;
    const client = { async callTool(name) {
      if (name === "agentbridge_host_coordinator_lease_acquire") return { coordinatorLease: lease({ hostInstanceId: "other-host" }) };
      preparations += 1;
    } };
    const identity = { bound: true, binding, client };
    const runner = createTaskCommandRunner({ identityRouter: { resolvePinnedSession: () => identity,
      endpointKeyForSession: () => conflict === "endpoint" ? "wrong-endpoint" : "workspace:test-user" }, coordinator: {} });
    const result = runner.schedule({ taskId, commandKey: "command-1", sessionKey: context.sessionKey, endpointKey: "workspace:test-user" },
      conflict === "owner" ? { binding: { key: "other-owner" } } : null);
    assert.equal((await result.promise).status, "unconfirmed"); assert.equal(preparations, 0);
  }
});

test("prepared Workspace interaction starts its real original poll without a delivery route", async () => {
  let releaseDecision, polls = 0, resumes = 0;
  const decision = new Promise(resolve => { releaseDecision = resolve; });
  const client = { async callTool(name) {
    if (name === "agentbridge_host_coordinator_lease_acquire") return { coordinatorLease: lease() };
    if (name === "agentbridge_host_task_prepare") return { status: "requires_user_action", interaction: interaction({ type: "business_input" }) };
    if (name === "agentbridge_interaction_get") { polls += 1; await decision;
      return { waitSupported: true, waitedMs: 1000, interaction: interaction({ type: "business_input", state: "completed", resume: { ready: true, completed: false } }) }; }
    if (name === "agentbridge_interaction_resume") { resumes += 1; return { status: "succeeded", nextAction: { type: "original_request_completed" } }; }
    return { status: "succeeded" };
  } };
  const coordinator = new InteractionCoordinator({ config: resolvePluginConfig({ autoPoll: true, syncTimeline: false,
    allowedCardOrigins: ["http://10.10.50.213:8780"] }), mcpClient: client,
    api: { logger: { info() {}, warn() {} }, runtime: { system: { enqueueSystemEvent() {}, requestHeartbeat() {} } } } });
  const identity = { bound: true, binding, client };
  const runner = createTaskCommandRunner({ identityRouter: { resolvePinnedSession: () => identity,
    endpointKeyForSession: () => "workspace:test-user" }, coordinator });
  await runner.schedule({ taskId, commandKey: "command-1", sessionKey: context.sessionKey, endpointKey: "workspace:test-user" }).promise;
  assert.equal(polls, 1);
  assert.equal(coordinator.polls.has("interaction-1234567890"), true);
  assert.equal(coordinator.sessionRoutes.size, 0);
  releaseDecision(); await coordinator.waitForIdle();
  assert.equal(resumes, 1);
});
