import { EXPECTED_PLUGIN_VERSION } from "./fixtures.js";
import test from "node:test";
import assert from "node:assert/strict";

import {
  matchIdentityBinding,
  resolveMcpEndpoint,
  resolvePluginConfig,
} from "../lib/config.js";
import { InteractionCoordinator } from "../lib/coordinator.js";
import { AgentBridgeIdentityRouter } from "../lib/identity-router.js";
import { AGENTBRIDGE_TOOL_CATALOG } from "../lib/tool-catalog.js";
import {
  AGENTBRIDGE_AGENT_FACING_TOOL_NAMES,
  AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES,
  AGENTBRIDGE_PROXY_TOOL_NAMES,
  createAgentBridgeProxyTools,
} from "../lib/proxy-tools.js";

const MCP_URL = "https://10.10.50.213:8790/mcp";

test("normalizes bindings and prefers an account-specific identity", () => {
  const config = multiUserConfig({
    identityBindings: [
      binding("1001", "TOKEN_GENERAL", { label: "general" }),
      binding("1001", "TOKEN_ACCOUNT", {
        accountId: "oa-bot",
        label: "account-specific",
      }),
    ],
  });

  assert.equal(
    matchIdentityBinding(config.identityBindings, {
      channel: "TELEGRAM",
      senderId: 1001,
      accountId: "oa-bot",
    }).label,
    "account-specific",
  );
  assert.equal(
    matchIdentityBinding(config.identityBindings, {
      channel: "telegram",
      senderId: "1001",
      accountId: "another-bot",
    }).label,
    "general",
  );
  assert.deepEqual(resolveMcpEndpoint(config, {}), {
    url: MCP_URL,
    timeoutSeconds: 150,
  });
});

test("rejects duplicate trusted sender selectors", () => {
  assert.throws(
    () =>
      multiUserConfig({
        identityBindings: [
          binding("1001", "TOKEN_A"),
          binding("1001", "TOKEN_B"),
        ],
      }),
    /duplicate AgentBridge identity binding/,
  );
});

test("rejects WeChat sender selectors that differ only by case", () => {
  assert.throws(
    () =>
      multiUserConfig({
        identityBindings: [
          {
            channel: "openclaw-weixin",
            senderId: "WeChat-User@im.wechat",
            tokenEnv: "TOKEN_A",
          },
          {
            channel: "openclaw-weixin",
            senderId: "wechat-user@im.wechat",
            tokenEnv: "TOKEN_B",
          },
        ],
      }),
    /duplicate AgentBridge identity binding/,
  );
});

test("routes two Telegram users to different MCP bearer tokens", async () => {
  const requests = [];
  const router = createRouter({
    requests,
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
  });
  const contextA = toolContext("1001");
  const contextB = toolContext("2002");

  const identityA = router.resolveToolContext(contextA);
  const identityB = router.resolveToolContext(contextB);
  await Promise.all([
    identityA.client.callTool("oa_session_status", {}),
    identityB.client.callTool("oa_session_status", {}),
  ]);

  assert.equal(identityA.bound, true);
  assert.equal(identityB.bound, true);
  assert.deepEqual(
    requests.map((request) => request.authorization).sort(),
    ["Bearer token-a", "Bearer token-b"],
  );
  assert.equal(JSON.stringify(identityA).includes("token-a"), false);
  assert.equal(JSON.stringify(identityB).includes("token-b"), false);
});

test("recovers a Telegram identity from its private session after restart", () => {
  const router = createRouter({
    requests: [],
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
  });

  const identity = router.resolveToolContext({
    sessionKey: "agent:main:telegram:direct:1001",
  });

  assert.equal(identity.bound, true);
  assert.equal(identity.binding.senderId, "1001");
});

test("fails closed when a private session identity is account-ambiguous", () => {
  const config = multiUserConfig({
    identityBindings: [
      binding("1001", "TOKEN_A", { accountId: "bot-a" }),
      binding("1001", "TOKEN_B", { accountId: "bot-b" }),
    ],
  });
  const router = createRouter({
    requests: [],
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
    config,
  });

  const identity = router.resolveToolContext({
    sessionKey: "agent:main:telegram:direct:1001",
  });

  assert.equal(identity.bound, false);
  assert.equal(identity.reason, "identity_not_provisioned");
});

