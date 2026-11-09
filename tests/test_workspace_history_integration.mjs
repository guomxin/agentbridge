import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createDocument} from './workspace_dom_fixture.mjs';
import {applyHistoryPage, historyPresentation, loadHistoryPage} from '../agentbridge/workspace/static/workspace_history.mjs';
import {createLifecycle, isCancellation} from '../agentbridge/workspace/static/workspace_lifecycle.mjs';
import {createReadingScroll, timelineNodeMatchesKey} from '../agentbridge/workspace/static/workspace_navigation.mjs';
import {createProjectionOrder, applyCurrentSnapshot} from '../agentbridge/workspace/static/workspace_projection.mjs';
import {restoreHistoryAnchor} from '../agentbridge/workspace/static/workspace_history_autoload.mjs';
import {createResultView, historyMessageKey, parseTimestampMilliseconds, textHash} from '../agentbridge/workspace/static/workspace_results.mjs';

const source = readFileSync(new URL('../agentbridge/workspace/static/workspace.js', import.meta.url), 'utf8');
function implementation(name, async = true) {
  const start = source.indexOf(`${async ? 'async ' : ''}function ${name}(`);
  assert.notEqual(start, -1, `missing integration function ${name}`);
  const end = source.indexOf('\n}', start);
  assert.notEqual(end, -1);
  return source.slice(start, end + 2);
}
const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return {promise, resolve, reject};
}
const cursor = id => ({createdAt:'2026-11-05T00:00:00+00:00', kind:'message', id});
const page = ({messages = [], cards = [], hasMore = true, id = 'older'} = {}) => ({
  schemaVersion:'agentbridge.workspace-history.v1',
  timeline:{items:messages.map(entry_id => ({entry_type:'chat_message', entry_id, text:entry_id, role:'assistant'}))},
  cards:cards.map(task_id => ({task:{task_id}})), unavailableTaskIds:[], hasMore,
  nextCursor:hasMore ? cursor(id) : null,
});

