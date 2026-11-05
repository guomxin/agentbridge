import test from 'node:test';
import assert from 'node:assert/strict';
import {createHistoryAutoload, restoreHistoryAnchor} from '../agentbridge/workspace/static/workspace_history_autoload.mjs';

function scrollFixture(options = {}) {
  const listeners = new Map();
  const container = {scrollTop:1000, clientHeight:600, scrollHeight:3000,
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
  };
  const emit = (name, event = {}) => {
    for (const callback of listeners.get(name) || []) callback({target:container, ...event});
  };
  let clock = 0, calls = 0, valid = true, deferredCleanup;
  const scope = {current:() => valid, defer:callback => { deferredCleanup = callback; return () => {}; }};
  const controller = createHistoryAutoload({container, scope, now:() => clock, pending:() => true,
    load:() => { calls++; return true; }, ...options});
  return {container, controller, emit, listeners, calls:() => calls,
    tick(ms) { clock += ms; },
    scroll(top) { container.scrollTop = top; emit('scroll'); },
    disposeAccount() { valid = false; deferredCleanup(); }};
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test('programmatic and initial top scrolls do not load; upward user scrolling does', async () => {
  const f = scrollFixture();
  f.scroll(0);
  await settle();
  assert.equal(f.calls(), 0);
  f.scroll(600);
  f.emit('wheel', {deltaY:-300});
  f.scroll(250);
  await settle();
  assert.equal(f.calls(), 1);
  f.scroll(100); // Anchor adjustment after rendering must not fetch another page.
  await settle();
  assert.equal(f.calls(), 1);
  f.emit('wheel', {deltaY:-30}); // Continued upward input can load another short page.
  await settle();
  assert.equal(f.calls(), 2);
});

test('one in-flight page coalesces repeated scroll events and explicit loads', async () => {
  let resolve, calls = 0;
  const f = scrollFixture({load:() => { calls++; return new Promise(done => { resolve = done; }); }});
  f.scroll(200);
  f.emit('wheel', {deltaY:-20});
  f.emit('wheel', {deltaY:-20});
  f.scroll(50);
  const first = f.controller.load();
  await settle();
  assert.equal(calls, 1);
  resolve(true);
  assert.equal(await first, true);
  await settle();
  assert.equal(calls, 1);
  assert.equal(f.controller.loading, false);
});

test('keyboard, touch and scrollbar dragging can initiate an older-page read', async () => {
  for (const input of [
    f => f.emit('keydown', {key:'PageUp'}),
    f => f.emit('keydown', {key:'Home'}),
    f => { f.emit('touchstart', {touches:[{clientY:100}]}); f.emit('touchmove', {touches:[{clientY:180}]}); },
    f => { f.emit('pointerdown', {button:0}); f.scroll(100); f.emit('pointerup'); },
  ]) {
    const f = scrollFixture();
    f.scroll(250); input(f); await settle();
    assert.equal(f.calls(), 1);
    f.controller.dispose();
  }
});

test('downward gestures, editing keys and expired intent do not trigger loading', async () => {
  const f = scrollFixture();
  f.scroll(200);
  f.emit('wheel', {deltaY:30});
  f.emit('keydown', {key:'ArrowUp', target:{closest:() => ({})}});
  f.emit('pointerdown', {button:0, target:{}}); // Expanding a card is not a scrollbar drag.
  f.scroll(100);
  await settle();
  assert.equal(f.calls(), 0);
  f.scroll(600); f.emit('wheel', {deltaY:-10}); f.tick(1600); f.scroll(200);
  await settle();
  assert.equal(f.calls(), 0);
});

test('inactive, not-ready and exhausted histories do not produce requests', async () => {
  for (const options of [{active:() => false}, {ready:() => false}, {pending:() => false}]) {
    const f = scrollFixture(options);
    f.scroll(0); f.emit('wheel', {deltaY:-10});
    assert.equal(await f.controller.load(), false);
    assert.equal(f.calls(), 0);
    f.controller.dispose();
  }
});

test('failure blocks wheel retry storms until an explicit retry succeeds', async () => {
  let calls = 0;
  const errors = [];
  const f = scrollFixture({load:() => { calls++; if (calls === 1) throw new Error('offline'); return true; },
    onError:error => errors.push(error.message)});
  f.scroll(0); f.emit('wheel', {deltaY:-10}); await settle();
  assert.equal(f.controller.blocked, true);
  for (let i = 0; i < 5; i++) f.emit('wheel', {deltaY:-10});
  await settle();
  assert.equal(calls, 1);
  assert.deepEqual(errors, ['offline']);
  assert.equal(await f.controller.load(), true);
  assert.equal(f.controller.blocked, false);
  f.emit('wheel', {deltaY:-10}); await settle();
  assert.equal(calls, 3);
});

test('a handled false result also blocks automatic retries; reset permits another gesture', async () => {
  let calls = 0;
  const f = scrollFixture({load:() => { calls++; return calls > 1; }});
  f.scroll(0); f.emit('wheel', {deltaY:-10}); await settle();
  assert.equal(f.controller.blocked, true);
  f.controller.reset(); f.emit('wheel', {deltaY:-10}); await settle();
  assert.equal(calls, 2);
  assert.equal(f.controller.blocked, false);
});

test('account disposal removes listeners and does not report errors from a stale read', async () => {
  let reject;
  const errors = [];
  const f = scrollFixture({load:() => new Promise((_done, fail) => { reject = fail; }), onError:error => errors.push(error)});
  f.scroll(0); f.emit('wheel', {deltaY:-10}); await settle();
  f.disposeAccount(); reject(new Error('old account request')); await settle();
  assert.equal(f.controller.current(), false);
  assert.deepEqual(errors, []);
  assert.equal([...f.listeners.values()].reduce((sum, value) => sum + value.size, 0), 0);
  assert.equal(await f.controller.load(), false);
});

test('refresh restoration loads pages until the saved reading anchor exists', async () => {
  let loaded = 0;
  const reading = {anchor:'task:older', atBottom:false};
  const result = await restoreHistoryAnchor({reading, hasAnchor:anchor => anchor === 'task:older' && loaded >= 3,
    pending:() => true, load:async() => { loaded++; }, progress:() => loaded});
  assert.deepEqual(result, {status:'found', pages:3});
  assert.equal(loaded, 3);
});

test('restoration is bounded and distinguishes exhaustion, failure, cancellation and no progress', async () => {
  const reading = {anchor:'missing', atBottom:false}, base = {reading, hasAnchor:() => false, pending:() => true};
  let loaded = 0;
  assert.deepEqual(await restoreHistoryAnchor({...base, maxPages:2, load:async() => { loaded++; }}), {status:'limit', pages:2});
  assert.equal(loaded, 2);
  assert.deepEqual(await restoreHistoryAnchor({...base, pending:() => false, load:() => assert.fail('unexpected read')}), {status:'exhausted', pages:0});
  assert.deepEqual(await restoreHistoryAnchor({...base, load:async() => false}), {status:'failed', pages:1});
  assert.deepEqual(await restoreHistoryAnchor({...base, progress:() => 'same cursor', load:async() => true}), {status:'stalled', pages:1});
  let current = true;
  assert.deepEqual(await restoreHistoryAnchor({...base, scope:{current:() => current}, load:async() => { current = false; }}), {status:'canceled', pages:1});
  assert.deepEqual(await restoreHistoryAnchor({...base, reading:{...reading, atBottom:true}, load:() => assert.fail('unexpected read')}), {status:'not-needed', pages:0});
  assert.deepEqual(await restoreHistoryAnchor({...base, maxPages:0, load:() => assert.fail('unexpected read')}), {status:'limit', pages:0});
  let invalidLimitReads = 0;
  assert.deepEqual(await restoreHistoryAnchor({...base, maxPages:Infinity, load:async() => { invalidLimitReads++; }}), {status:'limit', pages:32});
  assert.equal(invalidLimitReads, 32);
});
