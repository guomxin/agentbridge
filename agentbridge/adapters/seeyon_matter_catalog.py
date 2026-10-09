from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


MEETING_CREATE_URL = "http://10.10.50.110/seeyon/meeting.do?method=editor&showTab=true"


@dataclass(frozen=True)
class MatterTargetSpec:
    matter_id: str
    name: str
    subject: str
    category_tag: str
    aliases: tuple[str, ...]
    template_title: str = ""
    launch_url: str = ""
    launch_type: str = "template"
    recommended_fields: tuple[str, ...] = ()
    launch_profile: dict[str, Any] | None = None
    received_profile: dict[str, Any] | None = None


FIRST_BATCH_TARGETS: tuple[MatterTargetSpec, ...] = (
    MatterTargetSpec(
        matter_id="matter-seal-request",
        name="\u3010\u7528\u5370\u3011\u7528\u5370\u7533\u8bf7\u5355",
        subject="\u7528\u5370\u7533\u8bf7\u5355",
        category_tag="\u7528\u5370",
        aliases=("\u7528\u5370", "\u7528\u5370\u7533\u8bf7", "\u7528\u5370\u7533\u8bf7\u5355"),
        template_title="\u3010\u7528\u5370\u3011\u7528\u5370\u7533\u8bf7\u5355",
        recommended_fields=("content_coll",),
    ),
    MatterTargetSpec(
        matter_id="matter-missed-punch-request",
        name="\u3010HR\u3011\u8865\u7b7e\u7533\u8bf7\u5355",
        subject="\u8865\u7b7e\u7533\u8bf7\u5355",
        category_tag="HR",
        aliases=("\u8865\u7b7e", "\u8865\u7b7e\u7533\u8bf7", "\u8865\u7b7e\u7533\u8bf7\u5355"),
        template_title="\u3010HR\u3011\u8865\u7b7e\u7533\u8bf7\u5355",
        recommended_fields=("content_coll",),
        received_profile={
            "profile_id": "workflow.missed_punch.approval.v1",
            "profile_status": "live_validated",
            "business_intent": "approve",
            "user_facing_action": "approve_missed_punch_request",
            "default_opinion": "\u540c\u610f",
            "execution_route": "matter_execute",
            "binding": "ContinueSubmit",
            "verification_method": "pending_disappearance",
            "required_prefill": [],
            "validated_samples": [
                {
                    "validated_at": "2026-07-06",
                    "title_pattern": "\u3010HR\u3011\u8865\u7b7e\u7533\u8bf7\u5355-*",
                    "node_policy": "approve",
                    "business_form_detected": False,
                    "required_business_prefill": [],
                    "verification": "pending_disappearance",
                }
            ],
        },
    ),
    MatterTargetSpec(
        matter_id="matter-business-trip-request",
        name="\u3010HR\u3011\u51fa\u5dee\u7533\u8bf7\u5355",
        subject="\u51fa\u5dee\u7533\u8bf7\u5355",
        category_tag="HR",
        aliases=("\u51fa\u5dee", "\u51fa\u5dee\u7533\u8bf7", "\u51fa\u5dee\u7533\u8bf7\u5355"),
        template_title="\u3010HR\u3011\u51fa\u5dee\u7533\u8bf7\u5355",
        recommended_fields=("content_coll",),
        launch_profile={
            "profile_id": "workflow.business_trip.launch.v1",
            "profile_status": "draft_profile_ready",
            "business_intent": "start_business_trip_request",
            "user_facing_action": "start_business_trip_request",
            "execution_route": "matter_launch_save_draft",
            "binding": "collaboration.launch.save_draft",
            "verification_method": "launch_draft_ack",
            "required_prefill": ["content_coll"],
            "default_field_values": {"content_coll": "Draft note"},
            "validated_samples": [
                {
                    "validated_at": "2026-07-07",
                    "title_pattern": "\u3010HR\u3011\u51fa\u5dee\u7533\u8bf7\u5355",
                    "field_policy": "outer_collaboration_shell",
                    "submit_policy": "draft_only",
                    "verification": "launch_dry_run",
                }
            ],
        },
    ),
    MatterTargetSpec(
        matter_id="matter-meeting-create",
        name="\u65b0\u5efa\u4f1a\u8bae",
        subject="\u65b0\u5efa\u4f1a\u8bae",
        category_tag="\u4f1a\u8bae",
        aliases=("\u4f1a\u8bae", "\u65b0\u5efa\u4f1a\u8bae", "\u53d1\u8d77\u4f1a\u8bae"),
        launch_url=MEETING_CREATE_URL,
        launch_type="fixed_url",
        recommended_fields=("title", "mtTitle"),
    ),
)


def enrich_with_target_matters(matters: list[dict[str, Any]], templates: list[Any]) -> list[dict[str, Any]]:
    if not templates:
        return matters
    enriched = [dict(matter) for matter in matters]
    for spec in FIRST_BATCH_TARGETS:
        seed = _seed_matter(spec, templates)
        existing = _find_existing_target(enriched, seed, spec)
        if existing is None:
            enriched.append(seed)
            continue
        _merge_target_seed(existing, seed)
    return enriched


