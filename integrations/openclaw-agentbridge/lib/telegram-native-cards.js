import { createHash } from 'node:crypto';
import { openProjectionLocalStore } from './timeline-storage.js';
import { checkedReceipt, sendFailure, singleAttemptConfig } from './workspace-query-delivery.js';
import { canonicalTelegramAccount, sameTelegramAccount, telegramBotRouteRef } from './telegram-identity.js';
import { hostContextMeta, HOST_CONTEXT_META_KEY } from './host-contract.js';
import { buildPresentation, normalizeInteraction } from './interaction.js';

const hash = value => createHash('sha256').update(value).digest('hex');
const active = new Set();
const closed = new Set(['completed', 'expired', 'declined', 'failed', 'superseded']);
const labels = { credential: '登录卡', business_input: '填写卡', execution_authorization: '授权卡' };
const id = value => typeof value === 'string' && /^[a-f0-9-]{36}$/i.test(value);
const interactionId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{16,128}$/.test(value);
export function nativeCardClosureText(type, state) {
  const label = labels[type];
  if (!label || !closed.has(state)) return null;
  if (state === 'completed') return {
    credential: '此前发送的登录卡已完成登录，无需再次输入。请以原任务的当前状态和结果为准。',
    business_input: '此前发送的填写卡已接收内容，无需再次填写。请以原任务结果为准。',
    execution_authorization: '此前发送的授权卡已接收授权，无需再次授权。业务结果仍以原任务核验为准。',
  }[type];
  return {
    expired: `此前发送的${label}已过期，请勿继续使用旧卡。请回到原任务查看状态。`,
    declined: `此前发送的${label}已被拒绝，旧卡已关闭。请以原任务当前状态为准。`,
    failed: `此前发送的${label}未能完成，旧卡已关闭。请回到原任务查看状态。`,
    superseded: `此前发送的${label}已被替换，请勿继续使用旧卡。请回到原任务查看最新入口。`,
  }[state];
}

