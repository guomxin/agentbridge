import test from 'node:test';
import assert from 'node:assert/strict';
import {createDailyDigestSettings} from '../agentbridge/workspace/static/workspace_daily_digest.mjs';
import {createLifecycle} from '../agentbridge/workspace/static/workspace_lifecycle.mjs';
import {createDocument} from './workspace_dom_fixture.mjs';

function settings(extra = {}) {
  return {enabled:false,time:'08:30',workspace:true,telegramEndpointId:null,telegramAvailable:false,
    version:0,nextRunAt:null,lastRunAt:null,lastStatus:null,
    telegramOptions:[{id:'my-chat',label:'本人私聊'}],canConfigure:true,...extra};
}

function harness(transport) {
  const lifecycle = createLifecycle(), document = createDocument(), nodes = new Map(), calls = [];
  let scope = lifecycle.scope();
  const $ = selector => {
    if (!nodes.has(selector)) nodes.set(selector, document.createElement('div'));
    return nodes.get(selector);
  };
  const field = name => $(`#oa-digest-${name}`);
  const controller = createDailyDigestSettings({document,$,getScope:()=>scope,request:(path,requestScope,options) => {
    const call = {path,scope:requestScope,...options};
    calls.push(call);
    return transport(call);
  }});
  return {controller, lifecycle, field, calls,
    edit(name,value) {
      field(name)[typeof value === 'boolean' ? 'checked' : 'value'] = value;
      field('form').events.input();
    },
    submit:()=>field('form').events.submit({preventDefault(){}}),
    reread:()=>field('reload').events.click(),
    replaceAccount() { scope.dispose(); controller.reset(); scope = lifecycle.scope(); },
  };
}

test('enabling selects both channels, saves once with CSRF and reads back Beijing time', async () => {
  let resolve;
  const h = harness(call => call.method ? new Promise(done => {resolve = done;}) : settings());
  await h.controller.activate();
  assert.equal(h.field('save').disabled,true);
  assert.equal(h.field('telegram').children.length,2);
  h.edit('enabled',true);
  h.edit('telegram','my-chat');
  const saving = h.submit();
  await h.submit();
  assert.equal(h.calls.length,2);
  assert.equal(h.calls[1].path,'/api/oa-daily-digest');
  assert.equal(h.calls[1].csrf,true);
  assert.deepEqual(h.calls[1].body,{enabled:true,time:'08:30',workspace:true,telegramEndpointId:'my-chat',expectedVersion:0});
  resolve(settings({enabled:true,telegramEndpointId:'my-chat',telegramAvailable:true,version:1,
    nextRunAt:'2026-11-13T00:30:00Z',lastRunAt:'2026-11-12T01:30:00Z',lastStatus:'published'}));
  await saving;
  assert.match(h.field('summary').textContent,/已开启.*08:30/);
  assert.match(h.field('runs').textContent,/下次发送.*08:30.*北京时间.*上次执行.*09:30.*已生成简报/);
  assert.equal(h.field('status').textContent,'简报设置已保存。');
  h.lifecycle.dispose();
});

test('a failed save preserves edited time and explicitly rereads before rebasing on remote channels',async()=>{
  let remote = settings({enabled:true,version:1});
  const h = harness(call => {
    if (!call.method) return remote;
    if (call.body.expectedVersion === 1) {
      remote = settings({enabled:true,version:2,workspace:false,telegramEndpointId:'my-chat',telegramAvailable:true});
      throw Object.assign(new Error('conflict'),{status:409});
    }
    return {...remote,...call.body,version:3};
  });
  await h.controller.activate();
  h.edit('time','17:45');
  await h.submit();
  assert.equal(h.field('time').value,'17:45');
  assert.equal(h.field('save').disabled,true);
  assert.equal(h.field('reload').hidden,false);
  await h.controller.refresh();
  assert.equal(h.calls.length,2,'background focus refresh does not silently rebase a conflicted draft');
  await h.reread();
  assert.equal(h.field('time').value,'17:45');
  assert.equal(h.field('workspace').checked,false);
  assert.equal(h.field('telegram').value,'my-chat');
  assert.equal(h.calls.filter(call=>call.method).length,1,'reading settings never resubmits a change');
  await h.submit();
  assert.equal(h.calls.at(-1).body.expectedVersion,2);
  h.lifecycle.dispose();
});

test('a stale Telegram selection must be addressed explicitly while stopping preserves it',async()=>{
  const remote = settings({enabled:true,version:4,telegramEndpointId:'my-chat',telegramAvailable:false});
  const h = harness(call=>call.method ? {...remote,enabled:false,version:5} : remote);
  await h.controller.activate();
  assert.match(h.field('telegram-note').textContent,/不可用/);
  h.edit('time','18:00');
  await h.submit();
  assert.equal(h.calls.length,1);
  assert.match(h.field('status').textContent,/重新选择/);
  h.edit('enabled',false);
  await h.submit();
  assert.deepEqual(h.calls.at(-1).body,{enabled:false,time:'18:00',workspace:true,telegramEndpointId:'my-chat',expectedVersion:4});
  assert.match(h.field('summary').textContent,/已停用/);
  h.lifecycle.dispose();
});

test('an account without a Telegram binding can enable only Workspace and must pick a channel',async()=>{
  const remote = settings({workspace:false,telegramOptions:[]});
  const h = harness(call=>call.method ? {...remote,...call.body,version:1} : remote);
  await h.controller.activate();
  assert.equal(h.field('telegram').disabled,true);
  assert.match(h.field('telegram-note').textContent,/没有已绑定/);
  h.edit('enabled',true);
  await h.submit();
  assert.equal(h.calls.length,1);
  assert.match(h.field('status').textContent,/至少选择一个接收渠道/);
  h.edit('workspace',true);
  await h.submit();
  assert.deepEqual(h.calls.at(-1).body,{enabled:true,time:'08:30',workspace:true,telegramEndpointId:null,expectedVersion:0});
  h.lifecycle.dispose();
});

test('revoked OA permission leaves only stopping available, and account replacement discards old writes',async()=>{
  const revoked = settings({enabled:true,version:3,canConfigure:false,telegramOptions:[]});
  let resolve;
  let remote = revoked;
  const h = harness(call=>call.method ? new Promise(done=>{resolve=done;}) : remote);
  await h.controller.activate();
  assert.equal(h.field('time').disabled,true);
  assert.equal(h.field('workspace').disabled,true);
  assert.equal(h.field('enabled').disabled,false);
  h.edit('enabled',false);
  const stopping = h.submit();
  assert.equal(h.calls.at(-1).body.expectedVersion,3);
  assert.equal(h.calls.at(-1).body.enabled,false);
  h.replaceAccount();
  assert.equal(h.field('settings').open,false);
  remote = settings({time:'12:10',telegramOptions:[]});
  await h.controller.activate();
  resolve({...revoked,enabled:false,version:4});
  await stopping;
  assert.equal(h.field('time').value,'12:10');
  assert.doesNotMatch(h.field('status').textContent,/保存/);
  assert.equal(h.field('telegram').children.length,1);
  h.lifecycle.dispose();
});
