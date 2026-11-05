import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createDocument, descendants} from './workspace_dom_fixture.mjs';
import {historyPresentation, loadHistoryPage, applyHistoryPage, appendMessageImages} from '../agentbridge/workspace/static/workspace_history.mjs';
import {createProjectionOrder} from '../agentbridge/workspace/static/workspace_projection.mjs';

test('a unified page applies text and cards before advancing its shared cursor without moving SSE replay', () => {
  const cursor={createdAt:'2026-11-05T00:00:00+00:00',kind:'message',id:'older'};
  const state={historyCursor:null,timelineCursor:42,localMessages:new Map([['draft','unsent']])};
  const calls=[];
  applyHistoryPage(state,{schemaVersion:'agentbridge.workspace-history.v1',
    cards:[{task:{task_id:'task'}}],timeline:{items:[{entry_type:'chat_message',entry_id:'text'}]},
    unavailableTaskIds:['revoked'],hasMore:true,nextCursor:cursor},
    {upsert(detail,options){assert.equal(state.historyCursor,null);assert.equal(options.render,false);calls.push(detail.task.task_id);},
      ingest(entry,render,advance){assert.equal(state.historyCursor,null);assert.equal(render,false);assert.equal(advance,false);calls.push(entry.entry_id);},
      retire:id=>calls.push(id)});
  assert.deepEqual(calls,['task','text','revoked']);
  assert.deepEqual(state.historyCursor,cursor);assert.equal(state.historyHasMore,true);
  assert.equal(state.timelineCursor,42);assert.equal(state.localMessages.get('draft'),'unsent');
});

test('incomplete or stalled unified pages cannot append orphan text or advance the reading window', () => {
  const cursor={createdAt:'2026-11-05T00:00:00+00:00',kind:'task',id:'same'};
  const state={historyCursor:cursor,historyHasMore:true};let changes=0;
  const callbacks={upsert(){changes++;},ingest(){changes++;},retire(){changes++;}};
  const page={schemaVersion:'agentbridge.workspace-history.v1',cards:[],
    timeline:{items:[{entry_type:'chat_message',entry_id:'text'}]},unavailableTaskIds:[],hasMore:true,nextCursor:cursor};
  for (const invalid of [page,{...page,cards:[{}],nextCursor:{...cursor,id:'different'}},
      {...page,nextCursor:null},{...page,timeline:{items:Array(33).fill(page.timeline.items[0])},hasMore:false}]) {
    assert.throws(()=>applyHistoryPage(state,invalid,callbacks),/响应不完整/);
    assert.equal(changes,0);assert.equal(state.historyCursor,cursor);assert.equal(state.historyHasMore,true);
  }
});

test('a newer unavailable projection prevents an old history page from reviving the card while its cursor progresses', () => {
  const state={},order=createProjectionOrder(),cards=new Map();
  const ticket=order.begin();order.invalidate('revoked');
  applyHistoryPage(state,{schemaVersion:'agentbridge.workspace-history.v1',cards:[{task:{task_id:'revoked'}}],
    timeline:{items:[]},unavailableTaskIds:[],hasMore:false,nextCursor:null},
    {upsert:detail=>{if(order.accept(detail.task.task_id,ticket))cards.set(detail.task.task_id,detail);},ingest(){},retire(){}});
  assert.equal(cards.size,0);assert.equal(state.historyHasMore,false);
});

test('unified history states distinguish automatic loading, recovery, failure and exhaustion', () => {
  const state={unifiedHistory:true,historyHasMore:true,legacyHistoryLoaded:false};
  assert.match(historyPresentation(state).notice,/向上翻阅.*自动加载/);
  state.historyRestoring=true;assert.equal(historyPresentation(state).busy,true);
  assert.match(historyPresentation(state).notice,/恢复上次/);
  state.historyRestoring=false;state.historyError='网络暂不可用';
  assert.equal(historyPresentation(state).label,'重试加载历史');
  assert.equal(historyPresentation(state).notice,'网络暂不可用');
  state.historyError='';state.historyHasMore=false;state.legacyHistoryLoaded=true;
  assert.equal(historyPresentation(state).pending,false);assert.match(historyPresentation(state).notice,/加载完毕/);
});

