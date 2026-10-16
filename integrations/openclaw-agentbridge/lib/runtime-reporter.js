import { hostContextMeta } from "./host-contract.js";

export function createHostRuntimeReporter({
  identityRouter,
  coordinator,
  performanceRecorder = null,
  taskCommandRunner = null,
  logger = null,
  intervalMs = 60_000,
  initialDelayMs = 5_000,
  now = () => Date.now(),
  sleep = backgroundSleep,
} = {}) {
  const startedAt = now();
  let controller = null;
  let loop = null;
  let transportErrorCount = 0;
  let lastErrorCode = null;
  let timingTimer = null;
  let collecting = false;

  async function collectOnce({ signal, timingsOnly = false } = {}) {
    if (collecting) return [];
    collecting = true;
    try {
    const counts = coordinator?.hostRuntimeCounts?.() || {
      activeTaskCount: 0,
      waitingInteractionCount: 0,
    };
    const results = [];
    for (const { binding, client } of identityRouter?.configuredIdentities?.() || []) {
      const timingEvents = performanceRecorder?.take(binding.key, 32) || [];
      if (timingsOnly && timingEvents.length === 0) continue;
      const snapshot = {
        status: "healthy",
        observedAt: new Date(now()).toISOString(),
        uptimeSeconds: Math.max((now() - startedAt) / 1000, 0),
        activeTaskCount: counts.activeTaskCount,
        waitingInteractionCount: counts.waitingInteractionCount,
        transportErrorCount,
        ...(lastErrorCode ? { lastErrorCode } : {}),
      };
      if (timingEvents.length) snapshot.timingEvents = timingEvents;
      try {
        const response = await client.callTool(
          "agentbridge_host_runtime_snapshot",
          { snapshot },
          { signal, meta: hostContextMeta() },
        );
        if (
          !response ||
          response.status !== "succeeded" ||
          response.error
        ) {
          const error = new Error("AgentBridge rejected the runtime snapshot");
          error.code =
            response?.error?.code || "HOST_RUNTIME_SIGNAL_REJECTED";
          throw error;
        }
        results.push({ bindingKey: binding.key, response });
        identityRouter.rememberCatalogVersions?.(binding, response);
        taskCommandRunner?.recover(response.pendingCommands, { binding, client });
        if (/^(?:sha256:)?[a-f0-9]{64}$/u.test(response.permissionVersion || "")
            && identityRouter.permissionVersionForBinding?.(binding) !== response.permissionVersion) {
          await identityRouter.refreshIdentityProfiles?.({
            bindingKey: binding.key, logger, signal,
          });
        }
      } catch (error) {
        transportErrorCount += 1;
        lastErrorCode = safeErrorCode(error);
        logger?.warn?.(
          `AgentBridge host runtime snapshot failed for ${binding.label || binding.key} (${lastErrorCode}; transport=${safeErrorCode({ code: error.transportCode })})`,
        );
        results.push({ bindingKey: binding.key, errorCode: lastErrorCode });
      }
    }
    return results;
    } finally { collecting = false; }
  }

  function start() {
    if (controller || !identityRouter?.enabled) {
      return false;
    }
    controller = new AbortController();
    performanceRecorder?.onPending(scheduleTimings);
    if (performanceRecorder?.status().queued) scheduleTimings();
    loop = run(controller.signal);
    loop.catch((error) => {
      if (!controller?.signal.aborted) {
        logger?.warn?.(
          `AgentBridge host runtime reporter stopped (${safeErrorCode(error)})`,
        );
      }
    });
    return true;
  }

  async function run(signal) {
    await sleep(Math.max(Number(initialDelayMs) || 0, 0), signal);
    while (!signal.aborted) {
      await collectOnce({ signal });
      await sleep(Math.max(Number(intervalMs) || 60_000, 15_000), signal);
    }
  }

  function stop() {
    performanceRecorder?.onPending(null);
    clearTimeout(timingTimer);
    timingTimer = null;
    controller?.abort();
    controller = null;
  }

  function scheduleTimings() {
    if (!controller || timingTimer) return;
    timingTimer = setTimeout(async () => {
      timingTimer = null;
      if (!controller) return;
      await collectOnce({ signal: controller.signal, timingsOnly: true });
      if (performanceRecorder?.status().queued) scheduleTimings();
    }, 1_000);
    timingTimer.unref?.();
  }

  async function waitForIdle() {
    await loop;
  }

  return {
    collectOnce,
    start,
    stop,
    waitForIdle,
    status() {
      return {
        running: Boolean(controller),
        transportErrorCount,
        lastErrorCode,
      };
    },
  };
}

function safeErrorCode(error) {
  const value = error?.code || error?.name || "HOST_RUNTIME_SIGNAL_FAILED";
  return String(value)
    .toUpperCase()
    .replace(/[^A-Z0-9]+/gu, "_")
    .replace(/^_+|_+$/gu, "")
    .slice(0, 120);
}

function backgroundSleep(milliseconds, signal) {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(resolve, milliseconds);
    timer.unref?.();
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}
