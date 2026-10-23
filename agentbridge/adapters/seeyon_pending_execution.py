from __future__ import annotations

import json
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from functools import wraps
from typing import Callable

from agentbridge.adapters.base import AdapterBusinessRuleRejected
from agentbridge.adapters.seeyon_pending_submission import PendingSubmissionTracker
from agentbridge.adapters.seeyon_pending_verification import VERIFICATION_METHOD, verify_pending_action
from agentbridge.adapters.seeyon_submit_phases import pump_browser_events
from agentbridge.browser.read_budget import (
    ReadBudgetExceeded, read_deadline_scope, check_read_deadline, remaining_ms,
)


_READBACK_REASONS = {
    "still_pending": "该事项仍在 OA 待办中",
    "pending_coverage_incomplete": "待办列表未读完整，不能证明目标已消失",
    "pending_coverage_inconsistent": "待办数量与完整性证据不一致",
    "done_affair_not_found": "待办中已消失，但尚未找到同一事项的已办记录",
    "done_identity_mismatch": "已办记录与授权事项的流程标识不一致",
    "done_detail_identity_unavailable": "已办详情缺少可核对的流程标识",
}

_READY_TIMEOUT_SECONDS = 15.0
_PREFLIGHT_TIMEOUT_SECONDS = 60.0
_SUBMIT_ENTRIES = {"submitClickFunc", "dealSubmitFunc", "$.content.callback.dealSubmit"}


class PendingActionNotReady(AdapterBusinessRuleRejected):
    """A bounded read-only preflight ended before authorization was consumed."""

    error_code = "OA_PENDING_ACTION_NOT_READY"


def observe_pending_phase(enter_commit_boundary, phase: str, reason: str | None = None) -> None:
    observer = getattr(enter_commit_boundary, "observe_pending_phase", None)
    if callable(observer):
        try:
            observer(phase, reason)
        except Exception:
            # Progress delivery is diagnostic; it cannot authorize or replay a write.
            pass


def pending_action_preflight(function):
    """Cover the first detail navigation through the last material validation."""
    @wraps(function)
    def bounded(*args, **kwargs):
        try:
            with read_deadline_scope(time.monotonic() + _PREFLIGHT_TIMEOUT_SECONDS):
                observe_pending_phase(kwargs.get("enter_commit_boundary"), "waiting_business_page")
                result = function(*args, **kwargs)
                # A successful write already has its own post-boundary proof; do
                # not reapply the expired preflight deadline on return.
                return result
        except ReadBudgetExceeded as exc:
            raise PendingActionNotReady("OA 提交前核对超时，尚未提交，授权未消耗。") from exc
    return bounded


def _readiness(page, target: dict, contract_error: type[Exception]) -> dict:
    check_read_deadline()
    observation = page.evaluate(PENDING_READINESS_SCRIPT, target)
    check_read_deadline()
    if not isinstance(observation, dict):
        raise PendingActionNotReady("OA 正文就绪状态不可读取，尚未提交。")
    if observation.get("identity_matches") is not True:
        raise contract_error("OA 事项或办理节点在等待正文期间变化，尚未提交。")
    return observation


def wait_pending_action_ready(page, target: dict, *, contract_error: type[Exception]) -> dict:
    """Wait for OA's business-body callback, not only DOM/material readiness."""
    started = time.monotonic()
    deadline = started + _READY_TIMEOUT_SECONDS
    while True:
        try:
            observation = _readiness(page, target, contract_error)
        except (contract_error, PendingActionNotReady, ReadBudgetExceeded):
            raise
        except Exception as exc:
            raise PendingActionNotReady("OA 正文状态读取失败，尚未提交。") from exc
        if observation.get("ready") is True:
            return {
                "state": "ready", "wait_ms": max(0, round((time.monotonic() - started) * 1000)),
                "body_loaded": observation.get("body_loaded") is True,
                "content_api_loaded": observation.get("content_api_loaded"),
                "submit_entry": observation.get("submit_entry"),
            }
        if time.monotonic() >= deadline:
            detail = {"execution": {
                "readiness": {"state": "not_ready", "reason": observation.get("reason"),
                              "wait_ms": max(0, round((time.monotonic() - started) * 1000))},
                "dispatch": {"state": "not_invoked"},
            }}
            raise PendingActionNotReady(
                "OA 正文或提交入口未在时限内就绪，尚未提交，授权未消耗。 "
                + json.dumps(detail, ensure_ascii=False, sort_keys=True)
            )
        pump_browser_events(page, remaining_ms(100, deadline=deadline))


