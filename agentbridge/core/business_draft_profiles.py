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
    action_kind: str = "approval"
    review_kind: str = ""
    entry_label: str = "核对办理内容"
    effect: str = ""

    @property
    def marker(self):
        return {"id": self.profile_id, "version": self.version}

    @property
    def recheck(self):
        return self.view_kind != "work_log"

    @property
    def identity_fields(self):
        if self.view_kind == "approval" or self.review_kind == "workflow_revoke":
            return ("affair_id", "summary_id", "process_id", "template_id", "form_app_id", "form_record_id", "profile")
        if self.review_kind == "meeting_room_cancel":
            return ("application_id", "room_id")
        if self.review_kind == "alarm_remark":
            return ("alarmId", "rtuId", "deviceCode")
        if self.review_kind in {"business_trip", "leave", "missed_punch"}:
            return ("template_id", "form_app_id")
        return ()

    def presentation(self):
        """Static host hints only; business values remain on the trusted page."""
        return {"schemaVersion": "agentbridge.business-view.v1",
                "profileId": self.profile_id, "profileVersion": self.version,
                "viewKind": self.view_kind,
                "entryLabel": self.entry_label,
                "submitLabel": self.submit_label}

    def ui(self, schema):
        fields = [{key: value for key, value in item.items()
                   if key in {"name", "label", "control", "required", "max_length", "rows", "minimum", "maximum", "step", "autocomplete", "options", "value_type"}}
                  for item in schema["fields"] if item["name"] in self.fields]
        return {"profileId": self.profile_id, "profileVersion": self.version,
                "viewKind": self.view_kind, "title": self.title,
                "submitLabel": self.submit_label, "effect": self.effect or schema.get("effect", ""),
                "systemName": schema.get("system", ""), "actionKind": self.action_kind,
                "reviewKind": self.review_kind,
                "fields": fields, "review": deepcopy(schema.get("review"))}


# These names are deliberately closed. New write capabilities require an explicit
# action, field and review contract instead of automatically acquiring a finalizer.
_PROFILE_CONTRACTS = (
    ("taihua.work_log.create.prepare", "taihua.work_log.create", "work_log", "work_log", "create", "填写并核对工作日志", "确认并提交日志", "填写工作日志", "向泰华日志系统新增这一条工作日志。"),
    ("oa.meeting_room.application.prepare", "oa.meeting_room.application.create", "meeting_room", "meeting_room_application", "create", "申请会议室", "确认并申请会议室", "选择会议室", "提交这一项会议室申请，不创建或发送会议。"),
    ("oa.meeting.create.prepare", "oa.meeting.create", "meeting_room", "meeting", "create", "创建并发送会议", "确认并创建发送会议", "安排会议", "创建并发送会议，参会范围为当前 OA 用户。"),
    ("oa.meeting_room.application.cancel.prepare", "oa.meeting_room.application.cancel", "form", "meeting_room_cancel", "cancel", "撤销会议室申请", "确认并撤销申请", "核对撤销申请", "撤销这一条符合条件的会议室申请。"),
    ("oa.workflow.revoke.prepare", "oa.workflow.revoke", "form", "workflow_revoke", "revoke", "撤销已发流程", "确认并撤销流程", "核对流程撤销", "撤销这一条进行中的已发流程并退回待发，可能通知处理人并保留审计记录。"),
    ("oa.business_trip.prepare", "oa.business_trip.save_draft", "form", "business_trip", "save_draft", "保存出差申请草稿", "确认并保存草稿", "填写出差草稿", "保存出差申请草稿，不发起审批。"),
    ("oa.business_trip.submit.prepare", "oa.business_trip.submit", "form", "business_trip", "submit", "提交出差申请", "确认并提交申请", "填写出差申请", "正式提交出差申请并发起审批。"),
    ("oa.leave.prepare", "oa.leave.save_draft", "form", "leave", "save_draft", "保存请假申请草稿", "确认并保存草稿", "填写请假草稿", "保存请假申请草稿，不发起审批。"),
    ("oa.leave.submit.prepare", "oa.leave.submit", "form", "leave", "submit", "提交请假申请", "确认并提交申请", "填写请假申请", "正式提交请假申请并发起审批。"),
    ("oa.missed_punch.prepare", "oa.missed_punch.save_draft", "form", "missed_punch", "save_draft", "保存补签申请草稿", "确认并保存草稿", "填写补签草稿", "保存补签申请草稿，不发起审批。"),
    ("smartlight.alarm.remark.update.prepare", "smartlight.alarm.remark.update", "form", "alarm_remark", "update", "修改告警备注", "确认并更新备注", "编辑告警备注", "更新这一条告警的备注，留空会清除原备注。"),
)
_PENDING_CONTRACTS = (
    ("missed_punch", "补签申请"), ("efficiency_data", "效能数据"),
    ("travel_expense", "差旅费报销"), ("business_trip", "出差申请"),
    ("labor_contract_renewal", "劳动合同续签"), ("intellectual_property_declaration", "知识产权申报"),
    ("overtime", "加班申请"), ("leave", "请假申请"), ("resignation", "离职申请"),
    ("work_handover", "工作交接"), ("flight_application", "乘坐飞机申请"),
    ("standard_collaboration", "普通协同事项"),
)
BATCH_PREPARES = frozenset({"oa.workflow.pending.batch.prepare", "oa.missed_punch.approval.batch.prepare"})


