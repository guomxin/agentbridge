import test from "node:test";
import assert from "node:assert/strict";
import { createActionableResults } from "../agentbridge/workspace/static/workspace_actionable.mjs";
import { createDocument, descendants } from "./workspace_dom_fixture.mjs";

const group = () => ({sourceTaskId:"source", operationId:"operation", targets:[
  {targetKey:"one", title:"考勤一", workflowLabel:"考勤确认", enabled:true},
  {targetKey:"two", title:"周报二", workflowLabel:"周报阅办", enabled:true},
  {targetKey:"unknown", title:"其他事项", workflowLabel:"暂未支持", enabled:false, disabledReason:"未支持的类型"},
]});
const find = (root, tag, text) => descendants(root).find(n => n.tag === tag && (!text || n.textContent === text));
test("single selection submits only server target keys and does not silently include disabled rows", async () => {
  const document=createDocument(), root=document.createElement("div"), calls=[];
  const ui=createActionableResults({document,state:{},makeRequestKey:()=>"request",prepareTargets:async (...args)=>{calls.push(args);return {taskId:"new"};}});
  ui.render(root,[group()]);
  assert.equal(find(root,"button","选择当前可办 2 项").disabled,false);
  await find(root,"button","核对办理").events.click();
  // DOM event callbacks start the asynchronous preparation without blocking UI.
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(calls,[["source",{operationId:"operation",targetKeys:["one"],requestKey:"request"}]]);
  assert.match(root.textContent,/已记录准备请求/);
  assert.equal(find(root,"button","查看进度").disabled,true);
});
test("refresh and another view preserve selection, focus and one shared in-flight request", async () => {
  const document=createDocument(), state={}, a=document.createElement("div"), b=document.createElement("div");
  let release, count=0;
  const pending=new Promise(resolve=>{release=resolve;});
  const ui=createActionableResults({document,state,makeRequestKey:()=>"request",prepareTargets:async()=>{count++;await pending;return {taskId:"new"};}});
  ui.render(a,[group()]);ui.render(b,[group()]);
  const checkbox=find(rows(a)[0],"input");checkbox.checked=true;checkbox.events.change();checkbox.focus();
  ui.render(a,[group()]);
  assert.equal(document.activeElement,checkbox);
  assert.equal(find(rows(b)[0],"input").checked,true);
  find(a,"button","准备已选事项（1）").events.click();
  find(b,"button","正在准备…").events.click();
  assert.equal(count,1);release();await new Promise(resolve=>setImmediate(resolve));
});
test("lost single response can recover with the identical key and immutable target selection", async () => {
  const document=createDocument(), root=document.createElement("div"), calls=[];
  const ui=createActionableResults({document,state:{},makeRequestKey:()=>"fixed",prepareTargets:async (_task,body)=>{
    calls.push(body);if(calls.length===1)throw new Error("应答丢失");return {taskId:"new"};}});
  ui.render(root,[group()]);find(root,"button","核对办理").events.click();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(find(rows(root)[0],"input").disabled,true);
  find(root,"button","恢复本次准备").events.click();await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(calls,[{operationId:"operation",targetKeys:["one"],requestKey:"fixed"},{operationId:"operation",targetKeys:["one"],requestKey:"fixed"}]);
});

test("an unconfirmed request remains recoverable after expiry or availability changes", async () => {
  for (const refreshed of [[], [{...group(),targets:group().targets.map(t=>({...t,enabled:false}))}]]) {
    const document=createDocument(),root=document.createElement("div"),calls=[];
    const ui=createActionableResults({document,state:{},makeRequestKey:()=>"fixed",prepareTargets:async (_task,body)=>{
      calls.push(body);if(calls.length===1)throw new Error("应答丢失");return {taskId:"existing"};}});
    ui.render(root,[group()]);find(root,"button","核对办理").events.click();await new Promise(resolve=>setImmediate(resolve));
    ui.render(root,refreshed);
    assert.equal(find(rows(root)[0],"input").disabled,true);
    find(root,"button","恢复本次准备").events.click();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls.length,2);assert.deepEqual(calls[1],calls[0]);
  }
});

