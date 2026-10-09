from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class WriteActionSpec:
    code: str
    label: str
    action_type: str
    risk: str
    promotion_status: str
    execute_allowed: bool
    dry_run_allowed: bool
    verification_method: str
    requirements: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()


ACTION_LABELS = {
    "Archive": "\u5904\u7406\u540e\u5f52\u6863",
    "Comment": "\u6682\u5b58\u5f85\u529e",
    "CommonPhrase": "\u5e38\u7528\u8bed",
    "ContinueSubmit": "\u63d0\u4ea4",
    "Delete": "\u5220\u9664",
    "Disagree": "\u4e0d\u540c\u610f",
    "Opinion": "\u610f\u89c1",
    "Return": "\u9000\u56de",
    "Revoke": "\u64a4\u9500",
    "Submit": "\u63d0\u4ea4",
    "Track": "\u8ddf\u8e2a",
    "UploadAttachment": "\u4e0a\u4f20\u9644\u4ef6",
}

HIGH_RISK_CODES = {
    "Archive",
    "ContinueSubmit",
    "Delete",
    "Disagree",
    "Return",
    "Revoke",
    "Submit",
    "UploadAttachment",
}

PROMOTED_EXECUTABLE_CODES = {"ContinueSubmit"}

DRY_RUN_ONLY_WRITE_CODES = {
    "Archive",
    "Comment",
    "Delete",
    "Disagree",
    "Return",
    "Revoke",
    "Submit",
    "UploadAttachment",
}

ACTION_TYPES = {
    "Archive": "workflow.archive",
    "Comment": "workflow.comment",
    "ContinueSubmit": "workflow.submit",
    "Delete": "workflow.delete",
    "Disagree": "workflow.disagree",
    "Return": "workflow.return",
    "Revoke": "workflow.revoke",
    "Submit": "workflow.submit",
    "UploadAttachment": "workflow.upload_attachment",
}


WRITE_GOVERNANCE_LIFECYCLE = [
    "resolve_target",
    "dry_run_precheck",
    "confirmation_gate",
    "execute",
    "readback_verification",
    "sanitized_audit",
]


def build_write_governance(
    action_type: str,
    *,
    verification_method: str,
) -> dict:
    return {
        "action_type": str(action_type or ""),
        "lifecycle": list(WRITE_GOVERNANCE_LIFECYCLE),
        "confirmation_required_for_execute": True,
        "verification_method": str(verification_method or ""),
        "audit_policy": "redact_user_text",
    }


def write_action_type(code: str) -> str:
    return get_write_action_spec(code).action_type


def is_dry_run_only_write_action(code: str) -> bool:
    spec = get_write_action_spec(code)
    return spec.dry_run_allowed and not spec.execute_allowed


def write_action_promotion(code: str) -> dict:
    spec = get_write_action_spec(code)
    if spec.execute_allowed:
        return {
            "status": spec.promotion_status,
            "execute_allowed": True,
            "dry_run_allowed": spec.dry_run_allowed,
            "verification_method": spec.verification_method,
            "requirements": [],
            "blocked_reasons": [],
        }
    return {
        "status": spec.promotion_status,
        "execute_allowed": False,
        "dry_run_allowed": spec.dry_run_allowed,
        "verification_method": spec.verification_method,
        "requirements": list(spec.requirements),
        "blocked_reasons": list(spec.blocked_reasons),
    }


def list_write_action_specs() -> list[WriteActionSpec]:
    codes = sorted(
        set(ACTION_LABELS)
        | set(ACTION_TYPES)
        | set(PROMOTED_EXECUTABLE_CODES)
        | set(DRY_RUN_ONLY_WRITE_CODES)
    )
    return [get_write_action_spec(code) for code in codes]


def get_write_action_spec(code: str) -> WriteActionSpec:
    normalized = str(code or "").strip()
    label = ACTION_LABELS.get(normalized, normalized)
    risk = write_action_risk(normalized, label)
    action_type = ACTION_TYPES.get(normalized, "workflow.unpromoted")
    if normalized in PROMOTED_EXECUTABLE_CODES:
        return WriteActionSpec(
            code=normalized,
            label=label,
            action_type=action_type,
            risk=risk,
            promotion_status="promoted",
            execute_allowed=True,
            dry_run_allowed=True,
            verification_method="pending_disappearance",
        )
    requirements = (
        f"execution mapping for {normalized or 'this action'}",
        "post-write verification method",
        "real OA dry-run evidence and user-confirmed production test",
    )
    return WriteActionSpec(
        code=normalized,
        label=label,
        action_type=action_type,
        risk=risk,
        promotion_status="dry_run_only",
        execute_allowed=False,
        dry_run_allowed=True,
        verification_method="not_promoted",
        requirements=requirements,
        blocked_reasons=(f"execute not promoted for {normalized or 'this action'}",),
    )