// Exercise the actual page and refresh handlers. Only transport and DOM row
// rendering are replaced; the shared page validation, ordering and reading
// anchor calculations use their production implementations.
function harness({initialReading = null, realSkills = false, realHistoryGroups = false} = {}) {
  const document = createDocument(), container = document.createElement('main');
  const notice = document.createElement('button'), existing = document.createElement('article');
  existing.dataset.timelineKey = 'existing'; existing.offsetTop = 200; container.append(existing);
  Object.assign(container, {scrollTop:300, scrollHeight:3000, clientHeight:600});
  const draft = {value:'尚未发送的输入草稿'};
  const state = {unifiedHistory:true, historyHasMore:true, historyCursor:cursor('initial'),
    historyLoading:false, historyRestoring:false, historyError:'', legacyHistoryLoaded:true,
    legacyHistoryLoading:false, currentLoaded:true, timelineCursor:70,
    localMessages:new Map([['draft', draft.value]]), syncedMessages:new Map(), historyMessages:new Map(),
    account:{accountId:'alice'}, skillCards:new Map(), recentSkillEvents:[], skillHistoryRead:null, queryGroupOpen:new Map()};
  const cards = new Map(), timeline = new Map(), renders = [], restores = [], requests = [], errors = [], timings = [];
  const lifecycle = createLifecycle(), scope = lifecycle.scope();
  const reading = createReadingScroll({container, notice});
  const {groupQueryCards, restoreHistoryCardState} = createResultView({document,state,openTask(){}});
  container.events.scroll();
  const readingScroll = {snapshot:() => reading.snapshot(), restore(value) { restores.push(value); reading.restore(value); }};
  const renderChatTimeline = () => {
    let rows = [...cards.values(), ...timeline.values(), ...state.historyMessages.values(), ...state.skillCards.values()];
    if (realHistoryGroups) rows = groupQueryCards(rows.sort((a,b) => Number(a.dataset.timelineAt || 0)-Number(b.dataset.timelineAt || 0)));
    rows.forEach((node, index) => { node.offsetTop = 80 + index * 40; });
    existing.offsetTop = 200 + rows.length * 40;
    container.scrollHeight = 3000 + rows.length * 40;
    container.replaceChildren(...rows, existing);
    if (realHistoryGroups) restoreHistoryCardState(container);
    renders.push({cards:cards.size, messages:timeline.size, loading:state.historyLoading,
      restoring:state.historyRestoring, cursor:state.historyCursor});
  };
  const bindings = {state, sessionScope:scope, readingScroll, initialReading,
    projectionOrder:createProjectionOrder(), applyHistoryPage, historyPresentation, loadHistoryPage,
    applyCurrentSnapshot, restoreHistoryAnchor, timelineNodeMatchesKey, isCancellation,
    scopedApi:requestScope => path => {
      const response = deferred(); requests.push({path, scope:requestScope, ...response}); return response.promise;
    },
    renderChatTimeline,
    upsertTaskCard(detail, options) {
      assert.equal(options.render, false);
      const node = document.createElement('article'); node.dataset.timelineKey = `task:${detail.task.task_id}`;
      if (realHistoryGroups && detail.oaHistoryQuery) Object.assign(node.dataset, {
        taskId:detail.task.task_id, timelineAt:parseTimestampMilliseconds(detail.task.created_at),
        oaHistoryQueryId:detail.oaHistoryQuery.query_id, oaHistoryScope:state.account.accountId,
        oaHistoryFoldable:String(detail.oaHistoryGrouping?.eligible === true), queryStatus:detail.task.status,
      });
      cards.set(detail.task.task_id, node);
    },
    ingestTimelineEntry(entry, render, advance) {
      assert.equal(render, false); assert.equal(advance, false);
      const node = document.createElement('article'); node.dataset.timelineKey = `timeline:${entry.entry_id}`;
      timeline.set(entry.entry_id, node);
    },
    retireTaskCard:id => cards.delete(id),
    friendlyError:error => error.message,
    toast:message => errors.push(message),
    loadOlderChatMessages:() => assert.fail('separate message paging must not be used'),
    loadTaskHistory:() => assert.fail('separate task paging must not be used'),
    $:selector => selector === '#chat-messages' ? container : draft,
    messageElement(message) { const node = document.createElement('article'); node.textContent = message.text; return node; },
    historyMessageKey, parseTimestampMilliseconds, textHash,
    hydrateSkillCards:async() => {}, addVisibleSkillCards:() => false,
    renderSkillCard(event) {
      const node = document.createElement('article'); node.dataset.timelineKey = `skill:${event.event_id}`;
      return node;
    },
    dismissTerminalLiveMessages() {}, restoreActiveDispatches() {},
    markWorkspaceTiming:name => timings.push(name),
  };
  const implementations = [implementation('setTimelineNode', false), implementation('loadLegacyHistory'),
    implementation('loadMoreHistory'), implementation('loadChat'), implementation('reconcileTimeline')];
  if (realSkills) {
    delete bindings.hydrateSkillCards; delete bindings.addVisibleSkillCards;
    implementations.push(implementation('hydrateSkillCards'), implementation('addVisibleSkillCards', false));
  }
  const functions = new Function(...Object.keys(bindings), implementations.join('\n') +
    ';return {loadMoreHistory,loadChat,reconcileTimeline,hydrateSkillCards,replaceAccount(scope,scroll){sessionScope=scope;readingScroll=scroll;}};')(...Object.values(bindings));
  return {state, scope, lifecycle, cards, timeline, renders, restores, requests, errors, timings, draft,
    container, document, existing, ...functions,
    scroll(top) { container.scrollTop = top; container.events.scroll(); },
  };
}

