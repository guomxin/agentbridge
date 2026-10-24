"""Closed identities for recovering input content, never execution authority.

New submissions freeze the originating principal and, where the adapter can
prove it, an existing business instance. Creation forms remain task-scoped.
Restore callers still authenticate the new source and perform a fresh read.
"""
from __future__ import annotations

import hashlib
import json


def principal_binding(session):
    expected = str(session.get("expected_principal_ref") or "").strip()
    observed = str(session.get("downstream_principal_ref") or "").strip()
    system = str(session.get("system_id") or "").strip()
    if not system or not expected or expected != observed:
        return None
    value = {"purpose": "private-business-content-principal-v1", "system": system, "principal": expected}
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _keys(profile):
    if profile.view_kind == "approval":
        keys = ("affair_id", "summary_id", "process_id", "template_id", "form_app_id", "form_record_id", "node_policy")
        return keys if profile.review_kind == "missed_punch" else keys + ("body_type",)
    return {
        "meeting_reply": ("affair_id", "meeting_id"),
        "workflow_revoke": ("affair_id", "summary_id", "process_id", "body_type", "template_id", "form_app_id", "form_record_id"),
        "meeting_room_cancel": ("application_id", "room_id"),
        "alarm_remark": ("alarmId", "rtuId", "deviceCode"),
    }.get(profile.review_kind)


def has_instance_identity(profile):
    return _keys(profile) is not None


def identity_from_target(profile, target):
    keys = _keys(profile)
    if keys is None or not isinstance(target, dict) or any(key not in target for key in keys):
        return None
    values = {}
    for key in keys:
        value = target[key]
        if value is None:
            value = ""
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        value = str(value)
        if len(value) > 256 or any(ord(char) < 32 for char in value):
            return None
        values[key] = value
    required = {
        "meeting_room_cancel": ("application_id", "room_id"),
        "alarm_remark": ("alarmId", "rtuId"),
        "meeting_reply": ("affair_id", "meeting_id"),
    }.get(profile.review_kind, ("affair_id", "summary_id", "process_id"))
    if any(not values.get(key) for key in required):
        return None
    if profile.view_kind == "approval" and not values.get("node_policy"):
        return None
    if "profile" in target and target["profile"] != profile.review_kind:
        return None
    return {"version": "1", "kind": profile.profile_id, "identity": values}


def validated_identity(profile, value):
    if (not isinstance(value, dict) or set(value) != {"version", "kind", "identity"}
            or value["version"] != "1" or value["kind"] != profile.profile_id
            or not isinstance(value["identity"], dict)):
        return None
    canonical = identity_from_target(profile, value["identity"])
    return canonical if canonical == value else None


def read_content_identity(adapter, worker, *, profile, arguments, preflight_result=None):
    """Read one exact existing instance with its original eligibility checks.

Only frozen target arguments are used. This never supplies synthetic opinions,
creates a plan/authorization, calls a commit, or treats saved content as consent.
An existing preflight result avoids reading the same materials a second time.
"""
    if not has_instance_identity(profile):
        return None
    if profile.view_kind == "approval" or profile.review_kind == "meeting_reply":
        if preflight_result is None:
            from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS, resolve_write_function
            from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
            definition = _TRUSTED_WRITE_DEFINITIONS[profile.prepare]
            preflight = resolve_write_function(definition["preflight_function"])
            preflight_result = preflight(adapter, worker, arguments, definition["preflight_profile"])
            review = (preflight_result or {}).get("review")
            if not isinstance(review, dict):
                raise BusinessDraftError("DRAFT_MATERIALS_INCOMPLETE", "当前材料尚不完整，请先重新取得完整材料。", status=409)
            BusinessDraftService._require_complete_review(review)
        target = (preflight_result or {}).get("_content_target")
    elif profile.review_kind == "workflow_revoke":
        from agentbridge.adapters.seeyon_workflow_revoke import (
            _bounded_identifier, _resolve_collection_row, _validate_revoke_target,
            _check_revoke_eligibility, _frozen_target,
        )
        affair = _bounded_identifier(arguments.get("affair_id"), "affair_id")
        row, page = _resolve_collection_row(adapter, worker, collection="sent", affair_id=affair)
        _validate_revoke_target(row)
        _check_revoke_eligibility(page, row)
        target = _frozen_target(row)
    elif profile.review_kind == "meeting_room_cancel":
        from agentbridge.adapters.seeyon_meeting_room_application import (
            _bounded_text, _resolve_application, _assert_cancelable, _frozen_application_target,
        )
        application = _bounded_text(arguments.get("application_id"), "application_id", 128)
        raw = _resolve_application(worker, adapter, application)
        _assert_cancelable(raw)
        target = _frozen_application_target(raw)
    else:
        from agentbridge.adapters.smartlight import _normalize_smartlight_alarm_id
        alarm = _normalize_smartlight_alarm_id(arguments)
        target = adapter.alarm_remark_snapshot(worker, alarm)
    return identity_from_target(profile, target)
