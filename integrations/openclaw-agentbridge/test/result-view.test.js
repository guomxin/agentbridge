import test from "node:test";
import assert from "node:assert/strict";
import { normalizeTrustedResultView, resultViewPresentation } from "../lib/result-view.js";
import { createAgentBridgeProxyTools, AGENTBRIDGE_AGENT_FACING_TOOL_NAMES } from "../lib/proxy-tools.js";
import { InteractionCoordinator, createInteractionSharedState } from "../lib/coordinator.js";
import { registerAgentBridgeInteractions } from "../lib/plugin.js";

const sourceTaskId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const endpointId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const sessionKey = "agent:main:telegram:direct:user-a";
const origin = "https://cards.example.test";
const url = `${origin}/results/${"a".repeat(43)}`;
const now = Date.parse("2026-10-21T01:00:00Z");
const view = (extra = {}) => ({ schemaVersion: "agentbridge.result-view.v1", sourceTaskId,
  title: "可篡改的标题不作为宿主按钮", expiresAt: new Date(now + 3_600_000).toISOString(),
  selectionAvailable: true, sourceExpiresAt: new Date(now + 600_000).toISOString(),
  presentation: { owner: "agentbridge", endpointId, modelMustNotCollectValues: true, url }, ...extra });
const normalize = value => normalizeTrustedResultView(value, { sourceTaskId, endpointId, allowedOrigins: [origin], now });

test("the result surface admits only the exact trusted task, endpoint, origin, ticket and finite expiry", () => {
  assert.equal(normalize(view()).url, url);
  for (const change of [{ schemaVersion: "future" }, { sourceTaskId: "other" },
    { expiresAt: "bad" }, { expiresAt: new Date(now).toISOString() }, { expiresAt: new Date(now + 7_200_000).toISOString() },
    ...[{ endpointId: "other" }, { owner: "model" }, { modelMustNotCollectValues: false },
      ...["javascript:alert(1)", `${origin}/auth/${"a".repeat(43)}`, `${origin}/results/short`,
        `https://elsewhere.test/results/${"a".repeat(43)}`, `${url}?target=other`, `${url}#submit`,
        `https://user:password@cards.example.test/results/${"a".repeat(43)}`].map(url => ({ url }))]
      .map(change => ({ presentation: { ...view().presentation, ...change } }))]) {
    assert.equal(normalize(view(change)), null, JSON.stringify(change));
  }
});

test("selection expiry changes the entry to progress without extending the source or making a callback", () => {
  const value = normalize(view());
  const before = resultViewPresentation([value], "telegram", now);
  const after = resultViewPresentation([value], "telegram", now + 600_001);
  assert.equal(before.blocks.at(-1).buttons[0].label, "选择并办理 OA 待办");
  assert.equal(after.blocks.at(-1).buttons[0].label, "查看办理进度");
  assert.deepEqual(before.blocks.at(-1).buttons[0].webApp, { url });
  assert.equal(JSON.stringify(before).includes("callback"), false);
  assert.equal(JSON.stringify(before).includes("可篡改的标题"), false);
  assert.equal(resultViewPresentation([value], "telegram", now + 3_600_001), null);
});

