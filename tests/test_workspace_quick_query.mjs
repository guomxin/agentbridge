import test from "node:test";
import assert from "node:assert/strict";
import { isCurrentOwnOaPendingQuery } from "../agentbridge/workspace/static/workspace_quick_query.mjs";

test("only explicit current personal OA pending reads use the closed shortcut", () => {
  for (const text of ["查看OA待办", "查询 OA 待办。", "查一下OA待办", "帮我查询OA待办",
    "查看我当前的OA待办事项", "查询本人当前ＯＡ待办？", "查看我的OA待办！"]) {
    assert.equal(isCurrentOwnOaPendingQuery(text), true, text);
  }
  for (const text of ["查询昨天OA待办", "查询张三的OA待办", "查看OA待办并审批", "查询OA待办后处理周报",
    "不要查看OA待办", "翻译：查看OA待办", "“查看OA待办”", "查看OA待办\n并提交", "查\n看OA待办",
    "查看OA待办，再查询会议室", "查看OA已办", "查看OA待办（仅会议）", "查看OA待办??", "", null]) {
    assert.equal(isCurrentOwnOaPendingQuery(text), false, text);
  }
});
