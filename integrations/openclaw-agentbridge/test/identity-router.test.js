import test from "node:test";
import assert from "node:assert/strict";

import { resolvePluginConfig } from "../lib/config.js";
import { AgentBridgeIdentityRouter } from "../lib/identity-router.js";
import { createAgentBridgeProxyTools } from "../lib/proxy-tools.js";

const version = digit => `sha256:${digit.repeat(64)}`;

function harness() {
  const calls = [];
  const identities = new Map([
    ["token-a", { tools: ["oa_session_status"], version: version("1") }],
    ["token-b", { tools: ["oa_template_list"], version: version("2") }],
  ]);
  const config = resolvePluginConfig({
    mcpUrl: "https://bridge.test/mcp",
    identityBindings: [
      { channel: "telegram", senderId: "1001", tokenEnv: "TOKEN_A" },
      { channel: "telegram", senderId: "1002", tokenEnv: "TOKEN_B" },
    ],
  });
  const router = new AgentBridgeIdentityRouter({
    config, hostConfig: {}, env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
    fetchImpl: async (_url, options) => {
      const body = JSON.parse(options.body);
      const token = options.headers.Authorization.replace("Bearer ", "");
      const identity = identities.get(token);
      const name = body.params.name;
      calls.push({ token, name });
      if (identity.hold) await identity.hold;
      if (identity.fail && name === "agentbridge_host_identity_profile") {
        throw Object.assign(new Error("offline"), { code: "TEST_OFFLINE" });
      }
      const result = name === "agentbridge_server_profile"
        ? { status: "succeeded", negotiation: { acceptedLevel: "L3" } }
        : { status: "succeeded", identity: { userSubject: token, scopes: [] },
            agentToolAccess: { allowedToolNames: identity.tools },
            catalogVersion: version("a"), permissionVersion: identity.version };
      return new Response(JSON.stringify({ jsonrpc: "2.0", id: body.id,
        result: { structuredContent: result } }),
      { status: 200, headers: { "Content-Type": "application/json" } });
    },
  });
  return { router, calls, identities, a: config.identityBindings[0], b: config.identityBindings[1] };
}

test("targeted profile refresh updates added and revoked tools on web and chat only for its identity", async () => {
  const { router, calls, identities, a, b } = harness();
  await router.refreshIdentityProfiles();
  const otherTools = router.allowedToolNamesForBinding(b);
  const sessionKey = "agent:main:agentbridge-workspace:direct:account-a";
  assert.equal(router.restoreSessionBinding({ sessionKey, bindingKey: a.key }), true);
  identities.get("token-a").tools = ["oa_session_status", "yuque_document_search"];
  identities.get("token-a").version = version("3");
  calls.length = 0;

  await router.refreshIdentityProfiles({ bindingKey: a.key });

  assert.deepEqual(calls.map(call => call.token), ["token-a", "token-a"]);
  assert.equal(router.permissionVersionForBinding(a), version("3"));
  assert.equal(router.allowedToolNamesForBinding(b), otherTools);
  const visible = context => createAgentBridgeProxyTools({ context, identityRouter: router,
    serverName: "agentbridge" }).map(tool => tool.name);
  const webTools = visible({ sessionKey, messageChannel: "webchat" });
  assert.deepEqual(webTools, visible({ sessionKey: "agent:main:telegram:direct:1001" }));
  assert.equal(webTools.includes("yuque_document_search"), true);

  identities.get("token-a").tools = ["yuque_document_search"];
  identities.get("token-a").version = version("4");
  await router.refreshIdentityProfiles({ bindingKey: a.key });
  assert.equal(visible({ sessionKey }).includes("oa_session_status"), false);
  assert.equal(router.allowedToolNamesForBinding(b), otherTools);
});

test("concurrent refreshes share one request pair per binding while different identities progress", async () => {
  const { router, calls, identities, a, b } = harness();
  let release;
  identities.get("token-a").hold = new Promise(resolve => { release = resolve; });
  const first = router.refreshIdentityProfiles({ bindingKey: a.key });
  const second = router.refreshIdentityProfiles({ bindingKey: a.key });
  await router.refreshIdentityProfiles({ bindingKey: b.key });
  assert.equal(router.permissionVersionForBinding(b), version("2"));
  assert.equal(router.permissionVersionForBinding(a), null);
  release();
  await Promise.all([first, second]);
  for (const token of ["token-a", "token-b"]) {
    assert.deepEqual(calls.filter(call => call.token === token).map(call => call.name),
      ["agentbridge_server_profile", "agentbridge_host_identity_profile"]);
  }
  assert.equal(router.identityProfileRefreshes.size, 0);
});

test("failed refresh retains the old permission stamp and permits a later retry", async () => {
  const { router, identities, a } = harness();
  await router.refreshIdentityProfiles({ bindingKey: a.key });
  const original = router.allowedToolNamesForBinding(a);
  identities.get("token-a").fail = true;
  identities.get("token-a").version = version("3");
  identities.get("token-a").tools = ["yuque_document_search"];
  assert.deepEqual(await router.refreshIdentityProfiles({ bindingKey: a.key }), []);
  assert.equal(router.permissionVersionForBinding(a), version("1"));
  assert.equal(router.allowedToolNamesForBinding(a), original);
  identities.get("token-a").fail = false;
  await router.refreshIdentityProfiles({ bindingKey: a.key });
  assert.equal(router.permissionVersionForBinding(a), version("3"));
  assert.deepEqual([...router.allowedToolNamesForBinding(a)], ["yuque_document_search"]);
});
