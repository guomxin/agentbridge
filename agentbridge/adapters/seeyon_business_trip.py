from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from agentbridge.adapters.seeyon_cap4 import wait_for_cap4_interactive


BUSINESS_TRIP_PREPARE_CAPABILITY = "oa.business_trip.prepare"
BUSINESS_TRIP_SAVE_CAPABILITY = "oa.business_trip.save_draft"
BUSINESS_TRIP_TEMPLATE_TITLE = "【HR】出差申请单"
BUSINESS_TRIP_TEMPLATE_ID = "2668910351205287097"
BUSINESS_TRIP_FORM_APP_ID = "4948077657800057670"
BUSINESS_TRIP_CONTRACT_VERSION = "seeyon-business-trip-draft-v2"
BUSINESS_TRIP_TRAVEL_MODES = ("大巴", "火车", "飞机", "轮渡", "自驾车")

BUSINESS_TRIP_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "start_time": {"type": "string", "maxLength": 32},
        "end_time": {"type": "string", "maxLength": 32},
        "travel_mode": {"type": "string", "enum": list(BUSINESS_TRIP_TRAVEL_MODES)},
        "origin": {"type": "string", "maxLength": 255},
        "destination": {"type": "string", "maxLength": 255},
        "reason": {"type": "string", "maxLength": 4000},
        "has_direct_supervisor": {"type": "boolean"},
        "input_submission_id": {"type": "string"},
    },
    "additionalProperties": False,
}

BUSINESS_TRIP_FIELD_CARD_SCHEMA = {
    "schema_version": "agentbridge.oa_business_trip_fields.v2",
    "title": "填写出差申请",
    "system": "致远 OA",
    "effect": "生成一份待确认的出差申请草稿计划",
    "submit_label": "提交字段",
    "notice": "字段提交后还需单独授权；最终只保存为待发草稿，不会发送或进入审批流程。",
    "fields": [
        {
            "name": "start_time",
            "label": "出差开始时间",
            "control": "datetime-local",
            "required": True,
        },
        {
            "name": "end_time",
            "label": "出差结束时间",
            "control": "datetime-local",
            "required": True,
        },
        {
            "name": "travel_mode",
            "label": "出差工具",
            "control": "select",
            "required": True,
            "options": [
                {"value": value, "label": value}
                for value in BUSINESS_TRIP_TRAVEL_MODES
            ],
        },
        {
            "name": "origin",
            "label": "出差始发地",
            "control": "text",
            "required": True,
            "max_length": 255,
            "autocomplete": "off",
        },
        {
            "name": "destination",
            "label": "出差目的地",
            "control": "text",
            "required": True,
            "max_length": 255,
            "autocomplete": "off",
        },
        {
            "name": "reason",
            "label": "出差事由",
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
            "message": "结束时间必须晚于开始时间，且出差时长不能超过 366 天。",
        }
    ],
}

BUSINESS_TRIP_SAVE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"authorization_id": {"type": "string"}},
    "required": ["authorization_id"],
    "additionalProperties": False,
}

_FIELD_CONTRACT = {
    "start_time": {"field": "field0006", "label": "出差开始时间", "kind": "datetime"},
    "end_time": {"field": "field0007", "label": "出差结束时间", "kind": "datetime"},
    "travel_mode": {"field": "field0027", "label": "出差工具", "kind": "select"},
    "origin": {"field": "field0023", "label": "出差始发地", "kind": "text"},
    "destination": {"field": "field0026", "label": "出差目的地", "kind": "text"},
    "trip_days": {"field": "field0029", "label": "出差天数", "kind": "decimal"},
    "trip_hours": {"field": "field0022", "label": "出差小时数", "kind": "decimal"},
    "reason": {"field": "field0009", "label": "出差事由", "kind": "textarea"},
    "has_direct_supervisor": {
        "field": "field0010",
        "label": "是否有直接上级",
        "kind": "radio",
    },
}


