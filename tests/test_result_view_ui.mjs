import assert from 'node:assert/strict';
import test from 'node:test';
import { installResultView } from '../agentbridge/auth/static/result_view.mjs';

const flush = () => new Promise(resolve => setImmediate(resolve));
const task = '12345678-1234-1234-1234-123456789abc';
const opaque = 'a'.repeat(43);

function fixture(saved = null, {extraState = {}} = {}) {
  function node(tag = '') { return {tag, children: [], listeners: {}, textContent: '', hidden: false,
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    addEventListener(name, fn) { this.listeners[name] = fn; }, click() { return this.listeners.click?.(); }}; }
  const ids = Object.fromEntries(['result-groups','result-title','result-status','result-count','result-prepare','result-recover','result-resume'].map(x => [x, node()]));
  const document = {getElementById: id => ids[id], createElement: node};
  const events = {}, timers = new Map(), calls = [], navigations = [];
  let value = saved ? JSON.stringify(saved) : null, seq = 0;
  const window = {location: {origin: 'https://trusted.test', href: `https://trusted.test/results/${opaque}`, hash: '#tgWebAppPlatform=android', replace: u => navigations.push(u)},
    addEventListener: (k,v) => { events[k] = v; }, sessionStorage: {getItem: () => value, setItem: (_k,v) => { value = v; }},
    crypto: {randomUUID: () => 'request-1234567890'}};
  const state = {title:'OA 待办',groups:[{operationId:'op1', targets:[{targetKey:'b'.repeat(64),title:'月度考勤',enabled:true,workflowLabel:'考勤确认'},
    {targetKey:'c'.repeat(64),title:'未支持事项',enabled:false}]}],preparations:[], ...extraState};
  const fetch = (url, options) => new Promise((resolve, reject) => { calls.push({url,options,resolve: (v,ok=true) => resolve({ok,json:async()=>v}),reject}); });
  const controller = installResultView({document,window,fetch,initial:{path:`/results/${opaque}`,csrfToken:'proof',state},
    setTimeout: fn => {const id=++seq;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id)});
  function select() { const input = ids['result-groups'].children[0].children.find(x=>x.tag==='label').children[0]; input.checked=true; input.listeners.change(); }
  return {controller,ids,calls,navigations,timers,select,state,hide:()=>events.pagehide(),show:()=>events.pageshow({persisted:true}),saved:()=>JSON.parse(value)};
}

test('selection queues only the exact original keys; no business approval is sent', async () => {
  const f=fixture(); f.select(); const promise=f.ids['result-prepare'].click();
  assert.equal(f.calls.length,1); assert.match(f.calls[0].url,/\/prepare$/);
  assert.deepEqual(JSON.parse(f.calls[0].options.body),{operationId:'op1',targetKeys:['b'.repeat(64)],requestKey:'request-1234567890'});
  f.calls[0].resolve({taskId:task}); await flush();
  assert.match(f.calls[1].url,/\/state\?task=/); assert.equal(f.calls[1].options.method,'GET');
  f.calls[1].resolve({state:'waiting_user',terminal:false,interaction:{state:'pending',presentation:{url:`https://trusted.test/flow/input/${opaque}`}}});
  await promise; assert.deepEqual(f.navigations,[`https://trusted.test/flow/input/${opaque}#tgWebAppPlatform=android`]);
});

test('lost prepare receipt requires an explicit recovery with the identical request', async () => {
  const f=fixture(); f.select(); const first=f.ids['result-prepare'].click();
  const body=f.calls[0].options.body; f.calls[0].reject(new Error('offline')); await first;
  assert.equal(f.calls.length,1); assert.equal(f.ids['result-recover'].hidden,false);
  const second=f.ids['result-recover'].click(); assert.equal(f.calls[1].options.body,body);
  f.calls[1].resolve({taskId:task}); await flush();
  f.calls[2].resolve({state:'unknown',terminal:true,title:'结果未知，不得重放'}); await second;
  assert.deepEqual(f.navigations,[]); assert.equal(f.timers.size,0);
});

test('pagehide suppresses late POST navigation and BFCache restores with GET only', async () => {
  const f=fixture(); f.select(); const first=f.ids['result-prepare'].click(); f.hide();
  f.calls[0].resolve({taskId:task}); await first;
  assert.equal(f.calls.length,1); assert.deepEqual(f.navigations,[]);
  f.show(); assert.equal(f.calls[1].options.method,'GET');
  f.calls[1].resolve({groups:[],preparations:[{taskId:task}]}); await flush();
  assert.equal(f.calls.filter(c=>c.options.method==='POST').length,1);
});