def _registered_profiles():
    from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS
    contracts = list(_PROFILE_CONTRACTS)
    for kind, name in _PENDING_CONTRACTS:
        contracts.append((f"oa.{kind}.approval.prepare", f"oa.{kind}.approval", "approval", kind,
                          "approval", f"核对{name}并审批", "确认并审批通过", f"核对{name}", f"审批通过这一条{name}。"))
    contracts.extend((
        ("oa.attendance_confirmation.prepare", "oa.attendance_confirmation", "approval", "attendance_confirmation", "confirmation", "核对月度考勤并确认", "确认并提交考勤单", "核对月度考勤", "按单据已有的考勤结果提交本次确认及处理意见，不改变只读的异议选择。"),
        ("oa.weekly_report.acknowledgement.prepare", "oa.weekly_report.acknowledgement", "approval", "weekly_report", "acknowledgement", "核对周报并阅办", "确认并阅办知会", "核对周报", "记录当前用户对这一条周报的阅办知会。"),
        ("oa.quarterly_performance.acknowledgement.prepare", "oa.quarterly_performance.acknowledgement", "approval", "quarterly_performance", "acknowledgement", "核对季度绩效并阅办", "确认并阅办知会", "核对季度绩效", "记录当前用户对这一条季度绩效汇总的阅办知会，不调整任何评级。"),
        ("oa.intellectual_property_reward.acknowledgement.prepare", "oa.intellectual_property_reward.acknowledgement", "approval", "intellectual_property_reward", "acknowledgement", "核对知识产权奖励并阅办", "确认并阅办知会", "核对知识产权奖励", "记录当前用户对这一条知识产权奖励单的阅办知会，不调整金额或人员。"),
    ))
    result = []
    for prepare, identity, view, review, action, title, label, entry, effect in contracts:
        definition = _TRUSTED_WRITE_DEFINITIONS[prepare]
        schema = definition["field_schema"]
        result.append(BusinessDraftProfile(identity, prepare, definition["commit_capability"], schema["schema_version"],
            tuple(field["name"] for field in schema["fields"]), view, title, label,
            action_kind=action, review_kind=review, entry_label=entry, effect=effect))
    return tuple(result)


PROFILES = _registered_profiles()
BY_PREPARE = {profile.prepare: profile for profile in PROFILES}
BY_ID = {profile.profile_id: profile for profile in PROFILES}


def profile_for_definition(prepare_capability, definition):
    if prepare_capability in BATCH_PREPARES:
        return next((profile for profile in PROFILES if profile.commit == definition.get("commit_capability")
                     and profile.view_kind == "approval"), None)
    return BY_PREPARE.get(prepare_capability)


def profile_for_source(source):
    # Import only on use to keep profile registration independent of the ledger.
    from agentbridge.core.business_drafts import BusinessDraftError
    schema = source.get("form_schema") or {}
    profile = (BY_ID.get((schema.get("_agentbridge_business_draft_profile") or {}).get("id"))
               if source.get("capability_name") in BATCH_PREPARES else BY_PREPARE.get(source.get("capability_name")))
    if profile and source.get("capability_name") in BATCH_PREPARES and profile.view_kind != "approval":
        profile = None
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
                    "effect": plan["expected_effect"], "action_contract": plan.get("action_contract"), "form_contract": plan.get("form_contract"),
                    "exact_payload": {key: value for key, value in (plan.get("exact_payload") or {}).items() if key != "createTime"},
                    "preconditions": {key: value for key, value in plan.get("preconditions", {}).items() if key not in {"checked_at", "prepared_at", "frame_module_id"}},
                    "materials": {key: summary[key] for key in ("sections", "attachments", "completeness", "truncated", "materialsVersion") if key in summary}}
        # Authorization instructions are present even when evidence is unchanged.
        # Use the executor's explicit source fingerprint comparison only.
        summary["reviewChanged"] = prepared.get("review_changed") is True
    return summary, deepcopy(semantic)
