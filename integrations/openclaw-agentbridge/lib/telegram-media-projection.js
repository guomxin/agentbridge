import {createHash} from 'node:crypto';
import {constants,openSync,closeSync,fstatSync,readSync,lstatSync,realpathSync} from 'node:fs';
import path from 'node:path';
import {hostContextMeta} from './host-contract.js';
import {openProjectionLocalStore} from './timeline-storage.js';
import {DurableTimelineProjection} from './timeline-projection.js';
import {MAX_TELEGRAM_MEDIA_BYTES} from './telegram-media-delivery.js';

const TTL=7*86400_000, MAX_TOTAL=12*1024*1024;
const hash=value=>createHash('sha256').update(value).digest('hex');
const TYPES=new Set(['image/png','image/jpeg','image/webp','application/pdf','text/plain','text/csv','text/html',
  'text/yaml','application/yaml','text/markdown','text/tab-separated-values','application/json','text/xml','application/xml']);
const utc=value=>new Date(Math.floor(value/1000)*1000).toISOString().replace('.000Z','+00:00');
const UUID=/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const bodyKeys=item=>Array.from({length:item.bodyCount||0},(_,index)=>`${item.bodyPrefix}:${index}`);

// The only read source is an official SDK local media path below this host's
// inbound directory. URLs, remote staging paths and caption text are not inputs.
export function readTelegramMedia(file,root) {
  if(!file || !TYPES.has(file.mimeType))return {failure:'unsupported'};
  try {
    if(typeof file.localPath!=='string' || !path.isAbsolute(file.localPath) ||
       path.dirname(path.resolve(file.localPath))!==path.resolve(root))return {failure:'unreadable'};
    const rootStat=lstatSync(root),parent=lstatSync(path.dirname(root));
    if(!rootStat.isDirectory() || rootStat.isSymbolicLink() || !parent.isDirectory() || parent.isSymbolicLink() ||
       rootStat.mode&0o077 || parent.mode&0o077 || typeof process.getuid==='function' && (rootStat.uid!==process.getuid()||parent.uid!==process.getuid()) ||
       realpathSync(root)!==path.join(realpathSync(path.dirname(path.dirname(root))),'media','inbound'))return {failure:'unreadable'};
    const fd=openSync(file.localPath,constants.O_RDONLY|constants.O_NOFOLLOW);
    try {
      const before=fstatSync(fd);
      // Official SDK files are 0644 inside the private 0700 media directory.
      // Reject foreign writes, links and owners; do not invent a 0600 SDK rule.
      if(!before.isFile() || before.nlink!==1 || before.mode&0o022 || typeof process.getuid==='function' && before.uid!==process.getuid())return {failure:'unreadable'};
      if(before.size>MAX_TELEGRAM_MEDIA_BYTES)return {failure:'too_large'};
      if(before.size<1)return {failure:'unreadable'};
      const body=Buffer.alloc(before.size);let offset=0;
      while(offset<body.length){const count=readSync(fd,body,offset,body.length-offset,null);if(!count)break;offset+=count;}
      const after=fstatSync(fd);
      if(offset!==body.length || ['size','mtimeMs','ctimeMs','ino','dev'].some(key=>before[key]!==after[key]))return {failure:'unreadable'};
      let valid;
      switch(file.mimeType){
        case 'image/png':valid=body.subarray(0,8).equals(Buffer.from('89504e470d0a1a0a','hex'));break;
        case 'image/jpeg':valid=body[0]===255 && body[1]===216 && body[2]===255;break;
        case 'image/webp':valid=body.subarray(0,4).toString()==='RIFF' && body.subarray(8,12).toString()==='WEBP';break;
        case 'application/pdf':valid=body.subarray(0,5).toString()==='%PDF-';break;
        default:{const text=new TextDecoder('utf-8',{fatal:true}).decode(body);valid=Boolean(text.trim()) && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u.test(text);}
      }
      if(!valid)return {failure:'unsupported'};
      // OpenClaw sanitizes the original basename and appends ---UUID. Preserve
      // that safe displayed name, without claiming the unsanitized filename.
      const fileName=path.basename(file.localPath).replace(/---[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}(?=\.[^.]+$|$)/i,'');
      if(!fileName || [...fileName].length>256 || /[\u0000-\u001f\u007f/\\]/u.test(fileName))return {failure:'unreadable'};
      return {body,fileName,mimeType:file.mimeType,byteSize:body.length,sha256:hash(body),failure:null};
    } finally {closeSync(fd);}
  } catch {return {failure:'unreadable'};}
}

