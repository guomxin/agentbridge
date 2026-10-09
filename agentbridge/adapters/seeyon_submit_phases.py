from __future__ import annotations

import hashlib
import html
import json
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from agentbridge.adapters.base import AdapterBusinessRuleRejected


_MAX_EVIDENCE_ITEMS = 20


class SeeyonBusinessValidationRequired(RuntimeError):
    def __init__(self, validation: dict[str, Any]) -> None:
        super().__init__(
            str(validation.get("message") or "OA business validation required")
        )
        self.validation = dict(validation)


class SeeyonBusinessRuleRejected(AdapterBusinessRuleRejected):
    """The OA accepted the request but rejected it under a business rule."""

    error_code = "OA_BUSINESS_RULE_REJECTED"


def pump_browser_events(page, milliseconds: int = 250) -> None:
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if callable(wait_for_timeout):
        try:
            wait_for_timeout(milliseconds)
            return
        except Exception:
            pass
    time.sleep(milliseconds / 1000)


class SubmissionPhaseTracker:
    """Collect sanitized evidence for Seeyon CAP4 submission phases."""

    def __init__(
        self,
        authorized_validation_fingerprints: set[str] | None = None,
    ) -> None:
        self.evidence: list[dict[str, Any]] = []
        self._pending_business_validation: dict[str, Any] | None = None
        self._continued_validation_fingerprints: set[str] = set()
        self._authorized_validation_fingerprints = set(
            authorized_validation_fingerprints or set()
        )
        self._runtime_errors: list[str] = []

    def install_page_observers(self, page) -> None:
        for target, _frame_url in _page_targets(page):
            _install_prompt_hooks(target)

    def observe_response(self, response) -> None:
        request = getattr(response, "request", None)
        method = _attribute_value(request, "method")
        if str(method or "").upper() != "POST":
            return

        url = str(getattr(response, "url", "") or "")
        phase = _classify_phase(url, request)
        if phase is None or len(self.evidence) >= _MAX_EVIDENCE_ITEMS:
            return

        status = _attribute_value(response, "status")
        entry: dict[str, Any] = {
            "sequence": len(self.evidence) + 1,
            "phase": phase["phase"],
            "method": "POST",
            "endpoint": phase["endpoint"],
            "status": int(status or 0),
        }
        if phase.get("operation"):
            entry["operation"] = phase["operation"]
        if phase["phase"] == "cap4_form_save":
            outcome = _read_cap4_outcome(response)
            if outcome is None:
                entry["businessStatus"] = "unparsed"
            else:
                entry.update(outcome["evidence"])
                if outcome.get("validation") is not None:
                    self._pending_business_validation = outcome["validation"]
        self.evidence.append(entry)

    def observe_dialog(self, dialog) -> None:
        dialog_type = str(_attribute_value(dialog, "type") or "unknown").lower()
        message = _clean_validation_message(_attribute_value(dialog, "message"))
        if dialog_type == "beforeunload":
            try:
                dialog.accept()
            except Exception:
                pass
            return

        final_send_observed = any(
            item.get("phase") == "workflow_send" for item in self.evidence
        )
        if dialog_type == "alert" and final_send_observed:
            try:
                dialog.accept()
            except Exception as exc:
                self.observe_page_error(exc)
            if len(self.evidence) < _MAX_EVIDENCE_ITEMS:
                self.evidence.append(
                    {
                        "sequence": len(self.evidence) + 1,
                        "phase": "post_submit_dialog",
                        "method": "DIALOG",
                        "endpoint": "alert",
                        "status": 0,
                    }
                )
            return

        validation = {
            "code": (
                "NATIVE_CONFIRMATION"
                if dialog_type == "confirm"
                else "NATIVE_PAGE_BLOCKER"
            ),
            "message": message or f"OA opened a {dialog_type} dialog before submission.",
            "force_check": dialog_type != "confirm",
            "can_continue": dialog_type == "confirm",
            "control_kind": "native_dialog",
        }
        validation["fingerprint"] = business_validation_fingerprint(validation)
        authorized = (
            validation["can_continue"]
            and validation["fingerprint"] in self._authorized_validation_fingerprints
        )
        try:
            if authorized or dialog_type == "alert":
                dialog.accept()
            else:
                dialog.dismiss()
        except Exception as exc:
            self.observe_page_error(exc)
        if authorized:
            validation["control_already_activated"] = True
        self._record_validation(
            validation,
            phase="native_confirmation",
            method="DIALOG",
            endpoint=dialog_type,
        )

    def observe_page_confirmation(self, page) -> None:
        validation = _read_page_confirmation(page)
        if validation is None:
            return
        self._record_validation(
            validation,
            phase="pre_submit_confirmation",
            method="DOM",
            endpoint="page",
        )

    def observe_page_error(self, error) -> None:
        message = _clean_validation_message(str(error or ""))
        if message and message not in self._runtime_errors:
            self._runtime_errors.append(message[:300])

    def _record_validation(
        self,
        validation: dict[str, Any],
        *,
        phase: str,
        method: str,
        endpoint: str,
    ) -> None:
        if (
            self._pending_business_validation is not None
            and self._pending_business_validation.get("fingerprint")
            == validation["fingerprint"]
        ):
            return
        self._pending_business_validation = dict(validation)
        already_observed = any(
            item.get("validationFingerprint") == validation["fingerprint"]
            for item in self.evidence
        )
        if already_observed or len(self.evidence) >= _MAX_EVIDENCE_ITEMS:
            return
        self.evidence.append(
            {
                "sequence": len(self.evidence) + 1,
                "phase": phase,
                "method": method,
                "endpoint": endpoint,
                "status": 0,
                "businessStatus": "validation_required",
                "validationCode": validation["code"],
                "validationCanContinue": bool(validation.get("can_continue")),
                "validationFingerprint": validation["fingerprint"],
            }
        )

    @property
    def pending_business_validation(self) -> dict[str, Any] | None:
        if self._pending_business_validation is None:
            return None
        return dict(self._pending_business_validation)

    def mark_business_validation_continued(self) -> None:
        if self._pending_business_validation is not None:
            self._continued_validation_fingerprints.add(
                str(self._pending_business_validation.get("fingerprint") or "")
            )
        self._pending_business_validation = None

    def business_validation_was_continued(self, fingerprint: str) -> bool:
        return fingerprint in self._continued_validation_fingerprints

    def unknown_outcome_detail(self) -> str:
        if not self.evidence:
            detail = "No recognized OA submission response was observed after the send click."
        else:
            last = self.evidence[-1]
            final_send_observed = any(
                item.get("phase") == "workflow_send" for item in self.evidence
            )
            final_state = "observed" if final_send_observed else "not observed"
            detail = (
                f"Last observed OA submission phase: {last['phase']} "
                f"(HTTP {last['status']}); final workflow send was {final_state}."
            )
            cap4_entries = [
                item for item in self.evidence if item.get("phase") == "cap4_form_save"
            ]
            if cap4_entries:
                cap4 = cap4_entries[-1]
                detail += (
                    f" CAP4 save count: {len(cap4_entries)}; last CAP4 business status: "
                    f"{cap4.get('businessStatus', 'unparsed')}"
                )
                if cap4.get("businessCode"):
                    detail += f" (code {cap4['businessCode']})"
                detail += "."
        if self._runtime_errors:
            detail += f" Page runtime error: {self._runtime_errors[0]}"
        return detail

