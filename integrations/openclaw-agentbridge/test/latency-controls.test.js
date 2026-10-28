import test from "node:test";
import assert from "node:assert/strict";
import { createAgentBridgeProxyTools } from "../lib/proxy-tools.js";
import { skillCatalog, rememberSkillBinding, boundSkillToolGuard, draftSampleGuard,
  releaseSkillRun, releaseSkillSession, skillBindingMeta } from "../lib/business-skills.js";
import { AgentBridgeIdentityRouter } from "../lib/identity-router.js";
import { resolvePluginConfig } from "../lib/config.js";
import { createTaskCommandRunner } from "../lib/task-commands.js";
import { InteractionCoordinator, createInteractionSharedState } from "../lib/coordinator.js";
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

test("opt-in loaded OA scopes reject unrelated tools, batch under single and another Skill; server binding remains attached", () => {
  const router = { config: { boundSkillToolGuard: true } }, identity = { binding };
  rememberSkillBinding(router, context, identity, { status: "succeeded", binding_id: "fixed-binding", skill_id: "oa-pending", profile: "single" });
  for (const name of ["exec", "taihua_work_log_create_prepare", "oa_workflow_pending_batch_prepare", "agentbridge_task_plan_prepare"]) {
    assert.equal(boundSkillToolGuard(router, context, identity, name, {}).error.code, "SKILL_TOOL_SCOPE_REQUIRED");
  }
  assert.equal(boundSkillToolGuard(router, context, identity, "oa_missed_punch_approval_prepare", {}), null);
  assert.equal(boundSkillToolGuard(router, context, identity, "agentbridge_skill_authoring", { action: "resource", data: { binding_id: "fixed-binding" } }), null);
  for (const action of ["discover", "composition"]) {
    assert.equal(boundSkillToolGuard(router, context, identity, "agentbridge_skill_authoring", { action, data: {} }), null);
  }
  assert.equal(boundSkillToolGuard(router, context, identity, "agentbridge_task_plan_catalog", {}), null);
  for (const action of ["save", "submit", "test"]) {
    assert.equal(boundSkillToolGuard(router, context, identity, "agentbridge_skill_authoring", { action, data: {} }).error.code, "SKILL_TOOL_SCOPE_REQUIRED");
  }
  assert.ok(boundSkillToolGuard(router, context, identity, "agentbridge_skill_get", { skill_id: "log-review", profile: "review" }));
  assert.equal(boundSkillToolGuard(router, { ...context, runId: "other-run" }, identity, "exec", {}), null);
});

