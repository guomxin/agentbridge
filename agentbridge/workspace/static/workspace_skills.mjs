import { isCancellation } from "./workspace_lifecycle.mjs";

const profileLabels = {preview: "草稿预览", fill: "准备填单", review: "总结与复核", track: "进展追踪", search: "案例检索", single: "单项办理", batch: "批量办理", use: "使用"};
const profileLabel = profile => profileLabels[profile] || profile;

export function skillPublicationCoverage(draft, report) {
  const samples = new Set(Array.isArray(draft.tested_profiles) ? draft.tested_profiles
    : (draft.tests || []).filter(t => t.revision === draft.revision && t.status === "reported").map(t => t.profile));
  const evaluated = new Set(report?.passed && report.revision === draft.revision && report.content_hash === draft.bundle.content_hash
    ? (report.cases || []).filter(c => c.kind === "output").map(c => c.profile) : []);
  return Object.keys(draft.bundle.manifest.profiles).map(profile => ({profile,
    eligible: samples.has(profile) || evaluated.has(profile),
    evidence: evaluated.has(profile) ? "本修订输出断言通过，语义待人工复核" : samples.has(profile) ? "本修订已有样例，待人工复核" : "本修订尚无输出样例",
  }));
}

function proposalFromBundle(bundle) {
  return {...Object.fromEntries(["name", "description", "selection", "profiles", "executionMode", "required_resources"].map(k => [k, bundle.manifest[k]])),
    instructions: bundle.resources["SKILL.md"], references: Object.fromEntries(Object.entries(bundle.resources).filter(([k]) => k !== "SKILL.md"))};
}

function ordered(value) {
  if (Array.isArray(value)) return value.map(ordered);
  if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map(k => [k, ordered(value[k])]));
  return value;
}

export function skillCandidateDiff(bundle, candidate) {
  const before = proposalFromBundle(bundle);
  const after = {...candidate, profiles: candidate.profiles || {use: {}}, references: candidate.references || {}, executionMode: candidate.executionMode || "read_exploration"};
  after.required_resources ??= Object.fromEntries(Object.keys(after.profiles).map(p => [p, Object.keys(after.references)]));
  if (candidate.method) {
    after.instructions += "\n\n" + [["输入", "inputs"], ["步骤", "steps"], ["异常处理", "exceptions"], ["验收", "acceptance"]]
      .filter(([, key]) => candidate.method[key]?.length)
      .map(([title, key]) => `## ${title}\n${candidate.method[key].map((v, i) => `${i + 1}. ${v}`).join("\n")}`).join("\n\n");
  }
  after.instructions = after.instructions.trim();
  const changes = [];
  const compare = (label, a, b) => {
    if (JSON.stringify(ordered(a)) !== JSON.stringify(ordered(b))) changes.push({label, before: a, after: b,
      kind: a === undefined ? "新增" : b === undefined ? "移除" : "修改"});
  };
  for (const [key, label] of [["name", "名称"], ["description", "简介"], ["instructions", "处理方法"], ["selection", "适用范围与输出"], ["executionMode", "执行方式"]]) compare(label, before[key], after[key]);
  for (const [key, label] of [["profiles", "模式与能力依赖"], ["references", "参考资料"], ["required_resources", "模式必读资料"]]) {
    for (const name of [...new Set([...Object.keys(before[key] || {}), ...Object.keys(after[key] || {})])].sort()) compare(`${label} · ${name}`, before[key]?.[name], after[key]?.[name]);
  }
  return changes;
}

