import { randomUUID } from "node:crypto";
import { performance } from "node:perf_hooks";

import { resolveMcpServer } from "./config.js";
import { transportRetryAllowed } from "./recovery-policy.js";
import { recordCurrentTiming, timingMetadata } from "./performance.js";

export class McpCallError extends Error {
  constructor(
    code,
    message = code,
    {
      cause = null,
      transportCode = null,
      retryable = false,
      attempts = 1,
    } = {},
  ) {
    super(message, cause ? { cause } : undefined);
    this.name = "McpCallError";
    this.code = code;
    this.transportCode = transportCode;
    this.retryable = retryable;
    this.attempts = attempts;
  }
}

export function createAgentBridgeMcpClient({
  hostConfig,
  serverName,
  endpoint = null,
  tokenEnv = null,
  fetchImpl = globalThis.fetch,
  env = process.env,
}) {
  const connection = resolveConnection({
    hostConfig,
    serverName,
    endpoint,
    tokenEnv,
    env,
  });
  if (!connection || typeof fetchImpl !== "function") {
    return null;
  }

  return Object.freeze({
    async listTools({ signal } = {}) {
      const result = await request("tools/list", {}, { signal });
      return Array.isArray(result?.tools) ? result.tools : [];
    },
    async callToolResult(name, arguments_, { signal, meta, retry } = {}) {
      return request(
        "tools/call",
        toolCallParams(name, arguments_, meta),
        { signal, retry },
      );
    },
    async callTool(name, arguments_, { signal, meta, retry } = {}) {
      const result = await request(
        "tools/call",
        toolCallParams(name, arguments_, meta),
        { signal, retry },
      );
      return extractToolPayload(result);
    },
  });

  async function request(method, params, { signal, retry } = {}) {
    const startedAt = performance.now();
    let outcome = "completed";
    const policy = normalizeRetryPolicy(retry);
    let attempt = 0;
    let lastError = null;
    let operationId;
    try {
    while (true) {
      attempt += 1;
      try {
        const result = await requestOnce(method, params, { signal, attempt });
        operationId = result?.structuredContent?.operationId || result?.operationId || result?.structuredContent?.operation?.operationId;
        if (attempt > 1) {
          await invokeObserver(policy?.onRecovered, {
            attempts: attempt,
            lastError,
          });
        }
        return result;
      } catch (error) {
        if (error instanceof McpCallError) {
          error.attempts = attempt;
        }
        const delayMs = policy?.delaysMs[attempt - 1];
        if (!transportRetryAllowed({
          retryable: error instanceof McpCallError && error.retryable === true,
          attempt,
          maximumAttempts: (policy?.delaysMs.length || 0) + 1,
          canceled: signal?.aborted === true,
        })) {
          throw error;
        }
        lastError = error;
        await invokeObserver(policy.onRetry, {
          attempt,
          nextAttempt: attempt + 1,
          delayMs,
          error,
        });
        await policy.sleep(delayMs, signal);
      }
    }
    } catch (error) { outcome = "error"; throw error; }
    finally {
      if (params.name !== "agentbridge_host_runtime_snapshot") recordCurrentTiming("host.mcp_request", {
        toolName: params.name, durationMs: performance.now() - startedAt,
        outcome, attempt, operationId,
      }, { taskId: params._meta?.["io.agentbridge/task"]?.taskId });
    }
  }

  async function requestOnce(method, params, { signal, attempt } = {}) {
    const timeoutSignal = AbortSignal.timeout(
      connection.timeoutSeconds * 1000,
    );
    const requestSignal = signal
      ? AbortSignal.any([signal, timeoutSignal])
      : timeoutSignal;
    let response;
    const startedAt = performance.now();
    let timeToFirstByteMs;
    let responseBytes;
    let outcome = "completed";
    const body = JSON.stringify({ jsonrpc: "2.0", id: randomUUID(), method, params });
    try {
    try {
      response = await fetchImpl(connection.url, {
        method: "POST",
        headers: {
          Authorization: connection.authorization,
          Accept: "application/json, text/event-stream",
          "Content-Type": "application/json",
          "MCP-Protocol-Version": "2025-06-18",
        },
        body,
        signal: requestSignal,
      });
      timeToFirstByteMs = performance.now() - startedAt;
    } catch (error) {
      if (requestSignal.aborted) {
        throw new McpCallError(
          "MCP_TIMEOUT",
          "AgentBridge MCP request timed out",
          { cause: error },
        );
      }
      throw new McpCallError(
        "MCP_UNREACHABLE",
        "AgentBridge MCP is unreachable",
        {
          cause: error,
          transportCode: transportErrorCode(error),
          retryable: true,
        },
      );
    }
    if (!response.ok) {
      throw new McpCallError(`MCP_HTTP_${response.status}`, undefined, {
        transportCode: `HTTP_${response.status}`,
        retryable: [502, 503, 504].includes(response.status),
      });
    }
    let rawResponse;
    try {
      rawResponse = await response.text();
      responseBytes = Buffer.byteLength(rawResponse);
    } catch (error) {
      if (requestSignal.aborted) {
        throw new McpCallError(
          "MCP_TIMEOUT",
          "AgentBridge MCP request timed out",
          { cause: error },
        );
      }
      throw new McpCallError(
        "MCP_RESPONSE_READ_FAILED",
        "AgentBridge MCP response could not be read",
        {
          cause: error,
          transportCode: transportErrorCode(error),
          retryable: true,
        },
      );
    }
    const rpc = parseMcpResponse(rawResponse);
    if (rpc.error) {
      throw new McpCallError(
        normalizeErrorCode(rpc.error.code, "MCP_RPC_ERROR"),
        "AgentBridge MCP returned an RPC error",
      );
    }
    return rpc.result;
    } catch (error) { outcome = "error"; throw error; }
    finally {
      if (params.name !== "agentbridge_host_runtime_snapshot") recordCurrentTiming("host.mcp_transport", {
        toolName: params.name, durationMs: performance.now() - startedAt,
        timeToFirstByteMs, requestPayloadBytes: Buffer.byteLength(body),
        responseStreamBytes: responseBytes, outcome, attempt,
      }, { taskId: params._meta?.["io.agentbridge/task"]?.taskId });
    }
  }
}

