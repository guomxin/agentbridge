import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createBusinessWatches} from '../agentbridge/workspace/static/workspace_watches.mjs';
import {createLifecycle} from '../agentbridge/workspace/static/workspace_lifecycle.mjs';
import {createWorkspaceRequests} from '../agentbridge/workspace/static/workspace_request.mjs';
import {createDocument, descendants} from './workspace_dom_fixture.mjs';

const deferred = () => {let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {resolve,reject,promise};};
const watch = (id='one', version=1, state='active', extra={}) => ({watch_id:id, version, state, title:`合成关注${id}`,
  snapshot:{workflow_state:'in_progress',current_nodes:[{name:'核对',handlers:['甲']}],current_handlers:['甲'],
    observed_at:'2026-11-10T08:00:00Z',unavailable_fields:[]},last_checked_at:'2026-11-10T08:00:00Z',
  next_check_at:'2026-11-10T09:00:00Z',expires_at:'2026-11-17T08:00:00Z',...extra});
const detail = value => ({watch:value,events:[{event_id:'created',event_type:'created',created_at:'2026-11-10T08:00:00Z',summary:'开始关注'}]});
function harness(handler) {
  const document=createDocument(),nodes=new Map(),lifecycle=createLifecycle();let scope=lifecycle.scope();
  const $=selector=>{if(!nodes.has(selector))nodes.set(selector,document.createElement('div'));return nodes.get(selector);};
  const calls=[];
  const controller=createBusinessWatches({document,$,getScope:()=>scope,
    request:(path,current,options={})=>{const call={path,scope:current,...options};calls.push(call);return handler(call,calls.length);}});
  const button=action=>descendants($('#business-watch-detail')).find(node=>node.dataset.watchAction===action);
  return {document,$,nodes,calls,controller,lifecycle,button,get scope(){return scope;},
    replaceAccount(){scope.dispose();controller.reset();scope=lifecycle.scope();}};
}

test('watch list pages beyond a screen, deduplicates IDs, preserves scrolling and never creates or checks a watch',async()=>{
  const title='<img src=x onerror=unsafe>'+'很长的标题'.repeat(80);
  const h=harness(call=>call.path.includes('cursor=')
    ? {items:[watch('31'),watch('32')],hasMore:false}
    : {items:Array.from({length:32},(_,i)=>watch(String(i),1,'active',i===0?{title}:{})),hasMore:true,nextCursor:'opaque-next'});
  await h.controller.activate();h.$('#business-watch-list').scrollTop=420;
  await h.controller.load({append:true});
  assert.equal(h.$('#business-watch-list').children.length,33);
  assert.equal(h.$('#business-watch-list').scrollTop,420);
  assert.ok(h.$('#business-watch-list').textContent.includes(title));
  assert.equal(descendants(h.$('#business-watch-list')).some(node=>node.tag==='img'),false);
  assert.ok(h.calls.every(call=>call.path.startsWith('/api/business-watches')&&!call.method));
  assert.equal(h.$('#business-watch-more').hidden,true);h.lifecycle.dispose();
});

test('failed page retains rows and retries only the same read cursor; empty list is explicit',async()=>{
  let count=0;
  const h=harness(()=>{count++;if(count===2)throw new Error('offline');return count===1
    ? {items:[watch()],hasMore:true,nextCursor:'next'} : {items:[watch('two')],hasMore:false};});
  await h.controller.activate();await h.controller.load({append:true});
  assert.match(h.$('#business-watch-list').textContent,/合成关注one/);
  assert.match(h.$('#business-watch-list-status').textContent,/保留/);
  await h.$('#business-watch-retry').events.click();
  assert.equal(h.calls[1].path,h.calls[2].path);assert.equal(h.$('#business-watch-list').children.length,2);
  h.lifecycle.dispose();
  const empty=harness(()=>({items:[],hasMore:false}));await empty.controller.activate();
  assert.match(empty.$('#business-watch-list-status').textContent,/暂时没有/);empty.lifecycle.dispose();
});

