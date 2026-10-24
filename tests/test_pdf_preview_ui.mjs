import assert from 'node:assert/strict';
import test from 'node:test';
import {renderPdfMaterial, PDF_ASSET_ROOT} from '../agentbridge/auth/static/pdf_preview.mjs';
import {createDocument, descendants} from './workspace_dom_fixture.mjs';
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve};};
function harness({pages = 3, dimensions = [600, 800], parse, pageRender, textContent, streamText} = {}) {
  const document = createDocument(), target = document.createElement('div'), timers = new Map(), workers = [], status = [], seen = [], options = [];
  const create = document.createElement.bind(document);
  document.createElement = tag => { const node = create(tag); node.tagName = tag.toUpperCase(); node.click = () => node.events.click?.(); node.style = {}; if (tag === 'canvas') node.getContext = () => ({}); return node; };
  let current = true, timerId = 0, destroyed = 0;
  const window = {setTimeout(fn, delay) {timers.set(++timerId, {fn, delay}); return timerId;}, clearTimeout(id) {timers.delete(id);},
    Worker: class {constructor(url, settings) {this.url = url; this.settings = settings; workers.push(this);} terminate() {this.terminated = true;}}};
  const pdf = {numPages: pages, getPage: async number => ({getViewport: ({scale}) => ({width: dimensions[0] * scale, height: dimensions[1] * scale}),
    getTextContent:async()=>textContent ? textContent(number) : {items:[{str:`第 ${number} 页可选正文`,hasEOL:true}]},
    ...(streamText ? {streamTextContent:()=>streamText(number)} : {}),
    render(value) {seen.push({number, ...value}); return {promise: pageRender || Promise.resolve(), cancel() {}};}, cleanup() {}})};
  const loading = {promise: parse || Promise.resolve(pdf), destroy() {destroyed++; return Promise.resolve();}};
  const library = {PDFWorker: class {constructor(value) {this.value = value;}}, getDocument(value) {options.push(value); return loading;}};
  const args = {document, window, target, bytes: new Uint8Array([37,80,68,70,45]), isCurrent: () => current,
    onStatus: text => status.push(text), loadLibrary: async () => library};
  return {args, target, workers, status, options, seen, timers, loading, get destroyed() {return destroyed;},
    stale() {current = false;}, fire(ms) {const entry = [...timers.values()].find(t => t.delay === ms); assert.ok(entry, `timer ${ms}`); entry.fn();},
    button(text) {return descendants(target).find(node => node.textContent === text && node.tagName === 'BUTTON');}};
}

test('local PDF renderer shows actual canvas pages, reports undisplayed pages and disables executable surfaces', async () => {
  const h = harness(), renderer = await renderPdfMaterial(h.args);
  assert.equal(h.workers[0].url, `${PDF_ASSET_ROOT}build/pdf.worker.min.mjs`);
  const options = h.options[0];
  assert.deepEqual([...options.data], [37,80,68,70,45]);
  assert.equal(options.url, undefined); assert.equal(options.isEvalSupported, false); assert.equal(options.enableXfa, false); assert.equal(options.useWasm, false);
  assert.equal(h.seen[0].annotationMode, 1);
  assert.match(h.status.at(-1), /第 1 \/ 3 页.*还有 2 页未展示/);
  h.button('下一页').click(); await flush();
  assert.match(h.status.at(-1), /第 2 \/ 3 页.*还有 1 页未展示/);
  h.button('下一页').click(); await flush();
  assert.match(h.status.at(-1), /全部页面已展示.*展示不代表已核对/);
  assert.equal(h.button('下一页').disabled, true);
  assert.equal(descendants(h.target).filter(n => n.tagName === 'CANVAS').length, 1);
  renderer.destroy(); assert.equal(h.workers[0].terminated, true); assert.equal(h.target.children.length, 0);
});

