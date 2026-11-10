import test from 'node:test';
import assert from 'node:assert/strict';
import {
  AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES,
  createAgentBridgeProxyTools,
  isIndependentTaskEntryTool,
} from '../lib/proxy-tools.js';
import { rememberSkillBinding } from '../lib/business-skills.js';
import { HOST_CONTEXT_META_KEY, OPENCLAW_HOST_VERSION } from '../lib/host-contract.js';
import { AGENTBRIDGE_TOOL_NAMES } from '../lib/tool-catalog.js';
import { InteractionCoordinator, createInteractionSharedState } from '../lib/coordinator.js';
import { resolvePluginConfig } from '../lib/config.js';

const watchNames = ['create', 'list', 'get', 'update'].map(name => `agentbridge_business_watch_${name}`);
const stateName = 'oa_workflow_state_get';
const watchId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';

function harness({ allowed = null, scope = 'all', guard = false, trial = null, reject = null } = {}) {
  const calls = [];
  const context = { sessionKey: 'agent:main:telegram:direct:watch-user', runId: 'watch-run-1' };
  const identity = { bound: true, binding: { key: 'watch-owner', channel: 'telegram' }, client: {
    async callTool() { assert.fail('Platform watch management must not create a business task or lease'); },
    async callToolResult(name, params, options) {
      calls.push({ name, params, options });
      const payload = reject || { watch: { watch_id: watchId, state: 'active', version: 7 } };
      return { structuredContent: payload, content: [{ type: 'text', text: JSON.stringify(payload) }] };
    },
  } };
  const router = { config: { toolScope: scope, boundSkillToolGuard: guard },
    resolveToolContext: () => identity,
    allowedToolNamesForBinding: () => allowed,
    endpointKeyForSession: () => 'telegram:*:watch-user' };
  const tools = createAgentBridgeProxyTools({ context, identityRouter: router, serverName: 'agentbridge',
    ...(trial ? { skillTrialResolver: () => trial } : {}) });
  return { calls, context, identity, router, tools,
    tool: name => tools.find(tool => tool.name === name),
    async run(name, args, callId = 'watch-call-1') {
      return tools.find(tool => tool.name === name).execute(callId, args);
    } };
}

function payload(result) {
  return result.structuredContent || JSON.parse(result.content[0].text);
}

test('general proxy exposes state and all watch tools while mutations remain governed entries', () => {
  const h = harness();
  for (const name of [...watchNames, stateName]) assert.ok(h.tool(name), name);
  for (const name of ['agentbridge_business_watch_create', 'agentbridge_business_watch_update']) {
    assert.ok(AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES.includes(name));
    assert.equal(h.tool(name).annotations.readOnlyHint, false);
    assert.equal(h.tool(name).annotations.destructiveHint, false);
    assert.equal(isIndependentTaskEntryTool(name), false);
  }
  for (const name of ['agentbridge_business_watch_list', 'agentbridge_business_watch_get', stateName])
    assert.equal(h.tool(name).annotations.readOnlyHint, true);
});

test('catalog uses closed source references and CAS without public identity or URL overrides', () => {
  const h = harness();
  for (const name of [...watchNames, stateName]) {
    const properties = h.tool(name).parameters.properties;
    for (const key of ['user_subject', 'url', 'source_url', 'affair_id', 'summary_id', 'process_id', 'case_id'])
      assert.equal(Object.hasOwn(properties, key), false, `${name}.${key}`);
  }
  assert.equal(h.tool('agentbridge_business_watch_create').parameters.properties.allow_mark_read.default, false);
  assert.deepEqual(Object.keys(h.tool(stateName).parameters.properties), ['target_ref']);
  assert.ok(h.tool('agentbridge_business_watch_update').parameters.required.includes('expected_version'));
});

test('host-private notification revalidation stays outside the generated model catalog and proxy', () => {
  const name = 'agentbridge_host_notification_validate';
  assert.equal(AGENTBRIDGE_TOOL_NAMES.includes(name), false);
  assert.equal(harness().tool(name), undefined);
});

test('omitted read consent stays absent or false and opaque reference is forwarded unchanged', async () => {
  const h = harness();
  const args = Object.freeze({ target_ref: 'opaque-owned-source-reference' });
  await h.run('agentbridge_business_watch_create', args);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].params.target_ref, args.target_ref);
  assert.notEqual(h.calls[0].params.allow_mark_read, true);
  assert.equal(h.calls[0].params.idempotency_key, 'openclaw:watch-call-1');
  assert.equal(Object.hasOwn(args, 'allow_mark_read'), false);
  assert.equal(h.calls[0].options.meta[HOST_CONTEXT_META_KEY].hostVersion, OPENCLAW_HOST_VERSION);
  assert.equal(h.calls[0].options.meta[HOST_CONTEXT_META_KEY].agentHost, 'openclaw');
});

