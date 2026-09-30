export const HOST_CONTRACT_SCHEMA = "agentbridge.host.v1";
export const HOST_CONTEXT_META_KEY = "io.agentbridge/host-context";
export const LEGACY_HOST_CONTEXT_META_KEY = "io.agentbridge/host";
export const HOST_PROFILE_META_KEY = "io.agentbridge/host-profile";
export const TASK_CONTEXT_META_KEY = "io.agentbridge/task";
export const OPENCLAW_HOST_VERSION = "0.4.105";
export const OPENCLAW_HOST_INSTANCE_ID = "openclaw-gateway";

export const HOST_CAPABILITY_NAMES = Object.freeze([
  "mcpApps",
  "privateResultMeta",
  "interactionPollResume",
  "taskTimeline",
  "proactiveDelivery",
  "artifactDelivery",
  "restartRecovery",
  "coordinatorLease",
  "batchTaskTimeline",
  "runtimeSignals",
  "boundedTransportRecovery",
]);

const LEVELS = Object.freeze(["L1", "L2", "L3"]);
const LEVEL_REQUIREMENTS = Object.freeze({
  L1: Object.freeze([]),
  L2: Object.freeze(["privateResultMeta", "interactionPollResume"]),
  L3: Object.freeze([
    "privateResultMeta",
    "interactionPollResume",
    "taskTimeline",
    "proactiveDelivery",
    "artifactDelivery",
    "restartRecovery",
    "coordinatorLease",
    "batchTaskTimeline",
    "runtimeSignals",
    "boundedTransportRecovery",
  ]),
});

export const OPENCLAW_HOST_PROFILE = Object.freeze({
  schema: HOST_CONTRACT_SCHEMA,
  hostInstanceId: OPENCLAW_HOST_INSTANCE_ID,
  implementation: Object.freeze({
    name: "openclaw",
    version: OPENCLAW_HOST_VERSION,
  }),
  levels: Object.freeze(["L1", "L2", "L3"]),
  capabilities: Object.freeze({
    mcpApps: false,
    privateResultMeta: true,
    interactionPollResume: true,
    taskTimeline: true,
    proactiveDelivery: true,
    artifactDelivery: true,
    restartRecovery: true,
    coordinatorLease: true,
    batchTaskTimeline: true,
    runtimeSignals: true,
    boundedTransportRecovery: true,
  }),
  endpointTypes: Object.freeze([
    "telegram_private",
    "weixin_private",
    "web_private",
  ]),
});

export function validateHostCapabilityProfile(value) {
  const profile = objectValue(value, "host capability profile");
  const allowed = new Set([
    "schema",
    "hostInstanceId",
    "implementation",
    "levels",
    "capabilities",
    "endpointTypes",
  ]);
  rejectUnknown(profile, allowed, "host capability profile");
  if (profile.schema !== HOST_CONTRACT_SCHEMA) {
    throw hostContractError("host capability profile schema is invalid");
  }
  const hostInstanceId = requiredText(
    profile.hostInstanceId,
    "hostInstanceId",
    160,
  );
  const implementation = objectValue(
    profile.implementation,
    "host implementation",
  );
  rejectUnknown(
    implementation,
    new Set(["name", "version"]),
    "host implementation",
  );
  const name = requiredText(implementation.name, "implementation.name", 80);
  const version = requiredText(
    implementation.version,
    "implementation.version",
    80,
  );
  if (!Array.isArray(profile.levels) || profile.levels.length === 0) {
    throw hostContractError("host levels are required");
  }
  const declaredLevels = [...new Set(profile.levels.map((item) => String(item).toUpperCase()))];
  if (declaredLevels.some((level) => !LEVELS.includes(level))) {
    throw hostContractError("unsupported host level");
  }
  if (!declaredLevels.includes("L1")) {
    throw hostContractError("every host must declare L1");
  }
  const highestIndex = Math.max(
    ...declaredLevels.map((level) => LEVELS.indexOf(level)),
  );
  const levels = LEVELS.slice(0, highestIndex + 1);
  if (
    declaredLevels
      .slice()
      .sort((left, right) => LEVELS.indexOf(left) - LEVELS.indexOf(right))
      .join("|") !== levels.join("|")
  ) {
    throw hostContractError("host levels must be contiguous from L1");
  }
  const rawCapabilities = objectValue(
    profile.capabilities,
    "host capabilities",
  );
  rejectUnknown(
    rawCapabilities,
    new Set(HOST_CAPABILITY_NAMES),
    "host capabilities",
  );
  const capabilities = {};
  for (const capability of HOST_CAPABILITY_NAMES) {
    if (typeof rawCapabilities[capability] !== "boolean") {
      throw hostContractError(`capabilities.${capability} must be boolean`);
    }
    capabilities[capability] = rawCapabilities[capability];
  }
  const missing = LEVEL_REQUIREMENTS[levels.at(-1)].filter(
    (capability) => capabilities[capability] !== true,
  );
  if (missing.length > 0) {
    throw hostContractError(
      `declared ${levels.at(-1)} host is missing capabilities: ${missing.join(",")}`,
    );
  }
  if (!Array.isArray(profile.endpointTypes) || profile.endpointTypes.length === 0) {
    throw hostContractError("host endpointTypes are required");
  }
  const endpointTypes = [
    ...new Set(
      profile.endpointTypes.map((item) => requiredText(item, "endpointType", 80)),
    ),
  ];
  return {
    schema: HOST_CONTRACT_SCHEMA,
    hostInstanceId,
    implementation: { name, version },
    levels,
    capabilities,
    endpointTypes,
  };
}

export function hostContextMeta() {
  return {
    [HOST_CONTEXT_META_KEY]: {
      version: "1",
      agentHost: OPENCLAW_HOST_PROFILE.implementation.name,
      hostInstanceId: OPENCLAW_HOST_INSTANCE_ID,
      hostVersion: OPENCLAW_HOST_VERSION,
    },
  };
}

export function hostRegistrationMeta() {
  return {
    ...hostContextMeta(),
    [HOST_PROFILE_META_KEY]: OPENCLAW_HOST_PROFILE,
  };
}

export function assertAcceptedHostNegotiation(value, minimumLevel = "L3") {
  const negotiation = objectValue(value, "host negotiation");
  const acceptedLevel = requiredText(
    negotiation.acceptedLevel,
    "acceptedLevel",
    8,
  ).toUpperCase();
  if (!LEVELS.includes(acceptedLevel)) {
    throw hostContractError("host negotiation returned an invalid level");
  }
  if (LEVELS.indexOf(acceptedLevel) < LEVELS.indexOf(minimumLevel)) {
    const error = hostContractError(
      `AgentBridge accepted ${acceptedLevel}; ${minimumLevel} is required`,
    );
    error.code = "HOST_CAPABILITY_REQUIRED";
    throw error;
  }
  return acceptedLevel;
}

function objectValue(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw hostContractError(`${name} must be an object`);
  }
  return value;
}

function rejectUnknown(value, allowed, name) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length > 0) {
    throw hostContractError(`${name} contains unsupported fields: ${unknown.join(",")}`);
  }
}

function requiredText(value, name, maximum) {
  if (typeof value !== "string" && typeof value !== "number") {
    throw hostContractError(`${name} is required`);
  }
  const normalized = String(value).trim();
  if (!normalized || normalized.length > maximum) {
    throw hostContractError(`${name} is invalid`);
  }
  return normalized;
}

function hostContractError(message) {
  const error = new Error(message);
  error.code = "HOST_CONTRACT_INVALID";
  return error;
}
