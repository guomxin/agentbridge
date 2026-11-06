import assert from 'node:assert/strict';
import test from 'node:test';
import {renderEvidence, reviewDifferences, reviewFocus, reviewSourceAliases, reviewSourceFields} from '../agentbridge/auth/static/business_draft.mjs';
import {createDocument, descendants} from './workspace_dom_fixture.mjs';

function render(review, kind) {
  const document = createDocument(), target = document.createElement('div');
  target.querySelectorAll = selector => selector === 'details' ? descendants(target).filter(node => node.tag === 'details') : [];
  renderEvidence(document, target, review, kind);
  return {document,target,nodes:descendants(target)};
}

function visibleOccurrences(nodes, value) {
  return nodes.filter(node => {
    if (!node._text.includes(value)) return false;
    for (let parent = node; parent; parent = parent.parentNode) {
      if (parent.hidden || (parent.tag === 'details' && !parent.open && node.tag !== 'summary')) return false;
    }
    return true;
  }).length;
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
  assert.equal(h.nodes.filter(n=>n.className==='review-missing').length,1);
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
  assert.deepEqual(source.map(field=>field.value),['001550.00','1551.00','0','原结算实体']);
  assert.equal(visibleOccurrences(h.nodes,'0000012345678'),0);
  assert.ok(h.nodes.some(n=>n.tag==='dd' && n.textContent==='0000012345678'));
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
  const review={fields:[{label:'应付金额合计',value:'155.00'}],sections:[{id:'settlement-information',kind:'facts',title:'原字段',fields:[
    {label:'应付金额合计',value:'155.00'},{label:'应付金额合计',value:'156.00'}]}]};
  const focus=reviewFocus(review,'business_settlement').flatMap(group=>group.fields);
  assert.deepEqual(focus.map(field=>field.value),['155.00','156.00']);
  assert.equal(focus[0].sourceAnchor,'review-section-0-field-0');
  const h=render(review,'business_settlement');
  assert.equal(visibleOccurrences(h.nodes,'155.00'),1);
  assert.equal(visibleOccurrences(h.nodes,'156.00'),1);
  assert.equal(h.nodes.find(n=>n.id==='review-section-0').open,false);
  assert.equal(h.nodes.find(n=>n.id==='review-section-0-field-0').textContent,'应付金额合计155.00');
  assert.equal(h.nodes.find(n=>n.id==='review-section-0-field-1').textContent,'应付金额合计156.00');
});

test('the two settlement dates, a zero document attachment count and repeated line vouchers keep their scopes', () => {
  const fields=[
    {label:'事项',value:'演示结算'}, {label:'发起人',value:'业务人员'}, {label:'日期',value:'11 月 6 日'},
    {label:'日期',value:'11 月 6 日'}, {label:'附件数量',value:0},
    {label:'费用明细 1 · 电子发票及凭证',value:'两张同名凭证.pdf'},
    {label:'费用明细 2 · 电子发票及凭证',value:'两张同名凭证.pdf'},
  ];
  const review={kind:'business_settlement',title:'演示结算',fields,completeness:{status:'complete'},sections:[
    {id:'settlement-information',kind:'facts',fields:[{label:'日期',value:'11 月 6 日'}]},
    {id:'settlement-expenses',kind:'table',columns:[{key:'doc',label:'电子发票及凭证'}],
      rows:[{id:'first',cells:{doc:'两张同名凭证.pdf'}},{id:'second',cells:{doc:'两张同名凭证.pdf'}}]},
    {id:'settlement-payments',kind:'table',columns:[{key:'p',label:'付款金额'}],rows:[{id:'blank',cells:{p:''}}]},
  ]};
  const before=JSON.stringify(review), h=render(review,review.kind), aliases=reviewSourceAliases(review);
  assert.deepEqual([...aliases],[['review-field-3','review-section-0-field-0'],['review-field-5','review-section-1-row-0'],['review-field-6','review-section-1-row-1']]);
  assert.equal(visibleOccurrences(h.nodes,'演示结算'),1);
  assert.ok(h.nodes.some(n=>n.tag==='dt'&&n.textContent==='待办日期'));
  assert.ok(h.nodes.some(n=>n.tag==='dt'&&n.textContent==='结算单日期'));
  assert.ok(h.nodes.some(n=>n.tag==='dt'&&n.textContent==='页面识别的附件链接数（费用凭证见明细）' && n.parentNode.textContent.endsWith('0')));
  assert.equal(visibleOccurrences(h.nodes,'两张同名凭证.pdf'),2);
  assert.equal(h.nodes.find(n=>n.id==='review-section-1').open,true);
  assert.equal(h.nodes.find(n=>n.id==='review-section-2').open,false);
  assert.ok(h.nodes.find(n=>n.id==='review-section-2').textContent.includes('无已登记付款'));
  assert.ok(h.nodes.some(n=>n.dataset.rowId==='blank'));
  review.sections[2].rows[0].cells.p='0';
  const nonempty=render(review,review.kind);
  assert.equal(nonempty.nodes.find(n=>n.id==='review-section-2').open,true);
  assert.doesNotMatch(nonempty.target.textContent,/无已登记付款/);
  review.sections[2].rows[0].cells.p='';
  assert.equal(JSON.stringify(review),before);
});

