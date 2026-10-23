import test from "node:test";
import assert from "node:assert/strict";
import { createOaPendingRecovery } from "../agentbridge/workspace/static/workspace_recovery.mjs";
import { createTaskCards } from "../agentbridge/workspace/static/workspace_cards.mjs";
import { createDocument, descendants } from "./workspace_dom_fixture.mjs";

const ORIGINAL = "11111111-1111-1111-1111-111111111111";
const CHILD = "22222222-2222-2222-2222-222222222222";
const ASSESSMENT = "33333333-3333-3333-3333-333333333333";
const T0 = Date.parse("2026-10-23T10:00:00Z");
const assessment = (overrides = {}) => ({status:"unresolved_still_pending", assessmentId:ASSESSMENT,
  assessmentHash:`sha256:${"a".repeat(64)}`, revision:1, expiresAt:new Date(T0 + 120000).toISOString(),
  reason:"still_pending_previous_unknown", doneCoverage:"complete", canPrepare:true, canExecute:false, previousOutcome:"unknown", ...overrides});
const outcome = (id = ORIGINAL) => ({task:{task_id:id, version:1, status:"outcome_unknown", title:"效能数据审批"},
  businessExecution:{schemaVersion:"agentbridge.business-execution.v1", phase:"outcome_unknown", readOnlyQuery:"oa_pending"},
  oaPendingRecoveryAvailable:true, interactions:[], events:[]});
const flush = () => new Promise(resolve => setImmediate(resolve));
const find = (root, tag, text) => descendants(root).find(n => n.tag === tag && (!text || n.textContent === text));
const click = async button => { button.events.click(); await flush(); };
function fixture(options = {}) {
  const document = createDocument(), state = {}, calls = {probe:[], prepare:[], open:[]};
  let key = 0, clock = T0;
  const ui = createOaPendingRecovery({document, state, now:() => clock, makeRequestKey:() => `synthetic-key-${++key}`,
    probeRecovery:async (...args) => {calls.probe.push(args);return assessment();},
    prepareRecovery:async (...args) => {calls.prepare.push(args);return {status:"prepared", taskId:CHILD,
      originalTaskId:args[0], assessmentId:ASSESSMENT, canExecute:false};},
    openTask:id => calls.open.push(id), ...options});
  const root = document.createElement("section");ui.render(root, outcome());
  return {document, state, root, calls, ui, setClock:value => {clock = value;}};
}

test("only a fresh original-item assessment and explicit risk acknowledgement can prepare one new form", async () => {
  const f = fixture();
  assert.equal(find(f.root,"button","准备全新办理表单").hidden,true);
  await click(find(f.root,"button","只读核对原事项"));
  assert.equal(f.calls.probe.length,1);
  assert.deepEqual(Object.keys(f.calls.probe[0][1]),["requestKey"]);
  const prepare = find(f.root,"button","准备全新办理表单");
  assert.equal(prepare.disabled,true);await click(prepare);assert.deepEqual(f.calls.prepare,[]);
  const ack = find(f.root,"input");ack.checked = true;ack.events.change();
  await click(prepare);
  assert.equal(f.calls.prepare.length,1);
  assert.deepEqual(f.calls.prepare[0],[ORIGINAL,{assessmentId:ASSESSMENT, assessmentHash:`sha256:${"a".repeat(64)}`,
    expectedRevision:1, requestKey:"synthetic-key-2", acknowledgement:"oa_unknown_new_attempt_v1"}]);
  assert.match(f.root.textContent,/新的办理任务已准备.*填写意见.*最终确认/);
  assert.deepEqual(f.calls.open,[]);await click(find(f.root,"button","查看新办理表单"));
  assert.deepEqual(f.calls.open,[CHILD]);
  assert.equal(prepare.hidden,true);assert.equal(ack.checked,false);
});

