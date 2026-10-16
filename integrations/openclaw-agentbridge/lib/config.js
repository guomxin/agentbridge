const DEFAULTS = Object.freeze({
  mcpServerName: "agentbridge",
  mcpUrl: null,
  mcpTimeoutSeconds: 150,
  autoPoll: true,
  pollIntervalSeconds: 2,
  maxPollSeconds: 1800,
  wakeAgentOnComplete: true,
  syncTimeline: true,
  notificationLongPoll: true,
  notificationConcurrency: 4,
  skillCatalogCache: true,
  boundSkillToolGuard: true,
  toolScope: "all",
});

export function resolvePluginConfig(raw) {
  const source = isRecord(raw) ? raw : {};
  const identityBindings = normalizeIdentityBindings(source.identityBindings);
  const mcpUrl = normalizeMcpUrl(source.mcpUrl) || DEFAULTS.mcpUrl;
  if (identityBindings.length > 0 && !mcpUrl) {
    throw new Error("mcpUrl is required for AgentBridge identity bindings");
  }
  const allowedCardOrigins = [];
  for (const value of Array.isArray(source.allowedCardOrigins)
    ? source.allowedCardOrigins
    : []) {
    const normalized = normalizeHttpOrigin(value);
    if (normalized && !allowedCardOrigins.includes(normalized)) {
      allowedCardOrigins.push(normalized);
    }
  }

  return Object.freeze({
    allowedCardOrigins: Object.freeze(allowedCardOrigins),
    mcpServerName: boundedString(source.mcpServerName, DEFAULTS.mcpServerName, 100),
    mcpUrl,
    mcpTimeoutSeconds: boundedInteger(
      source.mcpTimeoutSeconds,
      DEFAULTS.mcpTimeoutSeconds,
      1,
      300,
    ),
    identityBindings: Object.freeze(identityBindings),
    autoPoll: booleanValue(source.autoPoll, DEFAULTS.autoPoll),
    pollIntervalSeconds: boundedInteger(
      source.pollIntervalSeconds,
      DEFAULTS.pollIntervalSeconds,
      1,
      30,
    ),
    maxPollSeconds: boundedInteger(
      source.maxPollSeconds,
      DEFAULTS.maxPollSeconds,
      30,
      1800,
    ),
    wakeAgentOnComplete: booleanValue(
      source.wakeAgentOnComplete,
      DEFAULTS.wakeAgentOnComplete,
    ),
    syncTimeline: booleanValue(
      source.syncTimeline,
      DEFAULTS.syncTimeline,
    ),
    notificationLongPoll: booleanValue(source.notificationLongPoll, DEFAULTS.notificationLongPoll),
    notificationConcurrency: boundedInteger(source.notificationConcurrency, DEFAULTS.notificationConcurrency, 1, 8),
    skillCatalogCache: booleanValue(source.skillCatalogCache, DEFAULTS.skillCatalogCache),
    boundSkillToolGuard: booleanValue(source.boundSkillToolGuard, DEFAULTS.boundSkillToolGuard),
    toolScope: source.toolScope === "oa_pending" ? "oa_pending" : "all",
  });
}

export function resolveMcpServer(hostConfig, serverName) {
  if (!isRecord(hostConfig) || !isRecord(hostConfig.mcp)) {
    return null;
  }
  const servers = hostConfig.mcp.servers;
  if (!isRecord(servers) || !isRecord(servers[serverName])) {
    return null;
  }
  const server = servers[serverName];
  const url = boundedString(server.url, "", 2048);
  if (!url) {
    return null;
  }
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    return null;
  }
  return {
    url: parsed.toString(),
    headers: isRecord(server.headers) ? { ...server.headers } : {},
    timeoutSeconds: boundedInteger(server.timeout, 150, 1, 300),
  };
}

export function resolveMcpEndpoint(pluginConfig, hostConfig) {
  if (pluginConfig.mcpUrl) {
    return Object.freeze({
      url: pluginConfig.mcpUrl,
      timeoutSeconds: pluginConfig.mcpTimeoutSeconds,
    });
  }
  const legacy = resolveMcpServer(hostConfig, pluginConfig.mcpServerName);
  if (!legacy) {
    return null;
  }
  return Object.freeze({
    url: legacy.url,
    timeoutSeconds: legacy.timeoutSeconds,
  });
}