@pending_action_preflight
def execute_pending_action(
    adapter,
    worker,
    *,
    target: dict,
    opinion: str,
    attitude_code: str,
    action_kind: str,
    enter_commit_boundary: Callable[[], None],
    contract_error: type[Exception],
    outcome_error: type[Exception],
    timeout_seconds: float = 60,
    revalidate_target: Callable[[], None],
) -> dict:
    """Submit once, preserving the action page until authoritative readback ends."""
    fork_page = getattr(worker, "fork_page", None)
    if not callable(fork_page):
        raise contract_error("无法建立独立核验页面，尚未提交 OA 待办。")
    readiness = wait_pending_action_ready(worker.page, target, contract_error=contract_error)
    execution = {"readiness": readiness, "dispatch": {"state": "not_invoked"},
                 "readback": {"attempts": 0, "last_observed_at": None}}
    # Provision the reader before consuming authorization or dispatching submission.
    with ExitStack() as stack:
        try:
            reader_context = fork_page()
            reader = reader_context.__enter__()
            stack.callback(_cleanup, reader_context.__exit__, None, None, None)
            if (reader is worker or getattr(reader, "page", None) is None
                    or reader.page is worker.page):
                raise ValueError("readback page must be independent")
        except Exception as exc:
            raise contract_error("无法建立独立核验页面，尚未提交 OA 待办。") from exc
        check_read_deadline()
        scope = getattr(adapter, "capability_scope", None)
        tracker = PendingSubmissionTracker()
        stack.callback(_cleanup, tracker.detach)
        tracker.attach(worker.page)
        observe_pending_phase(enter_commit_boundary, "final_revalidation")
        recovery_check = getattr(enter_commit_boundary, "before_pending_revalidation", None)
        if recovery_check is not None:
            recovery_check()
        # Reader provisioning may take time. Re-read the original source and
        # current materials afterwards, immediately before consumption. Never
        # navigate the action page and restart its business-body loader here.
        revalidate_target()
        check_read_deadline()
        if _readiness(worker.page, target, contract_error).get("ready") is not True:
            raise PendingActionNotReady("OA 正文在最终核对期间重新加载，尚未提交，授权未消耗。")
        check_read_deadline()
        enter_commit_boundary()
        last_observation = {"confirmed": False, "reason": "not_observed"}
        last_error = None
        try:
            # Keep the action worker's validated detail cache visible until the
            # final material recheck ends. The independent reader owns only
            # readback; entering its scope earlier forced an avoidable fork.
            if callable(scope):
                stack.enter_context(scope(reader))
            deadline = time.monotonic() + max(timeout_seconds, 5)
            with read_deadline_scope(deadline, inherit=False):
                scheduled = worker.page.evaluate(
                    PENDING_ACTION_SCRIPT,
                    {
                        "affair_id": target["affair_id"],
                        "opinion": opinion,
                        "attitude_code": attitude_code,
                        "action_kind": action_kind,
                        "identity": target,
                    },
                )
                if not isinstance(scheduled, dict) or scheduled.get("scheduled") is not True:
                    raise RuntimeError("OA did not acknowledge scheduling the submission")
                entry = scheduled.get("submit_entry")
                execution["dispatch"] = {"state": "scheduled", "submit_entry": entry if entry in _SUBMIT_ENTRIES else None}
                observe_pending_phase(enter_commit_boundary, "submission_scheduled")
                while time.monotonic() < deadline:
                    # Dispatch original-page dialogs/events while the separate reader navigates.
                    pump_browser_events(worker.page, remaining_ms(800))
                    check_read_deadline()
                    try:
                        tracker.poll()
                    except Exception:
                        # Observation failure cannot replace an authoritative result.
                        pass
                    try:
                        dispatch = worker.page.evaluate(PENDING_DISPATCH_OBSERVATION_SCRIPT)
                        if isinstance(dispatch, dict) and dispatch.get("state") in {"invoked", "not_invoked"}:
                            execution["dispatch"]["state"] = dispatch["state"]
                            if dispatch["state"] == "invoked":
                                observe_pending_phase(enter_commit_boundary, "submission_invoked")
                    except Exception:
                        # The submit page may have closed. Authoritative readback
                        # still owns success; observation loss never retries a write.
                        pass
                    if tracker.request_observed:
                        observe_pending_phase(enter_commit_boundary, "request_observed")
                    try:
                        execution["readback"]["attempts"] += 1
                        check_read_deadline()
                        observe_pending_phase(enter_commit_boundary, "verifying_result")
                        last_observation = verify_pending_action(adapter, reader, target, deadline=deadline)
                        execution["readback"].update({"last_observed_at": datetime.now(timezone.utc).isoformat(),
                                                      "reason": last_observation.get("reason")})
                        last_error = None
                    except Exception as exc:
                        # Read-only retries never dispatch a second submission.
                        last_error = type(exc).__name__
                        if isinstance(exc, ReadBudgetExceeded) or execution["dispatch"]["state"] == "not_invoked":
                            break
                        continue
                    if last_observation.get("confirmed") is True:
                        check_read_deadline()
                        observe_pending_phase(enter_commit_boundary, "verification_completed")
                        return {
                            "verification": last_observation,
                            "submission_evidence": _snapshot(tracker),
                            "execution": execution,
                        }
                    if execution["dispatch"]["state"] == "not_invoked":
                        # The final same-page guard refused to invoke OA. Keep the
                        # consumed attempt visible, but do not burn the readback
                        # minute or turn it into an automatic second submission.
                        break
                detail = {
                    "readback": last_observation,
                    "last_read_error": last_error,
                    "submission": _snapshot(tracker),
                    "execution": execution,
                }
                raise outcome_error(
                    "OA 已尝试提交，但未确认办理完成；已停止自动处理，请先核对，不要重复提交。 "
                    + _READBACK_REASONS.get(last_observation.get("reason"), "未取得完整的办理结果证据")
                    + "。 "
                    + json.dumps(detail, ensure_ascii=False, sort_keys=True)
                )
        except outcome_error:
            raise
        except AdapterBusinessRuleRejected:
            raise
        except Exception as exc:
            raise outcome_error(
                "OA 已进入提交边界，但未取得权威结果；请先核对，不要重复提交。 "
                + json.dumps(
                    {"error_type": type(exc).__name__, "submission": _snapshot(tracker),
                     "execution": execution},
                    ensure_ascii=False, sort_keys=True,
                )
            ) from exc


