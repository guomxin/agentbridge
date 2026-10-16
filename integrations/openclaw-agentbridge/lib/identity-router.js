import { matchIdentityBinding, resolveMcpEndpoint } from "./config.js";
import { isPrivateSessionKey } from "./interaction.js";
import { createAgentBridgeMcpClient } from "./mcp-client.js";
import { createHash } from "node:crypto";
import {
  assertAcceptedHostNegotiation,
  hostContextMeta,
  hostRegistrationMeta,
} from "./host-contract.js";

export class AgentBridgeIdentityRouter {
  constructor({
    config,
    hostConfig,
    env = process.env,
    fetchImpl = globalThis.fetch,
    sessionBindings = new Map(),
    sessionEndpoints = new Map(),
    planningPolicies = new Map(),
    catalogVersions = new Map(),
    catalogCache = new Map(),
    skillRunState = null,
  }) {
    this.config = config;
    this.endpoint = resolveMcpEndpoint(config, hostConfig);
    this.env = env;
    this.fetchImpl = fetchImpl;
    this.bindingsByKey = new Map(
      config.identityBindings.map((binding) => [binding.key, binding]),
    );
    this.clients = new Map();
    this.clientCatalogKeys = new Map();
    this.identityProfiles = new Map();
    this.identityProfileRefreshes = new Map();
    this.planningPolicies = planningPolicies;
    this.catalogVersions = catalogVersions;
    this.catalogCache = catalogCache;
    this.skillRunState = skillRunState || { bindings: new Map(), samples: new Map(), ended: new Map() };
    this.sessionBindings = sessionBindings;
    this.sessionEndpoints = sessionEndpoints;
  }

  get enabled() {
    return this.config.identityBindings.length > 0;
  }

  resolveToolContext(context) {
    const deliveryContext =
      context.deliveryContext &&
      typeof context.deliveryContext === "object" &&
      !Array.isArray(context.deliveryContext)
        ? context.deliveryContext
        : {};
    const sessionKey = context.sessionKey;
    let channel = context.messageChannel || deliveryContext.channel;
    let accountId = context.agentAccountId || deliveryContext.accountId;
    let senderId =
      context.requesterSenderId ||
      trustedDirectDeliverySender({
        sessionKey,
        channel,
        deliveryTo: deliveryContext.to,
      });

    if (!senderId) {
      const sessionIdentity = trustedDirectSessionIdentity({
        sessionKey,
        channel,
        accountId,
        bindings: this.config.identityBindings,
      });
      if (sessionIdentity) {
        channel = sessionIdentity.channel;
        senderId = sessionIdentity.senderId;
        accountId = sessionIdentity.accountId;
      }
    }

    if (!senderId) {
      const pinned = this.resolvePinnedSession({
        sessionKey,
        channel,
        accountId,
      });
      if (pinned) {
        return pinned;
      }
    }
    return this.resolve({
      sessionKey,
      channel,
      senderId,
      accountId,
    });
  }

  bindSession({ sessionKey, channel, senderId, accountId }) {
    const resolved = this.resolve({
      sessionKey,
      channel,
      senderId,
      accountId,
      bindSession: true,
    });
    return resolved.bound;
  }

  clientForSession(sessionKey) {
    const bindingKey = this.sessionBindings.get(sessionKey);
    if (!bindingKey || bindingKey === "conflict") {
      return null;
    }
    return this.clientForBinding(this.bindingsByKey.get(bindingKey));
  }

  async resolveWorkspaceSession(sessionKey, { signal } = {}) {
    const pinned = this.resolvePinnedSession({ sessionKey });
    if (!isWorkspaceSessionKey(sessionKey)) {
      return pinned || unbound("identity_not_provisioned");
    }
    if (pinned?.bound && this.endpointKeyForSession(sessionKey)) {
      return pinned;
    }
    for (const { binding, client } of this.configuredIdentities()) {
      try {
        const result = await client.callTool(
          "agentbridge_host_workspace_session_resolve",
          {
            agent_host: "openclaw",
            session_key: sessionKey,
          },
          {
            signal,
            meta: hostContextMeta(),
          },
        );
        if (
          result?.status === "succeeded" &&
          result?.binding?.sessionKey === sessionKey &&
          this.restoreSessionBinding({
            sessionKey,
            bindingKey: binding.key,
            endpointKey: result.binding.endpointKey,
          })
        ) {
          return this.resolvePinnedSession({ sessionKey });
        }
      } catch {
        // Only the Bearer identity that owns this web session can resolve it.
      }
    }
    return unbound("identity_not_provisioned");
  }

