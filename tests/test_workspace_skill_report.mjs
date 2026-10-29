import test from 'node:test';
import assert from 'node:assert/strict';
import {skillReportHtml,skillProgressHtml,comparisonText} from '../agentbridge/workspace/static/workspace_skill_report.mjs';
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

test('progress distinguishes processed rows, recorded calls and complete estimated usage',()=>{
 const stats={completed:3,total:6,model_calls:2,usage:{input:1000,output:500,totalTokens:1500,cost_estimate_usd:0.01234},usage_calls:2,priced_calls:2,usage_complete:true,cost_complete:true};
 const html=skillProgressHtml(stats);
 assert.match(html,/完成 3\/6 项 · 已记录 2 次模型调用/);
 assert.match(html,/Token 1,500（输入 1,000，输出 500）/);
 assert.match(html,/估算 \$0\.01234（非账单）/);
 assert.doesNotMatch(html,/部分记录/);
 const r={...report(),progress_stats:stats};
 assert.match(skillReportHtml(r),/完成 3\/6 项/);
 assert.match(skillReportHtml(r),/所列断言通过/);
});

test('missing and partial usage never become zero billing',()=>{
 assert.equal(skillProgressHtml(null),'');
 const stats={completed:1,total:4,model_calls:1,usage:null,usage_calls:0,priced_calls:0,usage_complete:false,cost_complete:false};
 const missing=skillProgressHtml(stats);
 assert.match(missing,/Token 用量未返回/);assert.match(missing,/费用估算未返回/);
 assert.doesNotMatch(missing,/\$|Token 0/);
 const partial=skillProgressHtml({...stats,model_calls:3,usage:{input:30,output:10,totalTokens:40,cost_estimate_usd:0.001},usage_calls:1,priced_calls:1});
 assert.match(partial,/部分记录：用量仅含已返回的 1 次调用；费用仅含已估价的 1 次调用/);
 const incompleteTotal=skillProgressHtml({...stats,usage:{input:30,output:null,totalTokens:null}});
 assert.match(incompleteTotal,/Token 输入 30（合计未返回）/);
 const knownZero=skillProgressHtml({...stats,usage:{input:0,output:0,totalTokens:0,cost_estimate_usd:0},usage_complete:true,cost_complete:true});
 assert.match(knownZero,/Token 0（输入 0，输出 0）/);assert.match(knownZero,/估算 \$0\.0000（非账单）/);
 const interrupted=skillProgressHtml({...stats,unrecorded_calls_possible:true});
 assert.match(interrupted,/可能有未计入的模型调用/);
 assert.doesNotMatch(interrupted,/\$/);
});

test('generation without a fixed total does not imply that a successful one-call job is unfinished',()=>{
 const html=skillProgressHtml({completed:1,total:null,model_calls:1,usage:null},{kind:'generation'});
 assert.match(html,/已记录 1 次模型调用/);
 assert.doesNotMatch(html,/完成|最多|未记录 次/);
});

test('malformed numeric statistics cannot inject markup or fabricated amounts',()=>{
 const html=skillProgressHtml({completed:'<img>',total:Infinity,model_calls:-1,usage:{input:'10',output:NaN,totalTokens:-2,cost_estimate_usd:Infinity}});
 assert.doesNotMatch(html,/<img>|Infinity|NaN|\$|Token -2/);
 assert.match(html,/完成 未记录\/未记录 项/);
 assert.match(html,/费用估算未返回/);
});
