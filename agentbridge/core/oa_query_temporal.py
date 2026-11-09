"""Day-precision OA query ranges compiled from a trusted request timestamp.

This module only normalizes user intent. Callers must check the selected source's
date contract before choosing a server filter, and persist the returned context
for continuation rather than recomputing it from the resume time.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


OAQueryTemporalKind = Literal[
    "absolute_range", "between", "since", "until", "all", "previous_day",
    "day_before_yesterday", "previous_calendar_week", "previous_calendar_month",
    "current_calendar_month", "rolling_days",
]
OAQueryDateBasis = Literal["processed_at", "initiated_at"]
TEMPORAL_KINDS = get_args(OAQueryTemporalKind)
DATE_BASES = get_args(OAQueryDateBasis)
MAX_ROLLING_DAYS = 3660
_TEMPORAL_FIELDS = {"kind", "start", "end", "days", "date_basis"}


def oa_query_temporal_schema() -> dict[str, Any]:
    """The public date-only intent schema, shared by OA and plan entrypoints."""
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(TEMPORAL_KINDS)},
            "start": {"type": ["string", "null"], "format": "date", "maxLength": 10},
            "end": {"type": ["string", "null"], "format": "date", "maxLength": 10},
            "days": {"type": ["integer", "null"], "minimum": 1, "maximum": MAX_ROLLING_DAYS},
            "date_basis": {"type": ["string", "null"], "enum": [*DATE_BASES, None]},
        },
        "required": ["kind"],
        "additionalProperties": False,
    }


def validate_oa_query_temporal(value: Any) -> dict[str, Any]:
    """Validate intent without needing an anchor, preserving legacy kind names."""
    if not isinstance(value, dict) or set(value) - _TEMPORAL_FIELDS:
        raise ValueError("temporal 约束不合法。")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in TEMPORAL_KINDS:
        raise ValueError("temporal.kind 不受支持。")
    basis = value.get("date_basis")
    if basis is not None and (not isinstance(basis, str) or basis not in DATE_BASES):
        raise ValueError("temporal.date_basis 必须为 processed_at 或 initiated_at。")
    required = ({"start", "end"} if kind in {"between", "absolute_range"} else
                {"start"} if kind == "since" else {"end"} if kind == "until" else set())
    parsed_dates = {}
    for name in ("start", "end"):
        supplied = value.get(name)
        if name in required:
            if supplied is None:
                raise ValueError(f"{kind} 缺少 {name}。")
            parsed_dates[name] = _date(supplied, name)
        elif supplied is not None:
            raise ValueError(f"{kind} 不能声明 {name}。")
    if len(parsed_dates) == 2 and parsed_dates["start"] > parsed_dates["end"]:
        raise ValueError("绝对日期范围 start 不能晚于 end。")
    days = value.get("days")
    if kind == "rolling_days":
        if type(days) is not int or not 1 <= days <= MAX_ROLLING_DAYS:
            raise ValueError(f"rolling_days.days 必须为 1 至 {MAX_ROLLING_DAYS} 的整数。")
    elif days is not None:
        raise ValueError(f"{kind} 不能声明 days。")
    return {key: deepcopy(item) for key, item in value.items() if item is not None}


def normalize_oa_query_temporal(
    temporal: Any,
    *,
    accepted_at: str | None,
    default_date_basis: str | None = None,
    supported_date_bases: tuple[str, ...] | list[str] | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Resolve a range once; neither model input nor the wall clock supplies now.

    ``since`` and current relative periods end on the request's local day. This
    is not an exact-second snapshot. ``all`` has no fabricated date boundaries.
    A source with no verified requested date basis must reject before reading.
    """
    constraint = validate_oa_query_temporal(temporal)
    basis = constraint.get("date_basis", default_date_basis)
    if basis is not None and basis not in DATE_BASES:
        raise ValueError("来源日期口径不受支持。")
    if supported_date_bases is not None and basis is not None and basis not in supported_date_bases:
        raise ValueError(f"来源不支持日期口径 {basis}；不能替换为其他日期筛选。")
    anchor, local_day = _request_day(accepted_at, timezone_name)
    kind = constraint["kind"]
    start = end = None
    if kind in {"absolute_range", "between"}:
        start, end = _date(constraint["start"], "start"), _date(constraint["end"], "end")
    elif kind == "since":
        start, end = _date(constraint["start"], "start"), local_day
    elif kind == "until":
        end = _date(constraint["end"], "end")
    elif kind == "previous_day":
        start = end = local_day - timedelta(days=1)
    elif kind == "day_before_yesterday":
        start = end = local_day - timedelta(days=2)
    elif kind == "previous_calendar_week":
        monday = local_day - timedelta(days=local_day.weekday())
        start, end = monday - timedelta(days=7), monday - timedelta(days=1)
    elif kind == "previous_calendar_month":
        end = local_day.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif kind == "current_calendar_month":
        start, end = local_day.replace(day=1), local_day
    elif kind == "rolling_days":
        start, end = local_day - timedelta(days=constraint["days"] - 1), local_day
    if start is not None and end is not None and start > end:
        raise ValueError("日期范围 start 不能晚于请求日或 end。")
    return {
        "constraint": constraint,
        "acceptedAt": anchor.isoformat(),
        "timeZone": timezone_name,
        "precision": "day",
        "bounds": "inclusive",
        "absoluteRange": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
        "dateBasis": basis,
    }


def _date(value: Any, name: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(f"temporal.{name} 必须是 YYYY-MM-DD 日期，不支持时分秒。")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"temporal.{name} 必须是有效的 YYYY-MM-DD 日期。") from exc


def _request_day(accepted_at: str | None, timezone_name: str) -> tuple[datetime, date]:
    if not isinstance(accepted_at, str) or not accepted_at:
        raise ValueError("查询缺少可信的原请求受理时间。")
    try:
        anchor = datetime.fromisoformat(accepted_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("原请求受理时间无效。") from exc
    if anchor.tzinfo is None:
        raise ValueError("原请求受理时间必须包含时区。")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        if timezone_name != "Asia/Shanghai":
            raise ValueError("当前身份缺少可用时区。") from exc
        zone = timezone(timedelta(hours=8), name=timezone_name)
    return anchor, anchor.astimezone(zone).date()