test('saved task survives refresh and unknown never follows a forged continuation', async () => {
  const f=fixture({taskId:task}); assert.equal(f.calls[0].options.method,'GET');
  f.calls[0].resolve({terminal:true,state:'unknown',interaction:{state:'pending',presentation:{url:`https://trusted.test/flow/input/${opaque}`}}}); await flush();
  assert.deepEqual(f.navigations,[]); assert.equal(f.timers.size,0);
});

test('only same-origin exact trusted flow links are eligible for navigation', () => {
  const f=fixture();
  for (const url of [`https://evil.test/flow/input/${opaque}`,`https://user:pass@trusted.test/flow/input/${opaque}`,
    `/flow/input/${opaque}?approve=1`,`/flow/input/${opaque}#override`,`/input/${opaque}`,`javascript:alert(1)`]) {
    assert.equal(f.controller.safeAction({state:'pending',presentation:{url}}),null);
  }
  assert.equal(f.controller.safeAction({state:'completed',presentation:{url:`/flow/input/${opaque}`}}),null);
});

test('an outstanding frozen request disables choices and cannot be replaced by another preparation', async () => {
  const other = '22345678-1234-1234-1234-123456789abc';
  const f = fixture(null, {extraState:{preparations:[{taskId:other}]}});
  f.select(); const pending = f.ids['result-prepare'].click();
  const frozen = f.saved();
  const choices = f.ids['result-groups'].children[0].children.filter(node=>node.tag==='label').map(node=>node.children[0]);
  assert.ok(choices.every(node=>node.disabled));
  assert.equal(f.ids['result-resume'].children[0].disabled,true);
  f.ids['result-resume'].children[0].click();
  assert.deepEqual(f.saved(), frozen);
  assert.equal(f.calls.length, 1);
  f.calls[0].reject(new Error('reply lost')); await pending;
  f.ids['result-resume'].children[0].click();
  assert.deepEqual(f.saved(), frozen);
  assert.equal(f.calls.length, 1);
  assert.equal(f.ids['result-recover'].hidden,false);
});

test('a late old read failure after BFCache restoration cannot overwrite status or start another poll', async () => {
  const f = fixture({taskId:task});
  f.hide(); f.show();
  assert.equal(f.calls.length,2);
  f.calls[1].resolve({state:'active',terminal:false,message:'当前恢复后的状态'}); await flush();
  assert.equal(f.timers.size,1);
  f.calls[0].reject(new Error('上一页面的连接失败')); await flush();
  assert.equal(f.ids['result-status'].textContent,'当前恢复后的状态');
  assert.equal(f.timers.size,1);
  assert.deepEqual(f.navigations,[]);
});

test('a late old POST failure after restoration leaves the same pending key and fresh page intact', async () => {
  const f = fixture(); f.select(); const first=f.ids['result-prepare'].click();
  const frozen=f.saved(); f.hide(); f.show();
  f.calls[1].resolve({...f.state,title:'恢复后的原查询'}); await flush();
  const status=f.ids['result-status'].textContent;
  f.calls[0].reject(new Error('上一页面丢失的回执')); await first;
  assert.equal(f.ids['result-title'].textContent,'恢复后的原查询');
  assert.equal(f.ids['result-status'].textContent,status);
  assert.deepEqual(f.saved(),frozen);
  assert.equal(f.calls.filter(call=>call.options.method==='POST').length,1);
  assert.equal(f.timers.size,0);
});

test('an old response cannot navigate after two successive restored reads', async () => {
  const f=fixture({taskId:task}); f.hide(); f.show(); f.show();
  assert.equal(f.calls.length,3);
  f.calls[1].resolve({state:'waiting_user',terminal:false,interaction:{state:'pending',presentation:{url:`/flow/input/${opaque}`}}});
  await flush(); assert.deepEqual(f.navigations,[]);
  f.calls[2].resolve({state:'outcome_unknown',terminal:true,message:'当前结果待核对'});
  await flush();
  f.calls[0].resolve({state:'waiting_user',terminal:false,interaction:{state:'pending',presentation:{url:`/flow/input/${opaque}`}}});
  await flush();
  assert.deepEqual(f.navigations,[]);
  assert.equal(f.ids['result-status'].textContent,'当前结果待核对');
  assert.equal(f.timers.size,0);
});

