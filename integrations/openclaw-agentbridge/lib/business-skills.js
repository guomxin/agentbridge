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

export async function businessSkillContext(identity, prompt = "") {
  const catalog = await identity.client.callTool("agentbridge_skill_catalog", {});
  const authoring = catalog?.authoring ? "业务助手创作：所有已认证用户均可用 agentbridge_skill_authoring，无需业务权限。用户要求做助手时，优先 generate(material,request_key) 后台提炼并用 jobs 查看；也可 save 按工具结构化 schema 保存。先 list/get 检查已有草稿，inspect 查看诊断与差异。过程材料只含用户选定范围，删除业务原文、姓名、凭据；未知历史不编造。试用分为 test/test_result 人工样例和 evaluate 独立无工具评测，均不代表真实业务通过。发布用 submit，默认仅自己，必须管理员审批。自动沉淀只在总开关与范围授权同时开启时由后台处理，不能自行开启或自动提交。当前总开关：" + (catalog.authoring.value?.auto_draft ? "已开启。" : "关闭。") + "工具说明含完整参数。\n" : "";
  if (!Array.isArray(catalog?.items) || !catalog.items.length) return authoring || null;
  let items = catalog.items;
  if (items.length > 20) {
    const grams = text => new Set(Array.from(String(text).toLowerCase()).slice(0,-1).map((c,i)=>c+String(text).toLowerCase()[i+1]));
    const query = grams(prompt);
    const score = item => { const words = grams(item.name+item.description+JSON.stringify(item.selection)); return [...query].filter(w=>words.has(w)).length; };
    items = [...items].sort((a,b)=>score(b)-score(a)).slice(0,20);
  }
  return authoring + (catalog.items.length>20 ? `目录共 ${catalog.items.length} 项，当前仅列候选；未匹配或用户指定未列出的助手时，调用 agentbridge_skill_authoring discover 检索，不能声称不存在。\n` : "") + "AgentBridge 当前用户业务助手（中央发布）：\n" +
    JSON.stringify(items.map(item => ({ id: item.id, name: item.name,
      description: item.description, selection: item.selection, version: item.version, status: item.status, profiles: item.profiles }))) +
    "\n业务助手选择规则：\n" +
    "1. 先识别本轮用户要完成的任务目标。用户明确指定助手时，先检查适用性和可用性：适用且可用必须先成功调用 agentbridge_skill_get，再执行所选任务；不适用时按实际目标选择工具或助手，并在最终答复开头用一句话交代指定助手的适用范围、为何与本次目标不符，以及实际采用的查询或处理方式。不能仅展示结果而省略这项说明，也不能声称使用了未加载的助手。\n" +
    "2. 用户未指定助手时，按所需交付结果对照目录 selection 的 use_when、not_for、output：符合助手目标且不在排除范围，必须先加载；仅要原始记录、详情或计数且不要求业务归纳判断时直接查询。工具能返回数据不等于能替代助手的方法；不得因请求简短、只读、可一次查询或历史答案已有类似总结而跳过适用助手。不能按关键词机械匹配。\n" +
    "3. 多个候选按任务目标选择，只有歧义影响结果时才询问。每个独立任务使用一个主要助手；多步骤需求先用 composition 预检输入输出和依赖，再沿现有持久计划拆为独立任务，不能在已绑定任务中替换助手；数据库助手从当前用户获准且满足依赖的数据源中选择 source_id：只有一个适用来源时自动选择；多个来源按用户请求选择，无法确定再询问。\n" +
    "4. 加载成功会一次返回主说明及该模式必读资料，必读约束不得省略。optional_resources 列出的可选资料需要时用 authoring resource(binding_id,path) 按固定版本读取。加载助手与业务执行路径相互独立：仍按能力要求走原子工具或持久计划；允许直接调用工具不表示可以跳过适用助手。已选助手加载失败必须说明，不得静默退回直接工具完成同一业务方法。\n" +
    "5. 本轮独立任务不能用历史对话中的加载记录代替当前加载和绑定；已有任务续办沿用其绑定与状态。未分配、停用或缺依赖不得冒充已执行助手；不绕过失败的来源或写入约束。";
}

const sampleRuns = new WeakMap();
export function releaseSkillRun(router, context) {
  if (!context.runId || !context.sessionKey) return;
  for (const entries of [bindings.get(router), sampleRuns.get(router)]) {
    if (!entries) continue;
    for (const key of entries.keys()) {
      const [, session, run] = JSON.parse(key);
      if (session === context.sessionKey && run === context.runId) entries.delete(key);
    }
  }
}
export function draftSampleGuard(router, context, identity, toolName, params) {
  const key = skillRunKey(context, identity);
  if (toolName === "agentbridge_skill_authoring" && params?.action === "test") {
    if (!key) return {status:"rejected",error:{code:"SKILL_RUN_CONTEXT_REQUIRED",message:"缺少独立回合，不能开始草稿样例。"}};
    let runs=sampleRuns.get(router);
    if (!runs) { runs=new Set(); sampleRuns.set(router,runs); }
    if (runs.size >= 4096 && !runs.has(key)) return {status:"rejected",error:{code:"SKILL_SAMPLE_CAPACITY",message:"样例容量已满，请稍后重试。"}};
    runs.add(key);
  }
  if (key && sampleRuns.get(router)?.has(key) && !["agentbridge_skill_authoring","agentbridge_skill_catalog"].includes(toolName))
    return {status:"rejected",error:{code:"SKILL_SAMPLE_ONLY",message:"本轮为草稿合成样例，禁止调用业务工具。请另起一轮办理真实业务。"}};
  return null;
}
