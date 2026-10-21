import { normalizeHttpOrigin, isRecord } from "./config.js";
import { batchViewForInteraction, businessViewForInteraction, interactionCanOpen, interactionEntryLabel } from "./interaction-business-view.js";

export const INTERACTION_SCHEMA_VERSION = "agentbridge.interaction.v1";
export const WITHHELD_URL = "[trusted AgentBridge card URL withheld by OpenClaw host]";
export const SERVER_WITHHELD_URL =
  "[trusted AgentBridge URL withheld from model context]";

const MCP_APP_RESOURCE_URI = "ui://agentbridge/trusted-interaction.html";

const INTERACTION_TYPES = new Set([
  "credential",
  "business_input",
  "execution_authorization",
]);
const INTERACTION_STATES = new Set([
  "pending",
  "processing",
  "completed",
  "declined",
  "expired",
  "failed",
  "superseded",
]);
const EXTERNAL_BROWSER_LABEL = "浏览器打开";
const STATE_LABELS = {
  pending: "等待操作",
  processing: "处理中",
  completed: "交互已完成，业务结果待核验",
  declined: "已拒绝",
  expired: "已过期",
  failed: "失败",
  superseded: "已替换",
};
const MAX_WALK_DEPTH = 14;
const MAX_WALK_NODES = 10000;
const MAX_JSON_TEXT_LENGTH = 1024 * 1024;
const HISTORICAL_INTERACTION_CONTAINERS = new Set([
  "operation",
  "operations",
]);

export function processToolResult(result, allowedOrigins) {
  const normalizedOrigins = new Set(
    allowedOrigins.map(normalizeHttpOrigin).filter(Boolean),
  );
  const interactions = collectInteractions(result, normalizedOrigins);
  const trustedInteractions = collectInteractions(result, normalizedOrigins, {
    includeHistorical: true,
  });
  if (trustedInteractions.length === 0) {
    return { interactions, result, sanitized: false };
  }
  return {
    interactions,
    result: sanitizeTrustedResult(result, trustedInteractions),
    sanitized: true,
  };
}

export function collectInteractions(value, allowedOrigins, options = {}) {
  const includeHistorical = options.includeHistorical === true;
  const interactions = new Map();
  const visited = new WeakSet();
  let visitedNodes = 0;

  function walk(current, depth) {
    if (depth > MAX_WALK_DEPTH || visitedNodes >= MAX_WALK_NODES) {
      return;
    }
    visitedNodes += 1;

    if (typeof current === "string") {
      const parsed = parseJsonText(current);
      if (parsed !== null) {
        walk(parsed, depth + 1);
      }
      return;
    }
    if (typeof current !== "object" || current === null) {
      return;
    }
    if (visited.has(current)) {
      return;
    }
    visited.add(current);

    const interaction = normalizeInteraction(current, allowedOrigins);
    if (interaction) {
      interactions.set(interaction.interactionId, interaction);
      return;
    }
    const entries = Array.isArray(current)
      ? current.entries()
      : Object.entries(current);
    for (const [key, child] of entries) {
      if (
        !includeHistorical &&
        HISTORICAL_INTERACTION_CONTAINERS.has(String(key))
      ) {
        continue;
      }
      walk(child, depth + 1);
    }
  }

  walk(value, 0);
  return [...interactions.values()];
}

export function collectPublicInteractionReferences(value) {
  const references = new Map();
  const visited = new WeakSet();
  let visitedNodes = 0;

  function walk(current, depth) {
    if (depth > MAX_WALK_DEPTH || visitedNodes >= MAX_WALK_NODES) {
      return;
    }
    visitedNodes += 1;

    if (typeof current === "string") {
      const parsed = parseJsonText(current);
      if (parsed !== null) {
        walk(parsed, depth + 1);
      }
      return;
    }
    if (typeof current !== "object" || current === null || visited.has(current)) {
      return;
    }
    visited.add(current);

    const reference = normalizePublicInteractionReference(current);
    if (reference) {
      references.set(reference.interactionId, reference);
      return;
    }
    const entries = Array.isArray(current)
      ? current.entries()
      : Object.entries(current);
    for (const [key, child] of entries) {
      if (HISTORICAL_INTERACTION_CONTAINERS.has(String(key))) {
        continue;
      }
      walk(child, depth + 1);
    }
  }

  walk(value, 0);
  return [...references.values()];
}

