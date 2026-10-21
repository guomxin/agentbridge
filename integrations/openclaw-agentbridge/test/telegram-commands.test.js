import test from "node:test";
import assert from "node:assert/strict";
import { createTaskCommandRunner } from "../lib/task-commands.js";
import { createHostRuntimeReporter } from "../lib/runtime-reporter.js";
import { InteractionCoordinator } from "../lib/coordinator.js";
import { resolvePluginConfig } from "../lib/config.js";
import { interaction } from "./fixtures.js";
import { canonicalTelegramAccount, sameTelegramAccount } from "../lib/telegram-identity.js";
import { registerAgentBridgeInteractions } from "../lib/plugin.js";
import { processToolResult } from "../lib/interaction.js";

const sessionKey = "agent:main:telegram:direct:1001";
const endpointId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const taskId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const binding = { key: "telegram:default:1001", channel: "telegram", senderId: "1001", accountId: "default" };
const endpoint = { endpointId, clientType: "telegram", externalSubject: "1001", accountId: "default",
  conversationRef: sessionKey, route: { channel: "telegram", to: "telegram:1001", accountId: "default", threadId: null } };
const command = () => ({ taskId, commandKey: "original-opaque-key", sessionKey, endpointKey: binding.key, endpoint: structuredClone(endpoint) });
const hint = () => ({ deliveryId: "delivery-preparation-hint", deliveryMode: "prepared_command",
  task: { taskId, originEndpointId: endpointId, activeConversationRef: sessionKey },
  event: { eventType: "task.prepare.requested", payload: { commandKey: "original-opaque-key" } } });

