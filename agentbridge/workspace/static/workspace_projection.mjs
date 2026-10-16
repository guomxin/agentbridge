// Share ordering across current snapshots, history pages and event batches.
// Keep retirement tombstones until every older read is ineligible, including
// same-version changes to access, query TTL or signed presentation URLs.
export function createProjectionOrder({ capacity = 4096, onOverflow = () => {} } = {}) {
  let epoch = 0, serial = 0, floor = 0;
  const latest = new Map();
  function room(id) {
    if (!latest.has(id) && latest.size >= capacity) {
      floor = ++serial; latest.clear(); onOverflow();
    }
  }
  return {
    begin: () => ({ epoch, sequence: ++serial }),
    invalidate(id) { room(id); latest.set(id, ++serial); },
    accept(id, ticket) {
      if (!id || ticket?.epoch !== epoch || ticket.sequence < floor || ticket.sequence < (latest.get(id) || 0)) return false;
      room(id);
      if (ticket.sequence < floor) return false;
      latest.set(id, ticket.sequence); return true;
    },
    reset() { epoch++; serial = 0; floor = 0; latest.clear(); },
  };
}

// Snapshot rows are independent of event replay: a later projection cannot
// advance the durable cursor past events that the browser has never received.
export function applyCurrentSnapshot(state, snapshot, { ingest, upsert, retire }) {
  if (snapshot.schemaVersion !== "agentbridge.workspace-current.v1") throw new Error("当前工作区响应无效，请刷新重试。");
  const initial = !state.currentLoaded;
  for (const entry of snapshot.timeline?.items || []) ingest(entry, false, false);
  for (const detail of snapshot.cards || []) upsert(detail, { render: false });
  for (const id of snapshot.unavailableTaskIds || []) retire(id);
  const oldest = Number(snapshot.timeline?.oldestSequence) || 0;
  if (oldest && (!state.chatTimelineOldestSequence || oldest < state.chatTimelineOldestSequence)) state.chatTimelineOldestSequence = oldest;
  if (initial) {
    state.chatTimelineHasOlder = Boolean(snapshot.timeline?.hasMore);
    state.timelineCursor = Math.max(0, Number(snapshot.cursors?.timeline) || 0);
  }
  state.currentLoaded = true;
  return initial;
}

// Coalesce read-only refreshes without letting an older response revive a card.
export function createProjectionQueue({ fetchBatch, apply, retire, render, onError = () => {},
  onOverflow = () => {}, timers = globalThis, delay = 80, batchSize = 32, capacity = 256 }) {
  let epoch = 0, serial = 0, timer = null, running = false;
  const pending = new Map(), latest = new Map();
  function arm() {
    if (timer !== null || running || !pending.size) return;
    timer = timers.setTimeout(() => { timer = null; void flush(); }, delay);
  }
  function enqueue(id) {
    if (!id) return;
    if (!pending.has(id) && !latest.has(id) && latest.size >= capacity) { onOverflow(); return; }
    const ticket = ++serial;
    pending.set(id, ticket); latest.set(id, ticket); arm();
  }
  async function flush() {
    if (running || !pending.size) return;
    const ownEpoch = epoch, batch = new Map([...pending].slice(0, batchSize));
    for (const id of batch.keys()) pending.delete(id);
    running = true;
    try {
      const result = await fetchBatch([...batch.keys()]);
      if (ownEpoch !== epoch) return;
      const fresh = id => batch.has(id) && latest.get(id) === batch.get(id);
      for (const item of result.items || []) {
        if (fresh(item.taskId) && !item.unchanged && item.detail?.task?.task_id === item.taskId) apply(item.detail);
      }
      for (const id of result.unavailableTaskIds || []) if (fresh(id)) retire(id);
      render();
    } catch (error) {
      if (ownEpoch === epoch) onError(error);
    } finally {
      if (ownEpoch === epoch) {
        for (const [id, ticket] of batch) if (latest.get(id) === ticket) latest.delete(id);
        running = false; arm();
      }
    }
  }
  function reset() {
    epoch += 1;
    if (timer !== null) timers.clearTimeout(timer);
    timer = null; running = false; pending.clear(); latest.clear();
  }
  return { enqueue, reset };
}
