import { cancellationError, abortableDelay } from "./workspace_lifecycle.mjs";

export function createWorkspaceRequests({ fetch = globalThis.fetch, document, getScope, addLiveProgress }) {
  async function api(path, options = {}) {
    const scope = options.scope || getScope();
    const { controller, release } = scope.controller();
    const abort = () => controller.abort();
    if (options.signal?.aborted) controller.abort();
    options.signal?.addEventListener("abort", abort, { once: true });
    try {
    scope.assertCurrent();
    if (controller.signal.aborted) throw cancellationError();
    const headers = { Accept: "application/json" };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (options.csrf) {
      headers["X-AgentBridge-CSRF"] = cookieValue(
        "agentbridge_workspace_csrf",
      );
    }
    const response = await fetch(path, {
      method: options.method || "GET",
      headers,
      credentials: "same-origin",
      signal: controller.signal,
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const payload = await response.json().catch(() => ({}));
    scope.assertCurrent();
    if (controller.signal.aborted) throw cancellationError();
    if (!response.ok) {
      const error = new Error(
        payload?.error?.message || payload?.error?.code || "请求失败",
      );
      error.code = payload?.error?.code;
      throw error;
    }
    return payload;
    } catch (error) {
      if (!scope.current() || controller.signal.aborted) throw cancellationError();
      throw error;
    } finally {
      release();
      options.signal?.removeEventListener("abort", abort);
    }
  }

  function cookieValue(name) {
    const prefix = `${name}=`;
    return (
      document.cookie
        .split(";")
        .map((item) => item.trim())
        .find((item) => item.startsWith(prefix))
        ?.slice(prefix.length) || ""
    );
  }

  async function fetchChatStreamResponse({
    message,
    idempotencyKey,
    attachments,
    activeStream,
  }) {
    const request = {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        "X-AgentBridge-CSRF": cookieValue("agentbridge_workspace_csrf"),
      },
      credentials: "same-origin",
      signal: activeStream.controller.signal,
      body: JSON.stringify({
        message,
        idempotencyKey,
        attachments: attachments.map((item) => ({
          type: "image",
          mimeType: item.mimeType,
          fileName: item.fileName,
          content: item.content,
        })),
      }),
    };
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        if (request.signal.aborted) throw cancellationError();
        const response = await fetch("/api/chat/send-stream", request);
        if (request.signal.aborted) throw cancellationError();
        return response;
      } catch (error) {
        if (request.signal.aborted) throw cancellationError();
        const canRetry =
          attempt === 0 &&
          error instanceof TypeError &&
          !activeStream.controller.signal.aborted;
        if (!canRetry) throw error;
        addLiveProgress(
          idempotencyKey,
          "\u7f51\u7edc\u8fde\u63a5\u77ed\u6682\u4e2d\u65ad\uff0c\u6b63\u5728\u6062\u590d",
          "active",
        );
        await abortableDelay(350, request.signal);
      }
    }
    throw new TypeError("chat stream connection failed");
  }

  return { api, fetchChatStreamResponse };
}