function harness({ badLease = false, missingIdentity = true, owner = binding, offlineSnapshot = false, noCommands = false,
  liveEndpoint = endpoint, mixedBatch = false } = {}) {
  const calls = [], sent = [], bindings = new Map();
  if (!missingIdentity) bindings.set(sessionKey, binding.key);
  let pending = !noCommands;
  let presentation = null;
  const client = { async callTool(name, args, options) {
    calls.push({ name, args, options });
    if (name === "agentbridge_host_runtime_snapshot") {
      if (offlineSnapshot) throw new Error("synthetic offline snapshot");
      return { status: "succeeded", pendingCommands: pending ? [{ ...command(), endpointKey: owner.key,
        endpoint: structuredClone(liveEndpoint) }] : [] };
    }
    if (name === "agentbridge_host_coordinator_lease_acquire") return { coordinatorLease: {
      taskId, hostInstanceId: badLease ? "another-host" : "openclaw-gateway", state: "active", version: 7,
      expiresAt: new Date(Date.now() + 600_000).toISOString() } };
    if (name === "agentbridge_host_task_prepare") {
      pending = false;
      return { status: "requires_user_action", interaction: interaction({ type: "business_input",
        presentation: { url: "https://cards.example.test/input/original-opaque-submission" }, display: { businessView: {
          schemaVersion: "agentbridge.business-view.v1", profileId: mixedBatch ? "oa.weekly_report.acknowledgement" : "oa.attendance_confirmation",
          profileVersion: "1", viewKind: "approval" }, ...(mixedBatch ? { batch: { schemaVersion: "agentbridge.batch-view.v1",
            totalCount: 2, currentIndex: 1, succeededCount: 0, state: "waiting_user" } } : {}) } }) };
    }
    if (name === "agentbridge_host_interaction_present") {
      assert.equal(args.interaction_id, "interaction-1234567890");
      assert.equal(args.endpoint_key, owner.key);
      // Central's presentation method creates the endpoint-specific row and
      // wraps it only after the endpoint has declared continuous_flow_v1.
      presentation = interaction({ type: "business_input", presentation: {
        url: "https://cards.example.test/flow/input/original-opaque-submission/present/endpoint-ticket",
        endpointId, presentationId: "endpoint-ticket", individualized: true,
        flowVersion: "agentbridge.trusted-flow.v1" }, display: { businessView: {
        schemaVersion: "agentbridge.business-view.v1", profileId: mixedBatch ? "oa.weekly_report.acknowledgement" : "oa.attendance_confirmation",
        profileVersion: "1", viewKind: "approval" }, ...(mixedBatch ? { batch: { schemaVersion: "agentbridge.batch-view.v1",
        totalCount: 2, currentIndex: 1, succeededCount: 0, state: "waiting_user" } } : {}) } });
      return { protocolVersion: "0.1", status: "succeeded", interaction: presentation };
    }
    return { status: "succeeded" };
  } };
  const identityRouter = { enabled: true,
    configuredIdentities: () => [{ binding: owner, client }],
    resolvePinnedSession: ({ sessionKey }) => bindings.has(sessionKey)
      ? { bound: true, binding: { ...owner, key: bindings.get(sessionKey) }, client } : null,
    async resolveWorkspaceSession() { throw new Error("Telegram must not enter Workspace resolver"); },
    restoreSessionBinding({ sessionKey, bindingKey }) {
      if (bindings.has(sessionKey) && bindings.get(sessionKey) !== bindingKey) return false;
      bindings.set(sessionKey, bindingKey); return true;
    },
    endpointKeyForSession: sessionKey => bindings.get(sessionKey),
  };
  const coordinator = new InteractionCoordinator({ interactionPresenter: async (input, targetSession) => {
    assert.equal(targetSession, sessionKey);
    assert.equal(input.presentation.url, "https://cards.example.test/input/original-opaque-submission");
    const response = await client.callTool("agentbridge_host_interaction_present", { agent_host: "openclaw",
      endpoint_key: identityRouter.endpointKeyForSession(targetSession), interaction_id: input.interactionId });
    return processToolResult(response, ["https://cards.example.test"]).interactions[0];
  }, config: resolvePluginConfig({ allowedCardOrigins: ["https://cards.example.test"],
    autoPoll: false, wakeAgentOnComplete: false }), api: { logger: { info() {}, warn() {} }, config: {}, runtime: {
      channel: { outbound: { async loadAdapter() { return { async renderPresentation({ payload }) { return payload; },
        async sendPayload(payload) { sent.push(payload); } }; } } } } } });
  const runner = createTaskCommandRunner({ identityRouter, coordinator });
  const reporter = createHostRuntimeReporter({ identityRouter, coordinator, taskCommandRunner: runner });
  coordinator.taskCommandRunner = runner;
  coordinator.hostRuntimeReporter = reporter;
  return { calls, sent, client, identityRouter, coordinator, runner, reporter, endpointPresentation: () => presentation };
}

test("a durable Telegram hint recovers the authenticated command immediately after a host restart", async () => {
  const h = harness();
  await h.coordinator.processEndpointNotification(h.identityRouter, binding, h.client,
    new AbortController().signal, endpoint, hint());
  await h.runner.waitForIdle();
  assert.equal(h.calls.filter(item => item.name === "agentbridge_host_runtime_snapshot").length, 1);
  assert.equal(h.calls.filter(item => item.name === "agentbridge_host_task_prepare").length, 1);
  const prepare = h.calls.find(item => item.name === "agentbridge_host_task_prepare");
  assert.deepEqual(prepare.args, { task_id: taskId, command_key: "original-opaque-key", session_key: sessionKey, endpoint_key: binding.key });
  assert.equal(prepare.options.meta["io.agentbridge/task"].coordinatorLeaseVersion, "7");
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].to, endpoint.route.to);
  assert.equal(h.sent[0].accountId, endpoint.accountId);
  assert.equal(h.sent[0].payload.presentation.blocks.at(-1).buttons[0].label, "核对月度考勤");
  assert.equal(h.calls.find(item => item.name.endsWith("_ack")).args.succeeded, true);
  assert.equal(h.calls.some(item => /commit|resume/.test(item.name)), false);
  await h.coordinator.processEndpointNotification(h.identityRouter, binding, h.client,
    new AbortController().signal, endpoint, { ...hint(), deliveryId: "duplicate-hint" });
  await h.runner.waitForIdle();
  assert.equal(h.calls.filter(item => item.name === "agentbridge_host_task_prepare").length, 1);
  h.runner.stop(); h.coordinator.stopAll();
});

