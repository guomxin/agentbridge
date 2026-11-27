import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { nativeCardClosureText } from '../lib/telegram-native-cards.js';
import { nativeFixture, uuid } from './support/native-card-fixture.mjs';


function fixture(t, options = {}) {
  const home = mkdtempSync(path.join(os.tmpdir(), 'native-card-closure-'));
  const h = nativeFixture(home, options); t.after(() => { h.close(); rmSync(home, { recursive: true, force: true }); }); return h;
}

test('normal reply preserves public answer, sends original card once and closes once across a real store restart', async t => {
  const h = fixture(t), card = h.card();
  const reply = await h.reply(card, '普通公开答复');
  assert.deepEqual(reply, { payload: { text: '普通公开答复' } });
  assert.equal(h.sends.length, 1); assert.equal(h.rows()[0].card.receipt, 501);
  assert.ok(h.sends[0].payload.channelData.telegram.buttons.length);
  assert.equal(h.sends[0].cfg.channels.telegram.retry.attempts, 1);
  assert.equal(h.api.config.channels.telegram.retry.attempts, 4);
  assert.equal(h.calls.filter(call => !call.args.state_only && call.name.includes('present')).length, 0);
  await h.reply(card); assert.equal(h.sends.length, 1);
  h.finish(); h.restart(); await h.observe();
  assert.equal(h.sends.length, 2); assert.equal(h.sends[1].payload.text, nativeCardClosureText('credential', 'completed'));
  assert.equal(h.rows()[0].closure.receipt, 502);
  h.restart(); await h.observe(); await h.observe(); assert.equal(h.sends.length, 2);
  assert.ok(h.calls.every(call => call.name === 'agentbridge_host_notification_claim' || call.args.state_only));
  assert.equal(JSON.stringify(h.rows()).includes('/auth/'), false);
});

for (const state of ['completed', 'expired', 'declined', 'failed', 'superseded']) {
  test(`${state} is authoritative and closes the exact original once without changing current task`, async t => {
    const h = fixture(t); await h.reply(); h.finish(1, state);
    h.task.currentInteractionId = uuid(2); await h.observe(); await h.observe();
    assert.equal(h.sends.length, 2); assert.equal(h.sends[1].text, nativeCardClosureText('credential', state));
    assert.equal(h.task.currentInteractionId, uuid(2));
  });
}

test('same task with two distinct interactions retains two independent card/closure identities', async t => {
  const h = fixture(t); await h.reply(h.card(1)); await h.reply(h.card(2, 'business_input'));
  h.finish(1, 'superseded'); h.finish(2); await h.observe(); h.restart(); await h.observe();
  assert.equal(h.sends.length, 4); assert.equal(h.rows().length, 2);
  assert.deepEqual(new Set(h.rows().flatMap(row => [row.card.receipt, row.closure.receipt])).size, 4);
});

for (const mode of ['transport', 'wrong_chat', 'missing_receipt', 'callback_throw']) {
  test(`original ${mode} does not replay or manufacture a closure on restart`, async t => {
    const h = fixture(t); h.sendBehavior = async context => {
      if (mode === 'transport') throw Error('ECONNRESET');
      if (mode === 'wrong_chat') return { chatId: '1002', messageId: '501' };
      if (mode === 'missing_receipt') return {};
      await context.onDeliveryResult({ chatId: '1001', messageId: '501' }); throw Error('SDK post-send bookkeeping failed');
    };
    const reply = await h.reply(); assert.equal(h.sends.length, 1);
    assert.equal(reply.payload.text.includes('尚未确认'), mode !== 'callback_throw');
    h.finish(); h.restart(); h.sendBehavior = null; await h.observe(); await h.observe();
    assert.equal(h.sends.length, mode === 'callback_throw' ? 2 : 1);
    assert.equal(h.rows()[0].card.state, mode === 'callback_throw' ? 'sent' : 'delivery_unknown');
  });
}

test('unknown closure freezes its own send and never resends the known original card', async t => {
  const h = fixture(t); await h.reply(); h.finish(); h.sendBehavior = async () => { throw Error('write completed, response lost'); };
  await h.observe(); assert.equal(h.rows()[0].closure.state, 'delivery_unknown');
  h.restart(); h.sendBehavior = null; await h.observe(); assert.equal(h.sends.length, 2);
});

test('original explicit 429 alone gets a new attempt and a fresh authorized presentation after its due time', async t => {
  const h = fixture(t); h.sendBehavior = async () => { throw { error_code: 429, parameters: { retry_after: 3 } }; };
  await h.reply(); h.restart(); h.sendBehavior = null; await h.observe(); assert.equal(h.sends.length, 1);
  h.now += 3001; await h.observe(); assert.equal(h.sends.length, 2); assert.equal(h.rows()[0].card.attempt, 2);
  assert.equal(h.calls.filter(call => call.name.includes('present') && !call.args.state_only).length, 1);
  h.finish(); await h.observe(); assert.equal(h.sends.length, 3);
});

