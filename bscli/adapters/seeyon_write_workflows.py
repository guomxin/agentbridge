"""Static OA write declarations; business execution remains in the leaf modules.

The legacy and mixed-batch prepare entries share a canonical missed-punch commit
binding. Mixed-batch per-item dispatch and authorization stay in the existing runtime.
"""
from __future__ import annotations

from dataclasses import dataclass

from bscli.adapters.seeyon_business_trip import (
    BUSINESS_TRIP_FIELD_CARD_SCHEMA,
    BUSINESS_TRIP_PREPARE_CAPABILITY,
    BUSINESS_TRIP_PREPARE_INPUT_SCHEMA,
    BUSINESS_TRIP_SAVE_CAPABILITY,
    BUSINESS_TRIP_SAVE_INPUT_SCHEMA,
    BusinessTripContractMismatch,
    BusinessTripOutcomeUnknown,
    prepare_business_trip_draft,
    save_business_trip_draft,
)
from bscli.adapters.seeyon_business_trip_submit import (
    BUSINESS_TRIP_SUBMIT_CAPABILITY,
    BUSINESS_TRIP_SUBMIT_FIELD_CARD_SCHEMA,
    BUSINESS_TRIP_SUBMIT_INPUT_SCHEMA,
    BUSINESS_TRIP_SUBMIT_PREPARE_CAPABILITY,
    BUSINESS_TRIP_SUBMIT_PREPARE_INPUT_SCHEMA,
    prepare_business_trip_submission,
    submit_business_trip_request,
)
from bscli.adapters.seeyon_leave import (
    LEAVE_FIELD_CARD_SCHEMA,
    LEAVE_PREPARE_CAPABILITY,
    LEAVE_PREPARE_INPUT_SCHEMA,
    LEAVE_SAVE_CAPABILITY,
    LEAVE_SAVE_INPUT_SCHEMA,
    LeaveContractMismatch,
    LeaveOutcomeUnknown,
    prepare_leave_draft,
    save_leave_draft,
)
from bscli.adapters.seeyon_leave_submit import (
    LEAVE_SUBMIT_CAPABILITY,
    LEAVE_SUBMIT_FIELD_CARD_SCHEMA,
    LEAVE_SUBMIT_INPUT_SCHEMA,
    LEAVE_SUBMIT_PREPARE_CAPABILITY,
    LEAVE_SUBMIT_PREPARE_INPUT_SCHEMA,
    prepare_leave_submission,
    submit_leave_request,
)
from bscli.adapters.seeyon_missed_punch import (
    MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY,
    MISSED_PUNCH_APPROVAL_BATCH_PREPARE_INPUT_SCHEMA,
    MISSED_PUNCH_APPROVAL_FIELD_CARD_SCHEMA,
    MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY,
    MISSED_PUNCH_APPROVAL_PREPARE_INPUT_SCHEMA,
    MISSED_PUNCH_APPROVE_CAPABILITY,
    MISSED_PUNCH_APPROVE_INPUT_SCHEMA,
    MISSED_PUNCH_FIELD_CARD_SCHEMA,
    MISSED_PUNCH_PREPARE_CAPABILITY,
    MISSED_PUNCH_PREPARE_INPUT_SCHEMA,
    MISSED_PUNCH_SAVE_CAPABILITY,
    MISSED_PUNCH_SAVE_INPUT_SCHEMA,
    MissedPunchContractMismatch,
    MissedPunchOutcomeUnknown,
    approve_missed_punch_request,
    build_missed_punch_approval_batch_field_schema,
    prepare_missed_punch_approval,
    prepare_missed_punch_draft,
    save_missed_punch_draft,
)
from bscli.adapters.seeyon_pending_actions import (
    ATTENDANCE_CONFIRMATION_FIELD_CARD_SCHEMA,
    BUSINESS_TRIP_APPROVAL_FIELD_CARD_SCHEMA,
    EFFICIENCY_DATA_APPROVAL_FIELD_CARD_SCHEMA,
    FLIGHT_APPLICATION_APPROVAL_FIELD_CARD_SCHEMA,
    INTELLECTUAL_PROPERTY_DECLARATION_APPROVAL_FIELD_CARD_SCHEMA,
    LABOR_CONTRACT_RENEWAL_APPROVAL_FIELD_CARD_SCHEMA,
    LEAVE_APPROVAL_FIELD_CARD_SCHEMA,
    OVERTIME_APPROVAL_FIELD_CARD_SCHEMA,
    PENDING_ACTION_CAPABILITY_DEFINITIONS,
    PENDING_ACTION_COMMIT_INPUT_SCHEMA,
    PENDING_ACTION_PREPARE_INPUT_SCHEMA,
    PendingActionContractMismatch,
    PendingActionOutcomeUnknown,
    RESIGNATION_APPROVAL_FIELD_CARD_SCHEMA,
    STANDARD_COLLABORATION_APPROVAL_FIELD_CARD_SCHEMA,
    TRAVEL_EXPENSE_APPROVAL_FIELD_CARD_SCHEMA,
    WEEKLY_REPORT_ACKNOWLEDGEMENT_FIELD_CARD_SCHEMA,
    WORK_HANDOVER_APPROVAL_FIELD_CARD_SCHEMA,
    acknowledge_weekly_report,
    approve_business_trip_request,
    approve_efficiency_data,
    approve_flight_application,
    approve_intellectual_property_declaration,
    approve_labor_contract_renewal,
    approve_leave_request,
    approve_overtime,
    approve_resignation,
    approve_standard_collaboration,
    approve_travel_expense,
    approve_work_handover,
    confirm_attendance,
    preflight_pending_action,
    prepare_attendance_confirmation,
    prepare_business_trip_approval,
    prepare_efficiency_data_approval,
    prepare_flight_application_approval,
    prepare_intellectual_property_declaration_approval,
    prepare_labor_contract_renewal_approval,
    prepare_leave_approval,
    prepare_overtime_approval,
    prepare_resignation_approval,
    prepare_standard_collaboration_approval,
    prepare_travel_expense_approval,
    prepare_weekly_report_acknowledgement,
    prepare_work_handover_approval,
)
from bscli.adapters.seeyon_pending_batch import (
    PENDING_BATCH_INPUT_SCHEMA,
    PENDING_BATCH_PREPARE_CAPABILITY,
)
from bscli.adapters.seeyon_workflow_revoke import (
    WORKFLOW_REVOKE_CAPABILITY,
    WORKFLOW_REVOKE_FIELD_CARD_SCHEMA,
    WORKFLOW_REVOKE_INPUT_SCHEMA,
    WORKFLOW_REVOKE_PREPARE_CAPABILITY,
    WORKFLOW_REVOKE_PREPARE_INPUT_SCHEMA,
    WorkflowRevokeContractMismatch,
    WorkflowRevokeOutcomeUnknown,
    prepare_workflow_revoke,
    revoke_workflow,
)
from bscli.core.capability import CapabilitySpec
from bscli.core.write_workflow import (
    CommitHandler,
    PrepareHandler,
    WritePrepareAliasDefinition,
    WriteWorkflowDefinition,
)