test("the Telegram dispatcher rejects forged arguments, group routes, altered recipients, threads and owner conflicts", async () => {
  const changes = [value => { value.arguments = {}; }, value => { delete value.endpoint; },
    value => { value.endpoint.endpointId = "invalid"; }, value => { value.endpoint.clientType = "web"; },
    value => { value.endpoint.route.to = "1002"; }, value => { value.endpoint.route.threadId = "topic-1"; },
    value => { value.endpoint.route.accountId = "other-account"; },
    value => { value.endpoint.conversationRef = "agent:main:telegram:direct:1002"; },
    value => { value.sessionKey = value.endpoint.conversationRef = "agent:main:telegram:group:-100"; }];
  for (const change of changes) {
    const h = harness(); const value = command(); change(value);
    assert.equal(h.runner.schedule(value, { binding, client: h.client }).accepted, false);
    await h.runner.waitForIdle(); assert.equal(h.calls.length, 0);
    h.runner.stop(); h.coordinator.stopAll();
  }
  for (const owner of [null, { ...binding, key: "another-binding" }, { ...binding, senderId: "1002" }, { ...binding, accountId: "other-account" }]) {
    const h = harness();
    const job = h.runner.schedule(command(), owner ? { binding: owner, client: h.client } : null);
    assert.equal((await job.promise).status, "unconfirmed");
    assert.equal(h.calls.length, 0);
    h.runner.stop(); h.coordinator.stopAll();
  }
});

test("a fenced lease never reaches prepare even after a valid durable hint", async () => {
  const h = harness({ badLease: true });
  await h.coordinator.processEndpointNotification(h.identityRouter, binding, h.client,
    new AbortController().signal, endpoint, hint());
  await h.runner.waitForIdle();
  assert.equal(h.calls.some(item => item.name === "agentbridge_host_task_prepare"), false);
  assert.equal(h.sent.length, 0);
  h.runner.stop(); h.coordinator.stopAll();
});

test("only the exact authenticated origin hint can collect live commands", async () => {
  for (const change of [value => { value.task.originEndpointId = "another-endpoint"; },
    value => { value.task.activeConversationRef = "agent:main:telegram:direct:1002"; },
    value => { value.event.eventType = "model.proposed.command"; }, value => { delete value.event.payload.commandKey; }]) {
    const h = harness(); const value = hint(); change(value);
    await h.coordinator.processEndpointNotification(h.identityRouter, binding, h.client,
      new AbortController().signal, endpoint, value);
    assert.equal(h.calls.some(item => item.name === "agentbridge_host_runtime_snapshot"), false);
    assert.equal(h.calls.at(-1).args.succeeded, false);
    h.runner.stop(); h.coordinator.stopAll();
  }
});

test("snapshot failures retain the durable hint and never execute the untrusted notification payload", async () => {
  const h = harness({ offlineSnapshot: true });
  const value = hint(); value.event.payload.arguments = { affair_id: "forged" }; value.event.payload.capability = "oa.some.commit";
  await h.coordinator.processEndpointNotification(h.identityRouter, binding, h.client,
    new AbortController().signal, endpoint, value);
  await h.runner.waitForIdle();
  assert.equal(h.calls.some(item => item.name === "agentbridge_host_task_prepare"), false);
  assert.equal(h.calls.at(-1).args.succeeded, false);
  assert.equal(h.sent.length, 0);
  h.runner.stop(); h.coordinator.stopAll();
});

test("a live snapshot with no pending command acknowledges an obsolete hint without replay", async () => {
  const h = harness({ noCommands: true });
  await h.coordinator.processEndpointNotification(h.identityRouter, binding, h.client,
    new AbortController().signal, endpoint, hint());
  assert.equal(h.calls.some(item => item.name === "agentbridge_host_task_prepare"), false);
  assert.equal(h.calls.at(-1).args.succeeded, true);
  h.runner.stop(); h.coordinator.stopAll();
});

