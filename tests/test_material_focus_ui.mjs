import assert from 'node:assert/strict';
import test from 'node:test';
import {renderEvidence, reviewFocus, reviewSourceFields} from '../agentbridge/auth/static/business_draft.mjs';
import {createDocument, descendants} from './workspace_dom_fixture.mjs';

function render(review, kind) {
  const document = createDocument(), target = document.createElement('div');
  target.querySelectorAll = selector => selector === 'details' ? descendants(target).filter(node => node.tag === 'details') : [];
  renderEvidence(document, target, review, kind);
  return {document,target,nodes:descendants(target)};
}

test('efficiency focus never infers department period or numeric facts from a title or image', () => {
  const review = {title:'研发中心 2026年第40周 达成率99.9%', fields:[{label:'事项',value:'研发中心40周'}],
    sections:[{id:'body',kind:'text',text:'请见图中的指标99.9%'}],
    materialPreviews:[{id:'a'.repeat(64),kind:'image',state:'available',name:'部门指标99.9%.png'}]};
  const snapshot = JSON.stringify(review), focus = reviewFocus(review,'efficiency_data');
  assert.ok(focus.every(group => group.missing && !group.fields.length));
  const h = render(review,'efficiency_data');
  assert.equal(h.nodes.find(n=>n.className==='review-focus').textContent.includes('99.9%'),false);
  assert.match(h.target.textContent,/原材料未提供独立字段/);
  assert.equal(h.nodes.find(n=>n.href?.startsWith('#material-preview-')).href,`#material-preview-${'a'.repeat(64)}`);
  assert.equal(JSON.stringify(review),snapshot);
});

test('exact source values preserve zeros, money precision, conflicting duplicates and original time strings', () => {
  const review = {fields:[{label:'应付金额合计',value:'001550.00'},{label:'应付金额合计',value:'1551.00'},
    {label:'收款账号',value:'0000012345678'}, {label:'费用归属部门',value:0}],
    sections:[{kind:'facts',fields:[{label:'结算实体',value:'原结算实体'}]},
      {kind:'table',title:'原账目',columns:[{key:'amount',label:'金额'}],rows:[{id:'a',cells:{amount:'100.00'}},{id:'a',cells:{amount:'100.00'}}]}]};
  const h=render(review,'business_settlement');
  const source=reviewFocus(review,'business_settlement').flatMap(group=>group.fields);
  assert.deepEqual(source.map(field=>field.value),['001550.00','1551.00','0','原结算实体','0000012345678']);
  assert.deepEqual(h.nodes.filter(n=>n.tag==='tr' && n.dataset.rowId).map(n=>n.textContent),['100.00','100.00']);
  assert.doesNotMatch(h.target.textContent,/200\.00/);
  const travel=reviewFocus({fields:[{label:'出差开始时间',value:'2026-10-24 09:00 +08:00'},{label:'出差小时数',value:'0.00 小时'}]},'business_trip');
  assert.deepEqual(travel.flatMap(group=>group.fields).map(field=>field.value),['2026-10-24 09:00 +08:00','0.00 小时']);
});

test('focus link opens original collapsed material and moves keyboard focus without making a business control', () => {
  const h=render({sections:[{id:'original-fields',title:'原始字段',kind:'facts',fields:[null,{label:'奖励金额',value:'1000.00'}]}]},'intellectual_property_reward');
  const link=h.nodes.find(n=>n.href==='#review-section-0-field-1');
  const destination=h.nodes.find(n=>n.id==='review-section-0-field-1');
  const detail=h.nodes.find(n=>n.id==='review-section-0');
  assert.ok(link && destination); assert.equal(detail.open,false);
  let prevented=false,scrolled=false; destination.scrollIntoView=()=>{scrolled=true;};
  link.events.click({preventDefault(){prevented=true;}});
  assert.equal(detail.open,true); assert.equal(h.document.activeElement,destination);
  assert.ok(prevented && scrolled);
  assert.ok(!h.nodes.some(n=>['input','select','button','form'].includes(n.tag)));
});

