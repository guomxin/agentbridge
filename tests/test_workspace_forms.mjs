import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createLifecycle } from "../agentbridge/workspace/static/workspace_lifecycle.mjs";
import { createWorkspaceRequests } from "../agentbridge/workspace/static/workspace_request.mjs";
import { createComposer } from "../agentbridge/workspace/static/workspace_forms.mjs";
import { createSkillForms, skillPublicationCoverage, skillCandidateDiff, skillCandidateRemovals } from "../agentbridge/workspace/static/workspace_skills.mjs";
import { createSkillLeaveGuard } from "../agentbridge/workspace/static/workspace_skill_leave.mjs";

class Element {
  dataset = {};
  children = [];
  textContent = "";
  innerHTML = "";
  events = {};
  classList = { add() {}, remove() {} };
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute() {}
  addEventListener(name, callback) { this.events[name] = callback; }
  focus() {}
  querySelector() { return new Element(); }
  querySelectorAll() { return []; }
}
function dom() {
  const elements = new Map();
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  return { $, document: { cookie: "", createElement: () => new Element() } };
}
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
const flush = () => new Promise(resolve => setImmediate(resolve));
const workspaceSource = readFileSync(new URL('../agentbridge/workspace/static/workspace.js', import.meta.url), 'utf8');
function workspaceHandler(name, nextName, bindings) {
  const marker = workspaceSource.includes(`async function ${name}(`) ? `async function ${name}(` : `function ${name}(`;
  const start = workspaceSource.indexOf(marker);
  const next = workspaceSource.indexOf(`function ${nextName}(`, start);
  const end = workspaceSource.lastIndexOf('\n}', next) + 2;
  assert.ok(start >= 0 && end > start, 'load the actual workspace handler');
  return new Function(...Object.keys(bindings), `${workspaceSource.slice(start, end)}\nreturn ${name};`)(...Object.values(bindings));
}

test("quick query acknowledges only a valid task receipt so malformed success keeps its recovery request", async () => {
  const scope = createLifecycle().scope(), calls = [], timings = [], projected = [], synced = [];
  let receipt;
  const query = workspaceHandler('queryOaPending', 'continueTask', {
    sessionScope: scope, projectionOrder: {begin: () => 1, accept: () => true},
    scopedApi: () => async (_url, options) => {calls.push(options.body);return receipt;},
    markWorkspaceTiming: name => timings.push(name), upsertTaskCard: value => projected.push(value),
    scheduleTaskSync: id => synced.push(id),
  });
  const payload = {requestKey:'same-explicit-read-request'};
  for (receipt of [null, {}, {taskId:''}, {taskId:'unverified'}, {taskId:{value:'forged'}}]) {
    await assert.rejects(query(payload), /查询受理状态尚未确认/);
  }
  assert.deepEqual(synced, []);assert.deepEqual(projected, []);
  assert.ok(timings.every(name => name === 'quick_query_sent'));
  const taskId='12345678-1234-1234-1234-123456789abc';
  receipt={taskId,detail:{task:{task_id:taskId}}};
  assert.equal((await query(payload)).taskId,taskId);
  assert.deepEqual(synced,[taskId]);assert.equal(projected.length,1);
  assert.ok(calls.every(body => body === payload));scope.dispose();
});

function composerFixture(options = {}) {
  const scope = createLifecycle().scope();
  const state = { composerAttachments: [] };
  const readers = [];
  const messages = [];
  const warnings = [];
  class Reader {
    events = {};
    constructor() { readers.push(this); }
    addEventListener(name, callback) { this.events[name] = callback; }
    readAsDataURL() {}
    abort() { this.events.abort?.(); }
    load(content = "iVBORw0KGgo=") { this.result = `data:image/png;base64,${content}`; this.events.load(); }
  }
  const elements = dom();
  const forms = createComposer({ ...elements, state, getScope: () => scope,
    FileReader: Reader, crypto: { randomUUID: () => String(readers.length) },
    toast: (...args) => warnings.push(args), executeChatMessage: (...args) => messages.push(args), ...options,
  });
  return { forms, scope, state, readers, messages, warnings, ...elements };
}

test("typed query projects only its saved original message without advancing the SSE cursor", async () => {
  const scope = createLifecycle().scope(), ingested = [], projected = [], synced = [];
  let receipt;
  const query = workspaceHandler('queryOaPending', 'continueTask', {
    sessionScope: scope, projectionOrder: {begin: () => 1, accept: () => true},
    scopedApi: () => async () => receipt, markWorkspaceTiming() {},
    ingestTimelineEntry: (...args) => ingested.push(args), upsertTaskCard: value => projected.push(value),
    scheduleTaskSync: id => synced.push(id),
  });
  const taskId = '12345678-1234-1234-1234-123456789abc';
  const entry = {entry_id: '22345678-1234-1234-1234-123456789abc', sequence: 25,
    entry_type: 'chat_message', role: 'user', text: '查看OA待办', task_id: null, source: {is_origin: true}};
  const payload = {requestKey: 'original-typed-query', userMessage: entry.text};
  for (const invalid of [undefined, {...entry, text: '其他原文'}, {...entry, role: 'assistant'},
    {...entry, source: {is_origin: false}}, {...entry, task_id: 'another-task'}, {...entry, sequence: 0}]) {
    receipt = {taskId, userMessage: invalid};
    await assert.rejects(query(payload), /原消息保存状态尚未确认/);
  }
  assert.deepEqual(ingested, []);assert.deepEqual(projected, []);assert.deepEqual(synced, []);
  receipt = {taskId, userMessage: entry, detail: {task: {task_id: taskId}}};
  await query(payload);
  assert.deepEqual(ingested, [[entry, true, false]]);
  assert.deepEqual(synced, [taskId]);assert.equal(projected.length, 1);scope.dispose();
});

test("plain personal OA query uses the closed read once, while attachments and trial intent keep their original path", async () => {
  const reads = [], gate = deferred();
  const f = composerFixture({queryOaPending: async payload => {reads.push(payload);await gate.promise;}});
  const input = {value: "查看OA待办", disabled: false}, button = {disabled: false};
  const form = {elements: {message: input}, querySelectorAll: () => [button]};
  const sending = f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(input.disabled, true);assert.equal(button.disabled, true);
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(reads.length, 1);assert.deepEqual(Object.keys(reads[0]), ["requestKey", "userMessage"]);
  assert.equal(reads[0].userMessage, "查看OA待办");
  assert.deepEqual(f.messages, []);
  gate.resolve();await sending;
  assert.equal(input.value, "");assert.equal(input.disabled, false);assert.equal(button.disabled, false);
  f.state.composerAttachments = [{id: "image", size: 1}];input.value = "查看OA待办";
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(f.messages[0][0], "查看OA待办");assert.equal(f.messages[0][2].length, 1);
  assert.equal(reads.length, 1);
  f.state.composerSkillTrial = {draft_id: "trial", profile: "review", expected_revision: 1};
  input.value = "查看OA待办";
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(reads.length, 1);assert.equal(f.messages[1][3].draft_id, "trial");f.scope.dispose();
});

test("an uncertain plain query retains the same request and text until its original receipt is recovered", async () => {
  let attempt = 0, key = 0;
  const reads = [];
  const f = composerFixture({crypto: {randomUUID: () => `query-key-${++key}`},
    queryOaPending: async payload => {reads.push(payload);if (++attempt === 1) throw Error("等待应答已超时");}});
  const input = {value: "查看OA待办", disabled: false}, form = {elements: {message: input}};
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(input.value, "查看OA待办");assert.equal(input.disabled, false);
  assert.match(f.warnings[0][0], /超时/);assert.deepEqual(f.messages, []);
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(reads[0].requestKey, reads[1].requestKey);assert.equal(input.value, "");
  input.value = "查询OA待办";await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.notEqual(reads[1].requestKey, reads[2].requestKey);f.scope.dispose();
});

test("a late shortcut receipt cannot clear the next account's composer or recovery identity", async () => {
  let current = createLifecycle().scope(), key = 0;
  const reads = [], oldReply = deferred(), newReply = deferred();
  const f = composerFixture({getScope: () => current, crypto: {randomUUID: () => `query-key-${++key}`},
    queryOaPending: async payload => {reads.push(payload);await (reads.length === 1 ? oldReply.promise : newReply.promise);}});
  const input = {value: "查看OA待办", disabled: false}, form = {elements: {message: input}};
  const old = f.forms.sendChat({preventDefault() {}, currentTarget: form});
  current.dispose();current = createLifecycle().scope();input.disabled = false;input.value = "查询OA待办";
  const fresh = f.forms.sendChat({preventDefault() {}, currentTarget: form});
  oldReply.resolve();await old;
  assert.equal(input.value, "查询OA待办");assert.equal(input.disabled, true);
  assert.notEqual(reads[0].requestKey, reads[1].requestKey);assert.deepEqual(f.warnings, []);
  newReply.resolve();await fresh;assert.equal(input.value, "");assert.equal(input.disabled, false);
  current.dispose();f.scope.dispose();
});

test("recovering a timed-out query preserves later edited words until a separate explicit send", async () => {
  let key = 0, attempt = 0;
  const reads = [];
  const f = composerFixture({crypto: {randomUUID: () => `query-key-${++key}`},
    queryOaPending: async payload => {reads.push({...payload});if (++attempt === 1) throw Error('超时');}});
  const input = {value: '查看OA待办', disabled: false}, form = {elements: {message: input}};
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  input.value = '查询我的OA待办';
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.deepEqual(reads[1], reads[0]);assert.equal(reads[1].userMessage, '查看OA待办');
  assert.equal(input.value, '查询我的OA待办');assert.match(f.warnings.at(-1)[0], /当前修改的文字尚未发送/);
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.notEqual(reads[2].requestKey, reads[1].requestKey);
  assert.equal(reads[2].userMessage, '查询我的OA待办');assert.equal(input.value, '');
  assert.deepEqual(f.messages, []);f.scope.dispose();
});

test("account disposal cancels pending image reads and ignores a reader that reports load late", async () => {
  const f = composerFixture();
  const reading = f.forms.addComposerFiles([{type: "image/png", name: "old.png", size: 1}]);
  f.scope.dispose();
  f.readers[0].load();
  await reading;
  assert.deepEqual(f.state.composerAttachments, []);
  assert.deepEqual(f.warnings, []);
});

test("parallel image pastes share the attachment budget and submitted images keep a snapshot", async () => {
  const f = composerFixture();
  const reads = Array.from({length: 5}, (_, i) => f.forms.addComposerFiles([{type: "image/png", name: `${i}.png`, size: 1}]));
  f.readers.forEach(reader => reader.load());
  await Promise.all(reads);
  assert.equal(f.state.composerAttachments.length, 4);
  const original = f.state.composerAttachments[0];
  const form = { elements: { message: { value: "" } } };
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(f.state.composerAttachments.length, 0);
  assert.equal(f.messages[0][0], "请处理附加图片中的内容。");
  assert.equal(f.messages[0][2].length, 4);
  assert.notEqual(f.messages[0][2][0], original);
  f.scope.dispose();
});

test("file picker uses the actual supported image signature instead of the filename MIME type", async () => {
  for (const [bytes, mimeType] of [["\x89PNG\r\n\x1a\n", "image/png"],
    ["\xff\xd8\xffsample", "image/jpeg"], ["RIFF1234WEBP", "image/webp"]]) {
    const f = composerFixture();
    const reading = f.forms.addComposerFiles([{type: "image/jpeg", name: "renamed.jpg", size: bytes.length}]);
    f.readers[0].load(btoa(bytes));
    await reading;
    assert.equal(f.state.composerAttachments[0].mimeType, mimeType);
    assert.equal(f.state.composerAttachments[0].dataUrl, `data:${mimeType};base64,${btoa(bytes)}`);
    assert.equal(f.state.composerAttachments[0].fileName, "renamed.jpg");
    assert.deepEqual(f.warnings, []);
    f.scope.dispose();
  }
});

test("an unsupported or malformed image is rejected before sending without losing existing images", async () => {
  for (const content of [btoa("GIF89a"), btoa("RIFF1234WAVE"), "not-base64!"]) {
    const f = composerFixture();
    const existing = {id: "existing", size: 2};
    f.state.composerAttachments.push(existing);
    const reading = f.forms.addComposerFiles([{type: "image/png", name: "invalid.png", size: 12}]);
    f.readers[0].load(content);
    await reading;
    assert.deepEqual(f.state.composerAttachments, [existing]);
    assert.match(f.warnings[0][0], /图片内容不是/);
    assert.equal(f.messages.length, 0);
    f.scope.dispose();
  }
});

const trial = {draft_id: "draft-private-id", expected_revision: 7, profile: "preview", name: "周报助手"};
const trialForm = f => ({elements: {message: f.$("#chat-form textarea[name='message']")}});

test("short trial card binds the saved revision without putting technical context into material", () => {
  const f = composerFixture();
  f.forms.beginComposerTrial(trial);
  const card = f.$("#composer-skill-trial"), input = trialForm(f).elements.message;
  assert.equal(card.hidden, false);
  assert.match(card.children[0].textContent, /周报助手.*修订 7.*草稿预览/);
  assert.equal(input.value, "");
  assert.equal(input.maxLength, 3000);
  assert.equal(f.$("#attach-image").disabled, true);
  input.value = "计划修复登录问题。";
  card.children.at(-1).events.click();
  assert.equal(f.state.composerSkillTrial, null);
  assert.equal(input.value, "计划修复登录问题。");
  assert.equal(input.maxLength, 20000);
  assert.equal(f.$("#attach-image").disabled, false);
  assert.equal(card.hidden, true);
  f.scope.dispose();
});

test("short trial keeps material and binding until acceptance, and an old acceptance cannot clear a new trial", async () => {
  const f = composerFixture();
  f.forms.beginComposerTrial(trial);
  const form = trialForm(f);
  form.elements.message.value = "计划修复登录问题。";
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(f.messages[0][0], "计划修复登录问题。");
  assert.deepEqual(f.messages[0][2], []);
  assert.deepEqual(f.messages[0][3], trial);
  assert.notEqual(f.messages[0][3], f.state.composerSkillTrial);
  assert.equal(form.elements.message.value, "计划修复登录问题。");
  assert.deepEqual(f.state.composerSkillTrial, trial);
  f.messages[0][4]();
  assert.equal(f.state.composerSkillTrial, null);
  assert.equal(form.elements.message.value, "");
  f.forms.beginComposerTrial({...trial, expected_revision: 8});
  form.elements.message.value = "下一次的材料";
  f.messages[0][4]();
  assert.equal(f.state.composerSkillTrial.expected_revision, 8);
  assert.equal(form.elements.message.value, "下一次的材料");
  f.scope.dispose();
  assert.equal(f.state.composerSkillTrial, null);
  assert.equal(f.$("#composer-skill-trial").hidden, true);
});

