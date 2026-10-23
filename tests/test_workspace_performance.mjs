import test from "node:test";
import assert from "node:assert/strict";
import {markWorkspaceTiming} from "../agentbridge/workspace/static/workspace_performance.mjs";

test("diagnostic marks accept only bounded identifiers and known stages", () => {
  const calls=[];
  const clock={clearMarks:n=>calls.push(["clear",n]),mark:(n,o)=>calls.push(["mark",n,o])};
  markWorkspaceTiming("accepted",{requestId:"turn-1",runId:"run-1",message:"private body",password:"secret",taskId:"invalid / identifier"},clock);
  assert.equal(calls.length,2);
  assert.equal(calls[1][2].detail.requestId,"turn-1");
  assert.equal(calls[1][2].detail.message,undefined);
  assert.equal(calls[1][2].detail.password,undefined);
  assert.equal(calls[1][2].detail.taskId,undefined);
  markWorkspaceTiming("private body",{},clock);
  assert.equal(calls.length,2);
});

test("missing or failed performance API never changes application outcome", () => {
  assert.doesNotThrow(()=>markWorkspaceTiming("send",{},{}));
  assert.doesNotThrow(()=>markWorkspaceTiming("send",{},{mark:()=>{throw Error("unsupported");}}));
});

test("every browser timing call site has an accepted stage including quick query, batch and recovery", async () => {
  const {readdir,readFile}=await import("node:fs/promises");
  const root=new URL("../agentbridge/workspace/static/",import.meta.url),stages=new Set();
  for (const file of await readdir(root)) {
    if (!/\.(mjs|js)$/.test(file)) continue;
    const source=await readFile(new URL(file,root),"utf8");
    for (const match of source.matchAll(/markWorkspaceTiming\(\s*["']([^"']+)["']/g)) stages.add(match[1]);
  }
  for (const stage of ["quick_query_sent","quick_query_queued","batch_selection_sent","batch_selection_queued",
    "recovery_probe_sent","recovery_probe_received","recovery_prepare_sent","recovery_prepare_received","form_opened"]) {
    assert.equal(stages.has(stage),true,`${stage} must be wired to a real browser boundary`);
  }
  for (const stage of stages) {
    const calls=[];markWorkspaceTiming(stage,{requestId:"request-1",taskId:"task-1",status:"unknown",
      opinion:"private opinion",assessmentHash:"private assessment",userMessage:"private text"},{mark:(name,options)=>calls.push([name,options])});
    assert.equal(calls.length,1,`unregistered timing stage ${stage}`);
    assert.deepEqual(Object.keys(calls[0][1].detail).sort(),["clockSource","observedAt","requestId","status","taskId"]);
  }
});

test("account reset clears every query and recovery mark from performance and its DOM mirror", async () => {
  const {clearWorkspaceTimings}=await import("../agentbridge/workspace/static/workspace_performance.mjs");
  const previous=globalThis.document,marks=new Set();
  try {
    globalThis.document={documentElement:{dataset:{}}};
    const clock={mark:name=>marks.add(name),clearMarks:name=>marks.delete(name)};
    for (const stage of ["quick_query_sent","quick_query_queued","batch_selection_sent","recovery_probe_received","recovery_prepare_received"]) {
      markWorkspaceTiming(stage,{requestId:"request-1",taskId:"task-1"},clock);
    }
    assert.equal(marks.size,5);assert.equal(Object.keys(globalThis.document.documentElement.dataset).length,5);
    clearWorkspaceTimings(clock);assert.equal(marks.size,0);assert.deepEqual(globalThis.document.documentElement.dataset,{});
  } finally {globalThis.document=previous;}
});
