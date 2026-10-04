import test from "node:test";
import assert from "node:assert/strict";
import { createResultView } from "../bscli/workspace/static/workspace_results.mjs";

function element(tag = "article", dataset = {}) {
  return { tag, dataset, children: [], events: {}, classList: { add() {} },
    append(...items) { this.children.push(...items); },
    addEventListener(name, fn) { this.events[name] = fn; } };
}
function fixture() {
  const state = { queryGroupOpen: new Map() };
  const { groupQueryCards: group } = createResultView({ state, document: { createElement: element } });
  return { group, state };
}
const query = (turn = "one", scope = "endpoint-a", status = "succeeded") =>
  element("article", { queryTurn: turn, queryScope: scope, queryStatus: status });
const user = key => element("article", { messageRole: "user", timelineKey: key });

test("many independent reads fold into one group without losing steps or expansion on updates", () => {
  const { group } = fixture();
  const cards = Array.from({ length: 12 }, () => query());
  const output = group(cards);
  assert.equal(output.length, 1);
  const details = output[0].children.at(-1);
  assert.equal(details.open, false);
  assert.equal(details.children.length, 13);
  assert.equal(details.children[12], cards[11]);
  details.open = true;
  details.events.toggle();
  assert.equal(group([...cards, query()])[0].children.at(-1).open, true);
});

test("explicit turns and endpoints stay isolated even when calls interleave", () => {
  const { group } = fixture();
  const output = group([query(), query("two"), query("one", "endpoint-b"), query()]);
  assert.equal(output.length, 3);
  assert.equal(output[0].children.at(-1).children.length, 3);
});

test("historical fallback needs a user boundary and preserves other cards and final answers", () => {
  const { group } = fixture();
  const orphan = query("");
  const approval = element();
  const answer = element("article", { messageRole: "assistant" });
  const output = group([orphan, user("a"), query(""), approval, query(""), answer, user("b"), query("")]);
  assert.equal(output.length, 7);
  assert.equal(output[0], orphan);
  assert.equal(output[3], approval);
  assert.equal(output[4], answer);
  assert.equal(output[2].children.at(-1).children.length, 3);
});

test("failures, cancellations, unknown results and running steps remain visible outside disclosure", () => {
  const { group } = fixture();
  for (const status of ["failed", "canceled", "outcome_unknown"]) {
    const card = group([query(), query("one", "endpoint-a", status)])[0];
    assert.match(card.children[0].children[1].textContent, /需关注/);
    assert.match(card.children[1].textContent, /失败、取消或结果待核对/);
  }
  const active = group([query(), query("one", "endpoint-a", "active")])[0];
  assert.equal(active.children[0].children[1].textContent, "查询中");
});
