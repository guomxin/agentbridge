"""Real watch service, ownership, grants and locks; only downstream IO is fake."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentbridge.core.business_content_identity import principal_binding
from agentbridge.core.business_watches import BusinessWatchError
from agentbridge.core.central_service import CapabilityRejected
from tests.authorization_fixtures import authorized_service, grant_permissions
from tests.test_workspace import _create_account


def source(number=1):
    return {"affair_id": str(1000 + number), "summary_id": str(2000 + number),
            "process_id": str(3000 + number), "case_id": str(4000 + number),
            "title": f"合成关注事项 {number}"}


def snapshot(*, name="财务复核", state="in_progress"):
    return {"workflow_state": state,
            "current_nodes": [{"name": name, "handlers": ["合成经办人"]}] if state != "ended" else [],
            "current_handlers": ["合成经办人"] if state != "ended" else [],
            "observed_at": datetime.now(timezone.utc).isoformat(), "unavailable_fields": [],
            "completeness": "complete", "read_side_effects": ["may_mark_read"],
            "background_read_safe": False, "safety_level": "foreground_only_may_mark_read"}


class SyntheticWorker:
    def __init__(self, expected):
        self.expected = expected
        self.restored = None
        self.captured = self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def restore_session_state(self, value):
        self.restored = deepcopy(value)

    def capture_session_state(self):
        self.captured = True
        return {"cookies": [], "synthetic": self.expected}


@pytest.fixture
def context(tmp_path):
    workers = []
    def worker_factory(session, _adapter):
        worker = SyntheticWorker(session["expected_principal_ref"])
        workers.append(worker)
        return worker
    service = authorized_service(home=tmp_path, base_url="https://oa.example.test/seeyon/",
                                 worker_factory=worker_factory)
    accounts, sessions = {}, {}
    for subject, username, principal in (("user-a", "watchalice", "Alice"), ("user-b", "watchbob", "Bob")):
        accounts[subject] = _create_account(service, user_subject=subject, username=username,
            endpoint_key="telegram:*:" + username)
        session = service.sessions.get_or_create(user_subject=subject, system_id="oa", expected_principal_ref=principal)
        sessions[subject] = service.sessions.activate(session["session_id"], observed_principal_ref=principal)
        service.session_states.save(session["session_id"], {"cookies": [], "synthetic": principal})
    with patch.object(service.adapter, "probe_business_draft_session",
            side_effect=lambda worker: {"authenticated": True, "observed_principal_ref": worker.expected}) as probe:
        yield SimpleNamespace(service=service, watches=service.business_watch, accounts=accounts,
                              sessions=sessions, workers=workers, probe=probe)


def reference(ctx, *, subject="user-a", number=1, **changes):
    session = ctx.service.sessions.find(user_subject=subject, system_id="oa")
    record = {"user_subject": subject, "session_id": session["session_id"],
              "principal_ref": principal_binding(session),
              "created_at": datetime.now(timezone.utc).isoformat(),
              "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
              "query": {"collection": "done"}, **changes}
    return ctx.watches.target_reference(record, {"title": source(number)["title"], "_source": source(number)})


def create(ctx, *, subject="user-a", number=1, **changes):
    return ctx.watches.create(subject, {"target_ref": reference(ctx, subject=subject, number=number),
        "allow_mark_read": True, **changes})["watch"]


def counts(ctx):
    with sqlite3.connect(ctx.service.db_path) as db:
        return tuple(db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in
                     ("business_watches", "business_watch_observations", "business_watch_changes"))


def permissions(ctx, subject="user-a", *, without=()):
    grant = ctx.service.user_grants.get(subject)
    return grant_permissions(ctx.service.user_grants, subject, set(grant["permissions"]) - set(without))


def test_target_reference_is_subject_session_principal_instance_expiry_and_purpose_bound(context):
    ctx = context
    token = reference(ctx)
    value = ctx.watches.resolve_reference("user-a", token)
    assert value["source"] == source()
    for subject, candidate in (("user-b", token), ("user-a", token[:-2] + "AA"),
                               ("user-a", "not-a-reference"), ("user-a", ctx.watches._pack("page", "user-a", value))):
        with pytest.raises(BusinessWatchError):
            ctx.watches.resolve_reference(subject, candidate)
    for changes in ({"session_id": "another-session"}, {"principal_ref": "another-principal"},
                    {"system_instance": "another-instance"},
                    {"expires_at": (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}):
        altered = ctx.watches._pack("target", "user-a", {**value, **changes})
        with pytest.raises(BusinessWatchError) as error:
            ctx.watches.resolve_reference("user-a", altered)
        assert error.value.code == "WATCH_REFERENCE_EXPIRED"
    assert ctx.workers == []


@pytest.mark.parametrize("missing", ["oa.workflow.read", "oa.workflow.watch"])
def test_create_requires_both_grants_before_downstream_io(context, missing):
    ctx = context
    token = reference(ctx)
    permissions(ctx, without=[missing])
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state") as io:
        with pytest.raises(PermissionError):
            ctx.watches.create("user-a", {"target_ref": token, "allow_mark_read": True})
        io.assert_not_called()
    assert ctx.workers == [] and counts(ctx) == (0, 0, 0)


def test_consent_defaults_false_and_rejected_options_never_navigate(context):
    ctx = context
    for changes in ({}, {"allow_mark_read": False}, {"allow_mark_read": "true"},
                    {"allow_mark_read": True, "endpoint_ids": [ctx.accounts["user-b"]["endpoint_id"]]}):
        with patch("agentbridge.adapters.seeyon_watch.read_workflow_state") as io:
            with pytest.raises(BusinessWatchError):
                ctx.watches.create("user-a", {"target_ref": reference(ctx), **changes})
            io.assert_not_called()
    assert ctx.workers == [] and counts(ctx) == (0, 0, 0)


def test_create_and_check_use_real_observe_session_probe_state_and_restore_server_title(context):
    ctx = context
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: snapshot()) as io:
        watch = create(ctx)
        checked = ctx.watches.check("user-a", watch["watch_id"], expected_version=watch["version"])["watch"]
    assert io.call_count == 2
    assert all(call.kwargs["source"] == source() and call.kwargs["collection"] == "done" for call in io.call_args_list)
    assert checked["version"] > watch["version"] and counts(ctx) == (1, 2, 0)
    assert ctx.probe.call_count == 2
    assert all(worker.restored["synthetic"] == "Alice" and worker.captured and worker.closed for worker in ctx.workers)
    assert "target" not in checked["snapshot"] and "source" not in checked


@pytest.mark.parametrize("state", ["paused", "stopped", "completed"])
def test_paused_and_terminal_watches_never_navigate(context, state):
    ctx = context
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: snapshot()) as io:
        watch = create(ctx)
        if state == "completed":
            watch = ctx.watches.store.apply_observation(watch["watch_id"], user_subject="user-a",
                expected_version=watch["version"], snapshot=snapshot(state="ended"))["watch"]
        else:
            watch = ctx.watches.update("user-a", watch["watch_id"],
                {"action": "pause" if state == "paused" else "stop", "expected_version": watch["version"]})["watch"]
        calls = io.call_count
        with pytest.raises(BusinessWatchError):
            ctx.watches.check("user-a", watch["watch_id"], expected_version=watch["version"])
        assert io.call_count == calls


def test_pause_while_check_in_flight_discards_late_snapshot(context):
    ctx = context
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: snapshot()):
        watch = create(ctx)
    def pause(*_args, **_kwargs):
        ctx.watches.update("user-a", watch["watch_id"], {"action": "pause", "expected_version": watch["version"]})
        return snapshot(name="本次迟到节点")
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=pause):
        with pytest.raises(BusinessWatchError):
            ctx.watches.check("user-a", watch["watch_id"], expected_version=watch["version"])
    saved = ctx.watches.store.get(watch["watch_id"], user_subject="user-a")
    assert saved["state"] == "paused" and saved["last_snapshot"]["current_nodes"][0]["name"] == "财务复核"
    assert counts(ctx) == (1, 1, 0)


@pytest.mark.parametrize("revoked", ["oa.workflow.read", "oa.workflow.watch"])
def test_revocation_while_check_in_flight_cannot_persist_observation(context, revoked):
    ctx = context
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: snapshot()):
        watch = create(ctx)
    def revoke(*_args, **_kwargs):
        permissions(ctx, without=[revoked])
        return snapshot(name="撤权后返回节点")
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=revoke):
        with pytest.raises(PermissionError):
            ctx.watches.check("user-a", watch["watch_id"], expected_version=watch["version"])
    assert counts(ctx) == (1, 1, 0)
    assert ctx.watches.store.get(watch["watch_id"], user_subject="user-a")["version"] == watch["version"]


def test_rebind_while_read_in_flight_discards_result_and_does_not_save_browser_state(context):
    ctx = context
    original = ctx.sessions["user-a"]
    def rebind(*_args, **_kwargs):
        ctx.service.sessions.rebind_expected_principal(original["session_id"], expected_principal_ref="Carol", reason="synthetic rebind")
        ctx.service.sessions.activate(original["session_id"], observed_principal_ref="Carol")
        return snapshot()
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=rebind):
        with pytest.raises(BusinessWatchError):
            create(ctx)
    assert counts(ctx) == (0, 0, 0) and not ctx.workers[0].captured


def test_probe_wrong_account_rejects_before_state_read(context):
    ctx = context
    ctx.probe.side_effect = lambda _worker: {"authenticated": True, "observed_principal_ref": "Bob"}
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state") as io:
        with pytest.raises(BusinessWatchError) as error:
            create(ctx)
        assert error.value.code == "WATCH_PRINCIPAL_CHANGED"
        io.assert_not_called()
    assert ctx.workers[0].closed and not ctx.workers[0].captured and counts(ctx) == (0, 0, 0)


def test_observe_background_never_touches_activity_and_uses_short_session_lock(context):
    ctx = context
    before = ctx.service.sessions.get(ctx.sessions["user-a"]["session_id"])
    with patch.object(ctx.service.sessions, "touch_activity", side_effect=AssertionError("background activity forbidden")) as activity:
        with patch.object(ctx.service, "_session_lock", wraps=ctx.service._session_lock) as lock:
            with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: snapshot()):
                result = ctx.watches._observe("user-a", source(), "done", principal=principal_binding(before), background=True)
    assert result["workflow_state"] == "in_progress"
    activity.assert_not_called()
    assert lock.call_args.kwargs["wait_seconds"] == .05 and lock.call_args.kwargs["purpose"] == "business_watch"
    after = ctx.service.sessions.get(before["session_id"])
    assert after["last_user_activity_at"] == before["last_user_activity_at"]


def test_busy_real_session_mutex_prevents_worker_start(context):
    ctx = context
    session = ctx.sessions["user-a"]
    mutex = ctx.service._session_mutex(session["session_id"])
    acquired, release = threading.Event(), threading.Event()
    def hold():
        with mutex:
            acquired.set()
            release.wait(3)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert acquired.wait(1)
        with pytest.raises(BusinessWatchError) as error:
            ctx.watches._observe("user-a", source(), "done", principal=principal_binding(session), background=True)
        assert isinstance(error.value.__cause__, CapabilityRejected)
        assert error.value.__cause__.code == "SESSION_BUSY" and ctx.workers == []
    finally:
        release.set()
        thread.join(2)


def test_list_cursor_is_bound_to_owner_principal_and_instance(context):
    ctx = context
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: snapshot()):
        for number in range(3):
            create(ctx, number=number)
        create(ctx, subject="user-b", number=9)
    first = ctx.watches.list("user-a", limit=1)
    assert len(first["items"]) == 1 and first["hasMore"] and first["nextCursor"]
    second = ctx.watches.list("user-a", limit=1, cursor=first["nextCursor"])
    assert first["items"][0]["watch_id"] != second["items"][0]["watch_id"]
    for subject, cursor in (("user-b", first["nextCursor"]), ("user-a", first["nextCursor"][:-2] + "AA")):
        with pytest.raises(BusinessWatchError):
            ctx.watches.list(subject, limit=1, cursor=cursor)
    ctx.service.adapter.base_url = "https://another-oa.example.test/seeyon/"
    with pytest.raises(BusinessWatchError):
        ctx.watches.list("user-a", cursor=first["nextCursor"])
    ctx.service.adapter.base_url = "https://oa.example.test/seeyon/"
    ctx.service.sessions.rebind_expected_principal(ctx.sessions["user-a"]["session_id"], expected_principal_ref="Carol", reason="synthetic rebind")
    ctx.service.sessions.activate(ctx.sessions["user-a"]["session_id"], observed_principal_ref="Carol")
    with pytest.raises(BusinessWatchError):
        ctx.watches.list("user-a", cursor=first["nextCursor"])


def test_invalid_create_arguments_are_rejected_before_any_downstream_read(context):
    ctx = context
    variants = ({"idempotency_key": []}, {"idempotency_key": " "}, {"idempotency_key": "x\x00"},
                {"idempotency_key": "x"*201},
                {"event_types": [["nodes"]]}, {"event_types": [{"nodes": True}]})
    for changes in variants:
        with patch("agentbridge.adapters.seeyon_watch.read_workflow_state") as io:
            with pytest.raises(BusinessWatchError):
                ctx.watches.create("user-a", {"target_ref": reference(ctx), "allow_mark_read": True, **changes})
            io.assert_not_called()
    assert ctx.workers == [] and counts(ctx) == (0, 0, 0)


def test_invalid_check_versions_and_update_action_never_read_or_mutate(context):
    ctx = context
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: snapshot()) as io:
        watch = create(ctx)
        for version in (float(watch["version"]), True, False, 0, "2"):
            with pytest.raises(BusinessWatchError):
                ctx.watches.check("user-a", watch["watch_id"], expected_version=version)
        with pytest.raises(BusinessWatchError):
            ctx.watches.update("user-a", watch["watch_id"], {"action": [], "expected_version": watch["version"]})
        assert io.call_count == 1 and counts(ctx) == (1, 1, 0)


@pytest.mark.parametrize("action", ["pause", "stop"])
@pytest.mark.parametrize("loss", ["principal", "session", "permission"])
def test_pause_stop_after_authority_loss_returns_only_minimal_receipt(context, action, loss):
    ctx = context
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: snapshot()):
        watch = create(ctx)
    session = ctx.sessions["user-a"]
    if loss == "principal":
        ctx.service.sessions.rebind_expected_principal(session["session_id"], expected_principal_ref="Carol", reason="synthetic rebind")
        ctx.service.sessions.activate(session["session_id"], observed_principal_ref="Carol")
    elif loss == "session":
        ctx.service.sessions.mark_expired(session["session_id"], "synthetic session expired")
    else:
        permissions(ctx, without=["oa.workflow.read"])
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state") as io:
        result = ctx.watches.update("user-a", watch["watch_id"], {"action": action, "expected_version": watch["version"]})
        io.assert_not_called()
    assert set(result) == {"watch"} and set(result["watch"]) == {"watch_id", "state", "version"}
    assert result["watch"]["state"] == ("paused" if action == "pause" else "stopped")
    assert "title" not in repr(result) and "snapshot" not in repr(result) and "events" not in result


def test_same_key_retry_at_different_time_keeps_original_watch_and_expiry(context):
    ctx = context
    now = datetime.now(timezone.utc)
    token = reference(ctx, created_at=now.isoformat())
    body = {"target_ref": token, "allow_mark_read": True, "idempotency_key": "synthetic-create-once"}
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: snapshot()):
        initial = ctx.watches.create("user-a", body)
        with patch("agentbridge.core.business_watch_service._now", return_value=now+timedelta(seconds=30)):
            replay = ctx.watches.create("user-a", body)
    assert replay["reused"] is True and replay["watch"] == initial["watch"]
    assert replay["watch"]["expires_at"] == (now+timedelta(days=7)).isoformat()
    assert counts(ctx) == (1, 1, 0)


@pytest.mark.parametrize("route", ["get", "list"])
def test_get_list_revalidate_permissions_before_returning_material(context, route):
    ctx = context
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: snapshot()):
        watch = create(ctx)
    method = ctx.watches.store.history if route == "get" else ctx.watches.store.list
    def revoked(*args, **kwargs):
        result = method(*args, **kwargs)
        permissions(ctx, without=["oa.workflow.watch"])
        return result
    with patch.object(ctx.watches.store, "history" if route == "get" else "list", side_effect=revoked):
        with pytest.raises(PermissionError):
            ctx.watches.get("user-a", watch["watch_id"]) if route == "get" else ctx.watches.list("user-a")


def test_same_session_principal_reauthentication_generation_discards_inflight_read(context):
    ctx = context
    original = ctx.sessions["user-a"]
    def reauthenticate(*_args, **_kwargs):
        ctx.service.sessions.rebind_expected_principal(original["session_id"],
            expected_principal_ref="Alice", reason="synthetic same-account reauthentication")
        ctx.service.sessions.activate(original["session_id"], observed_principal_ref="Alice")
        return snapshot()
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=reauthenticate):
        with pytest.raises(BusinessWatchError) as error:
            create(ctx)
    assert error.value.code == "WATCH_SESSION_CHANGED" and counts(ctx) == (0, 0, 0)
    assert ctx.workers[0].closed and not ctx.workers[0].captured
