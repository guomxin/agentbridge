from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from agentbridge.adapters.seeyon_cap4 import wait_for_cap4_interactive


MISSED_PUNCH_PREPARE_CAPABILITY = "oa.missed_punch.prepare"
MISSED_PUNCH_SAVE_CAPABILITY = "oa.missed_punch.save_draft"
MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY = "oa.missed_punch.approval.prepare"
MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY = (
    "oa.missed_punch.approval.batch.prepare"
)
MISSED_PUNCH_APPROVE_CAPABILITY = "oa.missed_punch.approve"
MISSED_PUNCH_TEMPLATE_TITLE = "【HR】补签申请单"
MISSED_PUNCH_TEMPLATE_ID = "-8494358180075582561"
MISSED_PUNCH_FORM_APP_ID = "-3950641196724501449"
MISSED_PUNCH_DRAFT_CONTRACT_VERSION = "seeyon-missed-punch-draft-v1"
MISSED_PUNCH_APPROVAL_CONTRACT_VERSION = "seeyon-missed-punch-approval-v2"
MISSED_PUNCH_REASONS = ("忘记打卡", "人脸识别有误", "其他")

MISSED_PUNCH_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "start_time": {"type": "string", "maxLength": 32},
        "end_time": {"type": "string", "maxLength": 32},
        "location": {"type": "string", "maxLength": 255},
        "reason_type": {"type": "string", "enum": list(MISSED_PUNCH_REASONS)},
        "explanation": {"type": "string", "maxLength": 4000},
        "input_submission_id": {"type": "string"},
    },
    "additionalProperties": False,
}

MISSED_PUNCH_FIELD_CARD_SCHEMA = {
    "schema_version": "agentbridge.oa_missed_punch_fields.v1",
    "title": "填写补签申请",
    "system": "致远 OA",
    "effect": "生成一份待确认的补签申请草稿计划",
    "submit_label": "提交字段",
    "notice": "字段提交后还需单独授权；最终只保存为待发草稿，不会发送或进入审批流程。",
    "fields": [
        {
            "name": "start_time",
            "label": "开始时间",
            "control": "datetime-local",
            "required": True,
        },
        {
            "name": "end_time",
            "label": "结束时间",
            "control": "datetime-local",
            "required": True,
        },
        {
            "name": "location",
            "label": "外出地点",
            "control": "text",
            "required": True,
            "max_length": 255,
            "autocomplete": "off",
        },
        {
            "name": "reason_type",
            "label": "补签原因",
            "control": "select",
            "required": True,
            "options": [
                {"value": value, "label": value}
                for value in MISSED_PUNCH_REASONS
            ],
        },
        {
            "name": "explanation",
            "label": "事由说明",
            "control": "textarea",
            "required": True,
            "max_length": 4000,
            "rows": 4,
        },
    ],
    "constraints": [
        {
            "kind": "datetime_after",
            "earlier": "start_time",
            "later": "end_time",
            "maximum_minutes": 527040,
            "message": "结束时间必须晚于开始时间，且时间跨度不能超过 366 天。",
        }
    ],
}

MISSED_PUNCH_SAVE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"authorization_id": {"type": "string"}},
    "required": ["authorization_id"],
    "additionalProperties": False,
}

MISSED_PUNCH_APPROVAL_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "affair_id": {"type": "string"},
        "opinion": {"type": "string", "maxLength": 1000},
        "input_submission_id": {"type": "string"},
    },
    "required": ["affair_id"],
    "additionalProperties": False,
}

MISSED_PUNCH_APPROVAL_BATCH_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer"},
        "failure_policy": {"type": "string"},
        "batch_id": {"type": "string"},
        "affair_id": {"type": "string"},
        "batch_ordinal": {"type": "integer"},
        "batch_total": {"type": "integer"},
        "target_title": {"type": "string"},
        "target_sender": {"type": "string"},
        "target_date": {"type": "string"},
        "input_submission_id": {"type": "string"},
    },
    "additionalProperties": False,
}

