"""Business write bindings and scope policy; no task or transaction mutation."""

from bscli.adapters.seeyon_pending_batch import PENDING_BATCH_PREPARE_CAPABILITY

from bscli.adapters.taihua import (
    TAIHUA_WORK_LOG_CREATE_CAPABILITY,
    TAIHUA_WORK_LOG_CREATE_PREPARE_CAPABILITY,
    TAIHUA_WORK_LOG_FIELD_CARD_SCHEMA,
    TAIHUA_WORK_LOG_CREATE_WORKFLOW,
    TaihuaWorkLogContractMismatch,
    TaihuaWorkLogOutcomeUnknown,
    commit_taihua_work_log_create,
    prepare_taihua_work_log_create,
)

from bscli.adapters.smartlight import (
    SMARTLIGHT_ALARM_WORK_AREA_REVOKE_CAPABILITY,
    SMARTLIGHT_ALARM_WORK_AREA_REVOKE_PREPARE_CAPABILITY,
    SMARTLIGHT_ALARM_WORK_AREA_REVOKE_WORKFLOW,
    SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_CAPABILITY,
    SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_PREPARE_CAPABILITY,
    SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_WORKFLOW,
    SMARTLIGHT_ALARM_REMARK_FIELD_CARD_SCHEMA,
    SMARTLIGHT_ALARM_REMARK_UPDATE_CAPABILITY,
    SMARTLIGHT_ALARM_REMARK_UPDATE_PREPARE_CAPABILITY,
    SMARTLIGHT_ALARM_REMARK_UPDATE_WORKFLOW,
    SMARTLIGHT_RTU_ALARM_DISPOSE_CAPABILITY,
    SMARTLIGHT_RTU_ALARM_DISPOSE_PREPARE_CAPABILITY,
    SMARTLIGHT_RTU_ALARM_DISPOSE_WORKFLOW,
    SmartlightAlarmActionContractMismatch,
    SmartlightAlarmActionOutcomeUnknown,
    SmartlightAlarmRemarkContractMismatch,
    SmartlightAlarmRemarkOutcomeUnknown,
    commit_smartlight_alarm_work_area_revoke,
    commit_smartlight_alarm_work_area_submit,
    commit_smartlight_alarm_remark_update,
    commit_smartlight_rtu_alarm_dispose,
    prepare_smartlight_alarm_work_area_revoke,
    prepare_smartlight_alarm_work_area_submit,
    prepare_smartlight_alarm_remark_update,
    prepare_smartlight_rtu_alarm_dispose,
)

from bscli.adapters.seeyon_business_trip import (
    BUSINESS_TRIP_FIELD_CARD_SCHEMA,
    BUSINESS_TRIP_PREPARE_CAPABILITY,
    BUSINESS_TRIP_SAVE_CAPABILITY,
    BusinessTripContractMismatch,
    BusinessTripOutcomeUnknown,
    prepare_business_trip_draft,
    save_business_trip_draft,
)

from bscli.adapters.seeyon_business_trip_submit import (
    BUSINESS_TRIP_SUBMIT_CAPABILITY,
    BUSINESS_TRIP_SUBMIT_FIELD_CARD_SCHEMA,
    BUSINESS_TRIP_SUBMIT_PREPARE_CAPABILITY,
    prepare_business_trip_submission,
    submit_business_trip_request,
)

from bscli.adapters.seeyon_leave import (
    LEAVE_FIELD_CARD_SCHEMA,
    LEAVE_PREPARE_CAPABILITY,
    LEAVE_SAVE_CAPABILITY,
    LeaveContractMismatch,
    LeaveOutcomeUnknown,
    prepare_leave_draft,
    save_leave_draft,
)

from bscli.adapters.seeyon_leave_submit import (
    LEAVE_SUBMIT_CAPABILITY,
    LEAVE_SUBMIT_FIELD_CARD_SCHEMA,
    LEAVE_SUBMIT_PREPARE_CAPABILITY,
    prepare_leave_submission,
    submit_leave_request,
)

