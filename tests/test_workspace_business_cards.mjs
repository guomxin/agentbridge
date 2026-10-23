import test from "node:test";
import assert from "node:assert/strict";
import { createTaskCards } from "../agentbridge/workspace/static/workspace_cards.mjs";
import { reconcileChildren } from "../agentbridge/workspace/static/workspace_dom.mjs";
import { createDocument, descendants } from "./workspace_dom_fixture.mjs";

function fixture(options = {}) {
  const document = createDocument(), chat = document.createElement("main"), detail = document.createElement("main");
  const state = {taskCards:new Map(), taskCardMeta:new Map()};
  const cards = createTaskCards({document, state, $: selector => selector === "#task-detail" ? detail : chat,
    setTimelineNode() {}, appendArtifactList() {}, scrollChat() {},
    renderChatTimeline() { reconcileChildren(chat, [...state.taskCards.values()]); }, ...options});
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

test("trusted single confirmation retains one form and one recorded decision while legacy steps stay distinct", () => {
  const f = fixture();
  const input = {...interaction("single"), confirmation: {mode: "single", state: "pending"},
    message: "提交意见后还需在独立授权卡中核对精确事项"};
  const r = result(1, "waiting_user", [input]);
  f.upsertTaskCard(r);
  assert.match(f.chat.textContent, /一次确认提交/);
  assert.doesNotMatch(f.chat.textContent, /独立授权卡/);
  r.task.version = 2; r.task.status = "running";
  input.state = "completed"; input.confirmation.state = "recorded";
  r.interactions.push({...interaction("authorization", "execution_authorization", "completed"),
    confirmation: {mode: "single", state: "recorded", sourceInteractionId: "single"}});
  f.upsertTaskCard(r);
  const history = findClass(f.chat, "interaction-history");
  assert.match(history.textContent, /办理记录（1）.*一次确认已记录.*无需再次授权/);
  assert.doesNotMatch(history.textContent, /字段已提交|后续授权卡|已授权/);
  assert.equal(links(f.chat).length, 0);
  f.renderTaskDetail(r);
  assert.match(f.detail.textContent, /一次确认已记录/);
  assert.doesNotMatch(f.detail.textContent, /后续授权卡/);

  delete input.confirmation; delete r.interactions[1].confirmation;
  r.task.version = 3;
  f.upsertTaskCard(r);
  assert.match(history.textContent, /办理记录（2）.*字段已提交.*后续授权卡.*已授权/);
});

test("unknown outcome shows exact business title and bounded evidence with a closed read-only query action", async () => {
  const calls = [], continued = [], views = [];
  let finish;
  const f = fixture({makeFeedbackRequestKey: () => "original-query-key", continueTask: (...args) => continued.push(args),
    switchView: async name => views.push(name), refreshQuery: payload => {
      calls.push(payload); return new Promise(resolve => { finish = resolve; });
    }});
  const r = result(1, "outcome_unknown", []);
  r.businessTitle = "2026年第40、41周人工智能研发中心效能数据";
  r.businessExecution = {schemaVersion: "agentbridge.business-execution.v1", phase: "outcome_unknown",
    reason: "still_pending", lastObservedAt: "2026-10-23T01:02:03Z", submissionState: "invoked", readOnlyQuery: "oa_pending"};
  f.upsertTaskCard(r);
  assert.match(f.chat.textContent, /第40、41周.*该事项仍在 OA 待办中.*不要重复提交/s);
  f.renderTaskDetail(r);
  assert.match(f.detail.textContent, /最近核验/);
  assert.match(f.detail.textContent, /已调用原始提交入口，仍须核验业务结果/);
  const button = descendants(f.detail).find(node => node.tag === "button" && node.textContent === "只读查询 OA 待办");
  const pending = button.events.click();
  assert.deepEqual(calls, [{requestKey: "original-query-key"}]);
  assert.equal(button.disabled, true);
  f.renderTaskDetail(r);
  assert.equal(button.disabled, true);
  await button.events.click();
  assert.equal(calls.length, 1);
  finish({taskId: "readonly-query"}); await pending;
  assert.equal(continued.length, 0);
  assert.deepEqual(views, ["chat"]);
  assert.match(f.detail.textContent, /查询待办不等于确认办理成功/);
  assert.equal(r.task.status, "outcome_unknown");
  r.task.version++;
  r.task.summary = {batch: {state: "outcome_unknown", totalCount: 3, succeededCount: 1, failedCount: 1,
    currentOrdinal: 2, currentItem: {ordinal: 2, display: {title: "效能数据"}}}};
  f.upsertTaskCard(r);
  assert.match(findClass(f.chat, "application-card-copy").textContent,
    /成功 1 条.*结果未知 1 条.*未处理 1 条.*该事项仍在 OA 待办中/);
});

test("an uncertain read-only query restores its original request key and never calls conversational continuation", async () => {
  const calls = []; let keys = 0;
  const f = fixture({makeFeedbackRequestKey: () => `query-${++keys}`, continueTask: () => assert.fail("unknown resumed"),
    refreshQuery: async payload => { calls.push(payload); throw new Error("PRIVATE_GATEWAY_ERROR"); }});
  const r = result(1, "outcome_unknown", []);
  r.businessExecution = {schemaVersion: "agentbridge.business-execution.v1", phase: "outcome_unknown", readOnlyQuery: "oa_pending"};
  f.renderTaskDetail(r);
  const button = descendants(f.detail).find(node => node.tag === "button" && node.textContent === "只读查询 OA 待办");
  await button.events.click(); await button.events.click();
  assert.deepEqual(calls, [{requestKey: "query-1"}, {requestKey: "query-1"}]);
  assert.match(f.detail.textContent, /重试恢复本次查询/);
  assert.doesNotMatch(f.detail.textContent, /PRIVATE_GATEWAY_ERROR/);
  delete r.businessExecution; r.task.version++;
  f.renderTaskDetail(r);
  assert.equal(button.hidden, true);
  assert.equal(descendants(f.detail).find(node => node.tag === "button" && node.textContent === "请到业务系统核对").disabled, true);
  assert.equal(button.disabled, true);
});

test("not-ready, failed and verified cards preserve their different submission meanings", () => {
  const f = fixture();
  const r = result(1, "failed", []);
  r.businessExecution = {schemaVersion: "agentbridge.business-execution.v1", phase: "not_submitted",
    reason: "body_loading", submissionState: "not_invoked"};
  f.upsertTaskCard(r); f.renderTaskDetail(r);
  assert.match(f.chat.textContent, /正文尚未加载就绪，本次尚未提交/);
  assert.match(f.detail.textContent, /未调用提交入口/);
  r.task.version++; r.businessExecution.reason = "page_loading";
  f.upsertTaskCard(r);
  assert.match(f.chat.textContent, /办理页面尚在加载，本次尚未提交/);
  r.task.version++; r.businessExecution.reason = "pre_submit_validation_failed";
  f.upsertTaskCard(r);
  assert.match(f.chat.textContent, /提交前核对未通过，本次尚未提交，授权未消耗/);
  r.task.version++; r.businessExecution = {...r.businessExecution, phase: "failed", reason: undefined};
  f.upsertTaskCard(r);
  assert.doesNotMatch(findClass(f.chat, "application-card-copy").textContent, /尚未提交|完成核验/);
  r.task.version++; r.task.status = "succeeded"; r.businessExecution = {...r.businessExecution, phase: "verified"};
  f.upsertTaskCard(r);
  assert.match(f.chat.textContent, /已取得本次业务操作的完成核验证据/);
});

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
  assert.equal(f.document.activeElement.textContent, "查看核验详情");
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

const feedbackResult = (version=1) => ({...result(version, 'succeeded', []),
  skill: {name: '工作日报助手', version: '1.2.3', profile: 'single'}, feedback: {available: true, receipt: null}});
const feedbackNodes = root => {
  const panel = findClass(root, 'task-feedback');
  return {panel, form: findClass(panel, 'task-feedback-form'),
    textarea: descendants(panel).find(node => node.tag === 'textarea'),
    choices: descendants(panel).filter(node => node.tag === 'input'),
    button: descendants(panel).find(node => node.tag === 'button'),
    status: findClass(panel, 'task-feedback-status'), receipt: findClass(panel, 'task-feedback-receipt')};
};
const choose = (view, value) => { const input = view.choices.find(node => node.value === value); input.checked = true; input.events.change(); };
const comment = (view, value) => { view.textarea.value = value; view.textarea.events.input(); };
const submit = view => view.form.events.submit({preventDefault() {}});
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; };

