// The server supplies problems from the application’s frozen evidence. These
// reasons accept a known limitation; they never change a model score or review.
export function skillReviewDecision(form, requestId, candidateProblems = []) {
  const decision = form.get("decision");
  const result = {request_id: requestId, decision,
    reviewed_tests: form.get("reviewed_tests") === "on", reason: form.get("reason"),
    manual_quality_reason: form.get("manual_quality_reason") || null};
  if (decision === "approve" && candidateProblems.length) {
    result.problem_resolutions = Object.fromEntries(candidateProblems.map((problem, index) => {
      const reason = String(form.get(`problem_resolution_${index}`) || "").trim();
      if (!reason || reason.length > 1000) throw new Error(`请为第 ${index + 1} 项候选问题填写 1 至 1000 字的接受理由；也可选择退回修改或拒绝。`);
      return [problem.row_key, reason];
    }));
  }
  return result;
}

// The active dialog owns its action and completion; a replaced form cannot be
// enabled or receive errors from a previous submission.
export function createModalController({ modal, select, formData = form => new FormData(form) }) {
  let action = null;
  let revision = 0;
  let busy = false;
  let locked = false;
  return {
    captureSubmission() {
      if (!busy) return null;
      const expected = revision;
      return { current: () => expected === revision };
    },
    open({ kicker = "管理操作", title, body, submit = "确认", danger = false, action: next, locked: required = false }) {
      revision += 1;
      busy = false;
      locked = required;
      action = next;
      select("#modal-kicker").textContent = kicker;
      select("#modal-title").textContent = title;
      select("#modal-body").innerHTML = body;
      select("#modal-submit").textContent = submit;
      select("#modal-submit").className = `button ${danger ? "danger" : "primary"}`;
      select("#modal-submit").disabled = false;
      select("#modal-cancel").classList.toggle("hidden", locked);
      select("#modal-close").classList.toggle("hidden", locked);
      select("#modal-error").textContent = "";
      modal.showModal();
    },
    close() {
      revision += 1;
      action = null;
      busy = false;
      if (modal.open) modal.close();
    },
    cancel(event) {
      event.preventDefault();
      if (!locked) this.close();
    },
    async submit(event) {
      event.preventDefault();
      if (!action || busy) return;
      const expected = revision;
      const submit = action;
      busy = true;
      select("#modal-error").textContent = "";
      select("#modal-submit").disabled = true;
      try {
        await submit(formData(event.currentTarget));
      } catch (error) {
        if (revision === expected && error.name !== "AbortError") select("#modal-error").textContent = error.message;
      } finally {
        if (revision === expected) {
          busy = false;
          select("#modal-submit").disabled = false;
        }
      }
    },
  };
}
