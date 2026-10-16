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
