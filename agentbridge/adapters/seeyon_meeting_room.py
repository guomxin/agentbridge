from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import re
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse


MEETING_ROOM_AVAILABILITY_CAPABILITY = "oa.meeting_room.availability.list"
MEETING_ROOM_MY_APPLICATIONS_CAPABILITY = "oa.meeting_room.my_applications.list"

MEETING_ROOM_CAPABILITIES = frozenset(
    {
        MEETING_ROOM_AVAILABILITY_CAPABILITY,
        MEETING_ROOM_MY_APPLICATIONS_CAPABILITY,
    }
)

MEETING_ROOM_AVAILABILITY_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "start_time": {"type": "string", "maxLength": 32},
        "end_time": {"type": "string", "maxLength": 32},
        "room_name": {"type": "string", "maxLength": 100},
        "minimum_capacity": {"type": "integer", "minimum": 0, "maximum": 100000},
        "only_available": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
    },
    "additionalProperties": False,
}

MEETING_ROOM_MY_APPLICATIONS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "room_name": {"type": "string", "maxLength": 100},
        "application_start_date": {
            "type": "string",
            "format": "date",
            "description": "Lower bound for the date the application was submitted.",
        },
        "application_end_date": {
            "type": "string",
            "format": "date",
            "description": "Upper bound for the date the application was submitted.",
        },
        "usage_start_date": {
            "type": "string",
            "format": "date",
            "description": "Lower bound for the reserved room usage date.",
        },
        "usage_end_date": {
            "type": "string",
            "format": "date",
            "description": "Upper bound for the reserved room usage date.",
        },
        "audit_status": {
            "type": "string",
            "enum": ["pending", "approved", "rejected"],
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
    },
    "additionalProperties": False,
}

MEETING_ROOM_INPUT_SCHEMAS = {
    MEETING_ROOM_AVAILABILITY_CAPABILITY: MEETING_ROOM_AVAILABILITY_INPUT_SCHEMA,
    MEETING_ROOM_MY_APPLICATIONS_CAPABILITY: MEETING_ROOM_MY_APPLICATIONS_INPUT_SCHEMA,
}

_SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
_AUDIT_STATUS_TO_CODE = {"pending": "0", "approved": "1", "rejected": "2"}
_AUDIT_CODE_TO_LABEL = {"0": "待审核", "1": "审核通过", "2": "审核未通过"}
_ROOM_APPROVAL_KNOWN_CODES = frozenset({"0", "1", "2"})
_ROOM_APPROVAL_REQUIRED_CODE = "1"


class MeetingRoomContractMismatch(RuntimeError):
    pass


def invoke_meeting_room_capability(
    capability_name: str,
    adapter,
    worker,
    arguments: dict,
) -> dict:
    if capability_name == MEETING_ROOM_AVAILABILITY_CAPABILITY:
        return list_meeting_room_availability(adapter, worker, arguments)
    if capability_name == MEETING_ROOM_MY_APPLICATIONS_CAPABILITY:
        return list_my_meeting_room_applications(adapter, worker, arguments)
    raise KeyError(f"unsupported meeting-room capability: {capability_name}")


