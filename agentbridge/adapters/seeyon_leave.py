from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from agentbridge.adapters.seeyon_cap4 import wait_for_cap4_interactive


LEAVE_PREPARE_CAPABILITY = "oa.leave.prepare"
LEAVE_SAVE_CAPABILITY = "oa.leave.save_draft"
LEAVE_TEMPLATE_TITLE = "【HR】请假申请单"
LEAVE_TEMPLATE_ID = "-7765568933726502821"
LEAVE_FORM_APP_ID = "6773919591095560889"
LEAVE_CONTRACT_VERSION = "seeyon-leave-draft-v2"
LEAVE_SUPPORTED_TYPES = ("年休", "事假", "调休")

LEAVE_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "leave_type": {"type": "string", "enum": list(LEAVE_SUPPORTED_TYPES)},
        "start_time": {"type": "string", "maxLength": 32},
        "end_time": {"type": "string", "maxLength": 32},
        "reason": {"type": "string", "maxLength": 4000},
        "has_direct_supervisor": {"type": "boolean"},
        "input_submission_id": {"type": "string"},
    },
    "additionalProperties": False,
}

LEAVE_FIELD_CARD_SCHEMA = {
    "schema_version": "agentbridge.oa_leave_fields.v1",
    "title": "填写请假申请",
    "system": "致远 OA",
    "effect": "生成一份待确认的请假申请草稿计划",
    "submit_label": "提交字段",
    "notice": (
        "一期支持年休、事假和调休；请假天数与小时数由 OA 自动计算。"
        "字段提交后还需单独授权，最终只保存为待发草稿，不会进入审批流程。"
    ),
    "fields": [
        {
            "name": "leave_type",
            "label": "请假类型",
            "control": "select",
            "required": True,
            "options": [
                {"value": value, "label": value} for value in LEAVE_SUPPORTED_TYPES
            ],
        },
        {
            "name": "start_time",
            "label": "请假开始时间",
            "control": "datetime-local",
            "required": True,
        },
        {
            "name": "end_time",
            "label": "请假结束时间",
            "control": "datetime-local",
            "required": True,
        },
        {
            "name": "reason",
            "label": "请假事由",
            "control": "textarea",
            "required": True,
            "max_length": 4000,
            "rows": 4,
        },
        {
            "name": "has_direct_supervisor",
            "label": "是否有直接上级",
            "control": "segmented",
            "value_type": "boolean",
            "required": True,
            "options": [
                {"value": "true", "label": "是"},
                {"value": "false", "label": "否"},
            ],
        },
    ],
    "constraints": [
        {
            "kind": "datetime_after",
            "earlier": "start_time",
            "later": "end_time",
            "maximum_minutes": 527040,
            "message": "结束时间必须晚于开始时间，且请假时长不能超过 366 天。",
        }
    ],
}

LEAVE_SAVE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"authorization_id": {"type": "string"}},
    "required": ["authorization_id"],
    "additionalProperties": False,
}

_FIELD_CONTRACT = {
    "leave_type": {"field": "field0008", "label": "请假类型", "kind": "select"},
    "start_time": {"field": "field0006", "label": "请假开始时间", "kind": "datetime"},
    "end_time": {"field": "field0007", "label": "请假结束时间", "kind": "datetime"},
    "leave_days": {"field": "field0022", "label": "请假天数", "kind": "calculated"},
    "leave_hours": {"field": "field0023", "label": "请假小时数", "kind": "calculated"},
    "reason": {"field": "field0009", "label": "请假事由", "kind": "textarea"},
    "has_direct_supervisor": {
        "field": "field0010",
        "label": "是否有直接上级",
        "kind": "radio",
    },
}


class LeaveContractMismatch(RuntimeError):
    pass


class LeaveOutcomeUnknown(RuntimeError):
    pass