function proxyHarness({ session = sessionKey, channel = "telegram", failed = false, originEndpointId = endpointId,
  issueFailure = false, viewOverride = null, beforeIssueReturn = null, routeOverride = null,
  contextFactory = true, afterFactory = null, endpointOverride = null, ownerAccount = "account-a" } = {}) {
  const calls = [], captured = [];
  let namespace = "credential-user-a";
  const binding = { key: "telegram:*:user-a", channel, senderId: "user-a", accountId: ownerAccount };
  const route = { channel: "telegram", to: "user-a", accountId: ownerAccount, threadId: null, ...routeOverride };
  const originalEndpoint = { endpointId, agentHost: "openclaw", state: "active", clientType: "telegram",
    externalSubject: "user-a", accountId: ownerAccount, conversationRef: sessionKey, route: { ...route }, ...endpointOverride };
  const client = { async callTool(name, args, options) {
    calls.push({ name, args, options });
    if (name === "agentbridge_host_task_ensure") return { task: { taskId: sourceTaskId, originEndpointId }, endpoint: originalEndpoint, coordinatorLease: {
      schema: "agentbridge.coordinator-lease.v1", taskId: sourceTaskId, hostInstanceId: "openclaw-gateway",
      agentHost: "openclaw", state: "active", version: 1, expiresAt: new Date(Date.now() + 600_000).toISOString() } };
    if (name === "agentbridge_result_view") {
      if (issueFailure) throw new Error("synthetic issue failure");
      await beforeIssueReturn?.({ coordinator, changeIdentity() { namespace = "credential-user-b"; } });
      return { status: "succeeded", resultView: viewOverride || view() };
    }
    return { status: "succeeded" };
  }, async callToolResult(name) {
    calls.push({ name });
    return { structuredContent: { status: failed ? "failed" : "succeeded", result: { items: [] } } };
  } };
  let currentClient = client;
  const identityRouter = { config: {}, resolveToolContext: () => ({ bound: true, client, binding }),
    resolvePinnedSession: () => ({ bound: true, binding, client: currentClient }), clientForBinding: () => currentClient,
    catalogCacheKeyForBinding: () => namespace, endpointKeyForSession: () => binding.key };
  const coordinator = new InteractionCoordinator({ now: () => now, config: { mcpServerName: "agentbridge", allowedCardOrigins: [origin] },
    toolIdentityResolver: () => JSON.stringify([namespace, binding.key]),
    api: { logger: { info() {}, warn() {} } } });
  coordinator.bindDeliveryRoute({ sessionKey: session, ...route });
  const tools = createAgentBridgeProxyTools({ context: { sessionKey: session, runId: "run-1" }, identityRouter,
    serverName: "agentbridge", trustedResultHandler: async event => { captured.push(event); return event.result; },
    ...(contextFactory ? { trustedResultContextFactory: request => {
      const result = coordinator.createTrustedResultContext(request); afterFactory?.({ coordinator,
        changeIdentity() { namespace = "credential-user-b"; } }); return result;
    } } : {}) });
  return { calls, captured, coordinator, replaceClient() { currentClient = {}; namespace = "new-credential"; },
    run: () => tools.find(tool => tool.name === "oa_workflow_pending_list").execute("call-1", {}) };
}

test("the real proxy issues a host-only surface after task finalization and keeps the private URL out of the model result", async () => {
  const h = proxyHarness();
  const result = await h.run();
  assert.deepEqual(h.calls.map(item => item.name), ["agentbridge_host_task_ensure", "oa_workflow_pending_list",
    "agentbridge_host_task_finish", "agentbridge_result_view"]);
  assert.equal(h.calls.at(-1).options.meta["io.agentbridge/task"].endpointId, endpointId);
  assert.deepEqual(h.calls.at(-1).args, { operation: "issue", task_id: sourceTaskId });
  assert.equal(h.captured[0].trustedResultView.view.presentation.url, url);
  assert.equal(JSON.stringify(result).includes(url), false);
  assert.equal(AGENTBRIDGE_AGENT_FACING_TOOL_NAMES.includes("agentbridge_result_view"), false);
  assert.deepEqual(h.calls[0].args.capabilities, ["direct_status", "trusted_interaction", "continuous_flow_v1"]);
});

test("trusted Telegram result issuance accepts default aliases without treating distinct custom accounts as aliases", async () => {
  for (const [accountId, routeAccount] of [[null, "default"], ["default", null], ["", "default"], [null, ""]]) {
    const h = proxyHarness({ ownerAccount: "default", routeOverride: { accountId: routeAccount }, endpointOverride: { accountId } });
    await h.run();
    assert.equal(h.calls.filter(item => item.name === "agentbridge_result_view").length, 1, JSON.stringify({ accountId, routeAccount }));
  }
  const h = proxyHarness({ ownerAccount: "primary", routeOverride: { accountId: " primary " } });
  await h.run();
  assert.equal(h.calls.some(item => item.name === "agentbridge_result_view"), false);
});

