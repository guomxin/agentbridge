import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { InteractionCoordinator, createInteractionSharedState } from "../lib/coordinator.js";
import { resolvePluginConfig } from "../lib/config.js";

const closedText = "此前发送的登录卡已完成登录，无需再次输入。请以原任务的当前状态和结果为准。";
const uuid = number => `${String(number).padStart(8, "0")}-0000-4000-8000-000000000001`;
const taskId = uuid(90);
const closure = number => ({ deliveryMode: "host_notification_v2", deliveryId: uuid(number),
  attemptCount: 1, pairingRef: "c".repeat(64), sourceTaskId: taskId,
  sourceEventId: uuid(number + 100), notificationKind: "public_status" });
const resultCard = () => ({ deliveryMode: "workspace_query_v2", deliveryId: uuid(70),
  attemptCount: 1, pairingRef: "c".repeat(64), sourceTaskId: taskId });

// Exercise the real Coordinator -> private delivery consumer -> durable store.
// MCP and Telegram are explicit boundary doubles; this is not a platform test.
function fixture(t, options = {}) {
  const home = mkdtempSync(path.join(os.tmpdir(), "agentbridge-card-closure-"));
  t.after(() => rmSync(home, { recursive: true, force: true }));
  const binding = { key: "telegram:default:1001", channel: "telegram", accountId: "default", senderId: "1001" };
  const endpoint = { endpointId: uuid(80), clientType: "telegram", externalSubject: "1001", accountId: "default",
    conversationRef: "agent:main:telegram:direct:1001", route: { channel: "telegram", to: "1001", accountId: "default" } };
  const events = [], sends = [], controls = [];
  const h = { notifications: [closure(1)], failAck: options.failAck || 0, beginUnknown: options.beginUnknown || false,
    issueOverride: options.issueOverride || null, sendError: options.sendError || false };
  const api = { config: { channels: { telegram: { botToken: "777:synthetic-only", retry: { attempts: 3 } } } },
    logger: { warn() {}, debug() {}, info() {} }, runtime: { state: { resolveStateDir: () => home },
      channel: { outbound: { async loadAdapter() { return {
        async renderPresentation({ payload, presentation }) {
          return { ...payload, channelData: { telegram: { buttons: presentation.blocks.at(-1).buttons } } };
        },
        async sendPayload(context) {
          events.push("send"); sends.push(context);
          if (h.sendError && context.payload.text === closedText) throw new Error("ECONNRESET after request write");
          const receipt = { chatId: "1001", messageId: String(200 + sends.length) };
          await context.onDeliveryResult(receipt);
          return receipt;
        },
      }; } } },
      system: { enqueueSystemEvent() { assert.fail("Closing a card must not invoke a model"); },
        requestHeartbeat() { assert.fail("Closing a card must not resume a model"); } } } };
  const config = resolvePluginConfig({ autoPoll: false, syncTimeline: false, wakeAgentOnComplete: false,
    allowedCardOrigins: ["https://cards.example.test"] });
  const client = { async callTool(name, args) {
    if (name === "agentbridge_host_notification_claim") {
      events.push("claim");
      assert.equal(args.agent_host, "openclaw");
      assert.equal(args.endpoint_key, binding.key);
      assert.equal(args.include_notification_v2, true);
      return { endpoint, notifications: h.notifications };
    }
    assert.ok(["agentbridge_host_notification_delivery", "agentbridge_host_workspace_result_view"].includes(name),
      "A closure cannot acquire a business lease or call interaction_resume");
    events.push(args.action); controls.push({ name, ...args });
    const item = h.notifications.find(value => value.deliveryId === args.delivery_id) || closure(1);
    assert.equal(args.endpoint_key, binding.key);
    assert.equal(args.pairing_ref, item.pairingRef);
    if (args.action === "issue") {
      const base = { available: true, leaseExpiresAt: new Date(Date.now() + 60_000).toISOString() };
      if (item.deliveryMode === "workspace_query_v2") return { ...base, publicText: "已查询本人待办。",
        resultView: { schemaVersion: "agentbridge.result-view.v1", sourceTaskId: taskId,
          expiresAt: new Date(Date.now() + 60_000).toISOString(), sourceExpiresAt: new Date(Date.now() + 60_000).toISOString(),
          selectionAvailable: true, presentation: { owner: "agentbridge", endpointId: endpoint.endpointId,
            modelMustNotCollectValues: true, url: `https://cards.example.test/results/${"r".repeat(43)}` } } };
      return { ...base, sourceEventId: item.sourceEventId, sourceTaskId: taskId,
        notificationKind: "public_status", publicText: closedText, ...h.issueOverride };
    }
    if (args.action === "begin") {
      if (h.beginUnknown) throw new Error("begin reply lost");
      return { allowed: true };
    }
    assert.equal(args.action, "ack");
    if (h.failAck-- > 0) throw new Error("ACK reply lost after recording");
    return { recorded: true };
  } };
  const identityRouter = { restoreSessionBinding: () => true, catalogCacheKeyForBinding: () => "original-credential",
    resolvePinnedSession: () => ({ bound: true, binding, client }) };
  const bridges = [];
  const restart = () => {
    const bridge = new InteractionCoordinator({ api, config, sharedState: createInteractionSharedState() });
    bridges.push(bridge); return bridge;
  };
  t.after(() => bridges.forEach(bridge => bridge.stopAll()));
  const claim = bridge => bridge.deliverEndpointNotifications(identityRouter, binding, client, new AbortController().signal);
  return Object.assign(h, { restart, claim, sends, events, controls, binding, api });
}