test('closure explicit 429 waits, retries only the closure and never reissues a card', async t => {
  const h = fixture(t); await h.reply(); h.finish(); h.sendBehavior = async () => { throw { error_code: 429, parameters: { retry_after: 3 } }; };
  await h.observe(); h.restart(); h.sendBehavior = null; await h.observe(); assert.equal(h.sends.length, 2);
  h.now += 3001; await h.observe(); assert.equal(h.sends.length, 3); assert.equal(h.rows()[0].closure.attempt, 2);
  assert.ok(h.calls.every(call => call.name === 'agentbridge_host_notification_claim' || call.args.state_only));
});

for (const change of ['token', 'bot', 'task_subject', 'endpoint_subject', 'endpoint_id', 'route', 'conversation', 'withdrawn']) {
  test(`original ${change} cannot be inherited for a closure`, async t => {
    const h = fixture(t); await h.reply(); h.finish(); h.restart();
    if (change === 'token') h.token = 'different-token';
    if (change === 'bot') h.api.config.channels.telegram.botToken = '888:synthetic-other';
    if (change === 'task_subject') h.task.userSubject = 'other';
    if (change === 'endpoint_subject') { h.task.userSubject = 'other'; h.endpoint.userSubject = 'other'; }
    if (change === 'endpoint_id') { h.endpoint.endpointId = uuid(91); h.task.originEndpointId = uuid(91); }
    if (change === 'route') h.endpoint.route.to = '1002';
    if (change === 'conversation') h.task.activeConversationRef = 'agent:main:telegram:direct:1002';
    if (change === 'withdrawn') h.endpoint.state = 'inactive';
    await h.observe(); assert.equal(h.sends.length, 1);
  });
}

test('missing authoritative task fails closed before sending while retaining the normal reply', async t => {
  const h = fixture(t); h.authorityChange = response => ({ ...response, task: null });
  const reply = await h.reply(); assert.ok(reply.payload.text.startsWith('请先完成本人登录'));
  assert.ok(reply.payload.text.includes('尚未确认')); assert.equal(h.sends.length, 0); assert.equal(h.rows().length, 1);
  assert.equal(h.rows()[0].card.state, 'prepared');
});

test('historical delivered boolean cannot mint a native receipt or a closure', async t => {
  const h = fixture(t), card = h.card(); h.bridge.captureInteractions([card], { sessionKey: h.sessionKey }, { sessionKey: h.sessionKey }, h.taskId);
  h.bridge.records.get(card.interactionId).delivered = true; h.finish(); await h.observe();
  assert.equal(h.sends.length, 0); assert.equal(h.rows().length, 0);
});

test('a reused original platform id is not accepted as a separate closure receipt', async t => {
  const h = fixture(t); await h.reply(); h.finish(); h.sendBehavior = async () => ({ chatId: '1001', messageId: '501' });
  await h.observe(); assert.equal(h.rows()[0].closure.state, 'delivery_unknown');
  assert.equal(h.rows()[0].closure.receipt, undefined); h.restart(); await h.observe(); assert.equal(h.sends.length, 2);
});

test('interrupted sending intent survives process restart without a platform retry', async t => {
  const h = fixture(t); await h.reply(); const row = h.store.entries()[0];
  h.store.update(row.key, value => ({ ...value, card: { state: 'sending', attempt: 1, ownerPid: 99999999 } }));
  h.finish(); h.restart(); await h.observe(); assert.equal(h.sends.length, 1); assert.equal(h.rows()[0].closure, undefined);
});

test('suppressing the legacy close never suppresses original read continuation or wakes it twice', async t => {
  const h = fixture(t); await h.reply(); h.finish();
  h.bridge.config = { ...h.bridge.config, wakeAgentOnComplete: true };
  const record = h.bridge.records.get(uuid(1)); record.interaction.state = 'completed';
  record.readContinuation = { toolName: 'oa_workflow_pending_list', arguments: { limit: 2 }, capturedAt: h.now };
  let reads = 0;
  h.extraTool = async (name, args) => {
    if (name !== 'oa_workflow_pending_list') return undefined;
    reads++; assert.deepEqual(args, { limit: 2 }); return { status: 'succeeded', result: { items: [] } };
  };
  await h.bridge.notify(record, 'completed', null, [], { resumeOriginalRequest: true });
  await h.bridge.notify(record, 'completed', null, [], { resumeOriginalRequest: true });
  assert.equal(reads, 1); assert.equal(h.sends.length, 2);
  await h.observe(); assert.equal(h.sends.length, 3);
  assert.equal(h.sends.filter(send => send.text === nativeCardClosureText('credential', 'completed')).length, 1);
});

