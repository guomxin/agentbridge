function agentbridgePageScript(payload) {
  return runSeeyonLaunchSaveDraft(payload);
}

function runSeeyonLaunchSaveDraft(payload) {
  const handlerVersion = "launch-save-draft-v4-page-script";
  const compactText = (value, max) => String(value || "").replace(/\s+/g, " ").trim().slice(0, max);
  const normalizeLookup = (value) => String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
  const controlText = (element) => element.innerText || element.value || element.getAttribute("title") || element.getAttribute("aria-label") || "";
  const labelIndex = () => {
    const labels = new Map();
    for (const label of Array.from(document.querySelectorAll("label[for]"))) {
      labels.set(label.getAttribute("for") || "", compactText(label.innerText || "", 200));
    }
    return labels;
  };
  const looksLikeSaveDraft = (code, text) => {
    const value = `${code || ""} ${text || ""}`.toLowerCase();
    return (
      value.includes("savedraft") ||
      value.includes("save draft") ||
      value.includes("\u4fdd\u5b58\u5f85\u53d1") ||
      value.includes("\u4fdd\u5b58\u8349\u7a3f") ||
      value.includes("\u6682\u5b58\u5f85\u53d1")
    );
  };
  const looksLikeForbiddenSubmit = (code, text) => {
    const value = `${code || ""} ${text || ""}`.toLowerCase();
    return (
      value.includes("sendid_a") ||
      value.includes("continuesubmit") ||
      value.includes("submit") ||
      value.includes("send") ||
      value.includes("\u53d1\u9001") ||
      value.includes("\u63d0\u4ea4")
    );
  };
  const findSaveDraftControl = () => {
    const controls = Array.from(document.querySelectorAll("button,input[type=button],input[type=submit],a"));
    for (const control of controls) {
      const code = `${control.id || ""} ${control.getAttribute("name") || ""}`;
      const text = controlText(control);
      if (looksLikeSaveDraft(code, text) && !looksLikeForbiddenSubmit(code, text)) {
        return control;
      }
    }
    return null;
  };
  const findLaunchField = (key) => {
    const wanted = normalizeLookup(key);
    const labels = labelIndex();
    for (const element of Array.from(document.querySelectorAll("input,textarea,select"))) {
      const type = (element.getAttribute("type") || "").toLowerCase();
      if (["hidden", "button", "submit", "reset", "file"].includes(type)) {
        continue;
      }
      if (element.disabled || element.readOnly) {
        continue;
      }
      const label = labels.get(element.id || "") || "";
      const candidates = [element.getAttribute("name"), element.id, label];
      if (candidates.some((candidate) => normalizeLookup(candidate) === wanted)) {
        return element;
      }
    }
    return null;
  };
  const fillLaunchField = (element, value) => {
    const tag = element.tagName.toLowerCase();
    const text = String(value ?? "");
    if (tag === "select") {
      const option = Array.from(element.options || []).find((item) => item.value === text || compactText(item.text, 200) === text);
      if (!option) {
        throw new Error(`select option not found for ${element.getAttribute("name") || element.id || "(unnamed)"}`);
      }
      element.value = option.value;
    } else if ((element.getAttribute("type") || "").toLowerCase() === "checkbox") {
      element.checked = ["1", "true", "yes", "on", "checked"].includes(text.toLowerCase());
    } else if ((element.getAttribute("type") || "").toLowerCase() === "radio") {
      element.checked = element.value === text;
    } else {
      const valueSetter = Object.getOwnPropertyDescriptor(element.constructor.prototype, "value")?.set;
      if (valueSetter) {
        valueSetter.call(element, text);
      } else {
        element.value = text;
      }
    }
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const describeField = (key, value, element) => ({
    input_key: String(key),
    name: element.getAttribute("name") || element.id || String(key),
    id: element.id || "",
    tag: element.tagName.toLowerCase(),
    type: element.getAttribute("type") || "",
    length: String(value ?? "").length,
  });
  const describeControl = (control) => ({
    id: control.id || "",
    name: control.getAttribute("name") || "",
    text: compactText(controlText(control), 200),
  });

  if (payload.confirm !== true) {
    throw new Error("confirm=true is required");
  }
  const expectedTemplateId = String(payload.template_id || "");
  if (expectedTemplateId) {
    const pageTemplateId = new URLSearchParams(location.search).get("templateId") || "";
    if (pageTemplateId && pageTemplateId !== expectedTemplateId) {
      throw new Error(`template_id mismatch: page=${pageTemplateId} expected=${expectedTemplateId}`);
    }
  }
  const fields = payload.fields || {};
  if (!fields || typeof fields !== "object" || Array.isArray(fields)) {
    throw new Error("fields must be an object");
  }

  const scheduledFields = [];
  for (const [key, value] of Object.entries(fields)) {
    const element = findLaunchField(key);
    if (!element) {
      throw new Error(`launch field not found: ${key}`);
    }
    scheduledFields.push(describeField(key, value, element));
  }

  const saveDraftControl = findSaveDraftControl();
  if (!saveDraftControl) {
    throw new Error("saveDraft control was not found; refused to click sendId_a or ContinueSubmit");
  }
  const clicked = describeControl(saveDraftControl);

  setTimeout(() => {
    const dialogs = [];
    const originalConfirm = window.confirm;
    const originalAlert = window.alert;
    window.confirm = function agentbridgeLaunchDraftConfirm(message) {
      dialogs.push({ type: "confirm", message: String(message || "").slice(0, 1000), accepted: true });
      return true;
    };
    window.alert = function agentbridgeLaunchDraftAlert(message) {
      dialogs.push({ type: "alert", message: String(message || "").slice(0, 1000) });
      return undefined;
    };
    const outcome = {
      ok: true,
      handler_version: handlerVersion,
      action: "SaveDraft",
      save_attempt_mode: "scheduled_fill_click_ack",
      submitted_count: 0,
      started_at: new Date().toISOString(),
      url: location.href,
      title: document.title,
    };
    try {
      const filledFields = [];
      for (const [key, value] of Object.entries(fields)) {
        const element = findLaunchField(key);
        if (!element) {
          throw new Error(`launch field not found during scheduled fill: ${key}`);
        }
        fillLaunchField(element, value);
        filledFields.push(describeField(key, value, element));
      }
      const freshSaveDraftControl = findSaveDraftControl();
      if (!freshSaveDraftControl) {
        throw new Error("saveDraft control disappeared before scheduled click");
      }
      outcome.filled_fields = filledFields;
      outcome.clicked = describeControl(freshSaveDraftControl);
      outcome.clicked_at = new Date().toISOString();
      freshSaveDraftControl.click();
    } catch (error) {
      outcome.ok = false;
      outcome.error = String(error && error.message ? error.message : error);
      console.error("AgentBridge scheduled Seeyon launch save-draft failed", error);
    } finally {
      outcome.dialogs = dialogs;
      window.__agentbridgeLaunchSaveDraftLast = outcome;
      setTimeout(() => {
        window.confirm = originalConfirm;
        window.alert = originalAlert;
      }, Number(payload.dialog_restore_ms || 3000));
    }
  }, 0);

  return {
    draft_saved: true,
    handler_version: handlerVersion,
    save_attempt_mode: "scheduled_fill_click_ack",
    fill_scheduled: true,
    click_scheduled: true,
    action: "SaveDraft",
    clicked,
    scheduled_fields: scheduledFields,
    submitted_count: 0,
    url: location.href,
    title: document.title,
    dialogs: [],
  };
}
