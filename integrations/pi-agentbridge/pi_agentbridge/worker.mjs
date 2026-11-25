// One Pi SDK run per process. Only the parent owns MCP transport and business credentials.
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

const input = createInterface({ input: process.stdin, crlfDelay: Infinity })[Symbol.asyncIterator]();
let runId;
function send(kind, values = {}) {
  process.stdout.write(JSON.stringify({ runId, kind, ...values }) + "\n");
}

async function main() {
  const first = await input.next();
  if (first.done) throw new Error("Missing request");
  const request = JSON.parse(first.value);
  runId = request.runId;
  const sdkDir = process.argv[2];
  const manifest = JSON.parse(await readFile(join(sdkDir, "package.json"), "utf8"));
  if (manifest.name !== "@earendil-works/pi-coding-agent" || manifest.version !== "1.6.0") {
    throw new Error("Unexpected Pi version");
  }
  const sdk = await import(pathToFileURL(join(sdkDir, "dist", "index.js")));
  const runtime = await sdk.ModelRuntime.create({
    authPath: request.model.authFile, modelsPath: request.model.modelsFile ?? null,
    allowModelNetwork: false,
  });
  const model = runtime.getModel(request.model.provider, request.model.id);
  if (!model || !runtime.hasConfiguredAuth(request.model.provider)) throw new Error("Pi model unavailable");
  if (request.mode === "ready") {
    let serviceOrigin = null;
    if (typeof model.baseUrl === "string") {
      try {
        const url = new URL(model.baseUrl);
        if (url.protocol === "https:" || url.protocol === "http:") serviceOrigin = url.origin;
      } catch {}
    }
    send("ready", { model: model.provider + "/" + model.id, images: model.input?.includes("image") === true, serviceOrigin });
    return;
  }
  if (request.mode === "completion") {
    const result = await runtime.completeSimple(model, {
      systemPrompt: request.system, messages: [{ role: "user", content: request.message, timestamp: Date.now() }], tools: [],
    }, { maxTokens: 6000, signal: AbortSignal.timeout(90000) });
    if (result.stopReason !== "stop" || result.content.some(item => item.type === "toolCall")) throw new Error("Incomplete evaluation");
    const text = result.content.filter(item => item.type === "text").map(item => item.text).join("\n");
    if (!text.trim() || text.length > 48000) throw new Error("Incomplete evaluation");
    const usage = {};
    for (const k of ["input", "output", "totalTokens", "cacheRead", "cacheWrite"]) {
      if (Number.isSafeInteger(result.usage?.[k]) && result.usage[k] >= 0) usage[k] = result.usage[k];
    }
    const cost = result.usage?.cost?.total;
    if (typeof cost === "number" && Number.isFinite(cost) &&
        (cost > 0 || cost === 0 && Object.values(model.cost ?? {}).some(value => value > 0))) usage.cost_estimate_usd = cost;
    send("final", { text, execution: "tool_free_completion", model: model.provider + "/" + model.id, usage });
    return;
  }
  // The complete resource set is owned by this integration. Even a newly installed
  // personal extension or local AGENTS.md cannot affect a business or sample run.
  const extensionRuntime = sdk.createExtensionRuntime();
  const loader = {
    getExtensions: () => ({ extensions: [], errors: [], runtime: extensionRuntime }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => "你是业务工作台助手。按用户要求使用本人获准的中央工具，依据工具结果回答。用户指定业务助手或请求匹配目录中的助手时，先从本人目录核实技能、模式、来源和修订，再加载执行。有多个数据来源时请用户选择。权限和业务事实以中央结果为准。不要向用户收集密码、授权凭证或私有表单。",
    getSystemPromptSource: () => undefined, getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [],
    extendResources() {}, async reload() {},
  };
  const tools = request.tools.map(definition => ({
    ...definition, label: definition.name,
    async execute(callId, args) {
      send("tool", { callId, name: definition.name, arguments: args });
      const next = await input.next();
      if (next.done) throw new Error("Tool connection closed");
      const reply = JSON.parse(next.value);
      if (reply.runId !== runId || reply.callId !== callId || reply.kind !== "result" || typeof reply.text !== "string") {
        throw new Error("Invalid tool result");
      }
      if (!Array.isArray(reply.visibleTools) || reply.visibleTools.some(name => !names.includes(name))) {
        throw new Error("Invalid visible tool set");
      }
      session.setActiveToolsByName(reply.visibleTools);
      return { content: [{ type: "text", text: reply.text }], details: {} };
    },
  }));
  // Reconstruct only finalized, public turns. In-memory mode prevents Pi from
  // retaining base64 attachments, provider diagnostics or raw tool events in JSONL.
  const manager = sdk.SessionManager.inMemory(process.cwd());
  for (const pair of request.history) {
    manager.appendMessage({ role: "user", content: pair.prompt, timestamp: Date.now() });
    // The SDK requires usage on every historical assistant message. Older
    // ledger rows carry no provider counters. Their replay-only placeholder
    // is never submitted as a sample's measured token or cost receipt.
    const usage = pair.usage ?? { input: 0, output: 0, totalTokens: 0, cacheRead: 0, cacheWrite: 0,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
    manager.appendMessage({ role: "assistant", content: [{ type: "text", text: pair.answer }], timestamp: Date.now(),
      api: model.api, provider: model.provider, model: model.id, stopReason: "stop", usage });
  }
  const names = tools.map(item => item.name);
  const { session } = await sdk.createAgentSession({
    cwd: process.cwd(), agentDir: process.env.PI_CODING_AGENT_DIR, modelRuntime: runtime, model, resourceLoader: loader,
    tools: names, customTools: tools, sessionManager: manager, thinkingLevel: "off",
    settingsManager: sdk.SettingsManager.inMemory({ retry: { enabled: false }, compaction: { enabled: false } }),
  });
  try {
    if (session.getActiveToolNames().sort().join(",") !== [...names].sort().join(",")) throw new Error("Unexpected tools");
    let settled;
    session.subscribe(event => {
      if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
        send("delta", { text: event.assistantMessageEvent.delta });
      } else if (event.type === "agent_settled") {
        settled = !event.aborted;
      }
    });
    if (request.images.length && !model.input.includes("image")) throw new Error("Model has no image input");
    await session.prompt(request.message, { expandPromptTemplates: false, images: request.images, source: "rpc" });
    await session.waitForIdle();
    const last = session.messages.at(-1);
    const text = session.getLastAssistantText();
    if (!settled || last?.role !== "assistant" || last.stopReason !== "stop" || !text?.trim()) {
      throw new Error("Incomplete Pi response");
    }
    send("final", { text, usage: last.usage });
  } finally {
    session.dispose();
  }
}

try {
  await main();
} catch {
  // SDK, tools, and provider exceptions may contain private input.
  send("failed");
  process.exitCode = 1;
} finally {
  process.stdin.destroy();
}