test("routes a trusted WeChat sender and bot account to its own token", async () => {
  const requests = [];
  const senderId = "wechat-user-1002@im.wechat";
  const accountId = "wechat-bot-account";
  const config = multiUserConfig({
    identityBindings: [
      {
        channel: "openclaw-weixin",
        senderId,
        accountId,
        tokenEnv: "WECHAT_TOKEN",
        label: "WeChat OA User",
      },
    ],
  });
  const router = createRouter({
    requests,
    env: { WECHAT_TOKEN: "wechat-token" },
    config,
  });
  const identity = router.resolveToolContext({
    sessionKey: `agent:main:openclaw-weixin:direct:${senderId}`,
    messageChannel: "openclaw-weixin",
    requesterSenderId: senderId,
    agentAccountId: accountId,
  });

  assert.equal(identity.bound, true);
  await identity.client.callTool("oa_session_status", {});
  assert.equal(requests[0].authorization, "Bearer wechat-token");
});

test("routes a lowercased explicit WeChat sender to its canonical binding", () => {
  const senderId = "o9cq806QdYig1P-49QIJq1wlPiMo@im.wechat";
  const accountId = "6eaa3d9b1434-im-bot";
  const config = multiUserConfig({
    identityBindings: [
      {
        channel: "openclaw-weixin",
        senderId,
        accountId,
        tokenEnv: "WECHAT_TOKEN",
      },
    ],
  });
  const router = createRouter({
    requests: [],
    env: { WECHAT_TOKEN: "wechat-token" },
    config,
  });

  const identity = router.resolveToolContext({
    sessionKey:
      "agent:main:openclaw-weixin:direct:o9cq806qdyig1p-49qijq1wlpimo@im.wechat",
    messageChannel: "openclaw-weixin",
    requesterSenderId: senderId.toLowerCase(),
    agentAccountId: accountId,
  });

  assert.equal(identity.bound, true);
  assert.equal(identity.binding.senderId, senderId);
});

test("recovers one account-specific WeChat identity from its private session", () => {
  const senderId = "wechat-user-1002@im.wechat";
  const accountId = "wechat-bot-account";
  const config = multiUserConfig({
    identityBindings: [
      {
        channel: "openclaw-weixin",
        senderId,
        accountId,
        tokenEnv: "WECHAT_TOKEN",
        label: "WeChat OA User",
      },
    ],
  });
  const router = createRouter({
    requests: [],
    env: { WECHAT_TOKEN: "wechat-token" },
    config,
  });

  const identity = router.resolveToolContext({
    sessionKey: `agent:main:openclaw-weixin:direct:${senderId}`,
  });

  assert.equal(identity.bound, true);
  assert.equal(identity.binding.accountId, accountId);
});

test("recovers the canonical WeChat identity from a lowercased private session", () => {
  const senderId = "o9cq806QdYig1P-49QIJq1wlPiMo@im.wechat";
  const accountId = "6eaa3d9b1434-im-bot";
  const config = multiUserConfig({
    identityBindings: [
      {
        channel: "openclaw-weixin",
        senderId,
        accountId,
        tokenEnv: "WECHAT_TOKEN",
        label: "WeChat OA User",
      },
    ],
  });
  const router = createRouter({
    requests: [],
    env: { WECHAT_TOKEN: "wechat-token" },
    config,
  });
  const sessionKey =
    "agent:main:openclaw-weixin:direct:o9cq806qdyig1p-49qijq1wlpimo@im.wechat";

  const identity = router.resolveToolContext({
    sessionKey,
    messageChannel: "openclaw-weixin",
    agentAccountId: accountId,
  });

  assert.equal(identity.bound, true);
  assert.equal(identity.binding.senderId, senderId);
  assert.equal(router.endpointKeyForSession(sessionKey), identity.binding.key);
});

test("does not recover a different WeChat peer from a private session", () => {
  const senderId = "o9cq806QdYig1P-49QIJq1wlPiMo@im.wechat";
  const accountId = "6eaa3d9b1434-im-bot";
  const config = multiUserConfig({
    identityBindings: [
      {
        channel: "openclaw-weixin",
        senderId,
        accountId,
        tokenEnv: "WECHAT_TOKEN",
      },
    ],
  });
  const router = createRouter({
    requests: [],
    env: { WECHAT_TOKEN: "wechat-token" },
    config,
  });

  const identity = router.resolveToolContext({
    sessionKey:
      "agent:main:openclaw-weixin:direct:another-user@im.wechat",
    messageChannel: "openclaw-weixin",
    agentAccountId: accountId,
  });

  assert.equal(identity.bound, false);
  assert.equal(identity.reason, "identity_not_provisioned");
});

