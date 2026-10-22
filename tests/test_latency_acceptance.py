"""The performance workload must retain real security and outcome semantics."""
import threading

import pytest

from tests.support.latency_acceptance import (
    HttpWorkload, JsonHttpClient, LatencyAcceptanceFixture, LatencyContract, SelectedPreparationWorkload,
    compare_reports, distribution,
)


@pytest.fixture
def workload(tmp_path):
    fixture = LatencyAcceptanceFixture(tmp_path, latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0), seed=False)
    servers = fixture.servers()
    threads = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        threads.append(thread)
    fixture._acceptance_test_workspace_server = servers[1]
    try:
        yield HttpWorkload(fixture), fixture
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(2)
        fixture.close()


@pytest.mark.parametrize("scenario,expected_writes,outcome", [
    ("single", 1, "succeeded"), ("mixed", 2, "succeeded"), ("legacy", 2, "succeeded"),
    ("drift", 0, "material_change_blocked"), ("unknown", 1, "unknown_no_replay"),
])
def test_http_workload_keeps_exact_decisions_and_downstream_boundaries(workload, scenario, expected_writes, outcome):
    runner, fixture = workload
    result = runner.process(scenario)
    assert result["writes"] == expected_writes
    assert result["outcome"] == outcome
    assert len(fixture.oa.writes) == expected_writes


def test_query_performance_sample_does_not_create_authorization(workload):
    runner, fixture = workload
    result = runner.process("query")
    assert result["querySeconds"] >= 0
    assert not fixture.oa.writes
    assert not fixture.meta["draft_cases"]


def test_query_serve_case_projects_the_real_result_without_authorization(workload):
    runner, fixture = workload
    case = fixture.add_query_case()
    status, detail, _ = runner.workspace.json("GET", f"/api/tasks/{case['task_id']}")
    assert status == 200
    assert fixture.service.tasks.get_task(case["task_id"], user_subject="interaction-review")["status"] == "succeeded"
    assert fixture.service.operations.get(case["operation_id"])["status"] == "succeeded"
    assert fixture.audit()["decisionCount"] == 0
    assert not fixture.oa.writes


def test_workspace_selection_requires_csrf_and_persists_only_preparation_intent(workload):
    from unittest.mock import patch
    from uuid import uuid4
    runner, fixture = workload
    case = fixture.add_query_case()
    status, detail, _ = runner.workspace.json("GET", f"/api/tasks/{case['task_id']}")
    assert status == 200
    group = detail["actionableResults"][0]
    body = {"operationId": group["operationId"], "targetKeys": [group["targets"][0]["targetKey"]], "requestKey": uuid4().hex}
    path = f"/api/tasks/{case['task_id']}/prepare-selection"
    assert runner.workspace.json("POST", path, body)[0] == 401
    with patch.object(fixture.service, "invoke", side_effect=AssertionError("Workspace cannot execute business")):
        status, queued, _ = runner.workspace.json("POST", path, body, csrf=True)
    assert status == 202
    assert queued["status"] == "queued"
    assert "command" not in queued["detail"]["task"]["summary"]
    assert queued["detail"]["task"]["summary"]["preparation"] == {
        "kind": "oa_pending_prepare", "state": "pending", "selectedCount": 1}
    assert fixture.audit()["decisionCount"] == 0
    assert not fixture.oa.writes
    repeated_status, repeated, _ = runner.workspace.json("POST", path, body, csrf=True)
    assert repeated_status == 202 and repeated["taskId"] == queued["taskId"]


def test_workspace_selection_rejects_client_business_arguments_with_structured_error(workload):
    from uuid import uuid4
    runner, fixture = workload
    case = fixture.add_query_case()
    _, detail, _ = runner.workspace.json("GET", f"/api/tasks/{case['task_id']}")
    group = detail["actionableResults"][0]
    body = {"operationId": group["operationId"], "targetKeys": [group["targets"][0]["targetKey"]],
        "requestKey": uuid4().hex, "capabilityName": "oa.workflow.revoke"}
    status, response, _ = runner.workspace.json("POST", f"/api/tasks/{case['task_id']}/prepare-selection", body, csrf=True)
    assert status == 400 and response["error"]["code"] == "ACTION_REQUEST_INVALID"
    assert not fixture.oa.writes