  configuredIdentities() {
    return this.config.identityBindings
      .map((binding) => ({
        binding,
        client: this.clientForBinding(binding),
      }))
      .filter((item) => item.client);
  }

  async refreshIdentityProfiles({ logger = null, signal, bindingKey = null } = {}) {
    const profiles = [];
    for (const { binding, client } of this.configuredIdentities()) {
      if (bindingKey !== null && binding.key !== bindingKey) continue;
      let refresh = this.identityProfileRefreshes.get(binding.key);
      if (!refresh) {
        refresh = this._refreshIdentityProfile({ binding, client }, { logger, signal });
        this.identityProfileRefreshes.set(binding.key, refresh);
      }
      try {
        const profile = await refresh;
        if (profile) profiles.push(profile);
      } finally {
        if (this.identityProfileRefreshes.get(binding.key) === refresh) {
          this.identityProfileRefreshes.delete(binding.key);
        }
      }
    }
    return profiles;
  }

  async _refreshIdentityProfile({ binding, client }, { logger, signal }) {
    try {
      const serverProfile = await client.callTool(
        "agentbridge_server_profile",
        {},
        { signal, meta: hostRegistrationMeta() },
      );
      const acceptedHostLevel = assertAcceptedHostNegotiation(
        serverProfile?.negotiation,
        "L3",
      );
      const result = await client.callTool(
        "agentbridge_host_identity_profile",
        { agent_host: "openclaw" },
        {
          signal,
          meta: hostContextMeta(),
        },
      );
      const allowedToolNames = Array.isArray(
        result?.agentToolAccess?.allowedToolNames,
      )
        ? result.agentToolAccess.allowedToolNames.filter(
            (name) => typeof name === "string" && name.trim(),
          )
        : null;
      if (!allowedToolNames) {
        throw new Error("AgentBridge identity profile omitted tool access");
      }
      const profile = Object.freeze({
        userSubject: identityPart(result?.identity?.userSubject, false),
        scopes: Object.freeze(
          Array.isArray(result?.identity?.scopes)
            ? result.identity.scopes.filter(
                (scope) => typeof scope === "string" && scope.trim(),
              )
            : [],
        ),
        allowedToolNames: new Set(allowedToolNames),
        expiresAt: identityPart(result?.identity?.expiresAt, false),
        acceptedHostLevel,
        planningPolicy: normalizePlanningPolicy(serverProfile?.planning),
        permissionVersion: validCatalogVersion(result?.permissionVersion),
      });
      this.identityProfiles.set(binding.key, profile);
      this.rememberCatalogVersions(binding, result);
      const policyKey = JSON.stringify([this.endpoint?.url, binding.key]);
      if (profile.planningPolicy) {
        this.planningPolicies.set(policyKey, profile.planningPolicy);
      } else {
        this.planningPolicies.delete(policyKey);
      }
      return {
        bindingKey: binding.key,
        userSubject: profile.userSubject,
        allowedToolCount: profile.allowedToolNames.size,
        acceptedHostLevel,
      };
    } catch (error) {
      logger?.warn?.(
        `AgentBridge identity tool profile unavailable for ${binding.label || binding.key}; existing fail-open catalog is retained (${safeErrorCode(error)})`,
      );
      return null;
    }
  }

  allowedToolNamesForBinding(binding) {
    return binding ? this.identityProfiles.get(binding.key)?.allowedToolNames || null : null;
  }

  permissionVersionForBinding(binding) {
    return binding ? this.identityProfiles.get(binding.key)?.permissionVersion || null : null;
  }

  catalogCacheKeyForBinding(binding) {
    if (!binding) return null;
    this.clientForBinding(binding);
    return this.clientCatalogKeys.get(binding.key) || null;
  }

  rememberCatalogVersions(binding, value) {
    const key = this.catalogCacheKeyForBinding(binding);
    if (!key || !/^(?:sha256:)?[a-f0-9]{64}$/u.test(value?.catalogVersion || "")
        || !/^(?:sha256:)?[a-f0-9]{64}$/u.test(value?.permissionVersion || "")) return;
    this.catalogVersions.set(key, { catalogVersion: value.catalogVersion, permissionVersion: value.permissionVersion });
    while (this.catalogVersions.size > 256) this.catalogVersions.delete(this.catalogVersions.keys().next().value);
  }

  catalogVersionsForBinding(binding) { return this.catalogVersions.get(this.catalogCacheKeyForBinding(binding)); }