test("reuses the trusted inbound binding when WeChat omits SenderId", () => {
  const senderId = "o9cq806QdYig1P-49QIJq1wlPiMo@im.wechat";
  const accountId = "6eaa3d9b1434-im-bot";
  const sessionKey =
    "agent:main:openclaw-weixin:direct:o9cq806qdyig1p-49qijq1wlpimo@im.wechat";
  const config = multiUserConfig({
    identityBindings: [
      {
        channel: "openclaw-weixin",
        senderId,
        accountId,
        tokenEnv: "WECHAT_TOKEN",
        label: "WeChat OA User",
      },
    ],
  });
  const router = createRouter({
    requests: [],
    env: { WECHAT_TOKEN: "wechat-token" },
    config,
  });

  assert.equal(
    router.bindSession({
      sessionKey,
      channel: "openclaw-weixin",
      senderId,
      accountId,
    }),
    true,
  );
  const identity = router.resolveToolContext({
    sessionKey,
    messageChannel: "openclaw-weixin",
    agentAccountId: accountId,
  });

  assert.equal(identity.bound, true);
  assert.equal(identity.binding.label, "WeChat OA User");
});

test("recovers a WeChat private sender from matching delivery context", () => {
  const senderId = "o9cq806QdYig1P-49QIJq1wlPiMo@im.wechat";
  const accountId = "6eaa3d9b1434-im-bot";
  const sessionKey =
    "agent:main:openclaw-weixin:direct:o9cq806qdyig1p-49qijq1wlpimo@im.wechat";
  const config = multiUserConfig({
    identityBindings: [
      {
        channel: "openclaw-weixin",
        senderId,
        accountId,
        tokenEnv: "WECHAT_TOKEN",
        label: "WeChat OA User",
      },
    ],
  });
  const router = createRouter({
    requests: [],
    env: { WECHAT_TOKEN: "wechat-token" },
    config,
  });
  const identity = router.resolveToolContext({
    sessionKey,
    messageChannel: "openclaw-weixin",
    agentAccountId: accountId,
    deliveryContext: {
      channel: "openclaw-weixin",
      to: senderId,
      accountId,
    },
  });

  assert.equal(identity.bound, true);
  assert.equal(identity.binding.label, "WeChat OA User");
});

test("rejects WeChat delivery fallback outside the matching private chat", () => {
  const senderId = "wechat-user-1002@im.wechat";
  const accountId = "wechat-bot-account";
  const config = multiUserConfig({
    identityBindings: [
      {
        channel: "openclaw-weixin",
        senderId,
        accountId,
        tokenEnv: "WECHAT_TOKEN",
      },
    ],
  });
  const router = createRouter({
    requests: [],
    env: { WECHAT_TOKEN: "wechat-token" },
    config,
  });
  const contexts = [
    {
      sessionKey:
        "agent:main:openclaw-weixin:direct:another-user@im.wechat",
      messageChannel: "openclaw-weixin",
      agentAccountId: accountId,
      deliveryContext: {
        channel: "openclaw-weixin",
        to: senderId,
        accountId,
      },
    },
    {
      sessionKey: `agent:main:openclaw-weixin:group:${senderId}`,
      messageChannel: "openclaw-weixin",
      agentAccountId: accountId,
      deliveryContext: {
        channel: "openclaw-weixin",
        to: senderId,
        accountId,
      },
    },
    {
      sessionKey: `agent:main:telegram:direct:${senderId}`,
      messageChannel: "telegram",
      agentAccountId: accountId,
      deliveryContext: {
        channel: "telegram",
        to: senderId,
        accountId,
      },
    },
  ];

  for (const context of contexts) {
    const identity = router.resolveToolContext(context);
    assert.equal(identity.bound, false);
  }
});

test("fails closed when pinned session account changes", () => {
  const senderId = "wechat-user-1002@im.wechat";
  const accountId = "wechat-bot-account";
  const sessionKey =
    "agent:main:openclaw-weixin:direct:wechat-user-1002@im.wechat";
  const config = multiUserConfig({
    identityBindings: [
      {
        channel: "openclaw-weixin",
        senderId,
        accountId,
        tokenEnv: "WECHAT_TOKEN",
      },
    ],
  });
  const router = createRouter({
    requests: [],
    env: { WECHAT_TOKEN: "wechat-token" },
    config,
  });

  assert.equal(
    router.bindSession({
      sessionKey,
      channel: "openclaw-weixin",
      senderId,
      accountId,
    }),
    true,
  );
  const identity = router.resolveToolContext({
    sessionKey,
    messageChannel: "openclaw-weixin",
    agentAccountId: "another-bot-account",
  });

  assert.equal(identity.bound, false);
  assert.equal(identity.reason, "session_identity_conflict");
  assert.equal(router.clientForSession(sessionKey), null);
});

