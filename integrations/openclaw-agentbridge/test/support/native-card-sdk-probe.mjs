// Optional local-only proof. Never loads a channel monitor, reads a production
// config or calls getUpdates. The official outbound SDK uses a loopback Bot API.
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { mkdtempSync, rmSync, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { nativeFixture } from './native-card-fixture.mjs';
import { nativeCardClosureText } from '../../lib/telegram-native-cards.js';
const sdkDir = process.argv[2];
if (!sdkDir || !path.isAbsolute(sdkDir)) throw Error('ABSOLUTE_REVIEWED_SDK_DIR_REQUIRED');
const home = mkdtempSync(path.join(os.tmpdir(), 'oc-native-card-sdk-'));
process.env.OPENCLAW_STATE_DIR = path.join(home, 'sdk-state');
process.env.OPENCLAW_CONFIG_PATH = path.join(home, 'isolated-config.json');
const wire = [], callbacks = [];
let replyMode = 'ok';
const server = http.createServer(async (request, response) => {
  const chunks = []; for await (const chunk of request) chunks.push(chunk);
  if (request.method !== 'POST' || !request.url.endsWith('/sendMessage')) {
    response.writeHead(400); response.end(JSON.stringify({ ok: false, error_code: 400, description: 'Unexpected local fixture method' })); return;
  }
  const body = JSON.parse(Buffer.concat(chunks)); wire.push(body);
  assert.equal(String(body.chat_id), '1001');
  if (replyMode === 'unknown') { request.socket.destroy(); return; }
  if (replyMode === '429') {
    response.writeHead(429, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ ok: false, error_code: 429, parameters: { retry_after: 2 }, description: 'Synthetic wait' })); return;
  }
  response.setHeader('Content-Type', 'application/json');
  response.end(JSON.stringify({ ok: true, result: { message_id: 800 + wire.length, date: 1795740000,
    chat: { id: 1001, type: 'private' }, from: { id: 777, is_bot: true, first_name: 'Synthetic' }, text: body.text } }));
});
server.listen(0, '127.0.0.1'); await once(server, 'listening');
let h;
try {
  const { telegramPlugin } = await import(pathToFileURL(path.join(sdkDir, 'dist/extensions/telegram/channel-plugin-api.js')).href);
  const native = telegramPlugin.outbound;
  assert.equal(typeof native.sendPayload, 'function');
  const adapter = { ...native, async sendPayload(context) {
    return native.sendPayload({ ...context, onDeliveryResult: async result => {
      callbacks.push({ messageId: result.messageId, chatId: result.chatId });
      await context.onDeliveryResult?.(result);
    } });
  } };
  const hostConfig = { channels: { telegram: { botToken: '777:synthetic-only',
    apiRoot: `http://127.0.0.1:${server.address().port}`, retry: { attempts: 4 } } } };
  h = nativeFixture(home, { adapter, hostConfig });
  const card = h.card();
  const answer = await h.reply(card, '普通公开答复保留一次');
  assert.deepEqual(answer, { payload: { text: '普通公开答复保留一次' } });
  assert.equal(wire.length, 1); assert.equal(h.rows()[0].card.receipt, 801);
  await native.sendPayload({ cfg: hostConfig, to: '1001', accountId: 'default', text: answer.payload.text,
    payload: answer.payload, formatting: { parseMode: 'HTML' } });
  assert.equal(wire.length, 2); assert.equal(wire[1].text, answer.payload.text);
  const buttons = wire[0].reply_markup.inline_keyboard.flat();
  assert.ok(buttons.length > 0); assert.ok(buttons.every(button => new URL(button.url || button.web_app?.url).origin === 'https://cards.example.test'));
  h.finish();
  const record = h.bridge.records.get(card.interactionId);
  record.interaction.state = 'completed';
  record.readContinuation = { toolName: 'oa_workflow_pending_list', arguments: { limit: 2 }, capturedAt: h.now };
  h.bridge.config = { ...h.bridge.config, wakeAgentOnComplete: true };
  let reads = 0;
  h.extraTool = async name => name === 'oa_workflow_pending_list'
    ? (reads++, { status: 'succeeded', result: { items: [] } }) : undefined;
  await h.bridge.notify(record, 'completed', null, [], { resumeOriginalRequest: true });
  await h.bridge.notify(record, 'completed', null, [], { resumeOriginalRequest: true });
  assert.equal(reads, 1); assert.equal(wire.length, 3);
  h.restart(); await h.observe();
  assert.equal(wire.length, 4); assert.equal(wire[3].text, nativeCardClosureText('credential', 'completed'));
  assert.equal(h.rows()[0].closure.receipt, 804);
  h.restart(); await h.observe(); await h.observe(); assert.equal(wire.length, 4);
  assert.ok(h.calls.every(call => call.name === 'oa_workflow_pending_list' || call.name === 'agentbridge_host_notification_claim' || call.args.state_only));
  replyMode = 'unknown';
  const uncertain = await h.reply(h.card(2));
  assert.ok(uncertain.payload.text.includes('尚未确认')); assert.equal(wire.length, 5);
  h.restart(); replyMode = 'ok'; await h.observe(); assert.equal(wire.length, 5);
  replyMode = '429'; await h.reply(h.card(3)); assert.equal(wire.length, 6);
  h.restart(); replyMode = 'ok'; await h.observe(); assert.equal(wire.length, 6);
  h.now += 2001; await h.observe(); assert.equal(wire.length, 7);
  const guardedSources = ['after_capture', 'before_callback', 'storage_unavailable', 'adapter_unavailable'];
  for (const condition of guardedSources) {
    const boundary = nativeFixture(path.join(home, condition), { adapter, hostConfig, taskId: null });
    try {
      const original = boundary.card();
      if (condition === 'before_callback') boundary.beforeNativeCapture = () => { boundary.token = 'changed-before-result'; };
      await boundary.captureStandalone(original);
      if (condition === 'after_capture') boundary.token = 'changed-before-first-send';
      if (condition === 'storage_unavailable') boundary.bridge.nativeCards.store = null;
      if (condition === 'adapter_unavailable') boundary.api.runtime.channel.outbound.loadAdapter = async () => null;
      const before = wire.length;
      const ordinary = await boundary.reply(original, '原公开文字保留');
      assert.equal(ordinary.payload.presentation, undefined);
      assert.ok(ordinary.payload.text.startsWith('原公开文字保留'));
      assert.equal(wire.length, before, 'A known original source cannot fall back to a card POST');
      await native.sendPayload({ cfg: hostConfig, to: '1001', accountId: 'default', payload: ordinary.payload,
        text: ordinary.payload.text, formatting: { parseMode: 'HTML' } });
      assert.equal(wire.length, before + 1);
      assert.equal(wire.at(-1).reply_markup?.inline_keyboard?.flat().length || 0, 0);
      await boundary.bridge.deliverInteractionsDirect(boundary.sessionKey, [original]);
      assert.equal(wire.length, before + 1, 'Direct delivery cannot resend the old source either');
      const pending = await boundary.command.handler({ sessionKey: boundary.sessionKey, channelId: 'telegram', args: 'pending' });
      assert.equal(pending.presentation, undefined, 'The user command cannot bypass the same source boundary');
      if (condition !== 'adapter_unavailable') assert.equal(boundary.calls.length, 0);
    } finally { boundary.close(); }
  }
  console.log(JSON.stringify({ passed: true, proof: 'actual-sdk-loopback-synthetic-central', sdkVersion: JSON.parse(readFileSync(path.join(sdkDir, 'package.json'))).version,
    originalCardWire: 1, ordinaryAnswerWire: 1, originalReadContinuationWire: 1, originalReadCalls: reads,
    closureWire: 1, callbackReceipts: callbacks.length, replayedAfterRestart: 0,
    unknownHttpAttempts: 1, rateLimitedHttpAttempts: 1, retryAfterNewAttemptWire: 1,
    knownSourceBoundary: { scenarios: guardedSources, legacyCardHttpAttempts: 0, preservedOrdinaryReplies: 4 },
    closuresUseStateOnly: true, realTelegram: false, platformPolling: false,
    textSha256: wire.map(body => createHash('sha256').update(body.text).digest('hex')) }));
} finally { h?.close(); await new Promise(resolve => server.close(resolve)); rmSync(home, { recursive: true, force: true }); }