async function invokeObserver(observer, event) {
  if (typeof observer !== "function") {
    return;
  }
  try {
    await observer(event);
  } catch {
    // Diagnostic observers must never change the business-call outcome.
  }
}

function normalizeRetryPolicy(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const delaysMs = Array.isArray(value.delaysMs)
    ? value.delaysMs
        .filter(
          (item) => Number.isFinite(item) && item >= 0 && item <= 5_000,
        )
        .slice(0, 3)
    : [];
  if (delaysMs.length === 0) {
    return null;
  }
  return {
    delaysMs,
    sleep:
      typeof value.sleep === "function" ? value.sleep : abortableDelay,
    onRetry:
      typeof value.onRetry === "function" ? value.onRetry : null,
    onRecovered:
      typeof value.onRecovered === "function" ? value.onRecovered : null,
  };
}

function abortableDelay(delayMs, signal) {
  if (signal?.aborted) {
    return Promise.reject(
      new McpCallError("MCP_ABORTED", "AgentBridge MCP request was aborted"),
    );
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    const onAbort = () => {
      clearTimeout(timer);
      reject(
        new McpCallError("MCP_ABORTED", "AgentBridge MCP request was aborted"),
      );
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function transportErrorCode(error) {
  let current = error;
  for (let depth = 0; depth < 5 && current; depth += 1) {
    const value = normalizeErrorCode(current.code, "");
    if (value) {
      return value;
    }
    current = current.cause;
  }
  return null;
}

function toolCallParams(name, arguments_, meta) {
  const params = { name, arguments: arguments_ };
  // Timers inherit AsyncLocalStorage. Do not make diagnostic transport create
  // more diagnostic events or attach the last business request to health data.
  const timingMeta = name === "agentbridge_host_runtime_snapshot" ? meta : timingMetadata(meta);
  if (timingMeta && typeof timingMeta === "object" && !Array.isArray(timingMeta)) {
    params._meta = timingMeta;
  }
  return params;
}

function resolveConnection({ hostConfig, serverName, endpoint, tokenEnv, env }) {
  if (endpoint) {
    const token =
      typeof tokenEnv === "string" && typeof env[tokenEnv] === "string"
        ? env[tokenEnv].trim()
        : "";
    if (!token) {
      return null;
    }
    return {
      url: endpoint.url,
      timeoutSeconds: endpoint.timeoutSeconds,
      authorization: `Bearer ${token}`,
    };
  }

  const server = resolveMcpServer(hostConfig, serverName);
  if (!server) {
    return null;
  }
  const authorization = resolveHeader(server.headers, "Authorization", env);
  if (!authorization) {
    return null;
  }
  return {
    url: server.url,
    timeoutSeconds: server.timeoutSeconds,
    authorization,
  };
}

export function parseMcpResponse(raw) {
  const text = String(raw || "").trim();
  if (!text) {
    throw new McpCallError("MCP_EMPTY_RESPONSE");
  }
  if (text.startsWith("{")) {
    try {
      return JSON.parse(text);
    } catch {
      throw new McpCallError("MCP_INVALID_RESPONSE");
    }
  }
  for (const line of text.split(/\r?\n/)) {
    if (!line.startsWith("data:")) {
      continue;
    }
    try {
      return JSON.parse(line.slice(5).trim());
    } catch {
      throw new McpCallError("MCP_INVALID_RESPONSE");
    }
  }
  throw new McpCallError("MCP_INVALID_RESPONSE");
}

export function extractToolPayload(result) {
  let payload = result;
  if (result && typeof result.structuredContent === "object") {
    payload = result.structuredContent;
  } else if (Array.isArray(result?.content)) {
    for (const block of result.content) {
      if (block?.type !== "text" || typeof block.text !== "string") {
        continue;
      }
      const text = block.text.trim();
      if (!text.startsWith("{") && !text.startsWith("[")) {
        continue;
      }
      try {
        payload = JSON.parse(text);
        break;
      } catch {
        continue;
      }
    }
  }
  if (
    payload &&
    typeof payload === "object" &&
    !Array.isArray(payload) &&
    result?._meta &&
    typeof result._meta === "object" &&
    !Array.isArray(result._meta)
  ) {
    return { ...payload, _meta: result._meta };
  }
  return payload;
}

function resolveHeader(headers, name, env) {
  const pair = Object.entries(headers).find(
    ([key]) => key.toLowerCase() === name.toLowerCase(),
  );
  if (!pair || typeof pair[1] !== "string") {
    return null;
  }
  let missing = false;
  const resolved = pair[1].replace(
    /\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g,
    (_match, variable) => {
      const value = env[variable];
      if (typeof value !== "string" || !value) {
        missing = true;
        return "";
      }
      return value;
    },
  );
  return missing || !resolved.trim() ? null : resolved.trim();
}

function normalizeErrorCode(value, fallback) {
  const normalized = String(value ?? "")
    .toUpperCase()
    .replace(/[^A-Z0-9_.-]/g, "_")
    .slice(0, 80);
  return normalized || fallback;
}
