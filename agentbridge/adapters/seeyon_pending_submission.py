from __future__ import annotations

import copy
import hashlib
import html
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from agentbridge.adapters.seeyon_submit_phases import (
    _find_cap4_result,
    _sanitize_business_validation,
)


_MAX_EVENTS = 20
_MAX_MESSAGES = 5
_MAX_MESSAGE_LENGTH = 300
_MAX_RESPONSE_LENGTH = 65536
# These confirmations only repeat the already authorized act of submission. Any
# additional text (amounts, routing, validation exceptions, etc.) needs review.
_STANDARD_SUBMISSION_CONFIRMATIONS = frozenset(
    {"是否提交", "确定提交吗", "确认提交", "是否确认提交", "确认提交吗", "确定要提交吗"}
)


class PendingSubmissionTracker:
    """Observe an approval attempt without treating transport evidence as success.

    OA approval endpoints have no established terminal rejection contract here.
    Even a CAP4 save rejection, alert, or HTTP error is diagnostic evidence only:
    it does not prove that another request could not have committed the action.
    The caller must independently verify the workflow identity and final state.
    """

    def __init__(
        self, authorized_confirmation_fingerprints: set[str] | None = None
    ) -> None:
        self._authorized_confirmations = set(authorized_confirmation_fingerprints or ())
        self._page = None
        self._listeners: list[tuple[str, Any]] = []
        self._responses: list[dict[str, Any]] = []
        self._runtime_errors: list[str] = []
        self._validations: list[dict[str, Any]] = []
        self._observation_errors: list[str] = []
        self._unhandled_confirmation = False

    def attach(self, page) -> None:
        if self._page is page:
            return
        if self._page is not None:
            raise RuntimeError("The pending submission tracker is already attached.")
        self._page = page
        try:
            for event, handler in (
                ("response", self.observe_response),
                ("pageerror", self.observe_page_error),
                ("dialog", self.observe_dialog),
                ("requestfailed", self.observe_request_failed),
            ):
                page.on(event, handler)
                self._listeners.append((event, handler))
        except Exception:
            self.detach()
            raise

    def detach(self) -> None:
        page, self._page = self._page, None
        listeners, self._listeners = self._listeners, []
        if page is None:
            return
        remove = getattr(page, "remove_listener", None) or getattr(page, "off", None)
        if callable(remove):
            for event, handler in listeners:
                try:
                    remove(event, handler)
                except Exception as exc:
                    self._record_observation_error(exc)

    def poll(self) -> None:
        """Read visible prompt text; never invoke or continue a page control."""
        if self._page is None:
            return
        for target in _page_targets(self._page):
            try:
                messages = target.evaluate(_VISIBLE_PROMPTS_SCRIPT)
            except Exception as exc:
                self._record_observation_error(exc)
                continue
            if not isinstance(messages, list):
                continue
            for message in messages[:_MAX_MESSAGES]:
                if not isinstance(message, dict):
                    continue
                kind = message.get("kind")
                if kind not in {"alert", "confirmation"}:
                    continue
                self._record_validation(kind, message.get("message"), source="page")

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "response_evidence": self._responses,
                "runtime_errors": self._runtime_errors,
                "validations": self._validations,
                "unhandled_confirmation": self._unhandled_confirmation,
                "observation_errors": self._observation_errors,
            }
        )

    @property
    def definitive_rejection(self) -> None:
        # Do not infer "not committed" from the absence of a recognized request.
        return None

    def unknown_outcome_detail(self) -> str:
        parts = []
        if self._validations:
            parts.append(f"OA page message: {self._validations[-1]['message']}")
        if self._runtime_errors:
            parts.append(f"Page runtime error: {self._runtime_errors[0]}")
        if self._responses:
            last = self._responses[-1]
            parts.append(
                f"Last observed OA response: {last['phase']} (HTTP {last['status']}); "
                "this does not establish the approval result."
            )
        if not parts:
            parts.append("No definitive OA approval response was observed.")
        if self._unhandled_confirmation:
            parts.append("OA requested an additional confirmation; it was not authorized.")
        return " ".join(parts)[:1000]

    def observe_response(self, response) -> None:
        if len(self._responses) >= _MAX_EVENTS:
            return
        try:
            request = _attribute(response, "request")
            phase = _request_phase(request, str(_attribute(response, "url") or ""))
            if phase is None:
                return
            status = _attribute(response, "status")
            entry: dict[str, Any] = {**phase, "status": int(status or 0)}
            if phase["phase"] == "cap4_form_save":
                self._record_cap4_outcome(response, entry)
            self._responses.append(entry)
        except Exception as exc:
            self._record_observation_error(exc)

    def observe_request_failed(self, request) -> None:
        if len(self._responses) >= _MAX_EVENTS:
            return
        try:
            phase = _request_phase(request, str(_attribute(request, "url") or ""))
            if phase is not None:
                self._responses.append(
                    {**phase, "status": 0, "transport_status": "request_failed"}
                )
        except Exception as exc:
            self._record_observation_error(exc)

    def observe_page_error(self, error) -> None:
        _append_message(self._runtime_errors, str(error or ""))

    def observe_dialog(self, dialog) -> None:
        try:
            kind = str(_attribute(dialog, "type") or "unknown").lower()
            message = str(_attribute(dialog, "message") or "")
            if kind == "beforeunload":
                dialog.accept()
                return
            if kind == "alert":
                self._record_validation("alert", message, source="native_dialog")
                dialog.accept()
                return
            fingerprint = confirmation_fingerprint(message)
            normalized = re.sub(r"[\s?？!！。.]", "", message)
            authorized = kind == "confirm" and (
                normalized in _STANDARD_SUBMISSION_CONFIRMATIONS
                or fingerprint in self._authorized_confirmations
            )
            self._record_validation(
                "confirmation", message, source="native_dialog", authorized=authorized
            )
            if authorized:
                dialog.accept()
            else:
                self._unhandled_confirmation = True
                dialog.dismiss()
        except Exception as exc:
            self._record_observation_error(exc)

    def _record_cap4_outcome(self, response, entry: dict[str, Any]) -> None:
        outcome = _find_cap4_result(_bounded_json(response))
        if outcome is None:
            entry["business_status"] = "unparsed"
            return
        success = outcome.get("success")
        code = str(outcome.get("code") or "")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", code):
            entry["business_code"] = code
        if success in (1, 2, "1", "2"):
            entry["business_status"] = "form_save_accepted"
            return
        if success not in (0, "0", False):
            entry["business_status"] = "unparsed"
            return
        validation = _sanitize_business_validation(outcome, code)
        entry["business_status"] = "form_save_validation"
        self._record_validation(
            "confirmation" if validation.get("can_continue") else "alert",
            validation.get("message"),
            source="cap4_form_save",
        )

    def _record_validation(
        self, kind: str, message: Any, *, source: str, authorized: bool = False
    ) -> None:
        clean = _clean_message(message)
        if not clean:
            return
        item = {"kind": kind, "message": clean, "source": source}
        if kind == "confirmation":
            item["fingerprint"] = confirmation_fingerprint(str(message or ""))
            item["authorized"] = authorized
            if not authorized:
                self._unhandled_confirmation = True
        if item not in self._validations and len(self._validations) < _MAX_MESSAGES:
            self._validations.append(item)

    def _record_observation_error(self, error) -> None:
        _append_message(self._observation_errors, str(error or ""))


