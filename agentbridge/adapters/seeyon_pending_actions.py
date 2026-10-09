from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from typing import Any, Callable

from agentbridge.adapters.seeyon_business_trip import (
    BUSINESS_TRIP_FORM_APP_ID,
    BUSINESS_TRIP_TEMPLATE_ID,
    BUSINESS_TRIP_TRAVEL_MODES,
)
from agentbridge.adapters.seeyon_flight_application import (
    flight_application_business_snapshot, flight_application_summary_fields,
)
from agentbridge.adapters.seeyon_work_handover import (
    work_handover_business_snapshot, work_handover_summary_fields,
)


EFFICIENCY_DATA_APPROVAL_PREPARE_CAPABILITY = (
    "oa.efficiency_data.approval.prepare"
)
EFFICIENCY_DATA_APPROVE_CAPABILITY = "oa.efficiency_data.approve"
TRAVEL_EXPENSE_APPROVAL_PREPARE_CAPABILITY = (
    "oa.travel_expense.approval.prepare"
)
TRAVEL_EXPENSE_APPROVE_CAPABILITY = "oa.travel_expense.approve"
BUSINESS_TRIP_APPROVAL_PREPARE_CAPABILITY = (
    "oa.business_trip.approval.prepare"
)
BUSINESS_TRIP_APPROVE_CAPABILITY = "oa.business_trip.approve"
LABOR_CONTRACT_RENEWAL_APPROVAL_PREPARE_CAPABILITY = (
    "oa.labor_contract_renewal.approval.prepare"
)
LABOR_CONTRACT_RENEWAL_APPROVE_CAPABILITY = "oa.labor_contract_renewal.approve"
INTELLECTUAL_PROPERTY_DECLARATION_APPROVAL_PREPARE_CAPABILITY = (
    "oa.intellectual_property_declaration.approval.prepare"
)
INTELLECTUAL_PROPERTY_DECLARATION_APPROVE_CAPABILITY = (
    "oa.intellectual_property_declaration.approve"
)
OVERTIME_APPROVAL_PREPARE_CAPABILITY = "oa.overtime.approval.prepare"
OVERTIME_APPROVE_CAPABILITY = "oa.overtime.approve"
LEAVE_APPROVAL_PREPARE_CAPABILITY = "oa.leave.approval.prepare"
LEAVE_APPROVE_CAPABILITY = "oa.leave.approve"
RESIGNATION_APPROVAL_PREPARE_CAPABILITY = "oa.resignation.approval.prepare"
RESIGNATION_APPROVE_CAPABILITY = "oa.resignation.approve"
WORK_HANDOVER_APPROVAL_PREPARE_CAPABILITY = "oa.work_handover.approval.prepare"
WORK_HANDOVER_APPROVE_CAPABILITY = "oa.work_handover.approve"
FLIGHT_APPLICATION_APPROVAL_PREPARE_CAPABILITY = "oa.flight_application.approval.prepare"
FLIGHT_APPLICATION_APPROVE_CAPABILITY = "oa.flight_application.approve"
ATTENDANCE_CONFIRMATION_PREPARE_CAPABILITY = (
    "oa.attendance_confirmation.prepare"
)
ATTENDANCE_CONFIRM_CAPABILITY = "oa.attendance_confirmation.confirm"
WEEKLY_REPORT_ACKNOWLEDGEMENT_PREPARE_CAPABILITY = (
    "oa.weekly_report.acknowledgement.prepare"
)
WEEKLY_REPORT_ACKNOWLEDGE_CAPABILITY = "oa.weekly_report.acknowledge"
STANDARD_COLLABORATION_APPROVAL_PREPARE_CAPABILITY = (
    "oa.standard_collaboration.approval.prepare"
)
STANDARD_COLLABORATION_APPROVE_CAPABILITY = (
    "oa.standard_collaboration.approve"
)

PENDING_ACTION_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "affair_id": {"type": "string"},
        "opinion": {"type": "string", "maxLength": 1000},
        "input_submission_id": {"type": "string"},
    },
    "required": ["affair_id"],
    "additionalProperties": False,
}

PENDING_ACTION_COMMIT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"authorization_id": {"type": "string"}},
    "required": ["authorization_id"],
    "additionalProperties": False,
}


def _opinion_card(
    *,
    schema_version: str,
    title: str,
    effect: str,
    submit_label: str,
) -> dict:
    return {
        "schema_version": schema_version,
        "title": title,
        "system": "致远 OA",
        "effect": effect,
        "submit_label": submit_label,
        "notice": (
            "提交意见后还需在独立授权卡中核对精确事项；"
            "授权前不会处理待办。"
        ),
        "fields": [
            {
                "name": "opinion",
                "label": "处理意见",
                "control": "textarea",
                "required": True,
                "max_length": 1000,
                "rows": 4,
            }
        ],
    }


EFFICIENCY_DATA_APPROVAL_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_efficiency_data_approval_fields.v1",
    title="填写效能数据审批意见",
    effect="审批通过一条效能数据流程",
    submit_label="提交审批意见",
)
TRAVEL_EXPENSE_APPROVAL_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_travel_expense_approval_fields.v1",
    title="填写差旅费审批意见",
    effect="审批通过一条差旅费审批报销单",
    submit_label="提交审批意见",
)
BUSINESS_TRIP_APPROVAL_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_business_trip_approval_fields.v1",
    title="填写出差申请审批意见",
    effect="审批通过一条出差申请单",
    submit_label="提交审批意见",
)
LABOR_CONTRACT_RENEWAL_APPROVAL_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_labor_contract_renewal_approval_fields.v1",
    title="填写劳动合同续签审批意见",
    effect="审批通过一条劳动合同续签表",
    submit_label="提交审批意见",
)
INTELLECTUAL_PROPERTY_DECLARATION_APPROVAL_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_intellectual_property_declaration_approval_fields.v1",
    title="填写知识产权申报审批意见",
    effect="审批通过一条知识产权申报审批单",
    submit_label="提交审批意见",
)
OVERTIME_APPROVAL_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_overtime_approval_fields.v1",
    title="填写加班申请审批意见",
    effect="审批通过一条加班申请审核单",
    submit_label="提交审批意见",
)
LEAVE_APPROVAL_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_leave_approval_fields.v1",
    title="填写请假申请审批意见",
    effect="审批通过一条请假申请单",
    submit_label="提交审批意见",
)
RESIGNATION_APPROVAL_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_resignation_approval_fields.v1",
    title="填写离职申请审批意见",
    effect="审批通过一条离职申请单",
    submit_label="提交审批意见",
)
WORK_HANDOVER_APPROVAL_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_work_handover_approval_fields.v1",
    title="填写工作交接审批意见",
    effect="审批通过一条工作交接单",
    submit_label="提交审批意见",
)
FLIGHT_APPLICATION_APPROVAL_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_flight_application_approval_fields.v1",
    title="填写乘坐飞机申请审批意见",
    effect="审批通过一条乘坐飞机申请单",
    submit_label="提交审批意见",
)
ATTENDANCE_CONFIRMATION_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_attendance_confirmation_fields.v1",
    title="填写月度考勤确认意见",
    effect="确认并提交一张月度考勤确认单",
    submit_label="提交确认意见",
)
WEEKLY_REPORT_ACKNOWLEDGEMENT_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_weekly_report_acknowledgement_fields.v1",
    title="填写周报阅办意见",
    effect="阅办一条周报发送流程",
    submit_label="提交阅办意见",
)
STANDARD_COLLABORATION_APPROVAL_FIELD_CARD_SCHEMA = _opinion_card(
    schema_version="agentbridge.oa_standard_collaboration_approval_fields.v1",
    title="填写普通协同事项审批意见",
    effect="审批通过一条普通协同事项",
    submit_label="提交审批意见",
)


