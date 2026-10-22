import assert from 'node:assert/strict';
import test from 'node:test';
import {mountTrustedFlow} from '../agentbridge/auth/static/flow_waiting.mjs';

const opaque = 'a'.repeat(43);
const signature = 'b'.repeat(64);
const config = {statusUrl: `/flow/authorize/${opaque}/status`, token: 'original-card-token'};
const state = (extra = {}) => ({state: 'processing', title: '提交与核验中', message: '请勿重复提交。',
  terminal: false, stateVersion: signature, ...extra});
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture({respectAbort = true} = {}) {
  const nodes = Object.fromEntries(['flow-title','flow-message','flow-recovery','flow-batch','flow-continue'].map(id =>
    [id, {textContent: '', hidden: true, removeAttribute(name) { delete this[name]; }}]));
  const calls = [], timers = new Map(), delays = [], events = new Map(), navigations = [];
  let sequence = 0, active = 0, maxActive = 0, now = 0;
  const window = {document: {getElementById: id => nodes[id]},
    location: {origin: 'https://trusted.example', hash: '#lifecycle-proof', replace: url => navigations.push(url)},
    addEventListener: (name, callback) => events.set(name, callback),
    setTimeout(callback, delay) { const id = ++sequence; delays.push(delay); timers.set(id, {callback, delay}); return id; },
    clearTimeout: id => timers.delete(id),
  };
  const fetch = (url, options) => {
    active++; maxActive = Math.max(maxActive, active);
    let resolve, reject;
    const promise = new Promise((a, b) => { resolve = a; reject = b; });
    let finished = false;
    const finish = () => { if (!finished) { finished = true; active--; } };
    calls.push({url, options,
      resolve(value, status = 200, json = async () => value) { finish(); resolve({ok: status === 200, status, json}); },
      reject(error = new TypeError('offline')) { finish(); reject(error); }});
    if (respectAbort) options.signal.addEventListener('abort', () => {
      finish(); reject(new DOMException('cancelled', 'AbortError'));
    }, {once: true});
    return promise;
  };
  const controller = mountTrustedFlow(config, {window, fetch, now: () => now});
  return {controller, calls, timers, delays, nodes, navigations,
    maxActive: () => maxActive,
    hide() { events.get('pagehide')(); },
    restore() { events.get('pageshow')({persisted: true}); },
    advance(milliseconds) { now += milliseconds; },
    fireTimer(delay) { const entry = delay === undefined ? [...timers.entries()].at(-1)
      : [...timers.entries()].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, `Expected timer ${delay}`); const [id, timer] = entry; timers.delete(id); timer.callback(); }};
}

test('healthy responses immediately continue one read-only long poll with only deadline and elapsed timers', async () => {
  const f = fixture();
  assert.equal(new URL(f.calls[0].url).searchParams.get('wait'), '20');
  assert.equal(new URL(f.calls[0].url).searchParams.has('after'), false);
  f.calls[0].resolve(state()); await flush();
  assert.equal(f.calls.length, 2);
  assert.equal(new URL(f.calls[1].url).searchParams.get('after'), signature);
  assert.deepEqual([...f.timers.values()].map(timer => timer.delay).sort((a,b) => a-b), [1000, 30000]);
  f.calls[1].resolve(state()); await flush();
  assert.equal(f.calls.length, 3);
  f.calls[2].resolve(state({state: 'completed', terminal: true}));
  await f.controller.done;
  assert.equal(f.maxActive(), 1);
  assert.ok(f.calls.every(call => call.options.method === 'GET'));
  assert.ok(f.calls.every(call => call.options.credentials === 'same-origin' && call.options.cache === 'no-store'));
  assert.ok(f.calls.every(call => call.options.headers['X-AgentBridge-Flow-Token'] === config.token));
  assert.equal(f.nodes['flow-recovery'].textContent, '');
});

test('pagehide cancels an in-flight read and never starts a replacement', async () => {
  const f = fixture();
  f.hide(); await f.controller.done;
  assert.equal(f.calls[0].options.signal.aborted, true);
  assert.equal(f.calls.length, 1);
  assert.equal(f.timers.size, 0);
  assert.equal(f.nodes['flow-recovery'].textContent, '');
});

test('a late response after pagehide cannot update or navigate the page', async () => {
  const f = fixture({respectAbort: false});
  f.hide();
  f.calls[0].resolve(state({state: 'review_ready', nextUrl: `/flow/authorize/${opaque}`}));
  await f.controller.done;
  assert.equal(f.nodes['flow-title'].textContent, '');
  assert.deepEqual(f.navigations, []);
  assert.equal(f.calls.length, 1);
});

test('back-forward restoration starts one fresh read and an older response cannot overwrite it', async () => {
  const f = fixture({respectAbort: false});
  f.hide(); f.restore();
  assert.equal(f.calls.length, 2);
  f.calls[0].resolve(state({state: 'review_ready', nextUrl: `/flow/authorize/${opaque}`}));
  await flush();
  assert.deepEqual(f.navigations, []);
  assert.equal(f.nodes['flow-title'].textContent, '');
  f.calls[1].resolve(state({state: 'completed', terminal: true}));
  await f.controller.done;
  assert.equal(f.nodes['flow-title'].textContent, '提交与核验中');
  f.hide(); f.restore();
  assert.equal(f.calls.length, 2); // terminal receipts never restart a coordinator/read loop
});

