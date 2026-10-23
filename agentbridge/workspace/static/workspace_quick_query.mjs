// A closed set of equivalent reads; permissions and identity remain server-owned.
const CURRENT_OWN_OA_PENDING = new Set([
  "查看oa待办", "查询oa待办", "查oa待办", "查一下oa待办",
  "查看我的oa待办", "查询我的oa待办", "查看当前oa待办", "查询当前oa待办",
  "查看我当前的oa待办", "查询我当前的oa待办",
  "查看本人当前oa待办", "查询本人当前oa待办",
  "帮我查一下oa待办", "帮我查询oa待办", "帮我查看oa待办",
]);

export function isCurrentOwnOaPendingQuery(message) {
  if (typeof message !== "string" || message.length > 80) return false;
  const plain = message.normalize("NFKC").trim().replace(/[ \t]/g, "")
    .replace(/[。.!?]$/, "").toLowerCase().replace(/事项$/, "");
  return CURRENT_OWN_OA_PENDING.has(plain);
}
