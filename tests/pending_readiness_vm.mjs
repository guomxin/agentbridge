import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const scripts = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = fs.readFileSync(new URL('./fixtures/seeyon_pending_readiness.html', import.meta.url), 'utf8');
const originalEntry = html.match(/<script id="original-submit-entry">([\s\S]*?)<\/script>/)[1];
const target = {affair_id: 'fixture-affair', summary_id: 'fixture-summary', process_id: 'fixture-process',
  template_id: '', form_app_id: '', form_record_id: '', node_policy: 'approve', node_policy_name: '审批'};

function create() {
  class Field {
    constructor(value = '') { this.value = value; }
    dispatchEvent() {}
    getAttribute(name) { return name === 'code' ? 'agree' : null; }
  }
  const elements = {affairId: new Field(target.affair_id), summaryId: new Field(target.summary_id),
    processId: new Field(target.process_id), content_deal_comment: new Field(),
    hidAttitudeCode: new Field(), hidAttitude: new Field(), nodeattitude: new Field()};
  const radio = new Field('agree');
  const state = {_hasExecuteContentLoad: false, nodePolicy: 'approve', nodePolicyName: '审批',
    target, callbacks: [], entryCalls: 0, writes: 0, secretChecks: 0, secretValid: true,
    contentLoaded: true, Event: class Event {}, URLSearchParams,
    location: {pathname: '/seeyon/collaboration/collaboration.do', search: ''},
    document: {readyState: 'complete',
      querySelector(selector) { return selector.startsWith('#') ? elements[selector.slice(1)] || null : null; },
      querySelectorAll() { return [radio]; },
      getElementById(id) { return id === 'zwIframe' ? {contentWindow: {contentAPI: {isContentLoaded: () => state.contentLoaded}}} : elements[id] || null; }},
    setTimeout(fn) { state.callbacks.push(fn); },
    checkSecret() { state.secretChecks += 1; return state.secretValid; },
    mainbody_callBack_failed() {}, comDealSubmit() { state.writes += 1; },
  };
  state.window = state;
  const context = vm.createContext(state);
  vm.runInContext(originalEntry, context, {timeout: 1000});
  const nativeEntry = state.submitClickFunc;
  state.submitClickFunc = function () { state.entryCalls += 1; return nativeEntry.call(state); };
  const ready = () => vm.runInContext(`(${scripts.readiness})(target)`, context, {timeout: 1000});
  const schedule = () => vm.runInContext(`(${scripts.action})`, context, {timeout: 1000})(
    {affair_id: target.affair_id, opinion: '合成同意意见', attitude_code: 'agree', identity: target});
  return {state, elements, ready, schedule};
}

const result = {};
{
  const test = create();
  assert.equal(test.ready().ready, false);
  test.state.submitClickFunc();
  assert.equal(test.state.writes, 0); // Reproduce the historical silent return.
  assert.throws(test.schedule, /not ready/);
  assert.equal(test.elements.content_deal_comment.value, '');
  test.state._hasExecuteContentLoad = true;
  assert.equal(test.ready().ready, true);
  test.schedule();
  test.state.callbacks.forEach(fn => fn());
  assert.equal(test.state.writes, 1);
  assert.equal(test.state.secretChecks, 1);
  assert.equal(test.elements.content_deal_comment.value, '合成同意意见');
  result.lateBody = {writesBeforeReady: 0, writesAfterReady: test.state.writes, checks: test.state.secretChecks};
}
for (const mode of ['reload', 'identity']) {
  const test = create();
  test.state._hasExecuteContentLoad = true;
  test.schedule();
  if (mode === 'reload') test.state._hasExecuteContentLoad = false;
  else test.elements.processId.value = 'changed-process';
  test.state.callbacks.forEach(fn => fn());
  assert.equal(test.state.writes, 0);
  assert.equal(test.state.entryCalls, 0);
  assert.equal(test.state.__agentbridgePendingDispatch.state, 'not_invoked');
  result[mode] = {entryCalls: 0, writes: 0, dispatch: 'not_invoked'};
}
{
  const test = create();
  test.state._hasExecuteContentLoad = true;
  test.state.contentLoaded = false;
  assert.equal(test.ready().ready, false);
  test.state.contentLoaded = true;
  test.state.secretValid = false;
  test.schedule();
  test.state.callbacks.forEach(fn => fn());
  assert.equal(test.state.secretChecks, 1);
  assert.equal(test.state.writes, 0);
  result.originalValidation = {checks: 1, writes: 0};
}
process.stdout.write(JSON.stringify(result));
