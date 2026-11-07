import {escapeHtml, fmtTime, table} from './admin_presentation.mjs';

export async function renderSkillReviewList({content, api, openReview, onError}) {
  const labels={submitted:'待审批',published:'已发布',changes_requested:'需修改',rejected:'已拒绝',withdrawn:'已撤回'};
  let state='pending', cursor=null, items=[], loading=false;
  content.innerHTML='<p>个人发布与共享发布均须审批。请核对固定修订、使用范围及评测依据。</p><div class="actions"><button id="review-pending">待审批</button><button id="review-history" class="button secondary">历史申请</button></div><div id="review-rows"></div><button id="review-more" class="button secondary" hidden>加载更多申请</button><p id="review-list-status" role="status"></p><div id="review-metrics"></div>';
  const $=selector=>content.querySelector(selector);
  const controls=()=>{
    for(const key of ['pending','history']) {
      $('#review-'+key).disabled=loading;
      $('#review-'+key).setAttribute('aria-pressed',String(state===key));
    }
    $('#review-more').disabled=loading;$('#review-more').hidden=!cursor;
  };
  const load=async(nextState,append=false)=>{
    if(loading)return;
    loading=true;controls();$('#review-list-status').textContent='正在加载申请。';
    try {
      const query=new URLSearchParams({state:nextState});
      if(append&&cursor)query.set('cursor',cursor);
      const result=await api('/api/skill-reviews?'+query);
      state=nextState;cursor=result.next_cursor;
      items=[...new Map([...(append?items:[]),...result.items].map(r=>[r.request_id,r])).values()];
      $('#review-rows').innerHTML=items.length?table(['申请人','助手与修订','范围','状态','提交时间','操作'],items.map(r=>`<tr><td>${escapeHtml(r.owner_subject)}</td><td>${escapeHtml(r.name||'历史助手')} / ${r.draft_revision}</td><td>${escapeHtml(r.audience.join('、'))}</td><td>${escapeHtml(labels[r.state]||r.state)}</td><td>${fmtTime(r.created_at)}</td><td><button class="button secondary" data-skill-review="${escapeHtml(r.request_id)}">查看申请</button></td></tr>`)):'<p>暂无申请。</p>';
      $('#review-rows').querySelectorAll('[data-skill-review]').forEach(b=>b.onclick=()=>openReview(b.dataset.skillReview).catch(onError));
      $('#review-list-status').textContent=`已显示 ${items.length} 项申请。`;
      if(result.metrics) {
        const ratings={useful:'有帮助',incorrect:'结果有误',not_applicable:'不适用'};
        $('#review-metrics').innerHTML=`<details><summary>版本反馈与后台任务</summary><p>用户反馈不等于独立成功率，加载数量不作为质量证明。</p>${result.metrics.feedback.map(r=>`<p>${escapeHtml(r.skill_id)} · ${escapeHtml(r.version)} · ${escapeHtml(r.profile||'模式未记录')} · ${ratings[r.rating]} ${r.count} 次</p>`).join('')||'<p>暂无版本反馈。</p>'}${result.metrics.jobs.map(r=>`<p>${r.kind==='evaluation'?'评测':'生成'} · ${escapeHtml(r.state)} ${r.count} 项</p>`).join('')}</details>`;
      }
    } catch(e) {
      if(e.name!=='AbortError')$('#review-list-status').textContent=e.message;
      throw e;
    } finally {loading=false;controls();}
  };
  for(const key of ['pending','history'])$('#review-'+key).onclick=()=>load(key).catch(onError);
  $('#review-more').onclick=()=>load(state,true).catch(onError);
  await load('pending');
}
