// History is loaded in bounded pages; loading is separate from retention.
export function historyPresentation(state) {
  const pending = state.chatTimelineHasOlder || state.taskHistoryHasMore || !state.legacyHistoryLoaded;
  const busy = Boolean(state.historyLoading || state.chatTimelineLoadingOlder || state.taskHistoryLoading || state.legacyHistoryLoading);
  return { pending, busy,
    label: busy ? "正在加载历史内容…" : "加载更早内容",
    notice: state.legacyHistoryLoaded
      ? "已加载当前范围的消息和任务。旧会话仅补充最近120条文字，不代表全部历史；旧图片不在此补充范围。" + (pending ? "仍有更早消息或任务，可继续加载。" : "当前可分页读取的消息和任务已加载完毕。")
      : "当前先显示近期内容；更早消息、任务、文件和旧会话记录可统一加载。",
  };
}

export async function loadHistoryPage({state, getScope, loadMessages, loadTasks, loadLegacy, render}) {
  if (historyPresentation(state).busy) return;
  const scope = getScope();
  state.historyLoading = true; render();
  try {
    // Each source preserves its own cursor and retry state. Do not share a
    // mutable render/scroll transaction between concurrent page requests.
    if (state.chatTimelineHasOlder) await loadMessages();
    scope.assertCurrent();
    if (state.taskHistoryHasMore) await loadTasks();
    scope.assertCurrent();
    if (!state.legacyHistoryLoaded) await loadLegacy();
    scope.assertCurrent();
  } finally {
    if (scope.current()) { state.historyLoading = false; render(); }
  }
}

export function appendMessageImages({document, container, images, formatTime, onOpen, scope, now = Date.now}) {
  if (!Array.isArray(images) || !images.length) return;
  const list = document.createElement("div"); list.className = "message-image-list";
  for (const image of images) {
    const source = image.dataUrl || image.mediaUrl;
    const fileName = image.fileName || "附加图片";
    const figure = document.createElement("figure"); figure.className = "message-image-record";
    const open = document.createElement("button"); open.type = "button"; open.className = "message-image-button";
    open.title = `放大查看 ${fileName}`; open.setAttribute("aria-label", `放大查看 ${fileName}`);
    const caption = document.createElement("figcaption"); caption.className = "message-image-retention";
    const preview = document.createElement("img"); preview.alt = fileName; preview.loading = "lazy";
    const expires = Date.parse(image.expiresAt || "");
    let unavailable = false;
    const expired = () => image.state === "expired" || (Number.isFinite(expires) && expires <= now());
    const update = () => {
      if (expired() || image.state === "unavailable" || !source) {
        open.disabled = true;
        preview.removeAttribute("src");
        open.textContent = expired() ? "原图已过期" : "原图已不可用";
        caption.textContent = `${fileName} · ${expired() ? "保留期限已到，历史消息仍保留" : "历史记录保留，原图不可读取"}`;
      } else {
        open.disabled = unavailable;
        caption.textContent = `${fileName}${Number.isFinite(expires) ? ` · 原图保留至 ${formatTime(image.expiresAt)}` : ""}${unavailable ? " · 图片暂时无法加载，可稍后刷新重试" : ""}`;
      }
    };
    preview.addEventListener("error", () => { unavailable = true; preview.classList.add("unavailable"); update(); });
    open.addEventListener("click", () => {
      if (expired()) { update(); return; }
      if (!open.disabled) onOpen({source, downloadSource:image.downloadUrl || source, fileName, expiresAt:image.expiresAt});
    });
    open.append(preview); figure.append(open, caption); list.append(figure);
    if (!expired() && image.state !== "unavailable" && source) preview.src = source;
    update();
    if (!expired() && Number.isFinite(expires) && scope) {
      const arm = () => scope.timeout(() => { update(); if (!expired()) arm(); }, Math.min(2147483647, Math.max(1, expires - now() + 1)));
      arm();
    }
  }
  container.append(list);
}
