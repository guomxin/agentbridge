// Opt-in acceptance worker: actual candidate Coordinator/runner and bundled
// Telegram outbound adapter. It never starts a channel monitor or getUpdates.
import { readFileSync, writeFileSync, appendFileSync, mkdirSync, existsSync, unlinkSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import { resolvePluginConfig } from "../../lib/config.js";
import { AgentBridgeIdentityRouter } from "../../lib/identity-router.js";
import { InteractionCoordinator } from "../../lib/coordinator.js";
import { createTaskCommandRunner } from "../../lib/task-commands.js";
import { createHostRuntimeReporter } from "../../lib/runtime-reporter.js";

const spec = JSON.parse(readFileSync(process.argv[2], "utf8"));
if (!['offline', 'live'].includes(spec.mode) || !/^[1-9][0-9]*$/.test(spec.sender) ||
    new URL(spec.mcpUrl).hostname !== '127.0.0.1') throw new Error('INVALID_ISOLATED_ACCEPTANCE_SPEC');
process.env.OPENCLAW_STATE_DIR = path.join(spec.home, "openclaw-state");
process.env.OPENCLAW_CONFIG_PATH = path.join(spec.home, "openclaw-isolated.json");
mkdirSync(process.env.OPENCLAW_STATE_DIR, { recursive: true, mode: 0o700 });
const sdk = file => pathToFileURL(path.join(spec.sdkDir, "dist/extensions/telegram", file)).href;
const { telegramPlugin } = await import(sdk("channel-plugin-api.js"));
const native = telegramPlugin.outbound;
if (typeof native?.sendPayload !== "function") throw new Error("SDK_OUTBOUND_UNAVAILABLE");
let telegram;
if (spec.mode === 'live') {
  const bytes = readFileSync(spec.sourceConfig);
  if (createHash('sha256').update(bytes).digest('hex') !== spec.sourceConfigHash) throw new Error('SOURCE_CONFIG_CHANGED');
  const source = JSON.parse(bytes);
  const binding = source.plugins?.entries?.['agentbridge-interactions']?.config?.identityBindings?.find(value =>
    value.channel === 'telegram' && String(value.senderId) === spec.sender && (!value.accountId || value.accountId === 'default'));
  telegram = source.channels?.telegram;
  if (!binding || !telegram?.botToken || telegram.accounts || String(telegram.botToken).split(':')[0] !== spec.botId) {
    throw new Error('LIVE_BOT_OR_PRIVATE_SENDER_NOT_REVIEWED');
  }
  if (telegram.apiRoot && String(telegram.apiRoot).replace(/\/+$/,'') !== 'https://api.telegram.org') {
    throw new Error('LIVE_ACCEPTANCE_REQUIRES_OFFICIAL_TELEGRAM');
  }
  const { probeTelegram } = await import(sdk('runtime-api.js'));
  const probe = await probeTelegram(telegram.botToken, 15_000, { accountId:'default', proxyUrl:telegram.proxy,
    network:telegram.network, includeWebhookInfo:false });
  if (!probe.ok || String(probe.bot?.id) !== spec.botId || probe.bot?.isBot !== true) throw new Error('LIVE_BOT_IDENTITY_UNCONFIRMED');
} else {
  if (new URL(spec.botApiRoot).hostname !== '127.0.0.1') throw new Error('OFFLINE_BOT_MUST_BE_LOOPBACK');
  telegram = { botToken:'123456:synthetic-only-acceptance', apiRoot:spec.botApiRoot };
}
const hostConfig = { channels:{telegram}, session:{store:path.join(spec.home,'openclaw-state/sessions.json')} };
const config = resolvePluginConfig({ mcpUrl:spec.mcpUrl, allowedCardOrigins:[spec.cardOrigin], autoPoll:true,
  wakeAgentOnComplete:false, identityBindings:[{ channel:'telegram',senderId:spec.sender,accountId:'default',tokenEnv:'QUERY_FIXTURE_TOKEN' }] });
const logger = { info() {}, debug() {}, warn(message) {
  // Keep diagnostics restricted to static codes; never print SDK exceptions,
  // source credentials, Telegram request bodies or short-lived card links.
  appendFileSync(path.join(spec.home,'host-diagnostics.log'), String(message).replace(/https?:\/\/\S+/g,'[url]')+'\n', {mode:0o600});
} };
const receipts = new Set();
const adapter = { ...native, async sendPayload(context) {
  if (String(context.to).replace(/^telegram:/,'') !== spec.sender || context.accountId !== 'default' ||
      context.payload.mediaUrl || context.payload.mediaUrls?.length || receipts.size >= 12) throw new Error('FIXTURE_SEND_SCOPE_DENIED');
  return native.sendPayload({ ...context, onDeliveryResult:async result => {
    if (String(result.chatId) !== spec.sender || !/^[1-9][0-9]*$/.test(String(result.messageId))) throw new Error('FIXTURE_RECEIPT_MISMATCH');
    const key=String(result.messageId);
    if (!receipts.has(key)) {
      receipts.add(key);
      const buttons = Array.isArray(context.payload.channelData?.telegram?.buttons)
        ? context.payload.channelData.telegram.buttons.flat() : [];
      appendFileSync(path.join(spec.home,'telegram-receipts.jsonl'), JSON.stringify({ messageId:key, chatId:spec.sender,
        mode:spec.mode, nativeSdk:true, buttons:buttons.length,
        urlButtons:buttons.filter(button=>button.url).length, webAppButtons:buttons.filter(button=>button.web_app?.url).length,
        buttonOriginsMatch:buttons.every(button=>new URL(button.url || button.web_app?.url).origin===spec.cardOrigin),
        sentAt:new Date().toISOString() })+'\n',{mode:0o600,flush:true});
    }
    await context.onDeliveryResult?.(result);
  } });
} };
const api = { config:hostConfig, logger, runtime:{ state:{resolveStateDir:()=>process.env.OPENCLAW_STATE_DIR},
  channel:{outbound:{loadAdapter:async channel=>{ if(channel!=='telegram')throw new Error('FIXTURE_CHANNEL_DENIED');return adapter;}}},
  system:{enqueueSystemEvent(){throw new Error('MODEL_DISABLED');},requestHeartbeat(){throw new Error('MODEL_DISABLED');}} } };
const router = new AgentBridgeIdentityRouter({ config,hostConfig,env:{ QUERY_FIXTURE_TOKEN:readFileSync(spec.tokenFile,'utf8').trim() } });
const profiles = await router.refreshIdentityProfiles({logger});
if (profiles.length !== 1 || profiles[0]?.acceptedHostLevel !== 'L3') throw new Error('CANDIDATE_HOST_NOT_ADMITTED');
const coordinator = new InteractionCoordinator({api,config,mcpClientResolver:session=>router.clientForSession(session)});
const commands = createTaskCommandRunner({identityRouter:router,coordinator,logger});
const reporter = createHostRuntimeReporter({identityRouter:router,coordinator,taskCommandRunner:commands,logger,initialDelayMs:0,intervalMs:500});
reporter.start();
coordinator.startNotificationPump(router);
const wake = setInterval(() => {
  const marker = path.join(spec.home,'command-wake');
  if (existsSync(marker)) { unlinkSync(marker); reporter.collectOnce().catch(()=>logger.warn('FIXTURE_WAKE_RECOVERY_FAILED')); }
},100);
writeFileSync(path.join(spec.home,'host-ready.json'),JSON.stringify({ mode:spec.mode, nativeSdk:true, candidateCoordinator:true,
  candidateCommandRunner:true, pollingStarted:false, processId:process.pid }),{mode:0o600});
await new Promise(resolve=>{process.once('SIGTERM',resolve);process.once('SIGINT',resolve);});
clearInterval(wake);
reporter.stop();commands.stop();coordinator.stopAll();
