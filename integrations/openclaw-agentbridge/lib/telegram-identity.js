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
