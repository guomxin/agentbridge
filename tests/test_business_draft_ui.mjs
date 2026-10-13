import assert from "node:assert/strict";
import test from "node:test";
import {createDraftController, mountBusinessDraft, reviewSections, safeFlowUrl} from "../agentbridge/auth/static/business_draft.mjs";
import {createDocument, descendants} from "./workspace_dom_fixture.mjs";

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
    fieldNames: Object.keys(state.values),
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

const meeting = () => ({...initial(), values: {purpose: '研讨', room: '', start_time: '2026-10-14T10:00', end_time: '2026-10-14T11:00'},
  ui: {viewKind: 'meeting_room'}, allowedActions: ['save', 'options', 'preview'], options: null});
const optionsFor = (state, items = [{id: 'room-1', value: 'token-1', label: '第一会议室', available: true}]) => ({...state,
  version: state.version + 1, options: {revision: state.revision, version: state.version + 1, field: 'room',
    dependsOn: {start_time: state.values.start_time, end_time: state.values.end_time}, expiresAt: '2099-01-01T00:00:00Z', items,
    selected: items.find(item => item.value === state.values.room) || null}});

test('meeting candidates never choose a room automatically and time edits invalidate old selection immediately', async () => {
  let backend = meeting();
  const h = harness(async (action, payload) => {
    if (action === 'values') backend = {...backend, revision: backend.revision + 1, version: backend.version + 1,
      values: payload.values, options: null, preview: null, allowedActions: ['save', 'options', 'preview']};
    if (action === 'options') backend = optionsFor(backend, [{id: 'room-2', value: 'token-2', label: '第二会议室', available: true}]);
    if (action === 'preview') backend = reviewed(backend);
    return backend;
  }, backend);
  await h.controller.loadOptions();
  assert.equal(h.local().room, '');
  assert.equal(h.controller.state().canApprove, false);
  h.change({room: 'token-2'}); await h.controller.save();
  assert.equal(h.controller.state().canApprove, true);
  h.change({start_time: '2026-10-14T12:00', end_time: '2026-10-14T13:00'});
  assert.equal(h.controller.state().optionsFresh, false);
  assert.equal(h.controller.state().canApprove, false);
  assert.equal(h.local().room, 'token-2');
});

test('late candidate response cannot replace a newer time range or its explicit selection', async () => {
  const late = deferred(); let backend = meeting(); let count = 0;
  const h = harness(async (action, payload) => {
    if (action === 'options') {
      if (++count === 1) return late.promise;
      return backend = optionsFor(backend, [{id: 'new', value: 'new-token', label: '新时段房间', available: true}]);
    }
    if (action === 'values') return backend = {...backend, revision: 1, version: 3, values: payload.values};
    throw new Error('no preview without selection');
  }, backend);
  const pending = h.controller.loadOptions();
  h.change({start_time: '2026-10-15T10:00', end_time: '2026-10-15T11:00'});
  await h.controller.save();
  late.resolve(optionsFor(meeting())); await pending; await flush();
  assert.equal(count, 2);
  assert.equal(h.controller.state().current.options.dependsOn.start_time, '2026-10-15T10:00');
  assert.equal(h.controller.state().current.options.items[0].value, 'new-token');
  assert.equal(h.local().room, '');
});

test('non-time edits reuse rebased candidates and submit only registered meeting fields', async () => {
  let backend = meeting(); backend.values.room = 'token-1'; backend = optionsFor(backend);
  const h = harness(async (action, payload) => {
    if (action === 'values') backend = {...backend, revision: 1, version: 2, values: payload.values,
      options: {...backend.options, revision: 1}};
    else if (action === 'preview') backend = reviewed(backend);
    else throw new Error('candidate lookup should be reused');
    return backend;
  }, backend);
  h.change({purpose: '修改用途'}); await h.controller.save();
  assert.deepEqual(h.calls.map(item => item.action), ['values', 'preview']);
  assert.deepEqual(Object.keys(h.calls[0].payload.values).sort(), ['end_time', 'purpose', 'room', 'start_time']);
  assert.equal(h.controller.state().canApprove, true);
});

test('renamed or unavailable room keeps old token and cannot authorize', async () => {
  let backend = meeting(); backend.values.room = 'token-1'; backend = optionsFor(backend);
  const h = harness(async () => optionsFor(backend, [{id: 'room-1', value: 'renamed-token', label: '改名会议室', available: true}]), backend);
  await h.controller.loadOptions(true);
  assert.equal(h.local().room, 'token-1');
  assert.equal(h.controller.state().canApprove, false);
  await h.controller.approve();
  assert.deepEqual(h.calls.map(item => item.action), ['options']);
});