test('detail distinguishes missing fields, old observations and ended flow from business completion',async()=>{
  const value=watch('one',1,'completed',{snapshot:{workflow_state:'ended',current_nodes:[],current_handlers:[],
    observed_at:'2026-11-10T08:00:00Z',unavailable_fields:[]}});
  const h=harness(call=>call.path==='/api/business-watches'?{items:[value],hasMore:false}:detail(value));
  await h.controller.activate();await h.controller.select('one');
  const copy=h.$('#business-watch-detail').textContent;
  assert.match(copy,/无当前节点（流程已结束）/);assert.match(copy,/流程结束不代表款项已支付/);
  assert.match(copy,/不估算停留时长/);assert.equal(h.button('check').hidden,true);
  assert.equal(h.button('resume').hidden,true);h.lifecycle.dispose();
  const missing=watch('one',1,'active',{snapshot:{workflow_state:'running',current_nodes:[{name:'旧节点'}],
    current_handlers:['旧处理人'],unavailable_fields:['current_nodes','current_handlers']}});
  const m=harness(call=>call.path==='/api/business-watches'?{items:[missing],hasMore:false}:detail(missing));
  await m.controller.activate();await m.controller.select('one');
  assert.doesNotMatch(m.$('#business-watch-detail').textContent,/旧节点|旧处理人/);m.lifecycle.dispose();
});

test('pause and resume consume the current projected version once and leave OA actions absent',async()=>{
  const pending=deferred();let value=watch(),mutations=0;
  const h=harness(call=>call.method?(++mutations===1?pending.promise:{watch:watch('one',3,'active')}):call.path==='/api/business-watches'?{items:[value],hasMore:false}:detail(value));
  await h.controller.activate();await h.controller.select('one');
  const pause=h.controller.act('pause');await h.controller.act('pause');
  const mutation=h.calls.filter(call=>call.method);
  assert.equal(mutation.length,1);assert.equal(mutation[0].path,'/api/business-watches/one/update');
  assert.equal(mutation[0].csrf,true);assert.deepEqual(mutation[0].body,{expected_version:1,action:'pause'});
  assert.equal(h.button('pause').disabled,true);
  pending.resolve({watch:watch('one',2,'paused')});await pause;
  assert.equal(h.button('pause').hidden,true);assert.equal(h.button('resume').disabled,false);
  await h.controller.act('resume');
  assert.deepEqual(h.calls.filter(call=>call.method).at(-1).body,{expected_version:2,action:'resume'});
  assert.equal(h.button('check').disabled,false);
  assert.match(h.$('#business-watch-detail').textContent,/停止关注不改变 OA 流程/);
  assert.ok(h.calls.every(call=>!/(prepare|authorize|commit|replay)/.test(call.path)));h.lifecycle.dispose();
});

test('a manual check renders the authoritative new observation and deduplicates event receipts',async()=>{
  const changed=watch('one',2,'active',{snapshot:{workflow_state:'running',current_nodes:[{name:'复核',handlers:['乙','丙']}],
    current_handlers:['乙','丙'],observed_at:'2026-11-10T09:00:00Z',unavailable_fields:[]}});
  const event={event_id:'change-1',event_type:'changed',created_at:'2026-11-10T09:00:00Z',summary:'当前节点有变化'};
  const h=harness(call=>call.method?{watch:changed,events:[event,event,null]}
    :call.path==='/api/business-watches'?{items:[watch()],hasMore:false}:detail(watch()));
  await h.controller.activate();await h.controller.select('one');await h.controller.act('check');
  const check=h.calls.find(call=>call.method);
  assert.equal(check.path,'/api/business-watches/one/check');assert.equal(check.timeoutMs,45000);
  assert.equal(check.csrf,true);assert.deepEqual(check.body,{expected_version:1});
  assert.match(h.$('#business-watch-detail').textContent,/复核（乙、丙）/);
  assert.match(h.$('#business-watch-detail').textContent,/2026\/11\/10 17:00:00/);
  assert.equal(h.$('#business-watch-detail').textContent.split('当前节点有变化').length-1,1);
  assert.equal(h.button('check').disabled,false);h.lifecycle.dispose();
});

