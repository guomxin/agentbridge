import test from 'node:test';
import assert from 'node:assert/strict';
import { createViewScope, createTimerSlot } from '../bscli/admin/static/admin_lifecycle.mjs';
import { createAdminApi, csrfToken, restoreAdminSession } from '../bscli/admin/static/admin_request.mjs';
import { createModalController } from '../bscli/admin/static/admin_forms.mjs';
import { badge, sessionStateBadge, table, filteredTable, skillReviewQualityHtml, escapeHtml, fmtBytes, fmtDuration } from '../bscli/admin/static/admin_presentation.mjs';

const deferred = () => { let resolve, reject; const promise = new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; };
const response = (value, status=200) => ({ok: status < 400, status, json: async()=>value});
function client(fetchImpl, extra={}) {
  const readScope=createViewScope(), accountScope=createViewScope();
  return {readScope,accountScope,api:createAdminApi({fetchImpl,readScope,accountScope,cookieText:()=> 'x=1; agentbridge_admin_csrf=a%2Bb',...extra})};
}
function dialog() {
  const nodes=new Map();
  const select=key=>{
    if(!nodes.has(key)) nodes.set(key,{textContent:'',innerHTML:'',disabled:false,classList:{values:new Set(),toggle(k,on){if(on)this.values.add(k);else this.values.delete(k);}}});
    return nodes.get(key);
  };
  const modal={open:false,showModal(){this.open=true;},close(){this.open=false;}};
  const control=createModalController({modal,select,formData:x=>x});
  const event={preventDefault(){},currentTarget:{value:'synthetic'}};
  return {control,select,modal,event};
}