test('feedback stays folded and optional, shows original assistant binding, and preserves unsaved input/focus on refresh', () => {
  const f = fixture({submitTaskFeedback: async () => {throw new Error('must not auto-submit');}}), r = feedbackResult();
  f.upsertTaskCard(r);
  const view = feedbackNodes(f.chat), textarea = view.textarea;
  assert.equal(view.panel.hidden, false); assert.equal(view.panel.open, false);
  assert.match(view.panel.textContent, /工作日报助手 · 版本 1\.2\.3 · 单项办理/);
  assert.doesNotMatch(view.panel.textContent, /（single）/);
  assert.equal(view.textarea.maxLength, 1000); assert.equal(view.button.disabled, true);
  view.panel.open = true; view.panel.events.toggle(); choose(view, 'incorrect'); comment(view, '本页未保存的说明'); textarea.focus();
  f.upsertTaskCard(feedbackResult(2));
  assert.equal(feedbackNodes(f.chat).textarea, textarea);
  assert.equal(f.document.activeElement, textarea);
  assert.equal(textarea.value, '本页未保存的说明'); assert.equal(view.panel.open, true);
  f.renderTaskDetail(feedbackResult(2));
  const detail = feedbackNodes(f.detail);
  assert.equal(detail.textarea.value, textarea.value); assert.equal(detail.panel.open, true);
  assert.equal(detail.choices.find(node => node.value === 'incorrect').checked, true);
  comment(detail, '详情里的修改'); assert.equal(textarea.value, '详情里的修改');
  f.renderTaskDetail(feedbackResult(3));
  assert.equal(feedbackNodes(f.detail).textarea, detail.textarea);
});