test('directory links address the actual inert source tables and never a source URL', () => {
  const h=render({sections:[{kind:'text',id:'body',title:'正文',text:'原表',reading:{version:1,notices:[],nodes:[
    {tag:'table',children:[{tag:'tr',children:[{tag:'td',children:['100.00']}]}]},
    {tag:'table',children:[{tag:'tr',children:[{tag:'td',children:['不同表 99.00']}]}]},
  ]}}],materialPreviews:[{id:'https://evil.example',kind:'image',state:'available',name:'bad'},
    {id:'b'.repeat(64),kind:'pdf',state:'unavailable',name:'<img onerror=x>.pdf'}]},'efficiency_data');
  const links=h.nodes.filter(n=>n.tag==='a');
  assert.ok(links.every(n=>/^#(?:review-|material-preview-)/.test(n.href)));
  for(const n of links.filter(n=>n.href.includes('-table-')))assert.ok(h.nodes.some(t=>t.id===n.href.slice(1)&&t.className==='reading-table'&&t.tabIndex===0));
  assert.equal(links.filter(n=>n.href.includes('-table-')).length,2);
  assert.ok(!h.nodes.some(n=>['script','img','iframe'].includes(n.tag)));
  assert.match(h.target.textContent,/暂不支持预览/);
});

test('long source facts have full original anchors and rerenders cannot retain a previous value', () => {
  const long='长说明'.repeat(100), review={fields:[{label:'日志内容',value:long}]};
  const h=render(review,'work_log');
  const full=h.nodes.find(n=>n.id==='review-field-0');
  assert.equal(full.textContent,'日志内容'+long);
  assert.ok(h.nodes.some(n=>n.href==='#review-field-0'));
  renderEvidence(h.document,h.target,{fields:[{label:'日志内容',value:'新的原文字段'}]},'work_log');
  assert.doesNotMatch(h.target.textContent,/长说明/);
  assert.match(h.target.textContent,/新的原文字段/);
});

test('source index keeps original positions through empty or invalid entries and duplicate group names', () => {
  const result=reviewSourceFields({fields:[null,{label:'周期',value:'10月'}],groups:[{fields:[{label:'部门',value:'甲'}]},
    {fields:[{label:'部门',value:'乙'}]}],sections:[{kind:'facts',fields:[null,{label:'指标值',value:'0'}]}]});
  assert.deepEqual(result.map(f=>f.sourceAnchor),['review-field-1','review-group-0-field-0','review-group-1-field-0','review-section-0-field-1']);
});

test('summary repeats an identical fact once while preserving conflicting facts and original rows', () => {
  const review={fields:[{label:'应付金额合计',value:'155.00'}],sections:[{kind:'facts',title:'原字段',fields:[
    {label:'应付金额合计',value:'155.00'},{label:'应付金额合计',value:'156.00'}]}]};
  const focus=reviewFocus(review,'business_settlement').flatMap(group=>group.fields);
  assert.deepEqual(focus.map(field=>field.value),['155.00','156.00']);
  assert.equal(focus[0].sourceAnchor,'review-field-0');
  const h=render(review,'business_settlement');
  assert.equal(h.nodes.find(n=>n.id==='review-section-0-field-0').textContent,'应付金额合计155.00');
  assert.equal(h.nodes.find(n=>n.id==='review-section-0-field-1').textContent,'应付金额合计156.00');
});

test('row locator preserves complete original tables and selects the precise source row', () => {
  const rows=Array.from({length:201},(_,i)=>({id:`source-${i}`,cells:{person:`原人员${i}`,result:i===17?'原文：待补材料':'原记录'}}));
  const h=render({sections:[{id:'detail',kind:'table',title:'原记录',columns:[{key:'person',label:'人员'},{key:'result',label:'原结论'}],rows}]},'quarterly_performance');
  const link=h.nodes.find(n=>n.href==='#review-section-0-row-17');
  const row=h.nodes.find(n=>n.id==='review-section-0-row-17');
  assert.match(link.textContent,/原文：待补材料/);
  link.events.click({preventDefault(){}});
  assert.equal(h.document.activeElement,row);
  assert.equal(h.nodes.filter(n=>n.tag==='tr'&&n.dataset.rowId).length,201);
  assert.equal(h.nodes.filter(n=>n.href?.includes('-row-')).length,200);
  assert.match(h.target.textContent,/下方原表保留全部已读取行/);
  assert.equal(rows[17].cells.result,'原文：待补材料');
});