test('an incomplete observation preserves the last snapshot and does not claim a newly verified state',async()=>{
  const incomplete=watch('one',2,'active',{last_check_status:'incomplete'});
  const h=harness(call=>call.method?{watch:incomplete,events:[]}
    :call.path==='/api/business-watches'?{items:[watch()],hasMore:false}:detail(watch()));
  await h.controller.activate();await h.controller.select('one');await h.controller.act('check');
  assert.match(h.$('#business-watch-detail').textContent,/未取得完整可信状态/);
  assert.match(h.$('#business-watch-detail').textContent,/流转中/);
  assert.doesNotMatch(h.$('#business-watch-detail').textContent,/本次核验已结束/);h.lifecycle.dispose();
});

test('a redacted pause receipt after permission loss clears all formerly readable material',async()=>{
  const h=harness(call=>call.method?{watch:{watch_id:'one',state:'paused',version:2}}
    :call.path==='/api/business-watches'?{items:[watch()],hasMore:false}:detail(watch()));
  await h.controller.activate();await h.controller.select('one');await h.controller.act('pause');
  assert.match(h.$('#business-watch-detail').textContent,/关注已暂停/);
  assert.doesNotMatch(h.$('#business-watch-detail').textContent,/合成关注one|核对（甲）|开始关注/);
  assert.doesNotMatch(h.$('#business-watch-list').textContent,/合成关注one/);h.lifecycle.dispose();
});

test('version conflict blocks further mutations until a fresh GET establishes the new version',async()=>{
  let value=watch(), rejected=true;
  const h=harness(call=>{
    if(!call.method)return call.path==='/api/business-watches'?{items:[value],hasMore:false}:detail(value);
    if(rejected){rejected=false;value=watch('one',4,'paused');throw Object.assign(new Error('changed'),{status:409,code:'WATCH_VERSION_CONFLICT'});}
    return {watch:watch('one',5,'stopped')};
  });
  await h.controller.activate();await h.controller.select('one');await h.controller.act('pause');
  assert.match(h.$('#business-watch-detail').textContent,/刷新详情后再操作/);
  await h.controller.act('stop');assert.equal(h.calls.filter(c=>c.method).length,1);
  await h.controller.select('one',{preserve:true});await h.controller.act('stop');
  assert.deepEqual(h.calls.filter(c=>c.method).at(-1).body,{expected_version:4,action:'stop'});
  assert.equal(h.button('stop').hidden,true);h.lifecycle.dispose();
});

test('a source-read 409 preserves its actual failure reason and old snapshot without claiming the watch changed',async()=>{
  const message='本次未能核实流程状态，已保留上次可信结果。';
  const h=harness(call=>{
    if(call.method)throw Object.assign(new Error(message),{status:409,code:'WATCH_SOURCE_UNAVAILABLE'});
    return call.path==='/api/business-watches'?{items:[watch()],hasMore:false}:detail(watch());
  });
  await h.controller.activate();await h.controller.select('one');await h.controller.act('check');
  const copy=h.$('#business-watch-detail').textContent;
  assert.ok(copy.includes(message));assert.match(copy,/核对（甲）/);
  assert.doesNotMatch(copy,/关注记录已更新/);assert.equal(h.button('check').disabled,true);
  await h.controller.act('check');assert.equal(h.calls.filter(c=>c.method).length,1);
  await h.controller.select('one',{preserve:true});assert.equal(h.button('check').disabled,false);h.lifecycle.dispose();
});