def _cleanup(callback, *args) -> None:
    try:
        callback(*args)
    except Exception:
        # The outer browser worker also closes its context. Cleanup cannot erase
        # confirmed business evidence or replace the original unknown result.
        pass


def _snapshot(tracker) -> dict:
    try:
        return tracker.snapshot()
    except Exception:
        return {"observation_errors": ["submission_evidence_unavailable"]}


_PENDING_READINESS_FUNCTION = r"""
(target) => {
  const read = (names) => {
    for (const name of names) {
      const element = document.querySelector(`#${name}`)
        || document.querySelector(`[name='${name}']`);
      const value = String(element?.value || window[name] || '').trim();
      if (value) return value;
    }
    return '';
  };
  const actual = {
    affair_id: read(['affairId']) || new URLSearchParams(location.search).get('affairId') || '',
    summary_id: read(['summaryId']), process_id: read(['processId']),
    template_id: read(['templeteId', 'templateId']), form_app_id: read(['formAppId']),
    form_record_id: read(['formRecordid', 'formRecordId']),
    node_policy: String(window.nodePolicy || ''), node_policy_name: String(window.nodePolicyName || ''),
  };
  const matches = location.pathname === '/seeyon/collaboration/collaboration.do'
    && ['affair_id', 'summary_id', 'process_id', 'template_id', 'form_app_id',
        'form_record_id', 'node_policy', 'node_policy_name'].every((key) =>
      target[key] === undefined || String(actual[key]) === String(target[key] || ''));
  let submitEntry = null;
  if ((actual.node_policy.toLowerCase() === 'inform' || actual.node_policy_name.includes('知会'))
      && typeof window.dealSubmitFunc === 'function') submitEntry = 'dealSubmitFunc';
  else if (typeof window.submitClickFunc === 'function') submitEntry = 'submitClickFunc';
  else if (typeof window.dealSubmitFunc === 'function') submitEntry = 'dealSubmitFunc';
  else if (typeof window.$?.content?.callback?.dealSubmit === 'function') submitEntry = '$.content.callback.dealSubmit';
  const bodyLoaded = window._hasExecuteContentLoad === true;
  let contentLoaded = null;
  try {
    const frame = document.getElementById('zwIframe');
    const api = frame?.contentWindow?.contentAPI;
    if (typeof api?.isContentLoaded === 'function') contentLoaded = api.isContentLoaded() === true;
  } catch (_) { contentLoaded = false; }
  const reason = !bodyLoaded ? 'body_loading' : contentLoaded === false ? 'content_loading'
    : !submitEntry ? 'submit_entry_unavailable' : document.readyState === 'loading' ? 'page_loading' : 'ready';
  return {identity_matches: matches, ready: matches && reason === 'ready', reason,
          body_loaded: bodyLoaded, content_api_loaded: contentLoaded, submit_entry: submitEntry};
}
"""