from bscli.adapters.seeyon_meeting import (
    MEETING_CREATE_CAPABILITY,
    MEETING_CREATE_WORKFLOW,
    MEETING_FIELD_CARD_SCHEMA,
    MEETING_PREPARE_CAPABILITY,
    MeetingContractMismatch,
    MeetingOutcomeUnknown,
    build_meeting_field_card_schema,
    create_meeting,
    prepare_meeting_create,
)

from bscli.adapters.seeyon_meeting_room_application import (
    MEETING_ROOM_APPLICATION_CANCEL_CAPABILITY,
    MEETING_ROOM_APPLICATION_CANCEL_FIELD_CARD_SCHEMA,
    MEETING_ROOM_APPLICATION_CANCEL_PREPARE_CAPABILITY,
    MEETING_ROOM_APPLICATION_CANCEL_WORKFLOW,
    MEETING_ROOM_APPLICATION_CREATE_CAPABILITY,
    MEETING_ROOM_APPLICATION_CREATE_WORKFLOW,
    MEETING_ROOM_APPLICATION_FIELD_CARD_SCHEMA,
    MEETING_ROOM_APPLICATION_PREPARE_CAPABILITY,
    MeetingRoomApplicationContractMismatch,
    MeetingRoomApplicationOutcomeUnknown,
    build_meeting_room_application_field_card_schema,
    cancel_meeting_room_application,
    create_meeting_room_application,
    prepare_meeting_room_application,
    prepare_meeting_room_application_cancel,
)

from bscli.adapters.seeyon_missed_punch import (
    MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY,
    MISSED_PUNCH_APPROVAL_FIELD_CARD_SCHEMA,
    MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY,
    MISSED_PUNCH_APPROVE_CAPABILITY,
    MISSED_PUNCH_FIELD_CARD_SCHEMA,
    MISSED_PUNCH_PREPARE_CAPABILITY,
    MISSED_PUNCH_SAVE_CAPABILITY,
    MissedPunchContractMismatch,
    MissedPunchOutcomeUnknown,
    approve_missed_punch_request,
    build_missed_punch_approval_batch_field_schema,
    prepare_missed_punch_approval,
    prepare_missed_punch_draft,
    save_missed_punch_draft,
)

