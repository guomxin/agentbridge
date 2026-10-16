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
  assert.equal(find(root,"button","选择全部").disabled,true);
  await find(root,"button","核对办理").events.click();
  // DOM event callbacks start the asynchronous preparation without blocking UI.
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(calls,[["source",{operationId:"operation",targetKeys:["one"],requestKey:"request"}]]);
  assert.match(root.textContent,/已记录准备请求/);
  assert.equal(descendants(root).filter(n=>n.tag==="button" && n.textContent==="核对办理")[0].disabled,true);
});
test("refresh and another view preserve selection, focus and one shared in-flight request", async () => {
  const document=createDocument(), state={}, a=document.createElement("div"), b=document.createElement("div");
  let release, count=0;
  const pending=new Promise(resolve=>{release=resolve;});
  const ui=createActionableResults({document,state,makeRequestKey:()=>"request",prepareTargets:async()=>{count++;await pending;return {taskId:"new"};}});
  ui.render(a,[group()]);ui.render(b,[group()]);
  const checkbox=find(a,"input");checkbox.checked=true;checkbox.events.change();checkbox.focus();
  ui.render(a,[group()]);
  assert.equal(document.activeElement,checkbox);
  assert.equal(find(b,"input").checked,true);
  find(a,"button","准备已选事项（1）").events.click();
  find(b,"button","正在准备…").events.click();
  assert.equal(count,1);release();await new Promise(resolve=>setImmediate(resolve));
});
test("lost single response can recover with the identical key and immutable target selection", async () => {
  const document=createDocument(), root=document.createElement("div"), calls=[];
  const ui=createActionableResults({document,state:{},makeRequestKey:()=>"fixed",prepareTargets:async (_task,body)=>{
    calls.push(body);if(calls.length===1)throw new Error("应答丢失");return {taskId:"new"};}});
  ui.render(root,[group()]);find(root,"button","核对办理").events.click();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(find(root,"input").disabled,true);
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
    assert.equal(find(root,"input").disabled,true);
    find(root,"button","恢复本次准备").events.click();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls.length,2);assert.deepEqual(calls[1],calls[0]);
  }
});
