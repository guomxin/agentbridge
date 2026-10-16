import test from "node:test";
import assert from "node:assert/strict";
import { InteractionCoordinator, createInteractionSharedState } from "../lib/coordinator.js";
import { resolvePluginConfig } from "../lib/config.js";

const sessionKey = "agent:main:agentbridge-workspace:direct:synthetic-user";
const binding = { key: "synthetic-binding", channel: "webchat" };
const router = { restoreSessionBinding: () => true };
const endpoint = { clientType: "web", conversationRef: sessionKey };
const tick = () => new Promise(resolve => setImmediate(resolve));
function coordinator(options = {}) {
  return new InteractionCoordinator({ config: resolvePluginConfig({ autoPoll: false, syncTimeline: false,
    wakeAgentOnComplete: false, allowedCardOrigins: ["https://bridge.test"], ...options.config }),
    sharedState: createInteractionSharedState(), api: { logger: { info() {}, warn() {}, debug() {} },
      runtime: { system: { enqueueSystemEvent() {}, requestHeartbeat() {} } } }, ...options });
}
function completed(index, taskId = `task-${index}`) {
  return { deliveryId: `delivery-${index}`, deliveryMode: "origin_handled",
    event: { eventType: "task.interaction.completed", payload: { interactionId: `interaction-${index}` } },
    task: { taskId, activeConversationRef: sessionKey },
    interaction: { schemaVersion: "agentbridge.interaction.v1", interactionId: `interaction-${index}`,
      type: "execution_authorization", state: "completed", title: "Synthetic", message: "Synthetic",
      presentation: { url: "https://bridge.test/auth/synthetic" }, resume: { ready: true, completed: false } } };
}
function lease() { return { status: "succeeded", coordinatorLease: { hostInstanceId: "openclaw-gateway", version: 1 } }; }

test("notification claiming returns while a slow resume runs and another task completes", async () => {
  let releaseSlow;
  const slow = new Promise(resolve => { releaseSlow = resolve; });
  const calls = [];
  const bridge = coordinator();
  let presents = 0;
  bridge.interactionPresenter = () => { presents += 1; throw new Error("completed decisions need no presentation"); };
  const client = { async callTool(name, args) {
    calls.push({ name, args });
    if (name === "agentbridge_host_notification_claim") return { endpoint, notifications: [completed(1), completed(2)] };
    if (name === "agentbridge_host_coordinator_lease_acquire") return lease();
    if (name === "agentbridge_interaction_resume") {
      if (args.interaction_id === "interaction-1") await slow;
      return { status: "succeeded", nextAction: { type: "original_request_completed" }, taskStatus: "succeeded" };
    }
    return { status: "succeeded" };
  } };
  const claim = await bridge.deliverEndpointNotifications(router, binding, client, new AbortController().signal, { enqueue: true });
  assert.equal(claim.count, 2);
  await tick();
  assert.equal(calls.some(item => item.name.endsWith("_ack") && item.args.delivery_id === "delivery-2"), true);
  assert.equal(calls.some(item => item.name.endsWith("_ack") && item.args.delivery_id === "delivery-1"), false);
  assert.equal(presents, 0);
  releaseSlow();
  await bridge.waitForIdle();
  assert.equal(calls.filter(item => item.name === "agentbridge_interaction_resume").length, 2);
  assert.equal(calls.filter(item => item.name.endsWith("_ack")).length, 2);
});

test("bounded workers serialize each task across endpoints and deduplicate delivery IDs", async () => {
  const bridge = coordinator({ config: resolvePluginConfig({ notificationConcurrency: 2 }) });
  const signal = new AbortController().signal;
  const started = [];
  let release;
  const block = new Promise(resolve => { release = resolve; });
  bridge.enqueueNotification(binding, completed(1, "same-task"), signal, async () => { started.push(1); await block; });
  bridge.enqueueNotification({ key: "other-endpoint" }, completed(2, "same-task"), signal, async () => { started.push(2); });
  bridge.enqueueNotification(binding, completed(3, "other-task"), signal, async () => { started.push(3); });
  bridge.enqueueNotification(binding, completed(3, "other-task"), signal, async () => { started.push("duplicate"); });
  await tick();
  assert.deepEqual(started, [1, 3]);
  release();
  await bridge.waitForIdle();
  assert.deepEqual(started, [1, 3, 2]);
  assert.equal(bridge.notificationJobs.size, 0);
});

