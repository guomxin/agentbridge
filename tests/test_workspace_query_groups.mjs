import test from "node:test";
import assert from "node:assert/strict";
import { createDocument, descendants } from "./workspace_dom_fixture.mjs";
import { createResultView } from "../agentbridge/workspace/static/workspace_results.mjs";
import { reconcileChildren } from "../agentbridge/workspace/static/workspace_dom.mjs";
import { createReadingScroll, timelineNodeMatchesKey } from "../agentbridge/workspace/static/workspace_navigation.mjs";

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

test('only older expired OA queries fold; latest and active preparation stay visible and expansion persists', () => {
  const {group}=fixture();
  const old=element('article',{oaQueryScope:'owner-a',oaArchiveable:'true'});
  const active=element('article',{oaQueryScope:'owner-a',oaArchiveable:'false'});
  const other=element('article',{oaQueryScope:'owner-b',oaArchiveable:'true'});
  const latest=element('article',{oaQueryScope:'owner-a',oaArchiveable:'true'});
  const output=group([old,active,other,latest]);
  assert.equal(output[0].tag,'details');assert.equal(output[0].children[1],old);
  assert.equal(output[1],active);assert.equal(output[2],other);assert.equal(output[3],latest);
  output[0].open=true;assert.equal(group([old,active,other,latest])[0],output[0]);
  assert.equal(group([old,active,other,latest])[0].open,true);
});


function historyFixture() {
  const document = createDocument(), state = {queryGroupOpen:new Map()}, opened = [];
  const timeline = document.createElement("main");
  const {groupQueryCards:group,restoreHistoryCardState,resetHistoryGroups} = createResultView({document,state,openTask:id=>opened.push(id)});
  const card = (id, {query="query-one",scope="account-a",foldable=true,status="succeeded"}={}) => {
    const node=document.createElement("article");
    Object.assign(node.dataset,{taskId:id,timelineKey:`task:${id}`,oaHistoryQueryId:query,
      oaHistoryScope:scope,oaHistoryFoldable:String(foldable),queryStatus:status});
    const scroll=document.createElement("div");scroll.className="oa-history-scroll";scroll.scrollTop=0;scroll.scrollLeft=0;
    const table=document.createElement("table");table.textContent="query rows";scroll.append(table);
    const navigation=document.createElement("div");navigation.className="oa-history-navigation";
    for(const text of ["上一页","下一页"]){const button=document.createElement("button");button.textContent=text;navigation.append(button);}
    node.append(scroll,navigation);
    return node;
  };
  const render=nodes=>{const result=group(nodes);reconcileChildren(timeline,result);restoreHistoryCardState(timeline);return result;};
  return {document,state,opened,timeline,group,render,card,restoreHistoryCardState,resetHistoryGroups};
}

test("one query keeps one report with inspectable continuation records and stable controls", () => {
  const f=historyFixture(), first=f.card("first",{foldable:false}), next=f.card("next");
  assert.deepEqual(f.group([first,next]),[first]);
  const details=first.childNodes.at(-1), list=details.childNodes[1];
  assert.equal(first.dataset.oaHistoryGroupCount,"2");
  assert.equal(details.open,false);
  assert.equal(descendants(first).filter(x=>x.tag==="table").length,1);
  assert.match(details.childNodes[0].textContent,/查询过程（2 步）/);
  assert.match(list.childNodes[0].textContent,/步骤 1/);
  const control=list.childNodes[1].childNodes[0];control.focus();control.events.click();
  assert.deepEqual(f.opened,["next"]);
  details.open=true;details.events.toggle();
  const output=f.group([first,next,f.card("third")]);
  assert.deepEqual(output,[first]);assert.equal(first.childNodes.at(-1),details);
  assert.equal(list.childNodes[1].childNodes[0],control);
  assert.equal(f.document.activeElement,control);assert.equal(details.open,true);
  assert.match(details.childNodes[0].textContent,/3 步/);
});

test("different queries, accounts, unsafe steps and unresolved reads never disappear into a report", () => {
  const f=historyFixture(), first=f.card("first",{foldable:false}), next=f.card("next");
  const other=f.card("other",{query:"different-date-query"});
  const account=f.card("account",{scope:"account-b"});
  const mixed=f.card("mixed",{foldable:false});
  const unknown=f.card("unknown",{query:""});
  assert.deepEqual(f.group([first,other,next,account,mixed,unknown]),[first,other,account,mixed,unknown]);
  assert.equal(first.dataset.oaHistoryGroupCount,"2");
  assert.equal(mixed.dataset.oaHistoryGroupCount,undefined);
  next.dataset.oaHistoryFoldable="false";next.dataset.queryStatus="failed";
  assert.deepEqual(f.group([first,next]),[first,next]);
  assert.equal(first.childNodes.at(-1).hidden,true);
});

