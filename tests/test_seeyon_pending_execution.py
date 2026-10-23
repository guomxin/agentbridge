from tests.support.pending_execution_clock import single_readback_attempt
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agentbridge.adapters.seeyon_pending_actions import (
    PendingActionContractMismatch,
    PendingActionOutcomeUnknown,
    approve_efficiency_data,
    prepare_efficiency_data_approval,
)
from agentbridge.adapters.seeyon_pending_execution import PendingSubmissionTracker, PendingActionNotReady
from tests.test_seeyon_pending_actions import FakeAdapter, FakeWorker, _fixture, _inputs


@pytest.fixture
def attempt():
    worker = FakeWorker(_fixture("efficiency_data"))
    adapter = FakeAdapter(worker)
    plan = prepare_efficiency_data_approval(adapter, worker, _inputs())["plan"]
    return adapter, worker, plan


def test_delayed_submit_and_transient_read_errors_retry_reads_without_resubmitting(attempt, monkeypatch):
    adapter, worker, plan = attempt
    original_read = adapter.list_workflows
    read_count = 0

    def read_after_delay(reader, *, collection, arguments):
        nonlocal read_count
        assert reader is worker.readback_worker
        assert worker.page.commit_payload is not None
        if collection == "pending":
            read_count += 1
            if read_count == 1:
                raise RuntimeError("temporary transport error")
            if read_count == 2:
                return {"items": [worker.fixture["source"]], "coverage": {"status": "complete"}}
        return original_read(reader, collection=collection, arguments=arguments)

    monkeypatch.setattr(adapter, "list_workflows", read_after_delay)
    worker.page.evaluate = Mock(wraps=worker.page.evaluate)
    boundary = Mock()
    result = approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=boundary)

    assert result["verification"]["confirmed"] is True
    assert read_count == 3
    assert worker.page.pumped == [800, 800, 800]
    boundary.assert_called_once()
    submissions = [call for call in worker.page.evaluate.call_args_list
                   if len(call.args) == 2 and isinstance(call.args[1], dict)
                   and "opinion" in call.args[1]]
    assert len(submissions) == 1
    assert worker.readback_closed
    assert worker.page.handlers == {}


@pytest.mark.parametrize("kind", ["same_worker", "same_page", "missing_page", "creation_failed"])
def test_invalid_reader_is_rejected_without_consuming_authorization(attempt, monkeypatch, kind):
    adapter, worker, plan = attempt

    @contextmanager
    def invalid_reader():
        if kind == "creation_failed":
            raise RuntimeError("cannot create browser page")
        if kind == "missing_page":
            yield SimpleNamespace()
            return
        yield worker if kind == "same_worker" else SimpleNamespace(page=worker.page)

    monkeypatch.setattr(worker, "fork_page", invalid_reader)
    boundary = Mock()
    with pytest.raises(PendingActionContractMismatch, match="尚未提交"):
        approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_not_called()
    assert worker.page.commit_payload is None


def test_boundary_failure_closes_reader_and_detaches_observers(attempt):
    adapter, worker, plan = attempt
    with pytest.raises(ValueError, match="authorization expired"):
        approve_efficiency_data(
            adapter, worker, plan,
            enter_commit_boundary=Mock(side_effect=ValueError("authorization expired")),
        )
    assert worker.page.commit_payload is None
    assert worker.readback_closed
    assert worker.page.handlers == {}


