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
function task(query = page(), version = 1, taskId = "history-task") {
  return {task:{task_id:taskId,version,status:"succeeded",title:"查询 OA 历史",created_at:"2026-11-09T01:00:00Z"},
    oaHistoryQuery:query,interactions:[],events:[]};
}
function fixture(options = {}) {
  const document = createDocument(), root = document.createElement("section"), state = {};
  const renderer = createOaHistoryResults({document,state,...options});
  return {document,root,state,...renderer};
}

test('latest selection distinguishes all-source matches from the selected and verified candidate range',()=>{
  const f=fixture();f.render(f.root,task(page({collection:'sent',items:[item('sixth')],has_more:false,next_cursor:null,
    selection:{kind:'latest',count:1,workflow_state:'in_progress',date_basis:'initiated_at',order:'desc',scope:'all_matching_history',
      selected_count:1,selection_complete:true,reason:'complete'},
    progress:{source_total:224,scanned_count:224,matched_count:224,details_checked:1,details_failed:0},
    coverage:{status:'complete',scan_complete:true,details_complete:true,details_scope:'selection_candidates'}})));
  assert.match(byClass(f.root,'oa-history-heading').textContent,/选取完成/);
  assert.match(byClass(f.root,'oa-history-range').textContent,/最近 1 条流转中事项；已选 1 条/);
  assert.match(byClass(f.root,'oa-history-progress').textContent,/全源匹配 224.*所选候选已核对详情 1/);
  assert.doesNotMatch(byClass(f.root,'oa-history-notice').textContent,/全部.*224|所有.*查全/);
});

test('latest selection empty and uncertain outcomes do not claim no matching history or complete detail coverage',()=>{
  for(const [reason,message] of [['selection_tie',/相同时间/],['selection_date_unverified',/日期未核实/],
    ['selection_state_unverified',/更近事项.*未核实/],['source_row_budget',/尚未核实完整/]]){
    const f=fixture();f.render(f.root,task(page({state:'partial',items:[],has_more:false,next_cursor:null,
      selection:{kind:'latest',count:1,workflow_state:'in_progress',date_basis:'initiated_at',selected_count:0,selection_complete:false,reason},
      coverage:{status:'partial',scan_complete:true,details_complete:false,details_scope:'selection_candidates'}})));
    assert.match(byClass(f.root,'oa-history-heading').textContent,/选取未完成/);
    assert.match(byClass(f.root,'oa-history-notice').textContent,message);
    assert.match(byClass(f.root,'oa-history-empty').textContent,message);
    assert.doesNotMatch(byClass(f.root,'oa-history-empty').textContent,/该范围内没有/);
  }
  const empty=fixture();empty.render(empty.root,task(page({items:[],has_more:false,next_cursor:null,
    selection:{kind:'latest',count:1,workflow_state:'in_progress',selected_count:0,selection_complete:true,reason:'complete'}})));
  assert.equal(byClass(empty.root,'oa-history-empty').textContent,'该范围内没有符合所选状态的事项。');
});

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
    f.render(f.root,task(page(),2));assert.equal(rows(f.root).length,0);
    f.render(f.root,task(failure,3));assert.equal(rows(f.root).length,0);
    f.render(f.root,task(page(),2));assert.equal(rows(f.root).length,0);
  }
});

test("independent tasks for one query share live progress and paging without comparing task versions", async () => {
  const request=deferred(),calls=[];
  const f=fixture({loadHistoryPage:(id,cursor)=>{calls.push([id,cursor]);return request.promise;}});
  const next=f.document.createElement("section"),detail=f.document.createElement("section");
  const scan=page({state:"scanning",items:[],coverage:{status:"partial"},progress:{scanned_count:1000,details_checked:0}});
  f.render(f.root,task(scan,20,"initial-task"));f.render(detail,task(scan,20,"initial-task"));
  f.render(next,task(page({state:"details",items:[],coverage:{status:"partial"},progress:{scanned_count:1125,details_checked:5}}),2,"next-task"));
  assert.equal(f.state.oaHistoryQueries.get("initial-task"),f.state.oaHistoryQueries.get("next-task"));
  for(const root of [f.root,next,detail])assert.match(byClass(root,"oa-history-progress").textContent,/已扫描 1125.*已核对详情 5/);
  f.render(f.root,task(scan,21,"initial-task"));
  assert.match(byClass(next,"oa-history-heading").textContent,/核对中/);
  f.render(next,task(page(),3,"next-task"));
  for(const root of [f.root,next,detail])assert.match(rows(root)[0].textContent,/first/);
  button(next,"下一页").events.click();button(detail,"正在读取…").events.click();
  assert.deepEqual(calls,[["initial-task","opaque-next"]]);
  request.resolve(page({items:[item("shared-second")],has_more:false,next_cursor:null}));await settle();
  f.render(next,task(page(),4,"next-task"));
  for(const root of [f.root,next,detail]){
    assert.match(rows(root)[0].textContent,/shared-second/);
    assert.match(byClass(root,"oa-history-page").textContent,/第 2 页/);
  }
  button(detail,"上一页").events.click();
  for(const root of [f.root,next,detail])assert.match(rows(root)[0].textContent,/first/);
});