// New native cards only. No body, signed URL, credential, SDK database or poll
// offset is stored. Existing booleans can never create a row or a receipt here.
export class TelegramNativeCards {
  constructor({ api, config, store = undefined, now = Date.now, identityScope, endpointFor, routeFor, restoreRoute, clientFor }) {
    this.api = api; this.config = config; this.now = now;
    this.store = store === undefined ? openProjectionLocalStore(api, 'telegram-native-cards-v1') : store;
    this.identityScope = identityScope; this.endpointFor = endpointFor;
    this.routeFor = routeFor; this.restoreRoute = restoreRoute; this.clientFor = clientFor;
    this.enabled = Boolean(this.store && identityScope && endpointFor);
    this.cursor = new Map();
  }
  scope(sessionKey) {
    const peer = /^agent:[^:]+:telegram:(?:direct|dm):([1-9][0-9]*)$/i.exec(sessionKey || '')?.[1];
    const route = this.routeFor(sessionKey), identity = this.identityScope?.(sessionKey), endpointKey = this.endpointFor?.(sessionKey);
    const accountId = canonicalTelegramAccount(route?.accountId);
    const bot = telegramBotRouteRef(this.api.config, accountId, peer);
    if (!this.store || !peer || !identity || !endpointKey || !bot || route?.channel !== 'telegram' ||
        String(route.to).replace(/^telegram:/, '') !== peer || ![null, undefined, ''].includes(route.threadId)) return null;
    return { sessionKey, endpointKey, route: { to: peer, accountId },
      identityRef: hash(identity), botRouteRef: bot,
      hostInstanceId: hostContextMeta()[HOST_CONTEXT_META_KEY].hostInstanceId };
  }
  current(record) {
    const value = this.scope(record.sessionKey);
    return value && ['endpointKey', 'identityRef', 'botRouteRef', 'hostInstanceId'].every(k => value[k] === record[k]);
  }
  key(scope, interactionId) {
    // A changed token/pair must not inherit this original interaction's send.
    return 'native-card:' + hash(JSON.stringify([scope.sessionKey, scope.botRouteRef, interactionId]));
  }
  owns(record) {
    const scope = this.scope(record.sessionKey);
    if (!scope) return false;
    let original;
    try { original = this.store.lookup(this.key(scope, record.interaction.interactionId)); }
    catch { return false; }
    return original?.taskId === (record.taskId ?? null) && this.current(original) && original.card.state === 'sent' &&
      Number.isSafeInteger(original.card.receipt) && original.card.receipt > 0;
  }
  canHandle(sessionKey, interaction, taskId, standalone = false) {
    // An existing original intent always owns the fallback boundary, even when
    // its current identity can no longer send. Reconfiguration must not turn a
    // frozen/unknown native attempt into an untracked legacy send.
    if (this.original(sessionKey, interaction?.interactionId)) return true;
    return Boolean(this.enabled && this.scope(sessionKey) && (id(taskId) ||
      taskId == null && standalone && interaction?.type === 'credential') && interactionId(interaction?.interactionId) && labels[interaction.type]);
  }
  original(sessionKey, identifier) {
    try { return this.store?.entries().find(row => row.value.kind === 'native-card-v1' &&
      row.value.sessionKey === sessionKey && row.value.interactionId === identifier); }
    catch { return { unreadable: true }; }
  }
  async authority(record, client, signal) {
    if (!client || !this.current(record) || signal?.aborted) return null;
    const snapshot = await client.callTool('agentbridge_host_interaction_present', {
      agent_host: 'openclaw', endpoint_key: record.endpointKey, interaction_id: record.interactionId,
      state_only: true,
    }, { signal, meta: hostContextMeta() });
    const endpoint = snapshot?.endpoint, task = snapshot?.task, route = endpoint?.route;
    const ownedTask = record.taskId
      ? task?.taskId === record.taskId && task.agentHost === 'openclaw' && task.userSubject === endpoint?.userSubject &&
        task.originEndpointId === endpoint?.endpointId && task.activeConversationRef === record.sessionKey
      : record.standalone && record.type === 'credential' && task === null;
    if (snapshot?.status !== 'succeeded' || !ownedTask ||
        !id(endpoint?.endpointId) || endpoint.state !== 'active' || endpoint.agentHost !== 'openclaw' ||
        !endpoint.userSubject || endpoint.clientType !== 'telegram' ||
        endpoint.conversationRef !== record.sessionKey || endpoint.externalSubject !== record.route.to ||
        !sameTelegramAccount(endpoint.accountId, record.route.accountId) || route?.channel !== 'telegram' ||
        String(route.to).replace(/^telegram:/, '') !== record.route.to ||
        !sameTelegramAccount(route.accountId, record.route.accountId) || ![null, undefined, ''].includes(route.threadId) ||
        snapshot.interaction?.interactionId !== record.interactionId || snapshot.interaction.type !== record.type ||
        snapshot.interaction.taskId !== record.taskId ||
        record.endpointId && record.endpointId !== endpoint.endpointId ||
        record.subjectRef && record.subjectRef !== hash(endpoint.userSubject) || !this.current(record)) return null;
    return { endpoint, task, interaction: snapshot.interaction };
  }
  async deliver({ sessionKey, interaction, taskId = null, standalone = false, withDirectDelivery, signal }) {
    const scope = this.scope(sessionKey);
    const existing = this.original(sessionKey, interaction?.interactionId);
    if (existing?.unreadable) return 'unavailable';
    if (existing && (!scope || !this.current(existing.value) || existing.value.taskId !== taskId ||
        existing.value.type !== interaction.type)) return 'unavailable';
    if (!this.canHandle(sessionKey, interaction, taskId, standalone)) return 'unclaimed';
    const key = this.key(scope, interaction.interactionId), lock = `${this.store.filename}:${key}`;
    if (active.has(lock)) return 'pending';
    active.add(lock);
    try {
      let record = this.store.lookup(key);
      if (record && (!this.current(record) || record.taskId !== taskId || record.type !== interaction.type)) return 'unavailable';
      if (record) return await this.sendCard(key, record, withDirectDelivery, signal, interaction);
      record = { kind: 'native-card-v1', ...scope, taskId, interactionId: interaction.interactionId,
        type: interaction.type, standalone: !taskId && standalone, createdAt: this.now(), card: { state: 'prepared', attempt: 0 } };
      // Reserve a definitely-unsent intent before remote validation. A transient
      // state-only outage must not lose a card already removed from the reply
      // queue. No platform send occurs until original ownership is established.
      if (!this.store.registerIfAbsent(key, record)) return 'pending';
      return await this.sendCard(key, record, withDirectDelivery, signal, interaction);
    } catch {
      try { return this.store.lookup(key) ? 'unavailable' : 'unclaimed'; }
      catch { return 'unavailable'; }
    }
    finally { active.delete(lock); }
  }
  async sendCard(key, record, withDirectDelivery, signal, originalPresentation = null) {
    if (record.card.state === 'sending') return 'delivery_unknown';
    if (!['prepared', 'retry_after'].includes(record.card.state)) return record.card.state;
    if (record.card.dueAt > this.now()) return 'retry_after';
    const original = await this.authority(record, this.clientFor(record.sessionKey), signal);
    if (!original || !['pending', 'processing'].includes(original.interaction.state)) return 'unavailable';
    if (!record.endpointId) {
      this.store.update(key, current => {
        if (!current || current.endpointId || !this.current(current)) return undefined;
        current.endpointId = original.endpoint.endpointId; current.subjectRef = hash(original.endpoint.userSubject);
        return current;
      });
      record = this.store.lookup(key);
      if (record.endpointId !== original.endpoint.endpointId || record.subjectRef !== hash(original.endpoint.userSubject)) return 'unavailable';
    }
    // Initial sends reuse the normal tool's presentation. Only a definitely
    // unsent attempt may request the normal host presentation again. Closing
    // observation never calls this issuance path.
    if (!originalPresentation) {
      const issued = await this.clientFor(record.sessionKey).callTool('agentbridge_host_interaction_present', {
        agent_host: 'openclaw', endpoint_key: record.endpointKey, interaction_id: record.interactionId,
      }, { signal, meta: hostContextMeta() });
      if (issued?.status !== 'succeeded' || issued.interaction?.taskId !== record.taskId ||
          issued.interaction?.presentation?.endpointId !== record.endpointId) return 'unavailable';
      originalPresentation = issued.interaction;
    }
    const interaction = normalizeInteraction(originalPresentation, new Set(this.config.allowedCardOrigins));
    if (!interaction || interaction.interactionId !== record.interactionId || interaction.type !== record.type ||
        !['pending', 'processing'].includes(interaction.state) || !(Date.parse(interaction.expiresAt) > this.now())) return 'unavailable';
    const adapter = await this.api.runtime.channel.outbound.loadAdapter('telegram');
    if (!adapter?.sendPayload || !adapter.renderPresentation) return 'unavailable';
    const presentation = buildPresentation([interaction], 'telegram');
    const text = '请处理下面的 AgentBridge 安全卡片。';
    const payload = await adapter.renderPresentation({ payload: { text, presentation }, presentation,
      ctx: { cfg: this.api.config, ...record.route, payload: { text, presentation } } });
    if (!payload || !this.current(record) || signal?.aborted || !(Date.parse(interaction.expiresAt) > this.now())) return 'unavailable';
    return this.send(key, record, 'card', { ...payload, text }, adapter, withDirectDelivery, signal);
  }
  async send(key, record, part, payload, adapter, withDirectDelivery, signal) {
    const prior = record[part], attempt = prior.attempt + 1;
    if (attempt > 3) {
      this.store.update(key, current => {
        if (!current || current[part]?.attempt !== prior.attempt || current[part].state !== 'retry_after') return undefined;
        current[part].state = 'rejected'; return current;
      });
      return 'rejected';
    }
    if (signal?.aborted || !this.current(record)) return 'unavailable';
    let claimed = false;
    this.store.update(key, current => {
      if (!current || !this.current(current) || current[part]?.attempt !== prior.attempt ||
          !['prepared', 'retry_after'].includes(current[part].state)) return undefined;
      record = { ...current, [part]: { ...current[part], attempt, state: 'sending', ownerPid: process.pid } };
      claimed = true; return record;
    });
    if (!claimed) return 'pending';
    const capture = async result => {
      const receipt = checkedReceipt(result, record.route);
      if (!receipt) throw Error('NATIVE_CARD_RECEIPT_UNCONFIRMED');
      this.store.update(key, current => {
        if (!current || current.identityRef !== record.identityRef || current[part].attempt !== attempt ||
            current[part].receipt && current[part].receipt !== receipt) throw Error('NATIVE_CARD_RECEIPT_CONFLICT');
        if (this.store.entries().some(row => row.value.botRouteRef === record.botRouteRef &&
            Object.entries(row.value).some(([name, value]) => ['card', 'closure'].includes(name) &&
              value?.receipt === receipt && (row.key !== key || name !== part)))) {
          throw Error('NATIVE_CARD_RECEIPT_REUSED');
        }
        current[part] = { ...current[part], state: 'sent', receipt }; record = current; return current;
      });
    };
    try {
      const result = await withDirectDelivery(() => adapter.sendPayload({
        cfg: singleAttemptConfig(this.api.config), ...record.route, text: payload.text, payload,
        formatting: { parseMode: 'HTML' }, onDeliveryResult: capture,
      }));
      if (record[part].state !== 'sent') await capture(result);
    } catch (error) {
      this.store.update(key, current => {
        if (!current || current[part].attempt !== attempt || current[part].state === 'sent') return undefined;
        const failure = sendFailure(error);
        current[part] = { ...current[part], state: failure.outcome,
          ...(failure.outcome === 'retry_after' ? { dueAt: this.now() + failure.retryAfterSeconds * 1000 } : {}) };
        record = current; return current;
      });
    }
    return record[part].state;
  }
  async recover({ binding, client, endpoint, identityRouter, withDirectDelivery, signal }) {
    if (!this.store || binding.channel !== 'telegram' || signal?.aborted) return;
    let rows;
    try { rows = this.store.entries().filter(row => row.value.kind === 'native-card-v1' && row.value.endpointKey === binding.key); }
    catch { this.api.logger.debug?.('AgentBridge native card storage unavailable; delivery retained without replay'); return; }
    const start = (this.cursor.get(binding.key) || 0) % Math.max(1, rows.length);
    const batch = [...rows.slice(start), ...rows.slice(0, start)].slice(0, 16);
    this.cursor.set(binding.key, start + batch.length);
    for (const { key, value: record } of batch) {
      const lock = `${this.store.filename}:${key}`;
      if (active.has(lock) || signal?.aborted || !endpoint || record.endpointId && endpoint.endpointId !== record.endpointId ||
          endpoint.conversationRef !== record.sessionKey || endpoint.state !== 'active' ||
          endpoint.clientType !== 'telegram' || endpoint.externalSubject !== binding.senderId ||
          endpoint.externalSubject !== record.route.to || endpoint.route?.channel !== 'telegram' ||
          String(endpoint.route.to).replace(/^telegram:/, '') !== record.route.to ||
          !sameTelegramAccount(endpoint.accountId, binding.accountId) ||
          !sameTelegramAccount(endpoint.route.accountId, record.route.accountId) ||
          ![null, undefined, ''].includes(endpoint.route.threadId) ||
          !identityRouter.restoreSessionBinding({ sessionKey: record.sessionKey, bindingKey: binding.key })) continue;
      active.add(lock);
      try {
        // Restore only this exact authenticated route. No task resume/lease,
        // model call or old card send follows an uncertain boundary.
        this.restoreRoute?.(record.sessionKey, endpoint.route);
        if (!this.current(record) || this.clientFor(record.sessionKey) !== client) continue;
        if (['prepared', 'retry_after'].includes(record.card.state)) {
          await this.sendCard(key, record, fn => withDirectDelivery(record.sessionKey, fn), signal); continue;
        }
        if (record.card.state !== 'sent' || !Number.isSafeInteger(record.card.receipt) || record.card.receipt <= 0 ||
            record.closure && !['prepared', 'retry_after'].includes(record.closure.state)) continue;
        const original = await this.authority(record, client, signal);
        if (!original) continue;
        const interaction = original.interaction;
        if (!interaction || interaction.interactionId !== record.interactionId || interaction.type !== record.type ||
            interaction.taskId !== record.taskId || !closed.has(interaction.state) || !this.current(record)) continue;
        const text = nativeCardClosureText(record.type, interaction.state);
        if (!record.closure) this.store.update(key, current => {
          if (!current || current.card.state !== 'sent' || current.closure) return undefined;
          current.closure = { state: 'prepared', attempt: 0, observedState: interaction.state,
            originalReceipt: current.card.receipt, textHash: hash(text) }; return current;
        });
        const current = this.store.lookup(key);
        if (current.closure?.observedState !== interaction.state || current.closure.originalReceipt !== current.card.receipt ||
            current.closure.textHash !== hash(text) || current.closure.dueAt > this.now()) continue;
        const adapter = await this.api.runtime.channel.outbound.loadAdapter('telegram');
        if (!adapter?.sendPayload || !this.current(current) || signal?.aborted) continue;
        await this.send(key, current, 'closure', { text }, adapter, fn => withDirectDelivery(record.sessionKey, fn), signal);
      } catch { this.api.logger.debug?.('AgentBridge native card observation retained without replay'); }
      finally { active.delete(lock); }
    }
  }
}
