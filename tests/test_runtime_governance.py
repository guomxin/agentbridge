from __future__ import annotations

from tests.authorization_fixtures import authorized_service
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.operations import OperationStore
from agentbridge.core.runtime_governance import (
    RuntimeGovernanceStore,
    classify_runtime_error,
)
from agentbridge.mcp.central import CentralRuntimeGovernanceWorker


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 23, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class RuntimeGovernanceStoreTests(unittest.TestCase):
    def test_trace_spans_persist_and_sensitive_metadata_is_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agentbridge.db"
            store = RuntimeGovernanceStore(db_path, release_id="release-one")
            trace, reused = store.ensure_trace(
                user_subject="user-a",
                request_id="request-1",
                task_id="task-1234567890-abcdef",
                host_type="openclaw",
                host_instance_id="gateway-one",
                request_kind="read",
                system_id="oa",
                capability_name="oa.workflow.pending.list",
            )
            self.assertFalse(reused)
            span = store.start_span(
                trace_id=trace["trace_id"],
                stage="mcp.request",
                metadata={
                    "requestId": "request-1",
                    "password": "must-not-persist",
                    "cookie": "must-not-persist",
                },
            )
            store.finish_span(span["span_id"], status="succeeded", duration_ms=12)

            reopened = RuntimeGovernanceStore(db_path, release_id="release-two")
            detail = reopened.trace_detail(trace["trace_id"])

            self.assertEqual(detail["trace"]["release_id"], "release-one")
            self.assertEqual(detail["spans"][0]["duration_ms"], 12)
            self.assertEqual(detail["spans"][0]["metadata"]["password"], "[redacted]")
            self.assertNotIn("must-not-persist", db_path.read_bytes().decode("utf-8", errors="ignore"))

    def test_task_trace_is_reused_across_interaction_resume(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RuntimeGovernanceStore(Path(tmp) / "agentbridge.db")
            first, reused = store.ensure_trace(
                user_subject="user-a",
                task_id="task-1234567890-abcdef",
                host_type="openclaw",
                request_kind="interaction",
            )
            self.assertFalse(reused)
            store.update_trace(first["trace_id"], status="waiting", finished=False)
            second, reused = store.ensure_trace(
                user_subject="user-a",
                task_id="task-1234567890-abcdef",
                host_type="openclaw",
                host_run_id="resume-run",
                request_kind="write",
            )
            self.assertTrue(reused)
            self.assertEqual(second["trace_id"], first["trace_id"])
            self.assertEqual(second["host_run_id"], "resume-run")

    def test_unknown_write_creates_one_manual_reconciliation_incident(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agentbridge.db"
            operations = OperationStore(db_path)
            store = RuntimeGovernanceStore(db_path)
            trace, _ = store.ensure_trace(
                user_subject="user-a",
                request_id="request-unknown",
                request_kind="write",
                system_id="oa",
                capability_name="oa.leave.submit",
            )
            operation, _ = operations.create(
                user_subject="user-a",
                capability_name="oa.leave.submit",
                capability_version="1.0.0",
                input_summary={"authorization": {"redacted": True}},
                idempotency_key="submit-once",
            )
            operation = operations.mark_unknown(
                operation["operation_id"],
                code="RESULT_UNKNOWN",
                message="authoritative readback failed",
            )
            store.observe_operation(
                trace_id=trace["trace_id"],
                operation=operation,
                capability_effect="controlled_write",
                commit_capability=True,
            )
            store.evaluate_incidents()
            incidents = store.list_incidents()

            self.assertEqual(len(incidents), 1)
            self.assertEqual(incidents[0]["severity"], "P1")
            self.assertEqual(incidents[0]["actionability"], "manual_reconciliation")
            self.assertEqual(incidents[0]["evidence"]["boundary"], "B4_COMMIT_ATTEMPTED")

    def test_closed_unknown_attempt_is_not_reopened_but_new_attempt_alerts(self) -> None:
        for disposition in ("resolved", "suppressed"):
            with self.subTest(disposition=disposition), TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "agentbridge.db"
                operations = OperationStore(db_path)
                store = RuntimeGovernanceStore(db_path)
                operation, _ = operations.create(
                    user_subject="user-a", capability_name="oa.leave.submit",
                    capability_version="1", input_summary={}, idempotency_key="old-attempt",
                )
                operations.mark_unknown(operation["operation_id"], code="RESULT_UNKNOWN", message="test")
                store.evaluate_incidents()
                incident = store.list_incidents()[0]
                self.assertEqual(incident["operation_created_at"], operation["created_at"])
                store.transition_incident(incident["incident_id"], state=disposition, actor="maintenance", reason="user approved historical closure")
                for _ in range(2):
                    result = store.evaluate_incidents()
                    self.assertEqual(result["observed"], 0)
                    self.assertEqual(result["open"], 0)
                listed = store.list_incidents()
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["state"], disposition)
                self.assertEqual(listed[0]["occurrence_count"], incident["occurrence_count"])
                self.assertEqual(listed[0]["last_seen_at"], incident["last_seen_at"])
                self.assertEqual(listed[0]["disposition_actor"], "maintenance")
                self.assertEqual(listed[0]["disposition_reason"], "user approved historical closure")
                with closing(sqlite3.connect(db_path)) as connection:
                    self.assertEqual(connection.execute("SELECT status FROM operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()[0], "unknown")
                other, _ = operations.create(
                    user_subject="user-a", capability_name="oa.leave.submit",
                    capability_version="1", input_summary={}, idempotency_key="new-attempt",
                )
                operations.mark_unknown(other["operation_id"], code="RESULT_UNKNOWN", message="test")
                self.assertEqual(store.evaluate_incidents()["open"], 1)
                self.assertEqual(len(store.list_incidents()), 2)

    def test_isolation_incident_auto_resolves_when_condition_clears(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RuntimeGovernanceStore(Path(tmp) / "agentbridge.db")
            failing = {
                "isolation": {"violations": {"task_user_mismatch": 1}}
            }
            store.evaluate_incidents(task_diagnostics=failing)
            active = store.list_incidents(state="open")
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["severity"], "P0")

            store.evaluate_incidents(
                task_diagnostics={
                    "isolation": {"violations": {"task_user_mismatch": 0}}
                }
            )
            self.assertEqual(store.list_incidents(state="open"), [])
            self.assertEqual(len(store.list_incidents(state="resolved")), 1)

    def test_slo_rollup_separates_success_verify_and_unknown(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RuntimeGovernanceStore(Path(tmp) / "agentbridge.db")
            trace, _ = store.ensure_trace(
                user_subject="user-a",
                request_id="request-success",
                request_kind="write",
            )
            store.record_stage_once(
                trace_id=trace["trace_id"],
                stage="adapter.request",
                status="succeeded",
                side_effect_boundary="B5_VERIFIED",
                duration_ms=25,
            )
            store.update_trace(
                trace["trace_id"],
                status="succeeded",
                side_effect_boundary="B5_VERIFIED",
            )

            result = store.refresh_slo_rollups(hours=24)
            metrics = {item["metricKey"]: item for item in result["metrics"]}
            self.assertEqual(metrics["trace_success_rate"]["value"], 1.0)
            self.assertEqual(metrics["write_verify_coverage"]["value"], 1.0)
            self.assertEqual(metrics["identity_isolation_violations"]["value"], 0.0)

    def test_recovery_actions_are_idempotent_and_auditable(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RuntimeGovernanceStore(Path(tmp) / "agentbridge.db")
            first, reused = store.start_recovery_action(
                action_type="retry_delivery",
                target_type="delivery",
                target_id="delivery-1",
                actor="admin",
                reason="terminal result was not delivered",
                idempotency_key="retry-delivery-1-once",
                side_effect_boundary="B0_NO_EFFECT",
                before={"state": "failed"},
            )
            self.assertFalse(reused)
            store.finish_recovery_action(
                first["action_id"],
                status="succeeded",
                after={"state": "pending"},
            )
            second, reused = store.start_recovery_action(
                action_type="retry_delivery",
                target_type="delivery",
                target_id="delivery-1",
                actor="admin",
                reason="terminal result was not delivered",
                idempotency_key="retry-delivery-1-once",
                side_effect_boundary="B0_NO_EFFECT",
            )
            self.assertTrue(reused)
            self.assertEqual(second["action_id"], first["action_id"])
            self.assertEqual(second["status"], "succeeded")

    def test_readiness_distinguishes_full_service_schema_from_bare_store(self) -> None:
        with TemporaryDirectory() as tmp:
            service = authorized_service(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            readiness = service.runtime_governance.readiness()
            self.assertEqual(readiness["status"], "ready")
            self.assertTrue(all(item["status"] == "healthy" for item in readiness["checks"]))

    def test_governance_cycle_runs_independently_from_session_keepalive(self) -> None:
        with TemporaryDirectory() as tmp:
            service = authorized_service(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
                session_keepalive_lease_seconds=None,
            )
            result = CentralRuntimeGovernanceWorker(service).run_cycle()
            self.assertIn("taskPlans", result)
            self.assertIn("evaluation", result)
            self.assertIn("slo", result)
            self.assertIn("observations", result)
            self.assertIn("retention", result)

    def test_shadow_observation_captures_hourly_and_completes_without_business_calls(self) -> None:
        clock = MutableClock()
        with TemporaryDirectory() as tmp:
            store = RuntimeGovernanceStore(Path(tmp) / "agentbridge.db", clock=clock)
            observation, reused = store.start_observation(
                name="seven-day-shadow",
                duration_hours=2,
                created_by="test-operator",
                policy={"mode": "shadow", "automaticBusinessRecovery": False},
            )
            self.assertFalse(reused)
            first = store.capture_observation_snapshots()
            self.assertEqual(first["captured"], 1)
            self.assertEqual(first["items"][0]["collection"]["businessCalls"], 0)
            self.assertEqual(first["items"][0]["collection"]["businessListReads"], 0)
            self.assertEqual(first["items"][0]["collection"]["businessWrites"], 0)

            self.assertEqual(store.capture_observation_snapshots()["captured"], 0)
            clock.value += timedelta(minutes=60)
            self.assertEqual(store.capture_observation_snapshots()["captured"], 1)
            clock.value += timedelta(minutes=60)
            completed = store.capture_observation_snapshots()
            self.assertEqual(completed["completed"], 1)

            detail = store.observation_detail(observation["observation_id"])
            self.assertEqual(detail["observation"]["state"], "completed")
            self.assertEqual(detail["observation"]["snapshot_count"], 3)
            self.assertEqual(len(detail["snapshots"]), 3)
            self.assertEqual(len(detail["dailySummaries"]), 1)
            self.assertEqual(detail["dailySummaries"][0]["snapshotCount"], 3)
            self.assertEqual(detail["dailySummaries"][0]["businessWrites"], 0)
            self.assertEqual(store.summary()["active_observations"], 0)

    def test_start_observation_reuses_active_name(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RuntimeGovernanceStore(Path(tmp) / "agentbridge.db")
            first, reused = store.start_observation(
                name="shadow",
                created_by="operator",
            )
            self.assertFalse(reused)
            second, reused = store.start_observation(
                name="shadow",
                created_by="operator",
            )
            self.assertTrue(reused)
            self.assertEqual(first["observation_id"], second["observation_id"])

    def test_detector_finds_stalled_operation_task_and_delivery_without_business_calls(self) -> None:
        with TemporaryDirectory() as tmp:
            service = authorized_service(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            operation, _ = service.operations.create(
                user_subject="user-a",
                capability_name="oa.pending.list",
                capability_version="1.0.0",
                input_summary={},
                idempotency_key="stalled-read",
            )
            trace, _ = service.runtime_governance.ensure_trace(
                user_subject="user-a",
                request_id="stalled-read-request",
                request_kind="read",
                system_id="oa",
                capability_name="oa.pending.list",
            )
            service.runtime_governance.observe_operation(
                trace_id=trace["trace_id"],
                operation=operation,
                capability_effect="read",
                commit_capability=False,
            )
            task = service.ensure_host_task(
                user_subject="user-a",
                token_id="token-a",
                agent_host="reference-host",
                host_task_key="stalled-task",
                endpoint_key="reference:user-a",
                client_type="telegram",
                external_subject="user-a",
                conversation_ref="reference:user-a",
                title="Stalled test task",
                capabilities=["direct_status"],
            )["task"]
            with closing(sqlite3.connect(service.db_path)) as connection:
                connection.execute(
                    "UPDATE operations SET updated_at = '2000-01-01T00:00:00+00:00' WHERE operation_id = ?",
                    (operation["operation_id"],),
                )
                connection.execute(
                    "UPDATE agent_tasks SET updated_at = '2000-01-01T00:00:00+00:00' WHERE task_id = ?",
                    (task["taskId"],),
                )
                connection.execute(
                    "UPDATE notification_outbox SET state = 'failed', updated_at = '2000-01-01T00:00:00+00:00' WHERE task_id = ?",
                    (task["taskId"],),
                )
                connection.commit()

            result = CentralRuntimeGovernanceWorker(service).run_cycle()
            rules = {item["rule_id"] for item in service.runtime_governance.list_incidents()}
            self.assertEqual(
                {"operation_stalled", "task_stalled", "delivery_stalled"} - rules,
                set(),
            )
            self.assertEqual(result["evaluation"]["observed"], 3)

    def test_historical_orphans_do_not_pollute_current_incidents_or_slo(self) -> None:
        clock = MutableClock()
        with TemporaryDirectory() as tmp:
            service = authorized_service(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            service.runtime_governance._clock = clock
            operation, _ = service.operations.create(
                user_subject="user-a",
                capability_name="oa.leave.submit",
                capability_version="1.0.0",
                input_summary={},
                idempotency_key="historical-unknown",
            )
            service.operations.mark_unknown(
                operation["operation_id"],
                code="RESULT_UNKNOWN",
                message="historical test",
            )
            with closing(sqlite3.connect(service.db_path)) as connection:
                connection.execute(
                    "UPDATE operations SET updated_at = '2026-07-01T00:00:00+00:00' "
                    "WHERE operation_id = ?",
                    (operation["operation_id"],),
                )
                connection.commit()

            service.runtime_governance.evaluate_incidents()
            slo = service.runtime_governance.refresh_slo_rollups(hours=24)
            metrics = {item["metricKey"]: item for item in slo["metrics"]}

            self.assertEqual(metrics["write_outcome_unknown"]["value"], 0.0)
            self.assertEqual(
                service.runtime_governance.list_incidents()[0]["actionability"],
                "manual_reconciliation",
            )

    def test_prune_removes_raw_history_but_keeps_incidents(self) -> None:
        clock = MutableClock()
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agentbridge.db"
            store = RuntimeGovernanceStore(db_path, clock=clock)
            trace, _ = store.ensure_trace(
                user_subject="user-a",
                request_id="request-old",
                request_kind="read",
            )
            store.record_stage_once(
                trace_id=trace["trace_id"],
                stage="mcp.request",
                duration_ms=5,
            )
            store.upsert_incident(
                rule_id="manual-test",
                severity="P3",
                symptom_code="TEST",
                actionability="historical",
                title="test incident",
            )
            clock.value += timedelta(days=31)
            removed = store.prune(raw_days=30)

            self.assertEqual(removed["runtime_spans"], 1)
            self.assertEqual(len(store.list_incidents()), 1)


class RuntimeFailureClassificationTests(unittest.TestCase):
    def test_failure_classification_preserves_result_unknown_boundary(self) -> None:
        error = RuntimeError("RESULT_UNKNOWN after commit")
        self.assertEqual(classify_runtime_error(error)["code"], "RESULT_UNKNOWN")

    def test_timeout_is_classified_without_claiming_unknown_write(self) -> None:
        error = TimeoutError("downstream read timed out")
        self.assertEqual(classify_runtime_error(error)["code"], "DOWNSTREAM_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