def _seed_matter(spec: MatterTargetSpec, templates: list[Any]) -> dict[str, Any]:
    template, candidates = _match_target_template(spec, templates)
    match_status = "fixed_launch" if spec.launch_url else "matched" if template else "unmatched"
    launch_entry = {
        "type": spec.launch_type,
        "url": spec.launch_url,
    }
    if template:
        launch_entry = {
            "type": "template",
            "template_id": str(template.get("template_id") or ""),
            "url": str(template.get("href") or ""),
        }
    available_actions = _target_available_actions(bool(template or spec.launch_url))
    return {
        "matter_id": spec.matter_id,
        "name": spec.name,
        "subject": spec.subject,
        "category_tag": spec.category_tag,
        "aliases": list(spec.aliases),
        "catalog_source": "target_seed",
        "target_status": "first_batch",
        "matter_kind": "meeting" if spec.launch_url else "workflow_template",
        "frequency": {"count": 0, "share": 0, "kinds": [], "date_range": {"start": "", "end": ""}},
        "template_match_status": match_status,
        "template": template,
        "template_candidates": candidates,
        "launch_entry": launch_entry,
        "recommended_fields": list(spec.recommended_fields),
        "launch_workflow_profile": spec.launch_profile or {},
        "received_workflow_profile": spec.received_profile or {},
        "sample_items": [],
        "available_actions": available_actions,
    }


def _match_target_template(spec: MatterTargetSpec, templates: list[Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not spec.template_title:
        return {}, []
    candidates = []
    wanted = _normal(spec.template_title)
    aliases = [_normal(value) for value in (spec.subject, *spec.aliases) if value]
    for item in templates:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("subject") or "")
        normalized_title = _normal(title)
        if not normalized_title:
            continue
        score = 0.0
        evidence = []
        if normalized_title == wanted:
            score = 1.0
            evidence.append("exact_target_title")
        elif wanted and (wanted in normalized_title or normalized_title in wanted):
            score = 0.92
            evidence.append("contains_target_title")
        else:
            for alias in aliases:
                if alias and alias in normalized_title:
                    score = max(score, 0.8)
                    evidence.append("alias")
        if score <= 0:
            continue
        candidates.append(
            {
                "template_id": str(item.get("template_id") or ""),
                "title": title,
                "href": str(item.get("href") or ""),
                "score": score,
                "evidence": evidence,
            }
        )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    if not candidates or candidates[0]["score"] < 0.9:
        return {}, candidates[:3]
    top_id = candidates[0]["template_id"]
    for item in templates:
        if isinstance(item, dict) and str(item.get("template_id") or "") == top_id:
            return {
                "template_id": top_id,
                "title": str(item.get("title") or ""),
                "href": str(item.get("href") or ""),
                "score": candidates[0]["score"],
            }, candidates[:3]
    return {}, candidates[:3]


def _find_existing_target(matters: list[dict[str, Any]], seed: dict[str, Any], spec: MatterTargetSpec) -> dict[str, Any] | None:
    seed_template_id = str((seed.get("template") or {}).get("template_id") or "")
    names = {_normal(spec.name), _normal(spec.subject), *(_normal(alias) for alias in spec.aliases)}
    for matter in matters:
        if str(matter.get("matter_id") or "") == spec.matter_id:
            return matter
        template = matter.get("template") if isinstance(matter.get("template"), dict) else {}
        if seed_template_id and str(template.get("template_id") or "") == seed_template_id:
            return matter
        matter_names = {_normal(matter.get("name")), _normal(matter.get("subject"))}
        if any(name and other and (name == other or name in other or other in name) for name in names for other in matter_names):
            return matter
    return None


def _merge_target_seed(existing: dict[str, Any], seed: dict[str, Any]) -> None:
    existing["catalog_source"] = "history+target_seed"
    existing["target_status"] = "first_batch"
    existing["aliases"] = sorted(set(existing.get("aliases") or []) | set(seed.get("aliases") or []))
    existing["recommended_fields"] = sorted(set(existing.get("recommended_fields") or []) | set(seed.get("recommended_fields") or []))
    if not existing.get("template") and seed.get("template"):
        existing["template"] = seed["template"]
        existing["template_match_status"] = seed.get("template_match_status", "matched")
    if seed.get("launch_entry"):
        existing["launch_entry"] = seed["launch_entry"]
    if seed.get("launch_workflow_profile"):
        existing["launch_workflow_profile"] = seed["launch_workflow_profile"]
    if seed.get("received_workflow_profile"):
        existing["received_workflow_profile"] = seed["received_workflow_profile"]
    if not existing.get("matter_kind"):
        existing["matter_kind"] = seed.get("matter_kind", "")
    if not any(isinstance(action, dict) and action.get("status") == "available" for action in existing.get("available_actions") or []):
        existing["available_actions"] = seed.get("available_actions", [])


def _target_available_actions(launch_available: bool) -> list[dict[str, Any]]:
    status = "available" if launch_available else "blocked"
    actions = [
        {
            "name": "launch.save_draft",
            "command": "launch_save_draft",
            "status": status,
            "access": "write",
            "risk": "medium",
            "requires_confirmation": True,
            "description": "Create or update an OA launch-page draft for this matter.",
        },
        {
            "name": "launch.dry_run",
            "command": "launch_dry_run",
            "status": status,
            "access": "read",
            "risk": "low",
            "requires_confirmation": False,
            "description": "Validate launch-page fields before saving a draft.",
        },
    ]
    if not launch_available:
        for action in actions:
            action["reason"] = "target matter launch entry has not been resolved"
    return actions


def _normal(value: Any) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").lower(), flags=re.UNICODE)
