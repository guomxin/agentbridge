import assert from 'node:assert/strict';
import test from 'node:test';
import {renderPdfMaterial, PDF_ASSET_ROOT} from '../agentbridge/auth/static/pdf_preview.mjs';
import {createDocument, descendants} from './workspace_dom_fixture.mjs';
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve};};
function harness({pages = 3, dimensions = [600, 800], parse, pageRender} = {}) {
  const document = createDocument(), target = document.createElement('div'), timers = new Map(), workers = [], status = [], seen = [], options = [];
  const create = document.createElement.bind(document);
  document.createElement = tag => { const node = create(tag); node.tagName = tag.toUpperCase(); node.click = () => node.events.click?.(); node.style = {}; if (tag === 'canvas') node.getContext = () => ({}); return node; };
  let current = true, timerId = 0, destroyed = 0;
  const window = {setTimeout(fn, delay) {timers.set(++timerId, {fn, delay}); return timerId;}, clearTimeout(id) {timers.delete(id);},
    Worker: class {constructor(url, settings) {this.url = url; this.settings = settings; workers.push(this);} terminate() {this.terminated = true;}}};
  const pdf = {numPages: pages, getPage: async number => ({getViewport: ({scale}) => ({width: dimensions[0] * scale, height: dimensions[1] * scale}),
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
