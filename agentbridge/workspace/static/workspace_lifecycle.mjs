/** Own browser resources by account, view, and individual async operation. */
export function cancellationError() {
  const error = new Error("操作已取消");
  error.name = "AbortError";
  error.code = "WORKSPACE_CANCELED";
  return error;
}

export function isCancellation(error) {
  return error?.name === "AbortError" || error?.code === "WORKSPACE_CANCELED";
}

export function createLifecycle({ timers = globalThis, onError = console.error } = {}) {
  const roots = new Set();
  const timerOwners = new Map();
  function clearTimer(id) {
    const owned = timerOwners.get(id);
    if (!owned) return;
    timerOwners.delete(id);
    owned.remove();
    (owned.interval ? timers.clearInterval : timers.clearTimeout).call(timers, id);
  }
  function scope(parent = null) {
    let disposed = false;
    const children = new Set();
    const resources = new Set();
    const latest = new Map();
    const current = () => !disposed && (!parent || parent.current());
    const assertCurrent = () => { if (!current()) throw cancellationError(); };
    const defer = (cleanup) => {
      if (!current()) { cleanup(); return () => {}; }
      resources.add(cleanup);
      return () => resources.delete(cleanup);
    };
    function dispose() {
      if (disposed) return;
      disposed = true;
      for (const child of [...children]) child.dispose();
      for (const cleanup of [...resources]) cleanup();
      resources.clear();
      latest.clear();
      if (parent) parent.removeChild(owner); else roots.delete(owner);
    }
    function timer(callback, milliseconds, interval) {
      assertCurrent();
      const invoke = () => {
        if (!interval) { timerOwners.delete(id); remove(); }
        if (!current()) return;
        Promise.resolve().then(() => {
          if (current()) return callback();
        }).catch(error => { if (!isCancellation(error)) onError(error); });
      };
      const id = (interval ? timers.setInterval : timers.setTimeout).call(timers, invoke, milliseconds);
      const remove = defer(() => clearTimer(id));
      timerOwners.set(id, { interval, remove });
      return id;
    }
    const owner = {
      current, assertCurrent, dispose, defer,
      child: () => { assertCurrent(); return scope(owner); },
      replace(key) {
        assertCurrent();
        latest.get(key)?.dispose();
        const child = scope(owner);
        latest.set(key, child);
        return child;
      },
      addChild: child => children.add(child),
      removeChild: child => children.delete(child),
      controller() {
        assertCurrent();
        const controller = new AbortController();
        const release = defer(() => controller.abort());
        return { controller, release };
      },
      timeout: (callback, milliseconds) => timer(callback, milliseconds, false),
      interval: (callback, milliseconds) => timer(callback, milliseconds, true),
      clearTimer,
    };
    if (parent) parent.addChild(owner); else roots.add(owner);
    return owner;
  }
  return { scope, clearTimer, dispose() { for (const root of [...roots]) root.dispose(); } };
}

/** A retry delay must be interruptible as well as the network request itself. */
export function abortableDelay(milliseconds, signal, timers = globalThis) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(cancellationError()); return; }
    const abort = () => { timers.clearTimeout(timer); reject(cancellationError()); };
    const timer = timers.setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    signal?.addEventListener("abort", abort, { once: true });
  });
}
