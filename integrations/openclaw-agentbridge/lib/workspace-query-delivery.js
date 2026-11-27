import { createHash, randomUUID } from "node:crypto";
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { hostContextMeta } from "./host-contract.js";
import { normalizeTrustedResultView, resultViewPresentation } from "./result-view.js";
import { canonicalTelegramAccount, sameTelegramAccount } from "./telegram-identity.js";

const TOOL = "agentbridge_host_workspace_result_view";
const active = new Set();
const digest = value => createHash("sha256").update(value).digest("hex");

// This journal contains routing evidence and receipts, never message bodies,
// credentials or short-lived result links. A send boundary survives a restart;
// it is deliberately not a queue that can replay an uncertain Telegram POST.
export class WorkspaceQueryReceiptStore {
  constructor(directory) { this.directory = directory; }
  folder(bindingKey, settled = false) { return path.join(this.directory, digest(bindingKey), settled ? "settled" : "pending"); }
  file(record, settled = false) { return path.join(this.folder(record.endpointKey, settled), `${digest(record.deliveryId)}.${record.attemptCount}.json`); }
  read(record) {
    for (const settled of [false, true]) {
      try { return JSON.parse(readFileSync(this.file(record, settled), "utf8")); }
      catch (error) { if (error.code !== "ENOENT") throw error; }
    }
    return null;
  }
  reserve(record) {
    if (this.read(record)) return false;
    const file = this.file(record);
    durableDirectory(path.dirname(file));
    let fd;
    try { fd = openSync(file, "wx", 0o600); }
    catch (error) { if (error.code === "EEXIST") return false; throw error; }
    try { writeFileSync(fd, JSON.stringify(record)); fsyncSync(fd); }
    finally { closeSync(fd); }
    syncDirectory(path.dirname(file));
    return true;
  }
  save(record) {
    const file = this.file(record), temporary = `${file}.${randomUUID()}.tmp`;
    const fd = openSync(temporary, "wx", 0o600);
    try { writeFileSync(fd, JSON.stringify(record)); fsyncSync(fd); }
    finally { closeSync(fd); }
    renameSync(temporary, file);
    syncDirectory(path.dirname(file));
  }
  settle(record) {
    const target = this.file(record, true);
    durableDirectory(path.dirname(target));
    renameSync(this.file(record), target);
    syncDirectory(path.dirname(target));
    syncDirectory(path.dirname(this.file(record)));
  }
  pending(bindingKey) {
    let names;
    try { names = readdirSync(this.folder(bindingKey)); }
    catch (error) { if (error.code === "ENOENT") return []; throw error; }
    return names.filter(name => /^[a-f0-9]{64}\.[1-9][0-9]*\.json$/.test(name)).map(name =>
      JSON.parse(readFileSync(path.join(this.folder(bindingKey), name), "utf8")));
  }
  prior(record) {
    const prefix = `${digest(record.deliveryId)}.`;
    const records = [];
    for (const settled of [false, true]) {
      const folder = this.folder(record.endpointKey, settled);
      let names;
      try { names = readdirSync(folder); }
      catch (error) { if (error.code === "ENOENT") continue; throw error; }
      for (const name of names.filter(name => name.startsWith(prefix) && name.endsWith(".json"))) {
        records.push({ ...JSON.parse(readFileSync(path.join(folder, name), "utf8")), settled });
      }
    }
    return records;
  }
}

function syncDirectory(directory) {
  const fd = openSync(directory, "r");
  try { fsyncSync(fd); } finally { closeSync(fd); }
}

function durableDirectory(directory) {
  const first = mkdirSync(directory, { recursive: true, mode: 0o700 });
  if (!first) return;
  // fsync the new directory entries too, including the first existing parent.
  const stop = path.dirname(first);
  for (let current = directory; ; current = path.dirname(current)) {
    syncDirectory(current);
    if (current === stop) break;
  }
}

