// Per-view cancellation and stale-response checks, with injectable timers.
export function createViewScope() {
  let revision = 0;
  let controller = new AbortController();
  const capture = () => {
    const expected = revision;
    const signal = controller.signal;
    return { signal, current: () => expected === revision && !signal.aborted };
  };
  return {
    capture,
    invalidate() {
      controller.abort();
      revision += 1;
      controller = new AbortController();
    },
    begin() { this.invalidate(); return capture(); },
  };
}

export function staleResponse() {
  return new DOMException("页面已切换", "AbortError");
}

export function createTimerSlot({ schedule = setTimeout, cancel = clearTimeout } = {}) {
  let timer = null;
  let revision = 0;
  const clear = () => {
    revision += 1;
    if (timer !== null) cancel(timer);
    timer = null;
  };
  return {
    clear,
    replace(callback, delay) {
      clear();
      const expected = revision;
      timer = schedule(() => {
        if (expected !== revision) return;
        timer = null;
        callback();
      }, delay);
    },
  };
}
