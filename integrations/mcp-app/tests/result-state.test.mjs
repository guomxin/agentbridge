import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { interactionActionLabel, interactionStateLabel, operationStatus, terminalPresentation } from "../src/result-state.ts";
import { interactionActionLabel as workspaceLabel, displayTaskTitle } from "../../../agentbridge/workspace/static/workspace_results.mjs";

test("all registered business entry labels match Workspace and never borrow a final action label", () => {
  const schema = JSON.parse(readFileSync(new URL('../../../schemas/agent-host/v1/interaction-presentation.schema.json',import.meta.url)));
  const profiles = schema.properties.display.properties.businessView.properties.profileId.enum;
  assert.equal(profiles.length,29);
  for (const profileId of profiles) {
    let recognized=0;
    for (const viewKind of ['work_log','meeting_room','approval','form','future']) {
      const view = {schemaVersion:'agentbridge.business-view.v1',profileId,profileVersion:'1',viewKind,
        entryLabel:'伪造文案',submitLabel:'确认并批准'};
      const expected=workspaceLabel('business_input',{businessView:view});
      if(expected!=='填写信息') recognized++;
      assert.equal(interactionActionLabel('business_input',{businessView:view}),expected);
      assert.notEqual(expected,'确认并批准');
      assert.equal(interactionActionLabel('business_input',{businessView:{...view,profileVersion:'2'}}),'填写信息');
    }
    assert.equal(recognized,1,profileId);
  }
  for (const profileId of ['__proto__','constructor','unknown']) {
    assert.equal(interactionActionLabel('business_input',{businessView:{schemaVersion:'agentbridge.business-view.v1',profileVersion:'1',profileId}}),'填写信息');
  }
  assert.equal(interactionActionLabel('execution_authorization',null),'核对并确认');
});

test("intellectual-property rewards present acknowledgement with a closed business-specific entry", () => {
  const businessView = {schemaVersion: "agentbridge.business-view.v1", profileVersion: "1",
    profileId: "oa.intellectual_property_reward.acknowledgement", viewKind: "approval",
    entryLabel: "确认并审批通过"};
  for (const label of [interactionActionLabel, workspaceLabel]) {
    assert.equal(label("business_input", {businessView}), "核对知识产权奖励并阅办");
    assert.equal(label("business_input", {businessView: {...businessView,
      profileId: "oa.intellectual_property_reward.approval"}}), "填写信息");
  }
  assert.equal(displayTaskTitle("Prepare OA Intellectual-Property Reward Acknowledgement"), "OA 知识产权奖励阅办");
});

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

test("business settlement review keeps the same closed entry and title in both clients", () => {
  const businessView = {schemaVersion: "agentbridge.business-view.v1", profileVersion: "1",
    profileId: "oa.business_settlement.approval", viewKind: "approval", entryLabel: "直接付款"};
  for (const label of [interactionActionLabel, workspaceLabel]) {
    assert.equal(label("business_input", {businessView}), "核对业务结算单");
    assert.equal(label("business_input", {businessView: {...businessView,
      profileId: "oa.business_settlement.acknowledgement"}}), "填写信息");
  }
  assert.equal(displayTaskTitle("Prepare OA Business-Settlement Approval"), "OA 业务结算单审批");
});