test('one history action pages each pending source, keeping a failed source retryable', async () => {
  const state = {chatTimelineHasOlder:true, taskHistoryHasMore:true, legacyHistoryLoaded:false};
  const calls = [], scope = {assertCurrent(){}, current:()=>true};
  const options = {state,getScope:()=>scope,render(){},
    loadMessages:async()=>{calls.push('messages');state.chatTimelineHasOlder=false;},
    loadTasks:async()=>{calls.push('tasks');}, // Existing handler retains its cursor on failure.
    loadLegacy:async()=>{calls.push('legacy');state.legacyHistoryLoaded=true;}};
  await loadHistoryPage(options);
  assert.deepEqual(calls,['messages','tasks','legacy']);
  assert.equal(historyPresentation(state).pending,true);
  assert.match(historyPresentation(state).notice,/最近120条文字.*不代表全部历史/);
  calls.length=0;
  options.loadTasks=async()=>{calls.push('tasks');state.taskHistoryHasMore=false;};
  await loadHistoryPage(options);
  assert.deepEqual(calls,['tasks']);
  assert.equal(historyPresentation(state).pending,false);
  assert.match(historyPresentation(state).notice,/可分页读取.*加载完毕/);
});

test('history actions fence account switches and prevent duplicate concurrent reads', async () => {
  const state={chatTimelineHasOlder:true,taskHistoryHasMore:true,legacyHistoryLoaded:false};
  let release, current=true, tasks=0;
  const scope={assertCurrent(){if(!current)throw new Error('account changed');},current:()=>current};
  const opts={state,getScope:()=>scope,render(){},loadMessages:()=>new Promise(done=>release=done),
    loadTasks:async()=>{tasks++;},loadLegacy:async()=>{}};
  const first=loadHistoryPage(opts);
  await loadHistoryPage(opts);
  assert.equal(tasks,0);
  current=false; release();
  await assert.rejects(first,/account changed/);
  assert.equal(tasks,0);
});

function imageFixture(image) {
  const document=createDocument(),container=document.createElement('article'),timers=[],opened=[];
  let clock=Date.parse('2026-11-05T04:00:00Z');
  appendMessageImages({document,container,images:[image],formatTime:value=>value,
    onOpen:value=>opened.push(value),scope:{timeout:(fn,ms)=>timers.push({fn,ms})},now:()=>clock});
  const all=descendants(container);
  return {container,button:all.find(n=>n.tag==='button'),img:all.find(n=>n.tag==='img'),timers,opened,
    expire(){clock=Date.parse('2026-11-05T04:00:03Z');timers[0].fn();}};
}

test('image retention is visible and expires in place without another media request', () => {
  const f=imageFixture({mediaUrl:'/api/timeline/attachments/synthetic',fileName:'evidence.png',expiresAt:'2026-11-05T04:00:02Z'});
  assert.match(f.container.textContent,/原图保留至/);
  f.button.events.click();assert.equal(f.opened.length,1);
  f.expire();
  assert.equal(f.button.disabled,true);
  assert.match(f.container.textContent,/原图已过期.*历史消息仍保留/);
  f.button.events.click();assert.equal(f.opened.length,1);
});

test('expired and missing media remain named records, while network failure is not called expiry', () => {
  for(const state of ['expired','unavailable']) {
    const f=imageFixture({state,fileName:'original.png',mediaUrl:'/private-media'});
    assert.equal(f.button.disabled,true);assert.match(f.container.textContent,/original.png/);
    assert.equal(f.img,undefined);
  }
  const f=imageFixture({fileName:'retry.png',mediaUrl:'/private-media'});
  f.img.events.error();
  assert.match(f.container.textContent,/暂时无法加载/);
  assert.doesNotMatch(f.container.textContent,/已过期/);
});

test('download renewal notice uses the returned entry deadline rather than a fixed lifetime', async () => {
  const source=readFileSync(new URL('../agentbridge/workspace/static/workspace.js',import.meta.url),'utf8');
  const start=source.indexOf('async function reissueArtifact('),end=source.indexOf('\n}',start)+2;
  const notices=[],scope={assertCurrent(){}};
  const artifact={artifact_id:'artifact',state:'ready',download_url:'/safe-download',expires_at:'ten-minute-deadline'};
  const bindings={state:{selectedTaskId:null},sessionScope:scope,
    scopedApi:()=>async()=>({artifacts:[artifact]}),projectionOrder:{begin:()=>1,accept:()=>true},
    upsertTaskCard(){},renderTaskDetail(){},scheduleTaskSync(){},toast:text=>notices.push(text),
    formatTime:value=>value,isCancellation:()=>false,friendlyError:error=>error.message};
  const renew=new Function(...Object.keys(bindings),source.slice(start,end)+';return reissueArtifact;')(...Object.values(bindings));
  const button={disabled:false,isConnected:true,textContent:'续取原报告下载'};
  await renew('task','artifact',button);
  assert.match(notices[0],/ten-minute-deadline/);
  assert.doesNotMatch(notices[0],/30 分钟/);
  assert.equal(button.textContent,'续取原报告下载');
});