const preparedGroup = (state, taskId = "child-task") => ({...group(), targets: group().targets.map((target, index) =>
  index === 0 ? {...target, enabled: false, preparation: {taskId, state}} : target)});
const flush = () => new Promise(resolve => setImmediate(resolve));
const rows = root => descendants(root).filter(node => node.className === "actionable-result-row");

test('restored view preferences are filtered through the fresh source and cannot restore disabled selection authority',()=>{
  const document=createDocument(),root=document.createElement('div'),saved=[];
  const ui=createActionableResults({document,state:{},viewState:{getSource:()=>({search:'周报',type:'',onlySelected:false,selected:['one','two','unknown','removed']}),setSource:(key,value)=>saved.push([key,value])}});
  ui.render(root,[{...group(),targets:group().targets.map(item=>({...item,enabled:item.targetKey==='two'}))}]);
  assert.deepEqual(saved.at(-1)[1].selected,['two']);
  assert.equal(rows(root)[0].hidden,true);assert.equal(rows(root)[1].hidden,false);
  assert.equal(find(rows(root)[1],'input').checked,true);
});

test('single and batch acknowledgements hand off once with current response and expose cancellation on the original card',async()=>{
  const document=createDocument(),root=document.createElement('div'),opened=[];let cancelCount=0;
  const ui=createActionableResults({document,state:{},makeRequestKey:()=>'batch',prepareTargets:async()=>({taskId:'child',detail:{task:{task_id:'child',status:'running'}}}),
    openPreparation:(id,options)=>{opened.push([id,options]);options.onProgress('正在准备',true);return ()=>{cancelCount++;options.onProgress('已停止自动打开',false);};}});
  ui.render(root,[group()]);find(root,'button','选择当前可办 2 项').events.click();find(root,'button','准备已选事项（2）').events.click();await flush();
  assert.equal(opened.length,1);assert.equal(opened[0][1].automatic,true);assert.equal(opened[0][1].detail.task.task_id,'child');
  const cancel=find(root,'button','取消自动打开');assert.equal(cancel.hidden,false);cancel.events.click();assert.equal(cancelCount,1);assert.equal(cancel.hidden,true);
  assert.match(root.textContent,/已停止自动打开/);
});

test("explicit available selection respects search and keeps unsupported items visible and counted", async () => {
  const document=createDocument(), root=document.createElement("div"), calls=[];
  const ui=createActionableResults({document,state:{},makeRequestKey:()=>"filtered",prepareTargets:async (...args)=>{calls.push(args);return {taskId:"new"};}});
  ui.render(root,[group()]);
  assert.match(root.textContent,/共 3 项 · 可新办 2 项.*当前不可新办 1 项/);
  const search=descendants(root).find(n=>n.type==="search");
  search.value="周报"; search.events.input();
  find(root,"button","选择当前可办 1 项").events.click();
  assert.deepEqual(rows(root).map(r=>find(r,"input").checked),[false,true,false]);
  find(root,"button","准备已选事项（1）").events.click(); await flush();
  assert.deepEqual(calls[0][1].targetKeys,["two"]);
});

test("expired sources lock fresh selection but query recovery preserves its original request key", async () => {
  const document=createDocument(), root=document.createElement("div"), calls=[];
  const ui=createActionableResults({document,state:{},now:()=>Date.parse("2026-10-22T12:00:00Z"),makeRequestKey:()=>"refresh-fixed",
    refreshQuery:async body=>{calls.push(body);if(calls.length===1)throw new Error("应答丢失");return {taskId:"query-new"};}});
  ui.render(root,[{...group(),expiresAt:"2026-10-22T11:59:00Z"}]);
  assert.equal(find(root,"button","选择当前可办 0 项").disabled,true);
  assert.ok(rows(root).every(row=>find(row,"input").disabled));
  find(root,"button","重新查询待办").events.click(); await flush();
  find(root,"button","恢复本次查询").events.click(); await flush();
  assert.deepEqual(calls,[{requestKey:"refresh-fixed"},{requestKey:"refresh-fixed"}]);
});

