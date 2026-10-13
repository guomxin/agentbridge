import test from "node:test";
import assert from "node:assert/strict";
import { createTaskCards } from "../agentbridge/workspace/static/workspace_cards.mjs";
import { reconcileChildren } from "../agentbridge/workspace/static/workspace_dom.mjs";
import { createDocument, descendants } from "./workspace_dom_fixture.mjs";

function fixture() {
  const document = createDocument(), chat = document.createElement("main"), detail = document.createElement("main");
  const state = {taskCards:new Map(), taskCardMeta:new Map()};
  const cards = createTaskCards({document, state, $: selector => selector === "#task-detail" ? detail : chat,
    setTimelineNode() {}, appendArtifactList() {}, scrollChat() {},
    renderChatTimeline() { reconcileChildren(chat, [...state.taskCards.values()]); }});
  return {document, chat, detail, state, ...cards};
}
function interaction(id, type="business_input", state="pending") {
  return {interactionId:id, type, state, title:`事项 ${id}`, message:"核对当前事项", presentation:{url:`https://cards.example.test/${id}`}};
}
function result(version, status, interactions) {
  return {task:{task_id:"task", version, status, title:"审批待办", created_at:"2026-10-13T10:00:00Z"}, interactions,
    interaction:interactions.at(-1), events:[]};
}
const findClass = (root, name) => descendants(root).find(node => node.className.split(" ").includes(name));
const links = root => descendants(root).filter(node => node.tag === "a" && !node.hidden);

test("one business container retains its position and readable steps across input, authorization and unknown", () => {
  const f = fixture();
  f.upsertTaskCard(result(1, "waiting_user", [interaction("input")]));
  const card = f.chat.children[0], link = links(card)[0];
  link.focus();
  f.upsertTaskCard(result(2, "waiting_user", [interaction("input")]));
  assert.equal(f.chat.children[0], card);
  assert.equal(links(card)[0], link);
  assert.equal(f.document.activeElement, link);
  f.upsertTaskCard(result(3, "waiting_user", [interaction("input", "business_input", "completed"), interaction("auth", "execution_authorization")]));
  assert.equal(f.state.taskCards.size, 1);
  assert.match(card.textContent, /字段已提交/);
  assert.deepEqual(links(card).map(n => n.href), ["https://cards.example.test/auth"]);
  const history = findClass(card, "interaction-history"); history.open = true;
  const auth = links(card)[0]; auth.focus();
  f.upsertTaskCard(result(4, "outcome_unknown", [interaction("input", "business_input", "completed"), interaction("auth", "execution_authorization", "completed")]));
  assert.equal(f.chat.children[0], card);
  assert.equal(history.open, true);
  assert.equal(links(card).length, 0);
  assert.match(findClass(card, "application-card-status").textContent, /结果待核对/);
  assert.match(history.textContent, /已授权/);
  assert.equal(f.document.activeElement.textContent, "查看进度");
  // A late waiting-user response must never restore a used authorization link.
  f.upsertTaskCard(result(2, "waiting_user", [interaction("auth", "execution_authorization")]));
  assert.equal(links(card).length, 0);
  assert.match(findClass(card, "application-card-status").textContent, /结果待核对/);
});