test("duplicate completion events and a lost resume response do not replay a business write", async () => {
  const bridge = coordinator();
  const notification = completed(1);
  let resumes = 0;
  const idempotency = [];
  const client = { async callTool(name, args) {
    if (name === "agentbridge_host_notification_claim") return { endpoint, notifications: [notification,
      { ...notification, deliveryId: "another-delivery" }] };
    if (name === "agentbridge_host_coordinator_lease_acquire") return lease();
    if (name === "agentbridge_interaction_resume") { resumes += 1; idempotency.push(args.idempotency_key); throw new Error("reply lost after synthetic write"); }
    return { status: "succeeded" };
  } };
  await bridge.deliverEndpointNotifications(router, binding, client, new AbortController().signal, { enqueue: true });
  await bridge.waitForIdle();
  await bridge.deliverEndpointNotifications(router, binding, client, new AbortController().signal, { enqueue: true });
  await bridge.waitForIdle();
  assert.equal(resumes, 1);
  assert.deepEqual(idempotency, ["openclaw:interaction-1"]);
  assert.equal(bridge.resumeClaims.has("interaction-1"), true);
});

test("an ungranted lease never dispatches and later exact lease recovery dispatches once", async () => {
  const bridge = coordinator();
  let available = false;
  let resumes = 0;
  let acquisitions = 0;
  const client = { async callTool(name) {
    if (name === "agentbridge_host_notification_claim") return { endpoint, notifications: [completed(1)] };
    if (name === "agentbridge_host_coordinator_lease_acquire") { acquisitions += 1; return available ? lease() : { status: "failed" }; }
    if (name === "agentbridge_interaction_resume") { resumes += 1; return { status: "succeeded", nextAction: { type: "original_request_completed" } }; }
    return { status: "succeeded" };
  } };
  const signal = new AbortController().signal;
  await bridge.deliverEndpointNotifications(router, binding, client, signal, { enqueue: true });
  await bridge.waitForIdle();
  assert.equal(resumes, 0);
  assert.equal(bridge.resumeClaims.has("interaction-1"), false);
  available = true;
  await bridge.deliverEndpointNotifications(router, binding, client, signal, { enqueue: true });
  await bridge.waitForIdle();
  assert.equal(acquisitions, 2);
  assert.equal(resumes, 1);
});

test("old-server fallback removes unsupported wait argument and never spins", async () => {
  const controller = new AbortController();
  const sleeps = [];
  const claims = [];
  const bridge = coordinator({ notificationSleep: async ms => { sleeps.push(ms); if (sleeps.length === 3) controller.abort(); } });
  const client = { async callTool(_name, args) {
    claims.push(args);
    if (args.wait_seconds) throw new Error("unsupported wait argument");
    return { endpoint, notifications: [] };
  } };
  await bridge.runEndpointNotificationPump(router, binding, client, controller.signal, 2000);
  assert.equal(claims[0].wait_seconds, 20);
  assert.equal(claims[1].wait_seconds, undefined);
  assert.deepEqual(sleeps, [2000, 2000, 2000]);
});

