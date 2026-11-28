import { createHash } from 'node:crypto';
// Match Central's verified default-account aliases. Custom account names keep
// their original spelling; normalization must never merge distinct bot accounts.
export function canonicalTelegramAccount(value) {
  if (value == null) return "default";
  if (typeof value !== "string" || value.length > 512) return undefined;
  const trimmed = value.trim();
  return !trimmed || trimmed === "default" ? "default" : value;
}

export function sameTelegramAccount(left, right) {
  const expected = canonicalTelegramAccount(left);
  return expected !== undefined && expected === canonicalTelegramAccount(right);
}

export function telegramBotToken(config, accountId) {
  const account = canonicalTelegramAccount(accountId);
  const root = config?.channels?.telegram;
  if (!account || !root || typeof root !== "object") return null;
  const accounts = root.accounts && typeof root.accounts === "object" ? root.accounts : null;
  const token = account === "default"
    ? (accounts && Object.hasOwn(accounts, "default") ? accounts.default?.botToken : undefined) ?? root.botToken
    : (accounts && Object.hasOwn(accounts, account) ? accounts[account]?.botToken : undefined);
  return typeof token === "string" && /^[1-9][0-9]*:/.test(token) ? token : null;
}

// Route identity only, never a Bot credential or a display name. Unknown secret
// references cannot prove that an old message id belongs to the current Bot.
export function telegramBotRouteRef(config, accountId, chatId) {
  const account = canonicalTelegramAccount(accountId), peer = String(chatId || '');
  const token = telegramBotToken(config, account);
  if (!account || !/^[1-9][0-9]*$/.test(peer) || !token) return null;
  return createHash('sha256').update(JSON.stringify([account, peer, token.split(':', 1)[0]])).digest('hex');
}
