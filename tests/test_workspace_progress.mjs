import test from "node:test";
import assert from "node:assert/strict";
import { createChatProgress } from "../agentbridge/workspace/static/workspace_progress.mjs";
import { createResultView } from "../agentbridge/workspace/static/workspace_results.mjs";
import { createChatStream } from "../agentbridge/workspace/static/workspace_stream.mjs";
import { createLifecycle } from "../agentbridge/workspace/static/workspace_lifecycle.mjs";

class Element {
  children = [];
  attributes = {};
  className = "";
  textContent = "";
  isConnected = true;
  open = false;
  classList = { remove: () => {} };
  append(...children) { this.children.push(...children); }
  prepend(child) { this.children.unshift(child); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  get lastElementChild() { return this.children.at(-1); }
}

function fixture() {
  const root = new Element();
  const state = { liveMessages: new Map() };
  let scrolls = 0;
  const document = { createElement: () => new Element() };
  const { renderMarkdown } = createResultView({ document });
  const api = createChatProgress({ state, document, renderMarkdown,
    $: () => root, scrollChat: () => scrolls++, scheduleChatRefresh: () => {},
    attachDispatchCancel: () => { throw Error("accepted dispatch must not be canceled as unsent"); },
  });
  return { ...api, state, root, scrolls: () => scrolls };
}

test("cumulative preambles replace one collapsed description without scrolling per fragment", () => {
  const f = fixture();
  const text = "已定位到工作交接单，事项 ID 是 123456789。现在打开审批核对卡。";
  f.handleChatProgress({ runId: "run", kind: "preamble", text: text[0] });
  const before = f.scrolls();
  for (let i = 2; i <= text.length; i++) {
    f.handleChatProgress({ runId: "run", kind: "preamble", text: text.slice(0, i) });
  }
  const live = f.state.liveMessages.get("run");
  assert.equal(f.root.children.length, 1);
  assert.equal(live.progress.children.length, 2);
  assert.equal(live.progress.children[0], live.progressRow);
  assert.equal(live.progressDetails.open, false);
  assert.equal(live.progressDescription.textContent, text);
  assert.equal(live.progressRow.lastElementChild.textContent, "正在梳理处理步骤");
  assert.equal(f.scrolls(), before);
  live.progressDetails.open = true;
  f.handleChatProgress({ runId: "run", kind: "preamble", text: "下一步说明" });
  assert.equal(live.progressDetails.open, true);
  assert.equal(live.progressDescription.textContent, "下一步说明");
});

test("tool, lifecycle and failure update one accessible status and preserve actions", () => {
  const f = fixture();
  f.addLiveProgress("run", "正在连接智能体", "active");
  const live = f.state.liveMessages.get("run");
  const row = live.progressRow;
  const cancel = new Element();
  live.actions.append(cancel);
  for (const phase of ["start", "update", "result"]) {
    f.handleChatProgress({ runId: "run", kind: "tool", phase, label: "正在调用 OA 能力" });
  }
  assert.equal(live.progressRow, row);
  assert.equal(live.progress.children.length, 1);
  assert.equal(row.attributes.role, "status");
  f.handleChatProgress({ runId: "run", kind: "lifecycle", phase: "end", label: "智能体处理完成" });
  assert.equal(row.lastElementChild.textContent, "正在整理处理结果");
  f.addLiveProgress("run", "处理未完成", "failed");
  assert.equal(row.className, "live-progress-row failed");
  assert.equal(live.actions.children[0], cancel);
});

test("dispatch adoption, concurrent runs and final cleanup keep separate ownership", () => {
  const f = fixture();
  f.addLiveProgress("dispatch", "正在连接智能体", "active");
  const live = f.adoptLiveMessage("dispatch", "run", "处理请求");
  f.handleChatProgress({ runId: "run", kind: "preamble", text: "第一条说明" });
  f.handleChatProgress({ runId: "other", kind: "preamble", text: "另一条说明" });
  assert.equal(f.state.liveMessages.has("dispatch"), false);
  f.handleChatDelta({ runId: "run", state: "final", text: "请核对审批卡" });
  assert.equal(live.progress.children.length, 0);
  assert.equal(live.text.textContent, "请核对审批卡");
  assert.equal(f.state.liveMessages.has("run"), false);
  assert.equal(f.state.liveMessages.get("other").progressDescription.textContent, "另一条说明");
});


test("protected CSV cards accept only the exact authenticated same-origin route", () => {
  const { appendArtifactList: render } = createResultView({
    document: { createElement: () => new Element() },
  });
  for (const [url, allowed] of [["/api/database/reports/taihua_primary/" + "a".repeat(32) + "/download", true],
                               ["/api/analytics/reports/" + "a".repeat(32) + "/download", false],
                               ["//attacker.test/file", false], ["/api/other/file", false]]) {
    const root = new Element();
    render(root, [{state: "ready", filename: "daily.csv", artifact_type: "database_csv",
                   download_url: url, byte_size: 10, expires_at: "2099"}]);
    const row = root.children[0].children[1];
    assert.equal(row.children.some(child => child.className === "secondary task-artifact-download"), allowed);
  }
});


test("an explicit no-retry terminal event stays non-retryable without tool progress", async () => {
  for (const safe of [false, true]) {
    let renderedRetry;
    const scope = createLifecycle().scope();
    const { consumeChatStream: consume } = createChatStream({
      state: { activeStreams: new Map() }, getScope: () => scope,
      fetchChatStreamResponse: async () => ({ok: true, body: {getReader: () => ({
        read: async () => ({value: new TextEncoder().encode(
          `event: chat\ndata: ${JSON.stringify({state: "error", text: "结果未确认", safeToRetry: safe})}\n\n`
        ), done: false}),
        cancel: async () => {},
      })}}),
      agentFailureMessage: text => text,
      renderRunFailure: (_id, _text, retry) => { renderedRetry = retry; },
    });
    await assert.rejects(consume({message: "do not replay", idempotencyKey: "run"}));
    assert.equal(renderedRetry, safe);
    scope.dispose();
  }
});

test("refresh restores accepted result recovery without offering resend or cancel", () => {
  const f = fixture();
  const restore = f.restoreActiveDispatches;
  restore([{dispatchId: "d", runId: "r", state: "accepted", lastErrorCode: "HOST_RUN_RESULT_PENDING"}]);
  assert.equal(f.state.liveMessages.get("r").progressRow.lastElementChild.textContent, "请求已接收，正在恢复结果；不会重复执行");
  assert.equal(f.state.liveMessages.get("r").actions.children.length, 0);
});