test('deferred unified history preserves user movement during the request and renders one complete text/card page', async () => {
  const h = harness(), originalCursor = h.state.historyCursor;
  const loading = h.loadMoreHistory();
  assert.equal(h.requests.length, 1);
  const query = new URL(h.requests[0].path, 'https://workspace.invalid').searchParams;
  assert.equal(query.get('limit'), '32');
  assert.equal(query.get('before_id'), originalCursor.id);
  assert.equal(h.state.historyLoading, true);
  assert.equal(await h.loadMoreHistory(), false, 'a second history request must not overlap');
  assert.equal(h.requests.length, 1);
  h.scroll(125);
  h.requests[0].resolve(page({messages:Array.from({length:16}, (_, i) => `message-${i}`),
    cards:Array.from({length:16}, (_, i) => `card-${i}`)}));
  assert.equal(await loading, true);
  assert.equal(h.cards.size, 16); assert.equal(h.timeline.size, 16);
  assert.ok(h.renders.every(view => (view.cards === 0 && view.messages === 0) ||
    (view.cards === 16 && view.messages === 16)), 'no intermediate render may show orphan text or missing cards');
  assert.equal(h.restores.at(-1).top, 125, 'use the position immediately before insertion, not the request start');
  assert.equal(h.restores.at(-1).offset, -75);
  assert.equal(h.container.scrollTop, 125 + 32 * 40);
  assert.deepEqual(h.state.historyCursor, cursor('older'));
  assert.equal(h.state.historyLoading, false); assert.equal(h.state.timelineCursor, 70);
  assert.equal(h.draft.value, '尚未发送的输入草稿');
  assert.equal(h.state.localMessages.get('draft'), h.draft.value);
});

test('failed requests retain the shared cursor and draft, respect subsequent scrolling, and retry the same page', async () => {
  const h = harness(), originalCursor = h.state.historyCursor;
  const loading = h.loadMoreHistory(); h.scroll(40);
  h.requests[0].reject(new Error('network unavailable'));
  assert.equal(await loading, false);
  assert.equal(h.state.historyCursor, originalCursor); assert.equal(h.state.historyHasMore, true);
  assert.equal(h.cards.size, 0); assert.equal(h.timeline.size, 0);
  assert.match(h.state.historyError, /network unavailable.*重试/);
  assert.equal(h.container.scrollTop, 40); assert.equal(h.restores.at(-1).top, 40);
  assert.equal(h.draft.value, '尚未发送的输入草稿');
  const retry = h.loadMoreHistory();
  assert.equal(h.requests[1].path, h.requests[0].path);
  h.requests[1].resolve(page({messages:['recovered-text'], cards:['recovered-card']}));
  assert.equal(await retry, true); assert.equal(h.state.historyError, '');
  assert.equal(h.cards.size, 1); assert.equal(h.timeline.size, 1);
});

test('an incomplete page cannot display partial rows or consume its history cursor', async () => {
  const h = harness(), originalCursor = h.state.historyCursor;
  const loading = h.loadMoreHistory();
  h.requests[0].resolve({...page({messages:['must-not-appear']}), cards:[{}]});
  assert.equal(await loading, false);
  assert.equal(h.timeline.size, 0); assert.equal(h.cards.size, 0);
  assert.equal(h.state.historyCursor, originalCursor); assert.equal(h.state.historyHasMore, true);
  assert.match(h.state.historyError, /响应不完整/);
  assert.equal(h.draft.value, '尚未发送的输入草稿');
});

test('an old account response cannot apply rows, errors or reading restoration to a new account', async () => {
  const h = harness(), oldRead = h.loadMoreHistory();
  h.scope.dispose();
  const nextScope = h.lifecycle.scope(), nextRestores = [];
  h.replaceAccount(nextScope, {snapshot:() => ({top:91, anchor:'new-account', atBottom:false}),
    restore:value => nextRestores.push(value)});
  const nextCursor = cursor('new-account');
  Object.assign(h.state, {historyCursor:nextCursor, historyLoading:false, historyError:'new-account-state'});
  h.draft.value = '新账号的草稿';
  h.requests[0].resolve(page({messages:['old-private-text'], cards:['old-private-card']}));
  assert.equal(await oldRead, false);
  assert.equal(h.timeline.size, 0); assert.equal(h.cards.size, 0);
  assert.equal(h.state.historyCursor, nextCursor); assert.equal(h.state.historyLoading, false);
  assert.equal(h.state.historyError, 'new-account-state');
  assert.deepEqual(nextRestores, []); assert.deepEqual(h.restores, []);
  assert.equal(h.draft.value, '新账号的草稿');
});

