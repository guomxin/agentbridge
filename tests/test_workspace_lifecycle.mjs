import test from "node:test";
import assert from "node:assert/strict";
import { createLifecycle, abortableDelay, isCancellation } from "../bscli/workspace/static/workspace_lifecycle.mjs";

function clock() {
  let next = 0;
  const pending = new Map();
  return {
    pending,
    setTimeout(callback) { const id = ++next; pending.set(id, callback); return id; },
    setInterval(callback) { const id = ++next; pending.set(id, callback); return id; },
    clearTimeout(id) { pending.delete(id); },
    clearInterval(id) { pending.delete(id); },
  };
}
const flush = () => new Promise(resolve => queueMicrotask(resolve));

test("disposing an account cancels all owned requests, timers and event sources exactly once", async () => {
  const timers = clock();
  const lifecycle = createLifecycle({ timers });
  const account = lifecycle.scope();
  const view = account.child();
  const { controller } = view.controller();
  let closed = 0;
  let calls = 0;
  view.defer(() => closed++);
  account.interval(() => calls++, 10000);
  view.timeout(() => calls++, 3000);
  // Simulate a callback already queued by the browser before disposal.
  const queued = [...timers.pending.values()];
  account.dispose();
  account.dispose();
  queued.forEach(callback => callback());
  await flush();
  assert.equal(controller.signal.aborted, true);
  assert.equal(timers.pending.size, 0);
  assert.equal(closed, 1);
  assert.equal(calls, 0);
  assert.throws(() => view.timeout(() => {}, 1), isCancellation);
});

test("switching views destroys view work but leaves account timeline observers and new view alive", () => {
  const timers = clock();
  const lifecycle = createLifecycle({ timers });
  const account = lifecycle.scope();
  const timeline = account.interval(() => {}, 10000);
  const view = account.child();
  const timer = view.timeout(() => {}, 3000);
  const previous = view.replace("detail");
  const old = previous.controller();
  const current = view.replace("detail");
  assert.equal(old.controller.signal.aborted, true);
  assert.equal(current.current(), true);
  view.dispose();
  assert.equal(timers.pending.has(timer), false);
  assert.equal(timers.pending.has(timeline), true);
  assert.equal(account.child().current(), true);
  lifecycle.dispose();
  assert.equal(timers.pending.size, 0);
});

test("cleared and completed timers release resources; asynchronous cancellation is silent", async () => {
  const timers = clock();
  const errors = [];
  const lifecycle = createLifecycle({ timers, onError: error => errors.push(error) });
  const scope = lifecycle.scope();
  let calls = 0;
  const canceled = scope.timeout(() => calls++, 1);
  lifecycle.clearTimer(canceled);
  const finished = scope.timeout(() => calls++, 1);
  const tick = timers.pending.get(finished);
  tick();
  await flush();
  assert.equal(calls, 1);
  const failure = scope.timeout(() => { throw new Error("reported"); }, 1);
  timers.pending.get(failure)();
  await flush();
  await flush();
  assert.equal(errors.length, 1);
  scope.dispose();
});

test("retry delay rejects on cancellation and removes its timer", async () => {
  const timers = clock();
  const controller = new AbortController();
  const waiting = abortableDelay(350, controller.signal, timers);
  controller.abort();
  await assert.rejects(waiting, isCancellation);
  assert.equal(timers.pending.size, 0);
  await assert.rejects(abortableDelay(350, controller.signal, timers), isCancellation);
});
