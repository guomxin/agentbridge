import test from "node:test";
import assert from "node:assert/strict";
import { createLifecycle } from "../agentbridge/workspace/static/workspace_lifecycle.mjs";
import { createWorkspaceRequests } from "../agentbridge/workspace/static/workspace_request.mjs";
import { createComposer } from "../agentbridge/workspace/static/workspace_forms.mjs";
import { createSkillForms, skillPublicationCoverage, skillCandidateDiff } from "../agentbridge/workspace/static/workspace_skills.mjs";

class Element {
  dataset = {};
  children = [];
  textContent = "";
  innerHTML = "";
  classList = { add() {}, remove() {} };
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute() {}
  addEventListener() {}
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

function composerFixture() {
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
  const forms = createComposer({ ...dom(), state, getScope: () => scope,
    FileReader: Reader, crypto: { randomUUID: () => String(readers.length) },
    toast: (...args) => warnings.push(args), executeChatMessage: (...args) => messages.push(args),
  });
  return { forms, scope, state, readers, messages, warnings };
}

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
  bundle: {content_hash: "hash-3", manifest: {name: "日志助手", description: "归纳日志", selection: {use_when: "归纳", not_for: "无来源", output: "摘要"},
    profiles: {preview: {}, fill: {all: ["taihua.work_log.create"]}}, executionMode: "durable_plan", required_resources: {preview: ["references/evidence.md"], fill: []}},
    resources: {"SKILL.md": "保留计划与完成的区别。", "references/evidence.md": "核对来源。"}},
  tests: [{revision: 3, profile: "preview", status: "reported", prompt: "归纳计划", output: "计划"}],
});

function skillFixture({initial = editableDraft(), jobs = [], report = null, onAction, timers} = {}) {
  const nodes = new Map();
  const $ = selector => { if (!nodes.has(selector)) nodes.set(selector, new SkillElement()); return nodes.get(selector); };
  const view = createLifecycle({timers}).scope();
  const drafts = new Map([[initial.draft_id, structuredClone(initial)]]);
  const calls = [], navigations = [];
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
  const forms = createSkillForms({document: {createElement: () => new SkillElement()}, $, state: {activeView: "skills"}, api,
    FormData: SkillFormData, getScope: () => view, switchView: value => navigations.push(value)});
  return {forms, $, view, editor, drafts, calls, navigations};
}
const submitEvent = target => ({target, preventDefault() {}});

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
  assert.match(f.$("#chat-form textarea[name='message']").value, /修订 7.*准备填单（fill）/);
  assert.match(f.$("#chat-form textarea[name='message']").value, /草稿（draft）/);
  assert.doesNotMatch(f.$("#chat-form textarea[name='message']").value, /expected_revision|profile=/);
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