test("batch rows preserve independent states and current-only authorization while disclosures and focus survive refresh", () => {
  const f = fixture(), r = result(1, "waiting_user", [interaction("old"), interaction("current")]);
  r.batch = {batchId:"batch", currentOrdinal:3, totalCount:5, state:"waiting_user", items:[
    {itemId:"one",ordinal:1,state:"succeeded",display:{title:"已办一"}},
    {itemId:"two",ordinal:2,state:"succeeded",display:{title:"已办二"}},
    {itemId:"three",ordinal:3,state:"waiting_user",display:{title:"当前项"},interactionId:"current"},
    {itemId:"four",ordinal:4,state:"queued",display:{title:"之后一"}},
    {itemId:"five",ordinal:5,state:"queued",display:{title:"之后二"}},
  ]};
  f.upsertTaskCard(r);
  const card = f.chat.children[0], queue = findClass(card, "batch-queue");
  const row = descendants(queue).find(n => n.dataset.itemId === "one");
  queue.open = true; queue.children[0].focus();
  assert.deepEqual(links(card).map(n => n.href), ["https://cards.example.test/current"]);
  r.task.version = 2; r.task.status = "outcome_unknown";
  r.batch.state = "outcome_unknown"; r.batch.items[2].state = "outcome_unknown";
  f.upsertTaskCard(r);
  assert.equal(queue.open, true);
  assert.equal(f.document.activeElement, queue.children[0]);
  assert.equal(descendants(queue).find(n => n.dataset.itemId === "one"), row);
  const rows = descendants(queue).filter(n => n.dataset.itemId);
  assert.deepEqual(rows.map(n => n.dataset.state), ["succeeded", "succeeded", "outcome_unknown", "queued", "queued"]);
  assert.deepEqual(rows.map(n => findClass(n, "batch-item-result").textContent), ["已核验成功", "已核验成功", "结果待核对", "未处理", "未处理"]);
  assert.equal(links(card).length, 0);
  f.renderTaskDetail(r);
  const detailQueue = findClass(f.detail, "batch-queue"); detailQueue.open = true; detailQueue.children[0].focus();
  f.renderTaskDetail({...r, task:{...r.task, version:3}});
  assert.equal(findClass(f.detail, "batch-queue"), detailQueue);
  assert.equal(detailQueue.open, true);
  assert.equal(f.document.activeElement, detailQueue.children[0]);
});

test("parallel independent interactions remain available inside the same task and failures retire every action", () => {
  const f = fixture();
  const r = result(1,"waiting_user",[interaction("first"), interaction("second")]);
  f.upsertTaskCard(r);
  assert.equal(f.state.taskCards.size,1);
  assert.equal(links(f.chat).length,2);
  f.upsertTaskCard({...r,task:{...r.task,status:"failed",version:2}});
  assert.equal(links(f.chat).length,0);
  assert.match(findClass(f.chat,"interaction-history").textContent,/事项 first.*事项 second/);
});

test("reconciling timeline neighbours never detaches an unchanged focused control", () => {
  const document = createDocument(), root = document.createElement("main"), first = document.createElement("p"), card = document.createElement("article"), link = document.createElement("a");
  card.append(link); root.append(first,card); link.focus();
  const replacement = document.createElement("p");
  reconcileChildren(root,[replacement,card]);
  assert.equal(document.activeElement,link);
  assert.deepEqual(root.childNodes,[replacement,card]);
});

test("reopening the same detail after a loading placeholder rebuilds a usable current view", () => {
  const f = fixture(), r = result(1, "waiting_user", [interaction("current")]);
  f.renderTaskDetail(r);
  f.detail.replaceChildren(f.document.createElement("p"));
  f.renderTaskDetail(r);
  assert.match(f.detail.textContent, /审批待办/);
  assert.deepEqual(links(f.detail).map(node => node.href), ["https://cards.example.test/current"]);
});

test("trusted entry accepts local HTTP card servers but rejects remote HTTP, credentials and active URL schemes", () => {
  const f = fixture();
  let version = 0;
  for (const [url, allowed] of [
    ["http://127.0.0.1:58965/flow/input/test", true],
    ["http://localhost:58965/flow/input/test", true],
    ["http://[::1]:58965/flow/input/test", true],
    ["https://cards.example.test/flow/input/test", true],
    ["http://cards.example.test/flow/input/test", false],
    ["http://127.0.0.1.example.test/flow/input/test", false],
    ["https://user:password@cards.example.test/flow/input/test", false],
    ["javascript:alert(1)", false], ["data:text/html,bad", false],
    ["/flow/input/test", false],
  ]) {
    const step = interaction("entry"); step.presentation.url = url;
    const r = result(++version, "waiting_user", [step]);
    f.upsertTaskCard(r); f.renderTaskDetail(r);
    for (const root of [f.chat, f.detail]) {
      assert.equal(links(root).length, Number(allowed), url);
      if (allowed) assert.equal(links(root)[0].href, url);
    }
  }
});
