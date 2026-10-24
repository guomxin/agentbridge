import assert from "node:assert/strict";
import test from "node:test";
import {createMaterialPreviews, decodeMaterialArchive} from "../agentbridge/auth/static/material_preview.mjs";
import {createDocument, descendants} from "./workspace_dom_fixture.mjs";

const png = Uint8Array.from(Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1EAAAAASUVORK5CYII=", "base64"));
const pdf = new TextEncoder().encode("%PDF-1.4\nsynthetic");
const entry = (id = "a", kind = "image", state = "available") => ({id: id.repeat(64), name: `${id}合成材料`, kind, state});
const binary = (bytes = png, type = "image/png") => new Response(bytes, {headers: {"content-type": type}});
const archive = () => ({kind: "archive", totalEntries: 3, items: [
  {...entry("b"), contentType: "image/png", bodyBase64: Buffer.from(png).toString("base64"), byteLength: png.length},
  {...entry("c", "pdf"), contentType: "application/pdf", bodyBase64: Buffer.from(pdf).toString("base64"), byteLength: pdf.length},
  {...entry("d", "file", "unavailable"), reason: "表格格式暂不支持预览"},
]});
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => {resolve = a; reject = b;}); return {promise, resolve, reject}; };
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(fetch = async () => binary(), options = {}) {
  const document = createDocument(), original = document.createElement;
  document.createElement = tag => {
    const node = original(tag); node.style = {};
    if (tag === "img") node.decode = options.decode || (() => Promise.resolve());
    return node;
  };
  const target = document.createElement("div"), calls = [], created = [], revoked = [];
  const window = {location: {origin: "https://cards.test"}, Blob, AbortController, atob,
    setTimeout, clearTimeout, URL: {
      createObjectURL(blob) { const url = `blob:https://cards.test/${created.length}`; created.push({url, blob}); return url; },
      revokeObjectURL(url) { revoked.push(url); },
    }, fetch(...args) { calls.push(args); return fetch(...args); },
  };
  const controller = createMaterialPreviews({document, window, target, apiUrl: "/flow/input/source/draft", csrfToken: "private-page-token", timeoutMs: options.timeoutMs || 30, ...options});
  return {controller, document, target, calls, created, revoked,
    buttons: () => descendants(target).filter(node => node.tag === "button"),
    click: label => { const node = descendants(target).find(node => node.tag === "button" && node.textContent === label); assert.ok(node, label); return node.events.click(); },
  };
}

test("nothing reads automatically, exact authenticated same-origin GET runs only on click", async () => {
  const h = harness(); h.controller.render({materialPreviews: [entry()]});
  assert.equal(h.calls.length, 0); assert.match(h.target.textContent, /尚未读取/);
  await h.click("打开预览");
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0][0], `https://cards.test/flow/input/source/draft/materials/${"a".repeat(64)}`);
  assert.equal(h.calls[0][1].method, "GET"); assert.equal(h.calls[0][1].credentials, "same-origin");
  assert.equal(h.calls[0][1].headers["X-AgentBridge-Draft-Token"], "private-page-token");
  assert.match(h.target.textContent, /图片已显示/);
  const image = descendants(h.target).find(node => node.tag === "img"); assert.ok(image.src.startsWith("blob:"));
  h.controller.render({materialPreviews: [entry()]});
  assert.equal(h.created.length, 1); assert.equal(h.revoked.length, 0);
});

test("unsupported items stay visible, disabled and explicitly unread without fetch", () => {
  const h = harness(); h.controller.render({materialPreviews: [{...entry("a", "file", "unavailable"), reason: "只获取了附件名称"}]});
  assert.match(h.target.textContent, /只获取了附件名称/);
  assert.equal(h.buttons()[0].disabled, true); assert.equal(h.calls.length, 0);
});

test("cross-origin API, source URL and malformed ids never become requests", () => {
  const h = harness(undefined, {apiUrl: "https://foreign.test/draft"});
  h.controller.render({materialPreviews: [entry(), {...entry("b"), id: "https://oa.private/download?token=secret"}]});
  assert.equal(h.buttons()[0].disabled, true); assert.equal(h.calls.length, 0);
  assert.ok(!h.target.textContent.includes("private"));
});

test("ZIP files stay unopened until individually selected and unsupported members are retained", async () => {
  const h = harness(async () => new Response(JSON.stringify(archive()), {headers: {"content-type": "application/json"}}));
  h.controller.render({materialPreviews: [entry("a", "archive")]});
  await h.click("获取附件包");
  assert.match(h.target.textContent, /2 份可预览，1 份格式暂不支持/);
  assert.equal(h.created.length, 0); assert.match(h.target.textContent, /尚未打开/); assert.match(h.target.textContent, /表格格式暂不支持/);
  await h.click("打开此文件");
  assert.equal(h.created.length, 1); assert.equal(h.calls.length, 1); assert.match(h.target.textContent, /图片已显示/);
});