test("short trial rejects long material, pasted images and images finishing after the trial starts", async () => {
  const f = composerFixture();
  const reading = f.forms.addComposerFiles([{type: "image/png", name: "pending.png", size: 1}]);
  f.forms.beginComposerTrial(trial);
  f.readers[0].load(); await reading;
  assert.deepEqual(f.state.composerAttachments, []);
  await f.forms.addComposerFiles([{type: "image/png", name: "trial.png", size: 1}]);
  assert.equal(f.readers.length, 1);
  assert.match(f.warnings.at(-1)[0], /仅支持合成文字/);
  const form = trialForm(f);
  form.elements.message.value = "字".repeat(3001);
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(f.messages.length, 0);
  assert.match(f.warnings.at(-1)[0], /3000/);
  form.elements.message.value = "合成材料";
  f.state.composerAttachments = [{id: "forged"}];
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(f.messages.length, 0);
  assert.deepEqual(f.state.composerSkillTrial, trial);
  f.scope.dispose();
});

test("explicit logout waits for the editor decision before destroying account state", async () => {
  for (const discard of [false, true]) {
    const scope = createLifecycle().scope(), decision = deferred(), changes = [], state = {account: {username: 'alice'}};
    const logout = workspaceHandler('logout', 'switchView', {
      sessionScope: scope, viewScope: scope.child(), state, canLeaveSkillEditor: () => decision.promise,
      viewState:{clear:()=>changes.push('clear-reading')},readingScroll:{dispose:()=>changes.push('dispose-reading')},
      stopWorkspaceObservers: () => changes.push('observers'), lifecycle: createLifecycle(),
      resetWorkspaceState: () => changes.push('reset'), showAuth: () => changes.push('auth'),
      setBusy() {}, $: () => ({}), api: async () => changes.push('logout'), location: {reload: () => changes.push('reload')},
    });
    const pending = logout();
    assert.deepEqual(changes, []);
    assert.equal(scope.current(), true);
    decision.resolve(discard); await pending;
    assert.deepEqual(changes, discard ? ['clear-reading','dispose-reading','observers', 'reset', 'auth', 'logout', 'reload'] : []);
    assert.equal(scope.current(), !discard);
    assert.equal(state.account === null, discard);
    scope.dispose();
  }
});

test("canceling a dispatch clears only its original trial intent after success and keeps retry material", async () => {
  for (const scenario of ['original', 'new trial', 'failed cancel']) {
    const f = composerFixture(), gate = deferred(), live = {actions: new Element()};
    f.forms.beginComposerTrial(trial);
    const form = trialForm(f); form.elements.message.value = '计划修复登录问题';
    await f.forms.sendChat({preventDefault() {}, currentTarget: form});
    const request = f.messages[0], cancelTrial = request[5];
    const attachCancel = workspaceHandler('attachDispatchCancel', 'dismissTerminalLiveMessages', {
      state: {activeStreams: new Map([['run', {onCanceled: cancelTrial}]])}, ensureLiveMessage: () => live,
      document: f.document, api: async () => {await gate.promise;if(scenario === 'failed cancel')throw Error('cancel failed');},
      addLiveProgress() {}, isCancellation: () => false, toast() {}, friendlyError: error => error.message,
    });
    attachCancel('run', 'dispatch');
    const canceling = live.actions.children[0].events.click();
    assert.deepEqual(f.state.composerSkillTrial, trial);
    if (scenario === 'new trial') {
      f.forms.beginComposerTrial({...trial, expected_revision: 8});
      form.elements.message.value = '新卡的材料';
    }
    gate.resolve(); await canceling;
    assert.deepEqual(request[3], trial);
    if (scenario === 'original') {
      assert.equal(f.state.composerSkillTrial, null);
      assert.equal(form.elements.message.value, '计划修复登录问题');
      f.forms.beginComposerTrial({...trial, expected_revision: 9});
      cancelTrial(); // A later aborted event for the canceled run is harmless.
      assert.equal(f.state.composerSkillTrial.expected_revision, 9);
    } else {
      assert.equal(f.state.composerSkillTrial.expected_revision, scenario === 'new trial' ? 8 : 7);
      assert.equal(form.elements.message.value, scenario === 'new trial' ? '新卡的材料' : '计划修复登录问题');
    }
    f.scope.dispose();
  }
});

const draft = id => ({draft_id: id, revision: 1, state: "editing", provenance: {kind: "description"},
  bundle: {manifest: {name: id, description: "示例", selection: {use_when: "适用", not_for: "不适用", output: "输出"}, profiles: {review: {}}}, resources: {"SKILL.md": "方法"}},
  revisions: [], tests: [], requests: [],
});
const quality = label => ({diagnostics: {message: label, checks: []}, diff: [], evaluation: null});

test("switching drafts while quality is loading cannot paint the previous draft into the new panel", async () => {
  const { $, document } = dom();
  const view = createLifecycle().scope();
  const oldInspect = deferred();
  const inspecting = deferred();
  const requests = createWorkspaceRequests({ document, getScope: () => view,
    fetch: async (path, options) => {
      let value;
      if (path.includes("?id=")) value = draft(path.split("=")[1]);
      else {
        const {action, data} = JSON.parse(options.body);
        if (action === "inspect" && data.draft_id === "first") { inspecting.resolve(); value = await oldInspect.promise; }
        else if (action === "inspect") value = quality("current quality");
        else if (action === "recipients") value = {items: []};
        else throw Error(`unexpected action ${action}`);
      }
      return {ok: true, json: async () => value};
    },
  });
  const forms = createSkillForms({document, $, state: {activeView: "skills"}, api: requests.api, getScope: () => view});
  const first = forms.showSkillDraft("first");
  await inspecting.promise;
  await forms.showSkillDraft("second");
  assert.match($("#draft-detail").innerHTML, /second/);
  assert.match($("#skill-quality-panel").innerHTML, /current quality/);
  oldInspect.resolve(quality("private stale quality"));
  await first;
  assert.doesNotMatch($("#skill-quality-panel").innerHTML, /private stale quality/);
  assert.match($("#draft-detail").innerHTML, /second/);
  view.dispose();
});

test("leaving the skill view destroys background job polling and ignores an already queued timer", async () => {
  const { $, document } = dom();
  const pending = new Map();
  let next = 0;
  const timers = {
    setTimeout(callback) { pending.set(++next, callback); return next; },
    clearTimeout(id) { pending.delete(id); },
  };
  const view = createLifecycle({timers}).scope();
  let calls = 0;
  const requests = createWorkspaceRequests({ document, getScope: () => view,
    fetch: async () => { calls++; return {ok: true, json: async () => ({items: [{job_id: "j", kind: "generation", state: "queued"}]})}; },
  });
  const forms = createSkillForms({document, $, state: {activeView: "skills"}, api: requests.api, getScope: () => view});
  await forms.refreshSkillJobs();
  assert.equal(pending.size, 1);
  const queued = [...pending.values()];
  view.dispose();
  queued.forEach(callback => callback());
  await flush();
  assert.equal(pending.size, 0);
  assert.equal(calls, 1);
});

class SkillElement extends Element {
  value = "";
  disabled = false;
  checked = false;
  queries = new Map();
  controls = new Map();
  formValues = {};
  elements = {namedItem: name => this.controls.get(name)};
  querySelector(selector) {
    if (!this.queries.has(selector)) this.queries.set(selector, new SkillElement());
    return this.queries.get(selector);
  }
  querySelectorAll(selector) { return this.queries.get(selector) || []; }
  reportValidity() { return true; }
  focus() { this.focused = true; }
  click() { this.clicked = true; }
}
class SkillFormData {
  constructor(form) { this.form = form; }
  getAll(name) {
    // Match the browser's exclusion of disabled fields and fieldsets.
    if (this.form.disabled || this.form.fieldset?.disabled) return [];
    const control = this.form.controls.get(name);
    if (control) return control.disabled ? [] : [control.value];
    const value = this.form.formValues[name];
    return value == null ? [] : Array.isArray(value) ? [...value] : [value];
  }
  get(name) { return this.getAll(name)[0] ?? null; }
}
const editableDraft = (id = "draft") => ({...draft(id), revision: 3,
  skill_id: "user.skill",
  bundle: {content_hash: "hash-3", manifest: {name: "日志助手", description: "归纳日志", selection: {use_when: "归纳", not_for: "无来源", output: "摘要"},
    profiles: {preview: {}, fill: {all: ["taihua.work_log.create"]}}, executionMode: "durable_plan", required_resources: {preview: ["references/evidence.md"], fill: []}},
    resources: {"SKILL.md": "保留计划与完成的区别。", "references/evidence.md": "核对来源。"}},
  tests: [{revision: 3, profile: "preview", status: "reported", prompt: "归纳计划", output: "计划"}],
});

function skillFixture({initial = editableDraft(), jobs = [], report = null, onAction, onRequest, timers, confirm = () => false, refuseNavigation = false} = {}) {
  const nodes = new Map();
  const $ = selector => { if (!nodes.has(selector)) nodes.set(selector, new SkillElement()); return nodes.get(selector); };
  const view = createLifecycle({timers}).scope();
  const drafts = new Map([[initial.draft_id, structuredClone(initial)]]);
  const calls = [], navigations = [], trials = [];
  const windowEvents = new Map();
  const eventTarget = {
    addEventListener: (type, callback) => windowEvents.set(type, callback),
    removeEventListener: (type, callback) => { if (windowEvents.get(type) === callback) windowEvents.delete(type); },
  };
  const editor = $("#draft-editor");
  editor.fieldset = $("#draft-editor-fields");
  for (const [name, value] of Object.entries({name: initial.bundle.manifest.name, description: initial.bundle.manifest.description,
    instructions: initial.bundle.resources["SKILL.md"], ...initial.bundle.manifest.selection})) {
    const element = new SkillElement(); element.value = value; editor.controls.set(name, element);
  }
  $("#draft-test-profile").value = "preview";
  $("#draft-export").id = "draft-export";
  editor.queries.set("input,textarea,select,button", [...editor.controls.values(), $("#draft-test-profile"), $("#draft-test"), $("#draft-export")]);
  $("#draft-submit").formValues = {profiles: ["preview"], audience: ["alice"], reason: "核对本修订"};
  $("#skill-publication-profiles").queries.set('input[name="profiles"]:checked', [{value: "preview"}]);
  const api = async (path, options = {}) => {
    options.scope?.assertCurrent();
    const {action, data} = options.body || {};
    calls.push({path, action, data: structuredClone(data)});
    let result;
    const custom = await onRequest?.(path, options);
    if (custom !== undefined) return custom;
    if (path.includes("?id=")) result = structuredClone(drafts.get(decodeURIComponent(path.split("?id=")[1])));
    else if (!action) result = {items: [], preferences: {revision: 1, value: {auto_draft: false}}, scopes: {items: []}, scope: "workspace", jobs: {items: jobs}};
    else {
      result = await onAction?.(action, data);
      if (result === undefined) {
        if (action === "inspect") result = {...quality("方法检查"), evaluation: report};
        else if (action === "recipients") result = {items: [{subject: "alice", name: "仅自己"}]};
        else if (action === "jobs") result = {items: jobs};
        else if (action === "get") result = structuredClone(drafts.get(data.draft_id));
        else if (action === "save") result = {draft_id: data.draft_id, revision: 7, content_hash: "hash-7"};
        else if (action === "submit" || action === "evaluate") result = {status: "queued"};
        else if (action === "adopt") {
          if (data.draft_id && drafts.get(data.draft_id).revision !== data.expected_revision) throw Error("草稿修订已变化");
          result = {draft_id: data.draft_id || initial.draft_id};
        } else throw Error(`unexpected action ${action}`);
      }
    }
    options.scope?.assertCurrent();
    return result;
  };
  const switchView = async (value, permit) => {
      if (refuseNavigation || !await forms.canLeaveSkillEditor(permit)) return false;
      navigations.push(value); return true;
  };
  const forms = createSkillForms({document: {createElement: () => new SkillElement()}, $, state: {activeView: "skills"}, api, eventTarget, confirm,
    FormData: SkillFormData, getScope: () => view, switchView, startTrial: async (trial, permit) => {
      if (await switchView("chat", permit) === false) return false;
      trials.push(trial); $("#chat-form textarea[name='message']").value = ""; return true;
    }});
  return {forms, $, view, editor, drafts, calls, navigations, windowEvents, trials};
}
const submitEvent = target => ({target, preventDefault() {}});

test("leave dialog has explicit actions, Escape stays, and account disposal closes a pending decision", async () => {
  const scope = createLifecycle().scope(), body = new Element();
  let refocused = 0, value = "dirty";
  const document = {body, activeElement: {focus() {refocused++;}}, createElement: () => {
    const element = new Element();
    element.showModal = () => { element.open = true; };
    element.close = () => { element.open = false; };
    element.remove = () => { body.children = body.children.filter(child => child !== element); };
    return element;
  }};
  const guard = createSkillLeaveGuard({document, eventTarget: {addEventListener() {}, removeEventListener() {}}});
  guard.register(scope, {isDirty: () => true, isBusy: () => false, snapshot: () => value, notify() {}, refresh() {}});
  let decision = guard.request();
  let dialog = body.children[0];
  assert.equal(dialog.open, true);
  assert.deepEqual(dialog.children[2].children.map(button => button.textContent), ["继续编辑", "放弃修改并离开"]);
  assert.equal(await guard.request(), false);
  dialog.children[2].children[0].events.click();
  assert.equal(await decision, false);
  assert.equal(body.children.length, 0);
  assert.equal(refocused, 1);
  decision = guard.request(); dialog = body.children[0];
  let prevented = false;
  dialog.events.cancel({preventDefault() {prevented = true;}});
  assert.equal(await decision, false);
  assert.equal(prevented, true);
  decision = guard.request(); dialog = body.children[0];
  dialog.children[2].children[1].events.click();
  assert.equal(await decision, true);
  decision = guard.request(); dialog = body.children[0];
  value = "late edit";
  dialog.children[2].children[1].events.click();
  assert.equal(await decision, false);
  decision = guard.request(); dialog = body.children[0];
  scope.dispose();
  assert.equal(await decision, false);
  assert.equal(body.children.length, 0);
  assert.equal(await guard.request(), true);
  dialog.children[2].children[1].events.click();
  assert.equal(body.children.length, 0);
});

