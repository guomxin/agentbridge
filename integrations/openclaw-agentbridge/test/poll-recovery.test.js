import test from "node:test";
import assert from "node:assert/strict";
import { InteractionCoordinator } from "../lib/coordinator.js";
import { CARD_ORIGIN, interaction, toolResult } from "./fixtures.js";

function harness({ retryable = true, recover = true, maxPollSeconds = 300 } = {}) {
  let now = Date.parse("2026-08-31T00:00:00Z");
  const calls = [];
  const notices = [];
  const delays = [];
  const client = {
    async callTool(name) {
      calls.push(name);
      if (!recover || calls.length <= 6) {
        throw Object.assign(new Error("transport unavailable"), {
          code: "MCP_UNREACHABLE", transportCode: "ECONNRESET", retryable,
        });
      }
      return toolResult(interaction({
        state: "completed", resume: { ready: true, completed: false },
      }));
    },
  };
  const coordinator = new InteractionCoordinator({
    api: { logger: { info() {}, warn() {} } },
    config: { pollIntervalSeconds: 2, maxPollSeconds, allowedCardOrigins: [CARD_ORIGIN] },
    now: () => now,
    sleep: async (ms) => { delays.push(ms); now += ms; },
  });
  coordinator.notify = async (_record, status) => notices.push(status);
  coordinator.resume = async () => notices.push("resumed");
  return {
    calls, notices, delays,
    run: () => coordinator.poll({ interaction: interaction(), mcpClient: client }, new AbortController().signal),
  };
}

test("transient status-query errors beyond five attempts recover without replaying business tools", async () => {
  const h = harness();
  await h.run();
  assert.equal(h.calls.length, 7);
  assert.ok(h.calls.every((name) => name === "agentbridge_interaction_get"));
  assert.ok(h.delays.includes(30_000));
  assert.deepEqual(h.notices, ["resumed"]);
});

test("non-retryable poll errors still stop at the existing error limit", async () => {
  const h = harness({ retryable: false });
  await h.run();
  assert.equal(h.calls.length, 5);
  assert.deepEqual(h.notices, ["poll_failed"]);
});

test("persistent transport failure stops at the bounded polling deadline", async () => {
  const h = harness({ recover: false, maxPollSeconds: 45 });
  await h.run();
  assert.equal(h.calls.length, 6);
  assert.deepEqual(h.notices, ["poll_expired"]);
});

test("pending interaction waits immediately on the server and resumes without an initial timer", async () => {
  const calls = [];
  const sleeps = [];
  let resumes = 0;
  const coordinator = new InteractionCoordinator({ api: { logger: { info() {}, warn() {} } },
    config: { pollIntervalSeconds: 2, maxPollSeconds: 300, allowedCardOrigins: [CARD_ORIGIN] },
    sleep: async ms => { sleeps.push(ms); } });
  coordinator.resume = async () => { resumes += 1; };
  await coordinator.poll({ interaction: interaction(), mcpClient: { async callTool(name, args) {
    calls.push({ name, args });
    return { ...toolResult(interaction({ state: "completed", resume: { ready: true, completed: false } })),
      waitSupported: true, waitedMs: 1200 };
  } } }, new AbortController().signal);
  assert.equal(calls[0].args.wait_seconds, 20);
  assert.deepEqual(sleeps, []);
  assert.equal(resumes, 1);
});

test("interaction waiting falls back without unsupported arguments after an old-server rejection", async () => {
  const calls = [];
  const sleeps = [];
  let resumes = 0;
  const coordinator = new InteractionCoordinator({ api: { logger: { info() {}, warn() {} } },
    config: { pollIntervalSeconds: 2, maxPollSeconds: 300, allowedCardOrigins: [CARD_ORIGIN] },
    sleep: async ms => { sleeps.push(ms); } });
  coordinator.resume = async () => { resumes += 1; };
  await coordinator.poll({ interaction: interaction(), mcpClient: { async callTool(_name, args) {
    calls.push(args);
    if (args.wait_seconds) throw new Error("unsupported wait");
    return toolResult(interaction({ state: "completed", resume: { ready: true, completed: false } }));
  } } }, new AbortController().signal);
  assert.equal(calls[0].wait_seconds, 20);
  assert.equal(calls[1].wait_seconds, undefined);
  assert.deepEqual(sleeps, [2000]);
  assert.equal(resumes, 1);
});

test("long-poll wait is bounded by expiry and an expired result cannot resume", async () => {
  let now = Date.parse("2026-10-16T00:00:00Z");
  const args = [];
  const notices = [];
  const current = interaction({ expiresAt: new Date(now + 1500).toISOString() });
  const coordinator = new InteractionCoordinator({ api: { logger: { info() {}, warn() {} } }, now: () => now,
    config: { pollIntervalSeconds: 2, maxPollSeconds: 300, allowedCardOrigins: [CARD_ORIGIN] } });
  coordinator.notify = async (_record, status) => notices.push(status);
  coordinator.resume = async () => { throw new Error("must not resume expiry"); };
  await coordinator.poll({ interaction: current, mcpClient: { async callTool(_name, params) {
    args.push(params); now += 1500;
    return { ...toolResult(interaction({ ...current, state: "expired", resume: { ready: false, completed: false } })),
      waitSupported: true, waitedMs: 1500 };
  } } }, new AbortController().signal);
  assert.equal(args[0].wait_seconds, 1);
  assert.deepEqual(notices, ["expired"]);
});

test("an aborted long poll never dispatches a late completed response", async () => {
  const controller = new AbortController();
  let resumed = false;
  const coordinator = new InteractionCoordinator({ api: { logger: { info() {}, warn() {} } },
    config: { pollIntervalSeconds: 2, maxPollSeconds: 300, allowedCardOrigins: [CARD_ORIGIN] } });
  coordinator.resume = async () => { resumed = true; };
  await coordinator.poll({ interaction: interaction(), mcpClient: { async callTool() {
    controller.abort();
    return toolResult(interaction({ state: "completed", resume: { ready: true, completed: false } }));
  } } }, controller.signal);
  assert.equal(resumed, false);
});
