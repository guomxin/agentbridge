import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,rmSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {TimelineLocalStore} from '../lib/timeline-storage.js';
import {TelegramArtifactDelivery} from '../lib/telegram-artifact-delivery.js';

function fixture(t,{send,report}={}){
  const dir=mkdtempSync(path.join(os.tmpdir(),'artifact-journal-')),store=new TimelineLocalStore(path.join(dir,'store'));
  t.after(()=>{store.close();rmSync(dir,{recursive:true,force:true});});
  let now=Date.now(),identity='original',route={channel:'telegram',to:'101',accountId:'default'},sends=0,reads=0;
  const contexts=[],reports=[],sessionKey='agent:main:telegram:direct:101',body=Buffer.from('%PDF-1.7 synthetic');
  const receipt={channel:'telegram',messageId:'501',chatId:'101'};
  const client={async callTool(name,args){assert.equal(name,'agentbridge_host_artifact_delivery_report');reports.push(structuredClone(args));
    const response={status:'succeeded',eventId:'event-1',task:{taskId:args.task_id},delivery:{deliveryRef:args.delivery_ref,channel:args.channel,
      files:args.files.map(file=>({artifactId:file.artifact_id,state:file.state,attemptCount:file.attempt_count,errorCode:file.error_code}))}};
    return report?report(response):response;}};
  const api={config:{channels:{telegram:{botToken:'123:synthetic-only'}}},runtime:{channel:{outbound:{async loadAdapter(){return {async sendPayload(ctx){
    sends++;contexts.push(ctx);if(send)return send(ctx,sends,receipt);await ctx.onDeliveryResult(receipt);return receipt;}};}}}}};
  const options={api,store,now:()=>now,sleep:async ms=>{now+=ms;},identityScope:()=>identity,routeFor:()=>route,clientFor:()=>client};
  const delivery=new TelegramArtifactDelivery(options);
  const file={artifactId:'artifact-original',downloadId:'download-original',filename:'原文件.pdf',contentType:'application/pdf',
    mediaUrl:'https://central.invalid/download/synthetic-only/file',expiresAt:new Date(now+600000).toISOString()};
  const args={sessionKey,file,deliveryRef:'original-ref',materialize:async()=>{reads++;const local=path.join(dir,'file.pdf');writeFileSync(local,body,{mode:0o600});return {path:local,body};},withDirectDelivery:send=>send()};
  return {delivery,options,store,api,args,contexts,reports,client,body,get sends(){return sends;},get reads(){return reads;},
    rotate(){identity='new';},move(){route={...route,to:'102'};}};
}

test('original authorized artifact records real receipt, exact upload bytes and one per-call native attempt',async t=>{
  const h=fixture(t);const result=await h.delivery.deliver(h.args);assert.equal(result.state,'attachment_sent');assert.equal(result.telegramReceipt,501);
  const ctx=h.contexts[0];assert.equal(ctx.forceDocument,true);assert.equal(ctx.cfg.channels.telegram.retry.attempts,1);
  assert.equal(ctx.cfg.channels.telegram.network.dnsResultOrder,'verbatim');assert.deepEqual(await ctx.mediaReadFile(ctx.payload.mediaUrl),h.body);
  assert.equal((await new TelegramArtifactDelivery(h.options).deliver(h.args)).state,'attachment_sent');assert.equal(h.sends,1);assert.equal(h.reads,1);
});

test('unknown transport outcome and a missing or foreign receipt never retry or become URL fallback',async t=>{
  for(const send of [()=>{throw Error('socket reset');},()=>({}),()=>({messageId:2,chatId:'102'})]){
    const h=fixture(t,{send});const result=await h.delivery.deliver(h.args);assert.equal(result.errorCode,'TELEGRAM_DELIVERY_UNKNOWN');
    assert.equal((await new TelegramArtifactDelivery(h.options).deliver(h.args)).deliveryUnknown,true);assert.equal(h.sends,1);
    assert(h.contexts.every(ctx=>ctx.payload.mediaUrl&&!ctx.payload.text.includes('/download/')));
  }
});

test('only a structured 429 permits another separately persisted local attempt',async t=>{
  const h=fixture(t,{send:async(ctx,count,receipt)=>{if(count===1)throw {error_code:429,parameters:{retry_after:1}};await ctx.onDeliveryResult(receipt);return receipt;}});
  assert.equal((await h.delivery.deliver(h.args)).state,'attachment_sent');assert.equal(h.sends,2);
  const denied=fixture(t,{send:()=>{throw {error_code:403};}});assert.equal((await denied.delivery.deliver(denied.args)).errorCode,'TELEGRAM_REJECTED');assert.equal(denied.sends,1);
  const exhausted=fixture(t,{send:()=>{throw {error_code:429,parameters:{retry_after:1}};}});assert.equal((await exhausted.delivery.deliver(exhausted.args)).errorCode,'TELEGRAM_RATE_LIMITED');
  await new TelegramArtifactDelivery(exhausted.options).deliver(exhausted.args);assert.equal(exhausted.sends,3);
});

