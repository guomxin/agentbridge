from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import time
from typing import Any, Callable

from agentbridge.adapters.seeyon_business_trip import (
    BUSINESS_TRIP_FIELD_CARD_SCHEMA,
    BUSINESS_TRIP_FORM_APP_ID,
    BUSINESS_TRIP_PREPARE_INPUT_SCHEMA,
    BUSINESS_TRIP_SAVE_INPUT_SCHEMA,
    BUSINESS_TRIP_TEMPLATE_ID,
    BUSINESS_TRIP_TEMPLATE_TITLE,
    BusinessTripContractMismatch,
    BusinessTripOutcomeUnknown,
    _assert_readback,
    _fill_business_trip_form,
    _open_and_validate_form,
    _read_business_trip_form,
    _resolve_template,
    _validate_optional_inputs,
    business_trip_contract_fingerprint,
    business_trip_summary,
    normalize_business_trip_inputs,
)
from agentbridge.adapters.seeyon_sent_readback import (
    new_sent_candidates,
    sent_snapshot,
)
from agentbridge.adapters.seeyon_submit_phases import (
    SeeyonBusinessRuleRejected,
    SeeyonBusinessValidationRequired,
    SubmissionPhaseTracker,
    pump_browser_events,
)


BUSINESS_TRIP_SUBMIT_PREPARE_CAPABILITY = "oa.business_trip.submit.prepare"
BUSINESS_TRIP_SUBMIT_CAPABILITY = "oa.business_trip.submit"
BUSINESS_TRIP_SUBMIT_CONTRACT_VERSION = "seeyon-business-trip-submit-v5"
BUSINESS_TRIP_SUBMIT_PREPARE_INPUT_SCHEMA = BUSINESS_TRIP_PREPARE_INPUT_SCHEMA
BUSINESS_TRIP_SUBMIT_INPUT_SCHEMA = BUSINESS_TRIP_SAVE_INPUT_SCHEMA

BUSINESS_TRIP_SUBMIT_FIELD_CARD_SCHEMA = deepcopy(BUSINESS_TRIP_FIELD_CARD_SCHEMA)
BUSINESS_TRIP_SUBMIT_FIELD_CARD_SCHEMA.update(
    {
        "schema_version": "agentbridge.oa_business_trip_submit_fields.v2",
        "title": "填写并提交出差申请",
        "effect": "生成一份待确认的出差申请提交计划",
        "notice": "字段提交后还需单独授权；授权后会正式发送并进入 OA 审批流程。",
    }
)


class BusinessTripBusinessValidationRequired(SeeyonBusinessValidationRequired):
    pass


class BusinessTripSubmissionBlocked(
    BusinessTripContractMismatch,
    SeeyonBusinessRuleRejected,
):
    pass


def prepare_business_trip_submission(adapter, worker, arguments: dict) -> dict:
    inputs = normalize_business_trip_inputs(arguments)
    sent_snapshot = _sent_snapshot(adapter, worker)
    template = _resolve_template(adapter.list_templates(worker))
    page, frame = _open_and_validate_form(worker, template)
    _validate_optional_inputs(page, inputs)
    _fill_business_trip_form(page, frame, inputs)
    readback = _read_business_trip_form(page, frame)
    _assert_readback(inputs, readback, stage="prepare")
    expected_subject = str(readback.get("subject") or "").strip()
    if not expected_subject:
        raise BusinessTripContractMismatch(
            "The OA business-trip form did not produce a stable submission subject."
        )
    return {
        "plan": {
            "schema_version": "agentbridge.oa_business_trip_submit_plan.v1",
            "business_intent": "submit_business_trip_request",
            "target": {
                "template_title": template["title"],
                "template_id": template["template_id"],
                "form_app_id": template["form_app_id"],
                "launch_url": template["href"],
                "expected_subject": expected_subject,
            },
            "form_contract": {
                "version": BUSINESS_TRIP_SUBMIT_CONTRACT_VERSION,
                "fingerprint": business_trip_submit_contract_fingerprint(),
                "send_control": "sendId_a",
                "forbidden_controls": ["saveDraft_a"],
            },
            "exact_input": inputs,
            "sent_baseline_affair_ids": sent_snapshot["affair_ids"],
            "preconditions": {
                "template_resolved": True,
                "cap4_frame_loaded": True,
                "form_fields_matched": True,
                "send_control_present": True,
                "sent_collection_readable": True,
            },
            "expected_effect": {
                "workflow_submitted": True,
                "submitted_count": 1,
                "verification": [
                    "oa_submission_phase_observation",
                    "authoritative_sent_grid_delta",
                    "authoritative_sent_detail_readback",
                ],
            },
        },
        "summary": business_trip_submit_summary(inputs),
    }


