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
      description: item.description, selection: item.selection, version: item.version, status: item.status, profiles: item.profiles }))) +
    "\n业务助手选择规则：\n" +
    "1. 先识别本轮用户要完成的任务目标。用户明确指定助手时，先检查适用性和可用性：适用且可用必须先成功调用 agentbridge_skill_get，再执行所选任务；明显不适用则说明原因并按实际目标选择工具或助手。\n" +
    "2. 用户未指定助手时，按所需交付结果对照目录 selection 的 use_when、not_for、output：符合助手目标且不在排除范围，必须先加载；仅要原始记录、详情或计数且不要求业务归纳判断时直接查询。工具能返回数据不等于能替代助手的方法；不得因请求简短、只读、可一次查询或历史答案已有类似总结而跳过适用助手。不能按关键词机械匹配。\n" +
    "3. 多个候选按任务目标选择，只有歧义影响结果时才询问。每个任务使用一个主要助手；数据库助手从当前用户获准且满足依赖的数据源中选择 source_id：只有一个适用来源时自动选择；多个来源按用户请求选择，无法确定再询问。\n" +
    "4. 加载成功会一次返回主说明及该模式必读资料，无需文件路径或再次读取参考文件。加载助手与业务执行路径相互独立：仍按能力要求走原子工具或持久计划；允许直接调用工具不表示可以跳过适用助手。已选助手加载失败必须说明，不得静默退回直接工具完成同一业务方法。\n" +
    "5. 本轮独立任务不能用历史对话中的加载记录代替当前加载和绑定；已有任务续办沿用其绑定与状态。未分配、停用或缺依赖不得冒充已执行助手；不绕过失败的来源或写入约束。";
}
