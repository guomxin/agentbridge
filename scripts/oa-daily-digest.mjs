import { closeSync, constants, fstatSync, openSync, readFileSync, realpathSync } from "node:fs";
import { isAbsolute } from "node:path";

import { resolvePluginConfig } from "../integrations/openclaw-agentbridge/lib/config.js";
import { hostContextMeta } from "../integrations/openclaw-agentbridge/lib/host-contract.js";
import { createAgentBridgeMcpClient } from "../integrations/openclaw-agentbridge/lib/mcp-client.js";

const COMMANDS = new Map([
  ["trial", { trial: true, receipt: false }],
  ["run", { trial: false, receipt: false }],
  ["trial-status", { trial: true, receipt: true }],
  ["status", { trial: false, receipt: true }],
]);
const DELIVERY_STATES = new Set([
  "pending", "delivering", "acknowledged", "deferred", "failed", "cancelled", "archived",
]);

function readPrivateFile(path) {
  if (typeof path !== "string" || !isAbsolute(path)) {
    throw new Error("Private configuration path must be absolute");
  }
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.uid !== process.getuid() || (stat.mode & 0o077) || stat.size > 1024 * 1024) {
      throw new Error("Private configuration permissions or size are invalid");
    }
    return readFileSync(fd, "utf8").replace(/^\uFEFF/u, "");
  } finally {
    closeSync(fd);
  }
}

function samePath(left, right) {
  return typeof left === "string" && isAbsolute(left)
    && typeof right === "string" && isAbsolute(right)
    && realpathSync(left) === realpathSync(right);
}

function readProfileToken(envFile, name) {
  let token;
  for (const rawLine of readPrivateFile(envFile).split(/\r?\n/u)) {
    const line = rawLine.trim().replace(/^export /u, "");
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf("=");
    if (separator < 0) throw new Error("Invalid host environment");
    const key = line.slice(0, separator).trim();
    if (key !== name && key !== "NODE_TLS_REJECT_UNAUTHORIZED") continue;
    let value = line.slice(separator + 1).trim();
    if (value.length > 1 && ["\"", "'"].includes(value[0]) && value[0] === value.at(-1)) {
      value = value.slice(1, -1);
    }
    if (key === "NODE_TLS_REJECT_UNAUTHORIZED" && value === "0") {
      throw new Error("TLS verification must stay enabled");
    }
    if (key === name) token = value;
  }
  return token;
}

function publicReceipt(result, { trial, receipt }) {
  const expected = receipt ? ["succeeded", "not_found"] : ["published", "reused"];
  if (!expected.includes(result?.status)) throw new Error("Digest receipt is invalid");
  const output = { status: result.status, trial };
  if (result.status === "not_found") return output;
  if (result.trial !== trial || typeof result.localDate !== "string"
      || !/^\d{4}-\d{2}-\d{2}$/u.test(result.localDate)
      || !DELIVERY_STATES.has(result.deliveryState)) {
    throw new Error("Digest receipt is incomplete");
  }
  output.localDate = result.localDate;
  output.deliveryState = result.deliveryState;
  for (const key of ["publishedAt", "acknowledgedAt"]) {
    if (typeof result[key] === "string" && Number.isFinite(Date.parse(result[key]))) {
      output[key] = new Date(result[key]).toISOString();
    }
  }
  return output;
}

let phase = "arguments";
try {
  const [command, jobPath, ...extra] = process.argv.slice(2);
  const mode = COMMANDS.get(command);
  if (!mode || !jobPath || extra.length) throw new Error("Expected command and private job configuration");

  phase = "job_config";
  const job = JSON.parse(readPrivateFile(jobPath));
  if (job.schema !== "agentbridge.oa-daily-digest-job.v1"
      || typeof job.identityLabel !== "string" || !job.identityLabel
      || typeof job.telegramEndpointKey !== "string" || !job.telegramEndpointKey) {
    throw new Error("Daily digest configuration is invalid");
  }
  phase = "host_profile";
  const profile = JSON.parse(readPrivateFile(job.hostProfile));
  if (profile.schema !== "agentbridge.mac-host.v1" || profile.preparationOnly !== false
      || !samePath(process.execPath, profile.node)
      || !samePath(process.env.OPENCLAW_CONFIG_PATH, profile.configPath)
      || !samePath(process.env.OPENCLAW_STATE_DIR, profile.stateDir)
      || !samePath(process.env.NODE_EXTRA_CA_CERTS, profile.caCertificate)
      || process.env.NODE_TLS_REJECT_UNAUTHORIZED === "0") {
    throw new Error("Reviewed host environment is required");
  }
  const hostConfig = JSON.parse(readPrivateFile(profile.configPath));
  const entry = hostConfig.plugins?.entries?.["agentbridge-interactions"];
  if (entry?.enabled !== true || entry.hooks?.allowConversationAccess !== true) {
    throw new Error("Registered AgentBridge plugin is required");
  }
  const plugin = resolvePluginConfig(entry.config);
  const endpoint = new URL(plugin.mcpUrl);
  if (endpoint.protocol !== "https:" || endpoint.pathname !== "/mcp"
      || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
    throw new Error("Reviewed HTTPS MCP endpoint is required");
  }

  phase = "identity_binding";
  const matches = plugin.identityBindings.filter((binding) =>
    binding.label === job.identityLabel && binding.channel === "telegram",
  );
  const binding = matches[0];
  if (matches.length !== 1 || binding.key !== job.telegramEndpointKey || !/^[1-9]\d*$/u.test(binding.senderId)) {
    throw new Error("Exact Telegram private identity binding is required");
  }
  const token = process.env[binding.tokenEnv] || readProfileToken(profile.envFile, binding.tokenEnv);
  if (!token || /\s/u.test(token)) throw new Error("The bound MCP credential is unavailable");

  phase = "mcp_call";
  const client = createAgentBridgeMcpClient({
    endpoint: { url: endpoint.href, timeoutSeconds: mode.receipt ? 15 : 75 },
    tokenEnv: binding.tokenEnv,
    env: { [binding.tokenEnv]: token },
    fetchImpl: (url, options) => fetch(url, { ...options, redirect: "error" }),
  });
  const result = await client.callTool(
    mode.receipt ? "agentbridge_host_oa_daily_digest_status" : "agentbridge_host_oa_daily_digest",
    {
      agent_host: "openclaw",
      telegram_endpoint_key: binding.key,
      trial: mode.trial,
    },
    { meta: hostContextMeta() },
  );
  phase = "receipt";
  console.log(JSON.stringify(publicReceipt(result, mode)));
} catch {
  console.error(JSON.stringify({ status: "failed", phase }));
  process.exitCode = 1;
}
