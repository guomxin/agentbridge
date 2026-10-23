import assert from 'node:assert/strict';
import test from 'node:test';
import {installTelegramBridge} from '../agentbridge/auth/static/telegram_bridge.mjs';

function fixture({native = 'proxy', version = '8.0', dirty = false, disabled = true, complete = false, hash = '', label = '确认并审批通过'} = {}) {
  const posts = [], properties = {}, events = {}, documentEvents = {}, viewportEvents = {}, timers = [];
  let clicks = 0, confirmed = false, observes = 0, observerCallback;
  const form = {dataset: {agentbridgeDirty: String(dirty), agentbridgeBusy: 'false', agentbridgeLocked: 'false'}};
  const button = {disabled, hidden: false, dataset:{agentbridgeActionLabel:label}, textContent: '不会读取的业务标题与人员', click() { clicks++; this.disabled = true; }};
  Object.defineProperty(form, 'elements', {get() { throw new Error('must not read business fields'); }});
  const document = {documentElement: {dataset: {}, style: {setProperty: (key, value) => {properties[key] = value;}}},
    getElementById: id => id === 'draft-form' ? form : id === 'draft-submit' ? button : null,
    addEventListener: (name, callback) => {documentEvents[name] = callback;}, dispatchEvent: event => documentEvents[event.type]?.(event)};
  const window = {location: {hash: hash || `#tgWebAppPlatform=tdesktop&tgWebAppVersion=${version}&tgWebAppData=secret-launch-proof`},
    innerHeight: 800, visualViewport: {height: 600, addEventListener: (name, callback) => {viewportEvents[name] = callback;}},
    addEventListener: (name, callback) => {events[name] = callback;},
    setTimeout: (fn, delay) => {timers.push({fn, delay});}, confirm: () => confirmed,
    CustomEvent: class {constructor(type) {this.type=type;}},
    MutationObserver: class {constructor(callback) {observerCallback = callback;} observe() {observes++;}},
    ...(native === 'proxy' ? {TelegramWebviewProxy: {postEvent: (name, json) => posts.push({name, data: JSON.parse(json)})}} : {}),
    ...(native === 'notify' ? {external: {notify: json => {const value = JSON.parse(json); posts.push({name:value.eventType,data:value.eventData});}}} : {}),
  };
  const controller = installTelegramBridge({window, document, closeWhenComplete: complete,
    actionLabels:['确认并审批通过','确认并提交考勤单','确认并阅办知会']});
  const latest = name => posts.filter(post => post.name === name).at(-1)?.data;
  return {controller, window, document, form, button, properties, events, documentEvents, viewportEvents, posts, timers, latest,
    clicks: () => clicks, observes: () => observes, confirm: value => {confirmed = value;}, mutate: () => observerCallback?.(),
    receive: (name, data) => window.TelegramGameProxy.receiveEvent(name, data)};
}

test('native main action mirrors existing DOM permission and invokes the same button once', () => {
  const h = fixture();
  assert.equal(h.latest('web_app_setup_main_button').is_active, false);
  h.receive('main_button_pressed'); assert.equal(h.clicks(), 0);
  h.button.disabled = false; h.documentEvents['agentbridge:draft-state']();
  assert.equal(h.latest('web_app_setup_main_button').is_active, true);
  h.receive('main_button_pressed'); h.receive('main_button_pressed');
  assert.equal(h.clicks(), 1);
  assert.equal(h.latest('web_app_setup_main_button').is_active, false);
  assert.equal(h.observes(), 1);
  h.button.disabled = false; h.form.dataset.agentbridgeLocked = 'true'; h.mutate();
  h.receive('main_button_pressed'); assert.equal(h.clicks(), 1);
  assert.equal(h.latest('web_app_setup_main_button').is_active, false);
});

test('dirty and busy are explicit controller state; native controls never inspect business text', () => {
  const h = fixture({disabled:false,dirty:true});
  assert.deepEqual(h.latest('web_app_setup_closing_behavior'), {need_confirmation:true});
  h.form.dataset.agentbridgeDirty = 'false'; h.form.dataset.agentbridgeBusy = 'true'; h.documentEvents['agentbridge:draft-state']();
  assert.deepEqual(h.latest('web_app_setup_closing_behavior'), {need_confirmation:false});
  assert.equal(h.latest('web_app_setup_main_button').is_progress_visible,true);
  assert.equal(h.latest('web_app_setup_main_button').is_active,false);
  h.receive('main_button_pressed'); assert.equal(h.clicks(),0);
  const sent = JSON.stringify(h.posts);
  assert.ok(!sent.includes('业务标题')); assert.ok(!sent.includes('secret-launch-proof'));
  assert.ok(!h.posts.some(post=>/data_send|storage|clipboard|request_contact/.test(post.name)));
});

