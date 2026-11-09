import test from "node:test";
import assert from "node:assert/strict";
import { createOaHistoryResults } from "../agentbridge/workspace/static/workspace_oa_history.mjs";
import { createTaskCards } from "../agentbridge/workspace/static/workspace_cards.mjs";
import { createLifecycle } from "../agentbridge/workspace/static/workspace_lifecycle.mjs";
import { reconcileChildren } from "../agentbridge/workspace/static/workspace_dom.mjs";
import { createDocument, descendants } from "./workspace_dom_fixture.mjs";

const byClass = (root, name) => descendants(root).find(node => node.className.split(" ").includes(name));
const buttons = root => descendants(root).filter(node => node.tag === "button");
const button = (root, text) => buttons(root).find(node => node.textContent === text);
const visible = root => descendants(root).filter(node => {
  for (let ancestor = node; ancestor; ancestor = ancestor.parentNode) if (ancestor.hidden) return false;
  return true;
});
const rows = root => byClass(root, "oa-history-table").childNodes[1].childNodes;
const settle = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
function item(id, overrides = {}) {
  return {affair_id:id, title:`业务结算单 ${id}`, initiated_at:"2026-11-01 09:00:00",
    processed_at:"2026-11-02 11:00:00", workflow_state:"running", workflow_state_text:"流转中",
    current_nodes:[{name:"财务核对", handlers:["甲"]}, {name:"经理复核", handlers:["乙", "丙"]}],
    current_handlers:["甲", "乙", "丙"], observed_at:"2026-11-09T01:02:03Z", unavailable_fields:[], ...overrides};
}
function page(overrides = {}) {
  return {schema_version:"agentbridge.oa_history_query.v1", query_id:"query-one", collection:"done", state:"complete",
    temporal:{absoluteRange:{start:"2026-11-01",end:"2026-11-09"},dateBasis:"initiated_at",precision:"day",timeZone:"Asia/Shanghai"},
    progress:{source_total:1125,scanned_count:1125,matched_count:2,details_checked:2,details_failed:0},
    coverage:{status:"complete",scan_complete:true,details_complete:true}, items:[item("first")],
    next_cursor:"opaque-next", has_more:true, ...overrides};
}
function task(query = page(), version = 1) {
  return {task:{task_id:"history-task",version,status:"succeeded",title:"查询 OA 历史",created_at:"2026-11-09T01:00:00Z"},
    oaHistoryQuery:query,interactions:[],events:[]};
}
function fixture(options = {}) {
  const document = createDocument(), root = document.createElement("section"), state = {};
  const renderer = createOaHistoryResults({document,state,...options});
  return {document,root,state,...renderer};
}

test("history fields preserve parallel nodes, date basis, unknown evidence and Shanghai observation time", () => {
  const f = fixture(), query = page({items:[item("first"), item("missing", {
    initiated_at:"stale value", processed_at:null, current_nodes:[], current_handlers:[],
    unavailable_fields:["initiated_at", "current_nodes", "current_handlers"]})]});
  f.render(f.root,task(query));
  assert.match(byClass(f.root,"oa-history-range").textContent,/提交／发起日期：2026-11-01 至 2026-11-09.*按天/);
  assert.match(byClass(f.root,"oa-history-progress").textContent,/来源总数 1125.*已扫描 1125.*匹配 2/);
  assert.match(f.root.textContent,/不代表所选日期当时的历史状态/);
  assert.match(rows(f.root)[0].textContent,/财务核对（甲）；经理复核（乙、丙）/);
  assert.match(rows(f.root)[0].childNodes[6].textContent,/2026.*11.*09.*09:02:03/);
  for (const index of [1,2,4,5]) assert.equal(rows(f.root)[1].childNodes[index].textContent,"未读到");
  assert.doesNotMatch(f.root.textContent,/stale value/);
  assert.equal(descendants(f.root).some(node => node.type === "checkbox"),false);
});