def confirmation_fingerprint(message: str) -> str:
    # Hash the full text, before redaction/truncation, so different business
    # confirmations cannot accidentally share authorization after sanitization.
    normalized = re.sub(r"\s+", " ", str(message or "")).strip()
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _request_phase(request, url: str) -> dict[str, str] | None:
    if str(_attribute(request, "method") or "").upper() != "POST":
        return None
    parsed = urlparse(url)
    if parsed.path.endswith("/rest/cap4/form/saveOrUpdate"):
        return {"phase": "cap4_form_save", "endpoint": "/seeyon/rest/cap4/form/saveOrUpdate"}
    if parsed.path.endswith("/collaboration/collaboration.do"):
        method = parse_qs(parsed.query).get("method", [""])[0]
        phase = (
            "approval_endpoint_response" if method == "finishWorkItem"
            else "collaboration_response"
        )
        entry = {"phase": phase, "endpoint": "/seeyon/collaboration/collaboration.do"}
        if method == "finishWorkItem":
            entry["operation"] = "finishWorkItem"
        return entry
    if parsed.path.endswith("/ajax.do"):
        # An AJAX manager method is not a documented final approval contract.
        # Retain only the fixed endpoint; form data can contain user text/tokens.
        return {"phase": "ajax_response", "endpoint": "/seeyon/ajax.do"}
    return None