test('a confirmed API refusal and three rate-limit attempts are bounded independently from unknown', async t => {
  const h = fixture(t); h.sendBehavior = async () => { throw { error_code: 403 }; };
  await h.reply(); h.restart(); h.sendBehavior = null; await h.observe();
  assert.equal(h.sends.length, 1); assert.equal(h.rows()[0].card.state, 'rejected');
  h.sendBehavior = async () => { throw { error_code: 429, parameters: { retry_after: 1 } }; };
  await h.reply(h.card(2));
  for (let i = 0; i < 5; i++) { h.now += 1001; await h.observe(); }
  assert.equal(h.sends.length, 4); assert.equal(h.rows().find(row => row.interactionId === uuid(2)).card.state, 'rejected');
});

test('initial state-only outage persists an unsent intent and recovers once after restart', async t => {
  const h = fixture(t); h.authorityChange = () => { throw Error('temporary read outage'); };
  const reply = await h.reply(); assert.ok(reply.payload.text.includes('尚未确认')); assert.equal(h.sends.length, 0);
  assert.equal(h.rows()[0].card.state, 'prepared'); h.restart(); h.authorityChange = null;
  await h.observe(); await h.observe(); assert.equal(h.sends.length, 1); assert.equal(h.rows()[0].card.receipt, 501);
});

test('mixed eligible and unproven task-free cards preserve the original answer and the unsupported presentation', async t => {
  const h = fixture(t), legacy = h.card(2);
  h.bridge.captureInteractions([legacy], { sessionKey: h.sessionKey }, { sessionKey: h.sessionKey }, null);
  const reply = await h.reply(h.card(1), '原文字');
  assert.equal(reply.payload.text, '原文字'); assert.ok(reply.payload.presentation);
  assert.equal(h.sends.length, 1); assert.equal(h.rows().length, 1);
  assert.equal(h.bridge.records.get(legacy.interactionId).delivered, true);
});

test('direct mixed delivery retains the legacy card instead of swallowing it with an eligible native record', async t => {
  const h = fixture(t), native = h.card(1), legacy = h.card(2);
  h.bridge.captureInteractions([native], { sessionKey: h.sessionKey }, { sessionKey: h.sessionKey }, h.taskId);
  h.bridge.captureInteractions([legacy], { sessionKey: h.sessionKey }, { sessionKey: h.sessionKey }, null);
  assert.equal(await h.bridge.deliverInteractionsDirect(h.sessionKey, [native, legacy]), true);
  assert.equal(h.sends.length, 2); assert.equal(h.rows().length, 1);
});

test('a fresh trusted native standalone login owns its positive receipt and closes with task=null', async t => {
  const h = fixture(t, { taskId: null }), card = h.card();
  await h.captureStandalone(card);
  assert.equal(h.bridge.nativeCardCanHandle(h.sessionKey, card), true);
  const reply = await h.hooks.reply_payload_sending({ kind: 'final', sessionKey: h.sessionKey, channel: 'telegram', payload: { text: '独立登录' } },
    { sessionKey: h.sessionKey, channelId: 'telegram', conversationId: '1001', accountId: 'default' });
  assert.equal(reply.payload.text, '独立登录'); assert.equal(h.rows()[0].taskId, null);
  h.finish(); h.restart(); await h.observe(); await h.observe(); assert.equal(h.sends.length, 2);
});

test('the real opaque interaction identifier format is accepted without inventing a task ID', async t => {
  const h = fixture(t), old = h.card(), card = { ...old, interactionId: 'I6p09_9SxWSvH9vn4f96Wfc6Z-OpaQid' };
  h.interactions.set(card.interactionId, card); await h.reply(card);
  assert.equal(h.sends.length, 1); assert.equal(h.rows()[0].interactionId, card.interactionId);
});

test('a changed native standalone identity cannot fall back to an untracked old-card send', async t => {
  const h = fixture(t, { taskId: null }), card = h.card(); await h.captureStandalone(card);
  h.sendBehavior = async () => { throw Error('uncertain original send'); };
  await h.reply(card); assert.equal(h.sends.length, 1);
  h.token = 'replacement-identity';
  h.bridge.records.get(card.interactionId).delivered = false;
  const reply = await h.reply(card); assert.equal(h.sends.length, 1);
  assert.equal(reply.payload.presentation, undefined); assert.ok(reply.payload.text.includes('尚未确认'));
  h.api.config.channels.telegram.botToken = '888:synthetic-other';
  await h.reply(card); assert.equal(h.sends.length, 1); assert.equal(h.rows().length, 1);
});

test('a stale native call cannot create task-free source proof', async t => {
  const h = fixture(t, { taskId: null }), card = h.card();
  const original = h.bridge.planningCallCurrent.bind(h.bridge);
  h.bridge.planningCallCurrent = () => false; await h.captureStandalone(card);
  h.bridge.planningCallCurrent = original;
  assert.equal(h.bridge.nativeCardCanHandle(h.sessionKey, card), false);
  const reply = await h.reply(card); assert.ok(reply.payload.presentation);
  assert.equal(h.sends.length, 0); assert.equal(h.rows().length, 0);
});