test('preparation failure and permission or route withdrawal before send produce no platform request',async t=>{
  const h=fixture(t);assert.equal((await h.delivery.deliver({...h.args,materialize:async()=>{throw Error('read failed');}})).errorCode,'ARTIFACT_PREPARATION_FAILED');assert.equal(h.sends,0);
  const revoked=fixture(t);assert.equal((await revoked.delivery.deliver({...revoked.args,beforeSend:async()=>false})).state,'failed');assert.equal(revoked.sends,0);
  const moved=fixture(t);assert.equal((await moved.delivery.deliver({...moved.args,beforeSend:async()=>{moved.move();return false;}})).state,'failed');assert.equal(moved.sends,0);
});

test('a lost artifact report ACK retries only the exact report after restart',async t=>{
  let lose=true;const h=fixture(t,{report:result=>{if(lose)throw Error('ACK_LOST');return result;}});
  const result=await h.delivery.deliver(h.args),args={agent_host:'openclaw',task_id:'task-original',delivery_ref:'original-ref',channel:'telegram',
    files:[{artifact_id:h.args.file.artifactId,state:result.state,attempt_count:result.attemptCount,error_code:result.errorCode}]};
  assert.equal(await h.delivery.report({sessionKey:h.args.sessionKey,client:h.client,args}),false);
  lose=false;await new TelegramArtifactDelivery(h.options).recoverReports();assert.equal(h.sends,1);assert.equal(h.reports.length,2);assert.deepEqual(h.reports[0],h.reports[1]);
  assert(h.store.entries().some(row=>row.value.kind==='artifact-report'&&row.value.state==='sent'));
});

test('changed artifact identity, new credential and untrusted central receipt do not replace original evidence',async t=>{
  const h=fixture(t);await h.delivery.deliver(h.args);assert.equal((await h.delivery.deliver({...h.args,file:{...h.args.file,filename:'changed.pdf'}})).errorCode,'ARTIFACT_SOURCE_CONFLICT');
  h.rotate();assert.equal((await h.delivery.deliver(h.args)).errorCode,'ARTIFACT_SOURCE_CONFLICT');assert.equal(h.sends,1);
  const other=fixture(t,{report:result=>({...result,task:{taskId:'foreign'}})});assert.equal(await other.delivery.report({sessionKey:other.args.sessionKey,client:other.client,args:{task_id:'original',delivery_ref:'ref',channel:'telegram',files:[]}}),false);
});

test('successful callback receipt outranks a later SDK exception; explicit reissue owns a new ref',async t=>{
  const h=fixture(t,{send:async(ctx,count,receipt)=>{await ctx.onDeliveryResult({...receipt,messageId:String(500+count)});throw Error('bookkeeping failed');}});
  assert.equal((await h.delivery.deliver(h.args)).state,'attachment_sent');assert.equal((await h.delivery.deliver({...h.args,deliveryRef:'explicit-reissue'})).state,'attachment_sent');assert.equal(h.sends,2);
});

test('the atomic durable send boundary rejects another process claiming the same prepared attempt',async t=>{
  const h=fixture(t),update=h.store.update.bind(h.store);let race=true;
  h.store.update=(key,fn)=>update(key,current=>{
    const next=fn(current);
    if(race&&next?.kind==='artifact-attempt'&&next.bodyHash&&next.state==='prepared'){
      race=false;return {...next,state:'sending',attemptCount:1};
    }
    return next;
  });
  const result=await h.delivery.deliver(h.args);assert.equal(result.deliveryUnknown,true);assert.equal(h.sends,0);
});

test('artifact upload escapes HTML caption with HTML parseMode and recoverReports restores route after restart',async t=>{
  let lose=true;
  const h=fixture(t,{report:result=>{if(lose)throw Error('ACK_LOST');return result;}});
  const file={...h.args.file,filename:'oa_report_<2026>&1.pdf'};
  const result=await h.delivery.deliver({...h.args,file});
  assert.equal(result.state,'attachment_sent');
  assert.deepEqual(h.contexts[0].formatting,{parseMode:'HTML'});
  assert.equal(h.contexts[0].text,'AgentBridge 文件已准备完成：oa_report_&lt;2026&gt;&amp;1.pdf');
  const args={agent_host:'openclaw',task_id:'task-original',delivery_ref:'original-ref',channel:'telegram',
    files:[{artifact_id:file.artifactId,state:result.state,attempt_count:result.attemptCount,error_code:result.errorCode}]};
  assert.equal(await h.delivery.report({sessionKey:h.args.sessionKey,client:h.client,args}),false);
  lose=false;
  const routes=new Map();
  let restoredBinding=false;
  const restarted=new TelegramArtifactDelivery({
    ...h.options,
    routeFor:sessionKey=>routes.get(sessionKey)||null,
    clientFor:()=>null,
  });
  await restarted.recoverReports({
    binding:{key:'telegram:default:101',channel:'telegram',senderId:'101',accountId:'default'},
    client:h.client,
    endpoint:{state:'active',clientType:'telegram',agentHost:'openclaw',externalSubject:'101',accountId:'default',
      conversationRef:h.args.sessionKey,route:{channel:'telegram',to:'101',accountId:'default'}},
    identityRouter:{restoreSessionBinding(){restoredBinding=true;return true;}},
    restoreRoute:(sessionKey,route)=>routes.set(sessionKey,route),
  });
  assert.equal(restoredBinding,true);
  assert.equal(h.reports.length,2);
  assert(h.store.entries().some(row=>row.value.kind==='artifact-report'&&row.value.state==='sent'));
});
