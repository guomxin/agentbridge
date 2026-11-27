import { createHash, randomUUID } from "node:crypto";
import { hostContextMeta } from "./host-contract.js";
import { DurableTimelineProjection, openTimelineProjectionStore } from "./timeline-projection.js";

const RECENT_KEY_TTL_MS = 10_000;
const MAX_RECENT_KEYS = 500;
const TIMELINE_ATTEMPT_TIMEOUT_MS = 5_000;
const TIMELINE_MAX_ATTEMPTS = 3;
const TIMELINE_RETRY_DELAYS_MS = [250, 1_000];

export class TimelinePublisher {
  constructor({
    identityRouter,
    logger,
    sharedState = {},
    now = Date.now,
    sleep = retrySleep,
    api = null,
    projectionStore = undefined,
  }) {
    this.identityRouter = identityRouter;
    this.logger = logger;
    this.now = now;
    this.sleep = sleep;
    const durable = projectionStore === undefined ? openTimelineProjectionStore(api) : projectionStore;
    this.projection = durable ? new DurableTimelineProjection({ identityRouter, store: durable,
      hostConfig: api?.config || {}, logger, now, sleep }) : null;
    this.queues =
      sharedState.timelineQueues ||
      (sharedState.timelineQueues = new Map());
    this.recentKeys =
      sharedState.timelineRecentKeys ||
      (sharedState.timelineRecentKeys = new Map());
    this.publications =
      sharedState.timelinePublications ||
      (sharedState.timelinePublications = new Map());
  }

  capture({
    sessionKey,
    role,
    text,
    event = {},
    context = {},
    taskId = null,
  }) {
    const normalizedText = safeText(text, 50_000);
    if (
      !normalizedText ||
      !["user", "assistant"].includes(role) ||
      isWorkspaceSession(sessionKey)
    ) {
      return Promise.resolve(false);
    }
    const resolved = this.identityRouter.resolvePinnedSession({
      sessionKey,
      channel: event.channel || context.channelId,
      accountId: event.accountId || context.accountId,
    });
    const endpointKey = this.identityRouter.endpointKeyForSession(sessionKey);
    if (!resolved?.bound || !endpointKey) {
      return Promise.resolve(false);
    }
    const binding = resolved.binding;
    if (binding.channel === "telegram" && this.projection) {
      return this.projection.capture({ resolved, sessionKey, endpointKey, role, text, event, context, taskId });
    }
    const messageKey = this.messageKey({
      sessionKey,
      role,
      text: normalizedText,
      event,
      context,
    });
    const existingPublication = this.publications.get(messageKey);
    if (existingPublication) {
      return existingPublication.promise;
    }
    const route = {
      channel: binding.channel,
      to:
        safeText(
          context.conversationId ||
            event.conversationId ||
            event.to ||
            binding.senderId,
          768,
        ) || binding.senderId,
      accountId:
        safeText(context.accountId || event.accountId, 512) ||
        binding.accountId,
      threadId: safeText(context.threadId || event.threadId, 512),
    };
    const publication = this.enqueue(binding.key, async () => {
      let lastError = null;
      for (let attempt = 1; attempt <= TIMELINE_MAX_ATTEMPTS; attempt += 1) {
        try {
          await resolved.client.callTool(
            "agentbridge_host_timeline_append",
            {
              agent_host: "openclaw",
              endpoint_key: endpointKey,
              client_type: binding.channel,
              external_subject: binding.senderId,
              conversation_ref: sessionKey,
              message_key: messageKey,
              role,
              text: normalizedText,
              account_id: binding.accountId,
              label: binding.label,
              route,
              task_id: safeText(taskId, 128),
            },
            {
              signal: AbortSignal.timeout(TIMELINE_ATTEMPT_TIMEOUT_MS),
              meta: hostContextMeta(),
            },
          );
          return true;
        } catch (error) {
          lastError = error;
          if (attempt < TIMELINE_MAX_ATTEMPTS) {
            this.logger?.debug?.(
              `AgentBridge cross-end text synchronization retry (${attempt}/${TIMELINE_MAX_ATTEMPTS}, ${safeErrorCode(error)})`,
            );
            await this.sleep(TIMELINE_RETRY_DELAYS_MS[attempt - 1]);
          }
        }
      }
      this.logger?.warn?.(
        `AgentBridge cross-end text synchronization failed after ${TIMELINE_MAX_ATTEMPTS} attempts (${safeErrorCode(lastError)})`,
      );
      return false;
    });
    const publishedAt = this.now();
    this.publications.set(messageKey, {
      promise: publication,
      createdAt: publishedAt,
    });
    void publication.then((succeeded) => {
      if (
        !succeeded &&
        this.publications.get(messageKey)?.promise === publication
      ) {
        this.publications.delete(messageKey);
      }
    });
    return publication;
  }

