"""Closed, versioned profiles for trusted single-decision business pages."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True)
class BusinessDraftProfile:
    profile_id: str
    prepare: str
    commit: str
    schema_version: str
    fields: tuple[str, ...]
    view_kind: str
    title: str
    submit_label: str
    version: str = "1"

    @property
    def marker(self):
        return {"id": self.profile_id, "version": self.version}

    @property
    def recheck(self):
        return self.view_kind in {"meeting_room", "approval"}

    def ui(self, schema):
        fields = [{key: value for key, value in item.items()
                   if key in {"name", "label", "control", "required", "max_length", "rows", "minimum", "maximum", "step", "autocomplete"}}
                  for item in schema["fields"] if item["name"] in self.fields]
        return {"profileId": self.profile_id, "profileVersion": self.version,
                "viewKind": self.view_kind, "title": self.title,
                "submitLabel": self.submit_label, "effect": schema.get("effect", ""),
                "fields": fields, "review": deepcopy(schema.get("review"))}


PROFILES = (
    BusinessDraftProfile("taihua.work_log.create", "taihua.work_log.create.prepare", "taihua.work_log.create",
        "agentbridge.taihua_work_log_fields.v1", ("log_date", "hours", "project", "content"), "work_log", "填写并核对工作日志", "确认并提交日志"),
    BusinessDraftProfile("oa.meeting_room.application.create", "oa.meeting_room.application.prepare", "oa.meeting_room.application.create",
        "agentbridge.oa_meeting_room_application_fields.v1", ("purpose", "room", "start_time", "end_time"), "meeting_room", "申请会议室", "确认并申请会议室"),
    BusinessDraftProfile("oa.leave.approval", "oa.leave.approval.prepare", "oa.leave.approve",
        "agentbridge.oa_leave_approval_fields.v1", ("opinion",), "approval", "核对请假申请并审批", "确认并审批通过"),
)
BY_PREPARE = {profile.prepare: profile for profile in PROFILES}


def profile_for_source(source):
    # Import only on use to keep profile registration independent of the ledger.
    from agentbridge.core.business_drafts import BusinessDraftError
    schema = source.get("form_schema") or {}
    profile = BY_PREPARE.get(source.get("capability_name"))
    if not profile or schema.get("_agentbridge_business_draft") != "v1":
        raise BusinessDraftError("DRAFT_UNSUPPORTED", "当前事项使用原可信办理流程。", status=404)
    marker = schema.get("_agentbridge_business_draft_profile")
    legacy = marker is None and profile.view_kind == "work_log"
    if (not legacy and marker != profile.marker) or schema.get("schema_version") != profile.schema_version:
        raise BusinessDraftError("DRAFT_PROFILE_VERSION_UNSUPPORTED", "办理页面版本已变化，请从原任务重新准备。", status=409)
    if {field.get("name") for field in schema.get("fields", [])} != set(profile.fields):
        raise BusinessDraftError("DRAFT_PROFILE_INVALID", "办理页面字段与业务合同不匹配。", status=409)
    return profile


def room_choice(room_id, name):
    identity = json.dumps([str(room_id), str(name)], ensure_ascii=False, separators=(",", ":"))
    return {"id": str(room_id), "value": "room:" + hashlib.sha256(identity.encode()).hexdigest(),
            "label": str(name), "available": True}


def meeting_times(values):
    from datetime import datetime, timedelta
    from agentbridge.core.business_drafts import BusinessDraftError
    errors, parsed = {}, {}
    for name in ("start_time", "end_time"):
        try:
            value = datetime.fromisoformat(values.get(name, "").strip())
            if value.tzinfo is not None or value.second or value.microsecond:
                raise ValueError()
            parsed[name] = value
        except (ValueError, TypeError):
            errors[name] = "请选择有效的日期和时间（精确到分钟）。"
    if not errors and (parsed["end_time"] <= parsed["start_time"] or parsed["end_time"] - parsed["start_time"] > timedelta(days=7)):
        errors["end_time"] = "结束时间须晚于开始时间，且申请时长不能超过 7 天。"
    if errors:
        raise BusinessDraftError("DRAFT_FIELDS_INVALID", "请检查标记的字段。", status=422, field_errors=errors)
    return {key: value.strftime("%Y-%m-%d %H:%M") for key, value in parsed.items()}


def review_payload(profile, prepared):
    plan, summary = prepared["plan"], deepcopy(prepared["summary"])
    exact = plan["exact_input"]
    if profile.view_kind == "work_log":
        summary.update(project=({"id": exact["project_id"], "name": exact["project_name"], "code": exact.get("project_code")}
                                if exact.get("project_id") is not None else None),
            duplicateCheck={"state": "no_match", "message": "未发现日期、工时、项目和内容完全相同的日志",
                            "checkedAt": plan["preconditions"]["checked_at"], "sameDateLogCount": plan["preconditions"]["same_date_log_count"]})
        semantic = {"exact_input": exact, "effect": plan["expected_effect"], "same_date_log_count": plan["preconditions"]["same_date_log_count"]}
    else:
        semantic = {"profile": profile.marker, "target": plan["target"], "exact_input": exact,
                    "effect": plan["expected_effect"], "action_contract": plan["action_contract"]}
        # Authorization instructions are present even when evidence is unchanged.
        # Use the executor's explicit source fingerprint comparison only.
        summary["reviewChanged"] = prepared.get("review_changed") is True
    return summary, semantic
