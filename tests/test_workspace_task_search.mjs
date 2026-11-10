import test from 'node:test';
import assert from 'node:assert/strict';
import {createTaskSearch} from '../agentbridge/workspace/static/workspace_task_search.mjs';
import {createWorkspaceViewState} from '../agentbridge/workspace/static/workspace_navigation.mjs';
import {createLifecycle} from '../agentbridge/workspace/static/workspace_lifecycle.mjs';
import {createDocument} from './workspace_dom_fixture.mjs';

const deferred = () => {let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {resolve,reject,promise};};
const page = (offset,count=32,more=true) => ({items:Array.from({length:count},(_,i)=>({task_id:`task-${offset+i}`,title:`合成任务${offset+i}`})),
  activeCount:6,hasMore:more,nextCursor:more?{cursor:`cursor-${offset+count}`}:null});
function setup(handler) {
  const document=createDocument(), nodes=new Map();
  const $=id=>{if(!nodes.has(id))nodes.set(id,document.createElement('div'));return nodes.get(id);};
  const lifecycle=createLifecycle(), scope=lifecycle.scope(), state={tasks:[]}, calls=[];
  const storageData=new Map();let account='alice', renders=0, selectionsCleared=0;
  const views=createWorkspaceViewState({storage:{getItem:key=>storageData.get(key),setItem:(key,value)=>storageData.set(key,value)},getAccountId:()=>account});
  const search=createTaskSearch({state,$,getScope:()=>scope,viewState:views,
    request:(path,current)=>{calls.push(path);return handler(path,current,calls.length);},renderTasks:()=>{renders++;},
    onApply:()=>{selectionsCleared++;state.selectedTaskId=null;}});
  search.bind();search.restore();
  return {search,state,$,calls,views,scope,lifecycle,storageData,setAccount:value=>account=value,
    get renders(){return renders;},get selectionsCleared(){return selectionsCleared;}};
}

test('old tasks remain reachable beyond 150 and paging preserves position without replaying any action', async()=>{
  const h=setup((path)=>{const cursor=new URL(path,'http://local').searchParams.get('cursor');return page(cursor?Number(cursor.split('-')[1]):0);});
  await h.search.load();h.$('#task-list').scrollTop=410;
  for(let i=0;i<5;i++)await h.search.load({append:true});
  assert.equal(h.state.tasks.length,192);assert.equal(h.state.tasks.at(-1).task_id,'task-191');
  assert.equal(h.$('#task-list').scrollTop,410);assert.equal(h.$('#active-task-count').textContent,'6');
  assert.ok(h.calls.every(path=>path.startsWith('/api/tasks?')));
  const calls=h.calls.length;await h.search.load({automatic:true});assert.equal(h.calls.length,calls);
  assert.match(h.$('#task-list-status').textContent,/任务有更新/);assert.equal(h.state.tasks.length,192);
  h.lifecycle.dispose();
});

test('a late old-filter response cannot replace a newer unknown-result search', async()=>{
  const waiting=[];const h=setup(()=>{const d=deferred();waiting.push(d);return d.promise;});
  const old=h.search.load();h.$('#task-search').value='结算';h.$('#task-status').value='outcome_unknown';
  const fresh=h.search.apply();waiting[1].resolve(page(200,1,false));await fresh;
  waiting[0].resolve(page(0));await old;
  assert.equal(h.state.tasks[0].task_id,'task-200');
  const query=new URL(h.calls[1],'http://local').searchParams;
  assert.equal(query.get('status'),'outcome_unknown');assert.equal(query.get('q'),'结算');assert.equal(query.has('cursor'),false);
  assert.equal(h.$('#task-more').hidden,true);h.lifecycle.dispose();
});

