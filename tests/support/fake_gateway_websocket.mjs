import { appendFileSync } from "node:fs";


const realSetTimeout = globalThis.setTimeout;
const realDateNow = Date.now.bind(Date);
const clockStartedAt = realDateNow();
Date.now = () => clockStartedAt + (realDateNow() - clockStartedAt) * 100;
globalThis.setTimeout = (callback, delay, ...args) =>
  realSetTimeout(callback, Math.max(1, Math.round(Number(delay) / 100)), ...args);

const scenario = process.env.AB_GATEWAY_FAKE_SCENARIO || "normal";
const tracePath = process.env.AB_GATEWAY_FAKE_TRACE;
let sendCount = 0;
let targetedAbortCount = 0;
let sessionListCount = 0;
let lastRunId = null;

function trace(request) {
  if (!tracePath) return;
  appendFileSync(
    tracePath,
    `${JSON.stringify({
      requestId: request.id,
      method: request.method,
      runId: request.params?.runId ?? null,
      idempotencyKey: request.params?.idempotencyKey ?? null,
      turnRef: request.params?.turnRef ?? null,
      message: request.params?.message ?? null,
      attachments: Array.isArray(request.params?.attachments)
        ? request.params.attachments.map((attachment) => ({
            type: attachment?.type ?? null,
            mimeType: attachment?.mimeType ?? null,
            fileName: attachment?.fileName ?? null,
            contentLength:
              typeof attachment?.content === "string"
                ? attachment.content.length
                : 0,
          }))
        : [],
    })}\n`,
    "utf8",
  );
}

class FakeWebSocket {
  constructor() {
    this.listeners = new Map();
    this.closed = false;
    queueMicrotask(() =>
      this.emit("message", {
        data: JSON.stringify({
          type: "event",
          event: "connect.challenge",
          payload: { nonce: "fake-nonce" },
        }),
      }),
    );
  }

  addEventListener(name, handler) {
    const handlers = this.listeners.get(name) ?? [];
    handlers.push(handler);
    this.listeners.set(name, handlers);
  }

  send(raw) {
    const request = JSON.parse(String(raw));
    trace(request);
    if (request.method === "connect") {
      this.respond(request, { protocol: 4 });
      return;
    }
    if (request.method === "chat.abort") {
      if (request.params?.runId) targetedAbortCount += 1;
      if (
        request.params?.runId &&
        scenario.startsWith("timeout_abort_race")
      ) {
        this.emitChatAbort(
          request.params.sessionKey,
          request.params.runId,
        );
      }
      this.respond(request, {
        ok: true,
        aborted: true,
        runIds: request.params?.runId
          ? [request.params.runId]
          : ["old-run"],
      });
      return;
    }
    if (request.method === "chat.history") {
      if (request.id.startsWith("final-history-")) {
        const payload = { sessionKey: request.params.sessionKey, messages: [
          { role: "user", idempotencyKey: `${lastRunId}:user` },
          { role: "assistant", content: "persisted final", stopReason: "stop" },
        ] };
        if (scenario === "final_wrong_session") payload.sessionKey = "other-session";
        if (scenario === "final_forged_remote") payload.sessionKey = "other-session";
        if (scenario === "final_wrong_run") payload.messages[0].idempotencyKey = "old-run:user";
        if (scenario === "final_late_tool") payload.messages.push({ role: "tool", content: "later tool" });
        if (scenario === "final_wrong_response_id") this.respond({ ...request, id: "wrong-id" }, payload);
        else if (scenario === "final_history_error") this.respondError(request, "HISTORY_UNAVAILABLE", "history unavailable");
        else if (scenario === "final_close") this.emit("close", { reason: "closed during final read" });
        else this.respond(request, payload);
        return;
      }
      this.respond(request, this.chatHistoryPayload());
      return;
    }
    if (request.method === "sessions.list") {
      sessionListCount += 1;
      this.respond(request, this.sessionListPayload(request));
      return;
    }
    if (request.method === "agentbridge.workspace.bind") {
      this.respond(request, { ok: true, ...(scenario === "trial_bound" ? {trialBound:true} : {}) });
      return;
    }
    if (request.method === "chat.send") {
      sendCount += 1;
      const runId = request.params.idempotencyKey;
      lastRunId = runId;
      this.respond(request, { status: "started", runId }, () => {
        if (
          scenario === "normal" || scenario === "trial_bound" ||
          scenario.startsWith("final_") ||
          scenario === "preflight_wait" ||
          ([
            "startup_recovery",
            "startup_active",
            "startup_missing",
          ].includes(scenario) &&
            sendCount > 1)
        ) {
          this.emitRunCompletion(request.params.sessionKey, runId);
        } else if (scenario.startsWith("timeout_abort_race")) {
          this.emitRunStart(
            request.params.sessionKey,
            runId,
            scenario.endsWith("_with_tool"),
          );
        } else if (scenario === "late_start") {
          realSetTimeout(
            () => this.emitRunStart(request.params.sessionKey, runId),
            400,
          );
          realSetTimeout(
            () => this.emitRunFinal(request.params.sessionKey, runId),
            750,
          );
        } else if (scenario === "startup_queued_active") {
          realSetTimeout(
            () => this.emitRunStart(request.params.sessionKey, runId),
            400,
          );
          realSetTimeout(
            () => this.emitRunFinal(request.params.sessionKey, runId),
            500,
          );
        }
      });
      return;
    }
    this.respondError(request, "UNEXPECTED_METHOD", request.method);
  }