test("different query identities and dates never share models or pages", async () => {
  const f=fixture({loadHistoryPage:async()=>page({items:[item("query-one-second")],has_more:false,next_cursor:null})});
  const second=f.document.createElement("section"),unbound=f.document.createElement("section");
  f.render(f.root,task());
  f.render(second,task(page({query_id:"query-two",items:[item("separate-date")],
    temporal:{absoluteRange:{start:"2026-10-01",end:"2026-10-31"},dateBasis:"processed_at",precision:"day"}}),1,"another-task"));
  const pending=task(undefined,1,"unbound-next");delete pending.oaHistoryQuery;f.render(unbound,pending);
  assert.equal(f.state.oaHistoryQueries.has("unbound-next"),false);
  assert.notEqual(f.state.oaHistoryQueries.get("history-task"),f.state.oaHistoryQueries.get("another-task"));
  button(f.root,"下一页").events.click();await settle();
  assert.match(rows(second)[0].textContent,/separate-date/);
  assert.match(byClass(second,"oa-history-range").textContent,/2026-10-01 至 2026-10-31/);
  assert.match(byClass(second,"oa-history-page").textContent,/第 1 页/);
});

test("query access loss clears every task alias and blocks higher-version and new-task stale snapshots", async () => {
  for(const status of [401,403,404,410]){
    const f=fixture({loadHistoryPage:async()=>{throw Object.assign(new Error("denied"),{status});}});
    const second=f.document.createElement("section"),late=f.document.createElement("section");
    f.render(f.root,task());f.render(second,task(page(),50,"next-task"));
    button(second,"下一页").events.click();await settle();
    f.render(f.root,task(page(),100));f.render(second,task(page(),51,"next-task"));
    f.render(late,task(page(),1,"late-task"));
    for(const root of [f.root,second,late]){assert.equal(rows(root).length,0);assert.match(root.textContent,/已清除旧结果/);}
    f.render(second,task(page({query_id:"new-authorized-query",items:[item("new-query")]}),52,"next-task"));
    assert.match(rows(second)[0].textContent,/new-query/);assert.equal(rows(f.root).length,0);
  }
});

test("a task losing its projection cannot erase another task's authorized shared query", () => {
  const f=fixture(),second=f.document.createElement("section"),detail=f.document.createElement("section");
  f.render(f.root,task());f.render(detail,task());f.render(second,task(page(),1,"next-task"));
  const missing=task(undefined,2);delete missing.oaHistoryQuery;f.render(f.root,missing);
  for(const root of [f.root,detail]){assert.equal(rows(root).length,0);assert.match(root.textContent,/已清除旧结果/);}
  assert.match(rows(second)[0].textContent,/first/);
  f.render(f.root,task());assert.equal(rows(f.root).length,0);
  f.render(f.root,task(page(),2));assert.equal(rows(f.root).length,0);
  assert.notEqual(f.state.oaHistoryQueries.get("history-task"),f.state.oaHistoryQueries.get("next-task"));
});

test("retiring the last unavailable task removes query data while retaining its account-scoped access barrier", async () => {
  const lifecycle=createLifecycle();let scope=lifecycle.scope();
  const f=fixture({getScope:()=>scope,loadHistoryPage:async()=>{throw Object.assign(new Error("expired"),{status:410});}});
  f.render(f.root,task());button(f.root,"下一页").events.click();await settle();
  f.retire("history-task");assert.equal(f.state.oaHistoryQueries.size,0);
  f.render(f.root,task(page(),100,"late-task"));assert.equal(rows(f.root).length,0);
  assert.match(f.root.textContent,/旧结果已清除/);
  scope.dispose();scope=lifecycle.scope();
  f.render(f.root,task(page({items:[item("fresh-scope")]}),1,"fresh-task"));
  assert.match(rows(f.root)[0].textContent,/fresh-scope/);
});

