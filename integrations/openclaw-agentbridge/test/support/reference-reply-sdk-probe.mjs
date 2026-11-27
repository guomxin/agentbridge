// Actual public SDK dispatcher, hook fields, read-only session API and outbound
// adapter. Only loopback synthetic Telegram/MCP; no monitor, getUpdates or model.
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { mkdtempSync, rmSync, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { referenceFixture, query, summary } from './reference-reply-fixture.mjs';
import { TimelinePublisher } from '../../lib/timeline.js';
import { TimelineLocalStore } from '../../lib/timeline-storage.js';
const sdkDir = process.argv[2];
if (!sdkDir || !path.isAbsolute(sdkDir)) throw Error('ABSOLUTE_REVIEWED_SDK_DIR_REQUIRED');
const home = mkdtempSync(path.join(os.tmpdir(), 'oc-reference-sdk-'));
process.env.OPENCLAW_STATE_DIR = path.join(home, 'sdk-state');
process.env.OPENCLAW_CONFIG_PATH = path.join(home, 'isolated-config.json');
const load = name => import(pathToFileURL(path.join(sdkDir, 'dist/plugin-sdk', name + '.js')).href);
const wire = [], projectionCalls = [], hookContexts = [], centralRows = new Map();
let mode = 'ok', projectionAckLost = false, unexpectedNetwork = 0, modelHooks = 0, normalDispatch = 0, ordinaryControl = 0, h, resetHooks;
const server = http.createServer(async (request, response) => {
  const chunks = []; for await (const chunk of request) chunks.push(chunk);
  if (request.method !== 'POST' || !request.url.endsWith('/sendMessage')) {
    unexpectedNetwork++; response.writeHead(400); response.end('{}'); return;
  }
  const body = JSON.parse(Buffer.concat(chunks)); wire.push(body);
  assert.equal(String(body.chat_id), '1001');
  if (mode === 'unknown') { request.socket.destroy(); return; }
  if (mode === '429') {
    response.writeHead(429, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ ok: false, error_code: 429, parameters: { retry_after: 2 }, description: 'Synthetic wait' })); return;
  }
  response.setHeader('Content-Type', 'application/json');
  response.end(JSON.stringify({ ok: true, result: { message_id: 800 + wire.length, date: 1795780000,
    chat: { id: 1001, type: 'private' }, from: { id: 777, is_bot: true, first_name: 'Synthetic' }, text: body.text } }));
});
server.listen(0, '127.0.0.1'); await once(server, 'listening');
try {
  const sdk = await load('session-store-runtime');
  const { telegramPlugin } = await import(pathToFileURL(path.join(sdkDir, 'dist/extensions/telegram/channel-plugin-api.js')).href);
  const { initializeGlobalHookRunner, resetGlobalHookRunner, getGlobalHookRunner } = await load('plugin-runtime'); resetHooks = resetGlobalHookRunner;
  const { dispatchInboundMessageWithDispatcher, resetInboundDedupe } = await load('reply-runtime');
  const cfg = { channels: { telegram: { botToken: '777:synthetic-only', apiRoot: `http://127.0.0.1:${server.address().port}`,
    retry: { attempts: 4 }, enabled: true } }, session: { store: path.join(home, 'sessions.json'), dmScope: 'per-channel-peer' },
    agents: { defaults: { workspace: path.join(home, 'workspace'), skipBootstrap: true, model: { primary: 'synthetic/synthetic' } } },
    models: { providers: { synthetic: { baseUrl: `http://127.0.0.1:${server.address().port}/model`, apiKey: 'synthetic-only',
      api: 'openai-completions', models: [{ id: 'synthetic', name: 'synthetic', contextWindow: 32000, maxTokens: 1000 }] } } },
    messages: { visibleReplies: 'message_tool' } };
  h = referenceFixture(home, { adapter: telegramPlugin.outbound, hostConfig: cfg, loadSessionSdk: async () => sdk });
  h.binding.label = 'Synthetic private endpoint';
  const install = () => {
    const projectionStore = new TimelineLocalStore(path.join(home, 'projection')); h.stores.push(projectionStore);
    h.router.configuredIdentities = () => [{ binding: h.binding, client: h.client }];
    h.extraTool = async (name, args) => {
      if (name === 'agentbridge_host_cross_endpoint_context') return summary;
      if (name === 'agentbridge_host_timeline_append') {
        projectionCalls.push(args);
        const receipt = { entryId: '00000001-0000-4000-8000-000000000001', sourceEndpointId: h.endpoint.endpointId,
          messageKey: args.message_key, textSha256: createHash('sha256').update(args.text).digest('hex'), role: args.role };
        centralRows.set(args.message_key, receipt);
        if (projectionAckLost) throw Error('Synthetic ACK lost after Central stored original receipt');
        return { status: 'succeeded', projectionReceipt: receipt, entry: { entryId: receipt.entryId }, endpoint: h.endpoint };
      }
    };
    h.bridge.timelinePublisher = new TimelinePublisher({ identityRouter: h.router, api: h.api, projectionStore, logger: h.api.logger,
      sleep: async () => {}, sharedState: {} });
    const hooks = Object.entries(h.hooks).map(([hookName, handler]) => ({ pluginId: 'agentbridge', hookName, priority: 10,
      handler: hookName === 'reply_dispatch' ? (event, context) => { hookContexts.push(event.ctx); return handler(event, context); } : handler }));
    hooks.push({ pluginId: 'model-tripwire', hookName: 'before_prompt_build', handler() { modelHooks++; throw Error('Unexpected model entry'); } });
    hooks.push({ pluginId: 'ordinary-control', hookName: 'before_agent_reply', handler(event) {
      if (event.cleanedBody === '普通同端消息') { ordinaryControl++; return { handled: true, reply: { text: '普通同端答复' } }; }
    } });
    initializeGlobalHookRunner({ typedHooks: hooks, hooks: [], plugins: [], diagnostics: [] });
  };
  install();
  const dispatch = async (id, text = query, extra = {}) => {
    return dispatchInboundMessageWithDispatcher({ cfg, ctx: { Body: text, BodyForAgent: text, BodyForCommands: text, RawBody: text,
      From: 'telegram:1001', To: 'telegram:1001', OriginatingTo: 'telegram:1001', OriginatingChannel: 'telegram',
      Provider: 'telegram', Surface: 'telegram', AccountId: 'default', SessionKey: h.sessionKey, AgentId: 'main',
      SenderId: '1001', ChatId: '1001', ChatType: 'direct', CommandAuthorized: true, MessageSid: String(id), Timestamp: Date.now(),
      ChannelContext: { sender: { id: '1001' }, chat: { id: '1001', type: 'direct' } } },
    dispatcherOptions: { deliver: async payload => { normalDispatch++;
      await telegramPlugin.outbound.sendPayload({ cfg, to: '1001', accountId: 'default', payload, text: payload.text });
    } }, replyOptions: { suppressTyping: true }, ...extra });
  };
  // Let the official dispatcher initialize its normal runtime plugin surface;
  // only then attach this isolated SDK consumer's real plugin callbacks.
  await dispatch(1, '合成初始化', { replyResolver: async () => undefined });
  install();
  await dispatch(17);
  assert.equal(wire.length, 1); assert.equal(normalDispatch, 0); assert.equal(modelHooks, 0);
  assert.equal(hookContexts.length, 1); assert.equal(hookContexts[0].SenderId, '1001'); assert.equal(hookContexts[0].ChatId, '1001');
  assert.equal(hookContexts[0].MessageSid, '17'); assert.equal(wire[0].text, '网页原消息摘要\n合成公开摘要 &lt;内容&gt; &amp; 保留原文');
  assert.equal(projectionCalls.length, 1); assert.match(projectionCalls[0].message_key, /:assistant:801$/);
  assert.equal(projectionCalls[0].text, '网页原消息摘要\n' + summary.entries[0].text);
  const originalGuard = await getGlobalHookRunner().runBeforeToolCall({ toolName: 'message', params: { action: 'send', message: 'only tool, no text' } },
    { sessionKey: h.sessionKey });
  assert.equal(originalGuard.block, true, 'The actual model-only message call stays prohibited');
  await dispatch(17); assert.equal(wire.length, 1);
  resetInboundDedupe(); h.restart(); install(); await dispatch(17);
  assert.equal(wire.length, 1); assert.equal(projectionCalls.length, 1);
  mode = 'unknown'; await dispatch(18); assert.equal(wire.length, 2);
  mode = 'ok'; resetInboundDedupe(); h.restart(); install(); await dispatch(18); assert.equal(wire.length, 2);
  mode = '429'; await dispatch(19); assert.equal(wire.length, 3);
  mode = 'ok'; resetInboundDedupe(); h.restart(); install(); await dispatch(19); assert.equal(wire.length, 3);
  // A new, explicitly sent user message remains possible; no original attempt
  // is relabelled, retried, or projected as delivered after an unknown result.
  await dispatch(20); assert.equal(wire.length, 4); assert.equal(projectionCalls.length, 2);
  await Promise.all([dispatch(21), dispatch(21)]); assert.equal(wire.length, 5); assert.equal(centralRows.size, 3);
  projectionAckLost = true; await dispatch(22); assert.equal(wire.length, 6); assert.equal(centralRows.size, 4);
  projectionAckLost = false; resetInboundDedupe(); h.restart(); install();
  await h.observe(); await h.observe(); await dispatch(22);
  assert.equal(wire.length, 6); assert.equal(centralRows.size, 4);
  assert.equal(h.referenceStore.entries().filter(row => row.value.state === 'sent').every(row => row.value.projected), true);
  await Promise.all([dispatch(30), dispatch(31)]);
  assert.equal(wire.length, 8); assert.equal(centralRows.size, 6);
  assert.deepEqual(h.referenceStore.entries().filter(row => ['30', '31'].includes(row.value.sourceId)).map(row => row.value.sourceId).sort(), ['30', '31']);
  // The same official pipeline must still reach its next reply handler for an
  // ordinary same-end message. This control reply is synthetic, not an LLM.
  await dispatch(23, '普通同端消息', { replyOptions: { suppressTyping: true, sourceReplyDeliveryMode: 'automatic' } });
  assert.equal(ordinaryControl, 1); assert.equal(normalDispatch, 1); assert.equal(wire.length, 9);
  assert.equal(wire.at(-1).text, '普通同端答复');
  await sdk.patchSessionEntry({ sessionKey: h.sessionKey, storePath: cfg.session.store, readConsistency: 'latest',
    update: () => ({ sendPolicy: 'deny' }) });
  await dispatch(24); assert.equal(wire.length, 9); assert.equal(centralRows.size, 6);
  assert.equal(unexpectedNetwork, 0); assert.equal(modelHooks, 0);
  console.log(JSON.stringify({ passed: true, sdkVersion: JSON.parse(readFileSync(path.join(sdkDir, 'package.json'))).version,
    proof: 'official-dispatcher-hooks-session-read-and-outbound-loopback', referencePositiveReceipts: 6, unknownHttpAttempts: 1,
    rateLimitHttpAttempts: 1, restartResends: 0, referenceNormalDispatcherSends: 0, modelEntries: modelHooks,
    centralProjectionReceipts: centralRows.size, projectionAckLossRecovered: true, concurrentDuplicateSends: 0, concurrentDistinctSources: 2,
    ordinaryControlReplies: normalDispatch, deniedSdkSessionSends: 0, businessToolCalls: 0, realTelegram: false, polling: false }));
} finally { resetHooks?.(); h?.close(); await new Promise(resolve => server.close(resolve)); rmSync(home, { recursive: true, force: true }); }
