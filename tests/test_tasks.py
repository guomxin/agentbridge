from tests.authorization_fixtures import authorized_service
import sqlite3
import threading
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.interactions import InteractionStore
from agentbridge.core.operations import OperationStore
from agentbridge.core.tasks import TaskHubStore, TaskIntegrityError, TaskNotFound


class TaskHubStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TaskHubStore(Path(self.temp.name) / "agentbridge.db")

    def test_ensures_endpoint_and_task_idempotently_with_origin_subscription(self):
        endpoint, endpoint_reused = self._endpoint()
        task, task_reused = self._task(endpoint["endpoint_id"])
        endpoint_again, endpoint_reused_again = self._endpoint()
        task_again, task_reused_again = self._task(endpoint_again["endpoint_id"])

        self.assertFalse(endpoint_reused)
        self.assertFalse(task_reused)
        self.assertTrue(endpoint_reused_again)
        self.assertTrue(task_reused_again)
        self.assertEqual(endpoint_again["endpoint_id"], endpoint["endpoint_id"])
        self.assertEqual(task_again["task_id"], task["task_id"])
        self.assertEqual(
            [event["event_type"] for event in self.store.list_events(
                task_id=task["task_id"],
                user_subject="user-a",
            )],
            ["task.created"],
        )
        outbox = self.store.list_outbox(
            user_subject="user-a",
            endpoint_id=endpoint["endpoint_id"],
        )
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["payload"]["eventType"], "task.created")
        self.assertNotIn("token_id", outbox[0]["payload"])

    def test_links_operations_and_interactions_without_mutating_source_contracts(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        task = self.store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation={
                "operation_id": "operation-a",
                "user_subject": "user-a",
                "capability_name": "oa.leave.prepare",
                "capability_effect": "controlled_write",
                "status": "requires_user_action",
                "error": {"code": "FIELDS_REQUIRED"},
            },
        )
        task = self.store.link_interaction(
            task_id=task["task_id"],
            user_subject="user-a",
            interaction_record={
                "interaction_id": "interaction-a",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "interaction-a",
                "type": "business_input",
                "state": "pending",
            },
        )

        self.assertEqual(task["status"], "waiting_user")
        self.assertEqual(task["current_operation_id"], "operation-a")
        self.assertEqual(task["current_interaction_id"], "interaction-a")
        self.assertEqual(
            self.store.task_id_for_operation(
                "operation-a",
                user_subject="user-a",
            ),
            task["task_id"],
        )
        self.assertEqual(
            self.store.task_id_for_interaction(
                "interaction-a",
                user_subject="user-a",
            ),
            task["task_id"],
        )
        candidates = self.store.recovery_candidates(
            user_subject="user-a",
            endpoint_id=endpoint["endpoint_id"],
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["interaction_id"], "interaction-a")
        events = self.store.list_events(
            task_id=task["task_id"],
            user_subject="user-a",
        )
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "task.created",
                "task.operation.linked",
                "task.operation.requires_user_action",
                "task.interaction.waiting",
            ],
        )
        self.assertEqual(events[1]["payload"]["capabilityEffect"], "controlled_write")
        self.assertEqual(events[2]["payload"]["capabilityEffect"], "controlled_write")
        waiting = [
            event
            for event in events
            if event["event_type"] == "task.interaction.waiting"
        ]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["payload"]["interactionId"], "interaction-a")
        listed = self.store.list_tasks(
            user_subject="user-a",
            endpoint_id=endpoint["endpoint_id"],
            active_only=True,
        )
        self.assertEqual([item["task_id"] for item in listed], [task["task_id"]])

    def test_central_service_adds_registry_effect_to_operation_events(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        task_response = service.ensure_host_task(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            host_task_key="session|read",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            title="Read OA pending workflows",
            route={"channel": "telegram", "to": "1001"},
        )
        spec = service.registry.get("oa.workflow.pending.list")
        operation, _ = service.operations.create(
            user_subject="user-a",
            capability_name=spec.name,
            capability_version=spec.version,
            input_summary={"limit": 20},
        )
        operation = service.operations.mark_succeeded(
            operation["operation_id"],
            {"count": 0},
        )

        service.observe_host_task(
            user_subject="user-a",
            task_id=task_response["task"]["taskId"],
            operation_ids=[operation["operation_id"]],
        )

        events = service.tasks.list_events(
            task_id=task_response["task"]["taskId"],
            user_subject="user-a",
        )
        operation_events = [
            event
            for event in events
            if event["event_type"].startswith("task.operation.")
        ]
        self.assertEqual(len(operation_events), 2)
        self.assertTrue(
            all(
                event["payload"]["capabilityEffect"] == "read"
                for event in operation_events
            )
        )

    def test_lists_every_interaction_linked_to_one_task_in_order(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        for interaction_id, interaction_type in (
            ("interaction-a", "business_input"),
            ("interaction-b", "execution_authorization"),
        ):
            self.store.link_interaction(
                task_id=task["task_id"],
                user_subject="user-a",
                interaction_record={
                    "interaction_id": interaction_id,
                    "user_subject": "user-a",
                },
                interaction={
                    "interactionId": interaction_id,
                    "type": interaction_type,
                    "state": "pending",
                },
            )

        linked = self.store.list_task_interactions(
            task_id=task["task_id"],
            user_subject="user-a",
        )

        self.assertEqual(
            [item["interaction_id"] for item in linked],
            ["interaction-a", "interaction-b"],
        )
        self.assertEqual([item["last_state"] for item in linked], ["pending"] * 2)
        with self.assertRaises(TaskNotFound):
            self.store.list_task_interactions(
                task_id=task["task_id"],
                user_subject="user-b",
            )

    def test_task_artifact_is_user_bound_idempotent_and_queued_for_companion(self):
        origin, _ = self._endpoint()
        companion, _ = self.store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="openclaw-weixin:*:2002",
            client_type="openclaw-weixin",
            external_subject="2002",
            conversation_ref="agent:main:openclaw-weixin:direct:2002",
            capabilities=["direct_status", "trusted_interaction"],
            route={"channel": "openclaw-weixin", "to": "2002"},
        )
        task, _ = self._task(origin["endpoint_id"])
        artifact_input = {
            "artifact_type": "certificate_scan",
            "source_ref": "download-a",
            "filename": "certificate.pdf",
            "content_type": "application/pdf",
            "byte_size": 4096,
            "download_url": "https://10.10.50.213:8780/download/download-a/file",
            "expires_at": "2099-07-30T00:30:00+00:00",
        }

        artifact, reused = self.store.link_artifact(
            task_id=task["task_id"],
            user_subject="user-a",
            artifact=artifact_input,
        )
        artifact_again, reused_again = self.store.link_artifact(
            task_id=task["task_id"],
            user_subject="user-a",
            artifact=artifact_input,
        )

        self.assertFalse(reused)
        self.assertTrue(reused_again)
        self.assertEqual(artifact_again["artifact_id"], artifact["artifact_id"])
        self.assertEqual(
            self.store.list_artifacts(
                task_id=task["task_id"],
                user_subject="user-a",
            )[0]["filename"],
            "certificate.pdf",
        )
        self.assertEqual(self.store.list_user_artifacts(user_subject="user-b"), [])
        with self.assertRaises(TaskNotFound):
            self.store.list_artifacts(
                task_id=task["task_id"],
                user_subject="user-b",
            )
        companion_outbox = self.store.list_outbox(
            user_subject="user-a",
            endpoint_id=companion["endpoint_id"],
        )
        artifact_events = [
            item for item in companion_outbox
            if item["payload"].get("eventType") == "task.artifact.ready"
        ]
        self.assertEqual(len(artifact_events), 1)
        diagnostics = self.store.runtime_diagnostics()
        self.assertEqual(diagnostics["summary"]["ready_artifacts"], 1)
        self.assertTrue(diagnostics["isolation"]["passed"])
        self.assertNotIn(artifact_input["download_url"], str(diagnostics))

    def test_artifact_delivery_report_is_aggregated_idempotent_and_user_bound(self):
        origin, _ = self._endpoint()
        task, _ = self._task(origin["endpoint_id"])
        artifacts = []
        for suffix in ("a", "b"):
            artifact, _ = self.store.link_artifact(
                task_id=task["task_id"],
                user_subject="user-a",
                artifact={
                    "artifact_type": "certificate_scan",
                    "source_ref": f"download-{suffix}",
                    "filename": f"certificate-{suffix}.pdf",
                    "content_type": "application/pdf",
                    "byte_size": 4096,
                    "download_url": (
                        "https://10.10.50.213:8780/download/"
                        f"download-{suffix}/file"
                    ),
                    "expires_at": "2099-07-30T00:30:00+00:00",
                },
            )
            artifacts.append(artifact)

        reported_task, event, reused = self.store.record_artifact_delivery(
            task_id=task["task_id"],
            user_subject="user-a",
            agent_host="openclaw",
            delivery_ref="tool-result:certificate-batch",
            channel="telegram",
            files=[
                {
                    "artifact_id": artifacts[0]["artifact_id"],
                    "state": "attachment_sent",
                    "attempt_count": 1,
                },
                {
                    "artifact_id": artifacts[1]["artifact_id"],
                    "state": "fallback_link_sent",
                    "attempt_count": 2,
                    "error_code": "ETIMEDOUT",
                },
            ],
        )
        report = reported_task["summary"]["artifactDelivery"]
        self.assertFalse(reused)
        self.assertEqual(event["event_type"], "task.artifact.delivery")
        self.assertEqual(report["preparedCount"], 2)
        self.assertEqual(report["attachmentSentCount"], 1)
        self.assertEqual(report["fallbackLinkSentCount"], 1)
        self.assertEqual(report["failedCount"], 0)
        self.assertEqual(
            report["userMessage"],
            "2 份文件已准备，1 份已作为附件发送，1 份已改发下载链接。",
        )

        repeated_task, repeated_event, repeated = (
            self.store.record_artifact_delivery(
                task_id=task["task_id"],
                user_subject="user-a",
                agent_host="openclaw",
                delivery_ref="tool-result:certificate-batch",
                channel="telegram",
                files=[
                    {
                        "artifact_id": artifacts[0]["artifact_id"],
                        "state": "attachment_sent",
                        "attempt_count": 1,
                    }
                ],
            )
        )
        self.assertTrue(repeated)
        self.assertEqual(repeated_event["event_id"], event["event_id"])
        self.assertEqual(
            repeated_task["summary"]["artifactDelivery"],
            report,
        )
        delivery_events = [
            item
            for item in self.store.list_events(
                task_id=task["task_id"],
                user_subject="user-a",
            )
            if item["event_type"] == "task.artifact.delivery"
        ]
        self.assertEqual(len(delivery_events), 1)
        with self.assertRaises(TaskNotFound):
            self.store.record_artifact_delivery(
                task_id=task["task_id"],
                user_subject="user-b",
                agent_host="openclaw",
                delivery_ref="tool-result:cross-user",
                channel="telegram",
                files=[
                    {
                        "artifact_id": artifacts[0]["artifact_id"],
                        "state": "attachment_sent",
                        "attempt_count": 1,
                    }
                ],
            )

    def test_artifact_delivery_summary_keeps_cross_endpoint_results(self):
        origin, _ = self._endpoint()
        task, _ = self._task(origin["endpoint_id"])
        artifact, _ = self.store.link_artifact(
            task_id=task["task_id"],
            user_subject="user-a",
            artifact={
                "artifact_type": "certificate_scan",
                "source_ref": "download-cross-end",
                "filename": "certificate.pdf",
                "content_type": "application/pdf",
                "byte_size": 4096,
                "download_url": "https://10.10.50.213:8780/download/cross-end/file",
                "expires_at": "2099-07-30T00:30:00+00:00",
            },
        )
        self.store.record_artifact_delivery(
            task_id=task["task_id"],
            user_subject="user-a",
            agent_host="openclaw",
            delivery_ref="tool-result:workspace",
            channel="webchat",
            files=[
                {
                    "artifact_id": artifact["artifact_id"],
                    "state": "fallback_link_sent",
                    "attempt_count": 0,
                }
            ],
        )
        reported_task, _, _ = self.store.record_artifact_delivery(
            task_id=task["task_id"],
            user_subject="user-a",
            agent_host="openclaw",
            delivery_ref="tool-result:telegram",
            channel="telegram",
            files=[
                {
                    "artifact_id": artifact["artifact_id"],
                    "state": "attachment_sent",
                    "attempt_count": 1,
                }
            ],
        )

        aggregate = reported_task["summary"]["artifactDeliveryAggregate"]
        self.assertEqual(aggregate["preparedCount"], 1)
        self.assertEqual(aggregate["channelCount"], 2)
        self.assertIn("网页端：1 个下载入口可用", aggregate["userMessage"])
        self.assertIn("Telegram：1 份附件已送达", aggregate["userMessage"])

    def test_expired_artifact_is_refreshed_in_place_and_remains_user_bound(self):
        origin, _ = self._endpoint()
        task, _ = self._task(origin["endpoint_id"])
        artifact, _ = self.store.link_artifact(
            task_id=task["task_id"],
            user_subject="user-a",
            artifact={
                "artifact_type": "certificate_scan",
                "source_ref": "download-old",
                "filename": "certificate.pdf",
                "content_type": "application/pdf",
                "byte_size": 4096,
                "download_url": (
                    "https://10.10.50.213:8780/download/download-old/file"
                ),
                "expires_at": "2099-07-30T00:30:00+00:00",
            },
        )
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE task_artifacts SET state = 'expired' WHERE artifact_id = ?",
                (artifact["artifact_id"],),
            )

        refreshed = self.store.refresh_artifact(
            task_id=task["task_id"],
            artifact_id=artifact["artifact_id"],
            user_subject="user-a",
            expected_source_ref="download-old",
            artifact={
                "source_ref": "download-new",
                "filename": "certificate.pdf",
                "content_type": "application/pdf",
                "byte_size": 8192,
                "download_url": (
                    "https://10.10.50.213:8780/download/download-new/file"
                ),
                "expires_at": "2099-07-30T01:30:00+00:00",
            },
        )

        self.assertEqual(refreshed["artifact_id"], artifact["artifact_id"])
        self.assertEqual(refreshed["source_ref"], "download-new")
        self.assertEqual(refreshed["state"], "ready")
        self.assertEqual(
            len(
                self.store.list_artifacts(
                    task_id=task["task_id"],
                    user_subject="user-a",
                )
            ),
            1,
        )
        with self.assertRaises(TaskNotFound):
            self.store.get_artifact(
                task_id=task["task_id"],
                artifact_id=artifact["artifact_id"],
                user_subject="user-b",
                include_source_ref=True,
            )
        events = self.store.list_events(
            task_id=task["task_id"],
            user_subject="user-a",
        )
        self.assertEqual(events[-1]["event_type"], "task.artifact.refreshed")
        self.assertNotIn("download-old", str(events[-1]["payload"]))

    def test_completes_artifact_task_once_and_notifies_companion(self):
        origin, _ = self._endpoint()
        companion, _ = self.store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="openclaw-weixin:*:2002",
            client_type="openclaw-weixin",
            external_subject="2002",
            conversation_ref="agent:main:openclaw-weixin:direct:2002",
            capabilities=["direct_status"],
        )
        task, _ = self._task(origin["endpoint_id"])

        completed = self.store.complete_task(
            task_id=task["task_id"],
            user_subject="user-a",
            reason="artifact_ready",
            causation_ref="download-a",
        )
        completed_again = self.store.complete_task(
            task_id=task["task_id"],
            user_subject="user-a",
            reason="artifact_ready",
            causation_ref="download-a",
        )

        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed_again["version"], completed["version"])
        self.assertIsNotNone(completed["finished_at"])
        events = self.store.list_events(
            task_id=task["task_id"],
            user_subject="user-a",
        )
        completion_events = [
            event for event in events
            if event["event_type"] == "task.completed"
        ]
        self.assertEqual(len(completion_events), 1)
        self.assertEqual(completion_events[0]["payload"]["reason"], "artifact_ready")
        companion_outbox = self.store.list_outbox(
            user_subject="user-a",
            endpoint_id=companion["endpoint_id"],
        )
        self.assertEqual(
            len([
                item for item in companion_outbox
                if item["payload"].get("eventType") == "task.completed"
            ]),
            1,
        )

    def test_fails_unlinked_host_task_once_and_notifies_companion(self):
        origin, _ = self._endpoint()
        companion, _ = self.store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:2002",
            client_type="telegram",
            external_subject="2002",
            conversation_ref="agent:main:telegram:direct:2002",
            capabilities=["direct_status"],
        )
        task, _ = self._task(origin["endpoint_id"])

        failed = self.store.fail_task(
            task_id=task["task_id"],
            user_subject="user-a",
            error_code="MCP_TOOL_EXECUTION_FAILED",
            message="Capability input validation failed.",
            causation_ref="request-a",
        )
        failed_again = self.store.fail_task(
            task_id=task["task_id"],
            user_subject="user-a",
            error_code="MCP_TOOL_EXECUTION_FAILED",
            message="Capability input validation failed.",
            causation_ref="request-a",
        )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed_again["version"], failed["version"])
        self.assertEqual(
            failed["summary"]["failure"]["code"],
            "MCP_TOOL_EXECUTION_FAILED",
        )
        events = self.store.list_events(
            task_id=task["task_id"],
            user_subject="user-a",
        )
        self.assertEqual(
            [event["event_type"] for event in events].count("task.failed"),
            1,
        )
        companion_outbox = self.store.list_outbox(
            user_subject="user-a",
            endpoint_id=companion["endpoint_id"],
        )
        self.assertEqual(
            len(
                [
                    item
                    for item in companion_outbox
                    if item["payload"].get("eventType") == "task.failed"
                ]
            ),
            1,
        )

    def test_expires_only_stale_unreferenced_task_shells(self):
        endpoint, _ = self._endpoint()
        orphan, _ = self._task(endpoint["endpoint_id"])
        linked, _ = self.store.ensure_task(
            user_subject="user-a",
            agent_host="openclaw",
            host_task_key="session|linked",
            origin_endpoint_id=endpoint["endpoint_id"],
            active_conversation_ref=endpoint["conversation_ref"],
            title="Linked task",
        )
        self.store.link_operation(
            task_id=linked["task_id"],
            user_subject="user-a",
            operation={
                "operation_id": "operation-running",
                "user_subject": "user-a",
                "capability_name": "oa.workflow.pending.list",
                "capability_effect": "read",
                "status": "running",
            },
        )
        with self.store._connect() as connection:
            connection.execute(
                """
                UPDATE agent_tasks
                SET created_at = '2000-01-01T00:00:00+00:00',
                    updated_at = '2000-01-01T00:00:00+00:00'
                WHERE task_id IN (?, ?)
                """,
                (orphan["task_id"], linked["task_id"]),
            )

        reopened = TaskHubStore(self.store.db_path)

        self.assertEqual(
            reopened.get_task(orphan["task_id"], user_subject="user-a")["status"],
            "expired",
        )
        self.assertEqual(
            reopened.get_task(linked["task_id"], user_subject="user-a")["status"],
            "running",
        )
        self.assertEqual(
            [
                event["event_type"]
                for event in reopened.list_events(
                    task_id=orphan["task_id"],
                    user_subject="user-a",
                )
            ],
            ["task.created"],
        )

    def test_expires_stale_waiting_task_without_actionable_interaction(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        operation_store = OperationStore(self.store.db_path)
        operation, _ = operation_store.create(
            user_subject="user-a",
            capability_name="smartlight.system.overview",
            capability_version="1.0.0",
            input_summary={},
        )
        operation = operation_store.mark_requires_user_action(
            operation["operation_id"],
            code="LOGIN_REQUIRED",
            message="Login required.",
            next_action={"type": "session_login", "system": "smartlight"},
        )
        waiting = self.store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation=operation,
        )
        with self.store._connect() as connection:
            connection.execute(
                """
                UPDATE agent_tasks
                SET created_at = '2000-01-01T00:00:00+00:00',
                    updated_at = '2000-01-01T00:00:00+00:00'
                WHERE task_id = ?
                """,
                (task["task_id"],),
            )

        self.assertEqual(waiting["status"], "waiting_user")
        reopened = TaskHubStore(self.store.db_path)
        expired = reopened.get_task(
            task["task_id"],
            user_subject="user-a",
        )
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(expired["current_operation_id"], operation["operation_id"])

    def test_keeps_stale_waiting_task_with_actionable_interaction(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        self.store.link_interaction(
            task_id=task["task_id"],
            user_subject="user-a",
            interaction_record={
                "interaction_id": "interaction-pending",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "interaction-pending",
                "type": "execution_authorization",
                "state": "pending",
            },
        )
        with self.store._connect() as connection:
            connection.execute(
                """
                UPDATE agent_tasks
                SET created_at = '2000-01-01T00:00:00+00:00',
                    updated_at = '2000-01-01T00:00:00+00:00'
                WHERE task_id = ?
                """,
                (task["task_id"],),
            )

        reopened = TaskHubStore(self.store.db_path)
        waiting = reopened.get_task(
            task["task_id"],
            user_subject="user-a",
        )
        self.assertEqual(waiting["status"], "waiting_user")
        self.assertEqual(
            waiting["current_interaction_id"],
            "interaction-pending",
        )

    def test_central_service_finishes_unreferenced_host_tasks(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        succeeded = service.ensure_host_task(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            host_task_key="session|success",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            title="Successful no-reference tool",
        )
        failed = service.ensure_host_task(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            host_task_key="session|failure",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            title="Failed no-reference tool",
        )

        succeeded_result = service.finish_host_task(
            user_subject="user-a",
            task_id=succeeded["task"]["taskId"],
            outcome="succeeded",
        )
        failed_result = service.finish_host_task(
            user_subject="user-a",
            task_id=failed["task"]["taskId"],
            outcome="failed",
            error_code="SESSION_CHECK_UNAVAILABLE",
            message="Session check is temporarily unavailable.",
        )

        self.assertEqual(succeeded_result["task"]["status"], "succeeded")
        self.assertEqual(failed_result["task"]["status"], "failed")
        self.assertEqual(
            failed_result["task"]["summary"]["failure"]["code"],
            "SESSION_CHECK_UNAVAILABLE",
        )

    def test_cross_endpoint_continuation_choices_persist_and_select_owned_task(self):
        workspace, _ = self.store.ensure_endpoint(
            user_subject="user-a",
            token_id="workspace-account:account-a",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            label="Agent Workspace",
        )
        telegram, _ = self._endpoint()
        first, _ = self.store.ensure_task(
            user_subject="user-a",
            agent_host="openclaw",
            host_task_key="workspace|run-1",
            origin_endpoint_id=workspace["endpoint_id"],
            active_conversation_ref=workspace["conversation_ref"],
            title="Read OA pending items",
        )
        second, _ = self.store.ensure_task(
            user_subject="user-a",
            agent_host="openclaw",
            host_task_key="workspace|run-2",
            origin_endpoint_id=workspace["endpoint_id"],
            active_conversation_ref=workspace["conversation_ref"],
            title="Read OA sent items",
        )

        candidates = self.store.continuation_candidates(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_id=telegram["endpoint_id"],
            cross_endpoint_only=True,
            source_client_type="web",
        )
        candidate_ids = [item["task"]["task_id"] for item in candidates]
        self.assertEqual(set(candidate_ids), {first["task_id"], second["task_id"]})
        continuation, reused = self.store.set_continuation_candidates(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_id=telegram["endpoint_id"],
            candidate_task_ids=candidate_ids,
            reason="multiple_candidates",
        )
        self.assertFalse(reused)
        self.assertEqual(continuation["state"], "awaiting_selection")

        listed_continuations = self.store.list_continuations(
            user_subject="user-a",
        )
        self.assertEqual(
            [item["endpoint_id"] for item in listed_continuations],
            [telegram["endpoint_id"]],
        )
        self.assertEqual(
            self.store.list_continuations(user_subject="user-b"),
            [],
        )

        reopened = TaskHubStore(self.store.db_path)
        persisted = reopened.get_continuation(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_id=telegram["endpoint_id"],
        )
        self.assertEqual(persisted["candidate_task_ids"], candidate_ids)
        selected, task, endpoint, selected_reused = (
            reopened.select_continuation_candidate(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_id=telegram["endpoint_id"],
                ordinal=1,
                execution_mode="resume",
                reason="candidate_ordinal",
            )
        )
        self.assertFalse(selected_reused)
        self.assertEqual(selected["selected_task_id"], candidate_ids[0])
        self.assertTrue(selected["allow_new_operation"])
        self.assertEqual(task["active_conversation_ref"], telegram["conversation_ref"])
        self.assertEqual(endpoint["endpoint_id"], telegram["endpoint_id"])
        self.assertIn(
            "task.continuation.selected",
            [
                event["event_type"]
                for event in reopened.list_events(
                    task_id=task["task_id"],
                    user_subject="user-a",
                )
            ],
        )

        with self.assertRaises(TaskNotFound):
            reopened.get_continuation(
                user_subject="user-b",
                agent_host="openclaw",
                endpoint_id=telegram["endpoint_id"],
            )

    def test_expired_continuations_are_not_reported_as_active(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        self.store.select_continuation(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_id=endpoint["endpoint_id"],
            task_id=task["task_id"],
            execution_mode="follow_up",
        )
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            connection.execute(
                "UPDATE task_continuations SET expires_at = ? WHERE endpoint_id = ?",
                ("2000-01-01T00:00:00+00:00", endpoint["endpoint_id"]),
            )
            connection.commit()

        diagnostics = self.store.runtime_diagnostics()
        user = next(
            item for item in diagnostics["users"]
            if item["user_subject"] == "user-a"
        )
        self.assertEqual(diagnostics["summary"]["active_task_continuations"], 0)
        self.assertEqual(
            user["task_continuations"],
            [{"state": "expired", "execution_mode": "follow_up", "count": 1}],
        )

        listed = self.store.list_continuations(user_subject="user-a")
        self.assertEqual(listed[0]["state"], "expired")
        self.assertIsNone(listed[0]["selected_task_id"])
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            stored_state = connection.execute(
                "SELECT state FROM task_continuations WHERE endpoint_id = ?",
                (endpoint["endpoint_id"],),
            ).fetchone()[0]
        self.assertEqual(stored_state, "expired")

    def test_latest_user_event_id_tracks_only_the_owned_event_stream(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        created_event = self.store.list_events(
            task_id=task["task_id"],
            user_subject="user-a",
        )[-1]

        self.assertEqual(
            self.store.latest_user_event_id(user_subject="user-a"),
            created_event["event_id"],
        )
        self.assertIsNone(
            self.store.latest_user_event_id(user_subject="user-b"),
        )
        self.assertTrue(
            self.store.current_user_event_cursor(
                user_subject="user-b",
            ).startswith("time:"),
        )

        self.store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation={
                "operation_id": "operation-latest",
                "user_subject": "user-a",
                "capability_name": "oa.leave.prepare",
                "status": "running",
            },
        )
        latest_event = self.store.list_events(
            task_id=task["task_id"],
            user_subject="user-a",
        )[-1]
        self.assertNotEqual(latest_event["event_id"], created_event["event_id"])
        self.assertEqual(
            self.store.latest_user_event_id(user_subject="user-a"),
            latest_event["event_id"],
        )

    def test_waiting_interaction_is_broadcast_to_all_trusted_user_endpoints(self):
        origin, _ = self._endpoint()
        secondary, _ = self.store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="wechat:direct:user-a",
            client_type="wechat",
            external_subject="wechat-user-a",
            conversation_ref="agent:main:wechat:direct:user-a",
            capabilities=["trusted_interaction", "direct_status"],
            route={"channel": "wechat", "to": "wechat-user-a"},
        )
        task, _ = self._task(origin["endpoint_id"])

        self.store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation={
                "operation_id": "operation-broadcast",
                "user_subject": "user-a",
                "capability_name": "oa.leave.prepare",
                "status": "requires_user_action",
                "error": {"code": "FIELDS_REQUIRED"},
            },
        )
        self.store.link_interaction(
            task_id=task["task_id"],
            user_subject="user-a",
            interaction_record={
                "interaction_id": "interaction-broadcast",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "interaction-broadcast",
                "type": "execution_authorization",
                "state": "pending",
            },
        )

        secondary_outbox = self.store.list_outbox(
            user_subject="user-a",
            endpoint_id=secondary["endpoint_id"],
        )
        event_types = [
            item["payload"]["eventType"] for item in secondary_outbox
        ]
        self.assertEqual(
            event_types,
            [
                "task.created",
                "task.operation.linked",
                "task.interaction.waiting",
            ],
        )
        self.assertEqual(
            secondary_outbox[2]["payload"]["payload"]["interactionId"],
            "interaction-broadcast",
        )
        self.assertNotIn(
            "task.operation.requires_user_action",
            event_types,
        )

    def test_business_input_is_broadcast_to_all_trusted_user_endpoints(self):
        origin, _ = self._endpoint()
        secondary, _ = self.store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:2002",
            client_type="telegram",
            external_subject="2002",
            conversation_ref="agent:main:telegram:direct:2002",
            capabilities=["trusted_interaction", "direct_status"],
            route={"channel": "telegram", "to": "2002"},
        )
        task, _ = self._task(origin["endpoint_id"])

        self.store.link_interaction(
            task_id=task["task_id"],
            user_subject="user-a",
            interaction_record={
                "interaction_id": "input-broadcast",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "input-broadcast",
                "type": "business_input",
                "state": "pending",
            },
        )

        events = [
            item["payload"]["eventType"]
            for item in self.store.list_outbox(
                user_subject="user-a",
                endpoint_id=secondary["endpoint_id"],
            )
        ]
        self.assertEqual(
            events,
            ["task.created", "task.interaction.waiting"],
        )

    def test_endpoint_capabilities_are_merged_when_route_is_reobserved(self):
        endpoint, _ = self.store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            capabilities=["direct_status"],
        )
        updated, reused = self.store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            capabilities=["trusted_interaction"],
        )

        self.assertTrue(reused)
        self.assertEqual(updated["endpoint_id"], endpoint["endpoint_id"])
        self.assertEqual(
            set(updated["capabilities"]),
            {"direct_status", "trusted_interaction"},
        )

    def test_outbox_claim_and_ack_are_endpoint_and_user_bound(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])

        claimed = self.store.claim_outbox(
            user_subject="user-a",
            endpoint_id=endpoint["endpoint_id"],
            limit=1,
        )
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["state"], "delivering")
        self.assertEqual(claimed[0]["attempt_count"], 1)
        self.assertEqual(
            self.store.claim_outbox(
                user_subject="user-a",
                endpoint_id=endpoint["endpoint_id"],
                limit=1,
            ),
            [],
        )
        acknowledged = self.store.acknowledge_outbox(
            user_subject="user-a",
            endpoint_id=endpoint["endpoint_id"],
            delivery_id=claimed[0]["delivery_id"],
            succeeded=True,
        )
        self.assertEqual(acknowledged["state"], "acknowledged")
        with self.assertRaises(TaskNotFound):
            self.store.acknowledge_outbox(
                user_subject="user-b",
                endpoint_id=endpoint["endpoint_id"],
                delivery_id=claimed[0]["delivery_id"],
                succeeded=True,
            )
        self.assertEqual(task["status"], "active")

    def test_deferred_outbox_waits_for_endpoint_activity_then_reactivates(self):
        endpoint, _ = self._endpoint()
        self._task(endpoint["endpoint_id"])
        claimed = self.store.claim_outbox(
            user_subject="user-a",
            endpoint_id=endpoint["endpoint_id"],
            limit=1,
        )

        deferred = self.store.acknowledge_outbox(
            user_subject="user-a",
            endpoint_id=endpoint["endpoint_id"],
            delivery_id=claimed[0]["delivery_id"],
            succeeded=False,
            defer_until_activity=True,
        )

        self.assertEqual(deferred["state"], "deferred")
        self.assertEqual(deferred["attempt_count"], 1)
        self.assertEqual(
            self.store.claim_outbox(
                user_subject="user-a",
                endpoint_id=endpoint["endpoint_id"],
            ),
            [],
        )
        self.assertEqual(
            self.store.reactivate_deferred_outbox(
                user_subject="user-a",
                endpoint_id=endpoint["endpoint_id"],
                delay_seconds=0,
            ),
            1,
        )
        reclaimed = self.store.claim_outbox(
            user_subject="user-a",
            endpoint_id=endpoint["endpoint_id"],
            limit=1,
        )
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(reclaimed[0]["state"], "delivering")
        self.assertEqual(reclaimed[0]["attempt_count"], 1)

    def test_pending_outbox_respects_retry_and_activity_delays(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                self.store = TaskHubStore(Path(self.temp.name) / f"delay-{deferred}.db")
                endpoint, _ = self._endpoint()
                self._task(endpoint["endpoint_id"])
                args = dict(user_subject="user-a", endpoint_id=endpoint["endpoint_id"])
                with patch("agentbridge.core.tasks._utc_now", return_value="2099-01-01T00:00:00+00:00"):
                    claimed = self.store.claim_outbox(**args, limit=1)
                self.assertEqual(len(claimed), 1)
                delivery_id = claimed[0]["delivery_id"]
                with patch("agentbridge.core.tasks._utc_after", return_value="2099-01-01T00:01:00+00:00"):
                    self.store.acknowledge_outbox(
                        **args, delivery_id=delivery_id, succeeded=False,
                        retry_after_seconds=60, defer_until_activity=deferred,
                    )
                    if deferred:
                        self.store.reactivate_deferred_outbox(**args, delay_seconds=60)
                with patch("agentbridge.core.tasks._utc_now", return_value="2099-01-01T00:00:59+00:00"):
                    self.assertEqual(self.store.claim_outbox(**args), [])
                with patch("agentbridge.core.tasks._utc_now", return_value="2099-01-01T00:01:00+00:00"):
                    reclaimed = self.store.claim_outbox(**args)
                self.assertEqual([row["delivery_id"] for row in reclaimed], [delivery_id])
                self.store.acknowledge_outbox(**args, delivery_id=delivery_id, succeeded=True)

    def test_wechat_activity_suppresses_closed_task_status_but_preserves_live_work(self):
        for status, event, expected in [
            ("failed", "task.operation.failed", "archived"),
            ("active", "task.operation.failed", "pending"),
            ("succeeded", "task.artifact.ready", "pending"),
            ("succeeded", "plan.result.ready", "pending"),
        ]:
            with self.subTest(status=status, event=event):
                self.store = TaskHubStore(Path(self.temp.name) / f"{status}-{event}.db")
                endpoint, _ = self._endpoint()
                task, _ = self._task(endpoint["endpoint_id"])
                args = dict(user_subject="user-a", endpoint_id=endpoint["endpoint_id"])
                claimed = self.store.claim_outbox(**args, limit=1)[0]
                self.store.acknowledge_outbox(
                    **args, delivery_id=claimed["delivery_id"], succeeded=False,
                    defer_until_activity=True,
                )
                with sqlite3.connect(self.store.db_path) as connection:
                    connection.execute("UPDATE client_endpoints SET client_type = 'openclaw-weixin'")
                    connection.execute("UPDATE agent_tasks SET status = ? WHERE task_id = ?", (status, task["task_id"]))
                    connection.execute(
                        "UPDATE notification_outbox SET payload_json = json_set(payload_json, '$.eventType', ?) WHERE delivery_id = ?",
                        (event, claimed["delivery_id"]),
                    )
                count = self.store.reactivate_deferred_outbox(**args, delay_seconds=0)
                self.assertEqual(count, int(expected == "pending"))
                self.assertEqual(self.store.get_delivery(claimed["delivery_id"])["state"], expected)

    def test_outbox_ack_rejects_successful_activity_deferral(self):
        endpoint, _ = self._endpoint()
        self._task(endpoint["endpoint_id"])
        claimed = self.store.claim_outbox(
            user_subject="user-a",
            endpoint_id=endpoint["endpoint_id"],
            limit=1,
        )

        with self.assertRaises(ValueError):
            self.store.acknowledge_outbox(
                user_subject="user-a",
                endpoint_id=endpoint["endpoint_id"],
                delivery_id=claimed[0]["delivery_id"],
                succeeded=True,
                defer_until_activity=True,
            )

    def test_empty_outbox_claim_does_not_wait_for_writer_lock(self):
        endpoint, _ = self._endpoint()
        blocker = sqlite3.connect(self.store.db_path, timeout=1)
        completed = threading.Event()
        result = {}
        thread = None

        try:
            blocker.execute("BEGIN IMMEDIATE")

            def claim():
                try:
                    result["value"] = self.store.claim_outbox(
                        user_subject="user-a",
                        endpoint_id=endpoint["endpoint_id"],
                    )
                except Exception as exc:  # pragma: no cover - assertion reports it
                    result["error"] = exc
                finally:
                    completed.set()

            thread = threading.Thread(target=claim, daemon=True)
            thread.start()
            self.assertTrue(
                completed.wait(timeout=0.5),
                "empty outbox claim waited for an unrelated writer lock",
            )
        finally:
            blocker.rollback()
            blocker.close()

        self.assertIsNotNone(thread)
        thread.join(timeout=1)
        self.assertNotIn("error", result)
        self.assertEqual(result["value"], [])

    def test_outbox_delivery_stops_after_five_failed_attempts(self):
        endpoint, _ = self._endpoint()
        self._task(endpoint["endpoint_id"])
        delivery = None

        for _attempt in range(5):
            with self.store._connect() as connection:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET next_attempt_at = '2000-01-01T00:00:00+00:00'
                    WHERE endpoint_id = ? AND state IN ('pending', 'delivering')
                    """,
                    (endpoint["endpoint_id"],),
                )
            claimed = self.store.claim_outbox(
                user_subject="user-a",
                endpoint_id=endpoint["endpoint_id"],
                limit=1,
            )
            self.assertEqual(len(claimed), 1)
            delivery = self.store.acknowledge_outbox(
                user_subject="user-a",
                endpoint_id=endpoint["endpoint_id"],
                delivery_id=claimed[0]["delivery_id"],
                succeeded=False,
                retry_after_seconds=1,
            )

        self.assertEqual(delivery["state"], "failed")
        self.assertEqual(
            self.store.claim_outbox(
                user_subject="user-a",
                endpoint_id=endpoint["endpoint_id"],
            ),
            [],
        )

    def test_outbox_delivery_stops_after_five_expired_leases(self):
        endpoint, _ = self._endpoint()
        self._task(endpoint["endpoint_id"])
        delivery_id = None

        for attempt in range(5):
            with self.store._connect() as connection:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET next_attempt_at = '2000-01-01T00:00:00+00:00'
                    WHERE endpoint_id = ? AND state IN ('pending', 'delivering')
                    """,
                    (endpoint["endpoint_id"],),
                )
            claimed = self.store.claim_outbox(
                user_subject="user-a",
                endpoint_id=endpoint["endpoint_id"],
                limit=1,
            )
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0]["attempt_count"], attempt + 1)
            delivery_id = claimed[0]["delivery_id"]

        with self.store._connect() as connection:
            connection.execute(
                """
                UPDATE notification_outbox
                SET next_attempt_at = '2000-01-01T00:00:00+00:00'
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            )
        self.assertEqual(
            self.store.claim_outbox(
                user_subject="user-a",
                endpoint_id=endpoint["endpoint_id"],
            ),
            [],
        )
        with self.store._connect() as connection:
            row = connection.execute(
                """
                SELECT state, attempt_count FROM notification_outbox
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["attempt_count"], 5)

    def test_rejects_cross_user_endpoint_task_and_artifact_access(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])

        with self.assertRaises(TaskIntegrityError):
            self.store.ensure_endpoint(
                user_subject="user-b",
                token_id="token-b",
                agent_host="openclaw",
                endpoint_key="telegram:*:1001",
                client_type="telegram",
                external_subject="1001",
                conversation_ref="agent:main:telegram:direct:1001",
            )
        with self.assertRaises(TaskNotFound):
            self.store.get_task(task["task_id"], user_subject="user-b")
        with self.assertRaises(TaskIntegrityError):
            self.store.link_operation(
                task_id=task["task_id"],
                user_subject="user-a",
                operation={
                    "operation_id": "operation-b",
                    "user_subject": "user-b",
                    "status": "succeeded",
                },
            )

    def test_outcome_unknown_is_not_hidden_by_later_interaction_observation(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        task = self.store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation={
                "operation_id": "operation-unknown",
                "user_subject": "user-a",
                "capability_name": "oa.leave.submit",
                "status": "unknown",
                "error": {"code": "RESULT_UNKNOWN"},
            },
        )
        task = self.store.link_interaction(
            task_id=task["task_id"],
            user_subject="user-a",
            interaction_record={
                "interaction_id": "interaction-late",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "interaction-late",
                "type": "execution_authorization",
                "state": "completed",
            },
        )

        self.assertEqual(task["status"], "outcome_unknown")

    def test_successful_operation_finishes_task_and_leaves_active_view(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])

        task = self.store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation={
                "operation_id": "operation-succeeded",
                "user_subject": "user-a",
                "capability_name": "oa.workflow.pending.list",
                "status": "succeeded",
                "error": None,
            },
        )

        self.assertEqual(task["status"], "succeeded")
        self.assertIsNotNone(task["finished_at"])
        self.assertEqual(
            self.store.list_tasks(
                user_subject="user-a",
                active_only=True,
            ),
            [],
        )

    def test_reused_task_keeps_its_original_title(self):
        endpoint, _ = self._endpoint()
        first, reused = self.store.ensure_task(
            user_subject="user-a",
            agent_host="openclaw",
            host_task_key="shared-smartlight-turn",
            origin_endpoint_id=endpoint["endpoint_id"],
            active_conversation_ref="agent:main:telegram:direct:1001",
            title="照明系统概览",
        )
        second, reused_again = self.store.ensure_task(
            user_subject="user-a",
            agent_host="openclaw",
            host_task_key="shared-smartlight-turn",
            origin_endpoint_id=endpoint["endpoint_id"],
            active_conversation_ref="agent:main:telegram:direct:1001",
            title="查询照明漏电记录",
        )

        self.assertFalse(reused)
        self.assertTrue(reused_again)
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(second["title"], "照明系统概览")

    def test_completed_standalone_credential_interaction_finishes_task_once(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        pending = self.store.link_interaction(
            task_id=task["task_id"],
            user_subject="user-a",
            interaction_record={
                "interaction_id": "credential-standalone",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "credential-standalone",
                "type": "credential",
                "state": "pending",
            },
        )
        completed = self.store.link_interaction(
            task_id=task["task_id"],
            user_subject="user-a",
            interaction_record={
                "interaction_id": "credential-standalone",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "credential-standalone",
                "type": "credential",
                "state": "completed",
            },
        )
        completed_again = self.store.link_interaction(
            task_id=task["task_id"],
            user_subject="user-a",
            interaction_record={
                "interaction_id": "credential-standalone",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "credential-standalone",
                "type": "credential",
                "state": "completed",
            },
        )

        self.assertEqual(pending["status"], "waiting_user")
        self.assertEqual(completed["status"], "succeeded")
        self.assertIsNotNone(completed["finished_at"])
        self.assertEqual(completed_again["version"], completed["version"])

    def test_completed_credential_with_business_operation_stays_resumable(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        task = self.store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation={
                "operation_id": "operation-needs-login",
                "user_subject": "user-a",
                "capability_name": "smartlight.system.overview",
                "status": "requires_user_action",
                "error": {"code": "LOGIN_REQUIRED"},
            },
        )
        for state in ("pending", "completed"):
            task = self.store.link_interaction(
                task_id=task["task_id"],
                user_subject="user-a",
                interaction_record={
                    "interaction_id": "credential-for-operation",
                    "user_subject": "user-a",
                },
                interaction={
                    "interactionId": "credential-for-operation",
                    "type": "credential",
                    "state": state,
                },
            )

        self.assertEqual(task["status"], "active")
        self.assertIsNone(task["finished_at"])

    def test_superseded_interaction_finishes_task_and_new_operation_reopens_it(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        task = self.store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation={
                "operation_id": "operation-original",
                "user_subject": "user-a",
                "capability_name": "oa.missed_punch.approval.prepare",
                "status": "requires_user_action",
                "error": {"code": "FIELD_INPUT_REQUIRED"},
            },
        )
        for state in ("pending", "superseded"):
            task = self.store.link_interaction(
                task_id=task["task_id"],
                user_subject="user-a",
                interaction_record={
                    "interaction_id": "interaction-original",
                    "user_subject": "user-a",
                },
                interaction={
                    "interactionId": "interaction-original",
                    "type": "business_input",
                    "state": state,
                },
            )

        self.assertEqual(task["status"], "superseded")
        self.assertIsNotNone(task["finished_at"])
        self.assertEqual(
            self.store.list_tasks(user_subject="user-a", active_only=True),
            [],
        )
        self.assertEqual(
            self.store.recovery_candidates(
                user_subject="user-a",
                endpoint_id=endpoint["endpoint_id"],
            ),
            [],
        )

        reopened = self.store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation={
                "operation_id": "operation-replacement",
                "user_subject": "user-a",
                "capability_name": "oa.missed_punch.approval.prepare",
                "status": "requires_user_action",
                "error": {"code": "FIELD_INPUT_REQUIRED"},
            },
        )

        self.assertEqual(reopened["status"], "waiting_user")
        self.assertIsNone(reopened["finished_at"])

    def test_stale_interaction_observation_cannot_reopen_successful_task(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        task_id = task["task_id"]

        for state in ("pending", "processing", "completed"):
            self.store.link_interaction(
                task_id=task_id,
                user_subject="user-a",
                interaction_record={
                    "interaction_id": "fields-a",
                    "user_subject": "user-a",
                },
                interaction={
                    "interactionId": "fields-a",
                    "type": "business_input",
                    "state": state,
                },
            )
        self.store.link_interaction(
            task_id=task_id,
            user_subject="user-a",
            interaction_record={
                "interaction_id": "authorization-a",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "authorization-a",
                "type": "execution_authorization",
                "state": "pending",
            },
        )
        self.store.link_interaction(
            task_id=task_id,
            user_subject="user-a",
            interaction_record={
                "interaction_id": "authorization-a",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "authorization-a",
                "type": "execution_authorization",
                "state": "completed",
            },
        )
        succeeded = self.store.link_operation(
            task_id=task_id,
            user_subject="user-a",
            operation={
                "operation_id": "submit-a",
                "user_subject": "user-a",
                "capability_name": "oa.business_trip.submit",
                "status": "succeeded",
                "error": None,
            },
        )

        stale = self.store.link_interaction(
            task_id=task_id,
            user_subject="user-a",
            interaction_record={
                "interaction_id": "fields-a",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "fields-a",
                "type": "business_input",
                "state": "completed",
            },
        )
        duplicate_authorization = self.store.link_interaction(
            task_id=task_id,
            user_subject="user-a",
            interaction_record={
                "interaction_id": "authorization-a",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "authorization-a",
                "type": "execution_authorization",
                "state": "completed",
            },
        )
        late_pending = self.store.link_interaction(
            task_id=task_id,
            user_subject="user-a",
            interaction_record={
                "interaction_id": "late-pending-a",
                "user_subject": "user-a",
            },
            interaction={
                "interactionId": "late-pending-a",
                "type": "execution_authorization",
                "state": "pending",
            },
        )

        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(stale["status"], "succeeded")
        self.assertEqual(duplicate_authorization["status"], "succeeded")
        self.assertEqual(late_pending["status"], "succeeded")
        self.assertEqual(
            duplicate_authorization["current_interaction_id"],
            "authorization-a",
        )
        events = self.store.list_events(
            task_id=task_id,
            user_subject="user-a",
        )
        waiting_by_interaction = [
            event["payload"]["interactionId"]
            for event in events
            if event["event_type"] == "task.interaction.waiting"
        ]
        self.assertEqual(
            waiting_by_interaction,
            ["fields-a", "authorization-a"],
        )
        completed_by_interaction = [
            event["payload"]["interactionId"]
            for event in events
            if event["event_type"] == "task.interaction.completed"
        ]
        self.assertEqual(
            completed_by_interaction,
            ["fields-a", "authorization-a"],
        )

    def test_timeline_orders_events_and_deduplicates_cross_endpoint_messages(self):
        telegram, _ = self._endpoint()
        workspace, _ = self.store.ensure_endpoint(
            user_subject="user-a",
            token_id="workspace-token",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            capabilities=["workspace.timeline.read"],
        )
        task, _ = self._task(telegram["endpoint_id"])

        user_entry, reused = self.store.append_timeline_message(
            user_subject="user-a",
            source_endpoint_id=workspace["endpoint_id"],
            message_key="web-message-1:user",
            role="user",
            text="Submit a business trip request",
            task_id=task["task_id"],
        )
        same_entry, reused_again = self.store.append_timeline_message(
            user_subject="user-a",
            source_endpoint_id=workspace["endpoint_id"],
            message_key="web-message-1:user",
            role="user",
            text="Submit a business trip request",
            task_id=task["task_id"],
        )
        self.store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation={
                "operation_id": "prepare-a",
                "user_subject": "user-a",
                "capability_name": "oa.business_trip.submit.prepare",
                "status": "running",
            },
        )
        assistant_entry, assistant_reused = (
            self.store.append_timeline_message(
                user_subject="user-a",
                source_endpoint_id=workspace["endpoint_id"],
                message_key="web-message-1:assistant",
                role="assistant",
                text="Please confirm the trusted card.",
                task_id=task["task_id"],
            )
        )

        timeline = self.store.list_timeline(user_subject="user-a")
        sequences = [entry["sequence"] for entry in timeline]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))
        self.assertFalse(reused)
        self.assertTrue(reused_again)
        self.assertFalse(assistant_reused)
        self.assertEqual(same_entry["entry_id"], user_entry["entry_id"])
        self.assertGreater(
            assistant_entry["sequence"],
            user_entry["sequence"],
        )
        message_entries = [
            entry
            for entry in timeline
            if entry["entry_type"] == "chat_message"
        ]
        self.assertEqual(
            [entry["role"] for entry in message_entries],
            ["user", "assistant"],
        )

        telegram_outbox = self.store.list_outbox(
            user_subject="user-a",
            endpoint_id=telegram["endpoint_id"],
        )
        timeline_deliveries = [
            item
            for item in telegram_outbox
            if item["payload_type"] == "timeline_message"
        ]
        self.assertEqual(len(timeline_deliveries), 2)
        self.assertEqual(
            [item["payload"]["role"] for item in timeline_deliveries],
            ["user", "assistant"],
        )
        workspace_outbox = self.store.list_outbox(
            user_subject="user-a",
            endpoint_id=workspace["endpoint_id"],
        )
        self.assertFalse(
            any(
                item["payload_type"] == "timeline_message"
                for item in workspace_outbox
            )
        )

    def test_web_task_uses_timeline_without_push_outbox(self):
        workspace, _ = self.store.ensure_endpoint(
            user_subject="user-a",
            token_id="workspace-token",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            conversation_ref="agent:main:workspace:direct:account-a",
            capabilities=["workspace.task.read"],
        )

        task, _ = self._task(workspace["endpoint_id"])

        self.assertEqual(
            self.store.list_outbox(
                user_subject="user-a",
                endpoint_id=workspace["endpoint_id"],
            ),
            [],
        )
        timeline = self.store.list_timeline(user_subject="user-a")
        self.assertTrue(
            any(
                entry["entry_type"] == "task_event"
                and entry["task_id"] == task["task_id"]
                for entry in timeline
            )
        )

    def test_timeline_filters_chat_messages_and_pages_backward(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        messages = []
        for index, role in enumerate(("user", "assistant", "user"), start=1):
            entry, reused = self.store.append_timeline_message(
                user_subject="user-a",
                source_endpoint_id=endpoint["endpoint_id"],
                message_key=f"history-{index}",
                role=role,
                text=f"message {index}",
                task_id=task["task_id"],
            )
            self.assertFalse(reused)
            messages.append(entry)

        latest = self.store.list_timeline(
            user_subject="user-a",
            entry_type="chat_message",
            limit=2,
        )
        self.assertEqual(
            [entry["entry_id"] for entry in latest],
            [messages[1]["entry_id"], messages[2]["entry_id"]],
        )

        older = self.store.list_timeline(
            user_subject="user-a",
            before_sequence=latest[0]["sequence"],
            entry_type="chat_message",
            limit=2,
        )
        self.assertEqual(
            [entry["entry_id"] for entry in older],
            [messages[0]["entry_id"]],
        )

        after = self.store.list_timeline(
            user_subject="user-a",
            after_sequence=messages[0]["sequence"],
            entry_type="chat_message",
            limit=2,
        )
        self.assertEqual(
            [entry["entry_id"] for entry in after],
            [messages[1]["entry_id"], messages[2]["entry_id"]],
        )

        with self.assertRaises(ValueError):
            self.store.list_timeline(
                user_subject="user-a",
                after_sequence=1,
                before_sequence=2,
            )
        with self.assertRaises(ValueError):
            self.store.list_timeline(
                user_subject="user-a",
                entry_type="unknown",
            )

    def test_initialization_reconciles_legacy_web_push_deliveries(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        with self.store._connect() as connection:
            connection.execute(
                """
                UPDATE client_endpoints SET client_type = 'web'
                WHERE endpoint_id = ?
                """,
                (endpoint["endpoint_id"],),
            )

        repaired_store = TaskHubStore(self.store.db_path)
        deliveries = repaired_store.list_outbox(
            user_subject="user-a",
            endpoint_id=endpoint["endpoint_id"],
        )
        with repaired_store._connect() as connection:
            subscription = connection.execute(
                """
                SELECT state FROM task_subscriptions
                WHERE task_id = ? AND endpoint_id = ?
                """,
                (task["task_id"], endpoint["endpoint_id"]),
            ).fetchone()

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["state"], "acknowledged")
        self.assertIsNotNone(deliveries[0]["acknowledged_at"])
        self.assertEqual(subscription["state"], "inactive")

    def test_initialization_repairs_legacy_active_successful_tasks(self):
        db_path = Path(self.temp.name) / "repair-agentbridge.db"
        operations = OperationStore(db_path)
        operation, _ = operations.create(
            user_subject="user-a",
            capability_name="oa.workflow.pending.list",
            capability_version="1",
            input_summary={"limit": 20},
        )
        operation = operations.mark_succeeded(
            operation["operation_id"],
            {"count": 0},
        )
        task_store = TaskHubStore(db_path)
        endpoint, _ = task_store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
            client_type="workspace",
            external_subject="account-a",
            conversation_ref="agent:main:workspace:account-a",
        )
        task, _ = task_store.ensure_task(
            user_subject="user-a",
            agent_host="openclaw",
            host_task_key="agent:main:workspace:account-a|run-1",
            origin_endpoint_id=endpoint["endpoint_id"],
            active_conversation_ref="agent:main:workspace:account-a",
            title="List Pending OA Workflows",
        )
        for interaction_id in ("fields-repair", "authorization-repair"):
            task_store.link_interaction(
                task_id=task["task_id"],
                user_subject="user-a",
                interaction_record={
                    "interaction_id": interaction_id,
                    "user_subject": "user-a",
                },
                interaction={
                    "interactionId": interaction_id,
                    "type": "execution_authorization",
                    "state": "pending",
                },
            )
            task_store.link_interaction(
                task_id=task["task_id"],
                user_subject="user-a",
                interaction_record={
                    "interaction_id": interaction_id,
                    "user_subject": "user-a",
                },
                interaction={
                    "interactionId": interaction_id,
                    "type": "execution_authorization",
                    "state": "completed",
                },
            )
        task_store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation=operation,
        )
        with task_store._connect() as connection:
            connection.execute(
                """
                UPDATE agent_tasks
                SET status = 'waiting_user', finished_at = NULL,
                    current_interaction_id = 'fields-repair'
                WHERE task_id = ?
                """,
                (task["task_id"],),
            )

        repaired_store = TaskHubStore(db_path)
        repaired = repaired_store.get_task(
            task["task_id"],
            user_subject="user-a",
        )

        self.assertEqual(repaired["status"], "succeeded")
        self.assertEqual(
            repaired["current_interaction_id"],
            "authorization-repair",
        )
        self.assertIsNotNone(repaired["finished_at"])

    def test_initialization_repairs_legacy_completed_certificate_tasks(self):
        db_path = Path(self.temp.name) / "repair-certificate-task.db"
        task_store = TaskHubStore(db_path)
        endpoint, _ = task_store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            conversation_ref="agent:main:workspace:direct:account-a",
        )
        task, _ = task_store.ensure_task(
            user_subject="user-a",
            agent_host="openclaw",
            host_task_key="workspace|certificate-download",
            origin_endpoint_id=endpoint["endpoint_id"],
            active_conversation_ref=endpoint["conversation_ref"],
            title="Prepare and Deliver One OA Certificate Scan",
        )
        task_store.link_artifact(
            task_id=task["task_id"],
            user_subject="user-a",
            artifact={
                "artifact_type": "certificate_scan",
                "source_ref": "legacy-download-a",
                "filename": "certificate.pdf",
                "content_type": "application/pdf",
                "byte_size": 4096,
                "download_url": (
                    "https://10.10.50.213:8780/download/legacy-download-a/file"
                ),
                "expires_at": "2099-07-30T00:30:00+00:00",
            },
        )

        repaired_store = TaskHubStore(db_path)
        repaired = repaired_store.get_task(
            task["task_id"],
            user_subject="user-a",
        )

        self.assertEqual(repaired["status"], "succeeded")
        self.assertIsNotNone(repaired["finished_at"])

    def test_initialization_repairs_legacy_completed_standalone_login_task(self):
        db_path = Path(self.temp.name) / "repair-login-task.db"
        interactions = InteractionStore(db_path)
        task_store = TaskHubStore(db_path)
        endpoint, _ = task_store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            conversation_ref="agent:main:workspace:direct:account-a",
        )
        task, _ = task_store.ensure_task(
            user_subject="user-a",
            agent_host="openclaw",
            host_task_key="workspace|smartlight-login",
            origin_endpoint_id=endpoint["endpoint_id"],
            active_conversation_ref=endpoint["conversation_ref"],
            title="登录照明实验室测试系统",
        )
        record = interactions.register(
            interaction_type="credential",
            user_subject="user-a",
            system_id="smartlight",
            session_id="session-a",
            resource_id="challenge-a",
            title="登录照明实验室测试系统",
            message="请完成登录",
            display={},
            resume_spec={"kind": "session_ready", "systemId": "smartlight"},
            created_at="2026-08-12T01:00:00+00:00",
            expires_at="2026-08-12T01:10:00+00:00",
        )
        for state in ("pending", "completed"):
            task_store.link_interaction(
                task_id=task["task_id"],
                user_subject="user-a",
                interaction_record=record,
                interaction={
                    "interactionId": record["interaction_id"],
                    "type": "credential",
                    "state": state,
                },
            )
        with task_store._connect() as connection:
            connection.execute(
                """
                UPDATE agent_tasks
                SET status = 'active', finished_at = NULL
                WHERE task_id = ?
                """,
                (task["task_id"],),
            )

        repaired_store = TaskHubStore(db_path)
        repaired = repaired_store.get_task(
            task["task_id"],
            user_subject="user-a",
        )

        self.assertEqual(repaired["status"], "succeeded")
        self.assertIsNotNone(repaired["finished_at"])

    def test_initialization_repairs_legacy_active_superseded_tasks(self):
        db_path = Path(self.temp.name) / "repair-superseded-agentbridge.db"
        OperationStore(db_path)
        task_store = TaskHubStore(db_path)
        endpoint, _ = task_store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
        )
        task, _ = task_store.ensure_task(
            user_subject="user-a",
            agent_host="openclaw",
            host_task_key="session|superseded-run",
            origin_endpoint_id=endpoint["endpoint_id"],
            active_conversation_ref="agent:main:telegram:direct:1001",
            title="Superseded task",
        )
        for state in ("pending", "superseded"):
            task_store.link_interaction(
                task_id=task["task_id"],
                user_subject="user-a",
                interaction_record={
                    "interaction_id": "fields-superseded",
                    "user_subject": "user-a",
                },
                interaction={
                    "interactionId": "fields-superseded",
                    "type": "business_input",
                    "state": state,
                },
            )
        with task_store._connect() as connection:
            connection.execute(
                """
                UPDATE agent_tasks
                SET status = 'active', finished_at = NULL
                WHERE task_id = ?
                """,
                (task["task_id"],),
            )

        repaired_store = TaskHubStore(db_path)
        repaired = repaired_store.get_task(
            task["task_id"],
            user_subject="user-a",
        )

        self.assertEqual(repaired["status"], "superseded")
        self.assertIsNotNone(repaired["finished_at"])

    def test_central_service_recovers_only_the_bound_users_pending_interaction(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        task_response = service.ensure_host_task(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            host_task_key="session|run",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            title="Login and read OA",
            route={"channel": "telegram", "to": "1001"},
        )
        session = service.sessions.get_or_create(
            user_subject="user-a",
            system_id="oa",
        )
        challenge = service.challenges.create(
            user_subject="user-a",
            system_id="oa",
            system_name="OA",
            session_id=session["session_id"],
            origin="http://oa.example.test",
            page_fingerprint="login-page",
            nonce=None,
            fields=[],
            card_base_url="https://cards.example.test",
            challenge_type="interactive_browser_login",
        )
        interaction = service._credential_interaction(challenge)
        observed = service.observe_host_task(
            user_subject="user-a",
            task_id=task_response["task"]["taskId"],
            interaction_ids=[interaction["interactionId"]],
        )
        recovered = service.recover_host_tasks(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
        )

        self.assertEqual(observed["task"]["status"], "waiting_user")
        self.assertEqual(recovered["count"], 1)
        self.assertEqual(
            recovered["recoveries"][0]["task"]["taskId"],
            task_response["task"]["taskId"],
        )
        self.assertEqual(
            recovered["recoveries"][0]["interaction"]["interactionId"],
            interaction["interactionId"],
        )
        with self.assertRaises(TaskNotFound):
            service.recover_host_tasks(
                user_subject="user-b",
                agent_host="openclaw",
                endpoint_key="telegram:*:1001",
            )

    def test_central_service_recovers_workspace_tasks_for_the_same_user(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        service.ensure_host_task(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            host_task_key="telegram-companion",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            title="Companion endpoint",
            route={"channel": "telegram", "to": "1001"},
        )
        workspace = service.ensure_host_task(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            host_task_key="workspace|run",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            title="Approve from Workspace",
            route={"channel": "webchat", "to": "account-a"},
        )
        session = service.sessions.get_or_create(
            user_subject="user-a",
            system_id="oa",
        )
        challenge = service.challenges.create(
            user_subject="user-a",
            system_id="oa",
            system_name="OA",
            session_id=session["session_id"],
            origin="http://oa.example.test",
            page_fingerprint="login-page",
            nonce=None,
            fields=[],
            card_base_url="https://cards.example.test",
            challenge_type="interactive_browser_login",
        )
        interaction = service._credential_interaction(challenge)
        service.observe_host_task(
            user_subject="user-a",
            task_id=workspace["task"]["taskId"],
            interaction_ids=[interaction["interactionId"]],
        )

        direct_only = service.recover_host_tasks(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
        )
        all_endpoints = service.recover_host_tasks(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            include_user_endpoints=True,
        )

        self.assertEqual(direct_only["count"], 0)
        self.assertEqual(all_endpoints["count"], 1)
        self.assertEqual(
            all_endpoints["recoveries"][0]["task"]["taskId"],
            workspace["task"]["taskId"],
        )
        self.assertEqual(
            all_endpoints["recoveries"][0]["endpoint"]["clientType"],
            "web",
        )

        completed = {
            **interaction,
            "state": "completed",
            "resume": {
                "tool": "agentbridge_interaction_resume",
                "ready": True,
                "completed": False,
            },
        }
        with patch.object(
            service,
            "_load_interaction",
            return_value=({}, {}, completed),
        ):
            resumable = service.recover_host_tasks(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_key="telegram:*:1001",
                include_user_endpoints=True,
            )
        self.assertEqual(resumable["count"], 1)
        self.assertEqual(
            resumable["recoveries"][0]["interaction"]["state"],
            "completed",
        )

        expired = {
            **interaction,
            "state": "expired",
            "resume": {
                "tool": "agentbridge_interaction_resume",
                "ready": False,
                "completed": False,
            },
        }
        record = service.interactions.get(
            interaction["interactionId"],
            user_subject="user-a",
        )
        with patch.object(
            service,
            "_load_interaction",
            return_value=(record, {}, expired),
        ):
            terminal = service.recover_host_tasks(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_key="telegram:*:1001",
                include_user_endpoints=True,
            )
        reconciled = service.tasks.get_task(
            workspace["task"]["taskId"],
            user_subject="user-a",
        )
        self.assertEqual(terminal["count"], 0)
        self.assertEqual(reconciled["status"], "expired")

    def test_central_service_presents_one_authorization_on_multiple_endpoints(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        origin = service.ensure_host_task(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            host_task_key="telegram-task",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            title="Submit leave request",
            route={"channel": "telegram", "to": "1001"},
            capabilities=["trusted_interaction", "direct_status"],
        )
        service.tasks.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="wechat:direct:user-a",
            client_type="wechat",
            external_subject="wechat-user-a",
            conversation_ref="agent:main:wechat:direct:user-a",
            route={"channel": "wechat", "to": "wechat-user-a"},
            capabilities=["trusted_interaction", "direct_status"],
        )
        operation, _ = service.operations.create(user_subject="user-a", capability_name="oa.leave.submit.prepare",
            capability_version="1", input_summary={})
        authorization = service.write_authorizations.create(
            user_subject="user-a",
            system_id="oa",
            session_id="session-a",
            capability_name="oa.leave.submit",
            capability_version="1",
            prepare_operation_id=operation["operation_id"],
            plan={"reason": "Test"},
            summary={"title": "Submit leave request", "fields": []},
            card_base_url="https://cards.example.test",
        )
        interaction = service._execution_authorization_interaction(authorization)
        service.observe_host_task(
            user_subject="user-a",
            task_id=origin["task"]["taskId"],
            interaction_ids=[interaction["interactionId"]],
        )

        telegram = service.present_interaction(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            interaction_id=interaction["interactionId"],
        )
        wechat = service.claim_host_notifications(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="wechat:direct:user-a",
        )

        self.assertEqual(wechat["count"], 1)
        notification = wechat["notifications"][0]
        self.assertEqual(
            notification["deliveryMode"],
            "trusted_interaction",
        )
        self.assertNotEqual(
            telegram["interaction"]["presentation"]["url"],
            notification["interaction"]["presentation"]["url"],
        )
        self.assertTrue(
            telegram["interaction"]["presentation"]["individualized"]
        )
        acknowledged = service.acknowledge_host_notification(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="wechat:direct:user-a",
            delivery_id=notification["deliveryId"],
            succeeded=True,
        )
        self.assertEqual(
            acknowledged["delivery"]["state"],
            "acknowledged",
        )
        presentation_id = telegram["interaction"]["presentation"][
            "presentationId"
        ]
        csrf = service.write_authorizations.issue_csrf(
            authorization["authorization_id"],
            presentation_id=presentation_id,
        )
        service.write_authorizations.decide(
            authorization["authorization_id"],
            decision="approve",
            csrf_token=csrf,
            csrf_cookie=csrf,
            presentation_id=presentation_id,
        )
        service.get_interaction(
            user_subject="user-a",
            interaction_id=interaction["interactionId"],
        )
        terminal = service.claim_host_notifications(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="wechat:direct:user-a",
        )
        self.assertEqual(terminal["count"], 1)
        self.assertEqual(
            terminal["notifications"][0]["deliveryMode"],
            "status",
        )
        self.assertIn(
            "可信确认已完成",
            terminal["notifications"][0]["message"],
        )


    def test_central_service_presents_task_artifact_to_companion_endpoint(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        origin = service.ensure_host_task(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            host_task_key="workspace-certificate-task",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            account_id="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            title="Download OA certificate",
            capabilities=["workspace.task.read"],
        )
        service.tasks.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            route={"channel": "telegram", "to": "1001"},
            capabilities=["trusted_interaction", "direct_status"],
        )
        service.tasks.link_artifact(
            task_id=origin["task"]["taskId"],
            user_subject="user-a",
            artifact={
                "artifact_type": "certificate_scan",
                "source_ref": "download-a",
                "filename": "certificate.pdf",
                "content_type": "application/pdf",
                "byte_size": 4096,
                "download_url": (
                    "https://10.10.50.213:8780/download/download-a/file"
                ),
                "expires_at": "2099-07-30T00:30:00+00:00",
            },
        )

        claimed = service.claim_host_notifications(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
        )

        self.assertEqual(claimed["count"], 1)
        notification = claimed["notifications"][0]
        self.assertEqual(notification["deliveryMode"], "artifact")
        self.assertEqual(notification["artifact"]["filename"], "certificate.pdf")
        self.assertTrue(notification["artifact"]["mediaUrl"].endswith("/file"))

        report_id = "a" * 32
        protected, _ = service.tasks.link_artifact(
            task_id=origin["task"]["taskId"], user_subject="user-a",
            artifact={"artifact_type": "database_csv", "source_ref": report_id,
                      "filename": "taihua-personal-daily.csv", "content_type": "text/csv",
                      "byte_size": 100, "expires_at": "2099-07-30T00:30:00+00:00",
                      "download_url": f"/api/database/reports/taihua_primary/{report_id}/download"},
        )
        claimed = service.claim_host_notifications(
            user_subject="user-a", agent_host="openclaw", endpoint_key="telegram:*:1001",
        )
        self.assertEqual(claimed["count"], 1)
        self.assertEqual(claimed["notifications"][0]["deliveryMode"], "no_op")
        self.assertIsNone(claimed["notifications"][0]["artifact"])
        self.assertEqual(protected["state"], "ready")

    def test_central_service_delivers_private_plan_result_to_companion_only(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        service.tasks.ensure_endpoint(
            user_subject="user-a",
            token_id="telegram-token",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            route={"channel": "telegram", "to": "1001"},
            capabilities=["direct_status"],
        )
        origin = service.ensure_host_task(
            user_subject="user-a",
            token_id="workspace-token",
            agent_host="openclaw",
            host_task_key="workspace-plan-task",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            account_id="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            title="总结昨日工作",
            capabilities=["workspace.task.read"],
        )
        initial = service.claim_host_notifications(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
        )
        for item in initial["notifications"]:
            service.acknowledge_host_notification(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_key="telegram:*:1001",
                delivery_id=item["deliveryId"],
                succeeded=True,
            )
        plan, _ = service.task_plans.create(
            user_subject="user-a",
            parent_task_id=origin["task"]["taskId"],
            compiled_plan={
                "proposalSchemaVersion": "agentbridge.task-plan.proposal.v2",
                "goal": "总结昨日工作",
                "planHash": "d" * 64,
                "riskSummary": {"systems": ["oa"], "writeSinkCount": 0},
                "steps": [
                    {
                        "stepKey": "draft",
                        "ordinal": 1,
                        "kind": "transform",
                        "title": "生成摘要",
                        "transformName": "work_items_to_log_draft.v2",
                        "version": "2",
                        "dependsOn": [],
                        "arguments": {},
                        "bindings": {},
                        "effect": "read",
                        "systemId": "local",
                    }
                ],
            },
            proposal_source="agent_host",
            coordinator_lease_version=1,
            idempotency_key="private-plan-result",
        )
        private_draft = "昨日工作内容。" * 700
        projection = {
            "schemaVersion": "agentbridge.plan-result-projection.v1",
            "visibility": "user_private",
            "kind": "private_draft",
            "resultHash": "sha256:" + "e" * 64,
            "result": {
                "draft": private_draft,
                "included_count": 2,
                "excluded_count": 1,
                "coverage": {"status": "complete"},
            },
        }
        service.task_plans.set_result_projection(
            plan["plan_id"],
            user_subject="user-a",
            projection=projection,
        )
        service.tasks.record_plan_event(
            task_id=origin["task"]["taskId"],
            user_subject="user-a",
            event_type="plan.result.ready",
            payload={"planId": plan["plan_id"], "resultHash": projection["resultHash"]},
        )

        claimed = service.claim_host_notifications(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
        )

        self.assertEqual(claimed["count"], 1)
        notification = claimed["notifications"][0]
        self.assertEqual(notification["deliveryMode"], "plan_result")
        self.assertLessEqual(len(notification["message"]), 3_800)
        self.assertIn("（已截断）", notification["message"])
        self.assertEqual(notification["planResult"]["result"]["draft"], private_draft)
        events = service.tasks.list_events(
            task_id=origin["task"]["taskId"],
            user_subject="user-a",
        )
        plan_event = next(
            item for item in events if item["event_type"] == "plan.result.ready"
        )
        self.assertNotIn("draft", str(plan_event["payload"]))

    def test_central_service_presents_business_input_on_multiple_endpoints(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        origin = service.ensure_host_task(
            user_subject="user-a",
            token_id="workspace-token",
            agent_host="openclaw",
            host_task_key="workspace-task",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            account_id="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            title="Submit business trip",
            capabilities=["workspace.task.read"],
        )
        service.tasks.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            route={"channel": "telegram", "to": "1001"},
            capabilities=["trusted_interaction", "direct_status"],
        )
        operation, _ = service.operations.create(user_subject="user-a", capability_name="oa.business_trip.submit.prepare",
            capability_version="1", input_summary={})
        submission = service.field_submissions.create(
            user_subject="user-a",
            system_id="oa",
            session_id="session-a",
            capability_name="oa.business_trip.submit.prepare",
            capability_version="1",
            create_operation_id=operation["operation_id"],
            form_schema={
                "title": "Business trip",
                "fields": [
                    {
                        "name": "reason",
                        "label": "Reason",
                        "control": "text",
                        "required": True,
                    }
                ],
            },
            card_base_url="https://cards.example.test",
        )
        interaction = service._business_input_interaction(submission)
        service.observe_host_task(
            user_subject="user-a",
            task_id=origin["task"]["taskId"],
            interaction_ids=[interaction["interactionId"]],
        )

        workspace = service.present_interaction(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
            interaction_id=interaction["interactionId"],
        )
        telegram = service.claim_host_notifications(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
        )

        self.assertEqual(telegram["count"], 1)
        delivered = telegram["notifications"][0]
        self.assertEqual(delivered["deliveryMode"], "trusted_interaction")
        self.assertTrue(
            delivered["interaction"]["presentation"]["individualized"]
        )
        self.assertNotEqual(
            workspace["interaction"]["presentation"]["url"],
            delivered["interaction"]["presentation"]["url"],
        )


    def test_central_service_claims_cross_endpoint_timeline_message(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        service.tasks.ensure_endpoint(
            user_subject="user-a",
            token_id="telegram-token",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            label="Telegram",
            route={"channel": "telegram", "to": "1001"},
            capabilities=["direct_status", "timeline_message"],
        )

        appended = service.append_host_timeline_message(
            user_subject="user-a",
            token_id="workspace-token",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            message_key="workspace-message-1",
            role="user",
            text="Submit a business trip request",
            label="Agent Workspace",
        )
        claimed = service.claim_host_notifications(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
        )

        self.assertFalse(appended["reused"]["entry"])
        self.assertEqual(claimed["count"], 1)
        notification = claimed["notifications"][0]
        self.assertEqual(notification["deliveryMode"], "timeline_message")
        self.assertIsNone(notification["task"])
        self.assertIsNone(notification["event"])
        self.assertIn("Submit a business trip request", notification["message"])
        self.assertEqual(
            notification["timeline"]["source"]["label"],
            "Agent Workspace",
        )

    def test_wechat_user_activity_archives_missed_chats_without_replaying_history(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        wechat, _ = service.tasks.ensure_endpoint(
            user_subject="user-a",
            token_id="wechat-token",
            agent_host="openclaw",
            endpoint_key="openclaw-weixin:*:wechat-user-a",
            client_type="openclaw-weixin",
            external_subject="wechat-user-a",
            conversation_ref=(
                "agent:main:openclaw-weixin:direct:wechat-user-a"
            ),
            label="WeChat",
            route={"channel": "openclaw-weixin", "to": "wechat-user-a"},
            capabilities=["direct_status", "timeline_message"],
        )
        service.append_host_timeline_message(
            user_subject="user-a",
            token_id="workspace-token",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            message_key="workspace-message-for-wechat",
            role="user",
            text="Read OA pending workflows",
            label="Agent Workspace",
        )
        claimed = service.claim_host_notifications(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="openclaw-weixin:*:wechat-user-a",
        )
        service.acknowledge_host_notification(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="openclaw-weixin:*:wechat-user-a",
            delivery_id=claimed["notifications"][0]["deliveryId"],
            succeeded=False,
            defer_until_activity=True,
        )

        assistant = service.append_host_timeline_message(
            user_subject="user-a",
            token_id="wechat-token",
            agent_host="openclaw",
            endpoint_key="openclaw-weixin:*:wechat-user-a",
            client_type="openclaw-weixin",
            external_subject="wechat-user-a",
            conversation_ref=(
                "agent:main:openclaw-weixin:direct:wechat-user-a"
            ),
            message_key="wechat-assistant-message",
            role="assistant",
            text="Previous assistant response",
            label="WeChat",
        )
        self.assertEqual(assistant["reactivatedDeliveries"], 0)
        user = service.append_host_timeline_message(
            user_subject="user-a",
            token_id="wechat-token",
            agent_host="openclaw",
            endpoint_key="openclaw-weixin:*:wechat-user-a",
            client_type="openclaw-weixin",
            external_subject="wechat-user-a",
            conversation_ref=(
                "agent:main:openclaw-weixin:direct:wechat-user-a"
            ),
            message_key="wechat-user-message",
            role="user",
            text="Continue",
            label="WeChat",
        )

        self.assertEqual(user["reactivatedDeliveries"], 0)
        deliveries = service.tasks.list_outbox(
            user_subject="user-a",
            endpoint_id=wechat["endpoint_id"],
            limit=20,
        )
        reactivated = next(
            item
            for item in deliveries
            if item["delivery_id"] == claimed["notifications"][0]["deliveryId"]
        )
        self.assertEqual(reactivated["state"], "archived")
        self.assertEqual(reactivated["attempt_count"], 1)
        self.assertEqual(service.tasks.get_timeline_message(
            message_key="workspace-message-for-wechat",
            user_subject="user-a",
        )["text"], "Read OA pending workflows")
        self.assertEqual(service.tasks.claim_outbox(
            user_subject="user-a", endpoint_id=wechat["endpoint_id"],
        ), [])

    def test_wechat_duplicate_ingress_does_not_wake_delivery_queue(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        args = dict(
            user_subject="user-a", token_id="wechat-token", agent_host="openclaw",
            endpoint_key="openclaw-weixin:*:wechat-user-a",
            client_type="openclaw-weixin", external_subject="wechat-user-a",
            conversation_ref="agent:main:openclaw-weixin:direct:wechat-user-a",
            message_key="wechat-inbound-1", role="user", text="Query OA pending items",
        )
        first = service.append_host_timeline_message(**args)
        with patch.object(service.tasks, "reactivate_deferred_outbox") as reactivate:
            duplicate = service.append_host_timeline_message(**args)
        self.assertTrue(duplicate["reused"]["entry"])
        self.assertEqual(duplicate["entry"]["entryId"], first["entry"]["entryId"])
        self.assertEqual(duplicate["reactivatedDeliveries"], 0)
        reactivate.assert_not_called()

    def test_central_service_returns_only_same_user_other_endpoint_context(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        service.tasks.ensure_endpoint(
            user_subject="user-a",
            token_id="telegram-token",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            label="Telegram",
            capabilities=["timeline_message"],
        )
        service.append_host_timeline_message(
            user_subject="user-a",
            token_id="telegram-token",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            message_key="telegram-message-1",
            role="user",
            text="Current endpoint text must not be injected",
            label="Telegram",
        )
        service.append_host_timeline_message(
            user_subject="user-a",
            token_id="workspace-token",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            message_key="workspace-message-1",
            role="assistant",
            text="Cross-end affair_id is affair-new-123",
            label="Agent Workspace",
        )
        service.append_host_timeline_message(
            user_subject="user-b",
            token_id="workspace-token-b",
            agent_host="openclaw",
            endpoint_key="workspace:account-b",
            client_type="web",
            external_subject="account-b",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-b"
            ),
            message_key="workspace-message-b",
            role="assistant",
            text="Another user's private timeline text",
            label="Agent Workspace B",
        )

        context = service.get_host_cross_endpoint_context(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            max_age_minutes=60,
            limit=5,
        )

        self.assertEqual(context["status"], "succeeded")
        self.assertEqual(context["count"], 1)
        self.assertEqual(
            context["entries"][0]["text"],
            "Cross-end affair_id is affair-new-123",
        )
        self.assertEqual(
            context["entries"][0]["source"],
            {"clientType": "web", "label": "Agent Workspace"},
        )
        serialized = str(context)
        self.assertNotIn("Current endpoint text", serialized)
        self.assertNotIn("Another user's private", serialized)

    def test_central_service_resolves_terminal_follow_up_and_ambiguous_tasks(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        first = service.ensure_host_task(
            user_subject="user-a",
            token_id="workspace-account:account-a",
            agent_host="openclaw",
            host_task_key="workspace|pending-list",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            account_id="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            title="List OA pending workflows",
            label="Agent Workspace",
        )
        second = service.ensure_host_task(
            user_subject="user-a",
            token_id="workspace-account:account-a",
            agent_host="openclaw",
            host_task_key="workspace|sent-list",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            account_id="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            title="List OA sent workflows",
            label="Agent Workspace",
        )
        service.tasks.ensure_endpoint(
            user_subject="user-a",
            token_id="telegram-token",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            label="Telegram",
            route={"channel": "telegram", "to": "1001"},
        )
        operation, _ = service.operations.create(
            user_subject="user-a",
            capability_name="oa.workflow.pending.list",
            capability_version="1",
            input_summary={"limit": 20},
        )
        operation = service.operations.mark_succeeded(
            operation["operation_id"],
            {"count": 3},
        )
        service.tasks.link_operation(
            task_id=first["task"]["taskId"],
            user_subject="user-a",
            operation=operation,
        )

        selected = service.resolve_host_task_continuation(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            task_id=first["task"]["taskId"],
        )
        self.assertEqual(selected["status"], "selected")
        self.assertEqual(
            selected["continuation"]["executionMode"],
            "observe_only",
        )
        self.assertFalse(selected["continuation"]["allowNewOperation"])
        self.assertEqual(
            selected["snapshot"]["summary"]["operation"]["capability"],
            "oa.workflow.pending.list",
        )
        self.assertEqual(
            selected["snapshot"]["summary"]["origin"]["clientType"],
            "web",
        )

        follow_up = service.resolve_host_task_continuation(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            task_id=first["task"]["taskId"],
            allow_follow_up=True,
        )
        self.assertEqual(
            follow_up["continuation"]["executionMode"],
            "follow_up",
        )
        self.assertTrue(follow_up["continuation"]["allowNewOperation"])

        # A recent-task reference must not reuse an older selected task.
        recent = service.resolve_host_task_continuation(
            user_subject="user-a", agent_host="openclaw",
            endpoint_key="telegram:*:1001", prefer_latest=True,
        )
        self.assertEqual(recent["task"]["taskId"], second["task"]["taskId"])
        self.assertNotEqual(recent["continuation"]["reason"], "existing_selection")

        ambiguous = service.resolve_host_task_continuation(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            source_client_type="web",
            cross_endpoint_only=True,
            prefer_active=False,
            reuse_selected=False,
        )
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertEqual(ambiguous["count"], 2)
        self.assertEqual(
            {item["taskId"] for item in ambiguous["candidates"]},
            {first["task"]["taskId"], second["task"]["taskId"]},
        )

        ordinal = service.resolve_host_task_continuation(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            ordinal=2,
            reuse_selected=False,
        )
        self.assertEqual(ordinal["status"], "selected")
        self.assertIn(
            ordinal["task"]["taskId"],
            {first["task"]["taskId"], second["task"]["taskId"]},
        )

        latest = service.resolve_host_task_continuation(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            source_client_type="web",
            cross_endpoint_only=True,
            prefer_active=False,
            prefer_latest=True,
            reuse_selected=False,
        )
        self.assertEqual(latest["status"], "selected")
        self.assertEqual(
            latest["task"]["taskId"],
            ordinal["task"]["taskId"],
        )
        self.assertEqual(
            latest["continuation"]["reason"],
            "latest_relative_reference",
        )

    def test_workspace_task_does_not_overwrite_registered_endpoint(self):
        service = authorized_service(
            home=Path(self.temp.name),
            base_url="http://oa.example.test/seeyon/main.do?method=main",
        )
        endpoint, _ = service.tasks.ensure_endpoint(
            user_subject="user-a",
            token_id="workspace-account:account-a",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            account_id="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            label="Agent Workspace: alice",
            capabilities=["workspace.task.read"],
        )

        service.ensure_host_task(
            user_subject="user-a",
            token_id="telegram-token",
            agent_host="openclaw",
            host_task_key="workspace-session|run-1",
            endpoint_key="workspace:account-a",
            client_type="web",
            external_subject="account-a",
            account_id="account-a",
            conversation_ref=(
                "agent:main:agentbridge-workspace:direct:account-a"
            ),
            title="Read pending items",
            capabilities=["workspace.task.read"],
        )
        preserved = service.tasks.endpoint_for_key(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="workspace:account-a",
        )

        self.assertEqual(preserved["endpoint_id"], endpoint["endpoint_id"])
        self.assertEqual(
            preserved["token_id"],
            "workspace-account:account-a",
        )
        self.assertEqual(preserved["client_type"], "web")
        self.assertEqual(preserved["label"], "Agent Workspace: alice")

    def test_task_schema_migrates_alongside_existing_ledgers(self):
        db_path = Path(self.temp.name) / "legacy-agentbridge.db"
        operations = OperationStore(db_path)
        operation, _ = operations.create(
            user_subject="user-a",
            capability_name="oa.workflow.pending.list",
            capability_version="1",
            input_summary={"limit": 1},
        )
        interactions = InteractionStore(db_path)
        interaction = interactions.register(
            interaction_type="business_input",
            user_subject="user-a",
            system_id="oa",
            session_id="session-a",
            operation_id=operation["operation_id"],
            resource_id="resource-a",
            title="Enter fields",
            message="Use the trusted page.",
            display={"fieldCount": 1},
            resume_spec={
                "kind": "capability",
                "capability": "oa.leave.prepare",
                "arguments": {"input_submission_id": "resource-a"},
            },
            created_at="2026-07-30T00:00:00+00:00",
            expires_at="2099-07-30T00:30:00+00:00",
        )

        task_store = TaskHubStore(db_path)

        self.assertEqual(
            operations.get(operation["operation_id"])["user_subject"],
            "user-a",
        )
        self.assertEqual(
            interactions.get(
                interaction["interaction_id"],
                user_subject="user-a",
            )["resource_id"],
            "resource-a",
        )
        endpoint, _ = task_store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
        )
        task, _ = task_store.ensure_task(
            user_subject="user-a",
            agent_host="openclaw",
            host_task_key="session|run",
            origin_endpoint_id=endpoint["endpoint_id"],
            active_conversation_ref="agent:main:telegram:direct:1001",
            title="Migrated task",
        )
        self.assertEqual(task["status"], "active")

    def test_runtime_diagnostics_report_counts_without_message_content(self):
        first, _ = self._endpoint()
        second, _ = self.store.ensure_endpoint(
            user_subject="user-b",
            token_id="token-b",
            agent_host="openclaw",
            endpoint_key="openclaw-weixin:*:2002",
            client_type="openclaw-weixin",
            external_subject="2002",
            conversation_ref="agent:main:openclaw-weixin:direct:2002",
            capabilities=["direct_status", "trusted_interaction"],
        )
        task, _ = self._task(first["endpoint_id"])
        self.store.select_continuation(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_id=first["endpoint_id"],
            task_id=task["task_id"],
            execution_mode="resume",
        )
        self.store.append_timeline_message(
            user_subject="user-a",
            source_endpoint_id=first["endpoint_id"],
            message_key="message-secret",
            role="user",
            text="sensitive business text",
            task_id=task["task_id"],
        )
        self.store.append_timeline_message(
            user_subject="user-a",
            source_endpoint_id=first["endpoint_id"],
            message_key="message-without-task",
            role="assistant",
            text="ordinary cross-end status",
        )

        report = self.store.runtime_diagnostics()

        self.assertTrue(report["isolation"]["passed"])
        self.assertEqual(report["summary"]["users"], 2)
        self.assertEqual(report["summary"]["active_endpoints"], 2)
        self.assertEqual(report["summary"]["active_task_continuations"], 1)
        self.assertGreaterEqual(report["summary"]["timeline_entries"], 1)
        user_a = next(
            item for item in report["users"]
            if item["user_subject"] == "user-a"
        )
        self.assertEqual(
            user_a["task_continuations"],
            [{"state": "selected", "execution_mode": "resume", "count": 1}],
        )
        user_b = next(
            item for item in report["users"]
            if item["user_subject"] == "user-b"
        )
        self.assertEqual(user_b["endpoints"][0]["client_type"], "openclaw-weixin")
        self.assertNotIn("sensitive business text", str(report))
        self.assertNotIn(repr(first["external_subject"]), str(report))
        self.assertNotIn(repr(second["external_subject"]), str(report))

    def test_runtime_diagnostics_detect_cross_user_task_corruption(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            connection.execute(
                "UPDATE agent_tasks SET user_subject = 'user-b' WHERE task_id = ?",
                (task["task_id"],),
            )
            connection.commit()

        report = self.store.runtime_diagnostics()

        self.assertFalse(report["isolation"]["passed"])
        self.assertGreater(
            report["isolation"]["violations"]["task_origin_user_mismatch"],
            0,
        )

    def test_runtime_diagnostics_detect_cross_user_continuation_corruption(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        self.store.select_continuation(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_id=endpoint["endpoint_id"],
            task_id=task["task_id"],
            execution_mode="resume",
        )
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            connection.execute(
                "UPDATE task_continuations SET user_subject = 'user-b' "
                "WHERE endpoint_id = ?",
                (endpoint["endpoint_id"],),
            )
            connection.commit()

        report = self.store.runtime_diagnostics()

        self.assertFalse(report["isolation"]["passed"])
        self.assertGreater(
            report["isolation"]["violations"]["continuation_binding_mismatch"],
            0,
        )

    def test_batch_freezes_items_and_advances_without_finishing_parent_early(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        batch, reused = self.store.create_batch(
            parent_task_id=task["task_id"],
            user_subject="user-a",
            system_id="oa",
            capability_name="oa.missed_punch.approval.batch.prepare",
            selection_summary={"workflowType": "missed_punch"},
            failure_policy="stop_on_failure",
            items=[
                {
                    "resource_ref": f"affair-{ordinal}",
                    "display_summary": {
                        "title": f"补签申请单-{ordinal}",
                        "sender": "Alice",
                    },
                }
                for ordinal in range(1, 4)
            ],
        )

        self.assertFalse(reused)
        self.assertEqual(batch["total_count"], 3)
        self.assertEqual(
            [item["resource_ref"] for item in batch["items"]],
            ["affair-1", "affair-2", "affair-3"],
        )
        public = self.store.get_batch_for_task(
            parent_task_id=task["task_id"],
            user_subject="user-a",
        )
        self.assertNotIn("resource_ref", public["items"][0])

        self.store.record_batch_item_activity(
            parent_task_id=task["task_id"],
            user_subject="user-a",
            operation_id="operation-1",
            interaction_id="interaction-1",
        )
        completion = self.store.complete_current_batch_item(
            parent_task_id=task["task_id"],
            user_subject="user-a",
            operation_id="commit-1",
            expected_ordinal=1,
            result_summary={"verified": True},
        )

        self.assertEqual(completion["batch"]["state"], "running")
        self.assertEqual(completion["batch"]["current_ordinal"], 2)
        self.assertEqual(completion["nextItem"]["resource_ref"], "affair-2")
        linked = self.store.link_operation(
            task_id=task["task_id"],
            user_subject="user-a",
            operation={
                "operation_id": "commit-1",
                "user_subject": "user-a",
                "capability_name": "oa.missed_punch.approve",
                "status": "succeeded",
                "error": None,
            },
        )
        self.assertEqual(linked["status"], "running")

    def test_batch_finishes_only_after_last_item_and_halts_on_unknown(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        self.store.create_batch(
            parent_task_id=task["task_id"],
            user_subject="user-a",
            system_id="oa",
            capability_name="oa.missed_punch.approval.batch.prepare",
            selection_summary={},
            failure_policy="stop_on_failure",
            items=[
                {"resource_ref": "affair-1", "display_summary": {"title": "补签1"}},
                {"resource_ref": "affair-2", "display_summary": {"title": "补签2"}},
            ],
        )
        self.store.complete_current_batch_item(
            parent_task_id=task["task_id"],
            user_subject="user-a",
            operation_id="commit-1",
            expected_ordinal=1,
        )
        halted = self.store.fail_current_batch_item(
            parent_task_id=task["task_id"],
            user_subject="user-a",
            operation_id="commit-2",
            expected_ordinal=2,
            item_state="outcome_unknown",
            error_code="RESULT_UNKNOWN",
        )

        self.assertEqual(halted["state"], "outcome_unknown")
        self.assertEqual(halted["succeeded_count"], 1)
        self.assertEqual(
            self.store.get_task(task["task_id"], user_subject="user-a")["status"],
            "outcome_unknown",
        )

    def test_batch_duplicate_completion_cannot_skip_the_next_item(self):
        endpoint, _ = self._endpoint()
        task, _ = self._task(endpoint["endpoint_id"])
        self.store.create_batch(
            parent_task_id=task["task_id"],
            user_subject="user-a",
            system_id="oa",
            capability_name="oa.missed_punch.approval.batch.prepare",
            selection_summary={},
            failure_policy="stop_on_failure",
            items=[
                {"resource_ref": "affair-1", "display_summary": {"title": "补签1"}},
                {"resource_ref": "affair-2", "display_summary": {"title": "补签2"}},
            ],
        )
        self.store.record_batch_item_activity(
            parent_task_id=task["task_id"],
            user_subject="user-a",
            operation_id="commit-1",
            interaction_id=None,
        )
        self.store.complete_current_batch_item(
            parent_task_id=task["task_id"],
            user_subject="user-a",
            operation_id="commit-1",
            expected_ordinal=1,
        )

        stale_activity = self.store.record_batch_item_activity(
            parent_task_id=task["task_id"],
            user_subject="user-a",
            operation_id="commit-1",
            interaction_id=None,
        )
        duplicate = self.store.complete_current_batch_item(
            parent_task_id=task["task_id"],
            user_subject="user-a",
            operation_id="commit-1",
            expected_ordinal=1,
        )

        self.assertEqual(stale_activity["current_ordinal"], 2)
        self.assertEqual(stale_activity["succeeded_count"], 1)
        self.assertEqual(stale_activity["items"][1]["state"], "preparing")
        self.assertIsNone(stale_activity["items"][1]["operation_id"])
        self.assertTrue(duplicate["reused"])
        self.assertEqual(duplicate["nextItem"]["resource_ref"], "affair-2")
        self.assertEqual(duplicate["batch"]["succeeded_count"], 1)

    def _endpoint(self):
        return self.store.ensure_endpoint(
            user_subject="user-a",
            token_id="token-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            label="User A",
            capabilities=["direct_status", "trusted_interaction"],
            route={"channel": "telegram", "to": "1001"},
        )

    def _task(self, endpoint_id):
        return self.store.ensure_task(
            user_subject="user-a",
            agent_host="openclaw",
            host_task_key="agent:main:telegram:direct:1001|run-1",
            origin_endpoint_id=endpoint_id,
            active_conversation_ref="agent:main:telegram:direct:1001",
            title="OA Leave Request",
        )


if __name__ == "__main__":
    unittest.main()