@dataclass(frozen=True)
class _PendingActionBinding:
    field_schema: dict
    prepare_function: PrepareHandler
    commit_function: CommitHandler
    field_message: str
    authorization_message: str


_PENDING_ACTION_BINDINGS = {
    'efficiency_data': _PendingActionBinding(
        field_schema=EFFICIENCY_DATA_APPROVAL_FIELD_CARD_SCHEMA,
        prepare_function=prepare_efficiency_data_approval,
        commit_function=approve_efficiency_data,
        field_message="The efficiency-data opinion must be entered in the trusted field card.",
        authorization_message="The efficiency-data approval requires trusted confirmation.",
    ),
    'travel_expense': _PendingActionBinding(
        field_schema=TRAVEL_EXPENSE_APPROVAL_FIELD_CARD_SCHEMA,
        prepare_function=prepare_travel_expense_approval,
        commit_function=approve_travel_expense,
        field_message="The travel-expense opinion must be entered in the trusted field card.",
        authorization_message="The travel-expense approval requires trusted confirmation.",
    ),
    'business_trip': _PendingActionBinding(
        field_schema=BUSINESS_TRIP_APPROVAL_FIELD_CARD_SCHEMA,
        prepare_function=prepare_business_trip_approval,
        commit_function=approve_business_trip_request,
        field_message="The business-trip approval opinion must be entered in the trusted field card.",
        authorization_message="The business-trip approval requires trusted confirmation.",
    ),
    'labor_contract_renewal': _PendingActionBinding(
        field_schema=LABOR_CONTRACT_RENEWAL_APPROVAL_FIELD_CARD_SCHEMA,
        prepare_function=prepare_labor_contract_renewal_approval,
        commit_function=approve_labor_contract_renewal,
        field_message="The labor-contract renewal opinion must be entered in the trusted field card.",
        authorization_message="The labor-contract renewal approval requires trusted confirmation.",
    ),
    'intellectual_property_declaration': _PendingActionBinding(
        field_schema=INTELLECTUAL_PROPERTY_DECLARATION_APPROVAL_FIELD_CARD_SCHEMA,
        prepare_function=prepare_intellectual_property_declaration_approval,
        commit_function=approve_intellectual_property_declaration,
        field_message="The intellectual-property declaration opinion must be entered "
                        "in the trusted field card.",
        authorization_message="The intellectual-property declaration approval requires trusted "
                        "confirmation.",
    ),
    'overtime': _PendingActionBinding(
        field_schema=OVERTIME_APPROVAL_FIELD_CARD_SCHEMA,
        prepare_function=prepare_overtime_approval,
        commit_function=approve_overtime,
        field_message="The overtime approval opinion must be entered in the trusted field card.",
        authorization_message="The overtime approval requires trusted confirmation.",
    ),
    'leave': _PendingActionBinding(
        field_schema=LEAVE_APPROVAL_FIELD_CARD_SCHEMA,
        prepare_function=prepare_leave_approval,
        commit_function=approve_leave_request,
        field_message="The leave-request approval opinion must be entered in the trusted field card.",
        authorization_message="The leave-request approval requires trusted confirmation.",
    ),
    'resignation': _PendingActionBinding(
        field_schema=RESIGNATION_APPROVAL_FIELD_CARD_SCHEMA,
        prepare_function=prepare_resignation_approval,
        commit_function=approve_resignation,
        field_message="The resignation approval opinion must be entered in the trusted field card.",
        authorization_message="The resignation approval requires trusted confirmation.",
    ),
    'work_handover': _PendingActionBinding(
        field_schema=WORK_HANDOVER_APPROVAL_FIELD_CARD_SCHEMA,
        prepare_function=prepare_work_handover_approval,
        commit_function=approve_work_handover,
        field_message="The work-handover approval opinion must be entered in the trusted field card.",
        authorization_message="The work-handover approval requires trusted confirmation.",
    ),
    'flight_application': _PendingActionBinding(
        field_schema=FLIGHT_APPLICATION_APPROVAL_FIELD_CARD_SCHEMA,
        prepare_function=prepare_flight_application_approval,
        commit_function=approve_flight_application,
        field_message="The flight-application opinion must be entered in the trusted field card.",
        authorization_message="The flight-application approval requires trusted confirmation.",
    ),
    'attendance_confirmation': _PendingActionBinding(
        field_schema=ATTENDANCE_CONFIRMATION_FIELD_CARD_SCHEMA,
        prepare_function=prepare_attendance_confirmation,
        commit_function=confirm_attendance,
        field_message="The attendance-confirmation opinion must be entered in the trusted field card.",
        authorization_message="The attendance confirmation requires trusted confirmation.",
    ),
    'weekly_report': _PendingActionBinding(
        field_schema=WEEKLY_REPORT_ACKNOWLEDGEMENT_FIELD_CARD_SCHEMA,
        prepare_function=prepare_weekly_report_acknowledgement,
        commit_function=acknowledge_weekly_report,
        field_message="The weekly-report opinion must be entered in the trusted field card.",
        authorization_message="The weekly-report acknowledgement requires trusted confirmation.",
    ),
    'standard_collaboration': _PendingActionBinding(
        field_schema=STANDARD_COLLABORATION_APPROVAL_FIELD_CARD_SCHEMA,
        prepare_function=prepare_standard_collaboration_approval,
        commit_function=approve_standard_collaboration,
        field_message="The collaboration opinion must be entered in the trusted field card.",
        authorization_message="The collaboration approval requires trusted confirmation.",
    ),
}