test("dirty navigation can continue editing or discard once without saving, and owns beforeunload only while dirty", async () => {
  let discard = false;
  const prompts = [];
  const f = skillFixture({confirm: message => {prompts.push(message); return discard;}});
  f.drafts.set("other", editableDraft("other"));
  await f.forms.showSkillDraft("draft");
  assert.equal(f.windowEvents.size, 0);
  const field = f.editor.controls.get("instructions"), original = field.value;
  field.value = "未保存的重要修改"; f.editor.oninput();
  assert.equal(f.windowEvents.size, 1);
  let prevented = false;
  const unloading = {preventDefault() {prevented = true;}};
  f.windowEvents.get("beforeunload")(unloading);
  assert.equal(prevented, true);
  assert.equal(unloading.returnValue, "");
  const count = f.calls.length;
  assert.equal(await f.forms.showSkillDraft("other"), false);
  assert.equal(await f.forms.loadSkillDrafts(), false);
  assert.equal(await f.forms.canLeaveSkillEditor(), false);
  assert.equal(f.calls.length, count);
  assert.equal(field.value, "未保存的重要修改");
  assert.match(prompts[0], /放弃修改并离开.*继续编辑/);
  field.value = original; f.editor.oninput();
  assert.equal(f.windowEvents.size, 0);
  field.value = "再修改"; f.editor.oninput();
  discard = true;
  const priorPrompts = prompts.length;
  await f.forms.showSkillDraft("other");
  assert.equal(prompts.length, priorPrompts + 1);
  assert.equal(f.windowEvents.size, 0);
  assert.equal(f.calls.some(c => c.action === "save"), false);
  f.view.dispose();
});

test("saving blocks user switches but a completed save can explicitly redraw the editor", async () => {
  const gate = deferred();
  let prompts = 0;
  const f = skillFixture({confirm: () => {prompts++; return true;}, onAction: action => action === "save" ? gate.promise : undefined});
  await f.forms.showSkillDraft("draft");
  f.editor.controls.get("instructions").value = "保存中的修改"; f.editor.oninput();
  const saving = f.editor.onsubmit(submitEvent(f.editor));
  const count = f.calls.length;
  assert.equal(await f.forms.canLeaveSkillEditor(), false);
  assert.equal(await f.forms.loadSkillDrafts(), false);
  assert.equal(await f.forms.showSkillDraft("draft"), false);
  assert.equal(f.calls.length, count);
  assert.equal(prompts, 0);
  assert.match(f.$("#draft-detail-error").textContent, /正在处理/);
  gate.resolve({draft_id: "draft", revision: 7, content_hash: "hash-7"});
  await saving;
  assert.equal(f.calls.filter(c => c.path === "/api/skill-drafts?id=draft").length, 2);
  assert.equal(await f.forms.canLeaveSkillEditor(), true);
  assert.equal(f.windowEvents.size, 0);
  f.view.dispose();
});

test("late input during normal save stays protected instead of authorizing an internal redraw", async () => {
  const gate = deferred();
  const f = skillFixture({onAction: action => action === "save" ? gate.promise : undefined});
  await f.forms.showSkillDraft("draft");
  f.editor.controls.get("instructions").value = "发送的修改";
  const saving = f.editor.onsubmit(submitEvent(f.editor));
  f.editor.controls.get("instructions").value = "等待保存时追加的修改"; f.editor.oninput();
  gate.resolve({draft_id: "draft", revision: 7, content_hash: "hash-7"});
  await saving;
  assert.equal(f.calls.filter(c => c.path === "/api/skill-drafts?id=draft").length, 1);
  assert.equal(f.editor.controls.get("instructions").value, "等待保存时追加的修改");
  assert.equal(await f.forms.canLeaveSkillEditor(), false);
  assert.equal(f.windowEvents.size, 1);
  f.view.dispose();
  assert.equal(f.windowEvents.size, 0);
  assert.equal(await f.forms.canLeaveSkillEditor(), true);
});

test("a refused trial navigation does not overwrite the existing composer", async () => {
  const f = skillFixture({refuseNavigation: true});
  await f.forms.showSkillDraft("draft");
  f.$("#chat-form textarea[name='message']").value = "原有问题";
  await f.$("#draft-test").onclick();
  assert.equal(f.$("#chat-form textarea[name='message']").value, "原有问题");
  assert.deepEqual(f.navigations, []);
  f.view.dispose();
});

test("unsaved method edits block publication and evaluation without sending the old revision", async () => {
  const f = skillFixture();
  await f.forms.showSkillDraft("draft");
  f.editor.controls.get("instructions").value = "新增未保存的方法";
  f.editor.oninput();
  assert.match(f.$("#draft-save-state").textContent, /未保存/);
  assert.equal(f.$("#draft-test").textContent, "保存后试用");
  assert.equal(f.$("#draft-submit-button").disabled, true);
  assert.equal(f.$("#skill-eval-submit").disabled, true);
  await f.$("#draft-submit").onsubmit(submitEvent(f.$("#draft-submit")));
  await f.$("#skill-eval-form").onsubmit(submitEvent(f.$("#skill-eval-form")));
  assert.equal(f.calls.some(c => ["submit", "evaluate"].includes(c.action)), false);
  assert.match(f.$("#draft-detail-error").textContent, /请先保存/);
  f.view.dispose();
});

test("save then trial preserves disabled form values and uses the returned revision and selected mode", async () => {
  const gate = deferred();
  const f = skillFixture({onAction: action => action === "save" ? gate.promise : undefined});
  await f.forms.showSkillDraft("draft");
  f.editor.controls.get("instructions").value = "材料不足时写明缺口。";
  f.$("#draft-test-profile").value = "fill";
  f.editor.oninput();
  const pending = f.$("#draft-test").onclick();
  assert.equal(f.$("#draft-editor-fields").disabled, true);
  assert.equal(new SkillFormData(f.editor).get("instructions"), null);
  assert.equal(f.calls.find(c => c.action === "save").data.proposal.instructions, "材料不足时写明缺口。");
  assert.deepEqual(f.calls.find(c => c.action === "save").data.proposal.references, {"references/evidence.md": "核对来源。"});
  assert.equal(f.calls.find(c => c.action === "save").data.expected_revision, 3);
  await f.$("#draft-test").onclick();
  assert.equal(f.calls.filter(c => c.action === "save").length, 1);
  gate.resolve({draft_id: "draft", revision: 7, content_hash: "hash-7"});
  await pending;
  assert.deepEqual(f.navigations, ["chat"]);
  assert.deepEqual(f.trials, [{draft_id: "draft", expected_revision: 7, profile: "fill", name: "日志助手"}]);
  assert.equal(f.$("#chat-form textarea[name='message']").value, "");
  f.view.dispose();
});

test("text changed during a pending save is kept and cannot be trialed under the returned revision", async () => {
  const gate = deferred();
  const f = skillFixture({initial: {...editableDraft(), tested_profiles: ["preview", "fill"]}, onAction: action => action === "save" ? gate.promise : undefined});
  await f.forms.showSkillDraft("draft");
  f.editor.controls.get("instructions").value = "第一次修改";
  const pending = f.$("#draft-test").onclick();
  // Simulate a late host input despite the disabled fieldset.
  f.editor.controls.get("instructions").value = "保存等待中新增的修改";
  f.editor.oninput();
  gate.resolve({draft_id: "draft", revision: 7, content_hash: "hash-7"});
  await pending;
  assert.equal(f.editor.controls.get("instructions").value, "保存等待中新增的修改");
  assert.deepEqual(f.navigations, []);
  assert.match(f.$("#draft-save-state").textContent, /修订 7.*未保存/);
  assert.match(f.$("#draft-detail-error").textContent, /再次保存后试用/);
  assert.equal(f.$("#draft-submit-button").disabled, true);
  assert.match(f.$("#skill-publication-profiles").innerHTML, /value="preview" disabled/);
  assert.match(f.$("#skill-publication-profiles").innerHTML, /value="fill" disabled/);
  f.view.dispose();
});

test("a failed save keeps dirty text and never navigates to trial", async () => {
  const f = skillFixture({onAction: action => {if (action === "save") throw Error("草稿修订已变化");}});
  await f.forms.showSkillDraft("draft");
  f.editor.controls.get("instructions").value = "要保留的修改";
  await f.$("#draft-test").onclick();
  assert.equal(f.editor.controls.get("instructions").value, "要保留的修改");
  assert.deepEqual(f.navigations, []);
  assert.match(f.$("#draft-save-state").textContent, /未保存/);
  assert.equal(f.$("#draft-editor-fields").disabled, false);
  f.view.dispose();
});

test('complete skill configuration is saved with metadata and updated mode before trial',async()=>{
  const initial=editableDraft();initial.bundle.manifest.standard_metadata={license:'MIT',compatibility:'只处理文字'};
  const f=skillFixture({initial});await f.forms.showSkillDraft('draft');
  const walk=node=>[node,...node.children.flatMap(walk)];
  const field=(title,index=0)=>walk(f.$('#skill-configuration')).filter(node=>node.dataset.configField===title)[index];
  field('模式标识').value='summary';field('模式标识').oninput();
  field('模式说明').value='按证据整理';field('资料路径').value='references/来源.md';field('资料路径').oninput();
  field('资料内容').value='新的核对步骤';
  field('明确设置执行范围').checked=true;field('明确设置执行范围').onchange();
  field('明确选择可用能力').checked=true;field('明确选择可用能力').onchange();
  assert.equal(f.$('#draft-submit-button').disabled,true);
  await f.$('#draft-test').onclick();
  const proposal=f.calls.find(call=>call.action==='save').data.proposal;
  assert.deepEqual(proposal.standard_metadata,{license:'MIT',compatibility:'只处理文字'});
  assert.equal(proposal.profiles.summary.description,'按证据整理');assert.equal(proposal.profiles.preview,undefined);
  assert.deepEqual(proposal.profiles.summary.execution,{effects:['read'],allow_batch:false,capabilities:[]});
  assert.deepEqual(proposal.references,{'references/来源.md':'新的核对步骤'});
  assert.deepEqual(proposal.required_resources,{summary:['references/来源.md'],fill:[]});
  assert.equal(f.trials[0].profile,'summary');assert.equal(f.trials[0].expected_revision,7);
  f.view.dispose();
});

test('configuration conflicts preserve edits and block accidental use of the old revision',async()=>{
  const f=skillFixture({onAction:action=>{if(action==='save')throw Error('草稿修订已变化');}});
  await f.forms.showSkillDraft('draft');
  const walk=node=>[node,...node.children.flatMap(walk)];
  const description=walk(f.$('#skill-configuration')).find(node=>node.dataset.configField==='模式说明');
  description.value='保留这项修改';description.oninput();
  await f.$('#draft-test').onclick();
  assert.equal(description.value,'保留这项修改');assert.deepEqual(f.trials,[]);
  assert.match(f.$('#draft-detail-error').textContent,/修订已变化/);
  assert.equal(f.$('#draft-submit-button').disabled,true);
  f.view.dispose();
});

test('save errors are announced next to editor actions and brought into view without losing input',async()=>{
  const f=skillFixture({onAction:action=>{if(action==='save')throw Error('草稿已更新，请刷新后再操作');}});
  await f.forms.showSkillDraft('draft');
  const source=f.$('#draft-detail').innerHTML,notice=f.$('#draft-detail-error'),scrolls=[];
  assert.match(source,/id="draft-detail-error" role="alert" aria-live="assertive"/);
  assert.ok(source.indexOf('id="draft-detail-error"')<source.indexOf('历史修订'));
  assert.equal((source.match(/id="draft-detail-error"/g)||[]).length,1);
  notice.scrollIntoView=options=>scrolls.push(options);
  f.editor.controls.get('instructions').value='保留我刚编辑的长方法';
  await f.editor.onsubmit(submitEvent(f.editor));
  assert.match(notice.textContent,/草稿已更新/);
  assert.deepEqual(scrolls,[{block:'nearest',behavior:'smooth'}]);
  assert.equal(f.editor.controls.get('instructions').value,'保留我刚编辑的长方法');
  assert.equal(f.$('#draft-submit-button').disabled,true);
  f.view.dispose();
});

test('job progress refresh keeps method input and expanded report while showing partial usage',async()=>{
  let jobs=[{job_id:'job-progress',kind:'evaluation',state:'running',progress:1,name:'周报',revision:3,
    progress_stats:{completed:1,total:4,model_calls:1,usage:null,usage_complete:false,cost_complete:false}}];
  const f=skillFixture({onAction:action=>action==='jobs'?{items:jobs}:undefined});
  await f.forms.showSkillDraft('draft');
  f.editor.controls.get('instructions').value='不能被后台进度覆盖';f.editor.oninput();
  const expanded={dataset:{reportJob:'job-progress'},open:true};f.$('#skill-jobs').queries.set('[data-report-job]',[expanded]);
  await f.forms.refreshSkillJobs();
  assert.match(f.$('#skill-jobs').innerHTML,/完成 1\/4 项/);
  assert.match(f.$('#skill-jobs').innerHTML,/费用估算未返回/);
  jobs=[{...jobs[0],progress:2,progress_stats:{completed:2,total:4,model_calls:2,
    usage:{input:20,output:5,totalTokens:25,cost_estimate_usd:0.001},usage_calls:1,priced_calls:1,usage_complete:false,cost_complete:false}}];
  await f.forms.refreshSkillJobs();
  assert.match(f.$('#skill-jobs').innerHTML,/完成 2\/4 项/);
  assert.match(f.$('#skill-jobs').innerHTML,/部分记录/);
  assert.equal(f.editor.controls.get('instructions').value,'不能被后台进度覆盖');
  assert.equal(expanded.open,true);assert.equal(f.$('#draft-submit-button').disabled,true);
  f.view.dispose();
});

const draftListing = (items=[],jobs=[]) => ({items,preferences:{revision:1,value:{auto_draft:false}},scopes:{items:[]},scope:'workspace',jobs:{items:jobs}});

