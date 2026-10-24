import assert from 'node:assert/strict';
import test from 'node:test';
import { installResultView } from '../agentbridge/auth/static/result_view.mjs';

const flush = () => new Promise(resolve => setImmediate(resolve));
const task = '12345678-1234-1234-1234-123456789abc';
const opaque = 'a'.repeat(43);

function fixture(saved = null, {extraState = {}, savedQuery = null} = {}) {
  function node(tag = '') { return {tag, children: [], listeners: {}, dataset:{}, style:{}, attributes:{}, setAttribute(name,value){this.attributes[name]=value;}, textContent: '', hidden: false,
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    addEventListener(name, fn) { this.listeners[name] = fn; }, click() { return this.listeners.click?.(); }}; }
  const ids = Object.fromEntries(['result-groups','result-title','result-status','result-count','result-prepare','result-recover','result-resume',
    'result-refresh','result-query-recover','result-search','result-type','result-selected-only','result-validity'].map(x => [x, node()]));
  const documentEvents={};
  const document = {getElementById: id => ids[id], createElement: node,addEventListener:(name,fn)=>documentEvents[name]=fn};
  const events = {}, timers = new Map(), calls = [], navigations = [];
  const key=`agentbridge-result-view:/results/${opaque}`;
  const storage = new Map([[key,JSON.stringify(saved)],[`${key}:query`,JSON.stringify(savedQuery)]]);
  let seq = 0, elapsed = 0, requestsMade = 0;
  const window = {location: {origin: 'https://trusted.test', href: `https://trusted.test/results/${opaque}`, hash: '#tgWebAppPlatform=android', replace: u => navigations.push(u)},
    addEventListener: (k,v) => { events[k] = v; }, sessionStorage: {getItem: k => storage.get(k), setItem: (k,v) => { storage.set(k,v); }},
    crypto: {randomUUID: () => requestsMade++ ? `request-1234567890-${requestsMade}` : 'request-1234567890'}};
  const state = {title:'OA 待办',groups:[{operationId:'op1', targets:[{targetKey:'b'.repeat(64),title:'月度考勤',enabled:true,workflowLabel:'考勤确认'},
    {targetKey:'c'.repeat(64),title:'未支持事项',enabled:false}]}],preparations:[], ...extraState};
  const fetch = (url, options) => new Promise((resolve, reject) => { calls.push({url,options,respond:resolve,
    resolve: (v,ok=true,status=ok?200:503) => resolve({ok,status,json:async()=>v}),reject}); });
  const controller = installResultView({document,window,fetch,initial:{path:`/results/${opaque}`,csrfToken:'proof',state},
    now:()=>elapsed, setTimeout: (fn,delay) => {const id=++seq;fn.at=elapsed+delay;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id)});
  const advance = async ms => {
    const until=elapsed+ms;
    while (true) {
      const next=[...timers].filter(([,fn])=>fn.at<=until).sort((a,b)=>a[1].at-b[1].at)[0];
      if (!next) break;
      timers.delete(next[0]); elapsed=next[1].at; next[1](); await flush();
    }
    elapsed=until;
  };
  function select() { const input = ids['result-groups'].children[0].children.find(x=>x.tag==='label').children[0]; input.checked=true; input.listeners.change(); }
  return {controller,ids,calls,navigations,timers,select,state,advance,hide:()=>events.pagehide(),show:()=>events.pageshow({persisted:true}),
    nativeRestore:()=>documentEvents['agentbridge:native-resume'](),
    saved:()=>JSON.parse(storage.get(key)),savedQuery:()=>JSON.parse(storage.get(`${key}:query`)),savedRecovery:()=>JSON.parse(storage.get(`${key}:recovery`)||'[]')};
}

test('selection queues only the exact original keys; no business approval is sent', async () => {
  const f=fixture(); f.select(); const promise=f.ids['result-prepare'].click();
  assert.equal(f.calls.length,1); assert.match(f.calls[0].url,/\/prepare$/);
  assert.deepEqual(JSON.parse(f.calls[0].options.body),{operationId:'op1',targetKeys:['b'.repeat(64)],requestKey:'request-1234567890'});
  f.calls[0].resolve({taskId:task}); await flush();
  assert.match(f.calls[1].url,/\/state\?task=/); assert.equal(f.calls[1].options.method,'GET');
  f.calls[1].resolve({state:'waiting_user',terminal:false,interaction:{state:'pending',presentation:{url:`https://trusted.test/flow/input/${opaque}`}}});
  await flush();assert.match(f.calls[2].url,/\/open$/);
  assert.equal(JSON.parse(f.calls[2].options.body).taskId,task);
  f.calls[2].resolve({taskId:task,interaction:{state:'pending',presentation:{url:`https://trusted.test/flow/input/${opaque}?result=${opaque}`}}});
  await promise; assert.deepEqual(f.navigations,[`https://trusted.test/flow/input/${opaque}?result=${opaque}#tgWebAppPlatform=android`]);
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
  assert.equal(f.ids['result-count'].textContent,'显示 2 项，其中可办 1 项、不可直接办理 1 项。已选择 1 项；每项仍需单独确认。');
  input.checked=false; input.listeners.change();
  assert.equal(input.checked,false);
  assert.equal(f.ids['result-prepare'].disabled,true);
});