_PROFILES = {
    "efficiency_data": {
        "prepare_capability": EFFICIENCY_DATA_APPROVAL_PREPARE_CAPABILITY,
        "commit_capability": EFFICIENCY_DATA_APPROVE_CAPABILITY,
        "contract_version": "seeyon-efficiency-data-approval-v1",
        "plan_schema": "agentbridge.oa_efficiency_data_approval_plan.v1",
        "result_schema": "agentbridge.oa_efficiency_data_approval_result.v1",
        "business_intent": "approve_efficiency_data",
        "title_rule": {"kind": "contains", "value": "效能数据"},
        "required_fields": {"接收人"},
        "allowed_fields": {"接收人"},
        "template_id": "",
        "form_app_id": "",
        "node_policies": {"approve", "审批"},
        "node_policy_names": {"审批"},
        "action_kind": "approval",
        "attitude_code": "agree",
        "action_display": "审批通过",
        "summary_title": "审批效能数据流程",
        "summary_effect": "审批通过后该效能数据事项将离开待办列表",
        "authorize_label": "授权审批通过",
    },
    "travel_expense": {
        "prepare_capability": TRAVEL_EXPENSE_APPROVAL_PREPARE_CAPABILITY,
        "commit_capability": TRAVEL_EXPENSE_APPROVE_CAPABILITY,
        "contract_version": "seeyon-travel-expense-approval-v1",
        "plan_schema": "agentbridge.oa_travel_expense_approval_plan.v1",
        "result_schema": "agentbridge.oa_travel_expense_approval_result.v1",
        "business_intent": "approve_travel_expense_reimbursement",
        "title_rule": {
            "kind": "prefix",
            "value": "【报销】差旅费审批报销单-",
        },
        "required_fields": {
            "流水号",
            "姓名",
            "关联出差申请单",
            "应付金额合计",
            "收款账号",
        },
        "allowed_fields": None,
        "template_id": "-2046021869351779722",
        "form_app_id": "-2571419096251022663",
        "node_policies": {"报销审批"},
        "node_policy_names": {"报销审批"},
        "action_kind": "approval",
        "attitude_code": "agree",
        "action_display": "审批通过",
        "summary_title": "审批差旅费报销单",
        "summary_effect": "审批通过后该报销事项将离开待办列表",
        "authorize_label": "授权审批通过",
    },
    "business_trip": {
        "prepare_capability": BUSINESS_TRIP_APPROVAL_PREPARE_CAPABILITY,
        "commit_capability": BUSINESS_TRIP_APPROVE_CAPABILITY,
        "contract_version": "seeyon-business-trip-approval-v1",
        "plan_schema": "agentbridge.oa_business_trip_approval_plan.v1",
        "result_schema": "agentbridge.oa_business_trip_approval_result.v1",
        "business_intent": "approve_business_trip_request",
        "title_rule": {
            "kind": "prefix",
            "value": "【HR】出差申请单-",
        },
        "required_fields": {
            "姓名",
            "出差开始时间",
            "出差始发地",
            "事由",
            "是否存在有非部门经理的直接上级（组负责人）",
        },
        "allowed_fields": None,
        "template_id": BUSINESS_TRIP_TEMPLATE_ID,
        "form_app_id": BUSINESS_TRIP_FORM_APP_ID,
        "node_policies": {"approve", "审批"},
        "node_policy_names": {"审批", "直接上级"},
        "action_kind": "approval",
        "attitude_code": "agree",
        "action_display": "审批通过",
        "summary_title": "审批出差申请单",
        "summary_effect": "审批通过后该出差申请将离开待办列表",
        "authorize_label": "授权审批通过",
        "business_snapshot_policy": "business_trip_approval_v1",
    },
    "labor_contract_renewal": {
        "prepare_capability": LABOR_CONTRACT_RENEWAL_APPROVAL_PREPARE_CAPABILITY,
        "commit_capability": LABOR_CONTRACT_RENEWAL_APPROVE_CAPABILITY,
        "contract_version": "seeyon-labor-contract-renewal-approval-v1",
        "plan_schema": "agentbridge.oa_labor_contract_renewal_approval_plan.v1",
        "result_schema": "agentbridge.oa_labor_contract_renewal_approval_result.v1",
        "business_intent": "approve_labor_contract_renewal",
        "title_rule": {
            "kind": "contains",
            "value": "【HR】劳动合同续签表-",
        },
        "required_fields": {
            "姓名",
            "入职日期",
            "综合评价及指导意见",
            "综合评价",
            "鉴于以上意见，建议",
            "续签情况反馈",
        },
        "allowed_fields": None,
        "template_id": "3868679303223263344",
        "form_app_id": "6514522401641018463",
        "node_policies": {"approve", "审批"},
        "node_policy_names": {"审批"},
        "action_kind": "approval",
        "attitude_code": "agree",
        "action_display": "审批通过",
        "summary_title": "审批劳动合同续签表",
        "summary_effect": "审批通过后该劳动合同续签事项将离开待办列表",
        "authorize_label": "授权审批通过",
        "business_snapshot_policy": "labor_contract_renewal_v1",
    },
    "intellectual_property_declaration": {
        "prepare_capability": (
            INTELLECTUAL_PROPERTY_DECLARATION_APPROVAL_PREPARE_CAPABILITY
        ),
        "commit_capability": INTELLECTUAL_PROPERTY_DECLARATION_APPROVE_CAPABILITY,
        "contract_version": "seeyon-intellectual-property-declaration-approval-v1",
        "plan_schema": (
            "agentbridge.oa_intellectual_property_declaration_approval_plan.v1"
        ),
        "result_schema": (
            "agentbridge.oa_intellectual_property_declaration_approval_result.v1"
        ),
        "business_intent": "approve_intellectual_property_declaration",
        "title_rule": {
            "kind": "prefix",
            "value": "【知识产权】知识产权申报审批单-",
        },
        "required_fields": {
            "申请人",
            "知识产权类型",
            "名称",
            "发明/设计人",
            "权属类型",
            "权属单位",
            "申请用途",
            "申请材料",
        },
        "allowed_fields": None,
        "template_id": "-2986710992990286032",
        "form_app_id": "-6972097064001584076",
        "node_policies": {"approve", "审批"},
        "node_policy_names": {"审批"},
        "action_kind": "approval",
        "attitude_code": "agree",
        "action_display": "审批通过",
        "summary_title": "审批知识产权申报单",
        "summary_effect": "审批通过后该知识产权申报事项将离开待办列表",
        "authorize_label": "授权审批通过",
        "business_snapshot_policy": "intellectual_property_declaration_v1",
    },
    "overtime": {
        "prepare_capability": OVERTIME_APPROVAL_PREPARE_CAPABILITY,
        "commit_capability": OVERTIME_APPROVE_CAPABILITY,
        "contract_version": "seeyon-overtime-approval-v1",
        "plan_schema": "agentbridge.oa_overtime_approval_plan.v1",
        "result_schema": "agentbridge.oa_overtime_approval_result.v1",
        "business_intent": "approve_overtime_request",
        "title_rule": {
            "kind": "prefix",
            "value": "【HR】加班申请审核单-",
        },
        "required_fields": {
            "姓名",
            "申请开始时间",
            "加班事由",
            "是否存在有非部门经理的直接上级（组负责人）",
            "实际开始时间",
        },
        "allowed_fields": None,
        "template_id": "-170938662527873499",
        "form_app_id": "-213895278619572887",
        "node_policies": {"考勤审批"},
        "node_policy_names": {"考勤审批"},
        "action_kind": "approval",
        "attitude_code": "agree",
        "action_display": "审批通过",
        "summary_title": "审批加班申请审核单",
        "summary_effect": "审批通过后该加班申请将离开待办列表",
        "authorize_label": "授权审批通过",
    },
    "leave": {
        "prepare_capability": LEAVE_APPROVAL_PREPARE_CAPABILITY,
        "commit_capability": LEAVE_APPROVE_CAPABILITY,
        "contract_version": "seeyon-leave-approval-v1",
        "plan_schema": "agentbridge.oa_leave_approval_plan.v1",
        "result_schema": "agentbridge.oa_leave_approval_result.v1",
        "business_intent": "approve_leave_request",
        "title_rule": {
            "kind": "prefix",
            "value": "【HR】请假申请单-",
        },
        "required_fields": {
            "姓名",
            "请假类型",
            "开始时间",
            "事由",
            "证明材料",
            "是否存在有非部门经理的直接上级（组负责人）",
        },
        "allowed_fields": None,
        "template_id": "-7765568933726502821",
        "form_app_id": "6773919591095560889",
        "node_policies": {"approve", "审批"},
        "node_policy_names": {"审批"},
        "action_kind": "approval",
        "attitude_code": "agree",
        "action_display": "审批通过",
        "summary_title": "审批请假申请单",
        "summary_effect": "审批通过后该请假申请将离开待办列表",
        "authorize_label": "授权审批通过",
        "business_snapshot_policy": "leave_approval_v1",
    },
    "resignation": {
        "prepare_capability": RESIGNATION_APPROVAL_PREPARE_CAPABILITY,
        "commit_capability": RESIGNATION_APPROVE_CAPABILITY,
        "contract_version": "seeyon-resignation-approval-v1",
        "plan_schema": "agentbridge.oa_resignation_approval_plan.v1",
        "result_schema": "agentbridge.oa_resignation_approval_result.v1",
        "business_intent": "approve_resignation_request",
        "title_rule": {
            "kind": "prefix",
            "value": "【HR】离职申请单-",
        },
        "required_fields": {
            "姓名",
            "入职时间",
            "是否有证书",
            "离职原因",
        },
        "allowed_fields": None,
        "template_id": "3483439346772952417",
        "form_app_id": "9167110384557358951",
        "node_policies": {"approve", "审批"},
        "node_policy_names": {"审批"},
        "action_kind": "approval",
        "attitude_code": "agree",
        "action_display": "审批通过",
        "summary_title": "审批离职申请单",
        "summary_effect": "审批通过后该离职申请将离开待办列表",
        "authorize_label": "授权审批通过",
        "business_snapshot_policy": "resignation_v1",
    },
    "work_handover": {
        "prepare_capability": WORK_HANDOVER_APPROVAL_PREPARE_CAPABILITY,
        "commit_capability": WORK_HANDOVER_APPROVE_CAPABILITY,
        "contract_version": "seeyon-work-handover-approval-v1",
        "plan_schema": "agentbridge.oa_work_handover_approval_plan.v1",
        "result_schema": "agentbridge.oa_work_handover_approval_result.v1",
        "business_intent": "approve_work_handover_request",
        "title_rule": {
            "kind": "optional_auto_prefix",
            "value": "【HR】工作交接单-",
        },
        "required_fields": {"移交人", "交接类别", "工作事项", "交接内容"},
        "allowed_fields": None,
        "template_id": "-1377138047593329703",
        "form_app_id": "5247761788960957082",
        "node_policies": {"approve", "审批"},
        "node_policy_names": {"审批"},
        "action_kind": "approval",
        "attitude_code": "agree",
        "action_display": "审批通过",
        "summary_title": "审批工作交接单",
        "summary_effect": "审批通过后该工作交接将离开待办列表",
        "authorize_label": "授权审批通过",
        "business_snapshot_policy": "work_handover_v1",
    },
    "flight_application": {
        "prepare_capability": FLIGHT_APPLICATION_APPROVAL_PREPARE_CAPABILITY,
        "commit_capability": FLIGHT_APPLICATION_APPROVE_CAPABILITY,
        "contract_version": "seeyon-flight-application-approval-v1",
        "plan_schema": "agentbridge.oa_flight_application_approval_plan.v1",
        "result_schema": "agentbridge.oa_flight_application_approval_result.v1",
        "business_intent": "approve_flight_application",
        "title_rule": {"kind": "prefix", "value": "【综合】乘坐飞机申请单"},
        "required_fields": {"申请人", "出发时间"},
        "allowed_fields": None,
        "template_id": "-2302881233144493310",
        "form_app_id": "2631521264809559746",
        "node_policies": {"approve"},
        "node_policy_names": {"审批"},
        "action_kind": "approval",
        "attitude_code": "agree",
        "action_display": "审批通过",
        "summary_title": "审批乘坐飞机申请单",
        "summary_effect": "审批通过后该乘坐飞机申请将离开待办列表",
        "authorize_label": "授权审批通过",
        "business_snapshot_policy": "flight_application_v1",
    },
    "attendance_confirmation": {
        "prepare_capability": ATTENDANCE_CONFIRMATION_PREPARE_CAPABILITY,
        "commit_capability": ATTENDANCE_CONFIRM_CAPABILITY,
        "contract_version": "seeyon-attendance-confirmation-v1",
        "plan_schema": "agentbridge.oa_attendance_confirmation_plan.v1",
        "result_schema": "agentbridge.oa_attendance_confirmation_result.v1",
        "business_intent": "confirm_monthly_attendance",
        "title_rule": {
            "kind": "contains",
            "value": "【HR】月度考勤确认单-",
        },
        "required_fields": {
            "姓名",
            "应出勤天数",
            "本月考勤是否有异议",
        },
        "allowed_fields": None,
        "template_id": "-7231800401165464345",
        "form_app_id": "5072944770639779741",
        "node_policies": {"sendoredit"},
        "node_policy_names": {"发起人填写"},
        "action_kind": "confirmation",
        "attitude_code": "agree",
        "action_display": "确认并提交",
        "summary_title": "确认月度考勤",
        "summary_effect": "确认后该月度考勤事项将离开待办列表",
        "authorize_label": "授权确认月度考勤",
        "business_snapshot_policy": "attendance_confirmation_v1",
    },
    "weekly_report": {
        "prepare_capability": WEEKLY_REPORT_ACKNOWLEDGEMENT_PREPARE_CAPABILITY,
        "commit_capability": WEEKLY_REPORT_ACKNOWLEDGE_CAPABILITY,
        "contract_version": "seeyon-weekly-report-acknowledgement-v1",
        "plan_schema": "agentbridge.oa_weekly_report_acknowledgement_plan.v1",
        "result_schema": "agentbridge.oa_weekly_report_acknowledgement_result.v1",
        "business_intent": "acknowledge_weekly_report",
        "title_rule": {"kind": "contains", "value": "周报发送流程"},
        "required_fields": {
            "周报名称",
            "年度",
            "本周 工作总结",
            "下周 工作计划",
        },
        "allowed_fields": None,
        "template_id": "1610567580409022440",
        "form_app_id": "-2351708227632217917",
        "node_policies": {"inform", "知会"},
        "node_policy_names": {"知会"},
        "action_kind": "acknowledgement",
        "attitude_code": "",
        "action_display": "阅办知会",
        "summary_title": "阅办周报发送流程",
        "summary_effect": "阅办后该周报事项将离开待办列表",
        "authorize_label": "授权阅办周报",
    },
    "standard_collaboration": {
        "prepare_capability": STANDARD_COLLABORATION_APPROVAL_PREPARE_CAPABILITY,
        "commit_capability": STANDARD_COLLABORATION_APPROVE_CAPABILITY,
        "contract_version": "seeyon-standard-collaboration-approval-v1",
        "plan_schema": "agentbridge.oa_standard_collaboration_approval_plan.v1",
        "result_schema": "agentbridge.oa_standard_collaboration_approval_result.v1",
        "business_intent": "approve_standard_collaboration",
        "title_rule": {"kind": "standard_collaboration", "value": ""},
        "required_fields": set(),
        "allowed_fields": {"接收人"},
        "template_id": "",
        "form_app_id": "",
        "node_policies": {"approve", "审批"},
        "node_policy_names": {"审批"},
        "action_kind": "approval",
        "attitude_code": "agree",
        "action_display": "审批通过",
        "summary_title": "审批普通协同事项",
        "summary_effect": "审批通过后该普通协同事项将离开待办列表",
        "authorize_label": "授权审批通过",
    },
}