test("does not poison a pinned Telegram identity when webchat inspects its session", () => {
  const router = createRouter({
    requests: [],
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
  });
  const sessionKey = "agent:main:telegram:direct:1001";

  assert.equal(
    router.resolveToolContext(toolContext("1001", sessionKey)).bound,
    true,
  );
  const webchatContext = router.resolveToolContext({
    sessionKey,
    messageChannel: "webchat",
  });

  assert.equal(webchatContext.bound, false);
  assert.equal(webchatContext.reason, "session_identity_conflict");
  assert.notEqual(router.clientForSession(sessionKey), null);
  assert.equal(
    router.resolveToolContext(toolContext("1001", sessionKey)).bound,
    true,
  );
});

test("fails closed when one OpenClaw session changes Telegram identity", () => {
  const router = createRouter({
    requests: [],
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
  });
  const sessionKey = "agent:main:telegram:direct:shared-session";

  assert.equal(
    router.resolveToolContext(toolContext("1001", sessionKey)).bound,
    true,
  );
  const conflicted = router.resolveToolContext(toolContext("2002", sessionKey));

  assert.equal(conflicted.bound, false);
  assert.equal(conflicted.reason, "session_identity_conflict");
  assert.equal(router.clientForSession(sessionKey), null);
});

test("keeps a one-use-bound workspace session pinned across web channel metadata", () => {
  const router = createRouter({
    requests: [],
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
  });
  const sessionKey =
    "agent:main:agentbridge-workspace:direct:account-123";
  const bindingKey = router.config.identityBindings[0].key;

  assert.equal(
    router.restoreSessionBinding({ sessionKey, bindingKey }),
    true,
  );
  const identity = router.resolveToolContext({
    sessionKey,
    messageChannel: "webchat",
    agentAccountId: "workspace-account",
  });

  assert.equal(identity.bound, true);
  assert.equal(identity.binding.key, bindingKey);
});

test("workspace and direct sessions expose the same governed capabilities", () => {
  const router = createRouter({
    requests: [],
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
  });
  const sessionKey =
    "agent:main:agentbridge-workspace:direct:account-123";
  const bindingKey = router.config.identityBindings[0].key;
  assert.equal(
    router.restoreSessionBinding({ sessionKey, bindingKey }),
    true,
  );

  const workspaceTools = createAgentBridgeProxyTools({
    context: {
      sessionKey,
      messageChannel: "webchat",
    },
    identityRouter: router,
    serverName: "agentbridge",
  });
  const directTools = createAgentBridgeProxyTools({
    context: toolContext("1001"),
    identityRouter: router,
    serverName: "agentbridge",
  });

  assert.deepEqual(
    workspaceTools.map((tool) => tool.name),
    directTools.map((tool) => tool.name),
  );
  assert.deepEqual(
    workspaceTools.map((tool) => tool.name),
    AGENTBRIDGE_AGENT_FACING_TOOL_NAMES,
  );
  const governedWrites = workspaceTools
    .slice(1)
    .filter((tool) => tool.annotations?.readOnlyHint !== true)
    .map((tool) => tool.name)
    .sort();
  assert.deepEqual(
    governedWrites,
    [...AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES, "agentbridge_skill_authoring"].sort(),
  );
});

test("scope profiles give one identity the same reduced catalog on web and chat", async () => {
  const requests = [];
  const config = multiUserConfig({
    identityBindings: [binding("1001", "TOKEN_A", { label: "limited" })],
  });
  const router = createRouter({
    requests,
    env: { TOKEN_A: "token-a" },
    config,
    responseForTool(name) {
      if (name === "agentbridge_server_profile") {
        return {
          structuredContent: {
            status: "succeeded",
            negotiation: { acceptedLevel: "L3" },
          },
        };
      }
      if (name === "agentbridge_host_identity_profile") {
        return {
          structuredContent: {
            status: "succeeded",
            identity: {
              userSubject: "user-a",
              scopes: ["oa:read"],
              expiresAt: "2099-01-01T00:00:00+00:00",
            },
            agentToolAccess: {
              allowedToolNames: [
                "agentbridge_server_profile",
                "oa_template_list",
                "oa_session_status",
                "oa_session_login",
              ],
            },
          },
        };
      }
      return null;
    },
  });
  const profiles = await router.refreshIdentityProfiles();
  const sessionKey =
    "agent:main:agentbridge-workspace:direct:account-123";
  const bindingKey = router.config.identityBindings[0].key;
  assert.equal(
    router.restoreSessionBinding({ sessionKey, bindingKey }),
    true,
  );

  const workspaceNames = createAgentBridgeProxyTools({
    context: { sessionKey, messageChannel: "webchat" },
    identityRouter: router,
    serverName: "agentbridge",
  }).map((tool) => tool.name);
  const directNames = createAgentBridgeProxyTools({
    context: toolContext("1001"),
    identityRouter: router,
    serverName: "agentbridge",
  }).map((tool) => tool.name);

  assert.equal(profiles.length, 1);
  assert.deepEqual(workspaceNames, directNames);
  assert.deepEqual(workspaceNames, [
    "agentbridge_identity_status",
    "agentbridge_server_profile",
    "oa_template_list",
    "oa_session_status",
    "oa_session_login",
  ]);
  assert.equal(workspaceNames.includes("oa_business_trip_prepare"), false);
  assert.equal(workspaceNames.includes("yuque_document_search"), false);
  assert.equal(
    requests[0].body.params.name,
    "agentbridge_server_profile",
  );
  assert.equal(
    requests[1].body.params.name,
    "agentbridge_host_identity_profile",
  );
});