test('terminal and unknown tasks can return through GET while an unknown item stays disabled', async () => {
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
  const unknownBack=u.ids['result-resume'].children.at(-1);
  assert.equal(unknownBack.textContent,'返回待办列表（此项仍待核对）'); unknownBack.click();
  assert.equal(u.calls[1].options.method,'GET');
  const blocked={...u.state,groups:u.state.groups.map(group=>({...group,targets:group.targets.map(target=>({...target,
    enabled:false,disabledReason:'原办理结果待核对，不要重复提交'}))}))};
  u.calls[1].resolve(blocked); await flush(); u.select();
  assert.equal(u.ids['result-prepare'].disabled,true);
  assert.equal(u.calls.filter(call=>call.options.method==='POST').length,0);
});

test('restoration cannot enable selections after the fresh permission read failed', async () => {
  const f=fixture(); f.select(); f.hide(); f.show();
  f.calls[0].resolve({error:{message:'入口已失效'}},false); await flush();
  assert.equal(f.ids['result-prepare'].disabled,true);
  f.ids['result-prepare'].click();
  assert.equal(f.calls.filter(c=>c.options.method==='POST').length,0);
});

test('superseded skipped and completed are terminal even with a stale pending interaction', async () => {
  for (const state of ['superseded','skipped','completed','partially_succeeded']) {
    const f=fixture({taskId:task});
    f.calls[0].resolve({state,terminal:false,interaction:{state:'pending',presentation:{url:`/flow/input/${opaque}`}}});
    await flush();
    assert.deepEqual(f.navigations,[]); assert.equal(f.timers.size,0);
    assert.equal(f.ids['result-resume'].children.at(-1).textContent,'返回待办选择');
  }
});

test('permanent endpoint and expiry errors stop reads and prevent another preparation', async () => {
  for (const [status,code] of [[403,'RESULT_CSRF_INVALID'],[410,'RESULT_VIEW_EXPIRED'],[409,'RESULT_VIEW_EXPIRED'],[404,'ACTION_SOURCE_NOT_FOUND']]) {
    const f=fixture({taskId:task});
    f.calls[0].resolve({error:{code,message:'原入口不可用'}},false,status); await flush();
    assert.equal(f.timers.size,0); assert.equal(f.ids['result-recover'].hidden,true);
    assert.match(f.ids['result-status'].textContent,/回原 Telegram 聊天/);
    f.ids['result-prepare'].click(); f.show(); await flush();
    assert.equal(f.calls.length,1); assert.deepEqual(f.navigations,[]);
  }
});

test('slow state reads show measured elapsed time, then offer an explicit bounded GET recovery', async () => {
  const f=fixture({taskId:task});
  await f.advance(6000); assert.match(f.ids['result-status'].textContent,/已等待 6 秒/);
  await f.advance(9000);
  assert.equal(f.calls[0].options.signal.aborted,true);
  assert.equal(f.timers.size,0);
  assert.equal(f.ids['result-recover'].hidden,false);
  assert.match(f.ids['result-status'].textContent,/超时/);
  f.ids['result-recover'].click();
  assert.equal(f.calls.length,2); assert.equal(f.calls[1].options.method,'GET');
  f.calls[1].resolve({state:'superseded',terminal:true,message:'原卡已替代'}); await flush();
  f.calls[0].resolve({state:'waiting_user',interaction:{state:'pending',presentation:{url:`/flow/input/${opaque}`}}}); await flush();
  assert.deepEqual(f.navigations,[]); assert.equal(f.ids['result-status'].textContent,'原卡已替代');
  assert.equal(f.timers.size,0);
});

test('prepare timeout preserves the exact frozen request and ignores its late reply', async () => {
  const f=fixture(); f.select(); const first=f.ids['result-prepare'].click();
  const original=f.calls[0].options.body;
  await f.advance(30000); await first;
  assert.equal(f.calls.length,1); assert.equal(f.ids['result-recover'].hidden,false);
  const second=f.ids['result-recover'].click();
  assert.equal(f.calls[1].options.body,original);
  f.calls[1].resolve({taskId:task}); await flush();
  f.calls[2].resolve({state:'unknown',terminal:true,message:'原结果待核对'}); await second;
  f.calls[0].resolve({taskId:'22345678-1234-1234-1234-123456789abc'}); await flush();
  assert.equal(f.saved().taskId,task); assert.equal(f.calls.length,3);
  assert.deepEqual(f.navigations,[]); assert.equal(f.timers.size,0);
});

test('an unrecognized task state stops rather than navigating or polling forever', async () => {
  const f=fixture({taskId:task});
  f.calls[0].resolve({state:'new-unknown-state',terminal:false,interaction:{state:'pending',presentation:{url:`/flow/input/${opaque}`}}});
  await flush();
  assert.equal(f.timers.size,0);assert.deepEqual(f.navigations,[]);
  assert.match(f.ids['result-status'].textContent,/无法识别/);
  assert.equal(f.ids['result-recover'].hidden,true);assert.equal(f.saved().taskId,task);
});