test('explicit consent, request key, selected events and personal endpoints pass through exactly', async () => {
  const h = harness();
  const args = { target_ref: 'opaque-fresh-reference', allow_mark_read: true, interval_seconds: 1800,
    expires_in_days: 3, event_types: ['nodes', 'ended'], endpoint_ids: ['owned-endpoint'],
    idempotency_key: 'explicit-watch-request' };
  await h.run('agentbridge_business_watch_create', args);
  assert.deepEqual(h.calls[0].params, args);
});

test('update preserves watch ID and integer CAS version without adding consent', async () => {
  const h = harness();
  const args = Object.freeze({ watch_id: watchId, expected_version: 7, action: 'pause' });
  await h.run('agentbridge_business_watch_update', args);
  assert.deepEqual(h.calls[0].params, args);
  assert.equal(Object.hasOwn(h.calls[0].params, 'allow_mark_read'), false);
  assert.equal(h.calls.length, 1);
  const next = { watch_id: watchId, expected_version: 8, action: 'configure', allow_mark_read: false, interval_seconds: 3600 };
  await h.run('agentbridge_business_watch_update', next, 'watch-call-2');
  assert.deepEqual(h.calls[1].params, next);
});

test('central CAS rejection is preserved without replaying the platform mutation', async () => {
  const rejection = { status: 'rejected', error: { code: 'WATCH_VERSION_CONFLICT', message: 'Synthetic version conflict' } };
  const h = harness({ reject: rejection });
  const result = await h.run('agentbridge_business_watch_update', { watch_id: watchId, expected_version: 6, action: 'stop' });
  assert.deepEqual(payload(result), rejection);
  assert.equal(h.calls.length, 1);
});

test('server tool access and the explicit pending-only scope retain existing visibility restrictions', () => {
  const allowed = new Set(['agentbridge_business_watch_list', 'agentbridge_business_watch_get']);
  const limited = harness({ allowed });
  assert.ok(limited.tool('agentbridge_business_watch_list'));
  assert.ok(limited.tool('agentbridge_business_watch_get'));
  assert.equal(limited.tool('agentbridge_business_watch_create'), undefined);
  assert.equal(limited.tool('agentbridge_business_watch_update'), undefined);
  const pending = harness({ scope: 'oa_pending' });
  for (const name of [...watchNames, stateName]) assert.equal(pending.tool(name), undefined);
  assert.ok(pending.tool('oa_workflow_pending_list'));
});

test('an explicitly enforced loaded OA skill cannot use new platform watches to escape its scope', async () => {
  const h = harness({ guard: true });
  rememberSkillBinding(h.router, h.context, h.identity, { status: 'succeeded', binding_id: 'fixed-watch-test-binding',
    skill_id: 'oa-pending', profile: 'single' });
  for (const name of watchNames) {
    const result = await h.run(name, { target_ref: 'opaque-ref', watch_id: watchId, expected_version: 7, action: 'pause' });
    assert.equal(payload(result).error.code, 'SKILL_TOOL_SCOPE_REQUIRED');
  }
  assert.equal(h.calls.length, 0);
});

test('a selected workspace skill trial cannot create or change persistent watches', async () => {
  const h = harness({ trial: { draft_id: 'chosen-draft', expected_revision: 1, profile: 'review',
    prompt: '合成材料', request_key: 'chosen-trial' } });
  for (const name of watchNames) {
    const result = await h.run(name, { target_ref: 'opaque-ref', watch_id: watchId, expected_version: 7, action: 'pause' });
    assert.equal(payload(result).error.code, 'SKILL_TRIAL_BOUND');
  }
  assert.equal(h.calls.length, 0);
});