test("unbound origins, non-Telegram sessions and failed queries never request a result surface", async () => {
  for (const options of [{ originEndpointId: null }, { channel: "web" }, { session: "agent:main:telegram:group:-100" },
    { session: "agent:main:main" }, { failed: true }]) {
    const h = proxyHarness(options); await h.run();
    assert.equal(h.calls.some(item => item.name === "agentbridge_result_view"), false, JSON.stringify(options));
    assert.equal(h.captured[0].trustedResultView, undefined);
  }
  const h = proxyHarness({ issueFailure: true });
  assert.equal((await h.run()).structuredContent.status, "succeeded");
  assert.equal(h.captured[0].trustedResultView, undefined);
});

function coordinatorHarness({ deliver = true, beforeLoadReturn = null, beforeRenderReturn = null, bindCall = false, runId = "run-1" } = {}) {
  const sent = [];
  let namespace = "private-user-a-namespace";
  const binding = { key: "telegram:account-a:user-a", channel: "telegram", senderId: "user-a", accountId: "account-a" };
  const client = {};
  const identityRouter = { resolvePinnedSession: () => ({ bound: true, binding, client }), clientForBinding: () => client,
    catalogCacheKeyForBinding: () => namespace, endpointKeyForSession: () => binding.key };
  const changeIdentity = () => { namespace = "other-user-namespace"; };
  const coordinator = new InteractionCoordinator({ now: () => now, config: { mcpServerName: "agentbridge", allowedCardOrigins: [origin] },
    toolIdentityResolver: () => JSON.stringify([namespace, binding.key]),
    api: { logger: { info() {}, warn() {} }, config: {}, runtime: { channel: { outbound: { async loadAdapter() {
      await beforeLoadReturn?.({ coordinator, changeIdentity });
      return { async renderPresentation({ payload }) { await beforeRenderReturn?.({ coordinator, changeIdentity }); return payload; }, async sendPayload(payload) {
        if (!deliver) throw new Error("synthetic channel unavailable"); sent.push(payload); } };
    } } } } } });
  coordinator.bindDeliveryRoute({ sessionKey, channel: "telegram", to: "user-a", accountId: "account-a" });
  if (bindCall) coordinator.bindToolCall({ toolCallId: "call-1", toolName: "oa_workflow_pending_list" }, { sessionKey, runId: "run-1" });
  const issuance = coordinator.createTrustedResultContext({ identityRouter, identity: { bound: true, binding, client },
    context: { sessionKey, runId }, toolCallId: "call-1", toolName: "oa_workflow_pending_list" });
  assert.ok(issuance);
  assert.equal(issuance.bindEndpoint({ sourceTaskId, endpointId, endpoint: { endpointId, agentHost: "openclaw",
    state: "active", clientType: "telegram", externalSubject: "user-a", accountId: "account-a", conversationRef: sessionKey,
    route: { channel: "telegram", to: "user-a", accountId: "account-a", threadId: null } } }), true);
  const event = { toolCallId: "call-1", toolName: "oa_workflow_pending_list", result: { details: {
    mcpServer: "agentbridge", mcpTool: "oa_workflow_pending_list", agentbridgeTaskId: sourceTaskId } },
    trustedResultView: { sourceTaskId, endpointId, view: view(), issuance } };
  return { coordinator, sent, event, changeIdentity };
}

test("private collection cards are delivered once and cannot survive identity, route or session replacement", async () => {
  const h = coordinatorHarness();
  await h.coordinator.captureTrustedResultView(h.event, { sessionKey });
  await h.coordinator.captureTrustedResultView(h.event, { sessionKey });
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].to, "user-a");
  assert.equal(h.sent[0].accountId, "account-a");
  assert.equal(h.coordinator.takeResultViewPresentation(sessionKey, "telegram"), null);
  const queued = coordinatorHarness({ deliver: false });
  await queued.coordinator.captureTrustedResultView(queued.event, { sessionKey });
  assert.equal(queued.coordinator.takeResultViewPresentation("agent:main:telegram:direct:user-b", "telegram"), null);
  queued.changeIdentity();
  assert.equal(queued.coordinator.takeResultViewPresentation(sessionKey, "telegram"), null);
  const changedRoute = coordinatorHarness({ deliver: false });
  await changedRoute.coordinator.captureTrustedResultView(changedRoute.event, { sessionKey });
  changedRoute.coordinator.bindDeliveryRoute({ sessionKey, channel: "telegram", to: "user-b" });
  assert.equal(changedRoute.coordinator.takeResultViewPresentation(sessionKey, "telegram"), null);
  changedRoute.coordinator.removeSession(sessionKey);
  assert.equal(changedRoute.coordinator.resultViews.size, 0);
});

