"""Pure task ledger projections without reads, writes, clocks or authorization.

Callers supply already-authorized row snapshots. Explicit source/reference flags
retain the existing public versus internal payload boundaries.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from bscli.core.task_plans import json_hash, task_plan_response


def batch_task_summary(
    task: Mapping[str, Any], batch: Mapping[str, Any], current_item: Mapping[str, Any],
) -> dict:
    summary = json.loads(task["summary_json"] or "{}")
    summary["batch"] = {
        "batchId": batch["batch_id"],
        "systemId": batch["system_id"],
        "capability": batch["capability_name"],
        "state": batch["state"],
        "currentOrdinal": int(batch["current_ordinal"]),
        "totalCount": int(batch["total_count"]),
        "succeededCount": int(batch["succeeded_count"]),
        "failedCount": int(batch["failed_count"]),
        "skippedCount": int(batch["skipped_count"]),
        "failurePolicy": batch["failure_policy"],
        "currentItem": {
            "ordinal": int(current_item["ordinal"]),
            "state": current_item["state"],
            "display": json.loads(current_item["display_summary_json"] or "{}"),
        },
    }
    return summary

def _endpoint_from_row(row: Mapping[str, Any]) -> dict:
    value = dict(row)
    value["capabilities"] = json.loads(value.pop("capabilities_json"))
    value["route"] = json.loads(value.pop("route_json"))
    return value


def _task_from_row(row: Mapping[str, Any]) -> dict:
    value = dict(row)
    value["summary"] = json.loads(value.pop("summary_json"))
    return value


def _batch_from_row(row: Mapping[str, Any]) -> dict:
    value = dict(row)
    value["selection_summary"] = json.loads(
        value.pop("selection_summary_json") or "{}"
    )
    return value


def _batch_item_from_row(
    row: Mapping[str, Any],
    *,
    include_resource_ref: bool,
) -> dict:
    value = dict(row)
    value["display_summary"] = json.loads(
        value.pop("display_summary_json") or "{}"
    )
    value["result_summary"] = json.loads(
        value.pop("result_summary_json") or "{}"
    )
    if not include_resource_ref:
        value.pop("resource_ref", None)
    return value


def _artifact_from_row(
    row: Mapping[str, Any],
    *,
    include_source_ref: bool = False,
) -> dict:
    result = {
        "artifact_id": row["artifact_id"],
        "task_id": row["task_id"],
        "user_subject": row["user_subject"],
        "artifact_type": row["artifact_type"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "byte_size": int(row["byte_size"]),
        "download_url": row["download_url"],
        "state": row["state"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
    }
    if include_source_ref:
        result["source_ref"] = row["source_ref"]
    return result


def _event_from_row(row: Mapping[str, Any]) -> dict:
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


def _outbox_from_row(row: Mapping[str, Any]) -> dict:
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


def _timeline_from_row(row: Mapping[str, Any]) -> dict:
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


def _continuation_from_row(row: Mapping[str, Any]) -> dict:
    value = dict(row)
    value["candidate_task_ids"] = json.loads(
        value.pop("candidate_task_ids_json")
    )
    value["allow_new_operation"] = value["execution_mode"] in {
        "resume",
        "follow_up",
    }
    return value


def _artifact_delivery_aggregate(by_channel: dict[str, dict]) -> dict:
    artifacts: set[str] = set()
    parts: list[str] = []
    delivered_channels = 0
    failed_channels = 0
    for channel, report in sorted(by_channel.items()):
        files = report.get("files") if isinstance(report, dict) else []
        for item in (files if isinstance(files, list) else []):
            artifact_id = item.get("artifactId") if isinstance(item, dict) else None
            if artifact_id:
                artifacts.add(str(artifact_id))
        attachment_count = max(0, int(report.get("attachmentSentCount") or 0))
        fallback_count = max(0, int(report.get("fallbackLinkSentCount") or 0))
        failed_count = max(0, int(report.get("failedCount") or 0))
        label = {
            "telegram": "Telegram",
            "openclaw-weixin": "微信",
            "weixin": "微信",
            "wechat": "微信",
            "web": "网页端",
            "webchat": "网页端",
            "agentbridge-workspace": "网页端",
        }.get(str(channel).casefold(), str(channel))
        channel_parts = []
        if attachment_count:
            channel_parts.append(f"{attachment_count} 份附件已送达")
        if fallback_count:
            channel_parts.append(
                f"{fallback_count} 个下载入口可用"
                if label == "网页端"
                else f"{fallback_count} 份已通过下载链接送达"
            )
        if failed_count:
            channel_parts.append(f"{failed_count} 份未送达")
        if channel_parts:
            parts.append(f"{label}：{'，'.join(channel_parts)}")
        if report.get("state") == "delivered":
            delivered_channels += 1
        elif report.get("state") in {"partial", "failed"}:
            failed_channels += 1
    prepared_count = len(artifacts)
    prefix = f"{prepared_count} 份文件已准备" if prepared_count else "文件已准备"
    return {
        "state": "partial" if failed_channels else "delivered",
        "completionMeaning": "cross_endpoint_delivery_reported",
        "preparedCount": prepared_count,
        "channelCount": len(by_channel),
        "deliveredChannelCount": delivered_channels,
        "failedChannelCount": failed_channels,
        "channels": by_channel,
        "userMessage": f"{prefix}；{'；'.join(parts)}。" if parts else f"{prefix}。",
    }



def build_plan_result_projection(
    *, projection_kind: str, step: dict[str, Any], operation_id: str, result: dict[str, Any],
) -> dict[str, Any]:
    projection: dict[str, Any] = {
        "schemaVersion": "agentbridge.plan-result-projection.v1",
        "visibility": "user_private",
        "kind": projection_kind,
        "stepKey": step["step_key"],
        "operationId": operation_id,
        "resultHash": json_hash(result),
        "sourceSteps": sorted(
            {
                reference["step"]
                for binding in (step.get("bindings") or {}).values()
                for reference in (
                    binding.get("items") or []
                    if binding.get("mode") == "many"
                    else [binding]
                )
            }
        ),
    }
    if projection_kind == "private_draft":
        projection["result"] = {
            key: result.get(key)
            for key in (
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
            )
        }
    else:
        projection["result"] = {
            key: result.get(key)
            for key in (
                "items",
                "source_summaries",
                "coverage",
                "source_incomplete",
                "empty",
                "source_count",
                "item_count",
                "duplicate_count",
            )
        }
    return projection


def plan_result_response(plan: dict[str, Any]) -> dict[str, Any]:
    status = {
        "succeeded": "succeeded",
        "failed": "failed",
        "outcome_unknown": "outcome_unknown",
        "canceled": "canceled",
        "waiting_user": "requires_user_action",
    }.get(plan["state"], "running")
    response = {
        "protocolVersion": "0.1",
        "status": status,
        "plan": task_plan_response(plan),
    }
    if status == "succeeded" and plan.get("result_projection"):
        response["nextAction"] = {
            "type": "report_plan_result",
            "source": "plan.resultProjection.result",
            "doNotQueryOperations": True,
        }
    if plan.get("terminal_reason") == "PLAN_SOURCE_INCOMPLETE":
        response["nextAction"] = {
            "type": "report_plan_failure", "doNotRetryAtomicTools": True,
            "businessWriteOccurred": False,
            "source": "plan.resultProjection.result.source_summaries",
        }
    return response
