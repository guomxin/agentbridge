import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {DurableTimelineProjection,publicTimelineText} from '../lib/timeline-projection.js';
import {TimelinePublisher} from '../lib/timeline.js';

const sha=text=>createHash('sha256').update(text).digest('hex');
function memoryStore(){
  const rows=new Map();
  return {rows,delete(key){return rows.delete(key);},lookup(key){return structuredClone(rows.get(key));},registerIfAbsent(key,value){assert(Buffer.byteLength(JSON.stringify(value))<=65536,'bounded local value');if(rows.has(key))return false;rows.set(key,structuredClone(value));return true;},
    update(key,fn){const value=fn(structuredClone(rows.get(key)));if(value===undefined)return false;rows.set(key,structuredClone(value));return true;},
    entries(){return [...rows].map(([key,value])=>({key,value:structuredClone(value)}));}};
}
function setup({store=memoryStore(),send,sender='101',bot='123',credential='credential-a',channel='telegram'}={}){
  const requests=[],logs=[],binding={key:`telegram:default:${sender}`,channel,accountId:'default',senderId:sender,label:'private'};
  const sessionKey=`agent:main:telegram:direct:${sender}`,endpointKey=binding.key;
  const router={endpoint:{url:'https://central.invalid/mcp'},credential,binding,
    catalogCacheKeyForBinding(){return this.credential;},configuredIdentities(){return [{binding,client}];},
    resolvePinnedSession(){return {bound:true,binding,client};},endpointKeyForSession(){return endpointKey;}};
  const receipt=request=>({status:'succeeded',entry:{entryId:'entry-'+request.message_key},endpoint:{endpointId:'original-endpoint'},projectionReceipt:{
    entryId:'entry-'+request.message_key,sourceEndpointId:'original-endpoint',messageKey:request.message_key,textSha256:sha(request.text),role:request.role}});
  const client={async callTool(name,request){assert.equal(name,'agentbridge_host_timeline_append');requests.push(structuredClone(request));return send?send(request,receipt):receipt(request);}};
  const hostConfig={channels:{telegram:{botToken:bot+':synthetic-test-token'}}};
  const options={identityRouter:router,store,hostConfig,logger:{warn:value=>logs.push(value)},sleep:async()=>{},now:()=>1000};
  const publisher=new DurableTimelineProjection(options);
  const capture=(id,text='同一文字',extra={})=>publisher.capture({resolved:{binding,client},sessionKey,endpointKey,role:'user',text,event:{messageId:id},context:{conversationId:sender,accountId:'default'},...extra});
  return {publisher,store,router,binding,hostConfig,requests,logs,options,capture,sessionKey,endpointKey,receipt};
}


test('same text with distinct platform IDs persists as two original messages',async()=>{
  const h=setup();const first=h.capture('1'),second=h.capture('2');
  assert.equal(h.publisher.journal.pending().length,2);assert.equal(h.requests.length,0);
  await Promise.all([first,second]);assert.equal(h.requests.length,2);assert.notEqual(h.requests[0].message_key,h.requests[1].message_key);
  assert(h.requests.every(r=>r.projection_v2===true));assert.equal(h.publisher.journal.pending().length,0);
  assert(h.store.entries().filter(e=>e.value.state==='sent').every(e=>!('request' in e.value)));
});

test('same original id deduplicates after acknowledgement, different body conflicts',async()=>{
  const h=setup();assert.equal(await h.capture('1'),true);assert.equal(await h.capture('1'),true);assert.equal(h.requests.length,1);
  assert.equal(await h.capture('1','changed body'),false);assert.equal(h.requests.length,1);assert.match(h.logs.at(-1),/TIMELINE_MESSAGE_ID_CONFLICT/);
});

test('lost Central ACK survives new publisher and retries only the same timeline operation',async()=>{
  let reachable=false;const h=setup({send:(request,receipt)=>{if(!reachable)throw new Error('LOST_ACK');return receipt(request);}});
  assert.equal(await h.capture('1'),false);assert.equal(h.requests.length,3);assert.equal(h.publisher.journal.pending().length,1);
  reachable=true;const restarted=new DurableTimelineProjection(h.options);await restarted.recover();
  assert.equal(h.requests.length,4);assert.equal(new Set(h.requests.map(r=>r.message_key)).size,1);assert.equal(restarted.journal.pending().length,0);
});

