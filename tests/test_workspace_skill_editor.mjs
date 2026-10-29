import test from 'node:test';
import assert from 'node:assert/strict';
import { createSkillConfigurationEditor, configurationProposal, skillConfiguration } from '../agentbridge/workspace/static/workspace_skill_editor.mjs';

function fixture(bundle, catalog={}) {
  class Node {
    children=[];dataset={};value='';checked=false;disabled=false;textContent='';
    append(...nodes){for(const child of nodes){child.parent=this;this.children.push(child);}}
    replaceChildren(...nodes){this.children=[];this.append(...nodes);}
    remove(){this.parent.children=this.parent.children.filter(child=>child!==this);}
    setAttribute(){}
  }
  const document={createElement:tag=>Object.assign(new Node(),{tagName:tag})},root=new Node();let changes=0;
  const editor=createSkillConfigurationEditor({document,root,bundle,catalog,onChange:()=>changes++});
  const walk=node=>[node,...node.children.flatMap(walk)];
  return {editor,root,nodes:()=>walk(root),field:(title,index=0)=>walk(root).filter(node=>node.dataset.configField===title)[index],
    button:title=>walk(root).find(node=>node.tagName==='button'&&node.textContent===title),changes:()=>changes};
}
const legacy=()=>({manifest:{executionMode:'durable_plan',profiles:{preview:{},fill:{all:['write']}},
  required_resources:{preview:['references/source.md'],fill:[]}},
  resources:{'SKILL.md':'主说明','references/source.md':'这是参考原文。'}});
const catalog={business:[{id:'write',label:'准备日志',capabilities:['log.prepare']}],database:[{name:'database.logs.query',description:'读取日志'}],
  capabilities:[{name:'log.prepare',description:'准备日志',effect:'controlled_write',batch:false}]};

test('configuration preserves old execution omission and profile-specific required resources',()=>{
  const bundle=legacy(),copy=structuredClone(bundle),f=fixture(bundle,catalog);
  const value=configurationProposal(f.editor.read());
  assert.equal(value.executionMode,'durable_plan');assert.equal(value.profiles.preview.execution,undefined);
  assert.deepEqual(value.required_resources,{preview:['references/source.md'],fill:[]});
  assert.deepEqual(value.references,{'references/source.md':'这是参考原文。'});
  assert.equal(f.changes(),0);assert.deepEqual(bundle,copy);
  delete bundle.manifest.required_resources;
  assert.deepEqual(skillConfiguration(bundle).profiles.map(profile=>profile.required),[['references/source.md'],['references/source.md']]);
});

test('reference renaming keeps exact mode associations and deletion removes those associations',()=>{
  const f=fixture(legacy(),catalog),path=f.field('资料路径');
  path.value='references/说明.md';path.oninput();
  let value=configurationProposal(f.editor.read());
  assert.deepEqual(value.required_resources,{preview:['references/说明.md'],fill:[]});
  f.button('删除资料').onclick();value=configurationProposal(f.editor.read());
  assert.deepEqual(value.references,{});assert.deepEqual(value.required_resources,{preview:[],fill:[]});
  f.button('添加参考资料').onclick();f.field('资料路径').value='references/new.md';f.field('资料内容').value='新的长文\n'.repeat(100);
  value=configurationProposal(f.editor.read());
  assert.deepEqual(value.required_resources,{preview:[],fill:[]},'new resources are optional until a mode explicitly selects them');
  assert.equal(value.references['references/new.md'].length,500);
});

test('new profiles have explicit read-only execution and can be removed without changing other modes',()=>{
  const f=fixture(legacy(),catalog);
  f.button('添加模式').onclick();f.field('模式标识',2).value='summary';f.field('模式说明',2).value='只归纳文字';
  let value=configurationProposal(f.editor.read());
  assert.deepEqual(value.profiles.summary.execution,{effects:['read'],allow_batch:false});
  assert.equal(value.profiles.summary.description,'只归纳文字');assert.deepEqual(value.required_resources.summary,[]);
  f.nodes().filter(node=>node.tagName==='button'&&node.textContent==='删除模式')[2].onclick();
  value=configurationProposal(f.editor.read());assert.deepEqual(Object.keys(value.profiles),['preview','fill']);
});

test('explicit capability restriction preserves an intentionally empty set and disabled values',()=>{
  const f=fixture(legacy(),catalog);
  f.field('明确设置执行范围').checked=true;f.field('明确设置执行范围').onchange();
  f.field('明确选择可用能力').checked=true;f.field('明确选择可用能力').onchange();
  f.field('需要数据库来源').checked=true;f.field('需要数据库来源').onchange();
  const before=f.editor.read();f.editor.setDisabled(true);
  assert.deepEqual(f.editor.read(),before);assert.ok(f.nodes().filter(node=>['input','select','textarea','button'].includes(node.tagName)).every(node=>node.disabled));
  f.editor.setDisabled(false);
  const value=configurationProposal(f.editor.read());
  assert.deepEqual(value.profiles.preview.execution.capabilities,[]);
  assert.deepEqual(value.profiles.preview.database,{all:[],any:[]},'an explicit source requirement is not the same as no database requirement');
  assert.equal(f.field('允许批量办理',1).disabled,true,'legacy profile controls remain inactive');
});

test('invalid names, paths and effect mismatches remain editable but cannot be saved',()=>{
  const value=skillConfiguration(legacy());
  for(const path of ['../private.md','references/../private.md','references/a.js','references//a.md']){
    const copy=structuredClone(value);copy.references[0].path=path;assert.throws(()=>configurationProposal(copy),/参考资料路径/);
  }
  const duplicate=structuredClone(value);duplicate.profiles[1].id='preview';assert.throws(()=>configurationProposal(duplicate),/模式标识不能重复/);
  const effects=structuredClone(value);effects.executionMode='read_exploration';effects.profiles[0].execution={effects:['controlled_write'],allow_batch:false};
  assert.throws(()=>configurationProposal(effects),/只能选择读取效果/);
  effects.profiles[0].execution.effects=[];assert.throws(()=>configurationProposal(effects),/至少一种/);
  const missing=structuredClone(value);missing.references=[];assert.throws(()=>configurationProposal(missing),/必读资料不存在/);
});

test('unknown existing dependencies and unsafe display text are preserved as data',()=>{
  const bundle=legacy();bundle.manifest.profiles.preview.all=['unknown.permission'];bundle.manifest.profiles.preview.description='<img onerror=alert(1)>';
  const f=fixture(bundle,catalog),value=configurationProposal(f.editor.read());
  assert.deepEqual(value.profiles.preview.all,['unknown.permission'],'server validation must report an unknown dependency instead of silently dropping it');
  assert.equal(value.profiles.preview.description,'<img onerror=alert(1)>');
  assert.ok(f.nodes().every(node=>node.innerHTML===undefined));
});
