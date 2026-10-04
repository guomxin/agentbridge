import { cancellationError } from "./workspace_lifecycle.mjs";

export function createChatStream({ state, getScope, fetchChatStreamResponse, adoptLiveMessage, addLiveProgress, ensureLiveMessage, handleChatProgress, handleChatDelta, attachDispatchCancel, agentFailureMessage, renderRunFailure }) {
  async function consumeChatStream({ message, idempotencyKey, attachments = [] }) {
    const scope = getScope().child();
    const { controller } = scope.controller();
    const activeStream = {
      controller,
      requestMessage: message,
      requestAttachments: attachments,
      runIds: new Set(),
      terminal: false,
      timelineCompleted: false,
    };
    registerActiveStream(activeStream, idempotencyKey);
    let runId = idempotencyKey;
    let hadToolActivity = false;
    let terminalFailure = null;
    let streamFailure = null;
    let reader = null;
    const closeReader = () => {
      const current = reader;
      reader = null;
      return current ? current.cancel().catch(() => {}) : Promise.resolve();
    };
    scope.defer(closeReader);
    try {
      const response = await fetchChatStreamResponse({
        message,
        idempotencyKey,
        attachments,
        activeStream,
      });
      scope.assertCurrent();
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const error = new Error(
          payload?.error?.message || payload?.error?.code || "请求失败",
        );
        error.code = payload?.error?.code;
        throw error;
      }
      if (!response.body) {
        const error = new Error("浏览器不支持流式响应");
        error.code = "STREAM_UNAVAILABLE";
        throw error;
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let stopReading = false;
      readLoop:
      for (;;) {
        const { value, done } = await reader.read();
        scope.assertCurrent();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const blocks = buffer.split("\n\n");
        buffer = done ? "" : blocks.pop() || "";
        for (const block of blocks) {
          const event = parseSseBlock(block);
          if (!event) continue;
          if (event.name === "accepted" && event.data.runId) {
            const previousRunId = runId;
            runId = event.data.runId;
            registerActiveStream(activeStream, runId);
            adoptLiveMessage(previousRunId, runId, message);
            addLiveProgress(runId, "请求已交给智能体", "active");
            ensureLiveMessage(runId).actions.replaceChildren();
          } else if (event.name === "progress") {
            if (event.data.kind === "tool") hadToolActivity = true;
            handleChatProgress(event.data);
            if (["queued", "waiting_host"].includes(event.data.phase)) {
              attachDispatchCancel(runId, event.data.dispatchId);
            } else if (
              ["dispatching", "reconciling_acceptance"].includes(
                event.data.phase,
              )
            ) {
              ensureLiveMessage(runId).actions.replaceChildren();
            }
          } else if (event.name === "chat") {
            if (["final", "error", "aborted"].includes(event.data.state)) {
              activeStream.terminal = true;
              stopReading = true;
            }
            if (["error", "aborted"].includes(event.data.state)) {
              terminalFailure = event.data;
            } else {
              handleChatDelta(event.data);
            }
          } else if (event.name === "stream-error") {
            streamFailure = event.data;
            stopReading = true;
          }
          if (stopReading) break readLoop;
        }
        if (done) break;
      }
      if (stopReading && reader) {
        await closeReader();
      }
      if (streamFailure) {
        const code = streamFailure.code || "GATEWAY_STREAM_FAILED";
        const error = new Error(code);
        error.code = code;
        error.runId = runId;
        error.details = streamFailure.details || {};
        error.safeToRetry = streamFailure.safeToRetry === true;
        throw error;
      }
      if (terminalFailure) {
        const effectiveToolActivity =
          hadToolActivity || terminalFailure.hadToolActivity === true;
        const safeToRetry =
          typeof terminalFailure.safeToRetry === "boolean"
            ? terminalFailure.safeToRetry
            : terminalFailure.state === "error" && !effectiveToolActivity;
        const text = agentFailureMessage(
          terminalFailure.text,
          safeToRetry,
          terminalFailure.state,
        );
        renderRunFailure(runId, text, safeToRetry, message, attachments);
        const error = new Error(text);
        error.code =
          terminalFailure.state === "aborted"
            ? "AGENT_RUN_ABORTED"
            : "AGENT_RUN_FAILED";
        error.runId = runId;
        error.rendered = true;
        throw error;
      }
    } catch (error) {
      if (
        activeStream.timelineCompleted &&
        activeStream.controller.signal.aborted
      ) {
        return;
      }
      if (!scope.current()) throw cancellationError();
      throw error;
    } finally {
      await closeReader();
      unregisterActiveStream(activeStream);
      scope.dispose();
    }
  }

  function registerActiveStream(activeStream, runId) {
    if (!runId) return;
    activeStream.runIds.add(runId);
    state.activeStreams.set(runId, activeStream);
  }

  function unregisterActiveStream(activeStream) {
    activeStream.runIds.forEach((runId) => {
      if (state.activeStreams.get(runId) === activeStream) {
        state.activeStreams.delete(runId);
      }
    });
  }

  function parseSseBlock(block) {
    let name = "message";
    const data = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) {
        name = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        data.push(line.slice(5).trimStart());
      }
    }
    if (data.length === 0) return null;
    try {
      const value = JSON.parse(data.join("\n"));
      return value && typeof value === "object"
        ? { name, data: value }
        : null;
    } catch {
      return null;
    }
  }

  return { consumeChatStream, registerActiveStream, unregisterActiveStream, parseSseBlock };
}