test("one task shares pending probe, risk acknowledgement and preparation across chat and detail without double submission", async () => {
  let finishProbe, finishPrepare;
  const calls = {probe:[], prepare:[]};
  const f = fixture({probeRecovery:(...args) => {calls.probe.push(args);return new Promise(done => {finishProbe=done;});},
    prepareRecovery:(...args) => {calls.prepare.push(args);return new Promise(done => {finishPrepare=done;});}});
  const detail = f.document.createElement("section");f.ui.render(detail,outcome());
  find(f.root,"button","只读核对原事项").events.click();
  assert.equal(find(detail,"button","正在核对原事项…").disabled,true);
  find(detail,"button","正在核对原事项…").events.click();assert.equal(calls.probe.length,1);
  finishProbe(assessment());await flush();
  const ack = find(detail,"input");ack.checked=true;ack.events.change();
  assert.equal(find(f.root,"input").checked,true);
  find(f.root,"button","准备全新办理表单").events.click();
  find(detail,"button","正在准备新表单…").events.click();assert.equal(calls.prepare.length,1);
  finishPrepare({status:"prepared",taskId:CHILD,originalTaskId:ORIGINAL,assessmentId:ASSESSMENT,canExecute:false});await flush();
  assert.equal(find(detail,"button","查看新办理表单").hidden,false);
});

test("partial or unknown done coverage discloses its limits in both the assessment and risk acknowledgement", async () => {
  for (const doneCoverage of ["partial", "incomplete", "unknown", undefined]) {
    const f=fixture({probeRecovery:async () => assessment({doneCoverage})});
    await click(find(f.root,"button","只读核对原事项"));
    const notice=descendants(f.root).find(n => n.className === "oa-recovery-status");
    const risk=descendants(f.root).find(n => n.className === "oa-recovery-risk");
    assert.match(notice.textContent,/已办记录仅部分覆盖，未找到记录不能证明上次未提交/);
    assert.match(risk.textContent,/已办记录仅部分覆盖，未找到记录不能证明上次未提交.*原提交结果仍未知.*重复处理风险/);
    const prepare=find(f.root,"button","准备全新办理表单");
    assert.equal(prepare.disabled,true);assert.deepEqual(f.calls.prepare,[]);
    const ack=find(f.root,"input");ack.checked=true;ack.events.change();
    await click(prepare);assert.equal(f.calls.prepare.length,1);
  }
  const full=fixture();await click(find(full.root,"button","只读核对原事项"));
  assert.doesNotMatch(full.root.textContent,/已办记录仅部分覆盖/);
  assert.match(full.root.textContent,/原提交结果仍未知.*重复处理风险/);
});

test("lost prepare response freezes its original assessment, acknowledgement and request until receipt recovery", async () => {
  const calls = [];let count=0;
  const f = fixture({prepareRecovery:async (...args) => {calls.push(args);if (++count === 1) throw Error("响应丢失");
    return {status:"existing",taskId:CHILD,originalTaskId:ORIGINAL,assessmentId:ASSESSMENT,canExecute:false};}});
  await click(find(f.root,"button","只读核对原事项"));
  const ack=find(f.root,"input");ack.checked=true;ack.events.change();
  await click(find(f.root,"button","准备全新办理表单"));
  assert.equal(ack.disabled,true);assert.equal(ack.checked,true);
  assert.equal(find(f.root,"button","只读核对原事项").disabled,true);
  f.setClock(T0+180000);
  await click(find(f.root,"button","恢复本次新表单准备"));
  assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);
  assert.equal(find(f.root,"button","查看新办理表单").hidden,false);
});

test("expired assessments and explicit server conflicts require a new read and new acknowledgement", async () => {
  const f=fixture();await click(find(f.root,"button","只读核对原事项"));
  const ack=find(f.root,"input");ack.checked=true;ack.events.change();
  f.setClock(T0+120001);await click(find(f.root,"button","准备全新办理表单"));
  assert.deepEqual(f.calls.prepare,[]);assert.match(f.root.textContent,/核对已过期/);
  f.setClock(T0);await click(find(f.root,"button","只读核对原事项"));
  assert.equal(find(f.root,"input").checked,false);
  assert.notEqual(f.calls.probe[0][1].requestKey,f.calls.probe[1][1].requestKey);

  const conflict=fixture({prepareRecovery:async () => {throw Object.assign(Error("登录身份已变化"),{status:409});}});
  await click(find(conflict.root,"button","只读核对原事项"));
  const input=find(conflict.root,"input");input.checked=true;input.events.change();
  await click(find(conflict.root,"button","准备全新办理表单"));
  assert.equal(input.checked,false);assert.equal(find(conflict.root,"button","准备全新办理表单").hidden,true);
  assert.equal(find(conflict.root,"button","只读核对原事项").disabled,false);
});