test("ended workflows distinguish verified empty nodes from unavailable node evidence", () => {
  const f = fixture();
  f.render(f.root,task(page({items:[item("ended",{workflow_state:"completed",workflow_state_text:"已结束",current_nodes:[],current_handlers:[]}),
    item("unread",{workflow_state:"completed",current_nodes:[],current_handlers:[],unavailable_fields:["current_nodes","current_handlers"]})]})));
  assert.match(rows(f.root)[0].childNodes[4].textContent,/无当前节点（流程已结束）/);
  assert.match(rows(f.root)[0].childNodes[5].textContent,/无当前处理人（流程已结束）/);
  assert.equal(rows(f.root)[1].childNodes[4].textContent,"未读到");
  assert.equal(rows(f.root)[1].childNodes[5].textContent,"未读到");
});

test("scanning, detail work, complete empty and partial empty have distinct empty messages", () => {
  for (const [state,coverage,message] of [
    ["scanning",{status:"partial"},/正在扫描历史事项，结果准备中/],
    ["details",{status:"partial",scan_complete:true},/正在读取匹配事项的详情，结果准备中/],
    ["complete",{status:"complete",scan_complete:true},/该范围内没有匹配事项/],
    ["partial",{status:"partial",scan_complete:false},/不能据此认定没有匹配记录/],
  ]) {
    const f=fixture({loadHistoryPage:async()=>{}});
    f.render(f.root,task(page({state,coverage,items:[],progress:{matched_count:0},has_more:state==="scanning"||state==="details"})));
    assert.match(byClass(f.root,"oa-history-empty").textContent,message);
    assert.equal(byClass(f.root,"oa-history-scroll").hidden,true);
    assert.match(byClass(f.root,"oa-history-progress").textContent,/来源总数 未确认/);
    if (state==="scanning"||state==="details") assert.equal(button(f.root,"继续读取").hidden,false);
  }
});

test("all history and open bounds are explicit, and partial field evidence is not shown as complete", () => {
  const f=fixture();
  f.render(f.root,task(page({temporal:{absoluteRange:{start:null,end:null},dateBasis:"processed_at",precision:"day"},
    state:"partial",coverage:{status:"partial",scan_complete:true,details_complete:false}})));
  assert.match(byClass(f.root,"oa-history-range").textContent,/本人处理日期：不限 至 不限/);
  assert.match(byClass(f.root,"oa-history-heading").textContent,/详情不完整/);
  f.render(f.root,task(page({temporal:{absoluteRange:{start:null,end:"2026-11-09"},dateBasis:"initiated_at",precision:"day"}}),2));
  assert.match(byClass(f.root,"oa-history-range").textContent,/不限 至 2026-11-09/);
});

test("partial results explain bounded stops in Chinese without exposing unknown internal reasons", () => {
  for (const [reason,expected] of [
    ["source_timeout",/读取 OA 来源超时/], ["source_row_budget",/事项读取上限/],
    ["detail_item_budget",/详情核对上限/], ["execution_time_budget",/总用时上限/],
    ["source_changed",/来源列表发生变化/], ["source_metadata_invalid",/分页或总数/],
    ["detail_fields_unavailable",/当前状态或节点字段未读到/], ["PRIVATE_INTERNAL_UNKNOWN",/停止原因尚未确认/],
  ]) {
    const f=fixture();f.render(f.root,task(page({state:"partial",coverage:{status:"partial",scan_complete:false,completionReason:reason}})));
    const notice=byClass(f.root,"oa-history-notice").textContent;
    assert.match(notice,/尚未查全.*已读取的部分结果/);assert.match(notice,expected);assert.ok(!notice.includes(reason));
  }
});

test("work continuation stays on page one then output cursor turns pages in both views", async () => {
  const calls=[], responses=[page({state:"details",items:[],coverage:{status:"partial",scan_complete:true},next_cursor:"details-cursor"}),
    page({items:[item("report-one")],next_cursor:"report-cursor"}),page({items:[item("report-two")],next_cursor:null,has_more:false})];
  const f=fixture({loadHistoryPage:async(id,cursor)=>{calls.push([id,cursor]);return {oaHistoryQuery:responses.shift()};}});
  const second=f.document.createElement("section"), initial=task(page({state:"scanning",items:[],coverage:{status:"partial"},next_cursor:"scan-cursor"}));
  f.render(f.root,initial);f.render(second,initial);
  button(f.root,"继续读取").events.click();await settle();
  assert.match(byClass(second,"oa-history-heading").textContent,/核对中/);
  button(second,"继续读取").events.click();await settle();
  assert.match(byClass(f.root,"oa-history-page").textContent,/第 1 页/);
  assert.match(rows(second)[0].textContent,/report-one/);
  button(f.root,"下一页").events.click();await settle();
  assert.match(byClass(second,"oa-history-page").textContent,/第 2 页/);
  assert.match(rows(f.root)[0].textContent,/report-two/);
  button(second,"上一页").events.click();
  assert.match(rows(f.root)[0].textContent,/report-one/);
  button(f.root,"下一页").events.click();await settle();
  assert.match(rows(second)[0].textContent,/report-two/);
  assert.deepEqual(calls,[["history-task","scan-cursor"],["history-task","details-cursor"],["history-task","report-cursor"]]);
});