def _attribute(value, name: str):
    value = getattr(value, name, None)
    return value() if callable(value) else value


def _bounded_json(response):
    try:
        text = _attribute(response, "text")
        if isinstance(text, str):
            return json.loads(text) if len(text) <= _MAX_RESPONSE_LENGTH else None
        payload = _attribute(response, "json")
        return payload if len(json.dumps(payload)) <= _MAX_RESPONSE_LENGTH else None
    except Exception:
        # A redirected/closed page can make Playwright's response body
        # unavailable; retain the HTTP metadata as unparsed evidence.
        return None


def _append_message(messages: list[str], message: Any) -> None:
    clean = _clean_message(message)
    if clean and clean not in messages and len(messages) < _MAX_MESSAGES:
        messages.append(clean)


def _clean_message(value: Any) -> str:
    message = html.unescape(str(value or "")[:4096])
    message = re.sub(r"<[^>]+>", " ", message)
    message = re.sub(r"https?://[^\s<>\"']+", "[URL]", message, flags=re.I)
    message = re.sub(r"\bBearer\s+[^\s,;]+", "Bearer [redacted]", message, flags=re.I)
    message = re.sub(
        r'''(?i)(["']?(?:password|passwd|token|access_token|refresh_token|session(?:id)?|jsessionid|cookie|authorization|secret)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)''',
        r"\1[redacted]",
        message,
    )
    return re.sub(r"\s+", " ", message).strip()[:_MAX_MESSAGE_LENGTH]


def _page_targets(page) -> list[Any]:
    targets = [page]
    try:
        frames = list(_attribute(page, "frames") or [])
        main_frame = _attribute(page, "main_frame")
        targets.extend(frame for frame in frames[:10] if frame is not main_frame)
    except Exception:
        pass
    return targets


_VISIBLE_PROMPTS_SCRIPT = r"""/* agentbridge_pending_submission_prompts */
() => {
  const visible = (node) => {
    if (!node) return false;
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width > 0 && rect.height > 0;
  };
  const clean = (node) => String(node.innerText || node.textContent || '')
    .replace(/\s+/g, ' ').trim().slice(0, 1000);
  const result = [];
  for (const node of document.querySelectorAll('[role="alert"], [role="alertdialog"]')) {
    if (visible(node)) result.push({kind: 'alert', message: clean(node)});
    if (result.length >= 5) break;
  }
  const sure = document.getElementById('verifySure');
  const cancel = document.getElementById('verifyCancel');
  if (result.length < 5 && visible(sure) && visible(cancel)) {
    let node = sure.parentElement;
    while (node && node !== document.body && !node.contains(cancel)) node = node.parentElement;
    if (node && node !== document.body) {
      result.push({kind: 'confirmation', message: clean(node)});
    }
  }
  return result.slice(0, 5);
}
"""
