// Child of the Python-only synthetic Central fixture. No production inputs.
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { nativeFixture } from './native-card-fixture.mjs';
import { createAgentBridgeMcpClient } from '../../lib/mcp-client.js';
import { hostContextMeta } from '../../lib/host-contract.js';
import { nativeCardClosureText } from '../../lib/telegram-native-cards.js';
const spec = JSON.parse(readFileSync(0, 'utf8'));
for (const address of [spec.mcpUrl, spec.controlUrl]) assert.equal(new URL(address).hostname, '127.0.0.1');
process.env.OPENCLAW_STATE_DIR = path.join(spec.home, 'sdk-state');
process.env.OPENCLAW_CONFIG_PATH = path.join(spec.home, 'isolated-config.json');
const wire = [];
const server = http.createServer(async (request, response) => {
  assert.equal(request.method, 'POST'); assert.ok(request.url.endsWith('/sendMessage'));
  const chunks = []; for await (const chunk of request) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks)); assert.equal(String(body.chat_id), '1001'); wire.push(body);
  response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify({ ok: true, result: {
    message_id: 800 + wire.length, date: 1795740000, chat: { id: 1001, type: 'private' },
    from: { id: 777, is_bot: true, first_name: 'Synthetic' }, text: body.text } }));
});
server.listen(0, '127.0.0.1'); await once(server, 'listening');
let h;
try {
  const { telegramPlugin } = await import(pathToFileURL(path.join(spec.sdkDir, 'dist/extensions/telegram/channel-plugin-api.js')).href);
  const native = telegramPlugin.outbound;
  const client = createAgentBridgeMcpClient({ endpoint: { url: spec.mcpUrl, timeoutSeconds: 20 },
    tokenEnv: 'SYNTHETIC_TOKEN', env: { SYNTHETIC_TOKEN: spec.token } });
  h = nativeFixture(spec.home, { adapter: native, taskId: spec.taskId, endpoint: spec.endpoint,
    hostConfig: { channels: { telegram: { botToken: '777:synthetic-only', apiRoot: `http://127.0.0.1:${server.address().port}` } } } });
  h.token = createHash('sha256').update(spec.token).digest('hex');
  h.extraTool = (name, args) => client.callTool(name, args, { meta: hostContextMeta() });
  if (spec.taskId === null) await h.captureStandalone(spec.interaction);
  const reply = await h.reply(spec.interaction, '正常原生公开答复');
  assert.equal(reply.payload.text, '正常原生公开答复'); assert.equal(wire.length, 1);
  assert.equal(h.rows()[0].card.receipt, 801);
  const changed = await fetch(spec.controlUrl, { method: 'POST' }); assert.equal(changed.status, 204);
  const recover = () => h.bridge.nativeCards.recover({ binding: h.binding, client: h.client, endpoint: h.endpoint,
    identityRouter: h.router, withDirectDelivery: (_session, send) => send(), signal: new AbortController().signal });
  h.restart(); await recover(); assert.equal(wire.length, 2);
  assert.equal(wire[1].text, nativeCardClosureText('credential', 'completed'));
  assert.equal(h.rows()[0].closure.receipt, 802);
  h.restart(); await recover(); await recover(); assert.equal(wire.length, 2);
  assert.ok(h.calls.every(call => call.name === 'agentbridge_host_interaction_present' && call.args.state_only === true));
  console.log(JSON.stringify({ passed: true, proof: 'actual-central-mcp-and-native-sdk-loopback',
    realCentralMcp: true, actualSdk: true, originalCardReceipts: 1, closureReceipts: 1, replayedAfterRestart: 0,
    observerTools: ['agentbridge_host_interaction_present(state_only=true)'], production: false, realTelegram: false }));
} finally { h?.close(); await new Promise(resolve => server.close(resolve)); }