export function matchIdentityBinding(bindings, context) {
  const channel = normalizeIdentityPart(context.channel, true);
  const senderId = normalizeIdentityPart(context.senderId, false);
  const accountId = normalizeIdentityPart(context.accountId, false);
  if (!channel || !senderId) {
    return null;
  }
  const candidates = bindings.filter(
    (binding) =>
      binding.channel === channel &&
      identitySenderMatches(channel, binding.senderId, senderId) &&
      (binding.accountId === null || binding.accountId === accountId),
  );
  return (
    candidates.find(
      (binding) => binding.accountId !== null && binding.accountId === accountId,
    ) ||
    candidates.find((binding) => binding.accountId === null) ||
    null
  );
}

function normalizeIdentityBindings(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  const result = [];
  const seen = new Set();
  for (const item of value) {
    if (!isRecord(item)) {
      continue;
    }
    const channel = normalizeIdentityPart(item.channel, true);
    const senderId = normalizeIdentityPart(item.senderId, false);
    const accountId = normalizeIdentityPart(item.accountId, false);
    const tokenEnv = boundedString(item.tokenEnv, "", 128);
    if (
      !channel ||
      !senderId ||
      !/^[A-Za-z_][A-Za-z0-9_]*$/.test(tokenEnv)
    ) {
      continue;
    }
    const key = `${channel}:${accountId || "*"}:${senderId}`;
    const selectorKey = `${channel}:${accountId || "*"}:${
      channel === "openclaw-weixin" ? senderId.toLowerCase() : senderId
    }`;
    if (seen.has(selectorKey)) {
      throw new Error(`duplicate AgentBridge identity binding: ${key}`);
    }
    seen.add(selectorKey);
    result.push(
      Object.freeze({
        key,
        channel,
        senderId,
        accountId,
        tokenEnv,
        label: boundedString(item.label, "", 120) || null,
      }),
    );
  }
  return result;
}

function identitySenderMatches(channel, configuredSenderId, requestedSenderId) {
  return channel === "openclaw-weixin"
    ? configuredSenderId.toLowerCase() === requestedSenderId.toLowerCase()
    : configuredSenderId === requestedSenderId;
}

function normalizeIdentityPart(value, lowercase) {
  if (typeof value !== "string" && typeof value !== "number") {
    return null;
  }
  const normalized = String(value).trim().slice(0, 512);
  return normalized ? (lowercase ? normalized.toLowerCase() : normalized) : null;
}

export function normalizeHttpOrigin(value) {
  if (typeof value !== "string" || !value.trim()) {
    return null;
  }
  try {
    const parsed = new URL(value.trim());
    if (!["http:", "https:"].includes(parsed.protocol)) {
      return null;
    }
    if (
      parsed.username ||
      parsed.password ||
      parsed.pathname !== "/" ||
      parsed.search ||
      parsed.hash
    ) {
      return null;
    }
    return parsed.origin;
  } catch {
    return null;
  }
}

function normalizeMcpUrl(value) {
  if (typeof value !== "string" || !value.trim()) {
    return null;
  }
  try {
    const parsed = new URL(value.trim());
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password ||
      parsed.hash ||
      !parsed.pathname
    ) {
      return null;
    }
    return parsed.toString();
  } catch {
    return null;
  }
}

function boundedInteger(value, fallback, minimum, maximum) {
  if (!Number.isInteger(value)) {
    return fallback;
  }
  return Math.min(maximum, Math.max(minimum, value));
}

function boundedString(value, fallback, maximumLength) {
  if (typeof value !== "string") {
    return fallback;
  }
  const normalized = value.trim();
  if (!normalized) {
    return fallback;
  }
  return normalized.slice(0, maximumLength);
}

function booleanValue(value, fallback) {
  return typeof value === "boolean" ? value : fallback;
}

export function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