test("a confirmed source-read failure can retry its same request or refresh evidence for the same child", async () => {
  let attempt=0, probes=0;
  const calls=[];
  const f=fixture({probeRecovery:async () => ++probes === 1 ? assessment() : assessment({
    assessmentId:"44444444-4444-4444-4444-444444444444", expiresAt:new Date(T0+360000).toISOString(),
    recoveryTaskId:CHILD, childPreparation:"retryable"}),
  prepareRecovery:async (...args) => {
    calls.push(args);
    if (++attempt <= 2) throw Object.assign(Error("同一恢复任务的材料暂未准备好"),{status:409,code:"OA_RECOVERY_PREPARE_FAILED"});
    return {status:"existing", taskId:CHILD, originalTaskId:ORIGINAL,
      assessmentId:args[1].assessmentId, canExecute:false};
  }});
  await click(find(f.root,"button","只读核对原事项"));
  const ack=find(f.root,"input");ack.checked=true;ack.events.change();
  await click(find(f.root,"button","准备全新办理表单"));
  assert.equal(ack.checked,true);assert.equal(ack.disabled,true);
  assert.equal(find(f.root,"button","只读核对原事项").disabled,false);
  await click(find(f.root,"button","恢复本次新表单准备"));
  assert.deepEqual(calls[0],calls[1]);
  f.setClock(T0+180000);
  await click(find(f.root,"button","只读核对原事项"));
  assert.match(f.root.textContent,/同一恢复任务的材料尚未准备完成.*不会创建第二次恢复任务或提交/);
  assert.equal(ack.checked,false);
  const retry=find(f.root,"button","恢复同一任务材料准备");assert.equal(retry.disabled,true);
  ack.checked=true;ack.events.change();await click(retry);
  assert.equal(calls.length,3);assert.notEqual(calls[1][1].requestKey,calls[2][1].requestKey);
  assert.notEqual(calls[1][1].assessmentId,calls[2][1].assessmentId);
  assert.equal(find(f.root,"button","查看新办理表单").hidden,false);
});

test("a ready or locked existing child cannot enable source preparation even if a malformed response claims otherwise", async () => {
  for (const childPreparation of ["ready","locked"]) {
    const f=fixture({probeRecovery:async () => assessment({recoveryTaskId:CHILD,childPreparation,canPrepare:false})});
    await click(find(f.root,"button","只读核对原事项"));
    assert.equal(find(f.root,"button","准备全新办理表单").hidden,true);
    assert.deepEqual(f.calls.prepare,[]);
    const malformed=fixture({probeRecovery:async () => assessment({recoveryTaskId:CHILD,childPreparation,canPrepare:true})});
    await click(find(malformed.root,"button","只读核对原事项"));
    assert.equal(find(malformed.root,"button","准备全新办理表单").hidden,true);
    assert.match(malformed.root.textContent,/核对回执尚未确认/);
  }
});

test("verified-complete, blocked and existing-child evidence never enables a new prepare", async () => {
  for (const result of [assessment({status:"verified_complete",canPrepare:false}),
    assessment({status:"blocked",canPrepare:false,reason:"pending_coverage_incomplete"}),
    assessment({canPrepare:false,recoveryTaskId:CHILD})]) {
    const f=fixture({probeRecovery:async () => result});
    await click(find(f.root,"button","只读核对原事项"));
    assert.equal(find(f.root,"button","准备全新办理表单").hidden,true);
    assert.equal(find(f.root,"button","查看新办理表单").hidden,!result.recoveryTaskId);
    assert.deepEqual(f.calls.prepare,[]);
    if (result.status === "verified_complete") assert.match(f.root.textContent,/核验原事项办理完成.*无需重新准备/);
    if (result.status === "blocked") assert.match(f.root.textContent,/列表不完整/);
  }
});