test('changed approval materials show new preview and require a second explicit bound decision', async () => {
  const state = reviewed({...initial(), values: {opinion: '核对后同意'}, ui: {viewKind: 'approval'}});
  const updated = {...reviewed(state, 3), reviewChanged: true,
    preview: {...state.preview, previewId: 'new-material-preview', summary: {fields: [{label: '事由', value: '最新事由'}]}}};
  let attempts = 0;
  const h = harness(async (action, payload) => {
    if (action === 'state') return updated;
    if (action === 'approve' && ++attempts === 1) throw Object.assign(new Error('材料已更新'),
      {status: 409, code: 'DRAFT_REVIEW_CHANGED', current: updated});
    return {...updated, version: 4, state: 'finalized', allowedActions: [], receipt: {requestKey: payload.requestKey}};
  }, state);
  await h.controller.approve();
  assert.equal(attempts, 1);
  assert.equal(h.controller.state().reviewChanged, true);
  assert.equal(h.controller.state().current.preview.previewId, 'new-material-preview');
  assert.equal(h.controller.state().canApprove, true);
  await h.controller.poll();
  assert.equal(attempts, 1);
  await h.controller.approve();
  const decisions = h.calls.filter(item => item.action === 'approve');
  assert.equal(decisions[1].payload.previewId, 'new-material-preview');
  assert.notEqual(decisions[0].payload.requestKey, decisions[1].payload.requestKey);
  assert.equal(h.local().opinion, '核对后同意');
});

test('material overview keeps key reason excerpt while expanded groups retain every full value', () => {
  const reason = '完整事由'.repeat(100);
  const result = reviewSections({fields: [{label: '事由', value: reason}, {label: '请假小时', value: 0},
    ...Array.from({length: 110}, (_, i) => ({label: `航段${i} · 行程`, value: `目的地${i}`}))],
    groups: [{title: '补充材料', fields: [{label: '附件', value: '<script>纯文本</script>'}]}]});
  assert.match(result.overview.find(item => item.label === '事由').value, /全文见下方/);
  assert.equal(result.overview.find(item => item.label === '请假小时').value, '0');
  assert.equal(result.groups.find(([name]) => name === '事由 · 全文')[1][0].value, reason);
  assert.equal(result.groups.find(([name]) => name === '航段109')[1][0].value, '目的地109');
  assert.equal(result.groups.find(([name]) => name === '补充材料')[1][0].value, '<script>纯文本</script>');
});

function mountHarness(state) {
  const document = createDocument(), create = document.createElement;
  document.createElement = tag => { const node = create(tag); node.querySelectorAll = selector =>
    descendants(node).filter(item => selector === 'details[open]' && item.tag === 'details' && item.open); return node; };
  const nodes = new Map(); document.getElementById = id => nodes.get(id);
  const root = document.createElement('main');
  for (const id of ['draft-form', 'draft-status', 'draft-error', 'draft-submit', 'draft-retry', 'draft-conflict',
    'draft-use-server', 'draft-keep-local', 'draft-review', 'draft-evidence', 'draft-material-change',
    'review-fields', 'review-effect', 'room-options', 'room-status', 'room-selected', ...Object.keys(state.values),
    ...Object.keys(state.values).flatMap(name => [`error-${name}`, `conflict-local-${name}`, `conflict-server-${name}`, `conflict-row-${name}`])]) {
    const node = document.createElement('div'); node.id = id; node.value = state.values[id] || '';
    node.validity = {valid: true}; node.checkValidity = () => true; nodes.set(id, node); root.append(node);
  }
  const responses = [];
  const window = {fetch: async () => ({ok: true, json: async () => responses.shift() || state}),
    location: {origin: 'https://trusted.example', hash: '', replace() {}}, setTimeout() {}, clearTimeout() {}, addEventListener() {}};
  const controller = mountBusinessDraft({state, fieldNames: Object.keys(state.values), csrfToken: 't', apiUrl: '/draft'}, document, window);
  return {controller, document, nodes, responses};
}

test('candidate cards keep focused radio DOM across polling and disable stale candidates without replacement', async () => {
  let state = meeting(); state.values.room = 'token-1'; state = reviewed(optionsFor(state));
  const h = mountHarness(state); await flush();
  const radio = descendants(h.nodes.get('room-options')).find(node => node.tag === 'input');
  radio.focus(); await h.controller.poll();
  assert.equal(h.document.activeElement, radio);
  assert.equal(radio.checked, true);
  h.nodes.get('start_time').value = '2026-10-15T10:00'; h.controller.edit();
  assert.equal(radio.disabled, true);
  assert.equal(h.nodes.get('room').value, 'token-1');
  assert.match(h.nodes.get('room-selected').textContent, /不会自动替换/);
});

