import {createHash} from 'node:crypto';
import {unlink} from 'node:fs/promises';
import path from 'node:path';
import {openProjectionLocalStore} from './timeline-storage.js';
import {checkedReceipt,sendFailure,singleAttemptConfig} from './workspace-query-delivery.js';
import {hostContextMeta} from './host-contract.js';

const hash=value=>createHash('sha256').update(value).digest('hex');
const active=new Set();
const code=outcome=>outcome==='sent'?null:outcome==='delivery_unknown'?'TELEGRAM_DELIVERY_UNKNOWN':
  outcome==='retry_after'?'TELEGRAM_RATE_LIMITED':outcome==='rejected'?'TELEGRAM_REJECTED':'ATTACHMENT_DELIVERY_UNAVAILABLE';

// Existing task-artifact authorization and reports remain the authority. This
// ledger only fences that original native upload; it never creates another task,
// issues a public link, or treats a failed report ACK as permission to resend.
export class TelegramArtifactDelivery {
  constructor({api,store=undefined,now=Date.now,sleep,identityScope=()=>null,routeFor,clientFor}){
    this.api=api;this.store=store===undefined?openProjectionLocalStore(api,'telegram-artifact-v2'):store;
    this.now=now;this.sleep=sleep;this.identityScope=identityScope;this.routeFor=routeFor;this.clientFor=clientFor;
  }
  scope(sessionKey){
    const route=this.routeFor(sessionKey),peer=/^agent:[^:]+:telegram:(?:direct|dm):([1-9][0-9]*)$/i.exec(sessionKey||'')?.[1];
    const account=route?.accountId||'default',telegram=this.api.config?.channels?.telegram;
    const token=(telegram?.accounts?.[account]||telegram)?.botToken??telegram?.botToken;
    if(!peer || route?.channel!=='telegram' || String(route.to).replace(/^telegram:/,'')!==peer || route.threadId ||
       typeof token!=='string' || !/^[1-9][0-9]*:/.test(token))return null;
    const identity=this.identityScope(sessionKey);if(identity===null)return null;
    const routeKey=hash(JSON.stringify([sessionKey,account,token.split(':')[0]]));
    return {routeKey,namespace:hash(JSON.stringify([routeKey,identity])),route:{to:peer,accountId:account}};
  }
  current(record){const value=this.scope(record.sessionKey);return value?.routeKey===record.routeKey&&value.namespace===record.namespace;}
  save(key,record){this.store.update(key,current=>{
    if(current?.kind==='artifact-attempt' && (current.sourceHash!==record.sourceHash || current.namespace!==record.namespace ||
       current.attemptCount>record.attemptCount || current.state==='sending'&&record.state==='prepared' ||
       current.outcome==='sent'&&record.outcome!=='sent'))throw Error('ARTIFACT_DURABLE_STATE_CONFLICT');
    return record;
  });}
  result(record){return {state:record.outcome==='sent'?'attachment_sent':'failed',attemptCount:record.attemptCount,errorCode:record.rateLimited?'TELEGRAM_RATE_LIMITED':code(record.outcome),
    ...(record.receipt?{telegramReceipt:record.receipt}:{}),deliveryUnknown:record.outcome==='delivery_unknown'};}
  async deliver({sessionKey,file,deliveryRef,materialize,withDirectDelivery,beforeSend}){
    const scope=this.scope(sessionKey);
    if(!this.store||!scope||!deliveryRef)return {state:'failed',attemptCount:0,errorCode:'ARTIFACT_DURABLE_IDENTITY_UNAVAILABLE'};
    const fileId=file.artifactId||file.downloadId;
    if(!fileId)return {state:'failed',attemptCount:0,errorCode:'ARTIFACT_ORIGINAL_ID_UNAVAILABLE'};
    const key=`artifact:${hash(JSON.stringify([scope.routeKey,deliveryRef,fileId]))}`;
    const sourceHash=hash(JSON.stringify([fileId,file.downloadId,file.filename,file.contentType,file.expiresAt,
      file.protectedCsv?hash(Buffer.from(file.contentBase64,'base64')):hash(file.mediaUrl||'')]));
    if(active.has(key))return {state:'failed',attemptCount:0,errorCode:'ARTIFACT_DELIVERY_IN_FLIGHT'};
    active.add(key);let material,record,nativeEntered=false;
    try{
      record=this.store.lookup(key);
      if(record && (record.sourceHash!==sourceHash||record.namespace!==scope.namespace))return {state:'failed',attemptCount:0,errorCode:'ARTIFACT_SOURCE_CONFLICT'};
      if(record?.outcome && record.outcome!=='retry_after')return this.result(record);
      if(record?.state==='sending'){
        // An older process may still be finishing its upload. No second send is
        // allowed; preserve a durable unknown if no genuine receipt was saved.
        record.outcome='delivery_unknown';record.state='completed';this.save(key,record);return this.result(record);
      }
      if(!record){record={kind:'artifact-attempt',sessionKey,...scope,sourceHash,attemptCount:0,state:'prepared'};
        if(!this.store.registerIfAbsent(key,record))return this.result({...record,outcome:'delivery_unknown'});
      }
      const adapter=await this.api.runtime.channel.outbound.loadAdapter('telegram');
      if(typeof adapter?.sendPayload!=='function')return this.result({...record,outcome:'unavailable'});
      if(!file.protectedCsv && !(Date.parse(file.expiresAt)>this.now()))return this.result({...record,outcome:'unavailable'});
      material=await materialize();
      const bodyHash=hash(material.body);
      if(record.bodyHash && record.bodyHash!==bodyHash)throw Error('ARTIFACT_ORIGINAL_BYTES_CHANGED');
      record.bodyHash=bodyHash;record.byteSize=material.body.length;this.save(key,record);
      while(record.attemptCount<3){
        if(record.outcome==='retry_after'){
          const wait=record.dueAt-this.now();
          if(wait>300_000){record.outcome='rejected';record.rateLimited=true;this.save(key,record);return this.result(record);}
          if(wait>0)await this.sleep(wait);
        }
        if(!this.current(record) || beforeSend && !await beforeSend() || !this.current(record) || !file.protectedCsv&&!(Date.parse(file.expiresAt)>this.now())){
          record.outcome='unavailable';record.state='completed';this.save(key,record);return this.result(record);
        }
        const previousAttempt=record.attemptCount;let began=false;
        this.store.update(key,current=>{
          if(!current || current.sourceHash!==sourceHash || current.namespace!==scope.namespace ||
             current.attemptCount!==previousAttempt || current.state==='sending' ||
             current.outcome && current.outcome!=='retry_after')return undefined;
          record={...current,attemptCount:previousAttempt+1,state:'sending'};delete record.outcome;began=true;return record;
        });
        if(!began){const current=this.store.lookup(key);return this.result(current?.outcome==='sent'?current:{...record,outcome:'delivery_unknown'});}
        const capture=async result=>{
          const receipt=checkedReceipt(result,scope.route);
          if(!receipt || record.receipt&&record.receipt!==receipt)throw Error('ARTIFACT_PLATFORM_RECEIPT_INVALID');
          record.receipt=receipt;record.outcome='sent';record.state='completed';this.save(key,record);
        };
        try{
          const payload={text:`AgentBridge 文件已准备完成：${file.filename}`,mediaUrl:material.path};
          const result=await withDirectDelivery(()=>{nativeEntered=true;return adapter.sendPayload({cfg:singleAttemptConfig(this.api.config),...scope.route,
            text:payload.text,payload,forceDocument:true,mediaLocalRoots:[path.dirname(path.resolve(material.path))],
            mediaReadFile:async requested=>{if(path.resolve(requested)!==path.resolve(material.path))throw Error('ARTIFACT_PATH_MISMATCH');return Buffer.from(material.body);},
            onDeliveryResult:capture});});
          if(!record.receipt)await capture(result);
        }catch(error){if(!record.receipt)Object.assign(record,sendFailure(error));}
        record.state='completed';
        if(record.outcome==='retry_after')record.dueAt=this.now()+record.retryAfterSeconds*1000;
        this.save(key,record);
        if(record.outcome!=='retry_after')return this.result(record);
      }
      record.outcome='rejected';record.rateLimited=true;this.save(key,record);return this.result(record);
    }catch{
      if(nativeEntered||record?.state==='sending'){
        try{const saved=this.store.lookup(key);if(saved?.outcome==='sent')return this.result(saved);}catch{}
        return this.result({...record,outcome:'delivery_unknown'});
      }
      if(record){record.outcome='unavailable';record.state='completed';try{this.save(key,record);}catch{}}
      return {state:'failed',attemptCount:record?.attemptCount||0,errorCode:'ARTIFACT_PREPARATION_FAILED'};
    }
    finally{if(material?.path&&path.isAbsolute(material.path))await unlink(material.path).catch(()=>{});active.delete(key);}
  }
  async report({sessionKey,client,args}){
    const scope=this.scope(sessionKey);if(!scope||!this.store)return false;
    const key=`report:${hash(JSON.stringify([scope.routeKey,args.task_id,args.delivery_ref]))}`;
    const payloadHash=hash(JSON.stringify(args));let record=this.store.lookup(key);
    if(record && (record.namespace!==scope.namespace||record.payloadHash!==payloadHash))return false;
    if(record?.state==='sent')return true;
    if(!record){record={kind:'artifact-report',sessionKey,...scope,payloadHash,args,state:'pending'};this.store.registerIfAbsent(key,record);}
    return this.sendReport(key,record,client);
  }
  async sendReport(key,record,client){
    if(!this.current(record)||!client)return false;
    try{
      const response=await client.callTool('agentbridge_host_artifact_delivery_report',{...record.args,receipt_v2:true},{signal:AbortSignal.timeout(5000),meta:hostContextMeta()});
      if(response?.status!=='succeeded'||!response.eventId||response.task?.taskId!==record.args.task_id||
         response.delivery?.deliveryRef!==record.args.delivery_ref||response.delivery?.channel!==record.args.channel||
         !record.args.files.every(expected=>response.delivery.files?.some(actual=>actual.artifactId===expected.artifact_id&&
           actual.state===expected.state&&actual.attemptCount===expected.attempt_count&&actual.errorCode===expected.error_code)))return false;
      record.state='sent';record.receipt={eventId:response.eventId};this.save(key,record);return true;
    }catch{return false;}
  }
  async recoverReports(){
    if(!this.store)return;
    for(const {key,value} of this.store.entries())if(value.kind==='artifact-report'&&value.state==='pending')await this.sendReport(key,value,this.clientFor(value.sessionKey));
  }
}