  close() {
    this.closed = true;
  }

  sessionListPayload(request) {
    const key = request.params.search;
    if (scenario === "preflight_wait" && sessionListCount === 1) {
      return {
        sessions: [
          { key, hasActiveRun: true, activeRunIds: ["old-run"] },
        ],
      };
    }
    if (scenario === "preflight_stuck" && sendCount === 0) {
      return {
        sessions: [
          { key, hasActiveRun: true, activeRunIds: ["old-run"] },
        ],
      };
    }
    if (
      [
        "startup_recovery",
        "startup_active",
        "startup_queued_active",
        "startup_tool_activity",
        "startup_recovery_stalls_twice",
      ].includes(scenario) &&
      sendCount > targetedAbortCount
    ) {
      return {
        sessions: [
          { key, hasActiveRun: true, activeRunIds: [lastRunId] },
        ],
      };
    }
    return { sessions: [{ key, hasActiveRun: false }] };
  }

  chatHistoryPayload() {
    if (scenario !== "startup_tool_activity") {
      return { messages: [] };
    }
    return {
      messages: [
        {
          role: "user",
          content: "write something",
          idempotencyKey: `${lastRunId}:user`,
        },
        {
          role: "assistant",
          content: [
            {
              type: "toolCall",
              id: "tool-call-1",
              name: "oa_leave_submit",
              arguments: {},
            },
          ],
        },
      ],
    };
  }

  emitRunStart(sessionKey, runId, withTool = false) {
    this.emit("message", {
      data: JSON.stringify({
        type: "event",
        event: "agent",
        payload: {
          sessionKey,
          runId,
          stream: "lifecycle",
          data: { phase: "start" },
        },
      }),
    });
    if (!withTool) return;
    this.emit("message", {
      data: JSON.stringify({
        type: "event",
        event: "agent",
        payload: {
          sessionKey,
          runId,
          stream: "tool",
          data: { phase: "start", name: "oa_workflow_pending_list" },
        },
      }),
    });
  }

  emitChatAbort(sessionKey, runId) {
    this.emit("message", {
      data: JSON.stringify({
        type: "event",
        event: "chat",
        payload: { sessionKey, runId, state: "aborted" },
      }),
    });
  }

  emitRunCompletion(sessionKey, runId) {
    this.emitRunStart(sessionKey, runId);
    this.emitRunFinal(sessionKey, runId);
  }

  emitRunFinal(sessionKey, runId) {
    this.emit("message", {
      data: JSON.stringify({
        type: "event",
        event: "chat",
        payload: {
          sessionKey,
          runId,
          state: "final",
          message: {
            role: "assistant",
            content: [{ type: "text", text: "done" }],
          },
          ...(scenario === "final_forged_remote" ? { agentbridgeFinalHistory: {
            schemaVersion: "agentbridge.gateway-final.v1", source: "local-same-socket-history",
            sessionKey, runId, observedRunId: runId,
          }, authoritative: true, historyVerified: true } : {}),
        },
      }),
    });
  }

  respond(request, payload, after = null) {
    queueMicrotask(() => {
      this.emit("message", {
        data: JSON.stringify({
          type: "res",
          id: request.id,
          ok: true,
          payload,
        }),
      });
      if (after) queueMicrotask(after);
    });
  }

  respondError(request, code, message) {
    queueMicrotask(() =>
      this.emit("message", {
        data: JSON.stringify({
          type: "res",
          id: request.id,
          ok: false,
          error: { code, message },
        }),
      }),
    );
  }

  emit(name, event) {
    if (this.closed) return;
    for (const handler of this.listeners.get(name) ?? []) handler(event);
  }
}

globalThis.WebSocket = FakeWebSocket;