def classify_write_endpoint_candidates(candidates: Any, *, action: str) -> list[dict[str, Any]]:
    if not isinstance(candidates, list):
        return []
    return [
        classify_write_endpoint_candidate(candidate, action=action)
        for candidate in candidates
        if isinstance(candidate, dict)
    ]


def classify_write_endpoint_candidate(candidate: dict[str, Any], *, action: str) -> dict[str, Any]:
    url = str(candidate.get("url") or "")
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    method_text = " ".join(query.get("method", []))
    action_code = str(action or "").strip()
    haystack = f"{parsed.path} {parsed.query} {method_text}".lower()
    classification = "unknown_write_candidate"
    relation = "unknown"
    confidence = "low"
    reasons: list[str] = []

    if action_code == "Archive" and "finishworkitem" in haystack and "archive" in haystack:
        classification = "possible_archive_completion"
        relation = "possible"
        confidence = "medium"
        reasons.append("URL contains both finishWorkItem and archive markers.")
    elif action_code == "Archive" and any(marker in haystack for marker in ("archive", "pigeonhole")):
        classification = "possible_archive_endpoint"
        relation = "possible"
        confidence = "medium"
        reasons.append("URL contains archive-like markers.")
    elif "supervise" in haystack:
        classification = "auxiliary_supervise"
        relation = "unlikely_direct_archive" if action_code == "Archive" else "auxiliary"
        confidence = "low"
        reasons.append("URL targets supervise APIs, which are usually auxiliary reminder/supervision settings.")
    elif "upload" in haystack:
        classification = "auxiliary_upload"
        relation = "auxiliary"
        confidence = "low"
        reasons.append("URL targets upload behavior, not the requested workflow action itself.")
    elif "opinion" in haystack:
        classification = "opinion_support"
        relation = "auxiliary"
        confidence = "low"
        reasons.append("URL targets opinion/comment behavior.")
    elif any(marker in haystack for marker in ("finish", "submit", "workitem", "save")):
        classification = "generic_write_endpoint"
        relation = "possible"
        confidence = "low"
        reasons.append("URL contains generic write-like markers but no action-specific marker.")
    else:
        reasons.append("No action-specific marker was found in the candidate URL.")

    return {
        **candidate,
        "classification": classification,
        "relation_to_action": relation,
        "confidence": confidence,
        "reasons": reasons,
        "safe_to_call": False,
        "probe_status": "not_called",
        "probe_policy": "do_not_call_without_user_confirmed_test_plan",
    }


def normalize_write_action(action: str) -> dict:
    code = str(action or "").strip()
    label = ACTION_LABELS.get(code, code)
    return {
        "code": code,
        "label": label,
        "risk": write_action_risk(code, label),
    }


def write_action_risk(code: str, label: str = "") -> str:
    if code in HIGH_RISK_CODES:
        return "high"
    value = f"{code} {label}"
    if any(
        marker in value
        for marker in (
            "\u5220\u9664",
            "\u64a4\u9500",
            "\u9000\u56de",
            "\u63d0\u4ea4",
            "\u4e0d\u540c\u610f",
            "\u5f52\u6863",
            "\u4e0a\u4f20",
        )
    ):
        return "high"
    return "medium"


