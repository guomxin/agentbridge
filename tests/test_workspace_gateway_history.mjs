import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { historyMessageKey, parseTimestampMilliseconds, textHash } from "../agentbridge/workspace/static/workspace_results.mjs";
import { historyEvidenceForRun } from "../agentbridge/workspace/gateway_run_guard.mjs";

const vectors = JSON.parse(readFileSync(new URL("./fixtures/workspace_gateway_history_vectors.json", import.meta.url), "utf8"));
for (const vector of vectors) {
  test(`authoritative history: ${vector.name}`, () => {
    assert.deepEqual(historyEvidenceForRun(vector.payload, vector.runId, vector.sessionKey), vector.expected);
  });
}

const source = readFileSync(new URL("../agentbridge/workspace/static/workspace.js", import.meta.url), "utf8");
function handler(name, nextName) {
  const start = source.indexOf(`function ${name}(`);
  const next = source.indexOf(`function ${nextName}(`, start);
  const end = source.lastIndexOf("\n}", next) + 2;
  assert.ok(start >= 0 && end > start, `load the actual ${name} handler`);
  return source.slice(start, end);
}

function fixture() {
  const state = {historyMessages: new Map(), syncedMessages: new Map(), localMessages: new Map()};
  let history = [];
  const scope = {assertCurrent() {}, current: () => true};
  const bindings = {
    state, parseTimestampMilliseconds, textHash, historyMessageKey,
    sessionScope: {replace: () => scope},
    scopedApi: () => async () => ({messages: history}),
    messageElement: message => ({dataset: {messageRole: message.role, messageTextHash: textHash(message.text)}}),
    timelineEntryImages: () => [], renderChatTimeline() {},
    markWorkspaceTiming() {}, scrollChat() {}, scheduleTaskSync() {},
    hydrateSkillCards: async () => {}, isCancellation: () => false,
    toast: message => assert.fail(message),
  };
  const code = [handler("setTimelineNode", "ingestTimelineEntry"),
    handler("ingestTimelineEntry", "renderChatTimeline"),
    "async " + handler("loadLegacyHistory", "loadTaskHistory"),
    handler("reconcileOriginChatMessage", "timelineEntryImages"),
    handler("removeMatchingHistoryMessage", "renderRunFailure")].join("\n");
  const functions = new Function(...Object.keys(bindings), `${code}\nreturn {loadLegacyHistory,reconcileOriginChatMessage,ingestTimelineEntry};`)(...Object.values(bindings));
  return {state, receive: functions.reconcileOriginChatMessage, ingest: functions.ingestTimelineEntry,
    async load(messages) {
      history = messages;
      state.legacyHistoryLoaded = false;
      await functions.loadLegacyHistory();
    }};
}

const oldMessage = {role: "user", text: "查询OA待办", timestamp: "2026-10-23T03:00:00Z"};
const quickQuery = (id = "quick-a") => ({entry_id: id, sequence: 30, role: "user", text: oldMessage.text,
  message_key: "workspace:quick-query:user:" + id, created_at: "2026-10-23T03:01:00Z"});
const gatewayEntry = () => ({...quickQuery("gateway"), message_key: "workspace:user:original-run", created_at: oldMessage.timestamp});

test("quick-query receipts keep distinct earlier Gateway messages and deduplicate only their own entry IDs", async () => {
  const f = fixture();
  await f.load([oldMessage]);
  f.receive(quickQuery());
  f.receive(quickQuery("quick-b"));
  f.receive(quickQuery());
  assert.equal(f.state.historyMessages.size, 1);
  assert.equal(f.state.syncedMessages.size, 2);
  assert.notEqual(f.state.syncedMessages.get("quick-a"), f.state.syncedMessages.get("quick-b"));
});

test("loading Gateway history after a quick-query receipt preserves its independent same-text message", async () => {
  const f = fixture();
  f.receive(quickQuery());
  await f.load([oldMessage]);
  await f.load([oldMessage]);
  assert.equal(f.state.historyMessages.size, 1);
  assert.equal(f.state.syncedMessages.size, 1);
});

test("a quick query synced from another endpoint cannot suppress local Gateway history", async () => {
  const f = fixture();
  const entry = {...quickQuery(), entry_type: "chat_message", source: {is_origin: false}};
  f.ingest(entry);
  f.ingest(entry);
  await f.load([oldMessage]);
  assert.equal(f.state.historyMessages.size, 1);
  assert.equal(f.state.syncedMessages.size, 1);
});

test("ordinary same-origin Gateway receipts still reconcile history and their local message", async () => {
  const f = fixture();
  await f.load([oldMessage]);
  f.state.localMessages.set("original-run", {dataset: {}});
  f.receive(quickQuery());
  f.receive(gatewayEntry());
  f.receive(gatewayEntry());
  assert.equal(f.state.historyMessages.size, 0);
  assert.equal(f.state.localMessages.size, 0);
  assert.equal(f.state.syncedMessages.size, 2);
});

test("ordinary Gateway receipts still suppress their own later legacy copy alongside quick queries", async () => {
  const f = fixture();
  f.receive(gatewayEntry());
  f.receive(quickQuery());
  await f.load([oldMessage]);
  assert.equal(f.state.historyMessages.size, 0);
  assert.equal(f.state.syncedMessages.size, 2);
});