from bscli.adapters.seeyon_pending_actions import (
    prepare_intellectual_property_declaration_approval,
    ATTENDANCE_CONFIRMATION_FIELD_CARD_SCHEMA,
    ATTENDANCE_CONFIRMATION_PREPARE_CAPABILITY,
    ATTENDANCE_CONFIRM_CAPABILITY,
    BUSINESS_TRIP_APPROVAL_FIELD_CARD_SCHEMA,
    BUSINESS_TRIP_APPROVAL_PREPARE_CAPABILITY,
    BUSINESS_TRIP_APPROVE_CAPABILITY,
    EFFICIENCY_DATA_APPROVAL_FIELD_CARD_SCHEMA,
    EFFICIENCY_DATA_APPROVAL_PREPARE_CAPABILITY,
    EFFICIENCY_DATA_APPROVE_CAPABILITY,
    INTELLECTUAL_PROPERTY_DECLARATION_APPROVAL_FIELD_CARD_SCHEMA,
    INTELLECTUAL_PROPERTY_DECLARATION_APPROVAL_PREPARE_CAPABILITY,
    INTELLECTUAL_PROPERTY_DECLARATION_APPROVE_CAPABILITY,
    LABOR_CONTRACT_RENEWAL_APPROVAL_FIELD_CARD_SCHEMA,
    LABOR_CONTRACT_RENEWAL_APPROVAL_PREPARE_CAPABILITY,
    LABOR_CONTRACT_RENEWAL_APPROVE_CAPABILITY,
    LEAVE_APPROVAL_FIELD_CARD_SCHEMA,
    LEAVE_APPROVAL_PREPARE_CAPABILITY,
    LEAVE_APPROVE_CAPABILITY,
    OVERTIME_APPROVAL_FIELD_CARD_SCHEMA,
    OVERTIME_APPROVAL_PREPARE_CAPABILITY,
    OVERTIME_APPROVE_CAPABILITY,
    RESIGNATION_APPROVAL_FIELD_CARD_SCHEMA,
    WORK_HANDOVER_APPROVAL_FIELD_CARD_SCHEMA,
    FLIGHT_APPLICATION_APPROVAL_FIELD_CARD_SCHEMA,
    RESIGNATION_APPROVAL_PREPARE_CAPABILITY,
    WORK_HANDOVER_APPROVAL_PREPARE_CAPABILITY,
    FLIGHT_APPLICATION_APPROVAL_PREPARE_CAPABILITY,
    RESIGNATION_APPROVE_CAPABILITY,
    WORK_HANDOVER_APPROVE_CAPABILITY,
    FLIGHT_APPLICATION_APPROVE_CAPABILITY,
    STANDARD_COLLABORATION_APPROVAL_FIELD_CARD_SCHEMA,
    STANDARD_COLLABORATION_APPROVAL_PREPARE_CAPABILITY,
    STANDARD_COLLABORATION_APPROVE_CAPABILITY,
    TRAVEL_EXPENSE_APPROVAL_FIELD_CARD_SCHEMA,
    TRAVEL_EXPENSE_APPROVAL_PREPARE_CAPABILITY,
    TRAVEL_EXPENSE_APPROVE_CAPABILITY,
    WEEKLY_REPORT_ACKNOWLEDGEMENT_FIELD_CARD_SCHEMA,
    WEEKLY_REPORT_ACKNOWLEDGEMENT_PREPARE_CAPABILITY,
    WEEKLY_REPORT_ACKNOWLEDGE_CAPABILITY,
    PendingActionContractMismatch,
    PendingActionOutcomeUnknown,
    acknowledge_weekly_report,
    approve_business_trip_request,
    confirm_attendance,
    approve_efficiency_data,
    approve_intellectual_property_declaration,
    approve_labor_contract_renewal,
    approve_leave_request,
    approve_overtime,
    approve_resignation,
    approve_work_handover,
    approve_flight_application,
    approve_standard_collaboration,
    approve_travel_expense,
    prepare_efficiency_data_approval,
    prepare_attendance_confirmation,
    prepare_business_trip_approval,
    prepare_labor_contract_renewal_approval,
    prepare_leave_approval,
    prepare_overtime_approval,
    prepare_resignation_approval,
    prepare_work_handover_approval,
    prepare_flight_application_approval,
    prepare_standard_collaboration_approval,
    prepare_travel_expense_approval,
    prepare_weekly_report_acknowledgement,
    preflight_pending_action,
)

from bscli.adapters.seeyon_workflow_revoke import (
    WORKFLOW_REVOKE_CAPABILITY,
    WORKFLOW_REVOKE_FIELD_CARD_SCHEMA,
    WORKFLOW_REVOKE_PREPARE_CAPABILITY,
    WorkflowRevokeContractMismatch,
    WorkflowRevokeOutcomeUnknown,
    prepare_workflow_revoke,
    revoke_workflow,
)

