from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse

from bscli.adapters.seeyon_meeting_room import (
    MeetingRoomContractMismatch,
    available_rooms as shared_available_rooms,
    query_meeting_room_snapshot,
    resolve_room as shared_resolve_room,
    room_app as shared_room_app,
    room_is_available as shared_room_is_available,
)
from bscli.core.capability import CapabilitySpec
from bscli.core.write_workflow import WriteWorkflowDefinition


MEETING_PREPARE_CAPABILITY = "oa.meeting.create.prepare"
MEETING_CREATE_CAPABILITY = "oa.meeting.create"
MEETING_CONTRACT_VERSION = "seeyon-meeting-create-v1"

MEETING_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "maxLength": 255},
        "room": {"type": "string", "maxLength": 100},
        "start_time": {"type": "string", "maxLength": 32},
        "end_time": {"type": "string", "maxLength": 32},
        "input_submission_id": {"type": "string"},
    },
    "additionalProperties": False,
}

MEETING_FIELD_CARD_SCHEMA = {
    "schema_version": "agentbridge.oa_meeting_create_fields.v1",
    "title": "填写会议创建信息",
    "system": "致远 OA",
    "effect": "创建并发送完整会议，可同时占用会议室",
    "submit_label": "提交字段",
    "notice": "这是会议创建流程，不是独立会议室申请。字段提交后还需单独授权；授权前不会创建或发送会议。",
    "fields": [
        {
            "name": "subject",
            "label": "会议主题",
            "control": "text",
            "required": True,
            "max_length": 255,
            "autocomplete": "off",
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
            "message": "结束时间必须晚于开始时间，且会议时长不能超过 7 天。",
        }
    ],
}

MEETING_CREATE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"authorization_id": {"type": "string"}},
    "required": ["authorization_id"],
    "additionalProperties": False,
}


class MeetingContractMismatch(RuntimeError):
    pass


class MeetingOutcomeUnknown(RuntimeError):
    pass


