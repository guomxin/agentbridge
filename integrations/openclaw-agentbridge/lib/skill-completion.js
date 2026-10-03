// Text-only model boundary for the central durable workbench. This never creates
// an agent session, installs tools, or reads a user's conversation/workspace.
export function registerSkillCompletion(api, { loadSdk } = {}) {
  if (typeof api.registerGatewayMethod !== 'function') return;
  let busy = false;
  api.registerGatewayMethod('agentbridge.skills.complete', async ({ params, respond }) => {
    if (busy) return respond(false, undefined, { code: 'SKILL_BUSY', message: 'Skill model worker is busy.' });
    if (!params || Object.keys(params).some(k => !['system', 'prompt'].includes(k)) ||
        typeof params.system !== 'string' || typeof params.prompt !== 'string' ||
        !params.prompt.trim() || params.system.length > 56000 || params.prompt.length > 24000) {
      return respond(false, undefined, { code: 'INVALID_REQUEST', message: 'Invalid text completion request.' });
    }
    busy = true;
    try {
      const sdk = await (loadSdk ? loadSdk() : import('openclaw/plugin-sdk/agent-runtime'));
      const cfg = api.runtime?.config?.current?.() || api.config;
      const prepared = await sdk.prepareSimpleCompletionModelForAgent({ cfg, agentId: 'main' });
      if (prepared.error) throw new Error('model unavailable');
      const result = await sdk.completeWithPreparedSimpleCompletionModel({
        model: prepared.model, auth: prepared.auth, cfg,
        context: { systemPrompt: params.system,
          messages: [{ role: 'user', content: params.prompt, timestamp: Date.now() }], tools: [] },
        options: { maxTokens: 6000, signal: AbortSignal.timeout(90000) },
      });
      if (['error', 'aborted', 'length', 'toolUse'].includes(result.stopReason) ||
          result.content?.some(c => c.type === 'toolCall')) throw new Error('incomplete output');
      const text = (result.content || []).filter(c => c.type === 'text').map(c => c.text).join('\n');
      if (!text.trim() || text.length > 48000) throw new Error('invalid output');
      respond(true, { text, model: `${prepared.model.provider}/${prepared.model.id}`,
        usage: result.usage ? { input: result.usage.input, output: result.usage.output, totalTokens: result.usage.totalTokens } : null,
        execution: 'tool_free_completion' });
    } catch {
      // Provider errors can contain URLs or authentication material. Never echo.
      respond(false, undefined, { code: 'SKILL_MODEL_UNAVAILABLE', message: 'Text model completion unavailable or incomplete.' });
    } finally { busy = false; }
  }, { scope: 'operator.write' });
}