test('search and type filters select only visible enabled targets without replacing focused checkboxes', async () => {
  const f=fixture(null,{extraState:{groups:[{operationId:'op1',targets:[
    {targetKey:'a'.repeat(64),title:'财务周报',workflowType:'weekly',workflowLabel:'周报',sender:'张甲',enabled:true},
    {targetKey:'b'.repeat(64),title:'研发周报',workflowType:'weekly',workflowLabel:'周报',sender:'李乙',enabled:true},
    {targetKey:'c'.repeat(64),title:'财务考勤',workflowType:'attendance',workflowLabel:'考勤',enabled:true},
    {targetKey:'d'.repeat(64),title:'财务附件审批',workflowType:'unsupported',workflowLabel:'其他',enabled:false,disabledReason:'暂不支持'},
  ]}]}});
  const section=f.ids['result-groups'].children[0], labels=section.children.filter(child=>child.tag==='label');
  f.ids['result-search'].value='财务';f.ids['result-search'].listeners.input();
  f.ids['result-type'].value='weekly';f.ids['result-type'].listeners.change();
  assert.deepEqual(labels.map(label=>label.hidden),[false,true,true,true]);
  const all=section.children.find(child=>child.className==='result-select-current');
  assert.equal(all.textContent,'选择当前可办 1 项');all.click();
  assert.equal(f.ids['result-groups'].children[0],section);
  assert.equal(labels[0].children[0].checked,true);assert.equal(labels[3].children[0].checked,false);
  f.ids['result-search'].value='';f.ids['result-search'].listeners.input();
  f.ids['result-type'].value='';f.ids['result-type'].listeners.change();
  f.ids['result-selected-only'].checked=true;f.ids['result-selected-only'].listeners.change();
  assert.deepEqual(labels.map(label=>label.hidden),[false,true,true,true]);
  const prepare=f.ids['result-prepare'].click();
  assert.deepEqual(JSON.parse(f.calls[0].options.body).targetKeys,['a'.repeat(64)]);
  f.calls[0].reject(new Error('stop'));await prepare;
});

test('select current eligible explicitly reports disabled items and cannot mix source groups', async () => {
  const f=fixture(null,{extraState:{groups:[
    {operationId:'op1',targets:[{targetKey:'a'.repeat(64),title:'甲',enabled:true},{targetKey:'d'.repeat(64),title:'不可办',enabled:false}]},
    {operationId:'op2',targets:[{targetKey:'b'.repeat(64),title:'乙',enabled:true}]},
  ]}});
  assert.match(f.ids['result-count'].textContent,/不可直接办理 1 项/);
  const groups=f.ids['result-groups'].children;
  groups[0].children.find(node=>node.className==='result-select-current').click();
  groups[1].children.find(node=>node.className==='result-select-current').click();
  assert.equal(groups[0].children.find(node=>node.tag==='label').children[0].checked,false);
  const prepare=f.ids['result-prepare'].click();
  assert.deepEqual(JSON.parse(f.calls[0].options.body),{operationId:'op2',targetKeys:['b'.repeat(64)],requestKey:'request-1234567890'});
  f.calls[0].reject(new Error('stop'));await prepare;
});

test('resume cards identify actual titles, one-based batch position and explicit terminal result action', () => {
  const f=fixture(null,{extraState:{preparations:[
    {taskId:task,title:'核对并办理OA待办',selectedTitles:['九月考勤','部门周报'],state:'waiting_user',batch:{totalCount:2,currentIndex:2,succeededCount:1}},
    {taskId:'22345678-1234-1234-1234-123456789abc',selectedTitles:['会议邀请'],state:'superseded',terminal:true},
  ]}});
  assert.equal(f.ids['result-resume'].children[0].textContent,'继续办理 · 九月考勤、部门周报 · 等待核对 · 已完成 1/2 项，当前第 2 项');
  assert.equal(f.ids['result-resume'].children[1].textContent,'查看结果 · 会议邀请 · 已被替代');
});

test('query shortcut uses its own durable key, polls the read task and retrieves only its new same-origin ticket', async () => {
  const f=fixture();const start=f.ids['result-refresh'].click();
  assert.match(f.calls[0].url,/\/refresh$/);
  assert.deepEqual(JSON.parse(f.calls[0].options.body),{requestKey:'request-1234567890'});
  assert.equal(f.saved(),null);assert.equal(f.ids['result-prepare'].disabled,true);
  f.calls[0].resolve({taskId:task,query:{taskId:task,state:'active',terminal:false}});await start;
  assert.match(f.ids['result-status'].textContent,/查询已排队/);
  await f.advance(1000);assert.match(f.calls[1].url,/\/state\?query=/);assert.equal(f.calls[1].options.method,'GET');
  f.calls[1].resolve({taskId:task,state:'succeeded',terminal:true,selectionAvailable:true});await flush();
  assert.equal(f.calls[2].options.body,f.calls[0].options.body);
  f.calls[2].resolve({taskId:task,resultView:{presentation:{url:`https://trusted.test/results/${'z'.repeat(43)}`}}});await flush();
  assert.deepEqual(f.navigations,[`https://trusted.test/results/${'z'.repeat(43)}#tgWebAppPlatform=android`]);
  assert.equal(f.savedQuery(),null);assert.equal(f.timers.size,0);
  assert.equal(f.calls.filter(call=>/\/prepare$/.test(call.url)).length,0);
});

test('a new query containing only existing unknown work opens its readonly result ticket without prepare', async () => {
  const f=fixture(); const start=f.ids['result-refresh'].click();
  f.calls[0].resolve({taskId:task,query:{taskId:task,state:'succeeded',terminal:true,selectionAvailable:false,entryAvailable:true},
    resultView:{presentation:{url:`https://trusted.test/results/${'z'.repeat(43)}`}}});
  await start;
  assert.deepEqual(f.navigations,[`https://trusted.test/results/${'z'.repeat(43)}#tgWebAppPlatform=android`]);
  assert.equal(f.calls.filter(call=>/\/prepare$/.test(call.url)).length,0);
  assert.equal(f.savedQuery(),null);
});