def test_final_material_check_keeps_action_scope_and_reader_scope_starts_after_consumption(attempt, monkeypatch):
    adapter, worker, plan = attempt
    active = [worker]
    observations = []
    original_check = adapter.revalidate_workflow_detail
    original_read = adapter.list_workflows

    @contextmanager
    def capability_scope(current):
        previous, active[0] = active[0], current
        try:
            yield
        finally:
            active[0] = previous

    def check(*args, **kwargs):
        assert active[0] is worker
        assert worker.readback_opened and worker.page.commit_payload is None
        observations.append("materials")
        return original_check(*args, **kwargs)

    def consume():
        assert active[0] is worker
        assert observations == ["materials"]
        observations.append("consumed")

    def read(reader, **kwargs):
        assert active[0] is reader is worker.readback_worker
        assert observations == ["materials", "consumed"]
        return original_read(reader, **kwargs)

    monkeypatch.setattr(adapter, "capability_scope", capability_scope, raising=False)
    monkeypatch.setattr(adapter, "revalidate_workflow_detail", check)
    monkeypatch.setattr(adapter, "list_workflows", read)
    result = approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=consume)
    assert result["verification"]["confirmed"] is True
    assert active[0] is worker and worker.readback_closed


def test_missing_scheduling_acknowledgement_is_unknown_and_not_replayed(attempt, monkeypatch):
    adapter, worker, plan = attempt
    original_evaluate = worker.page.evaluate

    def evaluate(script, argument=None):
        if isinstance(argument, dict) and "opinion" in argument:
            worker.page.commit_payload = argument
            return {"scheduled": False}
        return original_evaluate(script, argument)

    monkeypatch.setattr(worker.page, "evaluate", Mock(side_effect=evaluate))
    boundary = Mock()
    with pytest.raises(PendingActionOutcomeUnknown, match="RuntimeError"):
        approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_called_once()
    assert adapter.pending_reads == 0
    assert worker.readback_closed
    assert worker.page.handlers == {}


def test_cleanup_or_diagnostic_failure_cannot_erase_verified_outcome(attempt, monkeypatch):
    adapter, worker, plan = attempt

    @contextmanager
    def close_fails():
        try:
            yield worker.readback_worker
        finally:
            raise RuntimeError("browser page already closed")

    monkeypatch.setattr(worker, "fork_page", close_fails)
    monkeypatch.setattr(PendingSubmissionTracker, "poll", Mock(side_effect=RuntimeError("observer failed")))
    monkeypatch.setattr(PendingSubmissionTracker, "snapshot", Mock(side_effect=RuntimeError("diagnostics failed")))
    result = approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=Mock())
    assert result["verification"]["confirmed"] is True
    assert result["submission_evidence"]["observation_errors"] == ["submission_evidence_unavailable"]
    assert worker.page.handlers == {}


def test_pending_absence_without_matching_done_identity_remains_unknown(attempt, monkeypatch):
    adapter, worker, plan = attempt
    original_read = adapter.list_workflows

    def wrong_done(reader, *, collection, arguments):
        result = original_read(reader, collection=collection, arguments=arguments)
        if collection == "done":
            result["items"][0]["process_id"] = "another-workflow"
        return result

    monkeypatch.setattr(adapter, "list_workflows", wrong_done)
    with single_readback_attempt():
        with pytest.raises(PendingActionOutcomeUnknown, match="done_identity_mismatch"):
            approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=Mock())
    assert worker.readback_closed
    assert worker.page.handlers == {}