function notificationHarness(validation, { pauseAdapter = false } = {}) {
  const calls = [], sent = [], order = [];
  const adapterEntered = Promise.withResolvers(), adapterGate = Promise.withResolvers();
  const sessionKey = 'agent:main:telegram:direct:1001';
  const binding = { key: 'telegram:default:1001', channel: 'telegram', senderId: '1001', accountId: 'default' };
  const endpoint = { endpointId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', clientType: 'telegram',
    externalSubject: '1001', conversationRef: sessionKey, accountId: 'default',
    route: { channel: 'telegram', to: 'telegram:1001', accountId: 'default', threadId: null } };
  const notification = { deliveryId: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', attemptCount: 3,
    deliveryMode: 'business_watch', message: 'STALE_PRIVATE_CACHED_MESSAGE',
    task: { taskId: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', activeConversationRef: `business-watch:${watchId}` },
    event: { eventType: 'business_watch.changed', payload: { watchId, sequence: 2 } } };
  let current = validation;
  const client = { async callTool(name, args, options) {
    calls.push({ name, args, options }); order.push(name);
    if (name === 'agentbridge_host_notification_validate') {
      if (current instanceof Error) throw current;
      return typeof current === 'function' ? current(args) : current;
    }
    if (name === 'agentbridge_host_notification_ack') return { status: 'succeeded' };
    assert.fail(`Watch notifications must not invoke OA, resume, prepare or model tools: ${name}`);
  } };
  const coordinator = new InteractionCoordinator({ sharedState: createInteractionSharedState(),
    config: resolvePluginConfig({ autoPoll: false, syncTimeline: false, wakeAgentOnComplete: false }),
    api: { config: {}, logger: { info() {}, warn() {}, debug() {}, error() {} }, runtime: { channel: { outbound: {
      async loadAdapter() {
        order.push('loadAdapter'); adapterEntered.resolve();
        if (pauseAdapter) await adapterGate.promise;
        return { async sendPayload(value) { order.push('send'); sent.push(value); } };
      },
    } } } } });
  const router = { restoreSessionBinding: () => true };
  const controller = new AbortController();
  return { calls, sent, order, coordinator, notification, binding, controller,
    adapterEntered: adapterEntered.promise, releaseAdapter: () => adapterGate.resolve(),
    setValidation(value) { current = value; },
    run(value = notification) { return coordinator.processEndpointNotification(router, binding, client,
      controller.signal, endpoint, value); } };
}

test('real Coordinator validates the current attempt and sends only freshly authorized watch content', async () => {
  const message = '业务关注更新：合成事项\n当前节点：当前核对节点';
  const h = notificationHarness({ allowed: true, message });
  try {
    await h.run();
    assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].text, message);
    assert.equal(h.sent[0].payload.text, message);
    assert.equal(h.sent[0].to, 'telegram:1001');
    assert.equal(h.sent[0].accountId, 'default');
    assert.equal(h.order[0], 'agentbridge_host_notification_validate');
    assert.ok(h.order.indexOf('send') < h.order.indexOf('agentbridge_host_notification_ack'));
    assert.deepEqual(h.calls[0].args, { agent_host: 'openclaw', endpoint_key: h.binding.key,
      delivery_id: h.notification.deliveryId, attempt_count: 3 });
    assert.equal(h.calls[0].options.signal, h.controller.signal);
    assert.equal(h.calls[0].options.meta[HOST_CONTEXT_META_KEY].hostVersion, OPENCLAW_HOST_VERSION);
    const ack = h.calls.find(call => call.name.endsWith('_ack'));
    assert.equal(ack.args.delivery_id, h.notification.deliveryId);
    assert.equal(ack.args.succeeded, true);
    assert.equal(ack.args.attempt_count, 3);
    assert.ok(h.calls.filter(call => call.name.endsWith('_validate')).length >= 2);
    assert.equal(h.notification.deliveryMode, 'business_watch');
    assert.equal(h.sent.some(value => JSON.stringify(value).includes('STALE_PRIVATE')), false);
  } finally { h.coordinator.stopAll(); }
});

test('permission withdrawn after claim suppresses delivery and acknowledges the obsolete watch notice', async () => {
  const h = notificationHarness({ allowed: true, message: 'previous permission' });
  const claimed = structuredClone(h.notification);
  h.setValidation({ allowed: false });
  try {
    await h.run(claimed);
    assert.equal(h.sent.length, 0);
    assert.deepEqual(h.calls.map(call => call.name), ['agentbridge_host_notification_validate', 'agentbridge_host_notification_ack']);
    assert.equal(h.calls[1].args.succeeded, true);
    assert.equal(h.calls[1].args.defer_until_activity, false);
  } finally { h.coordinator.stopAll(); }
});

