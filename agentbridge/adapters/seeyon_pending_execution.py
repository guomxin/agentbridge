from __future__ import annotations

import json
import time
from contextlib import ExitStack
from typing import Callable

from agentbridge.adapters.base import AdapterBusinessRuleRejected
from agentbridge.adapters.seeyon_pending_submission import PendingSubmissionTracker
from agentbridge.adapters.seeyon_pending_verification import VERIFICATION_METHOD, verify_pending_action
from agentbridge.adapters.seeyon_submit_phases import pump_browser_events


_READBACK_REASONS = {
    "still_pending": "该事项仍在 OA 待办中",
    "pending_coverage_incomplete": "待办列表未读完整，不能证明目标已消失",
    "pending_coverage_inconsistent": "待办数量与完整性证据不一致",
    "done_affair_not_found": "待办中已消失，但尚未找到同一事项的已办记录",
    "done_identity_mismatch": "已办记录与授权事项的流程标识不一致",
    "done_detail_identity_unavailable": "已办详情缺少可核对的流程标识",
}


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
) -> dict:
    """Submit once, preserving the action page until authoritative readback ends."""
    fork_page = getattr(worker, "fork_page", None)
    if not callable(fork_page):
        raise contract_error("无法建立独立核验页面，尚未提交 OA 待办。")
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
        tracker = PendingSubmissionTracker()
        stack.callback(_cleanup, tracker.detach)
        tracker.attach(worker.page)
        enter_commit_boundary()
        last_observation = {"confirmed": False, "reason": "not_observed"}
        last_error = None
        try:
            scheduled = worker.page.evaluate(
                PENDING_ACTION_SCRIPT,
                {
                    "affair_id": target["affair_id"],
                    "opinion": opinion,
                    "attitude_code": attitude_code,
                    "action_kind": action_kind,
                },
            )
            if not isinstance(scheduled, dict) or scheduled.get("scheduled") is not True:
                raise RuntimeError("OA did not acknowledge scheduling the submission")
            deadline = time.monotonic() + max(timeout_seconds, 5)
            while time.monotonic() < deadline:
                # Dispatch original-page dialogs/events while the separate reader navigates.
                pump_browser_events(worker.page, 800)
                try:
                    tracker.poll()
                except Exception:
                    # Observation failure cannot replace an authoritative result.
                    pass
                try:
                    last_observation = verify_pending_action(adapter, reader, target)
                    last_error = None
                except Exception as exc:
                    # Read-only retries never dispatch a second submission.
                    last_error = type(exc).__name__
                    continue
                if last_observation.get("confirmed") is True:
                    return {
                        "verification": last_observation,
                        "submission_evidence": _snapshot(tracker),
                    }
            detail = {
                "readback": last_observation,
                "last_read_error": last_error,
                "submission": _snapshot(tracker),
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
                    {"error_type": type(exc).__name__, "submission": _snapshot(tracker)},
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


PENDING_ACTION_SCRIPT = r"""
({ affair_id, opinion, attitude_code }) => {
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
  window.setTimeout(() => submit.call(window), 0);
  return { scheduled: true, submit_entry: submitName };
}
"""