test("an original task completing during preparation remains a verification-only outcome without a child", async () => {
  const original=outcome();
  const f=fixture({prepareRecovery:async () => ({status:"verified_complete",taskId:ORIGINAL,
    originalTaskId:ORIGINAL,assessmentId:ASSESSMENT,canExecute:false})});
  f.ui.render(f.root,original);await click(find(f.root,"button","只读核对原事项"));
  const ack=find(f.root,"input");ack.checked=true;ack.events.change();
  await click(find(f.root,"button","准备全新办理表单"));
  assert.match(f.root.textContent,/已办理完成.*无需新的办理/);
  assert.equal(find(f.root,"button","查看新办理表单").hidden,true);
  assert.equal(original.task.status,"outcome_unknown");
});

test("assessment state is isolated across tasks and discarded account models cannot acknowledge a later account", async () => {
  const f=fixture(), other=f.document.createElement("section");
  f.ui.render(other,outcome(CHILD));await click(find(f.root,"button","只读核对原事项"));
  const ack=find(f.root,"input");ack.checked=true;ack.events.change();
  assert.equal(find(other,"input").checked,false);
  assert.equal(find(other,"button","准备全新办理表单").hidden,true);
  f.state.oaPendingRecovery.clear();
  const next=f.document.createElement("section");f.ui.render(next,outcome());
  assert.equal(find(next,"input").checked,false);
  assert.equal(find(next,"button","准备全新办理表单").hidden,true);
});

test("business-card recovery opens only the new task while keeping the existing context read-only query", async () => {
  const document=createDocument(), chat=document.createElement("main"), detail=document.createElement("main"), calls=[];
  const cards=createTaskCards({document,state:{taskCards:new Map(),taskCardMeta:new Map()},
    $:selector => selector === "#task-detail" ? detail : chat, renderChatTimeline() {}, scrollChat() {}, setTimelineNode() {},
    appendArtifactList() {}, makeFeedbackRequestKey:() => "new-recovery-key", switchView:async name => {calls.push(["view",name]);},
    loadTaskDetail:id => {calls.push(["task",id]);}, refreshQuery:async payload => {calls.push(["query",payload]);return {taskId:CHILD};},
    probeRecovery:async () => assessment({expiresAt:"2099-10-23T10:02:00Z"}),
    prepareRecovery:async () => ({status:"prepared",taskId:CHILD,originalTaskId:ORIGINAL,assessmentId:ASSESSMENT,canExecute:false})});
  cards.renderTaskDetail(outcome());
  await click(find(detail,"button","只读查询 OA 待办"));
  assert.deepEqual(calls[0],["query",{requestKey:"new-recovery-key"}]);
  await click(find(detail,"button","只读核对原事项"));
  const ack=find(detail,"input");ack.checked=true;ack.events.change();
  await click(find(detail,"button","准备全新办理表单"));
  await click(find(detail,"button","查看新办理表单"));
  assert.deepEqual(calls.at(-2),["view","tasks"]);assert.deepEqual(calls.at(-1),["task",CHILD]);
});

test("persisted recovery projection restores a child and completion without reviving an execution assessment", () => {
  const f=fixture(), r=outcome();
  r.oaPendingRecovery={originalTaskId:ORIGINAL,status:"recovery_in_progress",availability:"can_probe",canProbe:true,
    canPrepare:false,canExecute:false,nextAction:"open_recovery",recoveryTaskId:CHILD,recoveryTaskStatus:"waiting_user"};
  f.ui.render(f.root,r);
  assert.equal(find(f.root,"button","查看新办理表单").hidden,false);
  assert.equal(find(f.root,"button","准备全新办理表单").hidden,true);
  assert.equal(find(f.root,"input").checked,false);
  r.oaPendingRecovery={...r.oaPendingRecovery,status:"recovery_succeeded",recoveryTaskStatus:"succeeded",canProbe:false};
  f.ui.render(f.root,r);
  assert.match(f.root.textContent,/后续办理已完成并取得业务核验证据/);
  assert.equal(find(f.root,"button","查看办理结果").hidden,false);
  assert.equal(find(f.root,"button","只读核对原事项").hidden,true);
  assert.equal(r.task.status,"outcome_unknown");
});

test("an unavailable original recovery remains visible with an actionable reason instead of disappearing", () => {
  const f=fixture(),r=outcome();r.oaPendingRecoveryAvailable=false;
  r.oaPendingRecovery={originalTaskId:ORIGINAL,status:"blocked",availability:"login_required",reason:"login_required",
    canProbe:false,canPrepare:false,canExecute:false,nextAction:"restore_login"};
  f.ui.render(f.root,r);assert.equal(f.root.hidden,false);
  assert.match(f.root.textContent,/登录已失效.*恢复原账号登录/);
  assert.equal(find(f.root,"button","只读核对原事项").hidden,true);
  assert.equal(find(f.root,"button","准备全新办理表单").hidden,true);
});

