import test from "node:test";
import assert from "node:assert/strict";
import { createLifecycle, isCancellation } from "../agentbridge/workspace/static/workspace_lifecycle.mjs";
import { createWorkspaceRequests } from "../agentbridge/workspace/static/workspace_request.mjs";
import { createChatStream } from "../agentbridge/workspace/static/workspace_stream.mjs";

const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
const response = payload => ({ ok: true, json: async () => payload });
const document = { cookie: "other=value; agentbridge_workspace_csrf=opaque-token" };

test("EOF without a terminal result fails visibly and never replays even after acceptance", async () => {
  for (const wire of ["retry: 1000\n\n", "retry: 1000\n\nHTTP/1.0 400 Bad Request\r\n\r\n{\"error\":{\"code\":\"INVALID_REQUEST\"}}",
    'event: accepted\ndata: {"runId":"accepted"}\n\n',
    'event: chat\ndata: {"state":"delta","text":"partial"}\n\n']) {
    const scope = createLifecycle().scope();
    const state = { activeStreams: new Map() };
    let calls = 0;
    let closed = 0;
    let rendered = 0;
    const { consumeChatStream } = createChatStream({state, getScope: () => scope,
      fetchChatStreamResponse: async () => { calls++; return {ok: true, body: {getReader: () => ({
        read: async () => ({done: true, value: new TextEncoder().encode(wire)}), cancel: async () => closed++,
      })}}; },
      adoptLiveMessage() {}, addLiveProgress() {}, ensureLiveMessage: () => ({actions: {replaceChildren() {}}}),
      handleChatDelta: () => rendered++,
    });
    await assert.rejects(consumeChatStream({message: "do not replay", idempotencyKey: "local"}),
      error => error.code === "WORKSPACE_STREAM_INCOMPLETE" && error.safeToRetry === false);
    assert.equal(calls, 1);
    assert.equal(closed, 1);
    assert.equal(state.activeStreams.size, 0);
    assert.equal(rendered, wire.includes('"delta"') ? 1 : 0);
    scope.dispose();
  }
});

test("a final result succeeds while a late server failure remains explicit and non-retryable", async () => {
  for (const fail of [false, true]) {
    const scope = createLifecycle().scope();
    const { consumeChatStream } = createChatStream({state: {activeStreams: new Map()}, getScope: () => scope,
      fetchChatStreamResponse: async () => new Response(fail
        ? 'event: stream-error\ndata: {"code":"WORKSPACE_STREAM_FAILED","safeToRetry":false}\n\n'
        : 'event: chat\ndata: {"state":"final","text":"completed"}\n\n'),
      handleChatDelta() {},
    });
    const result = consumeChatStream({message: "test", idempotencyKey: "id"});
    if (fail) await assert.rejects(result, e => e.code === "WORKSPACE_STREAM_FAILED" && !e.safeToRetry);
    else await result;
    scope.dispose();
  }
});

test("JSON requests preserve credentials, CSRF, body and server error codes without replay", async () => {
  const scope = createLifecycle().scope();
  const calls = [];
  const { api } = createWorkspaceRequests({ document, getScope: () => scope,
    fetch: async (path, options) => { calls.push({path, ...options}); return response({value: 1}); },
  });
  assert.deepEqual(await api("/api/tasks/continue", { method: "POST", csrf: true, body: {id: 4} }), {value: 1});
  assert.equal(calls[0].credentials, "same-origin");
  assert.equal(calls[0].headers["X-AgentBridge-CSRF"], "opaque-token");
  assert.equal(calls[0].body, '{"id":4}');
  assert.ok(calls[0].signal instanceof AbortSignal);
  let failures = 0;
  const failing = createWorkspaceRequests({ document, getScope: () => scope,
    fetch: async () => { failures++; return { ok: false, json: async () => ({error: {code: "OUTCOME_UNKNOWN", message: "核对结果"}}) }; },
  });
  await assert.rejects(failing.api("/api/write", { method: "POST" }), error => error.code === "OUTCOME_UNKNOWN" && error.message === "核对结果");
  assert.equal(failures, 1);
  scope.dispose();
});

test("an old account response is rejected even when the transport ignores abort during JSON decoding", async () => {
  const lifecycle = createLifecycle();
  let account = lifecycle.scope();
  const body = deferred();
  let signal;
  const client = createWorkspaceRequests({ document, getScope: () => account,
    fetch: async (_path, options) => { signal = options.signal; return {ok: true, json: () => body.promise}; },
  });
  const old = client.api("/api/skills");
  const rejected = assert.rejects(old, isCancellation);
  await Promise.resolve();
  account.dispose();
  account = lifecycle.scope();
  body.resolve({secret: "old account"});
  await rejected;
  assert.equal(signal.aborted, true);
  assert.equal(account.current(), true);
  lifecycle.dispose();
});

