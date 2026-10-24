import assert from "node:assert/strict";
import test from "node:test";
import {appendReading, bindDraftField, createDraftController, createDraftTransport, mountBusinessDraft, renderEvidence, reviewDifferences, REVIEW_DESIGNS, reviewSections, safeFlowUrl, safeResultReturn} from "../agentbridge/auth/static/business_draft.mjs";
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

test("explicit segmented false remains a chosen value through draft restore and locking", () => {
  const document = createDocument();
  const root = document.createElement("fieldset");
  const controls = ["true", "false"].map(value => {
    const control = document.createElement("input");
    control.value = value; control.validity = {valid: true}; control.checkValidity = () => true;
    return control;
  });
  root.querySelectorAll = () => controls;
  const field = bindDraftField(root, {control: "segmented"});
  assert.equal(field.value, "");
  field.value = "false";
  assert.equal(field.value, "false");
  assert.deepEqual(controls.map(control => control.checked), [false, true]);
  field.disabled = true;
  assert.ok(controls.every(control => control.disabled));
  field.setAttribute("aria-invalid", "true");
  assert.ok(controls.every(control => control.attributes["aria-invalid"] === "true"));
  field.value = "true";
  assert.deepEqual(controls.map(control => control.checked), [true, false]);
});

test("enum values are saved as exact source options and native validation remains authoritative", () => {
  const document = createDocument(), select = document.createElement("select");
  select.value = "火车"; select.validity = {valid: false}; select.validationMessage = "请选择有效选项";
  select.checkValidity = () => false;
  const field = bindDraftField(select, {control: "select"});
  assert.equal(field.value, "火车");
  assert.equal(field.checkValidity(), false);
  assert.equal(field.validity.valid, false);
  assert.equal(field.validationMessage, "请选择有效选项");
  field.value = "轮渡";
  assert.equal(select.value, "轮渡");
});

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

function mountHarness(state, {fetch, configure} = {}) {
  const document = createDocument(), create = document.createElement;
  document.createElement = tag => { const node = create(tag); node.querySelectorAll = selector =>
    descendants(node).filter(item => selector === 'details[open]' && item.tag === 'details' && item.open); return node; };
  const nodes = new Map(); document.getElementById = id => nodes.get(id);
  const root = document.createElement('main');
  for (const id of ['draft-form', 'draft-status', 'draft-error', 'draft-submit', 'draft-retry', 'draft-conflict',
    'draft-content','draft-content-saved','draft-content-candidate','draft-content-restore','draft-content-discard',
    'draft-content-confirmation','draft-content-confirm-title','draft-content-confirm-message','draft-content-cancel','draft-content-confirm',
    'draft-use-server', 'draft-keep-local', 'draft-review', 'draft-evidence', 'draft-material-change',
    'review-fields', 'review-effect', 'review-project', 'review-project-code', 'review-duplicate', 'review-content',
    'room-options', 'room-status', 'room-selected', ...Object.keys(state.values),
    ...Object.keys(state.values).flatMap(name => [`error-${name}`, `conflict-local-${name}`, `conflict-server-${name}`, `conflict-row-${name}`])]) {
    const node = document.createElement('div'); node.id = id; node.name = id; node.value = state.values[id] || '';
    node.validity = {valid: true}; node.checkValidity = () => true; nodes.set(id, node); root.append(node);
  }
  const responses = [], requests = [], timers = new Map(), events = new Map(), navigations = [];
  const documentEvents=new Map(); document.addEventListener=(name,callback)=>documentEvents.set(name,callback);
  let timerId = 0;
  const window = {fetch: (url, options) => {
      requests.push({url, options});
      return fetch ? fetch(url, options) : Promise.resolve({ok: true, json: async () => responses.shift() || state});
    },
    location: {origin: 'https://trusted.example', hash: '', replace: url => navigations.push(url)},
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, {callback, delay}); return id; },
    clearTimeout: id => timers.delete(id), addEventListener: (name, callback) => events.set(name, callback),
    removeEventListener: (name, callback) => {if (events.get(name) === callback) events.delete(name);}};
  configure?.(document, window);
  const controller = mountBusinessDraft({state, fieldNames: Object.keys(state.values), csrfToken: 't', apiUrl: '/draft'}, document, window);
  return {controller, document, nodes, responses, requests, timers, navigations,window,events,documentEvents,
    hide: () => events.get('pagehide')(), restore: () => events.get('pageshow')({persisted: true}),
    edit(name, value) { nodes.get(name).value = value; nodes.get('draft-form').events.input({target: nodes.get(name)}); }};
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

test('explicitly incomplete or unverified material never enables the final business action', async () => {
  for (const completeness of [{status: 'incomplete'}, {status: 'unverified'}, {status: 'complete'}]) {
    const state = reviewed({...initial(), values: {opinion: '同意'}, ui: {viewKind: 'approval'}});
    state.preview.summary = {completeness, fields: [{label: '事项', value: '待核对申请'}]};
    const h = harness(async () => { throw new Error('must not approve incomplete evidence'); }, state);
    assert.equal(h.controller.state().canApprove, completeness.status === 'complete');
    if (completeness.status !== 'complete') { await h.controller.approve(); assert.equal(h.calls.length, 0); }
  }
  const state = reviewed(initial()); state.preview.summary.truncated = true;
  assert.equal(harness(async () => state, state).controller.state().canApprove, false);
});

