import { processToolResult } from "./interaction.js";
import { hostContextMeta, TASK_CONTEXT_META_KEY } from "./host-contract.js";

// Only a command key and its original routing identity cross this boundary.
// Capability, target, business arguments and Skill binding stay in Central.
export function createTaskCommandRunner({ identityRouter, coordinator, performanceRecorder = null, logger = null, maxActive = 4 } = {}) {
  const jobs = new Map();
  const queue = [];
  let active = 0;
  let stopped = false;
  const controller = new AbortController();

  function schedule(command, owner = null) {
    const value = normalizeCommand(command);
    if (!value || stopped) return { accepted: false, reason: "invalid_command" };
    const key = JSON.stringify([value.taskId, value.commandKey]);
    if (jobs.has(key)) return { accepted: true, duplicate: true, promise: jobs.get(key).promise };
    if (jobs.size >= 32) return { accepted: false, reason: "capacity" };
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    const job = { key, command: value, owner, promise, resolve };
    jobs.set(key, job);
    queue.push(job);
    drain();
    return { accepted: true, duplicate: false, promise };
  }

  function drain() {
    while (!stopped && active < maxActive && queue.length) {
      const job = queue.shift();
      active += 1;
      const work = performanceRecorder ? performanceRecorder.run({ sessionKey: job.command.sessionKey,
        requestId: job.command.commandKey, runId: `prepared:${job.command.commandKey}`, taskId: job.command.taskId,
        toolName: "agentbridge_host_task_prepare" }, () => execute(job), "host.tool_call") : execute(job);
      work.then(job.resolve, error => {
        logger?.warn?.(`AgentBridge prepared command retained for recovery (${safeCode(error)})`);
        job.resolve({ status: "unconfirmed", taskId: job.command.taskId });
      }).finally(() => { active -= 1; jobs.delete(job.key); drain(); });
    }
  }

  async function execute({ command, owner }) {
    let identity = identityRouter.resolvePinnedSession({ sessionKey: command.sessionKey });
    if (command.endpoint) {
      const endpoint = command.endpoint;
      if (!owner?.binding || owner.binding.channel !== "telegram" || owner.binding.key !== command.endpointKey ||
          owner.binding.senderId !== endpoint.externalSubject ||
          (owner.binding.accountId !== null && owner.binding.accountId !== endpoint.accountId) ||
          (identity?.bound && identity.binding.key !== owner.binding.key) ||
          !identityRouter.restoreSessionBinding?.({ sessionKey: command.sessionKey, bindingKey: owner.binding.key })) {
        throw Object.assign(new Error("Telegram command owner does not match the exact endpoint"), { code: "TASK_COMMAND_BINDING_REQUIRED" });
      }
      identity = identityRouter.resolvePinnedSession({ sessionKey: command.sessionKey });
      coordinator.bindDeliveryRoute?.({ sessionKey: command.sessionKey, channel: "telegram",
        to: endpoint.route.to, accountId: endpoint.route.accountId || endpoint.accountId, threadId: null });
    } else if (!identity?.bound) {
      identity = await identityRouter.resolveWorkspaceSession(command.sessionKey, { signal: controller.signal });
    }
    if (!identity?.bound || (owner && identity.binding?.key !== owner.binding?.key)
        || identityRouter.endpointKeyForSession(command.sessionKey) !== command.endpointKey) {
      throw Object.assign(new Error("Command routing identity is not bound"), { code: "TASK_COMMAND_BINDING_REQUIRED" });
    }
    const client = identity.client;
    const leaseResponse = await client.callTool("agentbridge_host_coordinator_lease_acquire", {
      task_id: command.taskId, lease_seconds: 600, takeover: false, expected_version: null,
    }, { signal: controller.signal, meta: hostContextMeta() });
    if (leaseResponse?.status === "ignored" && leaseResponse.reason === "task_terminal") {
      return { status: "ignored", taskId: command.taskId };
    }
    const lease = leaseResponse?.coordinatorLease;
    if (!lease || lease.hostInstanceId !== "openclaw-gateway" || lease.taskId !== command.taskId
        || lease.state !== "active" || !Number.isInteger(lease.version) || lease.version < 1
        || !Number.isFinite(Date.parse(lease.expiresAt)) || Date.parse(lease.expiresAt) <= Date.now()) {
      throw Object.assign(new Error("Command coordinator lease was not granted"), { code: "HOST_COORDINATOR_LEASE_CONFLICT" });
    }
    const response = await client.callTool("agentbridge_host_task_prepare", {
      task_id: command.taskId, command_key: command.commandKey,
      session_key: command.sessionKey, endpoint_key: command.endpointKey,
    }, { signal: controller.signal, meta: { ...hostContextMeta(), [TASK_CONTEXT_META_KEY]: {
      taskId: command.taskId, hostRunId: command.commandKey, coordinatorLeaseVersion: String(lease.version),
    } } });
    const processed = processToolResult(response, coordinator.config.allowedCardOrigins);
    for (const interaction of processed.interactions) {
      await coordinator.restoreRecoveredInteraction({ taskId: command.taskId,
        interaction, sessionKey: command.sessionKey, mcpClient: client });
    }
    return { status: response?.status || "unconfirmed", taskId: command.taskId,
      commandKey: command.commandKey, interactionReady: processed.interactions.length > 0 };
  }

  return { schedule,
    recover(commands, owner) {
      if (!Array.isArray(commands)) return [];
      return commands.slice(0, 32).map(command => schedule(command, owner));
    },
    async waitForIdle() { await Promise.all([...jobs.values()].map(job => job.promise)); },
    stop() {
      stopped = true; controller.abort();
      for (const job of queue.splice(0)) { jobs.delete(job.key); job.resolve({ status: "deferred", taskId: job.command.taskId }); }
    },
    status() { return { active, queued: queue.length }; },
  };
}