def prepare_leave_draft(adapter, worker, arguments: dict) -> dict:
    inputs = normalize_leave_inputs(arguments)
    template = _resolve_template(adapter.list_templates(worker))
    page, frame = _open_and_validate_form(worker, template)
    _validate_supported_option(frame, inputs["leave_type"])
    _fill_leave_form(page, frame, inputs)
    readback = _read_leave_form(page, frame)
    _assert_readback(inputs, readback, stage="prepare")
    return {
        "plan": {
            "schema_version": "agentbridge.oa_leave_plan.v1",
            "business_intent": "save_leave_request_draft",
            "target": {
                "template_title": template["title"],
                "template_id": template["template_id"],
                "form_app_id": template["form_app_id"],
                "launch_url": template["href"],
            },
            "form_contract": {
                "version": LEAVE_CONTRACT_VERSION,
                "fingerprint": leave_contract_fingerprint(),
                "fields": {
                    name: {"field": item["field"], "kind": item["kind"]}
                    for name, item in _FIELD_CONTRACT.items()
                },
                "save_control": "saveDraft_a",
                "forbidden_controls": ["sendId_a"],
            },
            "exact_input": inputs,
            "preconditions": {
                "template_resolved": True,
                "cap4_frame_loaded": True,
                "supported_leave_type_present": True,
                "form_fields_matched": True,
                "computed_duration_advisory": True,
                "save_draft_control_present": True,
                "send_control_present_but_forbidden": True,
                "page_title": str(page.title() or ""),
                "frame_module_id": _frame_module_id(frame.url),
            },
            "expected_effect": {
                "draft_saved": True,
                "workflow_submitted": False,
                "submitted_count": 0,
                "verification": "wait_send_identifiers_and_field_readback",
            },
        },
        "summary": leave_summary(inputs),
    }


def save_leave_draft(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
    timeout_seconds: float = 60,
) -> dict:
    _validate_frozen_plan(plan)
    inputs = normalize_leave_inputs(plan["exact_input"])
    template = _resolve_template(adapter.list_templates(worker))
    target = plan["target"]
    if any(
        (
            template["template_id"] != target.get("template_id"),
            template["form_app_id"] != target.get("form_app_id"),
            template["title"] != target.get("template_title"),
        )
    ):
        raise LeaveContractMismatch("The OA leave template changed after authorization.")
    page, frame = _open_and_validate_form(worker, template)
    _validate_supported_option(frame, inputs["leave_type"])
    _fill_leave_form(page, frame, inputs)
    precommit_readback = _read_leave_form(page, frame)
    _assert_readback(inputs, precommit_readback, stage="precommit")

    observed_requests: list[dict[str, Any]] = []
    click_started = False

    def observe_response(response) -> None:
        url = str(getattr(response, "url", "") or "")
        parsed = urlparse(url)
        if not parsed.path.endswith("/collaboration/collaboration.do"):
            return
        if str(parse_qs(parsed.query).get("method", [""])[0] or "") != "saveDraft":
            return
        request = getattr(response, "request", None)
        method = getattr(request, "method", "") if request is not None else ""
        if callable(method):
            method = method()
        if str(method or "").upper() != "POST":
            return
        observed_requests.append(
            {
                "method": "POST",
                "endpoint": "/seeyon/collaboration/collaboration.do?method=saveDraft",
                "status": int(getattr(response, "status", 0) or 0),
            }
        )

    page.on("response", observe_response)
    page.on("dialog", lambda dialog: dialog.accept())
    enter_commit_boundary()
    click_started = True
    try:
        page.locator("#saveDraft_a").click(timeout=10000)
        deadline = time.monotonic() + max(timeout_seconds, 5)
        while time.monotonic() < deadline:
            if _is_wait_send_url(page.url):
                break
            page.wait_for_timeout(250)
        if not _is_wait_send_url(page.url):
            raise LeaveOutcomeUnknown(
                "OA did not expose the wait-send readback URL after the leave draft save."
            )
        page.wait_for_load_state("domcontentloaded", timeout=max(timeout_seconds, 5) * 1000)
        saved_frame = _wait_for_cap4_frame(page, timeout_seconds=min(timeout_seconds, 30))
        _validate_form_controls(page, saved_frame)
        saved_readback = _read_leave_form(page, saved_frame)
        _assert_readback(inputs, saved_readback, stage="verification")
        identifiers = _wait_send_identifiers(page.url)
        if not identifiers["summary_id"] or not identifiers["affair_id"]:
            raise LeaveOutcomeUnknown(
                "OA reloaded the leave draft without stable summary and affair identifiers."
            )
        duration_evidence = _duration_evidence(saved_readback)
        return {
            "schema_version": "agentbridge.oa_leave_save_result.v1",
            "business_intent": "save_leave_request_draft",
            "draft_saved": True,
            "workflow_submitted": False,
            "submitted_count": 0,
            "draft": {
                "summary_id": identifiers["summary_id"],
                "affair_id": identifiers["affair_id"],
                "subject": saved_readback["subject"],
                "state": "wait_send",
            },
            "verification": {
                "confirmed": True,
                "method": "wait_send_identifiers_and_field_readback",
                "matched_fields": sorted(_expected_readback_fields()),
                "server_reloaded": True,
                "duration": duration_evidence,
            },
            "request_evidence": observed_requests,
            "transport": "central_browser_session",
        }
    except LeaveOutcomeUnknown:
        raise
    except BaseException as exc:
        if click_started:
            raise LeaveOutcomeUnknown(
                "The OA leave save-draft boundary was crossed, but verification failed."
            ) from exc
        raise


