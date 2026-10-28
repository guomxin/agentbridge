import test from 'node:test';
import assert from 'node:assert/strict';
import {skillReportHtml,comparisonText} from '../agentbridge/workspace/static/workspace_skill_report.mjs';
const report=()=>({job_id:'job',revision:3,name:'周报',passed:true,scores:{candidate:{passed:2,total:2}},cases:[{prompt:'完成联调',profile:'use'},{prompt:'明天排查',profile:'use'}],rows:[{case:0,repeat:0,key:'0:0:candidate',variant:'candidate',output:'完成联调',passed:true},{case:1,repeat:0,key:'0:1:candidate',variant:'candidate',output:'明天排查',passed:true},{case:1,repeat:0,key:'0:1:published',variant:'published',output:'旧回答',passed:true}]});
test('groups matching outputs by case, prioritizes regression without changing automatic score',()=>{
 const r=report();r.reviews={'0:0:candidate':{status:'problem',note:'<script>bad</script>',version:1}};
 const html=skillReportHtml(r,{source_revision:2,inputs_equal:true,models:'known_same',regressed_cases:[1]},true);
 assert.ok(html.indexOf('第 2 题')<html.indexOf('第 1 题'));
 assert.match(html,/第 2 题[\s\S]*明天排查[\s\S]*旧回答[\s\S]*第 1 题/);
 assert.match(html,/所列断言通过/);assert.match(html,/1 项有问题，2 项未复核/);
 assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
 assert.match(html,/data-review-row="0:1:candidate"/);assert.match(html,/历史线上对照（版本未记录）/);
});
test('unknown or changed model cannot be described as same conditions',()=>{
 for(const models of ['changed','unknown']){
 const text=comparisonText({inputs_equal:true,models,source_revision:1});
 assert.match(text,models==='changed'?/模型已变化/:/无法确认可比性/);assert.doesNotMatch(text,/同题同条件/);
 }
 assert.match(comparisonText({inputs_equal:false}),/分数不直接比较/);
 assert.match(comparisonText({same_report:true}),/尚无新的对照/);
});
test('new job does not inherit review and historical job reports stay read only',()=>{
 const r=report(); const html=skillReportHtml(r);
 assert.match(html,/0 项有问题，3 项未复核/);assert.doesNotMatch(html,/<form/);
 assert.match(skillReportHtml(null),/尚无独立评测/);
});
