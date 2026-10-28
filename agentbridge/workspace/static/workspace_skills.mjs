import { isCancellation } from "./workspace_lifecycle.mjs";
import { createSkillLeaveGuard } from "./workspace_skill_leave.mjs";
import { skillReportHtml, comparisonText } from "./workspace_skill_report.mjs";

const profileLabels = {preview: "草稿预览", fill: "准备填单", review: "总结与复核", track: "进展追踪", search: "案例检索", single: "单项办理", batch: "批量办理", use: "使用"};
const profileLabel = profile => profileLabels[profile] || profile;

export function skillPublicationCoverage(draft, report, evidence) {
  if (evidence !== undefined) {
    const current = evidence?.schema === "agentbridge.skill-publication-evidence.v1" &&
      evidence.revision === draft.revision && evidence.content_hash === draft.bundle.content_hash;
    return Object.keys(draft.bundle.manifest.profiles).map(profile => {
      const row = current && evidence.profiles?.find(item => item.profile === profile);
      return {profile, eligible: Boolean(row?.eligible && !row.blocked && !evidence.shared?.blocked),
        evidence: row?.evidence || "发布依据已变化，请刷新草稿。"};
    });
  }
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

function comparableDependencies(value) {
  if (value === undefined) return value;
  const normalized = {...value, all: [...(value.all || [])].sort(), any: [...(value.any || [])].sort()};
  // An explicit database requirement, even {}, is different from no database
  // requirement. Only its all/any defaults and order are presentation noise.
  if (Object.hasOwn(value, "database")) normalized.database = comparableDependencies(value.database);
  return normalized;
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
  const compare = (label, a, b, normalize = v => v) => {
    if (JSON.stringify(ordered(normalize(a))) !== JSON.stringify(ordered(normalize(b)))) changes.push({label, before: a, after: b,
      kind: a === undefined ? "新增" : b === undefined ? "移除" : "修改"});
  };
  for (const [key, label] of [["name", "名称"], ["description", "简介"], ["instructions", "处理方法"], ["selection", "适用范围与输出"], ["executionMode", "执行方式"]]) compare(label, before[key], after[key]);
  for (const [key, label] of [["profiles", "模式与能力依赖"], ["references", "参考资料"], ["required_resources", "模式必读资料"]]) {
    for (const name of [...new Set([...Object.keys(before[key] || {}), ...Object.keys(after[key] || {})])].sort()) compare(`${label} · ${name}`, before[key]?.[name], after[key]?.[name], key === "profiles" ? comparableDependencies : undefined);
  }
  return changes;
}

export function skillCandidateRemovals(changes) {
  const removed = [];
  for (const change of changes) {
    const [kind, name] = change.label.split(" · ");
    if (kind === "模式与能力依赖") {
      if (change.after === undefined) removed.push(`模式：${profileLabel(name)}（${name}）`);
      else {
        for (const key of ["all", "any"]) {
          const missing = (change.before?.[key] || []).filter(item => !(change.after[key] || []).includes(item));
          if (missing.length) removed.push(`${name} ${key === "all" ? "必需" : "任选"}能力：${missing.join("、")}`);
        }
        if (change.before && Object.hasOwn(change.before, "database") && !Object.hasOwn(change.after, "database")) removed.push(`${name} 数据源要求`);
        else for (const key of ["all", "any"]) {
          const missing = (change.before?.database?.[key] || []).filter(item => !(change.after.database?.[key] || []).includes(item));
          if (missing.length) removed.push(`${name} 数据源${key === "all" ? "必需" : "任选"}能力：${missing.join("、")}`);
        }
      }
    } else if (kind === "参考资料" && change.after === undefined) removed.push(`参考资料：${name}`);
    else if (kind === "模式必读资料") {
      const missing = (change.before || []).filter(item => !(change.after || []).includes(item));
      if (missing.length) removed.push(`${name} 必读资料：${missing.join("、")}`);
    }
  }
  return removed;
}

export function createSkillForms({ document, state, $, api: request, switchView, startTrial, clearTrial = () => {}, getScope, FormData: FormDataType = globalThis.FormData, eventTarget = globalThis, confirm }) {
  const leaveGuard = createSkillLeaveGuard({document, eventTarget, confirm});
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
  async function draftChat(message, permit) {
    if (await switchView("chat", permit) === false) return false;
    clearTrial();
    const input=$("#chat-form textarea[name='message']"); input.value=message; input.focus();
    return true;
  }
  const scopedApi = scope => (path, options = {}) => request(path, { ...options, scope });
  const draftActions = scope => (action, data = {}) => request("/api/skill-drafts", {
    method: "POST", csrf: true, body: { action, data }, scope,
  });
  async function navigateAfterAction(scope, action) {
    if (!await leaveGuard.request() || !scope.current()) return false;
    const permit = leaveGuard.capture();
    const finish = leaveGuard.begin(scope);
    if (!finish) return false;
    try { return await action(permit); } finally { finish(); }
  }
  async function reloadDraft(id, permit, evaluation) {
    const view = getScope();
    if (await loadSkillDrafts(permit) === false) return false;
    if (id && view.current()) await showSkillDraft(id, permit, evaluation);
  }
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
  async function loadSkillDrafts(permit) {
    const parent = getScope();
    if (!await leaveGuard.request(permit) || !parent.current()) return false;
    const scope = parent.replace("skill-draft-list");
    parent.replace("skill-draft-detail").dispose();
    parent.replace("skill-jobs-request").dispose();
    parent.replace("skill-candidate-preview").dispose();
    const api = scopedApi(scope);
    const draftAction = draftActions(scope);
    const root=$("#skill-authoring"); if (!root) return;
    root.innerHTML = '<p role="status">正在加载草稿。</p>';
    try {
      const data=await api("/api/skill-drafts");scope.assertCurrent();
      root.innerHTML=`<h3>我的草稿</h3><p>从需求或当前对话提炼方法，先试用，再提交管理控制台审批。草稿仅自己可见。</p>
        <div class="draft-actions"><button id="draft-generate" class="secondary">描述需求生成</button><button id="draft-extract" class="secondary">总结当前对话</button><button id="draft-import" class="secondary">导入草稿</button></div>
        <label class="draft-auto"><input type="checkbox" id="draft-auto" ${data.preferences.value.auto_draft?"checked":""}> 自动沉淀有价值的交互（仅私有草稿，不自动发布）</label>
        <div id="skill-workbench-home"></div><p id="draft-error" role="status"></p><div id="skill-candidate-preview"></div><div id="draft-list"></div><div id="draft-detail"></div><h3>可用助手</h3>`;
      $("#draft-generate").onclick=()=>{$("#skill-generate-form").hidden=false;$("#skill-generate-material").focus();};
      renderSkillWorkbenchHome(data);
      $("#draft-extract").onclick=()=>draftChat("把刚才的过程做成助手草稿");
      $("#draft-auto").onchange=async e=>{try { const result = await navigateAfterAction(scope, async permit => {await draftAction("preferences",{value:{auto_draft:e.target.checked},expected_revision:data.preferences.revision}); await loadSkillDrafts(permit);}); if (result === false) e.target.checked=!e.target.checked; } catch(err){if(!scope.current()||isCancellation(err))return;$("#draft-error").textContent=err.message;e.target.checked=!e.target.checked;} };
      $("#draft-import").onclick=()=>{
        const input=document.createElement("input");input.type="file";input.accept="application/json,.json";
        input.onchange=async()=>{try {const f=input.files[0];if(!f)return;if(f.size>100000)throw new Error("文件不能超过100KB");const v=JSON.parse(await f.text());scope.assertCurrent();await navigateAfterAction(scope, async permit => {if(v.format==="agentskills.files.v1")await draftAction("import_standard",{package:v,request_key:crypto.randomUUID()});else if(v.format==="agentbridge.skill-draft.v1")await draftAction("save",{proposal:v.proposal,request_key:crypto.randomUUID(),provenance:{kind:"import"}});else throw new Error("不支持的草稿格式");await loadSkillDrafts(permit);});}catch(err){if(!scope.current()||isCancellation(err))return;$("#draft-error").textContent=err.message;}};input.click();
      };
      const list=$("#draft-list");
      if(!data.items.length)list.textContent="还没有草稿。可以直接在对话中说：做个周报助手。";
      for(const d of data.items){const b=document.createElement("button");b.className="secondary";b.textContent=`${d.name} · 修订 ${d.revision} · ${draftStatus[d.review_state||d.state]||d.state}${d.published_version?" · 线上 "+d.published_version:" · 尚未发布"}`;b.onclick=()=>showSkillDraft(d.draft_id);list.append(b);}
    } catch(e){if(!scope.current()||isCancellation(e))return false;root.textContent=e.message;return false;}
  }
  async function showSkillDraft(id, permit, carriedEvaluation = null) {
    const view = getScope();
    if (!await leaveGuard.request(permit) || !view.current()) return false;
    const scope = view.replace("skill-draft-detail");
    const api = scopedApi(scope);
    const draftAction = draftActions(scope);
    const root=$("#draft-detail");
    root.innerHTML = '<p role="status">正在加载草稿详情。</p>';
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
        <div id="skill-sample-list">${d.tests.map(t=>`<details><summary>修订 ${t.revision} · ${escapeHtml(profileLabel(t.profile))}（${escapeHtml(t.profile)}） · ${escapeHtml({reported:'已保存，待复核',running:'试用中'}[t.status] || t.status)}</summary><pre>${escapeHtml(t.prompt)}\n${escapeHtml(t.output||"等待样例结果")}</pre>${t.status === 'reported' && t.test_id ? `<button type="button" class="secondary" data-sample-case="${escapeHtml(t.test_id)}">转成验收样例</button><form data-sample-feedback="${escapeHtml(t.test_id)}" class="draft-editor"><label>这个样例是否有帮助？<select name="rating"><option value="useful">有帮助</option><option value="incorrect">结果有误</option></select></label><label>一句话说明<textarea name="comment" required maxlength="1000" placeholder="例如：把计划误写成已完成。"></textarea></label><button type="submit" class="secondary">保存反馈</button><p role="status"></p></form>` : ''}</details>`).join("")||"暂无样例"}</div>
        <div id="skill-quality-panel"></div><form id="draft-submit" class="draft-editor"><h4 id="draft-publication-title">申请发布已保存修订 ${d.revision}</h4><fieldset id="skill-publication-profiles"><legend>本次发布模式</legend><p>正在核对本修订样例。</p></fieldset><p id="skill-publication-state" role="status"></p><fieldset id="skill-recipients"><legend>使用范围（默认仅自己）</legend></fieldset><label>发布说明<textarea name="reason" required maxlength="1000"></textarea></label><button id="draft-submit-button" type="submit" disabled>提交审批</button></form>
        <h4>发布申请</h4><div id="draft-requests"></div><p id="draft-detail-error" role="status"></p>`;
      const error=e=>{if (scope.current() && !isCancellation(e)) $("#draft-detail-error").textContent=e.message;};
      const editor = $("#draft-editor");
      const fields = ["name", "description", "instructions", "use_when", "not_for", "output"];
      const readFields = () => Object.fromEntries(fields.map(k => [k, editor.elements?.namedItem(k)?.value ?? ""]));
      let savedFields = readFields();
      let retainedEvaluation = carriedEvaluation;
      let readEvaluation = () => retainedEvaluation?.value || {cases: [], repeats: '1'};
      let reviewForms = [];
      const reviewValue = form => JSON.stringify({status:form.querySelector('[name=review_status]').value,note:form.querySelector('[name=review_note]').value});
      const reviewDirty = () => reviewForms.some(form => reviewValue(form) !== form.dataset.savedReview);
      let savedEvaluation = carriedEvaluation?.baseline ?? JSON.stringify(readEvaluation());
      const evaluationDirty = () => JSON.stringify(readEvaluation()) !== savedEvaluation;
      const editorSnapshot = () => JSON.stringify({method: readFields(), evaluation: readEvaluation(), reviews: reviewForms.map(reviewValue)});
      let busy = false;
      let coverage = null;
      const isDirty = () => JSON.stringify(readFields()) !== JSON.stringify(savedFields);
      const refreshState = () => {
        if (!scope.current()) return;
        const dirty = isDirty();
        const processing = busy || leaveGuard.busy();
        leaveGuard.update();
        $("#draft-revision-label").textContent = `已保存：修订 ${d.revision} · 来源：${d.provenance.kind} · 仅保存方法，勿包含业务原文或凭据。`;
        $("#draft-publication-title").textContent = `申请发布已保存修订 ${d.revision}`;
        $("#draft-save-state").textContent = `已保存修订 ${d.revision} · ${processing ? "正在处理，请稍候" : dirty ? "有未保存修改。请先保存，再评测或申请发布。" : "当前内容已保存"}`;
        $("#draft-editor-fields").disabled = processing;
        for (const form of reviewForms) for (const input of form.querySelectorAll("input,textarea,select,button")) input.disabled = processing;
        for (const control of editor.querySelectorAll("input,textarea,select,button")) control.disabled = processing || (d.state === "archived" && control.id !== "draft-export");
        $("#draft-test").textContent = dirty ? "保存后试用" : `试用已保存修订 ${d.revision}`;
        $("#draft-restore").disabled = processing;
        const selected = [...$("#skill-publication-profiles").querySelectorAll('input[name="profiles"]:checked')].map(input => input.value);
        $("#draft-submit-button").disabled = dirty || processing || !coverage || !selected.length || d.state === "archived";
        $("#skill-publication-state").textContent = dirty ? "有未保存修改，不能申请发布旧内容。" :
          !coverage ? "正在核对本修订样例。" : !selected.length ? "请选择已有本修订输出样例的模式；样例仍需人工复核。" :
          `将申请：修订 ${d.revision} · ${selected.map(profileLabel).join("、")}。不会增加业务权限。`;
        const evaluate = $("#skill-eval-submit");
        if (evaluate) evaluate.disabled = dirty || processing || d.state === "archived";
        const saveCases = $("#skill-save-pending");
        if(saveCases) saveCases.disabled = dirty || processing || d.state === "archived";
      };
      leaveGuard.register(scope, {isDirty: () => isDirty() || evaluationDirty() || reviewDirty(), isBusy: () => busy, snapshot: editorSnapshot, refresh: refreshState,
        notify: message => error(new Error(message))});
      const requireSaved = () => {
        scope.assertCurrent();
        if (reviewDirty()) throw new Error("有未保存的复核说明，请先保存复核。");
        if (busy || leaveGuard.busy()) throw new Error("正在处理，请稍候。");
        if (isDirty()) throw new Error("有未保存修改，请先保存，再评测或申请发布。");
        if (d.state === "archived") throw new Error("草稿已归档，不能评测或申请发布。");
      };
      const saveEdits = async () => {
        if (reviewDirty()) throw new Error("有未保存的复核说明，请先保存复核，再保存方法。");
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
        if (reviewDirty()) throw new Error(`修订 ${d.revision} 已保存，但复核说明又有修改；请先保存复核，再刷新报告。`);
        retainedEvaluation = {value: readEvaluation(), baseline: savedEvaluation};
        readEvaluation = () => retainedEvaluation.value;
        $("#skill-quality-panel").replaceChildren();
        return saved;
      };
      const perform = async action => {
        if (busy || leaveGuard.busy() || !scope.current()) return;
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
          await reloadDraft(id, leaveGuard.capture(), retainedEvaluation);
        });
      };
      $("#draft-restore").onclick = async () => {
        if (!await leaveGuard.request()) return;
        const permit = leaveGuard.capture();
        return perform(async () => {
          await draftAction("restore", {draft_id:id, expected_revision:d.revision, target_revision:Number($("#draft-revision").value), request_key:crypto.randomUUID()});
          await reloadDraft(id, permit);
        });
      };
      $("#draft-test").onclick = async () => {
        if ((evaluationDirty() || reviewDirty()) && !await leaveGuard.request()) return;
        if (d.state === "archived") { error(new Error("草稿已归档，不能试用。")); return; }
        if (editor.reportValidity && !editor.reportValidity()) return;
        const profile = $("#draft-test-profile").value;
        if (!Object.hasOwn(d.bundle.manifest.profiles, profile)) { error(new Error("请选择试用模式。")); return; }
        await perform(async () => {
          if (isDirty()) await saveEdits();
          scope.assertCurrent();
          if (isDirty()) { await renderSkillQuality(d, scope, controls); throw new Error(`修订 ${d.revision} 已保存，但仍有未保存修改；请再次保存后试用。`); }
          await startTrial({draft_id:id, expected_revision:d.revision, profile, name:d.bundle.manifest.name}, leaveGuard.capture());
        });
      };
      $("#draft-export").onclick=async()=>{try{const result=await draftAction("export",{draft_id:id});downloadJson(result, "skill-draft.json", scope);}catch(e){if(!scope.current()||isCancellation(e))return;error(e);}};
      $("#draft-archive").onclick=async()=>{if(!await leaveGuard.request())return;const permit=leaveGuard.capture();return perform(async()=>{await draftAction("archive",{draft_id:id,expected_revision:d.revision});await loadSkillDrafts(permit);});};
      $("#draft-submit").onsubmit = async e => {
        e.preventDefault();
        try {
          requireSaved();
          const f = new FormDataType(e.target);
          const profiles = f.getAll("profiles");
          if (!profiles.length || !coverage || profiles.some(p => !coverage.some(c => c.profile === p && c.eligible))) throw new Error("请选择已有本修订输出样例的模式。");
          const audience = f.getAll("audience");
          const permit = leaveGuard.capture();
          await perform(async () => {
            await draftAction("submit", {draft_id:id, expected_revision:d.revision, request_key:crypto.randomUUID(), reason:f.get("reason"), profiles, ...(audience.length ? {audience} : {})});
            await showSkillDraft(id, permit);
          });
        } catch (e) { error(e); }
      };
      const controls = {requireSaved, refreshState, perform, setCoverage: value => {coverage = value;},
        setReviewForms: forms => {reviewForms = forms; leaveGuard.update();},
        reviewChanged: () => leaveGuard.update(),
        evaluation: () => retainedEvaluation,
        setEvaluationReader: read => {
          readEvaluation = read;
          if (!retainedEvaluation) savedEvaluation = JSON.stringify(read());
          retainedEvaluation = null;
          leaveGuard.update();
        },
        evaluationChanged: () => leaveGuard.update(),
        evaluationSaved: value => {savedEvaluation = JSON.stringify(value); leaveGuard.update();},
      };
      refreshState();
      await renderSkillQuality(d, scope, controls);
      scope.assertCurrent();
      for(const r of d.requests){const p=document.createElement("p");p.textContent=`修订 ${r.draft_revision} · ${draftStatus[r.state]||r.state} ${r.published_version||""} ${r.decision_reason||""}`;if(r.state==="submitted"){const b=document.createElement("button");b.textContent="撤回申请";b.onclick=async()=>{if(!await leaveGuard.request())return;const permit=leaveGuard.capture();return perform(async()=>{await draftAction("withdraw",{request_id:r.request_id});await showSkillDraft(id,permit);});};p.append(b);}$("#draft-requests").append(p);}
    } catch(e){if(!scope.current()||isCancellation(e))return;root.textContent=e.message;}
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
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
    const openReports = new Set([...root.querySelectorAll('[data-report-job]')].filter(d => d.open).map(d => d.dataset.reportJob));
    const openCandidates = new Set([...root.querySelectorAll('[data-candidate-job]')].filter(d => d.open).map(d => d.dataset.candidateJob));
    scope.clearTimer(state.skillJobTimer);
    if(state.activeView==='skills'&&items.some(j=>['queued','running'].includes(j.state)))state.skillJobTimer=scope.timeout(()=>refreshSkillJobs().catch(e=>{if(scope.current()&&!isCancellation(e))$('#draft-error').textContent=e.message;}),3000);
    const labels={queued:'排队中',running:'执行中',succeeded:'已完成',failed:'失败',canceled:'已取消'};
    const kinds={method:'可复用方法',preference:'个人偏好',fact:'业务事实',none:'无需沉淀'};
    root.innerHTML=items.map(j=>`<article class="skill-job"><p><strong>${j.kind==='evaluation'?'独立评测':'方法提炼'}</strong> · ${labels[j.state]}${j.kind==='evaluation'?` · ${escapeHtml(j.name || '助手')} · 修订 ${escapeHtml(j.revision)} · 已完成 ${j.progress} 项`:''}</p>
      ${j.result?`<p>${escapeHtml(kinds[j.result.classification]||'')} ${escapeHtml(j.result.summary||j.result.message||'')}</p>`:''}
      ${j.error?`<p role="status">${escapeHtml(j.error)}</p>`:''}
      ${j.kind==='evaluation' && j.result?.rows ? `<details data-report-job="${escapeHtml(j.job_id)}"><summary>查看本次报告</summary>${skillReportHtml(j.result)}</details>` : ''}
      ${j.result?.draft_id?`<button type="button" class="secondary" data-open-draft="${escapeHtml(j.result.draft_id)}">查看草稿</button>`:''}
      ${(j.result?.similar||[]).map(x=>`<button type="button" class="secondary" data-open-draft="${escapeHtml(x.draft_id)}">相似草稿：${escapeHtml(x.name)}</button>`).join('')}
      ${j.result?.candidate?`<details data-candidate-job="${escapeHtml(j.job_id)}"><summary>查看候选方法并采用</summary><pre>${escapeHtml(j.result.candidate.instructions)}</pre><button type="button" class="secondary" data-adopt-job="${j.job_id}">另存为新草稿</button>${(j.result.similar||[]).map(x=>`<button type="button" class="secondary" data-preview-adopt="${escapeHtml(j.job_id)}" data-adopt-target="${escapeHtml(x.draft_id)}">查看替换“${escapeHtml(x.name)}”的差异</button>`).join('')}</details>`:''}
      ${['queued','running'].includes(j.state)?`<button type="button" class="secondary" data-job-action="cancel_job" data-job="${j.job_id}">取消</button>`:''}
      ${j.state==='failed'&&j.attempts<3?`<button type="button" class="secondary" data-job-action="retry_job" data-job="${j.job_id}">重试</button>`:''}</article>`).join('')||'<p>暂无后台任务。</p>';
    root.querySelectorAll('[data-report-job]').forEach(d => {d.open = openReports.has(d.dataset.reportJob);});
    root.querySelectorAll('[data-candidate-job]').forEach(d => { d.open = openCandidates.has(d.dataset.candidateJob); });
    root.querySelectorAll('[data-open-draft]').forEach(b=>b.onclick=()=>showSkillDraft(b.dataset.openDraft));
    root.querySelectorAll('[data-adopt-job]').forEach(b => {
      const pending = pendingAdoptions(view);
      b.onclick = async () => {
        const jobId = b.dataset.adoptJob;
        if (pending.has(jobId) || !scope.current()) return;
        pending.add(jobId); refreshAdoptionButtons(view);
        try {
          await navigateAfterAction(scope, async permit => {
            const saved = await draftAction('adopt', {job_id:jobId, request_key:crypto.randomUUID()});
            await reloadDraft(saved.draft_id, permit);
          });
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
    if (leaveGuard.busy()) return;
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
      const removals = skillCandidateRemovals(changes);
      const display = value => value === undefined ? '（无）' : typeof value === 'string' ? value : JSON.stringify(value, null, 2);
      root.innerHTML = `<h3>替换“${escapeHtml(target.bundle.manifest.name)}”前核对</h3>
        <p>目标：修订 ${expectedRevision}。候选会完整替换本草稿的方法、适用范围、模式与依赖、参考资料及必读清单；不会自动合并。旧修订保留，已发布版本不变。</p>
        <p>${changes.length ? `发现 ${changes.length} 项变化，请核对新增、修改和移除项。` : '候选与目标方法内容一致。'}</p>
        ${removals.length ? `<div role="note"><strong>替换将移除以下内容</strong><ul>${removals.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul><p>下方可展开查看完整前后内容。</p></div>` : ''}
        ${changes.map(c => `<details><summary>${escapeHtml(c.kind)} · ${escapeHtml(c.label)}</summary><p>当前修订 ${expectedRevision}</p><pre>${escapeHtml(display(c.before))}</pre><p>替换后</p><pre>${escapeHtml(display(c.after))}</pre></details>`).join('')}
        <div class="draft-actions"><button id="candidate-save-new" type="button" class="secondary">另存为新草稿</button><button id="candidate-replace" type="button">确认替换为新修订</button><button id="candidate-refresh" type="button" class="secondary">重新查看差异</button></div><p id="candidate-replace-status" role="status"></p>`;
      const pending = pendingAdoptions(view);
      const adopt = async replace => {
        if (pending.has(job.job_id) || !scope.current()) return;
        pending.add(job.job_id); refreshAdoptionButtons(view);
        try {
          await navigateAfterAction(scope, async permit => {
            // The revision belongs to the displayed comparison. Never re-read it
            // at confirmation: a newer draft must conflict, not be overwritten.
            const saved = await draftAction('adopt', {job_id:job.job_id, request_key:crypto.randomUUID(),
              ...(replace ? {draft_id:targetId, expected_revision:expectedRevision} : {})});
            scope.assertCurrent();
            await reloadDraft(saved.draft_id, permit);
          });
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
      <h4>独立评测</h4><p>仅使用合成文字，无业务工具。输入简短问题及可检查的期望；断言不会提供给被测模型。适用场景判断仅检查方法说明，不证明宿主实际选择或加载。</p>
      ${skillReportHtml(quality.evaluation, quality.comparison, true)}
      <p id="skill-regression-status"></p><div class="draft-actions">
      ${quality.evaluation ? '<button id="skill-pin-regression" type="button" class="secondary">将本次评测题目设为常用回归题</button>' : ''}
      <button id="skill-use-regression" type="button" class="secondary" hidden>使用常用回归题</button>
      <button id="skill-clear-regression" type="button" class="secondary" hidden>取消固定</button></div>
      ${quality.previous_report ? `<button id="skill-reuse-cases" class="secondary" type="button">用上次样例重测</button><p>来源：修订 ${quality.previous_report.revision} 的上次评测。仅复用问题和验收条件，不继承通过结论。</p>` : ''}
      <form id="skill-eval-form" class="draft-editor"><div id="skill-eval-cases"></div><div class="draft-actions"><button id="skill-add-case" type="button" class="secondary">添加样本</button><label>每项重复<select name="repeats"><option>1</option><option>2</option><option>3</option></select></label><button type="button" id="skill-save-pending" class="secondary">保存待测题目</button><button id="skill-eval-submit">启动独立评测</button></div><p id="skill-eval-state" role="status">可先保存待测题目，稍后再启动评测；保存不代表评测通过。</p></form>
      <h4>我的近期反馈</h4><p>反馈用于发现需要检查的问题，不代表独立评测通过。</p><button type="button" id="skill-refresh-feedback" class="secondary">刷新反馈进度</button><p id="skill-feedback-state" role="status"></p><div id="skill-feedback-list"></div>`;
    const reviewForms = [...root.querySelectorAll('[data-review-row]')];
    const reviewValue = form => JSON.stringify({status:form.querySelector('[name=review_status]').value,note:form.querySelector('[name=review_note]').value});
    for (const form of reviewForms) {
      let version = quality.evaluation.reviews?.[form.dataset.reviewRow]?.version || 0;
      form.dataset.savedReview = reviewValue(form);
      form.oninput = form.onchange = controls.reviewChanged;
      form.onsubmit = async event => {
        event.preventDefault();
        const value = reviewValue(form), fields = JSON.parse(value), status = form.querySelector('[role=status]');
        await controls.perform(async () => {
          try {
            const saved = await request('/api/skill-report-reviews', {method:'POST', csrf:true, scope,
              body:{job_id:quality.evaluation.job_id,row_key:form.dataset.reviewRow,...fields,expected_version:version}});
            scope.assertCurrent(); version = saved.version; form.dataset.savedReview = value;
            quality.evaluation.reviews ||= {}; quality.evaluation.reviews[form.dataset.reviewRow] = saved;
            const counts = {acceptable:0,problem:0,unreviewed:0};
            for(const row of quality.evaluation.rows) counts[quality.evaluation.reviews[row.key]?.status || 'unreviewed']++;
            root.querySelector('[data-review-summary]').textContent = `人工复核：${counts.acceptable} 项可接受，${counts.problem} 项有问题，${counts.unreviewed} 项未复核。复核只对本次结果有效。`;
            status.textContent = value === reviewValue(form) ? '复核已保存。' : '已保存刚才的复核，当前仍有未保存修改。';
            controls.reviewChanged();
            await refreshFeedback();
          } catch(error) {if(scope.current()&&!isCancellation(error))status.textContent=error.message;}
        });
      };
    }
    controls.setReviewForms(reviewForms);
    const coverage = skillPublicationCoverage(d, quality.evaluation, quality.publication_evidence);
    $('#skill-publication-profiles').innerHTML = '<legend>本次发布模式</legend>' +
      (quality.publication_evidence?.shared?.reason ? `<p>${escapeHtml(quality.publication_evidence.shared.reason)}</p>` : '') + coverage.map(c =>
      `<label><input type="checkbox" name="profiles" value="${escapeHtml(c.profile)}" ${c.eligible ? 'checked' : 'disabled'}> ${escapeHtml(profileLabel(c.profile))}（${escapeHtml(c.profile)}） — ${escapeHtml(c.evidence)}</label>`).join('');
    controls.setCoverage(coverage);
    $('#skill-recipients').innerHTML='<legend>使用范围（默认仅自己）</legend>'+recipients.items.map((x,i)=>`<label><input type="checkbox" name="audience" value="${escapeHtml(x.subject)}" ${i===0?'checked':''}>${escapeHtml(x.name)}</label>`).join('');
    const profiles = Object.keys(d.bundle.manifest.profiles);
    const evalError = error => {if (scope.current() && !isCancellation(error)) $('#skill-eval-state').textContent = error.message;};
    const add=(seed, source = '')=>{
      if (!scope.current()) return;
      const cases=$('#skill-eval-cases');if(cases.children.length>=12)return;
      const missingProfile = seed && !profiles.includes(seed.profile);
      const node=document.createElement('fieldset');node.dataset.feedbackId=seed?.feedback_id || '';node.innerHTML=`<legend>验收样例</legend><p data-case-source></p><label>问题<textarea name="prompt" required maxlength="3000" placeholder="整理成周报：计划修复登录问题。"></textarea></label>
        <label>模式<select name="profile" required>${missingProfile ? '<option value="">请选择当前修订的模式</option>' : ''}${profiles.map(p=>`<option value="${escapeHtml(p)}">${escapeHtml(profileLabel(p))}（${escapeHtml(p)}）</option>`).join('')}</select></label>
        <label>测试内容<select name="kind"><option value="output">回答质量</option><option value="trigger_true">适用场景：应该使用</option><option value="trigger_false">适用场景：不应使用</option></select></label>
        <label>应包含（每行一项）<textarea name="contains" placeholder="计划"></textarea></label><label>不应包含（每行一项）<textarea name="excludes" placeholder="已修复"></textarea></label><button type="button" class="secondary">移除此样本</button>`;
      node.querySelector('[data-case-source]').textContent = source + (missingProfile ? ` 原模式${seed.profile && seed.profile !== 'unknown' ? `“${seed.profile}”已不在当前修订` : '未记录'}，请明确选择当前模式。` : '');
      node.querySelector('[name=prompt]').value = seed?.prompt || '';
      node.querySelector('[name=profile]').value = missingProfile ? '' : seed?.profile || profiles[0];
      node.querySelector('[name=kind]').value = seed?.kind === 'trigger' ? seed.expected_trigger ? 'trigger_true' : 'trigger_false' : 'output';
      for (const key of ['contains', 'excludes']) node.querySelector(`[name=${key}]`).value = (seed?.[key] || []).join('\n');
      node.querySelector('button').onclick=()=>{node.remove();controls.evaluationChanged();};cases.append(node);controls.evaluationChanged();
    };
    const reuse = (seeds, source, repeats) => {
      try {
        controls.requireSaved();
        const container = $('#skill-eval-cases'), existing = [...container.children];
        const blank = existing.length === 1 && ['prompt', 'contains', 'excludes'].every(key => !existing[0].querySelector(`[name=${key}]`).value.trim());
        if (seeds.length + (blank ? 0 : existing.length) > 12) throw new Error('最多 12 个验收样例，请先移除不需要的样例。');
        if (blank) container.replaceChildren();
        for (const seed of seeds) add(seed, source);
        if (repeats) $('#skill-eval-form').querySelector('[name=repeats]').value = String(repeats);
        controls.evaluationChanged();
        $('#skill-eval-state').textContent = '已填入可编辑样例，尚未运行或保存。请核对模式，再补充“应包含/不应包含”的内容，之后启动评测。';
      } catch (error) { evalError(error); }
    };
    const carried = controls.evaluation();
    const stored = quality.pending_cases;
    let pendingVersion = stored?.version || 0;
    const restored = carried?.value || stored?.value;
    $('#skill-eval-cases').replaceChildren();
    if (restored?.cases.length) {
      for (const row of restored.cases) {
        add({prompt: row.prompt, profile: row.profile, kind: row.kind === 'output' ? 'output' : 'trigger',
          expected_trigger: row.kind === 'trigger_true', feedback_id: row.feedback_id, contains: row.contains.split('\n'), excludes: row.excludes.split('\n')}, carried ? '保留的待运行样例，请核对当前修订。' : `已保存待测题目，来源修订 ${stored.source_revision}${stored.source_revision !== d.revision ? '；修订已变化，请核对当前模式' : ''}。`);
      }
    } else add();
    $('#skill-eval-form').querySelector('[name=repeats]').value = restored?.repeats || '1';
    const readEvaluation = () => ({cases: [...$('#skill-eval-cases').children].map(node =>
      ({...Object.fromEntries(['prompt', 'profile', 'kind', 'contains', 'excludes'].map(key => [key, node.querySelector(`[name=${key}]`).value])), ...(node.dataset.feedbackId ? {feedback_id: node.dataset.feedbackId} : {})})),
      repeats: $('#skill-eval-form').querySelector('[name=repeats]').value || '1'});
    controls.setEvaluationReader(readEvaluation);
    if (stored && !carried) $('#skill-eval-state').textContent = `已恢复修订 ${stored.source_revision} 保存的待测题目；保存不代表已经运行或通过。`;
    $('#skill-save-pending').onclick = async () => {
      try {
        controls.requireSaved();
        const value = readEvaluation();
        await controls.perform(async () => {
          try {
            const saved = await request('/api/skill-pending-cases',{method:'POST',csrf:true,scope,
              body:{draft_id:d.draft_id,expected_revision:d.revision,expected_version:pendingVersion,value}});
            scope.assertCurrent();pendingVersion=saved.version;quality.pending_cases=saved;
            controls.evaluationSaved(value);
            $('#skill-eval-state').textContent = JSON.stringify(readEvaluation()) === JSON.stringify(value)
              ? '待测题目已保存，可以稍后回来继续；尚未运行，旧评测结论不适用于这些编辑。'
              : '刚才的待测题目已保存，当前仍有未保存修改。';
            await refreshFeedback();
          } catch(error) {evalError(error);}
        });
      } catch(error) {evalError(error);}
    };

    $('#skill-eval-form').oninput = controls.evaluationChanged;
    $('#skill-eval-form').onchange = controls.evaluationChanged;
    $('#skill-add-case').onclick=()=>add();
    const refreshRegression = () => {
      const fixed = quality.regression;
      $('#skill-use-regression').hidden = !fixed;
      $('#skill-clear-regression').hidden = !fixed;
      const comparable = quality.evaluation?.case_fingerprint;
      $('#skill-regression-status').textContent = fixed
        ? `常用回归题：修订 ${fixed.revision} 的 ${fixed.cases.length} 题，每项重复 ${fixed.repeats} 次。仅复用题目，不继承通过结论。` +
          (comparable ? comparable === fixed.case_fingerprint ? ' 题目、断言和重复次数一致；模型比较见报告。' : ' 本次评测的题目或条件已调整，分数不直接比较。' : '')
        : '可固定一组常用回归题，临时测试不会替换它。';
    };
    const pinRegression = async jobId => {
      try {
        controls.requireSaved();
        await controls.perform(async () => {
          quality.regression = await request('/api/skill-regressions', {method:'POST', csrf:true, scope,
            body:{draft_id:d.draft_id, expected_revision:d.revision, job_id:jobId}});
          scope.assertCurrent(); refreshRegression();
          // Pinning changes the comparison source, not the report or edited inputs.
          quality.comparison = jobId ? {same_report:true,regressed_cases:[]} : null;
          const comparison = root.querySelector('[data-report-comparison]');
          if(comparison) comparison.textContent = jobId ? comparisonText(quality.comparison) : '';
          const note = root.querySelector('[data-regression-note]'); if(note)note.textContent='';
          for(const summary of root.querySelectorAll('[data-report-case] > summary')) summary.textContent=summary.textContent.replace('上次通过 → 本次失败 · ','');
        });
      } catch(error) {evalError(error);}
    };
    if (quality.evaluation) $('#skill-pin-regression').onclick = () => pinRegression(quality.evaluation.job_id);
    $('#skill-clear-regression').onclick = () => pinRegression(null);
    $('#skill-use-regression').onclick = () => {if(quality.regression)reuse(quality.regression.cases,
      `常用回归题，来源修订 ${quality.regression.revision}；请核对当前模式。`,quality.regression.repeats);};
    refreshRegression();
    if (quality.previous_report) $('#skill-reuse-cases').onclick=()=>reuse(quality.previous_report.cases,
      `来源：修订 ${quality.previous_report.revision} 的上次评测；不继承通过结论。`, quality.previous_report.repeats);
    $('#skill-sample-list').querySelectorAll('[data-sample-case]').forEach(button => {
      button.onclick=()=>{const sample=d.tests.find(t=>t.test_id===button.dataset.sampleCase);if(sample)reuse([{prompt:sample.prompt,profile:sample.profile}], `来源：修订 ${sample.revision} 的${profileLabel(sample.profile)}试用样例。请补上“应包含/不应包含”的内容。`);};
    });
    const feedbackItems = [...(quality.feedback?.items || [])];
    const renderFeedback = () => {
      if (!scope.current()) return;
      const list = $('#skill-feedback-list');
      const expanded=new Set([...list.querySelectorAll('[data-feedback-id]')].filter(n=>n.open).map(n=>n.dataset.feedbackId));
      list.replaceChildren();
      if (!feedbackItems.length) list.textContent = '暂无反馈。可在上方已保存的试用样例中反馈效果。';
      for (const feedback of feedbackItems.slice(0, 20)) {
        const source = feedback.source || {}, item = document.createElement('details');
        item.dataset.feedbackId=feedback.feedback_id;item.open=expanded.has(feedback.feedback_id);
        const summary = document.createElement('summary');
        const version = source.revision ? `修订 ${source.revision}` : feedback.version?.startsWith('draft:') ? `修订 ${feedback.version.slice(6)}` : `版本 ${feedback.version || '未记录'}`;
        const mode = feedback.profile && feedback.profile !== 'unknown' ? profileLabel(feedback.profile) : '模式未记录';
        summary.textContent = `${version} · ${mode} · ${{useful:'有帮助',incorrect:'结果有误',not_applicable:'不适用'}[feedback.rating] || feedback.rating}`;
        const note = document.createElement('p'); note.textContent = `来源：${source.load_event_id ? '助手使用记录' : ({sample:'试用样例',binding:'任务使用',catalog:'目录反馈'}[source.kind] || '未记录')}。${feedback.comment || '未填写说明。'}`;
        item.append(summary, note);
        const progress=feedback.progress;
        if(progress){
          const p=document.createElement('p'),latest=progress.latest_job;
          const stateLabel={queued:'排队中',running:'执行中',succeeded:'已完成',failed:'失败',canceled:'已取消'};
          p.textContent = `${progress.pending_saved ? '已保存关联待测题目。' : ''}${latest ? `最近测试：修订 ${latest.revision} · ${stateLabel[latest.state] || latest.state}。` : '尚无关联评测。'}`;
          if(latest?.state==='succeeded') p.textContent += `关联题的候选断言 ${latest.candidate.passed}/${latest.candidate.total}；人工复核 ${latest.review.problem} 项有问题、${latest.review.unreviewed} 项未复核。测试结果不代表线上已修复。`;
          item.append(p);
          if(latest){
            const open=document.createElement('button'),result=document.createElement('div');open.type='button';open.className='secondary';open.textContent='查看对应结果';
            open.onclick=async()=>{open.disabled=true;try{const job=await draftAction('jobs',{job_id:latest.job_id});scope.assertCurrent();result.innerHTML=job.result?.rows?skillReportHtml(job.result):`<p>${escapeHtml(stateLabel[job.state] || job.state)}${job.error?'：'+escapeHtml(job.error):''}。尚无完整结果。</p>`;}catch(error){if(scope.current()&&!isCancellation(error))result.textContent=error.message;}finally{if(scope.current())open.disabled=false;}};
            item.append(open,result);
          }
        }
        if ((source.kind === 'sample' && source.prompt) || source.kind === 'binding') {
          const button = document.createElement('button'); button.type='button';button.className='secondary';button.textContent=source.kind === 'sample' ? '转成验收样例' : '整理成脱敏验收样例';
          button.onclick=()=>reuse([{prompt:source.prompt || '', profile:source.profile || feedback.profile, feedback_id:feedback.feedback_id}],
            `来源：${version} 的${mode}反馈。纠错说明：${feedback.comment || '未填写'} 请填写脱敏问题及“应包含/不应包含”；不自动复制业务原文，反馈不会自动成为验收条件。`);
          item.append(button);
        }
        list.append(item);
      }
    };
    let feedbackRequest = 0;
    async function refreshFeedback() {
      const current = ++feedbackRequest;
      try {
        const latest = await draftAction('inspect',{draft_id:d.draft_id});
        scope.assertCurrent();if(current!==feedbackRequest)return;
        feedbackItems.splice(0,feedbackItems.length,...(latest.feedback?.items || []));renderFeedback();
        $('#skill-feedback-state').textContent = `进度更新于 ${new Date().toLocaleTimeString()}；后台任务完成后可刷新。`;
      } catch(error) {if(scope.current()&&current===feedbackRequest&&!isCancellation(error))$('#skill-feedback-state').textContent='进度暂未更新，可刷新重试；已保存的内容不受影响。';}
    }
    $('#skill-refresh-feedback').onclick = refreshFeedback;
    renderFeedback();
    $('#skill-sample-list').querySelectorAll('[data-sample-feedback]').forEach(form => {
      let saving = false;
      form.onsubmit = async event => {
        event.preventDefault();
        if (saving || !scope.current()) return;
        const sample = d.tests.find(t => t.test_id === form.dataset.sampleFeedback);
        if (!sample || sample.status !== 'reported') return;
        const rating = form.querySelector('[name=rating]').value, comment = form.querySelector('[name=comment]').value.trim();
        const status = form.querySelector('[role=status]'), button = form.querySelector('button');
        if (!comment) { status.textContent = '请用一句话说明原因。'; return; }
        saving = true; button.disabled = true;
        try {
          const saved = await draftAction('feedback', {skill_id:d.skill_id, profile:sample.profile, rating, comment, test_id:sample.test_id, version:`draft:${sample.revision}`});
          scope.assertCurrent();
          status.textContent = '反馈已保存，可在下方转成验收样例。';
          feedbackItems.unshift({...saved, rating, comment}); renderFeedback();
        } catch (error) {if(scope.current()&&!isCancellation(error))status.textContent=error.message;}
        finally {saving=false;if(scope.current())button.disabled=false;}
      };
    });
    $('#skill-eval-form').onsubmit = async e => {
      e.preventDefault();
      try {
        controls.requireSaved();
        const cases = [...$('#skill-eval-cases').children].map(n => {
          const kind = n.querySelector('[name=kind]').value;
          return {prompt:n.querySelector('[name=prompt]').value, profile:n.querySelector('[name=profile]').value,
            ...(n.dataset.feedbackId ? {feedback_id:n.dataset.feedbackId} : {}),
            kind:kind === 'output' ? 'output' : 'trigger', ...(kind === 'output' ? {} : {expected_trigger:kind === 'trigger_true'}),
            ...Object.fromEntries(['contains','excludes'].map(k => [k,n.querySelector(`[name=${k}]`).value.split('\n').map(v => v.trim()).filter(Boolean)]))};
        });
        if (!cases.length) throw new Error('请添加至少一个验收样例。');
        for (const item of cases) {
          if (!profiles.includes(item.profile)) throw new Error('请为每个样例明确选择当前修订的模式。');
          if (!item.prompt.trim()) throw new Error('请填写样例问题。');
          if (item.kind === 'output' && !item.contains.length && !item.excludes.length) throw new Error('请为回答质量样例填写至少一项应包含或不应包含的断言。');
        }
        const repeats = Number($('#skill-eval-form').querySelector('[name=repeats]').value || 1);
        const submittedEvaluation = readEvaluation();
        await controls.perform(async () => {
          await draftAction('evaluate', {draft_id:d.draft_id, expected_revision:d.revision, cases, repeats, request_key:crypto.randomUUID()});
          scope.assertCurrent();
          controls.evaluationSaved(submittedEvaluation);
          $('#draft-detail-error').textContent=`修订 ${d.revision} 的评测已排队；完成后在后台任务中查看本次报告。`;
          await refreshSkillJobs();
          await refreshFeedback();
        });
      } catch(err) { if(scope.current() && !isCancellation(err)) $('#draft-detail-error').textContent=err.message; }
    };
    controls.refreshState();
    $('#skill-standard-export').onclick=async()=>{try{const pack=await draftAction('export_standard',{draft_id:d.draft_id});downloadJson(pack, "skill-files.json", scope);}catch(e){if(!scope.current()||isCancellation(e))return;$('#draft-detail-error').textContent=e.message;}};
  }

  function addSkillFeedback(card,item) {
    const scope = getScope();
    const draftAction = draftActions(scope);
    const eligible = profile => Object.hasOwn(item.profiles, profile) && (item.profiles[profile].available !== false || item.profiles[profile].needs_source);
    const details=document.createElement('details');details.innerHTML=`<summary>反馈此版本的效果</summary><p>版本 ${escapeHtml(item.version)} · 反馈仅用于改进方法</p><label>本次使用的模式<select name="profile" required><option value="">请选择具体模式</option>${Object.keys(item.profiles).map(profile=>`<option value="${escapeHtml(profile)}" ${eligible(profile)?'':'disabled'}>${escapeHtml(profileLabel(profile))}（${escapeHtml(profile)}）${eligible(profile)?'':' · 当前不可用'}</option>`).join('')}</select></label><label>效果评价<select name="rating"><option value="useful">有帮助</option><option value="incorrect">结果有误</option><option value="not_applicable">不适用</option></select></label><textarea aria-label="纠正说明" maxlength="1000" placeholder="哪里需要改进？"></textarea><button type="button" class="secondary">保存反馈</button><p role="status"></p>`;
    let saving = false;
    details.querySelector('button').onclick=async()=>{
      if (saving || !scope.current()) return;
      const profile=details.querySelector('[name=profile]').value;
      const status=details.querySelector('[role=status]'),button=details.querySelector('button');
      if(!eligible(profile)){status.textContent='请选择本次使用的具体可用模式。';return;}
      saving=true;button.disabled=true;
      try{await draftAction('feedback',{skill_id:item.id,version:item.version,profile,rating:details.querySelector('[name=rating]').value,comment:details.querySelector('textarea').value});scope.assertCurrent();status.textContent='已保存反馈。';}
      catch(e){if(scope.current()&&!isCancellation(e))status.textContent=e.message;}
      finally{saving=false;if(scope.current())button.disabled=false;}
    };
    card.append(details);
  }

  return { loadSkillDrafts, showSkillDraft, refreshSkillJobs, addSkillFeedback, canLeaveSkillEditor: leaveGuard.request, canAutoReloadSkillEditor: leaveGuard.canAutoReload };
}
