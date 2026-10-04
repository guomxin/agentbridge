"""Shared validation primitives with explicit, existing entrypoint semantics.

These profiles preserve the supported schema subsets; they are not a complete
JSON Schema implementation. Callers own root guards, errors and execution timing.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Literal


class ValidationProfile(Enum):
    CAPABILITY = "capability_compat"
    PLAN = "plan_compat"
    TRANSFORM = "transform_compat"


@dataclass(frozen=True)
class SchemaIssue:
    kind: Literal["missing", "unexpected", "overlap", "type", "enum",
                  "min_length", "max_length", "minimum", "maximum"]
    path: str = ""
    expected: Any = None
    fields: tuple[str, ...] = ()


def matches_json_type(
    value: Any, expected: Any, *, profile: ValidationProfile
) -> bool:
    if isinstance(expected, list):
        candidates = expected
        if profile is ValidationProfile.CAPABILITY:
            candidates = [item for item in expected if isinstance(item, str)]
        return any(matches_json_type(value, item, profile=profile) for item in candidates)
    mapping = {
        "string": str, "object": dict, "array": list, "boolean": bool,
        "integer": int, "number": (int, float), "null": type(None),
    }
    target = mapping.get(expected)
    if target is None:
        return True
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, target)


def object_fields_issue(
    value: dict, schema: dict, *, profile: ValidationProfile,
    path: str = "", bound_fields: Iterable[str] = (),
) -> SchemaIssue | None:
    """Keep both the original error precedence and required-field ordering."""
    properties = schema.get("properties") or {}
    bound = set(bound_fields)
    if profile is ValidationProfile.PLAN:
        if schema.get("additionalProperties") is False:
            unexpected = sorted((set(value) | bound) - set(properties))
            if unexpected:
                return SchemaIssue("unexpected", path, fields=tuple(unexpected))
        missing = sorted(set(schema.get("required") or []) - set(value) - bound)
    else:
        missing = [name for name in schema.get("required") or [] if name not in value]
    if missing:
        return SchemaIssue("missing", path, fields=tuple(missing))
    if profile is ValidationProfile.PLAN:
        overlap = sorted(set(value) & bound)
        if overlap:
            return SchemaIssue("overlap", path, fields=tuple(overlap))
    elif schema.get("additionalProperties") is False:
        unexpected = sorted(set(value) - set(properties))
        if unexpected:
            return SchemaIssue("unexpected", path, fields=tuple(unexpected))
    return None


def object_input_issue(
    value: dict, schema: dict, *, profile: ValidationProfile,
    bound_fields: Iterable[str] = (),
) -> SchemaIssue | None:
    """Check the object envelope and its declared fields without normalizing."""
    issue = object_fields_issue(value, schema, profile=profile, bound_fields=bound_fields)
    if issue is not None:
        return issue
    properties = schema.get("properties") or {}
    for name, item in value.items():
        definition = properties.get(name)
        if isinstance(definition, dict):
            issue = schema_value_issue(item, definition, profile=profile, path=name)
            if issue is not None:
                return issue
    return None


def schema_value_issue(
    value: Any, schema: dict, *, profile: ValidationProfile, path: str,
) -> SchemaIssue | None:
    expected = schema.get("type")
    if expected is not None and not matches_json_type(value, expected, profile=profile):
        return SchemaIssue("type", path, expected=expected)
    if profile is ValidationProfile.CAPABILITY:
        return None
    if "enum" in schema and value not in schema["enum"]:
        return SchemaIssue("enum", path)
    if profile is ValidationProfile.PLAN:
        if isinstance(value, str):
            if len(value) < int(schema.get("minLength", 0)):
                return SchemaIssue("min_length", path)
            maximum = schema.get("maxLength")
            if maximum is not None and len(value) > int(maximum):
                return SchemaIssue("max_length", path)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                return SchemaIssue("minimum", path)
            if "maximum" in schema and value > schema["maximum"]:
                return SchemaIssue("maximum", path)
        return None
    if isinstance(value, dict):
        issue = object_fields_issue(value, schema, profile=profile, path=path)
        if issue is not None:
            return issue
        properties = schema.get("properties") or {}
        for name, item in value.items():
            definition = properties.get(name)
            if isinstance(definition, dict):
                issue = schema_value_issue(
                    item, definition, profile=profile, path=f"{path}.{name}"
                )
                if issue is not None:
                    return issue
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            issue = schema_value_issue(
                item, schema["items"], profile=profile, path=f"{path}[{index}]"
            )
            if issue is not None:
                return issue
    return None