_TRUSTED_WRITE_DEFINITIONS = {
    BUSINESS_TRIP_PREPARE_CAPABILITY: {
        "commit_capability": BUSINESS_TRIP_SAVE_CAPABILITY,
        "field_schema": BUSINESS_TRIP_FIELD_CARD_SCHEMA,
        "context_fields": (),
        "prepare_function": "prepare_business_trip_draft",
        "commit_function": "save_business_trip_draft",
        "contract_error": BusinessTripContractMismatch,
        "outcome_error": BusinessTripOutcomeUnknown,
        "field_message": "Business-trip fields must be entered in the trusted field card.",
        "authorization_message": "The business-trip draft plan requires confirmation in the trusted action card.",
    },
    BUSINESS_TRIP_SUBMIT_PREPARE_CAPABILITY: {
        "commit_capability": BUSINESS_TRIP_SUBMIT_CAPABILITY,
        "field_schema": BUSINESS_TRIP_SUBMIT_FIELD_CARD_SCHEMA,
        "context_fields": (),
        "prepare_function": "prepare_business_trip_submission",
        "commit_function": "submit_business_trip_request",
        "contract_error": BusinessTripContractMismatch,
        "outcome_error": BusinessTripOutcomeUnknown,
        "field_message": "Business-trip fields must be entered in the trusted field card.",
        "authorization_message": "The business-trip submission plan requires confirmation in the trusted action card.",
    },
    LEAVE_PREPARE_CAPABILITY: {
        "commit_capability": LEAVE_SAVE_CAPABILITY,
        "field_schema": LEAVE_FIELD_CARD_SCHEMA,
        "context_fields": (),
        "prepare_function": "prepare_leave_draft",
        "commit_function": "save_leave_draft",
        "contract_error": LeaveContractMismatch,
        "outcome_error": LeaveOutcomeUnknown,
        "field_message": "Leave-request fields must be entered in the trusted field card.",
        "authorization_message": "The leave draft plan requires confirmation in the trusted action card.",
    },
    LEAVE_SUBMIT_PREPARE_CAPABILITY: {
        "commit_capability": LEAVE_SUBMIT_CAPABILITY,
        "field_schema": LEAVE_SUBMIT_FIELD_CARD_SCHEMA,
        "context_fields": (),
        "prepare_function": "prepare_leave_submission",
        "commit_function": "submit_leave_request",
        "contract_error": LeaveContractMismatch,
        "outcome_error": LeaveOutcomeUnknown,
        "field_message": "Leave-request fields must be entered in the trusted field card.",
        "authorization_message": "The leave submission plan requires confirmation in the trusted action card.",
    },
    MISSED_PUNCH_PREPARE_CAPABILITY: {
        "commit_capability": MISSED_PUNCH_SAVE_CAPABILITY,
        "field_schema": MISSED_PUNCH_FIELD_CARD_SCHEMA,
        "context_fields": (),
        "prepare_function": "prepare_missed_punch_draft",
        "commit_function": "save_missed_punch_draft",
        "contract_error": MissedPunchContractMismatch,
        "outcome_error": MissedPunchOutcomeUnknown,
        "field_message": "Missed-punch fields must be entered in the trusted field card.",
        "authorization_message": "The missed-punch draft plan requires confirmation in the trusted action card.",
    },
    MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY: {
        "commit_capability": MISSED_PUNCH_APPROVE_CAPABILITY,
        "field_schema": MISSED_PUNCH_APPROVAL_FIELD_CARD_SCHEMA,
        "context_fields": ("affair_id",),
        "prepare_function": "prepare_missed_punch_approval",
        "commit_function": "approve_missed_punch_request",
        "contract_error": MissedPunchContractMismatch,
        "outcome_error": MissedPunchOutcomeUnknown,
        "field_message": "The missed-punch approval opinion must be entered in the trusted field card.",
        "authorization_message": "The missed-punch approval plan requires confirmation in the trusted action card.",
    },
    MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY: {
        "commit_capability": MISSED_PUNCH_APPROVE_CAPABILITY,
        "field_schema": MISSED_PUNCH_APPROVAL_FIELD_CARD_SCHEMA,
        "field_schema_function": "build_missed_punch_approval_batch_field_schema",
        "context_fields": ("batch_id", "affair_id"),
        "prepare_function": "prepare_missed_punch_approval",
        "commit_function": "approve_missed_punch_request",
        "contract_error": MissedPunchContractMismatch,
        "outcome_error": MissedPunchOutcomeUnknown,
        "field_message": "The current missed-punch opinion must be entered in the trusted field card.",
        "authorization_message": "The current missed-punch approval plan requires confirmation in the trusted action card.",
    },
    MEETING_PREPARE_CAPABILITY: MEETING_CREATE_WORKFLOW.legacy_definition(),
    MEETING_ROOM_APPLICATION_PREPARE_CAPABILITY: MEETING_ROOM_APPLICATION_CREATE_WORKFLOW.legacy_definition(),
    MEETING_ROOM_APPLICATION_CANCEL_PREPARE_CAPABILITY: MEETING_ROOM_APPLICATION_CANCEL_WORKFLOW.legacy_definition(),
    WORKFLOW_REVOKE_PREPARE_CAPABILITY: {
        "commit_capability": WORKFLOW_REVOKE_CAPABILITY,
        "field_schema": WORKFLOW_REVOKE_FIELD_CARD_SCHEMA,
        "context_fields": ("affair_id",),
        "prepare_function": "prepare_workflow_revoke",
        "commit_function": "revoke_workflow",
        "contract_error": WorkflowRevokeContractMismatch,
        "outcome_error": WorkflowRevokeOutcomeUnknown,
        "field_message": "The workflow revoke comment must be entered in the trusted field card.",
        "authorization_message": "The workflow revoke plan requires confirmation in the trusted action card.",
    },
}

