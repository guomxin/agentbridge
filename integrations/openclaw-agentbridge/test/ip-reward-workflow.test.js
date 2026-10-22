import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { staticToolScope, boundToolScope } from "../lib/tool-scopes.js";
import { AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES, isIndependentTaskEntryTool } from "../lib/proxy-tools.js";
import { businessViewForInteraction } from "../lib/interaction-business-view.js";

const prepare = "oa_intellectual_property_reward_acknowledgement_prepare";

test("reward acknowledgement prepares are admitted to the existing OA pending skill and host entry", () => {
  const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url)));
  assert.equal(manifest.contracts.tools.includes(prepare), true);
  assert.equal(AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES.includes(prepare), true);
  assert.equal(isIndependentTaskEntryTool(prepare), true);
  for (const scope of [staticToolScope("oa_pending"), ...["single", "batch"].map(profile =>
    boundToolScope({skillId: "oa-pending", profile}))]) {
    assert.equal(scope.has(prepare), true);
    assert.equal(scope.has("oa_intellectual_property_reward_acknowledge"), false);
    assert.equal(scope.has("oa_intellectual_property_reward_approval_prepare"), false);
  }
});

test("reward acknowledgement uses the established approval view without admitting an invented approval capability", () => {
  const interaction = {type: "business_input", display: {businessView: {
    schemaVersion: "agentbridge.business-view.v1", profileVersion: "1", viewKind: "approval",
    profileId: "oa.intellectual_property_reward.acknowledgement", entryLabel: "确认审批通过"}}};
  assert.deepEqual(businessViewForInteraction(interaction), {
    profileId: "oa.intellectual_property_reward.acknowledgement", viewKind: "approval",
    entryLabel: "核对知识产权奖励并阅办"});
  interaction.display.businessView.profileId = "oa.intellectual_property_reward.approval";
  assert.equal(businessViewForInteraction(interaction), null);
});
