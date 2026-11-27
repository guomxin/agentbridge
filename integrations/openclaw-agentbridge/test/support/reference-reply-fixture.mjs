import path from 'node:path';
import { nativeFixture } from './native-card-fixture.mjs';
import { TimelineLocalStore } from '../../lib/timeline-storage.js';

export const summary = { decision: 'selected', allowNewOperation: false, entries: [{ entryId: 'synthetic-entry',
  source: { clientType: 'web' }, role: 'user', text: '合成公开摘要 <内容> & 保留原文', createdAt: '2026-11-27T13:00:00+00:00' }] };
export const query = '继续网页消息 synthetic-entry，请解释这条公开说明。';
export function referenceFixture(home, options = {}) {
  const h = nativeFixture(home, options), restart = h.restart;
  h.projections = []; h.policy = 'allow'; h.sessionId = 'synthetic-session';
  h.extraTool = async name => {
    if (name === 'agentbridge_host_cross_endpoint_context') return h.resolve ? h.resolve() : summary;
  };
  h.restart = () => {
    restart();
    const store = new TimelineLocalStore(path.join(home, 'reference-reply'));
    h.stores.push(store); h.referenceStore = store;
    h.reference = h.bridge.telegramReferenceReply;
    h.reference.store = store;
    h.reference.loadSessionSdk = options.loadSessionSdk || (async () => ({
      resolveStorePath: () => '/synthetic-only', getSessionEntry: () => ({ sessionId: h.sessionId }),
      resolveSendPolicy: () => h.policy,
    }));
    h.bridge.timelinePublisher = { async capture(value) { h.projections.push(value); return h.project ? h.project(value) : true; } };
  };
  h.context = () => ({ sessionKey: h.sessionKey, sessionId: h.sessionId, trigger: 'user', channel: 'telegram',
    messageProvider: 'telegram', channelId: '1001', chatId: '1001', senderId: '1001',
    channelContext: { sender: { id: '1001' }, chat: { id: '1001', type: 'direct' } } });
  h.receive = (id = '17', text = query, changes = {}) => { h.sourceId = id; return h.hooks.message_received({ sessionKey: h.sessionKey,
    from: 'telegram:1001', senderId: '1001', messageId: id, content: text, ...changes },
  { sessionKey: h.sessionKey, channelId: 'telegram', conversationId: 'telegram:1001', accountId: 'default', messageId: id }); };
  h.ask = (text = query, context = h.context(), id = h.sourceId) => h.hooks.reply_dispatch({ sessionKey: context.sessionKey,
    sendPolicy: context.trigger === 'user' ? 'allow' : 'deny', suppressUserDelivery: false,
    ctx: { SessionKey: context.sessionKey, MessageSid: id, BodyForCommands: text, RawBody: text,
      From: 'telegram:1001', AccountId: 'default', ChatType: 'direct', OriginatingChannel: context.channel,
      Provider: context.messageProvider, ChatId: context.chatId, To: context.channelId, OriginatingTo: context.channelId,
      SenderId: context.senderId, ChannelContext: context.channelContext } }, {});
  h.restart(); return h;
}
