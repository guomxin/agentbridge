from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Callable

from bscli.core.query_contracts import HISTORY_QUERY_EVIDENCE_SCHEMA
from bscli.core.input_validation import (
    ValidationProfile, matches_json_type, schema_value_issue,
)


class TransformRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


TransformHandler = Callable[[dict[str, Any]], dict[str, Any]]
TransformItemCounter = Callable[[dict[str, Any]], int]


@dataclass(frozen=True)
class TransformSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    maximum_input_items: int
    maximum_output_chars: int
    halts_on_empty: bool = False
    halts_on_incomplete: bool = False
    result_projection: str | None = None
    required_bound_inputs: tuple[str, ...] = ()
    input_item_counter: TransformItemCounter | None = None

    @property
    def version(self) -> str:
        return self.name.rsplit(".v", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "maximumInputItems": self.maximum_input_items,
            "maximumOutputChars": self.maximum_output_chars,
            "haltsOnEmpty": self.halts_on_empty,
            "haltsOnIncomplete": self.halts_on_incomplete,
            "resultProjection": self.result_projection,
            "requiredBoundInputs": list(self.required_bound_inputs),
            "deterministic": True,
            "networkAccess": False,
        }


class TransformRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, TransformSpec] = {}
        self._handlers: dict[str, TransformHandler] = {}

    def register(self, spec: TransformSpec, handler: TransformHandler) -> None:
        if spec.name in self._specs and self._specs[spec.name] != spec:
            raise ValueError(f"transform already registered: {spec.name}")
        if not callable(handler):
            raise TypeError("transform handler must be callable")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def get(self, name: str) -> TransformSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"unknown transform: {name}") from exc

    def list(self) -> list[TransformSpec]:
        return [self._specs[name] for name in sorted(self._specs)]

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self.get(name)
        if not isinstance(arguments, dict):
            raise TransformRejected(
                "TRANSFORM_INPUT_INVALID",
                "transform input must be an object",
            )
        _validate_schema_value(
            arguments,
            spec.input_schema,
            path="input",
            error_code="TRANSFORM_INPUT_INVALID",
        )
        input_count = (
            spec.input_item_counter(arguments)
            if spec.input_item_counter is not None
            else _count_input_items(arguments)
        )
        if input_count > spec.maximum_input_items:
            raise TransformRejected(
                "TRANSFORM_INPUT_TOO_LARGE",
                f"transform accepts at most {spec.maximum_input_items} items",
            )
        result = self._handlers[name](arguments)
        if not isinstance(result, dict):
            raise TransformRejected(
                "TRANSFORM_OUTPUT_INVALID",
                "transform output must be an object",
            )
        _validate_schema_value(
            result,
            spec.output_schema,
            path="output",
            error_code="TRANSFORM_OUTPUT_INVALID",
        )
        draft = result.get("draft")
        if isinstance(draft, str) and len(draft) > spec.maximum_output_chars:
            raise TransformRejected(
                "TRANSFORM_OUTPUT_TOO_LARGE",
                f"transform output exceeds {spec.maximum_output_chars} characters",
            )
        return result


WORK_ITEMS_TO_LOG_DRAFT = "work_items_to_log_draft.v1"
MERGE_WORK_ITEMS = "merge_work_items.v1"
WORK_ITEMS_TO_LOG_DRAFT_V2 = "work_items_to_log_draft.v2"

WORK_ITEMS_TO_LOG_DRAFT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "affair_id": {"type": "string"},
                    "title": {"type": "string"},
                    "date": {"type": "string"},
                    "category": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

WORK_ITEMS_TO_LOG_DRAFT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "draft": {"type": "string"},
        "empty": {"type": "boolean"},
        "source_count": {"type": "integer"},
        "included_count": {"type": "integer"},
        "excluded_count": {"type": "integer"},
        "excluded_automatic_count": {"type": "integer"},
        "excluded_duplicate_count": {"type": "integer"},
        "included_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "date": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["title", "date", "category"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "draft",
        "empty",
        "source_count",
        "included_count",
        "excluded_count",
        "excluded_automatic_count",
        "excluded_duplicate_count",
        "included_items",
    ],
    "additionalProperties": False,
}


