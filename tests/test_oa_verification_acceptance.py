"""Independent real-adapter checks over the closed IO acceptance transport."""
from pathlib import Path
import time

import pytest

from agentbridge.adapters.seeyon_pending_actions import prepare_efficiency_data_approval
from agentbridge.adapters.seeyon_pending_verification import verify_pending_action
from agentbridge.browser.read_budget import read_deadline_scope
from tests.support.oa_verification_acceptance import (
    OaVerificationAcceptanceFixture, VerificationContract, VerificationWorker,
)


@pytest.fixture
def fixture(tmp_path):
    value = OaVerificationAcceptanceFixture(tmp_path / "verification",
        contract=VerificationContract(io_delay=0, fixed_wait_scale=0))
    try:
        yield value
    finally:
        value.close()


def historical_target(fixture, **shape):
    """Generate real frozen materials, then expose a historical OA done row.

    This is a read-only verification fixture, not a claimed approval journey.
    No decision/auth/outcome row is inserted or rewritten.
    """
    case = fixture.add_scenario("first-page", **shape)
    affair = case["arguments"]["affair_id"]
    adapter = fixture.service.adapter_for_system("oa")
    worker = VerificationWorker(fixture.oa, is_reader=True, target_id=affair)
    with adapter.capability_scope(worker):
        target = prepare_efficiency_data_approval(adapter, worker,
            {"affair_id": affair, "opinion": "仅获取真实冻结核验材料"})["plan"]["target"]
    fixture.oa.data["affairs"][affair]["processed"] = True
    return adapter, worker, target


@pytest.mark.parametrize("index,total,confirmed", [(0, 53, True), (51, 53, True), (999, 1000, True), (1000, 1001, False)])
def test_actual_adapter_reads_exact_done_target_with_bounded_pages(fixture, index, total, confirmed):
    adapter, worker, target = historical_target(fixture, target_index=index, history_total=total)
    start = len(fixture.oa.data["verification_io"])
    with adapter.capability_scope(worker):
        evidence = verify_pending_action(adapter, worker, target)
    assert evidence["confirmed"] is confirmed
    calls = fixture.oa.data["verification_io"][start:]
    pages = [event["page"] for event in calls if event["kind"] == "history_page"]
    expected = list(range(1, min(index // 50 + 1, 20) + 1))
    if confirmed and index >= 50:
        expected.append(1)  # Recheck the original ordering anchor after traversal.
    assert pages == expected
    assert not any(event["kind"] == "history_grid" for event in calls), "Exact detail must not re-read the original first page"
    audit = fixture.audit()
    assert audit["decisions"] == audit["syntheticWrites"] == audit["modelDispatches"] == 0


@pytest.mark.parametrize("fault", ["total_drift", "duplicate", "short", "failed"])
def test_real_pagination_faults_cannot_become_completion(fixture, fault):
    adapter, worker, target = historical_target(fixture, target_index=51, history_total=53, page_fault=fault)
    with adapter.capability_scope(worker):
        try:
            evidence = verify_pending_action(adapter, worker, target)
        except Exception as error:
            assert isinstance(error, (RuntimeError, ValueError, TimeoutError))
        else:
            assert evidence["confirmed"] is False
    assert fixture.audit()["syntheticWrites"] == 0


@pytest.mark.parametrize("fault", ["date", "basis"])
def test_unfiltered_exact_identity_does_not_require_an_inferred_processed_date(fixture, fault):
    adapter, worker, target = historical_target(fixture, target_index=51, history_total=53, page_fault=fault)
    with adapter.capability_scope(worker):
        evidence = verify_pending_action(adapter, worker, target)
    assert evidence["confirmed"] is True


def test_same_affair_with_changed_done_process_is_not_confirmed(fixture):
    adapter, worker, target = historical_target(fixture, target_index=51, history_total=53)
    fixture.oa.data["affairs"][target["affair_id"]]["signals"]["identity"]["process_id"] = "different-completed-process"
    with adapter.capability_scope(worker):
        evidence = verify_pending_action(adapter, worker, target)
    assert evidence["confirmed"] is False and evidence["reason"] == "done_identity_mismatch"


def test_equal_count_ordering_drift_fails_the_original_page_anchor(fixture, monkeypatch):
    adapter, worker, target = historical_target(fixture, target_index=51, history_total=53)
    original = worker.page.evaluate
    first_page_reads = 0

    def changed_anchor(script, argument=None):
        nonlocal first_page_reads
        response = original(script, argument)
        if isinstance(argument, dict) and argument.get("managerMethod") == "getDoneList" and argument["pageNumber"] == 1:
            first_page_reads += 1
            if first_page_reads == 2:
                response["items"][0], response["items"][1] = response["items"][1], response["items"][0]
        return response

    monkeypatch.setattr(worker.page, "evaluate", changed_anchor)
    with adapter.capability_scope(worker):
        evidence = verify_pending_action(adapter, worker, target)
    assert first_page_reads == 2
    assert evidence["confirmed"] is False and evidence["reason"] == "done_scan_unproven"


def test_slow_done_page_receives_remaining_budget_and_stops_without_false_completion(fixture):
    adapter, worker, target = historical_target(fixture, done_delay=.15)
    offset = len(fixture.oa.data["verification_io"])
    start = time.monotonic()
    with adapter.capability_scope(worker):
        try:
            with read_deadline_scope(start + .06):
                result = verify_pending_action(adapter, worker, target)
        except (RuntimeError, TimeoutError):
            pass
        else:
            assert result["confirmed"] is False
    elapsed = time.monotonic() - start
    calls = fixture.oa.data["verification_io"][offset:]
    timed_out = [event for event in calls if event["timedOut"]]
    assert timed_out and timed_out[-1]["kind"] == "history_page"
    assert 0 < timed_out[-1]["requestedTimeoutMs"] <= 60
    assert elapsed < .3
    assert fixture.audit()["syntheticWrites"] == 0