test('native back protects unsaved edits, closes only after choice, and never submits or cancels business', () => {
  const h = fixture({dirty:true,disabled:false});
  h.receive('back_button_pressed');assert.equal(h.latest('web_app_close'),undefined);
  h.confirm(true);h.receive('back_button_pressed');assert.deepEqual(h.latest('web_app_close'),{});
  assert.equal(h.clicks(),0);
  const clean=fixture({dirty:false});clean.receive('back_button_pressed');assert.deepEqual(clean.latest('web_app_close'),{});
});

test('theme safe-area and viewport accept bounded geometry and strict hex colors only', () => {
  const h=fixture();
  h.receive('theme_changed',{theme_params:{bg_color:'#111111',secondary_bg_color:'#222222',text_color:'#eeeeee',
    button_color:'url(https://untrusted.test)',hint_color:'red;display:none',unknown:'#ffffff'}});
  assert.equal(h.properties['--paper'],'#111111');assert.equal(h.properties['--surface'],'#222222');
  assert.equal(h.properties['--ink'],'#eeeeee');assert.equal(h.properties['color-scheme'],'dark');
  assert.equal(h.properties['--teal'],'#4dbea8');assert.equal(h.properties['--muted'],'#adbcc7');
  h.receive('safe_area_changed',{top:28,right:0,bottom:34,left:12});
  h.receive('content_safe_area_changed',{top:56,bottom:14,left:-1,right:Infinity});
  assert.equal(h.properties['--agent-safe-bottom'],'34px');assert.equal(h.properties['--agent-content-safe-top'],'56px');
  assert.equal(h.properties['--agent-content-safe-left'],undefined);assert.equal(h.properties['--agent-content-safe-right'],undefined);
  h.receive('viewport_changed',{height:490,is_state_stable:true});
  assert.equal(h.properties['--agent-viewport-height'],'490px');assert.equal(h.properties['--agent-viewport-stable-height'],'490px');
  h.receive('viewport_changed',{height:300,is_state_stable:false});
  assert.equal(h.properties['--agent-viewport-height'],'300px');assert.equal(h.properties['--agent-viewport-stable-height'],'490px');
  for(const height of [-1,Infinity,'900',20001])h.receive('viewport_changed',{height,is_state_stable:true});
  assert.equal(h.properties['--agent-viewport-height'],'300px');
});

test('plain web remains usable without native transport even when a Telegram hash is copied', () => {
  const h=fixture({native:null});
  assert.equal(h.controller.enabled,false);assert.deepEqual(h.posts,[]);assert.deepEqual(h.properties,{});
  assert.equal(h.document.documentElement.dataset.agentHost,undefined);assert.equal(h.observes(),0);
  assert.equal(h.button.hidden,false);
});

test('old client versions receive supported events only and external notify uses the same closed payload', () => {
  const old=fixture({version:'6.0',dirty:true});
  assert.ok(old.latest('web_app_setup_main_button'));
  assert.equal(old.latest('web_app_setup_closing_behavior'),undefined);assert.equal(old.latest('web_app_setup_back_button'),undefined);
  assert.equal(old.latest('web_app_request_safe_area'),undefined);
  const legacy=fixture({native:'notify'});
  assert.ok(legacy.posts.some(post=>post.name==='web_app_ready'));
  legacy.window.TelegramGameProxy_receiveEvent('viewport_changed',{height:333,is_state_stable:true});
  assert.equal(legacy.properties['--agent-viewport-height'],'333px');
  legacy.window.Telegram.WebView.receiveEvent('safe_area_changed',{top:9});
  assert.equal(legacy.properties['--agent-safe-top'],'9px');
});

test('pagehide disables native clicks; BFCache restores current state without submitting', () => {
  const h=fixture({disabled:false});
  h.events.pagehide();h.receive('main_button_pressed');assert.equal(h.clicks(),0);
  assert.deepEqual(h.latest('web_app_setup_main_button'),{is_visible:false});
  h.button.disabled=true;h.events.pageshow();assert.equal(h.latest('web_app_setup_main_button').is_active,false);
  assert.equal(h.clicks(),0);
  h.button.disabled=false;h.mutate();assert.equal(h.latest('web_app_setup_main_button').is_active,true);
});