export function createSkillForms({ document, state, $, api: request, switchView, getScope, FormData: FormDataType = globalThis.FormData }) {
  const adoptionsByView = new WeakMap();
  const pendingAdoptions = view => {
    if (!adoptionsByView.has(view)) adoptionsByView.set(view, new Set());
    return adoptionsByView.get(view);
  };
  function refreshAdoptionButtons(view) {
    if (!view.current()) return;
    const pending = pendingAdoptions(view);
    $('#skill-jobs')?.querySelectorAll('[data-adopt-job]').forEach(b => { b.disabled = pending.has(b.dataset.adoptJob); });
    const jobId = $('#skill-candidate-preview')?.dataset.adoptJob;
    for (const id of ['candidate-save-new', 'candidate-replace', 'candidate-refresh']) {
      const button = $('#' + id);
      if (button) button.disabled = pending.has(jobId);
    }
  }
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
    parent.replace("skill-candidate-preview").dispose();
    const api = scopedApi(scope);
    const draftAction = draftActions(scope);
    const root=$("#skill-authoring"); if (!root) return;
    try {
      const data=await api("/api/skill-drafts");scope.assertCurrent();
      root.innerHTML=`<h3>我的草稿</h3><p>从需求或当前对话提炼方法，先试用，再提交管理控制台审批。草稿仅自己可见。</p>
        <div class="draft-actions"><button id="draft-generate" class="secondary">描述需求生成</button><button id="draft-extract" class="secondary">总结当前对话</button><button id="draft-import" class="secondary">导入草稿</button></div>
        <label class="draft-auto"><input type="checkbox" id="draft-auto" ${data.preferences.value.auto_draft?"checked":""}> 自动沉淀有价值的交互（仅私有草稿，不自动发布）</label>
        <div id="skill-workbench-home"></div><p id="draft-error" role="status"></p><div id="skill-candidate-preview"></div><div id="draft-list"></div><div id="draft-detail"></div><h3>可用助手</h3>`;
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
      root.innerHTML=`<form id="draft-editor" class="draft-editor"><h3>${escapeHtml(m.name)}</h3><p id="draft-revision-label">已保存：修订 ${d.revision} · 来源：${escapeHtml(d.provenance.kind)} · 仅保存方法，勿包含业务原文或凭据。</p><p id="draft-save-state" role="status"></p><fieldset id="draft-editor-fields"><legend>编辑草稿</legend>
        <label>名称<input name="name" required maxlength="120" value="${escapeHtml(m.name)}"></label>
        <label>简介<textarea name="description" required maxlength="500">${escapeHtml(m.description)}</textarea></label>
        ${[["use_when","适用场景"],["not_for","不适用场景"],["output","输出结果"]].map(([k,t])=>`<label>${t}<textarea name="${k}" required maxlength="500">${escapeHtml(m.selection[k])}</textarea></label>`).join("")}
        <label>处理方法<textarea name="instructions" required maxlength="40000" rows="12">${escapeHtml(d.bundle.resources["SKILL.md"])}</textarea></label>
        <label>试用模式<select id="draft-test-profile">${Object.keys(m.profiles).map(p => `<option value="${escapeHtml(p)}">${escapeHtml(profileLabel(p))}（${escapeHtml(p)}）</option>`).join("")}</select></label>
        <div class="draft-actions"><button type="submit">保存新修订</button><button type="button" id="draft-test" class="secondary">试用已保存修订 ${d.revision}</button><button type="button" id="draft-export" class="secondary">导出</button><button type="button" id="draft-archive" class="secondary">归档</button></div></fieldset></form>
        <div class="draft-actions"><label>历史修订<select id="draft-revision">${d.revisions.map(v=>`<option value="${v.revision}">修订 ${v.revision}</option>`).join("")}</select></label><button type="button" id="draft-restore" class="secondary">恢复为新草稿修订</button></div><p>恢复不影响已发布版本；重新试用并审批后才会生效。</p>
        <h4>样例试运行</h4><p>以下为模型样例，未经独立验证，需人工核对。</p>
        ${d.tests.map(t=>`<details><summary>修订 ${t.revision} · ${escapeHtml(t.profile)} · ${escapeHtml(t.status)}</summary><pre>${escapeHtml(t.prompt)}\n${escapeHtml(t.output||"等待样例结果")}</pre></details>`).join("")||"暂无样例"}
        <div id="skill-quality-panel"></div><form id="draft-submit" class="draft-editor"><h4 id="draft-publication-title">申请发布已保存修订 ${d.revision}</h4><fieldset id="skill-publication-profiles"><legend>本次发布模式</legend><p>正在核对本修订样例。</p></fieldset><p id="skill-publication-state" role="status"></p><fieldset id="skill-recipients"><legend>使用范围（默认仅自己）</legend></fieldset><label>发布说明<textarea name="reason" required maxlength="1000"></textarea></label><button id="draft-submit-button" type="submit" disabled>提交审批</button></form>
        <h4>发布申请</h4><div id="draft-requests"></div><p id="draft-detail-error" role="status"></p>`;
      const error=e=>{if (scope.current() && !isCancellation(e)) $("#draft-detail-error").textContent=e.message;};
      const editor = $("#draft-editor");
      const fields = ["name", "description", "instructions", "use_when", "not_for", "output"];
      const readFields = () => Object.fromEntries(fields.map(k => [k, editor.elements?.namedItem(k)?.value ?? ""]));
      let savedFields = readFields();
      let busy = false;
      let coverage = null;
      const isDirty = () => JSON.stringify(readFields()) !== JSON.stringify(savedFields);
      const refreshState = () => {
        if (!scope.current()) return;
        const dirty = isDirty();
        $("#draft-revision-label").textContent = `已保存：修订 ${d.revision} · 来源：${d.provenance.kind} · 仅保存方法，勿包含业务原文或凭据。`;
        $("#draft-publication-title").textContent = `申请发布已保存修订 ${d.revision}`;
        $("#draft-save-state").textContent = `已保存修订 ${d.revision} · ${busy ? "正在处理，请稍候" : dirty ? "有未保存修改。请先保存，再评测或申请发布。" : "当前内容已保存"}`;
        $("#draft-editor-fields").disabled = busy;
        for (const control of editor.querySelectorAll("input,textarea,select,button")) control.disabled = busy || (d.state === "archived" && control.id !== "draft-export");
        $("#draft-test").textContent = dirty ? "保存后试用" : `试用已保存修订 ${d.revision}`;
        $("#draft-restore").disabled = busy;
        const selected = [...$("#skill-publication-profiles").querySelectorAll('input[name="profiles"]:checked')].map(input => input.value);
        $("#draft-submit-button").disabled = dirty || busy || !coverage || !selected.length || d.state === "archived";
        $("#skill-publication-state").textContent = dirty ? "有未保存修改，不能申请发布旧内容。" :
          !coverage ? "正在核对本修订样例。" : !selected.length ? "请选择已有本修订输出样例的模式；样例仍需人工复核。" :
          `将申请：修订 ${d.revision} · ${selected.map(profileLabel).join("、")}。不会增加业务权限。`;
        const evaluate = $("#skill-eval-submit");
        if (evaluate) evaluate.disabled = dirty || busy || d.state === "archived";
      };
      const requireSaved = () => {
        scope.assertCurrent();
        if (busy) throw new Error("正在处理，请稍候。");
        if (isDirty()) throw new Error("有未保存修改，请先保存，再评测或申请发布。");
        if (d.state === "archived") throw new Error("草稿已归档，不能评测或申请发布。");
      };
      const saveEdits = async () => {
        if (d.state === "archived") throw new Error("草稿已归档，不能保存或试用。");
        const values = readFields();
        const proposal = {...proposalFromBundle(d.bundle), ...Object.fromEntries(["name", "description", "instructions"].map(k => [k, values[k]])),
          selection: Object.fromEntries(["use_when", "not_for", "output"].map(k => [k, values[k]]))};
        const saved = await draftAction("save", {draft_id:id, expected_revision:d.revision, request_key:crypto.randomUUID(), proposal, provenance:{kind:"revision"}});
        scope.assertCurrent();
        // Do not clear text changed while the save was pending, even if a host
        // enabled the disabled form or dispatched an input programmatically.
        savedFields = values;
        d.revision = saved.revision;
        d.tested_profiles = [];
        d.bundle = {manifest: {...d.bundle.manifest, ...Object.fromEntries(["name", "description", "selection"].map(k => [k, proposal[k]]))},
          resources: {...d.bundle.resources, "SKILL.md": proposal.instructions}, content_hash: saved.content_hash};
        coverage = null;
        $("#skill-publication-profiles").innerHTML = "<legend>本次发布模式</legend><p>新修订需重新核对样例。</p>";
        $("#skill-quality-panel").replaceChildren();
        return saved;
      };
      const perform = async action => {
        if (busy || !scope.current()) return;
        busy = true; refreshState();
        try { await action(); } catch (e) { error(e); }
        finally { busy = false; refreshState(); }
      };
      editor.oninput = refreshState;
      $("#draft-submit").onchange = refreshState;
      editor.onsubmit = async e => {
        e.preventDefault();
        if (editor.reportValidity && !editor.reportValidity()) return;
        await perform(async () => {
          await saveEdits();
          if (isDirty()) { await renderSkillQuality(d, scope, controls); error(new Error(`修订 ${d.revision} 已保存，仍有未保存修改，请继续保存。`)); return; }
          await loadSkillDrafts(); if (view.current()) await showSkillDraft(id);
        });
      };
      $("#draft-restore").onclick = () => perform(async () => {
        await draftAction("restore", {draft_id:id, expected_revision:d.revision, target_revision:Number($("#draft-revision").value), request_key:crypto.randomUUID()});
        await loadSkillDrafts(); if (view.current()) await showSkillDraft(id);
      });
      $("#draft-test").onclick = async () => {
        if (d.state === "archived") { error(new Error("草稿已归档，不能试用。")); return; }
        if (editor.reportValidity && !editor.reportValidity()) return;
        const profile = $("#draft-test-profile").value;
        if (!Object.hasOwn(d.bundle.manifest.profiles, profile)) { error(new Error("请选择试用模式。")); return; }
        await perform(async () => {
          if (isDirty()) await saveEdits();
          scope.assertCurrent();
          if (isDirty()) { await renderSkillQuality(d, scope, controls); throw new Error(`修订 ${d.revision} 已保存，但仍有未保存修改；请再次保存后试用。`); }
          draftChat(`请试用“${d.bundle.manifest.name}”草稿（${id}）的修订 ${d.revision}，模式 ${profileLabel(profile)}（${profile}），用一个简短的合成例子。版本变化请提示，不要改用其他修订。`);
        });
      };
      $("#draft-export").onclick=async()=>{try{const result=await draftAction("export",{draft_id:id});downloadJson(result, "skill-draft.json", scope);}catch(e){if(!scope.current()||isCancellation(e))return;error(e);}};
      $("#draft-archive").onclick=async()=>{try{await draftAction("archive",{draft_id:id,expected_revision:d.revision});await loadSkillDrafts();}catch(e){if(!scope.current()||isCancellation(e))return;error(e);}};
      $("#draft-submit").onsubmit = async e => {
        e.preventDefault();
        try {
          requireSaved();
          const f = new FormDataType(e.target);
          const profiles = f.getAll("profiles");
          if (!profiles.length || !coverage || profiles.some(p => !coverage.some(c => c.profile === p && c.eligible))) throw new Error("请选择已有本修订输出样例的模式。");
          const audience = f.getAll("audience");
          await perform(async () => {
            await draftAction("submit", {draft_id:id, expected_revision:d.revision, request_key:crypto.randomUUID(), reason:f.get("reason"), profiles, ...(audience.length ? {audience} : {})});
            await showSkillDraft(id);
          });
        } catch (e) { error(e); }
      };
      const controls = {requireSaved, refreshState, perform, setCoverage: value => {coverage = value;}};
      refreshState();
      await renderSkillQuality(d, scope, controls);
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
    const openCandidates = new Set([...root.querySelectorAll('[data-candidate-job]')].filter(d => d.open).map(d => d.dataset.candidateJob));
    scope.clearTimer(state.skillJobTimer);
    if(state.activeView==='skills'&&items.some(j=>['queued','running'].includes(j.state)))state.skillJobTimer=scope.timeout(()=>refreshSkillJobs().catch(e=>{if(scope.current()&&!isCancellation(e))$('#draft-error').textContent=e.message;}),3000);
    const labels={queued:'排队中',running:'执行中',succeeded:'已完成',failed:'失败',canceled:'已取消'};
    const kinds={method:'可复用方法',preference:'个人偏好',fact:'业务事实',none:'无需沉淀'};
    root.innerHTML=items.map(j=>`<article class="skill-job"><p><strong>${j.kind==='evaluation'?'独立评测':'方法提炼'}</strong> · ${labels[j.state]}${j.kind==='evaluation'?` · 已完成 ${j.progress} 项`:''}</p>
      ${j.result?`<p>${escapeHtml(kinds[j.result.classification]||'')} ${escapeHtml(j.result.summary||j.result.message||'')}</p>`:''}
      ${j.error?`<p role="status">${escapeHtml(j.error)}</p>`:''}
      ${j.result?.draft_id?`<button type="button" class="secondary" data-open-draft="${escapeHtml(j.result.draft_id)}">查看草稿</button>`:''}
      ${(j.result?.similar||[]).map(x=>`<button type="button" class="secondary" data-open-draft="${escapeHtml(x.draft_id)}">相似草稿：${escapeHtml(x.name)}</button>`).join('')}
      ${j.result?.candidate?`<details data-candidate-job="${escapeHtml(j.job_id)}"><summary>查看候选方法并采用</summary><pre>${escapeHtml(j.result.candidate.instructions)}</pre><button type="button" class="secondary" data-adopt-job="${j.job_id}">另存为新草稿</button>${(j.result.similar||[]).map(x=>`<button type="button" class="secondary" data-preview-adopt="${escapeHtml(j.job_id)}" data-adopt-target="${escapeHtml(x.draft_id)}">查看替换“${escapeHtml(x.name)}”的差异</button>`).join('')}</details>`:''}
      ${['queued','running'].includes(j.state)?`<button type="button" class="secondary" data-job-action="cancel_job" data-job="${j.job_id}">取消</button>`:''}
      ${j.state==='failed'&&j.attempts<3?`<button type="button" class="secondary" data-job-action="retry_job" data-job="${j.job_id}">重试</button>`:''}</article>`).join('')||'<p>暂无后台任务。</p>';
    root.querySelectorAll('[data-candidate-job]').forEach(d => { d.open = openCandidates.has(d.dataset.candidateJob); });
    root.querySelectorAll('[data-open-draft]').forEach(b=>b.onclick=()=>showSkillDraft(b.dataset.openDraft));
    root.querySelectorAll('[data-adopt-job]').forEach(b => {
      const pending = pendingAdoptions(view);
      b.onclick = async () => {
        const jobId = b.dataset.adoptJob;
        if (pending.has(jobId) || !scope.current()) return;
        pending.add(jobId); refreshAdoptionButtons(view);
        try {
          const saved = await draftAction('adopt', {job_id:jobId, request_key:crypto.randomUUID()});
          await loadSkillDrafts(); if(view.current()) await showSkillDraft(saved.draft_id);
        } catch(e) { if(scope.current() && !isCancellation(e)) $('#draft-error').textContent=e.message; }
        finally { pending.delete(jobId); refreshAdoptionButtons(view); }
      };
    });
    refreshAdoptionButtons(view);
    root.querySelectorAll('[data-preview-adopt]').forEach(b => b.onclick = () => {
      const job = items.find(j => j.job_id === b.dataset.previewAdopt);
      if (job) return previewCandidateReplacement(job, b.dataset.adoptTarget);
    });
    root.querySelectorAll('[data-job-action]').forEach(b=>b.onclick=async()=>{try{await draftAction(b.dataset.jobAction,{job_id:b.dataset.job});await refreshSkillJobs();}catch(e){if(!scope.current()||isCancellation(e))return;$('#draft-error').textContent=e.message;}});
  }

  async function previewCandidateReplacement(job, targetId) {
    const view = getScope();
    const scope = view.replace("skill-candidate-preview");
    const draftAction = draftActions(scope);
    const root = $('#skill-candidate-preview');
    if (!root) return;
    root.dataset.adoptJob = job.job_id;
    root.innerHTML = '<p role="status">正在读取目标草稿，核对替换差异。</p>';
    try {
      const candidate = structuredClone(job.result.candidate);
      const target = await draftAction('get', {draft_id:targetId});
      scope.assertCurrent();
      const expectedRevision = target.revision;
      const changes = skillCandidateDiff(target.bundle, candidate);
      const display = value => value === undefined ? '（无）' : typeof value === 'string' ? value : JSON.stringify(value, null, 2);
      root.innerHTML = `<h3>替换“${escapeHtml(target.bundle.manifest.name)}”前核对</h3>
        <p>目标：修订 ${expectedRevision}。候选会完整替换本草稿的方法、适用范围、模式与依赖、参考资料及必读清单；不会自动合并。旧修订保留，已发布版本不变。</p>
        <p>${changes.length ? `发现 ${changes.length} 项变化，请核对新增、修改和移除项。` : '候选与目标方法内容一致。'}</p>
        ${changes.map(c => `<details><summary>${escapeHtml(c.kind)} · ${escapeHtml(c.label)}</summary><p>当前修订 ${expectedRevision}</p><pre>${escapeHtml(display(c.before))}</pre><p>替换后</p><pre>${escapeHtml(display(c.after))}</pre></details>`).join('')}
        <div class="draft-actions"><button id="candidate-save-new" type="button" class="secondary">另存为新草稿</button><button id="candidate-replace" type="button">确认替换为新修订</button><button id="candidate-refresh" type="button" class="secondary">重新查看差异</button></div><p id="candidate-replace-status" role="status"></p>`;
      const pending = pendingAdoptions(view);
      const adopt = async replace => {
        if (pending.has(job.job_id) || !scope.current()) return;
        pending.add(job.job_id); refreshAdoptionButtons(view);
        try {
          // The revision belongs to the displayed comparison. Never re-read it
          // at confirmation: a newer draft must conflict, not be overwritten.
          const saved = await draftAction('adopt', {job_id:job.job_id, request_key:crypto.randomUUID(),
            ...(replace ? {draft_id:targetId, expected_revision:expectedRevision} : {})});
          scope.assertCurrent();
          await loadSkillDrafts(); if (view.current()) await showSkillDraft(saved.draft_id);
        } catch(e) {
          if (scope.current() && !isCancellation(e)) $('#candidate-replace-status').textContent = `${e.message}。可重新查看差异后再操作；不会自动重试替换。`;
        } finally {
          pending.delete(job.job_id); refreshAdoptionButtons(view);
        }
      };
      $('#candidate-save-new').onclick = () => adopt(false);
      $('#candidate-replace').onclick = () => adopt(true);
      $('#candidate-refresh').onclick = () => { if (!pending.has(job.job_id)) return previewCandidateReplacement(job, targetId); };
      refreshAdoptionButtons(view);
    } catch(e) { if(scope.current() && !isCancellation(e)) root.textContent=e.message; }
  }

  async function renderSkillQuality(d, scope, controls) {
    const revision = d.revision;
    const draftAction = draftActions(scope);
    const [quality, recipients]=await Promise.all([draftAction('inspect',{draft_id:d.draft_id}),draftAction('recipients')]);
    scope.assertCurrent();
    if (d.revision !== revision) return;
    const root=$('#skill-quality-panel');if(!root)return;
    root.innerHTML=`<h4>方法检查与版本对照</h4><p>当前草稿修订 ${d.revision} · ${quality.published_version?'线上 '+escapeHtml(quality.published_version):'尚未发布'}</p>
      <p>${escapeHtml(quality.diagnostics.message)}</p>${quality.diagnostics.checks.map(c=>`<p>${c.level==='warning'?'待完善':'已识别'}：${escapeHtml(c.message)}</p>`).join('')}
      <details><summary>与线上版本的差异（${quality.diff.length} 项）</summary>${quality.diff.map(c=>`<p>${escapeHtml(c.field)}</p><pre>${escapeHtml(c.diff??JSON.stringify({原来:c.before,现在:c.after},null,2))}</pre>`).join('')}</details>
      <button id="skill-standard-export" class="secondary" type="button">导出标准 Skill 文件包</button>
      <h4>独立评测</h4><p>仅使用合成文字，无业务工具。输入简短问题及可检查的期望；断言不会提供给被测模型。</p>
      ${skillReportHtml(quality.evaluation)}
      <form id="skill-eval-form" class="draft-editor"><div id="skill-eval-cases"></div><div class="draft-actions"><button id="skill-add-case" type="button" class="secondary">添加样本</button><label>每项重复<select name="repeats"><option>1</option><option>2</option><option>3</option></select></label><button id="skill-eval-submit">启动独立评测</button></div></form>`;
    const coverage = skillPublicationCoverage(d, quality.evaluation);
    $('#skill-publication-profiles').innerHTML = '<legend>本次发布模式</legend>' + coverage.map(c =>
      `<label><input type="checkbox" name="profiles" value="${escapeHtml(c.profile)}" ${c.eligible ? 'checked' : 'disabled'}> ${escapeHtml(profileLabel(c.profile))}（${escapeHtml(c.profile)}） — ${escapeHtml(c.evidence)}</label>`).join('');
    controls.setCoverage(coverage);
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
    $('#skill-eval-form').onsubmit = async e => {
      e.preventDefault();
      try {
        controls.requireSaved();
        const cases = [...$('#skill-eval-cases').children].map(n => {
          const kind = n.querySelector('[name=kind]').value;
          return {prompt:n.querySelector('[name=prompt]').value, profile:n.querySelector('[name=profile]').value,
            kind:kind === 'output' ? 'output' : 'trigger', ...(kind === 'output' ? {} : {expected_trigger:kind === 'trigger_true'}),
            ...Object.fromEntries(['contains','excludes'].map(k => [k,n.querySelector(`[name=${k}]`).value.split('\n').map(v => v.trim()).filter(Boolean)]))};
        });
        await controls.perform(async () => {
          await draftAction('evaluate', {draft_id:d.draft_id, expected_revision:d.revision, cases, repeats:Number(new FormDataType(e.target).get('repeats')), request_key:crypto.randomUUID()});
          scope.assertCurrent();
          $('#draft-detail-error').textContent=`修订 ${d.revision} 的评测已排队；完成后重新打开草稿查看对照。`;
          await refreshSkillJobs();
        });
      } catch(err) { if(scope.current() && !isCancellation(err)) $('#draft-detail-error').textContent=err.message; }
    };
    controls.refreshState();
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