test('only terminal eligible tasks expose feedback and legacy tasks remain unchanged', () => {
  const f = fixture({submitTaskFeedback() {}}), r = feedbackResult();
  for (const [status, eligible, skill, expected] of [
    ['waiting_user',true,r.skill,false], ['running',true,r.skill,false], ['succeeded',false,r.skill,false],
    ['failed',true,null,false], ['outcome_unknown',true,r.skill,true], ['partially_succeeded',true,r.skill,true],
  ]) {
    r.task = {...r.task, status, version: r.task.version + 1}; r.skill = skill; r.feedback = {available: eligible, receipt: null};
    f.upsertTaskCard(r); assert.equal(!feedbackNodes(f.chat).panel.hidden, expected);
  }
});

test('one feedback request disables controls, lost response retries the exact payload and key, then updates card and detail receipt', async () => {
  const first = deferred(); const calls = []; let keys = 0;
  const receipt = {status:'recorded', feedback_id:'feedback-1', rating:'useful', comment:'保留评价', version:'1.2.3', profile:'single'};
  const f = fixture({makeFeedbackRequestKey: () => `key-${++keys}`,
    submitTaskFeedback: async (taskId, payload) => { calls.push({taskId,payload}); if (calls.length === 1) return first.promise; return {feedback:{available:false,receipt}}; }});
  f.upsertTaskCard(feedbackResult()); f.renderTaskDetail(feedbackResult());
  const view = feedbackNodes(f.chat); choose(view,'useful'); comment(view,'保留评价');
  const waiting = submit(view);
  assert.equal(view.button.disabled,true); assert.equal(view.textarea.disabled,true);
  assert.ok(view.choices.every(input => input.disabled)); assert.equal(feedbackNodes(f.detail).textarea.disabled,true);
  await submit(view); assert.equal(calls.length,1);
  first.reject(new TypeError('连接中断')); await waiting;
  assert.equal(view.textarea.value,'保留评价'); assert.equal(view.textarea.disabled,true);
  assert.equal(view.button.disabled,false); assert.equal(view.button.textContent,'重试保存评价');
  await submit(view);
  assert.deepEqual(calls[0],calls[1]); assert.equal(keys,1);
  assert.deepEqual(Object.keys(calls[1].payload).sort(),['comment','rating','requestKey']);
  for (const root of [f.chat,f.detail]) {
    const saved = feedbackNodes(root); assert.equal(saved.form.hidden,true); assert.equal(saved.receipt.hidden,false);
    assert.match(saved.receipt.textContent,/已保存评价 · 有帮助.*保留评价/);
  }
  f.upsertTaskCard(feedbackResult(2));
  assert.equal(feedbackNodes(f.chat).form.hidden,true,'late null receipt cannot reopen saved feedback');
  await submit(view); assert.equal(calls.length,2);
});