test("newer detail selection wins and aborts older rendering while independent timeline requests survive", async () => {
  const account = createLifecycle().scope();
  const view = account.child();
  const waiting = new Map();
  const client = createWorkspaceRequests({ document, getScope: () => account,
    fetch: (path, options) => { const result = deferred(); waiting.set(path, {...result, signal: options.signal}); return result.promise; },
  });
  let shown;
  const select = id => client.api(id, {scope: view.replace("detail")}).then(data => { shown = data.id; });
  const first = select("first");
  const canceled = assert.rejects(first, isCancellation);
  const second = select("second");
  const timeline = client.api("timeline");
  waiting.get("second").resolve(response({id: "second"}));
  await second;
  waiting.get("first").resolve(response({id: "first"}));
  await canceled;
  assert.equal(shown, "second");
  assert.equal(waiting.get("first").signal.aborted, true);
  view.dispose();
  assert.equal(waiting.get("timeline").signal.aborted, false);
  waiting.get("timeline").resolve(response({cursor: 9}));
  assert.deepEqual(await timeline, {cursor: 9});
  account.dispose();
});

test("stream transport retries only a network failure and keeps the same idempotency payload", async () => {
  const scope = createLifecycle().scope();
  const calls = [];
  const progress = [];
  const client = createWorkspaceRequests({ document, getScope: () => scope,
    addLiveProgress: (...args) => progress.push(args),
    fetch: async (path, options) => { calls.push({path, ...options}); if (calls.length === 1) throw new TypeError("offline"); return {ok: true}; },
  });
  const controller = new AbortController();
  const result = await client.fetchChatStreamResponse({message: "请处理", idempotencyKey: "stable",
    attachments: [{mimeType: "image/png", fileName: "a.png", content: "abc"}], activeStream: {controller},
  });
  assert.equal(result.ok, true);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].body, calls[1].body);
  assert.equal(JSON.parse(calls[0].body).idempotencyKey, "stable");
  assert.equal(calls[0].headers["X-AgentBridge-CSRF"], "opaque-token");
  assert.equal(progress.length, 1);
  scope.dispose();
});

test("short trial transport retries the exact bound revision and sends no display metadata", async () => {
  const calls = [], paths = [];
  const trial = {draft_id: "draft", expected_revision: 7, profile: "preview", name: "周报助手"};
  const client = createWorkspaceRequests({document, addLiveProgress() {}, fetch: async (_path, options) => {
    paths.push(_path);
    calls.push(options.body);
    if (calls.length === 1) { trial.expected_revision = 8; throw new TypeError("offline"); }
    return {ok: true};
  }});
  await client.fetchChatStreamResponse({message: "计划修复登录问题", idempotencyKey: "bound", attachments: [], skillTrial: trial,
    activeStream: {controller: new AbortController()}});
  assert.equal(calls[0], calls[1]);
  assert.deepEqual(paths, ['/api/skill-trials/send-stream', '/api/skill-trials/send-stream']);
  assert.deepEqual(JSON.parse(calls[0]), {message: "计划修复登录问题", idempotencyKey: "bound", attachments: [],
    skillTrial: {draft_id: "draft", expected_revision: 7, profile: "preview"}});
});

test('old server 404 never downgrades a structured trial into ordinary chat', async () => {
  const paths=[];
  const client=createWorkspaceRequests({document,addLiveProgress(){},fetch:async path=>{
    paths.push(path);return {ok:false,status:404};
  }});
  const response=await client.fetchChatStreamResponse({message:'计划修复登录问题',idempotencyKey:'trial',attachments:[],
    skillTrial:{draft_id:'draft',expected_revision:1,profile:'preview'},activeStream:{controller:new AbortController()}});
  assert.equal(response.status,404);
  assert.deepEqual(paths,['/api/skill-trials/send-stream']);
});