test('legacy text supplementation also preserves scrolling performed while its network request is pending', async () => {
  const h = harness();
  Object.assign(h.state, {historyHasMore:false, historyCursor:null, legacyHistoryLoaded:false});
  const loading = h.loadMoreHistory();
  assert.equal(h.requests[0].path, '/api/chat/history?limit=120');
  h.scroll(25);
  h.requests[0].resolve({messages:[{role:'assistant', text:'legacy text', timestamp:'2026-10-01T00:00:00+00:00'}]});
  assert.equal(await loading, true);
  assert.equal(h.state.historyMessages.size, 1); assert.equal(h.state.legacyHistoryLoaded, true);
  assert.equal(h.container.scrollTop, 25 + 40);
  assert.equal(h.state.historyLoading, false); assert.equal(h.draft.value, '尚未发送的输入草稿');
});

test('refresh loads the saved reading window without manual input and fences another current refresh while restoring', async () => {
  const reading = {top:900, atBottom:false, anchor:'timeline:older-anchor', offset:12};
  const h = harness({initialReading:reading});
  Object.assign(h.state, {currentLoaded:false, unifiedHistory:false});
  const refreshing = h.loadChat();
  assert.match(h.requests[0].path, /\/api\/current\?.*history_limit=32/);
  h.requests[0].resolve({schemaVersion:'agentbridge.workspace-current.v1', cards:[], unavailableTaskIds:[],
    timeline:{items:[{entry_type:'chat_message', entry_id:'newest', text:'newest'}], oldestSequence:70},
    history:{schemaVersion:'agentbridge.workspace-history.v1', hasMore:true, nextCursor:cursor('seed')},
    cursors:{timeline:70}, dispatches:{items:[]}});
  await flush();
  assert.equal(h.state.historyRestoring, true);
  assert.equal(h.requests.length, 2); assert.match(h.requests[1].path, /\/api\/history\?/);
  assert.equal(historyPresentation(h.state).busy, true);
  await h.loadChat(); assert.equal(h.requests.length, 2, 'do not replace the active restoration scope');
  h.requests[1].resolve(page({messages:['older-anchor'], cards:['paired-query'], id:'restored-window'}));
  await refreshing;
  assert.equal(h.state.historyRestoring, false); assert.equal(h.state.currentLoaded, true);
  assert.deepEqual(h.restores.at(-1), reading);
  assert.equal(h.container.scrollTop, h.timeline.get('older-anchor').offsetTop + reading.offset);
  assert.equal(h.cards.has('paired-query'), true); assert.deepEqual(h.state.historyCursor, cursor('restored-window'));
  assert.equal(h.draft.value, '尚未发送的输入草稿'); assert.deepEqual(h.errors, []);
  assert.ok(h.timings.includes('chat_loaded'));
});

const historyCard = (id, ordinal, continuation = true) => ({
  task:{task_id:id,status:'succeeded',created_at:`2026-11-09T01:00:${String(ordinal).padStart(2,'0')}Z`},
  oaHistoryQuery:{query_id:'saved-query'}, oaHistoryGrouping:continuation ? {queryId:'saved-query',eligible:true} : undefined,
});
const historyCurrent = cards => ({schemaVersion:'agentbridge.workspace-current.v1',cards,unavailableTaskIds:[],
  timeline:{items:[],oldestSequence:70},
  history:{schemaVersion:'agentbridge.workspace-history.v1',hasMore:true,nextCursor:cursor('recent')},
  cursors:{timeline:70},dispatches:{items:[]}});