def business_validation_fingerprint(validation: dict[str, Any]) -> str:
    frozen = {
        "code": str(validation.get("code") or ""),
        "force_check": bool(validation.get("force_check")),
        "message": str(validation.get("message") or ""),
    }
    canonical = json.dumps(
        frozen,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _classify_phase(url: str, request) -> dict[str, str] | None:
    parsed = urlparse(url)
    path = parsed.path
    if path.endswith("/ajax.do"):
        query_method = str(parse_qs(parsed.query).get("method", [""])[0] or "")
        if query_method != "ajaxAction":
            return None
        post_data = str(_attribute_value(request, "post_data") or "")
        manager_method = str(parse_qs(post_data).get("managerMethod", [""])[0] or "")
        phase_by_method = {
            "checkAffairAndLock4NewColJson": "affair_lock_check",
            "checkTemplate": "template_check",
        }
        phase = phase_by_method.get(manager_method)
        if phase is None:
            return None
        return {
            "phase": phase,
            "endpoint": "/seeyon/ajax.do",
            "operation": manager_method,
        }
    if path.endswith("/rest/cap4/form/saveOrUpdate"):
        return {
            "phase": "cap4_form_save",
            "endpoint": "/seeyon/rest/cap4/form/saveOrUpdate",
        }
    if path.endswith("/collaboration/collaboration.do"):
        return {
            "phase": "workflow_send",
            "endpoint": "/seeyon/collaboration/collaboration.do",
            "operation": str(parse_qs(parsed.query).get("method", [""])[0] or ""),
        }
    return None


def _attribute_value(value, name: str):
    if value is None:
        return None
    result = getattr(value, name, None)
    return result() if callable(result) else result


def _read_cap4_outcome(response) -> dict[str, Any] | None:
    payload: Any = None
    try:
        payload = _attribute_value(response, "json")
    except Exception:
        pass
    if payload is None:
        try:
            raw_text = _attribute_value(response, "text")
            payload = json.loads(raw_text) if isinstance(raw_text, str) else None
        except Exception:
            return None
    outcome = _find_cap4_result(payload)
    if outcome is None:
        return None

    success = _as_int(outcome.get("success"))
    code = str(outcome.get("code") or "").strip()
    evidence: dict[str, Any] = {"businessSuccess": success}
    if code:
        evidence["businessCode"] = code
    if success in {1, 2}:
        evidence["businessStatus"] = "accepted"
        return {"evidence": evidence, "validation": None}
    validation = _sanitize_business_validation(outcome, code)
    evidence.update(
        {
            "businessStatus": (
                "validation_required"
                if code in {"3003", "3004", "3008"}
                else "rejected"
            ),
            "validationCode": code,
            "validationCanContinue": validation["can_continue"],
        }
    )
    return {"evidence": evidence, "validation": validation}

def _page_targets(page) -> list[tuple[Any, str]]:
    targets: list[tuple[Any, str]] = [(page, "")]
    try:
        frames = getattr(page, "frames", [])
        frames = frames() if callable(frames) else frames
        main_frame = getattr(page, "main_frame", None)
        main_frame = main_frame() if callable(main_frame) else main_frame
        targets.extend(
            (frame, str(getattr(frame, "url", "") or ""))
            for frame in list(frames or [])
            if frame is not main_frame
        )
    except Exception:
        pass
    return targets


def _install_prompt_hooks(target) -> None:
    try:
        target.evaluate(
            r"""
            () => {
              const queueName = '__agentbridgePromptEvents';
              if (!Array.isArray(window[queueName])) window[queueName] = [];
              const jq = window.$;
              if (!jq) return false;
              const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
              const capture = (kind, args) => {
                try {
                  const first = args[0];
                  const options = first && typeof first === 'object' ? first : {};
                  const message = clean(
                    typeof first === 'string'
                      ? first
                      : options.msg || options.message || options.title
                  ).slice(0, 1000);
                  const buttons = Array.isArray(options.buttons)
                    ? options.buttons.map((button) => ({
                        id: clean(button && button.id).slice(0, 80),
                        text: clean(button && button.text).slice(0, 80),
                      }))
                    : [];
                  window[queueName].push({kind, message, buttons});
                  if (window[queueName].length > 20) window[queueName].shift();
                } catch (_) {}
              };
              for (const name of ['alert', 'messageBox', 'confirm']) {
                const original = jq[name];
                if (typeof original !== 'function' || original.__agentbridgeWrapped) continue;
                const wrapped = function(...args) {
                  capture(name, args);
                  return original.apply(this, args);
                };
                try { Object.assign(wrapped, original); } catch (_) {}
                wrapped.__agentbridgeWrapped = true;
                jq[name] = wrapped;
              }
              return true;
            }
            """
        )
    except Exception:
        return


def _read_page_confirmation(page) -> dict[str, Any] | None:
    for target, frame_url in _page_targets(page):
        observed_hook = _read_hooked_prompt(target)
        if observed_hook is not None:
            return _hooked_prompt_validation(observed_hook, frame_url)

        observed = _evaluate_page_confirmation(target)
        if not isinstance(observed, dict):
            continue
        if observed.get("confirmText") != "\u7ee7\u7eed" or observed.get(
            "cancelText"
        ) != "\u53d6\u6d88":
            continue
        validation = {
            "code": "PRE_SUBMIT_CONFIRMATION",
            "message": _clean_validation_message(observed.get("message"))
            or "OA requested confirmation before saving the form.",
            "force_check": False,
            "can_continue": True,
            "control_selector": "#verifySure",
        }
        if frame_url:
            validation["control_frame_url"] = frame_url
        validation["fingerprint"] = business_validation_fingerprint(validation)
        return validation
    return None


def _read_hooked_prompt(target) -> dict[str, Any] | None:
    try:
        observed = target.evaluate(
            """
            () => {
              const queue = window.__agentbridgePromptEvents;
              return Array.isArray(queue) && queue.length ? queue.shift() : null;
            }
            """
        )
    except Exception:
        return None
    if not isinstance(observed, dict):
        return None
    if observed.get("kind") not in {"alert", "messageBox", "confirm"}:
        return None
    return observed


def _hooked_prompt_validation(
    observed: dict[str, Any],
    frame_url: str,
) -> dict[str, Any]:
    kind = str(observed.get("kind") or "")
    buttons = [
        item for item in observed.get("buttons") or [] if isinstance(item, dict)
    ]
    continue_button = next(
        (
            item
            for item in buttons
            if item.get("id") == "verifySure"
            or str(item.get("text") or "").strip() == "\u7ee7\u7eed"
        ),
        None,
    )
    can_continue = kind == "confirm" or continue_button is not None
    validation = {
        "code": (
            "PRE_SUBMIT_CONFIRMATION"
            if can_continue
            else "OA_PAGE_BLOCKER"
        ),
        "message": _clean_validation_message(observed.get("message"))
        or "OA stopped the submission with a page message.",
        "force_check": not can_continue,
        "can_continue": can_continue,
    }
    if continue_button is not None and continue_button.get("id") == "verifySure":
        validation["control_selector"] = "#verifySure"
    elif continue_button is not None:
        validation["control_text"] = "\u7ee7\u7eed"
    elif kind == "confirm":
        validation["control_text"] = "\u786e\u5b9a"
    if frame_url:
        validation["control_frame_url"] = frame_url
    validation["fingerprint"] = business_validation_fingerprint(validation)
    return validation


def _evaluate_page_confirmation(target) -> dict[str, Any] | None:
    try:
        return target.evaluate(
            r"""
            () => {
              const visible = (element) => {
                if (!element) return false;
                const style = getComputedStyle(element);
                const box = element.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && box.width > 0 && box.height > 0;
              };
              const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
              const sureButtons = [...document.querySelectorAll('[id="verifySure"]')]
                .filter(visible);
              const cancelButtons = [...document.querySelectorAll('[id="verifyCancel"]')]
                .filter(visible);
              for (const sure of sureButtons) {
                for (const cancel of cancelButtons) {
                  const confirmText = clean(sure.innerText || sure.textContent);
                  const cancelText = clean(cancel.innerText || cancel.textContent);
                  let message = '';
                  let node = sure.parentElement;
                  while (node && node !== document.body) {
                    if (node.contains(cancel)) {
                      let candidate = clean(node.innerText || node.textContent);
                      candidate = clean(candidate.split(confirmText).join(' '));
                      candidate = clean(candidate.split(cancelText).join(' '));
                      if (candidate) {
                        message = candidate;
                        break;
                      }
                    }
                    node = node.parentElement;
                  }
                  if (node) return {confirmText, cancelText, message};
                }
              }
              return null;
            }
            """
        )
    except Exception:
        return None

def _find_cap4_result(payload: Any) -> dict[str, Any] | None:
    queue: list[tuple[Any, int]] = [(payload, 0)]
    visited: set[int] = set()
    while queue and len(visited) < 100:
        candidate, depth = queue.pop(0)
        if depth > 6:
            continue
        if isinstance(candidate, str):
            stripped = candidate.strip()
            if stripped.startswith(("{", "[")):
                try:
                    queue.append((json.loads(stripped), depth + 1))
                except (TypeError, ValueError):
                    pass
            continue
        if not isinstance(candidate, (dict, list)):
            continue
        if id(candidate) in visited:
            continue
        visited.add(id(candidate))
        if isinstance(candidate, dict):
            if "success" in candidate and "code" in candidate:
                return candidate
            queue.extend(
                (value, depth + 1)
                for value in candidate.values()
                if isinstance(value, (dict, list, str))
            )
        else:
            queue.extend((value, depth + 1) for value in candidate)
    return None

def _sanitize_business_validation(
    outcome: dict[str, Any],
    code: str,
) -> dict[str, Any]:
    data = outcome.get("data") if isinstance(outcome.get("data"), dict) else {}
    message = ""
    force_check = True
    if code == "3003":
        raw_result = data.get("validateResult")
        try:
            result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        except (TypeError, ValueError):
            result = None
        if isinstance(result, dict):
            message = str(result.get("ruleError") or "")
            force_check = _as_int(result.get("forceCheck")) == 1
    elif code == "3004":
        unique = data.get("validateDataUnique")
        if isinstance(unique, dict):
            message = str(unique.get("msg") or "")
    elif code == "3008":
        message = str(outcome.get("message") or "")
    else:
        message = str(outcome.get("message") or "")

    clean_message = _clean_validation_message(message) or f"OA business validation {code}"
    validation = {
        "code": code,
        "message": clean_message,
        "force_check": force_check,
        "can_continue": code == "3003" and not force_check,
    }
    validation["fingerprint"] = business_validation_fingerprint(validation)
    return validation


def _clean_validation_message(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))
    return re.sub(r"\s+", " ", without_tags).strip()[:500]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