def test_selected_performance_samples_create_fresh_commands_without_authorization_or_writes(workload):
    _, fixture = workload
    thread = threading.Thread(target=fixture.coordinate, daemon=True)
    thread.start()
    try:
        runner = SelectedPreparationWorkload(fixture)
        first, second = runner.selected_preparation(), runner.selected_preparation()
        assert first["sourceTaskId"] != second["sourceTaskId"]
        assert first["sourceOperationId"] != second["sourceOperationId"]
        assert first["preparedTaskId"] != second["preparedTaskId"]
        assert first["sourceHash"] != second["sourceHash"]
        assert first["duplicateRequestSameTask"] is second["duplicateRequestSameTask"] is True
        assert first["modelCalls"] == second["modelCalls"] == 0
        assert fixture.audit()["authorizationStates"] == {}
        assert fixture.audit()["decisionCount"] == fixture.audit()["downstreamWriteCount"] == 0
        assert fixture.coordinator_errors == []
    finally:
        fixture.stop_event.set()
        thread.join(2)


def test_long_history_and_http_observer_are_separate_from_the_business_workload(workload):
    runner, fixture = workload
    fixture.seed_history(messages=4, tasks=2)
    assert fixture.meta["latency_history"] == {"messages": 4, "tasks": 2}
    assert len(fixture.meta["query_cases"]) == 2
    assert not fixture.meta["draft_cases"]
    assert fixture.audit()["decisionCount"] == 0
    with pytest.raises(ValueError, match="different data shape"):
        fixture.seed_history(messages=5, tasks=2)
    output = fixture.observe_workspace_http(fixture._acceptance_test_workspace_server)
    status, _, _ = runner.workspace.json("GET", "/api/tasks")
    assert status == 200
    import json
    observed = json.loads(output.read_text())
    assert observed["requestCount"] == 1
    assert observed["requests"][0]["route"] == "/api/tasks"
    assert "query" not in observed["requests"][0]


def test_measurement_contract_rejects_external_origins_and_records_actual_delay():
    for origin in ("http://localhost:1234", "https://127.0.0.1:1234", "https://example.com"):
        with pytest.raises(ValueError):
            JsonHttpClient(origin)
    latency = LatencyContract(io_delay_seconds=.001)
    offset = latency.snapshot()
    latency.io("http")
    measured = latency.since(offset)
    assert measured["counts"] == {"http": 1}
    assert measured["controlledDelaySeconds"]["http"] >= .001
    assert distribution(list(range(1, 31))) == {"count": 30, "median": 15, "p95": 29, "max": 30, "min": 1}


@pytest.mark.parametrize("scale", [0, 1])
def test_fixed_wait_measurement_separates_legacy_settle_from_new_readiness_window(scale):
    from types import SimpleNamespace
    from unittest.mock import patch
    from agentbridge.browser.central import OA_DETAIL_STABILITY_MS
    from tests.support.latency_acceptance import TimedWorker, FullOaWorker
    worker = object.__new__(TimedWorker)
    latency = LatencyContract(io_delay_seconds=0, worker_start_seconds=0, fixed_wait_scale=scale)
    worker.transport = SimpleNamespace(latency=latency)
    with (patch.object(FullOaWorker, "rendered_snapshot", return_value={}),
          patch.object(latency, "pause") as pause):
        worker.rendered_snapshot("detail", settle_ms=1800)
        worker.rendered_snapshot("detail", settle_ms=0, readiness={"kind": "oa_workflow_detail"})
    assert pause.call_args_list[1].args == ("snapshotSettleWait", 1.8 * scale)
    assert pause.call_args_list[3].args == ("snapshotReadinessWait", OA_DETAIL_STABILITY_MS / 1000 * scale)


def test_comparison_rejects_incompatible_delay_and_failed_samples():
    report = {"metadata": {"benchmarkContract": "v1", "latencyContract": {"io": 1},
                           "measurementKind": "automated-loopback-http"},
              "summary": {"single": {"samples": 30, "errors": 0,
                                     "metrics": {"totalSeconds": distribution([1] * 30)}}}}
    import copy
    candidate = copy.deepcopy(report)
    assert compare_reports(report, candidate)["single"]["totalSeconds"]["medianChangePercent"] == 0
    candidate["metadata"]["latencyContract"]["io"] = 0
    with pytest.raises(ValueError, match="latencyContract"):
        compare_reports(report, candidate)
    candidate = copy.deepcopy(report)
    candidate["summary"]["single"]["errors"] = 1
    with pytest.raises(ValueError, match="failed"):
        compare_reports(report, candidate)
