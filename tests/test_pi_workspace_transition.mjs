import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createComposer} from '../agentbridge/workspace/static/workspace_forms.mjs';

const source = readFileSync(new URL('../agentbridge/workspace/static/workspace.js', import.meta.url), 'utf8');
function handler(name, next, bindings) {
  const start = source.indexOf(`async function ${name}(`);
  const end = source.indexOf(`async function ${next}(`, start + 1);
  assert.ok(start >= 0 && end > start);
  return new Function(...Object.keys(bindings), source.slice(start, end) + `\nreturn ${name}`)(...Object.values(bindings));
}
function deferred() {
  let resolve;
  const promise = new Promise(done => {resolve = done;});
  return {promise, resolve};
}

test('an immediate submit cannot consume a draft or reach either host while the default changes', async () => {
  const post = deferred(), history = deferred();
  const scope = {current:() => true, assertCurrent() {}}, selection = {value:'pi-office'}, input = {value:'两张原图'}, form = {elements:{message:input}};
  const state = {hostChanging:null, hostChoices:{default_instance_id:'openclaw-default', revision:1}, composerAttachments:[{id:'one'}]};
  const elements = {'#chat-form':form, '#workspace-host':selection};
  let submits=0;
  const {sendChat} = createComposer({state, executeChatMessage:() => {submits++;}});
  const change = handler('changeDefaultHost','reloadHostWorkspace',{state, sessionScope:scope, $:id => elements[id],
    scopedApi:() => () => post.promise, reloadHostWorkspace:async () => {
      await history.promise;
      state.hostChoices={default_instance_id:'pi-office', revision:2}; return true;
    }, showHostChoices:() => {form.inert=false;}, toast() {}, friendlyError() {}});
  const switching=change();
  assert.equal(form.inert,true);
  await sendChat({preventDefault() {},currentTarget:form});
  post.resolve(); await Promise.resolve();
  await sendChat({preventDefault() {},currentTarget:form});
  assert.equal(submits,0); assert.equal(input.value,'两张原图'); assert.equal(state.composerAttachments.length,1);
  history.resolve(); await switching;
  assert.equal(form.inert,false); assert.equal(state.hostChoices.default_instance_id,'pi-office');
});

test('reloading history preserves edits made after the old view was cleared', async () => {
  const ready=deferred(), input={value:'原草稿'}, scope={current:() => true};
  const state={account:{id:'a'}, hostChoices:{default_instance_id:'pi-office'}, composerAttachments:[{id:'two'}]};
  const reload=handler('reloadHostWorkspace','enterWorkspace',{state, sessionScope:scope, $:() => input,
    enterWorkspace:() => {input.value=''; state.composerAttachments=[]; return ready.promise;},renderComposerAttachments() {}});
  const switching=reload();
  assert.equal(input.value,'原草稿');
  input.value='新编辑草稿'; ready.resolve(); assert.ok(await switching);
  assert.equal(input.value,'新编辑草稿'); assert.deepEqual(state.composerAttachments,[{id:'two'}]);
});