test('only errors back off, the delay is bounded and closing the page clears it', async () => {
  const f = fixture();
  for (let i = 0; i < 8; i++) {
    f.calls[i].reject(); await flush();
    assert.equal(f.timers.size, 2); // elapsed display plus bounded retry backoff
    if (i < 7) { f.fireTimer(); await flush(); }
  }
  assert.deepEqual(f.delays.filter((delay, index) => delay !== 30000 && index !== 0),
    [500, 1000, 2000, 4000, 8000, 10000, 10000, 10000]);
  assert.ok(f.nodes['flow-recovery'].textContent.includes('不会重发'));
  f.hide(); await f.controller.done;
  assert.equal(f.timers.size, 0);
  assert.equal(f.calls.length, 8);
  assert.ok(f.calls.every(call => call.options.method === 'GET'));
});

test('exact extra authorization carries the lifecycle fragment and never submits a decision', async () => {
  const f = fixture();
  const next = `/flow/authorize/${opaque}/present/${'c'.repeat(43)}`;
  f.calls[0].resolve(state({state: 'review_ready', nextUrl: next}));
  await f.controller.done;
  assert.deepEqual(f.navigations, [`https://trusted.example${next}#lifecycle-proof`]);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].options.method, 'GET');
});

test('the next batch item is an explicit same-origin input link with no automatic navigation or POST', async () => {
  const f = fixture();
  f.calls[0].resolve(state({state: 'completed', terminal: true,
    continueUrl: `/flow/input/${opaque}`, batch: {completedCount: 1, totalCount: 2, nextOrdinal: 2, nextTitle: '第二项'}}));
  await f.controller.done;
  assert.equal(f.nodes['flow-continue'].hidden, false);
  assert.equal(f.nodes['flow-continue'].href, `https://trusted.example/flow/input/${opaque}#lifecycle-proof`);
  assert.match(f.nodes['flow-batch'].textContent, /1 \/ 2.*第 2 项.*第二项/);
  assert.deepEqual(f.navigations, []);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].options.method, 'GET');
});

test('unknown and unavailable states are terminal without retries or following injected successors', async () => {
  for (const status of [200, 403]) {
    const f = fixture();
    f.calls[0].resolve(state({state: status === 403 ? 'unavailable' : 'unknown', terminal: true,
      stateVersion: status === 403 ? undefined : signature,
      nextUrl: `/flow/authorize/${opaque}`, continueUrl: `/flow/input/${opaque}`}), status);
    await f.controller.done;
    assert.deepEqual(f.navigations, []);
    assert.equal(f.nodes['flow-continue'].hidden, true);
    assert.equal(f.calls.length, 1);
    assert.equal(f.timers.size, 0);
  }
});

test('foreign, credentialed, untrusted path and query continuations are rejected', async () => {
  for (const next of [`https://foreign.example/flow/authorize/${opaque}`,
    `https://user:pass@trusted.example/flow/authorize/${opaque}`,
    `/authorize/${opaque}`, `/flow/authorize/${opaque}?approve=1`, `/flow/authorize/${opaque}#override`]) {
    const f = fixture();
    f.calls[0].resolve(state({state: 'review_ready', nextUrl: next}));
    await flush();
    assert.deepEqual(f.navigations, []);
    assert.equal(f.timers.size, 2);
    f.controller.stop(); await f.controller.done;
    assert.equal(f.calls.length, 1);
  }
});

test('permanent authentication and expiry failures stop even if the error body never resolves', async () => {
  for (const status of [401, 403, 404, 410]) {
    const f = fixture();
    f.calls[0].resolve(null, status, () => new Promise(() => {}));
    await f.controller.done;
    assert.equal(f.calls.length, 1);
    assert.equal(f.timers.size, 0);
    assert.match(f.nodes['flow-message'].textContent, /回到原任务/);
    assert.match(f.nodes['flow-recovery'].textContent, /停止自动刷新/);
    f.hide(); f.restore();
    assert.equal(f.calls.length, 1);
  }
});

test('a hung fetch times out at 30 seconds and reconnects with GET only', async () => {
  const f = fixture({respectAbort: false});
  f.advance(30000); f.fireTimer(30000); await flush();
  assert.equal(f.calls[0].options.signal.aborted, true);
  assert.match(f.nodes['flow-recovery'].textContent, /超过 30 秒.*已等待 30 秒.*不会重发/);
  f.fireTimer(500); await flush();
  assert.equal(f.calls.length, 2);
  f.calls[0].resolve(state({state: 'review_ready', nextUrl: `/flow/authorize/${opaque}`})); await flush();
  assert.deepEqual(f.navigations, []);
  f.calls[1].resolve(state({state: 'completed', terminal: true})); await f.controller.done;
  assert.ok(f.calls.every(call => call.options.method === 'GET'));
  assert.equal(f.timers.size, 0);
});

test('the same deadline bounds response JSON parsing and cancellation never waits for late JSON', async () => {
  const f = fixture();
  f.calls[0].resolve(null, 200, () => new Promise(() => {})); await flush();
  f.advance(30000); f.fireTimer(30000); await flush();
  assert.match(f.nodes['flow-recovery'].textContent, /超过 30 秒/);
  f.fireTimer(500); await flush();
  f.calls[1].resolve(null, 200, () => new Promise(() => {})); await flush();
  f.hide(); await f.controller.done;
  assert.equal(f.timers.size, 0);
});

test('slow status shows real observed elapsed time without inventing progress', async () => {
  const f = fixture();
  f.advance(7000); f.fireTimer(1000);
  assert.match(f.nodes['flow-recovery'].textContent, /本页已等待 7 秒/);
  assert.doesNotMatch(f.nodes['flow-recovery'].textContent, /%/);
  f.hide(); await f.controller.done;
});
