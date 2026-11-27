import { buildPresentation, normalizeInteraction } from "./interaction.js";

export const taskNotificationMode = "host_notification_v2";
const kinds = new Set(["public_status", "trusted_interaction"]);
const id = value => typeof value === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/u.test(value);

export function validTaskNotification(notification) {
  return id(notification.sourceEventId) && id(notification.sourceTaskId) && kinds.has(notification.notificationKind);
}

// Private presentation data may only enter the native buttons. The SDK also
// builds fallback text, which is cached as ordinary conversation history.
export async function taskNotificationPayload({ issued, record, adapter, allowedOrigins, runtimeConfig, route, now }) {
  if (issued.sourceEventId !== record.sourceEventId || issued.sourceTaskId !== record.sourceTaskId ||
      issued.notificationKind !== record.notificationKind || issued.resultView != null) return null;
  if (record.notificationKind === "public_status") {
    return issued.interaction == null && typeof issued.publicText === "string" && issued.publicText.trim()
      ? { text: issued.publicText } : null;
  }
  if (issued.publicText != null || issued.interaction?.presentation?.endpointId !== record.endpointId) return null;
  const interaction = normalizeInteraction(issued.interaction, new Set(allowedOrigins));
  if (!interaction || !["pending", "processing"].includes(interaction.state) ||
      !interaction.presentation.url || !(Date.parse(interaction.expiresAt) > now) ||
      typeof adapter.renderPresentation !== "function") return null;
  const presentation = buildPresentation([interaction], "telegram");
  if (!presentation?.blocks.some(block => block.type === "buttons" && block.buttons.length)) return null;
  const text = "请在原任务的安全卡片中核对。";
  const payload = { text, presentation };
  const rendered = await adapter.renderPresentation({ payload, presentation,
    ctx: { cfg: runtimeConfig, to: route.to, accountId: route.accountId, payload } });
  return rendered ? { ...rendered, text } : null;
}