test("only default Telegram account aliases compare equal; custom spelling and invalid values remain strict", () => {
  for (const left of [undefined, null, "", "default", " default "]) {
    for (const right of [undefined, null, "", "default", " default "]) assert.equal(sameTelegramAccount(left, right), true);
    assert.equal(canonicalTelegramAccount(left), "default");
  }
  for (const value of [0, false, {}, [], "x".repeat(513)]) assert.equal(sameTelegramAccount(value, "default"), false);
  for (const [left, right] of [["primary", "Primary"], ["primary", " primary "], ["primary", null], ["default", "other"]]) {
    assert.equal(sameTelegramAccount(left, right), false);
  }
});

test("real-shaped null/default snapshot endpoints recover single and mixed cards through the original lease exactly once", async () => {
  for (const mixedBatch of [false, true]) {
    for (const ownerAccount of [null, "default"]) {
      for (const [accountId, routeAccount] of [[null, "default"], ["default", null], ["", "default"], [null, ""], ["default", undefined]]) {
        const owner = { ...binding, accountId: ownerAccount };
        const liveEndpoint = structuredClone(endpoint);
        liveEndpoint.accountId = accountId;
        liveEndpoint.route.accountId = routeAccount;
        const h = harness({ owner, liveEndpoint, mixedBatch });
        await h.coordinator.processEndpointNotification(h.identityRouter, owner, h.client,
          new AbortController().signal, liveEndpoint, hint());
        await h.runner.waitForIdle();
        assert.equal(h.calls.filter(item => item.name === "agentbridge_host_task_prepare").length, 1, JSON.stringify({ mixedBatch, ownerAccount, accountId, routeAccount }));
        const prepare = h.calls.find(item => item.name === "agentbridge_host_task_prepare");
        assert.equal(prepare.options.meta["io.agentbridge/task"].coordinatorLeaseVersion, "7");
        assert.equal(prepare.args.task_id, taskId);
        assert.equal(h.sent.length, 1);
        assert.equal(h.sent[0].accountId, "default");
        assert.equal(h.sent[0].payload.presentation.blocks.at(-1).buttons[0].label,
          mixedBatch ? "第 1/2 项 · 核对周报" : "核对月度考勤");
        const card = h.coordinator.pendingForSession(sessionKey)[0];
        assert.equal(card.state, "pending");
        assert.equal(h.endpointPresentation().presentation.endpointId, endpointId);
        assert.equal(h.endpointPresentation().presentation.individualized, true);
        assert.equal(card.presentation.flowVersion, "agentbridge.trusted-flow.v1");
        assert.equal(h.calls.filter(item => item.name === "agentbridge_host_interaction_present").length, 1);
        for (const [nextAccount, nextRoute] of [["default", null], [null, "default"], ["", ""]]) {
          liveEndpoint.accountId = nextAccount; liveEndpoint.route.accountId = nextRoute;
          await h.coordinator.processEndpointNotification(h.identityRouter, owner, h.client,
            new AbortController().signal, liveEndpoint, { ...hint(), deliveryId: "same-owned-task-alias" });
          await h.runner.waitForIdle();
          assert.equal(h.calls.filter(item => item.name === "agentbridge_host_task_prepare").length, 1);
          assert.equal(h.coordinator.pendingForSession(sessionKey)[0].presentation.url,
            "https://cards.example.test/flow/input/original-opaque-submission/present/endpoint-ticket");
        }
        assert.equal(h.calls.some(item => /commit|resume/.test(item.name)), false);
        h.runner.stop(); h.coordinator.stopAll();
      }
    }
  }
});