test("transient failure retains the page, retries the same cursor, and task refresh retains the output page", async () => {
  const calls=[];let fail=true;
  const f=fixture({loadHistoryPage:async(id,cursor)=>{calls.push(cursor);if(fail)throw new Error("private upstream exception");return page({items:[item("second")],has_more:false,next_cursor:null});}});
  const original=task();f.render(f.root,original);
  button(f.root,"下一页").events.click();await settle();
  assert.match(rows(f.root)[0].textContent,/first/);
  assert.match(byClass(f.root,"oa-history-notice").textContent,/当前页已保留/);
  assert.doesNotMatch(f.root.textContent,/private upstream/);
  f.render(f.root,original);assert.match(byClass(f.root,"oa-history-notice").textContent,/当前页已保留/);
  fail=false;button(f.root,"下一页").events.click();await settle();
  f.render(f.root,task(original.oaHistoryQuery,2));
  assert.match(rows(f.root)[0].textContent,/second/);
  assert.deepEqual(calls,["opaque-next","opaque-next"]);
});

test("a response for a different query never mixes rows into the current results", async () => {
  const f=fixture({loadHistoryPage:async()=>page({query_id:"unrelated-query",items:[item("other-account-row")]})});
  f.render(f.root,task());button(f.root,"下一页").events.click();await settle();
  assert.match(rows(f.root)[0].textContent,/first/);
  assert.doesNotMatch(f.root.textContent,/other-account-row/);
  assert.match(byClass(f.root,"oa-history-notice").textContent,/与当前查询不一致/);
});

test("permission failure clears both views and old task snapshots cannot resurrect them", async () => {
  const f=fixture({loadHistoryPage:async()=>{throw Object.assign(new Error("denied"),{status:403});}}), second=f.document.createElement("section");
  const initial=task();f.render(f.root,initial);f.render(second,initial);
  button(f.root,"下一页").events.click();await settle();
  for(const root of [f.root,second]){assert.equal(rows(root).length,0);assert.match(root.textContent,/已清除旧结果/);}
  f.render(f.root,initial);assert.equal(rows(f.root).length,0);
  f.render(f.root,task(page({query_id:"new-authorized-query",items:[item("new-result")]}),2));
  assert.match(rows(second)[0].textContent,/new-result/);
});

test("an authoritative projection without history data clears stale rows", () => {
  const f=fixture();f.render(f.root,task());
  const revoked=task(undefined,2);delete revoked.oaHistoryQuery;f.render(f.root,revoked);
  assert.equal(rows(f.root).length,0);assert.match(f.root.textContent,/已清除旧结果/);
  f.render(f.root,task());assert.equal(rows(f.root).length,0);
});

test("unavailable and error projections are visible on first load without retaining sensitive rows", () => {
  for (const state of ["unavailable", "error"]) {
    const f=fixture(),failure={schema_version:"agentbridge.oa_history_query.v1",query_id:"query-one",state,
      items:[item("must-not-render")],error:{code:"OA_HISTORY_SESSION_CHANGED",message:"private exception"},has_more:false};
    f.render(f.root,task(failure));
    assert.equal(f.root.hidden,false);assert.equal(rows(f.root).length,0);
    assert.match(byClass(f.root,"oa-history-heading").textContent,state==="error"?/读取失败/:/暂不可读/);
    assert.match(byClass(f.root,"oa-history-notice").textContent,/登录或权限已变化/);
    assert.equal(byClass(f.root,"oa-history-range").hidden,true);
    assert.doesNotMatch(f.root.textContent,/private exception|must-not-render/);
    f.render(f.root,task(page(),2));assert.equal(rows(f.root).length,1);
    f.render(f.root,task(failure,3));assert.equal(rows(f.root).length,0);
    f.render(f.root,task(page(),2));assert.equal(rows(f.root).length,0);
  }
});