test("agent-facing catalogs hide internal commit and continuation tools", () => {
  const router = createRouter({
    requests: [],
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
  });
  const tools = createAgentBridgeProxyTools({
    context: toolContext("1001"),
    identityRouter: router,
    serverName: "agentbridge",
  });
  const visible = new Set(tools.map((tool) => tool.name));
  const governed = new Set(AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES);

  assert.equal(visible.has("oa_business_trip_submit_prepare"), true);
  assert.equal(visible.has("oa_missed_punch_approval_prepare"), true);
  assert.equal(visible.has("oa_workflow_revoke_prepare"), true);
  assert.equal(visible.has("oa_meeting_room_availability_list"), true);
  assert.equal(visible.has("oa_meeting_room_my_applications_list"), true);
  assert.equal(visible.has("oa_meeting_room_application_prepare"), true);
  assert.equal(visible.has("oa_meeting_room_application_create"), false);
  assert.equal(visible.has("oa_meeting_room_application_cancel_prepare"), true);
  assert.equal(visible.has("oa_meeting_room_application_cancel"), false);
  assert.equal(visible.has("oa_meeting_create_prepare"), true);
  assert.equal(visible.has("taihua_work_log_create_prepare"), true);
  const expectedGoverned = AGENTBRIDGE_TOOL_CATALOG
    .filter(
      (tool) =>
        tool.name.endsWith("_prepare") ||
        tool.name.endsWith("_session_login") ||
        ["agentbridge_task_plan_cancel", "agentbridge_task_cancel"].includes(tool.name),
    )
    .map((tool) => tool.name)
    .sort();
  assert.deepEqual(
    [...AGENTBRIDGE_GOVERNED_ENTRY_TOOL_NAMES].sort(),
    expectedGoverned,
  );
  for (const descriptor of AGENTBRIDGE_TOOL_CATALOG) {
    if (
      descriptor.name === "agentbridge_skill_authoring" || descriptor.annotations?.readOnlyHint === true ||
      governed.has(descriptor.name)
    ) {
      continue;
    }
    assert.equal(visible.has(descriptor.name), false, descriptor.name);
  }
});

test("workspace sessions recover a missing in-memory identity binding", async () => {
  const requests = [];
  const router = createRouter({
    requests,
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
    responseForTool(name, body) {
      if (name === "agentbridge_host_workspace_session_resolve") {
        return body.params?.arguments?.session_key?.endsWith("account-123")
          ? {
              structuredContent: {
                status: "succeeded",
                binding: {
                  endpointKey: "workspace:account-123",
                  sessionKey:
                    "agent:main:agentbridge-workspace:direct:account-123",
                },
              },
            }
          : {
              structuredContent: {
                status: "failed",
              },
            };
      }
      return {
        structuredContent: {
          status: "succeeded",
          authenticated: true,
        },
      };
    },
  });
  const sessionKey =
    "agent:main:agentbridge-workspace:direct:account-123";
  const tools = createAgentBridgeProxyTools({
    context: {
      sessionKey,
      messageChannel: "webchat",
    },
    identityRouter: router,
    serverName: "agentbridge",
  });

  assert.equal(tools.length > 1, true);
  const tool = tools.find((item) => item.name === "oa_session_status");
  const result = await tool.execute("workspace-call", {});

  assert.equal(result.structuredContent.authenticated, true);
  assert.equal(router.statusForSession(sessionKey).state, "bound");
  assert.deepEqual(
    requests.map((request) => request.body.params.name),
    [
      "agentbridge_host_workspace_session_resolve",
      "oa_session_status",
    ],
  );
});

