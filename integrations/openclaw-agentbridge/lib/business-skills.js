// Bind only within the authenticated host run; never retain another user's settings.
const bindings = new WeakMap();
export function skillRunKey(context, identity) {
  if (!context.runId || !context.sessionKey || !identity.binding?.key) return null;
  return JSON.stringify([identity.binding.key, context.sessionKey, context.runId]);
}
export function skillBindingMeta(router, context, identity) {
  const key = skillRunKey(context, identity);
  const id = key && bindings.get(router)?.get(key);
  return id ? { "agentbridge/skill": { bindingId: id } } : {};
}
export function rememberSkillBinding(router, context, identity, payload) {
  const key = skillRunKey(context, identity);
  if (!key || payload?.status !== "succeeded" || typeof payload.binding_id !== "string") return;
  let entries = bindings.get(router);
  if (!entries) { entries = new Map(); bindings.set(router, entries); }
  // Bound memory. An evicted active binding must fail closed rather than lose its guard.
  if (!entries.has(key) && entries.size >= 4096) throw new Error("Skill run binding capacity exceeded");
  entries.set(key, payload.binding_id);
}

export async function businessSkillContext(identity) {
  const catalog = await identity.client.callTool("agentbridge_skill_catalog", {});
  if (!Array.isArray(catalog?.items) || !catalog.items.length) return null;
  return "AgentBridge 当前用户业务助手（中央发布）：\n" +
    JSON.stringify(catalog.items.map(item => ({ id: item.id, name: item.name,
      description: item.description, version: item.version, status: item.status, profiles: item.profiles }))) +
    "\n业务助手选择规则：\n" +
    "1. 先识别本轮用户要完成的任务目标。用户明确指定助手时，先检查适用性和可用性：适用且可用必须先成功调用 agentbridge_skill_get，再执行所选任务；明显不适用则说明原因并按实际目标选择工具或助手。\n" +
    "2. 仅在用户未指定助手时，判断是否需要目录中助手提供的业务方法、判断标准或流程约束；需要则先加载，否则直接使用已有工具。业务关键词相同、只读或写入、工具调用数量，都不能单独作为加载依据。\n" +
    "3. 多个候选按任务目标选择，只有歧义影响结果时才询问。每个任务使用一个主要助手；数据库助手从当前用户获准且满足依赖的数据源中选择 source_id：只有一个适用来源时自动选择；多个来源按用户请求选择，无法确定再询问。\n" +
    "4. 加载助手与选择业务执行路径相互独立：加载后仍按业务能力要求走原子工具或持久计划；允许直接调用业务工具不表示可以跳过已选助手的加载。\n" +
    "5. 本轮独立任务不能用历史对话中的加载记录代替当前加载和绑定；已有任务续办沿用其绑定与状态。未分配、停用或缺依赖不得冒充已执行助手；不绕过失败的来源或写入约束。";
}