test("assessment expiry updates both visible panels without a user click and releases timer resources", async () => {
  const scheduled=new Map();let next=0;
  const timers={setTimeout:(fn,ms)=>{scheduled.set(++next,{fn,ms});return next;},clearTimeout:id=>scheduled.delete(id)};
  const f=fixture({timers});const detail=f.document.createElement("section");f.ui.render(detail,outcome());
  await click(find(f.root,"button","只读核对原事项"));
  const ack=find(f.root,"input");ack.checked=true;ack.events.change();
  assert.equal(find(detail,"button","准备全新办理表单").disabled,false);
  assert.equal(scheduled.size,1);
  f.setClock(T0+120001);const timer=[...scheduled.values()][0];scheduled.clear();timer.fn();
  for (const root of [f.root,detail]) {
    assert.match(root.textContent,/核对已过期/);assert.equal(find(root,"input").checked,false);
    assert.equal(find(root,"button","准备全新办理表单").disabled,true);
  }
  assert.equal(scheduled.size,0);
  f.setClock(T0);await click(find(f.root,"button","只读核对原事项"));assert.equal(scheduled.size,1);
  f.ui.detach(f.root);assert.equal(scheduled.size,1);f.ui.detach(detail);assert.equal(scheduled.size,0);
  f.ui.render(f.root,outcome());assert.equal(scheduled.size,1);f.ui.reset();assert.equal(scheduled.size,0);
});

test("discarded account models cannot accept a delayed assessment response or schedule expiry", async () => {
  let resolve;const scheduled=[];
  const f=fixture({probeRecovery:()=>new Promise(done=>{resolve=done;}),
    timers:{setTimeout:(fn,ms)=>{scheduled.push([fn,ms]);return 1;},clearTimeout(){}}});
  find(f.root,"button","只读核对原事项").events.click();
  f.ui.reset();f.state.oaPendingRecovery.clear();
  const next=f.document.createElement("section");f.ui.render(next,outcome());
  resolve(assessment());await flush();
  assert.equal(find(next,"button","准备全新办理表单").hidden,true);assert.deepEqual(scheduled,[]);
});

test("an expired persisted complete assessment is labelled historical and allows a new read only", () => {
  const f=fixture(),r=outcome();
  r.oaPendingRecovery={originalTaskId:ORIGINAL,status:"verified_complete",stale:true,
    observedAt:"2026-10-23T09:55:00Z",expiresAt:"2026-10-23T09:57:00Z",canProbe:true,
    canPrepare:false,canExecute:false,availability:"can_probe",nextAction:"probe"};
  f.ui.render(f.root,r);
  assert.match(f.root.textContent,/上次核对时.*该次核对已过期.*历史结论.*重新核对/);
  assert.doesNotMatch(f.root.textContent,/OA 当前记录|无需重新办理/);
  assert.equal(find(f.root,"button","只读核对原事项").hidden,false);
  assert.equal(find(f.root,"button","准备全新办理表单").hidden,true);
});

test("a prior recovery success remains a dated historical result after the current OA identity changes", () => {
  const f=fixture(),r=outcome();r.oaPendingRecoveryAvailable=false;
  r.oaPendingRecovery={originalTaskId:ORIGINAL,status:"recovery_succeeded",stale:true,reason:"identity_changed",
    observedAt:"2026-10-23T09:55:00Z",availability:"blocked",canProbe:false,canPrepare:false,canExecute:false,
    nextAction:"open_recovery",recoveryTaskId:CHILD,recoveryTaskStatus:"succeeded"};
  f.ui.render(f.root,r);
  assert.match(f.root.textContent,/此前后续办理已核验完成.*核验时间.*历史完成证据.*身份与原办理不一致/);
  assert.equal(find(f.root,"button","只读核对原事项").hidden,true);
  assert.equal(find(f.root,"button","准备全新办理表单").hidden,true);
  assert.equal(find(f.root,"button","查看办理结果").hidden,false);
});
