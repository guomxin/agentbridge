/** Canvas-only local PDF rendering. No PDF scripts, actions, links, forms or network URLs. */
export const PDF_ASSET_ROOT = '/assets/pdfjs/6.4.299/';
const PDF_FILE_LIMIT = 8 * 1024 * 1024, PDF_PAGE_LIMIT = 2000, PDF_PIXEL_LIMIT = 6 * 1024 * 1024;

export async function renderPdfMaterial({document, window, target, bytes, isCurrent = () => true,
  onStatus = () => {}, signal, loadLibrary = () => import('/assets/pdfjs/6.4.299/build/pdf.min.mjs')}) {
  if (!(bytes instanceof Uint8Array) || !bytes.length || bytes.length > PDF_FILE_LIMIT) throw new Error('PDF 大小超出预览限制，请在 OA 中查看。');
  if (typeof window.Worker !== 'function') throw new Error('当前客户端不支持隔离的 PDF 预览，请在支持的浏览器或 OA 中查看。');
  let alive = true, loading = null, pdf = null, worker = null, renderTask = null, currentPage = null;
  let busy = false, pageNumber = 1, pageCount = 0, generation = 0;
  const timers = new Set(), cancelWaits = new Set(), shown = new Set();
  const make = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  const shell = make('section'), toolbar = make('div'), display = make('div'), label = make('p');
  shell.className = 'material-pdf-viewer'; toolbar.className = 'material-preview-actions';
  label.setAttribute('role', 'status');
  const previous = make('button', '上一页'), next = make('button', '下一页'), jump = make('button', '查看页码');
  for (const button of [previous, next, jump]) { button.type = 'button'; button.className = 'secondary'; }
  const pageInput = make('input'); pageInput.type = 'number'; pageInput.min = '1'; pageInput.step = '1'; pageInput.value = '1';
  pageInput.setAttribute('aria-label', 'PDF 页码'); pageInput.style.width = '6em';
  toolbar.append(previous, pageInput, jump, next); shell.append(toolbar, label, display); target.append(shell);
  const live = () => alive && !signal?.aborted && isCurrent();
  function destroy() {
    if (!alive) return;
    alive = false; generation++;
    for (const cancel of [...cancelWaits]) cancel();
    for (const timer of timers) window.clearTimeout(timer); timers.clear();
    try { renderTask?.cancel(); } catch {}
    try { currentPage?.cleanup(); } catch {}
    try { const value = loading?.destroy(); value?.catch?.(() => {}); } catch {}
    try { worker?.terminate(); } catch {}
    signal?.removeEventListener('abort', destroy);
    for (const node of display.children || []) if (node.tagName?.toLowerCase() === 'canvas') { node.width = 0; node.height = 0; }
    target.replaceChildren();
  }
  signal?.addEventListener('abort', destroy, {once: true});
  function bounded(promise, milliseconds) {
    if (!live()) return Promise.reject(new Error('PDF 预览已关闭。'));
    let timer, cancel;
    const deadline = new Promise((_, reject) => {
      cancel = () => reject(new Error('PDF 预览已关闭。'));
      cancelWaits.add(cancel);
      timer = window.setTimeout(() => reject(new Error('PDF 读取或显示超时，请重新获取或在 OA 中查看。')), milliseconds);
      timers.add(timer);
    });
    return Promise.race([promise, deadline]).then(value => {
      if (!live()) throw new Error('PDF 预览已关闭。');
      return value;
    }).finally(() => { window.clearTimeout(timer); timers.delete(timer); cancelWaits.delete(cancel); });
  }
  function controls() {
    previous.disabled = busy || !alive || pageNumber <= 1;
    next.disabled = busy || !alive || pageNumber >= pageCount;
    jump.disabled = busy || !alive; pageInput.disabled = busy || !alive;
  }
  function progress() {
    const remaining = pageCount - shown.size;
    const text = `第 ${pageNumber} / ${pageCount} 页 · 已展示 ${shown.size} 页${remaining ? `，还有 ${remaining} 页未展示，请翻页核对。` : '，全部页面已展示。'} 展示不代表已核对。`;
    label.textContent = text; onStatus(text);
  }
  async function show(number) {
    if (!live() || busy || !Number.isInteger(number) || number < 1 || number > pageCount) return;
    busy = true; controls(); const ticket = ++generation;
    label.textContent = `正在显示第 ${number} 页…`;
    try {
      await bounded((async () => {
        const page = await pdf.getPage(number);
        if (!live() || ticket !== generation) { page.cleanup(); return; }
        const unit = page.getViewport({scale: 1});
        if (![unit.width, unit.height].every(value => Number.isFinite(value) && value > 0)) throw new Error('PDF 页面尺寸无效。');
        const scale = Math.min(1.5, 4096 / unit.width, 4096 / unit.height, Math.sqrt(PDF_PIXEL_LIMIT / (unit.width * unit.height)));
        const viewport = page.getViewport({scale});
        const canvas = make('canvas'); canvas.width = Math.max(1, Math.floor(viewport.width)); canvas.height = Math.max(1, Math.floor(viewport.height));
        if (canvas.width * canvas.height > PDF_PIXEL_LIMIT) throw new Error('PDF 页面超过显示限制。');
        canvas.style.width = '100%'; canvas.style.maxWidth = `${canvas.width}px`; canvas.style.height = 'auto';
        canvas.setAttribute('aria-label', `PDF 第 ${number} 页`); canvas.setAttribute('role', 'img');
        const context = canvas.getContext('2d', {alpha: false});
        if (!context) throw new Error('当前客户端无法显示 PDF 页面。');
        // Paint annotation appearances into the canvas, without an interactive
        // annotation layer, form controls, links or PDF action execution.
        renderTask = page.render({canvasContext: context, viewport, intent: 'display', annotationMode: 1,
          background: 'rgb(255,255,255)'});
        await renderTask.promise;
        if (!live() || ticket !== generation) { canvas.width = canvas.height = 0; page.cleanup(); return; }
        for (const old of display.children || []) if (old.tagName?.toLowerCase() === 'canvas') { old.width = old.height = 0; }
        currentPage?.cleanup(); currentPage = page; renderTask = null;
        display.replaceChildren(canvas); pageNumber = number; pageInput.value = String(number); shown.add(number); progress();
      })(), 15000);
    } finally { busy = false; if (live()) controls(); }
  }
  function navigate(number) {
    if (!Number.isInteger(number) || number < 1 || number > pageCount) { label.textContent = `请输入 1 至 ${pageCount} 之间的页码。`; return; }
    void show(number).catch(error => { if (live()) { const message = error.message || 'PDF 无法继续显示，请重新获取。'; destroy(); onStatus(message); } });
  }
  previous.addEventListener('click', () => navigate(pageNumber - 1));
  next.addEventListener('click', () => navigate(pageNumber + 1));
  jump.addEventListener('click', () => navigate(Number(pageInput.value)));
  pageInput.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); navigate(Number(pageInput.value)); } });
  try {
    controls();
    const library = await bounded(loadLibrary(), 15000);
    // An explicit real worker avoids PDF.js's main-thread fake-worker fallback.
    worker = new window.Worker(`${PDF_ASSET_ROOT}build/pdf.worker.min.mjs`, {type: 'module'});
    const pdfWorker = new library.PDFWorker({port: worker});
    loading = library.getDocument({data: bytes.slice(), worker: pdfWorker,
      cMapUrl: `${PDF_ASSET_ROOT}cmaps/`, cMapPacked: true,
      standardFontDataUrl: `${PDF_ASSET_ROOT}standard_fonts/`, wasmUrl: `${PDF_ASSET_ROOT}wasm/`,
      isEvalSupported: false, enableXfa: false, stopAtErrors: true, useWasm: false, useWorkerFetch: true,
      disableAutoFetch: true, disableStream: true, disableRange: true,
      maxImageSize: 16 * 1024 * 1024, canvasMaxAreaInBytes: PDF_PIXEL_LIMIT * 4});
    loading.onPassword = () => { destroy(); onStatus('此 PDF 需要密码，请在 OA 中查看。'); };
    pdf = await bounded(loading.promise, 20000);
    if (!Number.isInteger(pdf.numPages) || pdf.numPages < 1 || pdf.numPages > PDF_PAGE_LIMIT) throw new Error(`PDF 超过 ${PDF_PAGE_LIMIT} 页的预览限制，请在 OA 中查看。`);
    pageCount = pdf.numPages; pageInput.max = String(pageCount);
    await show(1);
    return {destroy};
  } catch (error) { destroy(); throw error; }
}