  async waitForIdle() {
    await Promise.allSettled([...this.queues.values(), this.projection?.waitForIdle()]);
  }

  start() { this.projection?.start(); }
  stop() { this.projection?.stop(); }

  messageKey({ sessionKey, role, text, event, context }) {
    const now = this.now();
    this.prune(now);
    // Successful delivery IDs distinguish repeated identical messages and remain
    // stable across duplicate callbacks even beyond the short fingerprint cache.
    const deliveredId = event.success === true
      ? safeText(event.messageId || context.messageId, 256) : null;
    if (deliveredId) return `${role}:${sessionKey}:sent:${deliveredId}`.slice(0, 768);
    // A real platform message id wins over the text fingerprint. Two messages
    // with identical content are still separate user turns.
    const platformId = safeText(event.messageId || context.messageId, 256);
    if (platformId) return `${role}:${sessionKey}:${platformId}`.slice(0, 768);
    const fingerprint = createHash("sha256")
      .update(`${role}\0${sessionKey}\0${text}`, "utf8")
      .digest("hex");
    const recent = this.recentKeys.get(fingerprint);
    if (recent && now - recent.createdAt <= RECENT_KEY_TTL_MS) {
      return recent.messageKey;
    }
    const sourceId = safeText(
      event.messageId ||
        event.id ||
        event.runId ||
        context.messageId ||
        context.runId,
      256,
    );
    const messageKey = sourceId
      ? `${role}:${sessionKey}:${sourceId}`.slice(0, 768)
      : (
          `${role}:${sessionKey}:${now}:${randomUUID()}`
        ).slice(0, 768);
    this.recentKeys.set(fingerprint, { messageKey, createdAt: now });
    return messageKey;
  }

  enqueue(key, work) {
    const previous = this.queues.get(key) || Promise.resolve();
    const current = previous
      .catch(() => undefined)
      .then(work)
      .finally(() => {
        if (this.queues.get(key) === current) {
          this.queues.delete(key);
        }
      });
    this.queues.set(key, current);
    return current;
  }

  prune(now) {
    for (const [key, value] of this.recentKeys) {
      if (now - value.createdAt > RECENT_KEY_TTL_MS) {
        this.recentKeys.delete(key);
      }
    }
    while (this.recentKeys.size > MAX_RECENT_KEYS) {
      this.recentKeys.delete(this.recentKeys.keys().next().value);
    }
    for (const [key, value] of this.publications) {
      if (now - value.createdAt > RECENT_KEY_TTL_MS) {
        this.publications.delete(key);
      }
    }
    while (this.publications.size > MAX_RECENT_KEYS) {
      this.publications.delete(this.publications.keys().next().value);
    }
  }
}

function isWorkspaceSession(sessionKey) {
  return (
    typeof sessionKey === "string" &&
    /^agent:[^:]+:agentbridge-workspace:direct:/i.test(sessionKey.trim())
  );
}

function safeText(value, maximum) {
  if (typeof value !== "string" && typeof value !== "number") {
    return null;
  }
  const normalized = String(value).replaceAll("\0", "").trim();
  return normalized ? normalized.slice(0, maximum) : null;
}

function safeErrorCode(error) {
  return String(error?.code || error?.name || "TIMELINE_SYNC_FAILED")
    .toUpperCase()
    .replace(/[^A-Z0-9_.-]/g, "_")
    .slice(0, 80);
}

function retrySleep(milliseconds) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, milliseconds);
    timer.unref?.();
  });
}