def build_oa_write_plan(
    *,
    affair_id: str,
    action: str,
    opinion: str,
    mode: str,
    source_url: str = "",
) -> dict:
    normalized_action = normalize_write_action(action)
    promotion = write_action_promotion(normalized_action["code"])
    request_status = {
        "draft": "not_built",
        "dry-run": "not_sent",
        "execute": "blocked",
    }.get(mode, "not_built")
    request_reason = (
        f"OA action {normalized_action['code']} has not been promoted to an executable command"
        if not promotion["execute_allowed"]
        else "confirmed OA write will use the promoted central workflow capability"
        if mode == "execute"
        else "write endpoint discovery is required before production execution"
    )
    if mode != "execute":
        request_reason = (
            f"OA action {normalized_action['code']} is available for dry-run only until promotion requirements are met"
            if not promotion["execute_allowed"]
            else "write endpoint discovery is required before production execution"
        )
    target = {"affair_id": str(affair_id)}
    if source_url:
        target["source_url"] = source_url
    payload_preview = {
        "affairId": str(affair_id),
        "actionCode": normalized_action["code"],
        "opinionText": str(opinion or ""),
        "sourceUrl": source_url or "",
        "dryRunOnly": True,
    }
    payload_fields = [
        {"name": "affairId", "value_present": bool(payload_preview["affairId"])},
        {"name": "actionCode", "value_present": bool(payload_preview["actionCode"])},
        {
            "name": "opinionText",
            "value_present": bool(payload_preview["opinionText"]),
            "length": len(payload_preview["opinionText"]),
        },
        {"name": "sourceUrl", "value_present": bool(payload_preview["sourceUrl"])},
    ]
    return {
        "schema_version": "agentbridge.oa_write_plan.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "system": "oa",
        "target": target,
        "action": {
            "code": normalized_action["code"],
            "label": normalized_action["label"],
            "risk": normalized_action["risk"],
        },
        "opinion": {
            "text": str(opinion or ""),
            "length": len(str(opinion or "")),
        },
        "safety": {
            "will_execute": False,
            "requires_confirmation": True,
            "risk": normalized_action["risk"],
            "dry_run_only": True,
        },
        "governance": build_write_governance(
            write_action_type(normalized_action["code"]),
            verification_method=promotion["verification_method"],
        ),
        "promotion": promotion,
        "request": {
            "status": request_status,
            "method": None,
            "url": None,
            "body": None,
            "payload_preview": payload_preview,
            "payload_fields": payload_fields,
            "reason": request_reason,
        },
    }


def build_oa_launch_draft_plan(
    *,
    template_id: str,
    url: str,
    fields: Any,
    mode: str,
    inspection: dict[str, Any] | None = None,
) -> dict:
    field_values = normalize_launch_field_values(fields)
    inspection = inspection if isinstance(inspection, dict) else {}
    target_url = str(inspection.get("url") or url or "")
    target_template_id = str(inspection.get("template_id") or template_id or "")
    matched_fields, missing = _launch_draft_field_plan(field_values, inspection)
    save_button = find_launch_save_draft_button(inspection)
    checks = [
        {
            "name": "target_resolved",
            "passed": bool(target_url),
            "detail": "launch URL resolved" if target_url else "template_id or url is required",
        },
        {
            "name": "save_draft_button",
            "passed": bool(save_button),
            "detail": "save draft control found" if save_button else "save draft control was not found",
        },
        {
            "name": "fields_writable",
            "passed": not missing,
            "detail": "all requested fields are writable" if not missing else "some requested fields are missing or not writable",
        },
    ]
    blocked_reasons = []
    if not target_url:
        blocked_reasons.append("template_id or url is required")
    if not save_button:
        blocked_reasons.append("save draft control was not found on the launch page")
    blocked_reasons.extend(missing)
    execute_ready = not blocked_reasons
    request_status = "not_sent" if mode == "dry-run" else "blocked"
    request_reason = "dry-run only; no page mutation will be performed"
    if mode == "save-draft":
        request_reason = (
            "confirmed save-draft will use the promoted central launch-page workflow"
            if execute_ready
            else "save-draft is blocked until precheck passes"
        )
    return {
        "schema_version": "agentbridge.oa_launch_draft_plan.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "mode": str(mode or ""),
        "system": "oa",
        "target": {
            "template_id": target_template_id,
            "url": target_url,
            "title": str(inspection.get("title") or ""),
        },
        "action": {
            "code": "SaveDraft",
            "label": "保存待发",
            "risk": "medium",
        },
        "fields": matched_fields,
        "field_count": len(matched_fields),
        "checks": checks,
        "missing": missing,
        "blocked_reasons": blocked_reasons,
        "safety": {
            "will_execute": False,
            "requires_confirmation": True,
            "risk": "medium",
            "submitted_count": 0,
            "forbidden_actions": ["sendId_a", "ContinueSubmit", "Submit", "发送", "提交"],
            "only_allowed_action": "SaveDraft",
            "execute_ready": execute_ready,
        },
        "governance": build_write_governance(
            "workflow.launch.save_draft",
            verification_method="draft_save_scheduled_ack",
        ),
        "request": {
            "status": request_status,
            "method": "browser_click" if mode == "save-draft" and execute_ready else None,
            "url": target_url,
            "body": None,
            "payload_preview": {
                "templateId": target_template_id,
                "fieldCount": len(matched_fields),
                "fieldNames": [field["name"] for field in matched_fields],
                "actionCode": "SaveDraft",
            },
            "payload_fields": matched_fields,
            "reason": request_reason,
        },
        "read_effect": {
            "launch_page_opened": bool(inspection),
            "may_create_draft": mode == "save-draft",
            "submitted_count": 0,
        },
    }


