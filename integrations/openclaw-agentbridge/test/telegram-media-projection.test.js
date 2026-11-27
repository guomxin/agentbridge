import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,writeFileSync,rmSync,unlinkSync,symlinkSync,chmodSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {TimelineLocalStore} from '../lib/timeline-storage.js';
import {TelegramMediaProjection,readTelegramMedia} from '../lib/telegram-media-projection.js';

const PNG=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6KwAAAABJRU5ErkJggg==','base64');
const sha=body=>createHash('sha256').update(body).digest('hex');
const ENDPOINT='a1111111-1111-4111-8111-111111111111',ENTRY='b1111111-1111-4111-8111-111111111111',ATTACHMENT='C'.repeat(43);
function fixture(t,{append,source,now=Date.parse('2026-11-27T03:00:00Z')}={}){
  const state=mkdtempSync(path.join(os.tmpdir(),'agentbridge-media-test-')),root=path.join(state,'media','inbound');mkdirSync(root,{recursive:true,mode:0o700});
  const store=new TimelineLocalStore(path.join(state,'journal'));
  t.after(()=>{store.close();rmSync(state,{recursive:true,force:true});});
  const requests=[],saved=new Map(),logs=[];
  const binding={key:'telegram:default:101',channel:'telegram',senderId:'101',accountId:'default'},sessionKey='agent:main:telegram:direct:101';
  const receivedAt=new Date(now).toISOString().replace('.000Z','+00:00'),expiresAt=new Date(now+7*86400000).toISOString().replace('.000Z','+00:00');
  const makeReceipt=req=>({recorded:true,mediaReceipt:{sourceEndpointId:ENDPOINT,sourceMessageId:req.source_message_id,ordinal:req.ordinal,
    fileName:req.file_name,mimeType:req.mime_type,byteSize:req.byte_size,sha256:req.sha256,receivedAt:req.received_at,expiresAt,
    attachmentId:req.failure?null:ATTACHMENT,entryId:ENTRY,state:req.failure?'unavailable':'stored',failure:req.failure}});
  const send=req=>{
    const key=`${req.source_message_id}:${req.ordinal}`,existing=saved.get(key);
    if(existing){assert.deepEqual(existing.request,{...req,content_base64:undefined});return existing.receipt;}
    if(!req.failure && !req.content_base64)return {recorded:false};
    if(req.content_base64)assert.equal(sha(Buffer.from(req.content_base64,'base64')),req.sha256);
    const receipt=makeReceipt(req);saved.set(key,{request:{...req,content_base64:undefined},receipt});return receipt;
  };
  const client={async callTool(name,request){requests.push({name,request:structuredClone(request)});
    if(name==='agentbridge_host_telegram_media_source')return source?source(request):{recorded:true,sourceEndpointId:ENDPOINT,pairingRef:'a'.repeat(64),receivedAt:request.received_at};
    assert.equal(name,'agentbridge_host_telegram_media_append');return append?append(request,send):send(request);
  }};
  const router={endpoint:{url:'https://central.invalid/mcp'},credential:'original-credential',catalogCacheKeyForBinding(){return this.credential;},
    configuredIdentities(){return [{binding,client}];},resolvePinnedSession(){return {bound:true,binding,client};},endpointKeyForSession(){return binding.key;}};
  const api={config:{channels:{telegram:{botToken:'123:synthetic-only'}}},runtime:{state:{resolveStateDir:()=>state}}};
  const clock={now},options={api,identityRouter:router,store,logger:{warn:value=>logs.push(value)},now:()=>clock.now,sleep:async()=>{}};
  const publisher=new TelegramMediaProjection(options);
  const file=(name='原图---a1111111-1111-4111-8111-111111111111.png',body=PNG,mimeType='image/png')=>{
    const localPath=path.join(root,name);writeFileSync(localPath,body,{mode:0o600});return {localPath,mimeType};};
  const capture=(id,files=[file()],extra={})=>publisher.capture({sessionKey,event:{messageId:id,senderId:'101',from:'telegram:101',timestamp:now,
    content:'caption 独立文本',metadata:{mediaPaths:files.map(value=>value.localPath),mediaTypes:files.map(value=>value.mimeType)},...extra.event},
    context:{channelId:'telegram',accountId:'default',conversationId:'telegram:101',...extra.context}});
  return {publisher,options,clock,store,state,root,requests,saved,logs,router,api,capture,file,receivedAt,expiresAt,makeReceipt};
}