test("card closure traverses claim/issue/begin/native-send/ACK and retains plain status semantics", async t => {
  const h = fixture(t), bridge = h.restart();
  bridge.markTaskTerminal(taskId, "succeeded");
  await h.claim(bridge);
  assert.deepEqual(h.events, ["claim", "issue", "begin", "send", "ack"]);
  assert.equal(h.sends.length, 1);
  assert.equal(h.sends[0].text, closedText);
  assert.deepEqual(h.sends[0].payload, { text: closedText });
  assert.equal(h.sends[0].cfg.channels.telegram.retry.attempts, 1);
  assert.equal(h.api.config.channels.telegram.retry.attempts, 3);
  assert.equal(h.controls.at(-1).receipt, 201);
  assert.equal(h.controls.at(-1).outcome, "sent");
});

test("two original interaction closure events sharing a task and their original result each deliver once", async t => {
  const h = fixture(t);
  // Central has proved two original interaction receipts. Distinct immutable
  // source events must not collapse merely because task and public text match.
  h.notifications = [closure(1), closure(2), resultCard()];
  await h.claim(h.restart());
  await h.claim(h.restart());
  assert.equal(h.sends.length, 3);
  assert.equal(h.sends.filter(value => value.text === closedText).length, 2);
  assert.equal(h.sends.filter(value => value.payload.channelData).length, 1);
  const acknowledged = h.controls.filter(value => value.action === "ack");
  assert.deepEqual(acknowledged.map(value => value.delivery_id), [uuid(1), uuid(2), uuid(70)]);
  assert.deepEqual(acknowledged.map(value => value.receipt), [201, 202, 203]);
  assert.equal(h.controls.filter(value => value.action === "begin").length, 3);
});

test("lost closure ACK is repaired from the original durable receipt after restart without another send", async t => {
  const h = fixture(t, { failAck: 1 });
  await h.claim(h.restart());
  await h.claim(h.restart());
  await h.claim(h.restart());
  assert.equal(h.sends.length, 1);
  assert.deepEqual(h.controls.filter(value => value.action === "ack").map(value =>
    [value.delivery_id, value.attempt_count, value.outcome, value.receipt]),
  [[uuid(1), 1, "sent", 201], [uuid(1), 1, "sent", 201]]);
  assert.equal(h.controls.filter(value => value.action === "begin").length, 1);
});

test("unknown closure delivery never resends or suppresses the independently sourced original result", async t => {
  const h = fixture(t, { sendError: true });
  await h.claim(h.restart());
  h.notifications = [{ ...closure(1), attemptCount: 2 }, resultCard()];
  await h.claim(h.restart());
  await h.claim(h.restart());
  assert.equal(h.sends.length, 2);
  const acks = h.controls.filter(value => value.action === "ack");
  assert.deepEqual(acks.map(value => [value.delivery_id, value.outcome]),
    [[uuid(1), "delivery_unknown"], [uuid(70), "sent"]]);
  assert.equal(acks[0].receipt, undefined);
});

test("a lost closure begin reply is durable unknown and cannot create a send after restart", async t => {
  const h = fixture(t, { beginUnknown: true });
  await h.claim(h.restart());
  h.beginUnknown = false;
  await h.claim(h.restart());
  h.notifications = [{ ...closure(1), attemptCount: 2 }];
  await h.claim(h.restart());
  assert.equal(h.sends.length, 0);
  assert.equal(h.controls.filter(value => value.action === "begin").length, 1);
  assert.deepEqual(h.controls.filter(value => value.action === "ack").map(value =>
    [value.attempt_count, value.outcome]), [[1, "delivery_unknown"]]);
});

test("foreign source events and private card material cannot enter a public closure payload", async t => {
  for (const issueOverride of [{ sourceEventId: uuid(999) }, { sourceTaskId: uuid(999) },
    { notificationKind: "trusted_interaction" }, { interaction: { presentation: { url: "https://cards.example.test/auth/private" } } }]) {
    const h = fixture(t, { issueOverride });
    await h.claim(h.restart());
    assert.equal(h.sends.length, 0);
    assert.deepEqual(h.events, ["claim", "issue"]);
  }
});
