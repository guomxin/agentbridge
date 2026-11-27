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

// Route identity only, never a Bot credential or a display name. Unknown secret
// references cannot prove that an old message id belongs to the current Bot.
export function telegramBotRouteRef(config, accountId, chatId) {
  const account=canonicalTelegramAccount(accountId), peer=String(chatId || '');
  const root=config?.channels?.telegram, selected=root?.accounts?.[account] || root;
  const token=selected?.botToken ?? root?.botToken;
  if (!account || !/^[1-9][0-9]*$/.test(peer) || typeof token!=='string' || !/^[1-9][0-9]*:/.test(token)) return null;
  return createHash('sha256').update(JSON.stringify([account,peer,token.split(':',1)[0]])).digest('hex');
}