def build_transform_registry() -> TransformRegistry:
    registry = TransformRegistry()
    registry.register(
        TransformSpec(
            name=WORK_ITEMS_TO_LOG_DRAFT,
            description=(
                "把结构化事项列表去重并转换为可编辑的工作日志草稿；"
                "不推断工时、项目或用户未提供的业务成果。"
            ),
            input_schema=WORK_ITEMS_TO_LOG_DRAFT_INPUT_SCHEMA,
            output_schema=WORK_ITEMS_TO_LOG_DRAFT_OUTPUT_SCHEMA,
            maximum_input_items=100,
            maximum_output_chars=4000,
            halts_on_empty=True,
            required_bound_inputs=("items",),
        ),
        _work_items_to_log_draft,
    )
    registry.register(
        TransformSpec(
            name=MERGE_WORK_ITEMS,
            description=(
                "按来源合同合并多个结构化事项集合，保留已办/已发行为、稳定标识、"
                "查询范围和完整性；不同集合的同一标题不会被误删。"
            ),
            input_schema=_merge_work_items_input_schema(),
            output_schema=_merged_work_items_output_schema(),
            maximum_input_items=200,
            maximum_output_chars=8_000,
            halts_on_empty=True,
            halts_on_incomplete=True,
            result_projection="source_summary",
            required_bound_inputs=("sources",),
            input_item_counter=_count_merged_source_items,
        ),
        _merge_work_items,
    )
    registry.register(
        TransformSpec(
            name=WORK_ITEMS_TO_LOG_DRAFT_V2,
            description=(
                "把带来源质量的事项集合转换为可核对日志草稿；不完整来源会硬停止，"
                "且不推断工时、项目或业务成果。"
            ),
            input_schema=_work_items_to_log_draft_v2_input_schema(),
            output_schema=_work_items_to_log_draft_v2_output_schema(),
            maximum_input_items=100,
            maximum_output_chars=4_000,
            halts_on_empty=True,
            halts_on_incomplete=True,
            result_projection="private_draft",
            required_bound_inputs=("bundle",),
            input_item_counter=lambda arguments: len(
                ((arguments.get("bundle") or {}).get("items") or [])
            ),
        ),
        _work_items_to_log_draft_v2,
    )
    return registry


def _work_items_to_log_draft(arguments: dict[str, Any]) -> dict[str, Any]:
    source_items = arguments.get("items") or []
    included: list[dict[str, str]] = []
    seen: set[str] = set()
    automatic_count = 0
    duplicate_count = 0

    for raw in source_items:
        if not isinstance(raw, dict):
            continue
        title = _clean_text(raw.get("title"), maximum=240)
        if not title:
            continue
        category = _clean_text(raw.get("category"), maximum=120)
        date = _clean_text(raw.get("date"), maximum=40)
        if _is_automatic_item(title=title, category=category):
            automatic_count += 1
            continue
        affair_id = _clean_text(raw.get("affair_id"), maximum=256)
        dedupe_key = affair_id or f"{title.casefold()}\n{date}"
        if dedupe_key in seen:
            duplicate_count += 1
            continue
        seen.add(dedupe_key)
        included.append(
            {
                "title": title,
                "date": date,
                "category": category,
            }
        )

    lines: list[str] = []
    for item in included:
        context = "、".join(
            value for value in (item["category"], item["date"]) if value
        )
        suffix = f"（{context}）" if context else ""
        candidate = f"{len(lines) + 1}. 处理《{item['title']}》{suffix}。"
        projected = "\n".join([*lines, candidate])
        if len(projected) > 4000:
            break
        lines.append(candidate)

    if len(lines) < len(included):
        included = included[: len(lines)]
    excluded_count = max(0, len(source_items) - len(included))
    draft = "\n".join(lines)
    return {
        "draft": draft,
        "empty": not bool(draft),
        "source_count": len(source_items),
        "included_count": len(included),
        "excluded_count": excluded_count,
        "excluded_automatic_count": automatic_count,
        "excluded_duplicate_count": duplicate_count,
        "included_items": included,
    }


def _is_automatic_item(*, title: str, category: str) -> bool:
    text = f"{title} {category}".casefold()
    return any(
        marker in text
        for marker in (
            "自动发起",
            "系统通知",
            "自动触发",
        )
    )


def _clean_text(value: Any, *, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _coverage_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            **HISTORY_QUERY_EVIDENCE_SCHEMA,
            "status": {"type": "string", "enum": ["complete", "partial", "unknown"]},
            "queryApplied": {"type": "boolean"},
            "dateBasis": {"type": "string"},
            "requestedRange": {
                "type": "object",
                "properties": {
                    "start": {"type": ["string", "null"]},
                    "end": {"type": ["string", "null"]},
                },
                "required": ["start", "end"],
            },
            "scannedCount": {"type": "integer"},
            "matchedCount": {"type": "integer"},
            "hasMore": {"type": "boolean"},
            "completionReason": {"type": "string"},
            "observedAt": {"type": "string"},
            "queryHash": {"type": "string"},
        },
        "required": [
            "status",
            "queryApplied",
            "dateBasis",
            "requestedRange",
            "scannedCount",
            "matchedCount",
            "hasMore",
            "completionReason",
            "observedAt",
            "queryHash",
        ],
        "additionalProperties": False,
    }


