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
from agentbridge.adapters.seeyon_pending_execution import PendingSubmissionTracker
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
                   if len(call.args) == 2 and isinstance(call.args[1], dict)]
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


def test_missing_scheduling_acknowledgement_is_unknown_and_not_replayed(attempt, monkeypatch):
    adapter, worker, plan = attempt
    original_evaluate = worker.page.evaluate

    def evaluate(script, argument=None):
        if isinstance(argument, dict):
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
    with patch("agentbridge.adapters.seeyon_pending_execution.time.monotonic", side_effect=[0, 0, 61]):
        with pytest.raises(PendingActionOutcomeUnknown, match="done_identity_mismatch"):
            approve_efficiency_data(adapter, worker, plan, enter_commit_boundary=Mock())
    assert worker.readback_closed
    assert worker.page.handlers == {}