test('saved feedback is restored from GET after reload, and a conflict restores the server receipt safely', async () => {
  const receipt = {status:'recorded',feedback_id:'existing',rating:'not_applicable',comment:'<script>只是文字</script>',version:'1.2.3',profile:'single'};
  const saved = {...feedbackResult(),feedback:{available:false,receipt}};
  const reloaded = fixture({submitTaskFeedback() {}}); reloaded.upsertTaskCard(saved);
  assert.match(feedbackNodes(reloaded.chat).receipt.textContent,/不适用.*<script>只是文字<\/script>/);
  assert.ok(!descendants(reloaded.chat).some(node => node.tag === 'script'));
  assert.equal(feedbackNodes(reloaded.chat).panel.open,false);
  const f = fixture({makeFeedbackRequestKey: () => 'new-key', submitTaskFeedback: async () => {
    throw Object.assign(new Error('已有评价'),{status:409,code:'TASK_FEEDBACK_CONFLICT',feedback:{available:false,receipt}});
  }});
  f.upsertTaskCard(feedbackResult()); const view = feedbackNodes(f.chat); choose(view,'useful'); comment(view,'本页尚未保存'); await submit(view);
  assert.equal(view.form.hidden,true); assert.match(view.receipt.textContent,/不适用/);
  assert.equal(view.status.textContent,'此任务已有另一份评价，以下为已保存内容。');
  assert.doesNotMatch(view.receipt.textContent,/本页尚未保存/);
});

test('feedback validates comment length locally and permits correction after explicit validation rejection', async () => {
  let calls = 0, keys = 0;
  const f = fixture({makeFeedbackRequestKey: () => `key-${++keys}`, submitTaskFeedback: async () => {
    calls++; throw Object.assign(new Error('说明无效'),{status:422});
  }});
  f.upsertTaskCard(feedbackResult()); const view = feedbackNodes(f.chat); choose(view,'incorrect');
  comment(view,'a'.repeat(1001)); await submit(view); assert.equal(calls,0); assert.equal(view.button.disabled,true);
  comment(view,'可以更正'); await submit(view); assert.equal(calls,1); assert.equal(view.textarea.disabled,false);
  comment(view,'已更正'); await submit(view); assert.equal(keys,2);
});