export class TelegramMediaProjection {
  constructor({api,identityRouter,store=undefined,logger,now=Date.now,sleep=delay}) {
    this.store=store===undefined?openProjectionLocalStore(api,'telegram-media-v2'):store;
    this.router=identityRouter;this.api=api;this.logger=logger;this.now=now;this.sleep=sleep;this.queues=new Map();
    this.root=typeof api?.runtime?.state?.resolveStateDir==='function'?path.join(api.runtime.state.resolveStateDir(),'media','inbound'):null;
    // Reuse the proven credential/Bot/endpoint namespace and historical client
    // lookup only. This helper never starts a text publisher or registers routes.
    this.identity=this.store?new DurableTimelineProjection({identityRouter,store:this.store,hostConfig:api?.config||{},logger,now}):null;
  }
  key(record){return `media:${hash(record.messageKey)}`;}
  save(record){this.store.update(this.key(record),()=>record);return record;}
  capture({sessionKey,event={},context={}}) {
    if(!this.store || !this.root || !sessionKey || sessionKey.includes(':workspace:'))return Promise.resolve(false);
    const metadata=event.metadata||{},paths=Array.isArray(metadata.mediaPaths)&&metadata.mediaPaths.length?metadata.mediaPaths:
      metadata.mediaPath?[metadata.mediaPath]:[];
    // The SDK may have only remote/unsupported media metadata. Record a fixed
    // failure when present, but never fetch its URL or infer one from caption.
    const count=paths.length || (metadata.mediaUrl || metadata.mediaType || metadata.mediaStagingPending?1:0);
    if(!count)return Promise.resolve(false);
    try {
      const resolved=this.router.resolvePinnedSession({sessionKey,channel:event.channel||context.channelId,accountId:event.accountId||context.accountId});
      const endpointKey=this.router.endpointKeyForSession(sessionKey),binding=resolved?.binding;
      const rawId=event.messageId ?? context.messageId,sourceId=Number(rawId),timestamp=event.timestamp;
      const to=String(context.conversationId || event.conversationId || event.from || '').replace(/^telegram:/u,'');
      const sender=String(event.senderId || context.senderId || event.from || '').replace(/^telegram:/u,'');
      if(!resolved?.bound || !endpointKey || binding.channel!=='telegram' ||
         !['string','number'].includes(typeof rawId) || !/^[1-9][0-9]*$/.test(String(rawId)) || !Number.isSafeInteger(sourceId) || sourceId<1 || typeof timestamp!=='number' || !Number.isFinite(timestamp) || timestamp<=0 || timestamp>this.now()+60_000 ||
         !/^[1-9][0-9]*$/.test(to) || to!==String(binding.senderId) || sender!==String(binding.senderId) ||
         (context.accountId||event.accountId||'default')!==(binding.accountId||'default') ||
         context.threadId || event.threadId || metadata.groupId) return Promise.resolve(false);
      const {namespace,routeNamespace}=this.identity.identity(binding,sessionKey,endpointKey);
      const types=Array.isArray(metadata.mediaTypes)?metadata.mediaTypes:[];
      const files=count>4?[{failure:'too_large'}]:metadata.mediaRemoteHost||metadata.mediaStagingPending?
        Array.from({length:count},()=>({failure:'unreadable'})):Array.from({length:count},(_,index)=>({localPath:typeof paths[index]==='string'&&paths[index].length<=4096?paths[index]:null,
        mimeType:typeof(types[index]??metadata.mediaType)==='string'?(types[index]??metadata.mediaType):null}));
      const receivedAt=utc(timestamp),messageKey=`telegram-media:v2:${routeNamespace}:${sourceId}`;
      const sourceHash=hash(JSON.stringify([sourceId,receivedAt,count,files]));
      const record={kind:'telegram-media-source',namespace,routeNamespace,messageKey,sourceHash,bindingKey:binding.key,
        sessionKey,endpointKey,sourceMessageId:sourceId,receivedAt,expiresAt:utc(Math.floor(timestamp/1000)*1000+TTL),files,state:'source',items:[]};
      const old=this.store.lookup(this.key(record));
      if(old){if(old.sourceHash!==sourceHash || old.namespace!==namespace)throw Error('MEDIA_SOURCE_CONFLICT');
        if(old.state==='sent')return Promise.resolve(true);
      } else {
        let sequence;this.store.update('sequence',value=>({sequence:sequence=(value?.sequence||0)+1}));
        if(!Number.isSafeInteger(sequence))throw Error('MEDIA_SEQUENCE_INVALID');
        this.store.registerIfAbsent(this.key(record),{...record,sequence});
      }
      return this.schedule(namespace);
    } catch(error){this.warn(error);return Promise.resolve(false);}
  }
  entries(){return this.store.entriesWithPrefix?this.store.entriesWithPrefix('media:'):this.store.entries().filter(row=>row.key.startsWith('media:'));}
  pending(){return this.entries().map(row=>row.value).filter(row=>row.kind==='telegram-media-source'&&row.state!=='sent').sort((a,b)=>a.sequence-b.sequence);}
  async call(record,name,args){
    const client=this.identity.resolve(record);if(!client)throw Error('MEDIA_IDENTITY_CHANGED');
    return client.callTool(name,{agent_host:'openclaw',endpoint_key:record.endpointKey,...args},
      {signal:AbortSignal.timeout(5000),meta:hostContextMeta()});
  }
  async source(record){
    const result=await this.call(record,'agentbridge_host_telegram_media_source',{source_message_id:record.sourceMessageId,received_at:record.receivedAt});
    if(result?.recorded!==true || !UUID.test(result.sourceEndpointId||'') || !/^[a-f0-9]{64}$/.test(result.pairingRef||'') || result.receivedAt!==record.receivedAt)throw Error('MEDIA_SOURCE_RECEIPT_INVALID');
    record.sourceEndpointId=result.sourceEndpointId;record.pairingRef=result.pairingRef;record.state='preparing';this.save(record);
  }
  prepare(record){
    let total=record.items.reduce((sum,item)=>sum+item.request.byte_size,0);
    for(let ordinal=record.items.length;ordinal<record.files.length;ordinal++){
      const file=record.files[ordinal];
      let material=Date.parse(record.expiresAt)<=this.now()?{failure:'expired'}:file.failure?file:readTelegramMedia(file,this.root);
      if(!material.failure && total+material.byteSize>MAX_TOTAL)material={failure:'too_large'};
      const failure=material.failure || null;
      const request={pairing_ref:record.pairingRef,source_message_id:record.sourceMessageId,ordinal,
        file_name:failure?'附件':material.fileName,mime_type:failure?'application/octet-stream':material.mimeType,
        byte_size:failure?0:material.byteSize,sha256:failure?null:material.sha256,received_at:record.receivedAt,failure};
      const body=material.body?.toString('base64')||'';
      const manifestHash=hash(JSON.stringify(request));
      const bodyPrefix=`body:${hash(record.messageKey)}:${ordinal}:${manifestHash}`;
      let bodyCount=0;
      for(let offset=0;offset<body.length;offset+=32_000){
        const key=`${bodyPrefix}:${bodyCount}`;
        const value={kind:'telegram-media-body',text:body.slice(offset,offset+32_000)};
        this.store.registerIfAbsent(key,value);
        if(JSON.stringify(this.store.lookup(key))!==JSON.stringify(value))throw Error('MEDIA_BODY_CONFLICT');
        bodyCount++;
      }
      record.items.push({request,manifestHash,bodyPrefix,bodyCount,state:'pending'});total+=request.byte_size;
      this.save(record);
    }
    record.state='pending';delete record.files;this.save(record);this.pruneOrphans(record);
  }
  body(item){
    const value=bodyKeys(item).map(key=>{const part=this.store.lookup(key);if(part?.kind!=='telegram-media-body'||typeof part.text!=='string')throw Error('MEDIA_BODY_MISSING');return part.text;}).join('');
    const bytes=Buffer.from(value,'base64');
    if(bytes.length!==item.request.byte_size || hash(bytes)!==item.request.sha256 || bytes.toString('base64')!==value)throw Error('MEDIA_BODY_CORRUPT');
    return value;
  }
  receipt(record,request,result){
    const value=result?.mediaReceipt;
    if(result?.recorded!==true || !value || !UUID.test(value.entryId||'') || value.sourceEndpointId!==record.sourceEndpointId ||
       value.sourceMessageId!==request.source_message_id || value.ordinal!==request.ordinal || value.fileName!==request.file_name ||
       value.mimeType!==request.mime_type || value.byteSize!==request.byte_size || value.sha256!==request.sha256 ||
       value.receivedAt!==request.received_at || value.expiresAt!==record.expiresAt || value.failure!==request.failure ||
       value.state!==(request.failure?'unavailable':'stored') || (request.failure?value.attachmentId!==null:!/^[A-Za-z0-9_-]{43}$/.test(value.attachmentId||'')))throw Error('MEDIA_APPEND_RECEIPT_INVALID');
    return value;
  }
  prune(record){
    for(const item of record.items)if(item.state==='sent'||item.expiryRequest)for(const key of bodyKeys(item))this.store.delete(key);
    for(const item of record.items)if(item.state==='sent'||item.expiryRequest){delete item.bodyPrefix;delete item.bodyCount;}
    this.save(record);
  }
  pruneOrphans(record){
    const prefix=`body:${hash(record.messageKey)}:`,keep=new Set(record.items.flatMap(bodyKeys));
    const keys=this.store.keysWithPrefix?this.store.keysWithPrefix(prefix):this.store.entries().map(row=>row.key).filter(key=>key.startsWith(prefix));
    for(const key of keys)if(!keep.has(key))this.store.delete(key);
  }
  async publish(record){
    if(record.state==='source')await this.source(record);
    if(record.state==='preparing')this.prepare(record);
    for(const item of record.items){
      if(item.state==='sent')continue;
      if(hash(JSON.stringify(item.request))!==item.manifestHash)throw Error('MEDIA_MANIFEST_CORRUPT');
      let request=item.expiryRequest||item.request;
      // Probe the original manifest first: a response lost before expiry still
      // settles its original receipt after revocation or the seven-day deadline.
      let result=await this.call(record,'agentbridge_host_telegram_media_append',request);
      if(result?.recorded!==true){
        if(result?.recorded!==false)throw Error('MEDIA_APPEND_RECEIPT_INVALID');
        if(!request.failure && Date.parse(record.expiresAt)<=this.now()){
          request={...request,file_name:'附件',mime_type:'application/octet-stream',byte_size:0,sha256:null,failure:'expired'};
          item.expiryRequest=request;this.save(record);this.prune(record);
        }
        result=await this.call(record,'agentbridge_host_telegram_media_append',{
          ...request,...(!request.failure?{content_base64:this.body(item)}:{})});
      }
      item.receipt=this.receipt(record,request,result);item.state='sent';this.save(record);
      try{this.prune(record);}catch{}
    }
    record.state='sent';this.save(record);try{this.prune(record);}catch{}
    return true;
  }
  schedule(namespace){
    if(this.queues.has(namespace))return this.queues.get(namespace);
    const work=Promise.resolve().then(async()=>{
      for(;;){
        const record=this.pending().find(value=>value.namespace===namespace);if(!record)return true;
        let done=false;
        for(let attempt=0;attempt<3;attempt++)try{done=await this.publish(record);break;}catch(error){
          if(attempt===2)this.warn(error);else await this.sleep(attempt===0?250:1000);
        }
        if(!done)return false;
      }
    }).catch(error=>{this.warn(error);return false;}).finally(()=>this.queues.delete(namespace));
    this.queues.set(namespace,work);return work;
  }
  async recover(){
    if(!this.store)return;
    for(const {value} of this.entries())if(value.kind==='telegram-media-source'){
      if(value.items.some(item=>(item.state==='sent'||item.expiryRequest)&&item.bodyCount))this.prune(value);
      if(['pending','sent'].includes(value.state))this.pruneOrphans(value);
    }
    return Promise.allSettled([...new Set(this.pending().map(value=>value.namespace))].map(value=>this.schedule(value)));
  }
  start(){if(!this.store||this.timer)return;void this.recover().catch(error=>this.warn(error));this.timer=setInterval(()=>void this.recover().catch(error=>this.warn(error)),30_000);this.timer.unref?.();}
  stop(){clearInterval(this.timer);this.timer=null;}
  async waitForIdle(){await Promise.allSettled([...this.queues.values()]);}
  warn(error){this.logger?.warn?.(`AgentBridge media mirror pending (${/^MEDIA_[A-Z_]{1,64}$/.test(error?.message||'')?error.message:'MEDIA_SYNC_FAILED'})`);}
}
function delay(ms){return new Promise(resolve=>{const timer=setTimeout(resolve,ms);timer.unref?.();});}