_TRUSTED_WRITE_DEFINITIONS.update(
    {
        EFFICIENCY_DATA_APPROVAL_PREPARE_CAPABILITY: {
            "commit_capability": EFFICIENCY_DATA_APPROVE_CAPABILITY,
            "field_schema": EFFICIENCY_DATA_APPROVAL_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_efficiency_data_approval",
            "commit_function": "approve_efficiency_data",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": "The efficiency-data opinion must be entered in the trusted field card.",
            "authorization_message": "The efficiency-data approval requires trusted confirmation.",
        },
        TRAVEL_EXPENSE_APPROVAL_PREPARE_CAPABILITY: {
            "commit_capability": TRAVEL_EXPENSE_APPROVE_CAPABILITY,
            "field_schema": TRAVEL_EXPENSE_APPROVAL_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_travel_expense_approval",
            "commit_function": "approve_travel_expense",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": "The travel-expense opinion must be entered in the trusted field card.",
            "authorization_message": "The travel-expense approval requires trusted confirmation.",
        },
        BUSINESS_TRIP_APPROVAL_PREPARE_CAPABILITY: {
            "commit_capability": BUSINESS_TRIP_APPROVE_CAPABILITY,
            "field_schema": BUSINESS_TRIP_APPROVAL_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_business_trip_approval",
            "commit_function": "approve_business_trip_request",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": "The business-trip approval opinion must be entered in the trusted field card.",
            "authorization_message": "The business-trip approval requires trusted confirmation.",
        },
        LABOR_CONTRACT_RENEWAL_APPROVAL_PREPARE_CAPABILITY: {
            "commit_capability": LABOR_CONTRACT_RENEWAL_APPROVE_CAPABILITY,
            "field_schema": LABOR_CONTRACT_RENEWAL_APPROVAL_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_labor_contract_renewal_approval",
            "commit_function": "approve_labor_contract_renewal",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": "The labor-contract renewal opinion must be entered in the trusted field card.",
            "authorization_message": "The labor-contract renewal approval requires trusted confirmation.",
        },
        INTELLECTUAL_PROPERTY_DECLARATION_APPROVAL_PREPARE_CAPABILITY: {
            "commit_capability": (
                INTELLECTUAL_PROPERTY_DECLARATION_APPROVE_CAPABILITY
            ),
            "field_schema": (
                INTELLECTUAL_PROPERTY_DECLARATION_APPROVAL_FIELD_CARD_SCHEMA
            ),
            "context_fields": ("affair_id",),
            "prepare_function": (
                "prepare_intellectual_property_declaration_approval"
            ),
            "commit_function": "approve_intellectual_property_declaration",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": (
                "The intellectual-property declaration opinion must be entered "
                "in the trusted field card."
            ),
            "authorization_message": (
                "The intellectual-property declaration approval requires trusted "
                "confirmation."
            ),
        },
        OVERTIME_APPROVAL_PREPARE_CAPABILITY: {
            "commit_capability": OVERTIME_APPROVE_CAPABILITY,
            "field_schema": OVERTIME_APPROVAL_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_overtime_approval",
            "commit_function": "approve_overtime",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": "The overtime approval opinion must be entered in the trusted field card.",
            "authorization_message": "The overtime approval requires trusted confirmation.",
        },
        LEAVE_APPROVAL_PREPARE_CAPABILITY: {
            "commit_capability": LEAVE_APPROVE_CAPABILITY,
            "field_schema": LEAVE_APPROVAL_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_leave_approval",
            "commit_function": "approve_leave_request",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": "The leave-request approval opinion must be entered in the trusted field card.",
            "authorization_message": "The leave-request approval requires trusted confirmation.",
        },
        RESIGNATION_APPROVAL_PREPARE_CAPABILITY: {
            "commit_capability": RESIGNATION_APPROVE_CAPABILITY,
            "field_schema": RESIGNATION_APPROVAL_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_resignation_approval",
            "commit_function": "approve_resignation",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": (
                "The resignation approval opinion must be entered in the trusted field card."
            ),
            "authorization_message": (
                "The resignation approval requires trusted confirmation."
            ),
        },
        WORK_HANDOVER_APPROVAL_PREPARE_CAPABILITY: {
            "commit_capability": WORK_HANDOVER_APPROVE_CAPABILITY,
            "field_schema": WORK_HANDOVER_APPROVAL_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_work_handover_approval",
            "commit_function": "approve_work_handover",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": (
                "The work-handover approval opinion must be entered in the trusted field card."
            ),
            "authorization_message": (
                "The work-handover approval requires trusted confirmation."
            ),
        },
        FLIGHT_APPLICATION_APPROVAL_PREPARE_CAPABILITY: {
            "commit_capability": FLIGHT_APPLICATION_APPROVE_CAPABILITY,
            "field_schema": FLIGHT_APPLICATION_APPROVAL_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_flight_application_approval",
            "commit_function": "approve_flight_application",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": "The flight-application opinion must be entered in the trusted field card.",
            "authorization_message": "The flight-application approval requires trusted confirmation.",
        },
        ATTENDANCE_CONFIRMATION_PREPARE_CAPABILITY: {
            "commit_capability": ATTENDANCE_CONFIRM_CAPABILITY,
            "field_schema": ATTENDANCE_CONFIRMATION_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_attendance_confirmation",
            "commit_function": "confirm_attendance",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": "The attendance-confirmation opinion must be entered in the trusted field card.",
            "authorization_message": "The attendance confirmation requires trusted confirmation.",
        },
        WEEKLY_REPORT_ACKNOWLEDGEMENT_PREPARE_CAPABILITY: {
            "commit_capability": WEEKLY_REPORT_ACKNOWLEDGE_CAPABILITY,
            "field_schema": WEEKLY_REPORT_ACKNOWLEDGEMENT_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_weekly_report_acknowledgement",
            "commit_function": "acknowledge_weekly_report",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": "The weekly-report opinion must be entered in the trusted field card.",
            "authorization_message": "The weekly-report acknowledgement requires trusted confirmation.",
        },
        STANDARD_COLLABORATION_APPROVAL_PREPARE_CAPABILITY: {
            "commit_capability": STANDARD_COLLABORATION_APPROVE_CAPABILITY,
            "field_schema": STANDARD_COLLABORATION_APPROVAL_FIELD_CARD_SCHEMA,
            "context_fields": ("affair_id",),
            "prepare_function": "prepare_standard_collaboration_approval",
            "commit_function": "approve_standard_collaboration",
            "contract_error": PendingActionContractMismatch,
            "outcome_error": PendingActionOutcomeUnknown,
            "field_message": "The collaboration opinion must be entered in the trusted field card.",
            "authorization_message": "The collaboration approval requires trusted confirmation.",
        },
    }
)