def readiness_case(attempt, monkeypatch, *, ready_after=1, drift=None):
    adapter, worker, plan = attempt
    clock = {"now": 0.0, "reads": 0, "ready": False}
    original = worker.page.evaluate

    def evaluate(script, argument=None):
        if "agentbridge_pending_readiness" in script:
            clock["reads"] += 1
            clock["ready"] = ready_after is not None and clock["reads"] >= ready_after
            return {"identity_matches": not (drift == "identity" and clock["reads"] > 1),
                    "ready": clock["ready"], "body_loaded": clock["ready"],
                    "content_api_loaded": clock["ready"], "submit_entry": "submitClickFunc",
                    "reason": "ready" if clock["ready"] else "body_loading"}
        if isinstance(argument, dict) and "opinion" in argument:
            assert clock["ready"], "OA submit must not be called before its business-body callback"
        return original(script, argument)

    def pump(milliseconds):
        worker.page.pumped.append(milliseconds)
        clock["now"] += milliseconds / 1000
        if drift == "material":
            worker.fixture["detail"]["fields"][0]["value"] = "changed during body load"

    monkeypatch.setattr(worker.page, "evaluate", Mock(side_effect=evaluate))
    monkeypatch.setattr(worker.page, "wait_for_timeout", pump)
    monkeypatch.setattr("agentbridge.adapters.seeyon_pending_execution.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("agentbridge.adapters.seeyon_pending_execution._READY_TIMEOUT_SECONDS", .25)
    return adapter, worker, plan, clock


def test_late_body_callback_waits_before_single_authorization_and_submission(attempt, monkeypatch):
    adapter, worker, plan, clock = readiness_case(attempt, monkeypatch, ready_after=3)
    revalidate = Mock(wraps=adapter.revalidate_workflow_detail)
    monkeypatch.setattr(adapter, "revalidate_workflow_detail", revalidate)
    boundary = Mock(side_effect=lambda: clock["ready"] or pytest.fail("consumed before ready"))
    result = approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_called_once()
    revalidate.assert_called_once()
    assert worker.page.pumped[:2] == [100, 100]
    assert result["execution"]["readiness"]["wait_ms"] == 200
    assert result["execution"]["dispatch"]["state"] == "invoked"
    assert result["execution"]["readback"]["attempts"] == 1
    assert result["verification"]["confirmed"] is True
    assert len([call for call in worker.page.evaluate.call_args_list
                if len(call.args) == 2 and isinstance(call.args[1], dict) and "opinion" in call.args[1]]) == 1


def test_body_never_ready_is_bounded_without_consuming_or_readback(attempt, monkeypatch):
    adapter, worker, plan, clock = readiness_case(attempt, monkeypatch, ready_after=None)
    boundary = Mock()
    with pytest.raises(PendingActionNotReady, match="尚未提交") as captured:
        approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=boundary)
    assert captured.value.error_code == "OA_PENDING_ACTION_NOT_READY"
    assert .25 <= clock["now"] < .5
    boundary.assert_not_called()
    assert worker.page.commit_payload is None
    assert adapter.pending_reads == 0
    assert not worker.readback_opened


@pytest.mark.parametrize("drift", ["identity", "material"])
def test_identity_or_material_drift_during_body_wait_blocks_before_consumption(attempt, monkeypatch, drift):
    adapter, worker, plan, _clock = readiness_case(attempt, monkeypatch, ready_after=2, drift=drift)
    boundary = Mock()
    with pytest.raises(PendingActionContractMismatch):
        approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_not_called()
    assert worker.page.commit_payload is None
    assert adapter.pending_reads == 0


def test_final_guard_refusal_does_not_burn_a_minute_or_retry_submission(attempt, monkeypatch):
    adapter, worker, plan = attempt
    original = worker.page.evaluate

    def evaluate(script, argument=None):
        if "agentbridge_pending_dispatch_observation" in script:
            return {"state": "not_invoked"}
        return original(script, argument)

    monkeypatch.setattr(worker.page, "evaluate", Mock(side_effect=evaluate))
    def still_pending(_reader, **_kwargs):
        adapter.pending_reads += 1
        return {"items": [worker.fixture["source"]], "coverage": {"status": "complete"}}
    monkeypatch.setattr(adapter, "list_workflows", still_pending)
    boundary = Mock()
    with pytest.raises(PendingActionOutcomeUnknown, match='"state": "not_invoked"'):
        approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_called_once()
    assert adapter.pending_reads == 1
    assert worker.page.pumped == [800]