def list_meeting_room_availability(adapter, worker, arguments: dict) -> dict:
    query = _normalize_availability_arguments(arguments)
    start_ms = _datetime_ms(query["start_time"]) if query["start_time"] else None
    end_ms = _datetime_ms(query["end_time"]) if query["end_time"] else None
    snapshot = query_meeting_room_snapshot(
        worker,
        adapter,
        start_ms=start_ms,
        end_ms=end_ms,
        # OA applies literal server-side matching here. Load the visible directory
        # so aliases such as "三号会议室" can resolve to "4层3#会议室".
        room_name="",
    )
    room_match = match_meeting_rooms(query["room_name"], snapshot)
    rooms = room_match.pop("rooms")
    public_rooms = []
    for room in rooms if isinstance(rooms, list) else []:
        if not isinstance(room, dict):
            continue
        room_name = str(room.get("roomName") or "").strip()
        room_id = str(room.get("roomId") or "").strip()
        if not room_name or not room_id:
            continue
        capacity = _safe_int(room.get("seatCount"))
        if query["minimum_capacity"] and (capacity or 0) < query["minimum_capacity"]:
            continue
        available = None
        if start_ms is not None and end_ms is not None:
            available = room_is_available(
                snapshot,
                room_app(room, start_ms=start_ms, end_ms=end_ms),
            )
            if query["only_available"] and not available:
                continue
        public_rooms.append(
            {
                "room_id": room_id,
                "room_name": room_name,
                "capacity": capacity,
                **_public_approval_requirement(room.get("needApp")),
                "available": available,
                "busy_intervals": _public_busy_intervals(
                    snapshot,
                    room_id=room_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                ),
            }
        )

    matched_count = len(public_rooms)
    public_rooms = public_rooms[: query["limit"]]
    available_count = sum(room["available"] is True for room in public_rooms)
    occupied_count = sum(room["available"] is False for room in public_rooms)
    return {
        "schema_version": "agentbridge.oa_meeting_room_availability.v1",
        "query": {
            "start_time": query["start_time"] or None,
            "end_time": query["end_time"] or None,
            "room_name": query["room_name"] or None,
            "minimum_capacity": query["minimum_capacity"] or None,
            "only_available": query["only_available"],
        },
        "availability_evaluated": start_ms is not None,
        "room_match": room_match,
        "count": len(public_rooms),
        "matched_count": matched_count,
        "rooms": public_rooms,
        "summary": {
            "room_count": len(public_rooms),
            "available_count": available_count if start_ms is not None else None,
            "occupied_count": occupied_count if start_ms is not None else None,
        },
        "coverage": {
            "status": "complete" if matched_count <= len(public_rooms) else "partial",
            "source": "meetingAjaxManager.roomListInfo",
            "output_truncated": matched_count > len(public_rooms),
        },
        "transport": "central_browser_session",
    }


def list_my_meeting_room_applications(adapter, worker, arguments: dict) -> dict:
    query = _normalize_my_applications_arguments(arguments)
    search_params: dict[str, Any] = {}
    if query["application_start_date"]:
        search_params["beginDate"] = f"{query['application_start_date']} 00:00"
    if query["application_end_date"]:
        search_params["endDate"] = f"{query['application_end_date']} 23:59"
    if query["audit_status"]:
        search_params["status"] = _AUDIT_STATUS_TO_CODE[query["audit_status"]]

    # OA treats roomName as a literal filter, so conversational aliases such as
    # "五号会议室" would hide the formal "4层5#会议室" row. Load the bounded
    # application collection and apply the shared room-name semantics locally.
    page_size = 200
    raw_result = query_my_meeting_room_applications(
        worker,
        adapter,
        search_params=search_params,
        page_size=page_size,
        maximum_items=4000,
    )
    raw_items = raw_result["items"]
    source_total = raw_result["total"]
    source_pages = raw_result["pages"]
    page = raw_result["pages_loaded"]

    items = [_public_application(item) for item in raw_items]
    items = [item for item in items if _application_matches(item, query)]
    matched_count = len(items)
    items = items[: query["limit"]]
    source_exhausted = bool(
        source_total is not None
        and len(raw_items) >= source_total
        and (source_pages is None or page >= source_pages)
    )
    output_truncated = matched_count > len(items) or (
        source_total is not None and source_total > len(raw_items)
    )
    return {
        "schema_version": "agentbridge.oa_meeting_room_my_applications.v1",
        "query": {
            "room_name": query["room_name"] or None,
            "application_start_date": query["application_start_date"] or None,
            "application_end_date": query["application_end_date"] or None,
            "usage_start_date": query["usage_start_date"] or None,
            "usage_end_date": query["usage_end_date"] or None,
            "audit_status": query["audit_status"] or None,
        },
        "count": len(items),
        "matched_count": matched_count,
        "source_total": source_total,
        "items": items,
        "coverage": {
            "status": "complete" if source_exhausted and not output_truncated else "partial",
            "source": "meetingAjaxManager.getMyApps",
            "server_filters": sorted(search_params),
            "local_filters": [
                name
                for name in ("room_name", "usage_start_date", "usage_end_date")
                if query[name]
            ],
            "output_truncated": output_truncated,
            "source_pages_loaded": page,
        },
        "transport": "central_browser_session",
    }


