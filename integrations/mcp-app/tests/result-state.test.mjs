import test from "node:test";
import assert from "node:assert/strict";
import { interactionStateLabel, operationStatus, terminalPresentation } from "../src/result-state.ts";

test("absent, malformed and interaction-only results never claim business success", () => {
  for (const result of [undefined, null, {}, {structuredContent: {}},
    {structuredContent: []}, {structuredContent: {status: ""}},
    {structuredContent: {status: "completed"}}, {structuredContent: {interaction: {state: "completed"}}},
    {structuredContent: {status: "succeeded", interaction: {state: "completed"}}},
    {structuredContent: {status: "succeeded"}, _meta: {"io.agentbridge/interaction": {state: "completed"}}}]) {
    assert.equal(operationStatus(result), null);
    assert.equal(terminalPresentation(result).tone, "neutral");
    assert.equal(terminalPresentation(result).label, "业务结果待确认");
  }
});

test("only explicit successful business result produces a success presentation", () => {
  assert.equal(terminalPresentation({structuredContent: {status: "succeeded"}}).tone, "success");
  const error = {isError: true, structuredContent: {status: "succeeded"}};
  assert.equal(operationStatus(error), null);
  assert.equal(terminalPresentation(error).tone, "neutral");
  assert.equal(terminalPresentation({structuredContent: {status: "future_status"}}).tone, "neutral");
});

test("unknown business outcome asks for verification instead of repeat submission", () => {
  for (const status of ["unknown", "outcome_unknown", "result_unknown"]) {
    const presentation = terminalPresentation({structuredContent: {status}});
    assert.equal(presentation.label, "结果未知");
    assert.match(presentation.message, /不要重复提交/);
  }
});

test("completed interaction is labeled by its purpose, never as operation success", () => {
  assert.equal(interactionStateLabel("execution_authorization", "completed"), "已授权");
  assert.equal(interactionStateLabel("business_input", "completed"), "信息已提交");
  assert.equal(interactionStateLabel("credential", "completed"), "登录已完成");
});
