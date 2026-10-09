from unittest.mock import patch
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial

import pytest

from agentbridge.adapters.seeyon_pending_batch import (
    PENDING_BATCH_PREPARE_CAPABILITY as BATCH,
    PendingBatchSelectionError,
    select_pending_batch_items,
)
from agentbridge.adapters.seeyon_pending_actions import PendingActionOutcomeUnknown
from agentbridge.adapters.seeyon_missed_punch import MissedPunchOutcomeUnknown
from agentbridge.core.central_service import _TRUSTED_WRITE_DEFINITIONS
import tests.test_central_service as helpers


def make_service(path):
    return helpers.CentralCapabilityServiceTests._service(path, helpers.FakeWorker())


def row(ordinal, title="研发中心效能数据"):
    return {"affair_id": f"affair-{ordinal}", "title": f"{title}-{ordinal}", "sender": "测试用户", "date": "2026-09-07", "category": "协同"}


def pending(rows):
    return {"items": rows, "count": len(rows), "coverage": {"status": "complete"}}


@pytest.fixture
def run(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    helpers.CentralCapabilityServiceTests._activate(service)
    tid = helpers.CentralCapabilityServiceTests._ensure_host_task(service)
    rows = [row(1), row(2), row(3, "【HR】补签申请单"), row(4, "周报发送流程"), row(5)]
    prepared, committed = [], []

    def prepare(_adapter, _worker, arguments):
        prepared.append(arguments["affair_id"])
        return {"plan": {"target": {"affair_id": arguments["affair_id"]}, "exact_input": {"opinion": arguments["opinion"]}},
                "summary": {"title": "处理测试待办", "fields": []}}

    def commit(_adapter, _worker, plan, *, enter_commit_boundary):
        enter_commit_boundary()
        committed.append(plan["target"]["affair_id"])
        return {"workflow_approved": True, "verification": {"confirmed": True}}

    for definition in _TRUSTED_WRITE_DEFINITIONS.values():
        if "approval" in definition.get("prepare_function", "") or definition.get("prepare_function") == "prepare_weekly_report_acknowledgement":
            monkeypatch.setattr("agentbridge.core.write_catalog." + definition["prepare_function"], prepare)
            monkeypatch.setattr("agentbridge.core.write_catalog." + definition["commit_function"], commit)
    monkeypatch.setattr("agentbridge.core.write_catalog.preflight_pending_action", lambda *_a, **_kw: None)
    monkeypatch.setattr(service.adapter, "list_workflows", lambda *_a, **_kw: pending(rows))
    return service, tid, rows, prepared, committed


def start(service, tid, arguments=None):
    return service.invoke(user_subject="user-a", capability_name=BATCH, arguments=arguments or {}, task_id=tid)


def authorize(service, field):
    return helpers.CentralCapabilityServiceTests._submit_and_resume_opinion(service, field)


def finish(service, auth):
    return helpers.CentralCapabilityServiceTests._approve_and_resume_authorization(service, auth)


def test_five_mixed_items_advance_once_and_survive_restart(run, tmp_path, monkeypatch):
    service, tid, rows, prepared, committed = run
    field = start(service, tid)
    assert field["batch"]["totalCount"] == 5
    rows.append(row(6))  # New pending items must not enter the frozen batch.
    for ordinal in range(1, 6):
        assert field["batch"]["currentOrdinal"] == ordinal
        form = service.field_submissions.get(field["nextAction"]["inputSubmissionId"])["form_schema"]
        assert f"第 {ordinal}/5 条" in form["title"]
        auth = authorize(service, field)
        authorization = service.write_authorizations.get(auth["nextAction"]["authorizationId"])
        assert f"第 {ordinal}/5 条" in authorization["summary"]["title"]
        if ordinal == 2:
            service = make_service(tmp_path)
            monkeypatch.setattr(service.adapter, "list_workflows", lambda *_a, **_kw: pytest.fail("must not rediscover batch"))
        field = finish(service, auth)
        replay = service.resume_interaction(user_subject="user-a", interaction_id=auth["interaction"]["interactionId"])
        assert len(committed) == ordinal
        if ordinal < 5:
            assert replay["interaction"]["interactionId"] == field["interaction"]["interactionId"]
            assert service.tasks.get_task(tid, user_subject="user-a")["status"] == "waiting_user"
    assert field["batch"]["state"] == "succeeded"
    assert field["batch"]["succeededCount"] == 5
    assert prepared == committed == [f"affair-{i}" for i in range(1, 6)]


def test_mixed_batch_uses_each_frozen_definition_and_separate_authorization(run, monkeypatch):
    service, tid, rows, _prepared, _committed = run
    rows[:] = [row(1), row(2, "周报发送流程"), row(3, "【HR】补签申请单")]
    cases = (
        ("efficiency_data", "prepare_efficiency_data_approval", "approve_efficiency_data", "oa.efficiency_data.approve"),
        ("weekly_report", "prepare_weekly_report_acknowledgement", "acknowledge_weekly_report", "oa.weekly_report.acknowledge"),
        ("missed_punch", "prepare_missed_punch_approval", "approve_missed_punch_request", "oa.missed_punch.approve"),
    )
    preflights, prepared, committed, authorization_ids = [], [], [], []

    def preflight(_adapter, _worker, arguments, profile):
        preflights.append((profile, arguments["affair_id"]))
        return {"review": {"title": profile, "fields": []}, "review_fingerprint": "sha256:" + profile}

    def prepare(profile, _adapter, _worker, arguments):
        prepared.append((profile, arguments["affair_id"]))
        return {
            "plan": {"target": {"profile": profile, "affair_id": arguments["affair_id"],
                                "review_fingerprint": "sha256:" + profile},
                     "exact_input": {"opinion": arguments["opinion"]}},
            "summary": {"title": profile, "fields": []},
        }

    def commit(profile, _adapter, _worker, plan, *, enter_commit_boundary):
        assert plan["target"]["profile"] == profile
        enter_commit_boundary()
        committed.append((profile, plan["target"]["affair_id"]))
        return {"workflow_approved": True, "verification": {"confirmed": True}}

    monkeypatch.setattr("agentbridge.core.write_catalog.preflight_pending_action", preflight)
    for profile, prepare_name, commit_name, _capability in cases:
        monkeypatch.setattr("agentbridge.core.write_catalog." + prepare_name, partial(prepare, profile))
        monkeypatch.setattr("agentbridge.core.write_catalog." + commit_name, partial(commit, profile))

    field = start(service, tid)
    for ordinal, (profile, _prepare_name, _commit_name, capability) in enumerate(cases, 1):
        schema = service.field_submissions.get(field["nextAction"]["inputSubmissionId"])["form_schema"]
        assert schema["_agentbridge_resume_arguments"] == {
            "batch_id": field["batch"]["batchId"], "affair_id": f"affair-{ordinal}",
        }
        if profile == "missed_punch":
            assert "review" not in schema
        else:
            assert schema["review"]["title"] == profile
        assert preflights == [(name, f"affair-{i}") for i, (name, *_rest) in enumerate(cases[:min(ordinal, 2)], 1)]
        auth = authorize(service, field)
        authorization = service.write_authorizations.get(auth["nextAction"]["authorizationId"], include_plan=True)
        authorization_ids.append(authorization["authorization_id"])
        assert authorization["state"] == "pending"
        assert authorization["capability_name"] == capability
        assert authorization["plan"]["prepare_capability"] == BATCH
        assert authorization["plan"]["target"]["profile"] == profile
        assert len(committed) == ordinal - 1
        field = finish(service, auth)
        assert committed[-1] == (profile, f"affair-{ordinal}")
    assert len(set(authorization_ids)) == 3
    assert prepared == committed == [(profile, f"affair-{i}") for i, (profile, *_rest) in enumerate(cases, 1)]
    assert preflights == [("efficiency_data", "affair-1"), ("weekly_report", "affair-2")]
    assert field["batch"]["state"] == "succeeded"


def test_success_before_progress_crash_recovers_without_resubmission(run, tmp_path, monkeypatch):
    service, tid, _rows, _prepared, committed = run
    auth = authorize(service, start(service, tid))
    with patch.object(service, "_apply_batch_operation_response", side_effect=lambda **kw: kw["response"]):
        assert finish(service, auth)["status"] == "succeeded"
    service = make_service(tmp_path)
    monkeypatch.setattr(service.adapter, "list_workflows", lambda *_a, **_kw: pytest.fail("must use frozen batch"))
    recovered = service.resume_interaction(user_subject="user-a", interaction_id=auth["interaction"]["interactionId"])
    assert recovered["batch"]["currentOrdinal"] == 2
    assert recovered["error"]["code"] == "FIELD_INPUT_REQUIRED"
    assert committed == ["affair-1"]


def test_old_callback_returns_current_authorization_not_consumed_input(run):
    service, tid, _rows, _prepared, committed = run
    first_auth = authorize(service, start(service, tid))
    second_field = finish(service, first_auth)
    second_auth = authorize(service, second_field)
    replay = service.resume_interaction(user_subject="user-a", interaction_id=first_auth["interaction"]["interactionId"])
    assert replay["interaction"]["interactionId"] == second_auth["interaction"]["interactionId"]
    assert replay["batch"]["currentOrdinal"] == 2
    assert committed == ["affair-1"]


def test_unknown_second_item_stops_remaining_and_never_retries(run, monkeypatch):
    service, tid, _rows, _prepared, committed = run
    second = finish(service, authorize(service, start(service, tid)))

    def unknown(_a, _w, _p, *, enter_commit_boundary):
        enter_commit_boundary()
        committed.append("unknown-second")
        raise PendingActionOutcomeUnknown("test response lost")

    monkeypatch.setattr("agentbridge.core.write_catalog.approve_efficiency_data", unknown)
    auth = authorize(service, second)
    result = finish(service, auth)
    assert result["status"] == "unknown"
    assert result["batch"]["state"] == "outcome_unknown"
    assert result["batch"]["succeededCount"] == 1
    assert result["batch"]["items"][2]["state"] != "succeeded"
    service.resume_interaction(user_subject="user-a", interaction_id=auth["interaction"]["interactionId"])
    assert committed == ["affair-1", "unknown-second"]


@pytest.mark.parametrize("batch_capability", [BATCH, "oa.missed_punch.approval.batch.prepare"])
def test_shared_missed_punch_commit_unknown_stops_both_batch_entries(run, monkeypatch, batch_capability):
    service, tid, rows, prepared, committed = run
    rows[:] = [row(i, "【HR】补签申请单") for i in range(1, 4)]
    first = service.invoke(user_subject="user-a", capability_name=batch_capability, arguments={}, task_id=tid)
    second = finish(service, authorize(service, first))

    def unknown(_adapter, _worker, _plan, *, enter_commit_boundary):
        enter_commit_boundary()
        committed.append("unknown-second")
        raise MissedPunchOutcomeUnknown("simulated lost shared-commit response")

    monkeypatch.setattr("agentbridge.core.write_catalog.approve_missed_punch_request", unknown)
    auth = authorize(service, second)
    result = finish(service, auth)
    assert result["status"] == "unknown"
    assert result["error"]["code"] == "RESULT_UNKNOWN"
    assert result["batch"]["state"] == "outcome_unknown"
    assert result["batch"]["succeededCount"] == 1
    assert result["batch"]["items"][2]["state"] != "succeeded"
    aid = auth["nextAction"]["authorizationId"]
    assert service.write_authorizations.get(aid)["state"] == "consumed"
    assert service.operations.get(result["operationId"])["status"] == "unknown"
    for key in (None, "another-callback-key"):
        service.resume_interaction(
            user_subject="user-a", interaction_id=auth["interaction"]["interactionId"], idempotency_key=key,
        )
    assert prepared == ["affair-1", "affair-2"]
    assert committed == ["affair-1", "unknown-second"]
    assert service.tasks.get_batch_for_task(parent_task_id=tid, user_subject="user-a")["state"] == "outcome_unknown"


def test_frozen_selection_rejects_target_and_scope_changes(run):
    service, tid, _rows, _prepared, _committed = run
    field = start(service, tid, {"affair_ids": ["affair-2", "affair-1"]})
    assert field["batch"]["totalCount"] == 2
    assert start(service, tid, {"affair_ids": ["affair-3"]})["error"]["code"] == "BATCH_CONTEXT_MISMATCH"
    assert start(service, tid, {"batch_id": field["batch"]["batchId"], "affair_id": "affair-1"})["error"]["code"] == "BATCH_TARGET_MISMATCH"


def test_migrated_user_batch_checks_each_selected_business_approval(run):
    service, tid, _rows, _prepared, _committed = run
    service.user_grants.save(
        "user-a", ["oa.workflow.read", "oa.efficiency_data.approve"],
        expected_revision=service.user_grants.get("user-a")["revision"], actor="admin", reason="efficiency approval only",
    )
    denied = start(service, tid, {"affair_ids": ["affair-3"]})
    assert denied["status"] == "failed"
    assert "permission" in denied["error"]["message"]
    field = start(service, tid, {"affair_ids": ["affair-1"]})
    assert field["batch"]["totalCount"] == 1


def test_selection_limits_filters_and_fail_closed(tmp_path):
    registry = make_service(tmp_path).registry
    rows = [row(i) for i in range(1, 24)]
    assert len(select_pending_batch_items(pending(rows), {"max_items": 30}, registry)) == 23
    selected = select_pending_batch_items(pending(rows), {"affair_ids": ["affair-3", "affair-1"]}, registry)
    assert [i["resource_ref"] for i in selected] == ["affair-3", "affair-1"]
    assert not select_pending_batch_items(pending(rows), {"workflow_types": ["weekly_report"]}, registry)
    leave = select_pending_batch_items(
        pending([row(24, "【HR】请假申请单-韩志平-年休")]),
        {"workflow_types": ["leave"]},
        registry,
    )
    assert leave[0]["display_summary"]["profile"] == "leave"
    business_trip = select_pending_batch_items(
        pending([
            row(
                25,
                "【HR】出差申请单-郑其荣-2026-09-22 05:35-2026-09-23 17:35",
            )
        ]),
        {"workflow_types": ["business_trip"]},
        registry,
    )
    assert business_trip[0]["display_summary"]["profile"] == "business_trip"
    flight = select_pending_batch_items(
        pending([row(26, "【综合】乘坐飞机申请单")]),
        {"workflow_types": ["flight_application"]},
        registry,
    )
    assert flight[0]["display_summary"]["profile"] == "flight_application"
    assert len(select_pending_batch_items(pending(rows[:5]), {"keyword": "研发中心", "workflow_types": ["efficiency_data"]}, registry)) == 5
    cases = [
        (pending(rows), {}, "BATCH_LIMIT_EXCEEDED"),
        ({"items": rows, "coverage": {"status": "partial"}}, {}, "BATCH_SOURCE_INCOMPLETE"),
        (pending([row(1, "【HR】未知专用表单")]), {}, "BATCH_UNSUPPORTED_ITEMS"),
        (pending(rows), {"affair_ids": ["missing"]}, "BATCH_TARGET_UNAVAILABLE"),
        (pending([row(1), row(1)]), {}, "BATCH_SOURCE_INVALID"),
        (pending(rows), {"affair_ids": ["affair-1"], "workflow_types": ["weekly_report"]}, "BATCH_SELECTION_MISMATCH"),
    ]
    for source, arguments, code in cases:
        with pytest.raises(PendingBatchSelectionError) as error:
            select_pending_batch_items(source, arguments, registry)
        assert error.value.code == code


def test_handover_batch_uses_real_pending_limit_and_trusted_authorization(run, monkeypatch):
    from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter

    service, tid, rows, prepared, committed = run
    rows[:] = [row(1, "(自动发起)【HR】工作交接单-测试移交人-离职")]
    monkeypatch.setattr(service.adapter, "list_workflows", SeeyonCentralAdapter.list_workflows.__get__(service.adapter))
    monkeypatch.setattr(service.adapter, "_fetch_workflow_collection",
                        lambda *_a, **_kw: {"items": rows, "total": 1, "page": 1})
    field = start(service, tid, {"workflow_types": ["work_handover"]})
    assert field["error"]["code"] == "FIELD_INPUT_REQUIRED"
    assert not committed
    schema = service.field_submissions.get(field["nextAction"]["inputSubmissionId"])["form_schema"]
    assert "工作交接" in schema["title"]
    auth = authorize(service, field)
    assert not committed
    result = finish(service, auth)
    assert result["batch"]["state"] == "succeeded"
    assert committed == ["affair-1"]
    service.resume_interaction(user_subject="user-a", interaction_id=auth["interaction"]["interactionId"])
    assert committed == ["affair-1"]


def test_real_pending_source_over_100_is_not_silently_batched(run, monkeypatch):
    from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter

    service, tid, rows, prepared, committed = run
    rows[:] = [row(i) for i in range(101)]
    monkeypatch.setattr(service.adapter, "list_workflows", SeeyonCentralAdapter.list_workflows.__get__(service.adapter))
    monkeypatch.setattr(service.adapter, "_fetch_workflow_collection",
                        lambda *_a, **_kw: {"items": rows, "total": 101, "page": 1})
    result = start(service, tid, {"max_items": 100})
    assert result["error"]["code"] == "BATCH_SOURCE_INCOMPLETE"
    assert not prepared and not committed


def test_overlapping_legacy_and_generic_batches_are_rejected(run):
    service, tid, rows, _prepared, _committed = run
    start(service, tid, {"affair_ids": ["affair-3"]})
    other = service.ensure_host_task(user_subject="user-a", token_id="token-a", agent_host="test-host",
        host_task_key="other", endpoint_key="batch-test-endpoint", client_type="web", external_subject="user-a",
        conversation_ref="batch-test-conversation", title="legacy batch")["task"]["taskId"]
    result = service.invoke(user_subject="user-a", capability_name="oa.missed_punch.approval.batch.prepare", arguments={}, task_id=other)
    assert result["error"]["code"] == "BATCH_ALREADY_ACTIVE"


def test_second_user_cannot_observe_or_resume_batch(run):
    service, tid, _rows, _prepared, committed = run
    first = start(service, tid)
    with pytest.raises(KeyError):
        service.get_interaction(user_subject="user-b", interaction_id=first["interaction"]["interactionId"])
    with pytest.raises(KeyError):
        service.tasks.get_batch_for_task(parent_task_id=tid, user_subject="user-b")
    assert not committed


@pytest.mark.parametrize("stop", ["reject", "expire"])
def test_second_card_decline_or_expiry_stops_batch(run, monkeypatch, stop):
    service, tid, _rows, _prepared, committed = run
    second = finish(service, authorize(service, start(service, tid)))
    auth = authorize(service, second)
    aid = auth["nextAction"]["authorizationId"]
    if stop == "reject":
        csrf = service.write_authorizations.issue_csrf(aid)
        service.write_authorizations.decide(aid, decision="reject", csrf_token=csrf, csrf_cookie=csrf)
    else:
        monkeypatch.setattr(service.write_authorizations, "clock", lambda: datetime.now(timezone.utc) + timedelta(days=2))
    service.get_interaction(user_subject="user-a", interaction_id=auth["interaction"]["interactionId"])
    batch = service.tasks.get_batch_for_task(parent_task_id=tid, user_subject="user-a")
    assert batch["state"] not in {"running", "waiting_user", "succeeded"}
    service.resume_interaction(user_subject="user-a", interaction_id=auth["interaction"]["interactionId"])
    assert committed == ["affair-1"]


@pytest.mark.parametrize("guard", ["version", "policy"])
def test_atomic_profile_guards_are_rechecked_before_commit(run, monkeypatch, guard):
    service, tid, _rows, _prepared, committed = run
    auth = authorize(service, start(service, tid))
    spec = service.registry.get("oa.efficiency_data.approval.prepare")
    if guard == "version":
        monkeypatch.setitem(service.registry._specs, spec.name, replace(spec, version="9.0.0"))
        code = "BATCH_VERSION_CHANGED"
    else:
        service.governance_policies.pause(scope_type="global", scope_value="*", reason="test pause", actor="test")
        code = "WRITE_PAUSED"
    result = finish(service, auth)
    assert result["error"]["code"] == code
    assert not committed
    assert service.write_authorizations.get(auth["nextAction"]["authorizationId"])["state"] != "consumed"


@pytest.mark.parametrize("at_authorization", [False, True])
def test_cancel_batch_is_terminal_after_poll_and_late_callback(run, at_authorization):
    service, tid, _rows, _prepared, committed = run
    field = start(service, tid)
    waiting = authorize(service, field) if at_authorization else field
    response = service.cancel_unsubmitted_task(user_subject="user-a", task_id=tid)
    assert response["task"]["status"] == "canceled"
    for _ in range(2):
        service.get_interaction(user_subject="user-a", interaction_id=waiting["interaction"]["interactionId"])
        service.tasks.fail_current_batch_item(parent_task_id=tid, user_subject="user-a",
            operation_id=None, expected_ordinal=1, item_state="superseded", error_code="INTERACTION_SUPERSEDED")
    assert service.tasks.get_task(tid, user_subject="user-a")["status"] == "canceled"
    batch = service.tasks.get_batch_for_task(parent_task_id=tid, user_subject="user-a")
    assert batch["state"] == "canceled"
    assert all(item["state"] == "canceled" for item in batch["items"])
    assert committed == []


def test_submitted_batch_card_cannot_be_canceled(run):
    service, tid, _rows, _prepared, committed = run
    field = start(service, tid)
    sid = field["nextAction"]["inputSubmissionId"]
    csrf = service.field_submissions.issue_csrf(sid)
    service.field_submissions.submit(sid, csrf_token=csrf, csrf_cookie=csrf, values={"opinion": "同意"})
    response = service.cancel_unsubmitted_task(user_subject="user-a", task_id=tid)
    assert response["error"]["code"] == "TASK_ACTION_ALREADY_STARTED"
    assert service.tasks.get_batch_for_task(parent_task_id=tid, user_subject="user-a")["state"] == "waiting_user"
    assert committed == []