test("a lost refresh remains recoverable when its original source disappears before or after acknowledgement", async () => {
  const document=createDocument(), root=document.createElement("div"), state={}, calls=[];
  let reject;
  const first = new Promise((_, fail) => {reject=fail;});
  const ui=createActionableResults({document,state,makeRequestKey:()=>"refresh-frozen",
    refreshQuery:body=>{calls.push(body);return calls.length===1 ? first : Promise.resolve({taskId:"original-query"});}});
  ui.render(root,[group()]);find(root,"button","重新查询待办").events.click();
  ui.render(root,[]);
  assert.equal(root.hidden,false);assert.equal(find(root,"button","恢复本次查询").disabled,true);
  reject(new Error("查询回执丢失"));await flush();
  ui.render(root,[]);
  assert.match(root.textContent,/查询回执丢失/);
  assert.equal(find(root,"button","恢复本次查询").disabled,false);
  assert.ok(rows(root).every(row=>find(row,"input").disabled));
  find(root,"button","恢复本次查询").events.click();await flush();
  assert.deepEqual(calls,[{requestKey:"refresh-frozen"},{requestKey:"refresh-frozen"}]);
  ui.render(root,[]);assert.equal(root.hidden,true);assert.equal(state.actionableGroups.size,0);
});

test("a freshly loaded historical query restores completed business state and opens only the original result", () => {
  const document = createDocument(), root = document.createElement("div"), opened = [], calls = [];
  const ui = createActionableResults({document, state: {}, openPreparation: id => opened.push(id),
    prepareTargets: (...args) => {calls.push(args);}});
  ui.render(root, [preparedGroup("succeeded", "completed-reward")]);
  assert.match(rows(root)[0].textContent, /已完成/);
  assert.doesNotMatch(root.textContent, /已发起准备/);
  assert.equal(find(rows(root)[0], "input").disabled, true);
  const result = find(rows(root)[0], "button", "查看结果");
  assert.equal(result.disabled, false);
  result.events.click();
  assert.deepEqual(opened, ["completed-reward"]);
  assert.deepEqual(calls, []);
  assert.equal(find(root, "button", "选择当前可办 1 项").disabled, false);
});

test("accepted preparation advances through server review and submission states across both views without losing focus", async () => {
  const document = createDocument(), state = {}, a = document.createElement("div"), b = document.createElement("div");
  const opened = [], calls = [];
  const ui = createActionableResults({document, state, makeRequestKey: () => "one-request",
    openPreparation: id => opened.push(id), prepareTargets: async (...args) => {
      calls.push(args); return {taskId: "original-child"};
    }});
  ui.render(a, [group()]); ui.render(b, [group()]);
  const row = rows(a)[0], button = find(row, "button");
  button.focus(); button.events.click(); await flush();
  assert.match(row.textContent, /已发起准备/);
  assert.equal(find(row, "input").disabled, true);
  for (const [status, expected, label] of [["preparing", "正在准备材料", "查看进度"],
    ["waiting_user", "等待确认", "继续办理"], ["running", "提交中", "查看进度"],
    ["succeeded", "已完成", "查看结果"]]) {
    ui.render(a, [preparedGroup(status, "original-child")]);
    assert.equal(rows(a)[0], row);
    assert.equal(find(row, "button", label), button);
    assert.equal(document.activeElement, button);
    assert.match(rows(a)[0].textContent, new RegExp(expected));
    assert.match(rows(b)[0].textContent, new RegExp(expected));
    assert.doesNotMatch(a.textContent, /已发起准备/);
    button.events.click();
  }
  assert.equal(calls.length, 1);
  // The accepted selection starts a trusted-form handoff once, followed by the
  // four explicit progress/result actions. It never makes another preparation.
  assert.deepEqual(opened, ["original-child", "original-child", "original-child", "original-child", "original-child"]);
});

