import test from 'node:test';
import assert from 'node:assert/strict';
import {CrossEndpointReference,parseProjectionReference,referenceMessage} from '../lib/cross-endpoint-reference.js';
import {createAgentBridgeProxyTools} from '../lib/proxy-tools.js';
const session='agent:main:telegram:direct:701';
const workspace='agent:main:agentbridge-workspace:direct:fixture';
const entry={entryId:'entry-a',source:{clientType:'web'},role:'user',text:'合成待办结果',createdAt:'2026-11-27T00:00:00+00:00'};
const selected={decision:'selected',allowNewOperation:false,entries:[entry]};
function setup(payload=selected) {
  const calls=[], sharedState={}, messages=new Map([[session,{taskRunRef:'turn:1',intentText:'继续网页刚才那个'}]]);
  const identity={bound:true,binding:{key:'telegram:default:701',channel:'telegram',senderId:'701',accountId:'default'},
    client:{async callTool(name,args){calls.push({name,args});return typeof payload==='function'?payload():payload;}}};
  const router={resolveToolContext:()=>identity,endpointKeyForSession:()=>identity.binding.key,catalogCacheKeyForBinding:()=> 'original-credential'};
  const coordinator={recentUserMessages:messages,currentUserMessageForSession:key=>messages.get(key)?.intentText || null};
  const subject=new CrossEndpointReference({identityRouter:router,coordinator,sharedState,nativeReceiptProof:()=>true,nativeReceiptKnown:()=>true});
  return {subject,calls,identity,router,coordinator,sharedState,messages};
}
const native=(changes={})=>({sessionKey:session,content:'解释这条',messageId:'9',replyToId:'7',senderId:'701',from:'telegram:701',
  conversationId:'701',channelId:'telegram',accountId:'default',isGroup:false,...changes});

test('unified syntax chooses only explicit opposite-channel references and preserves identifiers',()=>{
  assert.deepEqual(parseProjectionReference('继续网页刚才那个',session),{reference:{},question:''});
  assert.deepEqual(parseProjectionReference('接着工作台消息 entry-a，解释一下',session),{reference:{entryId:'entry-a'},question:'解释一下'});
  assert.deepEqual(parseProjectionReference('继续电报任务 task-1',workspace),{reference:{taskId:'task-1'},question:''});
  assert.equal(parseProjectionReference('继续电报刚才那个',session),null);
  assert.equal(parseProjectionReference('继续网页刚才那个','agent:main:openclaw-weixin:direct:701'),null);
  assert.equal(parseProjectionReference('现在查看 OA 待办',session),null);
  assert.equal(parseProjectionReference('继续网页消息 ../foreign',session).invalid,true);
});

test('selected summary uses strict private reference protocol and blocks business before and after response',async()=>{
  const h=setup(); assert.ok(h.subject.guard({sessionKey:session}));
  const result=await h.subject.prompt('继续网页刚才那个',{sessionKey:session,runId:'run1'});
  assert.equal(h.calls.length,1);
  assert.deepEqual(h.calls[0].args,{agent_host:'openclaw',endpoint_key:'telegram:default:701',projection_reference:{},max_age_minutes:360,limit:8});
  assert.match(result.prependSystemContext,/不得调用工具/);
  assert.match(JSON.parse(result.prependContext).reply,/合成待办结果/);
  assert.ok(h.subject.guard({sessionKey:session,runId:'run1'}));
});

test('ambiguous references require concrete entry selection and do not imply latest',()=>{
  const message=referenceMessage({...selected,decision:'ambiguous',entries:[entry,{...entry,entryId:'entry-b',text:'第二条'}]},'telegram');
  assert.match(message,/继续网页消息 entry-a/);assert.match(message,/继续网页消息 entry-b/);
});