def _source_result_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "collection": {"type": "string"},
            "items": {"type": "array", "items": {"type": "object"}},
            "coverage": _coverage_schema(),
        },
        "required": ["collection", "items", "coverage"],
    }


def _merged_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "source_collection": {"type": "string"},
            "source_affair_id": {"type": "string"},
            "title": {"type": "string"},
            "date": {"type": "string"},
            "category": {"type": "string"},
            "status": {"type": "string"},
        },
        "required": [
            "source_collection",
            "source_affair_id",
            "title",
            "date",
            "category",
            "status",
        ],
        "additionalProperties": False,
    }


def _source_summary_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "coverage": _coverage_schema(),
            "collection": {"type": "string"},
            "status": {"type": "string"},
            "scanned_count": {"type": "integer"},
            "matched_count": {"type": "integer"},
            "included_count": {"type": "integer"},
            "query_hash": {"type": "string"},
        },
        "required": [
            "collection",
            "status",
            "scanned_count",
            "matched_count",
            "included_count",
            "query_hash",
        ],
        "additionalProperties": False,
    }


def _merge_work_items_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "sources": {"type": "array", "items": _source_result_schema()},
        },
        "required": ["sources"],
        "additionalProperties": False,
    }


def _merged_work_items_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": _merged_item_schema()},
            "source_summaries": {"type": "array", "items": _source_summary_schema()},
            "coverage": _coverage_schema(),
            "source_incomplete": {"type": "boolean"},
            "empty": {"type": "boolean"},
            "source_count": {"type": "integer"},
            "item_count": {"type": "integer"},
            "duplicate_count": {"type": "integer"},
        },
        "required": [
            "items",
            "source_summaries",
            "coverage",
            "empty",
            "source_count",
            "item_count",
            "duplicate_count",
        ],
        "additionalProperties": False,
    }


def _work_items_to_log_draft_v2_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"bundle": _merged_work_items_output_schema()},
        "required": ["bundle"],
        "additionalProperties": False,
    }


def _work_items_to_log_draft_v2_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "draft": {"type": "string"},
            "empty": {"type": "boolean"},
            "source_incomplete": {"type": "boolean"},
            "source_count": {"type": "integer"},
            "included_count": {"type": "integer"},
            "excluded_count": {"type": "integer"},
            "excluded_automatic_count": {"type": "integer"},
            "excluded_duplicate_count": {"type": "integer"},
            "source_summaries": {"type": "array", "items": _source_summary_schema()},
            "coverage": _coverage_schema(),
            "included_items": {"type": "array", "items": _merged_item_schema()},
        },
        "required": [
            "draft",
            "empty",
            "source_incomplete",
            "source_count",
            "included_count",
            "excluded_count",
            "excluded_automatic_count",
            "excluded_duplicate_count",
            "source_summaries",
            "coverage",
            "included_items",
        ],
        "additionalProperties": False,
    }


def _merge_work_items(arguments: dict[str, Any]) -> dict[str, Any]:
    sources = arguments["sources"]
    merged: list[dict[str, str]] = []
    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_count = 0
    statuses: list[str] = []
    total_scanned = 0
    total_matched = 0
    query_hashes: list[str] = []
    requested_ranges: list[dict[str, Any]] = []
    date_bases: list[str] = []

    for source in sources:
        collection = _clean_text(source.get("collection"), maximum=80)
        coverage = source["coverage"]
        status = str(coverage["status"])
        statuses.append(status)
        scanned = int(coverage["scannedCount"])
        matched = int(coverage["matchedCount"])
        total_scanned += scanned
        total_matched += matched
        query_hashes.append(str(coverage["queryHash"]))
        requested_ranges.append(dict(coverage["requestedRange"]))
        date_bases.append(str(coverage["dateBasis"]))
        before = len(merged)
        for raw in source.get("items") or []:
            affair_id = _clean_text(raw.get("affair_id"), maximum=256)
            title = _clean_text(raw.get("title"), maximum=240)
            if not title:
                continue
            stable_key = f"{collection}:{affair_id}" if affair_id else ""
            if stable_key and stable_key in seen:
                duplicate_count += 1
                continue
            if stable_key:
                seen.add(stable_key)
            merged.append(
                {
                    "source_collection": collection,
                    "source_affair_id": affair_id,
                    "title": title,
                    "date": _clean_text(raw.get("date"), maximum=40),
                    "category": _clean_text(raw.get("category"), maximum=120),
                    "status": _clean_text(raw.get("status"), maximum=120),
                }
            )
        summaries.append(
            {
                "collection": collection,
                "status": status,
                "scanned_count": scanned,
                "matched_count": matched,
                "included_count": len(merged) - before,
                "query_hash": str(coverage["queryHash"]),
                "coverage": dict(coverage),
            }
        )

    aggregate_status = (
        "complete"
        if statuses and all(value == "complete" for value in statuses)
        else "partial"
        if any(value == "partial" for value in statuses)
        else "unknown"
    )
    same_range = requested_ranges[0] if requested_ranges and all(
        value == requested_ranges[0] for value in requested_ranges
    ) else {"start": None, "end": None}
    coverage = {
        "status": aggregate_status,
        "queryApplied": bool(statuses) and all(
            bool(source["coverage"]["queryApplied"]) for source in sources
        ),
        "dateBasis": "+".join(date_bases),
        "requestedRange": same_range,
        "scannedCount": total_scanned,
        "matchedCount": total_matched,
        "hasMore": any(bool(source["coverage"]["hasMore"]) for source in sources),
        "completionReason": (
            "all_sources_complete" if aggregate_status == "complete" else "source_incomplete"
        ),
        "observedAt": max(str(source["coverage"]["observedAt"]) for source in sources),
        "queryHash": "sha256:"
        + hashlib.sha256("\n".join(query_hashes).encode("utf-8")).hexdigest(),
    }
    return {
        "items": merged,
        "source_summaries": summaries,
        "coverage": coverage,
        "source_incomplete": aggregate_status != "complete",
        "empty": not bool(merged),
        "source_count": len(sources),
        "item_count": len(merged),
        "duplicate_count": duplicate_count,
    }