test('a lost mutation response is never replayed and a GET can reveal that the original update completed',async()=>{
  let value=watch();
  const h=harness(call=>{
    if(call.method){value=watch('one',2,'paused');throw Object.assign(new Error('timeout'),{code:'WORKSPACE_REQUEST_TIMEOUT'});}
    return call.path==='/api/business-watches'?{items:[value],hasMore:false}:detail(value);
  });
  await h.controller.activate();await h.controller.select('one');await h.controller.act('pause');await h.controller.act('pause');
  assert.equal(h.calls.filter(c=>c.method).length,1);
  assert.match(h.$('#business-watch-detail').textContent,/不会自动重复操作/);
  await h.controller.refresh();assert.equal(h.calls.filter(c=>c.method).length,1);
  assert.equal(h.button('resume').disabled,false);h.lifecycle.dispose();
});

test('a stale list cannot roll back the version confirmed by a completed mutation',async()=>{
  const old=deferred();let lists=0;
  const h=harness(call=>call.path==='/api/business-watches'?(++lists===1?{items:[watch()],hasMore:false}:old.promise)
    :call.method?{watch:watch('one',3,'paused')}:detail(watch()));
  await h.controller.activate();await h.controller.select('one');
  const stale=h.controller.load();await h.controller.act('pause');
  old.resolve({items:[watch()],hasMore:false});await stale;
  assert.match(h.$('#business-watch-list').textContent,/已暂停/);assert.equal(h.button('resume').disabled,false);h.lifecycle.dispose();
});

test('late detail selection and disposed account reads cannot expose another row or account',async()=>{
  const delayed=deferred();
  const h=harness(call=>call.path==='/api/business-watches'?{items:[watch('one'),watch('two')],hasMore:false}
    :call.path.endsWith('/one')?delayed.promise:detail(watch('two')));
  await h.controller.activate();const first=h.controller.select('one');await h.controller.select('two');
  delayed.resolve(detail(watch('one')));await first;
  assert.match(h.$('#business-watch-detail').textContent,/合成关注two/);
  assert.doesNotMatch(h.$('#business-watch-detail').textContent,/合成关注one/);h.lifecycle.dispose();
  const pending=deferred(), next=harness(()=>pending.promise), read=next.controller.activate();
  next.replaceAccount();pending.resolve({items:[watch('secret-alice')],hasMore:false});await read;
  assert.doesNotMatch(next.$('#business-watch-list').textContent,/secret-alice/);next.lifecycle.dispose();
});

test('leaving during an update ignores its late receipt and re-entry reads before enabling new controls',async()=>{
  const pending=deferred();let value=watch();
  const h=harness(call=>call.method?pending.promise:call.path==='/api/business-watches'?{items:[value],hasMore:false}:detail(value));
  await h.controller.activate();await h.controller.select('one');const action=h.controller.act('pause');
  h.controller.deactivate();value=watch('one',2,'paused');pending.resolve({watch:value});await action;
  await h.controller.activate();
  assert.equal(h.button('resume').disabled,false);assert.equal(h.calls.filter(c=>c.method).length,1);h.lifecycle.dispose();
});

test('returning to the watch list and refreshing does not reopen the mobile detail overlay',async()=>{
  const h=harness(call=>call.path==='/api/business-watches'?{items:[watch()],hasMore:false}:detail(watch()));
  await h.controller.activate();await h.controller.select('one');
  const back=descendants(h.$('#business-watch-detail')).find(node=>node.textContent==='返回关注列表');
  back.events.click();await h.controller.refresh();
  assert.match(h.$('#business-watch-detail').className,/mobile-empty/);
  await h.controller.select('one');assert.doesNotMatch(h.$('#business-watch-detail').className,/mobile-empty/);h.lifecycle.dispose();
});