test('pure media freezes original source before any append and publishes only per-item public facts',async t=>{
  const h=fixture(t);assert.equal(await h.capture(1),true);
  assert.equal(h.requests[0].name,'agentbridge_host_telegram_media_source');
  assert.equal(h.saved.size,1);const stored=[...h.saved.values()][0];
  assert.equal(stored.request.file_name,'原图.png');assert.equal(stored.request.sha256,sha(PNG));assert.equal(stored.request.ordinal,0);
  assert.equal(stored.request.received_at,h.receivedAt);assert(!JSON.stringify(h.store.entries()).includes('caption 独立文本'));
  assert(!h.store.entries().some(row=>row.value.kind==='telegram-media-body'));
});

test('same bytes on distinct native messages remain distinct; same-id metadata conflict does not replace a receipt',async t=>{
  const h=fixture(t),file=h.file();assert.equal(await h.capture(1,[file]),true);assert.equal(await h.capture(2,[file]),true);
  assert.equal(h.saved.size,2);const count=h.requests.length;assert.equal(await h.capture(1,[file]),true);assert.equal(h.requests.length,count);
  assert.equal(await h.capture(1,[h.file('different.png')]),false);assert.equal(h.saved.size,2);assert.match(h.logs.at(-1),/MEDIA_SOURCE_CONFLICT/);
});

test('same caption and distinct attachments preserve original ordinal and byte identity',async t=>{
  const h=fixture(t);await h.capture(1,[h.file(),h.file('第二份.txt',Buffer.from('附件二 😀'),'text/plain')]);
  const values=[...h.saved.values()];assert.deepEqual(values.map(value=>value.request.ordinal),[0,1]);
  assert.notEqual(values[0].request.sha256,values[1].request.sha256);
});

test('lost append response survives restart and only probes the original receipt without rereading changed files',async t=>{
  let lose=true;const h=fixture(t,{append:(request,send)=>{const value=send(request);if(lose&&value.recorded)throw Error('ACK_LOST');return value;}});
  const file=h.file();assert.equal(await h.capture(1,[file]),false);assert.equal(h.saved.size,1);
  unlinkSync(file.localPath);lose=false;await new TelegramMediaProjection(h.options).recover();
  assert.equal(h.publisher.pending().length,0);assert.equal(h.requests.filter(value=>value.request.content_base64).length,1);
});

test('later items do not overtake an earlier unknown central append and resume independently',async t=>{
  let fail=true;const h=fixture(t,{append:(request,send)=>{if(fail&&request.ordinal===1)throw Error('OFFLINE');return send(request);}});
  await h.capture(1,[h.file(),h.file('two.png'),h.file('three.png')]);assert.equal(h.saved.size,1);
  assert(!h.requests.some(value=>value.request.ordinal===2));fail=false;await new TelegramMediaProjection(h.options).recover();
  assert.equal(h.saved.size,3);assert.equal(h.requests.filter(value=>value.request.ordinal===0&&value.request.content_base64).length,1);
});

test('unavailable source proof never reads local bytes or saves a media item',async t=>{
  const h=fixture(t,{source:()=>{throw Error('REVOKED');}});await h.capture(1);
  assert.equal(h.saved.size,0);assert(!h.store.entries().some(row=>row.value.kind==='telegram-media-body'));
});

test('credential or Bot rotation does not adopt old media; cross-chat, group, malformed id and missing native date are refused',async t=>{
  const h=fixture(t,{append:()=>{throw Error('OFFLINE');}});await h.capture(1);const count=h.requests.length;
  h.router.credential='new';await h.publisher.recover();assert.equal(h.requests.length,count);
  h.router.credential='original-credential';h.api.config.channels.telegram.botToken='999:synthetic-only';await h.publisher.recover();assert.equal(h.requests.length,count);
  for(const extra of [{context:{conversationId:'telegram:102'}},{context:{threadId:10}},{event:{senderId:'102'}},
    {event:{timestamp:undefined}},{event:{messageId:'1e2'}}])assert.equal(await h.capture(2,[h.file()],extra),false);
  assert.equal(h.requests.length,count);
});

test('strict original receipt fields are required before dropping local bytes',async t=>{
  const h=fixture(t,{append:(request,send)=>{const value=send(request);if(value.mediaReceipt)value.mediaReceipt.sourceEndpointId='foreign';return value;}});
  assert.equal(await h.capture(1),false);assert.equal(h.publisher.pending().length,1);assert(h.store.entries().some(row=>row.value.kind==='telegram-media-body'));
});

test('original seven-day expiry is explicit and an already recorded success can settle after expiry',async t=>{
  const h=fixture(t);h.clock.now+=8*86400000;await h.capture(1);assert.equal([...h.saved.values()][0].request.failure,'expired');
  let lose=true;const other=fixture(t,{append:(request,send)=>{const value=send(request);if(lose&&value.recorded)throw Error('LOST');return value;}});
  await other.capture(1);other.clock.now+=8*86400000;lose=false;await other.publisher.recover();
  assert.equal([...other.saved.values()][0].receipt.mediaReceipt.state,'stored');assert.equal(other.publisher.pending().length,0);
});