test("stopping aborts a long poll and leaves queued deliveries unacknowledged", async () => {
  const controller = new AbortController();
  const bridge = coordinator({ config: resolvePluginConfig({ notificationConcurrency: 1 }) });
  bridge.notificationController = controller;
  let release;
  const block = new Promise(resolve => { release = resolve; });
  let queuedRan = false;
  bridge.enqueueNotification(binding, completed(1), controller.signal, async () => { await block; });
  bridge.enqueueNotification(binding, completed(2), controller.signal, async () => { queuedRan = true; });
  const client = { async callTool(_name, _args, options) {
    return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true }));
  } };
  const pump = bridge.runEndpointNotificationPump(router, binding, client, controller.signal, 2000);
  await tick();
  bridge.stopAll();
  release();
  await pump;
  await bridge.waitForIdle();
  assert.equal(queuedRan, false);
  assert.equal(bridge.notificationClaimReservations, 0);
});

test("queue capacity and concurrency stay bounded under a blocked notification burst", async () => {
  const bridge = coordinator();
  let release;
  const block = new Promise(resolve => { release = resolve; });
  let active = 0;
  let maximum = 0;
  for (let i = 0; i < 32; i += 1) assert.equal(bridge.enqueueNotification(binding, completed(i), new AbortController().signal, async () => {
    active += 1; maximum = Math.max(maximum, active); await block; active -= 1;
  }), true);
  assert.equal(bridge.enqueueNotification(binding, completed(33), new AbortController().signal, async () => {}), false);
  await tick();
  assert.equal(maximum, 4);
  assert.equal(bridge.notificationJobs.size, 32);
  release();
  await bridge.waitForIdle();
  assert.equal(maximum, 4);
  assert.equal(bridge.notificationJobs.size, 0);
});

test("simultaneous identity long polls reserve queue capacity before claiming", async () => {
  const controller = new AbortController();
  const claims = [];
  const bridge = coordinator({ config: resolvePluginConfig({ notificationLongPoll: false }), notificationSleep: async (_ms, signal) => {
    await new Promise(resolve => signal.addEventListener("abort", resolve, { once: true }));
  } });
  const client = { async callTool(_name, args, { signal }) {
    claims.push(args);
    await new Promise(resolve => signal.addEventListener("abort", resolve, { once: true }));
    throw new Error("aborted");
  } };
  const pumps = Array.from({ length: 5 }, (_, index) => bridge.runEndpointNotificationPump(router,
    { key: `identity-${index}` }, client, controller.signal, 2000));
  await tick();
  assert.deepEqual(claims.map(item => item.limit), [10, 10, 10, 2]);
  assert.equal(bridge.notificationClaimReservations, 32);
  controller.abort();
  await Promise.all(pumps);
  assert.equal(bridge.notificationClaimReservations, 0);
});

test("ten idle endpoints do not starve a ready endpoint later in configuration", async () => {
  const bridge = coordinator();
  const controller = new AbortController();
  const claimed = [];
  let deliveredReady = false;
  let readyClaimed = false;
  const identities = Array.from({ length: 10 }, (_, index) => ({ binding: { key: `identity-${index}` }, client: {
    async callTool(name, args, { signal } = {}) {
      if (name === "agentbridge_host_notification_claim") {
        claimed.push({ index, args });
        if (index === 9 && !readyClaimed) {
          readyClaimed = true;
          return { waitSupported: true, waitedMs: 1, endpoint, notifications: [completed(9)] };
        }
        await new Promise(resolve => signal.addEventListener("abort", resolve, { once: true }));
        throw new Error("aborted");
      }
      if (name === "agentbridge_host_coordinator_lease_acquire") return lease();
      if (name === "agentbridge_interaction_resume") { deliveredReady = true; return { status: "succeeded", nextAction: { type: "original_request_completed" } }; }
      return { status: "succeeded" };
    },
  } }));
  const pump = bridge.runNotificationPump({ ...router, configuredIdentities: () => identities }, controller.signal, 2000);
  await tick();
  assert.equal(new Set(claimed.map(item => item.index)).size, 10);
  assert.equal(claimed.every(item => item.args.limit === 1 && item.args.wait_seconds === 20), true);
  assert.equal(deliveredReady, true);
  controller.abort();
  await pump;
  await bridge.waitForIdle();
  assert.equal(bridge.notificationClaimReservations, 0);
});