test("an unavailable continuation projection clears the current result even without an HTTP error", async () => {
  const f=fixture({loadHistoryPage:async()=>({schema_version:"agentbridge.oa_history_query.v1",query_id:"query-one",state:"unavailable",
    error:{code:"OA_HISTORY_EXPIRED"},items:[],has_more:false,next_cursor:null})});
  f.render(f.root,task());button(f.root,"下一页").events.click();await settle();
  assert.equal(rows(f.root).length,0);assert.match(byClass(f.root,"oa-history-heading").textContent,/暂不可读/);
});

test("changing query identity discards an outstanding response without unlocking another request", async () => {
  const first=deferred(),second=deferred();let calls=0;
  const f=fixture({loadHistoryPage:()=>++calls===1?first.promise:second.promise});
  f.render(f.root,task());button(f.root,"下一页").events.click();
  f.render(f.root,task(page({query_id:"new-query",items:[item("new-first")]}),2));
  button(f.root,"下一页").events.click();
  first.resolve(page({items:[item("stale-response")]}));await settle();
  assert.equal(button(f.root,"正在读取…").disabled,true);
  assert.doesNotMatch(f.root.textContent,/stale-response/);
  second.resolve(page({query_id:"new-query",items:[item("new-second")],has_more:false,next_cursor:null}));await settle();
  assert.match(rows(f.root)[0].textContent,/new-second/);
});

test("a late work response and stale refresh cannot roll completed results back into scanning", async () => {
  const request=deferred(),f=fixture({loadHistoryPage:()=>request.promise});
  const scan=task(page({state:"scanning",items:[],coverage:{status:"partial"},progress:{scanned_count:10}}));
  f.render(f.root,scan);button(f.root,"继续读取").events.click();
  f.render(f.root,task(page({items:[item("completed-result")]}),2));
  request.resolve(page({state:"details",items:[],coverage:{status:"partial"},progress:{scanned_count:1125,details_checked:1}}));await settle();
  f.render(f.root,scan);
  assert.match(rows(f.root)[0].textContent,/completed-result/);
  assert.match(byClass(f.root,"oa-history-heading").textContent,/已查全/);
});

test("account scope disposal removes old results and ignores outstanding replies", async () => {
  const lifecycle=createLifecycle(),scope=lifecycle.scope(),request=deferred();
  const f=fixture({getScope:()=>scope,loadHistoryPage:()=>request.promise});
  f.render(f.root,task());button(f.root,"下一页").events.click();scope.dispose();
  assert.equal(f.root.hidden,true);assert.equal(f.root.childNodes.length,0);
  request.resolve(page({items:[item("old-account")]}));await settle();
  assert.equal(f.root.childNodes.length,0);assert.equal(f.state.oaHistoryQueries.size,0);
});

test("long and untrusted row text stays text, preserves focus, and exposes an internal scroll region", () => {
  const f=fixture(),title=`<img src=x onerror=alert(1)> ${"很长的业务结算说明 ".repeat(100)}`;
  const query=page({items:Array.from({length:80},(_,index)=>item(String(index),{title})),has_more:false,next_cursor:null});
  f.render(f.root,task(query));const scroll=byClass(f.root,"oa-history-scroll");scroll.focus();
  f.render(f.root,task(query,2));
  assert.equal(rows(f.root).length,80);assert.equal(rows(f.root)[0].childNodes[0].textContent,title.trim());
  assert.equal(descendants(f.root).some(node=>node.tag==="img"),false);
  assert.equal(f.document.activeElement,scroll);assert.equal(scroll.attributes.role,"region");
  assert.equal(scroll.attributes.tabindex,"0");assert.match(scroll.attributes["aria-label"],/横向滚动/);
});

