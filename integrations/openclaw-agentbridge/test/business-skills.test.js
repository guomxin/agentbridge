import test from "node:test";
import assert from "node:assert/strict";
import { skillBindingMeta, rememberSkillBinding, businessSkillContext } from "../lib/business-skills.js";

test("business Skill binding is isolated by router, identity, session and run", () => {
  const router = {}, context = {runId:"r1",sessionKey:"s1"}, identity={binding:{key:"alice"}};
  rememberSkillBinding(router, context, identity, {status:"succeeded",binding_id:"binding-a"});
  assert.equal(skillBindingMeta(router, context, identity)["agentbridge/skill"].bindingId,"binding-a");
  for (const [r,c,i] of [[{},context,identity],[router,{...context,runId:"r2"},identity],
    [router,{...context,sessionKey:"s2"},identity],[router,context,{binding:{key:"bob"}}]]) {
    assert.deepEqual(skillBindingMeta(r,c,i),{});
  }
  rememberSkillBinding(router, context, identity, {status:"rejected",binding_id:"bad"});
  assert.equal(skillBindingMeta(router, context, identity)["agentbridge/skill"].bindingId,"binding-a");
});

test("directory is fetched fresh and full instructions are not preloaded", async () => {
  let count=0;
  const identity={client:{callTool:async(name)=>{assert.equal(name,"agentbridge_skill_catalog");count++;return {items:count===1?[{id:"review",name:"复核",description:"复核事项",selection:{use_when:"汇总工作",not_for:"原始记录",output:"来源总结"},version:"1",profiles:{},content:"private full instructions"}]:[]};}}};
  const context=await businessSkillContext(identity);
  assert.match(context,/agentbridge_skill_get/);
  assert.match(context,/"use_when":"汇总工作"/);
  assert.match(context,/"not_for":"原始记录"/);
  assert.match(context,/一次返回主说明/);
  assert.ok(!context.includes("private full instructions"));
  assert.equal(await businessSkillContext(identity),null);
  assert.equal(count,2);
});

test("published Skill load schema does not expose file selection", async () => {
  const {readFile} = await import("node:fs/promises");
  const catalog = JSON.parse(await readFile(new URL("../lib/agentbridge-tools.json", import.meta.url),"utf8"));
  const load = catalog.tools.find(t=>t.name==="agentbridge_skill_get");
  assert.ok(load);
  assert.equal(load.inputSchema.properties.resource, undefined);
  assert.deepEqual(load.inputSchema.required, ["skill_id", "profile"]);
});

test("unassigned users still get authoring and opt-in instructions",async()=>{
  const ctx=await businessSkillContext({client:{callTool:async()=>({items:[],authoring:{value:{auto_draft:true}}})}});
  assert.match(ctx,/agentbridge_skill_authoring/);assert.match(ctx,/已开启/);
});

test("draft sample blocks business calls only in its identity and run",async()=>{
  const {draftSampleGuard}=await import('../lib/business-skills.js');
  const r={},c={runId:'r',sessionKey:'s'},i={binding:{key:'alice'}};
  assert.equal(draftSampleGuard(r,c,i,'agentbridge_skill_authoring',{action:'test'}),null);
  assert.equal(draftSampleGuard(r,c,i,'database_execute',{}).error.code,'SKILL_SAMPLE_ONLY');
  assert.equal(draftSampleGuard(r,c,i,'agentbridge_skill_authoring',{action:'test_result'}),null);
  assert.equal(draftSampleGuard(r,{...c,runId:'other'},i,'database_execute',{}),null);
  assert.equal(draftSampleGuard(r,c,{binding:{key:'bob'}},'database_execute',{}),null);
});

test('completed run releases only its own skill and sample guards',async()=>{
  const {releaseSkillRun,draftSampleGuard}=await import('../lib/business-skills.js');
  const router={},identity={binding:{key:'alice'}},one={runId:'one',sessionKey:'s'},two={runId:'two',sessionKey:'s'};
  rememberSkillBinding(router,one,identity,{status:'succeeded',binding_id:'a'});
  rememberSkillBinding(router,two,identity,{status:'succeeded',binding_id:'b'});
  draftSampleGuard(router,one,identity,'agentbridge_skill_authoring',{action:'test'});
  releaseSkillRun(router,one);
  assert.deepEqual(skillBindingMeta(router,one,identity),{});
  assert.equal(skillBindingMeta(router,two,identity)['agentbridge/skill'].bindingId,'b');
  assert.equal(draftSampleGuard(router,one,identity,'database_execute',{}),null);
});

test('large catalog uses a bounded shortlist with explicit discovery fallback',async()=>{
  const items=Array.from({length:30},(_,i)=>({id:'s'+i,name:i===29?'特殊周报':'查询'+i,description:'方法',selection:{use_when:'处理任务'}}));
  const context=await businessSkillContext({client:{callTool:async()=>({items})}},'特殊周报');
  assert.match(context,/discover/);assert.match(context,/s29/);
  assert.equal((context.match(/"id":/g)||[]).length,20);
});
