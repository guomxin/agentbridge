/** Report presentation never changes automatic outcomes or inherits human reviews. */
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const reviewLabels = {unreviewed:'未复核',acceptable:'可接受',problem:'有问题'};
const amount = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const countText = value => amount(value) === null ? '未记录' : value.toLocaleString('zh-CN');
export function skillProgressHtml(stats, {kind='evaluation'} = {}) {
  if (!stats || typeof stats !== 'object') return '';
  const usage=stats.usage || {},tokens=amount(usage.totalTokens),cost=amount(usage.cost_estimate_usd);
  const tokenDetail=[['input','输入'],['output','输出']].filter(([key])=>amount(usage[key])!==null)
    .map(([key,label])=>`${label} ${countText(usage[key])}`).join('，');
  const tokenText=tokens===null?(tokenDetail?`Token ${tokenDetail}（合计未返回）`:'Token 用量未返回'):`Token ${countText(tokens)}${tokenDetail?`（${tokenDetail}）`:''}`;
  const costText=cost===null?'费用估算未返回':`估算 $${cost.toLocaleString('en-US',{minimumFractionDigits:4,maximumFractionDigits:6})}（非账单）`;
  const partial=[];
  if((tokens!==null||tokenDetail)&&stats.usage_complete!==true)partial.push(`用量仅含已返回的 ${countText(stats.usage_calls)} 次调用`);
  if(cost!==null&&stats.cost_complete!==true)partial.push(`费用仅含已估价的 ${countText(stats.priced_calls)} 次调用`);
  const progress=kind==='generation'?`已记录 ${countText(stats.model_calls)} 次模型调用`:
    `完成 ${countText(stats.completed)}/${countText(stats.total)} 项 · 已记录 ${countText(stats.model_calls)} 次模型调用`;
  return `<p class="skill-job-progress" role="status">${progress}</p>
    <p>${tokenText} · ${costText}${partial.length?`；部分记录：${partial.join('；')}`:''}。${stats.unrecorded_calls_possible===true?'可能有未计入的模型调用。':''}</p>`;
}
export function comparisonText(comparison) {
  if (!comparison) return '';
  if (comparison.same_report) return '当前报告就是固定题的来源报告，尚无新的对照。';
  if (!comparison.inputs_equal) return '题目、断言或重复次数已变化，分数不直接比较。';
  const model = {known_same:'候选输出记录的模型一致；不代表全部运行环境一致。',changed:'候选输出模型已变化，结果仅供参考，不判定退步。',unknown:'候选输出模型或结果记录不完整，无法确认可比性。'}[comparison.models];
  return `与固定题来源修订 ${comparison.source_revision} 比较：题目、断言和重复次数一致。${model || ''}`;
}
export function skillReportHtml(report, comparison = null, editable = false) {
  if (!report) return '<p>尚无独立评测。快速试用仅供人工复核。</p>';
  const names={candidate:'候选版本',published:report.baseline?.version ? `评测时线上 ${report.baseline.version}` : '历史线上对照（版本未记录）',without_skill:'不使用助手'};
  const reviews=report.reviews || {};
  const totals = (report.rows || []).reduce((a,r) => {a[reviews[r.key]?.status || 'unreviewed']++;return a;},{unreviewed:0,acceptable:0,problem:0});
  const regressions = new Set(comparison?.regressed_cases || []);
  const groups = new Map();
  for (const row of report.rows || []) {
    const index = row.case ?? groups.size;
    if (!groups.has(index)) groups.set(index, []);
    groups.get(index).push(row);
  }
  const groupRows = [...groups].sort(([a],[b]) => Number(regressions.has(b))-Number(regressions.has(a)) || a-b);
  const result = groupRows.map(([index,rows]) => `<details data-report-case="${escape(index)}" ${regressions.has(index)?'open':''}><summary>${regressions.has(index)?'上次通过 → 本次失败 · ':''}第 ${Number(index)+1} 题 · ${escape(report.cases?.[index]?.prompt || rows[0]?.prompt || '历史样例')}</summary>${rows.map(r=>{
    const review=reviews[r.key] || {status:'unreviewed',note:'',version:0};
    return `<section class="skill-report-output"><p><strong>${escape(names[r.variant] || r.variant)}</strong> · ${escape(report.cases?.[r.case]?.profile || '未记录模式')} · 第 ${Number(r.repeat || 0)+1} 次 · 自动断言${r.passed?'通过':'未通过'}</p><pre>${escape(r.output || r.skipped)}</pre><p>${escape(r.model || '模型未记录')} · ${r.elapsed_ms || 0} 毫秒</p>${(r.checks || []).map(c=>`<p>${c.passed?'✓':'×'} ${c.rule==='contains'?'应包含':c.rule==='excludes'?'不应包含':'适用场景判断'}：${escape(c.value || '')}</p>`).join('')}
      ${editable && report.job_id && r.key && r.output ? `<form data-review-row="${escape(r.key)}" class="draft-editor"><label>人工复核<select name="review_status">${Object.entries(reviewLabels).map(([v,label])=>`<option value="${v}" ${v===review.status?'selected':''}>${label}</option>`).join('')}</select></label><label>复核说明<textarea name="review_note" maxlength="1000" placeholder="例如：明天不能归到下周，不要补效果。">${escape(review.note)}</textarea></label><button type="submit" class="secondary">保存复核</button><p role="status">${escape(reviewLabels[review.status])}${review.version?' · 已保存':''}</p></form>` : `<p>人工复核：${escape(reviewLabels[review.status] || '未复核')}${review.note?' · '+escape(review.note):''}</p>`}</section>`;
  }).join('')}</details>`).join('');
  return `<p>${escape(report.name || '助手')} · 修订 ${escape(report.revision)} · ${escape(report.job_id || '')}</p>${skillProgressHtml(report.progress_stats)}<p><strong>${report.passed?'所列断言通过':'存在未通过断言'}</strong> · 仍需人工检查语义正确性</p><p>${escape(report.limitations)}</p>
    <div class="draft-actions">${Object.entries(report.scores || {}).map(([k,v])=>`<span>${escape(names[k] || k)}：${v.passed}/${v.total}</span>`).join('')}</div>
    <p data-review-summary>人工复核：${totals.acceptable} 项可接受，${totals.problem} 项有问题，${totals.unreviewed} 项未复核。复核只对本次结果有效。</p>
    ${comparison?`<p data-report-comparison>${escape(comparisonText(comparison))}</p>${comparison.regressed_cases?.length?`<p data-regression-note>候选退步题 ${comparison.regressed_cases.length} 道，已优先展开。</p>`:''}`:''}${result}`;
}