test("workspace sessions recover a missing endpoint for a pinned identity", async () => {
  const requests = [];
  const router = createRouter({
    requests,
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
    responseForTool(name) {
      if (name === "agentbridge_host_workspace_session_resolve") {
        return {
          structuredContent: {
            status: "succeeded",
            binding: {
              endpointKey: "workspace:account-123",
              sessionKey:
                "agent:main:agentbridge-workspace:direct:account-123",
            },
          },
        };
      }
      return {
        structuredContent: {
          status: "succeeded",
        },
      };
    },
  });
  const sessionKey =
    "agent:main:agentbridge-workspace:direct:account-123";
  const bindingKey = router.config.identityBindings[0].key;
  assert.equal(
    router.restoreSessionBinding({ sessionKey, bindingKey }),
    true,
  );
  assert.equal(router.endpointKeyForSession(sessionKey), null);

  const resolved = await router.resolveWorkspaceSession(sessionKey);

  assert.equal(resolved.bound, true);
  assert.equal(
    router.endpointKeyForSession(sessionKey),
    "workspace:account-123",
  );
  assert.deepEqual(
    requests.map((request) => request.body.params.name),
    ["agentbridge_host_workspace_session_resolve"],
  );
});

test("workspace tasks keep the web endpoint distinct from the bearer binding", async () => {
  const requests = [];
  const router = createRouter({
    requests,
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
    responseForTool(name) {
      if (name === "agentbridge_host_workspace_session_resolve") {
        return {
          structuredContent: {
            status: "succeeded",
            binding: {
              endpointKey: "workspace:account-123",
              sessionKey:
                "agent:main:agentbridge-workspace:direct:account-123",
            },
          },
        };
      }
      if (name === "agentbridge_host_task_ensure") {
        return {
          structuredContent: {
            status: "succeeded",
            task: { taskId: "task-workspace-1234567890" },
          },
        };
      }
      return {
        structuredContent: {
          status: "succeeded",
          result: { count: 0, items: [] },
        },
      };
    },
  });
  const sessionKey =
    "agent:main:agentbridge-workspace:direct:account-123";
  const tools = createAgentBridgeProxyTools({
    context: {
      sessionKey,
      messageChannel: "webchat",
      runId: "workspace-run",
    },
    identityRouter: router,
    serverName: "agentbridge",
  });

  const tool = tools.find((item) => item.name === "oa_workflow_pending_list");
  await tool.execute("workspace-call", { limit: 5 });

  const ensure = requests.find(
    (request) =>
      request.body.params.name === "agentbridge_host_task_ensure",
  );
  assert.equal(
    ensure.body.params.arguments.endpoint_key,
    "workspace:account-123",
  );
  assert.equal(ensure.body.params.arguments.client_type, "web");
  assert.equal(ensure.body.params.arguments.external_subject, "account-123");
  assert.deepEqual(ensure.body.params.arguments.route, {});
  assert.equal(
    router.endpointKeyForSession(sessionKey),
    "workspace:account-123",
  );
});

test("withholds OA tools from an unprovisioned Telegram user", async () => {
  const router = createRouter({
    requests: [],
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
  });
  const tools = createAgentBridgeProxyTools({
    context: toolContext("9999"),
    identityRouter: router,
    serverName: "agentbridge",
  });

  assert.deepEqual(tools.map((tool) => tool.name), [
    "agentbridge_identity_status",
  ]);
  const result = await tools[0].execute("status", {});
  assert.equal(result.details.structuredContent.status, "unbound");
  assert.equal(
    result.details.structuredContent.reason,
    "identity_not_provisioned",
  );
});

test("exposes the governed catalog and proxies raw MCP metadata for a bound user", async () => {
  const requests = [];
  const router = createRouter({
    requests,
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
    responseResult: {
      content: [{ type: "text", text: "AgentBridge status" }],
      structuredContent: { status: "succeeded", authenticated: true },
      _meta: { "io.agentbridge/test": { private: true } },
    },
  });
  const tools = createAgentBridgeProxyTools({
    context: toolContext("1001"),
    identityRouter: router,
    serverName: "agentbridge",
  });

  assert.equal(tools.length, AGENTBRIDGE_AGENT_FACING_TOOL_NAMES.length);
  assert.equal(AGENTBRIDGE_PROXY_TOOL_NAMES.length, tools.length);
  assert.equal(AGENTBRIDGE_TOOL_CATALOG.length > tools.length, true);
  assert.equal(new Set(tools.map((tool) => tool.name)).size, tools.length);
  const statusTool = tools.find((tool) => tool.name === "oa_session_status");
  const result = await statusTool.execute("tool-call", {});

  assert.equal(requests[0].authorization, "Bearer token-a");
  assert.equal(requests[0].body.params.name, "oa_session_status");
  assert.equal(result.details.mcpServer, "agentbridge");
  assert.equal(result.details.mcpTool, "oa_session_status");
  assert.equal(Object.hasOwn(result.details, "structuredContent"), false);
  assert.equal(result.structuredContent.authenticated, true);
  assert.deepEqual(result._meta, {
    "io.agentbridge/test": { private: true },
  });
});

