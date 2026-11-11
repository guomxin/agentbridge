"""Owned real Central/plan/CLI receipts; only downstream list/detail I/O is synthetic."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
import importlib
import json
from unittest.mock import patch

import pytest

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.oa_history_queries import response_chars
from agentbridge.core.task_plan_validation import PlanValidationError
from agentbridge.core.transforms import OA_HISTORY_REPORT, TransformRejected, project_oa_history_report
from tests import test_oa_history_query_mcp as history_fixture
from tests.test_oa_history_query_selection import history_row, workflow_detail


SELECTION = {"kind": "latest", "count": 1, "workflow_state": "in_progress"}


@pytest.fixture
def selected_plan():
    case = history_fixture.OaHistoryQueryMcpTests()
    case.setUp()
    service = case.service
    case.rows = [history_row(index, state="ended", initiated="2026-06-01T09:00:00+08:00") for index in range(224)]
    for index in range(5):
        case.rows[index]["initiated_at"] = f"2026-11-{10-index:02d}T09:00:00+08:00"
    case.rows[5] = history_row(5)
    for index in range(6, 34):
        case.rows[index] = history_row(index, initiated="2026-07-01T09:00:00+08:00")
    case.pages, case.details = [], []
    original_page = service.oa_history_query_service.read_page

    def read_page(*args, **kwargs):
        case.pages.append(kwargs["page_number"])
        return original_page(*args, **kwargs)

    def read_nodes(*args, row, **kwargs):
        case.details.append(row["affair_id"])
        return workflow_detail()

    service.oa_history_query_service.read_page = read_page
    service.oa_history_query_service.read_nodes = read_nodes
    service.oa_history_query_service.target_ref_factory = service.business_watch.target_reference
    tokens = McpIdentityTokenStore(service.db_path)
    issued = tokens.issue(user_subject="user-a", expected_principal_ref="Alice")
    case.identity = tokens.resolve_client(issued["token_id"])
    service.set_task_plan_authority_resolver(lambda token, scopes: tokens.resolve_client(token, required_scopes=scopes))
    case.task_id = service.ensure_host_task(user_subject="user-a", token_id=case.identity["token_id"],
        agent_host="test-host", host_task_key="selected-history-plan", endpoint_key="history-plan-endpoint",
        client_type="web", external_subject="user-a", conversation_ref="history-plan-chat",
        title="最近一条未结束已发事项")["task"]["taskId"]
    try:
        yield case
    finally:
        case.doCleanups()


def proposal(*, selection=SELECTION):
    arguments = {"collection": "sent", "temporal": {"kind": "all"}}
    if selection is not None:
        arguments["selection"] = deepcopy(selection)
    return {"schemaVersion": "agentbridge.task-plan.proposal.v2", "goal": "查询最近一条未结束已发事项",
        "constraints": {"temporal": {"kind": "all"}}, "steps": [
            {"stepKey": "query", "kind": "capability", "capabilityName": "oa.workflow.history.query", "arguments": arguments},
            {"stepKey": "report", "kind": "transform", "transformName": OA_HISTORY_REPORT,
                "dependsOn": ["query"], "bindings": {"report": {"mode": "single", "step": "query", "pointer": ""}}}]}


def start(case, *, plan=None):
    response = case.service.prepare_task_plan(user_subject="user-a", task_id=case.task_id,
        proposal=plan or proposal(), granted_scopes=case.identity["scopes"], authority_identity=case.identity,
        idempotency_key="selected-plan")
    assert response["status"] == "succeeded", response
    stored = case.service.task_plans.get(response["plan"]["planId"], user_subject="user-a")
    return response, stored


def test_real_plan_preserves_selection_scope_without_expanding_224_rows(selected_plan):
    case = selected_plan
    response, stored = start(case)
    report = stored["result_projection"]["result"]
    assert report["selection"]["selection_complete"] is True
    assert report["selection"]["scope"] == "all_matching_history"
    assert report["selection"]["selected_count"] == 1
    assert report["coverage"]["details_scope"] == "selection_candidates"
    assert report["progress"]["matched_count"] == 224
    assert report["progress"]["details_checked"] == 1
    assert report["total"] == 1
    assert [row["affair_id"] for row in report["items"]] == ["5"]
    assert case.details == ["5"] and case.pages == [1, 2, 3, 4, 5, 1, 5]
    compact = response["plan"]["resultProjection"]["result"]
    assert compact["selection"] == report["selection"]
    assert compact["coverage"]["details_scope"] == "selection_candidates"
    assert compact["total"] == 1 and "items" not in compact
    assert compact["items_available_via"] == "replay_cursor"
    assert response["nextAction"]["arguments"]["cursor"] == report["replay_cursor"]
    assert response_chars(response) <= 12000
    reads = (list(case.pages), list(case.details))
    replay = case.invoke(user_subject="user-a", capability_name="oa.workflow.history.next",
        arguments={"cursor": report["replay_cursor"]})
    assert replay["status"] == "succeeded"
    result = replay["result"]
    assert result["selection"] == report["selection"]
    assert [row["affair_id"] for row in result["items"]] == ["5"]
    assert result["has_more"] is False and result["next_cursor"] is None
    assert result["items"][0]["target_ref"]
    assert (case.pages, case.details) == reads


def test_partial_selection_reason_survives_stored_and_compact_report(selected_plan):
    case = selected_plan
    case.rows[0] = history_row(0, state="unknown", initiated="2026-11-10T09:00:00+08:00")

    def unavailable(*args, row, **kwargs):
        raise ValueError("synthetic unknown state")

    case.service.oa_history_query_service.read_nodes = unavailable
    response, stored = start(case)
    for report in (stored["result_projection"]["result"], response["plan"]["resultProjection"]["result"]):
        assert report["selection"]["selection_complete"] is False
        assert report["selection"]["reason"] == "selection_state_unverified"
        assert report["coverage"]["details_scope"] == "selection_candidates"
        assert report["source_complete"] is False
        assert report["total"] == 0
    assert stored["result_projection"]["result"]["items"] == []


def test_generic_cli_invoke_preserves_selected_receipt_and_whole_rows(selected_plan):
    case = selected_plan
    cli = importlib.import_module("agentbridge.cli.main")
    stdout = StringIO()
    with patch.object(cli, "CentralCapabilityService", return_value=case.service), redirect_stdout(stdout):
        status = cli.main(["--home", str(case.temporary.name), "capability", "invoke", "oa.workflow.history.query",
            "--user-subject", "user-a", "--json", json.dumps({"collection": "sent", "temporal": {"kind": "all"},
                "selection": SELECTION})])
    response = json.loads(stdout.getvalue())
    assert status == 0 and response["status"] == "succeeded"
    assert response["result"]["selection"]["selected_count"] == 1
    assert response["result"]["total"] == 1
    assert len(response["result"]["items"]) == 1
    assert response["result"]["coverage"]["details_scope"] == "selection_candidates"
    assert case.details == ["5"]


def test_plan_replay_paginates_only_the_selected_set_without_new_source_reads(selected_plan):
    case = selected_plan
    case.rows[6]["initiated_at"] = "2026-07-02T09:00:00+08:00"
    case.rows[7]["initiated_at"] = "2026-07-03T09:00:00+08:00"
    plan = proposal(selection={**SELECTION, "count": 3})
    plan["steps"][0]["arguments"]["limit"] = 1
    response, stored = start(case, plan=plan)
    report = stored["result_projection"]["result"]
    assert report["total"] == 3 and report["selection"]["selected_count"] == 3
    assert response["plan"]["resultProjection"]["result"]["total"] == 3
    reads = (list(case.pages), list(case.details))
    cursor, delivered = report["replay_cursor"], []
    while cursor:
        result = case.service.invoke(user_subject="user-a", capability_name="oa.workflow.history.next",
            arguments={"cursor": cursor})
        assert result["status"] == "succeeded"
        delivered.extend(result["result"]["items"])
        cursor = result["result"]["next_cursor"]
    assert [row["affair_id"] for row in delivered] == ["5", "7", "6"]
    assert (case.pages, case.details) == reads


@pytest.mark.parametrize("condition,expected", [
    ("owner", "OA_HISTORY_QUERY_NOT_FOUND"), ("principal", "OA_HISTORY_SESSION_CHANGED"),
    ("expired", "OA_HISTORY_QUERY_EXPIRED")])
def test_plan_reference_replay_rechecks_owner_principal_and_ttl_before_io(selected_plan, condition, expected):
    case = selected_plan
    _, stored = start(case)
    report = stored["result_projection"]["result"]
    reads = (list(case.pages), list(case.details))
    subject = "user-a"
    if condition == "owner":
        subject = "user-b"
        session = case.service.sessions.get_or_create(user_subject=subject, system_id="oa", expected_principal_ref="Bob")
        case.service.sessions.activate(session["session_id"], observed_principal_ref="Bob")
        case.service.session_states.save(session["session_id"], {"cookies": []})
    elif condition == "principal":
        session = case.service.sessions.find(user_subject=subject, system_id="oa")
        case.service.sessions.rebind_expected_principal(session["session_id"], expected_principal_ref="Charlie", reason="Synthetic account switch")
        case.service.sessions.activate(session["session_id"], observed_principal_ref="Charlie")
        case.service.session_states.save(session["session_id"], {"cookies": []})
    else:
        case.service.oa_history_queries.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=1)
    result = case.service.invoke(user_subject=subject, capability_name="oa.workflow.history.next",
        arguments={"cursor": report["replay_cursor"]})
    assert result["status"] == "failed", result
    assert result["error"]["code"] == expected
    assert result["result"] is None
    assert (case.pages, case.details) == reads


def test_model_filled_selection_report_cannot_replace_bound_authoritative_source(selected_plan):
    case = selected_plan
    plan = proposal()
    plan["steps"][-1]["bindings"] = {}
    plan["steps"][-1]["arguments"] = {"report": {"selection": SELECTION}}
    with pytest.raises(PlanValidationError) as rejected:
        start(case, plan=plan)
    assert rejected.value.code == "PLAN_PROVENANCE_REQUIRED"
    assert case.pages == case.details == []


def test_default_plan_keeps_legacy_report_and_reference_without_selection(selected_plan):
    case = selected_plan
    case.rows = [history_row(1, state="ended")]
    response, stored = start(case, plan=proposal(selection=None))
    for report in (stored["result_projection"]["result"], response["plan"]["resultProjection"]["result"]):
        assert "selection" not in report
        assert "details_scope" not in report["coverage"]
        assert report["total"] == 1


def test_selection_projection_is_explicitly_public_and_schema_closed(selected_plan):
    case = selected_plan
    response = case.invoke(user_subject="user-a", capability_name="oa.workflow.history.query",
        arguments={"collection": "sent", "temporal": {"kind": "all"}, "selection": SELECTION})
    report = response["result"]
    untrusted = deepcopy(report)
    untrusted["selection"]["_source"] = {"private": "private-selection-marker"}
    projected = project_oa_history_report(untrusted)
    assert "private-selection-marker" not in json.dumps(projected)
    assert projected["selection"] == report["selection"]
    with pytest.raises(TransformRejected) as rejected:
        case.service.transforms.invoke(OA_HISTORY_REPORT, {"report": untrusted})
    assert rejected.value.code == "TRANSFORM_INPUT_INVALID"
