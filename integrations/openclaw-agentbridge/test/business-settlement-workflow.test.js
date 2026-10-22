import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { staticToolScope, boundToolScope } from "../lib/tool-scopes.js";
import { AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES, isIndependentTaskEntryTool } from "../lib/proxy-tools.js";
import { businessViewForInteraction, interactionEntryLabel } from "../lib/interaction-business-view.js";

const prepare = "oa_business_settlement_approval_prepare";

test("business settlement enters the governed OA pending scope without exposing a direct commit", () => {
  const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url)));
  assert.equal(manifest.contracts.tools.includes(prepare), true);
  assert.equal(AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES.includes(prepare), true);
  assert.equal(isIndependentTaskEntryTool(prepare), true);
  for (const scope of [staticToolScope("oa_pending"), ...["single", "batch"].map(profile =>
    boundToolScope({skillId: "oa-pending", profile}))]) {
    assert.equal(scope.has(prepare), true);
    assert.equal(scope.has("oa_business_settlement_approve"), false);
    assert.equal(scope.has("oa_business_settlement_acknowledgement_prepare"), false);
  }
});

test("settlement approval uses its own static review entry for fields and authorization", () => {
  const businessView = {schemaVersion: "agentbridge.business-view.v1", profileVersion: "1",
    viewKind: "approval", profileId: "oa.business_settlement.approval", entryLabel: "直接付款"};
  assert.deepEqual(businessViewForInteraction({type: "business_input", display: {businessView}}), {
    profileId: "oa.business_settlement.approval", viewKind: "approval", entryLabel: "核对业务结算单"});
  assert.equal(businessViewForInteraction({type: "business_input", display: {
    businessView: {...businessView, profileId: "oa.business_settlement.acknowledgement"}}}), null);
  assert.equal(interactionEntryLabel({type: "execution_authorization", display: {actionView: {
    schemaVersion: "agentbridge.action-view.v1", capabilityId: "oa.business_settlement.approve",
    entryLabel: "直接付款"}}}), "核对业务结算单");
});