test('plain selectable text retains source content without active HTML or annotation links',async()=>{
  const h=harness({textContent:()=>({items:[{str:'<script>danger()</script>',hasEOL:true},{str:'第二段材料',hasEOL:true}]})});
  const renderer=await renderPdfMaterial(h.args),body=descendants(h.target).find(node=>node.className==='material-pdf-text-body');
  assert.equal(body.textContent,'<script>danger()</script>\n第二段材料');assert.equal(body.children.length,0);
  assert.equal(body.attributes.role,'document');assert.match(h.target.textContent,/提取顺序可能与原页面不同/);
  assert.equal(descendants(h.target).some(node=>['SCRIPT','A','IFRAME','OBJECT'].includes(node.tagName)),false);renderer.destroy();
});

test('scan pages explicitly lack searchable text and never report full text coverage',async()=>{
  const h=harness({pages:2,textContent:()=>({items:[]})}),renderer=await renderPdfMaterial(h.args);
  assert.match(h.target.textContent,/没有可提取文字.*未进行 OCR/);
  const input=descendants(h.target).find(node=>node.type==='search');input.value='考勤';h.button('搜索文字').click();await flush();
  assert.match(h.target.textContent,/2 页没有可提取文字，搜索不覆盖图像/);assert.doesNotMatch(h.target.textContent,/已检索全部/);renderer.destroy();
});

test('search is bounded to fifty pages per run and an explicit continuation searches the remaining pages',async()=>{
  const h=harness({pages:52,textContent:number=>({items:[{str:`关键词 合成第${number}页 尾部${number}`,hasEOL:true}]})}),renderer=await renderPdfMaterial(h.args);
  const input=descendants(h.target).find(node=>node.type==='search');input.value='关键词';h.button('搜索文字').click();await flush();
  assert.match(h.target.textContent,/已检查 50 \/ 52 页/);assert.match(h.target.textContent,/还有 2 页未检索/);assert.doesNotMatch(h.target.textContent,/已检索全部/);
  h.button('继续检索后续页面').click();await flush();assert.match(h.target.textContent,/已检索全部 52 页的可提取文字/);
  const result=descendants(h.target).find(node=>node.className==='secondary material-pdf-search-result' && node.textContent.startsWith('第 52 页：'));
  result.click();await flush();assert.match(h.status.at(-1),/第 52 \/ 52 页/);renderer.destroy();
});

test('oversized text and failed extraction remain explicitly partial without losing the rendered image',async()=>{
  const h=harness({pages:2,textContent:number=>{if(number===2)throw Error('text unavailable');return {items:[{str:'目标'.repeat(110000)}]};}});
  const renderer=await renderPdfMaterial(h.args);assert.match(h.target.textContent,/仅提供部分文字/);
  assert.ok(descendants(h.target).find(node=>node.className==='material-pdf-text-body').textContent.length<=200000);
  const input=descendants(h.target).find(node=>node.type==='search');input.value='目标';h.button('搜索文字').click();await flush();
  assert.match(h.target.textContent,/2 页未完整检索/);assert.doesNotMatch(h.target.textContent,/已检索全部/);
  assert.equal(descendants(h.target).filter(node=>node.tagName==='CANVAS').length,1);renderer.destroy();
});

test('stopping and closing a streamed text search cancel its reader and late chunks cannot repaint',async()=>{
  const pending=deferred();let canceled=0;
  const h=harness({pages:3,streamText:number=>({getReader(){let count=0;return {
    read:()=>number===2 ? pending.promise : Promise.resolve(count++ ? {done:true} : {done:false,value:{items:[{str:'可查文字'}]}}),
    cancel(){canceled++;if(number===2)pending.resolve({done:true});return Promise.resolve();},
  };}})}),renderer=await renderPdfMaterial(h.args);
  const input=descendants(h.target).find(node=>node.type==='search');input.value='文字';h.button('搜索文字').click();await flush();
  h.button('停止检索').click();await flush();assert.ok(canceled>=2);assert.match(h.target.textContent,/已停止/);
  assert.match(h.target.textContent,/还有 2 页未检索/);renderer.destroy();await flush();assert.equal(h.target.children.length,0);assert.equal(h.workers[0].terminated,true);
});