test("the general-purpose agent retains independent tasks and plan recovery after loading an OA Skill", () => {
  const router = { config: resolvePluginConfig({}) }, identity = { binding };
  rememberSkillBinding(router, context, identity, { status: "succeeded", binding_id: "fixed-binding", skill_id: "oa-pending", profile: "single" });
  assert.equal(router.config.boundSkillToolGuard, false);
  for (const name of ["agentbridge_task_plan_prepare", "taihua_work_log_create_prepare", "agentbridge_skill_get"]) {
    assert.equal(boundSkillToolGuard(router, context, identity, name, { skill_id: "log-review", profile: "review" }), null);
  }
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

test('unconstrained legacy reads survive absent hooks but explicit conflict cannot use factory runId', async () => {
  let calls=0;
  const router={config:{},resolveToolContext:()=>({bound:true,binding,client:{async callToolResult(){calls++;return{structuredContent:{status:'succeeded'}};}}})};
  const make=status=>createAgentBridgeProxyTools({context,identityRouter:router,serverName:'agentbridge',
    taskRunRefResolver:()=>null,taskRunContextResolver:()=>({status})});
  await make('missing').find(t=>t.name==='agentbridge_skill_catalog').execute('legacy-read',{});
  assert.equal(calls,1);
  for(const [status,name] of [['missing','agentbridge_skill_get'],['rejected','agentbridge_skill_get'],['rejected','agentbridge_skill_catalog']]){
    const result=await make(status).find(t=>t.name===name).execute('unobserved-call',{skill_id:'oa-pending',profile:'single'});
    assert.equal(JSON.parse(result.content[0].text).error.code,'SKILL_RUN_CONTEXT_REQUIRED');
  }
  assert.equal(calls,1);
});

test('lazy-factory telemetry uses only an exact bound run and never a rejected factory run', async () => {
  const recorded = [];
  const router = { config: {}, resolveToolContext: () => ({ bound: true, binding,
    client: { async callToolResult() { return { structuredContent: { status: 'succeeded' } }; } } }) };
  const run = async (factoryContext, exact) => {
    const tools = createAgentBridgeProxyTools({ context: factoryContext, identityRouter: router,
      serverName: 'agentbridge', taskRunRefResolver: () => exact.runId || null,
      taskRunContextResolver: () => exact,
      performanceRecorder: { run(observed, callback) { recorded.push(observed); return callback(); } } });
    return tools.find(t => t.name === 'agentbridge_skill_catalog').execute('exact-call', {});
  };
  await run({ sessionKey: context.sessionKey }, { status: 'bound', runId: 'native-exact-run' });
  assert.equal(recorded.at(-1).runId, 'native-exact-run');
  const rejected = await run(context, { status: 'rejected' });
  assert.equal(recorded.at(-1).runId, undefined);
  assert.equal(JSON.parse(rejected.content[0].text).error.code, 'SKILL_RUN_CONTEXT_REQUIRED');
  await run(context, { status: 'missing' });
  assert.equal(recorded.at(-1).runId, context.runId);
});

test('a failed telemetry lookup preserves unbound identity results and original execution errors', async () => {
  const observed = [];
  const failure = new Error('original identity resolution failed');
  let rejectIdentity = false;
  const tools = createAgentBridgeProxyTools({ context, serverName: 'agentbridge',
    identityRouter: { config: {}, resolveToolContext() {
      return { bound: false, reason: 'identity-unbound' };
    }, async resolveWorkspaceSession() {
      if (rejectIdentity) throw failure;
      return { bound: false, reason: 'identity-unbound' };
    } },
    taskRunContextResolver() { throw new Error('diagnostic context lookup failed'); },
    performanceRecorder: { run(value, callback) { observed.push(value); return callback(); } } });
  const tool = tools.find(t => t.name === 'agentbridge_skill_catalog');
  const result = await tool.execute('unbound-call', {});
  assert.equal(JSON.parse(result.content[0].text).status, 'unbound');
  assert.equal(observed.at(-1).runId, undefined);
  rejectIdentity = true;
  await assert.rejects(tool.execute('failed-call', {}), error => error === failure);
  assert.equal(observed.at(-1).runId, undefined);
});

test('active scope and sample constraints survive long turns and missing-call context',()=>{
  let now=1000;
  const router={config:{boundSkillToolGuard:true},skillRunNow:()=>now},identity={binding};
  rememberSkillBinding(router,context,identity,{status:'succeeded',binding_id:'retained',skill_id:'oa-pending',profile:'single'});
  const sample={...context,runId:'sample'};
  draftSampleGuard(router,sample,identity,'agentbridge_skill_authoring',{action:'test'});
  now+=60*60_000;
  assert.equal(boundSkillToolGuard(router,context,identity,'exec',{}).error.code,'SKILL_TOOL_SCOPE_REQUIRED');
  assert.equal(draftSampleGuard(router,sample,identity,'oa_workflow_pending_list',{}).error.code,'SKILL_SAMPLE_ONLY');
  const unknown={sessionKey:context.sessionKey};
  assert.equal(boundSkillToolGuard(router,unknown,identity,'exec',{}).error.code,'SKILL_RUN_CONTEXT_REQUIRED');
  assert.equal(draftSampleGuard(router,unknown,identity,'oa_workflow_pending_list',{}).error.code,'SKILL_RUN_CONTEXT_REQUIRED');
  releaseSkillRun(router,context);
  rememberSkillBinding(router,context,identity,{status:'succeeded',binding_id:'late',skill_id:'oa-pending',profile:'single'});
  assert.deepEqual(skillBindingMeta(router,context,identity),{});
  assert.equal(draftSampleGuard(router,context,identity,'oa_workflow_pending_list',{}).error.code,'SKILL_RUN_CONTEXT_CLOSED');
  releaseSkillSession(router,context.sessionKey);
  assert.equal(draftSampleGuard(router,sample,identity,'oa_workflow_pending_list',{}).error.code,'SKILL_RUN_CONTEXT_CLOSED');
});

test('capacity refuses new constraints while retaining existing scope and sample guards',()=>{
  const router={config:{boundSkillToolGuard:true}},identity={binding};
  for(let i=0;i<4096;i++){
    const c={...context,runId:'capacity-'+i};
    rememberSkillBinding(router,c,identity,{status:'succeeded',binding_id:'binding-'+i,skill_id:'oa-pending',profile:'single'});
    assert.equal(draftSampleGuard(router,c,identity,'agentbridge_skill_authoring',{action:'test'}),null);
  }
  const overflow={...context,runId:'overflow'};
  assert.throws(()=>rememberSkillBinding(router,overflow,identity,{status:'succeeded',binding_id:'extra'}),/capacity/);
  assert.equal(draftSampleGuard(router,overflow,identity,'agentbridge_skill_authoring',{action:'test'}).error.code,'SKILL_SAMPLE_CAPACITY');
  assert.equal(boundSkillToolGuard(router,{...context,runId:'capacity-0'},identity,'exec',{}).error.code,'SKILL_TOOL_SCOPE_REQUIRED');
  assert.equal(draftSampleGuard(router,{...context,runId:'capacity-0'},identity,'oa_workflow_pending_list',{}).error.code,'SKILL_SAMPLE_ONLY');
});

test('exact cross-registry call links are bounded, expire closed and survive local stop',()=>{
  let now=1000,scope='owner-a';
  const sharedState=createInteractionSharedState();
  const make=()=>new InteractionCoordinator({api:{logger:{warn(){}}},config:{},sharedState,
    now:()=>now,toolIdentityResolver:()=>scope});
  const a=make(),b=make(),sessionKey=context.sessionKey;
  a.bindToolCall({toolCallId:'exact',runId:'run-a'},{sessionKey});
  a.bindToolCall({toolCallId:'exact'},{sessionKey});
  assert.equal(b.taskRunRefForToolCall('exact',sessionKey,'agentbridge_skill_get'),'run-a');
  scope='owner-b';
  assert.equal(b.toolRunContextForCall('exact',sessionKey).status,'rejected');
  scope='owner-a';a.stopAll({clearSharedState:false});
  assert.equal(b.taskRunRefForToolCall('exact',sessionKey,'agentbridge_skill_get'),'run-a');
  now+=300_001;
  b.bindWorkspaceTurn(sessionKey,'new-dispatch','new request');
  assert.equal(b.taskRunRefForToolCall('exact',sessionKey,'agentbridge_skill_get'),null);
  for(let i=0;i<1001;i++)a.bindToolCall({toolCallId:'bounded-'+i,runId:'run-'+i},{sessionKey});
  assert.equal(sharedState.toolBindings.size,1000);
  assert.equal(b.taskRunRefForToolCall('bounded-0',sessionKey,'agentbridge_skill_get'),null);
  b.releaseToolRun(sessionKey,'run-1000');
  a.bindToolCall({toolCallId:'late',runId:'run-1000'},{sessionKey});
  assert.equal(b.taskRunRefForToolCall('late',sessionKey,'agentbridge_skill_get'),null);
});

test('catalog invalidation during first read retries once without returning or caching the old response', async () => {
  for (const cold of [false, true]) for (const staleRetry of [false, true]) {
    const router = catalogRouter(), binding = router.config.identityBindings[0];
    const old = stampedCatalog(), current = stampedCatalog('b'.repeat(64));
    if (!cold) router.rememberCatalogVersions(binding, old);
    let release, calls = 0;
    const pending = new Promise(resolve => {release = resolve;});
    const identity = {binding, client: {async callTool() {
      calls += 1; return calls === 1 ? pending : staleRetry ? old : current;
    }}};
    const first = skillCatalog(identity, {router}), second = skillCatalog(identity, {router});
    router.rememberCatalogVersions(binding, current); release(old);
    if (staleRetry) {
      for (const result of await Promise.allSettled([first, second])) {
        assert.equal(result.status, 'rejected'); assert.match(result.reason.message, /目录正在更新/);
      }
      assert.equal(router.catalogCache.size, 0);
    } else {
      const results = await Promise.all([first, second]);
      assert.ok(results.every(value => value.catalogVersion === current.catalogVersion));
      assert.equal((await skillCatalog(identity, {router})).catalogVersion, current.catalogVersion);
    }
    assert.equal(calls, 2);
    assert.equal(router.catalogVersionsForBinding(binding).catalogVersion, current.catalogVersion);
  }
});