test("semantic defaults do not admit another bot, sender, conversation, thread, or extra identity input", async () => {
  for (const change of [value => { value.endpoint.accountId = value.endpoint.route.accountId = "other"; },
    value => { value.endpoint.accountId = "primary"; value.endpoint.route.accountId = " primary "; },
    value => { value.endpoint.externalSubject = "2002"; }, value => { value.endpoint.route.to = "2002"; },
    value => { value.endpoint.conversationRef = "agent:main:telegram:direct:2002"; },
    value => { value.endpoint.route.threadId = "99"; }, value => { value.endpoint.tokenId = "untrusted-token"; }]) {
    const value = command(); change(value);
    const h = harness(); const scheduled = h.runner.schedule(value, { binding, client: h.client });
    if (scheduled.accepted) await scheduled.promise;
    assert.equal(h.calls.some(item => item.name === "agentbridge_host_task_prepare"), false);
    assert.equal(h.sent.length, 0);
    h.runner.stop(); h.coordinator.stopAll();
  }
});

test("startup Telegram recovery presents and delivers default aliases but rejects changed custom accounts and routes", async () => {
  for (const change of [null, endpoint => { endpoint.route.to = "2002"; }, endpoint => { endpoint.route.threadId = "99"; },
    endpoint => { endpoint.accountId = endpoint.route.accountId = "other"; }]) {
    const calls = [], sent = [], hooks = {};
    const liveEndpoint = structuredClone(endpoint);
    liveEndpoint.accountId = null;
    liveEndpoint.route.accountId = "default";
    change?.(liveEndpoint);
    const input = interaction({ type: "business_input", presentation: { url: "https://cards.example.test/input/startup-original" },
      display: { businessView: { schemaVersion: "agentbridge.business-view.v1", profileVersion: "1",
        profileId: "oa.attendance_confirmation", viewKind: "approval" } } });
    let pinned = false;
    const client = { async callTool(name) {
      calls.push(name);
      if (name === "agentbridge_host_task_recovery_list") return { recoveries: [{ task: { taskId }, interaction: input, endpoint: liveEndpoint }] };
      if (name === "agentbridge_host_interaction_present") return { interaction: { ...input, presentation: {
        ...input.presentation, url: "https://cards.example.test/flow/input/startup-original/present/endpoint-ticket",
        endpointId, presentationId: "endpoint-ticket", individualized: true, flowVersion: "agentbridge.trusted-flow.v1" } } };
      return { status: "succeeded", notifications: [] };
    } };
    const owner = { bound: true, binding, client };
    const router = { enabled: true, config: {}, configuredIdentities: () => [{ binding, client }],
      resolvePinnedSession: () => pinned ? owner : null, resolveToolContext: () => pinned ? owner : { bound: false },
      restoreSessionBinding: () => { pinned = true; return true; }, clientForSession: () => client,
      endpointKeyForSession: () => binding.key, catalogCacheKeyForBinding: () => "fixture-credential" };
    const api = { pluginConfig: { allowedCardOrigins: ["https://cards.example.test"], autoPoll: false, syncTimeline: false }, config: {},
      logger: { info() {}, warn() {} }, runtime: { channel: { outbound: { async loadAdapter() { return {
        renderPresentation: ({ payload }) => payload, async sendPayload(value) { sent.push(value); },
      }; } } } }, registerAgentToolResultMiddleware() {}, registerTool() {}, registerCommand() {}, registerGatewayMethod() {},
      on(name, callback) { hooks[name] = callback; } };
    const coordinator = registerAgentBridgeInteractions(api, { identityRouter: router });
    await hooks.gateway_start();
    if (!change) {
      assert.equal(sent.length, 1);
      assert.equal(sent[0].accountId, "default");
      assert.equal(calls.filter(name => name === "agentbridge_host_interaction_present").length, 1);
      assert.equal(coordinator.pendingForSession(sessionKey)[0].presentation.url,
        "https://cards.example.test/flow/input/startup-original/present/endpoint-ticket");
    } else {
      assert.equal(sent.length, 0);
      assert.equal(calls.includes("agentbridge_host_interaction_present"), false);
    }
    hooks.gateway_stop();
    assert.equal(calls.some(name => /commit|resume/.test(name)), false);
  }
});
