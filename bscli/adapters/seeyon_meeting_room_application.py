from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from typing import Any, Callable

from bscli.adapters.seeyon_meeting_room import (
    MeetingRoomContractMismatch,
    available_rooms,
    meeting_ajax,
    public_meeting_room_application,
    query_meeting_room_snapshot,
    query_my_meeting_room_applications,
    resolve_room,
    room_app,
    room_is_available,
)
from bscli.core.capability import CapabilitySpec
from bscli.core.write_workflow import WriteWorkflowDefinition


MEETING_ROOM_APPLICATION_PREPARE_CAPABILITY = (
    "oa.meeting_room.application.prepare"
)
MEETING_ROOM_APPLICATION_CREATE_CAPABILITY = (
    "oa.meeting_room.application.create"
)
MEETING_ROOM_APPLICATION_CANCEL_PREPARE_CAPABILITY = (
    "oa.meeting_room.application.cancel.prepare"
)
MEETING_ROOM_APPLICATION_CANCEL_CAPABILITY = (
    "oa.meeting_room.application.cancel"
)
MEETING_ROOM_APPLICATION_CONTRACT_VERSION = "seeyon-meeting-room-application-v1"
MEETING_ROOM_APPLICATION_CANCEL_CONTRACT_VERSION = (
    "seeyon-meeting-room-application-cancel-v1"
)

MEETING_ROOM_APPLICATION_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "purpose": {"type": "string", "maxLength": 80},
        "room": {"type": "string", "maxLength": 100},
        "start_time": {"type": "string", "maxLength": 32},
        "end_time": {"type": "string", "maxLength": 32},
        "input_submission_id": {"type": "string"},
    },
    "additionalProperties": False,
}

MEETING_ROOM_APPLICATION_FIELD_CARD_SCHEMA = {
    "schema_version": "agentbridge.oa_meeting_room_application_fields.v1",
    "title": "填写会议室申请",
    "system": "致远 OA",
    "effect": "申请一间会议室，不创建或发送会议",
    "submit_label": "提交字段",
    "notice": "提交字段后还需单独授权；授权前不会占用会议室。",
    "fields": [
        {
            "name": "purpose",
            "label": "会议室用途",
            "control": "textarea",
            "required": True,
            "max_length": 80,
            "rows": 3,
        },
        {
            "name": "room",
            "label": "会议室",
            "control": "text",
            "required": True,
            "max_length": 100,
            "autocomplete": "off",
        },
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
    ],
    "constraints": [
        {
            "kind": "datetime_after",
            "earlier": "start_time",
            "later": "end_time",
            "maximum_minutes": 10080,
            "message": "结束时间必须晚于开始时间，且申请时长不能超过 7 天。",
        }
    ],
}

MEETING_ROOM_APPLICATION_CREATE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"authorization_id": {"type": "string"}},
    "required": ["authorization_id"],
    "additionalProperties": False,
}

MEETING_ROOM_APPLICATION_CANCEL_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "application_id": {"type": "string", "maxLength": 128},
        "cancellation_reason": {"type": "string", "maxLength": 100},
        "input_submission_id": {"type": "string"},
    },
    "required": ["application_id"],
    "additionalProperties": False,
}

MEETING_ROOM_APPLICATION_CANCEL_FIELD_CARD_SCHEMA = {
    "schema_version": "agentbridge.oa_meeting_room_application_cancel_fields.v1",
    "title": "填写会议室申请撤销原因",
    "system": "致远 OA",
    "effect": "撤销一条自己的独立会议室申请",
    "submit_label": "提交撤销原因",
    "notice": "提交原因后还需单独授权；授权前不会撤销申请。",
    "fields": [
        {
            "name": "cancellation_reason",
            "label": "撤销原因",
            "control": "textarea",
            "required": True,
            "max_length": 100,
            "rows": 4,
        }
    ],
}

MEETING_ROOM_APPLICATION_CANCEL_INPUT_SCHEMA = deepcopy(
    MEETING_ROOM_APPLICATION_CREATE_INPUT_SCHEMA
)


class MeetingRoomApplicationContractMismatch(RuntimeError):
    pass


class MeetingRoomApplicationOutcomeUnknown(RuntimeError):
    pass


