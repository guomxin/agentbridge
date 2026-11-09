from __future__ import annotations

from collections.abc import Callable, Iterable
import base64
import binascii
from copy import deepcopy
import hashlib
import json
from typing import Any

from agentbridge.core.capability import CapabilityRegistry
from agentbridge.core.planning_policy import (
    COMPOSED_TASK_POLICY_VERSION,
    planning_descriptor,
)
from agentbridge.core.task_plan_validation import task_plan_step_json_schema
from agentbridge.core.transforms import TransformRegistry


ScopeResolver = Callable[[str], frozenset[str]]
PLANNING_DISCOVERY_SCHEMA = "agentbridge.planning-catalog.v3"
# The native host caps a model-visible tool result at 16000 UTF-16 units.
# Measure the complete, pretty-printed response and leave room for its envelope.
PLANNING_DISCOVERY_MAX_CHARS = 12000
PLANNING_DISCOVERY_PAGE_SIZE = 25
_DETAIL_SECTIONS = ("input", "output", "planning")


def planning_catalog_response_chars(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    return len(text.encode("utf-16-le")) // 2


def filter_planning_examples(catalog: dict[str, Any]) -> None:
    """Apply the final user grant to examples as well as capability entries."""
    capabilities = {item["name"] for item in catalog["capabilities"]}
    transforms = {item["name"] for item in catalog["transforms"]}
    guide = catalog["prepareInputGuide"]
    guide["examples"] = [
        example for example in guide["examples"]
        if all(
            (step.get("kind") == "capability" and step.get("capabilityName") in capabilities)
            or (step.get("kind") == "transform" and step.get("transformName") in transforms)
            for step in example["steps"]
        )
    ]


def _catalog_revision(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _discovery_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"schemaVersion": PLANNING_DISCOVERY_SCHEMA, "status": "rejected",
            "error": {"code": code, "message": message}, **extra}


def _summary_entry(kind: str, entry: dict[str, Any]) -> dict[str, Any]:
    fields = ("name", "version", "system", "effect", "planningRoles",
              "mayRequireTrustedInteraction", "resultProjection", "requiredBoundInputs",
              "maximumInputItems", "maximumOutputChars", "haltsOnEmpty", "haltsOnIncomplete",
              "readOnlyTerminal", "allowedSourceCapabilities")
    summary = {"kind": kind, **{key: entry[key] for key in fields if key in entry}}
    description = str(entry.get("description") or "")
    summary["description"] = description[:160]
    summary["descriptionComplete"] = len(description) <= 160
    return summary


def _summary_cursor(*, revision: str, system: str | None, kind: str | None, after: str) -> str:
    value = {"revision": revision, "system": system, "kind": kind, "after": after}
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def discover_planning_catalog(
    catalog: dict[str, Any], *, view: str = "summary", kind: str | None = None,
    name: str | None = None, section: str = "all", system: str | None = None,
    cursor: str | None = None, expected_revision: str | None = None,
) -> dict[str, Any]:
    """Bounded public projection; callers must supply the currently authorized catalog.

    Cursors locate public metadata only and never grant access. Every request
    rebuilds the authorized catalog before validating its revision and filters.
    Full internal contracts remain available to skill authoring and validation.
    """
    invalid = _discovery_error("PLANNING_CATALOG_INVALID_QUERY", "目录查询参数不合法。")
    if view not in {"summary", "detail"} or kind not in {None, "capability", "transform"}:
        return invalid
    if section not in {"all", *_DETAIL_SECTIONS}:
        return invalid
    for value, maximum in ((name, 160), (system, 64), (cursor, 2048), (expected_revision, 71)):
        if value is not None and (not isinstance(value, str) or not value or len(value) > maximum):
            return invalid
    if view == "summary" and (name is not None or section != "all"):
        return invalid
    if view == "detail" and (kind is None or name is None or cursor is not None or system is not None):
        return invalid
    revision = _catalog_revision(catalog)
    if expected_revision is not None and expected_revision != revision:
        return _discovery_error("PLANNING_CATALOG_CHANGED", "目录或当前授权已变化，请重新发现后读取合同。",
                                nextAction={"tool": "agentbridge_task_plan_catalog", "arguments": {}})
    common = {"schemaVersion": PLANNING_DISCOVERY_SCHEMA, "view": view,
              "catalogRevision": revision, "policyVersion": catalog["policyVersion"],
              "supportedProposalVersions": catalog["supportedProposalVersions"],
              "limits": catalog["limits"], "availableSections": list(_DETAIL_SECTIONS)}
    if view == "detail":
        entries = catalog["capabilities" if kind == "capability" else "transforms"]
        entry = next((item for item in entries if item["name"] == name), None)
        if entry is None:
            return _discovery_error("PLANNING_CATALOG_ENTRY_UNAVAILABLE", "当前目录没有可用的该项合同。")
        if section == "all":
            contract = deepcopy(entry)
        elif section in {"input", "output"}:
            field = section + "Schema"
            contract = {"name": name, "version": entry["version"], field: deepcopy(entry[field])}
        else:
            contract = deepcopy({key: value for key, value in entry.items()
                                 if key not in {"inputSchema", "outputSchema"}})
        result = {**common, "kind": kind, "name": name, "section": section,
                  "contractRevision": _catalog_revision(entry),
                  "completeSections": list(_DETAIL_SECTIONS) if section == "all" else [section],
                  "contract": contract}
        if planning_catalog_response_chars(result) > PLANNING_DISCOVERY_MAX_CHARS:
            if section == "all":
                return _discovery_error(
                    "PLANNING_CATALOG_SECTION_REQUIRED", "完整合同超出单次预算，请分别读取 input、output、planning。",
                    catalogRevision=revision, availableSections=list(_DETAIL_SECTIONS),
                    nextAction={"tool": "agentbridge_task_plan_catalog", "arguments": {
                        "view": "detail", "kind": kind, "name": name, "section": "input",
                        "expected_revision": revision}})
            return _discovery_error("PLANNING_CATALOG_SECTION_TOO_LARGE", "该完整合同分节超出读取预算，不能使用截断合同生成计划。")
        return result

    entries = [(entry_kind, item) for entry_kind, key in (("capability", "capabilities"), ("transform", "transforms"))
               for item in catalog[key] if (kind is None or kind == entry_kind)
               and (system is None or entry_kind == "transform" or item["system"] == system)]
    entries.sort(key=lambda pair: (pair[0], pair[1]["name"]))
    keys = [entry_kind + ":" + item["name"] for entry_kind, item in entries]
    start = 0
    if cursor is not None:
        try:
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            position = json.loads(raw)
            if not isinstance(position, dict) or set(position) != {"revision", "system", "kind", "after"}:
                return invalid
            if position["system"] != system or position["kind"] != kind:
                return invalid
            if position["revision"] != revision:
                return _discovery_error("PLANNING_CATALOG_CHANGED", "目录或当前授权已变化，请从首批重新发现。",
                                        nextAction={"tool": "agentbridge_task_plan_catalog", "arguments": {}})
            start = keys.index(position["after"]) + 1
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            return invalid
    result = {**common, "filters": {"system": system, "kind": kind},
              "total": len(entries), "startOffset": start, "returnedCount": 0,
              "entries": [], "hasMore": False, "nextCursor": None,
              "prepareInputGuide": {"requiredFields": catalog["prepareInputGuide"]["requiredFields"],
                                    "rules": catalog["prepareInputGuide"]["rules"]},
              "instructions": "摘要不含完整参数。选定名称后用 view=detail、kind、name 读取合同；大合同按 input/output/planning 分节，expected_revision 使用 catalogRevision。按 nextCursor 保持相同 system/kind 续取目录。system 仅筛选能力，通用转换仍保留。"}
    for index in range(start, len(entries)):
        entry_kind, entry = entries[index]
        result["entries"].append(_summary_entry(entry_kind, entry))
        result.update(returnedCount=len(result["entries"]), hasMore=index + 1 < len(entries),
                      nextCursor=_summary_cursor(revision=revision, system=system, kind=kind, after=keys[index])
                      if index + 1 < len(entries) else None)
        if (len(result["entries"]) > PLANNING_DISCOVERY_PAGE_SIZE
                or planning_catalog_response_chars(result) > PLANNING_DISCOVERY_MAX_CHARS):
            result["entries"].pop()
            if not result["entries"]:
                return _discovery_error("PLANNING_CATALOG_ENTRY_TOO_LARGE", "目录摘要超出单次读取预算。")
            result.update(returnedCount=len(result["entries"]), hasMore=True,
                          nextCursor=_summary_cursor(revision=revision, system=system, kind=kind,
                                                     after=keys[index - 1]))
            break
    if planning_catalog_response_chars(result) > PLANNING_DISCOVERY_MAX_CHARS:
        return _discovery_error("PLANNING_CATALOG_ENTRY_TOO_LARGE", "目录说明超出单次读取预算。")
    return result


def build_planning_catalog(
    *,
    registry: CapabilityRegistry,
    transforms: TransformRegistry,
    trusted_write_prepares: Iterable[str],
    hidden_commit_capabilities: Iterable[str],
    scope_resolver: ScopeResolver,
    granted_scopes: Iterable[str],
) -> dict[str, Any]:
    granted = frozenset(str(scope) for scope in granted_scopes)
    prepares = frozenset(trusted_write_prepares)
    hidden = frozenset(hidden_commit_capabilities)
    capabilities: list[dict[str, Any]] = []
    for spec in registry.list():
        if spec.name in hidden:
            continue
        if spec.effect != "read" and spec.name not in prepares:
            continue
        required = scope_resolver(spec.name)
        if not required.issubset(granted):
            continue
        descriptor = planning_descriptor(spec.name)
        roles = list((descriptor or {}).get("roles") or [])
        if not roles:
            roles = ["write_sink" if spec.name in prepares else "precondition"]
        capabilities.append(
            {
                "name": spec.name,
                "version": spec.version,
                "description": spec.description,
                "system": spec.system,
                "effect": spec.effect,
                "requiredScopes": sorted(required),
                "inputSchema": spec.input_schema,
                "outputSchema": spec.output_schema,
                "planningRole": roles[0],
                "planningRoles": roles,
                "planningDescriptor": descriptor,
                "mayRequireTrustedInteraction": spec.name in prepares,
            }
        )
    transforms_catalog = [spec.to_dict() for spec in transforms.list()]
    capability_names = {item["name"] for item in capabilities}
    transform_names = {item["name"] for item in transforms_catalog}
    examples: list[dict[str, Any]] = []

    if all(
        name in capability_names
        for name in ("oa.workflow.done.list", "oa.workflow.sent.list")
    ) and all(
        name in transform_names
        for name in ("merge_work_items.v1", "work_items_to_log_draft.v2")
    ):
        source_steps: list[dict[str, Any]] = [
            {
                "stepKey": "read_done",
                "kind": "capability",
                "capabilityName": "oa.workflow.done.list",
                "arguments": {
                    "start_date": "2026-07-01",
                    "end_date": "2026-07-31",
                    "limit": 1000,
                },
            },
            {
                "stepKey": "read_sent",
                "kind": "capability",
                "capabilityName": "oa.workflow.sent.list",
                "arguments": {
                    "start_date": "2026-07-01",
                    "end_date": "2026-07-31",
                    "limit": 1000,
                },
            },
            {
                "stepKey": "merge_items",
                "kind": "transform",
                "transformName": "merge_work_items.v1",
                "dependsOn": ["read_done", "read_sent"],
                "bindings": {
                    "sources": {
                        "mode": "many",
                        "items": [
                            {"step": "read_done", "pointer": ""},
                            {"step": "read_sent", "pointer": ""},
                        ],
                    }
                },
            },
        ]
        examples.append(
            {
                "schemaVersion": "agentbridge.task-plan.proposal.v2",
                "title": "合并读取 OA 已办和已发并只读展示",
                "goal": "汇总 2026 年 7 月 OA 已办和已发，只读展示",
                "constraints": {
                    "temporal": {
                        "kind": "absolute_range",
                        "start": "2026-07-01",
                        "end": "2026-07-31",
                    }
                },
                "steps": source_steps,
            }
        )
        draft_log_step = {
            "stepKey": "draft_log",
            "kind": "transform",
            "transformName": "work_items_to_log_draft.v2",
            "dependsOn": ["merge_items"],
            "bindings": {
                "bundle": {
                    "mode": "single",
                    "step": "merge_items",
                    "pointer": "",
                }
            },
        }
        examples.append(
            {
                "schemaVersion": "agentbridge.task-plan.proposal.v2",
                "title": "读取 OA 已办和已发并生成可核对的日志草稿",
                "goal": "汇总 2026 年 7 月 OA 已办和已发并生成工作日志草稿",
                "constraints": {
                    "temporal": {
                        "kind": "absolute_range",
                        "start": "2026-07-01",
                        "end": "2026-07-31",
                    }
                },
                "steps": source_steps + [draft_log_step],
            }
        )
        if "taihua.work_log.create.prepare" in capability_names:
            examples.append(
                {
                    "schemaVersion": "agentbridge.task-plan.proposal.v2",
                    "title": "读取 OA 已办和已发、生成草稿并打开日志系统可信填单",
                    "goal": "汇总 2026 年 7 月 OA 已办和已发并准备填写 3 小时工作日志",
                    "constraints": {
                        "temporal": {
                            "kind": "absolute_range",
                            "start": "2026-07-01",
                            "end": "2026-07-31",
                        }
                    },
                    "steps": source_steps
                    + [
                        draft_log_step,
                        {
                            "stepKey": "prepare_log",
                            "kind": "capability",
                            "capabilityName": "taihua.work_log.create.prepare",
                            "dependsOn": ["draft_log"],
                            "arguments": {
                                "log_date": "2026-07-31",
                                "hours": 3,
                            },
                            "bindings": {
                                "content": {
                                    "mode": "single",
                                    "step": "draft_log",
                                    "pointer": "/draft",
                                }
                            },
                        }
                    ],
                }
            )

    return {
        "schemaVersion": "agentbridge.planning-catalog.v2",
        "policyVersion": COMPOSED_TASK_POLICY_VERSION,
        "supportedProposalVersions": [
            "agentbridge.task-plan.proposal.v1",
            "agentbridge.task-plan.proposal.v2",
        ],
        "supportedStepKinds": ["capability", "transform"],
        "limits": {
            "maximumSteps": 12,
            "maximumWriteSinks": 1,
            "maximumGoalCharacters": 500,
        },
        "prepareInputGuide": {
            "requiredFields": ["goal", "steps"],
            "stepSchema": task_plan_step_json_schema(),
            "rules": [
                "capability 步骤使用 capabilityName，不得使用 transformName",
                "transform 步骤使用 transformName，不得使用 capabilityName",
                "v2 bindings 必须声明 mode=single 或 mode=many；many 最多包含 8 个来源",
                "arguments 只能使用所选能力或转换 inputSchema.properties 中明确声明的字段",
                "目录不支持的用户要求应保留在 goal 或最终说明中，不得臆造为步骤参数",
                "只使用本目录实际返回的能力与转换名称",
                "日期约束由中央服务按原请求时间和上海时区编译；前天使用 day_before_yesterday，昨天使用 previous_day，上周使用 previous_calendar_week",
                "已办按本人处理日期、已发按发起日期下推；按 sourceContract.queryContract 选择参数，不在模型文本中筛选无条件首页",
                "完整汇总使用能力声明的最大 limit；coverage 非 complete 时不得继续派生写入",
            ],
            "examples": examples,
        },
        "capabilities": capabilities,
        "transforms": transforms_catalog,
        "safety": {
            "hiddenCommitToolsExcluded": True,
            "arbitraryCodeExcluded": True,
            "arbitraryHttpExcluded": True,
            "writesRequireTrustedInteractions": True,
        },
    }