for _pending_profile, _pending_prepare_capability in (
    ("efficiency_data", EFFICIENCY_DATA_APPROVAL_PREPARE_CAPABILITY),
    ("travel_expense", TRAVEL_EXPENSE_APPROVAL_PREPARE_CAPABILITY),
    ("business_trip", BUSINESS_TRIP_APPROVAL_PREPARE_CAPABILITY),
    ("labor_contract_renewal", LABOR_CONTRACT_RENEWAL_APPROVAL_PREPARE_CAPABILITY),
    (
        "intellectual_property_declaration",
        INTELLECTUAL_PROPERTY_DECLARATION_APPROVAL_PREPARE_CAPABILITY,
    ),
    ("overtime", OVERTIME_APPROVAL_PREPARE_CAPABILITY),
    ("leave", LEAVE_APPROVAL_PREPARE_CAPABILITY),
    ("resignation", RESIGNATION_APPROVAL_PREPARE_CAPABILITY),
    ("work_handover", WORK_HANDOVER_APPROVAL_PREPARE_CAPABILITY),
    ("flight_application", FLIGHT_APPLICATION_APPROVAL_PREPARE_CAPABILITY),
    ("attendance_confirmation", ATTENDANCE_CONFIRMATION_PREPARE_CAPABILITY),
    ("weekly_report", WEEKLY_REPORT_ACKNOWLEDGEMENT_PREPARE_CAPABILITY),
    ("standard_collaboration", STANDARD_COLLABORATION_APPROVAL_PREPARE_CAPABILITY),
):
    _TRUSTED_WRITE_DEFINITIONS[_pending_prepare_capability].update(
        {
            "preflight_function": "preflight_pending_action",
            "preflight_profile": _pending_profile,
        }
    )