MISSED_PUNCH_APPROVAL_FIELD_CARD_SCHEMA = {
    "schema_version": "agentbridge.oa_missed_punch_approval_fields.v1",
    "title": "填写补签审批意见",
    "system": "致远 OA",
    "effect": "生成一份待确认的补签审批计划",
    "submit_label": "提交意见",
    "notice": "提交意见后还需在独立授权卡中核对目标事项；未授权前不会审批。",
    "fields": [
        {
            "name": "opinion",
            "label": "审批意见",
            "control": "textarea",
            "required": True,
            "max_length": 1000,
            "rows": 4,
        }
    ],
}

MISSED_PUNCH_APPROVE_INPUT_SCHEMA = MISSED_PUNCH_SAVE_INPUT_SCHEMA

_DRAFT_FIELD_CONTRACT = {
    "start_time": {"field": "field0007", "label": "开始时间", "kind": "datetime"},
    "end_time": {"field": "field0008", "label": "结束时间", "kind": "datetime"},
    "duration": {"field": "field0009", "label": "时长", "kind": "calculated"},
    "location": {"field": "field0010", "label": "外出地点", "kind": "text"},
    "reason_type": {"field": "field0011", "label": "补签原因", "kind": "select"},
    "explanation": {"field": "field0012", "label": "事由说明", "kind": "textarea"},
}


class MissedPunchContractMismatch(RuntimeError):
    pass


class MissedPunchOutcomeUnknown(RuntimeError):
    pass