test("terminal or unknown child outcomes never reopen a preparation from an old query", () => {
  const document = createDocument(), root = document.createElement("div"), opened = [], calls = [];
  const ui = createActionableResults({document, state: {}, openPreparation: id => opened.push(id),
    prepareTargets: (...args) => {calls.push(args);}});
  for (const state of ["failed", "expired", "canceled", "outcome_unknown", "partially_succeeded", "superseded", "skipped"]) {
    const value = preparedGroup(state);
    // A stale broad enabled flag cannot turn an existing task into a new write.
    value.targets[0].enabled = true;
    ui.render(root, [value]);
    const row = rows(root)[0];
    assert.equal(find(row, "input").disabled, true, state);
    assert.equal(find(root, "button", "选择当前可办 1 项").disabled, false, state);
    const result = find(row, "button", "查看结果");
    assert.equal(result.disabled, false, state);
    if (state === "outcome_unknown") assert.match(row.textContent, /不要重复提交/);
    if (["expired", "canceled", "skipped"].includes(state)) assert.match(row.textContent, /重新查询待办/);
    if (state === "failed") assert.match(row.textContent, /查看原任务.*不要重复准备或提交/);
    result.events.click();
  }
  assert.equal(opened.length, 7);
  assert.deepEqual(calls, []);
});

test("an expired selection source still offers the exact pending child and preserves each batch item outcome", () => {
  const document = createDocument(), root = document.createElement("div"), opened = [];
  const ui = createActionableResults({document, state: {}, openPreparation: id => opened.push(id)});
  const value = group();
  value.targets = value.targets.map((target, index) => ({...target, enabled: false,
    disabledReason: "查询已过期，请重新查询待办。", ...(index < 2 ? {preparation: {
      taskId: "batch-task", state: index === 0 ? "succeeded" : "waiting_user"}} : {})}));
  ui.render(root, [value]);
  const [done, waiting, unsupported] = rows(root);
  assert.match(done.textContent, /已完成/);
  assert.match(waiting.textContent, /等待确认/);
  assert.match(unsupported.textContent, /查询已过期/);
  assert.equal(find(done, "button", "查看结果").disabled, false);
  assert.equal(find(waiting, "button", "继续办理").disabled, false);
  assert.equal(find(unsupported, "button", "核对办理").disabled, true);
  find(waiting, "button", "继续办理").events.click();
  assert.deepEqual(opened, ["batch-task"]);
});

test("a late preparation acknowledgement cannot replace a newer authoritative child outcome or task", async () => {
  const document = createDocument(), root = document.createElement("div"), opened = [];
  let release;
  const request = new Promise(resolve => {release = resolve;});
  const ui = createActionableResults({document, state: {}, openPreparation: id => opened.push(id),
    makeRequestKey: () => "one-request", prepareTargets: () => request});
  ui.render(root, [group()]);
  find(root, "button", "核对办理").events.click();
  ui.render(root, [preparedGroup("succeeded", "authoritative-child")]);
  release({taskId: "older-response-child"}); await flush();
  const row = rows(root)[0];
  assert.match(row.textContent, /已完成/);
  assert.doesNotMatch(root.textContent, /已发起准备|已记录准备请求/);
  find(row, "button", "查看结果").events.click();
  assert.deepEqual(opened, ["authoritative-child"]);
});

test("unrecognized or malformed child state stays locked without borrowing an arbitrary action label", () => {
  const document = createDocument(), root = document.createElement("div"), opened = [], calls = [];
  const ui = createActionableResults({document, state: {}, openPreparation: id => opened.push(id),
    prepareTargets: (...args) => calls.push(args)});
  for (const preparation of [{taskId: "known-child", state: "future", label: "立即重新审批"},
    {taskId: "known-child", state: "__proto__"}, [], "succeeded", {state: "succeeded"}]) {
    const value = group(); value.targets[0].preparation = preparation;
    ui.render(root, [value]);
    const row = rows(root)[0];
    assert.equal(find(row, "input").disabled, true);
    assert.doesNotMatch(row.textContent, /立即重新审批/);
    const button = find(row, "button");
    if (!button.disabled) button.events.click();
  }
  assert.deepEqual(opened, ["known-child", "known-child"]);
  assert.deepEqual(calls, []);
});