test('generation opens its progress and polling adds the completed draft without rebuilding dirty editors',async()=>{
  const pending=new Map(),jobs=[],items=[];let next=0;
  const timers={setTimeout(cb){pending.set(++next,cb);return next;},clearTimeout(id){pending.delete(id);}};
  const f=skillFixture({jobs,timers,onAction:action=>{
    if(action==='generate'){jobs.push({job_id:'generated-job',kind:'generation',state:'queued'});return jobs[0];}
  },onRequest:(path,options)=>path==='/api/skill-drafts'&&!options.body?draftListing(items,jobs):undefined});
  await f.forms.loadSkillDrafts();await f.forms.showSkillDraft('draft');
  const row=f.$('#skill-eval-cases').children[0];row.querySelector('[name=prompt]').value='尚未保存的待测题';f.$('#skill-eval-form').oninput();
  f.editor.controls.get('instructions').value='尚未保存的方法';f.editor.oninput();
  f.$('#skill-generate-material').value='整理我给的文字';f.$('#skill-background-jobs').open=false;
  await f.$('#skill-generate-form').onsubmit(submitEvent(f.$('#skill-generate-form')));
  assert.equal(f.$('#skill-background-jobs').open,true);assert.match(f.$('#draft-error').textContent,/已加入/);
  const poll=async()=>{assert.equal(pending.size,1);const [id,cb]=pending.entries().next().value;pending.delete(id);cb();await flush();};
  jobs[0].state='running';await poll();assert.match(f.$('#draft-error').textContent,/正在后台生成/);
  jobs[0]={...jobs[0],state:'succeeded',result:{draft_id:'new-draft'}};
  items.push({draft_id:'new-draft',name:'新文字助手',revision:1,state:'editing'});
  await poll();
  assert.match(f.$('#draft-error').textContent,/草稿已生成/);assert.equal(pending.size,0);
  assert.match(f.$('#draft-list').children[0].textContent,/新文字助手/);
  assert.equal(f.editor.controls.get('instructions').value,'尚未保存的方法');
  assert.equal(f.$('#skill-eval-cases').children[0],row);assert.equal(row.querySelector('[name=prompt]').value,'尚未保存的待测题');
  assert.equal(f.$('#skill-generate-material').value,'整理我给的文字');assert.equal(f.forms.canAutoReloadSkillEditor(),false);
  assert.equal(f.calls.filter(c=>c.action==='generate').length,1);assert.equal(f.calls.filter(c=>c.path.includes('?id=')).length,1);
  f.view.dispose();
});

test('generation terminal notices follow only the latest submitted job including failure cancellation and no-draft results',async()=>{
  const jobs=[];let sequence=0;
  const f=skillFixture({jobs,onAction:action=>action==='generate'?{job_id:`generation-${++sequence}`}:undefined});
  await f.forms.loadSkillDrafts();
  const submit=()=>f.$('#skill-generate-form').onsubmit(submitEvent(f.$('#skill-generate-form')));
  await submit();await submit();
  jobs.push({job_id:'generation-1',kind:'generation',state:'succeeded',result:{draft_id:'old-result'}});
  await f.forms.refreshSkillJobs();assert.match(f.$('#draft-error').textContent,/已加入/);
  jobs.push({job_id:'generation-2',kind:'generation',state:'failed',error:'模型暂时不可用'});
  await f.forms.refreshSkillJobs();assert.match(f.$('#draft-error').textContent,/生成失败.*模型暂时不可用/);
  await submit();jobs.push({job_id:'generation-3',kind:'generation',state:'canceled'});
  await f.forms.refreshSkillJobs();assert.equal(f.$('#draft-error').textContent,'本次生成已取消。');
  await submit();jobs.push({job_id:'generation-4',kind:'generation',state:'succeeded',result:{classification:'preference'}});
  await f.forms.refreshSkillJobs();assert.match(f.$('#draft-error').textContent,/提炼已完成/);
  assert.equal(f.calls.filter(c=>c.path==='/api/skill-drafts'&&!c.action).length,1,'no draft result must not claim a new draft or reload its list');
  f.view.dispose();
});

test('a failed completed-draft list refresh preserves the success receipt and recovers without generating again',async()=>{
  const jobs=[{job_id:'generation',kind:'generation',state:'succeeded',result:{draft_id:'new'}}];let reads=0;
  const f=skillFixture({jobs,onAction:action=>action==='generate'?{job_id:'generation'}:undefined,
    onRequest:(path,options)=>{
      if(path!=='/api/skill-drafts'||options.body)return;
      if(++reads===2)throw Error('列表读取超时');
      return draftListing(reads>2?[{draft_id:'new',name:'已生成的草稿',revision:1,state:'editing'}]:[],jobs);
    }});
  await f.forms.loadSkillDrafts();await f.$('#skill-generate-form').onsubmit(submitEvent(f.$('#skill-generate-form')));
  assert.match(f.$('#draft-error').textContent,/草稿已生成；列表暂未刷新/);
  await f.$('#skill-refresh-jobs').onclick();
  assert.match(f.$('#draft-list').children[0].textContent,/已生成的草稿/);assert.doesNotMatch(f.$('#draft-error').textContent,/暂未刷新/);
  await f.$('#skill-refresh-jobs').onclick();assert.equal(reads,3,'completed list is fetched once after recovery');
  assert.equal(f.calls.filter(c=>c.action==='generate').length,1);f.view.dispose();
});

test('leaving during a completed generation list read cannot paint the departed account',async()=>{
  const gate=deferred();let reads=0;
  const jobs=[{job_id:'generation',kind:'generation',state:'succeeded',result:{draft_id:'private-draft'}}];
  const f=skillFixture({jobs,onAction:action=>action==='generate'?{job_id:'generation'}:undefined,
    onRequest:(path,options)=>path==='/api/skill-drafts'&&!options.body?(++reads===1?draftListing([],jobs):gate.promise):undefined});
  await f.forms.loadSkillDrafts();
  const submit=f.$('#skill-generate-form').onsubmit(submitEvent(f.$('#skill-generate-form')));await flush();
  f.view.dispose();f.$('#draft-error').textContent='另一个账号';
  gate.resolve(draftListing([{draft_id:'private-draft',name:'私有草稿',revision:1,state:'editing'}]));await submit;
  assert.equal(f.$('#draft-list').children.length,0);assert.equal(f.$('#draft-error').textContent,'另一个账号');
});

test('refreshing the same skill view ignores a late generation receipt from the replaced home',async()=>{
  const gate=deferred();
  const f=skillFixture({onAction:action=>action==='generate'?gate.promise:undefined});
  await f.forms.loadSkillDrafts();
  const submit=f.$('#skill-generate-form').onsubmit(submitEvent(f.$('#skill-generate-form')));await flush();
  await f.forms.loadSkillDrafts();f.$('#draft-error').textContent='刷新后的新提示';f.$('#skill-background-jobs').open=false;
  f.$('#skill-generate-material').value='刷新后新输入';
  gate.resolve({job_id:'old-generation'});await submit;
  assert.equal(f.$('#draft-error').textContent,'刷新后的新提示');assert.equal(f.$('#skill-background-jobs').open,false);
  assert.equal(f.$('#skill-generate-material').value,'刷新后新输入');
  assert.equal(f.calls.filter(c=>c.action==='jobs').length,0,'late receipt does not start polling for the replaced home');
  f.view.dispose();
});

test('another jobs refresh neither cancels nor duplicates a slow completed-draft list read',async()=>{
  const gate=deferred(),jobs=[{job_id:'generation',kind:'generation',state:'succeeded',result:{draft_id:'new'}}];let reads=0;
  const f=skillFixture({jobs,onAction:action=>action==='generate'?{job_id:'generation'}:undefined,
    onRequest:(path,options)=>path==='/api/skill-drafts'&&!options.body?(++reads===1?draftListing([],jobs):gate.promise):undefined});
  await f.forms.loadSkillDrafts();
  const submit=f.$('#skill-generate-form').onsubmit(submitEvent(f.$('#skill-generate-form')));await flush();
  await f.forms.refreshSkillJobs();assert.equal(reads,2);
  gate.resolve(draftListing([{draft_id:'new',name:'慢查询后的草稿',revision:1,state:'editing'}]));await submit;
  assert.match(f.$('#draft-list').children[0].textContent,/慢查询后的草稿/);assert.match(f.$('#draft-error').textContent,/草稿已生成/);
  assert.equal(reads,2);f.view.dispose();
});

test('evaluation completion updates its local report and exact task entry while preserving later inputs',async()=>{
  const jobs=[];let sequence=0;
  const f=skillFixture({jobs,onAction:action=>action==='evaluate'?{job_id:`evaluation-${++sequence}`}:undefined});
  await f.forms.showSkillDraft('draft');
  const row=f.$('#skill-eval-cases').children[0];row.querySelector('[name=prompt]').value='整理计划';row.querySelector('[name=contains]').value='计划';
  const submit=()=>f.$('#skill-eval-form').onsubmit(submitEvent(f.$('#skill-eval-form')));
  await submit();await submit();
  row.querySelector('[name=prompt]').value='之后编辑的待测题';f.$('#skill-eval-form').oninput();
  f.editor.controls.get('instructions').value='之后编辑的方法';f.editor.oninput();
  jobs.push({job_id:'evaluation-1',kind:'evaluation',state:'succeeded',result:reviewReport('evaluation-1')});
  await f.forms.refreshSkillJobs();assert.match(f.$('#skill-eval-state').textContent,/已排队/);
  jobs.push({job_id:'evaluation-2',kind:'evaluation',state:'running'});
  await f.forms.refreshSkillJobs();assert.match(f.$('#skill-eval-state').textContent,/正在评测修订 3/);
  jobs[1]={...jobs[1],state:'succeeded',result:reviewReport('evaluation-2')};
  await f.forms.refreshSkillJobs();
  assert.match(f.$('#skill-eval-state').textContent,/已完成.*不代表通过/);
  assert.equal(f.$('#skill-eval-open-job').textContent,'查看本次报告');
  assert.match(f.$('#skill-evaluation-report').innerHTML,/evaluation-2/);assert.doesNotMatch(f.$('#skill-evaluation-report').innerHTML,/data-review-row|尚无独立评测/);
  assert.equal(row.querySelector('[name=prompt]').value,'之后编辑的待测题');assert.equal(f.editor.controls.get('instructions').value,'之后编辑的方法');
  assert.equal(f.forms.canAutoReloadSkillEditor(),false);
  const older=new SkillElement(),current=new SkillElement();older.dataset.backgroundJob='evaluation-1';current.dataset.backgroundJob='evaluation-2';
  f.$('#skill-jobs').queries.set('[data-background-job]',[older,current]);f.$('#skill-background-jobs').open=false;
  f.$('#skill-eval-open-job').onclick();assert.equal(f.$('#skill-background-jobs').open,true);
  assert.equal(current.querySelector('[data-report-job]').open,true);assert.notEqual(older.querySelector('[data-report-job]').open,true);
  f.view.dispose();
});

test('evaluation terminal notices preserve existing human review and stop following a departed draft',async()=>{
  const report=reviewReport('previous'),jobs=[];
  const f=skillFixture({report,jobs,onAction:action=>action==='evaluate'?{job_id:'new-evaluation'}:undefined});
  const review=attachReviewForm(f);await f.forms.showSkillDraft('draft');
  const row=f.$('#skill-eval-cases').children[0];row.querySelector('[name=prompt]').value='整理计划';row.querySelector('[name=contains]').value='计划';
  await f.$('#skill-eval-form').onsubmit(submitEvent(f.$('#skill-eval-form')));
  review.querySelector('[name=review_note]').value='尚未保存的人工复核';review.oninput();
  f.$('#skill-evaluation-report').innerHTML='保留原报告和人工复核表单';
  jobs.push({job_id:'new-evaluation',kind:'evaluation',state:'succeeded',result:reviewReport('new-evaluation')});
  await f.forms.refreshSkillJobs();assert.equal(f.$('#skill-evaluation-report').innerHTML,'保留原报告和人工复核表单');
  assert.equal(review.querySelector('[name=review_note]').value,'尚未保存的人工复核');assert.equal(f.forms.canAutoReloadSkillEditor(),false);
  for(const [state,text] of [['failed',/评测失败.*模型超时/],['canceled',/本次评测已取消/]]){
    jobs[0]={...jobs[0],state,error:'模型超时'};await f.forms.refreshSkillJobs();assert.match(f.$('#skill-eval-state').textContent,text);
  }
  // Simulate leaving this draft while staying in the same view; stale progress
  // must not overwrite another draft's evaluation status.
  f.view.replace('skill-draft-detail').dispose();f.$('#skill-eval-state').textContent='另一个草稿';
  jobs[0].state='succeeded';await f.forms.refreshSkillJobs();assert.equal(f.$('#skill-eval-state').textContent,'另一个草稿');
  f.view.dispose();
});

test("publication covers only current reported samples or a matching passing output evaluation", () => {
  const d = editableDraft();
  d.tests.push({revision: 2, profile: "fill", status: "reported"}, {revision: 3, profile: "fill", status: "running"});
  const report = {passed: true, revision: 3, content_hash: "hash-3", cases: [{kind: "trigger", profile: "fill"}]};
  assert.deepEqual(skillPublicationCoverage(d, report).map(c => c.eligible), [true, false]);
  report.cases.push({kind: "output", profile: "fill"});
  assert.equal(skillPublicationCoverage(d, report)[1].eligible, true);
  assert.match(skillPublicationCoverage(d, report)[0].evidence, /样例.*待人工复核/);
  for (const patch of [{revision: 2}, {content_hash: "wrong"}, {passed: false}]) {
    assert.equal(skillPublicationCoverage(d, {...report, ...patch})[1].eligible, false);
  }
});

test("publication uses complete current-revision coverage instead of the truncated sample display", () => {
  const d = editableDraft();
  d.tests = Array.from({length: 30}, () => ({revision: 3, profile: "fill", status: "reported"}));
  d.tested_profiles = ["preview", "fill"];
  assert.deepEqual(skillPublicationCoverage(d, null).map(c => c.eligible), [true, true]);
  d.tested_profiles = [];
  assert.deepEqual(skillPublicationCoverage(d, null).map(c => c.eligible), [false, false]);
});

test("server publication evidence separates modes and cannot be overridden by reported samples", () => {
  const d = {...editableDraft(), tested_profiles: ["preview", "fill"]};
  const evidence = {schema: "agentbridge.skill-publication-evidence.v1", revision: 3, content_hash: "hash-3",
    shared: {blocked: false}, profiles: [
      {profile: "preview", eligible: true, blocked: false, evidence: "预览输出检查通过"},
      {profile: "fill", eligible: false, blocked: true, evidence: "填单输出检查失败"},
    ]};
  assert.deepEqual(skillPublicationCoverage(d, {passed: false}, evidence).map(c => c.eligible), [true, false]);
  assert.match(skillPublicationCoverage(d, null, evidence)[1].evidence, /失败/);
  for (const patch of [{revision: 2}, {content_hash: "old"}, {shared: {blocked: true}}]) {
    assert.deepEqual(skillPublicationCoverage(d, {passed: true}, {...evidence, ...patch}).map(c => c.eligible), [false, false]);
  }
});