function normalizePublicInteractionReference(value) {
  if (!isRecord(value) || value.schemaVersion !== INTERACTION_SCHEMA_VERSION) {
    return null;
  }
  const interactionId = safeString(value.interactionId, 128);
  const type = safeString(value.type, 64);
  const state = safeString(value.state, 64);
  const presentation = value.presentation;
  if (
    interactionId.length < 16 ||
    !INTERACTION_TYPES.has(type) ||
    !["pending", "processing"].includes(state) ||
    !isRecord(presentation) ||
    presentation.owner !== "agentbridge" ||
    presentation.modelMustNotCollectValues !== true ||
    presentation.hostHandled !== true ||
    presentation.url !== SERVER_WITHHELD_URL ||
    presentation.uiResourceUri !== MCP_APP_RESOURCE_URI
  ) {
    return null;
  }
  return Object.freeze({ interactionId, type, state });
}
export function normalizeInteraction(value, allowedOrigins) {
  if (!isRecord(value) || value.schemaVersion !== INTERACTION_SCHEMA_VERSION) {
    return null;
  }
  const interactionId = safeString(value.interactionId, 128);
  const type = safeString(value.type, 64);
  const state = safeString(value.state, 64);
  if (
    interactionId.length < 16 ||
    !INTERACTION_TYPES.has(type) ||
    !INTERACTION_STATES.has(state) ||
    !isRecord(value.presentation) ||
    value.presentation.owner !== "agentbridge" ||
    value.presentation.modelMustNotCollectValues !== true
  ) {
    return null;
  }

  const rawUrl = safeString(value.presentation.url, 4096);
  let cardUrl = null;
  if (rawUrl) {
    try {
      const parsed = new URL(rawUrl);
      if (
        ['http:', 'https:'].includes(parsed.protocol) &&
        !parsed.username &&
        !parsed.password &&
        !parsed.hash &&
        allowedOrigins.has(parsed.origin)
      ) {
        cardUrl = parsed.toString();
      }
    } catch {
      cardUrl = null;
    }
  }
  if (["pending", "processing"].includes(state) && !cardUrl) {
    return null;
  }

  const display = isRecord(value.display) ? { ...value.display } : {};
  const poll = isRecord(value.poll) ? { ...value.poll } : {};
  const resume = isRecord(value.resume) ? { ...value.resume } : {};
  return Object.freeze({
    schemaVersion: INTERACTION_SCHEMA_VERSION,
    interactionId,
    type,
    state,
    title: safeString(value.title, 200) || "AgentBridge",
    message: safeString(value.message, 1000),
    operationId: safeString(value.operationId, 128) || null,
    presentation: Object.freeze({
      owner: "agentbridge",
      url: cardUrl,
      modelMustNotCollectValues: true,
      ...(value.presentation.flowVersion === "agentbridge.trusted-flow.v1" &&
          cardUrl && /^\/flow\/(?:input|authorize)\//.test(new URL(cardUrl).pathname)
        ? { flowVersion: "agentbridge.trusted-flow.v1" } : {}),
    }),
    display: Object.freeze(display),
    expiresAt: safeString(value.expiresAt, 80) || null,
    poll: Object.freeze(poll),
    resume: Object.freeze(resume),
  });
}

export function sanitizeTrustedResult(result, interactions) {
  const byId = new Map(interactions.map((item) => [item.interactionId, item]));
  const urls = new Set(
    interactions.map((item) => item.presentation.url).filter(Boolean),
  );
  const seen = new WeakMap();

  function sanitize(value) {
    if (typeof value === "string") {

      let text = value;
      for (const url of urls) {
        text = text.split(url).join(WITHHELD_URL);
      }
      return text;
    }
    if (typeof value !== "object" || value === null) {
      return value;
    }
    if (seen.has(value)) {
      return seen.get(value);
    }
    const clone = Array.isArray(value) ? [] : {};
    seen.set(value, clone);
    for (const [key, child] of Object.entries(value)) {
      clone[key] = sanitize(child);
    }
    if (
      !Array.isArray(value) &&
      value.schemaVersion === INTERACTION_SCHEMA_VERSION &&
      byId.has(value.interactionId) &&
      isRecord(clone.presentation)
    ) {
      clone.presentation.url = WITHHELD_URL;
      clone.presentation.hostHandled = true;
    }
    return clone;
  }

  return sanitize(result);
}