_TRUSTED_WRITE_DEFINITIONS.update(
    {
        TAIHUA_WORK_LOG_CREATE_PREPARE_CAPABILITY: TAIHUA_WORK_LOG_CREATE_WORKFLOW.legacy_definition(),
        SMARTLIGHT_ALARM_REMARK_UPDATE_PREPARE_CAPABILITY: SMARTLIGHT_ALARM_REMARK_UPDATE_WORKFLOW.legacy_definition(),
        SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_PREPARE_CAPABILITY: SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_WORKFLOW.legacy_definition(),
        SMARTLIGHT_ALARM_WORK_AREA_REVOKE_PREPARE_CAPABILITY: SMARTLIGHT_ALARM_WORK_AREA_REVOKE_WORKFLOW.legacy_definition(),
        SMARTLIGHT_RTU_ALARM_DISPOSE_PREPARE_CAPABILITY: SMARTLIGHT_RTU_ALARM_DISPOSE_WORKFLOW.legacy_definition(),
    }
)

_TRUSTED_WRITE_DEFINITIONS[PENDING_BATCH_PREPARE_CAPABILITY] = {
    **_TRUSTED_WRITE_DEFINITIONS[MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY],
}

_TRUSTED_WRITE_COMMITS = {

    definition["commit_capability"]: (prepare_capability, definition)
    for prepare_capability, definition in _TRUSTED_WRITE_DEFINITIONS.items()
}
_TRUSTED_WRITE_COMMITS[MISSED_PUNCH_APPROVE_CAPABILITY] = (
    MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY,
    _TRUSTED_WRITE_DEFINITIONS[MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY],
)

