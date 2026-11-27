import { createHash } from 'node:crypto';
import { hostContextMeta } from './host-contract.js';
import {openTimelineLocalStore} from './timeline-storage.js';

const hash = value => createHash('sha256').update(value, 'utf8').digest('hex');
const NAMESPACE = 'agentbridge.timeline.projection.v2';
const MAXIMUM_TEXT = 200_000;
const PRIVATE = /\/(?:flow\/)?(?:auth|authorize|input|download|media|results|result-view)\/[a-z0-9_-]+|\/api\/(?:enrollment|private-cards?)\/|[?&#](?:start|ticket|presentation|token|code|signature|sig|key)=[^\s&]+|\bBearer\s+[^\s,;]+|\b(?:set-cookie|cookie|authorization)\s*:|io\.agentbridge\/(?:interaction|telegram-confirmation)|(?:\/agentbridge\s+link|\/start)\s+\S+|(?:确认码|验证码|配对码)\s*[:：]?\s*[0-9]{4,8}|-----BEGIN [A-Z ]*PRIVATE KEY-----/i;
const SECRET_LABEL = '(?:账号|帐号|用户名|帐密|账密|密码|口令|密钥|(?:[A-Za-z0-9]+[_-])*(?:user(?:name)?|password|passwd|pwd|access[_ -]?key(?:[_ -]?id)?|secret(?:[_ -]?key)?|api[_ -]?key|token|bearer))';
const SECRET = new RegExp(`(?:^|[^\\w])${SECRET_LABEL}["']?[ \\t]*[:：=][ \\t]*\\S|\\|[ *\u0060]*${SECRET_LABEL}[ *\u0060]*\\||\\b[a-z][a-z0-9+.-]*:\\/\\/[^/\\s:@]+:[^@\\s/]+@`, 'im');

// Select public text before durable storage. Central performs the authoritative
// check again. Neither rejected text nor a trusted card URL enters this journal.
export function publicTimelineText(value) {
  if (typeof value !== 'string' || value.includes('\0') || !value.isWellFormed()) return null;
  const text = value.replace(/^[\s\u001c-\u001f\u0085]+|[\s\u001c-\u001f\u0085]+$/gu,'');
  const decoded=text.replace(/(?:%[0-9a-f]{2})+/gi, bytes=>{
    try { return decodeURIComponent(bytes); }
    catch { return bytes.replace(/%([0-9a-f]{2})/gi, (_,hex)=>String.fromCharCode(parseInt(hex,16))); }
  });
  if (!text || /^[0-9]{8}$/.test(text) || PRIVATE.test(decoded) || SECRET.test(decoded)) return null;
  if ([...text].length > MAXIMUM_TEXT) return '原消息超过同步展示上限（200,000 个字符），请回本人 Telegram 原会话查看完整内容。';
  return text;
}

export function openTimelineProjectionStore(api) {
  return openTimelineLocalStore(api);
}

export class TimelineProjectionJournal {
  constructor(store) {
    if (!store || ['lookup','registerIfAbsent','update','entries','delete'].some(name => typeof store[name] !== 'function')) {
      throw new Error('TIMELINE_DURABLE_STORE_UNAVAILABLE');
    }
    this.store = store;
  }
  key(messageKey) { return `message:${hash(messageKey)}`; }
  reserve(record) {
    const key=this.key(record.messageKey);
    let old=this.store.lookup(key);
    if (old) {
      this.check(old,record);
      if (old.state==='sent') return old;
      if (old.state==='pending') return this.hydrate(old);
    }
    const chunks=bodyChunks(record.request.text);
    const bodyKeys=chunks.map((_,index)=>`body:${hash(record.messageKey)}:${record.payloadHash}:${index}`);
    if (!old) {
      let sequence;
      this.store.update('sequence',value=>({sequence:sequence=(value?.sequence || 0)+1}));
      if (!Number.isSafeInteger(sequence)) throw new Error('TIMELINE_SEQUENCE_INVALID');
      const {text,...request}=record.request;
      this.store.registerIfAbsent(key,{...record,request,bodyKeys,sequence,state:'assembling',attempts:0});
      old=this.check(this.store.lookup(key),record);
    }
    for (let index=0;index<chunks.length;index++) {
      const value={kind:'timeline-body',payloadHash:record.payloadHash,index,text:chunks[index]};
      this.store.registerIfAbsent(bodyKeys[index],value);
      const stored=this.store.lookup(bodyKeys[index]);
      if (JSON.stringify(stored)!==JSON.stringify(value)) throw new Error('TIMELINE_BODY_CONFLICT');
    }
    // The final manifest becomes visible only after every bounded local value was
    // committed. Interrupted assembly is not eligible for network publication.
    this.store.update(key,value=>{
      this.check(value,record);
      return value.state==='assembling' ? {...value,state:'pending'} : undefined;
    });
    const stored=this.store.lookup(key);
    return stored.state==='sent' ? stored : this.hydrate(stored);
  }
  check(stored, expected) {
    if (!stored || stored.payloadHash !== expected.payloadHash || stored.namespace !== expected.namespace) {
      throw new Error('TIMELINE_MESSAGE_ID_CONFLICT');
    }
    return stored;
  }
  hydrate(record) {
    if (!Array.isArray(record.bodyKeys) || !record.bodyKeys.length) throw new Error('TIMELINE_BODY_MISSING');
    const text=record.bodyKeys.map((key,index)=>{
      const chunk=this.store.lookup(key);
      if (chunk?.kind!=='timeline-body' || chunk.payloadHash!==record.payloadHash || chunk.index!==index || typeof chunk.text!=='string') {
        throw new Error('TIMELINE_BODY_MISSING');
      }
      return chunk.text;
    }).join('');
    const request={...record.request,text};
    // Preserve the original request field order when checking its canonical hash.
    if (hash(text)!==record.textSha256 || requestHash(request,record.sourceTextSha256)!==record.payloadHash) {
      throw new Error('TIMELINE_BODY_CORRUPT');
    }
    return {...record,request};
  }
  pending() { return this.store.entries().map(entry=>entry.value).filter(value=>value?.state==='pending').sort((a,b)=>a.sequence-b.sequence).map(value=>this.hydrate(value)); }
  attempted(record) {
    this.store.update(this.key(record.messageKey),value=>value?.state==='pending' ? {...value,attempts:value.attempts+1} : undefined);
  }
  complete(record,receipt) {
    this.store.update(this.key(record.messageKey),value=>{
      this.check(value,record);
      if (value.state==='sent') return undefined;
      const {request,...metadata}=value;
      return {...metadata,state:'sent',receipt};
    });
    // The receipt must survive even if later body cleanup fails. Cleanup never
    // changes delivery state or makes an already acknowledged entry replayable.
    try { this.pruneBody(this.store.lookup(this.key(record.messageKey))); } catch {}
  }
  pruneBody(record) {
    for (const key of record.bodyKeys || []) this.store.delete(key);
    this.store.update(this.key(record.messageKey),value=>{
      if (value?.state!=='sent' || !value.bodyKeys) return undefined;
      const {bodyKeys,...metadata}=value;return metadata;
    });
  }
  pruneCompleted() {
    for (const entry of this.store.entries()) if (entry.value?.state==='sent' && entry.value.bodyKeys) this.pruneBody(entry.value);
  }
}

function bodyChunks(text) {
  const chunks=[];let value='',size=0;
  for (const character of text) {
    const bytes=Buffer.byteLength(JSON.stringify(character),'utf8')-2;
    if (size+bytes>24_000 && value) {chunks.push(value);value='';size=0;}
    value+=character;size+=bytes;
  }
  if (value) chunks.push(value);
  return chunks;
}
function requestHash(request,sourceTextSha256) {
  return hash(JSON.stringify([Object.keys(request).sort().map(key=>[key,request[key]]),sourceTextSha256]));
}

export class DurableTimelineProjection {
  constructor({identityRouter,store,hostConfig={},logger,now=Date.now,sleep=delay}) {
    this.router=identityRouter;this.journal=new TimelineProjectionJournal(store);this.hostConfig=hostConfig;
    this.logger=logger;this.now=now;this.sleep=sleep;this.queues=new Map();this.timer=null;
  }
  identity(binding, sessionKey, endpointKey) {
    const credential=this.router.catalogCacheKeyForBinding(binding);
    const account=binding.accountId || 'default', telegram=this.hostConfig.channels?.telegram;
    const accountConfig=telegram?.accounts?.[account] || telegram;
    const token=accountConfig?.botToken ?? telegram?.botToken;
    // A configured Bot id is a route namespace, never a credential in the journal.
    const botId=typeof token==='string' && /^[0-9]+:/.test(token) ? token.split(':',1)[0] : null;
    if (!credential || !botId) throw new Error('TIMELINE_SOURCE_IDENTITY_UNAVAILABLE');
    const routeNamespace=hash(JSON.stringify([this.router.endpoint?.url,binding.channel,account,botId,sessionKey,endpointKey]));
    return {routeNamespace,namespace:hash(JSON.stringify([credential,routeNamespace]))};
  }
  capture({resolved,sessionKey,endpointKey,role,text,event={},context={},taskId=null}) {
    const selected=publicTimelineText(text);
    if (!selected) return Promise.resolve(false);
    const binding=resolved.binding;
    const sourceId=event.messageId ?? context.messageId;
    if (!['string','number'].includes(typeof sourceId) || !String(sourceId).trim() || String(sourceId).length>256) {
      this.warn('TIMELINE_PLATFORM_MESSAGE_ID_UNAVAILABLE');return Promise.resolve(false);
    }
    let record;
    try {
      const {namespace,routeNamespace}=this.identity(binding,sessionKey,endpointKey);
      const messageKey=`telegram:v2:${routeNamespace}:${role}:${String(sourceId)}`;
      const to=String(context.conversationId || event.conversationId || event.to || binding.senderId).replace(/^telegram:/u,'');
      const accountId=context.accountId || event.accountId || binding.accountId;
      if (!/^[1-9][0-9]*$/.test(to) || to!==String(binding.senderId) || (accountId || 'default')!==(binding.accountId || 'default') || context.threadId || event.threadId) {
        throw new Error('TIMELINE_SOURCE_ROUTE_CONFLICT');
      }
      const request={agent_host:'openclaw',projection_v2:true,endpoint_key:endpointKey,client_type:'telegram',external_subject:binding.senderId,
        conversation_ref:sessionKey,message_key:messageKey,role,text:selected,account_id:binding.accountId,label:binding.label,
        route:{channel:'telegram',to,accountId:binding.accountId,threadId:null},task_id:taskId};
      record=this.journal.reserve({schemaVersion:NAMESPACE,namespace,messageKey,bindingKey:binding.key,sessionKey,endpointKey,
        payloadHash:requestHash(request,hash(text)),sourceTextSha256:hash(text),textSha256:hash(selected),request,createdAt:this.now()});
      if (record.state==='sent') return Promise.resolve(true);
    } catch(error) {this.warn(error);return Promise.resolve(false);}
    // reserve above is synchronous: no network or async hook continuation can
    // precede durable admission once this observation callback has been invoked.
    return this.schedule(record.namespace);
  }
  resolve(record) {
    const identity=this.router.configuredIdentities().find(value=>value.binding.key===record.bindingKey);
    if (!identity || this.identity(identity.binding,record.sessionKey,record.endpointKey).namespace!==record.namespace) return null;
    const pinned=this.router.resolvePinnedSession({sessionKey:record.sessionKey,channel:'telegram',accountId:identity.binding.accountId});
    if (pinned && (!pinned.bound || pinned.binding.key!==record.bindingKey)) return null;
    // Never restore/register a Telegram endpoint from historical records. The
    // v2 Central call checks the original registered endpoint and current token.
    if (pinned?.bound && this.router.endpointKeyForSession(record.sessionKey)!==record.endpointKey) return null;
    return identity.client;
  }
  async send(record) {
    for (let attempt=0;attempt<3;attempt++) {
      try {
        const client=this.resolve(record);if (!client) return false;
        this.journal.attempted(record);
        const result=await client.callTool('agentbridge_host_timeline_append',record.request,
          {signal:AbortSignal.timeout(5000),meta:hostContextMeta()});
        const receipt=result?.projectionReceipt;
        if (result?.status!=='succeeded' || !receipt || !receipt.entryId || !receipt.sourceEndpointId || receipt.messageKey!==record.messageKey ||
            receipt.textSha256!==record.textSha256 || receipt.role!==record.request.role ||
            result.entry?.entryId!==receipt.entryId || result.endpoint?.endpointId!==receipt.sourceEndpointId) {
          throw new Error('TIMELINE_PROJECTION_RECEIPT_INVALID');
        }
        this.journal.complete(record,receipt);return true;
      } catch(error) {
        if (attempt<2) await this.sleep(attempt===0?250:1000);
        else this.warn(error);
      }
    }
    return false;
  }
  schedule(namespace) {
    if (this.queues.has(namespace)) return this.queues.get(namespace);
    const work=Promise.resolve().then(async()=>{
      for (;;) {
        const record=this.journal.pending().find(value=>value.namespace===namespace);
        if (!record) return true;
        if (!await this.send(record)) return false;
      }
    }).catch(error=>{this.warn(error);return false;}).finally(()=>this.queues.delete(namespace));
    this.queues.set(namespace,work);return work;
  }
  async recover() {
    this.journal.pruneCompleted();
    return Promise.allSettled([...new Set(this.journal.pending().map(value=>value.namespace))].map(value=>this.schedule(value)));
  }
  start() {
    if (this.timer) return;
    void this.recover().catch(error=>this.warn(error));
    this.timer=setInterval(()=>void this.recover().catch(error=>this.warn(error)),30_000);this.timer.unref?.();
  }
  stop() {clearInterval(this.timer);this.timer=null;}
  async waitForIdle() {await Promise.allSettled([...this.queues.values()]);}
  warn(error) {
    const message=typeof error==='string' ? error : error?.message;
    const code=/^TIMELINE_[A-Z0-9_]{1,64}$/.test(message || '') ? message
      : /^(?:SQLITE_|E[A-Z])[A-Z0-9_]{0,64}$/.test(error?.code || '') ? error.code : 'TIMELINE_SYNC_FAILED';
    this.logger?.warn?.(`AgentBridge durable timeline mirror pending (${code})`);
  }
}
function delay(ms) {return new Promise(resolve=>{const timer=setTimeout(resolve,ms);timer.unref?.();});}