test("a fresh read-only completion updates every source and expires even when the source query has already expired", () => {
  const document=createDocument(),a=document.createElement("div"),b=document.createElement("div"),scheduled=new Map(),opened=[];
  const start=Date.parse("2026-10-23T12:00:00Z");let clock=start,next=0;
  const ui=createActionableResults({document,state:{},now:()=>clock,openPreparation:id=>opened.push(id),
    prepareTargets:()=>assert.fail("verified source cannot prepare"),
    timers:{setTimeout:(fn,ms)=>{scheduled.set(++next,{fn,ms});return next;},clearTimeout:id=>scheduled.delete(id)}});
  const value=preparedGroup("verified_complete","original-unknown");value.expiresAt=new Date(start-60000).toISOString();
  value.targets[0].preparation={taskId:"original-unknown",state:"verified_complete",observedAt:new Date(start).toISOString(),expiresAt:new Date(start+120000).toISOString()};
  ui.render(a,[value]);ui.render(b,[value]);
  for (const root of [a,b]) {
    const row=rows(root)[0];assert.equal(row.dataset.preparationState,"verified_complete");
    assert.match(row.textContent,/原事项已核验完成.*核验时间/);assert.equal(find(row,"input").disabled,true);
    find(row,"button","查看结果").events.click();
  }
  assert.deepEqual(opened,["original-unknown","original-unknown"]);assert.equal(scheduled.size,1);
  clock=start+120001;const timer=[...scheduled.values()][0];scheduled.clear();timer.fn();
  for (const root of [a,b]) {
    const row=rows(root)[0];assert.equal(row.dataset.preparationState,"outcome_unknown");
    assert.match(row.textContent,/上次核验已完成.*核对已过期.*重新核对当前状态/);
    assert.equal(find(row,"input").disabled,true);assert.equal(find(row,"button","核对原事项").disabled,false);
  }
  assert.equal(scheduled.size,0);
  clock=start;ui.render(a,[value]);assert.equal(scheduled.size,1);ui.reset();assert.equal(scheduled.size,0);
});

test("a read-only completion without a valid evidence expiry remains historical and cannot reopen selection", () => {
  const document=createDocument(),root=document.createElement("div");
  const ui=createActionableResults({document,state:{},openPreparation(){},prepareTargets:()=>assert.fail("must remain read only")});
  ui.render(root,[preparedGroup("verified_complete","original-unknown")]);
  const row=rows(root)[0];assert.equal(row.dataset.preparationState,"outcome_unknown");
  assert.match(row.textContent,/核对已过期/);assert.equal(find(row,"input").disabled,true);
});

test('remaining query preserves original ordinals and permits selection only from its server-filtered targets',async()=>{
 const document=createDocument(),root=document.createElement('div'),calls=[];
 const ui=createActionableResults({document,state:{},makeRequestKey:()=>'remaining-selection',prepareTargets:async(...args)=>{calls.push(args);return {taskId:'new'};}});
 const value={...group(),targets:[group().targets[0]],remaining:{requestedCount:3,matchedCount:1,missingCount:1,changedCount:1,
 items:[{ordinal:3,title:'仍匹配',state:'matched'},{ordinal:4,title:'不在待办',state:'missing'},{ordinal:5,title:'变更',state:'changed'}]}};
 ui.render(root,[value]);assert.match(root.textContent,/剩余 3 项.*匹配 1 项.*未找到 1 项.*资格变化 1 项/);
 assert.match(root.textContent,/原第 4 项.*未在当前待办找到/);assert.equal(rows(root).length,1);
 find(root,'button','选择当前可办 1 项').events.click();find(root,'button','准备已选事项（1）').events.click();await flush();
 assert.deepEqual(calls,[['source',{operationId:'operation',targetKeys:['one'],requestKey:'remaining-selection'}]]);
});
