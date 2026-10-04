import { isCancellation } from "./workspace_lifecycle.mjs";

export function createSkillForms({ document, state, $, api: request, switchView, getScope }) {
  function draftChat(message) {
    switchView("chat");
    const input=$("#chat-form textarea[name='message']"); input.value=message; input.focus();
  }
  const scopedApi = scope => (path, options = {}) => request(path, { ...options, scope });
  const draftActions = scope => (action, data = {}) => request("/api/skill-drafts", {
    method: "POST", csrf: true, body: { action, data }, scope,
  });
  function downloadJson(value, filename, scope) {
    scope.assertCurrent();
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
    const release = scope.defer(() => URL.revokeObjectURL(url));
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    scope.timeout(() => { URL.revokeObjectURL(url); release(); }, 1000);
  }
  const draftStatus={submitted:"待审批",published:"已发布",changes_requested:"需修改",rejected:"已拒绝",withdrawn:"已撤回",editing:"草稿",archived:"已归档"};
  async function loadSkillDrafts() {
    const parent = getScope();
    const scope = parent.replace("skill-draft-list");
    parent.replace("skill-draft-detail").dispose();
    parent.replace("skill-jobs-request").dispose();
    const api = scopedApi(scope);
    const draftAction = draftActions(scope);
    const root=$("#skill-authoring"); if (!root) return;
    try {
      const data=await api("/api/skill-drafts");scope.assertCurrent();
      root.innerHTML=`<h3>我的草稿</h3><p>从需求或当前对话提炼方法，先试用，再提交管理控制台审批。草稿仅自己可见。</p>
        <div class="draft-actions"><button id="draft-generate" class="secondary">描述需求生成</button><button id="draft-extract" class="secondary">总结当前对话</button><button id="draft-import" class="secondary">导入草稿</button></div>
        <label class="draft-auto"><input type="checkbox" id="draft-auto" ${data.preferences.value.auto_draft?"checked":""}> 自动沉淀有价值的交互（仅私有草稿，不自动发布）</label>
        <div id="skill-workbench-home"></div><p id="draft-error" role="status"></p><div id="draft-list"></div><div id="draft-detail"></div><h3>可用助手</h3>`;
      $("#draft-generate").onclick=()=>{$("#skill-generate-form").hidden=false;$("#skill-generate-material").focus();};
      renderSkillWorkbenchHome(data);
      $("#draft-extract").onclick=()=>draftChat("把刚才的过程做成助手草稿");
      $("#draft-auto").onchange=async e=>{try { await draftAction("preferences",{value:{auto_draft:e.target.checked},expected_revision:data.preferences.revision}); await loadSkillDrafts(); } catch(err){if(!scope.current()||isCancellation(err))return;$("#draft-error").textContent=err.message;e.target.checked=!e.target.checked;} };
      $("#draft-import").onclick=()=>{
        const input=document.createElement("input");input.type="file";input.accept="application/json,.json";
        input.onchange=async()=>{try {const f=input.files[0];if(!f)return;if(f.size>100000)throw new Error("文件不能超过100KB");const v=JSON.parse(await f.text());scope.assertCurrent();if(v.format==="agentskills.files.v1")await draftAction("import_standard",{package:v,request_key:crypto.randomUUID()});else if(v.format==="agentbridge.skill-draft.v1")await draftAction("save",{proposal:v.proposal,request_key:crypto.randomUUID(),provenance:{kind:"import"}});else throw new Error("不支持的草稿格式");await loadSkillDrafts();}catch(err){if(!scope.current()||isCancellation(err))return;$("#draft-error").textContent=err.message;}};input.click();
      };
      const list=$("#draft-list");
      if(!data.items.length)list.textContent="还没有草稿。可以直接在对话中说：做个周报助手。";
      for(const d of data.items){const b=document.createElement("button");b.className="secondary";b.textContent=`${d.name} · 修订 ${d.revision} · ${draftStatus[d.review_state||d.state]||d.state}${d.published_version?" · 线上 "+d.published_version:" · 尚未发布"}`;b.onclick=()=>showSkillDraft(d.draft_id);list.append(b);}
    } catch(e){if(!scope.current()||isCancellation(e))return;root.textContent=e.message;}
  }
  async function showSkillDraft(id) {
    const view = getScope();
    const scope = view.replace("skill-draft-detail");
    const api = scopedApi(scope);
    const draftAction = draftActions(scope);
    const root=$("#draft-detail");
    try {
      const d=await api("/api/skill-drafts?id="+encodeURIComponent(id));scope.assertCurrent();const m=d.bundle.manifest;
      root.innerHTML=`<form id="draft-editor" class="draft-editor"><h3>${escapeHtml(m.name)}</h3><p>修订 ${d.revision} · 来源：${escapeHtml(d.provenance.kind)} · 仅保存方法，勿包含业务原文或凭据。</p>
        <label>名称<input name="name" required maxlength="120" value="${escapeHtml(m.name)}"></label>
        <label>简介<textarea name="description" required maxlength="500">${escapeHtml(m.description)}</textarea></label>
        ${[["use_when","适用场景"],["not_for","不适用场景"],["output","输出结果"]].map(([k,t])=>`<label>${t}<textarea name="${k}" required maxlength="500">${escapeHtml(m.selection[k])}</textarea></label>`).join("")}
        <label>处理方法<textarea name="instructions" required maxlength="40000" rows="12">${escapeHtml(d.bundle.resources["SKILL.md"])}</textarea></label>
        <div class="draft-actions"><button type="submit" ${d.state==="archived"?"disabled":""}>保存新修订</button><button type="button" id="draft-test" class="secondary">对话试用</button><button type="button" id="draft-export" class="secondary">导出</button><button type="button" id="draft-archive" class="secondary">归档</button></div></form>
        <div class="draft-actions"><label>历史修订<select id="draft-revision">${d.revisions.map(v=>`<option value="${v.revision}">修订 ${v.revision}</option>`).join("")}</select></label><button type="button" id="draft-restore" class="secondary">恢复为新草稿修订</button></div><p>恢复不影响已发布版本；重新试用并审批后才会生效。</p>
        <h4>样例试运行</h4><p>以下为模型样例，未经独立验证，需人工核对。</p>
        ${d.tests.map(t=>`<details><summary>修订 ${t.revision} · ${escapeHtml(t.profile)} · ${escapeHtml(t.status)}</summary><pre>${escapeHtml(t.prompt)}\n${escapeHtml(t.output||"等待样例结果")}</pre></details>`).join("")||"暂无样例"}
        <div id="skill-quality-panel"></div><form id="draft-submit" class="draft-editor"><h4>申请发布当前修订</h4><fieldset id="skill-recipients"><legend>使用范围（默认仅自己）</legend></fieldset><label>发布说明<textarea name="reason" required maxlength="1000"></textarea></label><button type="submit">提交审批</button></form>
        <h4>发布申请</h4><div id="draft-requests"></div><p id="draft-detail-error" role="status"></p>`;
      const error=e=>{if (scope.current() && !isCancellation(e)) $("#draft-detail-error").textContent=e.message;};
      $("#draft-editor").onsubmit=async e=>{e.preventDefault();try{const f=new FormData(e.target);const exported=await draftAction("export",{draft_id:id});const proposal={...exported.proposal,name:f.get("name"),description:f.get("description"),instructions:f.get("instructions"),selection:Object.fromEntries(["use_when","not_for","output"].map(k=>[k,f.get(k)]))};await draftAction("save",{draft_id:id,expected_revision:d.revision,request_key:crypto.randomUUID(),proposal,provenance:{kind:"revision"}});await loadSkillDrafts();if(!view.current())return;await showSkillDraft(id);}catch(e){if(!scope.current()||isCancellation(e))return;error(e);}};
      $("#draft-restore").onclick=async()=>{try{await draftAction("restore",{draft_id:id,expected_revision:d.revision,target_revision:Number($("#draft-revision").value),request_key:crypto.randomUUID()});await loadSkillDrafts();if(!view.current())return;await showSkillDraft(id);}catch(e){if(!scope.current()||isCancellation(e))return;error(e);}};
      $("#draft-test").onclick=()=>draftChat(`试用“${m.name}”草稿（${id}），用一个简短的合成例子`);
      $("#draft-export").onclick=async()=>{try{const result=await draftAction("export",{draft_id:id});downloadJson(result, "skill-draft.json", scope);}catch(e){if(!scope.current()||isCancellation(e))return;error(e);}};
      $("#draft-archive").onclick=async()=>{try{await draftAction("archive",{draft_id:id,expected_revision:d.revision});await loadSkillDrafts();}catch(e){if(!scope.current()||isCancellation(e))return;error(e);}};
      $("#draft-submit").onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const audience=f.getAll("audience");try{await draftAction("submit",{draft_id:id,expected_revision:d.revision,request_key:crypto.randomUUID(),reason:f.get("reason"),...(audience.length?{audience}:{})});await showSkillDraft(id);}catch(e){if(!scope.current()||isCancellation(e))return;error(e);}};
      await renderSkillQuality(d, scope);
      scope.assertCurrent();
      for(const r of d.requests){const p=document.createElement("p");p.textContent=`修订 ${r.draft_revision} · ${draftStatus[r.state]||r.state} ${r.published_version||""} ${r.decision_reason||""}`;if(r.state==="submitted"){const b=document.createElement("button");b.textContent="撤回申请";b.onclick=async()=>{try{await draftAction("withdraw",{request_id:r.request_id});await showSkillDraft(id);}catch(e){if(!scope.current()||isCancellation(e))return;error(e);}};p.append(b);}$("#draft-requests").append(p);}
    } catch(e){if(!scope.current()||isCancellation(e))return;root.textContent=e.message;}
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
  }

  function skillReportHtml(report) {
    if (!report) return '<p>尚无独立评测。快速试用仅供人工复核。</p>';
    const names={candidate:'候选版本',published:'线上版本',without_skill:'不使用助手'};
    return `<p><strong>${report.passed?'所列断言通过':'存在未通过断言'}</strong> · 仍需人工检查语义正确性</p>
      <p>${escapeHtml(report.limitations)}</p>
      <div class="draft-actions">${Object.entries(report.scores).map(([k,v])=>`<span>${names[k]}：${v.passed}/${v.total}</span>`).join('')}</div>
      ${report.rows.map(r=>`<details><summary>${names[r.variant]} · ${escapeHtml(r.prompt||'不支持的模式')} · ${r.passed?'通过':'未通过'}</summary><pre>${escapeHtml(r.output||r.skipped)}</pre><p>${escapeHtml(r.model||'')} · ${r.elapsed_ms||0} 毫秒</p>${(r.checks||[]).map(c=>`<p>${c.passed?'✓':'×'} ${c.rule==='contains'?'应包含':c.rule==='excludes'?'不应包含':'触发判断'}：${escapeHtml(c.value||'')}</p>`).join('')}</details>`).join('')}`;
  }

  function renderSkillWorkbenchHome(data) {
    const scope = getScope();
    const draftAction = draftActions(scope);
    const root=$('#skill-workbench-home');
    const selected=data.scopes?.items.some(x=>x.scope===data.scope&&x.enabled);
    root.innerHTML=`<label class="draft-auto"><input id="skill-auto-scope" type="checkbox" ${selected?'checked':''}>允许从此工作台的新交互后台提炼（还需开启上方总开关）</label>
      <form id="skill-generate-form" class="draft-editor" hidden><label>想生成什么助手？<textarea id="skill-generate-material" required maxlength="16000" placeholder="做个周报助手，只整理我给的文字。"></textarea></label><button>后台生成草稿</button><p>可填写需求或粘贴选定过程。方法保存为私有草稿；偏好和事实单独提示。</p></form>
      <details><summary>后台生成与评测</summary><button id="skill-refresh-jobs" type="button" class="secondary">刷新任务</button><div id="skill-jobs"></div></details>`;
    const fail=e=>{if (scope.current() && !isCancellation(e)) $('#draft-error').textContent=e.message;};
    $('#skill-auto-scope').onchange=async e=>{try{await draftAction('scopes',{scope:data.scope,enabled:e.target.checked});}catch(err){if(!scope.current()||isCancellation(err))return;e.target.checked=!e.target.checked;fail(err);}};
    $('#skill-generate-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{await draftAction('generate',{material:$('#skill-generate-material').value,request_key:crypto.randomUUID()});$('#draft-error').textContent='已加入后台生成队列，可继续其他工作。';await refreshSkillJobs();}catch(err){if(!scope.current()||isCancellation(err))return;fail(err);}finally{button.disabled=false;}};
    $('#skill-refresh-jobs').onclick=()=>refreshSkillJobs().catch(fail);
    renderSkillJobs(data.jobs?.items||[]);
  }

  async function refreshSkillJobs() {
    const scope = getScope().replace("skill-jobs-request");
    const data = await draftActions(scope)("jobs");
    scope.assertCurrent();
    renderSkillJobs(data.items);
  }
  function renderSkillJobs(items) {
    const scope = getScope();
    const view = scope;
    const draftAction = draftActions(scope);
    const root=$('#skill-jobs');if(!root)return;
    scope.clearTimer(state.skillJobTimer);
    if(state.activeView==='skills'&&items.some(j=>['queued','running'].includes(j.state)))state.skillJobTimer=scope.timeout(()=>refreshSkillJobs().catch(e=>{if(scope.current()&&!isCancellation(e))$('#draft-error').textContent=e.message;}),3000);
    const labels={queued:'排队中',running:'执行中',succeeded:'已完成',failed:'失败',canceled:'已取消'};
    const kinds={method:'可复用方法',preference:'个人偏好',fact:'业务事实',none:'无需沉淀'};
    root.innerHTML=items.map(j=>`<article class="skill-job"><p><strong>${j.kind==='evaluation'?'独立评测':'方法提炼'}</strong> · ${labels[j.state]}${j.kind==='evaluation'?` · 已完成 ${j.progress} 项`:''}</p>
      ${j.result?`<p>${escapeHtml(kinds[j.result.classification]||'')} ${escapeHtml(j.result.summary||j.result.message||'')}</p>`:''}
      ${j.error?`<p role="status">${escapeHtml(j.error)}</p>`:''}
      ${j.result?.draft_id?`<button type="button" class="secondary" data-open-draft="${escapeHtml(j.result.draft_id)}">查看草稿</button>`:''}
      ${(j.result?.similar||[]).map(x=>`<button type="button" class="secondary" data-open-draft="${escapeHtml(x.draft_id)}">相似草稿：${escapeHtml(x.name)}</button>`).join('')}
      ${j.result?.candidate?`<details><summary>查看候选方法并采用</summary><pre>${escapeHtml(j.result.candidate.instructions)}</pre><button type="button" class="secondary" data-adopt-job="${j.job_id}">另存为新草稿</button>${(j.result.similar||[]).map(x=>`<button type="button" class="secondary" data-adopt-job="${j.job_id}" data-adopt-target="${escapeHtml(x.draft_id)}">更新 ${escapeHtml(x.name)}</button>`).join('')}</details>`:''}
      ${['queued','running'].includes(j.state)?`<button type="button" class="secondary" data-job-action="cancel_job" data-job="${j.job_id}">取消</button>`:''}
      ${j.state==='failed'&&j.attempts<3?`<button type="button" class="secondary" data-job-action="retry_job" data-job="${j.job_id}">重试</button>`:''}</article>`).join('')||'<p>暂无后台任务。</p>';
    root.querySelectorAll('[data-open-draft]').forEach(b=>b.onclick=()=>showSkillDraft(b.dataset.openDraft));
    root.querySelectorAll('[data-adopt-job]').forEach(b=>b.onclick=async()=>{try{const target=b.dataset.adoptTarget;const d=target?await draftAction('get',{draft_id:target}):null;const saved=await draftAction('adopt',{job_id:b.dataset.adoptJob,request_key:crypto.randomUUID(),...(d?{draft_id:target,expected_revision:d.revision}:{})});await loadSkillDrafts();if(!view.current())return;await showSkillDraft(saved.draft_id);}catch(e){if(!scope.current()||isCancellation(e))return;$('#draft-error').textContent=e.message;}});
    root.querySelectorAll('[data-job-action]').forEach(b=>b.onclick=async()=>{try{await draftAction(b.dataset.jobAction,{job_id:b.dataset.job});await refreshSkillJobs();}catch(e){if(!scope.current()||isCancellation(e))return;$('#draft-error').textContent=e.message;}});
  }

  async function renderSkillQuality(d, scope) {
    const draftAction = draftActions(scope);
    const [quality, recipients]=await Promise.all([draftAction('inspect',{draft_id:d.draft_id}),draftAction('recipients')]);
    scope.assertCurrent();
    const root=$('#skill-quality-panel');if(!root)return;
    root.innerHTML=`<h4>方法检查与版本对照</h4><p>当前草稿修订 ${d.revision} · ${quality.published_version?'线上 '+escapeHtml(quality.published_version):'尚未发布'}</p>
      <p>${escapeHtml(quality.diagnostics.message)}</p>${quality.diagnostics.checks.map(c=>`<p>${c.level==='warning'?'待完善':'已识别'}：${escapeHtml(c.message)}</p>`).join('')}
      <details><summary>与线上版本的差异（${quality.diff.length} 项）</summary>${quality.diff.map(c=>`<p>${escapeHtml(c.field)}</p><pre>${escapeHtml(c.diff??JSON.stringify({原来:c.before,现在:c.after},null,2))}</pre>`).join('')}</details>
      <button id="skill-standard-export" class="secondary" type="button">导出标准 Skill 文件包</button>
      <h4>独立评测</h4><p>仅使用合成文字，无业务工具。输入简短问题及可检查的期望；断言不会提供给被测模型。</p>
      ${skillReportHtml(quality.evaluation)}
      <form id="skill-eval-form" class="draft-editor"><div id="skill-eval-cases"></div><div class="draft-actions"><button id="skill-add-case" type="button" class="secondary">添加样本</button><label>每项重复<select name="repeats"><option>1</option><option>2</option><option>3</option></select></label><button>启动独立评测</button></div></form>`;
    $('#skill-recipients').innerHTML='<legend>使用范围（默认仅自己）</legend>'+recipients.items.map((x,i)=>`<label><input type="checkbox" name="audience" value="${escapeHtml(x.subject)}" ${i===0?'checked':''}>${escapeHtml(x.name)}</label>`).join('');
    const add=()=>{
      const cases=$('#skill-eval-cases');if(cases.children.length>=12)return;
      const node=document.createElement('fieldset');node.innerHTML=`<legend>样本 ${cases.children.length+1}</legend><label>问题<textarea name="prompt" required maxlength="3000" placeholder="整理成周报：计划修复登录问题。"></textarea></label>
        <label>模式<select name="profile">${Object.keys(d.bundle.manifest.profiles).map(p=>`<option>${escapeHtml(p)}</option>`).join('')}</select></label>
        <label>测试内容<select name="kind"><option value="output">回答质量</option><option value="trigger_true">应该使用此助手</option><option value="trigger_false">不应使用此助手</option></select></label>
        <label>应包含（每行一项）<textarea name="contains" placeholder="计划"></textarea></label><label>不应包含（每行一项）<textarea name="excludes" placeholder="已修复"></textarea></label><button type="button" class="secondary">移除此样本</button>`;
      node.querySelector('button').onclick=()=>node.remove();cases.append(node);
    };
    add();$('#skill-add-case').onclick=add;
    $('#skill-eval-form').onsubmit=async e=>{e.preventDefault();try{const cases=[...$('#skill-eval-cases').children].map(n=>{const kind=n.querySelector('[name=kind]').value;return {prompt:n.querySelector('[name=prompt]').value,profile:n.querySelector('[name=profile]').value,kind:kind==='output'?'output':'trigger',...(kind==='output'?{}:{expected_trigger:kind==='trigger_true'}),...Object.fromEntries(['contains','excludes'].map(k=>[k,n.querySelector(`[name=${k}]`).value.split('\n').map(v=>v.trim()).filter(Boolean)]))};});await draftAction('evaluate',{draft_id:d.draft_id,expected_revision:d.revision,cases,repeats:Number(new FormData(e.target).get('repeats')),request_key:crypto.randomUUID()});$('#draft-detail-error').textContent='评测已排队，请在后台任务查看进度；完成后重新打开草稿查看对照。';await refreshSkillJobs();}catch(err){if(!scope.current()||isCancellation(err))return;$('#draft-detail-error').textContent=err.message;}};
    $('#skill-standard-export').onclick=async()=>{try{const pack=await draftAction('export_standard',{draft_id:d.draft_id});downloadJson(pack, "skill-files.json", scope);}catch(e){if(!scope.current()||isCancellation(e))return;$('#draft-detail-error').textContent=e.message;}};
  }

  function addSkillFeedback(card,item) {
    const scope = getScope();
    const draftAction = draftActions(scope);
    const details=document.createElement('details');details.innerHTML=`<summary>反馈此版本的效果</summary><p>版本 ${escapeHtml(item.version)} · 反馈仅用于改进方法</p><select aria-label="效果评价"><option value="useful">有帮助</option><option value="incorrect">结果有误</option><option value="not_applicable">不适用</option></select><textarea aria-label="纠正说明" maxlength="1000" placeholder="哪里需要改进？"></textarea><button type="button" class="secondary">保存反馈</button><p role="status"></p>`;
    details.querySelector('button').onclick=async()=>{try{await draftAction('feedback',{skill_id:item.id,version:item.version,profile:Object.keys(item.profiles)[0],rating:details.querySelector('select').value,comment:details.querySelector('textarea').value});details.querySelector('[role=status]').textContent='已保存反馈。';}catch(e){if(!scope.current()||isCancellation(e))return;details.querySelector('[role=status]').textContent=e.message;}};
    card.append(details);
  }

  return { loadSkillDrafts, showSkillDraft, refreshSkillJobs, addSkillFeedback };
}