def build_meeting_room_application_field_card_schema(
    adapter,
    worker,
    arguments: dict,
) -> dict:
    seed = _normalize_application_card_seed(arguments)
    start_ms = _datetime_ms(seed["start_time"])
    end_ms = _datetime_ms(seed["end_time"])
    snapshot = _query_room_snapshot(
        worker,
        adapter,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    free_rooms = available_rooms(snapshot, start_ms=start_ms, end_ms=end_ms)
    if not free_rooms:
        raise MeetingRoomApplicationContractMismatch(
            "No OA meeting rooms are available for the requested time range."
        )

    selected_room = None
    room_match_note = ""
    if seed["room"]:
        try:
            requested = resolve_room(seed["room"], snapshot)
        except MeetingRoomContractMismatch:
            room_match_note = "未能唯一匹配输入的会议室，请从 OA 当前空闲会议室中选择。"
        else:
            requested_app = room_app(
                requested,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            if room_is_available(snapshot, requested_app):
                selected_room = requested
            else:
                room_match_note = "输入的会议室在该时段已占用，请选择其他空闲会议室。"

    schema = deepcopy(MEETING_ROOM_APPLICATION_FIELD_CARD_SCHEMA)
    fields = {item["name"]: item for item in schema["fields"]}
    fields["purpose"]["value"] = seed["purpose"]
    fields["room"] = {
        "name": "room",
        "label": "会议室",
        "control": "select",
        "required": True,
        "options": [
            {"value": str(item["roomName"]), "label": str(item["roomName"])}
            for item in free_rooms
        ],
        "value": str(selected_room["roomName"]) if selected_room else "",
    }
    fields["start_time"]["value"] = seed["start_time"]
    fields["end_time"]["value"] = seed["end_time"]
    schema["fields"] = [fields[item["name"]] for item in schema["fields"]]
    schema["notice"] = " ".join(
        part
        for part in (
            f"已按 {seed['start_time']} 至 {seed['end_time']} 查询 OA，"
            f"当前有 {len(free_rooms)} 个空闲会议室。",
            room_match_note,
            "提交字段后会再次校验；授权前不会占用会议室。",
        )
        if part
    )
    return schema


def prepare_meeting_room_application(adapter, worker, arguments: dict) -> dict:
    inputs = normalize_meeting_room_application_inputs(arguments)
    start_ms = _datetime_ms(inputs["start_time"])
    end_ms = _datetime_ms(inputs["end_time"])
    snapshot = _query_room_snapshot(
        worker,
        adapter,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    room = _resolve_room(inputs["room"], snapshot)
    selected_app = room_app(room, start_ms=start_ms, end_ms=end_ms)
    selected_app["description"] = inputs["purpose"]
    _assert_room_available(snapshot, selected_app)
    _validate_room_app(worker, adapter, selected_app)
    baseline_ids = _application_ids(
        query_my_meeting_room_applications(worker, adapter)["items"]
    )
    return {
        "plan": {
            "schema_version": "agentbridge.oa_meeting_room_application_plan.v1",
            "business_intent": "apply_meeting_room",
            "target": {
                "room_id": selected_app["roomId"],
                "room_name": selected_app["roomName"],
            },
            "action_contract": {
                "version": MEETING_ROOM_APPLICATION_CONTRACT_VERSION,
                "fingerprint": meeting_room_application_contract_fingerprint(),
                "selection_policy": "exactly_one_room",
                "commit_entry": "meetingAjaxManager.applyRoom",
                "verification": "new_my_application_readback",
            },
            "exact_input": inputs,
            "baseline_application_ids": sorted(baseline_ids),
            "preconditions": {
                "room_resolved_uniquely": True,
                "room_available": True,
                "oa_room_validation_passed": True,
                "my_applications_baseline_loaded": True,
            },
            "expected_effect": {
                "meeting_room_application_created": True,
                "meeting_created": False,
                "submitted_count": 1,
            },
        },
        "summary": meeting_room_application_summary(inputs, selected_app["roomName"]),
    }


def create_meeting_room_application(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    _validate_application_plan(plan)
    inputs = normalize_meeting_room_application_inputs(plan.get("exact_input") or {})
    start_ms = _datetime_ms(inputs["start_time"])
    end_ms = _datetime_ms(inputs["end_time"])
    snapshot = _query_room_snapshot(
        worker,
        adapter,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    room = _resolve_room(inputs["room"], snapshot)
    selected_app = room_app(room, start_ms=start_ms, end_ms=end_ms)
    selected_app["description"] = inputs["purpose"]
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    if any(
        (
            selected_app["roomId"] != str(target.get("room_id") or ""),
            selected_app["roomName"] != str(target.get("room_name") or ""),
        )
    ):
        raise MeetingRoomApplicationContractMismatch(
            "The resolved OA meeting room changed after authorization."
        )
    _assert_room_available(snapshot, selected_app)
    _validate_room_app(worker, adapter, selected_app)
    baseline_ids = {
        str(item)
        for item in plan.get("baseline_application_ids") or []
        if str(item)
    }

    enter_commit_boundary()
    try:
        response = meeting_ajax(
            worker,
            adapter,
            "applyRoom",
            [{"roomApps": [selected_app]}],
        )
        _ensure_write_accepted(response, "meeting-room application")
        readback = _find_created_application(
            worker,
            adapter,
            baseline_ids=baseline_ids,
            room_id=selected_app["roomId"],
            start_ms=start_ms,
            end_ms=end_ms,
        )
        return {
            "schema_version": "agentbridge.oa_meeting_room_application_result.v1",
            "business_intent": "apply_meeting_room",
            "meeting_room_application_created": True,
            "meeting_created": False,
            "submitted_count": 1,
            "application": public_meeting_room_application(readback),
            "verification": {
                "confirmed": True,
                "method": "new_my_application_readback",
            },
            "transport": "central_http_session",
        }
    except MeetingRoomApplicationOutcomeUnknown:
        raise
    except BaseException as exc:
        raise MeetingRoomApplicationOutcomeUnknown(
            "The OA meeting-room application boundary was crossed, but verification failed."
        ) from exc


def prepare_meeting_room_application_cancel(
    adapter,
    worker,
    arguments: dict,
) -> dict:
    inputs = normalize_meeting_room_application_cancel_inputs(arguments)
    raw = _resolve_application(worker, adapter, inputs["application_id"])
    _assert_cancelable(raw)
    target = _frozen_application_target(raw)
    return {
        "plan": {
            "schema_version": "agentbridge.oa_meeting_room_application_cancel_plan.v1",
            "business_intent": "cancel_meeting_room_application",
            "target": target,
            "action_contract": {
                "version": MEETING_ROOM_APPLICATION_CANCEL_CONTRACT_VERSION,
                "fingerprint": meeting_room_application_cancel_contract_fingerprint(),
                "selection_policy": "exactly_one_own_standalone_application",
                "commit_entry": "meetingAjaxManager.cancelRoomApp",
                "operation": 0,
                "verification": "my_application_terminal_readback",
            },
            "exact_input": {
                "cancellation_reason": inputs["cancellation_reason"],
            },
            "preconditions": {
                "application_resolved_uniquely": True,
                "application_owned_by_current_session": True,
                "application_is_standalone": True,
                "application_cancelable": True,
            },
            "expected_effect": {
                "meeting_room_application_canceled": True,
                "canceled_count": 1,
            },
        },
        "summary": meeting_room_application_cancel_summary(
            target,
            inputs["cancellation_reason"],
        ),
    }


def cancel_meeting_room_application(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    _validate_cancel_plan(plan)
    target = dict(plan.get("target") or {})
    application_id = _bounded_text(
        target.get("application_id"),
        "application_id",
        128,
    )
    reason = _bounded_text(
        (plan.get("exact_input") or {}).get("cancellation_reason"),
        "cancellation_reason",
        100,
    )
    current = _resolve_application(worker, adapter, application_id)
    _assert_same_frozen_application(target, current)
    _assert_cancelable(current)
    snapshot = _query_room_snapshot(
        worker,
        adapter,
        start_ms=_datetime_ms(target["start_time"]),
        end_ms=_datetime_ms(target["end_time"]),
    )
    target_was_in_room_snapshot = _snapshot_contains_application(snapshot, target)

    enter_commit_boundary()
    acknowledgement = {"stage": "cancel_response", "state": "received"}
    try:
        response = meeting_ajax(
            worker,
            adapter,
            "cancelRoomApp",
            [
                {
                    "appIds": application_id,
                    "operation": 0,
                    "cancelComment": reason,
                }
            ],
            allow_empty_success=True,
            response_diagnostics=acknowledgement,
        )
        _ensure_write_accepted(response, "meeting-room application cancellation")
    except Exception as exc:
        # The write may already have taken effect. Never replay it; verify independently.
        acknowledgement.update({"state": "unconfirmed", "error_type": type(exc).__name__})
    try:
        verification = _verify_cancellation(
            worker,
            adapter,
            target,
            target_was_in_room_snapshot=target_was_in_room_snapshot,
        )
        return {
            "schema_version": "agentbridge.oa_meeting_room_application_cancel_result.v1",
            "business_intent": "cancel_meeting_room_application",
            "meeting_room_application_canceled": True,
            "canceled_count": 1,
            "target": target,
            "acknowledgement": acknowledgement,
            "verification": {
                "confirmed": True,
                "method": "my_application_terminal_readback",
                **verification,
            },
            "transport": "central_http_session",
        }
    except MeetingRoomApplicationOutcomeUnknown as exc:
        raise MeetingRoomApplicationOutcomeUnknown(
            f"Cancellation readback: {exc} Response state: "
            f"{acknowledgement['state']}; "
            f"response error: {acknowledgement.get('error_type', 'none')}."
        ) from exc
    except Exception as exc:
        raise MeetingRoomApplicationOutcomeUnknown(
            "The OA meeting-room cancellation boundary was crossed, but an "
            f"unexpected readback error occurred ({type(exc).__name__}); "
            f"response error: {acknowledgement.get('error_type', 'none')}."
        ) from exc


def normalize_meeting_room_application_inputs(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("meeting-room application input must be an object")
    start = _parse_datetime(arguments.get("start_time"), "start_time")
    end = _parse_datetime(arguments.get("end_time"), "end_time")
    if end <= start:
        raise ValueError("end_time must be later than start_time")
    if end - start > timedelta(days=7):
        raise ValueError("meeting-room application duration must not exceed 7 days")
    return {
        "purpose": _bounded_text(arguments.get("purpose"), "purpose", 80),
        "room": _bounded_text(arguments.get("room"), "room", 100),
        "start_time": start.strftime("%Y-%m-%d %H:%M"),
        "end_time": end.strftime("%Y-%m-%d %H:%M"),
    }


def normalize_meeting_room_application_cancel_inputs(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("meeting-room cancellation input must be an object")
    return {
        "application_id": _bounded_text(
            arguments.get("application_id"),
            "application_id",
            128,
        ),
        "cancellation_reason": _bounded_text(
            arguments.get("cancellation_reason"),
            "cancellation_reason",
            100,
        ),
    }


def meeting_room_application_summary(inputs: dict, room_name: str) -> dict:
    return {
        "title": "申请会议室",
        "system": "致远 OA",
        "effect": "占用一间会议室，但不创建或发送会议",
        "authorization_notice": "授权后将立即提交会议室申请，不会仅保存草稿。",
        "authorize_label": "授权申请会议室",
        "fields": [
            {"label": "用途", "value": inputs["purpose"]},
            {"label": "会议室", "value": room_name},
            {"label": "开始时间", "value": inputs["start_time"]},
            {"label": "结束时间", "value": inputs["end_time"]},
        ],
        "submitted_count": 1,
    }


def meeting_room_application_cancel_summary(target: dict, reason: str) -> dict:
    return {
        "title": "撤销会议室申请",
        "system": "致远 OA",
        "effect": "释放该独立会议室申请占用的时段",
        "authorization_notice": "授权后将立即撤销该会议室申请。",
        "authorize_label": "授权撤销申请",
        "fields": [
            {"label": "会议室", "value": target["room_name"]},
            {"label": "开始时间", "value": target["start_time"]},
            {"label": "结束时间", "value": target["end_time"]},
            {"label": "撤销原因", "value": reason},
        ],
        "canceled_count": 1,
    }


def meeting_room_application_contract_fingerprint() -> str:
    return _fingerprint(
        {
            "version": MEETING_ROOM_APPLICATION_CONTRACT_VERSION,
            "fields": ["roomId", "appBeginDate", "appEndDate", "description"],
            "sequence": [
                "roomListInfo",
                "validateRoomApps",
                "getMyApps",
                "applyRoom",
                "getMyApps",
            ],
            "verification": "new_my_application_readback",
        }
    )


def meeting_room_application_cancel_contract_fingerprint() -> str:
    return _fingerprint(
        {
            "version": MEETING_ROOM_APPLICATION_CANCEL_CONTRACT_VERSION,
            "selection_policy": "exactly_one_own_standalone_application",
            "operation": 0,
            "sequence": ["getMyApps", "cancelRoomApp", "getMyApps"],
            "verification": "my_application_terminal_readback",
        }
    )


def _normalize_application_card_seed(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("meeting-room application input must be an object")
    start = _parse_datetime(arguments.get("start_time"), "start_time")
    end = _parse_datetime(arguments.get("end_time"), "end_time")
    if end <= start:
        raise ValueError("end_time must be later than start_time")
    if end - start > timedelta(days=7):
        raise ValueError("meeting-room application duration must not exceed 7 days")
    return {
        "purpose": _optional_text(arguments.get("purpose"), "purpose", 80),
        "room": _optional_text(arguments.get("room"), "room", 100),
        "start_time": start.strftime("%Y-%m-%d %H:%M"),
        "end_time": end.strftime("%Y-%m-%d %H:%M"),
    }


def _query_room_snapshot(worker, adapter, *, start_ms: int, end_ms: int) -> dict:
    try:
        return query_meeting_room_snapshot(
            worker,
            adapter,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    except MeetingRoomContractMismatch as exc:
        raise MeetingRoomApplicationContractMismatch(str(exc)) from exc


def _resolve_room(requested: str, snapshot: dict) -> dict:
    try:
        return resolve_room(requested, snapshot)
    except MeetingRoomContractMismatch as exc:
        raise MeetingRoomApplicationContractMismatch(str(exc)) from exc


def _assert_room_available(snapshot: dict, selected_app: dict) -> None:
    if room_is_available(snapshot, selected_app):
        return
    raise MeetingRoomApplicationContractMismatch(
        "The requested OA meeting room is occupied for this time range."
    )


def _validate_room_app(worker, adapter, selected_app: dict) -> None:
    response = meeting_ajax(
        worker,
        adapter,
        "validateRoomApps",
        [{"roomApps": [selected_app]}],
    )
    if isinstance(response, dict) and response.get("success") is False:
        raise MeetingRoomApplicationContractMismatch(
            str(response.get("message") or "OA meeting-room validation failed.")
        )
    data = response.get("data") if isinstance(response, dict) else []
    rows = data if isinstance(data, list) else []
    errors = [
        str(item.get("message") or "OA meeting-room validation failed.")
        for item in rows
        if isinstance(item, dict)
        and item.get("validate")
    ]
    if errors:
        raise MeetingRoomApplicationContractMismatch(
            "; ".join(dict.fromkeys(errors))
        )


def _find_created_application(
    worker,
    adapter,
    *,
    baseline_ids: set[str],
    room_id: str,
    start_ms: int,
    end_ms: int,
    attempts: int = 4,
) -> dict:
    for attempt in range(attempts):
        rows = query_my_meeting_room_applications(worker, adapter)["items"]
        matches = [
            item
            for item in rows
            if _raw_application_id(item) not in baseline_ids
            and str(item.get("roomId") or "") == room_id
            and _raw_time_ms(item, "startDatetime", "appBeginDate") == start_ms
            and _raw_time_ms(item, "endDatetime", "appEndDate") == end_ms
        ]
        if len(matches) == 1 and _raw_application_id(matches[0]):
            return matches[0]
        if len(matches) > 1:
            raise MeetingRoomApplicationOutcomeUnknown(
                "OA returned more than one new matching meeting-room application."
            )
        if attempt + 1 < attempts:
            time.sleep(0.5)
    raise MeetingRoomApplicationOutcomeUnknown(
        "The new meeting-room application was not found in My Applications."
    )


def _resolve_application(worker, adapter, application_id: str) -> dict:
    rows = query_my_meeting_room_applications(worker, adapter)["items"]
    matches = [item for item in rows if _raw_application_id(item) == application_id]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise MeetingRoomApplicationContractMismatch(
            "The OA meeting-room application was not found in My Applications."
        )
    raise MeetingRoomApplicationContractMismatch(
        "The OA meeting-room application ID was not unique."
    )


def _assert_cancelable(raw: dict) -> None:
    application_id = _raw_application_id(raw)
    if not application_id:
        raise MeetingRoomApplicationContractMismatch(
            "The OA meeting-room application has no stable ID."
        )
    meeting_id = str(raw.get("meetingId") or "").strip()
    meeting_name = str(raw.get("meetingName") or "").strip()
    if meeting_id or meeting_name:
        raise MeetingRoomApplicationContractMismatch(
            "This application is linked to a meeting; use the meeting cancellation flow instead."
        )
    status = _status_code(raw)
    usage = _usage_status_code(raw)
    if status == "4":
        raise MeetingRoomApplicationContractMismatch(
            "The OA meeting-room application is already terminal and cannot be canceled."
        )
    if usage == "1":
        raise MeetingRoomApplicationContractMismatch(
            "The OA meeting room is currently in use and cannot be canceled."
        )
    if usage in {"2", "3"}:
        raise MeetingRoomApplicationContractMismatch(
            "The OA meeting-room usage time has ended and cannot be canceled."
        )


def _verify_cancellation(
    worker,
    adapter,
    target: dict,
    *,
    target_was_in_room_snapshot: bool,
    attempts: int = 7,
) -> dict:
    application_id = str(target.get("application_id") or "")
    delays = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)
    read_error_count = 0
    last_read_errors: dict[str, str] = {}
    readback_diagnostics: set[str] = set()
    saw_active_application = False
    for attempt in range(attempts):
        try:
            listing = query_my_meeting_room_applications(worker, adapter)
            rows = listing["items"]
            if not listing["complete"]:
                raise MeetingRoomContractMismatch("Cancellation requires a complete application listing.")
        except Exception as exc:
            read_error_count += 1
            last_read_errors["my_applications"] = type(exc).__name__
            readback_diagnostics.add(
                f"my_applications={type(exc).__name__}"
            )
        else:
            last_read_errors.pop("my_applications", None)
            matches = [
                item
                for item in rows
                if _raw_application_id(item) == application_id
            ]
            if not matches:
                return {
                    "state": "absent_from_my_applications",
                    "readback_attempts": attempt + 1,
                    "transient_read_errors": read_error_count,
                    "readback_diagnostics": sorted(readback_diagnostics),
                }
            if len(matches) == 1 and _status_code(matches[0]) == "4":
                return {
                    "state": "terminal_status_4",
                    "readback_attempts": attempt + 1,
                    "transient_read_errors": read_error_count,
                    "readback_diagnostics": sorted(readback_diagnostics),
                }
            saw_active_application = True
        if target_was_in_room_snapshot:
            try:
                snapshot = _query_room_snapshot(
                    worker,
                    adapter,
                    start_ms=_datetime_ms(target["start_time"]),
                    end_ms=_datetime_ms(target["end_time"]),
                )
                target_still_in_snapshot = _snapshot_contains_application(snapshot, target)
            except Exception as exc:
                read_error_count += 1
                last_read_errors["room_snapshot"] = type(exc).__name__
                readback_diagnostics.add(
                    f"room_snapshot={type(exc).__name__}"
                )
            else:
                last_read_errors.pop("room_snapshot", None)
                if not target_still_in_snapshot:
                    return {
                        "state": "absent_from_room_snapshot",
                        "readback_attempts": attempt + 1,
                        "transient_read_errors": read_error_count,
                        "readback_diagnostics": sorted(readback_diagnostics),
                    }
        if attempt + 1 < attempts:
            time.sleep(delays[min(attempt, len(delays) - 1)])
    if last_read_errors:
        read_errors = ", ".join(
            f"{source}={error_code}"
            for source, error_code in sorted(last_read_errors.items())
        )
        active_note = (
            " The application listing still reported the target as active."
            if saw_active_application
            else ""
        )
        raise MeetingRoomApplicationOutcomeUnknown(
            "Cancellation readback did not reach a confirmed state after "
            f"{attempts} attempts.{active_note} Last read errors: {read_errors}."
        )
    raise MeetingRoomApplicationOutcomeUnknown(
        "The canceled meeting-room application remained active in My Applications."
    )


def _snapshot_contains_application(snapshot: dict, target: dict) -> bool:
    application_id = str(target.get("application_id") or "")
    room_id = str(target.get("room_id") or "")
    if not application_id or not room_id:
        return False
    apps = snapshot.get("roomAppsInfo")
    rooms = snapshot.get("roomsInfo")
    if (
        not isinstance(apps, list)
        or any(not isinstance(item, dict) or not _raw_application_id(item)
               or not item.get("roomId") for item in apps)
        or not isinstance(rooms, list)
        or not any(isinstance(room, dict) and str(room.get("roomId")) == room_id for room in rooms)
    ):
        raise MeetingRoomContractMismatch("Cancellation requires a complete target-room snapshot.")
    return any(
        isinstance(item, dict)
        and _raw_application_id(item) == application_id
        and str(item.get("roomId") or "") == room_id
        for item in apps
    )


def _frozen_application_target(raw: dict) -> dict:
    public = public_meeting_room_application(raw)
    return {
        "application_id": public["application_id"],
        "room_id": public["room_id"],
        "room_name": public["room_name"],
        "start_time": public["start_time"],
        "end_time": public["end_time"],
        "audit_status_code": public["audit_status_code"],
        "usage_status_code": public["usage_status_code"],
    }


def _assert_same_frozen_application(target: dict, current: dict) -> None:
    actual = _frozen_application_target(current)
    if actual != target:
        raise MeetingRoomApplicationContractMismatch(
            "The OA meeting-room application changed after authorization."
        )


def _validate_application_plan(plan: dict) -> None:
    if not isinstance(plan, dict) or plan.get("business_intent") != "apply_meeting_room":
        raise MeetingRoomApplicationContractMismatch(
            "The frozen plan is not a meeting-room application plan."
        )
    contract = plan.get("action_contract") if isinstance(plan.get("action_contract"), dict) else {}
    if any(
        (
            contract.get("version") != MEETING_ROOM_APPLICATION_CONTRACT_VERSION,
            contract.get("fingerprint")
            != meeting_room_application_contract_fingerprint(),
        )
    ):
        raise MeetingRoomApplicationContractMismatch(
            "The OA meeting-room application contract changed after authorization."
        )


def _validate_cancel_plan(plan: dict) -> None:
    if (
        not isinstance(plan, dict)
        or plan.get("business_intent") != "cancel_meeting_room_application"
    ):
        raise MeetingRoomApplicationContractMismatch(
            "The frozen plan is not a meeting-room cancellation plan."
        )
    contract = plan.get("action_contract") if isinstance(plan.get("action_contract"), dict) else {}
    if any(
        (
            contract.get("version")
            != MEETING_ROOM_APPLICATION_CANCEL_CONTRACT_VERSION,
            contract.get("fingerprint")
            != meeting_room_application_cancel_contract_fingerprint(),
        )
    ):
        raise MeetingRoomApplicationContractMismatch(
            "The OA meeting-room cancellation contract changed after authorization."
        )


def _ensure_write_accepted(response: Any, operation: str) -> None:
    if isinstance(response, dict) and response.get("success") is False:
        raise MeetingRoomApplicationOutcomeUnknown(
            str(response.get("message") or f"OA rejected the {operation}.")
        )


def _application_ids(rows: list[dict]) -> set[str]:
    return {_raw_application_id(item) for item in rows if _raw_application_id(item)}


def _raw_application_id(raw: dict) -> str:
    return str(raw.get("roomAppId") or raw.get("appId") or "").strip()


def _status_code(raw: dict) -> str:
    value = raw.get("appStatus")
    return str(value if value is not None else "")


def _usage_status_code(raw: dict) -> str:
    value = raw.get("usedStatusDisplay")
    if value is None:
        value = raw.get("usedStatus")
    return str(value if value is not None else "")


def _raw_time_ms(raw: dict, *names: str) -> int | None:
    for name in names:
        value = raw.get(name)
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if text.isdigit():
            return int(text)
        try:
            return _datetime_ms(text)
        except ValueError:
            continue
    return None


def _parse_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    text = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"{name} must use YYYY-MM-DD HH:MM")


def _datetime_ms(value: str) -> int:
    local = _parse_datetime(value, "datetime").replace(
        tzinfo=timezone(timedelta(hours=8))
    )
    return int(local.timestamp() * 1000)


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    text = _optional_text(value, name, maximum)
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _optional_text(value: Any, name: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = value.strip()
    if len(text) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return text


def _fingerprint(contract: dict) -> str:
    canonical = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


MEETING_ROOM_APPLICATION_CREATE_WORKFLOW = WriteWorkflowDefinition(
    prepare_spec=CapabilitySpec(
        name=MEETING_ROOM_APPLICATION_PREPARE_CAPABILITY,
        version="0.1.0",
        description=(
            "Collect a standalone room application's purpose, room, and time in a "
            "trusted card, validate live availability, and create separate authorization."
        ),
        input_schema=MEETING_ROOM_APPLICATION_PREPARE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="controlled_write",
        adapter="seeyon-central",
        workflow="meeting-room-application-prepare-v1",
    ),
    commit_spec=CapabilitySpec(
        name=MEETING_ROOM_APPLICATION_CREATE_CAPABILITY,
        version="0.1.0",
        description=(
            "Consume trusted authorization, recheck availability, submit one standalone "
            "room application, and verify it in My Applications."
        ),
        input_schema=MEETING_ROOM_APPLICATION_CREATE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="controlled_write",
        adapter="seeyon-central",
        workflow="meeting-room-application-commit-v1",
    ),
    required_scopes=frozenset({"oa:write:meeting"}),
    field_schema=MEETING_ROOM_APPLICATION_FIELD_CARD_SCHEMA,
    context_fields=(),
    prepare_function=prepare_meeting_room_application,
    commit_function=create_meeting_room_application,
    contract_error=MeetingRoomApplicationContractMismatch,
    outcome_error=MeetingRoomApplicationOutcomeUnknown,
    field_message="会议室申请字段必须在可信字段卡中核对。",
    authorization_message="会议室申请计划需要在可信授权卡中确认。",
    field_schema_function=build_meeting_room_application_field_card_schema,
)


MEETING_ROOM_APPLICATION_CANCEL_WORKFLOW = WriteWorkflowDefinition(
    prepare_spec=CapabilitySpec(
        name=MEETING_ROOM_APPLICATION_CANCEL_PREPARE_CAPABILITY,
        version="0.1.0",
        description=(
            "Resolve one exact own standalone room application, collect a cancellation "
            "reason in a trusted card, and create separate cancellation authorization."
        ),
        input_schema=MEETING_ROOM_APPLICATION_CANCEL_PREPARE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="controlled_write",
        adapter="seeyon-central",
        workflow="meeting-room-application-cancel-prepare-v1",
    ),
    commit_spec=CapabilitySpec(
        name=MEETING_ROOM_APPLICATION_CANCEL_CAPABILITY,
        version="0.1.0",
        description=(
            "Consume trusted authorization, cancel one exact own standalone room "
            "application, and verify its terminal readback."
        ),
        input_schema=MEETING_ROOM_APPLICATION_CANCEL_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="controlled_write",
        adapter="seeyon-central",
        workflow="meeting-room-application-cancel-commit-v1",
    ),
    required_scopes=frozenset({"oa:write:meeting"}),
    field_schema=MEETING_ROOM_APPLICATION_CANCEL_FIELD_CARD_SCHEMA,
    context_fields=("application_id",),
    prepare_function=prepare_meeting_room_application_cancel,
    commit_function=cancel_meeting_room_application,
    contract_error=MeetingRoomApplicationContractMismatch,
    outcome_error=MeetingRoomApplicationOutcomeUnknown,
    field_message="会议室申请撤销原因必须在可信字段卡中核对。",
    authorization_message="会议室申请撤销计划需要在可信授权卡中确认。",
)