test('trusted business view metadata yields precise entry labels while mismatches retain generic input wording', () => {
  const f = fixture(); let version = 0;
  const profiles = [
    ['taihua.work_log.create','work_log','填写工作日志'],
    ['oa.meeting_room.application.create','meeting_room','选择会议室'],
    ['oa.leave.approval','approval','核对请假材料'],
    ['oa.meeting.create','meeting_room','安排会议'],
    ['oa.meeting_room.application.cancel','form','核对撤销申请'],
    ['oa.workflow.revoke','form','核对流程撤销'],
    ['oa.business_trip.save_draft','form','填写出差草稿'],
    ['oa.business_trip.submit','form','填写出差申请'],
    ['oa.leave.save_draft','form','填写请假草稿'],
    ['oa.leave.submit','form','填写请假申请'],
    ['oa.missed_punch.save_draft','form','填写补签草稿'],
    ['smartlight.alarm.remark.update','form','编辑告警备注'],
    ['oa.missed_punch.approval','approval','核对补签材料'],
    ['oa.efficiency_data.approval','approval','核对效能数据'],
    ['oa.travel_expense.approval','approval','核对差旅报销'],
    ['oa.business_trip.approval','approval','核对出差材料'],
    ['oa.labor_contract_renewal.approval','approval','核对合同续签'],
    ['oa.intellectual_property_declaration.approval','approval','核对知识产权材料'],
    ['oa.overtime.approval','approval','核对加班材料'],
    ['oa.resignation.approval','approval','核对离职材料'],
    ['oa.work_handover.approval','approval','核对交接材料'],
    ['oa.flight_application.approval','approval','核对乘机申请'],
    ['oa.standard_collaboration.approval','approval','核对协同事项'],
    ['oa.attendance_confirmation','approval','核对月度考勤'],
    ['oa.weekly_report.acknowledgement','approval','核对周报'],
  ];
  for (const [profileId,viewKind,expected] of profiles) {
    const view = {schemaVersion:'agentbridge.business-view.v1',profileVersion:'1',profileId,viewKind,
      entryLabel:'不可信文案',submitLabel:'确认并执行'};
    for (const [metadata,label] of [[view,expected],[{...view,profileVersion:'2'},'填写信息'],
      [{...view,profileId:'unknown'},'填写信息'],[{...view,profileId:'__proto__',viewKind:undefined},'填写信息'],
      [{...view,viewKind:'other'},'填写信息'],
      [{...view,schemaVersion:'other'},'填写信息'],[null,'填写信息']]) {
      const step = interaction('input'); step.display = {businessView:metadata};
      const r = result(++version,'waiting_user',[step]); f.upsertTaskCard(r); f.renderTaskDetail(r);
      assert.equal(links(f.chat)[0].textContent,label); assert.equal(links(f.detail)[0].textContent,label);
    }
    const auth = interaction('auth','execution_authorization'); auth.display = {businessView:view};
    f.upsertTaskCard(result(++version,'waiting_user',[auth]));
    assert.equal(links(f.chat)[0].textContent,'核对并确认');
  }
});

test("the primary action opens an existing trusted single-confirmation form without model continuation", async () => {
  const f = fixture({continueTask:() => assert.fail("form must not invoke model continuation")});
  const r = result(1, "waiting_user", [{...interaction("current"), confirmation:{mode:"single", state:"pending"}}]);
  f.renderTaskDetail(r);
  const action = links(f.detail)[0];
  assert.equal(action.textContent, "继续填写并确认");
  assert.equal(action.href, "https://cards.example.test/current");
  assert.equal(action.target, "_blank");
  assert.equal(links(f.detail).length, 1);
  for (const status of ["running", "succeeded", "failed", "expired", "canceled", "outcome_unknown"]) {
    r.task = {...r.task, version:r.task.version+1, status}; r.interactions[0].state="completed";
    f.renderTaskDetail(r);
    assert.equal(links(f.detail).length, 0);
    assert.doesNotMatch(f.detail.textContent, /继续任务/);
  }
});

test("the unknown primary action probes the exact original task while all-pending query stays secondary", async () => {
  const probes=[], queries=[], f=fixture({continueTask:() => assert.fail("unknown must not continue"),
    probeRecovery:async (...args) => {probes.push(args);return {status:"blocked",canPrepare:false,canExecute:false};},
    prepareRecovery:() => assert.fail("probe cannot prepare"), refreshQuery:payload => queries.push(payload)});
  const r=result(1,"outcome_unknown",[]);
  r.businessExecution={schemaVersion:"agentbridge.business-execution.v1",phase:"outcome_unknown",readOnlyQuery:"oa_pending"};
  f.renderTaskDetail(r);
  const primary=descendants(f.detail).find(node=>node.tag==="button" && node.textContent==="核对本事项");
  assert.equal(primary.className,"primary"); await primary.events.click();
  assert.equal(probes.length,1);assert.equal(probes[0][0],"task");assert.deepEqual(queries,[]);
  const query=descendants(f.detail).find(node=>node.tag==="button" && node.textContent==="只读查询 OA 待办");
  assert.equal(query.className,"secondary");
});

