from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest

from agentbridge.browser.central import CentralBrowserWorker
from agentbridge.browser.read_budget import (
    ReadBudgetExceeded, check_read_deadline, current_read_deadline,
    read_deadline_scope, remaining_seconds,
)


def test_nested_recovery_cannot_restart_its_callers_budget(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 10)
    assert remaining_seconds(30) == 30
    with read_deadline_scope(12):
        with read_deadline_scope(40):
            assert current_read_deadline() == 12
            assert remaining_seconds(30) == 2
        with read_deadline_scope(11):
            assert remaining_seconds(30) == 1
        # The post-consumption phase deliberately has its own budget.
        with read_deadline_scope(40, inherit=False):
            assert remaining_seconds(30) == 30
        assert current_read_deadline() == 12
    assert current_read_deadline() is None


def test_expired_scope_restores_context_and_does_not_allow_io(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 10)
    with pytest.raises(ReadBudgetExceeded):
        with read_deadline_scope(9):
            pytest.fail("Expired budgets must fail before entering")
    assert current_read_deadline() is None


def test_native_request_and_navigation_receive_remaining_timeout(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    response = SimpleNamespace(url="https://oa.test/path", headers={}, status=200,
                               text=lambda: "ok", dispose=Mock())
    page = SimpleNamespace(url="https://oa.test/path", goto=Mock())
    context = SimpleNamespace(pages=[page], request=SimpleNamespace(fetch=Mock(return_value=response)))
    worker = CentralBrowserWorker(profile_path=tmp_path, allowed_origins={"https://oa.test"})
    worker._context = context
    with read_deadline_scope(10.06):
        worker.request("GET", "https://oa.test/path")
        assert context.request.fetch.call_args.kwargs["timeout"] == pytest.approx(60)
        clock[0] += .04
        worker.goto("https://oa.test/path")
        assert page.goto.call_args.kwargs["timeout"] == pytest.approx(20)
        clock[0] += .03
        with pytest.raises(ReadBudgetExceeded):
            worker.request("GET", "https://oa.test/path")
    assert context.request.fetch.call_count == 1
    response.dispose.assert_called_once()
    # Other capabilities keep the original per-request timeout.
    worker.request("GET", "https://oa.test/path")
    assert context.request.fetch.call_args.kwargs["timeout"] == 30000


def test_late_native_response_is_disposed_and_never_returned_as_evidence(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    response = SimpleNamespace(url="https://oa.test/path", headers={}, status=200,
                               text=lambda: "ok", dispose=Mock())
    def slow_fetch(*args, **kwargs):
        clock[0] += 1
        return response
    worker = CentralBrowserWorker(profile_path=tmp_path, allowed_origins={"https://oa.test"})
    worker._context = SimpleNamespace(request=SimpleNamespace(fetch=slow_fetch))
    with read_deadline_scope(10.06), pytest.raises(ReadBudgetExceeded):
        worker.request("GET", "https://oa.test/path")
    response.dispose.assert_called_once()
