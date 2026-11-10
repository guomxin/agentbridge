"""Background watch integration: real ownership, session locks and check leases."""
from datetime import datetime, timedelta, timezone
import sqlite3
import threading
from unittest.mock import patch

import pytest

from agentbridge.core.business_content_identity import principal_binding
from agentbridge.core.business_watches import BusinessWatchStore
from agentbridge.browser.read_budget import remaining_seconds
from tests.test_business_watch_service import context, permissions, snapshot, source


class Clock:
    def __init__(self):
        self.value = datetime.now(timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


@pytest.fixture
def scheduled(context):
    context.clock = Clock()
    context.watches.store.clock = context.clock
    return context


def observed(ctx, **kwargs):
    return {**snapshot(**kwargs), "observed_at": ctx.clock().isoformat()}


def seed(ctx, number=1, *, consent=True):
    subject = "user-a"
    session = ctx.service.sessions.find(user_subject=subject, system_id="oa")
    value = source(number)
    watch, _ = ctx.watches.store.create(user_subject=subject, principal_ref=principal_binding(session),
        system_instance=ctx.watches.system_instance, source={**value, "collection": "done"},
        title=value["title"], allow_mark_read=consent, include_private=True)
    return ctx.watches.store.apply_observation(watch["watch_id"], user_subject=subject,
        expected_version=watch["version"], snapshot=observed(ctx), include_private=True)["watch"]


def saved(ctx, watch):
    return ctx.watches.store.get(watch["watch_id"], user_subject="user-a", include_private=True)


def ledger_counts(ctx):
    with sqlite3.connect(ctx.service.db_path) as db:
        return tuple(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in
            ("business_watch_observations", "business_watch_changes"))


@pytest.mark.parametrize("loss", ["read_permission", "watch_permission", "principal", "instance", "consent", "session"])
def test_due_checks_validate_current_authority_and_consent_before_any_io(scheduled, loss):
    ctx = scheduled
    watch = seed(ctx, consent=loss != "consent")
    ctx.clock.advance(hours=2)
    if loss.endswith("permission"):
        permissions(ctx, without=["oa.workflow.read" if loss == "read_permission" else "oa.workflow.watch"])
    elif loss == "principal":
        session = ctx.sessions["user-a"]
        ctx.service.sessions.rebind_expected_principal(session["session_id"], expected_principal_ref="Carol", reason="synthetic changed principal")
        ctx.service.sessions.activate(session["session_id"], observed_principal_ref="Carol")
    elif loss == "instance":
        ctx.service.adapter.base_url = "https://different-oa.example.test/seeyon/"
    elif loss == "session":
        ctx.service.sessions.mark_expired(ctx.sessions["user-a"]["session_id"], "synthetic expired login")
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state") as io:
        result = ctx.watches.run_due_checks(limit=4)
        io.assert_not_called()
    current = saved(ctx, watch)
    if loss == "consent":
        # The store excludes even a legacy active row without read consent.
        assert result["claimed"] == 0 and result["paused"] == 0
        assert not current["allow_mark_read"]
    else:
        assert result["claimed"] == 1 and result["paused"] == 1
        assert current["state"] == "paused" and current["pause_reason"]
    assert current["last_snapshot"] == watch["last_snapshot"] and ledger_counts(ctx) == (1, 0)
    assert ctx.workers == []


def test_due_checks_are_bounded_and_use_fresh_short_traces_without_activity(scheduled):
    ctx = scheduled
    watches = [seed(ctx, number) for number in range(3)]
    ctx.clock.advance(hours=2)
    def read(*_args, **_kwargs):
        assert 0 < remaining_seconds(60) <= 15
        return observed(ctx, name="当前新节点")
    with patch.object(ctx.service.sessions, "touch_activity", side_effect=AssertionError("background activity forbidden")) as touch:
        with patch.object(ctx.service.runtime_governance, "ensure_trace", wraps=ctx.service.runtime_governance.ensure_trace) as traces:
            with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=read) as io:
                result = ctx.watches.run_due_checks(limit=2)
    assert result["claimed"] == 2 and result["checked"] == 2 and result["changed"] == 2
    assert io.call_count == 2 and traces.call_count == 2
    assert len({call.kwargs["request_id"] for call in traces.call_args_list}) == 2
    assert all(call.kwargs["request_kind"] == "read" for call in traces.call_args_list)
    touch.assert_not_called()
    assert ledger_counts(ctx) == (5, 2)
    assert sum(saved(ctx, watch)["version"] > watch["version"] for watch in watches) == 2


