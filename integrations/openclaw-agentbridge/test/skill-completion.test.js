import test from 'node:test';
import assert from 'node:assert/strict';
import { registerSkillCompletion, completionUsage } from '../lib/skill-completion.js';

test('usage preserves known tokens and distinguishes missing prices from a known zero estimate', () => {
  assert.equal(completionUsage(null, {}), null);
  assert.deepEqual(completionUsage({input:100,output:20,totalTokens:120,cost:{total:0}}, {}),
    {input:100,output:20,totalTokens:120});
  assert.deepEqual(completionUsage({input:0,cost:{total:0}}, {cost:{input:2}}), {input:0,cost_estimate_usd:0});
  assert.deepEqual(completionUsage({input:10,output:-1,totalTokens:'10',cacheRead:5,cacheWrite:Infinity,cost:{total:.01}}, {}),
    {input:10,cacheRead:5,cost_estimate_usd:.01});
  assert.equal(completionUsage({input:NaN,cost:{total:Infinity}}, {}), null);
});

test('workbench completion has no tools, session or client model override', async () => {
  let handler; let scope; let input;
  registerSkillCompletion({ config:{}, registerGatewayMethod(name,h,s){assert.equal(name,'agentbridge.skills.complete');handler=h;scope=s;} }, {
    loadSdk: async()=>({prepareSimpleCompletionModelForAgent:async()=>({model:{provider:'test',id:'model'},auth:{}}),
      completeWithPreparedSimpleCompletionModel:async p=>{input=p;return {content:[{type:'text',text:'计划修复'}],stopReason:'stop'};}})
  });
  let result;
  await handler({params:{system:'方法',prompt:'整理一下'},respond:(...args)=>{result=args;}});
  assert.equal(result[0],true);assert.deepEqual(input.context.tools,[]);
  assert.equal(input.context.messages.length,1);assert.equal(input.sessionKey,undefined);
  assert.equal(scope.scope,'operator.write');
  await handler({params:{system:'x',prompt:'x',model:'arbitrary'},respond:(...args)=>{result=args;}});
  assert.equal(result[0],false);
});

test('provider errors and tool calls cannot become successful reports', async () => {
  for(const response of [{content:[{type:'toolCall',name:'write'}],stopReason:'toolUse'}, {content:[],stopReason:'stop'}]){
    let handler;let result;
    registerSkillCompletion({config:{},registerGatewayMethod(n,h){handler=h;}},{loadSdk:async()=>({
      prepareSimpleCompletionModelForAgent:async()=>({model:{},auth:{}}),completeWithPreparedSimpleCompletionModel:async()=>response})});
    await handler({params:{system:'x',prompt:'x'},respond:(...args)=>{result=args;}});
    assert.equal(result[0],false);assert.equal(result[2].code,'SKILL_MODEL_UNAVAILABLE');
  }
});
