import test from 'node:test';
import assert from 'node:assert/strict';
import { registerSkillCompletion } from '../lib/skill-completion.js';

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