test('live task projections cannot switch a reader away from the watch tab',()=>{
  const source=readFileSync(new URL('../agentbridge/workspace/static/workspace.js',import.meta.url),'utf8');
  const start=source.indexOf('apply: detail => {'),end=source.indexOf('\n  },\n  retire:',start);
  assert.ok(start>=0&&end>start);
  const state={activeView:'tasks',taskPanel:'watches',selectedTaskId:'task-1',taskCards:new Map()};
  const selected=[];
  const apply=new Function('state','upsertTaskCard','enqueueActionableSources','projectionOrder','projectionQueue','loadTaskDetail',
    'return ('+source.slice(start+'apply: '.length,end+4)+');')(
      state,()=>{},()=>{}, {invalidate(){}},{enqueue(){}},id=>selected.push(id));
  apply({task:{task_id:'task-1'}});assert.deepEqual(selected,[]);
  state.taskPanel='records';apply({task:{task_id:'task-1'}});assert.deepEqual(selected,['task-1']);
});

test('permission loss clears cached title and state; session failures preserve only the prior observation without replay',async()=>{
  let failure=null;
  const h=harness(call=>{
    if(call.method)throw failure;
    return call.path==='/api/business-watches'?{items:[watch()],hasMore:false}:detail(watch());
  });
  await h.controller.activate();await h.controller.select('one');
  failure=Object.assign(new Error('session'),{status:401,code:'WATCH_SESSION_UNAVAILABLE'});
  await h.controller.act('check');assert.match(h.$('#business-watch-detail').textContent,/未取得新状态/);
  assert.match(h.$('#business-watch-detail').textContent,/合成关注one/);assert.equal(h.button('check').disabled,true);
  await h.controller.select('one',{preserve:true});
  failure=Object.assign(new Error('denied'),{status:403,code:'HTTP403_PERMISSION_DENIED'});
  await h.controller.act('check');assert.doesNotMatch(h.$('#business-watch-detail').textContent,/合成关注one/);
  assert.doesNotMatch(h.$('#business-watch-list').textContent,/合成关注one/);h.lifecycle.dispose();
});

test('paused and unknown states cannot trigger manual checks, and missing read consent is not disguised as a version conflict',async()=>{
  let value=watch('one',1,'paused');
  const h=harness(call=>{
    if(call.method)throw Object.assign(new Error('consent'),{status:409,code:'WATCH_READ_CONSENT_REQUIRED'});
    return call.path==='/api/business-watches'?{items:[value],hasMore:false}:detail(value);
  });
  await h.controller.activate();await h.controller.select('one');await h.controller.act('check');
  assert.equal(h.calls.filter(c=>c.method).length,0);
  value=watch('one',2,'active');await h.controller.select('one',{preserve:true});await h.controller.act('check');
  assert.match(h.$('#business-watch-detail').textContent,/尚未允许检查时标为已读/);
  value=watch('one',3,'unknown-future');await h.controller.select('one',{preserve:true});await h.controller.act('stop');
  assert.equal(h.calls.filter(c=>c.method).length,1);h.lifecycle.dispose();
});

test('the production request helper sends watch mutations with same-origin credentials and CSRF',async()=>{
  const document=createDocument();document.cookie='agentbridge_workspace_csrf=synthetic-csrf';
  const lifecycle=createLifecycle(),scope=lifecycle.scope(),calls=[];
  const {api}=createWorkspaceRequests({document,getScope:()=>scope,fetch:async(path,options)=>{
    calls.push({path,...options});return {ok:true,json:async()=>({watch:watch('one',2,'paused')})};
  }});
  await api('/api/business-watches/one/update',{scope,method:'POST',csrf:true,body:{expected_version:1,action:'pause'}});
  assert.equal(calls[0].credentials,'same-origin');assert.equal(calls[0].headers['X-AgentBridge-CSRF'],'synthetic-csrf');
  assert.deepEqual(JSON.parse(calls[0].body),{expected_version:1,action:'pause'});lifecycle.dispose();
});