_CAPABILITY_SCOPES = {
    PENDING_BATCH_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    BUSINESS_TRIP_PREPARE_CAPABILITY: frozenset({"oa:write:draft"}),
    BUSINESS_TRIP_SAVE_CAPABILITY: frozenset({"oa:write:draft"}),
    BUSINESS_TRIP_SUBMIT_PREPARE_CAPABILITY: frozenset({"oa:write:submit"}),
    BUSINESS_TRIP_SUBMIT_CAPABILITY: frozenset({"oa:write:submit"}),
    LEAVE_PREPARE_CAPABILITY: frozenset({"oa:write:draft"}),
    LEAVE_SAVE_CAPABILITY: frozenset({"oa:write:draft"}),
    LEAVE_SUBMIT_PREPARE_CAPABILITY: frozenset({"oa:write:submit"}),
    LEAVE_SUBMIT_CAPABILITY: frozenset({"oa:write:submit"}),
    MISSED_PUNCH_PREPARE_CAPABILITY: frozenset({"oa:write:draft"}),
    MISSED_PUNCH_SAVE_CAPABILITY: frozenset({"oa:write:draft"}),
    MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY: frozenset(
        {"oa:write:approval"}
    ),
    MISSED_PUNCH_APPROVE_CAPABILITY: frozenset({"oa:write:approval"}),
    **MEETING_CREATE_WORKFLOW.scope_bindings(),
    **MEETING_ROOM_APPLICATION_CREATE_WORKFLOW.scope_bindings(),
    **MEETING_ROOM_APPLICATION_CANCEL_WORKFLOW.scope_bindings(),
    EFFICIENCY_DATA_APPROVAL_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    EFFICIENCY_DATA_APPROVE_CAPABILITY: frozenset({"oa:write:approval"}),
    TRAVEL_EXPENSE_APPROVAL_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    TRAVEL_EXPENSE_APPROVE_CAPABILITY: frozenset({"oa:write:approval"}),
    BUSINESS_TRIP_APPROVAL_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    BUSINESS_TRIP_APPROVE_CAPABILITY: frozenset({"oa:write:approval"}),
    LABOR_CONTRACT_RENEWAL_APPROVAL_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    LABOR_CONTRACT_RENEWAL_APPROVE_CAPABILITY: frozenset({"oa:write:approval"}),
    INTELLECTUAL_PROPERTY_DECLARATION_APPROVAL_PREPARE_CAPABILITY: frozenset(
        {"oa:write:approval"}
    ),
    INTELLECTUAL_PROPERTY_DECLARATION_APPROVE_CAPABILITY: frozenset(
        {"oa:write:approval"}
    ),
    OVERTIME_APPROVAL_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    OVERTIME_APPROVE_CAPABILITY: frozenset({"oa:write:approval"}),
    LEAVE_APPROVAL_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    LEAVE_APPROVE_CAPABILITY: frozenset({"oa:write:approval"}),
    RESIGNATION_APPROVAL_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    WORK_HANDOVER_APPROVAL_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    RESIGNATION_APPROVE_CAPABILITY: frozenset({"oa:write:approval"}),
    WORK_HANDOVER_APPROVE_CAPABILITY: frozenset({"oa:write:approval"}),
    FLIGHT_APPLICATION_APPROVAL_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    FLIGHT_APPLICATION_APPROVE_CAPABILITY: frozenset({"oa:write:approval"}),
    ATTENDANCE_CONFIRMATION_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    ATTENDANCE_CONFIRM_CAPABILITY: frozenset({"oa:write:approval"}),
    WEEKLY_REPORT_ACKNOWLEDGEMENT_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    WEEKLY_REPORT_ACKNOWLEDGE_CAPABILITY: frozenset({"oa:write:approval"}),
    STANDARD_COLLABORATION_APPROVAL_PREPARE_CAPABILITY: frozenset({"oa:write:approval"}),
    STANDARD_COLLABORATION_APPROVE_CAPABILITY: frozenset({"oa:write:approval"}),
    WORKFLOW_REVOKE_PREPARE_CAPABILITY: frozenset({"oa:write:revoke"}),
    WORKFLOW_REVOKE_CAPABILITY: frozenset({"oa:write:revoke"}),
    **TAIHUA_WORK_LOG_CREATE_WORKFLOW.scope_bindings(),
    **SMARTLIGHT_ALARM_REMARK_UPDATE_WORKFLOW.scope_bindings(),
    **SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_WORKFLOW.scope_bindings(),
    **SMARTLIGHT_ALARM_WORK_AREA_REVOKE_WORKFLOW.scope_bindings(),
    **SMARTLIGHT_RTU_ALARM_DISPOSE_WORKFLOW.scope_bindings(),
}


def capability_required_scopes(capability_name: str) -> frozenset[str]:
    try:
        return _CAPABILITY_SCOPES[capability_name]
    except KeyError as exc:
        raise KeyError(f"write capability has no MCP scope policy: {capability_name}") from exc


def resolve_write_function(name: str):
    """Resolve only registered adapter functions, never service globals."""
    if name not in WRITE_FUNCTION_NAMES:
        return None
    return globals().get(name)

WRITE_FUNCTION_NAMES = frozenset(
    definition[key]
    for definition in _TRUSTED_WRITE_DEFINITIONS.values()
    for key in ("prepare_function", "commit_function", "preflight_function", "field_schema_function")
    if key in definition
)
