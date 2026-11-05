// Loading older records is a read operation owned by the current account.
// Scroll events alone do not establish user intent: restoring an anchor and
// inserting content also emit them.
export function createHistoryAutoload({container, load, pending, ready = () => true,
  active = () => true, scope, threshold = 320, now = Date.now, onError = () => {}}) {
  let disposed = false, running = null, blocked = false;
  let previousTop = container.scrollTop, intentUntil = 0, touchY = null, dragging = false;
  let release = () => {};
  const listeners = [];
  const current = () => !disposed && (!scope || scope.current());
  const eligible = () => current() && ready() && active() && pending();
  const nearTop = () => container.scrollTop <= threshold;
  const listen = (target, name, callback, options) => {
    target?.addEventListener(name, callback, options);
    if (target) listeners.push(() => target.removeEventListener(name, callback, options));
  };
  const clearIntent = () => { intentUntil = 0; previousTop = container.scrollTop; };
  async function request(manual = false) {
    if (running) return running;
    if (!eligible() || (!manual && blocked)) return false;
    if (manual) blocked = false;
    clearIntent();
    // Defer invocation until the shared promise is set, including synchronous
    // callbacks which cause another scroll event while starting their request.
    running = Promise.resolve().then(() => {
      if (!eligible()) return false;
      return load();
    }).then(result => {
      if (result === false && current()) blocked = true;
      return current() && result !== false;
    }).catch(error => {
      if (current()) { blocked = true; onError(error); }
      return false;
    }).finally(() => {
      running = null;
      clearIntent();
    });
    return running;
  }
  const upwards = () => {
    if (!eligible() || running || blocked) return;
    intentUntil = now() + 1500;
    if (nearTop()) void request();
  };
  const onScroll = () => {
    const top = container.scrollTop, movedUp = top < previousTop;
    previousTop = top;
    if (movedUp && intentUntil > now() && nearTop()) void request();
  };
  listen(container, 'scroll', onScroll, {passive:true});
  listen(container, 'wheel', event => { if (event.deltaY < 0) upwards(); }, {passive:true});
  listen(container, 'keydown', event => {
    if (event.defaultPrevented || event.target?.closest?.('input, textarea, select, [contenteditable="true"]')) return;
    if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) upwards();
  });
  listen(container, 'touchstart', event => { touchY = event.touches?.[0]?.clientY ?? null; }, {passive:true});
  listen(container, 'touchmove', event => {
    const y = event.touches?.[0]?.clientY;
    if (Number.isFinite(y) && touchY !== null && y > touchY) upwards();
    touchY = Number.isFinite(y) ? y : null;
  }, {passive:true});
  listen(container, 'touchend', () => { touchY = null; }, {passive:true});
  listen(container, 'touchcancel', () => { touchY = null; }, {passive:true});
  listen(container, 'pointerdown', event => {
    if (event.button !== undefined && event.button !== 0) return;
    // A click inside a card can expand/collapse it and move the viewport.
    // Native scrollbar drags target the scrolling container itself.
    if (event.target !== container) return;
    dragging = true;
    if (eligible()) intentUntil = now() + 1500;
  }, {passive:true});
  listen(container, 'pointermove', () => { if (dragging && eligible()) intentUntil = now() + 1500; }, {passive:true});
  const endDrag = () => { dragging = false; };
  listen(container.ownerDocument?.defaultView || container, 'pointerup', endDrag, {passive:true});
  listen(container.ownerDocument?.defaultView || container, 'pointercancel', endDrag, {passive:true});
  function dispose() {
    if (disposed) return;
    disposed = true;
    listeners.splice(0).forEach(remove => remove());
    release();
  }
  if (scope) release = scope.defer(dispose);
  return {load:() => request(true), current, dispose,
    reset() { blocked = false; clearIntent(); },
    get loading() { return Boolean(running); }, get blocked() { return blocked; }};
}

// Restore only a reading anchor. Every page still comes from the live,
// authenticated history endpoint; neither results nor action URLs are saved.
export async function restoreHistoryAnchor({reading, hasAnchor, pending, load, scope,
  progress, maxPages = 32}) {
  if (!reading?.anchor || reading.atBottom) return {status:'not-needed', pages:0};
  let pages = 0;
  const pageLimit = Number.isInteger(maxPages) && maxPages >= 0 ? Math.min(maxPages, 128) : 32;
  const current = () => !scope || scope.current();
  while (current()) {
    if (hasAnchor(reading.anchor)) return {status:'found', pages};
    if (!pending()) return {status:'exhausted', pages};
    if (pages >= pageLimit) return {status:'limit', pages};
    const before = progress?.();
    try {
      const result = await load();
      pages++;
      if (!current()) break;
      if (result === false) return {status:'failed', pages};
    } catch (error) {
      if (!current()) break;
      return {status:'failed', pages, error};
    }
    if (progress && Object.is(before, progress()) && !hasAnchor(reading.anchor)) return {status:'stalled', pages};
  }
  return {status:'canceled', pages};
}