class BusinessTripContractMismatch(RuntimeError):
    pass


class BusinessTripOutcomeUnknown(RuntimeError):
    pass


def prepare_business_trip_draft(adapter, worker, arguments: dict) -> dict:
    inputs = normalize_business_trip_inputs(arguments)
    template = _resolve_template(adapter.list_templates(worker))
    page, frame = _open_and_validate_form(worker, template)
    _validate_optional_inputs(page, inputs)
    return {
        "plan": {
            "schema_version": "agentbridge.oa_business_trip_plan.v1",
            "business_intent": "save_business_trip_request_draft",
            "target": {
                "template_title": template["title"],
                "template_id": template["template_id"],
                "form_app_id": template["form_app_id"],
                "launch_url": template["href"],
            },
            "form_contract": {
                "version": BUSINESS_TRIP_CONTRACT_VERSION,
                "fingerprint": business_trip_contract_fingerprint(),
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
                "save_draft_control_present": True,
                "send_control_present_but_forbidden": True,
                "page_title": str(page.title() or ""),
                "frame_module_id": _frame_module_id(frame.url),
            },
            "expected_effect": {
                "draft_saved": True,
                "workflow_submitted": False,
                "submitted_count": 0,
                "verification": "wait_send_reload_and_field_readback",
            },
        },
        "summary": business_trip_summary(inputs),
    }


def save_business_trip_draft(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
    timeout_seconds: float = 60,
) -> dict:
    _validate_frozen_plan(plan)
    inputs = normalize_business_trip_inputs(plan["exact_input"])
    template = _resolve_template(adapter.list_templates(worker))
    target = plan["target"]
    if any(
        (
            template["template_id"] != target.get("template_id"),
            template["form_app_id"] != target.get("form_app_id"),
            template["title"] != target.get("template_title"),
        )
    ):
        raise BusinessTripContractMismatch(
            "The OA business-trip template changed after authorization."
        )
    page, frame = _open_and_validate_form(worker, template)
    _validate_optional_inputs(page, inputs)
    _fill_business_trip_form(page, frame, inputs)
    precommit_readback = _read_business_trip_form(page, frame)
    _assert_readback(inputs, precommit_readback, stage="precommit")

    observed_requests: list[dict[str, Any]] = []
    click_started = False

    def observe_response(response) -> None:
        url = str(getattr(response, "url", "") or "")
        if "collaboration.do?method=saveDraft" not in url:
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
            raise BusinessTripOutcomeUnknown(
                "OA did not expose the wait-send readback URL after the save-draft click."
            )
        page.wait_for_load_state("domcontentloaded", timeout=max(timeout_seconds, 5) * 1000)
        saved_frame = _wait_for_cap4_frame(page, timeout_seconds=min(timeout_seconds, 30))
        _validate_form_controls(page, saved_frame)
        saved_readback = _read_business_trip_form(page, saved_frame)
        _assert_readback(inputs, saved_readback, stage="verification")
        identifiers = _wait_send_identifiers(page.url)
        if not identifiers["summary_id"] or not identifiers["affair_id"]:
            raise BusinessTripOutcomeUnknown(
                "OA reloaded the draft but did not return stable summary and affair identifiers."
            )
        return {
            "schema_version": "agentbridge.oa_business_trip_save_result.v1",
            "business_intent": "save_business_trip_request_draft",
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
                "method": "wait_send_reload_and_field_readback",
                "matched_fields": sorted(_expected_readback_fields(inputs)),
                "server_reloaded": True,
            },
            "request_evidence": observed_requests,
            "transport": "central_browser_session",
        }
    except BusinessTripOutcomeUnknown:
        raise
    except BaseException as exc:
        if click_started:
            raise BusinessTripOutcomeUnknown(
                "The OA save-draft click crossed the commit boundary, but verification failed."
            ) from exc
        raise


