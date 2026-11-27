import { createHash } from 'node:crypto';
import { openProjectionLocalStore } from './timeline-storage.js';
import { checkedReceipt, sendFailure, singleAttemptConfig } from './workspace-query-delivery.js';
import { canonicalTelegramAccount, sameTelegramAccount, telegramBotRouteRef } from './telegram-identity.js';
import { hostContextMeta, HOST_CONTEXT_META_KEY } from './host-contract.js';
import { publicTimelineText } from './timeline-projection.js';

const hash = value => createHash('sha256').update(value).digest('hex');
const positive = value => /^[1-9][0-9]*$/.test(String(value)) && Number.isSafeInteger(Number(value));
const peer = value => String(value ?? '').replace(/^telegram:/, '');
const handled = Object.freeze({ handled: true, reason: 'agentbridge.read-only-reference' });

// A model message(send) call is not a reply capability: it may carry an arbitrary
// target and body. This public reply_dispatch handler owns only a received
// private Telegram message and sends the checked public summary itself. The SDK
// gets no reply payload, so its normal dispatcher cannot send it a second time.
export class TelegramReferenceReply {
  constructor({ api, router, coordinator, reference, sharedState, store, loadSessionSdk, now = Date.now }) {
    this.api = api; this.router = router; this.coordinator = coordinator; this.reference = reference; this.now = now;
    this.store = store === undefined ? openProjectionLocalStore(api, 'telegram-reference-replies-v1') : store;
    this.loadSessionSdk = loadSessionSdk || (() => import('openclaw/plugin-sdk/session-store-runtime'));
    this.sources = sharedState.telegramReferenceSources ||= new Map();
  }
  scope(sessionKey) {
    const to = /^agent:[^:]+:telegram:(?:direct|dm):([1-9][0-9]*)$/.exec(sessionKey || '')?.[1];
    const identity = this.reference.identity(sessionKey), route = this.coordinator.sessionRoutes.get(sessionKey);
    const fingerprint = this.reference.fingerprint(identity, sessionKey);
    const accountId = canonicalTelegramAccount(identity?.binding?.accountId);
    const botRouteRef = telegramBotRouteRef(this.api.config, accountId, to);
    if (!to || !identity?.bound || identity.binding.channel !== 'telegram' || peer(identity.binding.senderId) !== to ||
        !fingerprint || !botRouteRef || route?.channel !== 'telegram' || peer(route.to) !== to ||
        !sameTelegramAccount(route.accountId, accountId) || ![undefined, null, ''].includes(route.threadId)) return null;
    return { sessionKey, endpointKey: this.router.endpointKeyForSession(sessionKey), route: { to, accountId },
      identityRef: hash(fingerprint), botRouteRef, hostInstanceId: hostContextMeta()[HOST_CONTEXT_META_KEY].hostInstanceId };
  }
  current(record) {
    const scope = this.scope(record.sessionKey);
    return scope && ['endpointKey', 'identityRef', 'botRouteRef', 'hostInstanceId'].every(key => scope[key] === record[key]);
  }
  capture(event, context) {
    const sessionKey = event.sessionKey || context.sessionKey;
    const sourceKey = this.sourceKey(sessionKey, event.messageId);
    this.sources.delete(sourceKey);
    const selection = this.reference.select(event.content ?? event.text, { sessionKey });
    if (!selection) return;
    const scope = this.scope(sessionKey), sourceId = event.messageId, turn = this.reference.turn(sessionKey);
    const text = event.content ?? event.text;
    if (!scope || !turn || !positive(sourceId) || typeof text !== 'string' || !text.isWellFormed() ||
        context.channelId !== 'telegram' || peer(event.senderId) !== scope.route.to || peer(event.from) !== scope.route.to ||
        peer(context.conversationId) !== scope.route.to || !sameTelegramAccount(context.accountId, scope.route.accountId) ||
        [event.channelId, event.metadata?.provider, event.metadata?.originatingChannel].some(value => value != null && value !== 'telegram') ||
        [event.conversationId, context.senderId, event.metadata?.senderId, event.metadata?.originatingTo, event.metadata?.to]
          .some(value => value != null && peer(value) !== scope.route.to) ||
        !sameTelegramAccount(event.accountId ?? context.accountId, scope.route.accountId) ||
        context.sessionKey && context.sessionKey !== sessionKey ||
        [event.threadId, context.threadId, event.metadata?.threadId].some(value => value != null && value !== '') ||
        event.isGroup === true || context.isGroup === true || event.metadata?.isGroup === true ||
        context.messageId != null && String(context.messageId) !== String(sourceId)) return;
    this.sources.set(sourceKey, { ...scope, sourceId: String(sourceId), turn, selection: structuredClone(selection),
      sourceTextSha256: hash(text.trim()) });
    // Only volatile received-event snapshots, never persisted sends, are bounded.
    if (this.sources.size > 500) this.sources.delete(this.sources.keys().next().value);
  }
  sourceKey(sessionKey, sourceId) { return JSON.stringify([sessionKey, String(sourceId)]); }
  forget(sessionKey) {
    for (const [key, value] of this.sources) if (value.sessionKey === sessionKey) this.sources.delete(key);
  }
  validTurn(source, context, text) {
    return source && this.current(source) && !context.signal?.aborted &&
      this.sources.get(this.sourceKey(source.sessionKey, source.sourceId)) === source && source.sourceTextSha256 === hash(text.trim()) &&
      context.trigger === 'user' && context.channel === 'telegram' &&
      peer(context.senderId) === source.route.to && peer(context.chatId) === source.route.to &&
      peer(context.channelId) === source.route.to &&
      (!context.messageProvider || context.messageProvider === 'telegram') &&
      (!context.channelContext?.sender?.id || peer(context.channelContext.sender.id) === source.route.to) &&
      (!context.channelContext?.chat?.id || peer(context.channelContext.chat.id) === source.route.to) &&
      (!context.channelContext?.chat?.type || ['direct', 'private', 'dm'].includes(context.channelContext.chat.type));
  }
  canSend(sdk, context) {
    const agentId = context.sessionKey.split(':')[1];
    const storePath = sdk.resolveStorePath(this.api.config.session?.store, { agentId });
    const entry = sdk.getSessionEntry({ agentId, sessionKey: context.sessionKey, storePath, readConsistency: 'latest' });
    // Respect the original SDK session's /send policy, including a denial that
    // arrives while Central is resolving. A reset changes this snapshot; a new
    // session is allowed only by the original dispatcher and current SDK policy.
    return Boolean((entry?.sessionId ?? null) === context.sessionId && !entry?.spawnedBy && !entry?.parentSessionKey &&
      sdk.resolveSendPolicy({ cfg: this.api.config, entry, sessionKey: context.sessionKey, channel: 'telegram', chatType: 'direct' }) === 'allow');
  }
  fallback(event, context) {
    if (!/^agent:[^:]+:telegram:(?:direct|dm):[1-9][0-9]*$/.test(context.sessionKey || '') ||
        !this.reference.select(event.cleanedBody, context)) return undefined;
    this.reference.lock(context);
    return handled; // No authoritative original MessageSid: never guess a send.
  }
  async dispatch(event, context) {
    const input = event.ctx || {}, sessionKey = event.sessionKey || input.SessionKey;
    const sourceId = input.MessageSidFull ?? input.MessageSid ?? input.MessageSidFirst ?? input.MessageSidLast;
    const text = input.BodyForCommands || input.RawBody || input.Body;
    const source = this.sources.get(this.sourceKey(sessionKey, sourceId));
    const referenceContext = { sessionKey, runId: event.runId, referenceTurn: source?.turn };
    if (!/^agent:[^:]+:telegram:(?:direct|dm):[1-9][0-9]*$/.test(sessionKey || '') || typeof text !== 'string' ||
        !(source?.selection || this.reference.select(text, referenceContext))) return undefined;
    this.reference.lock(referenceContext);
    // message_tool_only suppresses automatic replies, not this authenticated
    // original-session transport. Parent-owned/background sessions stay barred.
    if (!event.isTailDispatch && event.sendPolicy === 'allow' &&
        (!event.suppressUserDelivery || event.sourceReplyDeliveryMode === 'message_tool_only') &&
        !input.ParentSessionKey && !input.ModelParentSessionKey &&
        input.ChatType === 'direct' && input.SessionKey === sessionKey &&
        peer(input.From) === source?.route.to && sameTelegramAccount(input.AccountId, source?.route.accountId) &&
        [input.MessageThreadId, event.originatingThreadId].every(value => value == null || value === '') &&
        (!event.shouldRouteToOriginating || event.originatingChannel === 'telegram' &&
          peer(event.originatingTo) === source?.route.to && sameTelegramAccount(event.originatingAccountId, source?.route.accountId))) {
      await this.reply({ cleanedBody: text }, { ...referenceContext, sourceId: String(sourceId), signal: context.abortSignal,
        trigger: 'user', channel: input.OriginatingChannel ?? input.Surface ?? input.Provider,
        messageProvider: input.Provider, chatId: peer(input.ChatId ?? input.To), channelId: peer(input.OriginatingTo ?? input.To),
        senderId: input.SenderId, channelContext: input.ChannelContext });
    }
    let confirmed = false;
    try {
      const record = source && this.store?.lookup(this.receiptKey(source));
      confirmed = Boolean(record?.state === 'sent' && positive(record.receipt) && this.current(record) &&
        record.sourceTextSha256 === hash(text.trim()));
    } catch {} // A failed receipt read cannot invent a delivered final reply.
    context.recordProcessed?.('completed', { reason: confirmed ? 'agentbridge_reference_sent' : 'agentbridge_reference_unconfirmed' });
    context.markIdle?.('message_completed');
    return { handled: true, queuedFinal: confirmed, counts: { tool: 0, block: 0, final: confirmed ? 1 : 0 } };
  }
  receiptKey(source) { return 'reply:' + hash(JSON.stringify([source.sessionKey, source.botRouteRef, source.sourceId])); }
  async reply(event, context) {
    const sessionKey = context.sessionKey, text = event.cleanedBody;
    if (!/^agent:[^:]+:telegram:(?:direct|dm):[1-9][0-9]*$/.test(sessionKey || '') ||
        typeof text !== 'string') return undefined;
    this.reference.lock(context);
    try {
      const source = this.sources.get(this.sourceKey(sessionKey, context.sourceId));
      if (!this.store || !this.validTurn(source, context, text)) return handled;
      const sdk = await this.loadSessionSdk();
      const storePath = sdk.resolveStorePath(this.api.config.session?.store, { agentId: sessionKey.split(':')[1] });
      context.sessionId = sdk.getSessionEntry({ sessionKey, agentId: sessionKey.split(':')[1], storePath, readConsistency: 'latest' })?.sessionId ?? null;
      if (!this.validTurn(source, context, text) || !this.canSend(sdk, context)) return handled;
      const key = this.receiptKey(source);
      const original = this.store.lookup(key);
      if (original) {
        if (original.sourceTextSha256 === source.sourceTextSha256 && this.current(original)) await this.project(key, original);
        return handled; // sent, uncertain, refused, or interrupted: never replay.
      }
      const resolved = await this.reference.resolve(text, context, { selection: source.selection,
        isCurrent: () => this.validTurn(source, context, text) });
      if (!resolved || !this.validTurn(source, context, text)) return handled;
      const replyText = publicTimelineText(resolved.message);
      const adapter = await this.api.runtime.channel.outbound.loadAdapter('telegram');
      if (!replyText || !adapter?.sendPayload || !this.validTurn(source, context, text) || !this.canSend(sdk, context)) return handled;
      // Fixed public text is bounded by referenceMessage (at most 8 short
      // candidates). HTML is escaped before the SDK, never interpreted as a
      // model-supplied presentation, URL or delivery target.
      const wireText = replyText.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
      if (wireText.length > 3500) return handled;
      const { selection, ...sourceReceipt } = source;
      const record = { ...sourceReceipt, kind: 'telegram-reference-reply-v1', state: 'sending',
        createdAt: this.now(), text: replyText, textSha256: hash(replyText) };
      if (!this.store.registerIfAbsent(key, record)) return handled;
      const capture = async result => {
        const receipt = checkedReceipt(result, source.route);
        if (!receipt) throw Error('REFERENCE_REPLY_RECEIPT_UNCONFIRMED');
        this.store.update(key, value => {
          if (!value || value.identityRef !== source.identityRef || value.receipt && value.receipt !== receipt) throw Error('REFERENCE_REPLY_RECEIPT_CONFLICT');
          if (this.store.entries().some(row => row.key !== key && row.value.botRouteRef === source.botRouteRef && row.value.receipt === receipt)) {
            throw Error('REFERENCE_REPLY_RECEIPT_REUSED');
          }
          return { ...value, state: 'sent', receipt };
        });
      };
      try {
        const result = await this.coordinator.withNativeDirectDelivery(sessionKey, () => adapter.sendPayload({
          cfg: singleAttemptConfig(this.api.config), ...source.route, text: wireText, payload: { text: wireText },
          formatting: { parseMode: 'HTML' }, onDeliveryResult: capture,
        }));
        if (this.store.lookup(key).state !== 'sent') await capture(result);
      } catch (error) {
        this.store.update(key, value => value?.state === 'sent' ? undefined : { ...value, state: sendFailure(error).outcome });
      }
      await this.project(key, this.store.lookup(key));
    } catch {
      this.api.logger.warn('AgentBridge reference reply retained without replay');
    }
    return handled;
  }
  async project(key, record) {
    if (record?.state !== 'sent' || record.projected || !positive(record.receipt) || !record.text ||
        hash(record.text) !== record.textSha256 || !this.current(record)) return;
    const ok = await this.coordinator.timelinePublisher?.capture({ sessionKey: record.sessionKey, role: 'assistant', text: record.text,
      event: { messageId: String(record.receipt), to: record.route.to },
      context: { channelId: 'telegram', conversationId: record.route.to, accountId: record.route.accountId }, taskId: null });
    if (ok) this.store.update(key, value => {
      if (value?.receipt !== record.receipt || value.state !== 'sent') return undefined;
      const { text, ...rest } = value; return { ...rest, projected: true };
    });
  }
  async recover({ binding, endpoint }) {
    if (!this.store || binding.channel !== 'telegram' || endpoint?.state !== 'active' || endpoint.clientType !== 'telegram' ||
        endpoint.agentHost !== 'openclaw' || peer(endpoint.externalSubject) !== String(binding.senderId) ||
        endpoint.route?.channel !== 'telegram' || peer(endpoint.route.to) !== String(binding.senderId) ||
        !sameTelegramAccount(endpoint.accountId, binding.accountId) || !sameTelegramAccount(endpoint.route.accountId, binding.accountId) ||
        ![undefined, null, ''].includes(endpoint.route.threadId)) return;
    try {
      for (const { key, value } of this.store.entries()) {
        if (value.state !== 'sent' || value.projected || value.endpointKey !== binding.key || value.sessionKey !== endpoint.conversationRef) continue;
        const originalIdentity = hash(JSON.stringify([binding.key, value.endpointKey, this.router.catalogCacheKeyForBinding(binding) || null]));
        if (value.identityRef !== originalIdentity || value.botRouteRef !== telegramBotRouteRef(this.api.config, binding.accountId, binding.senderId) ||
            value.hostInstanceId !== hostContextMeta()[HOST_CONTEXT_META_KEY].hostInstanceId) continue;
        if (!this.router.restoreSessionBinding({ sessionKey: value.sessionKey, bindingKey: binding.key })) continue;
        this.coordinator.bindDeliveryRoute({ sessionKey: value.sessionKey, ...endpoint.route });
        await this.project(key, value);
      }
    } catch { this.api.logger.debug?.('AgentBridge reference projection retained for recovery'); }
  }
}