test('failure of the earlier row prevents later rows overtaking it',async()=>{
  let allowed=false;const h=setup({send:(request,receipt)=>{if(!allowed)throw Error('OFFLINE');return receipt(request);}});
  await Promise.all([h.capture('1','first'),h.capture('2','second')]);assert(h.requests.every(r=>r.text==='first'));
  allowed=true;await h.publisher.recover();assert.deepEqual(h.requests.slice(-2).map(r=>r.text),['first','second']);
});

test('wrong or legacy receipt retains the original journal body for recovery',async()=>{
  for(const mutate of [value=>delete value.projectionReceipt,value=>value.projectionReceipt.textSha256='0'.repeat(64),
    value=>value.projectionReceipt.messageKey='foreign',value=>value.projectionReceipt.sourceEndpointId='foreign',
    value=>value.projectionReceipt.entryId='foreign',value=>value.projectionReceipt.role='assistant']){
    const h=setup({send:(request,receipt)=>{const value=receipt(request);mutate(value);return value;}});
    assert.equal(await h.capture('1'),false);assert.equal(h.publisher.journal.pending().length,1);
  }
});

test('credential or Bot rotation does not replay an old journal under a new identity',async()=>{
  const h=setup({send:()=>{throw Error('OFFLINE');}});await h.capture('1');const count=h.requests.length;
  h.router.credential='different-credential';await h.publisher.recover();assert.equal(h.requests.length,count);
  // Stable platform keys do not become new ids merely because a token rotates.
  assert.equal(await h.capture('1'),false);assert.equal(h.requests.length,count);
  h.router.credential='credential-a';h.hostConfig.channels.telegram.botToken='999:another-test-token';await h.publisher.recover();assert.equal(h.requests.length,count);
});

test('same message id on another Bot/account owns a different namespace',async()=>{
  const first=setup(),second=setup({bot:'999',store:first.store});await first.capture('1');await second.capture('1');
  assert.notEqual(first.requests[0].message_key,second.requests[0].message_key);
});

test('unknown source id, group/thread and cross-chat route never enter the journal',async()=>{
  const h=setup();assert.equal(await h.capture(undefined),false);
  assert.equal(await h.capture('1','public',{context:{conversationId:'different'}}),false);
  assert.equal(await h.capture('2','public',{context:{conversationId:'101',threadId:10}}),false);
  assert.equal(h.publisher.journal.pending().length,0);assert.equal(h.requests.length,0);
});

test('actual SDK Telegram target prefix is normalized without admitting another peer',async()=>{
  const h=setup();assert.equal(await h.capture('1','public',{context:{conversationId:'telegram:101',accountId:'default'}}),true);
  assert.equal(h.requests[0].route.to,'101');
  assert.equal(await h.capture('2','public',{context:{conversationId:'telegram:102'}}),false);
  assert.equal(await h.capture('3','public',{context:{conversationId:'telegram:telegram:101'}}),false);
});

test('local persistence failure does not perform network publication',async()=>{
  const store=memoryStore();store.update=()=>{throw Error('DISK_FULL');};const h=setup({store});
  assert.equal(await h.capture('1'),false);assert.equal(h.requests.length,0);assert.match(h.logs[0],/TIMELINE_SYNC_FAILED/);
});

test('transport exception messages never expose credentials or original card links in logs',async()=>{
  const h=setup({send:()=>{throw Error('https://secret.invalid/results/private-token Bearer hidden-secret');}});
  assert.equal(await h.capture('1'),false);assert(h.logs.every(value=>!value.includes('secret')&&!value.includes('private-token')));
});

test('failed receipt persistence leaves the same request recoverable',async()=>{
  const store=memoryStore(),update=store.update;let denyReceipt=true;
  store.update=(key,fn)=>update(key,current=>{const next=fn(current);if(denyReceipt&&next?.state==='sent')throw Error('DISK_FULL');return next;});
  const h=setup({store});assert.equal(await h.capture('1'),false);assert.equal(h.publisher.journal.pending().length,1);
  denyReceipt=false;await new DurableTimelineProjection(h.options).recover();assert.equal(h.publisher.journal.pending().length,0);assert.equal(new Set(h.requests.map(r=>r.message_key)).size,1);
});