test("untrusted result metadata, endpoint mismatches and model text cannot create a collection card", async () => {
  for (const mutate of [event => { event.result.details.mcpServer = "foreign"; },
    event => { event.result.details.agentbridgeTaskId = "other"; },
    event => { event.trustedResultView.endpointId = "other"; },
    event => { delete event.trustedResultView; event.result.content = [{ type: "text", text: JSON.stringify(view()) }]; }]) {
    const h = coordinatorHarness(); mutate(h.event);
    await h.coordinator.captureTrustedResultView(h.event, { sessionKey });
    assert.equal(h.sent.length, 0);
    assert.equal(h.coordinator.resultViews.size, 0);
  }
});

test("initial recipient, account or thread conflicts and absent issuance suppress only the optional collection card", async () => {
  for (const options of [{ routeOverride: { to: "user-b" } }, { routeOverride: { accountId: "account-b" } },
    { routeOverride: { threadId: 99 } }, { contextFactory: false }]) {
    const h = proxyHarness(options);
    const result = await h.run();
    assert.equal(result.structuredContent.status, "succeeded");
    assert.equal(h.calls.filter(item => item.name === "agentbridge_result_view").length, 0, JSON.stringify(options));
    assert.equal(h.captured[0].trustedResultView, undefined);
    assert.equal(JSON.stringify(result).includes(url), false);
  }
});

test("the actual ensure endpoint must match the original identity and route before a private URL is issued", async () => {
  for (const endpointOverride of [{ endpointId: "other" }, { externalSubject: "user-b" }, { accountId: "account-b" },
    { conversationRef: "agent:main:telegram:direct:user-b" }, { state: "disabled" }, { agentHost: "foreign" },
    { route: { channel: "telegram", to: "user-b", accountId: "account-a" } },
    { route: { channel: "telegram", to: "user-a", accountId: "account-a", threadId: 99 } }]) {
    const h = proxyHarness({ endpointOverride }); const result = await h.run();
    assert.equal(result.structuredContent.status, "succeeded");
    assert.equal(h.calls.some(item => item.name === "agentbridge_result_view"), false, JSON.stringify(endpointOverride));
  }
});

test("a stale factory cannot relabel its old transport with the new credential namespace", async () => {
  const h = proxyHarness(); h.replaceClient();
  assert.equal((await h.run()).structuredContent.status, "succeeded");
  assert.equal(h.calls.some(item => item.name === "agentbridge_result_view"), false);
  assert.equal(h.captured[0].trustedResultView, undefined);
});

test("an identity or route change during issue discards a late private URL while preserving the query result", async () => {
  for (const beforeIssueReturn of [({ changeIdentity }) => changeIdentity(), ({ coordinator }) =>
    coordinator.bindDeliveryRoute({ sessionKey, channel: "telegram", to: "user-b", accountId: "account-a" })]) {
    const h = proxyHarness({ beforeIssueReturn }); const result = await h.run();
    assert.equal(result.structuredContent.status, "succeeded");
    assert.equal(h.calls.filter(item => item.name === "agentbridge_result_view").length, 1);
    assert.equal(h.captured[0].trustedResultView, undefined);
    assert.equal(JSON.stringify(result).includes(url), false);
  }
});

test("an original call's identity scope rejects a previously issued URL after identity replacement", async () => {
  const h = coordinatorHarness({ bindCall: true });
  h.changeIdentity();
  assert.equal(h.coordinator.toolRunContextForCall("call-1", sessionKey, "run-1").status, "rejected");
  await h.coordinator.captureTrustedResultView(h.event, { sessionKey, runId: "run-1" });
  assert.equal(h.sent.length, 0);
  assert.equal(h.coordinator.resultViews.size, 0);
});