test('shared layouts keep one initial reading position for source-only, structured and conflicting facts', () => {
  for (const [kind,label,id] of [['intellectual_property_reward','奖励金额','reward-information'],
    ['travel_expense','应付金额合计','original-fields'],['leave','开始时间','original-fields'],
    ['meeting_room_application','会议室','original-fields']]) {
    for (const sections of [[],[{id,kind:'facts',fields:[{label,value:'唯一展示值'}]}]]) {
      const h=render({fields:[{label,value:'唯一展示值'}],sections},kind);
      assert.equal(visibleOccurrences(h.nodes,'唯一展示值'),1,kind);
      const anchor=sections.length?'review-section-0-field-0':'review-field-0';
      const link=h.nodes.find(n=>n.href==='#'+anchor), destination=h.nodes.find(n=>n.id===anchor);
      assert.ok(link && destination); link.events.click({preventDefault(){}});
      assert.equal(h.document.activeElement,destination);
      assert.equal(visibleOccurrences(h.nodes,'唯一展示值'),2);
    }
  }
});

test('a flattened row with different value or richer reading and ambiguous matches has no source alias', () => {
  const reading={version:1,nodes:[{tag:'strong',children:['字段']}],notices:[]};
  const review={fields:[{label:'费用明细 1 · 发票',value:'不同值'},{label:'合计',value:'字段',reading},{label:'部门',value:'研发'}],sections:[
    {id:'settlement-expenses',kind:'table',columns:[{key:'doc',label:'发票'}],rows:[{id:'r',cells:{doc:'原值'}}]},
    {id:'settlement-information',kind:'facts',fields:[{label:'合计',value:'字段'},{label:'部门',value:'研发'},{label:'部门',value:'研发'}]},
  ]};
  assert.equal(reviewSourceAliases(review,'business_settlement').size,0);
  const h=render(review,'business_settlement');
  assert.ok(h.nodes.some(n=>n.tag==='strong'&&n.textContent==='字段'));
  assert.match(h.target.textContent,/不同值/); assert.match(h.target.textContent,/原值/);
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

test('meeting reply and weekly report compare a single changed source field across their known projections', () => {
  for (const [kind, id, label] of [['meeting_reply','meeting','当前回执'],['weekly_report','current-week','本周说明']]) {
    const review = value => {
      const fields=[{label,value}];
      const sections=[{id,title:'专用材料',kind:'facts',fields}];
      if (kind==='weekly_report') sections.push({id:'original-fields',title:'原始表单',kind:'facts',fields});
      return {kind,fields,sections};
    };
    const h=render(review('新值'),kind);
    const source=h.nodes.find(n=>n.href==='#review-section-0-field-0');
    assert.ok(source); source.events.click({preventDefault(){}});
    assert.equal(h.document.activeElement.id,'review-section-0-field-0');
    assert.deepEqual(reviewDifferences(review('旧值'),review('新值')),{total:1,items:[
      {label:'专用材料 · '+label,before:'旧值',after:'新值'}]});
  }
});

test('a changed-detail notice keeps both dates scoped; the tracked form remark and its full-page row report once', () => {
  const notice={label:'详情变化',value:'填写意见后单据内容发生变化，请按当前详情重新核对本次授权。'};
  const review=(remark,changed=false,bodyTail='其他正文原样')=>{
    const fields=[{label:'日期',value:'业务日期'},{label:'备注',value:remark}];
    return {kind:'business_settlement',reviewChanged:changed,materialsVersion:'1',
      completeness:{status:'complete',scope:'rendered_detail'},
      fields:[...(changed?[notice]:[]),{label:'事项',value:'合成结算'},{label:'发起人',value:'合成人员'},
        {label:'日期',value:'待办日期值'},...fields],
      sections:[{id:'settlement-information',title:'业务结算信息',kind:'facts',fields},
        {id:'original-fields',title:'原始表单',kind:'facts',fields},
        {id:'body',title:'正文与页面材料',kind:'text',text:`来源正文\n备注 ${remark}\n${bodyTail}`} ]};
  };
  const before=review('旧备注'), after=review('新备注',true);
  const h=render(after,after.kind);
  assert.doesNotMatch(h.target.textContent,/详情变化/);
  assert.ok(h.nodes.some(n=>n.tag==='dt' && n.textContent==='待办日期' && n.parentNode.textContent.endsWith('待办日期值')));
  assert.ok(h.nodes.some(n=>n.tag==='dt' && n.textContent==='结算单日期' && n.parentNode.textContent.endsWith('业务日期')));
  assert.deepEqual(reviewDifferences(before,after),{total:1,items:[
    {label:'业务结算信息 · 备注',before:'旧备注',after:'新备注'}]});
  assert.deepEqual(reviewDifferences(before,review('新备注',true,'另一项正文变化')).items.map(i=>i.label),['业务结算信息 · 备注','正文与页面材料']);
  after.fields[3]={label:'日期',value:'另一待办日期'};
  assert.deepEqual(reviewDifferences(before,after).items.map(i=>i.label),['待办日期','业务结算信息 · 备注']);
  const independent={kind:'business_settlement',fields:[{label:'详情变化',value:'业务提供的旧值'}]};
  assert.equal(reviewDifferences(independent,{...independent,fields:[{label:'详情变化',value:'业务提供的新值'}]}).items[0].label,'详情变化');
});
