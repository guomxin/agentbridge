/** Protect the live editor without retaining draft content after its scope ends. */
export function confirmSkillDiscard(document, scope) {
  return new Promise(resolve => {
    const dialog = document.createElement("dialog");
    dialog.id = "skill-leave-dialog";
    dialog.className = "skill-leave-dialog";
    dialog.setAttribute("aria-labelledby", "skill-leave-title");
    const title = document.createElement("h3"); title.id = "skill-leave-title"; title.textContent = "有未保存的草稿或验收样例修改";
    const note = document.createElement("p"); note.textContent = "离开会丢弃这些修改；已保存的修订不受影响。";
    const actions = document.createElement("div"); actions.className = "draft-actions";
    const stay = document.createElement("button"); stay.type = "button"; stay.textContent = "继续编辑";
    const leave = document.createElement("button"); leave.type = "button"; leave.className = "secondary"; leave.textContent = "放弃修改并离开";
    const previousFocus = document.activeElement;
    let settled = false, release = () => {};
    const finish = allowed => {
      if (settled) return;
      settled = true; release(); dialog.close(); dialog.remove();
      if (scope.current() && !allowed) previousFocus?.focus();
      resolve(allowed);
    };
    stay.addEventListener("click", () => finish(false));
    leave.addEventListener("click", () => finish(true));
    dialog.addEventListener("cancel", event => { event.preventDefault(); finish(false); });
    actions.append(stay, leave); dialog.append(title, note, actions);
    document.body.append(dialog); dialog.showModal(); stay.focus();
    release = scope.defer(() => finish(false));
  });
}

export function createSkillLeaveGuard({ eventTarget = globalThis, document = globalThis.document, confirm } = {}) {
  let editor = null, listening = false;
  let confirming = false;
  const operations = new Set();
  const permits = new WeakSet();
  const beforeUnload = event => {
    if (!editor?.isDirty()) return;
    event.preventDefault();
    event.returnValue = "";
  };
  function update() {
    const dirty = Boolean(editor?.isDirty());
    if (dirty === listening) return;
    listening = dirty;
    eventTarget[dirty ? "addEventListener" : "removeEventListener"]?.("beforeunload", beforeUnload);
  }
  function register(scope, callbacks) {
    const current = {...callbacks, scope};
    editor = current;
    scope.defer(() => { if (editor === current) { editor = null; update(); } });
    update();
  }
  // A successful action may redraw only the exact editor contents it handled.
  // This does not grant a general bypass to clicks while the action is pending.
  function capture() {
    const permit = {editor, snapshot: editor?.snapshot()};
    permits.add(permit);
    return permit;
  }
  async function request(permit) {
    if (permit && permits.has(permit)) {
      if ((!editor && (!permit.editor || !permit.editor.scope.current())) ||
          (editor === permit.editor && (!editor || editor.snapshot() === permit.snapshot))) return true;
      editor?.notify("内容又有变化，请继续编辑；本次操作已完成，请稍后刷新查看。");
      return false;
    }
    if (operations.size || editor?.isBusy()) {
      editor?.notify("正在处理，请稍候再离开。");
      return false;
    }
    if (!editor?.isDirty()) return true;
    if (confirming) return false;
    const current = editor, snapshot = editor.snapshot();
    confirming = true;
    try {
      const allowed = await (confirm ? confirm("有未保存的草稿或验收样例修改。确定：放弃修改并离开；取消：继续编辑。")
        : confirmSkillDiscard(document, current.scope));
      return Boolean(allowed && editor === current && current.scope.current() && current.snapshot() === snapshot && !operations.size && !current.isBusy());
    } finally { confirming = false; }
  }
  function begin(scope) {
    if (operations.size || editor?.isBusy()) return null;
    const operation = {};
    operations.add(operation);
    const finish = () => { operations.delete(operation); editor?.refresh(); };
    const release = scope.defer(finish);
    editor?.refresh();
    return () => { release(); finish(); };
  }
  return {register, update, capture, request, begin, busy: () => operations.size > 0,
    canAutoReload: () => !operations.size && !confirming && !editor?.isDirty() && !editor?.isBusy()};
}