export function isPrivateSessionKey(sessionKey) {
  if (typeof sessionKey !== "string" || !sessionKey.trim()) {
    return false;
  }
  const normalized = sessionKey.trim().toLowerCase();
  if (/:(group|channel|room):/.test(normalized)) {
    return false;
  }
  if (/^agent:[^:]+:main(?:$|:thread:)/.test(normalized)) {
    return true;
  }
  return /:(direct|dm):/.test(normalized);
}

export function channelFromPrivateSessionKey(sessionKey) {
  if (!isPrivateSessionKey(sessionKey)) {
    return null;
  }
  const match = sessionKey
    .trim()
    .match(/^agent:[^:]+:([a-z0-9][a-z0-9._-]{0,63}):(direct|dm):/i);
  return match ? match[1].toLowerCase() : null;
}

export function isInteractionExpired(interaction, now = Date.now()) {
  if (!interaction.expiresAt) {
    return false;
  }
  const expiresAt = Date.parse(interaction.expiresAt);
  return Number.isFinite(expiresAt) && expiresAt <= now;
}

export function buildPresentation(interactions, channel) {
  if (interactions.length === 0) {
    return null;
  }
  const blocks = [];
  for (const [index, original] of interactions.entries()) {
    // Expiry removes actions, while retaining enough context to explain why an
    // old card cannot be used. It says nothing about the business outcome.
    const interaction = isInteractionExpired(original) && original.state === "pending"
      ? { ...original, state: "expired" } : original;
    if (index > 0) {
      blocks.push({ type: "divider" });
    }
    if (interactions.length > 1) {
      blocks.push({ type: "text", text: `${index + 1}. ${interaction.title}` });
    }
    if (interaction.message) {
      blocks.push({ type: "text", text: interaction.message });
    }
    blocks.push({ type: "context", text: contextText(interaction) });
    blocks.push({ type: "context", text: interactionGuidance(interaction) });
    if (interactionCanOpen(interaction) && !isInteractionExpired(interaction)) {
      const url = interaction.presentation.url;
      const button = {
        label: interactionEntryLabel(interaction),
        priority: 100,
        style: "primary",
      };
      const buttons = [button];
      if (!url) continue;
      if (channel === "telegram" && url.startsWith("https://")) {
        button.webApp = { url };
        buttons.push({
          label: EXTERNAL_BROWSER_LABEL,
          priority: 90,
          style: "secondary",
          url,
        });
      } else {
        button.url = url;
      }
      blocks.push({ type: "buttons", buttons });
    }
  }
  return {
    title: interactions.length === 1 ? interactions[0].title : `AgentBridge · ${interactions.length} 项交互`,
    tone: presentationTone(interactions),
    blocks,
  };
}

export function appendPresentationLinks(payload, presentation) {
  if (!isRecord(payload) || !isRecord(presentation)) {
    return payload;
  }
  const text = typeof payload.text === "string" ? payload.text : "";
  const links = [];
  const seen = new Set();
  for (const block of Array.isArray(presentation.blocks)
    ? presentation.blocks
    : []) {
    for (const button of isRecord(block) && Array.isArray(block.buttons)
      ? block.buttons
      : []) {
      const url = trustedButtonUrl(button);
      if (!url || seen.has(url) || text.includes(url)) {
        continue;
      }
      seen.add(url);
      links.push({
        label: safeString(button.label, 80) || "打开安全页面",
        url,
      });
    }
  }
  if (links.length === 0) {
    return payload;
  }
  const linkText = links.map((item) => `${item.label}：${item.url}`).join("\n");
  return {
    ...payload,
    text: text ? `${text}\n\n${linkText}` : linkText,
  };
}

export function mergePresentations(existing, agentbridge) {
  if (!existing || !Array.isArray(existing.blocks)) {
    return agentbridge;
  }
  return {
    ...existing,
    blocks: [
      ...existing.blocks,
      { type: "divider" },
      ...agentbridge.blocks,
    ],
  };
}

