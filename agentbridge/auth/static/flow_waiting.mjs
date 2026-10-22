// Read-only status continuation. Decisions remain in the original trusted forms.
export function mountTrustedFlow(config, environment = {}) {
  const window = environment.window || globalThis.window;
  const document = environment.document || window.document;
  const fetch = environment.fetch || window.fetch.bind(window);
  const now = environment.now || (() => window.performance?.now?.() ?? Date.now());
  const openedAt = now();
  const location = window.location;
  const title = document.getElementById('flow-title');
  const message = document.getElementById('flow-message');
  const recovery = document.getElementById('flow-recovery');
  const batch = document.getElementById('flow-batch');
  const continued = document.getElementById('flow-continue');
  let stopped = false, failures = 0, after = '';
  let epoch = 0, terminal = false, restorePending = false;
  let controller = null, timer = null, finishBackoff = null;
  let deadlineTimer = null, elapsedTimer = null, recoveryReason = '';

  function showElapsed() {
    const seconds = Math.max(0, Math.floor((now() - openedAt) / 1000));
    recovery.textContent = recoveryReason
      ? `${recoveryReason} 本页已等待 ${seconds} 秒。已提交内容不会重发。`
      : seconds >= 5 ? `本页已等待 ${seconds} 秒，正在读取最新状态。已提交内容不会重发。` : '';
  }

  function trackElapsed() {
    if (stopped || terminal) return;
    showElapsed();
    elapsedTimer = window.setTimeout(() => { elapsedTimer = null; trackElapsed(); }, 1000);
  }

  function stop() {
    stopped = true;
    epoch++;
    controller?.abort();
    if (timer !== null) window.clearTimeout(timer);
    if (deadlineTimer !== null) window.clearTimeout(deadlineTimer);
    if (elapsedTimer !== null) window.clearTimeout(elapsedTimer);
    deadlineTimer = elapsedTimer = null;
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
      trackElapsed();
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

  async function readStatus(url) {
    const request = new AbortController();
    controller = request;
    let timedOut = false;
    const cancelled = new Promise((_, reject) => {
      request.signal.addEventListener('abort', () => reject(Object.assign(new Error('status read cancelled'),
        {code: timedOut ? 'FLOW_STATUS_TIMEOUT' : 'FLOW_STATUS_ABORTED'})), {once: true});
    });
    const requestTimer = window.setTimeout(() => { timedOut = true; request.abort(); }, 30000);
    deadlineTimer = requestTimer;
    try {
      return await Promise.race([cancelled, (async () => {
        const response = await fetch(url.href, {
          method: 'GET', credentials: 'same-origin', cache: 'no-store', signal: request.signal,
          headers: {'X-AgentBridge-Flow-Token': config.token}
        });
        // A revoked/expired entry is final even when its error body is absent,
        // malformed or never finishes. It must not remain in a retry loop.
        if ([401, 403, 404, 410].includes(response.status)) return {response, state: null};
        return {response, state: await response.json()};
      })()]);
    } finally {
      window.clearTimeout(requestTimer);
      if (deadlineTimer === requestTimer) deadlineTimer = null;
      if (controller === request) controller = null;
    }
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
        const {response, state} = await readStatus(url);
        if (!current()) return;
        if ([401, 403, 404, 410].includes(response.status)) {
          title.textContent = '状态入口已失效';
          message.textContent = '页面已过期或当前无权查看，请回到原任务查看办理结果。';
          recovery.textContent = '已停止自动刷新。已提交内容不会重发。';
          continued.hidden = true; continued.removeAttribute('href');
          terminal = true; stop(); return;
        }
        if (!response.ok) throw new Error('status unavailable');
        if (typeof state?.title !== 'string' || typeof state.message !== 'string') throw new Error('invalid status');
        if (response.ok && !/^[A-Za-z0-9_-]{32,128}$/.test(state.stateVersion || '')) {
          throw new Error('invalid status version');
        }
        title.textContent = state.title;
        message.textContent = state.message;
        recoveryReason = '';
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
      } catch (error) {
        if (!current() || error?.code === 'FLOW_STATUS_ABORTED') return;
        failures += 1;
        recoveryReason = error?.code === 'FLOW_STATUS_TIMEOUT'
          ? '状态读取超过 30 秒，正在重新连接。' : '连接暂不可用，正在等待恢复。';
        showElapsed();
        await backoff();
      }
      // A healthy response immediately opens the next server-side wait. There
      // is one request at a time and no browser-side polling delay or POST retry.
    }
  }
  trackElapsed();
  let done = poll();
  return {stop() { restorePending = false; stop(); }, get done() { return done; }};
}
