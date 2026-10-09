from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentbridge.adapters.seeyon_write import get_write_action_spec


@dataclass(frozen=True)
class MatterIntentSpec:
    code: str
    label: str
    scene: str
    action: str
    action_type: str


INTENT_SPECS = {
    "approve": MatterIntentSpec(
        code="approve",
        label="审批通过",
        scene="received_pending",
        action="ContinueSubmit",
        action_type="workflow.submit",
    ),
    "archive": MatterIntentSpec(
        code="archive",
        label="处理后归档",
        scene="received_pending",
        action="Archive",
        action_type="workflow.archive",
    ),
}


def normalize_matter_intent(value: str) -> MatterIntentSpec:
    code = str(value or "").strip().lower()
    aliases = {
        "pass": "approve",
        "submit": "approve",
        "agree": "approve",
        "archive_after_process": "archive",
        "pigeonhole": "archive",
    }
    code = aliases.get(code, code)
    if code not in INTENT_SPECS:
        raise ValueError(f"unsupported matter intent: {value}")
    return INTENT_SPECS[code]


def build_matter_intent_preflight(
    *,
    source_item: dict[str, Any],
    evidence: dict[str, Any],
    intent: str,
    opinion: str = "",
) -> dict[str, Any]:
    spec = normalize_matter_intent(intent)
    identity = evidence.get("identity") if isinstance(evidence.get("identity"), dict) else {}
    actions = evidence.get("actions") if isinstance(evidence.get("actions"), dict) else {}
    action_items = actions.get("items") if isinstance(actions.get("items"), list) else []
    action_codes = actions.get("codes") if isinstance(actions.get("codes"), list) else []
    if not action_codes:
        action_codes = [str(item.get("code") or "") for item in action_items if isinstance(item, dict)]

    matched_action = _find_action(action_items, spec.action)
    action_available = bool(matched_action) or spec.action in action_codes
    write_spec = get_write_action_spec(spec.action)
    if not action_available:
        status = "blocked"
        blocked_reasons = [f"current pending item does not expose action {spec.action}"]
    elif write_spec.execute_allowed:
        status = "ready_for_execute"
        blocked_reasons = []
    elif write_spec.dry_run_allowed:
        status = "dry_run_only"
        blocked_reasons = list(write_spec.blocked_reasons)
    else:
        status = "blocked"
        blocked_reasons = list(write_spec.blocked_reasons)

    affair_id = str(identity.get("affair_id") or source_item.get("affair_id") or "")
    source_url = str(identity.get("url") or source_item.get("href") or "")
    return {
        "schema_version": "agentbridge.oa_matter_intent_preflight.v1",
        "scene": spec.scene,
        "target": {
            "affair_id": affair_id,
            "title": str(identity.get("title") or source_item.get("title") or ""),
            "source_url": source_url,
            "category": str(identity.get("category") or source_item.get("category") or ""),
        },
        "matter": {
            "name": str(identity.get("title") or source_item.get("title") or ""),
            "source": "pending_workflow",
        },
        "intent": {
            "code": spec.code,
            "label": spec.label,
            "opinion_required": True,
            "opinion_length": len(str(opinion or "")),
        },
        "binding": {
            "action": spec.action,
            "action_type": spec.action_type,
            "label": write_spec.label,
            "risk": write_spec.risk,
            "available_on_page": action_available,
            "promotion_status": write_spec.promotion_status,
            "execute_allowed": write_spec.execute_allowed,
            "dry_run_allowed": write_spec.dry_run_allowed,
            "verification_method": write_spec.verification_method,
        },
        "decision": {
            "status": status,
            "execute_allowed": status == "ready_for_execute",
            "requires_confirmation": True,
            "verification_method": write_spec.verification_method,
            "blocked_reasons": blocked_reasons,
        },
        "execution_contract": {
            "will_execute": False,
            "request_sent": False,
            "confirmation_required_for_execute": True,
            "low_level_dry_run_command": _write_command("dry-run", affair_id, spec.action),
            "low_level_execute_command": (
                _write_command("execute", affair_id, spec.action, confirm=True)
                if status == "ready_for_execute"
                else ""
            ),
        },
        "evidence_summary": {
            "action_codes": [code for code in action_codes if code],
            "action_count": len(action_codes),
        },
    }


def _find_action(actions: list[Any], code: str) -> dict[str, Any]:
    for action in actions:
        if isinstance(action, dict) and str(action.get("code") or "") == code:
            return action
    return {}


def _write_command(mode: str, affair_id: str, action: str, *, confirm: bool = False) -> str:
    if not affair_id or not action:
        return ""
    command = f"oa write {mode} --affair-id {affair_id} --action {action} --opinion <opinion>"
    if confirm:
        command = f"{command} --confirm"
    return command