function trustedButtonUrl(value) {
  if (!isRecord(value)) {
    return null;
  }
  const direct = safeString(value.url, 4096);
  const webApp = isRecord(value.webApp)
    ? safeString(value.webApp.url, 4096)
    : null;
  const candidate = direct || webApp;
  if (!candidate) {
    return null;
  }
  try {
    const parsed = new URL(candidate);
    return ["http:", "https:"].includes(parsed.protocol)
      ? parsed.toString()
      : null;
  } catch {
    return null;
  }
}

function contextText(interaction) {
  const parts = ["AgentBridge 可信交互"];
  if (typeof interaction.display.systemName === "string") {
    parts.push(interaction.display.systemName.slice(0, 100));
  }
  if (Number.isInteger(interaction.display.fieldCount)) {
    parts.push(`${interaction.display.fieldCount} 个字段`);
  }
  const batch = batchViewForInteraction(interaction);
  if (batch) parts.push(`批次第 ${batch.currentIndex}/${batch.totalCount} 项，已核验完成 ${batch.succeededCount}/${batch.totalCount} 项`);
  const completedLabels = {
    credential: "登录已完成",
    business_input: "办理内容已接收",
    execution_authorization: "已授权，业务结果待核验",
  };
  parts.push(`状态：${interaction.state === "completed"
    ? completedLabels[interaction.type] || STATE_LABELS.completed
    : STATE_LABELS[interaction.state] || interaction.state}`);
  return parts.join(" · ");
}

function interactionGuidance(interaction) {
  const batch = batchViewForInteraction(interaction);
  if (interaction.display?.batch && !batch) return "批次状态暂不可确认，请返回原任务核对最新状态。";
  if (batch && !["running", "waiting_user"].includes(batch.state)) {
    return batch.state === "outcome_unknown"
      ? "本批次已停止，当前项业务结果未知；请先核对原任务，不会自动重试或继续下一项。"
      : batch.state === "succeeded"
        ? "本批次各项已由业务系统核验完成，此入口不再办理。"
        : "本批次已停止，后续事项未继续执行，请返回原任务核对原因。";
  }
  if (interaction.state === "completed") {
    return interaction.type === "credential"
      ? "登录完成后将继续原任务，请以原任务的业务核验结果为准。"
      : "安全页面已接收本次处理；业务成功须以原任务的核验结果为准，请勿重复提交。";
  }
  if (interaction.state === "processing") {
    return "安全页面正在处理，请等待原任务更新，勿重复提交。";
  }
  if (interaction.state === "expired") return "此入口已失效，请返回原任务核对状态并重新准备。";
  if (interaction.state === "superseded") return "请使用原任务中的最新卡片，此入口不再办理。";
  if (interaction.state === "declined") return "本次交互已取消，系统不会据此继续执行。";
  if (interaction.state === "failed") return "请先核对原任务的失败原因和业务结果，勿直接重复提交。";
  if (interaction.type === "credential") return "凭据仅在安全页面填写，登录后继续原任务。";
  const view = businessViewForInteraction(interaction);
  if (view) {
    const guidance = view.viewKind === "approval"
      ? "在办理页核对本项材料、填写意见并最终确认；打开页面不会提交本项处理。"
      : "在同一办理页填写、预览并最终确认；打开页面不会提交业务。";
    return batch ? `${guidance}本项核验成功后才进入下一项，每项单独确认。` : guidance;
  }
  if (interaction.type === "execution_authorization") {
    return "请在安全页面核对目标、材料和执行范围后最终确认；授权后仍须核验业务结果。";
  }
  return interaction.presentation.flowVersion === "agentbridge.trusted-flow.v1"
    ? "在办理页补充信息后继续核对与最终确认，等待原任务核验业务结果。"
    : "请在安全页面补充信息，再按原任务提示核对并最终确认。";
}

function presentationTone(interactions) {
  const states = new Set(interactions.map((item) => item.state));
  if (["failed", "expired"].some((state) => states.has(state))) {
    return "danger";
  }
  if (["pending", "processing"].some((state) => states.has(state))) {
    return "warning";
  }
  // An interaction completion is not an independently verified business success.
  return "neutral";
}

function parseJsonText(value) {
  const text = value.trim();
  if (
    text.length === 0 ||
    text.length > MAX_JSON_TEXT_LENGTH ||
    !['{', '['].includes(text[0])
  ) {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function safeString(value, maximumLength) {
  return typeof value === "string" ? value.trim().slice(0, maximumLength) : "";
}