def submit_business_trip_request(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
    timeout_seconds: float = 75,
) -> dict:
    _validate_frozen_submit_plan(plan)
    inputs = normalize_business_trip_inputs(plan["exact_input"])
    template = _resolve_template(adapter.list_templates(worker))
    target = plan["target"]
    if any(
        (
            template["template_id"] != target.get("template_id"),
            template["form_app_id"] != target.get("form_app_id"),
            template["title"] != target.get("template_title"),
        )
    ):
        raise BusinessTripContractMismatch(
            "The OA business-trip template changed after submission authorization."
        )
    page, frame = _open_and_validate_form(worker, template)
    _validate_optional_inputs(page, inputs)
    _fill_business_trip_form(page, frame, inputs)
    precommit_readback = _read_business_trip_form(page, frame)
    _assert_readback(inputs, precommit_readback, stage="precommit")
    expected_subject = str(target.get("expected_subject") or "").strip()
    if str(precommit_readback.get("subject") or "").strip() != expected_subject:
        raise BusinessTripContractMismatch(
            "The OA business-trip subject changed after submission authorization."
        )

    validation_overrides = _validation_overrides(plan)
    phase_tracker = SubmissionPhaseTracker(
        {
            str(item.get("fingerprint") or "")
            for item in validation_overrides
            if isinstance(item, dict)
        }
    )
    boundary_crossed = False

    phase_tracker.install_page_observers(page)
    page.on("response", phase_tracker.observe_response)
    page.on("dialog", phase_tracker.observe_dialog)
    page.on("pageerror", phase_tracker.observe_page_error)
    with _sent_readback_worker(worker) as readback_worker:
        enter_commit_boundary()
        boundary_crossed = True
        try:
            page.locator("#sendId_a").click(timeout=10000)
            submitted = _wait_for_sent_readback(
                adapter,
                readback_worker,
                page=page,
                baseline_affair_ids=set(plan.get("sent_baseline_affair_ids") or []),
                expected_template_id=str(target["template_id"]),
                expected_form_app_id=str(target["form_app_id"]),
                title_markers=(
                    BUSINESS_TRIP_TEMPLATE_TITLE,
                    inputs["start_time"],
                    inputs["end_time"],
                ),
                timeout_seconds=timeout_seconds,
                phase_tracker=phase_tracker,
                validation_overrides=validation_overrides,
            )
            return {
                "schema_version": "agentbridge.oa_business_trip_submit_result.v1",
                "business_intent": "submit_business_trip_request",
                "workflow_submitted": True,
                "submitted_count": 1,
                "submitted": submitted,
                "verification": {
                    "confirmed": True,
                    "methods": [
                        "oa_submission_phase_observation",
                        "authoritative_sent_grid_delta",
                        "authoritative_sent_detail_readback",
                    ],
                },
                "request_evidence": phase_tracker.evidence,
                "transport": "central_browser_session",
            }
        except (
            BusinessTripBusinessValidationRequired,
            BusinessTripSubmissionBlocked,
        ):
            raise
        except BusinessTripOutcomeUnknown as exc:
            raise BusinessTripOutcomeUnknown(
                f"{exc} {phase_tracker.unknown_outcome_detail()}"
            ) from exc
        except BaseException as exc:
            if boundary_crossed:
                raise BusinessTripOutcomeUnknown(
                    "The OA business-trip send boundary was crossed, but verification failed. "
                    f"{phase_tracker.unknown_outcome_detail()}"
                ) from exc
            raise


