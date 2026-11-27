import assert from 'node:assert/strict';
import path from 'node:path';
import { registerAgentBridgeInteractions } from '../../lib/plugin.js';
import { TimelineLocalStore } from '../../lib/timeline-storage.js';
import { interaction } from '../fixtures.js';

export const uuid = number => `${String(number).padStart(8, '0')}-0000-4000-8000-000000000001`;
// Real registration, reply hook, Coordinator and SQLite. MCP and Telegram are
// explicit boundary doubles here; the companion probe loads the official SDK.
export function nativeFixture(home, options = {}) {
  const sessionKey = 'agent:main:telegram:direct:1001', taskId = Object.hasOwn(options, 'taskId') ? options.taskId : uuid(80);
  const binding = { key: 'telegram:default:1001', channel: 'telegram', accountId: 'default', senderId: '1001' };
  const endpoint = options.endpoint || { endpointId: uuid(90), userSubject: 'synthetic-owner', agentHost: 'openclaw', state: 'active',
    clientType: 'telegram', externalSubject: '1001', accountId: 'default', conversationRef: sessionKey,
    route: { channel: 'telegram', to: '1001', accountId: 'default' } };
  const task = taskId === null ? null : { taskId, userSubject: endpoint.userSubject, agentHost: 'openclaw', originEndpointId: endpoint.endpointId,
    activeConversationRef: sessionKey };
  const h = { now: Date.now(), token: 'same-central-identity', endpoint, task, binding, sessionKey, taskId,
    sends: [], calls: [], interactions: new Map(), bridges: [], stores: [], sendBehavior: null, authorityChange: null };
  h.card = (number = 1, type = 'credential') => {
    const value = interaction({ interactionId: uuid(number), type, expiresAt: '2099-07-14T12:00:00+00:00',
      taskId, presentation: { endpointId: endpoint.endpointId, url: 'https://cards.example.test/auth/synthetic-only' } });
    h.interactions.set(value.interactionId, value); return value;
  };
  const client = { async callTool(name, args) {
    h.calls.push({ name, args });
    if (h.extraTool) { const extra = await h.extraTool(name, args); if (extra !== undefined) return extra; }
    if (name === 'agentbridge_host_notification_claim') return { endpoint: h.endpoint, notifications: [] };
    assert.equal(name, 'agentbridge_host_interaction_present', 'Closure cannot get/resume a plan, acquire a lease, or execute OA');
    assert.equal(args.agent_host, 'openclaw'); assert.equal(args.endpoint_key, binding.key);
    const card = h.interactions.get(args.interaction_id);
    if (args.state_only) {
      const response = { status: 'succeeded', protocolVersion: '0.1', endpoint: structuredClone(h.endpoint), task: structuredClone(h.task),
        interaction: card && { interactionId: card.interactionId, type: card.type, state: card.state, taskId: card.taskId } };
      return h.authorityChange ? h.authorityChange(response) : response;
    }
    assert.ok(['pending', 'processing'].includes(card.state), 'Never reissue a closed card');
    return { status: 'succeeded', interaction: card };
  } };
  const router = { enabled: true, resolveToolContext: () => ({ bound: true, binding, client }),
    catalogCacheKeyForBinding: () => h.token, endpointKeyForSession: () => binding.key,
    clientForSession: () => client, bindSession: () => true, restoreSessionBinding: () => true,
    resolvePinnedSession: () => ({ bound: true, binding, client }), configuredIdentities: () => [], removeSession() {} };
  const adapter = options.adapter || {
    renderPresentation: ({ payload, presentation }) => ({ ...payload, channelData: { telegram: { buttons: presentation.blocks.at(-1).buttons } } }),
    async sendPayload(context) {
      h.sends.push(context);
      if (h.sendBehavior) return h.sendBehavior(context, h.sends.length);
      const result = { chatId: '1001', messageId: String(500 + h.sends.length), channel: 'telegram' };
      await context.onDeliveryResult?.(result); return result;
    },
  };
  const api = { config: options.hostConfig || { channels: { telegram: { botToken: '777:synthetic-only', retry: { attempts: 4 } } } },
    pluginConfig: { autoPoll: false, syncTimeline: false, wakeAgentOnComplete: false, allowedCardOrigins: ['https://cards.example.test'] },
    logger: { info() {}, warn() {}, debug() {} }, runtime: { channel: { outbound: { loadAdapter: async () => adapter } },
      system: { enqueueSystemEvent() { assert.fail('No model wake'); }, requestHeartbeat() { assert.fail('No model wake'); } } },
    registerAgentToolResultMiddleware() {}, registerTool() {}, registerCommand() {}, registerGatewayMethod() {}, on() {} };
  h.restart = () => {
    h.bridges.forEach(value => value.stopAll());
    h.stores.splice(0).forEach(value => value.close());
    const store = new TimelineLocalStore(path.join(home, 'native-cards'));
    h.stores.push(store); h.store = store;
    const hooks = {}; api.on = (name, handler) => { hooks[name] = handler; };
    h.bridge = registerAgentBridgeInteractions(api, { nativeCardStore: store, artifactReceiptStore: null,
      identityRouter: router, now: () => h.now });
    h.bridge.bindDeliveryRoute({ sessionKey, ...endpoint.route });
    h.bridges.push(h.bridge); h.hooks = hooks;
    return h.bridge;
  };
  h.reply = async (card = h.card(), text = '请先完成本人登录，随后查看待办。') => {
    h.bridge.captureInteractions([card], { sessionKey }, { sessionKey }, taskId);
    return h.hooks.reply_payload_sending({ kind: 'final', sessionKey, channel: 'telegram', payload: { text } },
      { sessionKey, channelId: 'telegram', conversationId: '1001', accountId: 'default' });
  };
  h.captureStandalone = async card => {
    const toolCallId = 'native-login-original-call', runId = 'native-login-original-run';
    const planningContext = h.bridge.capturePlanningCall({ toolCallId, sessionKey, runId,
      resolvedIdentityScope: h.bridge.toolIdentityResolver(sessionKey) });
    const payload = { status: 'requires_user_action', interaction: card };
    await h.bridge.captureNativeToolResult({ toolCallId, toolName: 'oa_session_login', planningContext,
      result: { content: [{ type: 'text', text: JSON.stringify(payload) }], structuredContent: payload,
        details: { mcpServer: 'agentbridge', mcpTool: 'oa_session_login', structuredContent: payload } } }, { sessionKey, runId });
  };
  h.observe = () => h.bridge.deliverEndpointNotifications(router, binding, client, new AbortController().signal);
  h.finish = (number = 1, state = 'completed') => { h.interactions.get(uuid(number)).state = state; };
  h.rows = () => h.store.entries().map(row => row.value);
  h.close = () => { h.bridges.forEach(value => value.stopAll()); h.stores.splice(0).forEach(value => value.close()); };
  h.api = api; h.client = client; h.router = router; h.restart(); return h;
}