test('unknown state blocks a continuation even if its terminal flag is incorrectly false', async () => {
  for (const state of ['unknown','outcome_unknown','failed','canceled','expired']) {
    const f=fixture({taskId:task});
    f.calls[0].resolve({state,terminal:false,interaction:{state:'pending',presentation:{url:`/flow/input/${opaque}`}}});
    await flush();
    assert.deepEqual(f.navigations,[]);
    assert.equal(f.timers.size,0);
  }
});

test('untrusted labels render as inert text and a disabled row cannot enter a preparation', () => {
  const injection='</script><img src=x onerror=alert(1)>';
  const f=fixture(null,{extraState:{title:injection,groups:[{operationId:'op1',title:injection,
    targets:[{targetKey:'c'.repeat(64),title:injection,enabled:false,disabledReason:injection}]}]}});
  assert.equal(f.ids['result-title'].textContent,injection);
  const section=f.ids['result-groups'].children[0];
  const label=section.children.find(node=>node.tag==='label');
  assert.equal(label.children[1].textContent,`待办 · ${injection}`);
  label.children[0].checked=true; label.children[0].listeners.change();
  f.ids['result-prepare'].click();
  assert.equal(f.calls.length,0);
  assert.equal(f.ids['result-prepare'].disabled,true);
  assert.ok(!section.children.some(node=>['img','script'].includes(node.tag)));
});

test('a refreshed frozen request recovers exactly and no client supplied task can widen the server view', async () => {
  const body={operationId:'op1',targetKeys:['b'.repeat(64)],requestKey:'original-request-key'};
  const f=fixture({body});
  assert.equal(f.calls.length,0);
  assert.equal(f.ids['result-prepare'].disabled,true);
  const recovered=f.ids['result-recover'].click();
  assert.deepEqual(JSON.parse(f.calls[0].options.body),body);
  f.calls[0].resolve({taskId:'not-a-task'}); await recovered;
  assert.equal(f.calls.length,1);
  assert.equal(f.saved().taskId,undefined);
  assert.deepEqual(f.saved().body,body);
});

test('checking an exact source keeps the same checkbox DOM for keyboard and screen reader focus', () => {
  const f=fixture();
  const section=f.ids['result-groups'].children[0];
  const input=section.children.find(node=>node.tag==='label').children[0];
  input.checked=true; input.listeners.change();
  assert.equal(f.ids['result-groups'].children[0],section);
  assert.equal(section.children.find(node=>node.tag==='label').children[0],input);
  assert.equal(input.checked,true);
  assert.equal(f.ids['result-count'].textContent,'已选择 1 项；每项仍需单独确认。');
  input.checked=false; input.listeners.change();
  assert.equal(input.checked,false);
  assert.equal(f.ids['result-prepare'].disabled,true);
});

test('a known terminal task can return to fresh choices without replay; unknown cannot', async () => {
  const f=fixture({taskId:task});
  f.calls[0].resolve({state:'succeeded',terminal:true}); await flush();
  const back=f.ids['result-resume'].children.at(-1);
  assert.equal(back.textContent,'返回待办选择'); back.click();
  assert.equal(f.calls[1].options.method,'GET'); assert.equal(f.saved(),null);
  assert.equal(f.ids['result-prepare'].disabled,true);
  f.calls[1].resolve(f.state); await flush(); f.select();
  assert.equal(f.ids['result-prepare'].disabled,false);
  assert.equal(f.calls.filter(c=>c.options.method==='POST').length,0);
  const u=fixture({taskId:task});
  u.calls[0].resolve({state:'outcome_unknown',terminal:true}); await flush();
  assert.ok(u.ids['result-resume'].children.every(x=>x.textContent!=='返回待办选择'));
  assert.equal(u.saved().taskId,task);
});

test('restoration cannot enable selections after the fresh permission read failed', async () => {
  const f=fixture(); f.select(); f.hide(); f.show();
  f.calls[0].resolve({error:{message:'入口已失效'}},false); await flush();
  assert.equal(f.ids['result-prepare'].disabled,true);
  f.ids['result-prepare'].click();
  assert.equal(f.calls.filter(c=>c.options.method==='POST').length,0);
});