def normalize_business_trip_inputs(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("business-trip input must be an object")
    start = _parse_business_datetime(arguments.get("start_time"), "start_time")
    end = _parse_business_datetime(arguments.get("end_time"), "end_time")
    if end <= start:
        raise ValueError("end_time must be later than start_time")
    if (end - start).total_seconds() > 366 * 24 * 60 * 60:
        raise ValueError("business trip duration must not exceed 366 days")
    travel_mode = _bounded_text(arguments.get("travel_mode"), "travel_mode", 20)
    if travel_mode not in BUSINESS_TRIP_TRAVEL_MODES:
        allowed = ", ".join(BUSINESS_TRIP_TRAVEL_MODES)
        raise ValueError(f"travel_mode must be one of: {allowed}")
    normalized = {
        "start_time": start.strftime("%Y-%m-%d %H:%M"),
        "end_time": end.strftime("%Y-%m-%d %H:%M"),
        "travel_mode": travel_mode,
        "origin": _bounded_text(arguments.get("origin"), "origin", 255),
        "destination": _bounded_text(arguments.get("destination"), "destination", 255),
        "reason": _bounded_text(arguments.get("reason"), "reason", 4000),
    }
    has_direct_supervisor = arguments.get("has_direct_supervisor")
    if not isinstance(has_direct_supervisor, bool):
        raise ValueError("has_direct_supervisor must be boolean")
    normalized["has_direct_supervisor"] = has_direct_supervisor
    for name, maximum in (("trip_days", 366), ("trip_hours", 8784)):
        value = arguments.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        if value < 0 or value > maximum:
            raise ValueError(f"{name} is outside the supported range")
        # OA recalculates duration after date changes. Validate legacy payloads,
        # but never drive the form or authorization plan with these values.
    note = arguments.get("note")
    if note is not None:
        normalized["note"] = _bounded_text(note, "note", 2000, allow_blank=True)
    return normalized


def business_trip_summary(inputs: dict) -> dict:
    fields = [
        {"label": "出差开始时间", "value": inputs["start_time"]},
        {"label": "出差结束时间", "value": inputs["end_time"]},
        {"label": "出行工具", "value": inputs["travel_mode"]},
        {"label": "出差始发地", "value": inputs["origin"]},
        {"label": "出差目的地", "value": inputs["destination"]},
        {"label": "事由", "value": inputs["reason"]},
        {
            "label": "是否有直接上级",
            "value": "是" if inputs["has_direct_supervisor"] else "否",
        },
    ]
    if "trip_days" in inputs:
        fields.append({"label": "出差天数", "value": str(inputs["trip_days"])})
    if "trip_hours" in inputs:
        fields.append({"label": "出差小时数", "value": str(inputs["trip_hours"])})
    if inputs.get("note"):
        fields.append({"label": "附言", "value": inputs["note"]})
    return {
        "title": "保存出差申请草稿",
        "system": "致远 OA",
        "effect": "仅保存待发草稿",
        "authorization_notice": "授权后仅保存为待发草稿，不会发送、提交或进入审批流程。",
        "authorize_label": "授权保存草稿",
        "fields": fields,
        "submitted_count": 0,
    }


def business_trip_contract_fingerprint() -> str:
    contract = {
        "version": BUSINESS_TRIP_CONTRACT_VERSION,
        "template_title": BUSINESS_TRIP_TEMPLATE_TITLE,
        "template_id": BUSINESS_TRIP_TEMPLATE_ID,
        "form_app_id": BUSINESS_TRIP_FORM_APP_ID,
        "fields": _FIELD_CONTRACT,
        "travel_modes": BUSINESS_TRIP_TRAVEL_MODES,
        "save_control": "saveDraft_a",
        "forbidden_controls": ["sendId_a"],
    }
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _resolve_template(template_list: dict) -> dict:
    candidates = [
        item
        for item in template_list.get("items") or []
        if isinstance(item, dict) and item.get("title") == BUSINESS_TRIP_TEMPLATE_TITLE
    ]
    if len(candidates) != 1:
        raise BusinessTripContractMismatch(
            "The OA business-trip template could not be resolved uniquely."
        )
    template = candidates[0]
    if (
        str(template.get("template_id") or "") != BUSINESS_TRIP_TEMPLATE_ID
        or str(template.get("form_app_id") or "") != BUSINESS_TRIP_FORM_APP_ID
    ):
        raise BusinessTripContractMismatch(
            "The OA business-trip template identity changed; rediscovery is required."
        )
    return template


def _open_and_validate_form(worker, template: dict):
    page = worker.goto(str(template["href"]), timeout_seconds=60)
    frame = _wait_for_cap4_frame(page, timeout_seconds=20)
    _wait_for_interactive_form(page, frame)
    _validate_form_controls(page, frame)
    if _frame_module_id(frame.url) != BUSINESS_TRIP_TEMPLATE_ID:
        raise BusinessTripContractMismatch(
            "The CAP4 frame is not bound to the expected business-trip template."
        )
    return page, frame


def _wait_for_cap4_frame(page, *, timeout_seconds: float):
    deadline = time.monotonic() + max(timeout_seconds, 1)
    while time.monotonic() < deadline:
        for frame in list(page.frames):
            if "/cap4/" not in str(frame.url or ""):
                continue
            try:
                if frame.locator("#field0006_id").count() == 1:
                    return frame
            except Exception:
                continue
        page.wait_for_timeout(100)
    raise BusinessTripContractMismatch("The OA CAP4 business-trip form did not load in time.")


def _validate_form_controls(page, frame) -> None:
    if page.locator("#saveDraft_a").count() != 1:
        raise BusinessTripContractMismatch("The OA save-draft control is missing.")
    if page.locator("#sendId_a").count() != 1:
        raise BusinessTripContractMismatch("The OA send control contract changed.")
    for item in _FIELD_CONTRACT.values():
        wrapper = frame.locator(f"#{item['field']}_id")
        if wrapper.count() != 1:
            raise BusinessTripContractMismatch(
                f"The OA business-trip field contract is missing {item['field']}."
            )
        text = str(wrapper.text_content(timeout=3000) or "")
        if item["label"] not in text:
            raise BusinessTripContractMismatch(
                f"The OA business-trip label changed for {item['field']}."
            )


def _validate_optional_inputs(page, inputs: dict) -> None:
    if "note" not in inputs:
        return
    note = page.locator("#content_coll")
    if note.count() != 1 or not note.is_visible():
        raise BusinessTripContractMismatch(
            "The OA business-trip note field is not editable in this template."
        )


def _wait_for_interactive_form(page, frame) -> None:
    wait_for_cap4_interactive(
        page,
        frame,
        error_type=BusinessTripContractMismatch,
        context="The OA business-trip form",
    )


def _fill_business_trip_form(page, frame, inputs: dict) -> None:
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
    _wait_for_interactive_form(page, frame)
    frame.locator("#field0023_id input:not([readonly])").fill(inputs["origin"])
    frame.locator("#field0026_id input:not([readonly])").fill(inputs["destination"])
    frame.locator("#field0009_id textarea:visible").first.fill(inputs["reason"])
    _wait_for_interactive_form(page, frame)
    frame.locator("#field0027_id").click()
    frame.get_by_text(inputs["travel_mode"], exact=True).last.click()
    _wait_for_interactive_form(page, frame)
    supervisor_label = "是" if inputs["has_direct_supervisor"] else "否"
    frame.locator("#field0010_id").get_by_text(supervisor_label, exact=True).last.click()
    _wait_for_interactive_form(page, frame)
    if "trip_days" in inputs:
        _fill_decimal(frame, "field0029", inputs["trip_days"])
    if "trip_hours" in inputs:
        _fill_decimal(frame, "field0022", inputs["trip_hours"])
    if "note" in inputs:
        page.locator("#content_coll").fill(inputs["note"])
    _wait_for_interactive_form(page, frame)
    page.wait_for_timeout(600)


def _fill_decimal(frame, field_id: str, value: int | float) -> bool:
    wrapper = frame.locator(f"#{field_id}_id")
    active = wrapper.locator("input.is-activeInput:not([readonly]):visible")
    editable = wrapper.locator("input:not([readonly]):visible")
    target = active.first if active.count() else editable.first if editable.count() else None
    if target is None:
        return False
    target.fill(str(value))
    return True


def _read_business_trip_form(page, frame) -> dict:
    values = frame.evaluate(
        r"""
        () => {
          const value = (selector) => document.querySelector(selector)?.value || '';
          const editableValue = (fieldId) => {
            const wrapper = document.querySelector(`#${fieldId}_id`);
            if (!wrapper) return '';
            const controls = Array.from(wrapper.querySelectorAll('input,textarea'));
            const active = controls.find((element) => element.classList.contains('is-activeInput'));
            const editable = controls.find((element) => !element.readOnly && getComputedStyle(element).display !== 'none');
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
            start_time: value('#field0006_format'),
            end_time: value('#field0007_format'),
            travel_mode: value('#field0027_inner'),
            origin: editableValue('field0023'),
            destination: editableValue('field0026'),
            trip_days: calculatedValue('field0029'),
            trip_hours: calculatedValue('field0022'),
            reason: editableValue('field0009'),
            supervisor_selection: selectedRadio('field0010'),
          };
        }
        """
    )
    values["has_direct_supervisor"] = _supervisor_choice_to_bool(
        values.pop("supervisor_selection", "")
    )
    values["note"] = page.locator("#content_coll").input_value()
    values["subject"] = page.locator("#subject").input_value()
    return values


def _assert_readback(expected: dict, actual: dict, *, stage: str) -> None:
    mismatches = []
    for field in _expected_readback_fields(expected):
        wanted = expected[field]
        observed = actual.get(field)
        if field in {"trip_days", "trip_hours"}:
            try:
                matches = float(observed) == float(wanted)
            except (TypeError, ValueError):
                matches = False
        else:
            matches = observed == wanted
        if not matches:
            mismatches.append(field)
    if mismatches:
        message = f"OA business-trip {stage} readback mismatch: {', '.join(mismatches)}"
        if stage == "verification":
            raise BusinessTripOutcomeUnknown(message)
        raise BusinessTripContractMismatch(message)


def _expected_readback_fields(inputs: dict) -> set[str]:
    fields = {
        "start_time",
        "end_time",
        "travel_mode",
        "origin",
        "destination",
        "reason",
        "has_direct_supervisor",
    }
    fields.update(name for name in ("trip_days", "trip_hours", "note") if name in inputs)
    return fields


def _supervisor_choice_to_bool(value: Any) -> bool | None:
    choice = str(value or "").strip()
    if choice == "是":
        return True
    if choice == "否":
        return False
    return None


def _validate_frozen_plan(plan: dict) -> None:
    if not isinstance(plan, dict) or plan.get("business_intent") != "save_business_trip_request_draft":
        raise BusinessTripContractMismatch("The frozen plan is not a business-trip draft plan.")
    contract = plan.get("form_contract") if isinstance(plan.get("form_contract"), dict) else {}
    if (
        contract.get("version") != BUSINESS_TRIP_CONTRACT_VERSION
        or contract.get("fingerprint") != business_trip_contract_fingerprint()
    ):
        raise BusinessTripContractMismatch("The business-trip form contract changed after authorization.")


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


def _parse_business_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must use YYYY-MM-DD HH:MM")
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD HH:MM") from exc


def _bounded_text(value: Any, name: str, maximum: int, *, allow_blank: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = re.sub(r"[\r\n\t]+", " ", value).strip()
    if not text and not allow_blank:
        raise ValueError(f"{name} is required")
    if len(text) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    return text