def query_my_meeting_room_applications(
    worker,
    adapter,
    *,
    search_params: dict[str, Any] | None = None,
    page_size: int = 100,
    maximum_items: int = 2000,
) -> dict:
    """Load the authenticated user's raw meeting-room applications."""
    page = 1
    page_size = min(200, max(1, int(page_size)))
    maximum_items = min(4000, max(1, int(maximum_items)))
    source_total: int | None = None
    source_pages: int | None = None
    raw_items: list[dict] = []
    observed_totals: set[int] = set()
    while page <= 20 and len(raw_items) < maximum_items:
        payload = meeting_ajax(
            worker,
            adapter,
            "getMyApps",
            [{"page": page, "size": page_size}, dict(search_params or {})],
        )
        if not isinstance(payload, dict):
            raise MeetingRoomContractMismatch("OA getMyApps did not return an object.")
        data = payload.get("data")
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise MeetingRoomContractMismatch("OA getMyApps returned an invalid data list.")
        page_items = data
        raw_items.extend(page_items)
        source_total = _safe_int(payload.get("total"))
        if source_total is not None:
            observed_totals.add(source_total)
        source_pages = _safe_int(payload.get("pages"))
        if not page_items or (source_pages is not None and page >= source_pages):
            break
        if source_total is not None and len(raw_items) >= source_total:
            break
        page += 1
    ids = [str(item.get("roomAppId") or item.get("appId") or "") for item in raw_items]
    complete = (
        source_total is not None
        and len(observed_totals) == 1
        and source_total == len(raw_items) == len(set(ids))
        and all(ids)
        and len(raw_items) <= maximum_items
    )
    return {
        "items": raw_items[:maximum_items],
        "total": source_total,
        "pages": source_pages,
        "pages_loaded": page,
        "complete": complete,
    }


def public_meeting_room_application(item: dict) -> dict:
    return _public_application(item)


def query_meeting_room_snapshot(
    worker,
    adapter,
    *,
    start_ms: int | None,
    end_ms: int | None,
    room_name: str = "",
) -> dict:
    payload = meeting_ajax(
        worker,
        adapter,
        "roomListInfo",
        [
            {
                "roomName": room_name,
                "sortType": "-1",
                "startDatetime": start_ms,
                "endDatetime": end_ms,
            }
        ],
    )
    if not isinstance(payload, dict):
        raise MeetingRoomContractMismatch("OA roomListInfo did not return an object.")
    return payload


def meeting_ajax(
    worker,
    adapter,
    manager_method: str,
    arguments: list[Any],
    *,
    allow_empty_success: bool = False,
    response_diagnostics: dict | None = None,
) -> Any:
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
    if response_diagnostics is not None:
        text = str(response.get("text") or "").strip()
        response_diagnostics.update({
            "http_status": int(response.get("status") or 0),
            "content_type": str(response.get("content_type") or "")[:120],
            "body_kind": (
                "empty" if not text else "json_null" if text == "null"
                else "parsed_json" if response.get("json") is not None
                else "unparsed_body"
            ),
            "body_length": len(text),
        })
    data = response_json(
        response,
        context=manager_method,
        allow_empty_success=allow_empty_success,
    )
    if isinstance(data, dict) and data.get("code") and data.get("message"):
        raise MeetingRoomContractMismatch(str(data.get("message")))
    return data


