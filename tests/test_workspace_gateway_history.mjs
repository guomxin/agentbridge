import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { historyEvidenceForRun } from "../agentbridge/workspace/gateway_run_guard.mjs";

const vectors = JSON.parse(readFileSync(new URL("./fixtures/workspace_gateway_history_vectors.json", import.meta.url), "utf8"));
for (const vector of vectors) {
  test(`authoritative history: ${vector.name}`, () => {
    assert.deepEqual(historyEvidenceForRun(vector.payload, vector.runId, vector.sessionKey), vector.expected);
  });
}
