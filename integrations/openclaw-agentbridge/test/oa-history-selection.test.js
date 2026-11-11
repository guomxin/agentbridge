import test from 'node:test';
import assert from 'node:assert/strict';
import {createAgentBridgeProxyTools} from '../lib/proxy-tools.js';
import {InteractionCoordinator} from '../lib/coordinator.js';
import {resolvePluginConfig} from '../lib/config.js';

function harness({partial = false} = {}) {
  const calls = [];
  const context = {sessionKey:'agent:main:agentbridge-workspace:direct:selection-user',runId:'selection-run'};
  const response = {status:'succeeded',result:{schema_version:'agentbridge.oa_history_query.v1',
    state:partial ? 'partial' : 'complete',items:partial ? [] : [{affair_id:'175',title:'合成事项'}],
    selection:{kind:'latest',count:1,workflow_state:'in_progress',date_basis:'initiated_at',
      selected_count:partial ? 0 : 1,selection_complete:!partial,
      reason:partial ? 'selection_state_unverified' : 'complete'},has_more:false,next_cursor:null}};
  const client = {
    async callTool(name) {
      if (name === 'agentbridge_host_task_ensure') return {task:{taskId:'selection-task-1'}};
      if (name === 'agentbridge_host_coordinator_lease_acquire') return {status:'succeeded',
        coordinatorLease:{version:1,hostInstanceId:'openclaw-gateway'}};
      if (name === 'agentbridge_host_task_observe') return {status:'succeeded'};
      assert.fail(`Unexpected coordination call ${name}`);
    },
    async callToolResult(name, args) {
      calls.push({name,args});
      return {structuredContent:response,content:[{type:'text',text:JSON.stringify(response)}]};
    },
  };
  const coordinator = new InteractionCoordinator({api:{logger:{warn(){},info(){},error(){}}},
    config:resolvePluginConfig({autoPoll:false,syncTimeline:false})});
  const tools = createAgentBridgeProxyTools({context,serverName:'agentbridge',
    identityRouter:{resolveToolContext:()=>({bound:true,binding:{},client}),
      endpointKeyForSession:()=> 'workspace:selection-user'},
    trustedResultHandler:(event,ctx)=>coordinator.captureNativeToolResult(event,ctx)});
  return {tool:tools.find(item=>item.name==='oa_workflow_history_query'),calls,response};
}

test('native history selection is optional, closed and distinct from page size', () => {
  const h = harness();
  const input = h.tool.parameters.properties.selection;
  const branch = input.anyOf?.find(item=>item.type!=='null') || input;
  const schema = branch.$ref ? h.tool.parameters.$defs[branch.$ref.split('/').at(-1)] : branch;
  assert.equal(schema.additionalProperties,false);
  assert.deepEqual(schema.required,['kind','count','workflow_state']);
  assert.equal(schema.properties.count.maximum,50);
  assert.deepEqual(schema.properties.workflow_state.enum,['all','in_progress','ended']);
  assert.equal(h.tool.parameters.required.includes('selection'),false);
  assert.equal(h.tool.annotations.readOnlyHint,true);
});

test('native history call preserves latest selection without narrowing dates or requesting other pages', async () => {
  const h = harness();
  const selection = Object.freeze({kind:'latest',count:1,workflow_state:'in_progress'});
  const result = await h.tool.execute('selection-call-1',Object.freeze({collection:'sent',
    temporal:Object.freeze({kind:'all'}),selection,limit:50}));
  assert.equal(h.calls.length,1);
  assert.equal(h.calls[0].name,'oa_workflow_history_query');
  assert.deepEqual(h.calls[0].args.selection,selection);
  assert.deepEqual(h.calls[0].args.temporal,{kind:'all'});
  assert.equal(h.calls[0].args.limit,50);
  assert.deepEqual(result.content,[{type:'text',text:JSON.stringify(h.response)}]);
});

test('native partial selection retains uncertainty and never automatically starts another query', async () => {
  const h = harness({partial:true});
  const result = await h.tool.execute('selection-call-unknown',{collection:'sent',temporal:{kind:'all'},
    selection:{kind:'latest',count:1,workflow_state:'in_progress'}});
  const value = JSON.parse(result.content[0].text);
  assert.equal(value.result.selection.selection_complete,false);
  assert.equal(value.result.selection.reason,'selection_state_unverified');
  assert.deepEqual(value.result.items,[]);
  assert.equal(h.calls.length,1);
});