def room_app(room: dict, *, start_ms: int, end_ms: int) -> dict:
    room_id = str(room.get("roomId") or "")
    room_name = str(room.get("roomName") or "")
    if not room_id or not room_name:
        raise MeetingRoomContractMismatch("The resolved OA meeting room is incomplete.")
    return {
        "roomId": room_id,
        "roomName": room_name,
        "pId": str(room.get("roomTypeId") or "-1"),
        "appBeginDate": int(start_ms),
        "appEndDate": int(end_ms),
    }


def room_is_available(room_list: Any, selected_room_app: dict) -> bool:
    apps = room_list.get("roomAppsInfo") if isinstance(room_list, dict) else []
    for app in apps if isinstance(apps, list) else []:
        if not isinstance(app, dict) or str(app.get("roomId") or "") != selected_room_app["roomId"]:
            continue
        app_start = _safe_int(app.get("appBeginDate"))
        app_end = _safe_int(app.get("appEndDate"))
        if app_start is None or app_end is None:
            continue
        if app_start < selected_room_app["appEndDate"] and app_end > selected_room_app["appBeginDate"]:
            return False
    return True


def available_rooms(room_list: Any, *, start_ms: int, end_ms: int) -> list[dict]:
    rooms = room_list.get("roomsInfo") if isinstance(room_list, dict) else []
    available = []
    for room in rooms if isinstance(rooms, list) else []:
        if not isinstance(room, dict):
            continue
        try:
            selected_room_app = room_app(room, start_ms=start_ms, end_ms=end_ms)
        except MeetingRoomContractMismatch:
            continue
        if room_is_available(room_list, selected_room_app):
            available.append(room)
    return available


def resolve_room(requested: str, room_list: Any) -> dict:
    match = match_meeting_rooms(requested, room_list)
    candidates = match["rooms"]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise MeetingRoomContractMismatch(f"OA meeting room was not found: {requested}")
    names = ", ".join(str(item.get("roomName") or "") for item in candidates)
    raise MeetingRoomContractMismatch(f"OA meeting room is ambiguous: {requested} -> {names}")


def match_meeting_rooms(requested: str, room_list: Any) -> dict:
    raw_rooms = room_list.get("roomsInfo") if isinstance(room_list, dict) else []
    rooms = [
        room
        for room in raw_rooms if isinstance(raw_rooms, list)
        if isinstance(room, dict)
        and str(room.get("roomId") or "").strip()
        and str(room.get("roomName") or "").strip()
    ]
    requested = str(requested or "").strip()
    if not requested:
        return {
            "requested_name": None,
            "status": "not_filtered",
            "strategy": None,
            "candidate_count": len(rooms),
            "rooms": rooms,
        }

    requested_casefold = requested.casefold()
    requested_norm = _normalize_room_name(requested)
    requested_number = _room_number(requested, requested=True)

    exact_name = [
        room
        for room in rooms
        if str(room.get("roomName") or "").strip().casefold() == requested_casefold
    ]
    normalized_name = [
        room
        for room in rooms
        if requested_norm
        and _normalize_room_name(str(room.get("roomName") or "")) == requested_norm
    ]
    numeric_alias = [
        room
        for room in rooms
        if requested_number
        and _room_number(str(room.get("roomName") or ""), requested=False)
        == requested_number
    ]
    text_contains = [
        room
        for room in rooms
        if requested_casefold in str(room.get("roomName") or "").casefold()
        or (
            requested_norm
            and requested_norm
            in _normalize_room_name(str(room.get("roomName") or ""))
        )
    ]

    strategy = "not_found"
    candidates: list[dict] = []
    for strategy_name, matches in (
        ("exact_name", exact_name),
        ("normalized_name", normalized_name),
        ("numeric_alias", numeric_alias),
        ("text_contains", text_contains),
    ):
        if matches:
            strategy = strategy_name
            candidates = matches
            break
    return {
        "requested_name": requested,
        "status": (
            "not_found"
            if not candidates
            else "unique"
            if len(candidates) == 1
            else "multiple"
        ),
        "strategy": strategy,
        "candidate_count": len(candidates),
        "rooms": candidates,
    }