def test_pausing_during_io_discards_snapshot_and_never_resurrects_watch(scheduled):
    ctx = scheduled
    watch = seed(ctx)
    ctx.clock.advance(hours=2)
    def pause(*_args, **_kwargs):
        ctx.watches.update("user-a", watch["watch_id"], {"action": "pause", "expected_version": watch["version"]})
        return observed(ctx, name="应丢弃的迟到节点")
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=pause):
        result = ctx.watches.run_due_checks(limit=1)
    current = saved(ctx, watch)
    assert result["discarded"] == 1 and current["state"] == "paused"
    assert current["last_snapshot"] == watch["last_snapshot"] and ledger_counts(ctx) == (1, 0)


@pytest.mark.parametrize("permission", ["oa.workflow.read", "oa.workflow.watch"])
def test_revocation_during_io_never_commits_new_observation(scheduled, permission):
    ctx = scheduled
    watch = seed(ctx)
    ctx.clock.advance(hours=2)
    def revoke(*_args, **_kwargs):
        permissions(ctx, without=[permission])
        return observed(ctx, name="撤权后返回节点")
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=revoke):
        ctx.watches.run_due_checks(limit=1)
    current = saved(ctx, watch)
    assert current["state"] == "paused" and current["last_snapshot"] == watch["last_snapshot"]
    assert ledger_counts(ctx) == (1, 0)


def test_same_session_same_principal_reauthentication_discards_old_generation(scheduled):
    ctx = scheduled
    watch = seed(ctx)
    ctx.clock.advance(hours=2)
    session = ctx.sessions["user-a"]
    def reauthenticate(*_args, **_kwargs):
        ctx.service.sessions.rebind_expected_principal(session["session_id"], expected_principal_ref="Alice", reason="synthetic same-principal login")
        ctx.service.sessions.activate(session["session_id"], observed_principal_ref="Alice")
        return observed(ctx, name="旧会话代际的返回节点")
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=reauthenticate):
        result = ctx.watches.run_due_checks(limit=1)
    current = saved(ctx, watch)
    assert result["checked"] == 0
    assert current["last_snapshot"] == watch["last_snapshot"] and ledger_counts(ctx) == (1, 0)
    assert not ctx.workers[0].captured


def test_busy_foreground_session_yields_without_starting_a_reader(scheduled):
    ctx = scheduled
    watch = seed(ctx)
    ctx.clock.advance(hours=2)
    mutex = ctx.service._session_mutex(ctx.sessions["user-a"]["session_id"])
    acquired, release = threading.Event(), threading.Event()
    def foreground():
        with mutex:
            acquired.set()
            release.wait(3)
    thread = threading.Thread(target=foreground)
    thread.start()
    try:
        assert acquired.wait(1)
        with patch("agentbridge.adapters.seeyon_watch.read_workflow_state") as io:
            result = ctx.watches.run_due_checks(limit=1)
            io.assert_not_called()
        current = saved(ctx, watch)
        assert result["claimed"] == 1 and current["state"] == "active"
        assert current["last_snapshot"] == watch["last_snapshot"] and current["next_check_at"]
        assert ctx.workers == [] and ledger_counts(ctx) == (1, 0)
    finally:
        release.set()
        thread.join(2)


