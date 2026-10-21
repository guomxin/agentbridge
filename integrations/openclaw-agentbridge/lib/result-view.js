import { normalizeHttpOrigin, isRecord } from "./config.js";

export function normalizeTrustedResultView(value, { sourceTaskId, endpointId, allowedOrigins, now = Date.now() }) {
  if (!isRecord(value) || value.schemaVersion !== "agentbridge.result-view.v1" ||
      value.sourceTaskId !== sourceTaskId || !sourceTaskId || !endpointId ||
      !isRecord(value.presentation) || value.presentation.owner !== "agentbridge" ||
      value.presentation.endpointId !== endpointId || value.presentation.modelMustNotCollectValues !== true) return null;
  const expiresAt = Date.parse(value.expiresAt);
  if (!Number.isFinite(expiresAt) || expiresAt <= now || expiresAt - now > 61 * 60_000) return null;
  let url;
  try {
    url = new URL(value.presentation.url);
    const origins = new Set(allowedOrigins.map(normalizeHttpOrigin).filter(Boolean));
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.hash || url.search ||
        !origins.has(url.origin) || !/^\/results\/[A-Za-z0-9_-]{43}$/.test(url.pathname)) return null;
  } catch { return null; }
  return Object.freeze({ schemaVersion: value.schemaVersion, sourceTaskId, endpointId,
    url: url.toString(), expiresAt: value.expiresAt,
    selectionAvailable: value.selectionAvailable === true,
    sourceExpiresAt: typeof value.sourceExpiresAt === "string" ? value.sourceExpiresAt : null });
}

export function resultViewPresentation(views, channel, now = Date.now()) {
  const current = views.filter(view => Date.parse(view.expiresAt) > now);
  if (!current.length) return null;
  const blocks = [];
  for (const view of current) {
    const canSelect = view.selectionAvailable && Date.parse(view.sourceExpiresAt) > now;
    blocks.push({ type: "text", text: canSelect
      ? "可在办理页选择一项或多项 OA 待办，再逐项核对材料与最终确认。"
      : "原查询的可选时效已结束，可查看已选事项的办理进度；新选事项请先重新查询。" });
    const primary = { label: canSelect ? "选择并办理 OA 待办" : "查看办理进度", priority: 100, style: "primary" };
    const buttons = [primary];
    if (channel === "telegram" && view.url.startsWith("https://")) {
      primary.webApp = { url: view.url };
      buttons.push({ label: "浏览器打开", priority: 90, style: "secondary", url: view.url });
    } else { primary.url = view.url; }
    blocks.push({ type: "context", text: "选择与打开页面不会提交业务；批量办理也须逐项确认，结果未知时停止并核对。" });
    blocks.push({ type: "buttons", buttons });
  }
  return { title: "OA 待办 · 选择与办理", tone: "info", blocks };
}
