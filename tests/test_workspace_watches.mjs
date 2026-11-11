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
const options = (extra={}) => ({enabled:true,endpoints:[{endpoint_id:'my-phone',client_type:'telegram',label:'我的合成手机'}],
  defaults:{interval_seconds:3600,expires_in_days:7,event_types:['nodes','handlers','ended']},readNotice:'检查可能将事项标为已读。',...extra});
const isList = call => !call.method && call.path.startsWith('/api/business-watches?');
const detail = value => ({watch:value,events:[{event_id:'created',event_type:'created',created_at:'2026-11-10T08:00:00Z',summary:'开始关注'}]});
function harness(handler) {
  const document=createDocument(),nodes=new Map(),lifecycle=createLifecycle();let scope=lifecycle.scope();
  const $=selector=>{if(!nodes.has(selector))nodes.set(selector,document.createElement('div'));return nodes.get(selector);};
  const calls=[];let keyCalls=0;
  const controller=createBusinessWatches({document,$,getScope:()=>scope,
    makeKey:()=>`synthetic-watch-key-${++keyCalls}`,
    request:(path,current,options={})=>{const call={path,scope:current,...options};calls.push(call);return handler(call,calls.length);}});
  const button=action=>descendants($('#business-watch-detail')).find(node=>node.dataset.watchAction===action);
  const field=(name,value)=>descendants($('#business-watch-detail')).find(node=>node.name===name&&(value===undefined||node.value===value));
  const filter = $('#business-watch-state-filter');
  return {document,$,nodes,calls,controller,lifecycle,button,field,filter,
    filterTo(state){filter.value=state;return filter.events.change();},get keyCalls(){return keyCalls;},get scope(){return scope;},
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

test('changing the filter resets paging and discards late list and detail responses', async () => {
  const nextPage = deferred(), oldDetail = deferred();
  const h = harness(call => {
    if (!isList(call)) return oldDetail.promise;
    const query = new URL(call.path, 'https://workspace.test').searchParams;
    if (query.has('cursor')) return nextPage.promise;
    if (query.get('state') === 'stopped') {
      return {items:[watch('historical', 2, 'stopped')], hasMore:false};
    }
    return {items:[watch('current')], hasMore:true, nextCursor:'active-page'};
  });
  await h.controller.activate();
  assert.equal(h.filter.value, 'active');
  assert.equal(h.calls[0].path, '/api/business-watches?state=active');
  const reading = h.controller.select('current');
  const paging = h.controller.load({append:true});
  assert.equal(h.calls.at(-1).path, '/api/business-watches?state=active&cursor=active-page');
  await h.filterTo('stopped');
  assert.equal(h.calls.at(-1).path, '/api/business-watches?state=stopped');
  oldDetail.resolve(detail(watch('current')));
  nextPage.resolve({items:[watch('late')],hasMore:true,nextCursor:'stale'});
  await Promise.all([reading, paging]);
  assert.match(h.$('#business-watch-list').textContent, /合成关注historical/);
  assert.doesNotMatch(h.$('#business-watch-list').textContent, /current|late/);
  assert.doesNotMatch(h.$('#business-watch-detail').textContent, /current/);
  assert.equal(h.$('#business-watch-more').hidden, true);
  assert.ok(h.calls.every(call => !call.method));
  h.lifecycle.dispose();
});

test('a stopped watch leaves the default list and remains readable in its history filter', async () => {
  let value = watch();
  const h = harness(call => {
    if (call.method) return {watch: (value = watch('one', 2, 'stopped'))};
    if (!isList(call)) return detail(value);
    const state = new URL(call.path, 'https://workspace.test').searchParams.get('state');
    return {items: state === value.state || state === 'all' ? [value] : [], hasMore:false};
  });
  await h.controller.activate();
  await h.controller.select('one');
  await h.controller.act('stop');
  assert.match(h.$('#business-watch-list-status').textContent, /暂时没有关注中/);
  assert.doesNotMatch(h.$('#business-watch-list').textContent, /合成关注one/);
  assert.match(h.$('#business-watch-detail').textContent, /「已停止」筛选/);
  const back = descendants(h.$('#business-watch-detail')).find(node => node.textContent === '返回关注列表');
  back.events.click();
  assert.equal(h.document.activeElement, h.filter);
  await h.filterTo('stopped');
  await h.controller.refresh();
  assert.equal(h.filter.value, 'stopped');
  assert.equal(h.calls.at(-1).path, '/api/business-watches?state=stopped');
  assert.match(h.$('#business-watch-list').textContent, /合成关注one/);
  await h.controller.select('one');
  assert.equal(h.button('resume').hidden, true);
  h.replaceAccount();
  await h.controller.activate();
  assert.equal(h.filter.value, 'active');
  assert.equal(h.calls.at(-1).path, '/api/business-watches?state=active');
  assert.doesNotMatch(h.$('#business-watch-list').textContent, /合成关注one/);
  assert.equal(h.calls.filter(call => call.method).length, 1);
  h.lifecycle.dispose();
});

test('a filtered empty page with a cursor offers continuation before reporting an empty result', async () => {
  const h = harness(call => call.path.includes('cursor=')
    ? {items:[watch('later')], hasMore:false}
    : {items:[], hasMore:true, nextCursor:'more'});
  await h.controller.activate();
  assert.match(h.$('#business-watch-list-status').textContent, /本页.*可继续加载/);
  await h.$('#business-watch-more').events.click();
  assert.match(h.$('#business-watch-list').textContent, /合成关注later/);
  h.lifecycle.dispose();
});

test('detail distinguishes missing fields, old observations and ended flow from business completion',async()=>{
  const value=watch('one',1,'completed',{snapshot:{workflow_state:'ended',current_nodes:[],current_handlers:[],
    observed_at:'2026-11-10T08:00:00Z',unavailable_fields:[]}});
  const h=harness(call=>isList(call)?{items:[value],hasMore:false}:detail(value));
  await h.controller.activate();await h.controller.select('one');
  const copy=h.$('#business-watch-detail').textContent;
  assert.match(copy,/无当前节点（流程已结束）/);assert.match(copy,/流程结束不代表款项已支付/);
  assert.match(copy,/不估算停留时长/);assert.equal(h.button('check').hidden,true);
  assert.equal(h.button('resume').hidden,true);h.lifecycle.dispose();
  const missing=watch('one',1,'active',{snapshot:{workflow_state:'running',current_nodes:[{name:'旧节点'}],
    current_handlers:['旧处理人'],unavailable_fields:['current_nodes','current_handlers']}});
  const m=harness(call=>isList(call)?{items:[missing],hasMore:false}:detail(missing));
  await m.controller.activate();await m.controller.select('one');
  assert.doesNotMatch(m.$('#business-watch-detail').textContent,/旧节点|旧处理人/);m.lifecycle.dispose();
});

test('pause and resume consume the current projected version once and leave OA actions absent',async()=>{
  const pending=deferred();let value=watch(),mutations=0;
  const h=harness(call=>call.method?(++mutations===1?pending.promise:{watch:watch('one',3,'active')}):isList(call)?{items:[value],hasMore:false}:detail(value));
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
  const event={event_id:'change-1',event_type:'watch.changed',observed_at:'2026-11-10T09:00:00Z',
    change_types:['nodes'],diff:{nodes:{before:['核对'],after:['复核']}}};
  const h=harness(call=>call.method?{watch:changed,events:[event,event,null]}
    :isList(call)?{items:[watch()],hasMore:false}:detail(watch()));
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
    :isList(call)?{items:[watch()],hasMore:false}:detail(watch()));
  await h.controller.activate();await h.controller.select('one');await h.controller.act('check');
  assert.match(h.$('#business-watch-detail').textContent,/未取得完整可信状态/);
  assert.match(h.$('#business-watch-detail').textContent,/流转中/);
  assert.doesNotMatch(h.$('#business-watch-detail').textContent,/本次核验已结束/);h.lifecycle.dispose();
});

test('a redacted pause receipt after permission loss clears all formerly readable material',async()=>{
  const h=harness(call=>call.method?{watch:{watch_id:'one',state:'paused',version:2}}
    :isList(call)?{items:[watch()],hasMore:false}:detail(watch()));
  await h.controller.activate();await h.controller.select('one');await h.controller.act('pause');
  assert.match(h.$('#business-watch-detail').textContent,/关注已暂停/);
  assert.doesNotMatch(h.$('#business-watch-detail').textContent,/合成关注one|核对（甲）/);
  assert.doesNotMatch(h.$('#business-watch-list').textContent,/合成关注one/);h.lifecycle.dispose();
});

test('version conflict blocks further mutations until a fresh GET establishes the new version',async()=>{
  let value=watch(), rejected=true;
  const h=harness(call=>{
    if(!call.method)return isList(call)?{items:[value],hasMore:false}:detail(value);
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
    return isList(call)?{items:[watch()],hasMore:false}:detail(watch());
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
    return isList(call)?{items:[value],hasMore:false}:detail(value);
  });
  await h.controller.activate();await h.controller.select('one');await h.controller.act('pause');await h.controller.act('pause');
  assert.equal(h.calls.filter(c=>c.method).length,1);
  assert.match(h.$('#business-watch-detail').textContent,/不会自动重复操作/);
  await h.controller.refresh();assert.equal(h.calls.filter(c=>c.method).length,1);
  assert.equal(h.button('resume').disabled,false);h.lifecycle.dispose();
});

test('a stale list cannot roll back the version confirmed by a completed mutation',async()=>{
  const old=deferred();let lists=0;
  const h=harness(call=>isList(call)?(++lists===1?{items:[watch()],hasMore:false}:old.promise)
    :call.method?{watch:watch('one',3,'paused')}:detail(watch()));
  await h.controller.activate();await h.controller.select('one');
  const stale=h.controller.load();await h.controller.act('pause');
  old.resolve({items:[watch()],hasMore:false});await stale;
  assert.doesNotMatch(h.$('#business-watch-list').textContent,/合成关注one/);assert.equal(h.button('resume').disabled,false);h.lifecycle.dispose();
});

test('late detail selection and disposed account reads cannot expose another row or account',async()=>{
  const delayed=deferred();
  const h=harness(call=>isList(call)?{items:[watch('one'),watch('two')],hasMore:false}
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
  const h=harness(call=>call.method?pending.promise:isList(call)?{items:[value],hasMore:false}:detail(value));
  await h.controller.activate();await h.controller.select('one');const action=h.controller.act('pause');
  h.controller.deactivate();value=watch('one',2,'paused');pending.resolve({watch:value});await action;
  await h.controller.activate();
  assert.equal(h.button('resume').disabled,false);assert.equal(h.calls.filter(c=>c.method).length,1);h.lifecycle.dispose();
});

test('returning to the watch list and refreshing does not reopen the mobile detail overlay',async()=>{
  const h=harness(call=>isList(call)?{items:[watch()],hasMore:false}:detail(watch()));
  await h.controller.activate();await h.controller.select('one');
  const back=descendants(h.$('#business-watch-detail')).find(node=>node.textContent==='返回关注列表');
  back.events.click();
  assert.equal(h.document.activeElement.dataset.watchId,'one');
  await h.controller.refresh();
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
    return isList(call)?{items:[watch()],hasMore:false}:detail(watch());
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
    return isList(call)?{items:[value],hasMore:false}:detail(value);
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

test('creating requires explicit read consent and submits the chosen owned options with one idempotency key',async()=>{
  const pending=deferred(),h=harness(call=>call.path.endsWith('/options')?options():call.method?pending.promise:{items:[],hasMore:false});
  await h.controller.activate();await h.controller.createFrom({title:'<img>合成来源',target_ref:'opaque-current-source'});
  assert.equal(h.field('allow_mark_read').checked,false);assert.equal(h.field('interval_seconds').value,'3600');
  assert.equal(h.field('expires_in_days').value,'7');assert.equal(h.field('endpoint_ids','my-phone').checked,false);
  await h.controller.submitEditor();assert.equal(h.calls.filter(c=>c.method).length,0);
  assert.match(h.$('#business-watch-detail').textContent,/请主动确认/);
  h.field('allow_mark_read').checked=true;h.field('interval_seconds').value='900';h.field('expires_in_days').value='30';
  h.field('event_types','handlers').checked=false;h.field('event_types','ended').checked=false;h.field('endpoint_ids','my-phone').checked=true;
  const submit=h.controller.submitEditor();await h.controller.submitEditor();
  const sent=h.calls.filter(c=>c.method);assert.equal(sent.length,1);assert.equal(sent[0].path,'/api/business-watches');
  assert.equal(sent[0].csrf,true);assert.deepEqual(sent[0].body,{target_ref:'opaque-current-source',idempotency_key:'synthetic-watch-key-1',
    interval_seconds:900,expires_in_days:30,event_types:['nodes'],endpoint_ids:['my-phone'],allow_mark_read:true});
  pending.resolve({watch:watch('new',1)});await submit;
  assert.match(h.$('#business-watch-detail').textContent,/已开始关注/);assert.equal(h.keyCalls,1);
  assert.equal(descendants(h.$('#business-watch-detail')).some(n=>n.tag==='img'),false);h.lifecycle.dispose();
});

test('creating from an empty list updates its count and retains the separate creation and check notices',async()=>{
  let value=watch('new'),posts=0;
  const h=harness(call=>call.path.endsWith('/options')?options():call.method
    ? {watch:++posts===1?value:(value={...value,version:2})}:{items:[],hasMore:false});
  await h.controller.activate();assert.equal(h.$('#business-watch-list-status').textContent,'暂时没有关注中的事项。');
  await h.filterTo('stopped');
  await h.controller.createFrom({title:'尚未关注的来源',target_ref:'opaque-source'});
  assert.equal(h.filter.value,'active');
  h.field('allow_mark_read').checked=true;await h.controller.submitEditor();
  const rows=descendants(h.$('#business-watch-list')).filter(node=>node.tag==='button');
  assert.equal(rows.length,1);assert.equal(rows[0].dataset.watchId,'new');
  assert.equal(h.$('#business-watch-list-status').textContent,'已显示 1 项。');
  assert.match(h.$('#business-watch-detail').textContent,/已开始关注；最新状态以读取时间为准。/);
  await h.controller.act('check');assert.equal(h.$('#business-watch-list-status').textContent,'已显示 1 项。');
  assert.match(h.$('#business-watch-detail').textContent,/本次核验已结束，流程状态以读取时间为准。/);
  assert.equal(posts,2);h.lifecycle.dispose();
});

test('creating a duplicate of a paused watch keeps it paused and points to its filter', async () => {
  const value = watch('existing', 3, 'paused');
  const h = harness(call => {
    if (call.path.endsWith('/options')) return options();
    if (call.method) return {watch:value,reused:true};
    if (isList(call)) return {items:call.path.endsWith('state=paused') ? [value] : [],hasMore:false};
    return detail(value);
  });
  await h.controller.activate();
  await h.controller.createFrom({title:'合成来源',target_ref:'opaque-source'});
  h.field('allow_mark_read').checked = true;
  await h.controller.submitEditor();
  assert.match(h.$('#business-watch-detail').textContent, /沿用现有设置.*「已暂停」筛选/);
  assert.doesNotMatch(h.$('#business-watch-detail').textContent, /已开始关注/);
  assert.equal(h.button('resume').disabled, false);
  assert.doesNotMatch(h.$('#business-watch-list').textContent, /合成关注existing/);
  await h.filterTo('paused');
  assert.match(h.$('#business-watch-list').textContent, /合成关注existing/);
  assert.equal(h.calls.filter(call => call.method).length, 1);
  h.lifecycle.dispose();
});

test('row updates preserve an in-flight list message and list failures while list reads retain detail conflicts',async()=>{
  const pending=deferred();let value=watch(),lists=0,mutations=0;
  const h=harness(call=>{
    if(call.method){
      if(++mutations===1)return {watch:(value=watch('one',2,'paused'))};
      throw Object.assign(new Error('changed'),{status:409,code:'WATCH_VERSION_CONFLICT'});
    }
    if(isList(call) && call.path.includes('cursor='))return pending.promise;
    if(isList(call))return {items:[value],hasMore:++lists===1,nextCursor:'next'};
    return detail(value);
  });
  await h.controller.activate();await h.controller.select('one');const loading=h.controller.load({append:true});
  await h.controller.act('pause');assert.equal(h.$('#business-watch-list-status').textContent,'正在加载更多…');
  pending.reject(new Error('offline'));await loading;const failure=h.$('#business-watch-list-status').textContent;
  assert.match(failure,/读取失败/);assert.equal(h.$('#business-watch-retry').hidden,false);
  await h.controller.select('one',{preserve:true});await h.controller.act('resume');
  assert.equal(h.$('#business-watch-list-status').textContent,failure);
  assert.match(h.$('#business-watch-detail').textContent,/刷新详情后再操作/);
  await h.controller.load();assert.equal(h.$('#business-watch-list-status').textContent,'暂时没有关注中的事项。');
  assert.match(h.$('#business-watch-detail').textContent,/刷新详情后再操作/);
  assert.equal(h.button('resume').disabled,true);h.lifecycle.dispose();
});

test('missing target references and disabled watch permission cannot create or request permission',async()=>{
  const h=harness(call=>call.path.endsWith('/options')?options({enabled:false,endpoints:[]}):{items:[],hasMore:false});
  await h.controller.activate();await h.controller.createFrom({title:'not a source',affair_id:'123',url:'https://example.test'});
  assert.equal(h.calls.length,1);
  await h.controller.createFrom({title:'合成来源',target_ref:'opaque'});await h.controller.submitEditor();
  assert.match(h.$('#business-watch-detail').textContent,/当前账号没有持续跟踪权限/);
  assert.equal(h.calls.filter(c=>c.method).length,0);assert.equal(h.field('allow_mark_read').checked,false);
  assert.ok(h.calls.every(c=>!/(grant|permission|authorize)/.test(c.path)));h.lifecycle.dispose();
});

test('expired source errors retain form choices but never replay a create, even after a list refresh',async()=>{
  const h=harness(call=>{
    if(call.path.endsWith('/options'))return options();
    if(call.method)throw Object.assign(new Error('来源引用已失效，请重新查询。'),{status:400,code:'WATCH_REFERENCE_INVALID'});
    return {items:[],hasMore:false};
  });
  await h.controller.activate();await h.controller.createFrom({title:'合成来源',target_ref:'expired-opaque'});
  h.field('allow_mark_read').checked=true;h.field('interval_seconds').value='14400';h.field('expires_in_days').value='12';
  await h.controller.submitEditor();await h.controller.submitEditor();await h.controller.refreshEditor();await h.controller.submitEditor();
  assert.equal(h.calls.filter(c=>c.method).length,1);assert.equal(h.keyCalls,1);
  assert.equal(h.field('interval_seconds').value,'14400');assert.equal(h.field('expires_in_days').value,'12');
  assert.equal(h.field('allow_mark_read').checked,true);assert.match(h.$('#business-watch-detail').textContent,/不会重复提交/);
  h.controller.closeEditor();await h.controller.createFrom({title:'新查询',target_ref:'new-opaque'});
  assert.equal(h.field('allow_mark_read').checked,false);assert.equal(h.keyCalls,2);h.lifecycle.dispose();
});

test('configure conflicts preserve drafts and require a fresh version before an explicit save',async()=>{
  let value=watch('one',1,'active',{interval_seconds:3600,event_types:['nodes','ended'],endpoint_ids:[],allow_mark_read:true}),posts=0;
  const h=harness(call=>{
    if(call.path.endsWith('/options'))return options();
    if(!call.method)return isList(call)?{items:[value],hasMore:false}:detail(value);
    if(++posts===1){value={...value,version:4};throw Object.assign(new Error('changed'),{status:409,code:'WATCH_VERSION_CONFLICT'});}
    return {watch:{...value,version:5,interval_seconds:900}};
  });
  await h.controller.activate();await h.controller.select('one');await h.controller.openSettings();
  assert.equal(h.field('allow_mark_read').checked,true);assert.equal(h.field('expires_in_days').value,'');
  h.field('interval_seconds').value='900';h.field('expires_in_days').value='9';h.field('endpoint_ids','my-phone').checked=true;
  await h.controller.submitEditor();await h.controller.submitEditor();
  assert.equal(posts,1);assert.equal(h.field('interval_seconds').value,'900');assert.equal(h.field('expires_in_days').value,'9');
  await h.controller.refreshEditor();assert.equal(posts,1);assert.equal(h.field('endpoint_ids','my-phone').checked,true);
  assert.match(h.$('#business-watch-detail').textContent,/保留了你的编辑内容/);
  await h.controller.submitEditor();assert.equal(posts,2);
  assert.deepEqual(h.calls.filter(c=>c.method).at(-1).body,{expected_version:4,action:'configure',interval_seconds:900,
    expires_in_days:9,event_types:['nodes','ended'],endpoint_ids:['my-phone'],allow_mark_read:true});h.lifecycle.dispose();
});

test('unknown configure outcomes retain choices, never resend on refresh, and preserve expiry when blank',async()=>{
  let value=watch('one',1,'active',{interval_seconds:3600,event_types:['nodes'],endpoint_ids:[],allow_mark_read:true});
  const h=harness(call=>{
    if(call.path.endsWith('/options'))return options();
    if(call.method){value={...value,version:2,interval_seconds:86400};throw Object.assign(new Error('timeout'),{code:'WORKSPACE_REQUEST_TIMEOUT'});}
    return isList(call)?{items:[value],hasMore:false}:detail(value);
  });
  await h.controller.activate();await h.controller.select('one');await h.controller.openSettings();h.field('interval_seconds').value='86400';
  await h.controller.submitEditor();await h.controller.submitEditor();await h.controller.refresh();
  const posts=h.calls.filter(c=>c.method);assert.equal(posts.length,1);assert.equal(Object.hasOwn(posts[0].body,'expires_in_days'),false);
  assert.equal(h.field('interval_seconds').value,'86400');assert.equal(h.field('expires_in_days').value,'');
  assert.match(h.$('#business-watch-detail').textContent,/保留了你的编辑内容/);h.lifecycle.dispose();
});

test('form rejects invalid terms and empty conditions before mutation and only uses current options endpoints',async()=>{
  const h=harness(call=>call.path.endsWith('/options')?options({endpoints:[{endpoint_id:'safe',label:'<img onerror=x>'+'端点'.repeat(80)}]}):{items:[],hasMore:false});
  await h.controller.activate();await h.controller.createFrom({title:'合成来源',target_ref:'opaque'});h.field('allow_mark_read').checked=true;
  for(const days of ['0','31','1.5','NaN','']){h.field('expires_in_days').value=days;await h.controller.submitEditor();}
  h.field('expires_in_days').value='7';for(const rule of ['nodes','handlers','ended'])h.field('event_types',rule).checked=false;
  await h.controller.submitEditor();assert.equal(h.calls.filter(c=>c.method).length,0);
  assert.match(h.$('#business-watch-detail').textContent,/至少选择一种/);
  assert.equal(descendants(h.$('#business-watch-detail')).some(n=>n.tag==='img'),false);
  assert.equal(h.field('endpoint_ids','safe').checked,false);h.lifecycle.dispose();
});

test('account reset disposes late options and creation receipts and clears every editable source and endpoint',async()=>{
  const delayed=deferred(),h=harness(call=>call.path.endsWith('/options')?delayed.promise:{items:[],hasMore:false});
  await h.controller.activate();const opening=h.controller.createFrom({title:'Alice-private-title',target_ref:'Alice-private-source'});
  h.replaceAccount();delayed.resolve(options({endpoints:[{endpoint_id:'Alice-endpoint',label:'Alice-private-endpoint'}]}));await opening;
  assert.doesNotMatch(h.$('#business-watch-detail').textContent,/Alice/);assert.equal(h.field('allow_mark_read').checked,false);
  assert.equal(h.field('expires_in_days').value,'');assert.equal(h.field('endpoint_ids','Alice-endpoint'),undefined);h.lifecycle.dispose();
  const receipt=deferred(),next=harness(call=>call.path.endsWith('/options')?options():call.method?receipt.promise:{items:[],hasMore:false});
  await next.controller.activate();await next.controller.createFrom({title:'Alice-private-title',target_ref:'Alice-source'});
  next.field('allow_mark_read').checked=true;const creating=next.controller.submitEditor();next.replaceAccount();
  receipt.resolve({watch:watch('Alice-private-result')});await creating;
  assert.doesNotMatch(next.$('#business-watch-detail').textContent,/Alice/);assert.doesNotMatch(next.$('#business-watch-list').textContent,/Alice/);next.lifecycle.dispose();
});

test('only confirmed change diffs produce bounded observed history; failed or unchanged checks create no change copy',async()=>{
  const events=[{event_id:'fake',event_type:'checked',summary:'错误地声称完成',created_at:'2026-11-10T10:00:00Z'},
    {event_id:'invalid',event_type:'watch.changed',summary:'无证据摘要',diff:{nodes:{before:['A'],after:['A']}}},
    ...Array.from({length:103},(_,i)=>({event_id:'change-'+i,event_type:'watch.changed',observed_at:'2026-11-10T08:00:00Z',
      created_at:'2026-11-10T09:00:00Z',diff:{nodes:{before:['节点'+i],after:['新节点'+i]}}}))];
  const h=harness(call=>isList(call)?{items:[watch()],hasMore:false}:{watch:watch(),events});
  await h.controller.activate();await h.controller.select('one');
  const copy=h.$('#business-watch-detail').textContent;assert.match(copy,/最多 100 条/);assert.doesNotMatch(copy,/错误地声称完成|无证据摘要/);
  const records=descendants(h.$('#business-watch-detail')).filter(n=>n.tag==='li');assert.equal(records.length,100);
  assert.equal(records.some(n=>n.textContent.includes('新节点102')),false);
  assert.match(copy,/2026\/11\/10 16:00:00/);h.lifecycle.dispose();
});

test('maintenance pause keeps the saved snapshot but prevents check, resume and configure while pause and stop remain available',async()=>{
  let value=watch('one',1,'active',{background_paused:true,next_check_at:null});
  const h=harness(call=>call.method?{watch:{...value,version:2,state:call.body.action==='pause'?'paused':'stopped'}}
    :isList(call)?{items:[value],hasMore:false}:detail(value));
  await h.controller.activate();await h.controller.select('one');
  assert.match(h.$('#business-watch-detail').textContent,/维护暂停中/);assert.match(h.$('#business-watch-detail').textContent,/核对（甲）/);
  assert.equal(h.button('check').disabled,true);await h.controller.act('check');await h.controller.openSettings();
  assert.equal(h.calls.filter(c=>c.method).length,0);assert.equal(h.calls.some(c=>c.path.endsWith('/options')),false);
  assert.equal(h.button('pause').disabled,false);await h.controller.act('pause');
  assert.equal(h.button('resume').disabled,true);await h.controller.act('resume');assert.equal(h.calls.filter(c=>c.method).length,1);
  await h.controller.act('stop');assert.equal(h.calls.filter(c=>c.method).length,2);h.lifecycle.dispose();
});