PENDING_READINESS_SCRIPT = "/* agentbridge_pending_readiness */\n" + _PENDING_READINESS_FUNCTION

PENDING_DISPATCH_OBSERVATION_SCRIPT = r"""/* agentbridge_pending_dispatch_observation */
() => ({state: window.__agentbridgePendingDispatch?.state || 'observation_unavailable'})
"""


PENDING_ACTION_SCRIPT = r"""
({ affair_id, opinion, attitude_code, identity }) => {
  const checkReady = __PENDING_READINESS_FUNCTION__;
  if (!checkReady(identity).ready) throw new Error('pending-action body is not ready before scheduling');
  const read = (names) => {
    for (const name of names) {
      const element = document.querySelector(`#${name}`)
        || document.querySelector(`[name='${name}']`);
      const value = String(element?.value || window[name] || '').trim();
      if (value) return value;
    }
    return '';
  };
  const pageAffairId = read(['affairId'])
    || new URLSearchParams(location.search).get('affairId')
    || '';
  if (String(pageAffairId) !== String(affair_id)) {
    throw new Error('affair_id mismatch before pending action');
  }
  const comment = document.querySelector('#content_deal_comment')
    || document.querySelector("textarea[name='content_deal_comment']")
    || document.querySelector('textarea#content')
    || document.querySelector("textarea[name='content']");
  if (!comment) throw new Error('pending-action opinion control is missing');
  const setValue = (element, value) => {
    const setter = Object.getOwnPropertyDescriptor(element.constructor.prototype, 'value')?.set;
    if (setter) setter.call(element, value); else element.value = value;
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
    element.dispatchEvent(new Event('blur', { bubbles: true }));
  };
  setValue(comment, String(opinion));
  if (attitude_code) {
    const radios = Array.from(document.querySelectorAll("input[type='radio'][name='attitude']"));
    const selected = radios.find((radio) => {
      const code = String(radio.getAttribute('code') || '').toLowerCase();
      const value = String(radio.value || '').toLowerCase();
      return code === attitude_code || value === attitude_code
        || value.endsWith(`.${attitude_code}`);
    });
    if (!selected) throw new Error('registered attitude is missing');
    selected.checked = true;
    selected.dispatchEvent(new Event('input', { bubbles: true }));
    selected.dispatchEvent(new Event('change', { bubbles: true }));
    const attitudeCode = selected.getAttribute('code') || selected.value || attitude_code;
    for (const selector of ['#hidAttitudeCode', '#hidAttitude', '#nodeattitude']) {
      const element = document.querySelector(selector);
      if (element) setValue(element, attitudeCode);
    }
  }
  const nodePolicy = String(window.nodePolicy || '').toLowerCase();
  const nodePolicyName = String(window.nodePolicyName || '');
  let submit = null;
  let submitName = '';
  if ((nodePolicy === 'inform' || nodePolicyName.includes('知会'))
      && typeof window.dealSubmitFunc === 'function') {
    submit = window.dealSubmitFunc;
    submitName = 'dealSubmitFunc';
  } else if (typeof window.submitClickFunc === 'function') {
    submit = window.submitClickFunc;
    submitName = 'submitClickFunc';
  } else if (typeof window.dealSubmitFunc === 'function') {
    submit = window.dealSubmitFunc;
    submitName = 'dealSubmitFunc';
  } else if (typeof window.$?.content?.callback?.dealSubmit === 'function') {
    submit = window.$.content.callback.dealSubmit;
    submitName = '$.content.callback.dealSubmit';
  }
  if (!submit) throw new Error('pending-action submit entry is missing');
  window.__agentbridgePendingDispatch = {state: 'scheduled'};
  window.setTimeout(() => {
    if (!checkReady(identity).ready) {
      window.__agentbridgePendingDispatch.state = 'not_invoked';
      return;
    }
    window.__agentbridgePendingDispatch.state = 'invoked';
    submit.call(window);
  }, 0);
  return { scheduled: true, submit_entry: submitName };
}
""".replace('__PENDING_READINESS_FUNCTION__', _PENDING_READINESS_FUNCTION)
