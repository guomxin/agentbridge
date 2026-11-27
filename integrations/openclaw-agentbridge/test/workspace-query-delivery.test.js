import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
import os from "node:os";
import path from "node:path";
import { WorkspaceQueryDelivery, WorkspaceQueryReceiptStore } from "../lib/workspace-query-delivery.js";
import { InteractionCoordinator } from "../lib/coordinator.js";
import { resolvePluginConfig } from "../lib/config.js";

function harness(t, options = {}) {
  const directory = mkdtempSync(path.join(os.tmpdir(), "agentbridge-openclaw-query-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const binding = { key: "telegram:default:1001", channel: "telegram", senderId: "1001", accountId: "default" };
  const endpoint = { endpointId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", clientType: "telegram", externalSubject: "1001",
    accountId: "default", conversationRef: "agent:main:telegram:direct:1001",
    route: { channel: "telegram", to: "telegram:1001", accountId: "default", threadId: null } };
  const notification = { deliveryMode: "workspace_query_v2", deliveryId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    sourceTaskId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc", attemptCount: 1, pairingRef: "d".repeat(64) };
  if (options.text) {
    delete notification.sourceTaskId;
    Object.assign(notification, { deliveryMode: "workspace_text_v2", sourceEntryId: "entry-1", partIndex: 1, partCount: 1 });
  }
  if (options.kind) Object.assign(notification, { deliveryMode: "host_notification_v2",
    notificationKind: options.kind, sourceEventId: "dddddddd-dddd-4ddd-8ddd-dddddddddddd" });
  if (options.media) {
    delete notification.sourceTaskId;
    Object.assign(notification, { deliveryMode: "workspace_media_v2", itemIndex: 1, itemCount: 2 });
  }
  const store = new WorkspaceQueryReceiptStore(directory);
  const calls = [], sends = [];
  let ackFails = options.ackFails || 0;
  const config = resolvePluginConfig({ allowedCardOrigins: ["https://cards.example.test"] });
  const api = { config: { channels: { telegram: { retry: { attempts: 3 }, richMessages: true,
    accounts: { default: { retry: { attempts: 4 }, richMessages: true }, second: { retry: { attempts: 5 } } } } } },
    logger: { warn() {}, debug() {} }, runtime: { channel: { outbound: { async loadAdapter() {
      return { async renderPresentation({ payload, presentation }) {
        return { ...payload, channelData: { telegram: { buttons: presentation.blocks.at(-1).buttons } } };
      }, async sendPayload(value) {
        sends.push(value);
        if (options.send) return options.send(value, sends.length);
        const result = { channel: "telegram", chatId: "1001", messageId: "123" };
        await value.onDeliveryResult(result);
        return result;
      } };
    } } } } };
  const issued = { available: true, leaseExpiresAt: new Date(Date.now() + 60_000).toISOString(), publicText: "已查询，发现 2 项 OA 待办。",
    resultView: { schemaVersion: "agentbridge.result-view.v1", sourceTaskId: notification.sourceTaskId,
      presentation: { owner: "agentbridge", endpointId: endpoint.endpointId, modelMustNotCollectValues: true,
        url: "https://cards.example.test/results/" + "t".repeat(43) }, selectionAvailable: true,
      expiresAt: new Date(Date.now() + 3_600_000).toISOString(), sourceExpiresAt: new Date(Date.now() + 600_000).toISOString() },
    ...(options.text ? { resultView: null, publicText: "【网页端 · 你】\n查看 OA 待办" } : {}), ...(options.kind ? { resultView: null, publicText: "本次任务已取消。", notificationKind: options.kind,
      sourceTaskId: notification.sourceTaskId, sourceEventId: notification.sourceEventId } : {}),
    ...(options.media ? { publicText: null, resultView: null, resource: {
      fileName: "原始图片.png", mimeType: "image/png", byteSize: mediaBody.length,
      sha256: createHash('sha256').update(mediaBody).digest('hex'), contentBase64: mediaBody.toString('base64'),
      expiresAt: new Date(Date.now()+60_000).toISOString(),
    } } : {}), ...options.issued };
  const client = { async callTool(name, args) {
    calls.push({ name, args });
    if (name === "agentbridge_host_notification_claim") return { endpoint, notifications: [notification] };
    assert.equal(name, options.media ? "agentbridge_host_workspace_media_delivery"
      : options.kind ? "agentbridge_host_notification_delivery" : "agentbridge_host_workspace_result_view");
    assert.equal(args.agent_host, "openclaw");
    if (args.action === "issue") return issued;
    if (args.action === "begin") {
      if (options.beginThrows) throw new Error("lost begin response");
      return options.begin || { allowed: true };
    }
    if (args.action === "ack") {
      if (ackFails-- > 0) throw new Error("lost acknowledgement response");
      return { recorded: true };
    }
    throw new Error("Unexpected action");
  } };
  const identityRouter = { restoreSessionBinding: () => true,
    resolvePinnedSession: ({ sessionKey }) => {
      assert.equal(sessionKey, endpoint.conversationRef);
      return options.identityDrift ? { bound: false } : { bound: true, binding, client };
    } };
  let mediaWrites = 0;
  const consumer = () => new WorkspaceQueryDelivery({ api, config, store, saveMediaBuffer: async body => {
    const target=path.join(directory,`media-${++mediaWrites}.png`);writeFileSync(target,body,{mode:0o600});
    return {path:target,size:body.length};
  } });
  const context = { binding, client, endpoint, notification, identityRouter, signal: new AbortController().signal,
    withDirectDelivery: send => send() };
  return { consumer, context, calls, sends, store, directory, api, config, issued, mediaWrites:()=>mediaWrites };
}
const mediaBody = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=','base64');
const acks = h => h.calls.filter(call => call.args.action === "ack").map(call => call.args);

test("each original media item records one real route receipt and an ACK loss never uploads again",async t=>{
  const h=harness(t,{media:true,ackFails:1});
  await assert.rejects(h.consumer().deliver(h.context),/acknowledgement/);
  assert.equal(h.sends.length,1);assert.equal(h.sends[0].forceDocument,true);
  assert.equal(h.sends[0].payload.forceDocument,undefined);
  assert.equal(h.sends[0].cfg.channels.telegram.retry.attempts,1);
  assert.equal(existsSync(h.sends[0].payload.mediaUrl),false);
  await h.consumer().recover(h.context.binding,h.context.client,h.context.signal);
  await h.consumer().deliver(h.context);
  assert.equal(h.sends.length,1);assert.equal(h.mediaWrites(),1);
  assert.deepEqual(acks(h).map(a=>[a.outcome,a.receipt]),[['sent',123],['sent',123]]);
  const prior=h.store.prior({...h.context.notification,endpointKey:h.context.binding.key})[0];
  assert.equal(prior.resourceSha256,h.issued.resource.sha256);
  assert.equal(prior.resourceByteSize,mediaBody.length);
  assert.equal(JSON.stringify(prior).includes(h.issued.resource.contentBase64),false);
});

test("a partly delivered media group never resends the first item or sends a fallback after an unknown second upload",async t=>{
  const h=harness(t,{media:true,send:async(value,count)=>{
    if(count>1)throw new Error('ECONNRESET after upload');
    await value.onDeliveryResult({messageId:'126',chatId:'1001'});
  }});
  await h.consumer().deliver(h.context);
  const second={...h.context,notification:{...h.context.notification,deliveryId:'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',itemIndex:2}};
  await h.consumer().deliver(second);
  await h.consumer().deliver(h.context);await h.consumer().deliver(second);
  await h.consumer().deliver({...second,notification:{...second.notification,attemptCount:2}});
  assert.equal(h.sends.length,2);assert.equal(h.mediaWrites(),2);
  assert.deepEqual(acks(h).map(a=>a.outcome),['sent','delivery_unknown']);
  assert.ok(h.sends.every(x=>x.forceDocument&&x.payload.mediaUrl&&!x.payload.text.includes('http')));
});

test("corrupted media or revoked permission closes only the original item without a native send",async t=>{
  for(const revoked of [false,true]){
    const h=harness(t,{media:true,...(revoked?{begin:{allowed:false}}:{})});
    if(!revoked)h.issued.resource.sha256='a'.repeat(64);
    await h.consumer().deliver(h.context);await h.consumer().deliver(h.context);
    assert.equal(h.sends.length,0);assert.equal(acks(h)[0].outcome,'unavailable');
    assert.equal(h.calls.filter(c=>c.args.action==='begin').length,revoked?1:0);
  }
});

test("one OA result combines public text and trusted card, uses a genuine matching receipt and retains a tombstone", async t => {
  const h = harness(t);
  await h.consumer().deliver(h.context);
  await h.consumer().deliver(h.context);
  assert.equal(h.sends.length, 1);
  assert.equal(acks(h)[0].outcome, "sent");
  assert.equal(acks(h)[0].receipt, 123);
  assert.equal(h.sends[0].cfg.channels.telegram.retry.attempts, 1);
  assert.equal(h.sends[0].cfg.channels.telegram.accounts.default.retry.attempts, 1);
  assert.equal(h.sends[0].cfg.channels.telegram.accounts.default.richMessages, false);
  assert.equal(h.sends[0].cfg.channels.telegram.accounts.default.network.dnsResultOrder, "verbatim");
  assert.equal(h.api.config.channels.telegram.accounts.default.retry.attempts, 4);
  assert.equal(h.sends[0].formatting.parseMode, "HTML");
  const folder = h.store.folder(h.context.binding.key, true);
  const journal = readFileSync(path.join(folder, readdirSync(folder)[0]), "utf8");
  assert.equal(journal.includes("/results/"), false);
  assert.equal(journal.includes("OA 待办"), false);
  assert.equal(JSON.parse(journal).chatId, "1001");
});

test("a lost ACK is retried after restart without another issue, begin or Telegram send", async t => {
  const h = harness(t, { ackFails: 1 });
  await assert.rejects(h.consumer().deliver(h.context), /acknowledgement/);
  const restarted = h.consumer();
  await restarted.recover(h.context.binding, h.context.client, h.context.signal);
  await restarted.deliver(h.context);
  assert.equal(h.sends.length, 1);
  assert.equal(h.calls.filter(call => call.args.action === "begin").length, 1);
  assert.deepEqual(acks(h).map(ack => [ack.outcome, ack.receipt]), [["sent", 123], ["sent", 123]]);
  assert.equal(h.store.pending(h.context.binding.key).length, 0);
});

test("a lost begin reply becomes unknown on recovery and is never transmitted", async t => {
  const h = harness(t, { beginThrows: true });
  await h.consumer().deliver(h.context);
  await h.consumer().recover(h.context.binding, h.context.client, h.context.signal);
  await h.consumer().deliver(h.context);
  assert.equal(h.sends.length, 0);
  assert.equal(acks(h)[0].outcome, "delivery_unknown");
});

test("an uncertain network outcome is terminal and must not be replayed", async t => {
  const h = harness(t, { send: async () => { throw new Error("ECONNRESET after request write"); } });
  await h.consumer().deliver(h.context);
  await h.consumer().deliver({ ...h.context, notification: { ...h.context.notification, attemptCount: 2 } });
  await h.consumer().recover(h.context.binding, h.context.client, h.context.signal);
  await h.consumer().deliver(h.context);
  assert.equal(h.sends.length, 1);
  assert.equal(acks(h)[0].outcome, "delivery_unknown");
  assert.equal(acks(h)[0].receipt, undefined);
});

test("a journal failure before requesting begin never dispatches and recovers as unsent", async t => {
  const h = harness(t);
  const save = h.store.save.bind(h.store);
  h.store.save = () => { throw new Error("synthetic disk failure"); };
  await assert.rejects(h.consumer().deliver(h.context), /disk failure/);
  assert.equal(h.calls.some(call => call.args.action === "begin"), false);
  assert.equal(h.sends.length, 0);
  h.store.save = save;
  await h.consumer().recover(h.context.binding, h.context.client, h.context.signal);
  assert.equal(acks(h)[0].outcome, "unavailable");
});

test("recovery cannot settle an in-flight send before its native receipt arrives", async t => {
  let release;
  const hold = new Promise(resolve => { release = resolve; });
  const h = harness(t, { send: async value => {
    await hold;
    await value.onDeliveryResult({ messageId: "125", chatId: "1001" });
  } });
  const delivery = h.consumer().deliver(h.context);
  while (!h.sends.length) await new Promise(resolve => setImmediate(resolve));
  await h.consumer().recover(h.context.binding, h.context.client, h.context.signal);
  assert.equal(acks(h).length, 0);
  release();
  await delivery;
  assert.equal(acks(h)[0].outcome, "sent");
});

test("a persisted platform receipt survives a later SDK bookkeeping failure", async t => {
  const h = harness(t, { send: async value => {
    await value.onDeliveryResult({ channel: "telegram", chatId: "1001", messageId: "124" });
    throw new Error("synthetic SDK prompt-cache failure after acceptance");
  } });
  await h.consumer().deliver(h.context);
  assert.equal(acks(h)[0].outcome, "sent");
  assert.equal(acks(h)[0].receipt, 124);
});

test("429 is retried only through a fresh central attempt; an explicit refusal is terminal", async t => {
  const h = harness(t, { send: async (value, count) => {
    if (count === 1) throw Object.assign(new Error("rate limited"), { error_code: 429, parameters: { retry_after: 17 } });
    throw Object.assign(new Error("forbidden"), { error_code: 403 });
  } });
  await h.consumer().deliver(h.context);
  await h.consumer().deliver(h.context);
  assert.equal(h.sends.length, 1);
  assert.equal(acks(h)[0].outcome, "retry_after");
  assert.equal(acks(h)[0].retry_after_seconds, 17);
  await h.consumer().deliver({ ...h.context, notification: { ...h.context.notification, attemptCount: 2 } });
  assert.equal(h.sends.length, 2);
  assert.equal(acks(h)[1].outcome, "rejected");
});

test("foreign chat, bot account, and absent native receipts are never acknowledged as sent", async t => {
  for (const result of [{ messageId: "123", chatId: "1002" },
    { messageId: "123", chatId: "1001", accountId: "second" }, {}, true]) {
    const h = harness(t, { send: async () => result });
    await h.consumer().deliver(h.context);
    assert.equal(acks(h)[0].outcome, "delivery_unknown");
  }
});

test("wrong endpoint, expired issue, identity drift or denied begin cannot send", async t => {
  const wrong = harness(t);
  wrong.context.endpoint.route.accountId = "other-bot";
  await wrong.consumer().deliver(wrong.context);
  assert.equal(wrong.calls.length, 0);
  for (const options of [{ identityDrift: true }, { issued: { leaseExpiresAt: new Date(0).toISOString() } }, { begin: { allowed: false } }]) {
    const h = harness(t, options);
    await h.consumer().deliver(h.context);
    assert.equal(h.sends.length, 0);
  }
});

test("zero-result public text sends without a card; invalid or oversized content never reaches begin", async t => {
  const zero = harness(t, { issued: { resultView: null, publicText: "已查询，发现 0 项 OA 待办。" } });
  await zero.consumer().deliver(zero.context);
  assert.equal(zero.sends.length, 1);
  assert.equal(zero.sends[0].payload.channelData, undefined);
  for (const issued of [{ publicText: "x".repeat(4000) }, { resultView: {} }]) {
    const h = harness(t, { issued });
    await h.consumer().deliver(h.context);
    assert.equal(h.sends.length, 0);
    assert.equal(h.calls.some(call => call.args.action === "begin"), false);
  }
});

test("the notification pump requests v2 explicitly and never sends its legacy boolean ACK", async t => {
  const h = harness(t);
  const coordinator = new InteractionCoordinator({ api: h.api, config: h.config, workspaceQueryDelivery: h.consumer() });
  await coordinator.deliverEndpointNotifications(h.context.identityRouter, h.context.binding, h.context.client, h.context.signal);
  assert.equal(h.calls[0].args.include_workspace_query_v2, true);
  assert.equal(h.calls.some(call => call.name === "agentbridge_host_notification_ack"), false);
  assert.equal(coordinator.directDeliveries.size, 0);
  assert.equal(h.sends.length, 1);
});

test("concurrent duplicate claims do not acquire two permits or send twice", async t => {
  const h = harness(t);
  await Promise.all([h.consumer().deliver(h.context), h.consumer().deliver(h.context)]);
  assert.equal(h.sends.length, 1);
  assert.equal(h.calls.filter(call => call.args.action === "begin").length, 1);
});

test("overlapping direct deliveries keep the mirror guard until the last native send finishes", async t => {
  const releases = [];
  const h = harness(t, { send: value => new Promise(resolve => { releases.push(() => resolve({ messageId: "123", chatId: "1001" })); }) });
  const coordinator = new InteractionCoordinator({ api: h.api, config: h.config, workspaceQueryDelivery: h.consumer() });
  const session = h.context.endpoint.conversationRef;
  const first = coordinator.sendRoutePayload(session, h.context.endpoint.route, { text: "one" });
  const second = coordinator.sendRoutePayload(session, h.context.endpoint.route, { text: "two" });
  while (releases.length !== 2) await new Promise(resolve => setImmediate(resolve));
  releases[0]();
  await first;
  assert.equal(coordinator.directDeliveries.get(session), 1);
  releases[1]();
  await second;
  assert.equal(coordinator.directDeliveries.has(session), false);
});

test("frozen Workspace text preserves whitespace, escapes once and uses the reliable path without legacy ACK", async t => {
  const h = harness(t, { text: true, issued: { publicText: "【网页端 · 智能体】\n <one> & two\n " } });
  const coordinator = new InteractionCoordinator({ api: h.api, config: h.config, workspaceQueryDelivery: h.consumer() });
  await coordinator.deliverEndpointNotifications(h.context.identityRouter, h.context.binding, h.context.client, h.context.signal);
  assert.equal(h.sends.length, 1);
  assert.equal(h.sends[0].payload.text, "【网页端 · 智能体】\n &lt;one&gt; &amp; two\n ");
  assert.equal(h.sends[0].cfg.channels.telegram.retry.attempts, 1);
  assert.equal(h.sends[0].cfg.channels.telegram.network.dnsResultOrder, "verbatim");
  assert.equal(h.calls.some(call => call.name === "agentbridge_host_notification_ack"), false);
  const saved = h.store.prior({ endpointKey: h.context.binding.key, deliveryId: h.context.notification.deliveryId })[0];
  assert.equal(saved.deliveryMode, "workspace_text_v2");
  assert.equal(saved.sourceEntryId, "entry-1");
  assert.equal(saved.partIndex, 1);
  assert.equal(saved.partCount, 1);
  assert.equal(saved.receipt, 123);
  assert.equal(JSON.stringify(saved).includes("one"), false);
});

test("a Workspace text part survives ACK loss and restart without repeating the native send", async t => {
  const h = harness(t, { text: true, ackFails: 1 });
  await assert.rejects(h.consumer().deliver(h.context), /acknowledgement/);
  await h.consumer().recover(h.context.binding, h.context.client, h.context.signal);
  await h.consumer().deliver(h.context);
  assert.equal(h.sends.length, 1);
  assert.equal(h.calls.filter(call => call.args.action === "issue").length, 1);
  assert.deepEqual(acks(h).map(ack => [ack.outcome, ack.receipt]), [["sent", 123], ["sent", 123]]);
});

test("centrally released text parts retain separate original IDs, ordering and platform receipts", async t => {
  const h = harness(t, { text: true, send: async (value, count) => {
    const result = { messageId: String(122 + count), chatId: "1001" };
    await value.onDeliveryResult(result);
    return result;
  } });
  const parts = ["【网页端 · 智能体 · 1/2】\n" + "<&😀".repeat(100), "【网页端 · 智能体 · 2/2】\n末段"];
  const original = h.context.notification;
  for (let index = 0; index < parts.length; index++) {
    h.issued.publicText = parts[index];
    h.context.notification = { ...original, partIndex: index + 1, partCount: 2,
      deliveryId: index ? "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee" : original.deliveryId };
    await h.consumer().deliver(h.context);
  }
  assert.equal(h.sends.length, 2);
  assert.ok(h.sends[0].payload.text.endsWith("&lt;&amp;😀".repeat(100)));
  assert.equal(h.sends[1].payload.text, parts[1]);
  assert.deepEqual(acks(h).map(ack => [ack.delivery_id, ack.receipt]), [
    [original.deliveryId, 123], ["eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", 124],
  ]);
  await h.consumer().recover(h.context.binding, h.context.client, h.context.signal);
  assert.equal(h.sends.length, 2);
});

test("text retries require an explicit 429 and a new attempt; uncertain native outcomes cannot replay", async t => {
  const h = harness(t, { text: true, send: async (value, count) => {
    if (count === 1) throw Object.assign(new Error("rate limited"), { error_code: 429, parameters: { retry_after: 3 } });
    throw new Error("fetch failed after write");
  } });
  await h.consumer().deliver(h.context);
  await h.consumer().deliver(h.context);
  assert.equal(h.sends.length, 1);
  await h.consumer().deliver({ ...h.context, notification: { ...h.context.notification, attemptCount: 2 } });
  await h.consumer().deliver({ ...h.context, notification: { ...h.context.notification, attemptCount: 3 } });
  assert.equal(h.sends.length, 2);
  assert.deepEqual(acks(h).map(ack => ack.outcome), ["retry_after", "delivery_unknown"]);
});

test("malformed text metadata, unexpected cards and oversized escaped text never get a send permit", async t => {
  for (const invalid of [{ sourceEntryId: "" }, { partIndex: 0 }, { partIndex: 2 }, { partCount: 1.5 }]) {
    const h = harness(t, { text: true });
    Object.assign(h.context.notification, invalid);
    await h.consumer().deliver(h.context);
    assert.equal(h.calls.length, 0);
  }
  for (const issued of [{ resultView: {} }, { publicText: "&".repeat(701) }]) {
    const h = harness(t, { text: true, issued });
    await h.consumer().deliver(h.context);
    assert.equal(h.sends.length, 0);
    assert.equal(h.calls.some(call => call.args.action === "begin"), false);
  }
});

test("an original text subscription revoked between issue and begin cannot send", async t => {
  const h = harness(t, { text: true });
  const call = h.context.client.callTool.bind(h.context.client);
  h.context.client.callTool = async (name, args) => {
    const result = await call(name, args);
    if (args.action === "begin") h.context.identityRouter.resolvePinnedSession = () => ({ bound: false });
    return result;
  };
  await h.consumer().deliver(h.context);
  assert.equal(h.sends.length, 0);
  assert.equal(acks(h)[0].outcome, "delivery_unknown");
});

test("a terminal notification keeps its own event custody and recovers only the native receipt", async t => {
  const h = harness(t, { kind: "public_status", ackFails: 1 });
  await assert.rejects(h.consumer().deliver(h.context));
  await h.consumer().recover(h.context.binding, h.context.client, h.context.signal);
  await h.consumer().deliver(h.context);
  assert.equal(h.sends.length, 1);
  assert.equal(h.sends[0].text, "本次任务已取消。");
  assert.deepEqual(h.calls.map(x => [x.name, x.args.action]), ["issue", "begin", "ack", "ack"].map(action =>
    ["agentbridge_host_notification_delivery", action]));
});

test("generic task notifications verify kind, original event and task before requesting the permit", async t => {
  for (const issued of [{ sourceTaskId: "other" }, { sourceEventId: "other" },
    { notificationKind: "trusted_interaction" }, { interaction: {} }]) {
    const h = harness(t, { kind: "public_status", issued });
    await h.consumer().deliver(h.context);
    assert.equal(h.sends.length, 0);
    assert.deepEqual(h.calls.map(x => x.args.action), ["issue"]);
  }
});

test("private interactions use the original endpoint's card and keep links out of the ordinary text", async t => {
  const card = {
    schemaVersion: "agentbridge.interaction.v1", interactionId: "e".repeat(32), state: "pending", type: "credential",
    title: "登录", message: "请核对", expiresAt: new Date(Date.now() + 60_000).toISOString(),
    presentation: { owner: "agentbridge", modelMustNotCollectValues: true,
      endpointId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", url: "https://cards.example.test/flow/input/fixture" },
  };
  for (const invalid of [null, { presentation: { ...card.presentation, endpointId: "other" } },
    { presentation: { ...card.presentation, url: "https://invalid.example/fixture" } },
    { expiresAt: "2000-01-01T00:00:00Z" }]) {
    const h = harness(t, { kind: "trusted_interaction", issued: { publicText: null, interaction: { ...card, ...invalid } } });
    await h.consumer().deliver(h.context);
    assert.equal(h.sends.length, invalid ? 0 : 1);
    if (!invalid) {
      assert.ok(!h.sends[0].text.includes("https:"));
      assert.ok(h.sends[0].payload.channelData.telegram.buttons.some(button => button.webApp?.url === card.presentation.url));
    } else assert.deepEqual(h.calls.map(x => x.args.action), ["issue"]);
  }
});

test('native references require original Bot route and credential custody in the durable actual-send receipt',async t=>{
  const h=harness(t,{text:true});
  h.api.config.channels.telegram.botToken='777:synthetic-original';
  let credential='scope-original';h.context.identityRouter.catalogCacheKeyForBinding=()=>credential;
  const consumer=h.consumer();
  assert.equal(consumer.provesReceipt(h.context.binding,123,h.context.identityRouter),false);
  await consumer.deliver(h.context);
  assert.equal(h.consumer().provesReceipt(h.context.binding,123,h.context.identityRouter),true);
  assert.equal(consumer.provesReceipt({...h.context.binding,senderId:'2002'},123,h.context.identityRouter),false);
  h.api.config.channels.telegram.botToken='888:synthetic-replaced-bot';
  assert.equal(consumer.provesReceipt(h.context.binding,123,h.context.identityRouter),false);
  h.api.config.channels.telegram.botToken='777:rotated-same-bot';credential='scope-replaced';
  assert.equal(consumer.provesReceipt(h.context.binding,123,h.context.identityRouter),false);
  credential='scope-original';assert.equal(consumer.provesReceipt(h.context.binding,123,h.context.identityRouter),true);
  const record=h.store.matchingReceipt(h.context.binding.key,123)[0];
  assert.ok(record.botRouteRef);assert.ok(record.credentialRef);
  assert.equal(JSON.stringify(record).includes('synthetic-original'),false);
  delete record.botRouteRef;writeFileSync(h.store.file(record,true),JSON.stringify(record));
  assert.equal(consumer.provesReceipt(h.context.binding,123,h.context.identityRouter),false);
});
