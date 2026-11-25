import { escapeHtml, fmtTime, table } from "./admin_presentation.mjs";

const states = {enabled:"接单中", draining:"停止接单", revoked:"已撤权"};
const capabilities = {chat:"对话", attachments:"图片附件", credential_card:"凭据卡", skill_trial:"技能试用",
  skills:"技能", skill_model:"技能模型"};
const modelStatus = {unverified:"尚无模型调用回执", last_succeeded:"最近模型调用成功", last_unconfirmed:"最近模型调用未确认"};
const html = escapeHtml;
const option = (key, label, selected = false) =>
  `<option value="${html(key)}" ${selected ? "selected" : ""}>${html(label)}</option>`;
const action = (id, kind, title) =>
  `<button type="button" class="button secondary small" data-host-id="${html(id)}" data-host-action="${kind}">${title}</button>`;

export function createHostDirectoryView({api, content, openModal, closeModal, toast, reasonField, current, refresh}) {
  let catalog;
  const probeResults = new Map();
  const post = (path, payload) => api("/api/workspace-hosts" + path, {method:"POST", body:JSON.stringify(payload)});

  async function render() {
    catalog = await api("/api/workspace-hosts");
    const rows = catalog.items.map(i => {
      const probe = probeResults.get(i.instance_id);
      const status = probe ? (probe.available ? "连接正常" : "连接失败") + " · " + fmtTime(probe.checked_at) : "尚未检查";
      const details = [
        probe?.capabilities?.map(name => capabilities[name] || name).join("、"),
        probe?.modelConfiguration === "configured" && "已读取模型配置",
        probe?.centralConnection === "verified" && "启动时已核验中央连接",
        modelStatus[probe?.modelInvocation],
        probe && !probe.available && probe.code,
      ].filter(Boolean).map(text => `<br><small>${html(text)}</small>`).join("");
      const buttons = [action(i.instance_id, "probe", "检查连接"), action(i.instance_id, "edit", "配置")];
      if (i.state === "enabled" && i.instance_id !== "openclaw-default" &&
          catalog.routes.some(route => route.route_ref === i.route_ref && route.host_type === i.host_type)) {
        buttons.push(action(i.instance_id, "bind", "开通用户"));
      }
      return `<tr><td><strong>${html(i.display_name)}</strong><br><small>${html(i.instance_id)}</small></td>
        <td>${html(i.host_type)} · ${html(i.adapter_version)}</td><td>${html(i.route_ref)}</td>
        <td>${html(states[i.state])}</td><td>${i.subjects === null ? "所有工作台账号" : html(i.subjects.join("、") || "未开放")}</td>
        <td>${html(status)}${details}</td><td><div class="actions">${buttons.join("")}</div></td></tr>`;
    });
    const bindings = catalog.bindings.map(b => {
      const instance = catalog.items.find(i => i.instance_id === b.instance_id);
      let telegram = "";
      if (instance?.host_type === "pi") {
        const route = catalog.routes.find(r => r.route_ref === instance.route_ref && r.host_type === "pi");
        const allowed = instance.state === "enabled" && instance.subjects?.includes(b.user_subject) &&
          route?.telegram_subjects?.includes(b.user_subject);
        telegram = `<br><small>Telegram 私聊${allowed ? "可配对" : "未开放"}</small>`;
      }
      return `<tr><td>${html(b.instance_id)}</td><td>${html(b.user_subject)}${telegram}</td><td>${html(b.accepted_level || "尚未登记")}</td><td class="code">${html(b.endpoint_id)}</td>
      <td>${action(b.instance_id, "details", "查看绑定").replace("data-host-id", `data-subject="${html(b.user_subject)}" data-host-id`)}</td></tr>`;
    });
    content.innerHTML = `<div class="toolbar"><p>按部署连接接入宿主，为工作台账号开通实例。</p>
      <button type="button" class="button primary" data-host-action="new">接入实例</button></div>
      ${catalog.route_error ? '<p class="form-error">部署的连接清单无效。请核对受控配置，可继续停止接单或撤权。</p>' : ""}
      ${table(["实例","宿主及适配器版本","连接引用","接单状态","开放主体","最近连接检查","操作"],rows)}
      <div class="view-head section-spaced"><h2>独立宿主用户绑定</h2></div>
      ${table(["实例","中央主体","协商等级","工作台端点","操作"],bindings)}`;
  }

  function edit(i) {
    const used = new Set(catalog.items.map(item => item.route_ref));
    const routes = catalog.routes.filter(r => i ? r.route_ref === i.route_ref : !used.has(r.route_ref));
    if (!i && !routes.length) return toast("部署清单中没有待接入的连接。", true);
    const currentStates = i && !routes.length ? ["draining","revoked"] : Object.keys(states);
    const routeField = i ? `<p>实例 ID：${html(i.instance_id)}　连接引用：${html(i.route_ref)}</p>`
      : `<label>部署连接<select name="route" required>${routes.map(r =>
          option(r.route_ref, r.instance_id + " · " + r.route_ref)).join("")}</select></label>`;
    const scopeField = `<label>开放范围<select name="scope">${option("selected","指定主体",i?.subjects !== null)}${option("all","所有工作台账号",i?.subjects === null)}</select></label>
      <label>开放主体（每行一个中央主体）<textarea rows="3" name="subjects">${html((i?.subjects || []).join("\n"))}</textarea></label>`;
    openModal({title:i ? "配置工作台宿主" : "接入工作台宿主", submit:"保存配置",
      body:routeField + `<label>显示名称<input name="display_name" maxlength="100" required value="${html(i?.display_name || "")}"></label>
        <label>接单状态<select name="state">${currentStates.map(s => option(s,states[s],i?.state === s)).join("")}</select></label>
        <p class="muted">停止接单允许原任务收尾。撤权会阻止后续受保护操作。</p>` + scopeField + reasonField(),
      action:async form => {
        const route = i || routes.find(r => r.route_ref === form.get("route"));
        await post("", {instance_id:route.instance_id, route_ref:route.route_ref, display_name:form.get("display_name"),
          state:form.get("state"), expected_revision:i?.revision || 0, reason:form.get("reason"),
          subjects:form.get("scope") === "all" ? null : form.get("subjects").split("\n").map(s=>s.trim()).filter(Boolean)});
        closeModal(); await refresh();
      }});
  }

  function details(binding) {
    openModal({title:"宿主端绑定信息",submit:"关闭",
      body:`<p>部署时使用下列绑定字段，并由宿主私有配置指定个人凭据文件。</p><pre>${html(JSON.stringify(binding,null,2))}</pre>`,
      action:async () => closeModal()});
  }

  async function act(target) {
    const kind = target.dataset.hostAction;
    const i = catalog.items.find(row => row.instance_id === target.dataset.hostId);
    if (kind === "new" || kind === "edit") return edit(i);
    if (!i) return;
    if (kind === "details") {
      const binding = catalog.bindings.find(b => b.instance_id === i.instance_id && b.user_subject === target.dataset.subject);
      if (binding) details(binding);
    }
    if (kind === "probe") {
      const result = await post("/probe", {instance_id:i.instance_id});
      probeResults.set(i.instance_id,result);
      if (current()) await refresh();
    }
    if (kind === "bind") {
      const route = catalog.routes.find(r => r.route_ref === i.route_ref);
      const accounts = catalog.accounts.filter(a => route?.subjects.includes(a.user_subject) &&
        (i.subjects === null || i.subjects.includes(a.user_subject)));
      if (!accounts.length) return toast("没有已注册、已开放且已配置个人凭据引用的工作台账号。", true);
      openModal({title:"开通用户 · " + i.display_name, submit:"确认开通",
        body:`<label>工作台账号<select name="account">${accounts.map(a => option(a.account_id,
          a.username + " · " + a.user_subject)).join("")}</select></label>` + reasonField(),
        action:async form => {
          const bound = await post("/bind", {instance_id:i.instance_id, account_id:form.get("account"),
            expected_revision:i.revision,reason:form.get("reason")});
          closeModal(); await refresh(); details(bound);
        }});
    }
  }
  return {render, act};
}
