import assert from "node:assert/strict";
import test from "node:test";
import {createDraftController, safeFlowUrl} from "../agentbridge/auth/static/business_draft.mjs";

const values = {log_date: "2026-10-13", hours: "8", project: "P001", content: "本页日志"};
const initial = () => ({draftId: "draft-a", revision: 0, version: 0, state: "editable",
  expiresAt: "2099-01-01T00:00:00Z", values: {...values},
  allowedActions: ["save", "preview"], preview: null, receipt: null});
const reviewed = (state, version = state.version + 1) => ({...state, version,
  allowedActions: ["save", "preview", "approve"],
  preview: {previewId: `preview-${state.revision}`, revision: state.revision,
    expiresAt: "2099-01-01T00:00:00Z", summary: {project: {name: "项目", code: "P001"}}}});
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => {resolve = a; reject = b;}); return {promise, resolve, reject}; };
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(request, state = initial()) {
  let local = {...state.values};
  const calls = [], rendered = [], receipts = [];
  let keys = 0;
  const controller = createDraftController({initial: state,
    request(action, payload) { calls.push({action, payload}); return request(action, payload); },
    readValues: () => ({...local}), writeValues: next => {local = {...next};},
    render: view => rendered.push(view), makeKey: () => `request-${++keys}`,
    onReceipt: receipt => receipts.push(receipt)});
  return {controller, calls, rendered, receipts, local: () => local,
    change(next) {if (controller.state().canEdit) {local = {...local, ...next}; controller.edit();}}};
}

test("editing immediately disables a reviewed decision and preserves local values", async () => {
  const state = reviewed(initial());
  let backend = state;
  const h = harness(async (action, payload) => {
    if (action === "values") backend = {...backend, revision: backend.revision + 1,
      version: backend.version + 1, values: payload.values, preview: null, allowedActions: ["save", "preview"]};
    if (action === "preview") backend = reviewed(backend);
    return backend;
  }, state);
  assert.equal(h.controller.state().canApprove, true);
  h.change({hours: "7.5", content: "新的日志"});
  assert.equal(h.controller.state().canApprove, false);
  await h.controller.save();
  assert.equal(h.controller.state().canApprove, true);
  assert.equal(h.local().content, "新的日志");
  assert.deepEqual(h.calls.map(call => call.action), ["values", "preview"]);
});

test("late preview cannot restore an older revision or enable its decision", async () => {
  const oldPreview = deferred();
  let backend = initial();
  const h = harness(async (action, payload) => {
    if (action === "preview" && payload.expectedRevision === 0) return oldPreview.promise;
    if (action === "values") return backend = {...backend, revision: 1, version: 2, values: payload.values};
    if (action === "preview") return backend = reviewed(backend, 3);
  });
  const pending = h.controller.preview();
  h.change({content: "编辑中"});
  await h.controller.save();
  oldPreview.resolve(reviewed(initial(), 1));
  await pending;
  await flush();
  assert.equal(h.controller.state().current.revision, 1);
  assert.equal(h.controller.state().current.preview.revision, 1);
  assert.equal(h.local().content, "编辑中");
});

test("saving is serialized while typing and the second snapshot keeps new input", async () => {
  const first = deferred();
  let backend = initial();
  let saves = 0;
  const h = harness(async (action, payload) => {
    if (action === "values") {
      if (++saves === 1) { await first.promise; }
      return backend = {...backend, revision: backend.revision + 1, version: backend.version + 1, values: payload.values};
    }
    return backend = reviewed(backend);
  });
  h.change({content: "第一版"});
  const saving = h.controller.save();
  h.change({content: "第二版"});
  await h.controller.save();
  assert.equal(saves, 1);
  first.resolve();
  await saving;
  assert.equal(saves, 2);
  assert.equal(backend.values.content, "第二版");
  assert.equal(h.controller.state().dirty, false);
});

test("multi-tab revision conflict retains local input until an explicit choice", async () => {
  const remote = {...initial(), revision: 2, version: 4, values: {...values, content: "另一页内容"}};
  const h = harness(async action => {
    if (action === "values") throw Object.assign(new Error("另一页面已更新"),
      {status: 409, code: "DRAFT_REVISION_CONFLICT", current: remote});
    return reviewed(remote);
  });
  h.change({content: "未丢失的本页输入"});
  await h.controller.save();
  assert.equal(h.local().content, "未丢失的本页输入");
  assert.equal(h.controller.state().conflict.revision, 2);
  assert.equal(h.controller.state().canApprove, false);
  await h.controller.adoptServer();
  assert.equal(h.local().content, "另一页内容");
  assert.equal(h.controller.state().conflict, null);
});