test('missing, malformed and failed fresh validation neither sends nor acknowledges or replays OA', async () => {
  for (const validation of [undefined, null, {}, { allowed: 'true', message: 'unsafe' },
    { allowed: true }, { allowed: true, message: 42 }, new Error('synthetic validation unavailable')]) {
    const h = notificationHarness(validation);
    try {
      await assert.rejects(() => h.run());
      assert.equal(h.sent.length, 0);
      assert.deepEqual(h.calls.map(call => call.name), ['agentbridge_host_notification_validate']);
    } finally { h.coordinator.stopAll(); }
  }
});

test('a later outbox attempt revalidates authorization without resuming or replaying a business operation', async () => {
  const h = notificationHarness(new Error('synthetic uncertain validation response'));
  try {
    await assert.rejects(() => h.run());
    h.setValidation({ allowed: true, message: '新尝试的当前授权内容' });
    await h.run({ ...h.notification, attemptCount: 4 });
    assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].text, '新尝试的当前授权内容');
    assert.equal(h.calls.filter(call => call.name.endsWith('_ack')).length, 1);
    assert.ok(h.calls.every(call => ['agentbridge_host_notification_validate', 'agentbridge_host_notification_ack'].includes(call.name)));
    const attempts = h.calls.filter(call => call.name.endsWith('_validate')).map(call => call.args.attempt_count);
    assert.equal(attempts[0], 3);
    assert.ok(attempts.length >= 3 && attempts.slice(1).every(attempt => attempt === 4));
    assert.equal(h.calls.at(-1).args.attempt_count, 4);
  } finally { h.coordinator.stopAll(); }
});

test('stale validation leaves the delivery unacknowledged for its current lease holder', async () => {
  const h = notificationHarness({ allowed: false, stale: true });
  try {
    await h.run();
    assert.equal(h.sent.length, 0);
    assert.deepEqual(h.calls.map(call => call.name), ['agentbridge_host_notification_validate']);
  } finally { h.coordinator.stopAll(); }
});

test('permission revoked while the outbound adapter loads suppresses sending', { timeout: 2000 }, async () => {
  const h = notificationHarness({ allowed: true, message: '初次允许的正文' }, { pauseAdapter: true });
  const running = h.run();
  try {
    await h.adapterEntered;
    h.setValidation({ allowed: false });
    h.releaseAdapter();
    await running;
    assert.equal(h.sent.length, 0);
    const ack = h.calls.find(call => call.name.endsWith('_ack'));
    assert.equal(ack.args.succeeded, true);
    assert.equal(ack.args.attempt_count, 3);
    assert.ok(h.calls.filter(call => call.name.endsWith('_validate')).length >= 2);
  } finally { h.releaseAdapter(); await running.catch(() => {}); h.coordinator.stopAll(); }
});

test('the final authorization after adapter load supplies the actual outbound message', { timeout: 2000 }, async () => {
  const h = notificationHarness({ allowed: true, message: '不可发送的旧观察正文' }, { pauseAdapter: true });
  const running = h.run();
  try {
    await h.adapterEntered;
    h.setValidation({ allowed: true, message: '发送前最终核实的新正文' });
    h.releaseAdapter();
    await running;
    assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].text, '发送前最终核实的新正文');
    assert.equal(h.sent[0].payload.text, '发送前最终核实的新正文');
    assert.equal(h.sent.some(value => JSON.stringify(value).includes('旧观察正文')), false);
  } finally { h.releaseAdapter(); await running.catch(() => {}); h.coordinator.stopAll(); }
});

test('a stale lease detected at the final send boundary is neither sent nor acknowledged', { timeout: 2000 }, async () => {
  const h = notificationHarness({ allowed: true, message: '初次允许的正文' }, { pauseAdapter: true });
  const running = h.run();
  try {
    await h.adapterEntered;
    h.setValidation({ allowed: false, stale: true });
    h.releaseAdapter();
    await running;
    assert.equal(h.sent.length, 0);
    assert.equal(h.calls.filter(call => call.name.endsWith('_ack')).length, 0);
  } finally { h.releaseAdapter(); await running.catch(() => {}); h.coordinator.stopAll(); }
});

test('invalid final authorization after adapter load neither sends nor acknowledges', { timeout: 2000 }, async () => {
  const h = notificationHarness({ allowed: true, message: '初次允许的正文' }, { pauseAdapter: true });
  const running = h.run();
  try {
    await h.adapterEntered;
    h.setValidation({ allowed: true });
    h.releaseAdapter();
    await assert.rejects(running);
    assert.equal(h.sent.length, 0);
    assert.equal(h.calls.filter(call => call.name.endsWith('_ack')).length, 0);
  } finally { h.releaseAdapter(); await running.catch(() => {}); h.coordinator.stopAll(); }
});