test("a hookless result requires a real host issuance, and serialized or copied metadata cannot forge one", async () => {
  for (const fake of [undefined, {}, { route: { channel: "telegram", to: "user-a", accountId: "account-a" },
    isCurrent: () => true, bindEndpoint: () => true }]) {
    const h = coordinatorHarness(); h.event.trustedResultView.issuance = fake;
    await h.coordinator.captureTrustedResultView(h.event, { sessionKey });
    assert.equal(h.sent.length, 0);
    assert.equal(h.coordinator.resultViews.size, 0);
  }
  const h = coordinatorHarness();
  await h.coordinator.captureTrustedResultView(h.event, { sessionKey });
  assert.equal(h.sent.length, 1);
});

test("identity or route changes while loading or rendering an adapter stop delivery before send", async () => {
  for (const phase of ["beforeLoadReturn", "beforeRenderReturn"]) {
    for (const change of [({ changeIdentity }) => changeIdentity(), ({ coordinator }) =>
      coordinator.bindDeliveryRoute({ sessionKey, channel: "telegram", to: "user-b", accountId: "account-a" }),
    ({ coordinator }) => coordinator.bindDeliveryRoute({ sessionKey, channel: "telegram", to: "user-a", accountId: "account-b" }),
    ({ coordinator }) => coordinator.bindDeliveryRoute({ sessionKey, channel: "telegram", to: "user-a", accountId: "account-a", threadId: 99 })]) {
      const h = coordinatorHarness({ [phase]: change });
      await h.coordinator.captureTrustedResultView(h.event, { sessionKey });
      assert.equal(h.sent.length, 0, phase);
      assert.equal(h.coordinator.takeResultViewPresentation(sessionKey, "telegram"), null);
      assert.equal(h.coordinator.resultViews.size, 0);
    }
  }
});

test("an ended or conflicting original call never delivers a delayed collection card", async () => {
  for (const change of [h => h.coordinator.releaseToolRun(sessionKey, "run-1"), h =>
    h.coordinator.bindToolCall({ toolCallId: "call-1" }, { sessionKey: "agent:main:telegram:direct:user-b", runId: "other-run" })]) {
    const h = coordinatorHarness({ bindCall: true }); change(h);
    await h.coordinator.captureTrustedResultView(h.event, { sessionKey, runId: "run-1" });
    assert.equal(h.sent.length, 0);
    assert.equal(h.coordinator.resultViews.size, 0);
  }
});

test("removing an exact binding cannot downgrade its issued card into a hookless card", async () => {
  const h = coordinatorHarness({ bindCall: true });
  h.coordinator.toolBindings.delete("call-1");
  await h.coordinator.captureTrustedResultView(h.event, { sessionKey });
  assert.equal(h.sent.length, 0);
  assert.equal(h.coordinator.resultViews.size, 0);
});

test("a hookless issuance with no known run cannot survive agent end or a reset followed by identical rebinding", async () => {
  for (const change of [h => h.coordinator.releaseToolRun(sessionKey, "actual-native-run"), h => {
    h.coordinator.removeSession(sessionKey);
    h.coordinator.bindDeliveryRoute({ sessionKey, channel: "telegram", to: "user-a", accountId: "account-a" });
  }]) {
    const h = coordinatorHarness({ runId: null }); change(h);
    await h.coordinator.captureTrustedResultView(h.event, { sessionKey });
    assert.equal(h.sent.length, 0);
    assert.equal(h.coordinator.resultViews.size, 0);
  }
});

test("agent end or session reset during issue prevents the late result even after the same route is rebound", async () => {
  for (const beforeIssueReturn of [({ coordinator }) => coordinator.releaseToolRun(sessionKey, "run-1"), ({ coordinator }) => {
    coordinator.removeSession(sessionKey);
    coordinator.bindDeliveryRoute({ sessionKey, channel: "telegram", to: "user-a", accountId: "account-a" });
  }]) {
    const h = proxyHarness({ beforeIssueReturn });
    assert.equal((await h.run()).structuredContent.status, "succeeded");
    assert.equal(h.calls.filter(item => item.name === "agentbridge_result_view").length, 1);
    assert.equal(h.captured[0].trustedResultView, undefined);
  }
});

