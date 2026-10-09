from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from agentbridge.core.capability import CapabilityRegistry
from agentbridge.core.planning_policy import (
    COMPOSED_TASK_POLICY_VERSION,
    planning_descriptor,
)
from agentbridge.core.task_plan_validation import task_plan_step_json_schema
from agentbridge.core.transforms import TransformRegistry


ScopeResolver = Callable[[str], frozenset[str]]


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