test('account disposal rejects a late page and preferences never migrate to another account',async()=>{
  const d=deferred(),h=setup(()=>d.promise);
  h.$('#task-search').value='周报';h.$('#task-from').value='2026-10-01';const pending=h.search.apply();
  h.scope.dispose();h.search.reset();h.state.tasks=[];h.setAccount('bob');h.search.restore();
  d.resolve(page(0));await pending;
  assert.equal(h.state.tasks.length,0);assert.equal(h.$('#task-search').value,'');assert.equal(h.$('#task-from').value,'');
  assert.equal(h.$('#task-list-status').textContent,'');
  h.setAccount('alice');h.search.restore();assert.equal(h.$('#task-search').value,'周报');assert.equal(h.$('#task-from').value,'2026-10-01');
  assert.doesNotMatch([...h.storageData.values()].join(''),/cursor-|task-\d/);h.lifecycle.dispose();
});

test('failed next page preserves results and retry resumes the same cursor, deduplicating changed rows',async()=>{
  const h=setup((_path,_scope,n)=>{if(n===2)throw new Error('network');return n===1?page(0):{...page(31,2,false)};});
  await h.search.load();await h.search.load({append:true});
  assert.equal(h.state.tasks.length,32);assert.equal(h.$('#task-retry').hidden,false);
  assert.match(h.$('#task-list-status').textContent,/保留上次结果/);
  await h.$('#task-retry').events.click();
  assert.equal(h.calls[1],h.calls[2]);assert.equal(h.state.tasks.length,33);assert.equal(h.$('#task-retry').hidden,true);
  h.lifecycle.dispose();
});

test('incoming events do not submit partially typed filters and surface changes during a pending read',async()=>{
  const d=deferred();const h=setup(()=>d.promise);const initial=h.search.load();
  h.$('#task-search').value='尚未点击查找';await h.search.load({automatic:true});
  assert.equal(h.calls.length,1);d.resolve(page(0));await initial;
  assert.match(h.$('#task-list-status').textContent,/任务有更新/);
  assert.equal(new URL(h.calls[0],'http://local').searchParams.get('q'),'');h.lifecycle.dispose();
});

test('an invalid continuation retries from a fresh first page instead of looping on an expired cursor',async()=>{
  const h=setup((_path,_scope,n)=>{if(n===2)throw Object.assign(new Error('翻页信息已失效，请重新查询。'),{status:400});return page(0);});
  await h.search.load();await h.search.load({append:true});
  assert.equal(h.$('#task-more').hidden,true);assert.match(h.$('#task-list-status').textContent,/重新查询/);
  await h.$('#task-retry').events.click();
  assert.equal(new URL(h.calls[2],'http://local').searchParams.has('cursor'),false);
  assert.equal(h.state.tasks.length,32);h.lifecycle.dispose();
});

test('saved task filters are bounded reading preferences with status and system allowlists',()=>{
  const h=setup(()=>page(0));h.views.saveTaskFilters({q:'x'.repeat(500),status:'admin',system_id:'secret-system',updated_from:'bad',cursor:'secret'});
  h.search.restore();assert.equal(h.$('#task-search').value.length,200);assert.equal(h.$('#task-status').value,'all');
  assert.equal(h.$('#task-system').value,'');assert.equal(h.$('#task-from').value,'');
  assert.doesNotMatch([...h.storageData.values()].join(''),/secret|admin|cursor/);h.lifecycle.dispose();
});

test('new filters close old selection while automatic updates preserve a reader even on the first page',async()=>{
  const h=setup(()=>page(0));await h.search.load();h.state.selectedTaskId='old';
  h.$('#task-list').scrollTop=260;await h.search.load({automatic:true});
  assert.equal(h.calls.length,1);assert.equal(h.state.selectedTaskId,'old');
  h.$('#task-status').value='waiting_user';await h.search.apply();
  assert.equal(h.selectionsCleared,1);assert.equal(h.state.selectedTaskId,null);
  assert.equal(h.$('#task-list').scrollTop,0);h.lifecycle.dispose();
});