test("chat and detail show the same history report beside long history without approval selection", async () => {
  const document=createDocument(),chat=document.createElement("main"),detail=document.createElement("main");
  const state={taskCards:new Map(),taskCardMeta:new Map()};
  const cards=createTaskCards({document,state,$:selector=>selector==="#task-detail"?detail:chat,
    setTimelineNode(){},appendArtifactList(){},scrollChat(){},
    renderChatTimeline(){reconcileChildren(chat,[...state.taskCards.values()]);},
    loadHistoryPage:async()=>page({items:[item("page-two")],has_more:false,next_cursor:null})});
  for(let index=0;index<160;index++){
    const old=task();delete old.oaHistoryQuery;old.task={...old.task,task_id:`old-${index}`,title:"保留历史 ".repeat(30)};cards.upsertTaskCard(old);
  }
  const result=task();
  result.actionableResults=[{sourceTaskId:"history-task",operationId:"should-not-approve",coverage:"complete",targets:[{targetKey:"not-pending",title:"错误审批入口",enabled:true}]}];
  cards.upsertTaskCard(result);cards.renderTaskDetail(result);
  const card=state.taskCards.get("history-task:summary");
  assert.equal(state.taskCards.size,161);
  for(const root of [card,detail]){
    assert.match(byClass(root,"oa-history-heading").textContent,/OA 已办历史查询 · 已查全/);
    assert.equal(visible(root).some(node=>node.type==="checkbox"),false);
    assert.equal(visible(root).some(node=>node.textContent==="错误审批入口"),false);
  }
  button(card,"下一页").events.click();await settle();
  for(const root of [card,detail])assert.match(rows(root)[0].textContent,/page-two/);
  const detailButton=buttons(detail).find(node=>node.textContent==="查看查询结果");
  await detailButton.events.click();assert.equal(document.activeElement,byClass(detail,"oa-history-panel"));
});

test("continuation synchronizes card and detail outer statuses through details, completion and access loss", async () => {
  const document=createDocument(),chat=document.createElement("main"),detail=document.createElement("main"),list=document.createElement("main");
  const state={taskCards:new Map(),taskCardMeta:new Map()}, responses=[
    page({state:"details",items:[],coverage:{status:"partial",scan_complete:true}}), page(),
    Object.assign(new Error("session changed"),{status:401})];
  const cards=createTaskCards({document,state,$:selector=>selector==="#task-detail"?detail:selector==="#task-list"?list:chat,
    setTimelineNode(){},appendArtifactList(){},scrollChat(){},
    renderChatTimeline(){reconcileChildren(chat,[...state.taskCards.values()]);},
    loadHistoryPage:async()=>{const response=responses.shift();if(response instanceof Error)throw response;return response;}});
  const initial=task(page({state:"scanning",items:[],coverage:{status:"partial"}}));
  initial.events=[{event_id:"read-complete",event_type:"task.completed",created_at:"2026-11-09T01:00:00Z"}];
  state.tasks=[initial.task];
  cards.upsertTaskCard(initial);cards.renderTaskDetail(initial);
  cards.renderTasks();const taskButton=list.childNodes[0];taskButton.focus();
  assert.match(list.textContent,/读取中/);
  assert.match(detail.textContent,/只读调用已结束/);assert.doesNotMatch(detail.textContent,/任务已完成/);
  const card=state.taskCards.get("history-task:summary");
  button(detail,"继续读取").events.click();await settle();
  assert.equal(byClass(card,"application-card-status").textContent,"核对中");
  assert.equal(byClass(detail,"eyebrow").textContent,"核对中");
  assert.match(list.textContent,/核对中/);assert.equal(document.activeElement,taskButton);
  for(const root of [card,detail]){
    assert.match(byClass(root,"application-card-copy").textContent,/正在核对匹配事项/);
    assert.doesNotMatch(root.textContent,/正在读取符合条件的历史事项/);
  }
  button(card,"继续读取").events.click();await settle();
  cards.upsertTaskCard(initial);cards.renderTaskDetail(initial);
  assert.equal(byClass(card,"application-card-status").textContent,"已查全");
  assert.equal(byClass(detail,"eyebrow").textContent,"已查全");
  assert.match(list.textContent,/已查全/);
  for(const root of [card,detail])assert.doesNotMatch(root.textContent,/详情尚未读完|尚未查全/);
  button(detail,"下一页").events.click();await settle();
  assert.equal(byClass(card,"application-card-status").textContent,"暂不可读");
  assert.equal(byClass(detail,"eyebrow").textContent,"暂不可读");
  assert.match(list.textContent,/暂不可读/);
  for(const root of [card,detail])assert.doesNotMatch(root.textContent,/本次查询已完成/);
});