def normalize_launch_field_values(fields: Any) -> dict[str, str]:
    if fields is None:
        return {}
    if not isinstance(fields, dict):
        raise ValueError("fields must be an object")
    normalized = {}
    for key, value in fields.items():
        name = str(key or "").strip()
        if not name:
            continue
        normalized[name] = "" if value is None else str(value)
    return normalized


def find_launch_save_draft_button(inspection: dict[str, Any]) -> dict[str, Any]:
    buttons = inspection.get("buttons") if isinstance(inspection, dict) else []
    if not isinstance(buttons, list):
        return {}
    for button in buttons:
        if not isinstance(button, dict):
            continue
        code = str(button.get("id") or button.get("name") or "")
        text = str(button.get("text") or "")
        if _is_launch_save_draft_marker(code, text):
            return {
                "id": code,
                "name": str(button.get("name") or ""),
                "text": text,
                "tag": str(button.get("tag") or ""),
                "type": str(button.get("type") or ""),
            }
    return {}


def _launch_draft_field_plan(field_values: dict[str, str], inspection: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    available = inspection.get("fields") if isinstance(inspection.get("fields"), list) else []
    planned = []
    missing = []
    for input_key, value in field_values.items():
        field = _match_launch_field(input_key, available)
        if not field:
            missing.append(f"field:{input_key}")
            planned.append(
                {
                    "input_key": input_key,
                    "name": input_key,
                    "matched": False,
                    "writable": False,
                    "value_present": bool(value),
                    "length": len(value),
                }
            )
            continue
        readonly = bool(field.get("readonly")) or bool(field.get("disabled"))
        if readonly:
            missing.append(f"field_not_writable:{input_key}")
        planned.append(
            {
                "input_key": input_key,
                "name": str(field.get("name") or field.get("id") or input_key),
                "id": str(field.get("id") or ""),
                "label": str(field.get("label") or ""),
                "type": str(field.get("type") or ""),
                "matched": True,
                "writable": not readonly,
                "value_present": bool(value),
                "length": len(value),
            }
        )
    return planned, missing


def _match_launch_field(input_key: str, available: list[Any]) -> dict[str, Any]:
    normalized_key = _normalize_launch_lookup(input_key)
    for field in available:
        if not isinstance(field, dict):
            continue
        candidates = (
            field.get("name"),
            field.get("id"),
            field.get("label"),
        )
        if any(_normalize_launch_lookup(candidate) == normalized_key for candidate in candidates):
            return field
    return {}


def _normalize_launch_lookup(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_launch_save_draft_marker(code: str, text: str) -> bool:
    value = f"{code} {text}".lower()
    return any(marker in value for marker in ("savedraft", "save draft", "保存待发", "保存草稿", "暂存待发"))


def mark_oa_launch_draft_plan_for_execution(plan: dict[str, Any]) -> None:
    safety = plan.setdefault("safety", {})
    safety["will_execute"] = True
    safety["dry_run_only"] = False
    request = plan.setdefault("request", {})
    request["status"] = "queued"
    request["reason"] = "confirmed save-draft task queued for central worker execution"


def sanitize_oa_launch_draft_plan_for_audit(plan: dict) -> dict:
    sanitized = json.loads(json.dumps(plan, ensure_ascii=False))
    request = sanitized.get("request")
    if isinstance(request, dict):
        request["body"] = None
    return sanitized


def append_oa_launch_draft_audit(home: Path, plan: dict) -> Path:
    audit_dir = home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "oa-launch-drafts.jsonl"
    sanitized = sanitize_oa_launch_draft_plan_for_audit(plan)
    with audit_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(sanitized, ensure_ascii=False, sort_keys=True))
        file.write("\n")
    return audit_path


def build_oa_write_preflight(
    plan: dict,
    *,
    precheck_passed: bool,
    precheck_error: str = "",
) -> dict:
    sanitized_plan = sanitize_oa_write_plan_for_audit(plan)
    promotion = plan.get("promotion") if isinstance(plan.get("promotion"), dict) else {}
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    action = plan.get("action") if isinstance(plan.get("action"), dict) else {}
    blocked_reasons = list(plan.get("blocked_reasons") or [])
    if precheck_error and precheck_error != "write precheck blocked" and precheck_error not in blocked_reasons:
        blocked_reasons.append(precheck_error)
    if not precheck_passed:
        status = "blocked"
    elif promotion.get("execute_allowed") is True:
        status = "ready_for_execute"
    elif promotion.get("dry_run_allowed") is True:
        status = "dry_run_only"
        blocked_reasons.extend(reason for reason in promotion.get("blocked_reasons") or [] if reason not in blocked_reasons)
    else:
        status = "blocked"
        blocked_reasons.extend(reason for reason in promotion.get("blocked_reasons") or [] if reason not in blocked_reasons)

    affair_id = str(target.get("affair_id") or "")
    action_code = str(action.get("code") or "")
    dry_run_template = _write_command_template("dry-run", affair_id=affair_id, action=action_code)
    execute_template = (
        _write_command_template("execute", affair_id=affair_id, action=action_code, confirm=True)
        if status == "ready_for_execute"
        else ""
    )
    detail_opened = any(
        isinstance(check, dict) and check.get("name") == "detail_read"
        for check in plan.get("checks") or []
    )
    return {
        "schema_version": "agentbridge.oa_write_preflight.v1",
        "target": sanitized_plan.get("target", {}),
        "action": sanitized_plan.get("action", {}),
        "decision": {
            "status": status,
            "dry_run_passed": bool(precheck_passed),
            "dry_run_allowed": bool(promotion.get("dry_run_allowed")),
            "execute_allowed": status == "ready_for_execute",
            "requires_confirmation": True,
            "verification_method": promotion.get("verification_method", ""),
            "blocked_reasons": blocked_reasons,
            "missing": list(plan.get("missing") or []),
            "suggestions": list(plan.get("suggestions") or []),
        },
        "execution_contract": {
            "will_execute": False,
            "request_sent": False,
            "network_probe_sent": False,
            "confirmation_required_for_execute": True,
            "confirm_argument": "confirm=true",
            "confirm_flag": "--confirm",
            "dry_run_command_template": dry_run_template,
            "execute_command_template": execute_template,
        },
        "read_effect": {
            "detail_page_opened": detail_opened,
            "may_mark_read": detail_opened,
            "note": "Preflight reads a pending detail page and may change its read/unread state." if detail_opened else "",
        },
        "probe_policy": {
            "automatic_network_probe": False,
            "reason": "preflight does not call endpoint candidates or execute page write functions",
        },
        "plan": sanitized_plan,
    }


def _write_command_template(
    mode: str,
    *,
    affair_id: str,
    action: str,
    confirm: bool = False,
) -> str:
    if not affair_id or not action:
        return ""
    command = f"oa write {mode} --affair-id {affair_id} --action {action} --opinion <opinion>"
    if confirm:
        command = f"{command} --confirm"
    return command


def sanitize_oa_write_plan_for_audit(plan: dict) -> dict:
    sanitized = json.loads(json.dumps(plan, ensure_ascii=False))
    opinion = sanitized.get("opinion")
    if isinstance(opinion, dict):
        opinion.pop("text", None)
    request = sanitized.get("request")
    if isinstance(request, dict):
        request["body"] = None
        payload_preview = request.get("payload_preview")
        if isinstance(payload_preview, dict):
            payload_preview["opinionText"] = None
    return sanitized


def append_oa_write_audit(home: Path, plan: dict) -> Path:
    audit_dir = home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "oa-write-plans.jsonl"
    sanitized = sanitize_oa_write_plan_for_audit(plan)
    with audit_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(sanitized, ensure_ascii=False, sort_keys=True))
        file.write("\n")
    return audit_path


def append_oa_write_verification_audit(
    home: Path,
    *,
    affair_id: str,
    action: str,
    source_url: str,
    verification: dict,
    submit: dict | None = None,
) -> Path:
    audit_dir = home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "oa-write-verifications.jsonl"
    row = {
        "schema_version": "agentbridge.oa_write_verification.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "system": "oa",
        "target": {
            "affair_id": str(affair_id),
            "source_url": str(source_url or ""),
        },
        "action": normalize_write_action(action),
        "verification": verification,
        "submit": submit or {},
    }
    with audit_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        file.write("\n")
    return audit_path