test("mixed-mode evaluation presents and submits only the server-approved mode", async () => {
  const evidence = {schema: "agentbridge.skill-publication-evidence.v1", revision: 3, content_hash: "hash-3",
    shared: {blocked: false, reason: "共用要求通过"}, profiles: [
      {profile: "preview", eligible: true, blocked: false, evidence: "本模式输出断言通过"},
      {profile: "fill", eligible: false, blocked: true, evidence: "本模式输出断言未通过"},
    ]};
  const f = skillFixture({onAction: action => action === "inspect" ? {...quality("方法检查"), publication_evidence: evidence} : undefined});
  await f.forms.showSkillDraft("draft");
  assert.match(f.$("#skill-publication-profiles").innerHTML, /共用要求通过/);
  assert.match(f.$("#skill-publication-profiles").innerHTML, /value="preview" checked/);
  assert.match(f.$("#skill-publication-profiles").innerHTML, /value="fill" disabled/);
  f.$("#draft-submit").formValues.profiles = ["fill"];
  await f.$("#draft-submit").onsubmit(submitEvent(f.$("#draft-submit")));
  assert.equal(f.calls.some(c => c.action === "submit"), false);
  f.$("#draft-submit").formValues.profiles = ["preview"];
  await f.$("#draft-submit").onsubmit(submitEvent(f.$("#draft-submit")));
  assert.deepEqual(f.calls.find(c => c.action === "submit").data.profiles, ["preview"]);
  f.view.dispose();
});

test("publication sends the selected eligible subset and rejects untested modes in a forged form", async () => {
  const f = skillFixture();
  await f.forms.showSkillDraft("draft");
  assert.match(f.$("#skill-publication-profiles").innerHTML, /本修订已有样例，待人工复核/);
  assert.match(f.$("#skill-publication-profiles").innerHTML, /value="fill" disabled/);
  f.$("#draft-submit").formValues.profiles = ["fill"];
  await f.$("#draft-submit").onsubmit(submitEvent(f.$("#draft-submit")));
  assert.equal(f.calls.some(c => c.action === "submit"), false);
  f.$("#draft-submit").formValues.profiles = ["preview"];
  await f.$("#draft-submit").onsubmit(submitEvent(f.$("#draft-submit")));
  const submitted = f.calls.find(c => c.action === "submit").data;
  assert.deepEqual(submitted.profiles, ["preview"]);
  assert.equal(submitted.expected_revision, 3);
  f.view.dispose();
});

test("archived drafts keep export enabled while editing and trial remain disabled", async () => {
  const f = skillFixture({initial: {...editableDraft(), state: "archived"}});
  await f.forms.showSkillDraft("draft");
  assert.equal(f.$("#draft-editor-fields").disabled, false);
  assert.equal(f.$("#draft-export").disabled, false);
  assert.equal(f.editor.controls.get("instructions").disabled, true);
  await f.$("#draft-test").onclick();
  assert.deepEqual(f.navigations, []);
  f.view.dispose();
});

test("previous evaluation imports editable inputs and assertions without inheriting a pass or starting a run", async () => {
  const previous = {revision: 2, repeats: 3, cases: [
    {prompt: "整理计划", profile: "preview", kind: "output", contains: ["计划"], excludes: ["完成"]},
    {prompt: "查天气", profile: "preview", kind: "trigger", expected_trigger: false, contains: [], excludes: []},
  ]};
  const f = skillFixture({onAction: action => action === "inspect" ? {...quality("检查"), previous_report: previous} : undefined});
  await f.forms.showSkillDraft("draft");
  f.$("#skill-reuse-cases").onclick();
  const cases = f.$("#skill-eval-cases").children;
  assert.equal(cases.length, 2);
  assert.match(cases[0].querySelector('[data-case-source]').textContent, /修订 2.*不继承通过/);
  assert.equal(cases[0].querySelector('[name=contains]').value, "计划");
  assert.equal(cases[1].querySelector('[name=kind]').value, "trigger_false");
  assert.match(f.$("#skill-eval-state").textContent, /尚未运行或保存/);
  assert.equal(f.calls.some(c => c.action === "evaluate"), false);
  cases[0].querySelector('[name=prompt]').value = "编辑后的材料";
  await f.$("#skill-eval-form").onsubmit(submitEvent(f.$("#skill-eval-form")));
  const sent = f.calls.find(c => c.action === "evaluate").data;
  assert.equal(sent.expected_revision, 3);
  assert.equal(sent.repeats, 3);
  assert.equal(sent.cases[0].prompt, "编辑后的材料");
  assert.deepEqual(sent.cases[0].contains, ["计划"]);
  assert.equal(sent.cases[1].expected_trigger, false);
  assert.equal(Object.hasOwn(sent, "passed"), false);
  f.view.dispose();
});

test("sample conversion forces a removed mode to be selected and leaves assertions for a human", async () => {
  const initial = editableDraft();
  initial.tests = [{test_id: "sample", revision: 1, profile: "retired", status: "reported", prompt: "计划修复", output: "已完成"}];
  const f = skillFixture({initial});
  const button = new SkillElement(); button.dataset.sampleCase = "sample";
  f.$("#skill-sample-list").queries.set('[data-sample-case]', [button]);
  await f.forms.showSkillDraft("draft");
  button.onclick();
  assert.match(f.$("#draft-detail").innerHTML, /已保存，待复核/);
  const row = f.$("#skill-eval-cases").children[0];
  assert.equal(row.querySelector('[name=prompt]').value, "计划修复");
  assert.equal(row.querySelector('[name=profile]').value, "");
  assert.equal(row.querySelector('[name=contains]').value, "");
  assert.equal(row.querySelector('[name=excludes]').value, "");
  assert.match(row.querySelector('[data-case-source]').textContent, /retired.*请明确选择/);
  await f.$("#skill-eval-form").onsubmit(submitEvent(f.$("#skill-eval-form")));
  assert.match(f.$("#skill-eval-state").textContent, /明确选择/);
  row.querySelector('[name=profile]').value = "preview";
  await f.$("#skill-eval-form").onsubmit(submitEvent(f.$("#skill-eval-form")));
  assert.match(f.$("#skill-eval-state").textContent, /填写至少一项/);
  assert.equal(f.calls.some(c => c.action === "evaluate"), false);
  row.querySelector('[name=contains]').value = "计划";
  row.querySelector('[name=excludes]').value = "已完成";
  await f.$("#skill-eval-form").onsubmit(submitEvent(f.$("#skill-eval-form")));
  assert.deepEqual(f.calls.find(c => c.action === "evaluate").data.cases[0],
    {prompt: "计划修复", profile: "preview", kind: "output", contains: ["计划"], excludes: ["已完成"]});
  f.view.dispose();
});

test('evaluation validation, request failure and queue success stay beside evaluation controls',async()=>{
  let fail=true;
  const f=skillFixture({onAction:action=>{if(action==='evaluate'&&fail)throw Error('草稿已更新，请刷新后再操作');}});
  await f.forms.showSkillDraft('draft');
  const notice=f.$('#skill-eval-state'),scrolls=[],roles=[];
  notice.scrollIntoView=options=>scrolls.push(options);notice.setAttribute=(key,value)=>{if(key==='role')roles.push(value);};
  f.$('#draft-detail-error').textContent='编辑器提示不应覆盖';
  const source=f.$('#skill-quality-panel').innerHTML;
  assert.match(source,/id="skill-eval-submit"[\s\S]*id="skill-eval-state"[\s\S]*<\/form>/);
  const row=f.$('#skill-eval-cases').children[0];
  row.querySelector('[name=prompt]').value='整理明天的计划';
  row.querySelector('[name=contains]').value='';
  await f.$('#skill-eval-form').onsubmit(submitEvent(f.$('#skill-eval-form')));
  assert.match(notice.textContent,/填写至少一项/);assert.equal(f.calls.some(call=>call.action==='evaluate'),false);
  row.querySelector('[name=contains]').value='计划';
  await f.$('#skill-eval-form').onsubmit(submitEvent(f.$('#skill-eval-form')));
  assert.match(notice.textContent,/草稿已更新/);assert.equal(row.querySelector('[name=prompt]').value,'整理明天的计划');
  fail=false;
  await f.$('#skill-eval-form').onsubmit(submitEvent(f.$('#skill-eval-form')));
  assert.match(notice.textContent,/修订 3 的评测已排队/);assert.doesNotMatch(notice.textContent,/草稿已更新/);
  assert.deepEqual(roles,['alert','alert','status']);
  assert.deepEqual(scrolls,Array(3).fill({block:'nearest',behavior:'smooth'}));
  assert.equal(f.$('#draft-detail-error').textContent,'编辑器提示不应覆盖');f.view.dispose();
});

test('queued evaluation remains visibly queued if the subsequent jobs refresh fails',async()=>{
  const f=skillFixture({onAction:action=>{if(action==='jobs')throw Error('列表暂时不可用');}});
  await f.forms.showSkillDraft('draft');
  const row=f.$('#skill-eval-cases').children[0];row.querySelector('[name=prompt]').value='整理计划';row.querySelector('[name=contains]').value='计划';
  await f.$('#skill-eval-form').onsubmit(submitEvent(f.$('#skill-eval-form')));
  assert.match(f.$('#skill-eval-state').textContent,/评测已排队；任务列表暂未刷新/);
  assert.equal(f.calls.filter(call=>call.action==='evaluate').length,1);f.view.dispose();
});

test('standard export shows and replaces feedback next to its own button',async()=>{
  let fail=true;
  const f=skillFixture({onAction:action=>{if(action==='export_standard'){if(fail)throw Error('文件包暂时无法导出');return {files:[]};}}});
  await f.forms.showSkillDraft('draft');
  const source=f.$('#skill-quality-panel').innerHTML,notice=f.$('#skill-standard-export-state'),scrolls=[];
  assert.match(source,/id="skill-standard-export"[^>]*>[^<]*<\/button><p id="skill-standard-export-state" role="status"/);
  notice.scrollIntoView=options=>scrolls.push(options);
  f.$('#skill-eval-state').textContent='保留评测状态';
  await f.$('#skill-standard-export').onclick();assert.match(notice.textContent,/无法导出/);
  fail=false;await f.$('#skill-standard-export').onclick();
  assert.equal(notice.textContent,'文件包已开始下载。');assert.equal(scrolls.length,2);
  assert.equal(f.$('#skill-eval-state').textContent,'保留评测状态');assert.equal(f.$('#draft-detail-error').textContent,'');f.view.dispose();
});

test("sample feedback carries original test attribution and can become an editable case without turning correction into assertions", async () => {
  const initial = editableDraft();
  initial.tests[0].test_id = "sample";
  const gate = deferred(), comment = "把计划误写成已完成。";
  const f = skillFixture({initial, onAction: action => action === "feedback" ? gate.promise : undefined});
  const form = new SkillElement(); form.dataset.sampleFeedback = "sample";
  form.querySelector('[name=rating]').value = "incorrect";
  form.querySelector('[name=comment]').value = comment;
  f.$("#skill-sample-list").queries.set('[data-sample-feedback]', [form]);
  await f.forms.showSkillDraft("draft");
  const saving = form.onsubmit(submitEvent(form));
  await form.onsubmit(submitEvent(form));
  const calls = f.calls.filter(c => c.action === "feedback");
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].data, {skill_id: "user.skill", profile: "preview", rating: "incorrect", comment, test_id: "sample", version: "draft:3"});
  gate.resolve({feedback_id: "feedback", version: "draft:3", profile: "preview", source: {kind: "sample", revision: 3, profile: "preview", prompt: "归纳计划"}});
  await saving;
  assert.match(form.querySelector('[role=status]').textContent, /反馈已保存/);
  const feedback = f.$("#skill-feedback-list").children[0];
  assert.match(feedback.children[0].textContent, /修订 3.*草稿预览/);
  assert.doesNotMatch(feedback.children[0].textContent, /draft:/);
  feedback.children.at(-1).onclick();
  const row = f.$("#skill-eval-cases").children[0];
  assert.equal(row.querySelector('[name=prompt]').value, "归纳计划");
  assert.match(row.querySelector('[data-case-source]').textContent, /把计划误写成已完成/);
  assert.match(row.querySelector('[data-case-source]').textContent, /纠错说明：把计划误写成已完成。 请填写脱敏问题及“应包含\/不应包含”；不自动复制业务原文，反馈不会自动成为验收条件。/);
  assert.doesNotMatch(row.querySelector('[data-case-source]').textContent, /。。|断言/);
  assert.equal(row.querySelector('[name=contains]').value, "");
  assert.equal(row.querySelector('[name=excludes]').value, "");
  assert.equal(f.calls.some(c => c.action === "evaluate"), false);
  f.view.dispose();
});

test("legacy feedback labels unknown modes and stale feedback responses cannot paint another view", async () => {
  const gate = deferred(), initial = editableDraft(); initial.tests[0].test_id = "sample";
  const f = skillFixture({initial, onAction: action => action === "inspect" ? {...quality("检查"), feedback: {items: [{version: "1.0.0", profile: "unknown", rating: "incorrect", comment: "旧反馈", source: {}}]}}
    : action === "feedback" ? gate.promise : undefined});
  const form = new SkillElement(); form.dataset.sampleFeedback = "sample";
  form.querySelector('[name=rating]').value = "useful"; form.querySelector('[name=comment]').value = "已区分计划";
  f.$("#skill-sample-list").queries.set('[data-sample-feedback]', [form]);
  await f.forms.showSkillDraft("draft");
  assert.match(f.$("#skill-feedback-list").children[0].children[0].textContent, /模式未记录/);
  assert.equal(f.$("#skill-feedback-list").children[0].children.length, 2);
  const saving = form.onsubmit(submitEvent(form));
  f.view.dispose();
  form.querySelector('[role=status]').textContent = "新账号";
  gate.resolve({feedback_id: "old", profile: "preview", version: "draft:3", source: {kind: "sample"}});
  await saving;
  assert.equal(form.querySelector('[role=status]').textContent, "新账号");
});