test('lost query acknowledgement recovers the same read request independently of prepare and a new query is explicit', async () => {
  const f=fixture();const first=f.ids['result-refresh'].click();const body=f.calls[0].options.body;
  f.calls[0].reject(new Error('offline'));await first;
  assert.equal(f.ids['result-query-recover'].hidden,false);assert.equal(f.saved(),null);
  const second=f.ids['result-query-recover'].click();
  assert.equal(f.calls[1].options.body,body);
  f.calls[1].resolve({taskId:task,query:{taskId:task,state:'succeeded',terminal:true,selectionAvailable:false,groups:[{targets:[{title:'未支持单据',enabled:false,disabledReason:'需原系统核对'}]}]}});await second;await flush();
  assert.match(f.ids['result-status'].textContent,/已查询，发现 1 项/);
  assert.equal(f.ids['result-refresh'].disabled,false);
  assert.equal(f.ids['result-prepare'].disabled,true);
  assert.equal(f.ids['result-groups'].children.some(node=>node.tag==='label'),false);
  const third=f.ids['result-refresh'].click();
  assert.notEqual(f.calls[2].options.body,body);
  f.calls[2].reject(new Error('offline'));await third;
});

test('page restoration with a lost query reply exposes recovery without replaying its POST', async () => {
  const f=fixture();const first=f.ids['result-refresh'].click();const body=f.calls[0].options.body;
  f.hide();await first;f.show();
  assert.equal(f.calls[1].options.method,'GET');f.calls[1].resolve(f.state);await flush();
  assert.equal(f.ids['result-query-recover'].hidden,false);
  assert.equal(f.calls.filter(call=>call.options.method==='POST').length,1);
  const recovered=f.ids['result-query-recover'].click();assert.equal(f.calls[2].options.body,body);
  f.calls[2].resolve({taskId:task,query:{taskId:task,state:'failed',terminal:true,message:'查询未完成'}});await recovered;
  f.calls[0].resolve({taskId:'22345678-1234-1234-1234-123456789abc',query:{state:'active'}});await flush();
  assert.equal(f.savedQuery().taskId,task);
});

test('a refreshed pending read task resumes with GET and expired whole tickets never start new queries', async () => {
  const query={body:{requestKey:'original-query-request'},taskId:task};
  const f=fixture(null,{savedQuery:query});
  assert.match(f.calls[0].url,/\?query=/);assert.equal(f.calls[0].options.method,'GET');
  f.calls[0].resolve({error:{code:'RESULT_VIEW_EXPIRED',message:'入口已过期'}},false,409);await flush();
  assert.equal(f.ids['result-query-recover'].hidden,true);assert.equal(f.ids['result-refresh'].disabled,true);
  f.ids['result-refresh'].click();f.ids['result-query-recover'].click();f.show();await flush();
  assert.equal(f.calls.length,1);assert.equal(f.timers.size,0);
});

test('fresh result tickets accept only exact same-origin opaque paths and never follow forged links', async () => {
  const f=fixture();
  for (const url of [`https://evil.test/results/${opaque}`,`https://user:pass@trusted.test/results/${opaque}`,
    `/results/${opaque}?x=1`,`/results/${opaque}#override`,`/results/${'a'.repeat(42)}`,`/results/${opaque}/state`,`javascript:alert(1)`]) {
    assert.equal(f.controller.safeResultUrl(url),null);
  }
  const started=f.ids['result-refresh'].click();
  f.calls[0].resolve({taskId:task,query:{taskId:task,state:'succeeded',terminal:true,selectionAvailable:true},resultView:{presentation:{url:`https://evil.test/results/${opaque}`}}});await started;
  assert.deepEqual(f.navigations,[]);assert.equal(f.ids['result-query-recover'].hidden,false);
  assert.equal(f.calls.length,1);
});

test('timed out query preparation keeps its key and cannot be replaced with a second read request', async () => {
  const f=fixture();const started=f.ids['result-refresh'].click();const body=f.calls[0].options.body;
  await f.advance(30000);await started;
  assert.equal(f.ids['result-refresh'].disabled,true);assert.equal(f.ids['result-query-recover'].hidden,false);
  f.ids['result-refresh'].click();assert.equal(f.calls.length,1);
  const resumed=f.ids['result-query-recover'].click();assert.equal(f.calls[1].options.body,body);
  f.calls[1].resolve({taskId:task,query:{taskId:task,state:'succeeded',terminal:true,selectionAvailable:false,groups:[]}});await resumed;await flush();
  f.calls[0].resolve({taskId:'22345678-1234-1234-1234-123456789abc',query:{state:'active'}});await flush();
  assert.equal(f.savedQuery().taskId,task);assert.equal(f.timers.size,0);
  assert.match(f.ids['result-status'].textContent,/已查询，发现 0 项/);
});

test('query navigation freezes the old selection until the new ticket page replaces it', async () => {
  const f=fixture();f.select();const started=f.ids['result-refresh'].click();
  f.calls[0].resolve({taskId:task,query:{taskId:task,state:'succeeded',terminal:true,selectionAvailable:true},
    resultView:{presentation:{url:`/results/${'z'.repeat(43)}`}}});await started;
  assert.equal(f.ids['result-prepare'].disabled,true);
  f.ids['result-prepare'].click();f.ids['result-refresh'].click();assert.equal(f.calls.length,1);
});

