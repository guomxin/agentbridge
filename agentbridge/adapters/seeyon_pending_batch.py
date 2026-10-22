from __future__ import annotations

import hashlib
import json

from agentbridge.adapters.seeyon_missed_punch import (
    MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY,
    MISSED_PUNCH_APPROVE_CAPABILITY,
)
from agentbridge.adapters.seeyon_pending_actions import pending_action_profile_for_title


PENDING_BATCH_PREPARE_CAPABILITY = "oa.workflow.pending.batch.prepare"
PENDING_BATCH_TYPES = (
    "meeting_reply", "missed_punch", "efficiency_data", "travel_expense", "business_trip",
    "labor_contract_renewal",
    "intellectual_property_declaration", "intellectual_property_reward", "overtime", "leave", "resignation", "work_handover",
    "flight_application",
    "attendance_confirmation", "weekly_report", "quarterly_performance", "standard_collaboration",
)
PENDING_BATCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "affair_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
        "workflow_types": {"type": "array", "items": {"type": "string", "enum": list(PENDING_BATCH_TYPES)}, "minItems": 1},
        "keyword": {"type": "string", "maxLength": 200},
        "max_items": {"type": "integer", "minimum": 1, "maximum": 100},
        "batch_id": {"type": "string"},
        "affair_id": {"type": "string"},
        "input_submission_id": {"type": "string"},
    },
    "additionalProperties": False,
}


class PendingBatchSelectionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_COLLABORATION_CATEGORIES = frozenset({"协同", "collaboration"})


def _supports_pending_category(row: dict) -> bool:
    # The section's category is a business type, not a title or node policy.
    # Legacy sources may omit it; those still require the normal form/node
    # preflight. Explicit, unrecognized types must never acquire a workflow.
    category = row.get("category")
    if category is None:
        return True
    if not isinstance(category, str):
        return False
    category = category.strip().casefold()
    return not category or category in _COLLABORATION_CATEGORIES or category == "会议"


def _unsupported_item_label(row: dict) -> str:
    label = str(row.get("title") or "无标题事项")[:120]
    category = row.get("category")
    if isinstance(category, str) and category.strip():
        label += f"（事项类别：{category.strip()[:40]}）"
    return label


def _reject_unsupported(items: list[str]) -> None:
    raise PendingBatchSelectionError("BATCH_UNSUPPORTED_ITEMS", f"范围内有 {len(items)} 条未支持事项，未启动任何审批：{'；'.join(items[:5])}。请排除这些事项后重试。")


def select_pending_batch_items(pending: dict, arguments: dict, registry) -> list[dict]:
    maximum = arguments.get("max_items", 20)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 100:
        raise ValueError("max_items must be between 1 and 100")
    ids = arguments.get("affair_ids")
    types = arguments.get("workflow_types")
    if ids is not None and (not isinstance(ids, list) or not ids or len(ids) > 100
                            or any(not isinstance(i, str) or not i.strip() for i in ids)
                            or len(set(ids)) != len(ids)):
        raise ValueError("affair_ids must contain 1 to 100 distinct nonempty IDs")
    if types is not None and (not isinstance(types, list) or not types
                              or any(t not in PENDING_BATCH_TYPES for t in types)):
        raise ValueError("workflow_types contains unsupported types")
    coverage = pending.get("coverage") or {}
    if coverage.get("status") != "complete":
        raise PendingBatchSelectionError("BATCH_SOURCE_INCOMPLETE", "待办来源不完整，未创建批次；请缩小范围或稍后重查，不能把部分结果当作全部。")
    rows = pending.get("items")
    if not isinstance(rows, list) or any(not isinstance(r, dict) or not r.get("affair_id") for r in rows):
        raise PendingBatchSelectionError("BATCH_SOURCE_INVALID", "待办列表缺少有效事项标识，未创建批次。")
    source_ids = [str(r["affair_id"]) for r in rows]
    if len(source_ids) != len(set(source_ids)):
        raise PendingBatchSelectionError("BATCH_SOURCE_INVALID", "待办列表存在重复事项，未创建批次。")
    if ids and set(ids) - set(source_ids):
        raise PendingBatchSelectionError("BATCH_TARGET_UNAVAILABLE", "指定事项已不在当前用户待办中；未替换为其他事项，请重新核对范围。")
    if ids:
        # Explicit targets stay in scope even when a type/keyword filter would
        # otherwise hide their unsupported business category.
        rejected = [_unsupported_item_label(row) for row in rows
                    if str(row["affair_id"]) in ids and not _supports_pending_category(row)]
        if rejected:
            _reject_unsupported(rejected)
    keyword = str(arguments.get("keyword") or "").strip().casefold()
    selected, unsupported = [], []
    for row in rows:
        affair_id, title = str(row["affair_id"]), str(row.get("title") or "")
        if ids and affair_id not in ids:
            continue
        profile = (
            {"profile": "meeting_reply", "prepare_capability": "oa.meeting.reply.prepare",
             "commit_capability": "oa.meeting.reply"} if str(row.get("category") or "").strip() == "会议" else
            {"profile": "missed_punch", "prepare_capability": MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY,
             "commit_capability": MISSED_PUNCH_APPROVE_CAPABILITY}
            if "补签申请单" in title else pending_action_profile_for_title(title)
        )
        if types and (profile or {}).get("profile") not in types:
            if ids:
                raise PendingBatchSelectionError("BATCH_SELECTION_MISMATCH", "指定事项与流程类型筛选不一致，未创建批次。")
            continue
        if keyword and keyword not in title.casefold():
            if ids:
                raise PendingBatchSelectionError("BATCH_SELECTION_MISMATCH", "指定事项与关键词筛选不一致，未创建批次。")
            continue
        if not _supports_pending_category(row) or profile is None:
            unsupported.append(_unsupported_item_label(row))
            continue
        display = {k: str(row.get(k) or "") for k in ("title", "sender", "date", "category")}
        display.update(profile)
        display["prepare_version"] = registry.get(profile["prepare_capability"]).version
        display["commit_version"] = registry.get(profile["commit_capability"]).version
        fingerprint = hashlib.sha256(json.dumps({"affair_id": affair_id, **display}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        selected.append({"resource_ref": affair_id, "display_summary": display, "source_fingerprint": fingerprint})
    if unsupported:
        _reject_unsupported(unsupported)
    if len(selected) > maximum:
        raise PendingBatchSelectionError("BATCH_LIMIT_EXCEEDED", f"匹配 {len(selected)} 条，超过本批上限 {maximum} 条；未截断或开始审批，请明确缩小范围或调整上限（最多100）。")
    if ids:
        selected.sort(key=lambda item: ids.index(item["resource_ref"]))
    return selected