export function workspaceQueryStateDirectory(api) {
  if (api.runtime?.state?.resolveStateDir) return path.join(api.runtime.state.resolveStateDir(), "agentbridge", "workspace-query-v2");
  const home = process.env.OPENCLAW_HOME?.trim() || os.homedir();
  const expand = value => path.resolve(value.startsWith("~") ? path.join(home, value.slice(1)) : value);
  const state = process.env.OPENCLAW_STATE_DIR?.trim();
  const config = process.env.OPENCLAW_CONFIG_PATH?.trim();
  return path.join(state ? expand(state) : config ? path.dirname(expand(config)) : path.join(home, ".openclaw"), "agentbridge", "workspace-query-v2");
}

export class WorkspaceQueryDelivery {
  constructor({ api, config, store = null, now = Date.now }) {
    this.api = api;
    this.config = config;
    this.store = store || new WorkspaceQueryReceiptStore(workspaceQueryStateDirectory(api));
    this.now = now;
    this.recoveryOffset = 0;
  }

  async recover(binding, client, signal) {
    if (binding.channel !== "telegram") return;
    const pending = this.store.pending(binding.key);
    const start = this.recoveryOffset % Math.max(pending.length, 1);
    const batch = [...pending.slice(start), ...pending.slice(0, start)].slice(0, 16);
    this.recoveryOffset = start + batch.length;
    for (const record of batch) {
      const key = this.store.file(record);
      if (signal.aborted || active.has(key) || record.endpointKey !== binding.key) continue;
      // An old process might still be in its native send. It remains the owner;
      // a replacement process may settle its receipt only after it has exited.
      if (record.ownerPid !== process.pid && processAlive(record.ownerPid)) continue;
      active.add(key);
      try {
        if (!record.outcome) {
          record.outcome = record.state === "prepared" ? "unavailable" : "delivery_unknown";
          record.state = "completed";
          this.store.save(record);
        }
        await this.ack(record, client, signal);
      } catch {
        this.api.logger.warn("AgentBridge Workspace receipt settlement retained for recovery");
      } finally { active.delete(key); }
    }
  }

  async ack(record, client, signal) {
    const result = await client.callTool(TOOL, {
      ...controlArgs(record), action: "ack", outcome: record.outcome,
      ...(record.receipt ? { receipt: record.receipt } : {}),
      ...(record.outcome === "retry_after" ? { retry_after_seconds: record.retryAfterSeconds } : {}),
    }, { signal, meta: hostContextMeta() });
    if (result?.recorded === true) this.store.settle(record);
  }