test('refresh restores a saved continuation task alias from the current grouped query without redundant history reads', async () => {
  const saved={top:900,atBottom:false,anchor:'task:saved-next',offset:17};
  const h=harness({initialReading:saved,realHistoryGroups:true});h.state.currentLoaded=false;
  const refreshing=h.loadChat();
  h.requests[0].resolve(historyCurrent([historyCard('initial',1,false),historyCard('saved-next',2)]));
  await refreshing;
  assert.equal(h.requests.length,1);assert.equal(h.state.historyRestoring,false);
  const reports=h.container.children.filter(node=>node.dataset.oaHistoryQueryId);
  assert.equal(reports.length,1);assert.equal(reports[0],h.cards.get('initial'));
  assert.equal(timelineNodeMatchesKey(reports[0],saved.anchor),true);
  assert.equal(h.container.scrollTop,reports[0].offsetTop+saved.offset);
  assert.deepEqual(h.errors,[]);assert.equal(h.state.historyError,'');
  assert.equal(h.draft.value,'尚未发送的输入草稿');
});

test('refresh pages until a legacy task alias is authorized then stops at the single promoted query card', async () => {
  const saved={top:900,atBottom:false,anchor:'task:saved-next',offset:21};
  const h=harness({initialReading:saved,realHistoryGroups:true});h.state.currentLoaded=false;
  const refreshing=h.loadChat();
  h.requests[0].resolve(historyCurrent([historyCard('newest-next',3)]));await flush();
  assert.equal(h.state.historyRestoring,true);assert.equal(h.requests.length,2);
  assert.match(h.requests[1].path,/^\/api\/history\?/);
  const stableKey=h.cards.get('newest-next').dataset.timelineKey;
  h.requests[1].resolve({...page({id:'restored-query-window'}),
    cards:[historyCard('initial',1,false),historyCard('saved-next',2)]});
  await refreshing;
  const reports=h.container.children.filter(node=>node.dataset.oaHistoryQueryId);
  assert.equal(h.requests.length,2);assert.equal(reports.length,1);
  assert.equal(reports[0],h.cards.get('initial'));assert.equal(reports[0].dataset.timelineKey,stableKey);
  assert.equal(timelineNodeMatchesKey(reports[0],saved.anchor),true);
  assert.equal(h.container.scrollTop,reports[0].offsetTop+saved.offset);
  assert.deepEqual(h.state.historyCursor,cursor('restored-query-window'));
  assert.equal(h.state.historyRestoring,false);assert.deepEqual(h.errors,[]);assert.equal(h.state.historyError,'');
});

test('initial chat shows recent skill events without loading older history and preserves each existing feedback card', async () => {
  const h = harness({realSkills:true});
  Object.assign(h.state, {currentLoaded:false, legacyHistoryLoaded:false});
  const loading = h.loadChat();
  h.requests[0].resolve({schemaVersion:'agentbridge.workspace-current.v1',cards:[],timeline:{items:[]},
    history:{schemaVersion:'agentbridge.workspace-history.v1',hasMore:true,nextCursor:cursor('first')},cursors:{timeline:70}});
  await loading;
  assert.deepEqual(h.requests.map(r => r.path.split('?')[0]), ['/api/current','/api/skills/history']);
  assert.equal(h.state.legacyHistoryLoaded,false);
  const recent={event_id:'recent',created_at:'2026-11-06T12:01:00Z'};
  const older={event_id:'older',created_at:'2026-11-04T10:01:00Z'};
  h.scroll(170);
  h.requests[1].resolve({items:[recent,older]}); await flush();
  assert.deepEqual([...h.state.skillCards.keys()],['recent']);
  assert.equal(h.container.scrollTop,210);
  const card=h.state.skillCards.get('recent');
  const note=h.document.createElement('textarea');note.value='这次只想润色';card.append(note);
  const paging=h.loadMoreHistory();
  h.requests[2].resolve({...page({messages:['older-page']}),nextCursor:{...cursor('second'),createdAt:'2026-11-03T00:00:00Z'}});
  assert.equal(await paging,true);
  assert.deepEqual([...h.state.skillCards.keys()],['recent','older']);
  const refresh=h.reconcileTimeline();
  h.requests[3].resolve({items:[],cursor:71});await flush();
  assert.equal(h.requests[4].path,'/api/skills/history');
  h.requests[4].resolve({items:[{event_id:'new',created_at:'2026-11-06T12:10:00Z'},recent,older]});await refresh;
  assert.equal(h.state.skillCards.size,3);assert.equal(h.state.skillCards.get('recent'),card);assert.equal(note.value,'这次只想润色');
});