def normalize_leave_inputs(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("leave input must be an object")
    leave_type = _bounded_text(arguments.get("leave_type"), "leave_type", 20)
    if leave_type not in LEAVE_SUPPORTED_TYPES:
        allowed = ", ".join(LEAVE_SUPPORTED_TYPES)
        raise ValueError(
            f"leave_type must be one of the attachment-free first-phase types: {allowed}"
        )
    start = _parse_datetime(arguments.get("start_time"), "start_time")
    end = _parse_datetime(arguments.get("end_time"), "end_time")
    if end <= start:
        raise ValueError("end_time must be later than start_time")
    if (end - start).total_seconds() > 366 * 24 * 60 * 60:
        raise ValueError("leave duration must not exceed 366 days")
    has_direct_supervisor = arguments.get("has_direct_supervisor")
    if not isinstance(has_direct_supervisor, bool):
        raise ValueError("has_direct_supervisor must be boolean")
    return {
        "leave_type": leave_type,
        "start_time": start.strftime("%Y-%m-%d %H:%M"),
        "end_time": end.strftime("%Y-%m-%d %H:%M"),
        "reason": _bounded_text(arguments.get("reason"), "reason", 4000),
        "has_direct_supervisor": has_direct_supervisor,
    }


def leave_summary(inputs: dict) -> dict:
    return {
        "title": "保存请假申请草稿",
        "system": "致远 OA",
        "effect": "仅保存待发草稿",
        "authorization_notice": "授权后仅保存为待发草稿，不会发送或进入审批流程。",
        "authorize_label": "授权保存草稿",
        "fields": [
            {"label": "请假类型", "value": inputs["leave_type"]},
            {"label": "请假开始时间", "value": inputs["start_time"]},
            {"label": "请假结束时间", "value": inputs["end_time"]},
            {"label": "请假事由", "value": inputs["reason"]},
            {
                "label": "是否有直接上级",
                "value": "是" if inputs["has_direct_supervisor"] else "否",
            },
        ],
        "submitted_count": 0,
    }


def leave_contract_fingerprint() -> str:
    contract = {
        "version": LEAVE_CONTRACT_VERSION,
        "template_title": LEAVE_TEMPLATE_TITLE,
        "template_id": LEAVE_TEMPLATE_ID,
        "form_app_id": LEAVE_FORM_APP_ID,
        "fields": _FIELD_CONTRACT,
        "supported_types": LEAVE_SUPPORTED_TYPES,
        "save_control": "saveDraft_a",
        "forbidden_controls": ["sendId_a"],
    }
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _resolve_template(template_list: dict) -> dict:
    candidates = [
        item
        for item in template_list.get("items") or []
        if isinstance(item, dict) and item.get("title") == LEAVE_TEMPLATE_TITLE
    ]
    if len(candidates) != 1:
        raise LeaveContractMismatch("The OA leave template could not be resolved uniquely.")
    template = candidates[0]
    if (
        str(template.get("template_id") or "") != LEAVE_TEMPLATE_ID
        or str(template.get("form_app_id") or "") != LEAVE_FORM_APP_ID
    ):
        raise LeaveContractMismatch(
            "The OA leave template identity changed; rediscovery is required."
        )
    return template


def _open_and_validate_form(worker, template: dict):
    page = worker.goto(str(template["href"]), timeout_seconds=60)
    frame = _wait_for_cap4_frame(page, timeout_seconds=20)
    _dismiss_message_notices(page, frame)
    _validate_form_controls(page, frame)
    if _frame_module_id(frame.url) != LEAVE_TEMPLATE_ID:
        raise LeaveContractMismatch(
            "The CAP4 frame is not bound to the expected leave template."
        )
    return page, frame


def _dismiss_message_notices(page, frame) -> None:
    wait_for_cap4_interactive(
        page,
        frame,
        error_type=LeaveContractMismatch,
        context="The OA leave form",
    )


def _wait_for_cap4_frame(page, *, timeout_seconds: float):
    deadline = time.monotonic() + max(timeout_seconds, 1)
    while time.monotonic() < deadline:
        for frame in list(page.frames):
            if "/cap4/" not in str(frame.url or ""):
                continue
            try:
                if frame.locator("#field0008_id").count() == 1:
                    return frame
            except Exception:
                continue
        page.wait_for_timeout(100)
    raise LeaveContractMismatch("The OA CAP4 leave form did not load in time.")


def _validate_form_controls(page, frame) -> None:
    if page.locator("#saveDraft_a").count() != 1:
        raise LeaveContractMismatch("The OA leave save-draft control is missing.")
    if page.locator("#sendId_a").count() != 1:
        raise LeaveContractMismatch("The OA leave send control contract changed.")
    for item in _FIELD_CONTRACT.values():
        wrapper = frame.locator(f"#{item['field']}_id")
        if wrapper.count() != 1:
            raise LeaveContractMismatch(
                f"The OA leave field contract is missing {item['field']}."
            )
        text = str(wrapper.text_content(timeout=3000) or "")
        if item["label"] not in text:
            raise LeaveContractMismatch(f"The OA leave label changed for {item['field']}.")


def _validate_supported_option(frame, leave_type: str) -> None:
    _dismiss_message_notices(frame.page, frame)
    frame.locator("#field0008_id").click()
    try:
        if frame.get_by_text(leave_type, exact=True).count() < 1:
            raise LeaveContractMismatch(
                f"The OA leave form no longer exposes the supported type: {leave_type}."
            )
    finally:
        try:
            frame.page.keyboard.press("Escape")
        except Exception:
            pass


def _fill_leave_form(page, frame, inputs: dict) -> None:
    frame.evaluate(
        r"""
        ({ start, end }) => {
          const setValue = (element, value) => {
            if (!element) throw new Error('CAP4 datetime control is missing');
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(element, value);
            element.dispatchEvent(new Event('input', { bubbles: true }));
            element.dispatchEvent(new Event('change', { bubbles: true }));
            element.dispatchEvent(new Event('blur', { bubbles: true }));
          };
          setValue(document.querySelector('#field0006_format'), start);
          setValue(document.querySelector('#field0006'), start);
          setValue(document.querySelector('#field0007_format'), end);
          setValue(document.querySelector('#field0007'), end);
        }
        """,
        {"start": inputs["start_time"], "end": inputs["end_time"]},
    )
    _dismiss_message_notices(page, frame)
    frame.locator("#field0008_id").click()
    frame.get_by_text(inputs["leave_type"], exact=True).last.click()
    _dismiss_message_notices(page, frame)
    frame.locator("#field0009_id textarea:visible").first.fill(inputs["reason"])
    _dismiss_message_notices(page, frame)
    supervisor_label = "是" if inputs["has_direct_supervisor"] else "否"
    frame.locator("#field0010_id").get_by_text(supervisor_label, exact=True).last.click()
    page.wait_for_timeout(600)


def _read_leave_form(page, frame) -> dict:
    values = frame.evaluate(
        r"""
        () => {
          const value = (selector) => document.querySelector(selector)?.value || '';
          const editableValue = (fieldId) => {
            const wrapper = document.querySelector(`#${fieldId}_id`);
            if (!wrapper) return '';
            const controls = Array.from(wrapper.querySelectorAll('input,textarea'));
            const active = controls.find((element) => element.classList.contains('is-activeInput'));
            const editable = controls.find(
              (element) => !element.readOnly && getComputedStyle(element).display !== 'none'
            );
            return (active || editable || controls[0])?.value || '';
          };
          const calculatedValue = (fieldId) => {
            const wrapper = document.querySelector(`#${fieldId}_id`);
            if (!wrapper) return '';
            const browseValue = String(
              wrapper.querySelector('.cap4-number__browse')?.textContent || ''
            ).trim();
            if (browseValue) return browseValue;
            const values = Array.from(wrapper.querySelectorAll('input,textarea'))
              .map((element) => String(element.value || '').trim())
              .filter(Boolean);
            return values[0] || '';
          };
          const selectedRadio = (fieldId) => {
            const selected = document.querySelector(
              `#${fieldId}_id .cap-icon-danxuan-xuanzhong`
            );
            return String(selected?.closest('.cap4-radio__item')?.innerText || '')
              .replace(/\s+/g, ' ').trim();
          };
          return {
            leave_type: value('#field0008_inner'),
            start_time: value('#field0006_format'),
            end_time: value('#field0007_format'),
            leave_days: calculatedValue('field0022'),
            leave_hours: calculatedValue('field0023'),
            reason: editableValue('field0009'),
            supervisor_selection: selectedRadio('field0010'),
          };
        }
        """
    )
    values["has_direct_supervisor"] = _supervisor_choice_to_bool(
        values.pop("supervisor_selection", "")
    )
    values["subject"] = page.locator("#subject").input_value()
    return values


def _assert_readback(expected: dict, actual: dict, *, stage: str) -> None:
    mismatches = [
        field for field in _expected_readback_fields() if actual.get(field) != expected[field]
    ]
    if mismatches:
        message = f"OA leave {stage} readback mismatch: {', '.join(mismatches)}"
        if stage == "verification":
            raise LeaveOutcomeUnknown(message)
        raise LeaveContractMismatch(message)


def _duration_evidence(actual: dict) -> dict:
    leave_days = str(actual.get("leave_days") or "").strip()
    leave_hours = str(actual.get("leave_hours") or "").strip()
    return {
        "reported": bool(leave_days or leave_hours),
        "leave_days": leave_days,
        "leave_hours": leave_hours,
    }


def _expected_readback_fields() -> set[str]:
    return {"leave_type", "start_time", "end_time", "reason", "has_direct_supervisor"}


def _supervisor_choice_to_bool(value: Any) -> bool | None:
    choice = str(value or "").strip()
    if choice == "是":
        return True
    if choice == "否":
        return False
    return None


def _validate_frozen_plan(plan: dict) -> None:
    if not isinstance(plan, dict) or plan.get("business_intent") != "save_leave_request_draft":
        raise LeaveContractMismatch("The frozen plan is not a leave draft plan.")
    contract = plan.get("form_contract") if isinstance(plan.get("form_contract"), dict) else {}
    if (
        contract.get("version") != LEAVE_CONTRACT_VERSION
        or contract.get("fingerprint") != leave_contract_fingerprint()
    ):
        raise LeaveContractMismatch("The leave form contract changed after authorization.")


def _is_wait_send_url(url: str) -> bool:
    query = parse_qs(urlparse(str(url or "")).query, keep_blank_values=True)
    return query.get("from", [""])[0] == "waitSend"


def _wait_send_identifiers(url: str) -> dict:
    query = parse_qs(urlparse(str(url or "")).query, keep_blank_values=True)
    return {
        "summary_id": str(query.get("summaryId", [""])[0] or ""),
        "affair_id": str(query.get("affairId", [""])[0] or ""),
    }


def _frame_module_id(url: str) -> str:
    return str(parse_qs(urlparse(str(url or "")).query).get("moduleId", [""])[0] or "")


def _parse_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must use YYYY-MM-DD HH:MM")
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD HH:MM") from exc


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = re.sub(r"[\r\n\t]+", " ", value).strip()
    if not text:
        raise ValueError(f"{name} is required")
    if len(text) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    return text