def build_meeting_field_card_schema(adapter, worker, arguments: dict) -> dict:
    """Build a prefilled card from one live, read-only room availability query."""
    seed = _normalize_meeting_card_seed(arguments)
    start_ms = _datetime_ms(seed["start_time"])
    end_ms = _datetime_ms(seed["end_time"])
    meeting_info = _ajax(worker, adapter, "meetingInfo", [{"meetingId": "", "templateId": ""}])
    _validate_meeting_info(meeting_info)
    room_list = _query_room_list(worker, adapter, start_ms=start_ms, end_ms=end_ms)
    available_rooms = _available_rooms(room_list, start_ms=start_ms, end_ms=end_ms)
    if not available_rooms:
        raise MeetingContractMismatch(
            "No OA meeting rooms are available for the requested time range."
        )

    selected_room = None
    room_match_note = ""
    if seed["room"]:
        try:
            requested_room = _resolve_room(seed["room"], room_list)
        except MeetingContractMismatch:
            room_match_note = "未能唯一匹配输入的会议室，请从 OA 当前空闲会议室中选择。"
        else:
            requested_app = _room_app(
                requested_room,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            if _room_is_available(room_list, requested_app):
                selected_room = requested_room
            else:
                room_match_note = "输入的会议室在该时段已占用，请选择其他空闲会议室。"

    schema = deepcopy(MEETING_FIELD_CARD_SCHEMA)
    fields = {item["name"]: item for item in schema["fields"]}
    fields["subject"]["value"] = seed["subject"]
    fields["room"] = {
        "name": "room",
        "label": "会议室",
        "control": "select",
        "required": True,
        "options": [
            {"value": str(room["roomName"]), "label": str(room["roomName"])}
            for room in available_rooms
        ],
        "value": str(selected_room["roomName"]) if selected_room else "",
    }
    fields["start_time"]["value"] = seed["start_time"]
    fields["end_time"]["value"] = seed["end_time"]
    schema["fields"] = [fields[item["name"]] for item in schema["fields"]]
    checked_note = (
        f"已按 {seed['start_time']} 至 {seed['end_time']} 查询 OA，"
        f"当前有 {len(available_rooms)} 个空闲会议室。"
    )
    schema["notice"] = " ".join(
        part
        for part in (
            checked_note,
            room_match_note,
            "这是会议创建流程，不是独立会议室申请。提交字段后会再次校验；授权前不会创建或发送会议。",
        )
        if part
    )
    return schema


def prepare_meeting_create(adapter, worker, arguments: dict) -> dict:
    inputs = normalize_meeting_inputs(arguments)
    start_ms = _datetime_ms(inputs["start_time"])
    end_ms = _datetime_ms(inputs["end_time"])
    meeting_info = _ajax(worker, adapter, "meetingInfo", [{"meetingId": "", "templateId": ""}])
    _validate_meeting_info(meeting_info)
    room_list = _query_room_list(worker, adapter, start_ms=start_ms, end_ms=end_ms)
    room = _resolve_room(inputs["room"], room_list)
    room_app = _room_app(room, start_ms=start_ms, end_ms=end_ms)
    _assert_room_available(room_list, room_app)
    _validate_room_apps(worker, adapter, room_app)
    return {
        "plan": {
            "schema_version": "agentbridge.oa_meeting_create_plan.v1",
            "business_intent": "create_meeting",
            "target": {
                "room_id": room_app["roomId"],
                "room_name": room_app["roomName"],
            },
            "action_contract": {
                "version": MEETING_CONTRACT_VERSION,
                "fingerprint": meeting_contract_fingerprint(),
                "attendee_policy": "current_user_only",
                "verification": ["room_list_readback", "meeting_view_readback"],
            },
            "exact_input": inputs,
            "preconditions": {
                "meeting_info_loaded": True,
                "room_resolved_uniquely": True,
                "room_available": True,
                "oa_room_validation_passed": True,
            },
            "expected_effect": {
                "meeting_created": True,
                "meeting_sent": True,
                "submitted_count": 1,
            },
        },
        "summary": meeting_summary(inputs, room_app["roomName"]),
    }


def create_meeting(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    _validate_plan(plan)
    inputs = normalize_meeting_inputs(plan.get("exact_input") or {})
    start_ms = _datetime_ms(inputs["start_time"])
    end_ms = _datetime_ms(inputs["end_time"])

    meeting_info = _ajax(worker, adapter, "meetingInfo", [{"meetingId": "", "templateId": ""}])
    _validate_meeting_info(meeting_info)
    room_list = _query_room_list(worker, adapter, start_ms=start_ms, end_ms=end_ms)
    room = _resolve_room(inputs["room"], room_list)
    room_app = _room_app(room, start_ms=start_ms, end_ms=end_ms)
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    if (
        room_app["roomId"] != str(target.get("room_id") or "")
        or room_app["roomName"] != str(target.get("room_name") or "")
    ):
        raise MeetingContractMismatch("The resolved OA meeting room changed after authorization.")
    _assert_room_available(room_list, room_app)
    _validate_room_apps(worker, adapter, room_app)
    send_payload = _build_send_payload(
        meeting_info,
        subject=inputs["subject"],
        room_app=room_app,
        start_ms=start_ms,
        end_ms=end_ms,
    )

    boundary_crossed = False
    enter_commit_boundary()
    boundary_crossed = True
    try:
        content_save = _content_save(worker, adapter, meeting_info, inputs["subject"])
        send_result = _ajax(worker, adapter, "send", [send_payload])
        if isinstance(send_result, dict) and send_result.get("success") is False:
            raise MeetingOutcomeUnknown(
                str(send_result.get("message") or "OA reported that meeting creation failed.")
            )
        verify_list = _query_room_list(worker, adapter, start_ms=start_ms, end_ms=end_ms)
        room_readback = _verify_room_readback(
            verify_list,
            room_id=room_app["roomId"],
            start_ms=start_ms,
            end_ms=end_ms,
            subject=inputs["subject"],
        )
        meeting_id = str(room_readback.get("meetingId") or "")
        if not meeting_id:
            raise MeetingOutcomeUnknown(
                "The created meeting was found, but OA did not return a stable meeting ID."
            )
        meeting_view = _ajax(
            worker,
            adapter,
            "meetingView",
            [{"meetingId": meeting_id, "proxyId": ""}],
        )
        _verify_meeting_view(meeting_view, subject=inputs["subject"])
        return {
            "schema_version": "agentbridge.oa_meeting_create_result.v1",
            "business_intent": "create_meeting",
            "meeting_created": True,
            "meeting_sent": True,
            "submitted_count": 1,
            "meeting": {
                "meeting_id": meeting_id,
                "subject": inputs["subject"],
                "room_id": room_app["roomId"],
                "room_name": room_app["roomName"],
                "start_time": inputs["start_time"],
                "end_time": inputs["end_time"],
            },
            "verification": {
                "confirmed": True,
                "methods": ["room_list_readback", "meeting_view_readback"],
                "content_saved": bool(content_save.get("content_id")),
            },
            "transport": "central_http_session",
        }
    except MeetingOutcomeUnknown:
        raise
    except BaseException as exc:
        if boundary_crossed:
            raise MeetingOutcomeUnknown(
                "The OA meeting-create boundary was crossed, but verification failed."
            ) from exc
        raise


def normalize_meeting_inputs(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("meeting input must be an object")
    start = _parse_datetime(arguments.get("start_time"), "start_time")
    end = _parse_datetime(arguments.get("end_time"), "end_time")
    if end <= start:
        raise ValueError("end_time must be later than start_time")
    if end - start > timedelta(days=7):
        raise ValueError("meeting duration must not exceed 7 days")
    return {
        "subject": _bounded_text(arguments.get("subject"), "subject", 255),
        "room": _bounded_text(arguments.get("room"), "room", 100),
        "start_time": start.strftime("%Y-%m-%d %H:%M"),
        "end_time": end.strftime("%Y-%m-%d %H:%M"),
    }


def _normalize_meeting_card_seed(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("meeting input must be an object")
    start = _parse_datetime(arguments.get("start_time"), "start_time")
    end = _parse_datetime(arguments.get("end_time"), "end_time")
    if end <= start:
        raise ValueError("end_time must be later than start_time")
    if end - start > timedelta(days=7):
        raise ValueError("meeting duration must not exceed 7 days")
    return {
        "subject": _optional_bounded_text(arguments.get("subject"), "subject", 255),
        "room": _optional_bounded_text(arguments.get("room"), "room", 100),
        "start_time": start.strftime("%Y-%m-%d %H:%M"),
        "end_time": end.strftime("%Y-%m-%d %H:%M"),
    }


def meeting_summary(inputs: dict, room_name: str) -> dict:
    return {
        "title": "创建并发送会议",
        "system": "致远 OA",
        "effect": "预订会议室并向当前用户发送会议",
        "authorization_notice": "授权后将立即预订上述会议室并发送会议，不会仅保存为草稿。",
        "authorize_label": "授权创建并发送",
        "fields": [
            {"label": "会议主题", "value": inputs["subject"]},
            {"label": "会议室", "value": room_name},
            {"label": "开始时间", "value": inputs["start_time"]},
            {"label": "结束时间", "value": inputs["end_time"]},
            {"label": "参会人", "value": "当前 OA 用户"},
        ],
        "submitted_count": 1,
    }


def meeting_contract_fingerprint() -> str:
    contract = {
        "version": MEETING_CONTRACT_VERSION,
        "manager": "meetingAjaxManager",
        "sequence": [
            "meetingInfo",
            "roomListInfo",
            "validateRoomApps",
            "content.saveOrUpdate",
            "send",
            "roomListInfo",
            "meetingView",
        ],
        "attendee_policy": "current_user_only",
        "body_type_default": "10",
        "content_module_type": "6",
    }
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _validate_plan(plan: dict) -> None:
    if not isinstance(plan, dict) or plan.get("business_intent") != "create_meeting":
        raise MeetingContractMismatch("The frozen plan is not a meeting-create plan.")
    contract = plan.get("action_contract") if isinstance(plan.get("action_contract"), dict) else {}
    if (
        contract.get("version") != MEETING_CONTRACT_VERSION
        or contract.get("fingerprint") != meeting_contract_fingerprint()
    ):
        raise MeetingContractMismatch("The OA meeting contract changed after authorization.")


def _ajax(worker, adapter, manager_method: str, arguments: list[Any]) -> Any:
    url = urljoin(
        adapter.base_url,
        "/seeyon/ajax.do?method=ajaxAction&managerName=meetingAjaxManager",
    )
    body = urlencode(
        {
            "managerMethod": manager_method,
            "arguments": json.dumps(arguments, ensure_ascii=True, separators=(",", ":")),
        }
    )
    response = worker.request(
        "POST",
        url,
        headers={"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
        body=body,
    )
    data = _response_json(response, context=manager_method)
    if isinstance(data, dict) and data.get("code") and data.get("message"):
        raise MeetingContractMismatch(str(data.get("message")))
    return data


def _content_save(worker, adapter, meeting_info: dict, subject: str) -> dict:
    module_id = str(meeting_info.get("id_temp") or "").strip()
    current_user = meeting_info.get("currentUser") if isinstance(meeting_info.get("currentUser"), dict) else {}
    create_id = str(current_user.get("id") or "").strip()
    if not create_id:
        source = str(meeting_info.get("emceeId") or meeting_info.get("recorderId") or "")
        if "|" in source:
            create_id = source.split("|", 1)[1].strip()
    payload = {
        "_currentDiv": {"_currentDiv": "0"},
        "secretLevelId": {"secretLevelId": ""},
        "mainbodyDataDiv_0": {
            "id": "",
            "createId": create_id,
            "createDate": "",
            "modifyId": "",
            "modifyDate": "",
            "moduleType": "6",
            "moduleId": module_id,
            "contentType": str(meeting_info.get("bodyType") or "10"),
            "moduleTemplateId": "0",
            "contentTemplateId": "0",
            "sort": "0",
            "title": subject,
            "content": "",
            "rightId": "",
            "status": "STATUS_RESPONSE_NEW",
            "viewState": "1",
            "hasHtmlSignature": "0",
            "contentDataId": "",
        },
    }
    body = urlencode(
        {"_json_params": json.dumps(payload, ensure_ascii=True, separators=(",", ":"))}
    )
    url = urljoin(
        adapter.base_url,
        "/seeyon/content/content.do?method=saveOrUpdate&onlyGenerateSn=false&optType=undefined&_affairId=&_openFrom=",
    )
    response = worker.request(
        "POST",
        url,
        headers={"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
        body=body,
    )
    data = _response_json(response, context="meeting content save")
    if not isinstance(data, dict):
        raise MeetingOutcomeUnknown("OA meeting content save did not return an object.")
    success = data.get("success")
    if success is not True and str(success).lower() != "true":
        raise MeetingOutcomeUnknown(
            str(data.get("errorMsg") or data.get("message") or "OA meeting content save failed.")
        )
    content_all = data.get("contentAll") if isinstance(data.get("contentAll"), dict) else {}
    return {
        "content_id": str(content_all.get("id") or ""),
        "module_id": str(content_all.get("moduleId") or module_id),
    }


def _response_json(response: dict, *, context: str) -> Any:
    status = int(response.get("status") or 0)
    final_url = str(response.get("url") or "")
    response_text = str(response.get("text") or "")
    login_page = any(
        (
            "login" in urlparse(final_url).path.lower(),
            "method=login" in final_url.lower(),
            "type=\"password\"" in response_text.lower(),
            "type='password'" in response_text.lower(),
        )
    )
    if status in {301, 302, 303, 307, 308, 401, 403} or login_page:
        from bscli.adapters.seeyon_central import SeeyonLoginRequired

        raise SeeyonLoginRequired(f"The central OA session expired during {context}.")
    if status < 200 or status >= 300:
        raise MeetingContractMismatch(f"OA {context} returned HTTP {status}.")
    data = response.get("json")
    if data is None:
        raise MeetingContractMismatch(f"OA {context} did not return JSON.")
    return data


def _validate_meeting_info(meeting_info: Any) -> None:
    if not isinstance(meeting_info, dict):
        raise MeetingContractMismatch("OA meetingInfo did not return an object.")
    if not str(meeting_info.get("id_temp") or ""):
        raise MeetingContractMismatch("OA meetingInfo did not include id_temp.")
    current_user = meeting_info.get("currentUser")
    if not isinstance(current_user, dict) or not str(current_user.get("id") or ""):
        raise MeetingContractMismatch("OA meetingInfo did not identify the current user.")


def _query_room_list(worker, adapter, *, start_ms: int, end_ms: int) -> dict:
    try:
        return query_meeting_room_snapshot(
            worker,
            adapter,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    except MeetingRoomContractMismatch as exc:
        raise MeetingContractMismatch(str(exc)) from exc


def _resolve_room(requested: str, room_list: Any) -> dict:
    try:
        return shared_resolve_room(requested, room_list)
    except MeetingRoomContractMismatch as exc:
        raise MeetingContractMismatch(str(exc)) from exc


def _room_app(room: dict, *, start_ms: int, end_ms: int) -> dict:
    try:
        return shared_room_app(room, start_ms=start_ms, end_ms=end_ms)
    except MeetingRoomContractMismatch as exc:
        raise MeetingContractMismatch(str(exc)) from exc


def _assert_room_available(room_list: Any, room_app: dict) -> None:
    if _room_is_available(room_list, room_app):
        return
    raise MeetingContractMismatch(
        "The requested OA meeting room is occupied for this time range."
    )


def _room_is_available(room_list: Any, room_app: dict) -> bool:
    return shared_room_is_available(room_list, room_app)


def _available_rooms(room_list: Any, *, start_ms: int, end_ms: int) -> list[dict]:
    return shared_available_rooms(room_list, start_ms=start_ms, end_ms=end_ms)


def _validate_room_apps(worker, adapter, room_app: dict) -> None:
    result = _ajax(
        worker,
        adapter,
        "validateRoomApps",
        [{"roomApps": [room_app], "meetingId": "", "periodicityId": ""}],
    )
    if isinstance(result, dict) and result.get("success") is False:
        raise MeetingContractMismatch(
            str(result.get("message") or "OA meeting-room validation failed.")
        )
    data = result.get("data") if isinstance(result, dict) else []
    if not isinstance(data, list):
        data = []
    errors = [
        str(item.get("message") or "OA meeting-room validation failed.")
        for item in data
        if isinstance(item, dict) and item.get("validate")
    ]
    if errors:
        raise MeetingContractMismatch("; ".join(dict.fromkeys(errors)))


def _build_send_payload(
    meeting_info: dict,
    *,
    subject: str,
    room_app: dict,
    start_ms: int,
    end_ms: int,
) -> dict:
    current_user = meeting_info.get("currentUser") if isinstance(meeting_info.get("currentUser"), dict) else {}
    self_source = str(meeting_info.get("emceeId") or "")
    if not self_source and current_user.get("id"):
        self_source = f"Member|{current_user['id']}"
    if not self_source:
        raise MeetingContractMismatch("The current OA user could not be resolved as an attendee.")
    meeting_types = meeting_info.get("meetingTypes") if isinstance(meeting_info.get("meetingTypes"), list) else []
    meeting_type = meeting_types[0] if meeting_types and isinstance(meeting_types[0], dict) else {}
    return {
        "meetingId": "",
        "id_temp": str(meeting_info.get("id_temp") or ""),
        "isBatch": None,
        "title": subject,
        "beginDate": start_ms,
        "endDate": end_ms,
        "emceeValue": str(meeting_info.get("emceeId") or self_source),
        "recorderValue": str(meeting_info.get("recorderId") or self_source),
        "conferees": self_source,
        "impart": "",
        "resourcesId": "",
        "beforeTime": meeting_info.get("beforeTime") if meeting_info.get("beforeTime") is not None else 10,
        "meetingTypeId": str(meeting_info.get("meetingTypeId") or meeting_type.get("id") or ""),
        "meetingTypeName": str(meeting_type.get("name") or ""),
        "meetingType": "1",
        "isSendTextMessages": 0,
        "projectId": "",
        "projectName": "",
        "qrCodeSign": 0,
        "isPublic": 0,
        "mtTitle": subject,
        "leader": "",
        "attender": "",
        "tel": "",
        "notice": "",
        "plan": "",
        "meetPlace": "",
        "selectedRoomApps": [room_app],
        "selectedVideoRoom": {},
        "meetingPassword": "",
        "videoRoomShow": "",
        "selectedPeriodicity": None,
        "content": "",
        "bodyType": str(meeting_info.get("bodyType") or "10"),
        "sourceId": "0",
        "sourceType": "0",
        "linkConfigId": "",
    }


def _verify_room_readback(
    room_list: Any,
    *,
    room_id: str,
    start_ms: int,
    end_ms: int,
    subject: str,
) -> dict:
    apps = room_list.get("roomAppsInfo") if isinstance(room_list, dict) else []
    for app in apps if isinstance(apps, list) else []:
        if not isinstance(app, dict) or str(app.get("roomId") or "") != room_id:
            continue
        if _safe_int(app.get("appBeginDate")) != start_ms or _safe_int(app.get("appEndDate")) != end_ms:
            continue
        description = str(app.get("description") or app.get("title") or "")
        if description and subject not in description:
            continue
        return {
            "meetingId": str(app.get("meetingId") or ""),
            "description": description,
        }
    raise MeetingOutcomeUnknown(
        "The created meeting was not found in the OA room-list readback."
    )


def _verify_meeting_view(meeting_view: Any, *, subject: str) -> None:
    if not isinstance(meeting_view, dict):
        raise MeetingOutcomeUnknown("OA meetingView did not return an object.")
    text = json.dumps(meeting_view, ensure_ascii=False, sort_keys=True)
    if subject not in text:
        raise MeetingOutcomeUnknown("OA meetingView did not contain the authorized subject.")
    if "index超出正文数量" in text:
        raise MeetingOutcomeUnknown("OA meetingView reported an invalid meeting body binding.")


def _parse_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must use YYYY-MM-DD HH:MM")
    try:
        return datetime.strptime(value.strip().replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD HH:MM") from exc


def _datetime_ms(value: str) -> int:
    parsed = _parse_datetime(value, "datetime")
    return int(parsed.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)


def _optional_bounded_text(value: Any, name: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = re.sub(r"[\r\n\t]+", " ", value).strip()
    if len(text) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
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


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


MEETING_CREATE_WORKFLOW = WriteWorkflowDefinition(
    prepare_spec=CapabilitySpec(
        name=MEETING_PREPARE_CAPABILITY,
        version="0.1.0",
        description=(
            "Collect meeting fields in a trusted card, resolve and validate room "
            "availability, and create a separate meeting-create authorization."
        ),
        input_schema=MEETING_PREPARE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="controlled_write",
        adapter="seeyon-central",
        workflow="meeting-create-prepare-v1",
    ),
    commit_spec=CapabilitySpec(
        name=MEETING_CREATE_CAPABILITY,
        version="0.1.0",
        description=(
            "Consume one trusted authorization, recheck room availability, create "
            "and send the meeting, then verify room-list and meeting-view readback."
        ),
        input_schema=MEETING_CREATE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="controlled_write",
        adapter="seeyon-central",
        workflow="meeting-create-commit-v1",
    ),
    required_scopes=frozenset({"oa:write:meeting"}),
    field_schema=MEETING_FIELD_CARD_SCHEMA,
    context_fields=(),
    prepare_function=prepare_meeting_create,
    commit_function=create_meeting,
    contract_error=MeetingContractMismatch,
    outcome_error=MeetingOutcomeUnknown,
    field_message="Meeting fields must be entered in the trusted field card.",
    authorization_message="The meeting-create plan requires confirmation in the trusted action card.",
    field_schema_function=build_meeting_field_card_schema,
)
