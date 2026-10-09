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