test('public Unicode text uses code points and never silently truncates at 50k',async()=>{
  const h=setup();const text='😀'.repeat(200_000);assert.equal(publicTimelineText(text),text);assert.equal(publicTimelineText('a'.repeat(50_001)).length,50_001);
  assert.equal(await h.capture('1',text),true);assert.equal(h.requests[0].text,text);
  const oversized='😀'.repeat(200_001);assert.equal(await h.capture('2',oversized),true);assert.match(h.requests[1].text,/超过同步展示上限/);
  assert.equal(await h.capture('2',oversized+'changed'),false);
});

test('trusted links and secrets never enter the durable namespace',async()=>{
  const h=setup();for(const text of ['password=private','Bearer secret-value','https://x.invalid/results/'+'A'.repeat(43),
    'https://x.invalid/%72esults%2f'+'A'.repeat(43)+'%ZZ','/start private-pairing-code','12345678','| 用户名 | 密码 |\n| a | b |','bad\0text','bad\ud800']){
    assert.equal(publicTimelineText(text),null);assert.equal(await h.capture('1',text),false);
  }
  assert.equal(h.store.entries().length,0);assert.equal(publicTimelineText('进度 50% 完成'),'进度 50% 完成');
});

test('legacy source-key generation also preserves distinct same-text platform events',()=>{
  const h=setup();const publisher=new TimelinePublisher({identityRouter:h.router,now:()=>1000});
  const input={sessionKey:h.sessionKey,role:'user',text:'same',context:{}};
  assert.notEqual(publisher.messageKey({...input,event:{messageId:'1'}}),publisher.messageKey({...input,event:{messageId:'2'}}));
});

test('ordinary publisher sends original long text through the durable path',async()=>{
  const h=setup();const publisher=new TimelinePublisher({identityRouter:h.router,projectionStore:h.store,api:{config:h.hostConfig},sleep:async()=>{}});
  assert.equal(await publisher.capture({sessionKey:h.sessionKey,role:'user',text:'x'.repeat(50_001),event:{messageId:'1'},context:{conversationId:'101',accountId:'default'}}),true);
  assert.equal(h.requests[0].text.length,50_001);
});


test('long bodies use bounded original chunks and only a complete manifest can publish',async()=>{
  const store=memoryStore(),register=store.registerIfAbsent;let broken=true;
  store.registerIfAbsent=(key,value)=>{if(broken&&value.kind==='timeline-body'&&value.index===1)throw Error('DISK_FULL');return register(key,value);};
  const h=setup({store});const text='😀'.repeat(30_000);
  assert.equal(await h.capture('1',text),false);assert.equal(h.requests.length,0);assert.equal(h.publisher.journal.pending().length,0);
  assert(store.entries().some(e=>e.value.state==='assembling'));
  await h.publisher.recover();assert.equal(h.requests.length,0);
  broken=false;assert.equal(await h.capture('1',text),true);assert.equal(h.requests[0].text,text);
  assert(!store.entries().some(e=>e.value.kind==='timeline-body'));
});

test('a missing body chunk cannot become a partial public copy',async()=>{
  const h=setup({send:()=>{throw Error('OFFLINE');}});await h.capture('1','😀'.repeat(30_000));
  const body=h.store.entries().find(e=>e.value.kind==='timeline-body');h.store.delete(body.key);
  await assert.rejects(h.publisher.recover(),/TIMELINE_BODY_MISSING/);assert.equal(h.requests.length,3);
});

test('receipt survives interrupted body cleanup and restart prunes without republishing',async()=>{
  const h=setup();const remove=h.store.delete;h.store.delete=()=>{throw Error('DISK_FAILED');};
  assert.equal(await h.capture('1'),true);assert(h.store.entries().some(e=>e.value.kind==='timeline-body'));
  h.store.delete=remove;await new DurableTimelineProjection(h.options).recover();
  assert.equal(h.requests.length,1);assert(!h.store.entries().some(e=>e.value.kind==='timeline-body'));
});