def _pending_action_workflows() -> dict[str, WriteWorkflowDefinition]:
    workflows = {}
    for definition in PENDING_ACTION_CAPABILITY_DEFINITIONS:
        profile_name = definition["profile"].replace("_", " ")
        workflow_prefix = definition["workflow_prefix"]
        action_kind = definition["action_kind"]
        binding = _PENDING_ACTION_BINDINGS[definition["profile"]]
        workflows[definition["prepare_capability"]] = WriteWorkflowDefinition(
            prepare_spec=CapabilitySpec(
                name=definition["prepare_capability"],
                version="0.1.0",
                description=(
                    f"Collect a trusted opinion, validate one exact pending "
                    f"{profile_name} item, and create separate {action_kind} confirmation."
                ),
                input_schema=PENDING_ACTION_PREPARE_INPUT_SCHEMA,
                output_schema={"type": "object"},
                effect="controlled_write",
                adapter="seeyon-central",
                workflow=f"{workflow_prefix}-prepare-v1",
            ),
            commit_spec=CapabilitySpec(
                name=definition["commit_capability"],
                version="0.1.0",
                description=(
                    f"Consume one trusted authorization, process the frozen "
                    f"{profile_name} item, and verify pending disappearance."
                ),
                input_schema=PENDING_ACTION_COMMIT_INPUT_SCHEMA,
                output_schema={"type": "object"},
                effect="controlled_write",
                adapter="seeyon-central",
                workflow=f"{workflow_prefix}-commit-v1",
            ),
            required_scopes=frozenset({"oa:write:approval"}),
            field_schema=binding.field_schema,
            context_fields=("affair_id",),
            prepare_function=binding.prepare_function,
            commit_function=binding.commit_function,
            contract_error=PendingActionContractMismatch,
            outcome_error=PendingActionOutcomeUnknown,
            field_message=binding.field_message,
            authorization_message=binding.authorization_message,
            preflight_function=preflight_pending_action,
            preflight_profile=definition["profile"],
        )
    return workflows


