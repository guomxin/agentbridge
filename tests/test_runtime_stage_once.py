"""Completed audit stages stay atomic across real SQLite writers and restarts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import sqlite3
import threading

import pytest

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.runtime_governance import RuntimeGovernanceStore


START = "2026-11-10T01:00:00+00:00"
FINISH = "2026-11-10T01:00:01.250000+00:00"


class CountingStore(RuntimeGovernanceStore):
    """Observe actual connections/COMMITs without replacing SQLite execution."""
    connections = 0
    commits = 0

    @contextmanager
    def _connect(self, **kwargs):
        self.connections += 1
        with super()._connect(**kwargs) as connection:
            def count(statement):
                if statement.strip().upper() == "COMMIT":
                    self.commits += 1
            connection.set_trace_callback(count)
            yield connection


def new_trace(store, key="stage-once"):
    return store.ensure_trace(user_subject="synthetic-user", request_id=key,
        task_id="synthetic-task-" + key, system_id="oa",
        capability_name="oa.workflow.pending.list")[0]


def test_new_stage_uses_one_commit_and_reuse_stays_read_only(tmp_path):
    store = CountingStore(tmp_path / "runtime.db")
    trace = new_trace(store)
    store.connections = store.commits = 0
    first = store.record_stage_once(trace_id=trace["trace_id"], stage="delivery.result", delivery_id="delivery-one")
    assert (store.connections, store.commits) == (1, 1)
    store.connections = store.commits = 0
    repeated = store.record_stage_once(trace_id=trace["trace_id"], stage="delivery.result", delivery_id="delivery-one")
    assert repeated == first
    assert (store.connections, store.commits) == (1, 0)


@pytest.mark.parametrize("duration,expected", [(None, 1250), (17, 17), (-3, 0)])
def test_explicit_times_duration_metadata_and_trace_defaults_survive_restart(tmp_path, duration, expected):
    path = tmp_path / "runtime.db"
    store = RuntimeGovernanceStore(path)
    trace = new_trace(store)
    kwargs = dict(trace_id=trace["trace_id"], stage="delivery.result", status="waiting",
        operation_id="operation-one", interaction_id="interaction-one", artifact_id="artifact-one",
        delivery_id="delivery-one", error_code="DELIVERY_FAILED", side_effect_boundary="B2_INTERACTION_CREATED",
        started_at=START, finished_at=FINISH, duration_ms=duration,
        metadata={"attemptCount": 2, "nested": {"password": "PRIVATE_VALUE"}, "cookie": "PRIVATE_COOKIE"})
    first = store.record_stage_once(**kwargs)
    assert first["started_at"] == START and first["finished_at"] == FINISH
    assert first["duration_ms"] == expected and first["status"] == "waiting"
    assert first["user_subject"] == "synthetic-user" and first["task_id"] == trace["task_id"]
    assert first["system_id"] == "oa" and first["capability_name"] == "oa.workflow.pending.list"
    for field in ("operation_id", "interaction_id", "artifact_id", "delivery_id", "error_code"):
        assert first[field] == kwargs[field]
    assert first["attempt"] == 1 and first["parent_span_id"] is None
    assert first["metadata"] == {"attemptCount": 2, "nested": {"password": "[redacted]"}, "cookie": "[redacted]"}
    reopened = RuntimeGovernanceStore(path)
    before = reopened.get_trace(trace["trace_id"])
    repeated = reopened.record_stage_once(**{**kwargs, "status": "failed", "duration_ms": 999,
        "side_effect_boundary": "B5_VERIFIED", "metadata": {"attemptCount": 99}})
    assert repeated == first
    assert reopened.get_trace(trace["trace_id"]) == before
    assert len(reopened.trace_detail(trace["trace_id"])["spans"]) == 1


@pytest.mark.parametrize("field", ["operation_id", "interaction_id", "artifact_id", "delivery_id"])
def test_association_filters_keep_distinct_keys_and_omitted_key_wildcards(tmp_path, field):
    store = RuntimeGovernanceStore(tmp_path / "runtime.db")
    trace = new_trace(store)
    common = dict(trace_id=trace["trace_id"], stage="audit.association")
    first = store.record_stage_once(**common, **{field: "first"}, started_at=START)
    second = store.record_stage_once(**common, **{field: "second"}, started_at=FINISH,
        system_id="taihua", capability_name="taihua.work_log.my.list")
    assert second["span_id"] != first["span_id"]
    assert second["system_id"] == "taihua" and second["capability_name"] == "taihua.work_log.my.list"
    assert store.record_stage_once(**common, **{field: "first"}) == first
    assert store.record_stage_once(**common) == second
    assert store.record_stage_once(**common, **{field: ""}) == second
    assert len(store.trace_detail(trace["trace_id"])["spans"]) == 2


@pytest.mark.parametrize("status", ["active", "waiting", "succeeded", "failed", "unknown", "cancelled"])
def test_late_stage_preserves_trace_outcome_finish_and_max_boundary(tmp_path, status):
    clock = [datetime(2026, 11, 10, 1, tzinfo=timezone.utc)]
    store = RuntimeGovernanceStore(tmp_path / "runtime.db", clock=lambda: clock[0])
    trace = new_trace(store)
    store.update_trace(trace["trace_id"], status=status, side_effect_boundary="B3_PREPARED_AUTHORIZED")
    before = store.get_trace(trace["trace_id"])
    clock[0] += timedelta(minutes=1)
    lower = store.record_stage_once(trace_id=trace["trace_id"], stage="delivery.claimed",
        delivery_id="lower", side_effect_boundary="B1_READ_ONLY")
    higher = store.record_stage_once(trace_id=trace["trace_id"], stage="delivery.result",
        delivery_id="higher", side_effect_boundary="B5_VERIFIED")
    after = store.get_trace(trace["trace_id"])
    assert lower["side_effect_boundary"] == "B1_READ_ONLY"
    assert higher["side_effect_boundary"] == "B5_VERIFIED"
    assert after["side_effect_boundary"] == "B5_VERIFIED"
    assert (after["status"], after["finished_at"]) == (before["status"], before["finished_at"])
    assert after["updated_at"] == clock[0].isoformat()


def test_trace_update_failure_rolls_back_the_complete_stage(tmp_path):
    store = RuntimeGovernanceStore(tmp_path / "runtime.db")
    trace = new_trace(store)
    before = store.get_trace(trace["trace_id"])
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("CREATE TRIGGER reject_trace_update BEFORE UPDATE ON runtime_traces "
            "BEGIN SELECT RAISE(ABORT, 'synthetic trace update rejected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic trace update rejected"):
        store.record_stage_once(trace_id=trace["trace_id"], stage="delivery.result", delivery_id="rollback",
            side_effect_boundary="B5_VERIFIED")
    assert store.get_trace(trace["trace_id"]) == before
    assert store.trace_detail(trace["trace_id"])["spans"] == []
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TRIGGER reject_trace_update")
    result = store.record_stage_once(trace_id=trace["trace_id"], stage="delivery.result", delivery_id="rollback")
    assert result["status"] == "succeeded"
    assert len(store.trace_detail(trace["trace_id"])["spans"]) == 1


def test_invalid_new_stage_and_missing_trace_leave_no_partial_evidence(tmp_path):
    store = RuntimeGovernanceStore(tmp_path / "runtime.db")
    trace = new_trace(store)
    before = store.get_trace(trace["trace_id"])
    for changes in ({"status": " "}, {"started_at": "invalid", "finished_at": FINISH},
                    {"duration_ms": "not-a-duration"}, {"side_effect_boundary": "invalid"}):
        with pytest.raises(ValueError):
            store.record_stage_once(trace_id=trace["trace_id"], stage="delivery.result", **changes)
    with pytest.raises(KeyError, match="runtime trace not found"):
        store.record_stage_once(trace_id="missing-trace", stage="delivery.result")
    assert store.get_trace(trace["trace_id"]) == before
    assert store.trace_detail(trace["trace_id"])["spans"] == []


def test_preexisting_duplicate_stages_are_returned_without_rewriting_history(tmp_path):
    store = RuntimeGovernanceStore(tmp_path / "runtime.db")
    trace = new_trace(store)
    rows = []
    for timestamp in (START, FINISH):
        started = store.start_span(trace_id=trace["trace_id"], stage="delivery.result", delivery_id="old-duplicate",
                                   started_at=timestamp)
        rows.append(store.finish_span(started["span_id"], status="succeeded", duration_ms=7))
    before = store.trace_detail(trace["trace_id"])
    result = store.record_stage_once(trace_id=trace["trace_id"], stage="delivery.result", delivery_id="old-duplicate")
    assert result == rows[-1]
    assert store.trace_detail(trace["trace_id"]) == before


def test_eight_store_instances_concurrently_record_one_stage_per_delivery(tmp_path):
    path = tmp_path / "runtime.db"
    store = RuntimeGovernanceStore(path)
    stores = [RuntimeGovernanceStore(path) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for index in range(4):
            trace = new_trace(store, f"concurrent-{index}")
            barrier = threading.Barrier(8)

            def record(worker):
                barrier.wait(timeout=10)
                return stores[worker].record_stage_once(trace_id=trace["trace_id"], stage="delivery.result",
                    delivery_id=f"same-delivery-{index}", side_effect_boundary="B1_READ_ONLY")

            results = list(pool.map(record, range(8)))
            assert len({result["span_id"] for result in results}) == 1
            assert len(store.trace_detail(trace["trace_id"])["spans"]) == 1


def test_real_notification_acks_keep_one_audit_and_one_claim_without_business_writes(tmp_path, monkeypatch):
    key = tmp_path / "test-session.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    monkeypatch.setenv("AGENTBRIDGE_SESSION_KEY_FILE", str(key))

    def deny_worker(*_args):
        raise AssertionError("No downstream worker belongs in this audit test")

    service = CentralCapabilityService(home=tmp_path / "home", base_url="https://oa.example.test/seeyon",
                                       worker_factory=deny_worker)
    endpoint, _ = service.tasks.ensure_endpoint(user_subject="synthetic-user", token_id="synthetic-token",
        agent_host="openclaw", endpoint_key="synthetic-endpoint", client_type="telegram",
        external_subject="synthetic-subject", conversation_ref="synthetic-conversation")
    with ThreadPoolExecutor(max_workers=8) as pool:
        for index in range(3):
            task, _ = service.tasks.ensure_task(user_subject="synthetic-user", agent_host="openclaw",
                host_task_key=f"ack-{index}", origin_endpoint_id=endpoint["endpoint_id"],
                active_conversation_ref="synthetic-conversation", title="Synthetic audit test")
            trace, _ = service.runtime_governance.ensure_trace(user_subject="synthetic-user", task_id=task["task_id"])
            deliveries = service.tasks.claim_outbox(user_subject="synthetic-user", endpoint_id=endpoint["endpoint_id"])
            assert len(deliveries) == 1 and deliveries[0]["task_id"] == task["task_id"]
            delivery_id, barrier = deliveries[0]["delivery_id"], threading.Barrier(8)

            def acknowledge(_worker):
                barrier.wait(timeout=10)
                return service.acknowledge_host_notification(user_subject="synthetic-user", agent_host="openclaw",
                    endpoint_key="synthetic-endpoint", delivery_id=delivery_id, succeeded=True)

            results = list(pool.map(acknowledge, range(8)))
            assert all(result["delivery"]["state"] == "acknowledged" for result in results)
            spans = service.runtime_governance.trace_detail(trace["trace_id"])["spans"]
            assert len(spans) == 1 and spans[0]["stage"] == "delivery.result"
            with sqlite3.connect(service.db_path) as connection:
                assert connection.execute("SELECT attempt_count FROM notification_outbox WHERE delivery_id=?",
                                          (delivery_id,)).fetchone()[0] == 1
    with sqlite3.connect(service.db_path) as connection:
        for table in ("operations", "write_authorizations", "business_draft_decisions"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
