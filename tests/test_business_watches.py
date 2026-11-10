"""Watch persistence uses isolated SQLite ledgers, never OA or model calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading

import pytest

from agentbridge.core.business_watches import BusinessWatchConflict, BusinessWatchError, BusinessWatchStore
from agentbridge.core.tasks import TaskHubStore


class Clock:
    value = datetime(2026, 11, 10, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


@pytest.fixture
def context(tmp_path):
    clock = Clock()
    store = BusinessWatchStore(tmp_path / "ledger.db", clock=clock)
    tasks = TaskHubStore(store.db_path, maintain_on_startup=False)
    endpoints = {}
    for owner in ("user-a", "user-b"):
        endpoints[owner] = tasks.ensure_endpoint(user_subject=owner, token_id="synthetic-" + owner,
            agent_host="openclaw", endpoint_key="synthetic-endpoint-" + owner, client_type="telegram",
            external_subject="synthetic-" + owner, conversation_ref="synthetic-conversation-" + owner)[0]["endpoint_id"]
    return store, clock, endpoints


def source(number=1, collection="done", affair_id="11"):
    return {"collection": collection, "affair_id": affair_id, "summary_id": str(number),
            "process_id": str(1000 + number), "case_id": str(2000 + number)}


def create(store, **kwargs):
    return store.create(**{**dict(user_subject="user-a", principal_ref="principal-a", system_instance="oa-instance",
        source=source(), title="合成业务事项"), **kwargs})


def snapshot(clock, name="审核", handlers=("甲",), *, state="in_progress", **kwargs):
    return {**dict(workflow_state=state, current_nodes=[{"name": name, "handlers": list(handlers)}]
            if state != "ended" else [], current_handlers=list(handlers) if state != "ended" else [],
            observed_at=clock().isoformat(), unavailable_fields=[]), **kwargs}


def observe(store, watch, clock, **kwargs):
    return store.apply_observation(watch["watch_id"], user_subject="user-a", expected_version=watch["version"],
                                   snapshot=snapshot(clock, **kwargs))


def row_counts(store):
    with sqlite3.connect(store.db_path) as db:
        return tuple(db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in
            ("business_watches", "business_watch_observations", "business_watch_changes"))


def test_defaults_public_private_and_restart(context):
    store, clock, endpoints = context
    watch, reused = create(store, endpoints=[endpoints["user-a"]])
    assert not reused and watch["version"] == 1 and watch["state"] == "active"
    assert watch["rules"] == ["nodes", "handlers", "ended"] and watch["interval_seconds"] == 3600
    assert watch["expires_at"] == (clock() + timedelta(days=7)).isoformat()
    assert watch["endpoints"] == [endpoints["user-a"]] and watch["allow_mark_read"] is False
    assert watch["last_snapshot"] is None and watch["last_check_status"] == "never_checked"
    assert not {"source", "target_key", "principal_ref", "system_instance", "user_subject", "next_check_at"}.intersection(watch)
    reopened = BusinessWatchStore(store.db_path, clock=clock)
    assert reopened.get(watch["watch_id"], user_subject="user-a") == watch
    private = reopened.get(watch["watch_id"], user_subject="user-a", include_private=True)
    assert private["source"] == source() and private["principal_ref"] == "principal-a"


def test_create_replay_keeps_original_term_and_different_request_conflicts(context):
    store, clock, _ = context
    first, _ = create(store, idempotency_key="request-one")
    clock.advance(days=1)
    replayed, reused = create(store, idempotency_key="request-one")
    assert replayed == first and reused
    with pytest.raises(BusinessWatchError) as error:
        create(store, idempotency_key="request-one", rules=["ended"])
    assert error.value.code == "BUSINESS_WATCH_IDEMPOTENCY_CONFLICT" and error.value.status == 409
    assert row_counts(store) == (1, 0, 0)


def test_target_dedupes_affairs_but_never_principal_instance_or_owner(context):
    store, _, _ = context
    first, _ = create(store)
    same, reused = create(store, source=source(collection="sent", affair_id="22"), rules=["ended"], title="另一个标题")
    assert reused and same == first
    different = [create(store, principal_ref="principal-other")[0], create(store, system_instance="other-instance")[0],
                 create(store, user_subject="user-b", principal_ref="principal-b")[0]]
    assert len({row["watch_id"] for row in [first, *different]}) == 4


@pytest.mark.parametrize("change", [
    {"interval_seconds": 899}, {"interval_seconds": True}, {"allow_mark_read": 1},
    {"rules": []}, {"rules": ["nodes", "nodes"]}, {"rules": ["arbitrary"]},
    {"source": {**source(), "case_id": "missing"}}, {"source": {**source(), "collection": []}},
    {"expires_at": "2026-11-10T08:00:00"}, {"expires_at": "2026-11-10T07:59:59+00:00"},
    {"expires_at": "2026-12-10T08:00:01+00:00"},
])
def test_invalid_create_has_no_partial_record(context, change):
    store, _, _ = context
    with pytest.raises(BusinessWatchError):
        create(store, **change)
    assert row_counts(store) == (0, 0, 0)


def test_endpoints_are_owned_and_active(context):
    store, _, endpoints = context
    with pytest.raises(BusinessWatchError) as error:
        create(store, endpoints=[endpoints["user-b"]])
    assert error.value.code == "BUSINESS_WATCH_ENDPOINT_DENIED" and error.value.status == 403
    watch, _ = create(store, endpoints=[endpoints["user-a"]])
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?", (endpoints["user-a"],))
    paused = store.update(watch["watch_id"], user_subject="user-a", expected_version=watch["version"], state="paused")
    assert paused["state"] == "paused"
    with pytest.raises(BusinessWatchError):
        store.update(watch["watch_id"], user_subject="user-a", expected_version=paused["version"], state="active")
    assert store.get(watch["watch_id"], user_subject="user-a") == paused
    stopped = store.update(watch["watch_id"], user_subject="user-a", expected_version=paused["version"], state="stopped")
    assert stopped["state"] == "stopped"


def test_other_account_cannot_get_update_observe_or_read_history(context):
    store, clock, _ = context
    watch, _ = create(store)
    calls = [lambda: store.get(watch["watch_id"], user_subject="user-b", include_private=True),
        lambda: store.update(watch["watch_id"], user_subject="user-b", expected_version=1, state="stopped"),
        lambda: store.apply_observation(watch["watch_id"], user_subject="user-b", expected_version=1, snapshot=snapshot(clock)),
        lambda: store.history(watch["watch_id"], user_subject="user-b")]
    for call in calls:
        with pytest.raises(BusinessWatchError) as error:
            call()
        assert error.value.status == 404
    assert store.list(user_subject="user-b")["items"] == []
    assert row_counts(store) == (1, 0, 0)


def test_pause_resume_stop_consent_and_stale_inflight_result(context):
    store, clock, _ = context
    initial, _ = create(store)
    paused = store.update(initial["watch_id"], user_subject="user-a", expected_version=1,
        state="paused", pause_reason="read_side_effect_consent_required")
    assert paused["state"] == "paused" and paused["allow_mark_read"] is False
    with pytest.raises(BusinessWatchConflict):
        observe(store, initial, clock)
    with pytest.raises(BusinessWatchError) as error:
        observe(store, paused, clock)
    assert error.value.code == "BUSINESS_WATCH_NOT_ACTIVE"
    active = store.update(initial["watch_id"], user_subject="user-a", expected_version=paused["version"],
        state="active", allow_mark_read=True)
    assert active["allow_mark_read"] is True and active["pause_reason"] is None
    stopped = store.update(initial["watch_id"], user_subject="user-a", expected_version=active["version"], state="stopped")
    with pytest.raises(BusinessWatchConflict):
        observe(store, active, clock)
    with pytest.raises(BusinessWatchError):
        store.update(initial["watch_id"], user_subject="user-a", expected_version=stopped["version"], state="active")
    new, reused = create(store)
    assert new["watch_id"] != initial["watch_id"] and not reused
    assert row_counts(store) == (2, 0, 0)


def test_expiration_is_visible_read_only_and_old_receipt_never_starts_a_new_watch(context):
    store, clock, _ = context
    watch, _ = create(store, expires_at=(clock() + timedelta(days=30)).isoformat(), idempotency_key="term")
    clock.advance(days=30)
    expired = store.get(watch["watch_id"], user_subject="user-a")
    assert expired["state"] == "expired"
    assert store.list(user_subject="user-a", state="active")["items"] == []
    assert store.list(user_subject="user-a", state="expired")["items"][0]["watch_id"] == watch["watch_id"]
    with pytest.raises(BusinessWatchError):
        observe(store, expired, clock)
    replayed, reused = create(store, expires_at=watch["expires_at"], idempotency_key="term")
    assert reused and replayed["watch_id"] == watch["watch_id"] and replayed["state"] == "expired"
    assert row_counts(store) == (1, 0, 0)
    new, reused = create(store)
    assert not reused and new["watch_id"] != watch["watch_id"]


def test_same_time_owned_pages_have_no_gaps_or_private_fields(context):
    store, _, _ = context
    wanted = {create(store, source=source(index + 1))[0]["watch_id"] for index in range(67)}
    create(store, user_subject="user-b", principal_ref="principal-b")
    seen, cursor = [], {}
    while True:
        page = store.list(user_subject="user-a", limit=11, **cursor)
        seen.extend(item["watch_id"] for item in page["items"])
        assert all("source" not in item and "principal_ref" not in item for item in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    assert set(seen) == wanted and len(seen) == len(set(seen)) == 67
    assert seen == sorted(seen, reverse=True)


def test_baseline_reordering_and_nonsemantic_fields_do_not_notify(context):
    store, clock, _ = context
    watch, _ = create(store)
    baseline = observe(store, watch, clock, handlers=("乙", "甲", "甲"),
        current_nodes=[{"name": "财务", "handlers": ["乙"]}, {"name": "审批", "handlers": ["甲"]}],
        body="PRIVATE_BODY", node_duration_seconds=999)
    assert baseline["baseline_created"] and not baseline["changed"] and baseline["event"] is None
    watch = baseline["watch"]
    clock.advance(seconds=5)
    same = observe(store, watch, clock, handlers=("甲", "乙"),
        current_nodes=[{"name": "审批", "handlers": ["甲"]}, {"name": "财务", "handlers": ["乙"]}])
    assert not same["changed"] and not same["ignored"]
    assert same["watch"]["last_source_observed_at"] == clock().isoformat()
    assert "duration" not in json.dumps(same) and "PRIVATE_BODY" not in json.dumps(same)
    assert store.history(watch["watch_id"], user_subject="user-a") == []
    with sqlite3.connect(store.db_path) as db:
        assert "PRIVATE_BODY" not in str(db.execute("SELECT last_snapshot_json FROM business_watches").fetchone())


def test_repeated_real_transitions_get_new_sequences_and_ended_is_not_payment(context):
    store, clock, _ = context
    watch = observe(store, create(store)[0], clock)["watch"]
    events = []
    for name in ("复核", "审核", "复核"):
        clock.advance(seconds=1)
        result = observe(store, watch, clock, name=name)
        watch = result["watch"]
        assert result["changed"]
        events.append(result["event"])
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert len({event["event_id"] for event in events}) == 3
    completed = observe(store, watch, clock, state="ended")
    assert completed["watch"]["state"] == "completed"
    assert completed["event"]["event_type"] == "watch.completed" and completed["event"]["sequence"] == 4
    assert completed["event"]["diff"]["ended"] == {"before": False, "after": True}
    assert "payment" not in json.dumps(completed)
    history = store.history(watch["watch_id"], user_subject="user-a", before_seq=4, limit=2)
    assert [event["sequence"] for event in history] == [3, 2]


def test_unknown_incomplete_and_older_observations_keep_last_verified_snapshot(context):
    store, clock, _ = context
    watch = observe(store, create(store)[0], clock)["watch"]
    baseline = {key: watch[key] for key in ("last_snapshot", "last_verified_at", "last_source_observed_at")}
    for changes, reason in [({"state": "unknown"}, "unknown"),
        ({"current_nodes": None, "unavailable_fields": ["current_nodes"]}, "incomplete"),
        ({"observed_at": (clock() - timedelta(days=1)).isoformat()}, "older_observation")]:
        clock.advance(seconds=1)
        result = observe(store, watch, clock, **changes)
        watch = result["watch"]
        assert result["ignored"] and result["reason"] == reason and not result["changed"]
        assert {key: watch[key] for key in baseline} == baseline
        assert watch["last_check_status"] == reason and watch["state"] == "active"
    assert row_counts(store) == (1, 4, 0)


@pytest.mark.parametrize("changes", [
    {"workflow_state": []}, {"workflow_state": "finished"}, {"observed_at": None},
    {"observed_at": "2026-11-10T08:00:00"}, {"unavailable_fields": "current_nodes"},
    {"current_nodes": "节点"}, {"current_nodes": [{"name": "审批", "handlers": "甲"}]},
    {"current_handlers": ["另一个人"]}, {"workflow_state": "ended"},
])
def test_invalid_snapshot_never_partially_updates_or_completes(context, changes):
    store, clock, _ = context
    watch = observe(store, create(store)[0], clock)["watch"]
    before = row_counts(store)
    with pytest.raises(BusinessWatchError):
        store.apply_observation(watch["watch_id"], user_subject="user-a", expected_version=watch["version"],
            snapshot={**snapshot(clock), **changes})
    assert store.get(watch["watch_id"], user_subject="user-a") == watch and row_counts(store) == before


def test_rule_change_establishes_new_condition_baseline_without_false_transition(context):
    store, clock, _ = context
    watch, _ = create(store, rules=["ended"])
    watch = observe(store, watch, clock, current_nodes=None, current_handlers=None,
        unavailable_fields=["current_nodes", "current_handlers"])["watch"]
    watch = store.update(watch["watch_id"], user_subject="user-a", expected_version=watch["version"],
                         rules=["nodes", "handlers", "ended"])
    result = observe(store, watch, clock)
    assert not result["changed"] and result["watch"]["last_snapshot"]["current_nodes"]


def test_first_verified_ended_baseline_is_completed_without_inventing_a_change(context):
    store, clock, _ = context
    result = observe(store, create(store)[0], clock, state="ended")
    assert result["baseline_created"] and not result["changed"]
    assert result["watch"]["state"] == "completed" and row_counts(store) == (1, 1, 0)


def test_callback_and_snapshot_change_commit_or_rollback_together(context):
    store, clock, _ = context
    with sqlite3.connect(store.db_path) as db:
        db.execute("CREATE TABLE synthetic_outbox(event_id TEXT PRIMARY KEY)")
    failure = [True]

    def callback(connection, event, private_watch):
        assert connection.in_transaction and private_watch["source"] == source()
        connection.execute("INSERT INTO synthetic_outbox VALUES (?)", (event["event_id"],))
        if failure[0]:
            raise RuntimeError("synthetic notification transaction rejected")

    store.on_change = callback
    watch = observe(store, create(store)[0], clock)["watch"]
    with pytest.raises(RuntimeError, match="synthetic notification transaction rejected"):
        observe(store, watch, clock, name="复核")
    assert store.get(watch["watch_id"], user_subject="user-a") == watch and row_counts(store) == (1, 1, 0)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM synthetic_outbox").fetchone()[0] == 0
    failure[0] = False
    result = observe(store, watch, clock, name="复核")
    assert result["event"]["sequence"] == 1
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT event_id FROM synthetic_outbox").fetchone()[0] == result["event"]["event_id"]


def test_concurrent_creation_and_same_version_observation_have_one_winner(context):
    store, clock, _ = context
    instances = [BusinessWatchStore(store.db_path, clock=clock) for _ in range(8)]
    barrier = threading.Barrier(8)

    def creating(index):
        barrier.wait(timeout=10)
        return create(instances[index])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(creating, range(8)))
    assert len({watch["watch_id"] for watch, _ in results}) == 1
    assert sum(not reused for _, reused in results) == 1
    watch = results[0][0]
    barrier = threading.Barrier(8)

    def observing(index):
        barrier.wait(timeout=10)
        try:
            return observe(instances[index], watch, clock)
        except BusinessWatchConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        observed = list(pool.map(observing, range(8)))
    assert sum(result is not None for result in observed) == 1
    assert row_counts(store) == (1, 1, 0)
