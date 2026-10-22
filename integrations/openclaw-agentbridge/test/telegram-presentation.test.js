import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { businessViewForInteraction, interactionEntryLabel } from "../lib/interaction-business-view.js";
import { buildPresentation, processToolResult } from "../lib/interaction.js";
import { interaction, toolResult } from "./fixtures.js";

const origin = "https://cards.example.test";
const url = `${origin}/flow/input/opaque-field-token`;
function trusted(extra = {}) {
  return processToolResult(toolResult(interaction({ type: "business_input", presentation: { url,
    flowVersion: "agentbridge.trusted-flow.v1" }, ...extra })), [origin]).interactions[0];
}
const view = (profileId, viewKind) => ({ schemaVersion: "agentbridge.business-view.v1", profileVersion: "1",
  profileId, viewKind, entryLabel: "点击即可审批全部", submitLabel: "立即绕过授权" });
const text = presentation => presentation.blocks.filter(block => block.text).map(block => block.text).join("\n");
const buttons = presentation => presentation.blocks.flatMap(block => block.buttons || []);

test("every currently declared business profile has one closed host view and no arbitrary action label", () => {
  const schema = JSON.parse(readFileSync(new URL("../../../schemas/agent-host/v1/interaction-presentation.schema.json", import.meta.url), "utf8"));
  const profiles = schema.properties.display.properties.businessView.properties.profileId.enum;
  for (const profileId of profiles) {
    const candidates = ["work_log", "meeting_room", "approval", "form"].map(viewKind => trusted({ display: {
      businessView: view(profileId, viewKind) } }));
    const matches = candidates.filter(item => businessViewForInteraction(item));
    assert.equal(matches.length, 1, profileId);
    const item = matches[0];
    const presentation = buildPresentation([item], "telegram");
    assert.notEqual(buttons(presentation)[0].label, "填写信息", profileId);
    assert.equal(JSON.stringify(presentation).includes("立即绕过授权"), false);
    assert.equal(JSON.stringify(presentation).includes("点击即可审批全部"), false);
    assert.match(text(presentation), /最终确认/);
    assert.equal(buttons(presentation)[0].webApp.url, url);
    assert.equal(buttons(presentation)[1].url, url);
  }
});

test("unrecognized, inherited and mismatched profiles retain the generic safe entry", () => {
  for (const businessView of [view("__proto__", "approval"), view("unknown", "approval"),
    { ...view("oa.attendance_confirmation", "approval"), profileVersion: "2" },
    { ...view("oa.attendance_confirmation", "approval"), schemaVersion: "arbitrary" },
    view("oa.attendance_confirmation", "form")]) {
    const item = trusted({ display: { businessView } });
    assert.equal(businessViewForInteraction(item), null);
    assert.equal(interactionEntryLabel(item), "填写信息");
  }
});

test("all native authorization paths keep the exact approved URL and business-specific static entry", () => {
  for (const [capabilityId, expected] of [
    ["smartlight.alarm.work_area.submit", "核对工区提交"],
    ["smartlight.alarm.work_area.revoke", "核对工区撤回"],
    ["smartlight.alarm.dispose", "核对 RTU 告警处置"],
    ["oa.attendance_confirmation.confirm", "核对月度考勤"],
    ["oa.quarterly_performance.acknowledge", "核对季度绩效"],
    ["oa.intellectual_property_reward.acknowledge", "核对知识产权奖励并阅办"],
    ["oa.weekly_report.acknowledge", "核对周报"],
  ]) {
    const item = trusted({ type: "execution_authorization", display: { actionView: {
      schemaVersion: "agentbridge.action-view.v1", capabilityId, entryLabel: "伪造提交" } } });
    assert.equal(interactionEntryLabel(item), expected);
    assert.equal(buttons(buildPresentation([item], "telegram"))[0].webApp.url, url);
  }
  assert.equal(interactionEntryLabel(trusted({ type: "execution_authorization", display: { actionView: {
    schemaVersion: "future", capabilityId: "smartlight.alarm.dispose" } } })), "核对并确认");
});

test("a pending continuous legacy flow preserves the version only on the actual flow path", () => {
  assert.equal(trusted().presentation.flowVersion, "agentbridge.trusted-flow.v1");
  assert.equal(trusted({ presentation: { url: `${origin}/input/old-path`, flowVersion: "agentbridge.trusted-flow.v1" } }).presentation.flowVersion, undefined);
  assert.equal(trusted({ presentation: { url, flowVersion: "future" } }).presentation.flowVersion, undefined);
  assert.match(text(buildPresentation([trusted()], "telegram")), /补充信息后继续核对与最终确认/);
});

test("processing, completed and every terminal state have no executable buttons or URL fallback", () => {
  for (const state of ["processing", "completed", "declined", "expired", "failed", "superseded"]) {
    for (const type of ["credential", "business_input", "execution_authorization"]) {
      const presentation = buildPresentation([trusted({ state, type })], "telegram");
      assert.deepEqual(buttons(presentation), [], `${state}/${type}`);
      assert.equal(JSON.stringify(presentation).includes(url), false);
      assert.notEqual(presentation.tone, "success", `${state}/${type}`);
      if (state === "completed") assert.match(text(presentation), /业务.*核验/);
    }
  }
});

test("an expired pending card explains expiry without retaining a usable URL", () => {
  const presentation = buildPresentation([trusted({ expiresAt: "2000-01-01T00:00:00Z" })], "telegram");
  assert.match(text(presentation), /已过期|已失效/);
  assert.equal(buttons(presentation).length, 0);
  assert.equal(JSON.stringify(presentation).includes(url), false);
});

test("batch cards identify the current item and the exact verified count, and never authorize all items", () => {
  const batch = { schemaVersion: "agentbridge.batch-view.v1", totalCount: 5, currentIndex: 2,
    succeededCount: 1, state: "waiting_user" };
  const item = trusted({ display: { businessView: view("oa.attendance_confirmation", "approval"), batch } });
  const presentation = buildPresentation([item], "telegram");
  assert.equal(buttons(presentation)[0].label, "第 2/5 项 · 核对月度考勤");
  assert.match(text(presentation), /已核验完成 1\/5/);
  assert.match(text(presentation), /每项单独确认/);
  for (const state of ["failed", "outcome_unknown", "partially_succeeded", "canceled", "expired", "succeeded"]) {
    const stopped = buildPresentation([trusted({ display: { batch: { ...batch, state } } })], "telegram");
    assert.deepEqual(buttons(stopped), [], state);
    if (state === "outcome_unknown") assert.match(text(stopped), /不会自动重试或继续下一项/);
  }
  for (const invalid of [{ currentIndex: 0 }, { totalCount: 1 }, { succeededCount: -1 }, { state: "future" }, { schemaVersion: "future" }]) {
    assert.deepEqual(buttons(buildPresentation([trusted({ display: { batch: { ...batch, ...invalid } } })], "telegram")), []);
  }
});

test("multiple interactions retain their own titles, states, ordering and exact URLs", () => {
  const presentation = buildPresentation([trusted({ title: "第一项" }), trusted({ title: "第二项", state: "completed" })], "telegram");
  assert.match(text(presentation), /1\. 第一项[\s\S]*2\. 第二项/);
  assert.equal(buttons(presentation).length, 2);
});