test("PDF is rendered from bytes by the bounded local renderer and disposed on close", async () => {
  const calls = []; let destroyed = 0;
  const h = harness(async () => binary(pdf, "application/pdf"), {renderPdf: async args => {
    calls.push(args); args.onStatus("第 1 / 2 页；已展示 1 页，尚未全部查看");
    const canvas = args.document.createElement("canvas"); args.target.append(canvas);
    return {destroy() { destroyed++; }};
  }});
  h.controller.render({materialPreviews: [entry("a", "pdf")]});
  await h.click("打开预览");
  assert.deepEqual(calls[0].bytes, pdf); assert.equal(calls[0].signal.aborted, false);
  assert.ok(!descendants(h.target).some(node => ["iframe", "a"].includes(node.tag)));
  assert.equal(h.created.length, 0); assert.match(h.target.textContent, /尚未全部查看/);
  await h.click("关闭预览"); assert.equal(destroyed, 1); assert.equal(calls[0].signal.aborted, true);
});

test('material anchors do not fetch and PDF reopening retains only whitelisted reading metadata until suspension',async()=>{
  const views=[];const h=harness(async()=>binary(pdf,'application/pdf'),{renderPdf:async args=>{
    views.push(args.initialView);args.onViewState({page:2,fitWidth:false,zoom:1.5,textOpen:true,scrollTop:20,scrollLeft:0,text:'private text',query:'private query'});
    return {destroy(){}};
  }});
  h.controller.render({materialPreviews:[entry('a','pdf')]});
  const anchor=descendants(h.target).find(node=>node.id===`material-preview-${'a'.repeat(64)}`);assert.equal(anchor.tabIndex,-1);assert.equal(h.calls.length,0);
  await h.click('打开预览');await h.click('关闭预览');await h.click('重新获取预览');
  assert.deepEqual(views[1],{page:2,fitWidth:false,zoom:1.5,textOpen:true,scrollTop:20,scrollLeft:0});
  h.controller.suspend();h.controller.render({materialPreviews:[entry('a','pdf')]});await h.click('打开预览');assert.equal(views[2],undefined);
});

test("close, manifest change and page suspension revoke all Blob URLs", async () => {
  const h = harness(); h.controller.render({materialPreviews: [entry()]}); await h.click("打开预览");
  await h.click("关闭预览"); assert.equal(h.revoked.length, 1);
  await h.click("重新获取预览"); h.controller.render({materialPreviews: [entry("b")]});
  assert.equal(h.revoked.length, 2);
  await h.click("打开预览"); h.controller.suspend();
  assert.equal(h.revoked.length, 3); assert.equal(h.target.childNodes.length, 0);
});

test("late fetch after timeout cannot restore preview or mark the material loaded", async () => {
  const pending = deferred(), h = harness(() => pending.promise, {timeoutMs: 5});
  h.controller.render({materialPreviews: [entry()]});
  await h.click("打开预览");
  assert.match(h.target.textContent, /超时/); assert.equal(h.buttons()[0].disabled, false);
  pending.resolve(binary()); await flush();
  assert.equal(h.created.length, 0); assert.match(h.target.textContent, /超时/);
});

test("image decode is bounded and late decode does not undo failure", async () => {
  const decode = deferred(), h = harness(undefined, {decode: () => decode.promise, timeoutMs: 5});
  h.controller.render({materialPreviews: [entry()]}); await h.click("打开预览");
  assert.match(h.target.textContent, /超时/); assert.equal(h.revoked.length, 1);
  decode.resolve(); await flush();
  assert.match(h.target.textContent, /超时/); assert.ok(!h.target.textContent.includes("图片已显示"));
});

test("pagehide during a request invalidates late network bytes", async () => {
  const pending = deferred(), h = harness(() => pending.promise);
  h.controller.render({materialPreviews: [entry()]}); const operation = h.click("打开预览");
  h.controller.suspend(); pending.resolve(binary()); await operation;
  assert.equal(h.created.length, 0); assert.equal(h.target.childNodes.length, 0);
});

test("bad signatures, unexpected content types and server failures never show a loaded state", async () => {
  for (const response of [() => binary(new TextEncoder().encode("<script>bad</script>"), "image/png"),
                         () => binary(png, "text/html"),
                         () => new Response(JSON.stringify({error: {message: "办理入口已过期"}}), {status: 403, headers: {"content-type": "application/json"}})]) {
    const h = harness(response); h.controller.render({materialPreviews: [entry()]}); await h.click("打开预览");
    assert.ok(!h.target.textContent.includes("图片已显示")); assert.equal(h.created.length, 0);
  }
});

test("archive decoder rejects ambiguity, malformed Base64, size mismatch and active content", () => {
  const cases = [value => value.items.push(value.items[0]), value => value.items[0].bodyBase64 = "!!bad!!",
    value => value.items[0].byteLength = 0, value => value.items[0].contentType = "text/html",
    value => value.items[0].bodyBase64 = Buffer.from("<svg onload=bad()>").toString("base64")];
  for (const change of cases) { const value = archive(); change(value); assert.throws(() => decodeMaterialArchive(value)); }
});