def test_body_reloads_during_reader_creation_without_consumption_and_cleans_up(attempt, monkeypatch):
    adapter, worker, plan = attempt
    original = worker.page.evaluate
    reader_created = False

    @contextmanager
    def slow_reader():
        nonlocal reader_created
        reader_created = True
        try:
            yield worker.readback_worker
        finally:
            worker.readback_closed = True

    def evaluate(script, argument=None):
        if "agentbridge_pending_readiness" in script and reader_created:
            return {"identity_matches": True, "ready": False, "reason": "body_loading"}
        return original(script, argument)

    monkeypatch.setattr(worker, "fork_page", slow_reader)
    monkeypatch.setattr(worker.page, "evaluate", evaluate)
    boundary = Mock()
    with pytest.raises(PendingActionNotReady, match="尚未提交"):
        approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_not_called()
    assert worker.readback_closed
    assert worker.page.handlers == {}
    assert worker.page.commit_payload is None


def test_final_guard_refusal_stops_after_one_failed_readback_without_retry(attempt, monkeypatch):
    adapter, worker, plan = attempt
    original = worker.page.evaluate

    def evaluate(script, argument=None):
        if "agentbridge_pending_dispatch_observation" in script:
            return {"state": "not_invoked"}
        return original(script, argument)

    monkeypatch.setattr(worker.page, "evaluate", evaluate)
    read = Mock(side_effect=RuntimeError("read unavailable"))
    monkeypatch.setattr(adapter, "list_workflows", read)
    with pytest.raises(PendingActionOutcomeUnknown, match='"last_read_error": "RuntimeError"'):
        approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=Mock())
    read.assert_called_once()
    assert worker.page.pumped == [800]


@pytest.mark.parametrize("slow_stage", ["initial_detail", "final_materials", "nested_recovery"])
def test_preflight_budget_covers_initial_detail_and_final_guards_without_consumption(attempt, monkeypatch, slow_stage):
    adapter, worker, plan = attempt
    clock = [0.0]
    monkeypatch.setattr("agentbridge.adapters.seeyon_pending_execution.time.monotonic", lambda: clock[0])
    boundary = Mock()
    def slow(function):
        def call(*args, **kwargs):
            result = function(*args, **kwargs)
            clock[0] = 61.0
            return result
        return call
    if slow_stage == "nested_recovery":
        boundary.before_pending_revalidation = lambda: clock.__setitem__(0, 61.0)
    else:
        name = "resolve_workflow_detail" if slow_stage == "initial_detail" else "revalidate_workflow_detail"
        monkeypatch.setattr(adapter, name, slow(getattr(adapter, name)))
    with pytest.raises(PendingActionNotReady, match="授权未消耗"):
        approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_not_called()
    assert worker.page.commit_payload is None


def test_slow_successful_readback_after_deadline_remains_unknown_without_replay(attempt, monkeypatch):
    adapter, worker, plan = attempt
    clock = [0.0]
    monkeypatch.setattr("agentbridge.adapters.seeyon_pending_execution.time.monotonic", lambda: clock[0])
    original = adapter.list_workflows
    def late_done(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["collection"] == "done":
            clock[0] = 61.0
        return result
    monkeypatch.setattr(adapter, "list_workflows", late_done)
    worker.page.evaluate = Mock(wraps=worker.page.evaluate)
    boundary = Mock()
    with pytest.raises(PendingActionOutcomeUnknown, match="ReadBudgetExceeded"):
        approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_called_once()
    submits = [call for call in worker.page.evaluate.call_args_list
               if len(call.args) == 2 and isinstance(call.args[1], dict) and "opinion" in call.args[1]]
    assert len(submits) == 1
    assert worker.readback_closed


def test_progress_only_describes_observed_boundaries_and_contains_no_input(attempt):
    adapter, worker, plan = attempt
    phases = []
    def boundary():
        phases.append("consumed")
    boundary.observe_pending_phase = lambda phase, reason=None: phases.append(phase)
    result = approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=boundary)
    assert result["verification"]["confirmed"] is True
    assert phases.index("final_revalidation") < phases.index("consumed") < phases.index("submission_scheduled")
    assert phases.index("submission_scheduled") < phases.index("submission_invoked") < phases.index("verifying_result")
    assert phases[-1] == "verification_completed"
    assert "request_observed" not in phases  # Synthetic page emitted no request event.