test('an unrecorded expired original becomes one explicit disposition even when its response is lost',async t=>{
  let phase='offline';const h=fixture(t,{append:(request,send)=>{
    if(phase==='offline')throw Error('OFFLINE');const value=send(request);
    if(phase==='lost'&&value.recorded)throw Error('LOST');return value;}});
  await h.capture(1);h.clock.now+=8*86400000;phase='lost';await h.publisher.recover();
  assert.equal([...h.saved.values()][0].request.failure,'expired');assert(!h.store.entries().some(row=>row.value.kind==='telegram-media-body'));
  phase='ok';await new TelegramMediaProjection(h.options).recover();assert.equal(h.publisher.pending().length,0);assert.equal(h.saved.size,1);
});

test('per-file and per-message budgets produce explicit failures without URL fallbacks',async t=>{
  const h=fixture(t);await h.capture(1,[h.file('big.bin',Buffer.alloc(6*1024*1024+1),'application/pdf')]);
  assert.equal([...h.saved.values()][0].request.failure,'too_large');
  await h.capture(2,Array.from({length:5},(_,i)=>h.file(`${i}.png`)));assert.equal(h.saved.get('2:0').request.failure,'too_large');
  const body=Buffer.alloc(6*1024*1024,32);body.write('%PDF-1.7');
  await h.capture(3,[h.file('a.pdf',body,'application/pdf'),h.file('b.pdf',body,'application/pdf'),h.file('c.png')]);
  assert.equal(h.saved.get('3:0').request.byte_size,6*1024*1024);assert.equal(h.saved.get('3:1').request.byte_size,6*1024*1024);assert.equal(h.saved.get('3:2').request.failure,'too_large');
});

test('only bounded local SDK media files are read; symlinks, outside paths and incorrect signatures are unavailable',t=>{
  const h=fixture(t),file=h.file();assert.equal(readTelegramMedia(file,h.root).sha256,sha(PNG));
  const outside=path.join(h.state,'outside.png');writeFileSync(outside,PNG);assert.equal(readTelegramMedia({...file,localPath:outside},h.root).failure,'unreadable');
  const link=path.join(h.root,'link.png');symlinkSync(outside,link);assert.equal(readTelegramMedia({...file,localPath:link},h.root).failure,'unreadable');
  assert.equal(readTelegramMedia({...file,localPath:'https://x.invalid/file'},h.root).failure,'unreadable');
  assert.equal(readTelegramMedia(h.file('invalid.png',Buffer.from('not image')),h.root).failure,'unsupported');
  assert.equal(readTelegramMedia(h.file('invalid.txt',Buffer.from([0xff]),'text/plain'),h.root).failure,'unreadable');
  assert.equal(readTelegramMedia(h.file('unknown.bin',Buffer.from('content'),'application/octet-stream'),h.root).failure,'unsupported');
});

test('partial local persistence cannot append a partial resource and recovery keeps original bounded chunks',async t=>{
  const h=fixture(t),register=h.store.registerIfAbsent.bind(h.store);let broken=true;
  h.store.registerIfAbsent=(key,value)=>{if(broken&&value.kind==='telegram-media-body'&&key.endsWith(':1'))throw Error('DISK_FULL');return register(key,value);};
  const file=h.file('large.txt',Buffer.from('原文'.repeat(20_000)),'text/plain');await h.capture(1,[file]);assert.equal(h.saved.size,0);
  broken=false;await new TelegramMediaProjection(h.options).recover();assert.equal(h.saved.size,1);assert.equal(h.saved.get('1:0').request.sha256,sha(Buffer.from('原文'.repeat(20_000))));
});

test('remote staging and URL-only metadata create explicit unreadable dispositions without fetching',async t=>{
  const h=fixture(t);assert.equal(await h.capture(1,[],{event:{metadata:{mediaPath:'/elsewhere',mediaType:'image/png',mediaRemoteHost:'remote.invalid'}}}),true);
  assert.equal(h.saved.get('1:0').request.failure,'unreadable');
  assert.equal(await h.capture(2,[],{event:{metadata:{mediaUrl:'https://untrusted.invalid/original',mediaType:'image/png'}}}),true);
  assert.equal(h.saved.get('2:0').request.failure,'unreadable');
});

test('official SDK 0644 files remain readable only within private 0700 media directories',t=>{
  const h=fixture(t),file=h.file();chmodSync(file.localPath,0o644);assert.equal(readTelegramMedia(file,h.root).sha256,sha(PNG));
  chmodSync(file.localPath,0o666);assert.equal(readTelegramMedia(file,h.root).failure,'unreadable');
  chmodSync(file.localPath,0o644);chmodSync(h.root,0o755);assert.equal(readTelegramMedia(file,h.root).failure,'unreadable');
});