def business_trip_submit_summary(inputs: dict) -> dict:
    draft_summary = business_trip_summary(inputs)
    return {
        "title": "提交出差申请",
        "system": "致远 OA",
        "effect": "立即发送并进入审批流程",
        "authorization_notice": "授权后会正式提交该出差申请并进入 OA 审批流程，不会只保存为草稿。",
        "authorize_label": "授权提交审批",
        "fields": draft_summary["fields"],
        "submitted_count": 1,
    }


def business_trip_submit_contract_fingerprint() -> str:
    contract = {
        "version": BUSINESS_TRIP_SUBMIT_CONTRACT_VERSION,
        "template_title": BUSINESS_TRIP_TEMPLATE_TITLE,
        "template_id": BUSINESS_TRIP_TEMPLATE_ID,
        "form_app_id": BUSINESS_TRIP_FORM_APP_ID,
        "base_form_fingerprint": business_trip_contract_fingerprint(),
        "send_control": "sendId_a",
        "forbidden_controls": ["saveDraft_a"],
        "verification": [
            "oa_submission_phase_observation",
            "authoritative_sent_grid_delta",
            "authoritative_sent_detail_readback",
        ],
    }
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _sent_snapshot(adapter, worker) -> dict:
    return sent_snapshot(adapter, worker)


@contextmanager
def _sent_readback_worker(worker):
    fork_page = getattr(worker, "fork_page", None)
    if not callable(fork_page):
        # Lightweight test workers do not own a browser page.
        yield worker
        return
    with fork_page() as readback_worker:
        yield readback_worker


def _wait_for_sent_readback(
    adapter,
    worker,
    *,
    page,
    baseline_affair_ids: set[str],
    expected_template_id: str,
    expected_form_app_id: str,
    title_markers: tuple[str, ...],
    timeout_seconds: float,
    phase_tracker: SubmissionPhaseTracker,
    validation_overrides: list[dict[str, Any]],
) -> dict:
    deadline = time.monotonic() + max(timeout_seconds, 5)
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        phase_tracker.observe_page_confirmation(page)
        _handle_business_validation(
            page,
            phase_tracker,
            validation_overrides=validation_overrides,
        )
        try:
            candidates = new_sent_candidates(
                adapter,
                worker,
                baseline_affair_ids=baseline_affair_ids,
                template_id=expected_template_id,
                form_app_id=expected_form_app_id,
                title_markers=title_markers,
            )
            if len(candidates) == 1:
                item = candidates[0]
                affair_id = str(item.get("affair_id") or "")
                source_item, detail = adapter.resolve_sent_workflow_row_detail(
                    worker,
                    source_item=item,
                )
                detail_title = str(detail.get("title") or source_item.get("title") or "")
                if not all(marker in detail_title for marker in title_markers):
                    raise BusinessTripOutcomeUnknown(
                        "The new sent OA item detail did not match the authorized identity."
                    )
                return {
                    "affair_id": affair_id,
                    "title": detail_title,
                    "state": "sent",
                    "detail_readable": True,
                    "field_count": len(detail.get("fields") or []),
                }
            if len(candidates) > 1:
                raise BusinessTripOutcomeUnknown(
                    "Multiple new sent OA items matched the authorized subject."
                )
        except (
            BusinessTripBusinessValidationRequired,
            BusinessTripSubmissionBlocked,
            BusinessTripOutcomeUnknown,
        ):
            raise
        except BaseException as exc:
            last_error = exc
        pump_browser_events(page)
        phase_tracker.observe_page_confirmation(page)
        _handle_business_validation(
            page,
            phase_tracker,
            validation_overrides=validation_overrides,
        )
    message = "The submitted business-trip request was not confirmed in the OA sent collection."
    if last_error is not None:
        message += f" Last readback error: {type(last_error).__name__}."
    raise BusinessTripOutcomeUnknown(message)


