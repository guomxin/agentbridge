"""Versioned, plain-text OA evidence for trusted review pages only.

Attachment contents are not fetched. Completeness describes the controlled
rendered detail, not every historical opinion or attachment in the OA system.
"""
from __future__ import annotations

from copy import deepcopy

MATERIALS_VERSION = "1"
MAX_MATERIAL_TEXT = 262144
MAX_MATERIAL_FIELDS = 2048
MAX_MATERIAL_OPINIONS = 512
MAX_MATERIAL_VALUE = 65536


def material_snapshot(detail):
    supplied = detail.get("_trusted_materials")
    if isinstance(supplied, dict):
        return deepcopy(supplied)
    # Older persisted/fake details do not prove extraction coverage. Never
    # relabel their bounded public excerpt as complete material.
    return {"text": str(detail.get("text") or ""),
            "fields": deepcopy(detail.get("fields") or []),
            "opinions": deepcopy(detail.get("opinions") or detail.get("workflow") or []),
            "attachments": [{"name": str(item.get("name") or "")}
                            for item in detail.get("attachments") or [] if isinstance(item, dict)],
            "completeness": {"status": "unverified", "scope": "rendered_detail",
                             "reasons": ["legacy_detail_coverage_unreported"]}, "truncated": False}


def require_reviewable_materials(detail, error_type):
    material = material_snapshot(detail)
    if material.get("truncated") or material.get("completeness", {}).get("status") == "incomplete":
        raise error_type("The OA review materials are incomplete; refresh the controlled detail before approval.")


def material_fingerprint_payload(source, detail):
    return {"materials_version": MATERIALS_VERSION,
            "title": str(source.get("title") or detail.get("title") or ""),
            "materials": material_snapshot(detail)}


def _table(section_id, title, snapshot, labels, groups):
    columns = [{"key": key, "label": labels[key]} for key in groups]
    rows = {}
    for field in snapshot.get("fields") or []:
        if field.get("id") not in groups:
            continue
        record = str(field.get("record_id") or "")
        row = rows.setdefault(record, {"id": record, "cells": {}})
        row["cells"][field["id"]] = str(field.get("value") or "")
    return {"id": section_id, "title": title, "kind": "table", "columns": columns, "rows": list(rows.values())}


def review_materials(profile, detail, business_snapshot=None):
    material = material_snapshot(detail)
    fields = [{"label": str(item.get("name") or ""), "value": str(item.get("value") or "")}
              for item in material["fields"] if isinstance(item, dict)]
    sections = []
    if profile == "weekly_report":
        for marker, section_id, title in (("本周", "current-week", "本周工作"), ("下周", "next-week", "下周计划")):
            selected = [item for item in fields if marker in item["label"]]
            if selected:
                sections.append({"id": section_id, "title": title, "kind": "facts", "fields": selected})
    snapshot = business_snapshot or {}
    if profile == "work_handover" and snapshot:
        from agentbridge.adapters.seeyon_work_handover import FIELD_LABELS, WORK_FIELDS, FINANCE_FIELDS
        sections.extend([_table("handover-work", "工作交接明细", snapshot, FIELD_LABELS, WORK_FIELDS),
                         _table("handover-finance", "财务交接明细", snapshot, FIELD_LABELS, FINANCE_FIELDS)])
    if profile == "quarterly_performance" and snapshot:
        from agentbridge.adapters.seeyon_quarterly_performance import FIELD_LABELS, ROW_FIELDS
        sections.append(_table("performance-employees", "季度绩效人员明细（只读）", snapshot, FIELD_LABELS, ROW_FIELDS))
    if profile == "intellectual_property_reward" and snapshot:
        from agentbridge.adapters.seeyon_intellectual_property_reward import FIELD_LABELS, MASTER_FIELDS, ROW_FIELDS
        sections.append({"id": "reward-information", "title": "知识产权奖励信息（只读）", "kind": "facts",
                         "fields": [{"label": FIELD_LABELS[field["id"]], "value": field["value"]}
                                    for field in snapshot.get("fields") or [] if field["id"] in MASTER_FIELDS]})
        sections.append(_table("reward-allocations", "奖励分配明细（只读）", snapshot, FIELD_LABELS, ROW_FIELDS))
    if profile == "flight_application" and snapshot:
        from agentbridge.adapters.seeyon_flight_application import FIELD_LABELS, LEG_FIELDS
        sections.append(_table("flight-legs", "航段明细", snapshot, FIELD_LABELS, LEG_FIELDS))
    if fields:
        sections.append({"id": "original-fields", "title": "原始表单字段", "kind": "facts", "fields": fields})
    if material["text"]:
        sections.append({"id": "body", "title": "正文与页面材料", "kind": "text", "text": material["text"]})
    opinions = []
    for index, item in enumerate(material["opinions"]):
        if not isinstance(item, dict):
            continue
        opinions.append({"id": str(index), "cells": {key: str(item.get(key) or "")
                         for key in ("handler", "opinion", "time", "text")}})
    if opinions:
        sections.append({"id": "existing-opinions", "title": "已有处理意见", "kind": "table",
                         "columns": [{"key": key, "label": label} for key, label in
                                     (("handler", "处理人"), ("opinion", "意见"), ("time", "时间"), ("text", "原始意见"))],
                         "rows": opinions})
    return {"kind": profile, "materialsVersion": MATERIALS_VERSION, "sections": sections,
            "attachments": material["attachments"], "completeness": material["completeness"],
            "truncated": material["truncated"]}