test("retiring a task cancels its pending reply, keeps other members and releases the last query", async () => {
  const first=deferred(),second=deferred(),calls=[];
  const f=fixture({loadHistoryPage:(id,cursor)=>{calls.push([id,cursor]);return calls.length===1?first.promise:second.promise;}});
  const peer=f.document.createElement("section");
  f.render(f.root,task());f.render(peer,task(page(),1,"next-task"));
  const model=f.state.oaHistoryQueries.get("history-task");
  button(peer,"下一页").events.click();f.retire("history-task");
  assert.equal(f.root.hidden,true);assert.equal(f.root.childNodes.length,0);
  assert.equal(f.state.oaHistoryQueries.has("history-task"),false);
  button(peer,"下一页").events.click();
  first.resolve(page({items:[item("retired-reply")]}));await settle();
  assert.equal(button(peer,"正在读取…").disabled,true);assert.doesNotMatch(peer.textContent,/retired-reply/);
  second.resolve(page({items:[item("remaining-task-page")],has_more:false,next_cursor:null}));await settle();
  assert.match(rows(peer)[0].textContent,/remaining-task-page/);
  assert.deepEqual(calls,[["history-task","opaque-next"],["next-task","opaque-next"]]);
  f.retire("next-task");assert.equal(f.state.oaHistoryQueries.size,0);
  assert.equal(model.disposed,true);assert.equal(model.pages.length,0);assert.equal(model.projection,null);
});

test("account reset clears all aliases and permits a fresh model without reviving outstanding replies", async () => {
  const request=deferred(),f=fixture({loadHistoryPage:()=>request.promise}),peer=f.document.createElement("section");
  f.render(f.root,task());f.render(peer,task(page(),1,"next-task"));
  const old=f.state.oaHistoryQueries.get("history-task");button(peer,"下一页").events.click();f.reset();
  assert.equal(f.state.oaHistoryQueries.size,0);
  for(const root of [f.root,peer])assert.equal(root.childNodes.length,0);
  f.render(f.root,task(page({items:[item("fresh-account")]})));
  assert.notEqual(f.state.oaHistoryQueries.get("history-task"),old);
  request.resolve(page({items:[item("old-account-reply")]}));await settle();
  assert.match(rows(f.root)[0].textContent,/fresh-account/);assert.doesNotMatch(f.root.textContent,/old-account-reply/);
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

test('watch entry exists only for trusted references and passes only the current target and title',()=>{
  const calls=[],f=fixture({onWatch:value=>calls.push(value)});
  const query=page({items:[item('one',{target_ref:'opaque-first'}),item('without-source')]});
  f.render(f.root,task(query));
  const first=button(rows(f.root)[0],'关注后续'),missing=button(rows(f.root)[1],'关注后续');
  assert.equal(first.hidden,false);assert.equal(missing.hidden,true);missing.events.click();assert.deepEqual(calls,[]);
  first.events.click();assert.deepEqual(calls,[{title:'业务结算单 one',target_ref:'opaque-first'}]);
  f.render(f.root,task(page({items:[item('one',{title:'新标题',target_ref:'opaque-new'})]}),2));
  assert.equal(button(rows(f.root)[0],'关注后续'),first);first.events.click();
  assert.deepEqual(calls.at(-1),{title:'新标题',target_ref:'opaque-new'});
  f.render(f.root,task(page({items:[item('one')]}),3));first.events.click();assert.equal(calls.length,2);
});

test('expired queries, removed pages and access loss cannot activate an old watch button',async()=>{
  const calls=[],f=fixture({onWatch:value=>calls.push(value),loadHistoryPage:async()=>page({items:[item('second',{target_ref:'second-ref'})],has_more:false})});
  f.render(f.root,task(page({expires_at:'2020-01-01T00:00:00Z',items:[item('one',{target_ref:'expired-ref'})]})));
  const expired=button(rows(f.root)[0],'关注后续');assert.equal(expired.disabled,true);expired.events.click();assert.equal(calls.length,0);
  f.render(f.root,task(page({items:[item('one',{target_ref:'current-ref'})]}),2));
  const old=button(rows(f.root)[0],'关注后续');button(f.root,'下一页').events.click();await settle();
  old.events.click();assert.equal(calls.length,0);
  const current=button(rows(f.root)[0],'关注后续');current.events.click();assert.equal(calls.length,1);
  f.render(f.root,task({schema_version:'agentbridge.oa_history_query.v1',query_id:'query-one',state:'unavailable'},3));
  current.events.click();assert.equal(calls.length,1);
});

test('disposing the account clears watch sources and prevents retained callbacks from opening creation',()=>{
  const lifecycle=createLifecycle(),scope=lifecycle.scope(),calls=[],f=fixture({getScope:()=>scope,onWatch:value=>calls.push(value)});
  f.render(f.root,task(page({items:[item('one',{target_ref:'account-a-ref'})]})));
  const old=button(rows(f.root)[0],'关注后续');scope.dispose();old.events.click();
  assert.deepEqual(calls,[]);assert.equal(f.root.childNodes.length,0);lifecycle.dispose();
});
