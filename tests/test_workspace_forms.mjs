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

test("plain personal OA query uses the closed read once, while attachments and trial intent keep their original path", async () => {
  const reads = [], gate = deferred();
  const f = composerFixture({queryOaPending: async payload => {reads.push(payload);await gate.promise;}});
  const input = {value: "查看OA待办", disabled: false}, button = {disabled: false};
  const form = {elements: {message: input}, querySelectorAll: () => [button]};
  const sending = f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(input.disabled, true);assert.equal(button.disabled, true);
  await f.forms.sendChat({preventDefault() {}, currentTarget: form});
  assert.equal(reads.length, 1);assert.deepEqual(Object.keys(reads[0]), ["requestKey"]);
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
      stopWorkspaceObservers: () => changes.push('observers'), lifecycle: createLifecycle(),
      resetWorkspaceState: () => changes.push('reset'), showAuth: () => changes.push('auth'),
      setBusy() {}, $: () => ({}), api: async () => changes.push('logout'), location: {reload: () => changes.push('reload')},
    });
    const pending = logout();
    assert.deepEqual(changes, []);
    assert.equal(scope.current(), true);
    decision.resolve(discard); await pending;
    assert.deepEqual(changes, discard ? ['observers', 'reset', 'auth', 'logout', 'reload'] : []);
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

function skillFixture({initial = editableDraft(), jobs = [], report = null, onAction, timers, confirm = () => false, refuseNavigation = false} = {}) {
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
  assert.match(f.$("#draft-detail-error").textContent, /明确选择/);
  row.querySelector('[name=profile]').value = "preview";
  await f.$("#skill-eval-form").onsubmit(submitEvent(f.$("#skill-eval-form")));
  assert.match(f.$("#draft-detail-error").textContent, /填写至少一项/);
  assert.equal(f.calls.some(c => c.action === "evaluate"), false);
  row.querySelector('[name=contains]').value = "计划";
  row.querySelector('[name=excludes]').value = "已完成";
  await f.$("#skill-eval-form").onsubmit(submitEvent(f.$("#skill-eval-form")));
  assert.deepEqual(f.calls.find(c => c.action === "evaluate").data.cases[0],
    {prompt: "计划修复", profile: "preview", kind: "output", contains: ["计划"], excludes: ["已完成"]});
  f.view.dispose();
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
  assert.match(row.querySelector('[data-case-source]').textContent, /纠错说明：把计划误写成已完成。 请补上“应包含\/不应包含”的内容；反馈仅作参考，不会自动成为验收条件。/);
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
