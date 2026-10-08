import test from "node:test";
import assert from "node:assert/strict";

import { normalizeGatewayEvent } from "../bscli/workspace/gateway_events.mjs";

const sessionKey =
  "agent:main:agentbridge-workspace:direct:account-a";

test("filters events from other Workspace sessions", () => {
  const event = normalizeGatewayEvent(
    {
      type: "event",
      event: "chat",
      payload: {
        sessionKey:
          "agent:main:agentbridge-workspace:direct:account-b",
        runId: "run-b",
        state: "final",
        message: {
          role: "assistant",
          content: [{ type: "text", text: "private user B result" }],
        },
      },
    },
    sessionKey,
  );

  assert.equal(event, null);
});

test("filters another run in the same Workspace session", () => {
  const event = normalizeGatewayEvent(
    {
      type: "event",
      event: "chat",
      payload: {
        sessionKey,
        runId: "run-b",
        state: "final",
        message: {
          role: "assistant",
          content: [{ type: "text", text: "another run" }],
        },
      },
    },
    sessionKey,
    "run-a",
  );

  assert.equal(event, null);
});

test("keeps public chat text and strips message metadata", () => {
  const event = normalizeGatewayEvent(
    {
      type: "event",
      event: "chat",
      payload: {
        sessionKey,
        runId: "run-a",
        seq: 4,
        state: "delta",
        message: {
          role: "assistant",
          content: [{ type: "text", text: "正在读取 OA 待办" }],
          privateMetadata: { bearer: "must-not-leak" },
        },
      },
    },
    sessionKey,
  );

  assert.deepEqual(event, {
    type: "chat",
    runId: "run-a",
    state: "delta",
    seq: 4,
    text: "正在读取 OA 待办",
  });
  assert.equal(JSON.stringify(event).includes("must-not-leak"), false);
});

test("maps tool progress without exposing arguments or results", () => {
  const event = normalizeGatewayEvent(
    {
      type: "event",
      event: "agent",
      payload: {
        sessionKey,
        runId: "run-a",
        stream: "tool",
        data: {
          phase: "start",
          name: "oa_workflow_pending_list",
          args: { password: "secret", userSubject: "user-a" },
          result: { token: "secret-token" },
        },
      },
    },
    sessionKey,
  );

  assert.deepEqual(event, {
    type: "progress",
    runId: "run-a",
    kind: "tool",
    phase: "start",
    label: "正在调用 OA 能力",
  });
  assert.equal(JSON.stringify(event).includes("secret"), false);
});

test("final snapshots always replace abandoned prefixes", () => {
  const normalized = normalizeGatewayEvent({ type: "event", event: "chat", payload: {
    sessionKey, runId: "run-a", state: "final", replace: false,
    message: { role: "assistant", content: "Repeat\nRepeat" },
  } }, sessionKey, "run-a");
  assert.equal(normalized.replace, true);
  assert.equal(normalized.text, "Repeat\nRepeat");
});