test("catalog feedback requires a selected eligible mode instead of silently using the first", async () => {
  const f = skillFixture({onAction: action => action === "feedback" ? {status: "recorded"} : undefined});
  const card = new SkillElement();
  f.forms.addSkillFeedback(card, {id: "published", version: "1.0.0", profiles: {fill: {available: false}, preview: {available: true}}});
  const details = card.children[0];
  details.querySelector('[name=rating]').value = "useful";
  details.querySelector('textarea').value = "保留了计划";
  await details.querySelector('button').onclick();
  details.querySelector('[name=profile]').value = "fill";
  await details.querySelector('button').onclick();
  assert.equal(f.calls.some(c => c.action === "feedback"), false);
  details.querySelector('[name=profile]').value = "preview";
  await details.querySelector('button').onclick();
  assert.equal(f.calls.find(c => c.action === "feedback").data.profile, "preview");
  f.view.dispose();
});

const replacementJob = () => ({job_id: "job", kind: "generation", state: "succeeded", result: {similar: [{draft_id: "draft", name: "日志助手"}],
  candidate: {name: "新方法", description: "新说明", instructions: "<img onerror=bad>新步骤", selection: {use_when: "新目标", not_for: "新排除", output: "新输出"},
    profiles: {use: {}}, executionMode: "read_exploration", references: {}, required_resources: {use: []}}}});

function mountCandidateJobs(root) {
  // Emulate innerHTML replacing descendants: every render creates closed details
  // and fresh buttons, so a stale node cannot make preservation appear to work.
  Object.defineProperty(root, "innerHTML", {set(html) {
    const details = [...html.matchAll(/<details data-candidate-job="([^"]+)"/g)].map(match => {
      const detail = new SkillElement();
      detail.dataset = {candidateJob: match[1]}; detail.open = false;
      const button = new SkillElement();
      button.dataset = {previewAdopt: match[1], adoptTarget: "draft"};
      detail.previewButton = button;
      detail.saveButton = new SkillElement();
      detail.saveButton.dataset = {adoptJob: match[1]};
      return detail;
    });
    root.queries.set("[data-candidate-job]", details);
    root.queries.set("[data-preview-adopt]", details.map(d => d.previewButton));
    root.queries.set("[data-adopt-job]", details.map(d => d.saveButton));
  }});
  return () => root.querySelectorAll("[data-candidate-job]");
}

test("candidate adoption confirms dirty edits before mutation and refuses to discard late input", async () => {
  const gate = deferred();
  let discard = false;
  const f = skillFixture({jobs: [replacementJob()], confirm: () => discard, onAction: action => action === "adopt" ? gate.promise : undefined});
  const candidates = mountCandidateJobs(f.$("#skill-jobs"));
  await f.forms.refreshSkillJobs();
  await f.forms.showSkillDraft("draft");
  f.editor.controls.get("instructions").value = "当前未保存的方法"; f.editor.oninput();
  await candidates()[0].saveButton.onclick();
  assert.equal(f.calls.some(c => c.action === "adopt"), false);
  discard = true;
  const adopting = candidates()[0].saveButton.onclick();
  await flush();
  assert.equal(f.$("#draft-editor-fields").disabled, true);
  assert.equal(await f.forms.canLeaveSkillEditor(), false);
  f.editor.controls.get("instructions").value = "采用期间又输入的内容"; f.editor.oninput();
  gate.resolve({draft_id: "draft"});
  await adopting;
  assert.equal(f.calls.filter(c => c.path === "/api/skill-drafts?id=draft").length, 1);
  assert.equal(f.editor.controls.get("instructions").value, "采用期间又输入的内容");
  assert.match(f.$("#draft-detail-error").textContent, /内容又有变化/);
  assert.equal(f.$("#draft-editor-fields").disabled, false);
  assert.equal(f.windowEvents.size, 1);
  f.view.dispose();
});

test("candidate comparison ignores only dependency defaults and order, preserving database presence and actual removals", () => {
  const bundle = editableDraft().bundle;
  const candidate = {...structuredClone(bundle.manifest), instructions: bundle.resources["SKILL.md"], references: {"references/evidence.md": "核对来源。"}};
  candidate.profiles.preview = {all: [], any: []};
  bundle.manifest.profiles.fill = {all: ["b", "a"], database: {any: ["y", "x"]}};
  candidate.profiles.fill = {all: ["a", "b"], any: [], database: {all: [], any: ["x", "y"]}};
  assert.deepEqual(skillCandidateDiff(bundle, candidate), []);
  candidate.profiles.preview.database = {};
  assert.deepEqual(skillCandidateDiff(bundle, candidate).map(c => c.label), ["模式与能力依赖 · preview"]);
  delete candidate.profiles.preview.database;
  delete candidate.profiles.fill.database;
  candidate.profiles.fill.all = ["a"];
  delete candidate.references["references/evidence.md"];
  candidate.required_resources.preview = [];
  const changes = skillCandidateDiff(bundle, candidate);
  assert.deepEqual(skillCandidateRemovals(changes), ["fill 必需能力：b", "fill 数据源要求", "参考资料：references/evidence.md", "preview 必读资料：references/evidence.md"]);
  assert.deepEqual(changes.find(c => c.label === "模式与能力依赖 · fill").before, bundle.manifest.profiles.fill);
});

test("background polling preserves candidate expansion by job ID and keeps its preview action usable", async () => {
  const pending = new Map();
  let next = 0;
  const timers = {setTimeout(callback) { pending.set(++next, callback); return next; }, clearTimeout(id) { pending.delete(id); }};
  const jobs = [replacementJob(), {...replacementJob(), job_id: "second"}, {job_id: "evaluation", kind: "evaluation", state: "running", progress: 1}];
  const f = skillFixture({jobs, timers});
  const candidates = mountCandidateJobs(f.$("#skill-jobs"));
  const poll = async () => {
    assert.equal(pending.size, 1);
    const [id, callback] = pending.entries().next().value;
    pending.delete(id); callback(); await flush();
  };
  await f.forms.refreshSkillJobs();
  const opened = candidates()[0];
  opened.open = true;
  // Newer jobs can move existing entries, so preserving by array position is wrong.
  jobs.unshift({...replacementJob(), job_id: "new"});
  await poll();
  assert.notEqual(candidates()[1], opened);
  assert.deepEqual(candidates().map(d => [d.dataset.candidateJob, d.open]), [["new", false], ["job", true], ["second", false]]);
  await candidates()[1].previewButton.onclick();
  assert.equal(f.calls.find(c => c.action === "get").data.draft_id, "draft");
  assert.match(f.$("#skill-candidate-preview").innerHTML, /目标：修订 3/);
  candidates()[1].open = false;
  candidates()[2].open = true;
  await poll();
  assert.deepEqual(candidates().map(d => d.open), [false, false, true]);
  f.view.dispose();
  assert.equal(pending.size, 0);
});

for (const source of ["job list", "replacement preview"]) test(`a pending adoption from ${source} stays locked across polling and the other entry point`, async () => {
  const scheduled = new Map();
  let next = 0, fail = true;
  const timers = {setTimeout(callback) { scheduled.set(++next, callback); return next; }, clearTimeout(id) { scheduled.delete(id); }};
  const gate = deferred();
  const jobs = [replacementJob(), {job_id: "evaluation", kind: "evaluation", state: "running", progress: 1}];
  const f = skillFixture({jobs, timers, onAction: async action => {
    if (action !== "adopt") return;
    await gate.promise;
    if (fail) throw Error("暂时无法保存");
    return {draft_id: "draft"};
  }});
  const candidates = mountCandidateJobs(f.$("#skill-jobs"));
  await f.forms.refreshSkillJobs();
  await candidates()[0].previewButton.onclick();
  const original = candidates()[0].saveButton;
  const saving = (source === "job list" ? original : f.$("#candidate-save-new")).onclick();
  assert.equal(original.disabled, true);
  assert.equal(f.$("#candidate-save-new").disabled, true);
  const [id, callback] = scheduled.entries().next().value;
  scheduled.delete(id); callback(); await flush();
  const replacement = candidates()[0].saveButton;
  assert.notEqual(replacement, original);
  assert.equal(replacement.disabled, true);
  await replacement.onclick();
  await f.$("#candidate-save-new").onclick();
  await f.$("#candidate-replace").onclick();
  assert.equal(f.calls.filter(c => c.action === "adopt").length, 1);
  gate.resolve(); await saving;
  assert.equal(replacement.disabled, false);
  assert.equal(f.$("#candidate-save-new").disabled, false);
  fail = false;
  await replacement.onclick();
  assert.equal(f.calls.filter(c => c.action === "adopt").length, 2);
  f.view.dispose();
  assert.equal(scheduled.size, 0);
});

