import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";


const root = resolve(import.meta.dirname, "..");
const helper = join(root, "agentbridge", "workspace", "gateway_client.mjs");
const fake = join(import.meta.dirname, "support", "fake_gateway_websocket.mjs");
const sessionKey =
  "agent:main:agentbridge-workspace:direct:account-a";

function runScenario(scenario, overrides = {}) {
  const state = mkdtempSync(join(tmpdir(), "agentbridge-gateway-test-"));
  const tokenFile = join(state, "gateway.token");
  const traceFile = join(state, "trace.jsonl");
  const identityPath = join(state, "device-identity.json");
  writeFileSync(tokenFile, "g".repeat(48), "utf8");
  const idempotencyKey = "initial-run";
  const completed = spawnSync(
    process.execPath,
    ["--import", pathToFileURL(fake).href, helper],
    {
      cwd: root,
      encoding: "utf8",
      timeout: 15_000,
      input: JSON.stringify({
        mode: "send-stream",
        sessionKey,
        endpointKey: "workspace:account-a",
        grant: "a".repeat(48),
        message: "Read OA pending workflows",
        idempotencyKey,
        preflightAbort: true,
        acceptTimeoutMs: 35_000,
        startupProgressTimeoutMs: 5_000,
        startupActiveGraceMs: 20_000,
        sessionIdleTimeoutMs: 15_000,
        sessionIdlePollMs: 250,
        timeoutMs: 60_000,
        ...overrides,
      }),
      env: {
        ...process.env,
        AB_GATEWAY_URL: "ws://fake-gateway.local",
        AB_GATEWAY_TOKEN: "g".repeat(48),
        AB_GATEWAY_IDENTITY_PATH: identityPath,
        AB_GATEWAY_FAKE_SCENARIO: scenario,
        AB_GATEWAY_FAKE_TRACE: traceFile,
        AB_GATEWAY_INITIAL_RUN_ID: idempotencyKey,
      },
    },
  );
  assert.equal(completed.status, 0, completed.stderr || completed.stdout);
  const events = completed.stdout
    .trim()
    .split(/\r?\n/u)
    .filter(Boolean)
    .map((line) => JSON.parse(line))
    .filter((event) => event.type !== "eof");
  const trace = readFileSync(traceFile, "utf8")
    .trim()
    .split(/\r?\n/u)
    .filter(Boolean)
    .map((line) => JSON.parse(line));
  return { events, trace };
}

test("waits for the previous run to become idle before sending", () => {
  const { events, trace } = runScenario("preflight_wait");
  const methods = trace.map((item) => item.method);
  assert.deepEqual(
    methods.slice(1),
    [
      "chat.abort",
      "sessions.list",
      "sessions.list",
      "agentbridge.workspace.bind",
      "chat.send",
    ],
  );
  assert.deepEqual(
    events.map((event) => event.type),
    ["accepted", "progress", "chat"],
  );
  assert.equal(
    trace.find((item) => item.method === "agentbridge.workspace.bind")
      ?.turnRef,
    "initial-run",
  );
  assert.equal(
    trace.find((item) => item.method === "agentbridge.workspace.bind")
      ?.message,
    "Read OA pending workflows",
  );
});

test("forwards Workspace image attachments to chat.send", () => {
  const { trace } = runScenario("normal", {
    attachments: [
      {
        type: "image",
        mimeType: "image/png",
        fileName: "clipboard.png",
        content: "iVBORw0KGgo=",
      },
    ],
  });
  const send = trace.find((item) => item.method === "chat.send");
  assert.deepEqual(send.attachments, [
    {
      type: "image",
      mimeType: "image/png",
      fileName: "clipboard.png",
      contentLength: 12,
    },
  ]);
});

test("refuses to send while the previous session remains active", () => {
  const { events, trace } = runScenario("preflight_stuck");
  assert.equal(
    trace.filter((item) => item.method === "chat.send").length,
    0,
  );
  assert.equal(events.at(-1).type, "error");
  assert.equal(events.at(-1).error.code, "GATEWAY_SESSION_NOT_IDLE");
});