test("failed trial stream preserves its binding and receipt for explicit retry", async () => {
  const scope = createLifecycle().scope(), trial = {draft_id: "draft", expected_revision: 7, profile: "preview"};
  const receipt = {draft_id: "draft", revision: 7, profile: "preview", status: "not_reported"};
  let sent, failure, accepted = 0;
  const live = {actions: {replaceChildren() {}}};
  const onAccepted = () => accepted++;
  const {consumeChatStream} = createChatStream({state: {activeStreams: new Map()}, getScope: () => scope,
    fetchChatStreamResponse: async request => {sent = request; return new Response(
      'event: accepted\ndata: {"runId":"run"}\n\n' +
      `event: chat\ndata: ${JSON.stringify({runId: "run", state: "error", text: "失败", safeToRetry: true, skillTrial: receipt})}\n\n`);},
    ensureLiveMessage: () => live, adoptLiveMessage() {}, addLiveProgress() {}, agentFailureMessage: text => text,
    renderRunFailure: (...args) => {failure = args;},
  });
  await assert.rejects(consumeChatStream({message: "合成材料", idempotencyKey: "key", skillTrial: trial, onAccepted}));
  assert.deepEqual(sent.skillTrial, trial);
  assert.notEqual(sent.skillTrial, trial);
  assert.equal(accepted, 1);
  assert.deepEqual(failure.slice(2, 7), [true, "合成材料", [], trial, receipt]);
  assert.equal(failure[7], onAccepted);
  scope.dispose();
});

test("an aborted trial clears only its bound intent while ordinary failure keeps it for retry", async () => {
  for (const terminal of ['error', 'aborted']) {
    const scope = createLifecycle().scope(), trial = {draft_id: 'draft', expected_revision: 7, profile: 'preview'};
    let canceled = 0, accepted = 0, failure;
    const onCanceled = () => canceled++;
    const {consumeChatStream} = createChatStream({state: {activeStreams: new Map()}, getScope: () => scope,
      fetchChatStreamResponse: async () => new Response(`event: chat\ndata: ${JSON.stringify({runId: 'run', state: terminal, text: '已停止', safeToRetry: true})}\n\n`),
      agentFailureMessage: text => text, renderRunFailure: (...args) => {failure = args;},
    });
    await assert.rejects(consumeChatStream({message: '合成材料', idempotencyKey: 'key', skillTrial: trial, onAccepted: () => accepted++, onCanceled}));
    assert.equal(canceled, terminal === 'aborted' ? 1 : 0);
    assert.equal(accepted, 0);
    assert.deepEqual(failure[5], trial);
    assert.equal(failure[8], onCanceled);
    scope.dispose();
  }
});

test("canceling during the retry delay prevents a second send", async () => {
  let calls = 0;
  const controller = new AbortController();
  const client = createWorkspaceRequests({ document, getScope: () => createLifecycle().scope(),
    addLiveProgress: () => controller.abort(), fetch: async () => { calls++; throw new TypeError("offline"); },
  });
  await assert.rejects(client.fetchChatStreamResponse({message: "m", idempotencyKey: "once", attachments: [], activeStream: {controller}}), isCancellation);
  assert.equal(calls, 1);
});

test("destroying a streaming account ignores late chunks and closes the reader without synthesizing retry", async () => {
  const scope = createLifecycle().scope();
  const chunk = deferred();
  const started = deferred();
  let canceled = 0;
  let rendered = 0;
  const state = { activeStreams: new Map() };
  const { consumeChatStream } = createChatStream({ state, getScope: () => scope,
    fetchChatStreamResponse: async () => ({ok: true, body: {getReader: () => ({
      read: () => { started.resolve(); return chunk.promise; }, cancel: async () => canceled++,
    })}}),
    handleChatDelta: () => rendered++, renderRunFailure: () => rendered++,
  });
  const running = consumeChatStream({message: "do not replay", idempotencyKey: "old"});
  const rejected = assert.rejects(running, isCancellation);
  await started.promise;
  scope.dispose();
  chunk.resolve({done: false, value: new TextEncoder().encode('event: chat\ndata: {"runId":"old","state":"final","text":"private"}\n\n')});
  await rejected;
  assert.equal(rendered, 0);
  assert.equal(canceled, 1);
  assert.equal(state.activeStreams.size, 0);
});

test('task feedback conflict preserves HTTP status and the authoritative receipt without replay', async () => {
  const scope = createLifecycle().scope(); let calls = 0;
  const feedback = {available:false,receipt:{status:'recorded',feedback_id:'feedback-1',rating:'useful',comment:'原评价'}};
  const {api} = createWorkspaceRequests({document,getScope:() => scope,fetch:async () => {
    calls++; return {ok:false,status:409,json:async () => ({error:{code:'TASK_FEEDBACK_CONFLICT',message:'此任务已有评价'},feedback})};
  }});
  await assert.rejects(api('/api/tasks/task/feedback',{method:'POST',csrf:true,body:{rating:'incorrect',comment:'其他评价',requestKey:'key'}}),
    error => error.status === 409 && error.code === 'TASK_FEEDBACK_CONFLICT' && error.feedback.receipt.feedback_id === 'feedback-1');
  assert.equal(calls,1); scope.dispose();
});