  planningPolicyForBinding(binding) {
    return binding
      ? this.planningPolicies.get(JSON.stringify([this.endpoint?.url, binding.key])) || null
      : null;
  }

  restoreSessionBinding({ sessionKey, bindingKey, endpointKey = null }) {
    if (!isPrivateSessionKey(sessionKey)) {
      return false;
    }
    const binding = this.bindingsByKey.get(bindingKey);
    if (!binding || !this.clientForBinding(binding)) {
      return false;
    }
    const existing = this.sessionBindings.get(sessionKey);
    if (existing === "conflict" || (existing && existing !== bindingKey)) {
      this.sessionBindings.set(sessionKey, "conflict");
      return false;
    }
    this.sessionBindings.set(sessionKey, bindingKey);
    if (isWorkspaceSessionKey(sessionKey)) {
      const normalizedEndpointKey = identityPart(endpointKey, false);
      if (
        normalizedEndpointKey &&
        normalizedEndpointKey.startsWith("workspace:")
      ) {
        this.sessionEndpoints.set(sessionKey, normalizedEndpointKey);
      }
    }
    return true;
  }

  endpointKeyForSession(sessionKey) {
    if (isWorkspaceSessionKey(sessionKey)) {
      return this.sessionEndpoints.get(sessionKey) || null;
    }
    const bindingKey = this.sessionBindings.get(sessionKey);
    return bindingKey && bindingKey !== "conflict" ? bindingKey : null;
  }

  statusForSession(sessionKey) {
    const bindingKey = this.sessionBindings.get(sessionKey);
    const binding = this.bindingsByKey.get(bindingKey);
    return {
      enabled: this.enabled,
      bound: Boolean(binding && this.clientForBinding(binding)),
      label: binding?.label || null,
      state:
        bindingKey === "conflict"
          ? "identity_conflict"
          : binding
            ? "bound"
            : "unbound",
    };
  }

  removeSession(sessionKey) {
    this.sessionBindings.delete(sessionKey);
    this.sessionEndpoints.delete(sessionKey);
  }

  resolvePinnedSession({ sessionKey, channel, accountId }) {
    if (!isPrivateSessionKey(sessionKey) || !this.endpoint) {
      return null;
    }
    const bindingKey = this.sessionBindings.get(sessionKey);
    if (!bindingKey || bindingKey === "conflict") {
      return null;
    }
    const binding = this.bindingsByKey.get(bindingKey);
    if (!binding) {
      return null;
    }
    const workspaceSession = isWorkspaceSessionKey(sessionKey);
    const normalizedChannel = identityPart(channel, true);
    const normalizedAccountId = identityPart(accountId, false);
    if (
      !workspaceSession &&
      normalizedChannel &&
      normalizedChannel !== binding.channel
    ) {
      // Control-plane clients can inspect a channel session through webchat/http.
      // Reject that invocation without permanently poisoning the channel binding.
      return unbound("session_identity_conflict");
    }
    if (
      !workspaceSession &&
      normalizedAccountId &&
      binding.accountId !== null &&
      normalizedAccountId !== binding.accountId
    ) {
      this.sessionBindings.set(sessionKey, "conflict");
      return unbound("session_identity_conflict");
    }
    const client = this.clientForBinding(binding);
    if (!client) {
      return unbound("identity_token_unavailable");
    }
    return Object.freeze({
      bound: true,
      binding,
      client,
      reason: null,
    });
  }

  resolve({
    sessionKey,
    channel,
    senderId,
    accountId,
    bindSession = true,
  }) {
    if (!isPrivateSessionKey(sessionKey)) {
      return unbound("private_session_required");
    }
    if (!this.endpoint) {
      return unbound("mcp_endpoint_unavailable");
    }
    const binding = matchIdentityBinding(this.config.identityBindings, {
      channel,
      senderId,
      accountId,
    });
    if (!binding) {
      return unbound("identity_not_provisioned");
    }

    const existing = this.sessionBindings.get(sessionKey);
    if (existing === "conflict" || (existing && existing !== binding.key)) {
      this.sessionBindings.set(sessionKey, "conflict");
      return unbound("session_identity_conflict");
    }
    if (bindSession) {
      this.sessionBindings.set(sessionKey, binding.key);
    }

    const client = this.clientForBinding(binding);
    if (!client) {
      return unbound("identity_token_unavailable");
    }
    return Object.freeze({
      bound: true,
      binding,
      client,
      reason: null,
    });
  }