  async deliver({ binding, client, endpoint, notification, signal, identityRouter, withDirectDelivery }) {
    const route = checkedRoute(binding, endpoint);
    if (!route || !validNotification(notification) || signal.aborted ||
        !identityRouter.restoreSessionBinding({ sessionKey: endpoint.conversationRef, bindingKey: binding.key })) return;
    const textOnly = notification.deliveryMode === "workspace_text_v2";
    const record = { schema: "agentbridge.openclaw-workspace-query.v2", endpointKey: binding.key,
      deliveryId: notification.deliveryId, attemptCount: notification.attemptCount,
      pairingRef: notification.pairingRef, deliveryMode: notification.deliveryMode,
      ...(textOnly ? { sourceEntryId: notification.sourceEntryId,
        partIndex: notification.partIndex, partCount: notification.partCount } : { sourceTaskId: notification.sourceTaskId }),
      endpointId: endpoint.endpointId, accountId: route.accountId, chatId: route.to,
      state: "prepared", ownerPid: process.pid, createdAt: new Date(this.now()).toISOString() };
    const key = this.store.file(record);
    const identityScope = identityRouter.catalogCacheKeyForBinding?.(binding);
    const isCurrent = () => currentBinding(identityRouter, binding, endpoint, client) &&
      identityRouter.catalogCacheKeyForBinding?.(binding) === identityScope;
    if (active.has(key)) return;
    active.add(key);
    try {
      const previous = this.store.read(record);
      if (previous) {
        // A terminal journal entry is a durable tombstone, even if Central
        // accidentally reclaims the same notification after an ACK was lost.
        if (previous.outcome && existsSync(this.store.file(previous))) await this.ack(previous, client, signal);
        return;
      }
      // A new lease attempt is not evidence that the previous native send
      // failed. Only an acknowledged, explicit rate-limit refusal permits it.
      if (this.store.prior(record).some(prior => prior.attemptCount >= record.attemptCount ||
          prior.outcome !== "retry_after" || !prior.settled || prior.pairingRef !== record.pairingRef)) return;
      const issued = await client.callTool(TOOL, { ...controlArgs(record), action: "issue" }, { signal, meta: hostContextMeta() });
      if (!issued?.available || signal.aborted) return;
      // Central freezes each text part and releases the next part only after
      // the previous native receipt. Never add a card, trim, split or rebuild
      // this text: a changed payload must not reuse its original delivery ID.
      if (textOnly && issued.resultView) return;
      const adapter = await this.api.runtime.channel.outbound.loadAdapter("telegram");
      if (typeof adapter?.sendPayload !== "function") return;
      const view = issued.resultView ? normalizeTrustedResultView(issued.resultView, {
        sourceTaskId: record.sourceTaskId, endpointId: record.endpointId,
        allowedOrigins: this.config.allowedCardOrigins, now: this.now(),
      }) : null;
      if (issued.resultView && !view) return;
      const text = typeof issued.publicText === "string" ? (textOnly ? issued.publicText : issued.publicText.trim()) : "";
      if (!text && !view) return;
      let payload = { text: text || "OA 待办已查询，可从下方进入选择与办理。" };
      if (view) {
        if (typeof adapter.renderPresentation !== "function") return;
        const presentation = resultViewPresentation([view], "telegram", this.now());
        const publicCardText = [payload.text, presentation.title,
          ...presentation.blocks.filter(block => ["text", "context"].includes(block.type)).map(block => block.text)].join("\n\n");
        const rendered = await adapter.renderPresentation({ payload, presentation,
          ctx: { cfg: this.api.config, to: route.to, accountId: route.accountId, payload } });
        // The SDK's generic fallback includes button URLs in its plain text.
        // Keep signed links exclusively in native buttons, outside the text it
        // later caches as reply/model context.
        payload = rendered ? { ...rendered, text: publicCardText } : null;
      }
      // Freeze one compact text+buttons payload. Escape all text ourselves so
      // Markdown expansion, rich-message fallback and internal chunking cannot
      // turn this first-batch delivery into multiple platform sends.
      if (!payload || typeof payload.text !== "string" || payload.mediaUrl || payload.mediaUrls?.length) return;
      payload = { ...payload, text: escapeHtml(payload.text) };
      if (!payload.text || payload.text.length > 3500 || signal.aborted ||
          !(Date.parse(issued.leaseExpiresAt) > this.now()) || !isCurrent()) return;
      record.payloadHash = digest(JSON.stringify(payload));
      if (!this.store.reserve(record)) return;
      // Persist before asking for the permit. A lost begin reply, crash, abort,
      // or failed local write after it can never cause a second native send.
      record.state = "begin_requested";
      this.store.save(record);
      let began;
      try { began = await client.callTool(TOOL, { ...controlArgs(record), action: "begin" }, { signal, meta: hostContextMeta() }); }
      catch { return; }
      if (began?.allowed !== true || signal.aborted || !isCurrent()) {
        record.outcome = began?.allowed === false ? "unavailable" : "delivery_unknown";
      } else {
        const persistReceipt = async result => {
          const receipt = checkedReceipt(result, route);
          if (!receipt || (record.receipt && record.receipt !== receipt)) throw new Error("WORKSPACE_QUERY_RECEIPT_INVALID");
          record.receipt = receipt;
          record.state = "completed";
          record.outcome = "sent";
          this.store.save(record);
        };
        try {
          const result = await withDirectDelivery(() => adapter.sendPayload({
            cfg: singleAttemptConfig(this.api.config), to: route.to, accountId: route.accountId,
            text: payload.text, payload, formatting: { parseMode: "HTML" }, onDeliveryResult: persistReceipt,
          }));
          if (!record.receipt) await persistReceipt(result);
        } catch (error) {
          // A callback receipt outranks a subsequent SDK bookkeeping failure.
          if (!record.receipt) Object.assign(record, sendFailure(error));
        }
      }
      record.state = "completed";
      this.store.save(record);
      await this.ack(record, client, signal);
    } finally { active.delete(key); }
  }
}