test("choosing local contents after comparison uses the current server revision", async () => {
  let backend = {...initial(), revision: 3, version: 8};
  const h = harness(async (action, payload) => {
    if (action === "state") return backend;
    if (action === "values") return backend = {...backend, revision: 4, version: 9, values: payload.values};
    return backend = reviewed(backend);
  });
  h.change({content: "本页输入"});
  await h.controller.poll();
  assert.equal(h.controller.state().conflict.revision, 3);
  await h.controller.keepLocal();
  assert.equal(h.calls.find(call => call.action === "values").payload.expectedRevision, 3);
  assert.equal(backend.values.content, "本页输入");
});

test("one final confirmation freezes controls and recovery reuses the exact request key", async () => {
  const state = reviewed(initial());
  let attempts = 0;
  const h = harness(async action => {
    if (action === "state") return state;
    if (++attempts === 1) throw Object.assign(new Error("response lost"), {status: 503});
    return {...state, version: 2, state: "approved", allowedActions: [],
      receipt: {requestKey: "request-1", flowUrl: "/flow/authorize/" + "a".repeat(43)}};
  }, state);
  await h.controller.approve();
  h.change({content: "不能再改"});
  await h.controller.approve();
  assert.equal(attempts, 1);
  assert.equal(h.controller.state().locked, true);
  await h.controller.retry();
  const decisions = h.calls.filter(call => call.action === "approve");
  assert.deepEqual(decisions[0].payload, decisions[1].payload);
  assert.deepEqual(Object.keys(decisions[0].payload).sort(), ["expectedRevision", "previewId", "requestKey"]);
  assert.equal(h.local().content, values.content);
  assert.equal(h.receipts.length, 1);
});

test("late ready status cannot unlock an approved draft at the same revision", async () => {
  const status = deferred();
  const state = reviewed(initial());
  const h = harness(async action => action === "state" ? status.promise : ({...state,
    version: 4, state: "approved", allowedActions: [], receipt: {requestKey: "request-1"}}), state);
  const poll = h.controller.poll();
  await h.controller.approve();
  status.resolve({...state, version: 2});
  await poll;
  assert.equal(h.controller.state().current.version, 4);
  assert.equal(h.controller.state().canApprove, false);
  assert.equal(h.controller.state().canEdit, false);
});

test("preview validation and duplicate errors remain in place and never authorize", async () => {
  const h = harness(async () => {throw Object.assign(new Error("项目匹配不唯一"),
    {status: 422, code: "VALIDATION_FAILED", fieldErrors: {project: "请输入准确项目编码"}});});
  await h.controller.preview();
  assert.equal(h.controller.state().fieldErrors.project, "请输入准确项目编码");
  assert.equal(h.controller.state().canApprove, false);
  assert.deepEqual(h.local(), values);
  await h.controller.approve();
  assert.equal(h.calls.length, 1);
});

test("expired previews do not enable confirmation", () => {
  const state = reviewed(initial());
  state.preview.expiresAt = "2000-01-01T00:00:00Z";
  const h = harness(async () => state, state);
  assert.equal(h.controller.state().canApprove, false);
});

test("server expiry refreshes only the preview and never saves or approves", async () => {
  const state = reviewed(initial());
  const expired = {...state, version: 2, state: "editing", preview: null, allowedActions: ["save", "preview"]};
  const h = harness(async action => action === "state" ? expired : reviewed(expired, 3), state);
  await h.controller.poll();
  assert.deepEqual(h.calls.map(call => call.action), ["state", "preview"]);
  assert.equal(h.calls[1].payload.expectedRevision, state.revision);
  assert.equal(h.controller.state().canApprove, true);
  assert.deepEqual(h.local(), values);
});

test("temporary busy actions never permanently lock an editable draft", async () => {
  let backend = {...initial(), version: 1, allowedActions: []};
  const h = harness(async () => backend);
  await h.controller.poll();
  assert.equal(h.controller.state().canEdit, false);
  backend = {...initial(), version: 2};
  await h.controller.poll();
  assert.equal(h.controller.state().canEdit, true);
  assert.equal(h.controller.state().locked, false);
});

test("unusable receipt navigation stays visible while the decision remains locked", () => {
  const state = {...initial(), state: "approved", version: 4, allowedActions: [], receipt: {flowUrl: "bad"}};
  let rendered;
  const controller = createDraftController({initial: state, request: async () => state,
    readValues: () => values, writeValues() {}, render: view => { rendered = view; }, onReceipt: () => false});
  controller.start();
  assert.equal(rendered.locked, true);
  assert.match(rendered.message, /已记录/);
  assert.equal(rendered.canApprove, false);
});

test("continuation accepts only exact same-origin authorization paths", () => {
  const origin = "https://trusted.example";
  const path = "/flow/authorize/" + "a".repeat(43);
  assert.ok(safeFlowUrl(path, origin));
  for (const value of ["https://evil.example" + path, path + "?secret=1", path + "#x",
    "/flow/input/" + "a".repeat(43), "javascript:alert(1)", "https://user:pass@trusted.example" + path]) {
    assert.equal(safeFlowUrl(value, origin), null);
  }
});
