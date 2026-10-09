import test from "node:test";
import assert from "node:assert/strict";

import {
  canRecoverStartup,
  recoveryIdempotencyKey,
  sessionRunState,
} from "../agentbridge/workspace/gateway_run_guard.mjs";


const sessionKey =
  "agent:main:agentbridge-workspace:direct:account-a";

test("session state is idle when the exact session is absent", () => {
  assert.deepEqual(sessionRunState({ sessions: [] }, sessionKey, "run-a"), {
    known: false,
    active: false,
    currentRunActive: false,
    activeRunIds: [],
  });
});

test("session state distinguishes the current run from an old active run", () => {
  const payload = {
    sessions: [
      {
        key: sessionKey,
        hasActiveRun: true,
        activeRunIds: ["old-run"],
      },
    ],
  };
  assert.deepEqual(sessionRunState(payload, sessionKey, "run-a"), {
    known: true,
    active: true,
    currentRunActive: false,
    activeRunIds: ["old-run"],
  });
  assert.equal(
    sessionRunState(payload, sessionKey, "old-run").currentRunActive,
    true,
  );
});

test("startup recovery is allowed only before progress or tool activity", () => {
  const stalled = {
    known: true,
    active: true,
    currentRunActive: false,
    activeRunIds: ["old-run"],
  };
  assert.equal(
    canRecoverStartup({
      progressObserved: false,
      toolActivity: false,
      recoveryUsed: false,
      runState: stalled,
    }),
    true,
  );
  assert.equal(
    canRecoverStartup({
      progressObserved: true,
      toolActivity: false,
      recoveryUsed: false,
      runState: stalled,
    }),
    false,
  );
  assert.equal(
    canRecoverStartup({
      progressObserved: false,
      toolActivity: true,
      recoveryUsed: false,
      runState: stalled,
    }),
    false,
  );
  assert.equal(
    canRecoverStartup({
      progressObserved: false,
      toolActivity: false,
      recoveryUsed: true,
      runState: stalled,
    }),
    false,
  );
});

test("a registered run without real progress can be recovered after abort", () => {
  assert.equal(
    canRecoverStartup({
      progressObserved: false,
      toolActivity: false,
      recoveryUsed: false,
      runState: {
        known: true,
        active: true,
        currentRunActive: true,
        activeRunIds: ["run-a"],
      },
    }),
    true,
  );
});

test("recovery idempotency keys are stable, distinct, and bounded", () => {
  const base = "x".repeat(128);
  const first = recoveryIdempotencyKey(base, 1);
  const repeated = recoveryIdempotencyKey(base, 1);
  const second = recoveryIdempotencyKey(base, 2);
  assert.equal(first, repeated);
  assert.notEqual(first, second);
  assert.notEqual(first, base);
  assert.ok(first.length <= 128);
});