test('known permanent response headers survive a hanging error body and cannot restart polling', async () => {
  for (const status of [401,403,404,410]) {
    const f=fixture({taskId:task});
    f.calls[0].respond({ok:false,status,json:()=>new Promise(()=>{})});await flush();
    await f.advance(15000);
    assert.equal(f.timers.size,0);assert.equal(f.ids['result-recover'].hidden,true);
    assert.equal(f.ids['result-prepare'].disabled,true);assert.match(f.ids['result-status'].textContent,/已失效/);
    f.show();f.ids['result-recover'].click();f.ids['result-refresh'].click();await flush();
    assert.equal(f.calls.length,1);assert.deepEqual(f.navigations,[]);
  }
});

test('source expiry removes only new choices and preserves current task and explicit refresh actions', async () => {
  const f=fixture(null,{extraState:{groups:[{operationId:'op1',expiresAt:new Date(1000).toISOString(),targets:[
    {targetKey:'b'.repeat(64),title:'月度考勤',enabled:true}]}],preparations:[{taskId:task,state:'waiting_user'}]}});
  f.select();assert.equal(f.ids['result-prepare'].disabled,false);
  const section=f.ids['result-groups'].children[0], input=section.children.find(x=>x.tag==='label').children[0];
  await f.advance(1000);
  assert.equal(input.disabled,true);assert.equal(input.checked,false);
  assert.equal(f.ids['result-prepare'].disabled,true);assert.equal(f.ids['result-refresh'].disabled,false);
  assert.equal(f.ids['result-resume'].children[0].disabled,false);
  assert.equal(section.children.find(x=>x.className==='result-select-current').textContent,'选择当前可办 0 项');
  assert.match(f.ids['result-count'].textContent,/已选择 0 项/);
  assert.ok(section.children.some(x=>/原查询选择已过期/.test(x.textContent)));
  f.ids['result-prepare'].click();assert.equal(f.calls.length,0);assert.equal(f.timers.size,0);
});

test('expired source choices cannot be selected even with a forced checkbox event and expiry waits suspend on leave', async () => {
  for (const expiresAt of [new Date(0).toISOString(),'invalid-date']) {
    const f=fixture(null,{extraState:{groups:[{operationId:'op1',expiresAt,targets:[{targetKey:'b'.repeat(64),enabled:true}]}]}});
    f.select();f.ids['result-prepare'].click();
    assert.equal(f.calls.length,0);assert.equal(f.saved(),null);assert.equal(f.ids['result-refresh'].disabled,false);
  }
  const f=fixture(null,{extraState:{groups:[{operationId:'op1',expiresAt:new Date(1000).toISOString(),targets:[{targetKey:'b'.repeat(64),enabled:true}]}]}});
  assert.equal(f.timers.size,1);f.hide();assert.equal(f.timers.size,0);
  await f.advance(2000);f.show();assert.equal(f.calls[0].options.method,'GET');
  f.calls[0].resolve({...f.state,groups:[]});await flush();
  assert.equal(f.ids['result-prepare'].disabled,true);assert.equal(f.ids['result-refresh'].disabled,false);
});

test('a known source-expired rejection refreshes only the original state and keeps the one-hour ticket query available', async () => {
  const f=fixture();f.select();const started=f.ids['result-prepare'].click();
  f.calls[0].resolve({error:{code:'ACTION_SOURCE_EXPIRED',message:'原查询已过期'}},false,409);await flush();
  assert.equal(f.saved(),null);assert.equal(f.calls[1].options.method,'GET');assert.match(f.calls[1].url,/\/state$/);
  f.calls[1].resolve({...f.state,groups:[],preparations:[{taskId:task,state:'waiting_user'}]});await started;
  assert.equal(f.ids['result-refresh'].disabled,false);assert.equal(f.ids['result-recover'].hidden,true);
  assert.equal(f.ids['result-resume'].children[0].disabled,false);
  const refreshed=f.ids['result-refresh'].click();assert.match(f.calls[2].url,/\/refresh$/);
  assert.equal(f.calls.filter(call=>/\/prepare$/.test(call.url)).length,1);
  f.calls[2].resolve({taskId:task,query:{taskId:task,state:'succeeded',terminal:true,selectionAvailable:false,groups:[]}});await refreshed;
});

test('an unresolved preparation survives source expiry and recovers its original frozen key', async () => {
  const f=fixture(null,{extraState:{groups:[{operationId:'op1',expiresAt:new Date(1000).toISOString(),targets:[{targetKey:'b'.repeat(64),enabled:true}]}]}});
  f.select();const started=f.ids['result-prepare'].click();const original=f.calls[0].options.body;
  f.calls[0].reject(new Error('lost reply'));await started;await f.advance(2000);
  assert.deepEqual(f.saved().body,JSON.parse(original));
  const recovered=f.ids['result-recover'].click();assert.equal(f.calls[1].options.body,original);
  f.calls[1].resolve({taskId:task});await flush();
  f.calls[2].resolve({state:'superseded',terminal:true});await recovered;
  assert.equal(f.saved().taskId,task);assert.equal(f.calls.filter(call=>/\/prepare$/.test(call.url)).length,2);
});

const childTask='22345678-1234-1234-1234-123456789abc';
const publicRecovery=(extra={})=>({originalTaskId:task,status:'not_checked',availability:'can_probe',reason:'not_checked',canProbe:true,
  canPrepare:false,canExecute:false,stale:false,...extra});
