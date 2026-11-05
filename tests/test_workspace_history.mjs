import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createDocument, descendants} from './workspace_dom_fixture.mjs';
import {historyPresentation, loadHistoryPage, appendMessageImages} from '../agentbridge/workspace/static/workspace_history.mjs';

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