_MISSED_PUNCH_APPROVAL_WORKFLOW = WriteWorkflowDefinition(
    prepare_spec=CapabilitySpec(
        name=MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY,
        version="0.2.0",
        description=(
            "Collect an approval opinion in a trusted card, validate one exact "
            "pending missed-punch item, and create a separate approval authorization."
        ),
        input_schema=MISSED_PUNCH_APPROVAL_PREPARE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="controlled_write",
        adapter="seeyon-central",
        workflow="missed-punch-approval-prepare-v1",
    ),
    commit_spec=CapabilitySpec(
        name=MISSED_PUNCH_APPROVE_CAPABILITY,
        version="0.1.0",
        description=(
            "Consume one trusted authorization, approve the frozen missed-punch "
            "target, and verify that it left the pending collection."
        ),
        input_schema=MISSED_PUNCH_APPROVE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="controlled_write",
        adapter="seeyon-central",
        workflow="missed-punch-approval-commit-v1",
    ),
    required_scopes=frozenset({"oa:write:approval"}),
    field_schema=MISSED_PUNCH_APPROVAL_FIELD_CARD_SCHEMA,
    context_fields=("affair_id",),
    prepare_function=prepare_missed_punch_approval,
    commit_function=approve_missed_punch_request,
    contract_error=MissedPunchContractMismatch,
    outcome_error=MissedPunchOutcomeUnknown,
    field_message="The missed-punch approval opinion must be entered in the trusted field card.",
    authorization_message="The missed-punch approval plan requires confirmation in the trusted action card.",
)