test('completion close is never inferred from a disabled button or a native theme event', () => {
  const h=fixture();h.receive('theme_changed',{theme_params:{bg_color:'#ffffff'}});
  assert.equal(h.timers.length,0);assert.equal(h.latest('web_app_close'),undefined);
  const complete=fixture({complete:true});complete.timers[0].fn();assert.deepEqual(complete.latest('web_app_close'),{});
  const dirty=fixture({complete:true,dirty:true});dirty.timers[0].fn();assert.equal(dirty.latest('web_app_close'),undefined);
});

test('native action uses only a registered label and updates with the same disabled guard', () => {
  const h=fixture({disabled:false,label:'确认并提交考勤单'});
  assert.equal(h.latest('web_app_setup_main_button').text,'确认并提交考勤单');
  h.button.dataset.agentbridgeActionLabel='确认并阅办知会';h.mutate();
  assert.equal(h.latest('web_app_setup_main_button').text,'确认并阅办知会');
  h.button.dataset.agentbridgeActionLabel='姓名与业务正文';h.mutate();
  assert.equal(h.latest('web_app_setup_main_button').is_visible,false);
  h.receive('main_button_pressed');assert.equal(h.clicks(),0);
  assert.ok(!JSON.stringify(h.posts).includes('姓名与业务正文'));
});

test('native visibility restoration requests current presentation and never submits in background', () => {
  const h=fixture({disabled:false,dirty:true});let restored=0;
  h.document.addEventListener('agentbridge:native-resume',()=>restored++);
  h.receive('visibility_changed',{is_visible:false});
  assert.equal(h.latest('web_app_setup_main_button').is_active,false);
  h.receive('main_button_pressed');h.receive('back_button_pressed');
  assert.equal(h.clicks(),0);assert.equal(h.latest('web_app_close'),undefined);
  const requested=h.posts.filter(p=>p.name==='web_app_request_theme').length;
  h.receive('visibility_changed',{is_visible:true});
  assert.equal(restored,1);assert.equal(h.posts.filter(p=>p.name==='web_app_request_theme').length,requested+1);
  assert.deepEqual(h.latest('web_app_setup_closing_behavior'),{need_confirmation:true});
  assert.equal(h.clicks(),0);
  h.receive('visibility_changed',{is_visible:true});assert.equal(restored,1);
  h.events.pagehide();h.events.pageshow();
  assert.deepEqual(h.latest('web_app_setup_closing_behavior'),{need_confirmation:true});
});

test('native back delegates to the trusted in-page return control, without reading its destination', () => {
  const h=fixture({dirty:true});let returns=0;
  const original=h.document.getElementById;
  const target={hidden:false,disabled:false,click:()=>returns++};
  Object.defineProperty(target,'href',{get(){throw Error('native must not inspect bearer URLs');}});
  h.document.getElementById=id=>id==='agentbridge-back'?target:original(id);
  h.receive('back_button_pressed');assert.equal(returns,1);assert.equal(h.latest('web_app_close'),undefined);
  target.disabled=true;h.receive('back_button_pressed');assert.equal(returns,1);
});

test('theme foreground and background pairs remain readable for dark, light, and incomplete custom themes', () => {
  const lum=hex=>[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)/255)
    .map(n=>n<=.04045?n/12.92:((n+.055)/1.055)**2.4).reduce((s,n,i)=>s+n*[.2126,.7152,.0722][i],0);
  const ratio=(a,b)=>(Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05);
  const h=fixture();
  for(const theme of [{bg_color:'#101820',secondary_bg_color:'#202b35',text_color:'#eeeeee',button_color:'#e8dc8c',button_text_color:'#ffffff'},
    {bg_color:'#ffffff',text_color:'#111111',button_color:'#065ca0',button_text_color:'#001122'}, {bg_color:'#111111'},
    {bg_color:'#000000',section_bg_color:'#ffffff',text_color:'#bbbbbb'},
    {bg_color:'#ffffff',secondary_bg_color:'#111111',text_color:'#111111'}]) {
    h.receive('theme_changed',{theme_params:theme});
    for(const [fg,bg] of [['ink','paper'],['ink','surface'],['field-ink','field-bg'],['button-ink','teal'],['selected-ink','selected-bg'],['warning-ink','warning-bg'],['error-ink','error-bg'],['disabled-ink','disabled-bg']]) {
      assert.ok(ratio(h.properties[`--${fg}`],h.properties[`--${bg}`])>=4.5,`${fg}/${bg} must be readable`);
    }
  }
});