def response_json(
    response: dict,
    *,
    context: str,
    allow_empty_success: bool = False,
) -> Any:
    status = int(response.get("status") or 0)
    final_url = str(response.get("url") or "")
    response_text = str(response.get("text") or "")
    login_page = any(
        (
            "login" in urlparse(final_url).path.lower(),
            "method=login" in final_url.lower(),
            'type="password"' in response_text.lower(),
            "type='password'" in response_text.lower(),
        )
    )
    if status in {301, 302, 303, 307, 308, 401, 403} or login_page:
        from agentbridge.adapters.seeyon_central import SeeyonLoginRequired

        raise SeeyonLoginRequired(f"The central OA session expired during {context}.")
    if status < 200 or status >= 300:
        raise MeetingRoomContractMismatch(f"OA {context} returned HTTP {status}.")
    data = response.get("json")
    if data is None:
        if allow_empty_success and response_text.strip() in {"", "null"}:
            return {}
        raise MeetingRoomContractMismatch(f"OA {context} did not return JSON.")
    return data


def _normalize_availability_arguments(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("meeting-room availability input must be an object")
    start_raw = arguments.get("start_time")
    end_raw = arguments.get("end_time")
    if (start_raw is None) != (end_raw is None):
        raise ValueError("start_time and end_time must be supplied together")
    start = _parse_datetime(start_raw, "start_time") if start_raw is not None else None
    end = _parse_datetime(end_raw, "end_time") if end_raw is not None else None
    if start is not None and end is not None:
        if end <= start:
            raise ValueError("end_time must be later than start_time")
        if end - start > timedelta(days=31):
            raise ValueError("meeting-room availability range must not exceed 31 days")
    only_available = _as_bool(arguments.get("only_available"))
    if only_available and start is None:
        raise ValueError("only_available requires start_time and end_time")
    minimum_capacity = _validated_int(
        arguments.get("minimum_capacity"),
        "minimum_capacity",
        default=0,
        minimum=0,
        maximum=100000,
    )
    return {
        "start_time": start.strftime("%Y-%m-%d %H:%M") if start else "",
        "end_time": end.strftime("%Y-%m-%d %H:%M") if end else "",
        "room_name": _optional_text(arguments.get("room_name"), "room_name", 100),
        "minimum_capacity": minimum_capacity,
        "only_available": only_available,
        "limit": _validated_int(arguments.get("limit"), "limit", default=100, minimum=1, maximum=200),
    }


def _normalize_my_applications_arguments(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("meeting-room application input must be an object")
    start = _optional_date(arguments.get("application_start_date"), "application_start_date")
    end = _optional_date(arguments.get("application_end_date"), "application_end_date")
    if start and end and start > end:
        raise ValueError("application_start_date cannot be after application_end_date")
    usage_start = _optional_date(arguments.get("usage_start_date"), "usage_start_date")
    usage_end = _optional_date(arguments.get("usage_end_date"), "usage_end_date")
    if usage_start and usage_end and usage_start > usage_end:
        raise ValueError("usage_start_date cannot be after usage_end_date")
    audit_status = _optional_text(arguments.get("audit_status"), "audit_status", 20)
    if audit_status and audit_status not in _AUDIT_STATUS_TO_CODE:
        raise ValueError("audit_status must be pending, approved, or rejected")
    return {
        "room_name": _optional_text(arguments.get("room_name"), "room_name", 100),
        "application_start_date": start.isoformat() if start else "",
        "application_end_date": end.isoformat() if end else "",
        "usage_start_date": usage_start.isoformat() if usage_start else "",
        "usage_end_date": usage_end.isoformat() if usage_end else "",
        "audit_status": audit_status,
        "limit": _validated_int(arguments.get("limit"), "limit", default=50, minimum=1, maximum=500),
    }


def _public_busy_intervals(
    snapshot: dict,
    *,
    room_id: str,
    start_ms: int | None,
    end_ms: int | None,
) -> list[dict]:
    if start_ms is None or end_ms is None:
        return []
    apps = snapshot.get("roomAppsInfo") if isinstance(snapshot, dict) else []
    public = []
    for app in apps if isinstance(apps, list) else []:
        if not isinstance(app, dict) or str(app.get("roomId") or "") != room_id:
            continue
        app_start = _safe_int(app.get("appBeginDate"))
        app_end = _safe_int(app.get("appEndDate"))
        if app_start is None or app_end is None:
            continue
        if start_ms is not None and end_ms is not None and not (
            app_start < end_ms and app_end > start_ms
        ):
            continue
        status_code = str(app.get("status") if app.get("status") is not None else app.get("appStatus") or "")
        status_label = str(
            app.get("statusName") or app.get("appStatusName") or "已占用"
        )
        public.append(
            {
                "start_time": _format_datetime(app_start),
                "end_time": _format_datetime(app_end),
                "status_code": status_code or None,
                "status_label": status_label,
                "booked_by_name": str(app.get("perName") or "").strip() or None,
                "booked_by_department": str(
                    app.get("perDeptName") or ""
                ).strip()
                or None,
            }
        )
    public.sort(key=lambda item: (item["start_time"] or "", item["end_time"] or ""))
    return public


def _public_application(item: dict) -> dict:
    status_code = str(item.get("appStatus") if item.get("appStatus") is not None else "")
    usage_status = item.get("usedStatusDisplay")
    if usage_status is None:
        usage_status = item.get("usedStatus")
    return {
        "application_id": str(item.get("roomAppId") or item.get("appId") or ""),
        "room_id": str(item.get("roomId") or ""),
        "room_name": str(item.get("roomName") or "").strip(),
        "room_capacity": _safe_int(item.get("roomSeatCount")),
        "administrator_names": str(item.get("adminNames") or "").strip(),
        "meeting_id": str(item.get("meetingId") or ""),
        "meeting_name": str(item.get("meetingName") or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "applied_at": _format_datetime(item.get("appDatetime")),
        "start_time": _format_datetime(item.get("startDatetime")),
        "end_time": _format_datetime(item.get("endDatetime")),
        "audit_status_code": status_code or None,
        "audit_status": _audit_status_name(status_code),
        "audit_status_label": str(item.get("appStatusName") or _AUDIT_CODE_TO_LABEL.get(status_code) or "").strip(),
        "usage_status_code": str(usage_status if usage_status is not None else "") or None,
        "usage_status_label": str(item.get("usedStatusName") or "").strip(),
    }


def _application_matches(item: dict, query: dict) -> bool:
    if query["room_name"] and not _meeting_room_name_matches(
        query["room_name"], item["room_name"]
    ):
        return False
    if query["audit_status"] and item["audit_status"] != query["audit_status"]:
        return False
    applied_date = str(item.get("applied_at") or "")[:10]
    if query["application_start_date"] and (
        not applied_date or applied_date < query["application_start_date"]
    ):
        return False
    if query["application_end_date"] and (
        not applied_date or applied_date > query["application_end_date"]
    ):
        return False
    usage_start_date = str(item.get("start_time") or "")[:10]
    usage_end_date = str(item.get("end_time") or "")[:10]
    if query["usage_start_date"] and (
        not usage_end_date or usage_end_date < query["usage_start_date"]
    ):
        return False
    if query["usage_end_date"] and (
        not usage_start_date or usage_start_date > query["usage_end_date"]
    ):
        return False
    return True


def _meeting_room_name_matches(requested: str, actual: str) -> bool:
    requested_casefold = str(requested or "").strip().casefold()
    actual_casefold = str(actual or "").strip().casefold()
    if not requested_casefold or not actual_casefold:
        return False
    if requested_casefold == actual_casefold:
        return True
    requested_norm = _normalize_room_name(requested_casefold)
    actual_norm = _normalize_room_name(actual_casefold)
    if requested_norm and requested_norm == actual_norm:
        return True
    requested_number = _room_number(requested_casefold, requested=True)
    actual_number = _room_number(actual_casefold, requested=False)
    if requested_number and requested_number == actual_number:
        return True
    return requested_casefold in actual_casefold or (
        bool(requested_norm) and requested_norm in actual_norm
    )


def _audit_status_name(code: str) -> str | None:
    return {"0": "pending", "1": "approved", "2": "rejected"}.get(code)


def _format_datetime(value: Any) -> str | None:
    numeric = _safe_int(value)
    if numeric is not None and numeric > 10_000_000_000:
        return datetime.fromtimestamp(numeric / 1000, tz=_SHANGHAI_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for pattern in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                return datetime.strptime(text[:19], pattern).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                continue
        return text[:32]
    return None


def _parse_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must use YYYY-MM-DD HH:MM")
    try:
        return datetime.strptime(value.strip().replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD HH:MM") from exc


def _datetime_ms(value: str) -> int:
    parsed = _parse_datetime(value, "datetime")
    return int(parsed.replace(tzinfo=_SHANGHAI_TIMEZONE).timestamp() * 1000)


def _optional_date(value: Any, name: str) -> date | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD") from exc


def _optional_text(value: Any, name: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = re.sub(r"[\r\n\t]+", " ", value).strip()
    if len(text) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    return text


def _validated_int(value: Any, name: str, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, "", 0, "0", "false", "False", "N", "n"):
        return False
    if value in (1, "1", "true", "True", "Y", "y"):
        return True
    return bool(value)


def _public_approval_requirement(value: Any) -> dict:
    code = str(value).strip() if value is not None else ""
    if code not in _ROOM_APPROVAL_KNOWN_CODES:
        return {
            "requires_approval": None,
            "approval_requirement_code": code or None,
            "approval_requirement_label": "未知",
        }
    requires_approval = code == _ROOM_APPROVAL_REQUIRED_CODE
    return {
        "requires_approval": requires_approval,
        "approval_requirement_code": code,
        "approval_requirement_label": "需审批" if requires_approval else "无需审批",
    }


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100}


def _normalize_room_name(value: str) -> str:
    text = _replace_chinese_numerals(str(value or "").lower())
    text = re.sub(r"\s+", "", text)
    for token in ("会议室", "會議室", "号", "#", "層", "层", "樓", "楼"):
        text = text.replace(token, "")
    return text


def _room_number(value: str, *, requested: bool) -> str:
    text = _replace_chinese_numerals(str(value or ""))
    if requested:
        match = re.search(r"(\d+)\s*(?:号|#)\s*(?:会议室)?", text)
        if match:
            return match.group(1)
        match = re.search(r"(?:第\s*)?(\d+)\s*会议室", text)
        if match:
            return match.group(1)
        if re.fullmatch(r"\s*\d+\s*", text):
            return text.strip()
    for pattern in (
        r"层\s*(\d+)\s*(?:#|号)\s*会议室",
        r"楼\s*(\d+)\s*(?:#|号)\s*会议室",
        r"(\d+)\s*(?:#|号)\s*会议室",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def _replace_chinese_numerals(value: str) -> str:
    def replace(match: re.Match) -> str:
        return str(_parse_chinese_numeral(match.group(0)))

    return re.sub(r"[零〇一二两三四五六七八九十百]+", replace, value)


def _parse_chinese_numeral(value: str) -> int:
    if not any(character in _CHINESE_UNITS for character in value):
        digits = "".join(str(_CHINESE_DIGITS[character]) for character in value)
        return int(digits)
    total = 0
    current = 0
    for character in value:
        if character in _CHINESE_DIGITS:
            current = _CHINESE_DIGITS[character]
            continue
        unit = _CHINESE_UNITS[character]
        total += (current or 1) * unit
        current = 0
    return total + current