PENDING_ACTION_CAPABILITY_DEFINITIONS = tuple(
    {
        "profile": profile_key,
        "prepare_capability": profile["prepare_capability"],
        "commit_capability": profile["commit_capability"],
        "workflow_prefix": profile_key.replace("_", "-"),
        "action_kind": profile["action_kind"],
    }
    for profile_key, profile in _PROFILES.items()
)

_SPECIALIZED_TITLE_MARKERS = (
    "效能数据",
    "周报发送流程",
    "【HR】",
    "【报销】",
    "【采购】",
    "【用印】",
    "【知识产权】",
    "乘坐飞机申请单",
)


class PendingActionContractMismatch(RuntimeError):
    pass


class PendingActionOutcomeUnknown(RuntimeError):
    pass


def prepare_efficiency_data_approval(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(adapter, worker, arguments, "efficiency_data")


def approve_efficiency_data(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="efficiency_data",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_travel_expense_approval(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(adapter, worker, arguments, "travel_expense")


def approve_travel_expense(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="travel_expense",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_business_trip_approval(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(adapter, worker, arguments, "business_trip")


def approve_business_trip_request(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="business_trip",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_labor_contract_renewal_approval(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(
        adapter, worker, arguments, "labor_contract_renewal"
    )


def approve_labor_contract_renewal(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="labor_contract_renewal",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_intellectual_property_declaration_approval(
    adapter, worker, arguments: dict
) -> dict:
    return _prepare_pending_action(
        adapter, worker, arguments, "intellectual_property_declaration"
    )


def approve_intellectual_property_declaration(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="intellectual_property_declaration",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_overtime_approval(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(adapter, worker, arguments, "overtime")


def approve_overtime(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="overtime",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_resignation_approval(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(adapter, worker, arguments, "resignation")


def approve_resignation(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="resignation",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_leave_approval(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(adapter, worker, arguments, "leave")


def approve_leave_request(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="leave",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_work_handover_approval(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(adapter, worker, arguments, "work_handover")


def approve_work_handover(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="work_handover",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_flight_application_approval(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(adapter, worker, arguments, "flight_application")


def approve_flight_application(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="flight_application",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_weekly_report_acknowledgement(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(adapter, worker, arguments, "weekly_report")


def acknowledge_weekly_report(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="weekly_report",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_attendance_confirmation(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(
        adapter, worker, arguments, "attendance_confirmation"
    )


def confirm_attendance(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="attendance_confirmation",
        enter_commit_boundary=enter_commit_boundary,
    )


def prepare_standard_collaboration_approval(adapter, worker, arguments: dict) -> dict:
    return _prepare_pending_action(adapter, worker, arguments, "standard_collaboration")


def approve_standard_collaboration(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
) -> dict:
    return _commit_pending_action(
        adapter,
        worker,
        plan,
        profile_key="standard_collaboration",
        enter_commit_boundary=enter_commit_boundary,
    )


def pending_action_contract_fingerprint(profile_key: str) -> str:
    profile = _profile(profile_key)
    contract = {
        "version": profile["contract_version"],
        "title_rule": profile["title_rule"],
        "required_fields": sorted(profile["required_fields"]),
        "allowed_fields": (
            sorted(profile["allowed_fields"])
            if profile["allowed_fields"] is not None
            else None
        ),
        "template_id": profile["template_id"],
        "form_app_id": profile["form_app_id"],
        "node_policies": sorted(profile["node_policies"]),
        "node_policy_names": sorted(profile["node_policy_names"]),
        "action_kind": profile["action_kind"],
        "attitude_code": profile.get("attitude_code", ""),
        "business_snapshot_policy": profile.get("business_snapshot_policy"),
        "selection_policy": "exactly_one_pending_affair_id",
        "internal_binding": "ContinueSubmit",
        "verification": "pending_disappearance",
    }
    return _fingerprint(contract)


def _prepare_pending_action(
    adapter,
    worker,
    arguments: dict,
    profile_key: str,
) -> dict:
    inputs = _normalize_inputs(arguments)
    profile, source, detail, signals = _resolve_validated_target(
        adapter, worker, inputs["affair_id"], profile_key
    )
    target = _frozen_target(source, detail, signals, profile_key)
    return {
        "plan": {
            "schema_version": profile["plan_schema"],
            "business_intent": profile["business_intent"],
            "target": target,
            "action_contract": {
                "version": profile["contract_version"],
                "fingerprint": pending_action_contract_fingerprint(profile_key),
                "internal_binding": "ContinueSubmit",
                "action_kind": profile["action_kind"],
                "verification": "pending_disappearance",
            },
            "exact_input": {"opinion": inputs["opinion"]},
            "preconditions": {
                "pending_target_resolved": True,
                "workflow_profile_matched": True,
                "target_identity_complete": True,
                "approval_action_present": True,
                "opinion_control_present": True,
                "submit_entry_present": True,
            },
            "expected_effect": {
                "pending_action_processed": True,
                "processed_count": 1,
                "verification": "pending_disappearance",
            },
        },
        "summary": _summary(profile_key, profile, target, detail, inputs["opinion"]),
    }


def preflight_pending_action(
    adapter,
    worker,
    arguments: dict,
    profile_key: str,
) -> dict:
    affair_id = _bounded_identifier(arguments.get("affair_id"), "affair_id")
    profile, source, detail, signals = _resolve_validated_target(
        adapter, worker, affair_id, profile_key
    )
    target = _frozen_target(source, detail, signals, profile_key)
    summary = _summary(profile_key, profile, target, detail, "")
    review_fields = [
        dict(field)
        for field in summary.get("fields") or []
        if field.get("label") != "处理意见"
    ]
    return {
        "matched": True,
        "profile": profile_key,
        "affair_id": str(source.get("affair_id") or affair_id),
        "review_fingerprint": target["review_fingerprint"],
        "review": {
            "title": str(source.get("title") or summary["title"]),
            "effect": summary["effect"],
            "fields": review_fields,
        },
    }


def _resolve_validated_target(
    adapter,
    worker,
    affair_id: str,
    profile_key: str,
) -> tuple[dict, dict, dict, dict]:
    profile = _profile(profile_key)
    source, detail = adapter.resolve_workflow_detail(
        worker,
        collection="pending",
        affair_id=affair_id,
    )
    signals = _validate_target(
        profile_key,
        profile,
        source,
        detail,
        worker.page,
        affair_id,
    )
    return profile, source, detail, signals


def _commit_pending_action(
    adapter,
    worker,
    plan: dict,
    *,
    profile_key: str,
    enter_commit_boundary: Callable[[], None],
    timeout_seconds: float = 60,
) -> dict:
    profile = _profile(profile_key)
    _validate_plan(plan, profile_key, profile)
    target = dict(plan["target"])
    affair_id = _bounded_identifier(target.get("affair_id"), "affair_id")
    opinion = _bounded_text(plan.get("exact_input", {}).get("opinion"), "opinion", 1000)
    source, detail = adapter.resolve_workflow_detail(
        worker,
        collection="pending",
        affair_id=affair_id,
    )
    signals = _validate_target(
        profile_key,
        profile,
        source,
        detail,
        worker.page,
        affair_id,
    )
    current_target = _frozen_target(source, detail, signals, profile_key)
    _assert_frozen_target(target, current_target)

    page = worker.page
    page.on("dialog", lambda dialog: dialog.accept())
    boundary_crossed = False
    enter_commit_boundary()
    boundary_crossed = True
    try:
        scheduled = page.evaluate(
            _PENDING_ACTION_SCRIPT,
            {
                "affair_id": affair_id,
                "opinion": opinion,
                "action_kind": profile["action_kind"],
                "attitude_code": profile.get("attitude_code", ""),
            },
        )
        if not isinstance(scheduled, dict) or scheduled.get("scheduled") is not True:
            raise PendingActionOutcomeUnknown(
                "OA did not schedule the pending action submission."
            )
        deadline = time.monotonic() + max(timeout_seconds, 5)
        with _pending_readback_worker(worker) as readback_worker:
            while time.monotonic() < deadline:
                time.sleep(0.8)
                pending = adapter.list_workflows(
                    readback_worker,
                    collection="pending",
                    arguments={"limit": 100},
                )
                if profile_key in {"work_handover", "flight_application"} and (
                    pending.get("coverage") or {}
                ).get("status") != "complete":
                    raise PendingActionOutcomeUnknown(
                        "The pending readback is incomplete; disappearance is not proven."
                    )
                if not any(
                    str(item.get("affair_id") or "") == affair_id
                    for item in pending.get("items") or []
                ):
                    result = {
                        "schema_version": profile["result_schema"],
                        "business_intent": profile["business_intent"],
                        "pending_action_processed": True,
                        "processed_count": 1,
                        "action_kind": profile["action_kind"],
                        "workflow_profile": profile_key,
                        "target": {
                            "affair_id": affair_id,
                            "title": str(source.get("title") or ""),
                        },
                        "verification": {
                            "confirmed": True,
                            "method": "pending_disappearance",
                        },
                        "transport": "central_browser_session",
                    }
                    if profile["action_kind"] == "acknowledgement":
                        result["workflow_acknowledged"] = True
                    elif profile["action_kind"] == "confirmation":
                        result["workflow_confirmed"] = True
                    else:
                        result["workflow_approved"] = True
                    return result
        raise PendingActionOutcomeUnknown(
            "The pending action was scheduled, but the affair remained in the pending list."
        )
    except PendingActionOutcomeUnknown:
        raise
    except BaseException as exc:
        if boundary_crossed:
            raise PendingActionOutcomeUnknown(
                "The OA pending-action boundary was crossed, but verification failed."
            ) from exc
        raise


@contextmanager
def _pending_readback_worker(worker):
    fork_page = getattr(worker, "fork_page", None)
    if not callable(fork_page):
        yield worker
        return
    with fork_page() as readback_worker:
        yield readback_worker


def _validate_target(
    profile_key: str,
    profile: dict,
    source: dict,
    detail: dict,
    page,
    affair_id: str,
) -> dict:
    title = str(source.get("title") or detail.get("title") or "").strip()
    if not _title_matches(profile["title_rule"], title):
        raise PendingActionContractMismatch(
            f"The selected pending workflow is not a registered {profile_key} item."
        )
    field_names = {
        str(item.get("name") or "").strip()
        for item in detail.get("fields") or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    missing_fields = set(profile["required_fields"]) - field_names
    if missing_fields:
        raise PendingActionContractMismatch(
            "The pending workflow is missing registered fields: "
            + ", ".join(sorted(missing_fields))
        )
    allowed_fields = profile["allowed_fields"]
    if allowed_fields is not None and not field_names.issubset(set(allowed_fields)):
        raise PendingActionContractMismatch(
            "The pending workflow exposes fields outside the registered profile."
        )
    actions = {
        str(item.get("code") or "")
        for item in detail.get("actions") or []
        if isinstance(item, dict)
    }
    if "ContinueSubmit" not in actions:
        raise PendingActionContractMismatch(
            "The registered pending action is unavailable on the target workflow."
        )
    signals = page.evaluate(_PAGE_CONTRACT_SCRIPT, affair_id)
    if not isinstance(signals, dict):
        raise PendingActionContractMismatch(
            "The OA pending page no longer matches the registered contract."
        )
    if not all(
        (
            signals.get("affair_matches") is True,
            signals.get("comment_present") is True,
            signals.get("submit_present") is True,
            signals.get("page_path") == "/seeyon/collaboration/collaboration.do",
        )
    ):
        raise PendingActionContractMismatch(
            "The OA pending page no longer matches the registered contract."
        )
    node_policy = str(signals.get("node_policy") or "").strip()
    node_policy_name = str(signals.get("node_policy_name") or "").strip()
    if (
        node_policy not in profile["node_policies"]
        and node_policy_name not in profile["node_policy_names"]
    ):
        raise PendingActionContractMismatch(
            "The OA node policy does not match the registered workflow profile."
        )
    attitudes = {
        str(item or "").lower() for item in signals.get("attitude_codes") or []
    }
    attitude_code = str(profile.get("attitude_code") or "").lower()
    if attitude_code and attitude_code not in attitudes:
        raise PendingActionContractMismatch(
            "The OA page does not expose the registered attitude."
        )
    identity = signals.get("identity")
    if not isinstance(identity, dict):
        raise PendingActionContractMismatch("The OA workflow identity is unavailable.")
    if profile_key == "standard_collaboration" and (
        str(identity.get("body_type") or "").upper() not in {"10", "HTML"}
        or str(identity.get("form_record_id") or "")
    ):
        raise PendingActionContractMismatch("Standard collaboration requires an explicit non-form HTML body identity.")
    if not str(identity.get("summary_id") or "") or not str(
        identity.get("process_id") or ""
    ):
        raise PendingActionContractMismatch(
            "The OA workflow summary or process identity is unavailable."
        )
    if str(identity.get("template_id") or "") != profile["template_id"]:
        raise PendingActionContractMismatch(
            "The OA workflow template identity does not match the registered profile."
        )
    if str(identity.get("form_app_id") or "") != profile["form_app_id"]:
        raise PendingActionContractMismatch(
            "The OA workflow form identity does not match the registered profile."
        )
    if profile.get("business_snapshot_policy") == "labor_contract_renewal_v1":
        signals = dict(signals)
        signals["business_snapshot"] = _labor_contract_business_snapshot(
            page, detail
        )
    elif profile.get("business_snapshot_policy") == "attendance_confirmation_v1":
        signals = dict(signals)
        signals["business_snapshot"] = _attendance_confirmation_business_snapshot(
            page, detail
        )
    elif (
        profile.get("business_snapshot_policy")
        == "intellectual_property_declaration_v1"
    ):
        signals = dict(signals)
        signals["business_snapshot"] = (
            _intellectual_property_declaration_business_snapshot(page, detail)
        )
    elif profile.get("business_snapshot_policy") == "resignation_v1":
        signals = dict(signals)
        signals["business_snapshot"] = _resignation_business_snapshot(page)
    elif profile.get("business_snapshot_policy") == "leave_approval_v1":
        signals = dict(signals)
        signals["business_snapshot"] = _leave_business_snapshot(page)
    elif profile.get("business_snapshot_policy") == "business_trip_approval_v1":
        signals = dict(signals)
        signals["business_snapshot"] = _business_trip_business_snapshot(page)
    elif profile.get("business_snapshot_policy") == "work_handover_v1":
        if node_policy not in profile["node_policies"] or node_policy_name not in profile["node_policy_names"]:
            raise PendingActionContractMismatch("The OA work-handover approval node identity is inconsistent.")
        if not str(identity.get("form_record_id") or ""):
            raise PendingActionContractMismatch("The OA work-handover form record identity is unavailable.")
        try:
            signals = dict(signals)
            signals["business_snapshot"] = work_handover_business_snapshot(page)
        except ValueError as exc:
            raise PendingActionContractMismatch(str(exc)) from exc
    elif profile.get("business_snapshot_policy") == "flight_application_v1":
        if node_policy not in profile["node_policies"] or node_policy_name not in profile["node_policy_names"]:
            raise PendingActionContractMismatch("The OA flight-application approval node identity is inconsistent.")
        if not str(identity.get("form_record_id") or ""):
            raise PendingActionContractMismatch("The OA flight-application form record identity is unavailable.")
        try:
            signals = dict(signals)
            signals["business_snapshot"] = flight_application_business_snapshot(page)
        except ValueError as exc:
            raise PendingActionContractMismatch(str(exc)) from exc
    return signals


def _labor_contract_business_snapshot(page, detail: dict) -> dict:
    frame = None
    for candidate in list(page.frames):
        if "/cap4/" not in str(candidate.url or ""):
            continue
        try:
            if candidate.locator("#field0008_id").count() == 1:
                frame = candidate
                break
        except Exception:
            continue
    if frame is None:
        raise PendingActionContractMismatch(
            "The OA labor-contract CAP4 form is unavailable."
        )
    snapshot = frame.evaluate(
        r"""
        () => {
          const field = (id) => document.querySelector(`#${id}`);
          const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const selected = (id) => {
            const wrapper = field(id);
            const items = Array.from(wrapper?.querySelectorAll('.cap4-radio__item') || []);
            const chosen = items.filter((item) => item.querySelector('.cap4-radio-xuanzhong'));
            return chosen.length === 1
              ? clean(chosen[0].querySelector('.cap4-radio__text')?.textContent)
              : '';
          };
          const ids = ['field0008_id', 'field0009_id', 'field0011_id'];
          const browseOnly = ids.every((id) => {
            const section = field(id)?.querySelector('section');
            return Boolean(section?.classList.contains('is-none'));
          });
          return {
            browse_only: browseOnly,
            guidance: clean(
              field('field0008_id')?.querySelector('.cap4-textarea__browse')?.innerText
            ),
            evaluation: selected('field0009_id'),
            renewal_recommendation: selected('field0011_id'),
          };
        }
        """
    )
    if not isinstance(snapshot, dict) or snapshot.get("browse_only") is not True:
        raise PendingActionContractMismatch(
            "The OA labor-contract business fields are editable at this node; "
            "a separate field-entry contract is required."
        )
    for name in ("guidance", "evaluation", "renewal_recommendation"):
        if not str(snapshot.get(name) or "").strip():
            raise PendingActionContractMismatch(
                f"The OA labor-contract {name} value is unavailable."
            )
    values = _field_values(detail)
    result = {
        "employee": values.get("姓名", ""),
        "contract_term": values.get("入职日期", ""),
        "guidance": str(snapshot["guidance"]),
        "evaluation": str(snapshot["evaluation"]),
        "renewal_recommendation": str(snapshot["renewal_recommendation"]),
        "renewal_feedback": values.get("续签情况反馈", ""),
        "business_fields_browse_only": True,
    }
    for name, value in list(result.items()):
        if isinstance(value, str):
            result[name] = re.sub(r"\s+", " ", value).strip()[:1000]
    if not result["employee"] or not result["contract_term"]:
        raise PendingActionContractMismatch(
            "The OA labor-contract employee or contract term is unavailable."
        )
    return result


def _attendance_confirmation_business_snapshot(page, detail: dict) -> dict:
    frame = None
    for candidate in list(page.frames):
        if "/cap4/" not in str(candidate.url or ""):
            continue
        try:
            if candidate.locator("#field0097_id").count() == 1:
                frame = candidate
                break
        except Exception:
            continue
    if frame is None:
        raise PendingActionContractMismatch(
            "The OA attendance-confirmation CAP4 form is unavailable."
        )
    snapshot = frame.evaluate(
        r"""
        () => {
          const field = document.querySelector('#field0097_id');
          const section = field?.querySelector('section');
          const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const items = Array.from(field?.querySelectorAll('.cap4-radio__item') || []);
          const selected = items.filter((item) => Boolean(
            item.querySelector('.cap4-radio-xuanzhong, .cap-icon-danxuan-xuanzhong')
          ));
          return {
            browse_only: Boolean(section?.classList.contains('is-none')),
            selection_count: selected.length,
            decision: selected.length === 1
              ? clean(selected[0].querySelector('.cap4-radio__text')?.textContent)
              : '',
          };
        }
        """
    )
    if not isinstance(snapshot, dict) or snapshot.get("browse_only") is not True:
        raise PendingActionContractMismatch(
            "The OA attendance decision is editable; a separate field-entry contract is required."
        )
    if snapshot.get("selection_count") != 1 or snapshot.get("decision") not in {
        "有异议",
        "无异议",
    }:
        raise PendingActionContractMismatch(
            "The OA attendance decision is unavailable or ambiguous."
        )
    values = _field_values(detail)
    employee = values.get("姓名", "")
    attendance = values.get("应出勤天数", "")
    if not employee or not attendance:
        raise PendingActionContractMismatch(
            "The OA attendance employee or attendance totals are unavailable."
        )
    return {
        "employee": employee,
        "attendance_totals": attendance,
        "decision": str(snapshot["decision"]),
        "business_fields_browse_only": True,
    }


def _intellectual_property_declaration_business_snapshot(
    page, detail: dict
) -> dict:
    frame = None
    for candidate in list(page.frames):
        if "/cap4/" not in str(candidate.url or ""):
            continue
        try:
            if candidate.locator("#field0010_id").count() == 1:
                frame = candidate
                break
        except Exception:
            continue
    if frame is None:
        raise PendingActionContractMismatch(
            "The OA intellectual-property CAP4 form is unavailable."
        )
    snapshot = frame.evaluate(
        r"""
        () => {
          const field = (id) => document.querySelector(`#${id}`);
          const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const value = (id) => clean(field(id)?.innerText);
          const selected = (id) => {
            const wrapper = field(id);
            const items = Array.from(wrapper?.querySelectorAll('.cap4-radio__item') || []);
            const chosen = items.filter((item) => item.querySelector(
              '.cap4-radio-xuanzhong, .cap-icon-danxuan-xuanzhong'
            ));
            return chosen.length === 1
              ? clean(chosen[0].querySelector('.cap4-radio__text')?.textContent)
              : '';
          };
          const ids = [
            'field0001_id', 'field0002_id', 'field0003_id', 'field0004_id',
            'field0010_id', 'field0011_id', 'field0018_id', 'field0037_id',
            'field0034_id', 'field0041_id', 'field0046_id', 'field0027_id'
          ];
          const browseOnly = ids.every((id) => {
            const section = field(id)?.querySelector('section');
            return Boolean(section?.classList.contains('is-none'));
          });
          return {
            browse_only: browseOnly,
            applicant: value('field0001_id'),
            employee_number: value('field0002_id'),
            department: value('field0003_id'),
            application_date: value('field0004_id'),
            intellectual_property_type: selected('field0010_id'),
            declaration_name: value('field0011_id'),
            inventors: value('field0018_id'),
            ownership_type: selected('field0037_id'),
            ownership_unit: value('field0034_id'),
            application_purpose: value('field0041_id'),
            other_purpose: value('field0046_id'),
            application_material: value('field0027_id'),
          };
        }
        """
    )
    if not isinstance(snapshot, dict) or snapshot.get("browse_only") is not True:
        raise PendingActionContractMismatch(
            "The OA intellectual-property business fields are editable at this node; "
            "a separate field-entry contract is required."
        )
    required = (
        "applicant",
        "employee_number",
        "department",
        "application_date",
        "intellectual_property_type",
        "declaration_name",
        "inventors",
        "ownership_type",
        "ownership_unit",
        "application_purpose",
        "application_material",
    )
    for name in required:
        if not str(snapshot.get(name) or "").strip():
            raise PendingActionContractMismatch(
                f"The OA intellectual-property {name} value is unavailable."
            )
    result = {
        name: re.sub(r"\s+", " ", str(value or "")).strip()[:1000]
        for name, value in snapshot.items()
        if name != "browse_only"
    }
    result["business_fields_browse_only"] = True
    return result


def _leave_business_snapshot(page) -> dict:
    frame = None
    for candidate in list(page.frames):
        if "/cap4/" not in str(candidate.url or ""):
            continue
        try:
            if candidate.locator("#field0023_id").count() == 1:
                frame = candidate
                break
        except Exception:
            continue
    if frame is None:
        raise PendingActionContractMismatch(
            "The OA leave-request CAP4 form is unavailable."
        )
    snapshot = frame.evaluate(
        r"""
        () => {
          const field = (id) => document.querySelector(`#${id}`);
          const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const value = (id) => clean(field(id)?.innerText);
          const selected = (id) => {
            const wrapper = field(id);
            const items = Array.from(wrapper?.querySelectorAll('.cap4-radio__item') || []);
            const chosen = items.filter((item) => item.querySelector(
              '.cap4-radio-xuanzhong, .cap-icon-danxuan-xuanzhong'
            ));
            return chosen.length === 1
              ? clean(chosen[0].querySelector('.cap4-radio__text')?.textContent)
              : '';
          };
          const ids = [
            'field0001_id', 'field0002_id', 'field0003_id', 'field0004_id',
            'field0008_id', 'field0019_id', 'field0020_id', 'field0006_id',
            'field0007_id', 'field0022_id', 'field0023_id', 'field0009_id',
            'field0010_id'
          ];
          const browseOnly = ids.every((id) => {
            const section = field(id)?.querySelector('section');
            return Boolean(section?.classList.contains('is-none'));
          });
          return {
            browse_only: browseOnly,
            applicant: value('field0001_id'),
            employee_number: value('field0002_id'),
            department: value('field0003_id'),
            application_date: value('field0004_id'),
            leave_type: value('field0008_id'),
            remaining_annual_leave_days: value('field0019_id'),
            remaining_compensatory_leave_hours: value('field0020_id'),
            start_time: value('field0006_id'),
            end_time: value('field0007_id'),
            leave_days: value('field0022_id'),
            leave_hours: value('field0023_id'),
            reason: value('field0009_id'),
            has_direct_supervisor: selected('field0010_id'),
          };
        }
        """
    )
    if not isinstance(snapshot, dict) or snapshot.get("browse_only") is not True:
        raise PendingActionContractMismatch(
            "The OA leave-request business fields are editable at this node; "
            "a separate field-entry contract is required."
        )
    required = (
        "applicant",
        "employee_number",
        "department",
        "application_date",
        "leave_type",
        "start_time",
        "end_time",
        "leave_days",
        "leave_hours",
        "reason",
        "has_direct_supervisor",
    )
    for name in required:
        if not str(snapshot.get(name) or "").strip():
            raise PendingActionContractMismatch(
                f"The OA leave-request {name} value is unavailable."
            )
    if str(snapshot.get("has_direct_supervisor") or "").strip() not in {"是", "否"}:
        raise PendingActionContractMismatch(
            "The OA leave-request direct-supervisor selection is unsupported."
        )
    result = {
        name: re.sub(r"\s+", " ", str(value or "")).strip()[:1000]
        for name, value in snapshot.items()
        if name != "browse_only"
    }
    result["business_fields_browse_only"] = True
    return result


def _business_trip_business_snapshot(page) -> dict:
    frame = None
    for candidate in list(page.frames):
        if "/cap4/" not in str(candidate.url or ""):
            continue
        try:
            if candidate.locator("#field0027_id").count() == 1:
                frame = candidate
                break
        except Exception:
            continue
    if frame is None:
        raise PendingActionContractMismatch(
            "The OA business-trip request CAP4 form is unavailable."
        )
    snapshot = frame.evaluate(
        r"""
        () => {
          const field = (id) => document.querySelector(`#${id}`);
          const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const value = (id) => clean(field(id)?.innerText);
          const selected = (id) => {
            const wrapper = field(id);
            const items = Array.from(wrapper?.querySelectorAll('.cap4-radio__item') || []);
            const chosen = items.filter((item) => item.querySelector(
              '.cap4-radio-xuanzhong, .cap-icon-danxuan-xuanzhong'
            ));
            return chosen.length === 1
              ? clean(chosen[0].querySelector('.cap4-radio__text')?.textContent)
              : '';
          };
          const ids = [
            'field0001_id', 'field0002_id', 'field0003_id', 'field0004_id',
            'field0006_id', 'field0007_id', 'field0027_id', 'field0023_id',
            'field0026_id', 'field0029_id', 'field0022_id', 'field0009_id',
            'field0010_id', 'field0011_id'
          ];
          const browseOnly = ids.every((id) => {
            const section = field(id)?.querySelector('section');
            return Boolean(section?.classList.contains('is-none'));
          });
          return {
            browse_only: browseOnly,
            applicant: value('field0001_id'),
            employee_number: value('field0002_id'),
            department: value('field0003_id'),
            application_date: value('field0004_id'),
            start_time: value('field0006_id'),
            end_time: value('field0007_id'),
            travel_mode: value('field0027_id'),
            origin: value('field0023_id'),
            destination: value('field0026_id'),
            trip_days: value('field0029_id'),
            trip_hours: value('field0022_id'),
            reason: value('field0009_id'),
            has_direct_supervisor: selected('field0010_id'),
            direct_supervisor: value('field0011_id'),
          };
        }
        """
    )
    if not isinstance(snapshot, dict) or snapshot.get("browse_only") is not True:
        raise PendingActionContractMismatch(
            "The OA business-trip request fields are editable at this node; "
            "a separate field-entry contract is required."
        )
    required = (
        "applicant",
        "employee_number",
        "department",
        "application_date",
        "start_time",
        "end_time",
        "travel_mode",
        "origin",
        "destination",
        "trip_days",
        "trip_hours",
        "reason",
        "has_direct_supervisor",
    )
    for name in required:
        if not str(snapshot.get(name) or "").strip():
            raise PendingActionContractMismatch(
                f"The OA business-trip request {name} value is unavailable."
            )
    if str(snapshot.get("travel_mode") or "").strip() not in set(
        BUSINESS_TRIP_TRAVEL_MODES
    ):
        raise PendingActionContractMismatch(
            "The OA business-trip request travel mode is unsupported."
        )
    supervisor_selection = str(snapshot.get("has_direct_supervisor") or "").strip()
    if supervisor_selection not in {"是", "否"}:
        raise PendingActionContractMismatch(
            "The OA business-trip request direct-supervisor selection is unsupported."
        )
    if supervisor_selection == "是" and not str(
        snapshot.get("direct_supervisor") or ""
    ).strip():
        raise PendingActionContractMismatch(
            "The OA business-trip request direct supervisor is unavailable."
        )
    result = {
        name: re.sub(r"\s+", " ", str(value or "")).strip()[:1000]
        for name, value in snapshot.items()
        if name != "browse_only"
    }
    result["business_fields_browse_only"] = True
    return result


def _resignation_business_snapshot(page) -> dict:
    frame = None
    for candidate in list(page.frames):
        if "/cap4/" not in str(candidate.url or ""):
            continue
        try:
            if candidate.locator("#field0006_id").count() == 1:
                frame = candidate
                break
        except Exception:
            continue
    if frame is None:
        raise PendingActionContractMismatch(
            "The OA resignation CAP4 form is unavailable."
        )
    snapshot = frame.evaluate(
        r"""
        () => {
          const field = (id) => document.querySelector(`#${id}`);
          const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const value = (id) => clean(field(id)?.innerText);
          const selected = (id) => {
            const wrapper = field(id);
            const items = Array.from(wrapper?.querySelectorAll('.cap4-radio__item') || []);
            const chosen = items.filter((item) => item.querySelector(
              '.cap4-radio-xuanzhong, .cap-icon-danxuan-xuanzhong'
            ));
            return chosen.length === 1
              ? clean(chosen[0].querySelector('.cap4-radio__text')?.textContent)
              : '';
          };
          const ids = [
            'field0001_id', 'field0002_id', 'field0003_id', 'field0089_id',
            'field0005_id', 'field0006_id', 'field0027_id', 'field0096_id',
            'field0097_id', 'field0008_id', 'field0099_id'
          ];
          const browseOnly = ids.every((id) => {
            const section = field(id)?.querySelector('section');
            return Boolean(section?.classList.contains('is-none'));
          });
          return {
            browse_only: browseOnly,
            employee: value('field0001_id'),
            employee_number: value('field0002_id'),
            department: value('field0003_id'),
            position: value('field0089_id'),
            hire_date: value('field0005_id'),
            resignation_date: value('field0006_id'),
            position_category: value('field0027_id'),
            has_certificate: selected('field0096_id'),
            certificate_name: value('field0097_id'),
            resignation_reason: value('field0008_id'),
            handwritten_application: value('field0099_id'),
          };
        }
        """
    )
    if not isinstance(snapshot, dict) or snapshot.get("browse_only") is not True:
        raise PendingActionContractMismatch(
            "The OA resignation business fields are editable at this node; "
            "a separate field-entry contract is required."
        )
    required = (
        "employee",
        "employee_number",
        "department",
        "position",
        "hire_date",
        "resignation_date",
        "position_category",
        "has_certificate",
        "resignation_reason",
        "handwritten_application",
    )
    for name in required:
        if not str(snapshot.get(name) or "").strip():
            raise PendingActionContractMismatch(
                f"The OA resignation {name} value is unavailable."
            )
    has_certificate = str(snapshot.get("has_certificate") or "").strip()
    if has_certificate not in {"是", "否"}:
        raise PendingActionContractMismatch(
            "The OA resignation certificate selection is unsupported."
        )
    if has_certificate == "是" and not str(
        snapshot.get("certificate_name") or ""
    ).strip():
        raise PendingActionContractMismatch(
            "The OA resignation certificate name is unavailable."
        )
    result = {
        name: re.sub(r"\s+", " ", str(value or "")).strip()[:1000]
        for name, value in snapshot.items()
        if name != "browse_only"
    }
    result["business_fields_browse_only"] = True
    return result


def _frozen_target(
    source: dict,
    detail: dict,
    signals: dict,
    profile_key: str,
) -> dict:
    identity = signals["identity"]
    target = {
        "profile": profile_key,
        "affair_id": str(source.get("affair_id") or ""),
        "summary_id": str(identity.get("summary_id") or ""),
        "process_id": str(identity.get("process_id") or ""),
        "template_id": str(identity.get("template_id") or ""),
        "form_app_id": str(identity.get("form_app_id") or ""),
        "form_record_id": str(identity.get("form_record_id") or ""),
        "body_type": str(identity.get("body_type") or ""),
        "title": str(source.get("title") or ""),
        "sender": str(source.get("sender") or ""),
        "date": str(source.get("date") or ""),
        "node_policy": str(signals.get("node_policy") or ""),
        "node_policy_name": str(signals.get("node_policy_name") or ""),
        "detail_fingerprint": _detail_fingerprint(source, detail),
    }
    if signals.get("business_snapshot"):
        target["business_snapshot"] = dict(signals["business_snapshot"])
    target["review_fingerprint"] = _fingerprint(target)
    return target


def _assert_frozen_target(expected: dict, actual: dict) -> None:
    for name in (
        "profile",
        "affair_id",
        "summary_id",
        "process_id",
        "template_id",
        "form_app_id",
        "form_record_id",
        "body_type",
        "title",
        "sender",
        "date",
        "node_policy",
        "node_policy_name",
        "detail_fingerprint",
    ):
        if str(expected.get(name) or "") != str(actual.get(name) or ""):
            raise PendingActionContractMismatch(
                f"The OA pending target {name} changed after authorization."
            )
    if expected.get("business_snapshot") != actual.get("business_snapshot"):
        raise PendingActionContractMismatch(
            "The OA pending target business_snapshot changed after authorization."
        )
    if expected.get("review_fingerprint") != actual.get("review_fingerprint"):
        raise PendingActionContractMismatch(
            "The OA pending target review_fingerprint changed after authorization."
        )


def _summary(
    profile_key: str,
    profile: dict,
    target: dict,
    detail: dict,
    opinion: str,
) -> dict:
    fields = [
        {"label": "事项", "value": target["title"]},
        {"label": "发起人", "value": target["sender"]},
        {"label": "日期", "value": target["date"]},
    ]
    values = _field_values(detail)
    if profile_key == "travel_expense":
        for name in (
            "姓名",
            "费用归算类型",
            "费用归属部门",
            "费用归属事项",
            "关联出差申请单",
            "应付金额合计",
        ):
            if values.get(name):
                fields.append({"label": name, "value": values[name]})
    elif profile_key == "labor_contract_renewal":
        snapshot = target.get("business_snapshot") or {}
        for name, label in (
            ("employee", "员工信息"),
            ("contract_term", "合同期限"),
            ("guidance", "综合评价指导意见"),
            ("evaluation", "综合评价"),
            ("renewal_recommendation", "续签建议"),
            ("renewal_feedback", "续签情况反馈"),
        ):
            if snapshot.get(name):
                fields.append({"label": label, "value": snapshot[name]})
    elif profile_key == "attendance_confirmation":
        snapshot = target.get("business_snapshot") or {}
        for name, label in (
            ("employee", "员工与月份"),
            ("attendance_totals", "出勤统计"),
            ("decision", "OA 当前确认结论"),
        ):
            if snapshot.get(name):
                fields.append({"label": label, "value": snapshot[name]})
    elif profile_key == "intellectual_property_declaration":
        snapshot = target.get("business_snapshot") or {}
        for name, label in (
            ("applicant", "申请人"),
            ("department", "申请部门"),
            ("intellectual_property_type", "知识产权类型"),
            ("declaration_name", "申报名称"),
            ("inventors", "发明/设计人"),
            ("ownership_type", "权属类型"),
            ("ownership_unit", "权属单位"),
            ("application_purpose", "申请用途"),
            ("other_purpose", "其他用途"),
            ("application_material", "申请材料"),
        ):
            if snapshot.get(name):
                fields.append({"label": label, "value": snapshot[name]})
    elif profile_key == "overtime":
        for name in (
            "姓名",
            "申请开始时间",
            "加班事由",
            "是否存在有非部门经理的直接上级（组负责人）",
            "实际开始时间",
        ):
            if values.get(name):
                fields.append({"label": name, "value": values[name]})
    elif profile_key == "leave":
        snapshot = target.get("business_snapshot") or {}
        for name, label in (
            ("applicant", "申请人"),
            ("employee_number", "工号"),
            ("department", "所属部门"),
            ("application_date", "发起日期"),
            ("leave_type", "请假类型"),
            ("remaining_annual_leave_days", "剩余年休天数"),
            ("remaining_compensatory_leave_hours", "剩余调休小时"),
            ("start_time", "开始时间"),
            ("end_time", "结束时间"),
            ("leave_days", "请假天数"),
            ("leave_hours", "请假小时"),
            ("reason", "事由"),
            ("has_direct_supervisor", "是否有非部门经理直接上级"),
        ):
            if snapshot.get(name):
                fields.append({"label": label, "value": snapshot[name]})
    elif profile_key == "business_trip":
        snapshot = target.get("business_snapshot") or {}
        for name, label in (
            ("applicant", "申请人"),
            ("employee_number", "工号"),
            ("department", "所属部门"),
            ("application_date", "发起日期"),
            ("start_time", "出差开始时间"),
            ("end_time", "出差结束时间"),
            ("travel_mode", "出差工具"),
            ("origin", "出差始发地"),
            ("destination", "出差目的地"),
            ("trip_days", "出差天数"),
            ("trip_hours", "出差小时数"),
            ("reason", "出差事由"),
            ("has_direct_supervisor", "是否有非部门经理直接上级"),
            ("direct_supervisor", "直接上级"),
        ):
            if snapshot.get(name):
                fields.append({"label": label, "value": snapshot[name]})
    elif profile_key == "resignation":
        snapshot = target.get("business_snapshot") or {}
        for name, label in (
            ("employee", "员工"),
            ("employee_number", "工号"),
            ("department", "所属部门"),
            ("position", "岗位"),
            ("hire_date", "入职时间"),
            ("resignation_date", "申请离职时间"),
            ("position_category", "岗位类别"),
            ("has_certificate", "是否有证书"),
            ("certificate_name", "证书名称"),
            ("resignation_reason", "离职原因"),
            ("handwritten_application", "手写离职申请"),
        ):
            if snapshot.get(name):
                fields.append({"label": label, "value": snapshot[name]})
    elif profile_key == "work_handover":
        fields.extend(work_handover_summary_fields(target["business_snapshot"]))
    elif profile_key == "flight_application":
        fields.extend(flight_application_summary_fields(target["business_snapshot"]))
    elif profile_key == "weekly_report":
        for name in ("周报名称", "年度", "本周说明"):
            if values.get(name):
                fields.append({"label": name, "value": values[name]})
    fields.extend(
        [
            {
                "label": "附件数量",
                "value": str(len(detail.get("attachments") or [])),
            },
            {
                "label": "已有意见数量",
                "value": str(len(detail.get("workflow") or [])),
            },
            {"label": "本次操作", "value": profile["action_display"]},
            {"label": "处理意见", "value": opinion},
        ]
    )
    return {
        "title": profile["summary_title"],
        "system": "致远 OA",
        "effect": profile["summary_effect"],
        "authorization_notice": (
            "授权后将立即处理这一条精确绑定的待办；"
            "不会处理同名的其他事项，也不会自动重试未知结果。"
        ),
        "authorize_label": profile["authorize_label"],
        "fields": fields,
        "processed_count": 1,
    }


def _normalize_inputs(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("pending-action input must be an object")
    return {
        "affair_id": _bounded_identifier(arguments.get("affair_id"), "affair_id"),
        "opinion": _bounded_text(arguments.get("opinion"), "opinion", 1000),
    }


def _validate_plan(plan: dict, profile_key: str, profile: dict) -> None:
    if not isinstance(plan, dict) or plan.get("business_intent") != profile["business_intent"]:
        raise PendingActionContractMismatch(
            "The frozen plan is not the registered pending-action plan."
        )
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    contract = (
        plan.get("action_contract")
        if isinstance(plan.get("action_contract"), dict)
        else {}
    )
    if target.get("profile") != profile_key:
        raise PendingActionContractMismatch("The frozen workflow profile changed.")
    if (
        contract.get("version") != profile["contract_version"]
        or contract.get("fingerprint")
        != pending_action_contract_fingerprint(profile_key)
    ):
        raise PendingActionContractMismatch(
            "The pending-action contract changed after authorization."
        )


def pending_action_profile_for_title(title: str) -> dict | None:
    """Return a routing candidate; the existing node/form preflight remains mandatory."""
    for key, profile in _PROFILES.items():
        if _title_matches(profile["title_rule"], title):
            return {
                "profile": key,
                "prepare_capability": profile["prepare_capability"],
                "commit_capability": profile["commit_capability"],
            }
    return None


def _title_matches(rule: dict, title: str) -> bool:
    kind = rule.get("kind")
    value = str(rule.get("value") or "")
    if kind == "contains":
        return value in title
    if kind == "optional_auto_prefix":
        return title.startswith(value) or title.startswith("(自动发起)" + value)
    if kind == "prefix":
        return title.startswith(value)
    if kind == "standard_collaboration":
        return bool(title) and not any(marker in title for marker in _SPECIALIZED_TITLE_MARKERS)
    return False


def _field_values(detail: dict) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in detail.get("fields") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = re.sub(r"\s+", " ", str(item.get("value") or "")).strip()
        if name and value and name not in values:
            values[name] = value[:500]
    return values


def _detail_fingerprint(source: dict, detail: dict) -> str:
    value = {
        "title": str(source.get("title") or detail.get("title") or ""),
        "fields": [
            {
                "name": str(item.get("name") or ""),
                "value": str(item.get("value") or ""),
            }
            for item in detail.get("fields") or []
            if isinstance(item, dict)
        ],
        "attachments": [
            str(item.get("name") or "")
            for item in detail.get("attachments") or []
            if isinstance(item, dict)
        ],
    }
    return _fingerprint(value)


def _profile(profile_key: str) -> dict:
    try:
        return _PROFILES[profile_key]
    except KeyError as exc:
        raise ValueError(f"unsupported pending-action profile: {profile_key}") from exc


def _bounded_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty string of at most 256 characters")
    text = value.strip()
    if any(ord(character) < 32 for character in text):
        raise ValueError(f"{name} must not contain control characters")
    return text


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = re.sub(r"[\r\n\t]+", " ", value).strip()
    if not text:
        raise ValueError(f"{name} is required")
    if len(text) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    return text


def _fingerprint(value: dict) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


_PAGE_CONTRACT_SCRIPT = r"""
(expectedAffairId) => {
  const read = (names) => {
    for (const name of names) {
      const element = document.querySelector(`#${name}`)
        || document.querySelector(`[name='${name}']`);
      const value = String(element?.value || window[name] || '').trim();
      if (value) return value;
    }
    return '';
  };
  const pageAffairId = read(['affairId'])
    || new URLSearchParams(location.search).get('affairId')
    || '';
  const comment = document.querySelector('#content_deal_comment')
    || document.querySelector("textarea[name='content_deal_comment']")
    || document.querySelector('textarea#content')
    || document.querySelector("textarea[name='content']");
  return {
    affair_matches: String(pageAffairId) === String(expectedAffairId),
    comment_present: Boolean(comment),
    submit_present: typeof window.submitClickFunc === 'function'
      || typeof window.dealSubmitFunc === 'function'
      || typeof window.$?.content?.callback?.dealSubmit === 'function',
    page_path: location.pathname,
    node_policy: String(window.nodePolicy || ''),
    node_policy_name: String(window.nodePolicyName || ''),
    attitude_codes: Array.from(
      document.querySelectorAll("input[type='radio'][name='attitude']")
    ).map((radio) => String(radio.getAttribute('code') || radio.value || '').toLowerCase()),
    identity: {
      summary_id: read(['summaryId']),
      process_id: read(['processId']),
      template_id: read(['templeteId', 'templateId']),
      form_app_id: read(['formAppId']),
      form_record_id: read(['formRecordid', 'formRecordId']),
      body_type: read(['bodyType']),
    },
  };
}
"""


_PENDING_ACTION_SCRIPT = r"""
({ affair_id, opinion, attitude_code }) => {
  const read = (names) => {
    for (const name of names) {
      const element = document.querySelector(`#${name}`)
        || document.querySelector(`[name='${name}']`);
      const value = String(element?.value || window[name] || '').trim();
      if (value) return value;
    }
    return '';
  };
  const pageAffairId = read(['affairId'])
    || new URLSearchParams(location.search).get('affairId')
    || '';
  if (String(pageAffairId) !== String(affair_id)) {
    throw new Error('affair_id mismatch before pending action');
  }
  const comment = document.querySelector('#content_deal_comment')
    || document.querySelector("textarea[name='content_deal_comment']")
    || document.querySelector('textarea#content')
    || document.querySelector("textarea[name='content']");
  if (!comment) throw new Error('pending-action opinion control is missing');
  const setValue = (element, value) => {
    const setter = Object.getOwnPropertyDescriptor(element.constructor.prototype, 'value')?.set;
    if (setter) setter.call(element, value); else element.value = value;
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
    element.dispatchEvent(new Event('blur', { bubbles: true }));
  };
  setValue(comment, String(opinion));
  if (attitude_code) {
    const radios = Array.from(document.querySelectorAll("input[type='radio'][name='attitude']"));
    const selected = radios.find((radio) => {
      const code = String(radio.getAttribute('code') || '').toLowerCase();
      const value = String(radio.value || '').toLowerCase();
      return code === attitude_code || value === attitude_code
        || value.endsWith(`.${attitude_code}`);
    });
    if (!selected) throw new Error('registered attitude is missing');
    selected.checked = true;
    selected.dispatchEvent(new Event('input', { bubbles: true }));
    selected.dispatchEvent(new Event('change', { bubbles: true }));
    const attitudeCode = selected.getAttribute('code') || selected.value || attitude_code;
    for (const selector of ['#hidAttitudeCode', '#hidAttitude', '#nodeattitude']) {
      const element = document.querySelector(selector);
      if (element) setValue(element, attitudeCode);
    }
  }
  const nodePolicy = String(window.nodePolicy || '').toLowerCase();
  const nodePolicyName = String(window.nodePolicyName || '');
  let submit = null;
  let submitName = '';
  if ((nodePolicy === 'inform' || nodePolicyName.includes('知会'))
      && typeof window.dealSubmitFunc === 'function') {
    submit = window.dealSubmitFunc;
    submitName = 'dealSubmitFunc';
  } else if (typeof window.submitClickFunc === 'function') {
    submit = window.submitClickFunc;
    submitName = 'submitClickFunc';
  } else if (typeof window.dealSubmitFunc === 'function') {
    submit = window.dealSubmitFunc;
    submitName = 'dealSubmitFunc';
  } else if (typeof window.$?.content?.callback?.dealSubmit === 'function') {
    submit = window.$.content.callback.dealSubmit;
    submitName = '$.content.callback.dealSubmit';
  }
  if (!submit) throw new Error('pending-action submit entry is missing');
  window.setTimeout(() => submit.call(window), 0);
  return { scheduled: true, submit_entry: submitName };
}
"""
