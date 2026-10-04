import test from "node:test";
import assert from "node:assert/strict";
import { createResultView, completedInteractionPresentation as presentation,
  taskCardStatusMessage as batchMessage, taskCardStatusForInteraction as cardStatus,
  taskPlanFailurePresentation as failurePresentation } from "../bscli/workspace/static/workspace_results.mjs";
import { createTaskCards } from "../bscli/workspace/static/workspace_cards.mjs";

test("database CSV artifacts expose only valid authenticated download paths", () => {
  const node = (tag = "div") => ({tag, children: [], append(...items) { this.children.push(...items); }});
  const { appendArtifactList: render } = createResultView({ document: { createElement: node } });
  const path = `/api/database/reports/taihua_primary/${"a".repeat(32)}/download`;
  for (const [type, url, state, allowed] of [
    ["database_csv", path, "ready", true],
    ["database_csv", path, "expired", false],
    ["database_csv", path.replace("taihua_primary", "../other"), "ready", false],
    ["database_csv", "javascript:alert(1)", "ready", false],
    ["taihua_personal_csv", `/api/analytics/reports/${"a".repeat(32)}/download`, "ready", false],
  ]) {
    const root = node();
    render(root, [{artifact_type: type, download_url: url, state, filename: "report.csv"}]);
    const flatten = (n) => [n, ...n.children.flatMap(flatten)];
    const links = flatten(root).filter(n => n.tag === "a");
    assert.equal(links.length, allowed ? 1 : 0);
    if (allowed) assert.equal(links[0].href, path);
  }
});
test("canceled task takes precedence over its retired interaction", () => {
  for (const state of ["superseded", "pending", "completed", "expired"]) {
    assert.equal(cardStatus(state, "canceled"), "canceled");
  }
  assert.equal(cardStatus("superseded", "waiting_user"), "superseded");
});
test("batch progress distinguishes a single completed item from the whole batch", () => {
  const batch = { totalCount: 23, succeededCount: 4, currentOrdinal: 5, state: "waiting_user" };
  assert.match(batchMessage("waiting_user", { batch }), /4\/23.*19.*第 5 条/);
  assert.match(batchMessage("succeeded", { batch: { ...batch, state: "succeeded" } }), /全部完成.*23/);
  assert.match(batchMessage("outcome_unknown", { batch: { ...batch, state: "outcome_unknown" } }), /批次已停止/);
});

test("completed input and credential cards describe the step, not business success", () => {
  const fields = presentation({ state: "completed", type: "business_input" });
  assert.equal(fields.label, "字段已提交");
  assert.match(fields.message, /不代表业务已提交/);
  const login = presentation({ state: "completed", type: "credential" });
  assert.equal(login.label, "认证已完成");
  assert.match(login.message, /原任务结果/);
});

test("pending, failed, canceled and business authorization retain their actual state", () => {
  for (const state of ["pending", "processing", "failed", "declined", "expired"]) {
    assert.equal(presentation({ state, type: "business_input" }), null);
  }
  assert.equal(presentation({ state: "completed", type: "execution_authorization" }), null);
  assert.equal(presentation(null), null);
});

test("incomplete source plan shows safe stop with source counts and no business write", () => {
  const result = failurePresentation({
    terminalReason: "PLAN_SOURCE_INCOMPLETE",
    resultProjection: { result: { source_summaries: [
      { collection: "done", status: "partial", scanned_count: 50, coverage: { sourceQueryTotal: 100 } },
      { collection: "sent", status: "complete", scanned_count: 0 },
    ] } },
  });
  assert.equal(result.label, "已安全停止");
  assert.match(result.message, /OA 已办.*50.*100.*未进入业务写入/);
  assert.doesNotMatch(result.message, /OA 已发/);
  assert.equal(failurePresentation({ terminalReason: "OTHER" }), null);
});


test("Skill cards distinguish rule loading from business success and render text safely", () => {
  const node = () => ({ children: [], append(...items) { this.children.push(...items); } });
  const { renderSkillCard: render } = createTaskCards({
    document: { createElement: node }, setTimelineNode() {},
  });
  const success = render({name: "<img onerror=bad>", status: "succeeded", profile: "fill", version: "1.0.0"});
  assert.equal(success.children[0].textContent, "业务助手 · <img onerror=bad>");
  assert.equal(success.children[1].textContent, "已加载");
  assert.match(success.children[2].textContent, /准备填写.*业务执行结果请查看后续任务/);
  const failure = render({name: "助手", status: "rejected", message: "未分配"});
  assert.equal(failure.children[1].textContent, "加载失败");
  assert.equal(failure.children[2].textContent, "未分配");
});