test('a failed skill fetch can retry while legacy history is still unloaded, and late old-account activity is discarded', async () => {
  const h=harness({realSkills:true});h.state.legacyHistoryLoaded=false;
  const first=h.hydrateSkillCards(),sameRead=h.hydrateSkillCards();
  assert.equal(h.requests.length,1);
  h.requests[0].reject(new Error('网络超时'));await Promise.all([first,sameRead]);
  assert.equal(h.state.skillHistoryRead,null);assert.equal(h.errors.length,0);
  const retry=h.hydrateSkillCards();
  assert.equal(h.requests[1].path,'/api/skills/history');
  h.scope.dispose(); h.state.account={accountId:'bob'};h.state.skillHistoryRead=null;
  h.replaceAccount(h.lifecycle.scope(),null);
  const next=h.hydrateSkillCards();
  h.requests[1].resolve({items:[{event_id:'alice-private',created_at:'2026-11-06T16:00:00Z'}]});await retry;
  assert.equal(h.state.skillCards.size,0);assert.notEqual(h.state.skillHistoryRead,null);
  h.requests[2].resolve({items:[{event_id:'bob',created_at:'2026-11-06T16:00:00Z'}]});await next;
  assert.deepEqual([...h.state.skillCards.keys()],['bob']);
});

function restoringSkill() {
  const reading={top:170,atBottom:false,anchor:'skill:old-anchor',offset:8};
  const h=harness({realSkills:true,initialReading:reading});
  Object.assign(h.state,{currentLoaded:false,legacyHistoryLoaded:false});
  const loading=h.loadChat();
  h.requests[0].resolve({schemaVersion:'agentbridge.workspace-current.v1',cards:[],timeline:{items:[]},
    history:{schemaVersion:'agentbridge.workspace-history.v1',hasMore:true,nextCursor:cursor('recent')},cursors:{timeline:70}});
  return {h,loading,reading};
}

test('a failed skill-anchor read falls back without paging chat and retries on the next skill fetch',async()=>{
  const {h,loading}=restoringSkill();await flush();
  assert.equal(h.requests[1].path,'/api/skills/history');
  h.requests[1].reject(new Error('临时断开'));await loading;
  assert.equal(h.requests.length,2);assert.equal(h.state.historyHasMore,true);
  assert.equal(h.state.historyError,'');assert.equal(h.container.scrollTop,170);
  assert.match(h.errors.join(''),/暂时无法加载.*自动重试/);
  const retry=h.hydrateSkillCards();h.requests[2].resolve({items:[{event_id:'recent',created_at:'2026-11-06T00:00:00Z'}]});
  await retry;assert.deepEqual([...h.state.skillCards.keys()],['recent']);
});

test('an anchor outside the recent skill events cannot be recovered by paging ordinary chat history',async()=>{
  const {h,loading}=restoringSkill();await flush();
  h.requests[1].resolve({items:[{event_id:'another',created_at:'2026-11-06T00:00:00Z'}]});await loading;
  assert.equal(h.requests.length,2);assert.equal(h.state.historyHasMore,true);
  assert.equal(h.state.historyError,'');assert.equal(h.container.scrollTop,170);
  assert.match(h.errors.join(''),/近期助手记录中没有/);
});

test('a known skill anchor on an earlier page still restores its original offset',async()=>{
  const {h,loading,reading}=restoringSkill();await flush();
  h.requests[1].resolve({items:[{event_id:'old-anchor',created_at:'2026-11-04T12:00:00Z'}]});await flush();
  assert.match(h.requests[2].path,/^\/api\/history\?/);
  h.requests[2].resolve({...page({messages:['earlier-text']}),nextCursor:{...cursor('earlier'),createdAt:'2026-11-03T00:00:00Z'}});
  await loading;
  assert.equal(h.requests.length,3);assert.deepEqual(h.errors,[]);
  assert.equal(h.container.scrollTop,h.state.skillCards.get('old-anchor').offsetTop+reading.offset);
});