def _work_items_to_log_draft_v2(arguments: dict[str, Any]) -> dict[str, Any]:
    bundle = arguments["bundle"]
    source_items = bundle["items"]
    included: list[dict[str, str]] = []
    seen: set[str] = set()
    automatic_count = 0
    duplicate_count = 0
    lines: list[str] = []
    for item in source_items:
        title = _clean_text(item.get("title"), maximum=240)
        category = _clean_text(item.get("category"), maximum=120)
        if _is_automatic_item(title=title, category=category):
            automatic_count += 1
            continue
        stable_key = (
            f"{item.get('source_collection')}:{item.get('source_affair_id')}"
            if item.get("source_affair_id")
            else f"{item.get('source_collection')}:{title}:{item.get('date')}"
        )
        if stable_key in seen:
            duplicate_count += 1
            continue
        seen.add(stable_key)
        normalized = {key: str(item.get(key) or "") for key in (
            "source_collection", "source_affair_id", "title", "date", "category", "status"
        )}
        action = {"done": "处理", "sent": "发起"}.get(
            normalized["source_collection"], "办理"
        )
        context = "、".join(
            value for value in (normalized["category"], normalized["date"]) if value
        )
        suffix = f"（{context}）" if context else ""
        candidate = f"{len(lines) + 1}. {action}《{title}》{suffix}。"
        if len("\n".join([*lines, candidate])) > 4_000:
            break
        lines.append(candidate)
        included.append(normalized)
    draft = "\n".join(lines)
    return {
        "draft": draft,
        "empty": not bool(draft),
        "source_incomplete": bundle["coverage"]["status"] != "complete",
        "source_count": len(source_items),
        "included_count": len(included),
        "excluded_count": max(0, len(source_items) - len(included)),
        "excluded_automatic_count": automatic_count,
        "excluded_duplicate_count": duplicate_count,
        "source_summaries": bundle["source_summaries"],
        "coverage": bundle["coverage"],
        "included_items": included,
    }


def _count_input_items(value: Any) -> int:
    if isinstance(value, list):
        return len(value) + sum(_count_input_items(item) for item in value)
    if isinstance(value, dict):
        return sum(_count_input_items(item) for item in value.values())
    return 0


def _count_merged_source_items(arguments: dict[str, Any]) -> int:
    return sum(
        len(source.get("items") or [])
        for source in arguments.get("sources") or []
        if isinstance(source, dict)
    )


def _validate_schema_value(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str,
    error_code: str,
) -> None:
    issue = schema_value_issue(value, schema, profile=ValidationProfile.TRANSFORM, path=path)
    if issue is None:
        return
    messages = {
        "type": f"{issue.path} must be {issue.expected}",
        "enum": f"{issue.path} is outside the allowed values",
        "missing": f"{issue.path} is missing required fields: {', '.join(issue.fields)}",
        "unexpected": f"{issue.path} has unexpected fields: {', '.join(issue.fields)}",
    }
    raise TransformRejected(error_code, messages[issue.kind])


def _matches_schema_type(value: Any, expected: Any) -> bool:
    return matches_json_type(value, expected, profile=ValidationProfile.TRANSFORM)