test("keeps large MCP payloads out of middleware details", async () => {
  const requests = [];
  const items = Array.from({ length: 1_100 }, (_, index) => ({
    id: `energy-${index}`,
    value: index,
  }));
  const router = createRouter({
    requests,
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
    responseForTool(name) {
      if (name === "agentbridge_host_task_ensure") {
        return { structuredContent: { task: { taskId: "task-large-payload" } } };
      }
    },
    responseResult: {
      content: [{ type: "text", text: JSON.stringify({ items }) }],
      structuredContent: { status: "succeeded", result: { items } },
    },
  });
  const tools = createAgentBridgeProxyTools({
    context: toolContext("1001"),
    identityRouter: router,
    serverName: "agentbridge",
  });

  const energyList = tools.find(
    (tool) => tool.name === "smartlight_energy_record_list",
  );
  const result = await energyList.execute("large-energy-list", {
    last_days: 7,
  });

  assert.equal(result.structuredContent.result.items.length, 1_100);
  assert.deepEqual(result.details, {
    mcpServer: "agentbridge",
    mcpTool: "smartlight_energy_record_list",
    agentbridgeTaskId: "task-large-payload",
  });
});

test("normalizes business arguments before the MCP request", async () => {
  const requests = [];
  const router = createRouter({
    requests,
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
    responseForTool(name) {
      if (name === "agentbridge_host_task_ensure") {
        return { structuredContent: { task: { taskId: "task-normalized-args" } } };
      }
    },
    responseResult: {
      structuredContent: { status: "succeeded", result: { items: [] } },
    },
  });
  const tools = createAgentBridgeProxyTools({
    context: toolContext("1001"),
    identityRouter: router,
    serverName: "agentbridge",
    argumentNormalizer({ toolName, params }) {
      assert.equal(toolName, "smartlight_alarm_list");
      return { ...params, sort_by: "occurred_at" };
    },
  });

  const alarmList = tools.find(
    (tool) => tool.name === "smartlight_alarm_list",
  );
  await alarmList.execute("alarm-list", {
    sort_by: "last_activity",
    size: 1,
  });

  const alarmRequest = requests.find(
    (request) => request.body.params.name === "smartlight_alarm_list",
  );
  assert.ok(alarmRequest);
  assert.equal(
    alarmRequest.body.params.arguments.sort_by,
    "occurred_at",
  );
});

test("creates and observes one host-owned task without model task arguments", async () => {
  const requests = [];
  const router = createRouter({
    requests,
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
    responseForTool(name) {
      if (name === "agentbridge_host_task_ensure") {
        return {
          structuredContent: {
            status: "succeeded",
            task: { taskId: "task-1234567890-abcdef" },
          },
        };
      }
      if (name === "agentbridge_host_task_observe") {
        return { structuredContent: { status: "succeeded" } };
      }
      return {
        structuredContent: {
          status: "requires_user_action",
          operationId: "operation-123",
          interaction: {
            interactionId: "interaction-1234567890",
          },
        },
      };
    },
  });
  const tools = createAgentBridgeProxyTools({
    context: {
      ...toolContext("1001"),
      runId: "run-42",
      deliveryContext: {
        channel: "telegram",
        to: "1001",
      },
    },
    identityRouter: router,
    serverName: "agentbridge",
  });

  const tool = tools.find((item) => item.name === "oa_workflow_pending_list");
  const result = await tool.execute("tool-call-42", { limit: 5 });

  assert.deepEqual(
    requests.map((request) => request.body.params.name),
    [
      "agentbridge_host_task_ensure",
      "agentbridge_host_coordinator_lease_acquire",
      "oa_workflow_pending_list",
      "agentbridge_host_task_observe",
    ],
  );
  assert.equal(
    requests[0].body.params.arguments.host_task_key,
    "agent:main:telegram:direct:1001|tool-call-42",
  );
  assert.equal(
    Object.hasOwn(
    requests[2].body.params.arguments,
      "task_id",
    ),
    false,
  );
  assert.deepEqual(requests[2].body.params._meta, {
    "io.agentbridge/host-context": {
      version: "1",
      agentHost: "openclaw",
      hostInstanceId: "openclaw-gateway",
      hostVersion: EXPECTED_PLUGIN_VERSION,
    },
    "io.agentbridge/task": {
      taskId: "task-1234567890-abcdef",
      hostRunId: "tool-call-42",
      toolCallId: "tool-call-42",
      coordinatorLeaseVersion: "1",
    },
  });
  assert.deepEqual(
    requests[3].body.params.arguments.operation_ids,
    ["operation-123"],
  );
  assert.deepEqual(
    requests[3].body.params.arguments.interaction_ids,
    ["interaction-1234567890"],
  );
  assert.equal(
    result.details.agentbridgeTaskId,
    "task-1234567890-abcdef",
  );
});

