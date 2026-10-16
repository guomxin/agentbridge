import test from 'node:test';
import assert from 'node:assert/strict';
import { createProjectionQueue, applyCurrentSnapshot } from '../agentbridge/workspace/static/workspace_projection.mjs';

const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; };
function harness(options = {}) {
  const jobs = new Map(), calls = [], applied = [], retired = [], errors = [];
  let serial = 0, rendered = 0, overflow = 0;
  const queue = createProjectionQueue({
    timers: {setTimeout(fn) {const id=++serial;jobs.set(id,fn);return id;}, clearTimeout(id) {jobs.delete(id);}},
    fetchBatch(ids) {const result=deferred();calls.push({ids,...result});return result.promise;},
    apply: detail => applied.push(detail.task.task_id), retire: id => retired.push(id), render: () => rendered++,
    onError: e => errors.push(e), onOverflow: () => overflow++, ...options,
  });
  const tick = async () => {const work=[...jobs.values()];jobs.clear();work.forEach(fn=>fn());await new Promise(resolve=>setImmediate(resolve));};
  const settle = async (index, result) => {calls[index].resolve(result);await new Promise(resolve=>setImmediate(resolve));};
  return {queue,calls,applied,retired,errors,tick,settle,get rendered(){return rendered;},get overflow(){return overflow;}};
}
const item = id => ({taskId:id, detail:{task:{task_id:id}}});

test('a burst deduplicates IDs, bounds each request and renders each batch once', async () => {
  const h=harness();
  for(let i=0;i<65;i++) {h.queue.enqueue(String(i));h.queue.enqueue(String(i));}
  await h.tick(); assert.equal(h.calls.length,1);assert.equal(h.calls[0].ids.length,32);
  await h.settle(0,{items:h.calls[0].ids.map(item)});
  await h.tick();assert.equal(h.calls[1].ids.length,32);
  await h.settle(1,{items:h.calls[1].ids.map(item)});
  await h.tick();assert.equal(h.calls[2].ids.length,1);
  await h.settle(2,{items:h.calls[2].ids.map(item)});
  assert.equal(new Set(h.applied).size,65);assert.equal(h.rendered,3);
});

test('an event received during a read prevents stale detail or unavailability from winning', async () => {
  const h=harness();h.queue.enqueue('a');h.queue.enqueue('b');await h.tick();
  h.queue.enqueue('a');h.queue.enqueue('b');
  await h.settle(0,{items:[item('a')],unavailableTaskIds:['b','foreign']});
  assert.deepEqual(h.applied,[]);assert.deepEqual(h.retired,[]);
  await h.tick();await h.settle(1,{items:[item('b')],unavailableTaskIds:['a']});
  assert.deepEqual(h.applied,['b']);assert.deepEqual(h.retired,['a']);
});

test('account reset discards in-flight rows even if transport ignores abort and new account proceeds', async () => {
  const h=harness();h.queue.enqueue('old');await h.tick();h.queue.reset();
  h.queue.enqueue('new');await h.tick();assert.equal(h.calls.length,2);
  await h.settle(0,{items:[item('old')],unavailableTaskIds:['new']});
  assert.deepEqual(h.applied,[]);assert.equal(h.rendered,0);
  await h.settle(1,{items:[item('new')]});assert.deepEqual(h.applied,['new']);
});

test('failed read is not replayed; newer queued event can still refresh and excess IDs stay bounded', async () => {
  const h=harness({capacity:2});h.queue.enqueue('a');h.queue.enqueue('b');h.queue.enqueue('c');
  assert.equal(h.overflow,1);await h.tick();h.queue.enqueue('a');
  h.calls[0].reject(new Error('offline'));await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.errors.length,1);await h.tick();assert.deepEqual(h.calls[1].ids,['a']);
  await h.settle(1,{items:[item('foreign'),{...item('a'),detail:{task:{task_id:'different'}}}]});
  assert.deepEqual(h.applied,[]);await h.tick();assert.equal(h.calls.length,2);
});