function controlArgs(record) {
  return { agent_host: "openclaw", endpoint_key: record.endpointKey, pairing_ref: record.pairingRef,
    delivery_id: record.deliveryId, attempt_count: record.attemptCount };
}
function validNotification(value) {
  const source = value?.deliveryMode === "workspace_query_v2"
    ? typeof value.sourceTaskId === "string" && value.sourceTaskId.length > 0
    : value?.deliveryMode === "workspace_text_v2" && typeof value.sourceEntryId === "string" && value.sourceEntryId.length > 0 &&
      Number.isSafeInteger(value.partIndex) && Number.isSafeInteger(value.partCount) &&
      value.partIndex > 0 && value.partIndex <= value.partCount;
  return source && /^[a-f0-9-]{36}$/.test(value.deliveryId || "") &&
    /^[a-f0-9]{64}$/.test(value.pairingRef || "") && Number.isSafeInteger(value.attemptCount) && value.attemptCount > 0 &&
    ["workspace_query_v2", "workspace_text_v2"].includes(value.deliveryMode);
}
function checkedRoute(binding, endpoint) {
  const peer = /^agent:[^:]+:telegram:(?:direct|dm):([1-9][0-9]*)$/iu.exec(endpoint?.conversationRef || "")?.[1];
  const route = endpoint?.route;
  if (!peer || peer !== binding.senderId || binding.channel !== "telegram" || !endpoint.endpointId ||
      endpoint.clientType !== "telegram" || endpoint.externalSubject !== peer || route?.channel !== "telegram" ||
      String(route.to).replace(/^telegram:/u, "") !== peer || ![null, undefined, ""].includes(route.threadId) ||
      !sameTelegramAccount(route.accountId, binding.accountId) || !sameTelegramAccount(endpoint.accountId, binding.accountId)) return null;
  return { to: peer, accountId: canonicalTelegramAccount(binding.accountId) };
}
function currentBinding(router, binding, endpoint, client) {
  if (!checkedRoute(binding, endpoint)) return false;
  if (typeof router.resolvePinnedSession !== "function") return false;
  const current = router.resolvePinnedSession({ sessionKey: endpoint.conversationRef });
  return current?.bound && current.binding?.key === binding.key &&
    current.client === client && current.binding.senderId === binding.senderId && sameTelegramAccount(current.binding.accountId, binding.accountId);
}
function checkedReceipt(result, route) {
  const id = typeof result?.messageId === "string" && /^[1-9][0-9]*$/.test(result.messageId) ? Number(result.messageId) : result?.messageId;
  if (!Number.isSafeInteger(id) || id <= 0 || String(result?.chatId) !== route.to ||
      (result.channel !== undefined && result.channel !== "telegram") ||
      (result.accountId !== undefined && !sameTelegramAccount(result.accountId, route.accountId))) return null;
  return id;
}
function singleAttemptConfig(config) {
  const telegram = config?.channels?.telegram || {};
  // The Telegram transport has a second IPv4/IP fallback loop below retry.
  // An explicit non-ipv4first DNS order disables that loop in the supported
  // SDK; retain the existing proxy/account while pinning one transport attempt.
  const single = value => ({ ...value, retry: { ...value?.retry, attempts: 1 }, richMessages: false,
    network: { ...value?.network, dnsResultOrder: "verbatim" } });
  return { ...config, channels: { ...config?.channels, telegram: { ...single(telegram),
    ...(telegram.accounts ? { accounts: Object.fromEntries(Object.entries(telegram.accounts).map(([id, value]) => [id, single(value)])) } : {}),
  } } };
}
function sendFailure(error) {
  // Only structured Telegram API refusals prove the request was not accepted.
  // Wrapper messages, transport failures and missing/mismatched receipts do not.
  const response = error?.error_code ? error : error?.error?.error_code ? error.error : null;
  if (response?.error_code === 429 && Number.isSafeInteger(response.parameters?.retry_after) && response.parameters.retry_after > 0) {
    return { outcome: "retry_after", retryAfterSeconds: response.parameters.retry_after };
  }
  if ([400, 401, 403, 404].includes(response?.error_code)) return { outcome: "rejected" };
  return { outcome: "delivery_unknown" };
}
function processAlive(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (error) { return error.code === "EPERM"; }
}
function escapeHtml(text) { return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