test("stops the business call when task context cannot be established", async () => {
  const requests = [];
  const warnings = [];
  const router = createRouter({
    requests,
    env: { TOKEN_A: "token-a", TOKEN_B: "token-b" },
    responseForTool(name) {
      if (name === "agentbridge_host_task_ensure") {
        return {
          structuredContent: {
            status: "failed",
            error: { code: "TASK_HUB_UNAVAILABLE" },
          },
        };
      }
      return {
        structuredContent: {
          status: "succeeded",
          result: { count: 2, items: [] },
        },
      };
    },
  });
  const tools = createAgentBridgeProxyTools({
    context: toolContext("1001"),
    identityRouter: router,
    serverName: "agentbridge",
    logger: { warn(message) { warnings.push(message); } },
  });

  const tool = tools.find((item) => item.name === "oa_workflow_pending_list");
  const result = await tool.execute("tool-call-no-task", { limit: 2 });

  assert.deepEqual(
    requests.map((request) => request.body.params.name),
    ["agentbridge_host_task_ensure"],
  );
  assert.equal(result.details.structuredContent.error.code, "TASK_CONTEXT_UNAVAILABLE");
  assert.equal(result.details.agentbridgeTaskId, null);
  assert.deepEqual(warnings, [
    "AgentBridge task creation returned no task ID; business call stopped",
  ]);
});

test("pins coordinator records to their originating session clients", () => {
  const clients = {
    "session-a": { name: "client-a" },
    "session-b": { name: "client-b" },
  };
  const coordinator = new InteractionCoordinator({
    api: { logger: { info() {}, warn() {} } },
    config: {
      allowedCardOrigins: [],
      autoPoll: false,
      maxPollSeconds: 30,
      pollIntervalSeconds: 1,
      wakeAgentOnComplete: false,
    },
    mcpClientResolver: (sessionKey) => clients[sessionKey] || null,
  });

  const first = coordinator.upsert({
    interaction: { interactionId: "interaction-a-123456" },
    sessionKey: "session-a",
    runId: "run-a",
  });
  const second = coordinator.upsert({
    interaction: { interactionId: "interaction-b-123456" },
    sessionKey: "session-b",
    runId: "run-b",
  });

  assert.equal(first.mcpClient, clients["session-a"]);
  assert.equal(second.mcpClient, clients["session-b"]);
});

function multiUserConfig(overrides = {}) {
  return resolvePluginConfig({
    allowedCardOrigins: ["https://10.10.50.213:8780"],
    mcpUrl: MCP_URL,
    identityBindings: [
      binding("1001", "TOKEN_A", { label: "User A" }),
      binding("2002", "TOKEN_B", { label: "User B" }),
    ],
    ...overrides,
  });
}

function binding(senderId, tokenEnv, overrides = {}) {
  return {
    channel: "telegram",
    senderId,
    tokenEnv,
    ...overrides,
  };
}

function toolContext(
  senderId,
  sessionKey = `agent:main:telegram:direct:${senderId}`,
) {
  return {
    sessionKey,
    messageChannel: "telegram",
    requesterSenderId: senderId,
  };
}

function createRouter({
  requests,
  env,
  responseResult = null,
  responseForTool = null,
  config = multiUserConfig(),
}) {
  return new AgentBridgeIdentityRouter({
    config,
    hostConfig: {},
    env,
    fetchImpl: async (_url, options) => {
      const body = JSON.parse(options.body);
      requests.push({
        authorization: options.headers.Authorization,
        body,
      });
      const selectedResult =
        typeof responseForTool === "function"
          ? responseForTool(body.params?.name, body)
          : null;
      const normalizedResult =
        body.params?.name ===
          "agentbridge_host_coordinator_lease_acquire" &&
        !selectedResult?.structuredContent?.coordinatorLease
          ? {
              structuredContent: {
                status: "succeeded",
                coordinatorLease: {
                  taskId: body.params.arguments.task_id,
                  hostInstanceId: "openclaw-gateway",
                  version: 1,
                  state: "active",
                },
              },
            }
          : selectedResult;
      return new Response(
        JSON.stringify({
          jsonrpc: "2.0",
          id: "response",
          result:
            normalizedResult ||
            responseResult || {
              content: [
                {
                  type: "text",
                  text: JSON.stringify({ status: "succeeded" }),
                },
              ],
            },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    },
  });
}
