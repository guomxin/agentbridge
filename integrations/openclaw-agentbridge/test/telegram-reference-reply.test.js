import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { referenceFixture, query, summary } from './support/reference-reply-fixture.mjs';

function fixture(t) {
  const home = mkdtempSync(path.join(os.tmpdir(), 'oc-reference-reply-'));
  const h = referenceFixture(home); t.after(() => { h.close(); rmSync(home, { recursive: true, force: true }); }); return h;
}
test('message-only model regression has deterministic visible reply, original receipt and Workspace projection', async t => {
  const h = fixture(t); h.receive();
  const onlyTool = { toolName: 'message', params: { action: 'send', message: '任意模型文字' } };
  assert.equal(h.hooks.before_tool_call(onlyTool, h.context()).block, true);
  const result = await h.ask();
  assert.equal(result.handled, true); assert.equal(result.reply, undefined);
  assert.equal(h.sends.length, 1); assert.equal(h.sends[0].to, '1001');
  assert.equal(h.sends[0].cfg.channels.telegram.retry.attempts, 1);
  assert.match(h.sends[0].text, /网页原消息摘要/); assert.doesNotMatch(h.sends[0].text, /任意模型文字|请解释/);
  assert.match(h.sends[0].text, /&lt;内容&gt; &amp;/);
  assert.equal(h.projections.length, 1); assert.equal(h.projections[0].event.messageId, '501');
  assert.equal(h.projections[0].taskId, null); assert.equal(h.projections[0].text, '网页原消息摘要\n' + summary.entries[0].text);
  assert.equal(h.referenceStore.entries()[0].value.projected, true);
  assert.equal(h.referenceStore.entries()[0].value.text, undefined);
  assert.equal(h.calls.filter(call => call.name === 'agentbridge_host_cross_endpoint_context').length, 1);
});
test('same source is not sent twice by concurrent callback, replay, restart or changed payload', async t => {
  const h = fixture(t); h.receive(); await Promise.all([h.ask(), h.ask()]);
  h.receive(); await h.ask(); h.restart(); h.receive(); await h.ask();
  h.receive('17', query + '异文'); await h.ask(query + '异文');
  assert.equal(h.sends.length, 1); assert.equal(h.projections.length, 1);
  h.receive('18'); await h.ask(); assert.equal(h.sends.length, 2, 'A genuinely new user message has its own identity');
});
test('unknown POST and receipt mismatch remain frozen across restart; no false projection', async t => {
  const h = fixture(t); h.sendBehavior = async () => { throw Error('socket lost after request'); };
  h.receive(); await h.ask(); h.restart(); h.receive(); h.sendBehavior = null; await h.ask();
  assert.equal(h.sends.length, 1); assert.equal(h.projections.length, 0);
  assert.equal(h.referenceStore.entries()[0].value.state, 'delivery_unknown');
  h.sendBehavior = async () => ({ messageId: '999', chatId: '2002' });
  h.receive('18'); await h.ask(); assert.equal(h.sends.length, 2); assert.equal(h.projections.length, 0);
});
test('confirmed send plus lost projection ACK recovers only projection', async t => {
  const h = fixture(t); h.project = async () => false; h.receive(); await h.ask();
  assert.equal(h.sends.length, 1); assert.equal(h.referenceStore.entries()[0].value.state, 'sent');
  h.restart(); h.project = async () => true; await h.observe(); await h.observe();
  assert.equal(h.sends.length, 1); assert.equal(h.projections.length, 2);
  assert.equal(h.projections[0].event.messageId, h.projections[1].event.messageId);
});
test('post-receipt callback failure cannot demote sent or cause resend', async t => {
  const h = fixture(t); h.sendBehavior = async ctx => {
    await ctx.onDeliveryResult({ messageId: '777', chatId: '1001' }); throw Error('after positive callback');
  };
  h.receive(); await h.ask(); h.restart(); h.receive(); await h.ask();
  assert.equal(h.sends.length, 1); assert.equal(h.referenceStore.entries()[0].value.state, 'sent');
  assert.equal(h.projections[0].event.messageId, '777');
});
for (const mutation of ['sender', 'chat', 'channel', 'trigger', 'reset', 'identity', 'bot', 'policy']) {
  test(`original scope ${mutation} change cannot send or resolve as a new owner`, async t => {
    const h = fixture(t); h.receive(); const context = h.context();
    if (mutation === 'sender') context.senderId = '2002';
    if (mutation === 'chat') context.chatId = '2002';
    if (mutation === 'channel') context.channel = 'openclaw-weixin';
    if (mutation === 'trigger') context.trigger = 'cron';
    if (mutation === 'reset') h.hooks.session_end({ reason: 'reset' }, { sessionKey: h.sessionKey });
    if (mutation === 'identity') h.token = 'new-identity';
    if (mutation === 'bot') h.api.config.channels.telegram.botToken = '888:synthetic-other';
    if (mutation === 'policy') h.policy = 'deny';
    assert.equal((await h.ask(query, context)).handled, true); assert.equal(h.sends.length, 0); assert.equal(h.calls.length, 0);
  });
}
for (const mutation of ['identity', 'reset', 'policy', 'session']) {
  test(`scope changes during Central resolution: ${mutation}`, async t => {
    const h = fixture(t); h.receive();
    h.resolve = async () => {
      if (mutation === 'identity') h.token = 'new-identity';
      if (mutation === 'reset') h.hooks.session_end({ reason: 'reset' }, { sessionKey: h.sessionKey });
      if (mutation === 'policy') h.policy = 'deny';
      if (mutation === 'session') h.sessionId = 'reset-session';
      return summary;
    };
    await h.ask(); assert.equal(h.sends.length, 0); assert.equal(h.projections.length, 0);
  });
}
test('missing source/store/adapter fails closed before a platform request', async t => {
  const h = fixture(t); await h.ask(); assert.equal(h.sends.length, 0);
  h.receive('17', query, { messageId: undefined }); await h.ask(); assert.equal(h.sends.length, 0);
  h.receive(); h.reference.store = null; await h.ask(); assert.equal(h.sends.length, 0);
  h.reference.store = h.referenceStore; h.api.runtime.channel.outbound.loadAdapter = async () => null;
  await h.ask(); assert.equal(h.sends.length, 0); assert.equal(h.referenceStore.entries().length, 0);
});
test('two received same-text messages interleave before their callbacks without borrowing the latest identity', async t => {
  const h = fixture(t); h.receive('31'); h.receive('32');
  await Promise.all([h.ask(query, h.context(), '31'), h.ask(query, h.context(), '32')]);
  assert.equal(h.sends.length, 2); assert.equal(h.projections.length, 2);
  assert.deepEqual(h.referenceStore.entries().map(row => row.value.sourceId).sort(), ['31', '32']);
  await h.ask(query, h.context(), '31'); await h.ask(query, h.context(), '32'); assert.equal(h.sends.length, 2);
});
test('durable intent failure has zero platform requests and explicit 429 never becomes an automatic retry', async t => {
  const h = fixture(t); h.receive();
  const reserve = h.referenceStore.registerIfAbsent.bind(h.referenceStore);
  h.referenceStore.registerIfAbsent = () => { throw Error('Synthetic storage failure'); };
  await h.ask(); assert.equal(h.sends.length, 0);
  h.referenceStore.registerIfAbsent = reserve;
  h.sendBehavior = async () => { throw { error_code: 429, parameters: { retry_after: 1 } }; };
  await h.ask(); h.restart(); h.receive(); h.sendBehavior = null; await h.ask();
  assert.equal(h.sends.length, 1); assert.equal(h.referenceStore.entries()[0].value.state, 'retry_after');
});
for (const changes of [{ senderId: '2002' }, { from: 'telegram:2002' }, { threadId: 1 }, { isGroup: true },
  { accountId: 'other' }, { metadata: { originatingTo: 'telegram:2002' } }, { metadata: { provider: 'openclaw-weixin' } }]) {
  test(`untrusted original event ${JSON.stringify(changes)} cannot acquire a reply route`, async t => {
    const h = fixture(t); h.receive('17', query, changes); await h.ask();
    assert.equal(h.sends.length, 0); assert.equal(h.calls.length, 0);
  });
}
test('all tool names, arbitrary message target/content and original frozen run remain guarded', async t => {
  const h = fixture(t); h.receive(); await h.ask();
  for (const toolName of ['message', 'exec', 'oa_workflow_pending_list', 'oa_workflow_commit']) {
    assert.equal(h.hooks.before_tool_call({ toolName, params: { action: 'send', target: '2002', message: '任意文字' } }, h.context()).block, true);
  }
  assert.equal(h.sends.length, 1);
});
test('ordinary same-TG replies and non-TG sessions remain in the existing agent flow', async t => {
  const h = fixture(t); h.receive('18', '解释同端这条', { replyToId: '901' });
  assert.equal(await h.ask('解释同端这条'), undefined);
  assert.equal(h.hooks.before_tool_call({ toolName: 'message', params: {} }, h.context()), undefined);
  assert.equal(await h.ask(query, { ...h.context(), sessionKey: 'agent:main:openclaw-weixin:direct:1001' }), undefined);
  assert.equal(h.sends.length, 0); assert.equal(h.calls.length, 0);
});
test('unavailable and transport failure only send fixed public explanations, never raw errors', async t => {
  const h = fixture(t); h.resolve = async () => ({ decision: 'unavailable', allowNewOperation: false, entries: [] });
  h.receive(); await h.ask(); assert.match(h.sends[0].text, /失效/);
  h.resolve = async () => { throw Error('secret detail'); }; h.receive('18'); await h.ask();
  assert.match(h.sends[1].text, /暂时无法/); assert.doesNotMatch(h.sends[1].text, /secret detail/);
});