test("recovers a not-started accepted run exactly once", () => {
  const { events, trace } = runScenario("startup_recovery");
  const sends = trace.filter((item) => item.method === "chat.send");
  const targetedAborts = trace.filter(
    (item) => item.method === "chat.abort" && item.runId,
  );
  assert.equal(sends.length, 2);
  assert.equal(targetedAborts.length, 1);
  assert.equal(targetedAborts[0].runId, "initial-run");
  assert.notEqual(sends[0].idempotencyKey, sends[1].idempotencyKey);
  assert.ok(
    events.some(
      (event) =>
        event.type === "progress" && event.phase === "recovery",
    ),
  );
  const accepted = events.filter((event) => event.type === "accepted");
  assert.equal(accepted.length, 2);
  assert.equal(accepted[1].attempt, 1);
  assert.equal(accepted[1].recoveredFromRunId, "initial-run");
  assert.equal(events.at(-1).state, "final");
});

test("recovers a registered current run that emitted no real progress", () => {
  const { events, trace } = runScenario("startup_active");
  assert.equal(
    trace.filter((item) => item.method === "chat.send").length,
    2,
  );
  assert.equal(
    trace.filter((item) => item.method === "chat.abort" && item.runId).length,
    1,
  );
  assert.equal(
    trace.filter((item) => item.method === "chat.history").length,
    1,
  );
  assert.equal(events.at(-1).state, "final");
});

test("waits for a registered queued run instead of replaying it", () => {
  const { events, trace } = runScenario("startup_queued_active", {
    startupActiveGraceMs: 50_000,
  });
  assert.equal(
    trace.filter((item) => item.method === "chat.send").length,
    1,
  );
  assert.equal(
    trace.filter((item) => item.method === "chat.abort" && item.runId).length,
    0,
  );
  assert.ok(
    events.some(
      (event) => event.type === "progress" && event.phase === "queued",
    ),
  );
  assert.equal(events.at(-1).state, "final");
});

test("recovers an accepted run that disappeared before startup", () => {
  const { events, trace } = runScenario("startup_missing");
  assert.equal(
    trace.filter((item) => item.method === "chat.send").length,
    2,
  );
  assert.equal(
    trace.filter((item) => item.method === "chat.abort" && item.runId).length,
    0,
  );
  assert.equal(
    trace.filter((item) => item.method === "chat.history").length,
    1,
  );
  assert.equal(events.at(-1).state, "final");
});

test("blocks startup replay when session history shows tool activity", () => {
  const { events, trace } = runScenario("startup_tool_activity");
  assert.equal(
    trace.filter((item) => item.method === "chat.send").length,
    1,
  );
  assert.equal(
    trace.filter((item) => item.method === "chat.abort" && item.runId).length,
    1,
  );
  assert.equal(events.at(-1).type, "error");
  assert.equal(
    events.at(-1).error.code,
    "GATEWAY_START_RECOVERY_BLOCKED_TOOL_ACTIVITY",
  );
  assert.equal(events.at(-1).error.details.hadToolActivity, true);
  assert.equal(events.at(-1).error.details.safeToRetry, false);
});

test("uses at most one startup recovery", () => {
  const { events, trace } = runScenario("startup_recovery_stalls_twice");
  assert.equal(
    trace.filter((item) => item.method === "chat.send").length,
    2,
  );
  assert.equal(
    trace.filter((item) => item.method === "chat.abort" && item.runId).length,
    2,
  );
  assert.equal(
    trace.filter((item) => item.method === "chat.history").length,
    1,
  );
  assert.equal(events.at(-1).type, "error");
  assert.equal(
    events.at(-1).error.code,
    "GATEWAY_START_STALLED_ABORTED",
  );
});

test("waits for timeout abort confirmation instead of forwarding its echo", () => {
  const { events } = runScenario("timeout_abort_race");
  assert.equal(
    events.some(
      (event) => event.type === "chat" && event.state === "aborted",
    ),
    false,
  );
  assert.equal(events.at(-1).type, "error");
  assert.equal(events.at(-1).error.code, "GATEWAY_RUN_TIMEOUT_ABORTED");
  assert.equal(events.at(-1).error.details.hadToolActivity, false);
  assert.equal(events.at(-1).error.details.safeToRetry, true);
});

test("reports tool activity when a timed-out run is stopped", () => {
  const { events } = runScenario("timeout_abort_race_with_tool");
  assert.equal(events.at(-1).type, "error");
  assert.equal(events.at(-1).error.code, "GATEWAY_RUN_TIMEOUT_ABORTED");
  assert.equal(events.at(-1).error.details.hadToolActivity, true);
  assert.equal(events.at(-1).error.details.safeToRetry, false);
});

test("starts the execution budget at the first real progress event", () => {
  const { events } = runScenario("late_start", {
    startupProgressTimeoutMs: 55_000,
  });
  assert.equal(events.at(-1).type, "chat");
  assert.equal(events.at(-1).state, "final");
});