test('approval rendering uses safe text nodes, preserves expanded material, and replaces changed evidence visibly', async () => {
  const review = {title: '申请', fields: [{label: '申请人', value: '甲'}, {label: '事由', value: '<script>材料</script>'.repeat(20)}]};
  let state = reviewed({...initial(), values: {opinion: '同意'}, ui: {viewKind: 'approval', review}});
  state.preview.summary = review;
  const h = mountHarness(state); await flush();
  const detail = descendants(h.nodes.get('draft-evidence')).find(node => node.tag === 'details');
  detail.open = true; detail.focus();
  await h.controller.poll();
  assert.equal(h.document.activeElement, detail); assert.equal(detail.open, true);
  assert.ok(!descendants(h.nodes.get('draft-evidence')).some(node => node.tag === 'script'));
  h.responses.push({...state, version: 3, reviewChanged: true,
    preview: {...state.preview, summary: {title: '更新申请', fields: [{label: '事由', value: '最新内容'}]}}});
  await h.controller.poll();
  assert.equal(h.nodes.get('draft-material-change').hidden, false);
  assert.match(h.nodes.get('draft-evidence').textContent, /最新内容/);
});

test('seeded space-format times match native datetime-local values without repeated candidate requests', async () => {
  let state = meeting(); state.values.room = 'token-1'; state = optionsFor(state);
  state.options.dependsOn = {start_time: '2026-10-14 10:00', end_time: '2026-10-14 11:00:00'};
  const h = harness(async action => { assert.equal(action, 'preview'); return reviewed(state); }, state);
  assert.equal(h.controller.state().optionsFresh, true);
  h.controller.start(); await flush();
  assert.deepEqual(h.calls.map(item => item.action), ['preview']);
  assert.equal(h.controller.state().canApprove, true);
});

test('reload preserves changed-material warning from nested preview metadata', () => {
  const state = reviewed({...initial(), values: {opinion: '同意'}, ui: {viewKind: 'approval'}});
  state.preview.reviewChanged = true;
  const h = harness(async () => state, state);
  assert.equal(h.controller.state().reviewChanged, true);
});

test('state request started before our save does not mislabel the saved revision as another tab', async () => {
  const late = deferred(); let backend = initial();
  const h = harness(async (action, payload) => {
    if (action === 'state') return late.promise;
    if (action === 'values') return backend = {...backend, revision: 1, version: 2, values: payload.values};
    return backend = reviewed(backend, 3);
  });
  const pending = h.controller.poll();
  h.change({content: '本页已保存'}); await h.controller.save();
  late.resolve(backend); await pending;
  assert.equal(h.controller.state().conflict, null);
  assert.equal(h.controller.state().canApprove, true);
});

test('candidate refresh cannot overlap an in-flight preview at the same revision', async () => {
  let state = meeting(); state.values.room = 'token-1'; state = optionsFor(state);
  const waiting = deferred();
  const h = harness(async action => {
    assert.equal(action, 'preview'); return waiting.promise;
  }, state);
  const pending = h.controller.preview();
  await h.controller.loadOptions(true);
  assert.deepEqual(h.calls.map(item => item.action), ['preview']);
  waiting.resolve(reviewed(state)); await pending;
});

test('first ordinary meeting and approval previews never display a changed-material warning', async () => {
  let room = meeting(); room.values.room = 'token-1'; room = reviewed(optionsFor(room));
  const leave = reviewed({...initial(), values: {opinion: '同意'}, ui: {viewKind: 'approval',
    review: {title: '正常申请', snapshot: 'preview', reviewChanged: false, fields: [{label: '事由', value: '正常事由'}]}}});
  for (const state of [room, leave]) {
    state.preview.reviewChanged = false;
    state.preview.summary = {reviewChanged: false, authorization_notice: '授权后将立即提交',
      fields: [{label: '本次操作', value: '提交'}]};
    const h = mountHarness(state); await flush();
    assert.equal(h.controller.state().reviewChanged, false);
    assert.equal(h.nodes.get('draft-material-change').hidden, true);
    assert.equal(h.nodes.get('draft-status').textContent, '已检查，可以确认提交');
  }
});

test('changed-material metadata requires explicit booleans rather than truthy strings or snapshots', () => {
  const state = reviewed(initial());
  state.reviewChanged = 'false'; state.preview.reviewChanged = 'false';
  state.preview.summary.reviewChanged = 'preview';
  state.ui = {review: {snapshot: 'preview', reviewChanged: 'source'}};
  const h = harness(async () => state, state);
  assert.equal(h.controller.state().reviewChanged, false);
});