test('zoom, fit and in-memory reading metadata preserve page and position without material text',async()=>{
  const h=harness(),saved=[];h.target.clientWidth=300;
  const renderer=await renderPdfMaterial({...h.args,onViewState:value=>saved.push(value)});
  let canvas=descendants(h.target).find(node=>node.tagName==='CANVAS');assert.equal(canvas.width,300);
  h.button('放大').click();await flush();canvas=descendants(h.target).find(node=>node.tagName==='CANVAS');assert.equal(canvas.width,375);
  h.button('适合宽度').click();await flush();assert.equal(descendants(h.target).find(node=>node.tagName==='CANVAS').width,300);
  h.button('下一页').click();await flush();const display=descendants(h.target).find(node=>node.className==='material-pdf-display');
  display.scrollTop=27;display.scrollLeft=12;display.events.scroll();const position=renderer.getViewState();
  assert.equal(position.page,2);assert.equal(position.scrollTop,27);assert.deepEqual(Object.keys(position).sort(),['fitWidth','page','scrollLeft','scrollTop','textOpen','zoom']);
  assert.doesNotMatch(JSON.stringify(saved),/可选正文|query|关键词/);renderer.destroy();
  const reopened=harness(),next=await renderPdfMaterial({...reopened.args,initialView:position});assert.match(reopened.status.at(-1),/第 2 \/ 3 页/);assert.equal(next.getViewState().scrollTop,27);next.destroy();
});

test('zoom on the same page preserves the reading center while changing pages resets the viewport',async()=>{
  const h=harness(),renderer=await renderPdfMaterial(h.args),display=descendants(h.target).find(node=>node.className==='material-pdf-display');
  display.clientWidth=600;display.clientHeight=200;display.scrollTop=300;display.scrollLeft=100;
  h.button('放大').click();await flush();assert.equal(display.scrollTop,400);assert.equal(display.scrollLeft,200);
  h.button('下一页').click();await flush();assert.equal(display.scrollTop,0);assert.equal(display.scrollLeft,0);renderer.destroy();
});

test('one rendered page has a bounded pixel area even for extreme source dimensions', async () => {
  const h = harness({dimensions:[1000000,1000000]});
  const renderer = await renderPdfMaterial(h.args);
  const canvas = descendants(h.target).find(n => n.tagName === 'CANVAS');
  assert.ok(canvas.width * canvas.height <= 6*1024*1024); assert.ok(canvas.width <= 4096);
  renderer.destroy();
});

test('a parse deadline terminates the real worker and never claims a page was displayed', async () => {
  const pending = deferred(), h = harness({parse:pending.promise});
  const work = renderPdfMaterial(h.args); await flush(); h.fire(20000);
  await assert.rejects(work, /超时/);
  assert.equal(h.workers[0].terminated, true); assert.equal(h.destroyed, 1); assert.equal(h.status.length, 0);
});

test('closing during a pending render aborts the worker and late completion cannot restore content', async () => {
  const pending = deferred(), h = harness({pageRender:pending.promise}), controller = new AbortController();
  const work = renderPdfMaterial({...h.args, signal:controller.signal}); await flush(); controller.abort();
  await assert.rejects(work, /已关闭/); pending.resolve(); await flush();
  assert.equal(h.target.children.length, 0); assert.equal(h.status.length, 0); assert.equal(h.workers[0].terminated, true);
});

test('excessive page count fails explicitly without rendering a partial document', async () => {
  const h = harness({pages:2001}); await assert.rejects(renderPdfMaterial(h.args), /2000 页/);
  assert.equal(h.seen.length, 0); assert.equal(h.workers[0].terminated, true);
});

test('clients without an isolated worker refuse unsupported preview before loading PDF code', async () => {
  const h = harness(); delete h.args.window.Worker;
  await assert.rejects(renderPdfMaterial(h.args), /不支持隔离/); assert.equal(h.options.length, 0);
});