for (const [label,change] of Object.entries({legacy:{allowNewOperation:undefined},authorized:{allowNewOperation:true},
  count:{entries:[entry,entry]},foreign:{entries:[{...entry,source:{clientType:'telegram'}}]},secret:{entries:[{...entry,text:'password: synthetic'}]},
  role:{entries:[{...entry,role:'system'}]},oversize:{entries:[{...entry,text:'🙂'.repeat(601)}]},empty:{entries:[]},
  invalidDate:{entries:[{...entry,createdAt:'invalid'}]},unknown:{decision:'anything'}})) {
  test(`unusable ${label} response fails closed`,()=>assert.match(referenceMessage({...selected,...change},'telegram'),/失效/));
}

test('transport failure remains read-only and carries fixed public retry guidance',async()=>{
  const h=setup(()=>{throw new Error('private detail');});
  const result=await h.subject.prompt('继续网页刚才那个',{sessionKey:session,runId:'r'});
  assert.match(result.prependContext,/暂时无法/);assert.doesNotMatch(result.prependContext,/private detail/);
  assert.ok(h.subject.guard({sessionKey:session,runId:'r'}));
});

test('native exact reply is distinct from user-written message numbers',async()=>{
  const h=setup();h.messages.get(session).intentText='解释这条';
  h.subject.capture(native(),{sessionKey:session,channelId:'telegram',conversationId:'701',accountId:'default'});
  await h.subject.prompt('解释这条',{sessionKey:session,runId:'r'});
  assert.deepEqual(h.calls[0].args.projection_reference,{telegramReceipt:7});
  const plain=setup();plain.messages.get(session).intentText='回复消息 7 请解释';
  plain.subject.capture(native({content:'回复消息 7 请解释',replyToId:undefined}),{sessionKey:session});
  assert.equal(await plain.subject.prompt('回复消息 7 请解释',{sessionKey:session}),null);
});

for (const [label,change] of Object.entries({foreignPeer:{conversationId:'702'},foreignSender:{senderId:'702'},
  foreignBot:{accountId:'other'},foreignChannel:{channelId:'foreign'},group:{isGroup:true},topic:{threadId:1},
  invalidId:{replyToId:'0'},fraction:{replyToId:1.5},conflict:{metadata:{replyToId:'8'}},fullId:{replyToIdFull:'different'}})) {
  test(`native ${label} never falls back to a different latest entry`,async()=>{
    const h=setup();h.subject.capture(native(change),{sessionKey:session,accountId:'default'});
    const result=await h.subject.prompt('继续网页刚才那个',{sessionKey:session});
    assert.match(result.prependContext,/失效/);assert.equal(h.calls.length,0);
  });
}

test('typed exact identifier takes precedence over a valid native reply',async()=>{
  const h=setup();h.subject.capture(native(),{sessionKey:session});
  await h.subject.prompt('继续网页消息 entry-a',{sessionKey:session});
  assert.deepEqual(h.calls[0].args.projection_reference,{entryId:'entry-a'});
});

test('next ordinary turn does not inherit restriction but original run and frozen tools retain it',async()=>{
  const h=setup(),context={sessionKey:session,runId:'original'};
  const guard=h.subject.toolGuard(context);
  await h.subject.prompt('继续网页刚才那个',context);
  h.messages.set(session,{taskRunRef:'turn:2',intentText:'新的普通问题'});
  h.subject.capture(native({replyToId:undefined}),{sessionKey:session});
  assert.equal(h.subject.guard({sessionKey:session,runId:'next'}),null);
  assert.ok(h.subject.guard(context));assert.ok(guard());
  const runtime=new CrossEndpointReference({identityRouter:h.router,coordinator:h.coordinator,sharedState:h.sharedState});
  assert.ok(runtime.guard(context));
});

test('identity change while awaiting resolver discards summary without obtaining a new identity',async()=>{
  let release;const pending=new Promise(r=>{release=r;});const h=setup(()=>pending);
  const reply=h.subject.prompt('继续网页刚才那个',{sessionKey:session,runId:'r'});
  h.identity.binding={...h.identity.binding,key:'telegram:other:701'};release(selected);
  assert.match((await reply).prependContext,/失效/);assert.equal(h.calls.length,1);
});

