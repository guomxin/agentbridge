import test from "node:test";
import assert from "node:assert/strict";
import { createTaskCommandRunner } from "../lib/task-commands.js";

function harness({ drift = false, alteredTask = false, empty = false } = {}) {
  const taskId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const endpointId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const sessionKey = "agent:main:telegram:direct:1001";
  const binding = { key: "telegram:default:1001", channel: "telegram", senderId: "1001", accountId: null };
  const route = { channel: "telegram", to: "telegram:1001", accountId: "default", threadId: null };
  const command = { taskId, commandKey: "query-request-123456", sessionKey, endpointKey: binding.key,
    endpoint: { endpointId, clientType: "telegram", externalSubject: "1001", accountId: null, conversationRef: sessionKey, route } };
  const calls = [], sent = [];
  let key = binding.key;
  const client = { async callTool(name, args, options) {
    calls.push({ name, args, options });
    if (name === "agentbridge_host_coordinator_lease_acquire") return { coordinatorLease: {
      taskId, hostInstanceId: "openclaw-gateway", state: "active", version: 4, expiresAt: new Date(Date.now() + 600_000).toISOString(),
    } };
    if (name === "agentbridge_host_task_prepare") return { status: "succeeded", queryResult: { kind: "oa_pending_query",
      taskId: alteredTask ? "foreign-task" : taskId, endpointId, count: empty ? 0 : 2, complete: true } };
    if (name === "agentbridge_result_view") {
      if (drift) key = "telegram:default:1002";
      return { status: "succeeded", resultView: { schemaVersion: "agentbridge.result-view.v1", sourceTaskId: taskId,
        presentation: { owner: "agentbridge", endpointId, modelMustNotCollectValues: true,
          url: "https://cards.example.test/results/" + "t".repeat(43) }, selectionAvailable: true,
        expiresAt: new Date(Date.now() + 3_600_000).toISOString(), sourceExpiresAt: new Date(Date.now() + 600_000).toISOString() } };
    }
    throw new Error("Unexpected tool " + name);
  } };
  const identityRouter = { resolvePinnedSession: () => ({ bound: true, client, binding: { ...binding, key } }),
    restoreSessionBinding: () => true, endpointKeyForSession: () => key };
  const coordinator = { config: { allowedCardOrigins: ["https://cards.example.test"] },
    bindDeliveryRoute() {}, async sendRoutePayload(session, target, payload, presentation, guard) {
      if (!guard()) return false;
      sent.push({ session, target, payload, presentation }); return true;
    } };
  const runner = createTaskCommandRunner({ identityRouter, coordinator });
  return { runner, command, owner: { binding, client }, calls, sent };
}

test("explicit Telegram query runs the fenced opaque command and sends the new exact-source card without a model", async () => {
  const h = harness();
  const result = await h.runner.schedule(h.command, h.owner).promise;
  assert.equal(result.status, "succeeded");
  assert.deepEqual(h.calls.map(call => call.name), ["agentbridge_host_coordinator_lease_acquire", "agentbridge_host_task_prepare", "agentbridge_result_view"]);
  assert.equal(h.calls[1].options.meta["io.agentbridge/task"].coordinatorLeaseVersion, "4");
  assert.equal(h.calls[2].options.meta["io.agentbridge/task"].endpointId, h.command.endpoint.endpointId);
  assert.deepEqual(h.calls[2].args, { operation: "issue", task_id: h.command.taskId });
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].target.to, "telegram:1001");
  assert.match(h.sent[0].payload.text, /发现 2 项/);
  assert.equal(h.sent[0].presentation.blocks.at(-1).buttons[0].label, "选择并办理 OA 待办");
  h.runner.stop();
});

test("a query card cannot cross an identity change during ticket issuance", async () => {
  const h = harness({ drift: true });
  await h.runner.schedule(h.command, h.owner).promise;
  assert.equal(h.sent.length, 0);
  h.runner.stop();
});

test("query response from another task cannot issue or deliver a result ticket", async () => {
  const h = harness({ alteredTask: true });
  await h.runner.schedule(h.command, h.owner).promise;
  assert.equal(h.calls.some(call => call.name === "agentbridge_result_view"), false);
  assert.equal(h.sent.length, 0);
  h.runner.stop();
});

test("an empty query reports its actual result without pretending a task was processed", async () => {
  const h = harness({ empty: true });
  await h.runner.schedule(h.command, h.owner).promise;
  assert.equal(h.calls.some(call => call.name === "agentbridge_result_view"), false);
  assert.equal(h.sent.length, 1);
  assert.match(h.sent[0].payload.text, /已查询，发现 0 项/);
  assert.equal(h.sent[0].presentation, null);
  h.runner.stop();
});
