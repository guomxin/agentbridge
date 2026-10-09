function agentbridgePageScript(payload) {
  return runSeeyonContinueSubmit(payload);
}

function runSeeyonContinueSubmit(payload) {
  const handlerVersion = "continue-submit-v6-page-script";
  const expectedAffairId = String(payload.affair_id || "");
  const opinion = String(payload.opinion || "");
  const collectPageSignals = () => {
    const subject =
      document.querySelector("#subject")?.value ||
      document.querySelector("#title")?.value ||
      document.title ||
      "";
    return {
      node_policy: String(window.nodePolicy || ""),
      node_policy_name: String(window.nodePolicyName || ""),
      has_execute_content_load:
        typeof window._hasExecuteContentLoad === "undefined" ? null : window._hasExecuteContentLoad === true,
      subject: String(subject || "").slice(0, 300),
    };
  };
  const chooseSubmitEntry = (entries, pageSignals = {}) => {
    const nodePolicy = String(pageSignals.node_policy || "").toLowerCase();
    const nodePolicyName = String(pageSignals.node_policy_name || "");
    if ((nodePolicy === "inform" || nodePolicyName.includes("\u77e5\u4f1a")) && typeof entries.dealSubmitFunc === "function") {
      return { name: "dealSubmitFunc", fn: entries.dealSubmitFunc, reason: "inform_node_direct_deal_submit" };
    }
    if (typeof entries.submitClickFunc === "function") {
      return { name: "submitClickFunc", fn: entries.submitClickFunc, reason: "default_submit_click" };
    }
    if (typeof entries.dealSubmitFunc === "function") {
      return { name: "dealSubmitFunc", fn: entries.dealSubmitFunc, reason: "fallback_deal_submit" };
    }
    if (typeof entries.contentCallbackDealSubmit === "function") {
      return { name: "$.content.callback.dealSubmit", fn: entries.contentCallbackDealSubmit, reason: "fallback_content_callback" };
    }
    return null;
  };
  if (!expectedAffairId) {
    throw new Error("affair_id is required");
  }
  if (payload.confirm !== true) {
    throw new Error("confirm=true is required");
  }
  if (payload.action !== "ContinueSubmit") {
    throw new Error(`unsupported Seeyon write action: ${payload.action || ""}`);
  }

  const pageAffairId =
    String(window.affairId || "") ||
    document.querySelector("#affairId")?.value ||
    new URLSearchParams(location.search).get("affairId") ||
    "";
  if (String(pageAffairId) !== expectedAffairId) {
    throw new Error(`affair_id mismatch: page=${pageAffairId || "(empty)"} expected=${expectedAffairId}`);
  }
  const pageSignals = collectPageSignals();
  const submitEntry = chooseSubmitEntry(
    {
      submitClickFunc: window.submitClickFunc,
      dealSubmitFunc: window.dealSubmitFunc,
      contentCallbackDealSubmit: window.$?.content?.callback?.dealSubmit,
    },
    pageSignals,
  );
  if (!submitEntry) {
    throw new Error("Seeyon submit function submitClickFunc/dealSubmitFunc was not found on the detail page");
  }

  const dialogs = [];
  const findCommentElement = () => {
    const selectors = [
      "#content_deal_comment",
      "textarea[name='content_deal_comment']",
      "textarea#content",
      "textarea[name='content']",
    ];
    for (const selector of selectors) {
      const element = document.querySelector(selector);
      if (element) {
        return element;
      }
    }
    return null;
  };
  const setElementValue = (element, value) => {
    const valueSetter = Object.getOwnPropertyDescriptor(element.constructor.prototype, "value")?.set;
    if (valueSetter) {
      valueSetter.call(element, value);
    } else {
      element.value = value;
    }
    const eventView = element.ownerDocument?.defaultView || window;
    element.dispatchEvent(new eventView.Event("input", { bubbles: true }));
    element.dispatchEvent(new eventView.Event("change", { bubbles: true }));
    element.dispatchEvent(new eventView.Event("blur", { bubbles: true }));
  };
  const cap4InterviewSnapshot = () => {
    const frame = document.querySelector("#zwIframe");
    const frameDocument = frame?.contentDocument || null;
    const frameText = String(frameDocument?.body?.textContent || "");
    const frameHtml = String(frameDocument?.documentElement?.outerHTML || "");
    const pageText = `${document.title || ""} ${document.body?.innerText || ""}`;
    const field0038Present = Boolean(frameDocument?.querySelector("#field0038_id"));
    const field0041Present = Boolean(frameDocument?.querySelector("#field0041_id"));
    const hasInterviewMarker =
      pageText.includes("\u9762\u8bd5\u5ba1\u6279\u5355") ||
      frameText.includes("\u9762\u8bd5\u5ba1\u6279\u5355") ||
      frameHtml.includes("\u9762\u8bd5\u5ba1\u6279\u5355");
    const ready = Boolean(frameDocument) && hasInterviewMarker && field0038Present && field0041Present;
    return {
      frame_present: Boolean(frame),
      frame_document_present: Boolean(frameDocument),
      frame_text_length: frameText.length,
      frame_html_length: frameHtml.length,
      field0038_present: field0038Present,
      field0041_present: field0041Present,
      expects_interview_approval: hasInterviewMarker || (field0038Present && field0041Present),
      frameDocument,
      ready,
    };
  };
  const fillCap4InterviewApproval = () => {
    const result = {
      detected: false,
      opinion_set: false,
      trial_agree_clicked: false,
      selected_text: "",
      cap4_wait_attempts: 0,
      frame_present: false,
      frame_text_length: 0,
      frame_html_length: 0,
      field0038_present: false,
      field0041_present: false,
    };
    const readiness = cap4InterviewSnapshot();
    result.cap4_wait_attempts = payload.business_form_wait_result?.cap4_wait_attempts || 0;
    result.frame_present = readiness.frame_present === true;
    result.frame_text_length = readiness.frame_text_length || 0;
    result.frame_html_length = readiness.frame_html_length || 0;
    result.field0038_present = readiness.field0038_present === true;
    result.field0041_present = readiness.field0041_present === true;
    if (!readiness.ready) {
      if (readiness.expects_interview_approval === true) {
        throw new Error("CAP4 interview approval frame was not ready before submit");
      }
      return result;
    }
    result.detected = true;
    const frameDocument = readiness.frameDocument;
    const opinionField =
      frameDocument.querySelector("#field0038_id textarea:not([tabindex='-1'])") ||
      frameDocument.querySelector("#field0038_id textarea");
    if (opinionField) {
      opinionField.focus?.();
      setElementValue(opinionField, opinion);
      result.opinion_set = true;
    }
    const radioField = frameDocument.querySelector("#field0041_id");
    const trialAgreeItem = Array.from(radioField?.querySelectorAll(".cap4-radio__item") || []).find((item) => {
      const text = String(item.innerText || "").replace(/\s+/g, "");
      return text.includes("\u540c\u610f\u8bd5\u7528") && !text.includes("\u4e0d\u540c\u610f\u8bd5\u7528");
    });
    if (trialAgreeItem) {
      trialAgreeItem.click();
      result.trial_agree_clicked = true;
      result.selected_text = String(trialAgreeItem.innerText || "").replace(/\s+/g, " ").trim();
    }
    if (!result.opinion_set || !result.trial_agree_clicked) {
      throw new Error("CAP4 interview approval fields were detected but could not be filled");
    }
    return result;
  };
  const selectAgreeAttitude = () => {
    const result = {
      radio_selected: false,
      hidden_code_set: false,
      hidden_value_set: false,
      nodeattitude_set: false,
    };
    const radios = Array.from(document.querySelectorAll("input[type='radio'][name='attitude']"));
    const agreeRadio = radios.find((radio) => {
      const code = String(radio.getAttribute("code") || "").toLowerCase();
      const value = String(radio.value || "").toLowerCase();
      return code === "agree" || code === "haveread" || value === "agree" || value === "haveread";
    });
    if (agreeRadio) {
      agreeRadio.checked = true;
      agreeRadio.dispatchEvent(new Event("input", { bubbles: true }));
      agreeRadio.dispatchEvent(new Event("change", { bubbles: true }));
      result.radio_selected = true;
    }
    const attitudeCode = agreeRadio?.getAttribute("code") || agreeRadio?.value || "agree";
    const hiddenCode = document.querySelector("#hidAttitudeCode");
    if (hiddenCode) {
      setElementValue(hiddenCode, attitudeCode);
      result.hidden_code_set = true;
    }
    const hiddenValue = document.querySelector("#hidAttitude");
    if (hiddenValue) {
      setElementValue(hiddenValue, attitudeCode);
      result.hidden_value_set = true;
    }
    const nodeAttitude = document.querySelector("#nodeattitude");
    if (nodeAttitude) {
      setElementValue(nodeAttitude, attitudeCode);
      result.nodeattitude_set = true;
    }
    return result;
  };
  const originalConfirm = window.confirm;
  const originalAlert = window.alert;
  let dialogsRestored = false;
  const restoreDialogs = () => {
    if (dialogsRestored) {
      return;
    }
    dialogsRestored = true;
    window.confirm = originalConfirm;
    window.alert = originalAlert;
  };

  window.confirm = function agentbridgeWriteConfirm(message) {
    dialogs.push({ type: "confirm", message: String(message || "").slice(0, 1000), accepted: true });
    return true;
  };
  window.alert = function agentbridgeWriteAlert(message) {
    dialogs.push({ type: "alert", message: String(message || "").slice(0, 1000) });
    return undefined;
  };

  try {
    const comment = findCommentElement();
    if (!comment) {
      throw new Error("content_deal_comment was not found on the detail page");
    }
    setElementValue(comment, opinion);
    const attitude = selectAgreeAttitude();
    const businessForm = fillCap4InterviewApproval();
    const runSubmit = () => {
      const outcome = {
        ok: true,
        handler_version: handlerVersion,
        submit_scheduled: true,
        affair_id: expectedAffairId,
        action: "ContinueSubmit",
        opinion_length: opinion.length,
        url: location.href,
        title: document.title,
        submit_entry: submitEntry.name,
        submit_entry_reason: submitEntry.reason,
        page_signals: pageSignals,
        business_form: businessForm,
        dialogs,
      };
      try {
        const returned = submitEntry.fn.call(window);
        outcome.returned_promise = Boolean(returned && typeof returned.then === "function");
        if (outcome.returned_promise) {
          returned.catch((error) => {
            outcome.ok = false;
            outcome.error = String(error && error.message ? error.message : error);
          });
        }
      } catch (error) {
        outcome.ok = false;
        outcome.error = String(error && error.message ? error.message : error);
        console.error("AgentBridge scheduled Seeyon ContinueSubmit failed", error);
      } finally {
        window.__agentbridgeContinueSubmitLast = outcome;
        setTimeout(restoreDialogs, Number(payload.dialog_restore_ms || 3000));
      }
    };

    window.setTimeout(runSubmit, 0);
    return {
      submitted: true,
      handler_version: handlerVersion,
      submit_scheduled: true,
      affair_id: expectedAffairId,
      action: "ContinueSubmit",
      opinion_length: opinion.length,
      url: location.href,
      title: document.title,
      attitude,
      business_form: businessForm,
      submit_entry: submitEntry.name,
      submit_entry_reason: submitEntry.reason,
      page_signals: pageSignals,
      dialogs: [],
    };
  } catch (error) {
    restoreDialogs();
    throw error;
  }
}
