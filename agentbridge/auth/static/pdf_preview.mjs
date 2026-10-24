/** Local PDF canvas and plain text only. No PDF scripts, actions, links, forms or network URLs. */
export const PDF_ASSET_ROOT = '/assets/pdfjs/6.4.299/';
const PDF_FILE_LIMIT = 8 * 1024 * 1024, PDF_PAGE_LIMIT = 2000, PDF_PIXEL_LIMIT = 6 * 1024 * 1024;

export async function renderPdfMaterial({document, window, target, bytes, isCurrent = () => true,
  onStatus = () => {}, signal, initialView = {}, onViewState = () => {}, now = Date.now, loadLibrary = () => import('/assets/pdfjs/6.4.299/build/pdf.min.mjs')}) {
  if (!(bytes instanceof Uint8Array) || !bytes.length || bytes.length > PDF_FILE_LIMIT) throw new Error('PDF 大小超出预览限制，请在 OA 中查看。');
  if (typeof window.Worker !== 'function') throw new Error('当前客户端不支持隔离的 PDF 预览，请在支持的浏览器或 OA 中查看。');
  let alive = true, loading = null, pdf = null, worker = null, renderTask = null, currentPage = null;
  let busy = false, pageNumber = 1, pageCount = 0, generation = 0;
  let fitWidth = initialView.fitWidth !== false, zoom = Number(initialView.zoom) || 1;
  zoom = Math.max(0.1, Math.min(3, zoom));
  let displayedScale = zoom;
  let searching = false, stopSearch = false, searchGeneration = 0, query = '', searchCursor = 1;
  let searchChecked = 0, searchFailures = 0, searchEmpty = 0, searchResults = [];
  const readers = new Set(), textCache = new Map();
  const TEXT_LIMIT = 200000, SEARCH_PAGES = 50, SEARCH_CHARS = 2000000, SEARCH_RESULTS = 100;
  const searchLive = ticket => live() && !stopSearch && ticket === searchGeneration;
  const timers = new Set(), cancelWaits = new Set(), shown = new Set();
  const make = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  const shell = make('section'), toolbar = make('div'), display = make('div'), label = make('p');
  const textSection = make('details'), textTitle = make('summary','本页可选文字与读屏正文'), textStatus = make('p'), textBody = make('div');
  textSection.className = 'material-pdf-text'; textSection.open = initialView.textOpen === true;
  textBody.className = 'material-pdf-text-body'; textBody.style.whiteSpace = 'pre-wrap'; textBody.style.overflowWrap = 'anywhere';
  textBody.setAttribute('role','document'); textBody.setAttribute('aria-label','PDF 本页提取文字');
  textStatus.setAttribute('role','status'); textSection.append(textTitle,textStatus,textBody);
  display.className = 'material-pdf-display'; display.style.overflow = 'auto'; display.style.maxHeight = '70vh'; display.style.maxWidth = '100%';
  const smaller = make('button','缩小'), larger = make('button','放大'), fit = make('button','适合宽度'), scaleLabel = make('span');
  const searchBox = make('div'), searchInput = make('input'), searchButton = make('button','搜索文字'), searchContinue = make('button','继续检索后续页面'), searchStop = make('button','停止检索'), searchStatus = make('p'), results = make('div');
  searchBox.className = 'material-pdf-search'; searchInput.type = 'search'; searchInput.maxLength = 120; searchInput.placeholder = '搜索此 PDF 的可提取文字'; searchInput.setAttribute('aria-label','搜索 PDF 文字');
  searchStatus.setAttribute('role','status'); searchContinue.hidden = searchStop.hidden = true;
  searchBox.append(searchInput,searchButton,searchStop,searchContinue,searchStatus,results);
  shell.className = 'material-pdf-viewer'; toolbar.className = 'material-preview-actions';
  label.setAttribute('role', 'status');
  const previous = make('button', '上一页'), next = make('button', '下一页'), jump = make('button', '查看页码');
  for (const button of [previous, next, jump, smaller, larger, fit, searchButton, searchContinue, searchStop]) { button.type = 'button'; button.className = 'secondary'; }
  const pageInput = make('input'); pageInput.type = 'number'; pageInput.min = '1'; pageInput.step = '1'; pageInput.value = '1';
  pageInput.setAttribute('aria-label', 'PDF 页码'); pageInput.style.width = '6em';
  toolbar.append(previous, pageInput, jump, next, smaller, larger, fit, scaleLabel); shell.append(toolbar, label, searchBox, display, textSection); target.append(shell);
  const getViewState = () => ({page:pageNumber,fitWidth,zoom,textOpen:textSection.open === true,
    scrollTop:Math.max(0,Number(display.scrollTop) || 0),scrollLeft:Math.max(0,Number(display.scrollLeft) || 0)});
  const saveView = () => { if (live()) onViewState(getViewState()); };
  display.addEventListener('scroll',saveView); textSection.addEventListener('toggle',saveView);
  const live = () => alive && !signal?.aborted && isCurrent();
  function destroy() {
    if (!alive) return;
    alive = false; generation++; searchGeneration++; stopSearch = true;
    for (const reader of readers) { try { Promise.resolve(reader.cancel()).catch(() => {}); } catch {} } readers.clear();
    textCache.clear(); searchResults = [];
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
    previous.disabled = busy || searching || !alive || pageNumber <= 1;
    next.disabled = busy || searching || !alive || pageNumber >= pageCount;
    jump.disabled = busy || searching || !alive; pageInput.disabled = busy || searching || !alive;
    smaller.disabled = busy || searching || !alive || displayedScale <= 0.1;
    larger.disabled = busy || searching || !alive || displayedScale >= 3;
    fit.disabled = busy || searching || !alive;
    searchButton.disabled = busy || searching || !alive; searchInput.disabled = searching || !alive;
    searchContinue.disabled = busy || searching || !alive || searchCursor > pageCount || searchResults.length >= SEARCH_RESULTS;
    searchStop.hidden = !searching;
    for (const button of results.children || []) button.disabled = busy || searching || !alive;
  }
  function progress() {
    const remaining = pageCount - shown.size;
    const text = `第 ${pageNumber} / ${pageCount} 页 · 已展示 ${shown.size} 页${remaining ? `，还有 ${remaining} 页未展示，请翻页核对。` : '，全部页面已展示。'} 展示不代表已核对。`;
    label.textContent = text; onStatus(text);
  }
  async function pageText(page, check = live, deadline = now()+10000) {
    let value = '', truncated = false, available = true, reader;
    const append = items => {
      for (const item of items || []) {
        if (typeof item?.str !== 'string') continue;
        const part = item.str + (item.hasEOL ? '\n' : ' '), left = TEXT_LIMIT-value.length;
        value += part.slice(0,Math.max(0,left));
        if (part.length > left || value.length >= TEXT_LIMIT) {truncated = true;return false;}
      }
      return true;
    };
    try {
      if (typeof page.streamTextContent === 'function') {
        reader = page.streamTextContent({includeMarkedContent:false,disableNormalization:false}).getReader(); readers.add(reader);
        let chunks = 0;
        while (check()) {
          if (now() >= deadline) throw new Error('PDF 文字提取已到时间上限。');
          const item = await bounded(reader.read(),Math.max(1,deadline-now()));
          if (!check()) break;
          if (item.done) break;
          if (!append(item.value?.items) || ++chunks >= 2000) {truncated = true;break;}
        }
      } else if (typeof page.getTextContent === 'function') {
        const item = await bounded(page.getTextContent({includeMarkedContent:false,disableNormalization:false}),Math.max(1,deadline-now()));
        if (check()) append(item.items);
      } else available = false;
    } finally {
      if (reader) {readers.delete(reader);try {Promise.resolve(reader.cancel()).catch(()=>{});} catch {}}
    }
    return {value:value.trim(),truncated,available};
  }
  function paintText(value) {
    textBody.textContent = value.value;
    textStatus.textContent = !value.available ? '本页文字暂时无法提取，请阅读页面图像。' : !value.value ?
      '本页没有可提取文字，可能是扫描件或图片。未进行 OCR，请阅读页面图像；文字搜索不能覆盖这一页。' :
      `${value.truncated ? '本页文字超过显示限制，仅提供部分文字；请结合页面图像核对。' : '可选择并复制本页文字，也可使用读屏阅读。'} 提取顺序可能与原页面不同，表格请结合图像核对。`;
  }
  async function show(number) {
    if (!live() || busy || searching || !Number.isInteger(number) || number < 1 || number > pageCount) return;
    busy = true; controls(); const ticket = ++generation;
    const samePage=Boolean(currentPage && number===pageNumber);
    const centerX=((Number(display.scrollLeft)||0)+(display.clientWidth||0)/2)/displayedScale;
    const centerY=((Number(display.scrollTop)||0)+(display.clientHeight||0)/2)/displayedScale;
    label.textContent = `正在显示第 ${number} 页…`; textStatus.textContent = '正在提取本页文字…'; textBody.textContent = '';
    try {
      await bounded((async () => {
        const page = await pdf.getPage(number);
        if (!live() || ticket !== generation) {page.cleanup();return;}
        const unit = page.getViewport({scale:1});
        if (![unit.width,unit.height].every(value => Number.isFinite(value) && value > 0)) throw new Error('PDF 页面尺寸无效。');
        const width = Math.max(1,display.clientWidth || target.clientWidth || 600);
        const desired = fitWidth ? width/unit.width : zoom;
        const scale = Math.min(desired,4096/unit.width,4096/unit.height,Math.sqrt(PDF_PIXEL_LIMIT/(unit.width*unit.height)));
        displayedScale=scale;
        const viewport = page.getViewport({scale});
        const canvas = make('canvas'); canvas.width = Math.max(1,Math.floor(viewport.width)); canvas.height = Math.max(1,Math.floor(viewport.height));
        if (canvas.width*canvas.height > PDF_PIXEL_LIMIT) throw new Error('PDF 页面超过显示限制。');
        canvas.style.width = `${canvas.width}px`; canvas.style.maxWidth = 'none'; canvas.style.height = 'auto';
        canvas.setAttribute('aria-label',`PDF 第 ${number} 页图像；可选文字见下方`); canvas.setAttribute('role','img');
        const context = canvas.getContext('2d',{alpha:false});
        if (!context) throw new Error('当前客户端无法显示 PDF 页面。');
        renderTask = page.render({canvasContext:context,viewport,intent:'display',annotationMode:1,background:'rgb(255,255,255)'});
        await renderTask.promise;
        if (!live() || ticket !== generation) {canvas.width=canvas.height=0;page.cleanup();return;}
        for (const old of display.children || []) if (old.tagName?.toLowerCase()==='canvas') {old.width=old.height=0;}
        if (currentPage !== page) currentPage?.cleanup(); currentPage=page;renderTask=null;
        display.replaceChildren(canvas);pageNumber=number;pageInput.value=String(number);shown.add(number);
        scaleLabel.textContent = `${fitWidth ? '适宽 · ' : ''}${Math.round(scale*100)}%${scale<desired ? '（已限制显示尺寸）' : ''}`;
        display.scrollTop=samePage ? Math.max(0,centerY*scale-(display.clientHeight||0)/2) : 0;
        display.scrollLeft=samePage ? Math.max(0,centerX*scale-(display.clientWidth||0)/2) : 0;
        progress();saveView();
      })(),15000);
      if (live() && ticket===generation) {
        let content = textCache.get(number);
        if (!content) {
          try {content=await pageText(currentPage,()=>live() && ticket===generation);}
          catch {content={value:'',available:false,truncated:false};}
          if (live() && ticket===generation) {textCache.set(number,content);while(textCache.size>3) textCache.delete(textCache.keys().next().value);}
        }
        if (live() && ticket===generation) paintText(content);
      }
    } finally {busy=false;if(live()) controls();}
  }
  function searchProgress(note = '') {
    const remaining=Math.max(0,pageCount-searchCursor+1), complete=remaining===0 && searchFailures===0 && searchEmpty===0;
    searchStatus.textContent = `${note}${complete ? `已检索全部 ${pageCount} 页的可提取文字` : `已检查 ${searchChecked} / ${pageCount} 页，结果仅来自已提取的文字`}`+
      `；找到 ${searchResults.length} 个匹配页面。${remaining ? `还有 ${remaining} 页未检索。` : ''}${searchEmpty ? `${searchEmpty} 页没有可提取文字，搜索不覆盖图像。` : ''}`+
      `${searchFailures ? `${searchFailures} 页未完整检索，请结合原图核对。` : ''}${searchResults.length>=SEARCH_RESULTS && remaining ? '匹配页面已达显示上限，请缩小关键词重新搜索。' : ''} 检索不代表材料已核对。`;
    searchContinue.hidden = remaining===0 || searchResults.length>=SEARCH_RESULTS;
  }
  async function search(continuing = false) {
    if (!live() || busy || searching) return;
    const term=String(searchInput.value || '').trim().slice(0,120);
    if (!term) {searchStatus.textContent='请输入要查找的文字。';return;}
    if (!continuing || term!==query) {query=term;searchCursor=1;searchChecked=searchFailures=searchEmpty=0;searchResults=[];results.replaceChildren();}
    if (searchCursor>pageCount || searchResults.length>=SEARCH_RESULTS) return;
    searching=true;stopSearch=false;const ticket=++searchGeneration,started=now();let pages=0,characters=0;
    controls();searchProgress('正在检索…');
    try {
      while(searchLive(ticket) && searchCursor<=pageCount && pages<SEARCH_PAGES && characters<SEARCH_CHARS && searchResults.length<SEARCH_RESULTS && now()-started<10000) {
        const number=searchCursor;let page,content;
        try {
          page=await bounded(pdf.getPage(number),Math.max(1,10000-(now()-started)));
          if (!searchLive(ticket)) break;
          content=textCache.get(number) || await pageText(page,()=>searchLive(ticket),started+10000);
        } catch {content={value:'',available:false,truncated:true};}
        finally {if(page && page!==currentPage) page.cleanup();}
        if (!searchLive(ticket)) break;
        if (!content.available || content.truncated) searchFailures++;
        if (content.available && !content.value) searchEmpty++;
        const index=content.value.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
        if(index>=0) {
          const excerpt=content.value.slice(Math.max(0,index-40),Math.min(content.value.length,index+query.length+80));
          const result=make('button',`第 ${number} 页：${excerpt}`);result.type='button';result.className='secondary material-pdf-search-result';result.disabled=true;
          result.addEventListener('click',()=>{if(!searching){textSection.open=true;navigate(number);}});results.append(result);searchResults.push(number);
        }
        searchCursor++;searchChecked++;pages++;characters+=content.value.length;searchProgress('正在检索…');
      }
    } finally {
      if(live() && ticket===searchGeneration) {searching=false;searchProgress(stopSearch ? '已停止。' : searchCursor<=pageCount ? '本轮检索已到上限，可继续。' : '');controls();}
    }
  }
  searchButton.addEventListener('click',()=>void search());searchContinue.addEventListener('click',()=>void search(true));
  searchInput.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();void search();}});
  searchStop.addEventListener('click',()=>{stopSearch=true;searchStatus.textContent='正在停止检索，已得到的结果会保留。';for(const reader of readers){try{Promise.resolve(reader.cancel()).catch(()=>{});}catch{}}});
  smaller.addEventListener('click',()=>{fitWidth=false;zoom=Math.max(0.1,displayedScale/1.25);navigate(pageNumber);});
  larger.addEventListener('click',()=>{fitWidth=false;zoom=Math.min(3,Math.max(0.1,displayedScale*1.25));navigate(pageNumber);});
  fit.addEventListener('click',()=>{fitWidth=true;navigate(pageNumber);});
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
    const requested=Number(initialView.page);
    await show(Number.isInteger(requested) && requested>=1 && requested<=pageCount ? requested : 1);
    display.scrollTop=Math.max(0,Number(initialView.scrollTop)||0);display.scrollLeft=Math.max(0,Number(initialView.scrollLeft)||0);saveView();
    return {destroy,getViewState};
  } catch (error) { destroy(); throw error; }
}
