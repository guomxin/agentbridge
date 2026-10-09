import test from "node:test";
import assert from "node:assert/strict";
import { createLifecycle } from "../agentbridge/workspace/static/workspace_lifecycle.mjs";
import { createWorkspaceRequests } from "../agentbridge/workspace/static/workspace_request.mjs";
import { createComposer } from "../agentbridge/workspace/static/workspace_forms.mjs";
import { createSkillForms } from "../agentbridge/workspace/static/workspace_skills.mjs";

class Element {
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