const assessment=(extra={})=>({status:'unresolved_still_pending',canPrepare:true,canExecute:false,assessmentId:'32345678-1234-1234-1234-123456789abc',
  assessmentHash:`sha256:${'d'.repeat(64)}`,revision:3,expiresAt:new Date(2000).toISOString(),doneCoverage:'partial',...extra});
const recoveryItem=(p=publicRecovery())=>({taskId:task,state:'outcome_unknown',terminal:true,selectedTitles:['原事项'],oaPendingRecovery:p});
const recoveryPanel=f=>f.ids['result-resume'].children.find(n=>n.className==='result-recovery');

test('GET recovery state cannot supply preparation authority; only a fresh probe and explicit risk choice can', async()=>{
  const f=fixture(null,{extraState:{preparations:[{...recoveryItem(),assessment:assessment()}]}});
  const panel=recoveryPanel(f),probe=panel.children[4],risk=panel.children[5],prepare=panel.children[6];
  assert.equal(risk.hidden,true);assert.equal(prepare.hidden,true);
  prepare.click();assert.equal(f.calls.length,0);
  const checking=probe.click();assert.equal(f.calls[0].options.method,'POST');
  assert.match(f.calls[0].url,/recovery-probe$/);
  assert.equal(JSON.parse(f.calls[0].options.body).taskId,task);
  f.calls[0].resolve({taskId:task,oaPendingRecovery:publicRecovery({status:'unresolved_still_pending'}),assessment:assessment()});await checking;
  assert.equal(risk.hidden,false);assert.equal(prepare.disabled,true);
  assert.match(panel.children[3].textContent,/部分覆盖/);
  risk.children[0].checked=true;risk.children[0].listeners.change();assert.equal(prepare.disabled,false);
  await f.advance(2001);assert.equal(prepare.hidden,true);assert.equal(risk.children[0].checked,false);
  prepare.click();assert.equal(f.calls.length,1);
  assert.match(panel.children[3].textContent,/已到期/);
});

test('lost recovery prepare receipt keeps the exact original task and request while blocking another attempt',async()=>{
  const f=fixture(null,{extraState:{preparations:[{...recoveryItem(),taskId:childTask,batch:{totalCount:3,succeededCount:1,currentIndex:2}}]}});
  const panel=recoveryPanel(f),probe=panel.children[4],risk=panel.children[5],prepare=panel.children[6];
  const checking=probe.click();f.calls[0].resolve({taskId:task,oaPendingRecovery:publicRecovery({status:'unresolved_still_pending'}),assessment:assessment()});await checking;
  risk.children[0].checked=true;risk.children[0].listeners.change();const first=prepare.click();
  const body=f.calls[1].options.body;assert.equal(JSON.parse(body).taskId,task);assert.equal(JSON.parse(body).acknowledgement,'oa_unknown_new_attempt_v1');
  f.calls[1].reject(Error('reply lost'));await first;
  assert.equal(f.savedRecovery()[0].command.body.taskId,task);
  probe.click();assert.equal(f.calls.length,2);
  const retried=prepare.click();assert.equal(f.calls[2].options.body,body);
  f.calls[2].resolve({taskId:task,recoveryTaskId:childTask,oaPendingRecovery:publicRecovery({status:'recovery_in_progress',recoveryTaskId:childTask,canProbe:false})});await retried;
  assert.equal(f.savedRecovery().length,0);assert.equal(panel.children[7].hidden,false);assert.deepEqual(f.navigations,[]);
  const opened=panel.children[7].click();assert.match(f.calls[3].url,/recovery-open$/);
  f.calls[3].resolve({taskId:task,oaPendingRecovery:publicRecovery({status:'recovery_in_progress',recoveryTaskId:childTask,canProbe:false}),
    interaction:{state:'pending',presentation:{url:`/flow/input/${opaque}?result=${opaque}`}}});await opened;
  assert.equal(f.navigations.length,1);assert.ok(f.calls.every(call=>!/approve|authorize|commit/.test(call.url)));
});

test('native restoration drops probe authority and performs only a fresh GET, retaining no risk choice',async()=>{
  const f=fixture(null,{extraState:{preparations:[recoveryItem()]}});
  let panel=recoveryPanel(f);const checking=panel.children[4].click();
  f.calls[0].resolve({taskId:task,oaPendingRecovery:publicRecovery({status:'unresolved_still_pending'}),assessment:assessment()});await checking;
  panel.children[5].children[0].checked=true;panel.children[5].children[0].listeners.change();
  f.nativeRestore();assert.equal(f.calls[1].options.method,'GET');
  f.calls[1].resolve({...f.state,preparations:[recoveryItem(publicRecovery({status:'unresolved_still_pending'}))]});await flush();
  panel=recoveryPanel(f);assert.equal(panel.children[5].hidden,true);assert.equal(panel.children[6].hidden,true);
  assert.equal(f.calls.filter(c=>c.options.method==='POST').length,1);
});

test('expired verification is displayed as historical and no longer suppresses the readonly probe',async()=>{
  const f=fixture(null,{extraState:{preparations:[recoveryItem(publicRecovery({status:'verified_complete',canProbe:false,
    observedAt:new Date(0).toISOString(),expiresAt:new Date(500).toISOString()}))]}});
  const panel=recoveryPanel(f);assert.equal(panel.children[4].hidden,true);
  await f.advance(501);assert.match(panel.children[1].textContent,/上次核对/);assert.match(panel.children[1].textContent,/现已过期/);
  assert.equal(panel.children[4].hidden,false);assert.equal(panel.children[6].hidden,true);
});

