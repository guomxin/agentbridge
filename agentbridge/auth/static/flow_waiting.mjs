// Read-only status continuation. Decisions remain in the original trusted forms.
export function mountTrustedFlow(config, environment = {}) {
  const window = environment.window || globalThis.window;
  const document = environment.document || window.document;
  const fetch = environment.fetch || window.fetch.bind(window);
  const location = window.location;
  const title = document.getElementById('flow-title');
  const message = document.getElementById('flow-message');
  const recovery = document.getElementById('flow-recovery');
  const batch = document.getElementById('flow-batch');
  const continued = document.getElementById('flow-continue');
  let stopped = false, failures = 0, after = '';
  let epoch = 0, terminal = false, restorePending = false;
  let controller = null, timer = null, finishBackoff = null;

  function stop() {
    stopped = true;
    epoch++;
    controller?.abort();
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
    finishBackoff?.();
    finishBackoff = null;
  }
  window.addEventListener('pagehide', () => {
    restorePending = !terminal;
    stop();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted && restorePending) {
      restorePending = false;
      stopped = false;
      done = poll();
    }
  });

  function nextUrl(value, kind) {
    const next = new URL(value, location.origin);
    const path = kind === 'authorize'
      ? /^\/flow\/authorize\/[A-Za-z0-9_-]{32,128}(?:\/present\/[A-Za-z0-9_-]{32,128})?$/
      : /^\/flow\/input\/[A-Za-z0-9_-]{32,128}(?:\/present\/[A-Za-z0-9_-]{32,128})?$/;
    if (next.origin !== location.origin || next.username || next.password || !path.test(next.pathname)
        || next.search || next.hash) throw new Error('invalid continuation');
    next.hash = location.hash;
    return next;
  }

  function backoff() {
    return new Promise(resolve => {
      finishBackoff = resolve;
      timer = window.setTimeout(() => {
        timer = null; finishBackoff = null; resolve();
      }, Math.min(10000, 500 * 2 ** Math.min(failures - 1, 5)));
    });
  }

  async function poll() {
    const runEpoch = epoch;
    const current = () => !stopped && runEpoch === epoch;
    while (current()) {
      try {
        const url = new URL(config.statusUrl, location.origin);
        if (url.origin !== location.origin || url.username || url.password || url.search || url.hash
            || !/^\/flow\/(?:input|authorize)\/[A-Za-z0-9_-]{32,128}(?:\/present\/[A-Za-z0-9_-]{32,128})?\/status$/.test(url.pathname)) {
          throw new Error('invalid status URL');
        }
        url.searchParams.set('wait', '20');
        if (after) url.searchParams.set('after', after);
        controller = new AbortController();
        const response = await fetch(url.href, {
          method: 'GET', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
          headers: {'X-AgentBridge-Flow-Token': config.token}
        });
        if (!current()) return;
        const state = await response.json();
        if (!current()) return;
        if (!response.ok && response.status !== 403) throw new Error('status unavailable');
        if (typeof state.title !== 'string' || typeof state.message !== 'string'
            || (response.status === 403 && state.terminal !== true)) throw new Error('invalid status');
        if (response.ok && !/^[A-Za-z0-9_-]{32,128}$/.test(state.stateVersion || '')) {
          throw new Error('invalid status version');
        }
        title.textContent = state.title;
        message.textContent = state.message;
        recovery.textContent = '';
        continued.hidden = true;
        continued.removeAttribute('href');
        batch.hidden = !state.batch;
        if (state.batch) {
          batch.textContent = `本批次已完成 ${state.batch.completedCount} / ${state.batch.totalCount} 项`;
          if (state.batch.nextOrdinal) batch.textContent += `；下一项（第 ${state.batch.nextOrdinal} 项）：${state.batch.nextTitle}`;
        }
        failures = 0;
        after = state.stateVersion || '';
        if (state.nextUrl && state.state === 'review_ready' && !state.terminal) {
          const next = nextUrl(state.nextUrl, 'authorize');
          terminal = true;
          stop();
          location.replace(next.href);
          return;
        }
        if (state.continueUrl && state.state === 'completed') {
          const next = nextUrl(state.continueUrl, 'input');
          continued.href = next.href;
          continued.hidden = false;
        }
        if (state.terminal === true) { terminal = true; stop(); return; }
      } catch (_error) {
        if (!current() || controller?.signal.aborted) return;
        failures += 1;
        recovery.textContent = '连接暂不可用，正在等待恢复。已提交内容不会重发。';
        await backoff();
      }
      // A healthy response immediately opens the next server-side wait. There
      // is one request at a time and no browser-side polling delay or POST retry.
    }
  }
  let done = poll();
  return {stop() { restorePending = false; stop(); }, get done() { return done; }};
}