function normalizeCommand(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (Object.keys(value).some(key => !["taskId", "commandKey", "sessionKey", "endpointKey", "endpoint"].includes(key))) return null;
  for (const field of ["taskId", "commandKey", "sessionKey", "endpointKey"]) {
    if (typeof value[field] !== "string" || !value[field] || value[field].length > (field === "sessionKey" ? 1024 : 768)) return null;
  }
  const result = { taskId: value.taskId, commandKey: value.commandKey, sessionKey: value.sessionKey, endpointKey: value.endpointKey };
  if (/^agent:[^:]+:agentbridge-workspace:direct:/iu.test(value.sessionKey)) return value.endpoint ? null : result;
  const match = /^agent:[^:]+:telegram:(?:direct|dm):([^:]+)$/iu.exec(value.sessionKey);
  const endpoint = value.endpoint;
  if (!match || !endpoint || typeof endpoint !== "object" || Array.isArray(endpoint) ||
      Object.keys(endpoint).some(key => !["endpointId", "clientType", "externalSubject", "accountId", "conversationRef", "route"].includes(key)) ||
      typeof endpoint.endpointId !== "string" || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/iu.test(endpoint.endpointId) ||
      endpoint.clientType !== "telegram" || endpoint.conversationRef !== value.sessionKey ||
      endpoint.externalSubject !== match[1] || match[1].startsWith("-") ||
      !(endpoint.accountId === null || (typeof endpoint.accountId === "string" && endpoint.accountId.length <= 512))) return null;
  const route = endpoint.route;
  if (!route || typeof route !== "object" || Array.isArray(route) ||
      Object.keys(route).some(key => !["channel", "to", "accountId", "threadId"].includes(key)) ||
      route.channel !== "telegram" || typeof route.to !== "string" || route.to.replace(/^telegram:/u, "") !== endpoint.externalSubject ||
      ![null, undefined, ""].includes(route.threadId) ||
      !(route.accountId == null || route.accountId === endpoint.accountId)) return null;
  return { ...result, endpoint: { endpointId: endpoint.endpointId, clientType: endpoint.clientType,
    externalSubject: endpoint.externalSubject, accountId: endpoint.accountId, conversationRef: endpoint.conversationRef,
    route: { channel: "telegram", to: route.to, accountId: route.accountId || null, threadId: null } } };
}

function safeCode(error) { return String(error?.code || "TASK_COMMAND_UNCONFIRMED").replace(/[^A-Za-z0-9_]/gu, "_").slice(0, 100); }