test('return tokens accept only this exact source and never arbitrary query keys or duplicated values',()=>{
  const f=fixture();
  for(const query of [`?result=${'b'.repeat(43)}`,`?result=${opaque}&result=${opaque}`,`?result=${opaque}&returnUrl=https://evil.test`,`?result=%61${opaque.slice(1)}`])
    assert.equal(f.controller.safeAction({state:'pending',presentation:{url:`/flow/input/${opaque}${query}`}}),null);
});

test('a recovered child completed on another endpoint refreshes only the public result and stays read-only after reopen',async()=>{
  const finished=publicRecovery({status:'recovery_succeeded',recoveryTaskId:childTask,canProbe:false,observedAt:new Date(0).toISOString()});
  const f=fixture(null,{extraState:{preparations:[recoveryItem(finished)]}});
  let panel=recoveryPanel(f);assert.match(panel.children[1].textContent,/后续办理已核验完成/);
  const refreshing=panel.children[7].click();assert.equal(f.calls[0].options.method,'GET');
  assert.match(f.calls[0].url,/\/state\?task=/);
  f.calls[0].resolve({...recoveryItem(finished),taskId:task});await refreshing;
  assert.deepEqual(f.navigations,[]);assert.equal(f.calls.filter(c=>c.options.method==='POST').length,0);
  f.hide();f.show();f.calls[1].resolve({...f.state,preparations:[recoveryItem(finished)]});await flush();
  panel=recoveryPanel(f);assert.equal(panel.children[6].hidden,true);assert.equal(panel.children[4].hidden,true);
  panel.children[7].click();assert.equal(f.calls[2].options.method,'GET');
  f.calls[2].resolve({...recoveryItem(finished),taskId:task});await flush();
  assert.equal(f.calls.filter(c=>c.options.method==='POST').length,0);
});

test('a known form without a local presentation uses the explicit endpoint open capability',async()=>{
  const f=fixture({taskId:task});f.calls[0].resolve({taskId:task,state:'waiting_user',terminal:false,canOpen:true});await flush();
  assert.match(f.calls[1].url,/\/open$/);assert.equal(f.calls[1].options.method,'POST');
  f.calls[1].resolve({taskId:task,interaction:{state:'pending',presentation:{url:`/flow/input/${opaque}?result=${opaque}`}}});await flush();
  assert.deepEqual(f.navigations,[`https://trusted.test/flow/input/${opaque}?result=${opaque}#tgWebAppPlatform=android`]);
});

test('acknowledged form open retires the pending selection so trusted Back does not reopen the same form',async()=>{
  const f=fixture({taskId:task});f.calls[0].resolve({taskId:task,state:'waiting_user',terminal:false,canOpen:true});await flush();
  f.calls[1].resolve({taskId:task,interaction:{state:'pending',presentation:{url:`/flow/input/${opaque}?result=${opaque}`}}});await flush();
  assert.equal(f.saved(),null);
  const reopened=fixture(f.saved(),{extraState:{preparations:[{taskId:task,state:'waiting_user',canOpen:true}]}});
  assert.equal(reopened.calls.length,0);assert.deepEqual(reopened.navigations,[]);
});

test('an input receipt changing before open offers a readonly status retry instead of expiring the whole ticket',async()=>{
  const f=fixture({taskId:task});f.calls[0].resolve({taskId:task,state:'waiting_user',terminal:false,canOpen:true});await flush();
  f.calls[1].resolve({error:{code:'ACTION_SOURCE_UNAVAILABLE',message:'界面尚未就绪'}},false,409);await flush();
  assert.equal(f.ids['result-recover'].hidden,false);assert.equal(f.saved().openRequestKey,undefined);
  f.ids['result-recover'].click();assert.equal(f.calls[2].options.method,'GET');
  f.calls[2].resolve({taskId:task,state:'outcome_unknown',terminal:true});await flush();
  assert.deepEqual(f.navigations,[]);
});

test('resume buttons show fresh recovery conclusions while retaining original unknown history and expiring in place',async()=>{
  for(const [status,label] of [['recovery_succeeded','后续办理已完成'],['verified_complete','原事项已核验完成']]) {
    const projection=publicRecovery({status,canProbe:false,recoveryTaskId:status==='recovery_succeeded'?childTask:undefined,
      observedAt:new Date(0).toISOString(),expiresAt:new Date(500).toISOString()});
    const f=fixture(null,{extraState:{preparations:[recoveryItem(projection)]}});
    const button=f.ids['result-resume'].children[0],panel=recoveryPanel(f);
    assert.match(button.textContent,new RegExp(label));assert.doesNotMatch(button.textContent,/结果待核实/);
    assert.match(panel.children[2].textContent,/原尝试的结果仍保留为待核对/);
    assert.equal(f.state.preparations[0].state,'outcome_unknown');assert.equal(f.calls.length,0);
    await f.advance(501);assert.match(button.textContent,/上次核对已过期/);assert.equal(f.calls.length,0);
  }
  const forged=fixture(null,{extraState:{preparations:[recoveryItem({...publicRecovery({status:'recovery_succeeded'}),canExecute:true})]}});
  assert.match(forged.ids['result-resume'].children[0].textContent,/结果待核实/);
});

test('conflicting or completed evidence explains the bounded reason without enabling preparation',()=>{
  const f=fixture(null,{extraState:{preparations:[recoveryItem(publicRecovery({status:'blocked',reason:'conflicting_or_completed_evidence'}))]}});
  const panel=recoveryPanel(f);
  assert.equal(panel.children[3].textContent,'已有完成或待办、已办相互冲突的证据，请先到 OA 核对原事项。');
  assert.equal(panel.children[6].hidden,true);panel.children[6].click();assert.equal(f.calls.length,0);
});