def _missed_punch_prepare_alias(prepare_spec: CapabilitySpec) -> WritePrepareAliasDefinition:
    return WritePrepareAliasDefinition(
        prepare_spec=prepare_spec,
        canonical_workflow=_MISSED_PUNCH_APPROVAL_WORKFLOW,
        context_fields=("batch_id", "affair_id"),
        field_message="The current missed-punch opinion must be entered in the trusted field card.",
        authorization_message="The current missed-punch approval plan requires confirmation in the trusted action card.",
        field_schema_function=build_missed_punch_approval_batch_field_schema,
    )


OA_WRITE_DECLARATIONS: dict[str, WriteWorkflowDefinition | WritePrepareAliasDefinition] = {
    BUSINESS_TRIP_PREPARE_CAPABILITY: WriteWorkflowDefinition(
        prepare_spec=CapabilitySpec(
            name=BUSINESS_TRIP_PREPARE_CAPABILITY,
            version="0.3.0",
            description=(
                "Collect business-trip fields through a trusted card, validate the live "
                "OA form, and create a separate one-time confirmation card."
            ),
            input_schema=BUSINESS_TRIP_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="business-trip-draft-prepare-v2",
        ),
        commit_spec=CapabilitySpec(
            name=BUSINESS_TRIP_SAVE_CAPABILITY,
            version="0.1.0",
            description=(
                "Consume a trusted authorization once, save the frozen business-trip "
                "plan as an OA wait-send draft, and verify it by server readback."
            ),
            input_schema=BUSINESS_TRIP_SAVE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="business-trip-draft-save-v1",
        ),
        required_scopes=frozenset({"oa:write:draft"}),
        field_schema=BUSINESS_TRIP_FIELD_CARD_SCHEMA,
        context_fields=(),
        prepare_function=prepare_business_trip_draft,
        commit_function=save_business_trip_draft,
        contract_error=BusinessTripContractMismatch,
        outcome_error=BusinessTripOutcomeUnknown,
        field_message="Business-trip fields must be entered in the trusted field card.",
        authorization_message="The business-trip draft plan requires confirmation in the trusted action card.",
    ),
    BUSINESS_TRIP_SUBMIT_PREPARE_CAPABILITY: WriteWorkflowDefinition(
        prepare_spec=CapabilitySpec(
            name=BUSINESS_TRIP_SUBMIT_PREPARE_CAPABILITY,
            version="0.3.0",
            description=(
                "Collect business-trip fields through a trusted card, validate the live "
                "OA form and sent-item baseline, and create a separate submit authorization."
            ),
            input_schema=BUSINESS_TRIP_SUBMIT_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="business-trip-submit-prepare-v2",
        ),
        commit_spec=CapabilitySpec(
            name=BUSINESS_TRIP_SUBMIT_CAPABILITY,
            version="0.2.0",
            description=(
                "Consume one trusted authorization, submit the frozen business-trip "
                "request, and verify one new readable item in the OA sent collection."
            ),
            input_schema=BUSINESS_TRIP_SUBMIT_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="business-trip-submit-commit-v2",
        ),
        required_scopes=frozenset({"oa:write:submit"}),
        field_schema=BUSINESS_TRIP_SUBMIT_FIELD_CARD_SCHEMA,
        context_fields=(),
        prepare_function=prepare_business_trip_submission,
        commit_function=submit_business_trip_request,
        contract_error=BusinessTripContractMismatch,
        outcome_error=BusinessTripOutcomeUnknown,
        field_message="Business-trip fields must be entered in the trusted field card.",
        authorization_message="The business-trip submission plan requires confirmation in the trusted action card.",
    ),
    LEAVE_PREPARE_CAPABILITY: WriteWorkflowDefinition(
        prepare_spec=CapabilitySpec(
            name=LEAVE_PREPARE_CAPABILITY,
            version="0.2.0",
            description=(
                "Collect supported leave-request fields through a trusted card, validate "
                "the live OA form, and create a separate draft-save authorization."
            ),
            input_schema=LEAVE_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="leave-draft-prepare-v1",
        ),
        commit_spec=CapabilitySpec(
            name=LEAVE_SAVE_CAPABILITY,
            version="0.1.0",
            description=(
                "Consume one trusted authorization, save the frozen leave request as an "
                "OA wait-send draft, and verify it by server readback without submission."
            ),
            input_schema=LEAVE_SAVE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="leave-draft-save-v1",
        ),
        required_scopes=frozenset({"oa:write:draft"}),
        field_schema=LEAVE_FIELD_CARD_SCHEMA,
        context_fields=(),
        prepare_function=prepare_leave_draft,
        commit_function=save_leave_draft,
        contract_error=LeaveContractMismatch,
        outcome_error=LeaveOutcomeUnknown,
        field_message="Leave-request fields must be entered in the trusted field card.",
        authorization_message="The leave draft plan requires confirmation in the trusted action card.",
    ),
    LEAVE_SUBMIT_PREPARE_CAPABILITY: WriteWorkflowDefinition(
        prepare_spec=CapabilitySpec(
            name=LEAVE_SUBMIT_PREPARE_CAPABILITY,
            version="0.1.0",
            description=(
                "Collect supported leave-request fields through a trusted card, validate "
                "the live OA form and sent-item baseline, and create a submit authorization."
            ),
            input_schema=LEAVE_SUBMIT_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="leave-submit-prepare-v1",
        ),
        commit_spec=CapabilitySpec(
            name=LEAVE_SUBMIT_CAPABILITY,
            version="0.1.0",
            description=(
                "Consume one trusted authorization, submit the frozen leave request, "
                "and verify one new readable item in the OA sent collection."
            ),
            input_schema=LEAVE_SUBMIT_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="leave-submit-commit-v1",
        ),
        required_scopes=frozenset({"oa:write:submit"}),
        field_schema=LEAVE_SUBMIT_FIELD_CARD_SCHEMA,
        context_fields=(),
        prepare_function=prepare_leave_submission,
        commit_function=submit_leave_request,
        contract_error=LeaveContractMismatch,
        outcome_error=LeaveOutcomeUnknown,
        field_message="Leave-request fields must be entered in the trusted field card.",
        authorization_message="The leave submission plan requires confirmation in the trusted action card.",
    ),
    MISSED_PUNCH_PREPARE_CAPABILITY: WriteWorkflowDefinition(
        prepare_spec=CapabilitySpec(
            name=MISSED_PUNCH_PREPARE_CAPABILITY,
            version="0.2.0",
            description=(
                "Collect missed-punch fields in a trusted card, validate the live OA "
                "form, and create a separate draft-save authorization."
            ),
            input_schema=MISSED_PUNCH_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="missed-punch-draft-prepare-v1",
        ),
        commit_spec=CapabilitySpec(
            name=MISSED_PUNCH_SAVE_CAPABILITY,
            version="0.1.0",
            description=(
                "Consume one trusted authorization, save the frozen missed-punch plan "
                "as an OA wait-send draft, and verify it without submitting approval."
            ),
            input_schema=MISSED_PUNCH_SAVE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="missed-punch-draft-save-v1",
        ),
        required_scopes=frozenset({"oa:write:draft"}),
        field_schema=MISSED_PUNCH_FIELD_CARD_SCHEMA,
        context_fields=(),
        prepare_function=prepare_missed_punch_draft,
        commit_function=save_missed_punch_draft,
        contract_error=MissedPunchContractMismatch,
        outcome_error=MissedPunchOutcomeUnknown,
        field_message="Missed-punch fields must be entered in the trusted field card.",
        authorization_message="The missed-punch draft plan requires confirmation in the trusted action card.",
    ),
    MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY: _MISSED_PUNCH_APPROVAL_WORKFLOW,
    MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY: _missed_punch_prepare_alias(
        CapabilitySpec(
            name=MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY,
            version="0.1.0",
            description=(
                "Compatibility entry; use oa.workflow.pending.batch.prepare for new batches. "
                "Freeze up to ten current pending missed-punch items and process "
                "them sequentially with independent trusted input and authorization."
            ),
            input_schema=MISSED_PUNCH_APPROVAL_BATCH_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="missed-punch-approval-batch-prepare-v1",
        ),
    ),
    WORKFLOW_REVOKE_PREPARE_CAPABILITY: WriteWorkflowDefinition(
        prepare_spec=CapabilitySpec(
            name=WORKFLOW_REVOKE_PREPARE_CAPABILITY,
            version="0.1.0",
            description=(
                "Collect a revoke comment in a trusted card, resolve one exact active "
                "sent workflow, run non-destructive OA eligibility checks, and create "
                "a separate revoke authorization."
            ),
            input_schema=WORKFLOW_REVOKE_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="workflow-revoke-prepare-v1",
        ),
        commit_spec=CapabilitySpec(
            name=WORKFLOW_REVOKE_CAPABILITY,
            version="0.1.0",
            description=(
                "Consume one trusted authorization, revoke the frozen sent workflow "
                "through OA's native action, and verify its revoked wait-send state."
            ),
            input_schema=WORKFLOW_REVOKE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="workflow-revoke-commit-v1",
        ),
        required_scopes=frozenset({"oa:write:revoke"}),
        field_schema=WORKFLOW_REVOKE_FIELD_CARD_SCHEMA,
        context_fields=("affair_id",),
        prepare_function=prepare_workflow_revoke,
        commit_function=revoke_workflow,
        contract_error=WorkflowRevokeContractMismatch,
        outcome_error=WorkflowRevokeOutcomeUnknown,
        field_message="The workflow revoke comment must be entered in the trusted field card.",
        authorization_message="The workflow revoke plan requires confirmation in the trusted action card.",
    ),
    **_pending_action_workflows(),
    # Compatibility fallback only: the runtime selects each mixed-batch item.
    PENDING_BATCH_PREPARE_CAPABILITY: _missed_punch_prepare_alias(
        CapabilitySpec(
            name=PENDING_BATCH_PREPARE_CAPABILITY,
            version="0.1.0",
            description=(
                "Freeze a variable-length selection of current OA pending items, including mixed supported "
                "workflow types. Process each with independent trusted input, authorization and verification. "
                "Use this for multiple/all pending items instead of promising to continue singular prepares. "
                "Incomplete sources, unsupported selections and overflow stop before any approval."
            ),
            input_schema=PENDING_BATCH_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="pending-batch-prepare-v1",
        ),
    ),
}


def oa_write_capability_specs_by_name() -> dict[str, CapabilitySpec]:
    """Return isolated specs for callers to insert at their existing registry positions."""
    specs = {}
    for declaration in OA_WRITE_DECLARATIONS.values():
        for spec in declaration.capability_specs():
            if spec.name in specs:
                raise ValueError(f"duplicate OA write capability declaration: {spec.name}")
            specs[spec.name] = spec
    return specs