test('attendance evidence preserves the source decision as read-only and full tabular material is inert text', () => {
  const document = createDocument(), target = document.createElement('div'); target.querySelectorAll = () => [];
  renderEvidence(document, target, {title: '考勤确认', fields: [
    {label: '员工与月份', value: '甲 / 10月'}, {label: 'OA 当前确认结论', value: '有异议'},
  ], sections: [{id: 'original', kind: 'text', title: '原始材料', text: '<script>原文</script>'},
    {id: 'rows', kind: 'table', title: '明细', columns: [{key: 'item', label: '项目'}],
      rows: [{id: '1', cells: {item: '<img src=x onerror=alert(1)>'}}]}],
    attachments: [{name: '原始证明.pdf'}], completeness: {status: 'complete'}}, 'attendance_confirmation');
  assert.match(target.textContent, /有异议/);
  assert.match(target.textContent, /<script>原文<\/script>/);
  assert.match(target.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(target.textContent, /请打开下方支持的材料预览/);
  assert.match(target.textContent, /未打开或不支持预览的附件内容尚未读取/);
  assert.doesNotMatch(target.textContent, /附件内容未在本页读取/);
  assert.ok(!descendants(target).some(node => ['input', 'select', 'script', 'img'].includes(node.tag)));
  assert.ok(descendants(target).filter(node => node.tag === 'a').every(node => /^#review-/.test(node.href)));
  assert.equal(descendants(target).filter(node => node.tag === 'td').length, 1);
  assert.equal(descendants(target).find(node => node.dataset.reviewKind)?.dataset.reviewKind, 'attendance_confirmation');
});

const richReading = () => ({version: 1, notices: ['正文中有图片，图片内容未在本页读取。'], nodes: [
  {tag: 'p', children: ['本周完成', {tag: 'strong', children: ['关键事项']}, {tag: 'br', children: []}, '下一行']},
  {tag: 'ol', children: [{tag: 'li', children: ['第一项']}, {tag: 'li', children: ['第二项']}]},
  {tag: 'table', children: [{tag: 'tbody', children: [{tag: 'tr', children: [
    {tag: 'th', children: ['项目']}, {tag: 'td', colspan: 2, children: ['完整明细尾部']},
  ]}]}]},
]});

test('all OA designs display projected paragraphs lists and tables without source HTML or active elements', () => {
  for (const kind of Object.keys(REVIEW_DESIGNS)) {
    const document = createDocument(), target = document.createElement('div'); target.querySelectorAll = () => [];
    renderEvidence(document, target, {fields: [{label: '事项', value: '合成事项'}], sections: [
      {id: 'body', title: '正文与页面材料', kind: 'text', text: '本周完成关键事项\n下一行\n第一项\n第二项\n完整明细尾部', reading: richReading()},
    ], completeness: {status: 'complete'}}, kind);
    const nodes = descendants(target);
    assert.match(target.textContent, /本周完成关键事项下一行/);
    assert.match(target.textContent, /第一项第二项/);
    assert.match(target.textContent, /完整明细尾部/);
    assert.match(target.textContent, /图片内容未在本页读取/);
    assert.ok(nodes.some(node => node.tag === 'strong'));
    assert.equal(nodes.filter(node => node.tag === 'li').length, 2);
    assert.equal(nodes.find(node => node.tag === 'td').attributes.colspan, '2');
    assert.ok(nodes.some(node => node.className === 'reading-table' && node.tabIndex === 0));
    assert.ok(!nodes.some(node => ['script', 'img', 'iframe', 'form', 'input', 'button'].includes(node.tag)));
    assert.ok(nodes.filter(node => node.tag === 'a').every(node => /^#review-/.test(node.href)));
  }
});

test('projected original fields and opinion cells keep their full readable structure on rerender', () => {
  const document = createDocument(), target = document.createElement('div'); target.querySelectorAll = () => [];
  const review = {fields: [{label: '事项', value: '完整说明'.repeat(100), reading: richReading()}], sections: [
    {id: 'original-fields', kind: 'facts', fields: [{label: '富文本字段', value: '字段内容', reading: richReading()}]},
    {id: 'existing-opinions', kind: 'table', columns: [{key: 'opinion', label: '意见'}],
      rows: [{id: 'original-opinion', cells: {opinion: '可读意见'}, readings: {opinion: richReading()}}]},
  ]};
  for (const revision of ['原始材料', '更新后的材料']) {
    renderEvidence(document, target, {...review, title: revision}, 'efficiency_data');
    assert.match(target.textContent, /全文见下方/);
    assert.equal(descendants(target).filter(node => node.className === 'material-reading').length, 3);
    assert.equal(descendants(target).filter(node => node.tag === 'strong').length, 3);
  }
});

test('reading projection cannot carry arbitrary tags attributes URLs or unsafe cell spans', () => {
  for (const item of [{tag: 'script', children: ['do not execute']}, {tag: 'img', children: []},
    {tag: 'td', colspan: 101, children: ['invalid span']}, {tag: 'p', rowspan: 1, children: []}]) {
    const document = createDocument(), target = document.createElement('div');
    appendReading(document, target, '<script>literal fallback</script>', {version: 1, nodes: [item], notices: []});
    assert.equal(target.textContent, '<script>literal fallback</script>');
    assert.equal(descendants(target).length, 1);
  }
  const document = createDocument(), target = document.createElement('div');
  appendReading(document, target, '可读正文', {version: 1, nodes: [
    {tag: 'p', children: ['可读正文'], style: 'display:none', onclick: 'alert(1)', href: 'https://example.invalid/', id: 'draft-submit'},
  ], notices: []});
  assert.equal(target.textContent, '可读正文');
  const paragraph = descendants(target).find(node => node.tag === 'p');
  assert.deepEqual(paragraph.attributes, {});
});

test('overly deep reading data falls back to complete inert text including the tail', () => {
  const document = createDocument(), target = document.createElement('div');
  let item = '正文'; for (let i = 0; i < 70; i++) item = {tag: 'span', children: [item]};
  const text = '<p>完整原始内容</p>'.repeat(1000) + '不可丢失的尾部';
  appendReading(document, target, text, {version: 1, nodes: [item], notices: []});
  assert.equal(target.textContent, text);
  assert.equal(descendants(target).length, 1);
});

test('weekly report and handover layouts retain full source material and distinct item rows', () => {
  const document = createDocument(), target = document.createElement('div'); target.querySelectorAll = () => [];
  const text = '完整周报正文'.repeat(1000);
  renderEvidence(document, target, {fields: [{label: '周报名称', value: '本周周报'}], sections: [
    {id: 'report', kind: 'text', title: '本周工作总结', text},
    {id: 'plan', kind: 'text', title: '下周工作计划', text: '既有计划'},
  ]}, 'weekly_report');
  assert.ok(target.textContent.includes(text));
  assert.match(target.textContent, /本周工作总结/);
  assert.match(target.textContent, /下周工作计划/);
  renderEvidence(document, target, {fields: [{label: '移交人', value: '甲'}], sections: [
    {id: 'work', kind: 'table', title: '工作交接', columns: [{key: 'receiver', label: '接收人'}],
      rows: [{id: 'a', cells: {receiver: '乙'}}, {id: 'b', cells: {receiver: '丙'}}]},
  ]}, 'work_handover');
  assert.deepEqual(descendants(target).filter(node => node.tag === 'tr' && node.dataset.rowId).map(node => node.dataset.rowId), ['a', 'b']);
});

test('all registered review layouts expose their existing key facts without editable decisions', () => {
  assert.equal(Object.keys(REVIEW_DESIGNS).length, 24);
  for (const [kind, design] of Object.entries(REVIEW_DESIGNS)) {
    const document = createDocument(), target = document.createElement('div');
    target.querySelectorAll = () => [];
    renderEvidence(document, target, {fields: [{label: '事项', value: '源标题'},
      ...design.keys.map(label => ({label, value: `原始${label}`}))]}, kind);
    assert.match(target.textContent, new RegExp(design.title));
    assert.ok(descendants(target).some(node => node.className === `review review-${design.family}`));
    for (const label of design.keys) assert.ok(target.textContent.includes(`原始${label}`));
    assert.ok(!descendants(target).some(node => ['input', 'select', 'button'].includes(node.tag)));
  }
});

test('reward information and each named allocation open as read-only source materials', () => {
  const document = createDocument(), target = document.createElement('div');
  target.querySelectorAll = selector => selector === 'details' ? descendants(target).filter(node => node.tag === 'details') : [];
  const review = {title: '知识产权奖励阅办', fields: [
    {label: '奖项名称', value: '合成奖项'}, {label: '奖励金额', value: '2000.00'},
  ], sections: [
    {id: 'reward-information', kind: 'facts', title: '奖励信息', fields: [
      {label: '所属业务负责人', value: '负责人甲'}, {label: '提示信息', value: '按原分配明细知会'},
    ]},
    {id: 'reward-allocations', kind: 'table', title: '奖励人员及金额明细', columns: [
      {key: 'field0011_id', label: '奖励人姓名'}, {key: 'field0012_id', label: '奖励金额明细'},
    ], rows: [
      {id: 'record-a', cells: {field0011_id: '奖励人甲', field0012_id: '1200.00'}},
      {id: 'record-b', cells: {field0011_id: '奖励人乙', field0012_id: '800.00'}},
    ]},
  ]};
  renderEvidence(document, target, review, 'intellectual_property_reward');
  let nodes = descendants(target);
  for (const id of ['reward-information', 'reward-allocations']) {
    assert.equal(nodes.find(node => node.dataset.group === `material:${id}`).open, true);
  }
  assert.deepEqual(nodes.filter(node => node.tag === 'tr' && node.dataset.rowId).map(node => [
    node.dataset.rowId, ...node.children.map(cell => cell.textContent),
  ]), [['record-a', '奖励人甲', '1200.00'], ['record-b', '奖励人乙', '800.00']]);
  assert.match(target.textContent, /负责人甲/);
  assert.ok(!nodes.some(node => ['input', 'select', 'textarea', 'button', 'form'].includes(node.tag)));
  nodes.find(node => node.dataset.group === 'material:reward-allocations').open = false;
  renderEvidence(document, target, {...review, title: '更新后的知识产权奖励阅办'}, 'intellectual_property_reward');
  nodes = descendants(target);
  assert.equal(nodes.find(node => node.dataset.group === 'material:reward-allocations').open, false);
  assert.match(target.textContent, /1200\.00/);
  assert.match(target.textContent, /800\.00/);
});

test('primary weekly and itinerary sections open initially but preserve an explicit collapse across updates', () => {
  const document = createDocument(), target = document.createElement('div');
  target.querySelectorAll = selector => selector === 'details' ? descendants(target).filter(node => node.tag === 'details') : [];
  const review = {sections: [{id:'current-week',title:'本周工作',kind:'text',text:'原文'},
    {id:'existing-opinions',title:'已有意见',kind:'text',text:'只读意见'}]};
  renderEvidence(document,target,review,'weekly_report');
  let primary = descendants(target).find(node=>node.dataset.group === 'material:current-week');
  assert.equal(primary.open,true);
  assert.equal(descendants(target).find(node=>node.dataset.group === 'material:existing-opinions').open,false);
  primary.open=false;
  renderEvidence(document,target,{...review,title:'新标题'},'weekly_report');
  primary = descendants(target).find(node=>node.dataset.group === 'material:current-week');
  assert.equal(primary.open,false);
});

test('weekly overview does not repeat the same content shown in its primary side by side sections', () => {
  const document=createDocument(),target=document.createElement('div'); target.querySelectorAll=()=>[];
  renderEvidence(document,target,{fields:[{label:'周报名称',value:'合成周报'},{label:'本周说明',value:'独特本周说明'}],
    sections:[{id:'current-week',title:'本周工作',kind:'facts',fields:[{label:'本周说明',value:'独特本周说明'}]},
      {id:'next-week',title:'下周计划',kind:'facts',fields:[{label:'计划',value:'独特下周计划'}]}]},'weekly_report');
  assert.equal(target.textContent.split('独特本周说明').length,2);
  const grid=descendants(target).find(node=>node.className==='material-primary-grid');
  assert.equal(grid.children.length,2);
  assert.ok(grid.children.every(node=>node.open));
});

test('a hidden draft ignores late preview receipts and cannot start another request', async () => {
  const pending = deferred();
  const h = harness(() => pending.promise);
  const preview = h.controller.preview();
  const rendered = h.rendered.length;
  h.controller.suspend();
  pending.resolve({...reviewed(initial()), state: 'approved', allowedActions: [],
    receipt: {flowUrl: '/flow/authorize/' + 'a'.repeat(43)}});
  await preview;
  await h.controller.save(); await h.controller.approve();
  assert.equal(h.rendered.length, rendered);
  assert.equal(h.receipts.length, 0);
  assert.deepEqual(h.calls.map(call => call.action), ['preview']);
});

test('back-forward restoration preserves unsent input and focus, reads first, and restarts one poll', async () => {
  let backend = reviewed(initial());
  const h = mountHarness(backend, {fetch: async (url, options) => {
    const action = url.split('/').at(-1), body = options.body && JSON.parse(options.body);
    if (action === 'values') backend = {...backend, revision: 1, version: 2, values: body.values,
      allowedActions: ['save', 'preview'], preview: null};
    if (action === 'preview') backend = reviewed(backend);
    return {ok: true, json: async () => backend};
  }});
  await flush();
  h.nodes.get('content').focus(); h.edit('content', '尚未触发防抖的本页文字');
  h.hide();
  assert.equal(h.timers.size, 0);
  assert.equal(h.requests.length, 0);
  h.restore(); await flush();
  assert.deepEqual(h.requests.map(call => call.url.split('/').at(-1)), ['state', 'values', 'preview']);
  assert.equal(h.requests[0].options.method, 'GET');
  assert.equal(JSON.parse(h.requests[1].options.body).values.content, '尚未触发防抖的本页文字');
  assert.equal(h.nodes.get('content').value, '尚未触发防抖的本页文字');
  assert.equal(h.document.activeElement, h.nodes.get('content'));
  assert.equal(h.controller.state().canApprove, true);
  assert.equal(h.timers.size, 1);
  h.restore(); await flush();
  assert.equal(h.timers.size, 1);
  assert.ok(!h.requests.some(call => call.url.endsWith('/approve')));
});

test('a decision completed after hiding navigates only after a fresh restored read of its receipt', async () => {
  const pending = deferred(), state = reviewed(initial());
  let backend = state;
  const h = mountHarness(state, {fetch: (url) => url.endsWith('/approve') ? pending.promise :
    Promise.resolve({ok: true, json: async () => backend})});
  const approval = h.controller.approve();
  h.hide();
  backend = {...state, version: 2, state: 'finalized', allowedActions: [],
    receipt: {flowUrl: '/flow/authorize/' + 'a'.repeat(43)}};
  pending.resolve({ok: true, json: async () => backend});
  await approval;
  assert.deepEqual(h.navigations, []);
  assert.equal(h.requests.length, 1);
  h.restore(); await flush();
  assert.deepEqual(h.requests.map(call => call.options.method), ['POST', 'GET']);
  assert.deepEqual(h.navigations, ['https://trusted.example/flow/authorize/' + 'a'.repeat(43)]);
  assert.equal(h.controller.state().locked, true);
});

test('uncertain decision restoration is read only and an explicit retry preserves the original decision key', async () => {
  const pending = deferred(), state = reviewed(initial());
  let attempts = 0;
  const h = harness((action, payload) => {
    if (action === 'state') return Promise.resolve(state);
    if (action === 'approve' && ++attempts === 1) return pending.promise;
    return Promise.resolve({...state, version: 2, state: 'finalized', allowedActions: [],
      receipt: {requestKey: payload.requestKey}});
  }, state);
  const approval = h.controller.approve();
  h.controller.suspend(); pending.reject(new TypeError('closed WebView'));
  await approval;
  await h.controller.restore();
  assert.deepEqual(h.calls.map(call => call.action), ['approve', 'state']);
  assert.equal(h.controller.state().uncertain, true);
  assert.equal(h.controller.state().canApprove, false);
  assert.equal(h.controller.state().canRetry, true);
  await h.controller.retry();
  const decisions = h.calls.filter(call => call.action === 'approve');
  assert.equal(decisions.length, 2);
  assert.deepEqual(decisions[0].payload, decisions[1].payload);
  assert.equal(h.receipts.length, 1);
});

test('restored state rejects a changed revision before another save and keeps the local input for explicit comparison', async () => {
  const pending = deferred(), state = reviewed(initial());
  const changed = {...state, version: 2, revision: 1, values: {...values, content: '已落盘版本'}};
  const h = harness(action => action === 'values' ? pending.promise : Promise.resolve(changed), state);
  h.change({content: '本页尚未确认的文本'});
  const saving = h.controller.save();
  h.controller.suspend();
  await h.controller.restore();
  pending.resolve(changed); await saving;
  assert.equal(h.local().content, '本页尚未确认的文本');
  assert.equal(h.controller.state().conflict.revision, 1);
  assert.equal(h.controller.state().canApprove, false);
  assert.deepEqual(h.calls.map(call => call.action), ['values', 'state']);
});

test('restoration with lost permissions cannot follow a stale receipt or save local input', async () => {
  const pending = deferred(), state = reviewed(initial());
  const h = harness(action => action === 'approve' ? pending.promise :
    Promise.reject(Object.assign(new Error('当前权限已失效'), {status: 403})), state);
  const approval = h.controller.approve();
  h.controller.suspend();
  pending.resolve({...state, version: 2, state: 'finalized', allowedActions: [], receipt: {requestKey: 'request-1'}});
  await approval; await h.controller.restore();
  assert.deepEqual(h.calls.map(call => call.action), ['approve', 'state']);
  assert.equal(h.receipts.length, 0);
  assert.equal(h.controller.state().canApprove, false);
  assert.equal(h.controller.state().canEdit, false);
});

test('offline restoration keeps local editing available but cannot save or approve until a fresh read succeeds', async () => {
  const state = reviewed(initial()); let online = false, backend = state;
  const h = harness(async (action, payload) => {
    if (!online) throw new TypeError('offline');
    if (action === 'values') backend = {...backend, revision: 1, version: 2, values: payload.values,
      preview: null, allowedActions: ['save', 'preview']};
    if (action === 'preview') backend = reviewed(backend);
    return backend;
  }, state);
  h.change({content: '离线期间保留本地文字'}); h.controller.suspend();
  await h.controller.restore();
  assert.equal(h.controller.state().canEdit, true);
  assert.equal(h.controller.state().canApprove, false);
  assert.equal(h.controller.state().canRetry, true);
  assert.deepEqual(h.calls.map(call => call.action), ['state']);
  h.change({content: '离线期间继续编辑的文字'});
  await h.controller.save();
  assert.deepEqual(h.calls.map(call => call.action), ['state']);
  online = true; await h.controller.retry();
  assert.deepEqual(h.calls.map(call => call.action), ['state', 'state', 'values', 'preview']);
  assert.equal(h.local().content, '离线期间继续编辑的文字');
  assert.equal(h.controller.state().canApprove, true);
});


test('meeting invitation exposes exact title time place and expanded body', () => {
  const document = createDocument(), target = document.createElement('div');
  target.querySelectorAll = () => [];
  renderEvidence(document, target, {fields: [
    {label: '会议主题', value: '合成会议'}, {label: '开始时间', value: '08:45'},
    {label: '地点', value: '合成会议室'}, {label: '当前回执', value: '未回执'},
  ], sections: [{id: 'body', kind: 'text', title: '会议说明', text: '正文保留08:50原始说明'}]}, 'meeting_reply');
  for (const value of ['合成会议', '08:45', '合成会议室', '未回执']) assert.ok(target.textContent.includes(value));
  assert.ok(descendants(target).some(node => node.tag === 'details' && node.dataset.group === 'material:body' && node.open));
  assert.ok(target.textContent.includes('正文保留08:50原始说明'));
});

test('settlement account and full expense rows open read-only and preserve row identities', () => {
  const document = createDocument(), target = document.createElement('div');
  target.querySelectorAll = selector => selector === 'details' ? descendants(target).filter(node => node.tag === 'details') : [];
  const review = {title: '合成业务结算单', fields: [
    {label: '应付金额合计', value: '155.00'}, {label: '收款账号', value: 'SYNTHETIC-ACCOUNT'},
  ], sections: [
    {id: 'settlement-information', title: '业务结算信息', kind: 'facts', fields: [
      {label: '账户类型', value: '个人账户'}, {label: '收款账户名称', value: '合成收款人'},
    ]},
    {id: 'settlement-expenses', title: '费用明细', kind: 'table', columns: [
      {key: 'field0010_id', label: '事项说明'}, {key: 'field0015_id', label: '金额（元）'},
    ], rows: [
      {id: 'expense-a', cells: {field0010_id: '合成费用甲', field0015_id: '100.00'}},
      {id: 'expense-b', cells: {field0010_id: '合成费用乙', field0015_id: '55.00'}},
    ]},
    {id: 'settlement-payments', title: '付款记录', kind: 'table', columns: [
      {key: 'field0110_id', label: '付款金额（元）'},
    ], rows: [{id: 'payment-empty', cells: {field0110_id: ''}}]},
  ]};
  renderEvidence(document, target, review, 'business_settlement');
  let nodes = descendants(target);
  for (const id of ['settlement-information', 'settlement-expenses', 'settlement-payments']) {
    assert.equal(nodes.find(node => node.dataset.group === `material:${id}`).open, true);
  }
  assert.deepEqual(nodes.filter(node => node.tag === 'tr' && node.dataset.rowId).map(node => node.dataset.rowId),
    ['expense-a', 'expense-b', 'payment-empty']);
  for (const value of ['合成收款人', 'SYNTHETIC-ACCOUNT', '100.00', '55.00', '155.00']) {
    assert.ok(target.textContent.includes(value));
  }
  assert.ok(!nodes.some(node => ['input', 'select', 'textarea', 'button', 'form'].includes(node.tag)));
  nodes.find(node => node.dataset.group === 'material:settlement-expenses').open = false;
  renderEvidence(document, target, review, 'business_settlement');
  nodes = descendants(target);
  assert.equal(nodes.find(node => node.dataset.group === 'material:settlement-expenses').open, false);
});

function transportHarness() {
  let time = 0, id = 0;
  const timers = new Map(), requests = [], progress = [];
  const transport = createDraftTransport({apiUrl:'/draft',csrfToken:'proof',now:()=>time,
    onProgress:value=>progress.push(value),window:{
      fetch:(url,options)=>{const wait=deferred(); requests.push({url,options,...wait}); return wait.promise;},
      setTimeout:(fn,delay)=>{const key=++id;timers.set(key,{fn,at:time+delay});return key;},
      clearTimeout:key=>timers.delete(key),
    }});
  async function advance(ms) {
    const until=time+ms;
    while (true) {
      const next=[...timers].filter(([,entry])=>entry.at<=until).sort((a,b)=>a[1].at-b[1].at)[0];
      if (!next) break;
      timers.delete(next[0]);time=next[1].at;next[1].fn();await flush();
    }
    time=until;
  }
  return {transport,timers,requests,progress,advance};
}

test('all draft waits are bounded including a body that never decodes; late success cannot change the outcome', async () => {
  for (const [action,limit] of [['state',15000],['values',15000],['preview',45000],['options',30000],['approve',30000]]) {
    const h=transportHarness();
    const promise=h.transport.request(action,action==='state'?undefined:{requestKey:'original'}).catch(error=>error);
    if (action==='preview') h.requests[0].resolve({ok:true,json:()=>new Promise(()=>{})});
    await h.advance(6000);
    assert.equal(h.progress.at(-1).elapsedMs,6000);
    await h.advance(limit-6000);
    const error=await promise;
    assert.equal(error.code,'REQUEST_TIMEOUT');assert.equal(error.action,action);
    assert.equal(h.requests[0].options.signal.aborted,true);
    assert.equal(h.timers.size,0);assert.equal(h.requests.length,1);
    h.requests[0].resolve({ok:true,json:async()=>({receipt:{requestKey:'late'}})}); await flush();
    assert.equal((await promise).code,'REQUEST_TIMEOUT');
  }
});

test('draft transport preserves rejection status code and field errors and cancels waits on page departure', async () => {
  const h=transportHarness();
  const rejected=h.transport.request('preview',{}).catch(error=>error);
  h.requests[0].resolve({ok:false,status:403,json:async()=>({error:{code:'DRAFT_CSRF_INVALID',message:'会话已失效',fieldErrors:{opinion:'错误'}}})});
  const error=await rejected;
  assert.equal(error.status,403);assert.equal(error.code,'DRAFT_CSRF_INVALID');assert.equal(error.fieldErrors.opinion,'错误');
  const abandoned=h.transport.request('state').catch(error=>error); h.transport.suspend();
  assert.equal((await abandoned).name,'AbortError');assert.equal(h.requests[1].options.signal.aborted,true);
  assert.equal(h.timers.size,0);
});

test('save timeout retains local input and requires a fresh read before a conflicting save can resume', async () => {
  const changed={...initial(),revision:1,version:2,values:{...values,content:'原请求已保存'}};
  const h=harness(async action=>{
    if (action==='values') throw Object.assign(new Error('等待超时'),{code:'REQUEST_TIMEOUT'});
    return changed;
  });
  h.change({content:'本页新的输入'});await h.controller.save();
  assert.equal(h.controller.state().canApprove,false);
  h.change({content:'超时后继续编辑'});await h.controller.save();
  assert.equal(h.controller.state().canRetry,true);
  assert.match(h.controller.state().message,/重新检查/);
  assert.deepEqual(h.calls.map(call=>call.action),['values']);
  await h.controller.retry();
  assert.deepEqual(h.calls.map(call=>call.action),['values','state']);
  assert.equal(h.controller.state().conflict.revision,1);
  assert.equal(h.local().content,'超时后继续编辑');
});

test('an uncertain approval never replays when its original receipt read is unavailable', async () => {
  let online=false;
  const state=reviewed(initial());
  const h=harness(async (action,payload)=>{
    if (action==='approve') throw Object.assign(new Error('timeout'),{status:408});
    if (!online) throw new TypeError('offline');
    return {...state,state:'finalized',version:2,allowedActions:[],receipt:{requestKey:'request-1'}};
  },state);
  await h.controller.approve();await h.controller.retry();
  assert.equal(h.calls.filter(call=>call.action==='approve').length,1);
  assert.equal(h.controller.state().canEdit,false);assert.equal(h.controller.state().uncertain,true);
  online=true;await h.controller.retry();
  assert.equal(h.calls.filter(call=>call.action==='approve').length,1);
  assert.equal(h.receipts.length,1);
});

test('permanent draft permission failures stop scheduled polling and never enable retry', async () => {
  const state=reviewed(initial());
  const h=mountHarness(state,{fetch:async()=>({ok:false,status:410,json:async()=>({error:{code:'DRAFT_EXPIRED',message:'草稿已过期'}})})});
  const poll=[...h.timers.values()].find(timer=>timer.delay===2500);
  h.timers.clear();await poll.callback();await flush();
  assert.equal(h.controller.state().canEdit,false);assert.equal(h.controller.state().canRetry,false);
  assert.equal(h.timers.size,0);assert.equal(h.requests.length,1);
});

test('displayed material differences exclude current decision fields and bound source excerpts honestly', () => {
  const before = {fields:[{label:'金额',value:'10'},{label:'处理意见',value:'旧意见'}],
    sections:[{id:'body',title:'正文',kind:'text',text:'原文'}],attachments:[]};
  const after = {...before,fields:[{label:'金额',value:'20'},{label:'处理意见',value:'新意见'}],
    sections:[{id:'body',title:'正文',kind:'text',text:'<script>正文</script>'.repeat(200)}],attachments:[{name:'凭证.pdf'}]};
  const result = reviewDifferences(before,after);
  assert.equal(result.total,3);
  assert.deepEqual(result.items.map(item=>item.label),['金额','正文','附件清单']);
  assert.equal(result.items[0].before,'10'); assert.equal(result.items[0].after,'20');
  assert.match(result.items[1].after,/节选，请核对下方完整材料/);
  assert.equal(reviewDifferences(before,{...before,fields:[before.fields[0],{label:'处理意见',value:'仅修改意见'}]}).total,0);
});

test('material differences match field identity across inserted decision fields and reordered projections', () => {
  const before = {fields:[{label:'申请人',value:'甲'},{label:'事由',value:'原事由'}],
    groups:[{title:'费用明细',fields:[{label:'金额',value:'10'},{label:'金额',value:'20'}]}]};
  const after = {fields:[{label:'本次操作',value:'审批通过'},{label:'事由',value:'新事由'},
    {label:'申请人',value:'甲'},{label:'处理意见',value:'同意'}],
    groups:[{title:'费用明细',fields:[{label:'金额',value:'10'},{label:'金额',value:'30'}]}]};
  assert.deepEqual(reviewDifferences(before,after),{total:2,items:[
    {label:'事由',before:'原事由',after:'新事由'},
    {label:'费用明细 · 金额',before:'20',after:'30'}]});
});

test('long reading expands without altering content and a collapsed disclosure stays user controlled', () => {
  const document=createDocument(),target=document.createElement('div'); target.querySelectorAll=()=>[];
  const text='长材料段落'.repeat(500);
  renderEvidence(document,target,{sections:[{id:'body',kind:'text',title:'正文',text}]},'efficiency_data');
  const toggle=descendants(target).find(node=>node.tag==='button'&&node.textContent==='展开长文连续阅读');
  const reading=descendants(target).find(node=>node.className==='material-body');
  assert.equal(reading.textContent,text); toggle.events.click();
  assert.match(reading.className,/expanded/); assert.equal(reading.textContent,text);
  toggle.events.click(); assert.equal(reading.className,'material-body');
});

test('draft timeout preserves known permanent HTTP headers when an error body never finishes', async () => {
  for (const status of [401,403,404,410]) {
    const h=transportHarness();
    const wait=h.transport.request('state').catch(error=>error);
    h.requests[0].resolve({ok:false,status,json:()=>new Promise(()=>{})});await flush();
    await h.advance(15000);
    const error=await wait;
    assert.equal(error.status,status);assert.match(error.message,/已失效/);
    assert.equal(h.timers.size,0);
  }
});

test('approval with a permanent header and hanging JSON locks without reading or replaying another request', async () => {
  const state=reviewed(initial());
  const h=mountHarness(state,{fetch:async()=>({ok:false,status:403,json:()=>new Promise(()=>{})})});
  const approval=h.controller.approve();await flush();
  const deadline=[...h.timers.values()].find(timer=>timer.delay===30000);
  deadline.callback();await approval;
  assert.equal(h.controller.state().locked,true);assert.equal(h.controller.state().canRetry,false);
  assert.equal(h.controller.state().canEdit,false);
  await h.controller.retry();await h.controller.approve();
  assert.equal(h.requests.length,1);assert.deepEqual(h.navigations,[]);
});

test('trusted source return keeps its original bound navigation through state refresh and protects dirty input once',async()=>{
  const ticket='r'.repeat(43),state=reviewed({...initial(),navigation:{back:{kind:'result_list',url:`/results/${ticket}`,label:'ignored'}}});
  const h=mountHarness(state);await flush();
  const back=descendants(h.nodes.get('draft-form')).find(node=>node.id==='agentbridge-back');
  assert.equal(back.textContent,'返回待办列表');assert.equal(back.type,'button');
  h.window.location.hash='#tgWebAppVersion=8.0';h.edit('content','未保存的意见');
  h.window.confirm=()=>false;back.events.click();assert.deepEqual(h.navigations,[]);
  let prompts=0;h.window.confirm=()=>{prompts++;return true;};back.events.click();
  assert.equal(prompts,1);assert.deepEqual(h.navigations,[`https://trusted.example/results/${ticket}#tgWebAppVersion=8.0`]);
  let blocked=false;h.events.get('beforeunload')({preventDefault(){blocked=true;}});assert.equal(blocked,false);
  assert.ok(h.requests.every(item=>!item.url.endsWith('/approve')));
});

test('source return excludes external, credentials, fragments, arbitrary queries and invalid route shapes',()=>{
  const ticket='r'.repeat(43),origin='https://trusted.example';
  for(const url of [`https://evil.test/results/${ticket}`,`https://u:p@trusted.example/results/${ticket}`,
    `/results/${ticket}?task=other`,`/results/${ticket}#fake`,`/flow/input/${ticket}`,'javascript:alert(1)'])
    assert.equal(safeResultReturn({back:{kind:'result_list',url}},origin),null);
  assert.equal(safeResultReturn({back:{kind:'chat',url:`/results/${ticket}`}},origin),null);
  assert.equal(safeFlowUrl(`/flow/authorize/${ticket}?result=${ticket}&result=${ticket}`,origin),null);
  assert.equal(safeFlowUrl(`/flow/authorize/${ticket}?result=${ticket}`,origin).search,`?result=${ticket}`);
});

test('native reactivation refreshes the original draft and receipt without another approval',async()=>{
  const state=reviewed(initial()),h=mountHarness(state);await flush();
  h.documentEvents.get('agentbridge:native-resume')();await flush();
  assert.ok(h.requests.some(item=>item.url.endsWith('/state')));
  assert.ok(h.requests.every(item=>!item.url.endsWith('/approve')));
  assert.deepEqual(h.navigations,[]);
});

const retained = () => ({retentionHours:24,requiresFreshCheck:true,saved:null,
  restore:{contentId:'old-content',version:3,savedAt:'2026-10-23T12:00:00Z',expiresAt:'2099-01-01T00:00:00Z',scope:'same_item'}});
test('explicit content restore saves local edits first, replaces only after fresh response, then regenerates preview without approval',async()=>{
  let backend=reviewed({...initial(),contentDraft:retained()});
  const h=harness(async(action,payload)=>{
    if(action==='values') backend={...backend,revision:backend.revision+1,version:backend.version+1,values:payload.values,preview:null,allowedActions:['save','preview']};
    if(action==='content-restore') backend={...backend,revision:backend.revision+1,version:backend.version+1,values:{...values,content:'之前保存的内容'},preview:null,options:null,allowedActions:['save','preview']};
    if(action==='preview') backend=reviewed(backend);
    return backend;
  },backend);
  h.change({content:'当前未保存的修改'});await h.controller.restoreContent();
  assert.deepEqual(h.calls.map(item=>item.action),['values','content-restore','preview']);
  assert.equal(h.calls[1].payload.expectedRevision,1);assert.equal(h.calls[1].payload.contentVersion,3);
  assert.equal(h.local().content,'之前保存的内容');assert.equal(h.controller.state().dirty,false);
  assert.equal(h.controller.state().canApprove,true);assert.deepEqual(h.receipts,[]);
});
test('content restore never bypasses a save conflict or substitutes local values after a denied restore',async()=>{
  const state=reviewed({...initial(),contentDraft:retained()});
  const h=harness(async()=>{throw Object.assign(new Error('conflict'),{status:409,code:'DRAFT_REVISION_CONFLICT',current:{...state,revision:1,version:2}});},state);
  h.change({content:'保留本页'});await h.controller.restoreContent();
  assert.deepEqual(h.calls.map(item=>item.action),['values']);assert.equal(h.local().content,'保留本页');assert.ok(h.controller.state().conflict);
  const denied=harness(async()=>{throw Object.assign(new Error('expired'),{status:410});},state);
  await denied.controller.restoreContent();assert.equal(denied.local().content,values.content);assert.equal(denied.controller.state().locked,true);
});
test('lost content restore response reads on resume and retries only explicitly with the immutable original command',async()=>{
  let backend=reviewed({...initial(),contentDraft:retained()}),posts=0;
  const h=harness(async(action)=>{
    if(action==='content-restore' && posts++===0) {
      backend={...backend,version:2,revision:1,values:{...values,content:'已恢复'},preview:null,allowedActions:['save','preview']};
      throw new Error('reply lost');
    }
    if(action==='preview') backend=reviewed(backend);
    return backend;
  },backend);
  await h.controller.restoreContent();assert.equal(h.controller.state().canApprove,false);assert.equal(h.controller.state().canEdit,false);
  const first=h.calls[0].payload;
  h.controller.suspend();await h.controller.restore();assert.equal(posts,1);assert.equal(h.local().content,values.content);
  await h.controller.retry();
  assert.equal(posts,2);assert.deepEqual(h.calls.filter(item=>item.action==='content-restore').map(item=>item.payload),[first,first]);
  assert.equal(h.local().content,'已恢复');assert.equal(h.calls.some(item=>item.action==='approve'),false);
});
test('discarding a retained copy does not clear inputs and pending/expired sources cannot restore content',async()=>{
  const state=reviewed({...initial(),contentDraft:retained()});
  const h=harness(async()=>({...state,version:2,contentDraft:{...retained(),restore:null}}),state);
  await h.controller.discardContent();assert.equal(h.local().content,values.content);assert.deepEqual(h.calls.map(item=>item.action),['content-discard']);
  const expired=harness(async()=>{throw new Error('unexpected');},{...state,expiresAt:'2000-01-01T00:00:00Z'});
  assert.equal(expired.controller.state().canRestoreContent,false);await expired.controller.restoreContent();assert.deepEqual(expired.calls,[]);
  const terminal=harness(async()=>{throw new Error('unexpected');},{...state,state:'finalized'});
  await terminal.controller.discardContent();assert.deepEqual(terminal.calls,[]);
});
test('lost discard response retries the same explicit deletion without changing current inputs or approving',async()=>{
  let backend=reviewed({...initial(),contentDraft:retained()}),attempts=0;
  const h=harness(async(action)=>{
    if(action==='content-discard' && attempts++===0) {
      backend={...backend,version:2,contentDraft:{...retained(),restore:null,saved:null}};
      throw new Error('lost acknowledgement');
    }
    return backend;
  },backend);
  await h.controller.discardContent();const command=h.calls[0].payload;
  h.controller.suspend();await h.controller.restore();assert.equal(attempts,1);
  await h.controller.retry();assert.equal(attempts,2);
  assert.deepEqual(h.calls.filter(call=>call.action==='content-discard').map(call=>call.payload),[command,command]);
  assert.deepEqual(h.local(),values);assert.equal(h.controller.state().current.contentDraft.restore,null);
  assert.ok(h.calls.every(call=>call.action!=='approve' && call.action!=='values'));
});
test('restored meeting room content requires a new explicit selection even if the old room is still available',async()=>{
  let backend=reviewed({...optionsFor(meeting()),contentDraft:retained()});backend.values.room='token-1';
  const h=harness(async(action)=>{
    if(action==='content-restore') backend={...backend,version:backend.version+1,revision:backend.revision+1,preview:null,options:null,allowedActions:['save','options','preview']};
    if(action==='options') backend=optionsFor(backend);
    if(action==='preview') backend=reviewed(backend);
    return backend;
  },backend);
  await h.controller.restoreContent();assert.equal(h.controller.state().roomChoiceRequired,true);assert.equal(h.controller.state().canApprove,false);
  assert.deepEqual(h.calls.map(item=>item.action),['content-restore','options']);
});
test('clicking a fresh room after content restore saves the explicit choice and enables a newly checked decision',async()=>{
  let backend=reviewed({...optionsFor(meeting()),contentDraft:retained()});backend.values.room='token-1';
  const h=mountHarness(backend,{fetch:async(url,options)=>{
    const payload=options.body ? JSON.parse(options.body) : null;
    if(url.endsWith('/content-restore')) backend={...backend,revision:backend.revision+1,version:backend.version+1,preview:null,options:null,allowedActions:['save','options','preview']};
    if(url.endsWith('/values')) backend={...backend,revision:backend.revision+1,version:backend.version+1,values:payload.values,preview:null,options:null,allowedActions:['save','options','preview']};
    if(url.endsWith('/options')) backend=optionsFor(backend);
    if(url.endsWith('/preview')) backend=reviewed(backend);
    return {ok:true,json:async()=>backend};
  }});await flush();await h.controller.restoreContent();
  const radio=descendants(h.nodes.get('room-options')).find(node=>node.tag==='input');
  assert.equal(radio.checked,false);assert.equal(h.controller.state().canApprove,false);
  radio.checked=true;radio.events.change();await h.controller.save();
  assert.equal(h.controller.state().roomChoiceRequired,false);assert.equal(h.controller.state().canApprove,true);
  assert.ok(h.requests.some(item=>item.url.endsWith('/values')));assert.ok(h.requests.every(item=>!item.url.endsWith('/approve')));
});
test('content panel uses an accessible inline confirmation with no requests on opening or cancellation',async()=>{
  const state=reviewed({...initial(),contentDraft:retained()}),h=mountHarness(state);await flush();
  assert.match(h.nodes.get('draft-content-candidate').textContent,/同一事项.*恢复会替换当前输入/);
  h.window.confirm=()=>{throw new Error('Native dialog must not be used');};
  const before=h.requests.length;h.nodes.get('draft-content-restore').events.click();await flush();
  assert.equal(h.nodes.get('draft-content-confirmation').hidden,false);
  assert.match(h.nodes.get('draft-content-confirm-message').textContent,/不会授权或提交业务/);
  assert.equal(h.document.activeElement,h.nodes.get('draft-content-cancel'));
  assert.equal(h.nodes.get('draft-submit').disabled,true);assert.equal(h.nodes.get('draft-form').dataset.agentbridgeBusy,'true');
  h.nodes.get('draft-form').events.submit({preventDefault(){}});await flush();assert.equal(h.requests.length,before);
  h.nodes.get('draft-content-cancel').events.click();assert.equal(h.nodes.get('draft-content-confirmation').hidden,true);
  assert.equal(h.document.activeElement,h.nodes.get('draft-content-restore'));assert.equal(h.requests.length,before);
  h.nodes.get('draft-content-restore').events.click();h.nodes.get('draft-content-confirm').events.click();await flush();
  assert.equal(h.requests.filter(item=>item.url.endsWith('/content-restore')).length,1);
  assert.ok(h.requests.every(item=>!item.url.endsWith('/approve')));
});
test('inline discard requires its own confirm, supports Escape and does not authorize or clear inputs',async()=>{
  const state=reviewed({...initial(),contentDraft:retained()}),h=mountHarness(state);await flush();
  const before=h.requests.length;h.nodes.get('draft-content-discard').events.click();
  assert.match(h.nodes.get('draft-content-confirm-message').textContent,/全部保留内容.*不会清空/);
  h.nodes.get('draft-content-confirmation').events.keydown({key:'Escape',preventDefault(){}});
  assert.equal(h.nodes.get('draft-content-confirmation').hidden,true);assert.equal(h.requests.length,before);
  h.nodes.get('draft-content-discard').events.click();h.nodes.get('draft-content-confirm').events.click();await flush();
  assert.equal(h.requests.filter(item=>item.url.endsWith('/content-discard')).length,1);
  assert.equal(h.nodes.get('content').value,values.content);assert.ok(h.requests.every(item=>!item.url.endsWith('/approve')));
});
test('inline confirmation cannot silently target a changed retained copy and is forgotten on page restoration',async()=>{
  const state=reviewed({...initial(),contentDraft:retained()}),h=mountHarness(state);await flush();
  h.nodes.get('draft-content-restore').events.click();
  h.responses.push({...state,version:state.version+1,contentDraft:{...retained(),restore:{...retained().restore,contentId:'replacement'}}});
  await h.controller.poll();assert.equal(h.nodes.get('draft-content-confirm').disabled,true);
  h.nodes.get('draft-content-confirm').events.click();await flush();assert.ok(h.requests.every(item=>!item.url.endsWith('/content-restore')));
  h.hide();h.restore();await flush();assert.equal(h.nodes.get('draft-content-confirmation').hidden,true);
  assert.ok(h.requests.every(item=>!item.url.endsWith('/approve') && !item.url.endsWith('/content-restore')));
});
test('server-configured Workspace return supports deployment root, protects dirty input and drops native launch data',async()=>{
  const state=reviewed({...initial(),navigation:{back:{kind:'workspace',url:'http://127.0.0.1:54119/workspace/'}}});
  const h=mountHarness(state);await flush();h.window.location.hash='#tgWebAppData=private';
  const back=descendants(h.nodes.get('draft-form')).find(node=>node.id==='agentbridge-back');assert.equal(back.textContent,'返回 Workspace');
  h.edit('content','未保存');h.window.confirm=()=>false;back.events.click();assert.deepEqual(h.navigations,[]);
  h.window.confirm=()=>true;back.events.click();assert.deepEqual(h.navigations,['http://127.0.0.1:54119/workspace/']);
  for(const url of ['/workspace/','javascript:alert(1)','https://u:p@workspace.test/','https://workspace.test/?next=x','https://workspace.test/#x'])
    assert.equal(safeResultReturn({back:{kind:'workspace',url}},'https://trusted.example'),null);
});
test('Workspace decision receipt carries neither Telegram result token nor launch hash',async()=>{
  const state=reviewed({...initial(),navigation:{back:{kind:'workspace',url:'https://workspace.example/'}}});
  const receipt={flowUrl:`/flow/authorize/${'r'.repeat(43)}`};
  const h=mountHarness(state,{fetch:async url=>({ok:true,json:async()=>url.endsWith('/approve') ? {...state,version:state.version+1,receipt,state:'approved',allowedActions:[]} : state})});
  h.window.location.hash='#tgWebAppData=private';await h.controller.approve();
  assert.deepEqual(h.navigations,[`https://trusted.example${receipt.flowUrl}`]);
});

test('material anchor clearance follows actual wrapped navigation height and releases its observer while hidden',async()=>{
 let height=63.2;const measured=[],observers=[];
 const h=mountHarness(reviewed(initial()),{configure(document,window){
  const nav={getBoundingClientRect:()=>({height})};document.querySelector=selector=>selector==='.draft-nav'?nav:null;
  document.documentElement={style:{setProperty:(name,value)=>measured.push([name,value])}};
  window.ResizeObserver=class {constructor(callback){this.callback=callback;observers.push(this);}observe(target){this.target=target;}disconnect(){this.disconnected=true;}};
 }});
 assert.deepEqual(measured.at(-1),['--draft-nav-clearance','80px']);assert.equal(observers.length,1);
 height=132;observers[0].callback();assert.deepEqual(measured.at(-1),['--draft-nav-clearance','148px']);
 const count=measured.length;observers[0].callback();assert.equal(measured.length,count);
 h.hide();assert.equal(observers[0].disconnected,true);assert.equal(h.events.has('resize'),false);
 height=170;observers[0].callback();assert.equal(measured.length,count);
 h.restore();await flush();assert.deepEqual(measured.at(-1),['--draft-nav-clearance','186px']);assert.equal(observers.length,2);
 h.hide();assert.equal(observers[1].disconnected,true);
});

test('older WebViews measure navigation again on resize and ignore unavailable geometry',()=>{
 let height=80;const measured=[];
 const h=mountHarness(reviewed(initial()),{configure(document){
  document.querySelector=()=>({getBoundingClientRect:()=>({height})});
  document.documentElement={style:{setProperty:(_name,value)=>measured.push(value)}};
 }});
 assert.equal(measured.at(-1),'96px');height=140;h.events.get('resize')();assert.equal(measured.at(-1),'156px');
 const count=measured.length;height=NaN;h.events.get('resize')();assert.equal(measured.length,count);
 h.hide();assert.equal(h.events.has('resize'),false);
});

test('mixed body prioritizes its explicit formatted blocks and preserves every bare source character in a collapsed accessible disclosure',()=>{
 const document=createDocument(),target=document.createElement('div');target.querySelectorAll=()=>[];
 const plain=['合法开场与更多 审批 加签\n<script>仅文字</script>','\n补充说明','\n原始尾部'];
 const reading={version:1,notices:['图片未读取'],nodes:[plain[0],
  {tag:'p',children:['原格式化正文',{tag:'strong',children:['保留重点']}]},plain[1],
  {tag:'table',children:[{tag:'tr',children:[{tag:'td',children:['00100.00']}]}]},plain[2]]};
 const review={sections:[{id:'body',kind:'text',title:'正文与页面材料',text:'完整来源',reading}]},before=JSON.stringify(review);
 renderEvidence(document,target,review,'efficiency_data');
 const nodes=descendants(target),body=nodes.find(n=>n.className==='material-reading');
 assert.equal(body.children[0].tag,'p');assert.equal(body.children[0].textContent,'原格式化正文保留重点');
 assert.equal(body.children[1].className,'reading-table');assert.equal(body.children[1].textContent,'00100.00');
 const original=nodes.find(n=>n.className==='material-page-text');
 assert.equal(original.open,false);assert.equal(original.children[0].tag,'summary');
 assert.equal(original.children[0].textContent,'其他原始页面文字（展开查看）');
 assert.equal(original.children[1].textContent,plain.join(''));assert.equal(original.children[1].tabIndex,0);
 assert.equal(body.children.at(-1),original);assert.match(body.textContent,/图片未读取/);
 assert.ok(!nodes.some(n=>['script','img','iframe','input','form'].includes(n.tag)));
 assert.equal(JSON.stringify(review),before);
});

test('plain-only body remains directly readable and an invalid mixed projection keeps the complete inert fallback',()=>{
 for(const reading of [undefined,{version:1,notices:[],nodes:['仅有纯文字，包含合法正文']},
  {version:1,notices:[],nodes:[{tag:'pre',children:['仅有纯文字，包含合法正文']}]},
  {version:1,notices:[],nodes:['前置文字',{tag:'script',children:['不可执行']}]}]) {
  const document=createDocument(),target=document.createElement('div');target.querySelectorAll=()=>[];
  const text='仅有纯文字，包含合法正文';renderEvidence(document,target,{sections:[{id:'body',kind:'text',text,reading}]},'efficiency_data');
  const nodes=descendants(target),body=nodes.find(n=>n.className==='material-body');
  assert.equal(body.textContent,text);assert.equal(nodes.some(n=>n.className==='material-page-text'),false);
 }
});

test('field readings and other text sections retain mixed node order without hiding plain context',()=>{
 const document=createDocument(),target=document.createElement('div');target.querySelectorAll=()=>[];
 const reading={version:1,notices:[],nodes:['前置合法文字',{tag:'p',children:['格式化段落']},'后置合法文字']};
 renderEvidence(document,target,{fields:[{label:'事项',value:'原字段',reading}],sections:[
  {id:'report',kind:'text',title:'原报告',text:'原报告',reading}]},'weekly_report');
 const material=descendants(target).filter(n=>n.className==='material-reading');
 assert.equal(material.length,2);
 for(const body of material) assert.equal(body.textContent,'前置合法文字格式化段落后置合法文字');
 assert.equal(descendants(target).some(n=>n.className==='material-page-text'),false);
});

test('user-expanded original page text stays expanded after a changed evidence projection',()=>{
 const document=createDocument(),target=document.createElement('div');
 target.querySelectorAll=selector=>selector==='details'?descendants(target).filter(n=>n.tag==='details'):[];
 const review={sections:[{id:'body',kind:'text',text:'原材料',reading:{version:1,notices:[],nodes:['原页面文字',{tag:'p',children:['格式正文']} ]}}]};
 renderEvidence(document,target,review,'efficiency_data');
 descendants(target).find(n=>n.className==='material-page-text').open=true;
 renderEvidence(document,target,{...review,title:'更新后的来源标题'},'efficiency_data');
 assert.equal(descendants(target).find(n=>n.className==='material-page-text').open,true);
});