def test_restart_recovers_expired_check_lease_and_fences_old_holder(scheduled):
    ctx = scheduled
    watch = seed(ctx)
    ctx.clock.advance(hours=2)
    old = ctx.watches.store.claim_due(limit=1, lease_seconds=15)[0]
    assert old["watch"]["watch_id"] == watch["watch_id"]
    ctx.clock.advance(seconds=16)
    ctx.watches.store = BusinessWatchStore(ctx.service.db_path, clock=ctx.clock)
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: observed(ctx, name="重启后新观察节点")) as io:
        result = ctx.watches.run_due_checks(limit=1)
    assert result["checked"] == 1 and io.call_count == 1
    before = saved(ctx, watch)
    late = ctx.watches.store.finish_check(old["check_id"], old["lease_token"], snapshot=observed(ctx, name="过期租约的节点"))
    assert late["status"] == "discarded" and saved(ctx, watch) == before
    assert before["last_snapshot"]["current_nodes"][0]["name"] == "重启后新观察节点"


def test_unknown_observation_retains_last_verified_snapshot_and_schedules_again(scheduled):
    ctx = scheduled
    watch = seed(ctx)
    ctx.clock.advance(hours=2)
    unknown = {**observed(ctx), "workflow_state": "unknown", "current_nodes": None, "current_handlers": None,
               "unavailable_fields": ["workflow_state", "current_nodes", "current_handlers"], "completeness": "partial"}
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", return_value=unknown):
        result = ctx.watches.run_due_checks(limit=1)
    current = saved(ctx, watch)
    assert result["claimed"] == 1 and result["changed"] == 0
    assert result["checked"] == 0 and result["failed"] == 1
    assert current["state"] == "active" and current["last_snapshot"] == watch["last_snapshot"]
    assert current["last_check_status"] == "unknown" and current["next_check_at"]
    assert ledger_counts(ctx) == (2, 0)
    with sqlite3.connect(ctx.service.db_path) as db:
        traces = db.execute("SELECT status,finished_at FROM runtime_traces WHERE request_id LIKE 'business-watch:%'").fetchall()
    assert len(traces) == 1 and traces[0][0] == "failed" and traces[0][1]


def test_shutdown_signal_does_not_claim_another_target_after_current_io(scheduled):
    ctx = scheduled
    watches = [seed(ctx, number) for number in range(2)]
    ctx.clock.advance(hours=2)
    stopped = threading.Event()
    def read(*_args, **_kwargs):
        stopped.set()
        return observed(ctx)
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=read) as io:
        result = ctx.watches.run_due_checks(limit=4, should_stop=stopped.is_set)
    assert result["claimed"] == 1 and result["checked"] == 1 and io.call_count == 1
    assert sum(saved(ctx, watch)["version"] > watch["version"] for watch in watches) == 1


def test_background_reader_thread_does_not_block_runtime_governance_cycle(scheduled):
    from agentbridge.core.business_watch_worker import BusinessWatchWorker
    from agentbridge.mcp.central import CentralRuntimeGovernanceWorker
    ctx = scheduled
    watch = seed(ctx)
    ctx.clock.advance(hours=2)
    entered, release, tick_done = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def slow_read(*_args, **_kwargs):
        entered.set()
        release.wait(4)
        return observed(ctx)
    def governance_tick():
        try:
            CentralRuntimeGovernanceWorker(ctx.service).run_cycle()
        except Exception as exc:
            errors.append(exc)
        finally:
            tick_done.set()
    worker = BusinessWatchWorker(ctx.service, interval_seconds=60, initial_delay_seconds=0)
    tick = None
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=slow_read):
        worker.start()
        try:
            assert entered.wait(2)
            tick = threading.Thread(target=governance_tick)
            tick.start()
            assert tick_done.wait(2), "governance must finish while the watch reader remains blocked"
            assert not release.is_set() and not errors
        finally:
            release.set()
            worker.stop()
            if tick:
                tick.join(2)
    assert saved(ctx, watch)["state"] == "active"