test("restoring earlier history promotes the initial plan with stable reading key and process focus", () => {
  const f=historyFixture(), next=f.card("next"), last=f.card("last");
  assert.deepEqual(f.render([next,last]),[next]);
  const queryKey=next.dataset.timelineKey;
  const oldDetails=next.childNodes.at(-1), first=f.card("first",{foldable:false});
  oldDetails.open=true;oldDetails.events.toggle();
  const step=oldDetails.childNodes[1].childNodes[1].childNodes[0];step.focus();
  assert.deepEqual(f.render([first,next,last]),[first]);
  assert.equal(oldDetails.parentNode,first);assert.equal(oldDetails.open,true);
  assert.equal(f.document.activeElement,step);assert.equal(f.timeline.contains(step),true);
  assert.equal(first.dataset.timelineKey,queryKey);
  for(const id of ["first","next","last"])assert.equal(timelineNodeMatchesKey(first,`task:${id}`),true);
  assert.equal(first.dataset.oaHistoryGroupCount,"3");
  assert.deepEqual(f.render([next,last]),[next]);
  assert.equal(next.dataset.oaHistoryGroupCount,"2");
  assert.equal(next.childNodes.at(-1),oldDetails);
  assert.equal(descendants(oldDetails).filter(x=>x.tag==="button").length,2);
  assert.equal(f.document.activeElement,step);
  assert.equal(timelineNodeMatchesKey(next,"task:first"),false);
});

test("prepending the initial card preserves report navigation focus, internal scrolling and viewport anchor", () => {
  const f=historyFixture(),next=f.card("next"),last=f.card("last"),first=f.card("first",{foldable:false});
  next.offsetTop=300;first.offsetTop=700;
  Object.assign(f.timeline,{scrollTop:350,scrollHeight:2000,clientHeight:400});
  f.render([next,last]);
  const reading=createReadingScroll({container:f.timeline,notice:f.document.createElement("button")});
  f.timeline.events.scroll();const saved=reading.snapshot();
  const sourceScroll=next.childNodes[0],sourceNext=next.childNodes[1].childNodes[1];
  sourceScroll.scrollTop=175;sourceScroll.scrollLeft=640;sourceNext.focus();
  f.render([first,next,last]);reading.restore(saved);
  assert.equal(f.document.activeElement,first.childNodes[1].childNodes[1]);
  assert.equal(first.childNodes[0].scrollTop,175);assert.equal(first.childNodes[0].scrollLeft,640);
  assert.equal(f.timeline.scrollTop,750);assert.equal(first.dataset.timelineKey,saved.anchor);
  reading.restore({...saved,anchor:"task:next"});assert.equal(f.timeline.scrollTop,750);
  assert.equal(f.timeline.childNodes.length,1);
});

test("a continuation gaining query identity transfers its focused detail action to the retained step", () => {
  const f=historyFixture(),first=f.card("first",{foldable:false}),next=f.card("next",{query:""});
  const action=f.document.createElement("button");action.textContent="查看进度";next.append(action);
  f.render([first,next]);action.focus();
  next.dataset.oaHistoryQueryId="query-one";f.render([first,next]);
  assert.equal(f.timeline.childNodes.length,1);
  const steps=first.childNodes.at(-1),target=steps.childNodes[1].childNodes[1].childNodes[0];
  assert.equal(steps.open,true);assert.equal(f.document.activeElement,target);
  target.events.click();assert.deepEqual(f.opened,["next"]);
});

test("account reset removes query aliases, process controls and pending focus restoration", () => {
  const f=historyFixture(),next=f.card("next"),last=f.card("last"),first=f.card("first",{foldable:false});
  f.render([next,last]);const oldDetails=next.childNodes.at(-1);
  const oldControl=oldDetails.childNodes[1].childNodes[0].childNodes[0];oldControl.focus();
  const output=f.group([first,next,last]);reconcileChildren(f.timeline,output);
  f.resetHistoryGroups();f.restoreHistoryCardState(f.timeline);
  assert.equal(f.document.activeElement,null);assert.equal(oldDetails.parentNode,null);
  assert.equal(first.dataset.timelineKey,"task:first");assert.equal(first.dataset.timelineAliases,undefined);
  const other=f.card("fresh",{scope:"account-b"}),otherNext=f.card("fresh-next",{scope:"account-b"});
  f.render([other,otherNext]);
  assert.notEqual(other.childNodes.at(-1),oldDetails);
  assert.equal(timelineNodeMatchesKey(other,"task:next"),false);
  assert.equal(f.timeline.contains(oldControl),false);
});