const remainingPreparation=(overrides={})=>({taskId:task,state:'outcome_unknown',terminal:true,
 batch:{totalCount:3,succeededCount:1,items:[{ordinal:1,title:'已成功',state:'succeeded'},
 {ordinal:2,title:'未知项',state:'outcome_unknown',isCurrent:true},{ordinal:3,title:'剩余项',state:'queued'}]},
 oaBatchRemaining:{available:true,batchVersion:7,remainingCount:1},...overrides});
const findRemaining=f=>f.ids['result-resume'].children.find(n=>n.tag==='button'&&n.textContent.startsWith('重新查询并核对剩余'));

test('Telegram stopped batch shows independent item outcomes and explicitly queries only the server-defined remainder',async()=>{
 const f=fixture(null,{extraState:{preparations:[remainingPreparation()]}});
 const checklist=f.ids['result-resume'].children.find(n=>n.tag==='section');
 const list=checklist.children[0].children[2];
 assert.match(list.children[0].textContent,/已核验完成/);assert.match(list.children[1].textContent,/当前项.*结果待核对/);
 assert.match(list.children[2].textContent,/尚未处理/);
 findRemaining(f).click();assert.equal(f.calls.length,1);assert.match(f.calls[0].url,/\/remaining-query$/);
 const body=JSON.parse(f.calls[0].options.body);
 assert.deepEqual(body,{taskId:task,requestKey:'request-1234567890',expectedBatchVersion:7});
 assert.equal(f.ids['result-prepare'].disabled,true);assert.equal(findRemaining(f).disabled,true);
 findRemaining(f).click();assert.equal(f.calls.length,1);
 f.calls[0].reject(Error('lost receipt'));await flush();
 assert.equal(f.ids['result-query-recover'].hidden,false);f.ids['result-query-recover'].click();
 assert.equal(f.calls[1].options.body,f.calls[0].options.body);assert.match(f.calls[1].url,/\/remaining-query$/);
 f.calls[1].reject(Error('still offline'));await flush();assert.deepEqual(f.navigations,[]);
});

test('Telegram remaining query restores through GET then obtains a new ticket only by the original explicit POST',async()=>{
 const query='22345678-1234-1234-1234-123456789abc',ticket='z'.repeat(43);
 const body={taskId:task,requestKey:'original-remaining-key',expectedBatchVersion:7};
 const f=fixture(null,{extraState:{preparations:[remainingPreparation()]},savedQuery:{action:'remaining-query',body,taskId:query}});
 assert.equal(f.calls[0].options.method,'GET');assert.match(f.calls[0].url,/state\?query=/);
 f.calls[0].resolve({taskId:query,state:'succeeded',terminal:true,selectionAvailable:true});await flush();
 assert.match(f.calls[1].url,/\/remaining-query$/);assert.deepEqual(JSON.parse(f.calls[1].options.body),body);
 f.calls[1].resolve({taskId:query,resultView:{presentation:{url:`https://trusted.test/results/${ticket}`}}});await flush();
 assert.deepEqual(f.navigations,[`https://trusted.test/results/${ticket}#tgWebAppPlatform=android`]);assert.equal(f.savedQuery(),null);
 assert.equal(f.calls.filter(c=>/\/prepare$|\/recovery-prepare$|\/open$/.test(c.url)).length,0);
});

test('remaining query metadata never makes unknown or successful items selectable and malformed saved commands cannot widen scope',()=>{
 for(const value of [{available:false,batchVersion:1,remainingCount:2},{available:true,batchVersion:0,remainingCount:2},
  {available:true,batchVersion:1,remainingCount:0}]) {
  const f=fixture(null,{extraState:{preparations:[remainingPreparation({oaBatchRemaining:value})]}});
  assert.equal(findRemaining(f),undefined);assert.equal(f.calls.length,0);
 }
 const f=fixture(null,{savedQuery:{action:'remaining-query',body:{taskId:task,requestKey:'tampered-command-key',expectedBatchVersion:7,targetKeys:['x']}}});
 assert.equal(f.calls.length,0);assert.equal(f.ids['result-query-recover'].hidden,true);
});

test('remaining result distinguishes matched, missing and changed original ordinals without selecting unavailable items',()=>{
 const f=fixture(null,{extraState:{groups:[{operationId:'scoped-op',remaining:{requestedCount:3,matchedCount:1,missingCount:1,changedCount:1,
 items:[{ordinal:3,title:'仍在待办',state:'matched'},{ordinal:4,title:'已离开待办',state:'missing'},{ordinal:5,title:'节点变化',state:'changed'}]},
 targets:[{targetKey:'a'.repeat(64),title:'仍在待办',enabled:true}]}]}});
 const group=f.ids['result-groups'].children[0];
 const text=group.children.filter(n=>n.tag==='p').map(n=>n.textContent).join(' ');
 assert.match(text,/剩余 3 项.*匹配 1 项.*当前待办未找到 1 项.*资格变化 1 项.*未找到不代表已成功/);
 assert.doesNotMatch(text,/已不在当前待办|身份或资格变化/);
 assert.match(text,/原第 4 项.*不代表办理成功/);
 assert.equal(group.children.filter(n=>n.tag==='label').length,1);assert.equal(f.calls.length,0);
});
