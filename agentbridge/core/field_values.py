"""Trusted field-schema validation shared by legacy cards and draft pages."""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any

def _field_definitions(schema: Any) -> list[dict[str, Any]]:
    if not isinstance(schema, dict):
        raise ValueError("field schema must be an object")
    fields = schema.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("field schema must define fields")
    definitions: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in fields:
        if not isinstance(item, dict):
            raise ValueError("field definition must be an object")
        name = str(item.get("name") or "")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or name in names:
            raise ValueError("field name is invalid or duplicated")
        if item.get("control") not in {
            "text",
            "textarea",
            "datetime-local",
            "date",
            "select",
            "segmented",
            "number",
        }:
            raise ValueError("field control is unsupported")
        names.add(name)
        definitions.append(item)
    return definitions


def _normalize_submission(schema: dict[str, Any], raw_values: dict[str, str]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    definitions = _field_definitions(schema)
    for item in definitions:
        name = item["name"]
        label = str(item.get("label") or name)
        raw = raw_values.get(name, "")
        if not isinstance(raw, str):
            raise ValueError(f"{label}格式无效。")
        control = item["control"]
        required = bool(item.get("required"))
        if control in {"text", "textarea"}:
            value = raw.strip()
            if required and not value:
                raise ValueError(f"请填写{label}。")
            maximum = int(item.get("max_length") or 0)
            if maximum and len(value) > maximum:
                raise ValueError(f"{label}不能超过 {maximum} 个字符。")
            if value or required:
                normalized[name] = value
            continue
        if control == "date":
            value = raw.strip()
            if not value:
                if required:
                    raise ValueError(f"请选择{label}。")
                continue
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(f"{label}格式无效。") from exc
            normalized[name] = parsed.strftime("%Y-%m-%d")
            continue
        if control == "datetime-local":
            value = raw.strip()
            if not value:
                if required:
                    raise ValueError(f"请选择{label}。")
                continue
            try:
                parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M")
            except ValueError as exc:
                raise ValueError(f"{label}格式无效。") from exc
            normalized[name] = parsed.strftime("%Y-%m-%d %H:%M")
            continue
        if control in {"select", "segmented"}:
            options = item.get("options")
            if not isinstance(options, list) or not options:
                raise ValueError(f"{label}选项无效。")
            allowed = {
                str(option.get("value")): option
                for option in options
                if isinstance(option, dict) and option.get("value") is not None
            }
            value = raw.strip()
            if not value:
                if required:
                    raise ValueError(f"请选择{label}。")
                continue
            if value not in allowed:
                raise ValueError(f"{label}选项无效。")
            if item.get("value_type") == "boolean":
                if value not in {"true", "false"}:
                    raise ValueError(f"{label}选项无效。")
                normalized[name] = value == "true"
            else:
                normalized[name] = value
            continue
        if control == "number":
            value = raw.strip()
            if not value:
                if required:
                    raise ValueError(f"请填写{label}。")
                continue
            if not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
                raise ValueError(f"{label}必须是数字。")
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError(f"{label}必须是数字。") from exc
            minimum = item.get("minimum")
            maximum = item.get("maximum")
            if minimum is not None and number < Decimal(str(minimum)):
                raise ValueError(f"{label}不能小于 {minimum}。")
            if maximum is not None and number > Decimal(str(maximum)):
                raise ValueError(f"{label}不能大于 {maximum}。")
            normalized[name] = int(number) if number == number.to_integral() else float(number)

    for constraint in schema.get("constraints") or []:
        if not isinstance(constraint, dict) or constraint.get("kind") != "datetime_after":
            raise ValueError("字段约束无效。")
        earlier_name = str(constraint.get("earlier") or "")
        later_name = str(constraint.get("later") or "")
        try:
            earlier = datetime.strptime(str(normalized[earlier_name]), "%Y-%m-%d %H:%M")
            later = datetime.strptime(str(normalized[later_name]), "%Y-%m-%d %H:%M")
        except (KeyError, ValueError) as exc:
            raise ValueError(str(constraint.get("message") or "时间范围无效。")) from exc
        minutes = (later - earlier).total_seconds() / 60
        maximum_minutes = constraint.get("maximum_minutes")
        if minutes <= 0 or (
            maximum_minutes is not None and minutes > float(maximum_minutes)
        ):
            raise ValueError(str(constraint.get("message") or "时间范围无效。"))
    return normalized

