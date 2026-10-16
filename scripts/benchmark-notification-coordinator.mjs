// Real OpenClaw InteractionCoordinator and MCP HTTP transport. Only the remote
// notification/lease/resume service is synthetic; no business adapter is used.
import { createServer } from "node:http";
import { readFile, readdir, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { performance } from "node:perf_hooks";

const source = resolve(process.argv[2] || ".");
const output = resolve(process.argv[3] || "output/latency-performance-20261016/coordinator.json");
const count = Math.max(1, Math.min(100, Number(process.argv[4]) || 30));
const scenario = process.argv[5] === "interaction" ? "interaction" : "notification";
async function sourceFingerprint() {
  const root = resolve(source, "integrations/openclaw-agentbridge/lib");
  const hash = createHash("sha256");
  for (const file of (await readdir(root)).filter(file => file.endsWith(".js")).sort()) {
    hash.update(file); hash.update(await readFile(resolve(root, file)));
  }
  return hash.digest("hex");
}
const sourceBefore = await sourceFingerprint();
const { InteractionCoordinator, createInteractionSharedState } = await import(pathToFileURL(resolve(source, "integrations/openclaw-agentbridge/lib/coordinator.js")));
const { resolvePluginConfig } = await import(pathToFileURL(resolve(source, "integrations/openclaw-agentbridge/lib/config.js")));
const { createAgentBridgeMcpClient } = await import(pathToFileURL(resolve(source, "integrations/openclaw-agentbridge/lib/mcp-client.js")));
const packageVersion = JSON.parse(await readFile(resolve(source, "integrations/openclaw-agentbridge/package.json"), "utf8")).version;
const samples = Array.from({ length: count }, (_, index) => ({ index, bindingKey: `sample-${index}`,
  sessionKey: `agent:main:agentbridge-workspace:direct:sample-${index}`, claims: 0, writes: 0, acks: 0,
  phaseMs: 100 + Math.round((index / Math.max(count - 1, 1)) * (scenario === "interaction" ? 1800 : 9300)),
  pending: [], waiters: new Set() }));
const tasks = [];
const polls = [];
let finish;
const completed = new Promise(resolveDone => { finish = resolveDone; });

const server = createServer(async (request, response) => {
  let raw = "";
  for await (const chunk of request) raw += chunk;
  const rpc = JSON.parse(raw);
  const name = rpc.params.name;
  const args = rpc.params.arguments;
  const index = Number(request.headers.authorization.replace("Bearer sample-", ""));
  const sample = samples[index];
  const reply = result => {
    if (response.destroyed || response.writableEnded) return;
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify({ jsonrpc: "2.0", id: rpc.id, result: { structuredContent: result } }));
  };
  if (name === "agentbridge_interaction_get") {
    const started = performance.now();
    sample.claims += 1;
    if (!sample.timer) sample.timer = setTimeout(() => {
      sample.decisionAt = performance.now();
      for (const wake of sample.waiters) wake();
    }, sample.phaseMs);
    if (!sample.decisionAt && Number(args.wait_seconds) > 0) {
      await new Promise(resolveWait => {
        const wake = () => { clearTimeout(timer); sample.waiters.delete(wake); resolveWait(); };
        const timer = setTimeout(wake, Math.min(Number(args.wait_seconds), 20) * 1000);
        sample.waiters.add(wake); response.once("close", wake);
      });
    }
    reply({ waitSupported: Number(args.wait_seconds) > 0, waitSeconds: Number(args.wait_seconds) || 0,
      waitedMs: performance.now() - started, interaction: {
        schemaVersion: "agentbridge.interaction.v1", interactionId: `interaction-synthetic-${index}`,
        type: "execution_authorization", state: sample.decisionAt ? "completed" : "pending",
        title: "Synthetic benchmark", message: "Synthetic benchmark",
        presentation: { owner: "agentbridge", preferred: "url", fallback: "url", modelMustNotCollectValues: true, url: "https://bridge.test/auth/synthetic" },
        expiresAt: "2099-10-16T00:00:00Z", resume: { ready: Boolean(sample.decisionAt), completed: false },
      } });
  } else if (name === "agentbridge_host_notification_claim") {
    const claimStarted = performance.now();
    sample.claims += 1;
    if (!sample.timer) {
      // Baseline's 2 + 4 + 8 second idle sleeps place it in its 10 second
      // maximum sleep at 14 seconds. Identical decision schedule for all builds.
      sample.timer = setTimeout(() => {
        sample.decisionAt = performance.now();
        sample.pending.push({ deliveryId: `delivery-synthetic-${index}`, deliveryMode: "origin_handled",
          event: { eventType: "task.interaction.completed", payload: { interactionId: `interaction-synthetic-${index}` } },
          task: { taskId: `task-synthetic-${index}`, activeConversationRef: sample.sessionKey, originEndpointId: sample.bindingKey },
          interaction: { schemaVersion: "agentbridge.interaction.v1", interactionId: `interaction-synthetic-${index}`,
            type: "execution_authorization", state: "completed", title: "Synthetic benchmark", message: "Synthetic benchmark",
            presentation: { owner: "agentbridge", preferred: "url", fallback: "url", modelMustNotCollectValues: true, url: "https://bridge.test/auth/synthetic" },
            expiresAt: "2099-10-16T00:00:00Z", resume: { ready: true, completed: false },
          },
        });
        for (const wake of sample.waiters) wake();
      }, 14_100 + sample.phaseMs);
    }
    const take = () => ({ waitSupported: Number(args.wait_seconds) > 0, waitSeconds: Number(args.wait_seconds) || 0,
      waitedMs: performance.now() - claimStarted,
      endpoint: { clientType: "web", endpointKey: sample.bindingKey, conversationRef: sample.sessionKey },
      notifications: sample.pending.splice(0, args.limit || 10) });
    if (!sample.pending.length && Number(args.wait_seconds) > 0) {
      await new Promise(resolveWait => {
        const wake = () => { clearTimeout(timer); sample.waiters.delete(wake); resolveWait(); };
        const timer = setTimeout(wake, Math.min(Number(args.wait_seconds), 25) * 1000);
        sample.waiters.add(wake);
        response.once("close", wake);
      });
    }
    reply(take());
  } else if (name === "agentbridge_host_coordinator_lease_acquire") {
    reply({ status: "succeeded", coordinatorLease: { hostInstanceId: "openclaw-gateway", version: 1 } });
  } else if (name === "agentbridge_interaction_resume") {
    sample.commitStartedAt = performance.now();
    sample.writes += 1;
    reply({ status: "succeeded", nextAction: { type: "original_request_completed" }, taskStatus: "succeeded" });
    if (scenario === "interaction" && samples.every(item => item.writes === 1)) finish();
  } else if (name === "agentbridge_host_notification_ack") {
    sample.acks += 1;
    reply({ status: "succeeded" });
    if (samples.every(item => item.writes === 1 && item.acks === 1)) finish();
  } else {
    reply({ status: "failed", error: { code: "UNEXPECTED_SYNTHETIC_TOOL" } });
  }
});
await new Promise(resolveListen => server.listen(0, "127.0.0.1", resolveListen));
const endpoint = { url: `http://127.0.0.1:${server.address().port}`, timeoutSeconds: 40 };
for (const sample of samples) {
  const client = createAgentBridgeMcpClient({ endpoint, tokenEnv: "SAMPLE_TOKEN", env: { SAMPLE_TOKEN: `sample-${sample.index}` } });
  const binding = { key: sample.bindingKey, channel: "webchat" };
  const coordinator = new InteractionCoordinator({ config: resolvePluginConfig({ autoPoll: false, syncTimeline: false,
    wakeAgentOnComplete: false, allowedCardOrigins: ["https://bridge.test"] }), sharedState: createInteractionSharedState(),
    api: { logger: { info() {}, warn() {}, debug() {} }, runtime: { system: { enqueueSystemEvent() {}, requestHeartbeat() {} } } },
    mcpClient: client,
  });
  if (scenario === "notification") {
    coordinator.startNotificationPump({ enabled: true, configuredIdentities: () => [{ binding, client }], restoreSessionBinding: () => true });
  } else {
    sample.controller = new AbortController();
    const record = coordinator.upsert({ taskId: `task-synthetic-${sample.index}`, sessionKey: sample.sessionKey,
      interaction: { interactionId: `interaction-synthetic-${sample.index}`, type: "execution_authorization", state: "pending",
        expiresAt: "2099-10-16T00:00:00Z", resume: { ready: false, completed: false } } });
    polls.push(coordinator.poll(record, sample.controller.signal));
  }
  tasks.push(coordinator);
}
const timeout = setTimeout(() => { finish(new Error("coordinator benchmark timed out")); }, 60_000);
const failure = await completed;
clearTimeout(timeout);
for (const task of tasks) task.stopAll();
for (const sample of samples) { sample.controller?.abort(); clearTimeout(sample.timer); for (const wake of sample.waiters) wake(); }
await Promise.allSettled(tasks.map(task => task.notificationPump));
await Promise.allSettled(polls);
server.closeAllConnections();
await new Promise(resolveClose => server.close(resolveClose));
if (failure) throw failure;
const durations = samples.map(sample => sample.commitStartedAt - sample.decisionAt).sort((a, b) => a - b);
const percentile = p => durations[Math.max(0, Math.ceil(durations.length * p) - 1)];
const report = { schemaVersion: "agentbridge.coordinator-benchmark.v1", source, packageVersion, scenario,
  sourceFingerprint: sourceBefore, sourceUnchanged: sourceBefore === await sourceFingerprint(),
  observedAt: new Date().toISOString(), count, realClock: true, syntheticBoundary: "MCP notification/lease/resume service",
  containsBusinessAdapter: false, schedule: scenario === "interaction"
    ? "decision at first interaction_get + evenly-spaced 100..1900ms phase"
    : "decision at first claim + 14100ms + evenly-spaced 100..9400ms phase",
  decisionToResumeMs: { median: percentile(.5), p95: percentile(.95), maximum: durations.at(-1) },
  samples: samples.map(sample => ({ index: sample.index, phaseMs: sample.phaseMs, claims: sample.claims,
    syntheticResumeCount: sample.writes, acknowledgementCount: sample.acks,
    decisionToResumeMs: sample.commitStartedAt - sample.decisionAt })) };
await writeFile(output, JSON.stringify(report, null, 2) + "\n");
if (!report.sourceUnchanged) throw new Error("source changed during benchmark");
console.log(JSON.stringify({ count, decisionToResumeMs: report.decisionToResumeMs, output }));