test("replacement comparison includes method, scope, execution, removed modes and removed references", () => {
  const changes = skillCandidateDiff(editableDraft().bundle, replacementJob().result.candidate);
  for (const label of ["处理方法", "适用范围与输出", "执行方式", "模式与能力依赖 · fill", "参考资料 · references/evidence.md", "模式必读资料 · preview"])
    assert.ok(changes.some(c => c.label === label), label);
  assert.equal(changes.find(c => c.label === "参考资料 · references/evidence.md").kind, "移除");
  const withMethod = skillCandidateDiff(editableDraft().bundle, {...replacementJob().result.candidate, method: {inputs: ["输入文字"], steps: ["核对"], exceptions: [], acceptance: []}});
  assert.match(withMethod.find(c => c.label === "处理方法").after, /## 输入\n1\. 输入文字.*\n\n## 步骤\n1\. 核对/s);
});

test("replacement confirms the previewed revision without silently re-reading a newer target", async () => {
  const job = replacementJob();
  const f = skillFixture({jobs: [job]});
  const previewButton = new SkillElement(); previewButton.dataset = {previewAdopt: "job", adoptTarget: "draft"};
  f.$("#skill-jobs").queries.set("[data-preview-adopt]", [previewButton]);
  await f.forms.refreshSkillJobs();
  await previewButton.onclick();
  assert.match(f.$("#skill-candidate-preview").innerHTML, /目标：修订 3/);
  assert.match(f.$("#skill-candidate-preview").innerHTML, /移除 · 参考资料 · references\/evidence.md/);
  assert.ok(!f.$("#skill-candidate-preview").innerHTML.includes("<img onerror=bad>"));
  f.drafts.get("draft").revision = 4;
  await f.$("#candidate-replace").onclick();
  assert.equal(f.calls.filter(c => c.action === "get").length, 1);
  assert.equal(f.calls.find(c => c.action === "adopt").data.expected_revision, 3);
  assert.match(f.$("#candidate-replace-status").textContent, /草稿修订已变化/);
  assert.equal(f.calls.filter(c => c.action === "adopt").length, 1);
  await f.$("#candidate-refresh").onclick();
  assert.match(f.$("#skill-candidate-preview").innerHTML, /目标：修订 4/);
  f.view.dispose();
});

test("leaving a replacement preview cancels its late target response and prevents mutation", async () => {
  const gate = deferred(), job = replacementJob();
  const f = skillFixture({jobs: [job], onAction: action => action === "get" ? gate.promise : undefined});
  const button = new SkillElement(); button.dataset = {previewAdopt: "job", adoptTarget: "draft"};
  f.$("#skill-jobs").queries.set("[data-preview-adopt]", [button]);
  await f.forms.refreshSkillJobs();
  const reading = button.onclick();
  f.view.dispose();
  gate.resolve(editableDraft());
  await reading;
  assert.doesNotMatch(f.$("#skill-candidate-preview").innerHTML, /确认替换/);
  assert.equal(f.calls.some(c => c.action === "adopt"), false);
});

test('remaining query handler keeps request identity and failed denominator without projecting malformed receipts',async()=>{
 const scope=createLifecycle().scope(),marks=[],projected=[],synced=[],calls=[];let receipt={taskId:'invalid'};
 const query=workspaceHandler('queryOaBatchRemaining','prepareOaPendingRecovery',{
  sessionScope:scope,projectionOrder:{begin:()=>4,accept:()=>true},scopedApi:()=>async(url,options)=>{calls.push([url,options]);return receipt;},
  markWorkspaceTiming:(stage,data)=>marks.push([stage,data]),upsertTaskCard:value=>projected.push(value),scheduleTaskSync:id=>synced.push(id),
  isCancellation:()=>false,
 });
 const taskId='11111111-1111-1111-1111-111111111111',payload={requestKey:'same-remaining-query',expectedBatchVersion:7};
 await assert.rejects(query(taskId,payload),/恢复同一次查询/);assert.deepEqual(synced,[]);
 assert.equal(marks.at(-1)[0],'quick_query_failed');
 receipt={taskId:'22222222-2222-2222-2222-222222222222',detail:{task:{task_id:'22222222-2222-2222-2222-222222222222'}}};
 await query(taskId,payload);assert.equal(synced.length,1);assert.equal(projected.length,1);
 assert.equal(marks.at(-1)[0],'quick_query_queued');assert.equal(calls[0][1].body,payload);assert.equal(calls[1][1].body,payload);
 assert.match(calls[0][0],/oa-batch-remaining\/query$/);assert.equal(calls[0][1].csrf,true);
 scope.dispose();
});

test('pending evaluation inputs survive method save and remain protected until actually queued', async () => {
  const f = skillFixture(); await f.forms.showSkillDraft('draft');
  const row = f.$('#skill-eval-cases').children[0];
  row.querySelector('[name=prompt]').value = '整理成周报：完成联调，下周补测试。';
  row.querySelector('[name=contains]').value = '联调\n下周';
  f.$('#skill-eval-form').querySelector('[name=repeats]').value = '2';
  f.$('#skill-eval-form').oninput();
  assert.equal(await f.forms.canLeaveSkillEditor(),false);
  assert.ok(f.windowEvents.has('beforeunload'));
  f.editor.controls.get('instructions').value += ' 不添加原文外推断。';
  await f.editor.onsubmit(submitEvent(f.editor));
  const restored = f.$('#skill-eval-cases').children;
  assert.equal(restored.length,1);
  assert.equal(restored[0].querySelector('[name=prompt]').value,'整理成周报：完成联调，下周补测试。');
  assert.equal(restored[0].querySelector('[name=contains]').value,'联调\n下周');
  assert.equal(f.$('#skill-eval-form').querySelector('[name=repeats]').value,'2');
  assert.equal(await f.forms.canLeaveSkillEditor(),false);
  await f.$('#skill-eval-form').onsubmit(submitEvent(f.$('#skill-eval-form')));
  assert.equal(f.calls.filter(c=>c.action==='evaluate').length,1);
  assert.equal(await f.forms.canLeaveSkillEditor(),true);
  f.view.dispose();
});

test('completed evaluation task exposes its own frozen report and baseline, not current draft', async () => {
  const report={draft_id:'draft',revision:2,name:'周报助手',job_id:'old-job',passed:false,baseline:{version:'1.0.0'},
    scores:{candidate:{passed:0,total:1},published:{passed:1,total:1}},cases:[{profile:'use'}],
    rows:[{variant:'candidate',case:0,prompt:'整理成周报',output:'原文外推断',passed:false,checks:[{rule:'excludes',value:'推断',passed:false}]}]};
  const f=skillFixture({jobs:[{job_id:'old-job',kind:'evaluation',state:'succeeded',revision:2,name:'周报助手',progress:2,result:report}]});
  await f.forms.refreshSkillJobs();
  const html=f.$('#skill-jobs').innerHTML;
  assert.match(html,/查看本次报告/);assert.match(html,/修订 2/);assert.match(html,/评测时线上 1.0.0/);assert.match(html,/不应包含：推断/);
  f.view.dispose();
});


test('fixed regression pinning retains pending inputs and imports original conditions without a pass', async () => {
  const requests = [], fixed = {job_id:'old-job',revision:2,cases:[{prompt:'整理计划',profile:'preview',kind:'output',contains:['计划'],excludes:['已完成']}],repeats:2,case_fingerprint:'old-conditions'};
  const report = {job_id:'new-job',revision:3,cases:fixed.cases,scores:{},rows:[],case_fingerprint:'changed-conditions'};
  const f = skillFixture({report,onAction: action => action === 'inspect' ? {...quality('检查'),evaluation:report,regression:fixed} : undefined,
    onRequest:(path,options) => {if(path === '/api/skill-regressions'){requests.push(options.body);return {...fixed,job_id:options.body.job_id};}}});
  await f.forms.showSkillDraft('draft');
  assert.match(f.$('#skill-regression-status').textContent,/分数不直接比较/);
  const input=f.$('#skill-eval-cases').children[0].querySelector('[name=prompt]');input.value='未保存的新题';
  await f.$('#skill-pin-regression').onclick();assert.equal(input.value,'未保存的新题');
  assert.deepEqual(requests,[{draft_id:'draft',expected_revision:3,job_id:'new-job'}]);
  f.$('#skill-use-regression').onclick();
  assert.equal(f.$('#skill-eval-cases').children.length,2,'existing question is retained');
  const imported=f.$('#skill-eval-cases').children[1];
  assert.equal(imported.querySelector('[name=prompt]').value,'整理计划');
  assert.equal(imported.querySelector('[name=excludes]').value,'已完成');
  assert.equal(f.$('#skill-eval-form').querySelector('[name=repeats]').value,'2');
  assert.equal(f.calls.some(c => c.action === 'evaluate'),false);
  f.view.dispose();
});

test('historical use feedback starts with a blank redacted question and retains provenance in evaluation', async () => {
  const fid='12345678-1234-1234-1234-123456789abc';
  const f=skillFixture({onAction:action => action === 'inspect' ? {...quality('检查'),feedback:{items:[{feedback_id:fid,rating:'incorrect',comment:'不要补效果',profile:'preview',version:'1.0.0',source:{kind:'binding',load_event_id:'event',profile:'preview'}}]}}:undefined});
  await f.forms.showSkillDraft('draft');
  const item=f.$('#skill-feedback-list').children[0];
  assert.match(item.children[1].textContent,/助手使用记录/);
  item.children[2].onclick();
  const row=f.$('#skill-eval-cases').children[0];
  assert.equal(row.querySelector('[name=prompt]').value,'');
  assert.equal(row.querySelector('[name=contains]').value,'');
  row.querySelector('[name=prompt]').value='整理计划';row.querySelector('[name=contains]').value='计划';
  await f.$('#skill-eval-form').onsubmit(submitEvent(f.$('#skill-eval-form')));
  assert.equal(f.calls.find(c=>c.action === 'evaluate').data.cases[0].feedback_id,fid);
  f.view.dispose();
});


test('automatic version refresh waits for unsaved evaluation inputs and pending editor work', async () => {
  const f=skillFixture();await f.forms.showSkillDraft('draft');
  assert.equal(f.forms.canAutoReloadSkillEditor(),true);
  const row=f.$('#skill-eval-cases').children[0];
  row.querySelector('[name=prompt]').value='明天补测试';
  assert.equal(f.forms.canAutoReloadSkillEditor(),false);
  row.querySelector('[name=contains]').value='明天';
  await f.$('#skill-eval-form').onsubmit(submitEvent(f.$('#skill-eval-form')));
  assert.equal(f.forms.canAutoReloadSkillEditor(),true);
  let reloads=0,allowed=false;
  const state={activeStreams:new Set(),clientVersionCheckActive:false};
  const start=workspaceSource.indexOf('async function checkClientVersion()');
  const end=workspaceSource.indexOf('\n}',start)+2;
  const check=new Function('sessionScope','scopedApi','CLIENT_VERSION','state','canAutoReloadSkillEditor','location',
    workspaceSource.slice(start,end)+';return checkClientVersion;')(f.view,()=>async()=>({version:'new'}),'old',state,()=>allowed,{reload(){reloads++;}});
  await check();assert.equal(reloads,0);assert.equal(state.clientVersionCheckActive,false);
  allowed=true;await check();assert.equal(reloads,1);
  f.view.dispose();
});

function reviewReport(jobId = 'report-job') {
  return {job_id:jobId,draft_id:'draft',revision:3,name:'周报助手',passed:true,content_hash:'hash-3',
    cases:[{prompt:'整理成周报：明天补测试。',profile:'preview',kind:'output',contains:['明天'],excludes:[]}],
    rows:[{key:'0:0:candidate',case:0,repeat:0,variant:'candidate',prompt:'整理成周报：明天补测试。',output:'明天补测试。',passed:true,model:'synthetic-review',checks:[{rule:'contains',value:'明天',passed:true}]}],
    scores:{candidate:{passed:1,total:1}},reviews:{}};
}
function attachReviewForm(f, {status='unreviewed',note='',rowKey='0:0:candidate'} = {}) {
  const form=new SkillElement();form.dataset.reviewRow=rowKey;
  form.querySelector('[name=review_status]').value=status;
  form.querySelector('[name=review_note]').value=note;
  f.$('#skill-quality-panel').queries.set('[data-review-row]',[form]);
  return form;
}

test('unsaved human review protects navigation, auto reload, method save and evaluation',async()=>{
  let prompts=0;
  const f=skillFixture({report:reviewReport(),confirm:()=>{prompts++;return false;}});
  const form=attachReviewForm(f);await f.forms.showSkillDraft('draft');
  assert.equal(f.forms.canAutoReloadSkillEditor(),true);
  form.querySelector('[name=review_status]').value='problem';
  form.querySelector('[name=review_note]').value='不要补原文没有的效果。';form.oninput();
  assert.equal(f.forms.canAutoReloadSkillEditor(),false);
  assert.equal(f.windowEvents.has('beforeunload'),true);
  assert.equal(await f.forms.canLeaveSkillEditor(),false);assert.equal(prompts,1);
  await f.editor.onsubmit(submitEvent(f.editor));
  await f.$('#skill-eval-form').onsubmit(submitEvent(f.$('#skill-eval-form')));
  assert.equal(f.calls.some(c=>c.action==='save'||c.action==='evaluate'),false);
  assert.match(f.$('#draft-detail-error').textContent,/未保存的复核说明/);
  assert.equal(form.querySelector('[name=review_note]').value,'不要补原文没有的效果。');
  f.view.dispose();
});

test('review save binds original job and row, preserves edits during request, and leaves automatic scores unchanged',async()=>{
  const report=reviewReport(),pending=deferred(),requests=[];
  const f=skillFixture({report,onRequest:(path,options)=>{
    if(path!=='/api/skill-report-reviews')return;
    requests.push(structuredClone(options.body));
    if(requests.length===1)return pending.promise;
    return {...options.body,version:2};
  }});
  const form=attachReviewForm(f);await f.forms.showSkillDraft('draft');
  form.querySelector('[name=review_status]').value='problem';form.querySelector('[name=review_note]').value='计划被补写。';form.oninput();
  const first=form.onsubmit(submitEvent(form));await flush();
  assert.deepEqual(requests,[{job_id:'report-job',row_key:'0:0:candidate',status:'problem',note:'计划被补写。',expected_version:0}]);
  form.querySelector('[name=review_note]').value='计划被补写，还需要核对日期。';form.oninput();
  await form.onsubmit(submitEvent(form));assert.equal(requests.length,1,'pending save cannot create a competing request');
  pending.resolve({row_key:'0:0:candidate',status:'problem',note:'计划被补写。',version:1});await first;
  assert.equal(f.forms.canAutoReloadSkillEditor(),false);
  assert.match(form.querySelector('[role=status]').textContent,/仍有未保存修改/);
  assert.equal(report.passed,true);assert.deepEqual(report.scores,{candidate:{passed:1,total:1}});
  await form.onsubmit(submitEvent(form));
  assert.equal(requests[1].expected_version,1);
  assert.equal(requests[1].note,'计划被补写，还需要核对日期。');
  assert.equal(f.forms.canAutoReloadSkillEditor(),true);
  assert.equal(f.windowEvents.has('beforeunload'),false);
  assert.match(f.$('#skill-quality-panel').querySelector('[data-review-summary]').textContent,/1 项有问题/);
  f.view.dispose();
});

test('failed review save keeps note and original concurrency version for explicit retry',async()=>{
  const requests=[];
  const f=skillFixture({report:reviewReport(),onRequest:(path,options)=>{
    if(path!=='/api/skill-report-reviews')return;
    requests.push({...options.body});
    if(requests.length===1)throw new Error('网络不可用，请重试。');
    return {...options.body,version:1};
  }});
  const form=attachReviewForm(f);await f.forms.showSkillDraft('draft');
  form.querySelector('[name=review_status]').value='acceptable';form.querySelector('[name=review_note]').value='保留了明天。';form.oninput();
  await form.onsubmit(submitEvent(form));
  assert.match(form.querySelector('[role=status]').textContent,/网络不可用/);
  assert.equal(form.querySelector('[name=review_note]').value,'保留了明天。');
  assert.equal(f.forms.canAutoReloadSkillEditor(),false);
  await form.onsubmit(submitEvent(form));
  assert.deepEqual(requests[1],requests[0]);assert.equal(f.forms.canAutoReloadSkillEditor(),true);
  f.view.dispose();
});

test('background job refresh cannot erase an edited review and historic reports remain read only',async()=>{
  const report=reviewReport(),pendingTimers=new Map();let next=0;
  const timers={setTimeout(cb){pendingTimers.set(++next,cb);return next;},clearTimeout(id){pendingTimers.delete(id);}};
  const f=skillFixture({report,timers,jobs:[{job_id:'old',kind:'evaluation',state:'succeeded',result:report},{job_id:'running',kind:'evaluation',state:'running',progress:1}]});
  const form=attachReviewForm(f);await f.forms.showSkillDraft('draft');
  form.querySelector('[name=review_note]').value='这条还没检查完。';form.oninput();
  await f.forms.refreshSkillJobs();
  assert.equal(pendingTimers.size,1);
  assert.match(f.$('#skill-jobs').innerHTML,/查看本次报告/);
  assert.doesNotMatch(f.$('#skill-jobs').innerHTML,/data-review-row|name="review_note"/);
  assert.equal(form.querySelector('[name=review_note]').value,'这条还没检查完。');
  assert.equal(f.forms.canAutoReloadSkillEditor(),false);
  f.view.dispose();assert.equal(pendingTimers.size,0);
});

test('fresh report starts unreviewed and does not borrow prior report judgement',async()=>{
  const saved=reviewReport('old-job');saved.reviews={'0:0:candidate':{row_key:'0:0:candidate',status:'problem',note:'旧输出补写了效果。',version:2}};
  let active=saved;
  const f=skillFixture({onAction:action=>action==='inspect'?{...quality('检查'),evaluation:active}:undefined});
  let form=attachReviewForm(f,{status:'problem',note:'旧输出补写了效果。'});await f.forms.showSkillDraft('draft');
  assert.match(f.$('#skill-quality-panel').innerHTML,/1 项有问题/);
  active=reviewReport('new-job');form=attachReviewForm(f);await f.forms.showSkillDraft('draft');
  assert.equal(form.querySelector('[name=review_status]').value,'unreviewed');
  assert.equal(form.querySelector('[name=review_note]').value,'');
  assert.match(f.$('#skill-quality-panel').innerHTML,/0 项有问题，1 项未复核/);
  f.view.dispose();
});

test('review text entered while saving a method cannot be discarded by the save response',async()=>{
  const save=deferred(),f=skillFixture({report:reviewReport(),onAction:action=>action==='save'?save.promise:undefined});
  const form=attachReviewForm(f);await f.forms.showSkillDraft('draft');
  f.editor.controls.get('instructions').value+=' 请核对时间。';f.editor.oninput();
  const beforeInspects=f.calls.filter(c=>c.action==='inspect').length;
  const pending=f.editor.onsubmit(submitEvent(f.editor));await flush();
  // Even if another host or scripted event edits a disabled field, its new text
  // must not be blessed as saved or replaced by a stale network response.
  form.querySelector('[name=review_note]').value='保存方法期间新补的复核。';form.oninput();
  save.resolve({draft_id:'draft',revision:7,content_hash:'hash-7'});await pending;
  assert.equal(f.calls.filter(c=>c.action==='inspect').length,beforeInspects,'no report redraw after newer review edit');
  assert.equal(form.querySelector('[name=review_note]').value,'保存方法期间新补的复核。');
  assert.equal(f.forms.canAutoReloadSkillEditor(),false);
  assert.match(f.$('#draft-detail-error').textContent,/复核/);
  f.view.dispose();
});

const pendingValue = (patch={}) => ({cases:[{prompt:'',profile:'preview',kind:'output',contains:'\n明天\n',excludes:'',...patch}],repeats:'2'});

test('saved pending cases restore incomplete raw text without running or inheriting an old pass',async()=>{
  const report=reviewReport(),stored={version:4,source_revision:2,value:pendingValue({profile:'retired',kind:'trigger_false',feedback_id:'12345678-1234-1234-1234-123456789abc'})};
  const f=skillFixture({onAction:action=>action==='inspect'?{...quality('检查'),evaluation:report,pending_cases:stored}:undefined});
  await f.forms.showSkillDraft('draft');
  const row=f.$('#skill-eval-cases').children[0];
  assert.equal(row.querySelector('[name=prompt]').value,'');
  assert.equal(row.querySelector('[name=contains]').value,'\n明天\n');
  assert.equal(row.querySelector('[name=kind]').value,'trigger_false');
  assert.equal(row.querySelector('[name=profile]').value,'','retired mode must not be silently reassigned');
  assert.equal(row.dataset.feedbackId,stored.value.cases[0].feedback_id);
  assert.match(row.querySelector('[data-case-source]').textContent,/来源修订 2.*修订已变化.*原模式“retired”/);
  assert.equal(f.$('#skill-eval-form').querySelector('[name=repeats]').value,'2');
  assert.match(f.$('#skill-eval-state').textContent,/保存不代表已经运行或通过/);
  assert.equal(f.calls.some(c=>c.action==='evaluate'),false);
  assert.equal(await f.forms.canLeaveSkillEditor(),true);
  assert.equal(report.passed,true,'old result remains its own immutable evidence');
  await f.$('#skill-eval-form').onsubmit(submitEvent(f.$('#skill-eval-form')));
  assert.match(f.$('#skill-eval-state').textContent,/明确选择当前修订的模式/);
  assert.equal(f.calls.some(c=>c.action==='evaluate'),false);
  f.view.dispose();
});

test('pending save sends raw snapshot and companion version, preserving edits made during request',async()=>{
  const stored={version:4,source_revision:3,value:pendingValue()},first=deferred(),requests=[];
  const f=skillFixture({onAction:action=>action==='inspect'?{...quality('检查'),pending_cases:stored}:undefined,onRequest:(path,options)=>{
    if(path!=='/api/skill-pending-cases')return;
    requests.push(structuredClone(options.body));
    if(requests.length===1)return first.promise;
    return {version:6,source_revision:3,value:structuredClone(options.body.value)};
  }});
  await f.forms.showSkillDraft('draft');
  const row=f.$('#skill-eval-cases').children[0];
  row.querySelector('[name=prompt]').value='整理成周报：明天补测试。';
  f.$('#skill-eval-form').oninput();
  const saving=f.$('#skill-save-pending').onclick();await flush();
  assert.deepEqual(requests,[{draft_id:'draft',expected_revision:3,expected_version:4,value:pendingValue({prompt:'整理成周报：明天补测试。'})}]);
  assert.equal(f.$('#skill-save-pending').disabled,true);
  row.querySelector('[name=contains]').value='明天\n测试';f.$('#skill-eval-form').oninput();
  await f.$('#skill-save-pending').onclick();assert.equal(requests.length,1);
  first.resolve({version:5,source_revision:3,value:requests[0].value});await saving;
  assert.match(f.$('#skill-eval-state').textContent,/仍有未保存修改/);
  assert.equal(f.forms.canAutoReloadSkillEditor(),false);
  await f.$('#skill-save-pending').onclick();
  assert.equal(requests[1].expected_version,5);
  assert.equal(requests[1].value.cases[0].contains,'明天\n测试');
  assert.equal(f.forms.canAutoReloadSkillEditor(),true);
  assert.equal(await f.forms.canLeaveSkillEditor(),true);
  assert.match(f.$('#skill-eval-state').textContent,/已保存.*尚未运行/);
  assert.equal(f.calls.some(c=>c.action==='evaluate'),false);
  f.view.dispose();
});

test('pending save failure retains fields and expected version; incomplete question can be saved explicitly',async()=>{
  const requests=[];
  const f=skillFixture({onRequest:(path,options)=>{
    if(path!=='/api/skill-pending-cases')return;
    requests.push(structuredClone(options.body));
    if(requests.length===1)throw Error('网络暂时不可用，请重试。');
    return {version:1,source_revision:3,value:options.body.value};
  }});
  await f.forms.showSkillDraft('draft');
  const row=f.$('#skill-eval-cases').children[0];row.querySelector('[name=excludes]').value='已完成';f.$('#skill-eval-form').oninput();
  await f.$('#skill-save-pending').onclick();
  assert.match(f.$('#skill-eval-state').textContent,/网络暂时不可用/);
  assert.equal(row.querySelector('[name=prompt]').value,'');
  assert.equal(row.querySelector('[name=excludes]').value,'已完成');
  assert.equal(f.forms.canAutoReloadSkillEditor(),false);
  await f.$('#skill-save-pending').onclick();
  assert.deepEqual(requests[1],requests[0]);assert.equal(requests[0].expected_version,0);
  assert.equal(f.forms.canAutoReloadSkillEditor(),true);
  assert.equal(f.calls.some(c=>c.action==='evaluate'),false);
  f.view.dispose();
});

test('pending case save cannot silently save an unsaved method revision',async()=>{
  const requests=[];
  const f=skillFixture({onRequest:(path,options)=>{if(path==='/api/skill-pending-cases'){requests.push(options.body);return {version:1};}}});
  await f.forms.showSkillDraft('draft');
  f.editor.controls.get('instructions').value+='新方法。';f.editor.oninput();
  assert.equal(f.$('#skill-save-pending').disabled,true);
  await f.$('#skill-save-pending').onclick();
  assert.equal(requests.length,0);assert.match(f.$('#skill-eval-state').textContent,/未保存修改/);
  f.view.dispose();
});

function progressFeedback(latest) {
  return {feedback_id:'12345678-1234-1234-1234-123456789abc',rating:'incorrect',comment:'不要补效果',profile:'preview',version:'1.0.0',source:{kind:'binding',load_event_id:'event'},
    progress:{pending_saved:true,latest_job:latest}};
}

test('feedback displays the latest failed run without reusing passing scores and opens that exact job',async()=>{
  const latest={job_id:'latest-failed',revision:4,state:'failed',candidate:{passed:1,total:1},review:{problem:0,unreviewed:0}};
  const requests=[],f=skillFixture({onAction:(action,data)=>{
    if(action==='inspect')return {...quality('检查'),feedback:{items:[progressFeedback(latest)]}};
    if(action==='jobs'){requests.push(data);return {job_id:'latest-failed',state:'failed',error:'模型暂时不可用'};}
  }});
  await f.forms.showSkillDraft('draft');
  const item=f.$('#skill-feedback-list').children[0];
  assert.match(item.children[2].textContent,/已保存关联待测题目.*最近测试：修订 4 · 失败/);
  assert.doesNotMatch(item.children[2].textContent,/1\/1|已解决|线上已修复/);
  await item.children[3].onclick();
  assert.deepEqual(requests,[{job_id:'latest-failed'}]);
  assert.match(item.children[4].innerHTML,/模型暂时不可用.*尚无完整结果/);
  assert.doesNotMatch(item.children[4].innerHTML,/data-review-row/);
  f.view.dispose();
});

test('feedback completion keeps automatic and manual results separate and report link is read only',async()=>{
  const latest={job_id:'feedback-report',revision:4,state:'succeeded',candidate:{passed:1,total:1},review:{problem:1,unreviewed:0}};
  const report=reviewReport('feedback-report');report.reviews={'0:0:candidate':{status:'problem',note:'补了效果',version:1}};
  const f=skillFixture({onAction:(action,data)=>{
    if(action==='inspect')return {...quality('检查'),feedback:{items:[progressFeedback(latest)]}};
    if(action==='jobs'){assert.equal(data.job_id,'feedback-report');return {job_id:'feedback-report',state:'succeeded',result:report};}
  }});
  await f.forms.showSkillDraft('draft');const item=f.$('#skill-feedback-list').children[0];
  assert.match(item.children[2].textContent,/候选断言 1\/1.*人工复核 1 项有问题.*不代表线上已修复/);
  await item.children[3].onclick();
  assert.match(item.children[4].innerHTML,/feedback-report.*修订|修订.*feedback-report/);
  assert.match(item.children[4].innerHTML,/有问题.*补了效果/);
  assert.doesNotMatch(item.children[4].innerHTML,/<form|data-review-row/);
  f.view.dispose();
});

test('changing the fixed comparison source clears stale regression badges without replacing pending input',async()=>{
  const report=reviewReport(),comparison={source_revision:2,inputs_equal:true,models:'known_same',regressed_cases:[0]};
  const fixed={job_id:'old-job',revision:2,cases:report.cases,repeats:1};
  const f=skillFixture({onAction:action=>action==='inspect'?{...quality('检查'),evaluation:report,regression:fixed,comparison}:undefined,
    onRequest:(path,options)=>path==='/api/skill-regressions'?(options.body.job_id?{...fixed,job_id:options.body.job_id}:null):undefined});
  const summary=new SkillElement();summary.textContent='上次通过 → 本次失败 · 第 1 题 · 明天补测试';
  f.$('#skill-quality-panel').queries.set('[data-report-case] > summary',[summary]);
  await f.forms.showSkillDraft('draft');
  const input=f.$('#skill-eval-cases').children[0].querySelector('[name=prompt]');input.value='尚未保存的新题';
  await f.$('#skill-pin-regression').onclick();
  assert.equal(summary.textContent,'第 1 题 · 明天补测试');
  assert.match(f.$('#skill-quality-panel').querySelector('[data-report-comparison]').textContent,/尚无新的对照/);
  assert.equal(f.$('#skill-quality-panel').querySelector('[data-regression-note]').textContent,'');
  assert.equal(input.value,'尚未保存的新题');
  await f.$('#skill-clear-regression').onclick();
  assert.equal(f.$('#skill-quality-panel').querySelector('[data-report-comparison]').textContent,'');
  assert.equal(input.value,'尚未保存的新题');
  f.view.dispose();
});

test('pending and review saves refresh feedback progress in place without losing draft inputs or expanded feedback',async()=>{
  const report=reviewReport(),latest={job_id:report.job_id,revision:3,state:'succeeded',candidate:{passed:1,total:1},review:{problem:0,unreviewed:1}};
  let pendingSaved=false,inspects=0;
  const f=skillFixture({onAction:action=>{
    if(action!=='inspect')return;
    inspects++;
    const feedback=progressFeedback(structuredClone(latest));feedback.progress.pending_saved=pendingSaved;
    return {...quality('检查'),evaluation:report,feedback:{items:[feedback]}};
  },onRequest:(path,options)=>{
    if(path==='/api/skill-pending-cases'){pendingSaved=true;return {version:1,source_revision:3,value:options.body.value};}
    if(path==='/api/skill-report-reviews'){latest.review={problem:1,unreviewed:0};return {...options.body,version:1};}
  }});
  const review=attachReviewForm(f);await f.forms.showSkillDraft('draft');
  const list=f.$('#skill-feedback-list'),baseQuery=list.querySelectorAll.bind(list);
  list.querySelectorAll=selector=>selector==='[data-feedback-id]'?list.children:baseQuery(selector);
  list.children[0].open=true;
  assert.doesNotMatch(list.children[0].children[2].textContent,/已保存关联待测题目/);
  const row=f.$('#skill-eval-cases').children[0];row.querySelector('[name=prompt]').value='明天补测试';
  row.querySelector('[name=contains]').value='明天';f.$('#skill-eval-form').oninput();
  await f.$('#skill-save-pending').onclick();
  assert.equal(inspects,2);assert.match(list.children[0].children[2].textContent,/已保存关联待测题目/);
  assert.equal(list.children[0].open,true);
  assert.equal(f.$('#skill-eval-cases').children[0],row,'refresh must not reconstruct the case editor');
  row.querySelector('[name=prompt]').value='明天继续补测试';f.$('#skill-eval-form').oninput();
  review.querySelector('[name=review_status]').value='problem';review.querySelector('[name=review_note]').value='补了原文没有的效果';review.oninput();
  await review.onsubmit(submitEvent(review));
  assert.equal(inspects,3);assert.match(list.children[0].children[2].textContent,/人工复核 1 项有问题、0 项未复核/);
  assert.equal(row.querySelector('[name=prompt]').value,'明天继续补测试');
  assert.equal(f.forms.canAutoReloadSkillEditor(),false,'newer unsubmitted case edit remains dirty');
  assert.match(review.querySelector('[role=status]').textContent,/复核已保存/);
  f.view.dispose();
});

test('feedback refresh failure after a successful pending save preserves saved receipt and editable inputs',async()=>{
  let inspects=0,saves=0;
  const f=skillFixture({onAction:action=>{
    if(action!=='inspect')return;
    if(++inspects===2)throw Error('进度查询暂时失败');
    return {...quality('检查'),feedback:{items:[progressFeedback(null)]}};
  },onRequest:(path,options)=>{
    if(path==='/api/skill-pending-cases'){saves++;return {version:1,source_revision:3,value:options.body.value};}
  }});
  await f.forms.showSkillDraft('draft');
  const row=f.$('#skill-eval-cases').children[0];row.querySelector('[name=prompt]').value='明天补测试';f.$('#skill-eval-form').oninput();
  await f.$('#skill-save-pending').onclick();
  assert.equal(saves,1);assert.match(f.$('#skill-eval-state').textContent,/待测题目已保存/);
  assert.match(f.$('#skill-feedback-state').textContent,/进度暂未更新.*已保存的内容不受影响/);
  assert.equal(row.querySelector('[name=prompt]').value,'明天补测试');
  assert.equal(f.forms.canAutoReloadSkillEditor(),true);
  assert.equal(await f.forms.canLeaveSkillEditor(),true);
  await f.$('#skill-refresh-feedback').onclick();
  assert.equal(saves,1,'refresh does not repeat the acknowledged save');
  assert.match(f.$('#skill-feedback-state').textContent,/进度更新于/);
  assert.match(f.$('#skill-eval-state').textContent,/待测题目已保存/);
  f.view.dispose();
});

test('an older failed feedback refresh cannot overwrite the status of a newer successful refresh',async()=>{
  let inspections=0,rejectOld;
  const old=new Promise((resolve,reject)=>{rejectOld=reject;});
  const f=skillFixture({onAction:action=>{
    if(action!=='inspect')return;
    if(++inspections===2)return old;
    return {...quality('检查'),feedback:{items:[progressFeedback({job_id:'new',revision:4,state:'running'})]}};
  }});
  await f.forms.showSkillDraft('draft');
  const older=f.$('#skill-refresh-feedback').onclick();await flush();
  await f.$('#skill-refresh-feedback').onclick();
  const fresh=f.$('#skill-feedback-state').textContent;assert.match(fresh,/进度更新于/);
  rejectOld(Error('older request failed'));await older;
  assert.equal(f.$('#skill-feedback-state').textContent,fresh);
  assert.match(f.$('#skill-feedback-list').children[0].children[2].textContent,/修订 4 · 执行中/);
  f.view.dispose();
});