test("durable recovery completion becomes the current conclusion while the original unknown remains collapsed history", async () => {
  const opened=[], child="22222222-2222-2222-2222-222222222222";
  const f=fixture({switchView:async()=>{},loadTaskDetail:id=>opened.push(id)}), r=result(7,"outcome_unknown",[]);
  r.businessExecution={schemaVersion:"agentbridge.business-execution.v1",phase:"outcome_unknown",submissionState:"scheduled",readOnlyQuery:"oa_pending"};
  r.oaPendingRecovery={originalTaskId:"task",status:"recovery_succeeded",availability:"can_probe",canProbe:false,
    canPrepare:false,canExecute:false,nextAction:"open_recovery",recoveryTaskId:child,recoveryTaskStatus:"succeeded"};
  f.upsertTaskCard(r);f.renderTaskDetail(r);
  const card=f.chat.children[0];
  assert.match(findClass(card,"application-card-status").textContent,/后续办理已完成/);
  assert.match(card.className,/succeeded/);
  assert.doesNotMatch(findClass(f.detail,"detail-grid").textContent,/结果待核对|尚不能证明请求发出/);
  const history=findClass(f.detail,"execution-history");
  assert.equal(history.open,false);assert.match(history.textContent,/首次尝试：结果待核对.*提交入口已排队/);
  const action=descendants(f.detail).find(node=>node.tag==="button" && node.textContent==="查看办理结果");
  await action.events.click();assert.deepEqual(opened,[child]);
  assert.equal(r.task.status,"outcome_unknown");
});

test("verified completion moves prior submission observation below the current result", () => {
  const f=fixture(),r=result(1,"succeeded",[]);
  r.businessExecution={schemaVersion:"agentbridge.business-execution.v1",phase:"verified",submissionState:"scheduled"};
  f.renderTaskDetail(r);
  assert.doesNotMatch(findClass(f.detail,"detail-grid").textContent,/尚不能证明请求发出/);
  assert.match(findClass(f.detail,"execution-history").textContent,/完成核验前的提交观察.*最终结论以当前业务核验结果为准/);
  assert.match(findClass(f.detail,"application-card-copy").textContent,/已取得本次业务操作的完成核验证据/);
});

test("actual execution stages show durable total and stage elapsed time, while completion retires the timer", async () => {
  const {executionWaitMessage}=await import("../agentbridge/workspace/static/workspace_results.mjs");
  const started=Date.parse("2026-10-23T12:00:00Z"),f=fixture();
  const r=result(1,"running",[]);
  r.businessExecution={schemaVersion:"agentbridge.business-execution.v1",phase:"verifying_result",actualPhase:"verifying_result",
    operationId:"commit-current",operationStartedAt:new Date(started).toISOString(),
    phaseStartedAt:new Date(started+5000).toISOString(),phaseObservedAt:new Date(started+5000).toISOString()};
  assert.match(executionWaitMessage(r,started+12000),/本次执行已等待 12 秒.*当前阶段 7 秒.*可以离开此页/);
  f.upsertTaskCard(r);f.renderTaskDetail(r);
  assert.match(findClass(f.detail,"detail-grid").textContent,/核对 OA 结果/);
  assert.doesNotMatch(f.detail.textContent,/请求已发出|已完成|%/);
  for(const status of ["succeeded","failed","outcome_unknown","canceled"]) {
    r.task={...r.task,status,version:r.task.version+1};
    r.businessExecution={...r.businessExecution,phase:status==="succeeded"?"verified":status==="outcome_unknown"?"outcome_unknown":"failed"};
    assert.equal(executionWaitMessage(r,started+24000),"");f.renderTaskDetail(r);
    assert.equal(findClass(f.detail,"business-wait").hidden,true);
  }
});