test("a lazy proxy registry and the original reply registry share only authenticated issuance fallback", async () => {
  const sharedState = createInteractionSharedState();
  const binding = { key: "telegram:account-a:user-a", channel: "telegram", senderId: "user-a", accountId: "account-a" };
  let namespace = "credential-user-a";
  const endpoint = { endpointId, agentHost: "openclaw", state: "active", clientType: "telegram", externalSubject: "user-a",
    accountId: "account-a", conversationRef: sessionKey, route: { channel: "telegram", to: "user-a", accountId: "account-a", threadId: null } };
  const client = { async callTool(name) {
    if (name === "agentbridge_host_task_ensure") return { task: { taskId: sourceTaskId, originEndpointId: endpointId }, endpoint,
      coordinatorLease: { schema: "agentbridge.coordinator-lease.v1", taskId: sourceTaskId, hostInstanceId: "openclaw-gateway",
        agentHost: "openclaw", state: "active", version: 1, expiresAt: new Date(Date.now() + 600_000).toISOString() } };
    if (name === "agentbridge_result_view") return { status: "succeeded", resultView: view() };
    return { status: "succeeded" };
  }, async callToolResult() { return { structuredContent: { status: "succeeded", result: { items: [] } } }; } };
  const identity = { bound: true, binding, client };
  const router = { enabled: true, config: {}, resolveToolContext: () => identity, resolvePinnedSession: () => identity,
    clientForSession: () => client, clientForBinding: () => client, endpointKeyForSession: () => binding.key,
    catalogCacheKeyForBinding: () => namespace, configuredIdentities: () => [{ binding, client }] };
  function register() {
    const hooks = {}, factories = [];
    const api = { pluginConfig: { allowedCardOrigins: [origin], syncTimeline: false, autoPoll: false }, config: {},
      logger: { info() {}, warn() {} }, runtime: { channel: { outbound: { async loadAdapter() { return {
        async renderPresentation({ payload }) { return payload; }, async sendPayload() { throw new Error("synthetic temporary send failure"); },
      }; } } } }, registerAgentToolResultMiddleware() {}, registerTool(factory) { factories.push(factory); },
      on(name, callback) { hooks[name] = callback; }, registerCommand() {}, registerGatewayMethod() {} };
    const coordinator = registerAgentBridgeInteractions(api, { sharedState, identityRouter: router, now: () => now });
    return { coordinator, hooks, factory: factories[0] };
  }
  const original = register(), lazy = register();
  original.coordinator.bindDeliveryRoute({ sessionKey, channel: "telegram", to: "user-a", accountId: "account-a" });
  const native = lazy.factory({ sessionKey, runId: "run-1" }).find(tool => tool.name === "oa_workflow_pending_list");
  const result = await native.execute("call-1", {});
  assert.equal(JSON.stringify(result).includes(url), false);
  assert.equal(sharedState.resultViews.size, 1);
  const replacement = original.hooks.reply_payload_sending({ kind: "final", sessionKey, channel: "telegram", payload: { text: "查询完成" } },
    { sessionKey, channelId: "telegram", conversationId: "user-a", accountId: "account-a" });
  assert.equal(replacement.payload.presentation.blocks.at(-1).buttons[0].webApp.url, url);
  assert.equal(original.coordinator.takeResultViewPresentation(sessionKey, "telegram"), null);
  // Sharing a registry must never let another identity adopt a pending card.
  for (const record of sharedState.resultViews.values()) record.delivered = false;
  namespace = "credential-user-b";
  assert.equal(original.coordinator.takeResultViewPresentation(sessionKey, "telegram"), null);
  assert.equal(sharedState.resultViews.size, 0);
  original.coordinator.stopAll(); lazy.coordinator.stopAll();
});