  clientForBinding(binding) {
    if (!binding || !this.endpoint) {
      return null;
    }
    if (this.clients.has(binding.key)) {
      return this.clients.get(binding.key);
    }
    const client = createAgentBridgeMcpClient({
      endpoint: this.endpoint,
      tokenEnv: binding.tokenEnv,
      env: this.env,
      fetchImpl: this.fetchImpl,
    });
    if (client) {
      this.clients.set(binding.key, client);
      // Bind to the credential captured by this exact transport instance;
      // changing an environment object later cannot relabel an old client.
      const token = this.env[binding.tokenEnv];
      this.clientCatalogKeys.set(binding.key, JSON.stringify([this.endpoint.url, binding.key,
        createHash("sha256").update(token.trim()).digest("hex")]));
    }
    return client;
  }
}

function normalizePlanningPolicy(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    typeof value.schemaVersion !== "string" ||
    !value.schemaVersion.startsWith("agentbridge.composed-task-planning-policy.") ||
    typeof value.modelContext !== "string" ||
    !value.modelContext.trim()
  ) {
    return null;
  }
  return Object.freeze({
    schemaVersion: value.schemaVersion,
    skillProtocol: value.skillProtocol === "agentbridge.skills.v1" ? value.skillProtocol : null,
    modelContext: value.modelContext.slice(0, 6_000),
  });
}

function isWorkspaceSessionKey(sessionKey) {
  return (
    typeof sessionKey === "string" &&
    /^agent:[^:]+:agentbridge-workspace:direct:/i.test(sessionKey.trim())
  );
}

function unbound(reason) {
  return Object.freeze({
    bound: false,
    binding: null,
    client: null,
    reason,
  });
}

function trustedDirectDeliverySender({ sessionKey, channel, deliveryTo }) {
  if (
    identityPart(channel, true) !== "openclaw-weixin" ||
    typeof sessionKey !== "string" ||
    typeof deliveryTo !== "string"
  ) {
    return null;
  }
  const match = sessionKey
    .trim()
    .match(/^agent:[^:]+:openclaw-weixin:direct:(.+)$/i);
  const peer = identityPart(match?.[1], true);
  const target = identityPart(deliveryTo, true);
  return peer && target && peer === target ? deliveryTo.trim() : null;
}

function trustedDirectSessionIdentity({
  sessionKey,
  channel,
  accountId,
  bindings,
}) {
  if (typeof sessionKey !== "string") {
    return null;
  }
  const match = sessionKey
    .trim()
    .match(/^agent:[^:]+:([^:]+):direct:(.+)$/i);
  const sessionChannel = identityPart(match?.[1], true);
  const senderId = identityPart(match?.[2], false);
  const requestedChannel = identityPart(channel, true);
  const requestedAccountId = identityPart(accountId, false);
  if (
    !sessionChannel ||
    !senderId ||
    sessionChannel === "agentbridge-workspace" ||
    (requestedChannel && requestedChannel !== sessionChannel)
  ) {
    return null;
  }
  const comparableSenderId =
    sessionChannel === "openclaw-weixin"
      ? identityPart(senderId, true)
      : senderId;
  const candidates = bindings.filter(
    (binding) =>
      binding.channel === sessionChannel &&
      (sessionChannel === "openclaw-weixin"
        ? identityPart(binding.senderId, true) === comparableSenderId
        : binding.senderId === comparableSenderId) &&
      (!requestedAccountId ||
        binding.accountId === null ||
        binding.accountId === requestedAccountId),
  );
  const binding = requestedAccountId
    ? candidates.find(
        (candidate) => candidate.accountId === requestedAccountId,
      ) || candidates.find((candidate) => candidate.accountId === null)
    : candidates.length === 1
      ? candidates[0]
      : null;
  if (!binding) {
    return null;
  }
  return {
    channel: sessionChannel,
    senderId: binding.senderId,
    accountId: requestedAccountId || binding.accountId,
  };
}

function identityPart(value, lowercase) {
  if (typeof value !== "string" && typeof value !== "number") {
    return null;
  }
  const normalized = String(value).trim().slice(0, 512);
  return normalized ? (lowercase ? normalized.toLowerCase() : normalized) : null;
}

function validCatalogVersion(value) {
  return typeof value === "string" && /^(?:sha256:)?[a-f0-9]{64}$/u.test(value)
    ? value : null;
}

function safeErrorCode(error) {
  return String(error?.code || error?.name || "IDENTITY_PROFILE_ERROR")
    .toUpperCase()
    .replace(/[^A-Z0-9_.-]/g, "_")
    .slice(0, 80);
}