test('initial zero cursor is established without adopting a newer projection or chat row sequence', () => {
  const state={timelineCursor:0,chatTimelineOldestSequence:0}, rows=[];
  const initial=applyCurrentSnapshot(state,{schemaVersion:'agentbridge.workspace-current.v1',
    cursors:{timeline:0},cards:[{task:{version:9}}],timeline:{items:[{sequence:1}],oldestSequence:1,hasMore:true}},
    {ingest:(...args)=>rows.push(args),upsert:()=>{},retire:()=>{}});
  assert.equal(initial,true);assert.equal(state.currentLoaded,true);assert.equal(state.timelineCursor,0);
  assert.deepEqual(rows,[[{sequence:1},false,false]]);
});

test('refresh preserves replay position, older loaded messages, and unsent local inputs', () => {
  const state={currentLoaded:true,timelineCursor:10,chatTimelineOldestSequence:2,chatTimelineHasOlder:false,
    localMessages:new Map([['pending','draft']])}, retired=[];
  const initial=applyCurrentSnapshot(state,{schemaVersion:'agentbridge.workspace-current.v1',cursors:{timeline:99},
    timeline:{items:[],oldestSequence:80,hasMore:true},unavailableTaskIds:['revoked']},
    {ingest:()=>{},upsert:()=>{},retire:id=>retired.push(id)});
  assert.equal(initial,false);assert.equal(state.timelineCursor,10);assert.equal(state.chatTimelineOldestSequence,2);
  assert.equal(state.chatTimelineHasOlder,false);assert.equal(state.localMessages.get('pending'),'draft');
  assert.deepEqual(retired,['revoked']);
});

test('current, history and event reads share retirement tombstones even at the same task version', async () => {
  const { createProjectionOrder } = await import('../agentbridge/workspace/static/workspace_projection.mjs');
  const order=createProjectionOrder(), cards=new Map();
  const callbacks=ticket=>({ingest(){},upsert(detail){if(order.accept(detail.task.task_id,ticket))cards.set(detail.task.task_id,detail);},
    retire(id){if(order.accept(id,ticket))cards.delete(id);}});
  const initial={schemaVersion:'agentbridge.workspace-current.v1',cards:[{task:{task_id:'a',version:7}}]};
  const state={}, oldSnapshot=order.begin();
  order.invalidate('a');const eventRead=order.begin();
  assert.equal(order.accept('a',eventRead),true);cards.delete('a');
  applyCurrentSnapshot(state,initial,callbacks(oldSnapshot));
  assert.equal(cards.has('a'),false,'an older snapshot cannot resurrect revoked controls');
  const oldHistory=order.begin(),newCurrent=order.begin();
  applyCurrentSnapshot(state,initial,callbacks(newCurrent));
  assert.equal(cards.has('a'),true);
  if(order.accept('a',oldHistory))cards.delete('a');
  assert.equal(cards.has('a'),true,'old unavailable must not retire a newer permitted result');
});

test('an incoming event invalidates every older source while independent task rows remain usable', async () => {
  const { createProjectionOrder } = await import('../agentbridge/workspace/static/workspace_projection.mjs');
  const order=createProjectionOrder(),read=order.begin();order.invalidate('a');
  assert.equal(order.accept('a',read),false);assert.equal(order.accept('b',read),true);
  const followup=order.begin();assert.equal(order.accept('a',followup),true);
  assert.equal(order.accept('a',read),false);
});

test('account epochs and bounded tombstone rollover never re-enable an ancient response', async () => {
  const { createProjectionOrder } = await import('../agentbridge/workspace/static/workspace_projection.mjs');
  let overflow=0;const order=createProjectionOrder({capacity:2,onOverflow:()=>overflow++});
  const first=order.begin();assert.equal(order.accept('a',first),true);
  const second=order.begin();assert.equal(order.accept('b',second),true);
  assert.equal(order.accept('c',order.begin()),false);assert.equal(overflow,1);
  assert.equal(order.accept('a',first),false);assert.equal(order.accept('b',second),false);
  assert.equal(order.accept('c',order.begin()),true);
  const old=order.begin();order.reset();const next=order.begin();
  assert.equal(order.accept('c',old),false);assert.equal(order.accept('c',next),true);
});