def select_missed_punch_approval_batch_items(
    workflow_items: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("batch limit must be between 1 and 10")
    selected = []
    for source in workflow_items:
        if not isinstance(source, dict):
            continue
        affair_id = str(source.get("affair_id") or "").strip()
        title = str(source.get("title") or "").strip()
        if not affair_id or "补签申请单" not in title:
            continue
        display = {
            "title": title,
            "sender": str(source.get("sender") or "").strip(),
            "date": str(source.get("date") or "").strip(),
            "category": str(source.get("category") or "").strip(),
        }
        selected.append(
            {
                "resource_ref": affair_id,
                "display_summary": display,
                "source_fingerprint": hashlib.sha256(
                    json.dumps(
                        {"affair_id": affair_id, **display},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
    selected.sort(
        key=lambda item: (
            str(item["display_summary"].get("date") or ""),
            item["resource_ref"],
        )
    )
    return selected[:limit]


def build_missed_punch_approval_batch_field_schema(
    _adapter,
    _worker,
    arguments: dict,
) -> dict:
    schema = deepcopy(MISSED_PUNCH_APPROVAL_FIELD_CARD_SCHEMA)
    ordinal = int(arguments.get("batch_ordinal") or 1)
    total = int(arguments.get("batch_total") or 1)
    title = str(arguments.get("target_title") or "补签申请单").strip()
    sender = str(arguments.get("target_sender") or "").strip()
    date = str(arguments.get("target_date") or "").strip()
    target_parts = [value for value in (title, sender, date) if value]
    schema["title"] = f"填写补签审批意见（第 {ordinal}/{total} 条）"
    schema["notice"] = (
        f"当前事项：{' | '.join(target_parts)}。提交意见后仍需独立授权；"
        "本条权威验证成功后，系统才会进入下一条。"
    )
    return schema


def prepare_missed_punch_draft(adapter, worker, arguments: dict) -> dict:
    inputs = normalize_missed_punch_inputs(arguments)
    template = _resolve_template(adapter.list_templates(worker))
    page, frame = _open_and_validate_draft_form(worker, template)
    return {
        "plan": {
            "schema_version": "agentbridge.oa_missed_punch_plan.v1",
            "business_intent": "save_missed_punch_request_draft",
            "target": {
                "template_title": template["title"],
                "template_id": template["template_id"],
                "form_app_id": template["form_app_id"],
            },
            "form_contract": {
                "version": MISSED_PUNCH_DRAFT_CONTRACT_VERSION,
                "fingerprint": missed_punch_draft_contract_fingerprint(),
                "fields": {
                    name: {"field": item["field"], "kind": item["kind"]}
                    for name, item in _DRAFT_FIELD_CONTRACT.items()
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
        "summary": missed_punch_draft_summary(inputs),
    }


def save_missed_punch_draft(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
    timeout_seconds: float = 60,
) -> dict:
    _validate_draft_plan(plan)
    inputs = normalize_missed_punch_inputs(plan["exact_input"])
    template = _resolve_template(adapter.list_templates(worker))
    target = plan["target"]
    if any(
        (
            template["template_id"] != target.get("template_id"),
            template["form_app_id"] != target.get("form_app_id"),
            template["title"] != target.get("template_title"),
        )
    ):
        raise MissedPunchContractMismatch(
            "The OA missed-punch template changed after authorization."
        )
    page, frame = _open_and_validate_draft_form(worker, template)
    _fill_missed_punch_form(page, frame, inputs)
    _assert_draft_readback(inputs, _read_missed_punch_form(page, frame), stage="precommit")

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
            raise MissedPunchOutcomeUnknown(
                "OA did not expose the wait-send readback URL after the save-draft click."
            )
        page.wait_for_load_state("domcontentloaded", timeout=max(timeout_seconds, 5) * 1000)
        saved_frame = _wait_for_draft_frame(page, timeout_seconds=min(timeout_seconds, 30))
        _validate_draft_controls(page, saved_frame)
        saved_readback = _read_missed_punch_form(page, saved_frame)
        _assert_draft_readback(inputs, saved_readback, stage="verification")
        identifiers = _wait_send_identifiers(page.url)
        if not identifiers["summary_id"] or not identifiers["affair_id"]:
            raise MissedPunchOutcomeUnknown(
                "OA reloaded the draft without stable summary and affair identifiers."
            )
        return {
            "schema_version": "agentbridge.oa_missed_punch_save_result.v1",
            "business_intent": "save_missed_punch_request_draft",
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
                "matched_fields": sorted(_draft_readback_fields()),
                "server_reloaded": True,
            },
            "request_evidence": observed_requests,
            "transport": "central_browser_session",
        }
    except MissedPunchOutcomeUnknown:
        raise
    except BaseException as exc:
        if click_started:
            raise MissedPunchOutcomeUnknown(
                "The OA save-draft boundary was crossed, but verification failed."
            ) from exc
        raise


def prepare_missed_punch_approval(adapter, worker, arguments: dict) -> dict:
    inputs = normalize_missed_punch_approval_inputs(arguments)
    source, detail = adapter.resolve_workflow_detail(
        worker,
        collection="pending",
        affair_id=inputs["affair_id"],
    )
    signals = _validate_approval_target(
        source, detail, worker.page, inputs["affair_id"]
    )
    return {
        "plan": {
            "schema_version": "agentbridge.oa_missed_punch_approval_plan.v1",
            "business_intent": "approve_missed_punch_request",
            "target": _approval_target(source, detail, signals),
            "action_contract": {
                "version": MISSED_PUNCH_APPROVAL_CONTRACT_VERSION,
                "fingerprint": missed_punch_approval_contract_fingerprint(),
                "internal_binding": "ContinueSubmit",
                "action_kind": "approval",
                "verification": "pending_disappearance",
            },
            "exact_input": {"opinion": inputs["opinion"]},
            "preconditions": {
                "pending_target_resolved": True,
                "title_contract_matched": True,
                "approval_action_present": True,
                "opinion_control_present": True,
                "submit_entry_present": True,
            },
            "expected_effect": {
                "workflow_approved": True,
                "submitted_count": 1,
                "verification": "pending_disappearance",
            },
        },
        "summary": missed_punch_approval_summary(source, detail, inputs["opinion"]),
    }


def approve_missed_punch_request(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
    timeout_seconds: float = 60,
) -> dict:
    _validate_approval_plan(plan)
    affair_id = _bounded_identifier(plan.get("target", {}).get("affair_id"), "affair_id")
    opinion = _bounded_text(plan.get("exact_input", {}).get("opinion"), "opinion", 1000)
    source, detail = adapter.resolve_workflow_detail(
        worker,
        collection="pending",
        affair_id=affair_id,
    )
    signals = _validate_approval_target(source, detail, worker.page, affair_id)
    _assert_approval_target(
        dict(plan.get("target") or {}), _approval_target(source, detail, signals)
    )
    page = worker.page
    page.on("dialog", lambda dialog: dialog.accept())
    boundary_crossed = False
    enter_commit_boundary()
    boundary_crossed = True
    try:
        scheduled = page.evaluate(
            _APPROVAL_SCRIPT,
            {"affair_id": affair_id, "opinion": opinion},
        )
        if not isinstance(scheduled, dict) or scheduled.get("scheduled") is not True:
            raise MissedPunchOutcomeUnknown("OA did not schedule the approval submission.")
        deadline = time.monotonic() + max(timeout_seconds, 5)
        last_verification_error: Exception | None = None
        while time.monotonic() < deadline:
            time.sleep(0.8)
            try:
                pending = adapter.list_workflows(
                    worker,
                    collection="pending",
                    arguments={"limit": 100},
                )
            except Exception as exc:
                last_verification_error = exc
                continue
            if not any(
                str(item.get("affair_id") or "") == affair_id
                for item in pending.get("items") or []
            ):
                return {
                    "schema_version": "agentbridge.oa_missed_punch_approval_result.v1",
                    "business_intent": "approve_missed_punch_request",
                    "workflow_approved": True,
                    "submitted_count": 1,
                    "target": {
                        "affair_id": affair_id,
                        "title": str(source.get("title") or ""),
                    },
                    "verification": {
                        "confirmed": True,
                        "method": "pending_disappearance",
                    },
                    "transport": "central_browser_session",
                }
            last_verification_error = None
        if last_verification_error is not None:
            error_type = type(last_verification_error).__name__
            error_message = str(last_verification_error).strip()
            error_detail = f"{error_type}: {error_message}" if error_message else error_type
            raise MissedPunchOutcomeUnknown(
                "The approval was scheduled, but pending-list verification did not "
                f"recover before timeout. Last verification error: {error_detail}"
            ) from last_verification_error
        raise MissedPunchOutcomeUnknown(
            "The approval was scheduled, but the affair remained in the pending list."
        )
    except MissedPunchOutcomeUnknown:
        raise
    except BaseException as exc:
        if boundary_crossed:
            raise MissedPunchOutcomeUnknown(
                "The OA approval boundary was crossed, but verification failed."
            ) from exc
        raise


def normalize_missed_punch_inputs(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("missed-punch input must be an object")
    start = _parse_datetime(arguments.get("start_time"), "start_time")
    end = _parse_datetime(arguments.get("end_time"), "end_time")
    if end <= start:
        raise ValueError("end_time must be later than start_time")
    if (end - start).total_seconds() > 366 * 24 * 60 * 60:
        raise ValueError("missed-punch time span must not exceed 366 days")
    reason_type = _bounded_text(arguments.get("reason_type"), "reason_type", 50)
    if reason_type not in MISSED_PUNCH_REASONS:
        raise ValueError(f"reason_type must be one of: {', '.join(MISSED_PUNCH_REASONS)}")
    return {
        "start_time": start.strftime("%Y-%m-%d %H:%M"),
        "end_time": end.strftime("%Y-%m-%d %H:%M"),
        "location": _bounded_text(arguments.get("location"), "location", 255),
        "reason_type": reason_type,
        "explanation": _bounded_text(arguments.get("explanation"), "explanation", 4000),
    }


def normalize_missed_punch_approval_inputs(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("missed-punch approval input must be an object")
    return {
        "affair_id": _bounded_identifier(arguments.get("affair_id"), "affair_id"),
        "opinion": _bounded_text(arguments.get("opinion"), "opinion", 1000),
    }


def missed_punch_draft_summary(inputs: dict) -> dict:
    return {
        "title": "保存补签申请草稿",
        "system": "致远 OA",
        "effect": "仅保存待发草稿",
        "authorization_notice": "授权后仅保存为待发草稿，不会发送、提交或进入审批流程。",
        "authorize_label": "授权保存草稿",
        "fields": [
            {"label": "开始时间", "value": inputs["start_time"]},
            {"label": "结束时间", "value": inputs["end_time"]},
            {"label": "外出地点", "value": inputs["location"]},
            {"label": "补签原因", "value": inputs["reason_type"]},
            {"label": "事由说明", "value": inputs["explanation"]},
        ],
        "submitted_count": 0,
    }


def missed_punch_approval_summary(source: dict, detail: dict, opinion: str) -> dict:
    values = _approval_field_values(detail)
    fields = [
        {"label": "事项", "value": str(source.get("title") or "")},
        {"label": "发起人", "value": str(source.get("sender") or "")},
        {"label": "日期", "value": str(source.get("date") or "")},
    ]
    for name in ("申请人", "开始时间", "补签原因", "事由说明"):
        if values.get(name):
            fields.append({"label": name, "value": values[name]})
    fields.extend(
        [
            {"label": "附件数量", "value": str(len(detail.get("attachments") or []))},
            {
                "label": "已有意见数量",
                "value": str(len(detail.get("opinions") or detail.get("workflow") or [])),
            },
            {"label": "审批意见", "value": opinion},
        ]
    )
    return {
        "title": "审批补签申请",
        "system": "致远 OA",
        "effect": "审批通过后该事项将离开待办列表",
        "authorization_notice": "授权后将立即提交审批通过操作；不会只保存审批意见草稿。",
        "authorize_label": "授权审批通过",
        "fields": fields,
        "submitted_count": 1,
    }


def missed_punch_draft_contract_fingerprint() -> str:
    contract = {
        "version": MISSED_PUNCH_DRAFT_CONTRACT_VERSION,
        "template_title": MISSED_PUNCH_TEMPLATE_TITLE,
        "template_id": MISSED_PUNCH_TEMPLATE_ID,
        "form_app_id": MISSED_PUNCH_FORM_APP_ID,
        "fields": _DRAFT_FIELD_CONTRACT,
        "reasons": MISSED_PUNCH_REASONS,
        "save_control": "saveDraft_a",
        "forbidden_controls": ["sendId_a"],
    }
    return _fingerprint(contract)


def missed_punch_approval_contract_fingerprint() -> str:
    return _fingerprint(
        {
            "version": MISSED_PUNCH_APPROVAL_CONTRACT_VERSION,
            "title_prefix": MISSED_PUNCH_TEMPLATE_TITLE,
            "template_id": MISSED_PUNCH_TEMPLATE_ID,
            "form_app_id": MISSED_PUNCH_FORM_APP_ID,
            "node_policies": ["approve", "审批"],
            "required_attitude": "agree",
            "internal_binding": "ContinueSubmit",
            "opinion_selectors": [
                "#content_deal_comment",
                "textarea[name='content_deal_comment']",
                "textarea#content",
                "textarea[name='content']",
            ],
            "submit_entries": ["submitClickFunc", "dealSubmitFunc", "$.content.callback.dealSubmit"],
            "verification": "pending_disappearance",
        }
    )


def _resolve_template(template_list: dict) -> dict:
    candidates = [
        item
        for item in template_list.get("items") or []
        if isinstance(item, dict) and item.get("title") == MISSED_PUNCH_TEMPLATE_TITLE
    ]
    if len(candidates) != 1:
        raise MissedPunchContractMismatch(
            "The OA missed-punch template could not be resolved uniquely."
        )
    template = candidates[0]
    if (
        str(template.get("template_id") or "") != MISSED_PUNCH_TEMPLATE_ID
        or str(template.get("form_app_id") or "") != MISSED_PUNCH_FORM_APP_ID
    ):
        raise MissedPunchContractMismatch(
            "The OA missed-punch template identity changed; rediscovery is required."
        )
    return template


def _open_and_validate_draft_form(worker, template: dict):
    page = worker.goto(str(template["href"]), timeout_seconds=60)
    frame = _wait_for_draft_frame(page, timeout_seconds=20)
    _wait_for_interactive_draft_form(page, frame)
    _validate_draft_controls(page, frame)
    if _frame_module_id(frame.url) != MISSED_PUNCH_TEMPLATE_ID:
        raise MissedPunchContractMismatch(
            "The CAP4 frame is not bound to the expected missed-punch template."
        )
    return page, frame


def _wait_for_draft_frame(page, *, timeout_seconds: float):
    deadline = time.monotonic() + max(timeout_seconds, 1)
    while time.monotonic() < deadline:
        for frame in list(page.frames):
            if "/cap4/" not in str(frame.url or ""):
                continue
            try:
                if frame.locator("#field0007_id").count() == 1:
                    return frame
            except Exception:
                continue
        page.wait_for_timeout(100)
    raise MissedPunchContractMismatch("The OA CAP4 missed-punch form did not load in time.")


def _validate_draft_controls(page, frame) -> None:
    if page.locator("#saveDraft_a").count() != 1:
        raise MissedPunchContractMismatch("The OA save-draft control is missing.")
    if page.locator("#sendId_a").count() != 1:
        raise MissedPunchContractMismatch("The OA send control contract changed.")
    for item in _DRAFT_FIELD_CONTRACT.values():
        if frame.locator(f"#{item['field']}_id").count() != 1:
            raise MissedPunchContractMismatch(
                f"The OA missed-punch field contract is missing {item['field']}."
            )
        if frame.get_by_text(item["label"], exact=True).count() < 1:
            raise MissedPunchContractMismatch(
                f"The OA missed-punch label changed for {item['field']}."
            )
    frame.locator("#field0011_id").click()
    try:
        for reason in MISSED_PUNCH_REASONS:
            if frame.get_by_text(reason, exact=True).count() < 1:
                raise MissedPunchContractMismatch(
                    f"The OA missed-punch reason option changed: {reason}."
                )
    finally:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass


def _wait_for_interactive_draft_form(page, frame) -> None:
    wait_for_cap4_interactive(
        page,
        frame,
        error_type=MissedPunchContractMismatch,
        context="The OA missed-punch form",
    )


def _fill_missed_punch_form(page, frame, inputs: dict) -> None:
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
          setValue(document.querySelector('#field0007_format'), start);
          setValue(document.querySelector('#field0007'), start);
          setValue(document.querySelector('#field0008_format'), end);
          setValue(document.querySelector('#field0008'), end);
        }
        """,
        {"start": inputs["start_time"], "end": inputs["end_time"]},
    )
    _wait_for_interactive_draft_form(page, frame)
    frame.locator("#field0010_id input:not([readonly])").first.fill(inputs["location"])
    _wait_for_interactive_draft_form(page, frame)
    frame.locator("#field0011_id").click()
    frame.get_by_text(inputs["reason_type"], exact=True).last.click()
    _wait_for_interactive_draft_form(page, frame)
    frame.locator("#field0012_id textarea:visible").first.fill(inputs["explanation"])
    _wait_for_interactive_draft_form(page, frame)
    page.wait_for_timeout(600)


def _read_missed_punch_form(page, frame) -> dict:
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
          return {
            start_time: value('#field0007_format'),
            end_time: value('#field0008_format'),
            location: editableValue('field0010'),
            reason_type: value('#field0011_inner'),
            explanation: editableValue('field0012'),
          };
        }
        """
    )
    values["subject"] = page.locator("#subject").input_value()
    return values


def _assert_draft_readback(expected: dict, actual: dict, *, stage: str) -> None:
    mismatches = [
        field
        for field in _draft_readback_fields()
        if actual.get(field) != expected.get(field)
    ]
    if not mismatches:
        return
    message = f"OA missed-punch {stage} readback mismatch: {', '.join(mismatches)}"
    if stage == "verification":
        raise MissedPunchOutcomeUnknown(message)
    raise MissedPunchContractMismatch(message)


def _draft_readback_fields() -> set[str]:
    return {"start_time", "end_time", "location", "reason_type", "explanation"}


def _validate_draft_plan(plan: dict) -> None:
    if not isinstance(plan, dict) or plan.get("business_intent") != "save_missed_punch_request_draft":
        raise MissedPunchContractMismatch("The frozen plan is not a missed-punch draft plan.")
    contract = plan.get("form_contract") if isinstance(plan.get("form_contract"), dict) else {}
    if (
        contract.get("version") != MISSED_PUNCH_DRAFT_CONTRACT_VERSION
        or contract.get("fingerprint") != missed_punch_draft_contract_fingerprint()
    ):
        raise MissedPunchContractMismatch(
            "The missed-punch draft contract changed after authorization."
        )


def _approval_field_values(detail: dict) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in detail.get("fields") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = re.sub(r"\s+", " ", str(item.get("value") or "")).strip()
        if name and value and name not in values:
            values[name] = value[:1000]
    return values


def _approval_detail_fingerprint(source: dict, detail: dict) -> str:
    return _fingerprint(
        {
            "title": str(source.get("title") or detail.get("title") or ""),
            "fields": [
                {
                    "name": str(item.get("name") or ""),
                    "value": str(item.get("value") or ""),
                }
                for item in detail.get("fields") or []
                if isinstance(item, dict)
            ],
            "attachments": [
                str(item.get("name") or "")
                for item in detail.get("attachments") or []
                if isinstance(item, dict)
            ],
        }
    )


def _approval_target(source: dict, detail: dict, signals: dict) -> dict:
    identity = signals["identity"]
    return {
        "affair_id": str(source.get("affair_id") or ""),
        "summary_id": str(identity.get("summary_id") or ""),
        "process_id": str(identity.get("process_id") or ""),
        "template_id": str(identity.get("template_id") or ""),
        "form_app_id": str(identity.get("form_app_id") or ""),
        "form_record_id": str(identity.get("form_record_id") or ""),
        "title": str(source.get("title") or ""),
        "sender": str(source.get("sender") or ""),
        "date": str(source.get("date") or ""),
        "node_policy": str(signals.get("node_policy") or ""),
        "node_policy_name": str(signals.get("node_policy_name") or ""),
        "detail_fingerprint": _approval_detail_fingerprint(source, detail),
    }


def _assert_approval_target(expected: dict, actual: dict) -> None:
    for name, value in actual.items():
        if str(expected.get(name) or "") != str(value or ""):
            raise MissedPunchContractMismatch(
                f"The OA missed-punch target {name} changed after authorization."
            )


def _validate_approval_target(source: dict, detail: dict, page, affair_id: str) -> dict:
    title = str(source.get("title") or detail.get("title") or "")
    if not title.startswith(MISSED_PUNCH_TEMPLATE_TITLE):
        raise MissedPunchContractMismatch(
            "The selected pending workflow is not a missed-punch request."
        )
    actions = {
        str(item.get("code") or "")
        for item in detail.get("actions") or []
        if isinstance(item, dict)
    }
    if "ContinueSubmit" not in actions:
        raise MissedPunchContractMismatch(
            "The missed-punch approval action is not available on the target workflow."
        )
    signals = page.evaluate(
        r"""
        (expectedAffairId) => {
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
          const comment = document.querySelector('#content_deal_comment')
            || document.querySelector("textarea[name='content_deal_comment']")
            || document.querySelector('textarea#content')
            || document.querySelector("textarea[name='content']");
          return {
            affair_matches: String(pageAffairId) === String(expectedAffairId),
            comment_present: Boolean(comment),
            submit_present: typeof window.submitClickFunc === 'function'
              || typeof window.dealSubmitFunc === 'function'
              || typeof window.$?.content?.callback?.dealSubmit === 'function',
            page_path: location.pathname,
            node_policy: String(window.nodePolicy || ''),
            node_policy_name: String(window.nodePolicyName || ''),
            attitude_codes: Array.from(
              document.querySelectorAll("input[type='radio'][name='attitude']")
            ).map((radio) => String(radio.getAttribute('code') || radio.value || '').toLowerCase()),
            identity: {
              summary_id: read(['summaryId']),
              process_id: read(['processId']),
              template_id: read(['templeteId', 'templateId']),
              form_app_id: read(['formAppId']),
              form_record_id: read(['formRecordid', 'formRecordId']),
            },
          };
        }
        """,
        affair_id,
    )
    if not isinstance(signals, dict) or not all(
        (
            signals.get("affair_matches") is True,
            signals.get("comment_present") is True,
            signals.get("submit_present") is True,
            signals.get("page_path") == "/seeyon/collaboration/collaboration.do",
        )
    ):
        raise MissedPunchContractMismatch(
            "The missed-punch approval page no longer matches the registered contract."
        )
    if (
        str(signals.get("node_policy") or "") != "approve"
        and str(signals.get("node_policy_name") or "") != "审批"
    ):
        raise MissedPunchContractMismatch(
            "The missed-punch node is not a registered approval node."
        )
    attitudes = {str(item or "").lower() for item in signals.get("attitude_codes") or []}
    if "agree" not in attitudes:
        raise MissedPunchContractMismatch(
            "The missed-punch approval page does not expose the agree attitude."
        )
    identity = signals.get("identity")
    if not isinstance(identity, dict):
        raise MissedPunchContractMismatch("The missed-punch workflow identity is unavailable.")
    if not str(identity.get("summary_id") or "") or not str(identity.get("process_id") or ""):
        raise MissedPunchContractMismatch(
            "The missed-punch summary or process identity is unavailable."
        )
    if str(identity.get("template_id") or "") != MISSED_PUNCH_TEMPLATE_ID:
        raise MissedPunchContractMismatch("The missed-punch template identity changed.")
    if str(identity.get("form_app_id") or "") != MISSED_PUNCH_FORM_APP_ID:
        raise MissedPunchContractMismatch("The missed-punch form identity changed.")
    return signals


def _validate_approval_plan(plan: dict) -> None:
    if not isinstance(plan, dict) or plan.get("business_intent") != "approve_missed_punch_request":
        raise MissedPunchContractMismatch("The frozen plan is not a missed-punch approval plan.")
    contract = plan.get("action_contract") if isinstance(plan.get("action_contract"), dict) else {}
    if (
        contract.get("version") != MISSED_PUNCH_APPROVAL_CONTRACT_VERSION
        or contract.get("fingerprint") != missed_punch_approval_contract_fingerprint()
        or contract.get("action_kind") != "approval"
    ):
        raise MissedPunchContractMismatch(
            "The missed-punch approval contract changed after authorization."
        )


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


def _bounded_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty string of at most 256 characters")
    text = value.strip()
    if any(ord(character) < 32 for character in text):
        raise ValueError(f"{name} must not contain control characters")
    return text


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = re.sub(r"[\r\n\t]+", " ", value).strip()
    if not text:
        raise ValueError(f"{name} is required")
    if len(text) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    return text


def _fingerprint(value: dict) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


_APPROVAL_SCRIPT = r"""
({ affair_id, opinion }) => {
  const pageAffairId = String(window.affairId || '')
    || document.querySelector('#affairId')?.value
    || new URLSearchParams(location.search).get('affairId')
    || '';
  if (String(pageAffairId) !== String(affair_id)) {
    throw new Error('affair_id mismatch before approval');
  }
  const comment = document.querySelector('#content_deal_comment')
    || document.querySelector("textarea[name='content_deal_comment']")
    || document.querySelector('textarea#content')
    || document.querySelector("textarea[name='content']");
  if (!comment) throw new Error('approval opinion control is missing');
  const setValue = (element, value) => {
    const setter = Object.getOwnPropertyDescriptor(element.constructor.prototype, 'value')?.set;
    if (setter) setter.call(element, value); else element.value = value;
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
    element.dispatchEvent(new Event('blur', { bubbles: true }));
  };
  setValue(comment, String(opinion));
  const radios = Array.from(document.querySelectorAll("input[type='radio'][name='attitude']"));
  const agree = radios.find((radio) => {
    const code = String(radio.getAttribute('code') || '').toLowerCase();
    const value = String(radio.value || '').toLowerCase();
    return code === 'agree' || value === 'agree';
  });
  if (!agree) throw new Error('approval agree attitude is missing');
  agree.checked = true;
  agree.dispatchEvent(new Event('input', { bubbles: true }));
  agree.dispatchEvent(new Event('change', { bubbles: true }));
  const attitudeCode = agree.getAttribute('code') || agree.value;
  for (const selector of ['#hidAttitudeCode', '#hidAttitude', '#nodeattitude']) {
    const element = document.querySelector(selector);
    if (element) setValue(element, attitudeCode);
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
  if (!submit) throw new Error('approval submit entry is missing');
  window.setTimeout(() => submit.call(window), 0);
  return { scheduled: true, submit_entry: submitName };
}
"""
