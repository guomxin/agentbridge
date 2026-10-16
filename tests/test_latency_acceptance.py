"""The performance workload must retain real security and outcome semantics."""
import threading

import pytest

from tests.support.latency_acceptance import (
    HttpWorkload, JsonHttpClient, LatencyAcceptanceFixture, LatencyContract, compare_reports, distribution,
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