test('direct proxy invocation cannot bypass native hook or create planning/task context',async()=>{
  const h=setup();let planning=0;
  const tools=createAgentBridgeProxyTools({context:{sessionKey:session},identityRouter:h.router,
    readOnlyReferenceGuard:h.subject.toolGuard({sessionKey:session}),planningContextFactory:()=>{planning++;},serverName:'agentbridge'});
  const tool=tools.find(t=>t.name==='oa_workflow_pending_list');
  const reply=await tool.execute('synthetic-call',{});
  assert.match(JSON.stringify(reply),/CROSS_ENDPOINT_REFERENCE_READ_ONLY/);assert.equal(planning,0);assert.equal(h.calls.length,0);
});

test('Workspace selects only Telegram public entries and query status is a permitted public role',async()=>{
  const h=setup({...selected,entries:[{...entry,source:{clientType:'telegram'},role:'assistant'}]});
  h.messages.set(workspace,{taskRunRef:'workspace:1',intentText:'继续电报消息 entry-a'});
  h.router.endpointKeyForSession=()=> 'workspace:fixture';
  const value=await h.subject.prompt('继续电报消息 entry-a',{sessionKey:workspace,runId:'workspace-run'});
  assert.match(value.prependContext,/电报原消息摘要/);
  assert.deepEqual(h.calls[0].args.projection_reference,{entryId:'entry-a'});
  assert.match(referenceMessage({...selected,entries:[{...entry,role:'status',text:'原 OA 待办查询已完成。'}]},'telegram'),/已完成/);
});

test('oversized and unsupported legacy cross-end wording cannot fall back to business continuation',async()=>{
  for(const text of ['继续网页刚才那个，'+'🙂'.repeat(3300),'继续刚才其他端的任务']) {
    const h=setup();h.messages.get(session).intentText=text;
    const value=await h.subject.prompt(text,{sessionKey:session,runId:'r'});
    assert.match(value.prependContext,/失效/);assert.equal(h.calls.length,0);assert.ok(h.subject.guard({sessionKey:session,runId:'r'}));
  }
});

test('missing message callback still locks prompt run and tool factory without creating a task',async()=>{
  const h=setup();h.messages.clear();
  await h.subject.prompt('继续网页刚才那个',{sessionKey:session,runId:'restored-run'});
  assert.ok(h.subject.guard({sessionKey:session}));
  const guard=h.subject.toolGuard({sessionKey:session,runId:'restored-run'});
  assert.equal(await h.subject.prompt('新的普通问题',{sessionKey:session,runId:'new'}),null);
  assert.equal(h.subject.guard({sessionKey:session,runId:'new'}),null);assert.ok(guard());
});


test('native message id without original Bot receipt custody cannot select a public entry',async()=>{
  const h=setup();h.subject.nativeReceiptProof=()=>false;
  h.subject.capture(native(),{sessionKey:session});
  assert.match((await h.subject.prompt('解释这条',{sessionKey:session})).prependContext,/失效/);
  assert.equal(h.calls.length,0);
});


test('ordinary Telegram replies retain same-endpoint behavior and never import cross-end history',async()=>{
  const h=setup();h.messages.get(session).intentText='解释同端这条';
  h.subject.nativeReceiptProof=()=>false;h.subject.nativeReceiptKnown=()=>false;
  h.subject.capture(native({content:'解释同端这条'}),{sessionKey:session});
  assert.equal(await h.subject.prompt('解释同端这条',{sessionKey:session,runId:'ordinary'}),null);
  assert.equal(h.subject.guard({sessionKey:session,runId:'ordinary'}),null);assert.equal(h.calls.length,0);
});

test('explicit cross-end text with an unrecognized native receipt remains rejected, never latest',async()=>{
  const h=setup();h.subject.nativeReceiptProof=()=>false;h.subject.nativeReceiptKnown=()=>false;
  h.subject.capture(native({content:'继续网页刚才那个'}),{sessionKey:session});
  assert.match((await h.subject.prompt('继续网页刚才那个',{sessionKey:session})).prependContext,/失效/);
  assert.equal(h.calls.length,0);assert.ok(h.subject.guard({sessionKey:session}));
});
