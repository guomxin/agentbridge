const labels = {preview: "草稿预览", fill: "准备填单", review: "总结与复核", track: "进展追踪", search: "案例检索", single: "单项办理", batch: "批量办理", use: "使用"};

export function skillTrialTitle(trial) {
  return `${trial.name || "助手草稿"} · 修订 ${trial.revision ?? trial.expected_revision} · ${labels[trial.profile] || trial.profile}（${trial.profile}）`;
}

export function skillTrialBinding(trial) {
  return trial ? {draft_id: trial.draft_id, expected_revision: trial.expected_revision, profile: trial.profile} : undefined;
}

/** Only a server receipt may describe a sample as saved. Model text is separate. */
export function renderSkillTrialStatus(document, owner, trial, pending = false) {
  if (!trial) return;
  if (!owner.trialStatus) {
    owner.trialStatus = document.createElement("p");
    owner.trialStatus.className = "skill-trial-status";
    owner.trialStatus.setAttribute("role", "status");
    owner.item.append(owner.trialStatus);
  }
  owner.trialStatus.textContent = `${pending ? "正在试用" : "短试用"}：${skillTrialTitle(trial)}。${pending
    ? "等待样例结果。" : trial.status === "reported" && trial.test_id
      ? "样例已保存，待人工复核。" : "样例未保存，请回到草稿检查试用记录。"}`;
}
