import test from "node:test";
import assert from "node:assert/strict";
import { createTaskCommandRunner } from "../lib/task-commands.js";
import { createHostRuntimeReporter } from "../lib/runtime-reporter.js";
import { InteractionCoordinator } from "../lib/coordinator.js";
import { resolvePluginConfig } from "../lib/config.js";
import { interaction } from "./fixtures.js";

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

function harness({ badLease = false, missingIdentity = true, owner = binding, offlineSnapshot = false, noCommands = false } = {}) {
  const calls = [], sent = [], bindings = new Map();
  if (!missingIdentity) bindings.set(sessionKey, binding.key);
  let pending = !noCommands;
  const client = { async callTool(name, args, options) {
    calls.push({ name, args, options });
    if (name === "agentbridge_host_runtime_snapshot") {
      if (offlineSnapshot) throw new Error("synthetic offline snapshot");
      return { status: "succeeded", pendingCommands: pending ? [command()] : [] };
    }
    if (name === "agentbridge_host_coordinator_lease_acquire") return { coordinatorLease: {
      taskId, hostInstanceId: badLease ? "another-host" : "openclaw-gateway", state: "active", version: 7,
      expiresAt: new Date(Date.now() + 600_000).toISOString() } };
    if (name === "agentbridge_host_task_prepare") {
      pending = false;
      return { status: "requires_user_action", interaction: interaction({ type: "business_input",
        presentation: { url: "https://cards.example.test/flow/input/fixture" }, display: { businessView: {
          schemaVersion: "agentbridge.business-view.v1", profileId: "oa.attendance_confirmation", profileVersion: "1", viewKind: "approval" } } }) };
    }
    return { status: "succeeded" };
  } };
  const identityRouter = { enabled: true,
    configuredIdentities: () => [{ binding: owner, client }],
    resolvePinnedSession: ({ sessionKey }) => bindings.has(sessionKey)
      ? { bound: true, binding: { ...binding, key: bindings.get(sessionKey) }, client } : null,
    async resolveWorkspaceSession() { throw new Error("Telegram must not enter Workspace resolver"); },
    restoreSessionBinding({ sessionKey, bindingKey }) {
      if (bindings.has(sessionKey) && bindings.get(sessionKey) !== bindingKey) return false;
      bindings.set(sessionKey, bindingKey); return true;
    },
    endpointKeyForSession: sessionKey => bindings.get(sessionKey),
  };
  const coordinator = new InteractionCoordinator({ config: resolvePluginConfig({ allowedCardOrigins: ["https://cards.example.test"],
    autoPoll: false, wakeAgentOnComplete: false }), api: { logger: { info() {}, warn() {} }, config: {}, runtime: {
      channel: { outbound: { async loadAdapter() { return { async renderPresentation({ payload }) { return payload; },
        async sendPayload(payload) { sent.push(payload); } }; } } } } } });
  const runner = createTaskCommandRunner({ identityRouter, coordinator });
  const reporter = createHostRuntimeReporter({ identityRouter, coordinator, taskCommandRunner: runner });
  coordinator.taskCommandRunner = runner;
  coordinator.hostRuntimeReporter = reporter;
  return { calls, sent, client, identityRouter, coordinator, runner, reporter };
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
