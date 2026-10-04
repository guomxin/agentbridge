import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { transportRecoveryStrategy, transportRetryAllowed, interactionResumeAllowed, resumeClaimAllowed } from "../lib/recovery-policy.js";
import { createAgentBridgeMcpClient } from "../lib/mcp-client.js";
import { InteractionCoordinator } from "../lib/coordinator.js";
import { interaction } from "./fixtures.js";

const vectors = JSON.parse(readFileSync(new URL("../../../schemas/agent-host/v1/test-vectors.json", import.meta.url), "utf8"));

test("OpenClaw consumes shared strategy, budget, interaction and claim vectors", () => {
  for (const v of vectors.recoveryStrategies) assert.equal(transportRecoveryStrategy(v.callClass), v.expected, v.name);
  for (const v of vectors.retryDecisions) assert.equal(transportRetryAllowed(v.value), v.expected, v.name);
  for (const v of vectors.interactionResumeDecisions) assert.equal(interactionResumeAllowed(v.value), v.expected, v.name);
  for (const v of vectors.resumeClaimDecisions) assert.equal(resumeClaimAllowed(v.value), v.expected, v.name);
});

test("MCP retry exhaustion, cancellation and missing policy never exceed the admitted budget", async () => {
  for (const scenario of [
    { name: "bounded read", policy: true, attempts: 3 },
    { name: "unsafe without policy", policy: false, attempts: 1 },
    { name: "canceled", policy: true, canceled: true, attempts: 1 },
  ]) {
    const controller = new AbortController();
    let attempts = 0;
    const client = createAgentBridgeMcpClient({
      endpoint: { url: "https://agentbridge.invalid/mcp", timeoutSeconds: 5 },
      tokenEnv: "FIXTURE", env: { FIXTURE: "fixture" },
      fetchImpl: async () => {
        attempts += 1;
        if (scenario.canceled) controller.abort();
        throw Object.assign(new Error("offline"), { code: "ECONNRESET" });
      },
    });
    await assert.rejects(client.callTool("fixture", {}, {
      signal: controller.signal,
      ...(scenario.policy ? { retry: { delaysMs: [0, 0], sleep: async () => {} } } : {}),
    }));
    assert.equal(attempts, scenario.attempts, scenario.name);
  }
});

function resumeHarness({ failLease = false, canceled = false } = {}) {
  const calls = [];
  const notices = [];
  const controller = new AbortController();
  const coordinator = new InteractionCoordinator({
    api: { logger: { info() {}, warn() {} } }, config: {},
  });
  coordinator.notify = async (_record, state) => notices.push(state);
  const record = {
    taskId: "task-recovery-contract", interaction: interaction({ state: "completed", resume: { ready: true, completed: false } }),
    mcpClient: { async callTool(name) {
      calls.push(name);
      if (name === "agentbridge_host_coordinator_lease_acquire") {
        if (failLease) throw new Error("lease temporarily unavailable");
        return { coordinatorLease: { hostInstanceId: "openclaw-gateway", version: 1 } };
      }
      if (canceled) controller.abort();
      throw new Error("resume reply lost");
    } },
  };
  return { calls, notices, coordinator, record, signal: controller.signal };
}

test("lost or canceled dispatched resume retains its claim against duplicate completion", async () => {
  for (const canceled of [false, true]) {
    const h = resumeHarness({ canceled });
    await h.coordinator.resume(h.record, h.signal);
    await h.coordinator.resume(h.record, new AbortController().signal);
    assert.equal(h.calls.filter(name => name === "agentbridge_interaction_resume").length, 1);
    assert.equal(h.coordinator.resumeClaims.has(h.record.interaction.interactionId), true);
    assert.deepEqual(h.notices, canceled ? [] : ["resume_failed"]);
  }
});

test("failure before dispatch releases the claim while terminal tasks never acquire it", async () => {
  const h = resumeHarness({ failLease: true });
  await h.coordinator.resume(h.record, h.signal);
  assert.equal(h.coordinator.resumeClaims.has(h.record.interaction.interactionId), false);
  assert.equal(h.record.resumeStarted, false);
  h.coordinator.markTaskTerminal(h.record.taskId, "canceled");
  await h.coordinator.resume(h.record, h.signal);
  assert.deepEqual(h.calls, ["agentbridge_host_coordinator_lease_acquire"]);
});
