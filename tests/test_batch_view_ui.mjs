import test from 'node:test';
import assert from 'node:assert/strict';
import {renderBatchChecklist} from '../agentbridge/auth/static/batch_view.mjs';
import {createBatchRemaining} from '../agentbridge/workspace/static/workspace_batch_remaining.mjs';
import {createDocument,descendants} from './workspace_dom_fixture.mjs';
const ORIGINAL='11111111-1111-1111-1111-111111111111',QUERY='22222222-2222-2222-2222-222222222222';
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function dom(){const document=createDocument(),create=document.createElement;document.createElement=tag=>{const node=create(tag);node.style={};return node;};return document;}
const projection=(changes={})=>({task:{task_id:ORIGINAL},oaBatchRemaining:{available:true,batchVersion:3,remainingCount:2,...changes}});
function fixture(queryRemaining=async()=>({taskId:QUERY})) {
  const document=dom(),state={},root=document.createElement('section');let active=true;
  const ui=createBatchRemaining({document,state,queryRemaining,makeKey:()=> 'same-query-request',getScope:()=>({current:()=>active})});
  ui.render(root,projection());
  return {document,state,root,ui,dispose(){active=false;},button:()=>root.children[1]};
}

test('read-only batch checklist preserves disclosure and item identity across status updates without executable links',()=>{
 const document=dom(),root=document.createElement('section');
 const batch={totalCount:3,items:[{ordinal:1,title:'<img src=x>',state:'succeeded'},
  {ordinal:2,title:'核对中',state:'outcome_unknown',isCurrent:true,presentation:{url:'https://untrusted.test/approve'}},
  {ordinal:3,title:'之后事项',state:'queued'}]};
 renderBatchChecklist(document,root,batch);const details=root.children[0],list=details.children[2],row=list.children[1];
 details.open=true;details.children[0].focus();
 assert.match(root.textContent,/已核验完成.*当前项.*结果待核对.*尚未处理/);
 assert.equal(row.attributes['aria-current'],'step');assert.equal(descendants(root).some(n=>['a','button','img'].includes(n.tag)),false);
 batch.items[1].state='failed';renderBatchChecklist(document,root,batch);
 assert.equal(root.children[0],details);assert.equal(details.open,true);assert.equal(list.children[1],row);
 assert.equal(document.activeElement,details.children[0]);assert.match(row.textContent,/本项未完成/);
 renderBatchChecklist(document,root,{});assert.equal(root.hidden,true);
});

test('remaining query shares one frozen request across card and detail, accepts only an actual query receipt',async()=>{
 const calls=[];let finish;
 const f=fixture((...args)=>{calls.push(args);return new Promise(resolve=>finish=resolve);});
 const detail=f.document.createElement('section');f.ui.render(detail,projection());
 f.button().events.click();detail.children[1].events.click();
 assert.equal(calls.length,1);assert.deepEqual(calls[0],[ORIGINAL,{requestKey:'same-query-request',expectedBatchVersion:3}]);
 assert.equal(detail.children[1].disabled,true);finish({taskId:QUERY});await flush();
 assert.match(detail.textContent,/查询已受理.*仍需重新选择.*逐项确认/);
 assert.equal(f.state.batchRemaining.get(ORIGINAL).request,null);
});

test('lost or malformed receipt preserves the exact remaining query, including its original batch version',async()=>{
 const calls=[];let reply=null;
 const f=fixture(async(...args)=>{calls.push(args);if(reply===null)throw Error('offline');return reply;});
 f.button().events.click();await flush();
 f.ui.render(f.root,projection({batchVersion:99,available:false}));
 f.button().events.click();await flush();assert.deepEqual(calls[1],calls[0]);
 reply={taskId:'not-a-query'};f.button().events.click();await flush();
 assert.deepEqual(calls[2],calls[0]);assert.match(f.button().textContent,/恢复同一次/);
 reply={taskId:QUERY};f.button().events.click();await flush();assert.deepEqual(calls[3],calls[0]);
 assert.equal(f.button().disabled,true);assert.equal(f.state.batchRemaining.get(ORIGINAL).request,null);
});

test('unavailable or malformed remaining projection cannot initiate a request and account disposal ignores late receipt',async()=>{
 let calls=0,finish;
 const f=fixture(()=>{calls++;return new Promise(resolve=>finish=resolve);});
 for(const value of [{available:false},{batchVersion:0},{batchVersion:1.5},{remainingCount:0},{remainingCount:1.5}]) {
  f.ui.render(f.root,projection(value));f.button().events.click();await flush();assert.equal(calls,0);
 }
 f.ui.render(f.root,projection());f.button().events.click();assert.equal(calls,1);
 f.dispose();finish({taskId:QUERY});await flush();assert.doesNotMatch(f.root.textContent,/查询已受理/);
});

test('remaining query view detach and reset release old detail subscriptions',()=>{
 const f=fixture(),detail=f.document.createElement('section');f.ui.render(detail,projection());
 const model=f.state.batchRemaining.get(ORIGINAL);assert.equal(model.views.size,2);
 f.ui.detach(detail);assert.equal(model.views.size,1);assert.equal(detail._batchRemaining,undefined);
 f.ui.reset();assert.equal(model.views.size,0);
});