def _validation_overrides(plan: dict[str, Any]) -> list[dict[str, Any]]:
    overrides = plan.get("business_validation_overrides")
    if isinstance(overrides, list):
        return [dict(item) for item in overrides if isinstance(item, dict)]
    legacy = plan.get("business_validation_override")
    return [dict(legacy)] if isinstance(legacy, dict) else []


def _handle_business_validation(
    page,
    phase_tracker: SubmissionPhaseTracker,
    *,
    validation_overrides: list[dict[str, Any]],
) -> None:
    validation = phase_tracker.pending_business_validation
    if validation is None:
        return
    message = str(validation.get("message") or "OA business validation failed")
    if not validation.get("can_continue"):
        raise BusinessTripSubmissionBlocked(message)
    authorized_fingerprints = {
        str(item.get("fingerprint") or "")
        for item in validation_overrides
        if isinstance(item, dict)
    }
    if validation.get("fingerprint") not in authorized_fingerprints:
        raise BusinessTripBusinessValidationRequired(validation)
    if phase_tracker.business_validation_was_continued(validation["fingerprint"]):
        raise BusinessTripSubmissionBlocked(
            "The same OA business validation reappeared after its authorized Continue action."
        )
    if validation.get("control_already_activated"):
        phase_tracker.mark_business_validation_continued()
        return

    control_selector = str(validation.get("control_selector") or "").strip()
    control_text = str(validation.get("control_text") or "\u7ee7\u7eed").strip()
    control_scope = page
    control_frame_url = str(validation.get("control_frame_url") or "").strip()
    if control_frame_url:
        frames = getattr(page, "frames", [])
        frames = frames() if callable(frames) else frames
        matching_frames = [
            frame
            for frame in list(frames or [])
            if str(getattr(frame, "url", "") or "") == control_frame_url
        ]
        if len(matching_frames) != 1:
            raise BusinessTripOutcomeUnknown(
                "The authorized OA confirmation frame could not be identified uniquely."
            )
        control_scope = matching_frames[0]
    candidates = (
        control_scope.locator(f"{control_selector}:visible")
        if control_selector
        else control_scope.get_by_text(control_text, exact=True)
    )
    try:
        candidates.last.wait_for(state="visible", timeout=10000)
    except Exception as exc:
        raise BusinessTripOutcomeUnknown(
            "The authorized OA validation appeared, but its Continue control did not load."
        ) from exc
    visible = [
        candidates.nth(index)
        for index in range(candidates.count())
        if candidates.nth(index).is_visible()
    ]
    if len(visible) != 1:
        raise BusinessTripOutcomeUnknown(
            "The authorized OA validation appeared, but its Continue control was not unique."
        )
    try:
        visible[0].click(timeout=10000)
    except Exception as exc:
        raise BusinessTripOutcomeUnknown(
            "The authorized OA validation Continue control could not be activated."
        ) from exc
    phase_tracker.mark_business_validation_continued()


def _validate_frozen_submit_plan(plan: dict) -> None:
    if not isinstance(plan, dict) or plan.get("business_intent") != "submit_business_trip_request":
        raise BusinessTripContractMismatch(
            "The frozen plan is not a business-trip submission plan."
        )
    contract = plan.get("form_contract") if isinstance(plan.get("form_contract"), dict) else {}
    if (
        contract.get("version") != BUSINESS_TRIP_SUBMIT_CONTRACT_VERSION
        or contract.get("fingerprint") != business_trip_submit_contract_fingerprint()
    ):
        raise BusinessTripContractMismatch(
            "The business-trip submission contract changed after authorization."
        )