test('view replacement invalidates tickets and aborts only the previous controller',()=>{
  const scope=createViewScope();const first=scope.capture();const next=scope.begin();
  assert.equal(first.signal.aborted,true);assert.equal(first.current(),false);assert.equal(next.current(),true);
  scope.invalidate();assert.equal(next.current(),false);assert.equal(scope.capture().current(),true);
});
test('stale GET is rejected even when fetch ignores cancellation',async()=>{
  const pending=deferred();const {api,readScope}=client(()=>pending.promise);
  const run=api('/api/users');readScope.begin();pending.resolve(response({items:['old']}));
  await assert.rejects(run,{name:'AbortError'});
});
test('stale 401 cannot sign out the new view or account',async()=>{
  const pending=deferred();const {api,accountScope}=client(()=>pending.promise);
  const run=api('/api/session');accountScope.invalidate();pending.resolve(response({error:{message:'old session'}},401));
  await assert.rejects(run,{name:'AbortError'});
});
test('late network rejection after view replacement is classified as cancellation',async()=>{
  const pending=deferred();const {api,readScope}=client(()=>pending.promise);
  const run=api('/api/users');readScope.invalidate();pending.reject(new Error('old network'));
  await assert.rejects(run,{name:'AbortError'});
});
test('JSON parsing completion is also checked for stale responses',async()=>{
  const body=deferred();const {api,readScope}=client(async()=>({ok:true,json:()=>body.promise}));
  const run=api('/api/users');await Promise.resolve();readScope.invalidate();body.resolve({items:['old']});
  await assert.rejects(run,{name:'AbortError'});
});
test('write executes once, keeps CSRF, and is not canceled by navigation',async()=>{
  let calls=0,options;const pending=deferred();const {api,readScope}=client((_path,o)=>{calls++;options=o;return pending.promise;});
  const run=api('/api/skill-reviews',{method:'POST',body:'{}',headers:{'X-Test':'value'}});readScope.begin();
  assert.equal(options.signal,undefined);assert.equal(options.credentials,'same-origin');
  assert.equal(options.headers['X-AgentBridge-CSRF'],'a+b');assert.equal(options.headers['X-Test'],'value');
  pending.resolve(response({saved:true}));assert.deepEqual(await run,{saved:true});assert.equal(calls,1);
});
test('already submitted write cannot update a different signed-in account',async()=>{
  let calls=0;const pending=deferred();const {api,accountScope}=client(()=>{calls++;return pending.promise;});
  const run=api('/api/skill-reviews',{method:'POST',body:'{}'});accountScope.invalidate();pending.resolve(response({saved:true}));
  await assert.rejects(run,{name:'AbortError'});assert.equal(calls,1);
});
test('domain error shape and malformed JSON fallback remain compatible',async()=>{
  const denied=client(async()=>response({error:{message:'denied',code:'FORBIDDEN'}},403));
  await assert.rejects(denied.api('/api/users'),{message:'denied',code:'FORBIDDEN',status:403});
  const empty=client(async()=>({ok:false,status:502,json:async()=>{throw new SyntaxError('bad');}}));
  await assert.rejects(empty.api('/api/users'),{message:'请求失败 (502)',code:'REQUEST_FAILED',status:502});
  assert.equal(csrfToken('x=1'),'');
});
test('replaced and cleared toast timers cannot run even if callback was already queued',()=>{
  const callbacks=[],canceled=[];const timer=createTimerSlot({schedule:f=>{callbacks.push(f);return callbacks.length;},cancel:id=>canceled.push(id)});
  const seen=[];timer.replace(()=>seen.push('old'),1);timer.replace(()=>seen.push('new'),1);callbacks[0]();callbacks[1]();
  assert.deepEqual(seen,['new']);assert.deepEqual(canceled,[1]);
  timer.replace(()=>seen.push('logout'),1);timer.clear();callbacks[2]();assert.deepEqual(seen,['new']);
});
test('modal blocks duplicate submit while preserving current domain errors',async()=>{
  const {control,select,event}=dialog();const pending=deferred();let calls=0;
  control.open({title:'one',body:'',action:()=>{calls++;return pending.promise;}});
  const first=control.submit(event);await control.submit(event);assert.equal(calls,1);assert.equal(select('#modal-submit').disabled,true);
  pending.reject(new Error('domain'));await first;assert.equal(select('#modal-error').textContent,'domain');assert.equal(select('#modal-submit').disabled,false);
});
test('old modal completion cannot clear errors or unlock replacement submission',async()=>{
  const {control,select,event}=dialog();const first=deferred(),second=deferred();
  control.open({title:'one',body:'',action:()=>first.promise});const old=control.submit(event);
  control.open({title:'two',body:'',action:()=>second.promise});const next=control.submit(event);
  first.reject(new Error('old'));await old;assert.equal(select('#modal-title').textContent,'two');assert.equal(select('#modal-error').textContent,'');assert.equal(select('#modal-submit').disabled,true);
  second.resolve();await next;assert.equal(select('#modal-submit').disabled,false);
});
test('closing a form invalidates its write response before old action changes the UI',async()=>{
  const {control,event,select}=dialog();const pending=deferred();const {api}=client(()=>pending.promise,{submission:()=>control.captureSubmission()});let afterWrite=0;
  control.open({title:'old',body:'',action:async()=>{await api('/api/skill-reviews',{method:'POST',body:'{}'});afterWrite++;control.close();}});
  const run=control.submit(event);control.close();control.open({title:'new',body:'',action:()=>{}});pending.resolve(response({saved:true}));await run;
  assert.equal(afterWrite,0);assert.equal(select('#modal-title').textContent,'new');
});
test('native dialog cancel honors locked forms and clears normal actions',async()=>{
  const {control,event,modal}=dialog();let calls=0;
  control.open({title:'required',body:'',locked:true,action:()=>calls++});control.cancel(event);assert.equal(modal.open,true);
  control.open({title:'normal',body:'',action:()=>calls++});control.cancel(event);assert.equal(modal.open,false);await control.submit(event);assert.equal(calls,0);
});
test('status projections retain unknown and last-confirmed session distinctions',()=>{
  assert.match(badge('outcome_unknown'),/结果未知/);assert.match(sessionStateBadge({state:'active',session_state_basis:'last_confirmed'}),/上次确认有效/);
  assert.match(sessionStateBadge({state:'active',session_state_basis:'real_time'}),/有效/);
  assert.equal(fmtBytes(1024),'1.0 KB');assert.equal(fmtDuration(1000),'1.0 秒');
});
test('pure presentation escapes labels and preserves empty lists',()=>{
  assert.equal(escapeHtml(`<img x="'"> &`),'&lt;img x=&quot;&#39;&quot;&gt; &amp;');
  assert.match(badge('<img onerror=x>'),/&lt;img/);assert.match(table(['x'],[]),/暂无记录/);
  const html=filteredTable(['<script>'],['<tr></tr>'],'<img>', ['unknown']);
  assert.ok(!html.includes('<script>'));assert.ok(html.includes('&lt;img&gt;'));assert.match(html,/结果未知/);
});
test('skill review projection keeps manual and independent evidence separate',()=>{
  assert.match(skillReviewQualityHtml(null),/历史申请/);
  const base={diagnostics:{message:'<img>',checks:[]},diff:[],audience_diff:{added:[],removed:[]},evaluation:null};
  assert.match(skillReviewQualityHtml(base),/不会被标为独立评测通过/);assert.ok(!skillReviewQualityHtml(base).includes('<img>'));
  const text=skillReviewQualityHtml({...base,evaluation:{passed:true,limitations:'synthetic only',scores:{candidate:{passed:1,total:1}},rows:[]}});
  assert.match(text,/语义仍需人工复核/);assert.match(text,/synthetic only/);
});

for (const status of [200, 401]) test(`late initial session ${status} cannot clear a newer login`, async () => {
  const pending = deferred();
  const {api, accountScope} = client(() => pending.promise);
  const seen = [];
  const restore = restoreAdminSession({api, accountScope, showLogin:()=>seen.push('login'), showApp:()=>seen.push('app'), showPassword:()=>seen.push('password'), loadView:()=>seen.push('view')});
  accountScope.begin();
  pending.resolve(response({authenticated:false}, status));
  await restore;
  assert.deepEqual(seen, []);
});
test('session restoration handles unauthenticated and required password states', async () => {
  for (const authenticated of [false, true]) {
    const account = {must_change_password:true};
    const {api, accountScope} = client(async()=>response({authenticated,account}));
    const seen=[];
    await restoreAdminSession({api,accountScope,showLogin:()=>seen.push('login'),showApp:a=>{assert.equal(a,account);seen.push('app');},showPassword:()=>seen.push('password'),loadView:()=>seen.push('view')});
    assert.deepEqual(seen, authenticated ? ['app','password'] : ['login']);
  }
});
test('later modal intent rejects an older same-view response even when transport ignores abort', async () => {
  const old = deferred();
  const {api} = client(path=>path==='old'?old.promise:Promise.resolve(response({title:'new'})));
  const intent = createViewScope();
  const first = api('old',{signal:intent.begin().signal});
  const next = await api('new',{signal:intent.begin().signal});
  old.resolve(response({title:'old'}));
  await assert.rejects(first,{name:'AbortError'});
  assert.equal(next.title,'new');
});
