import asyncio
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.core import commit_notifications as notices
from agentbridge.core.write_authorizations import WriteAuthorizationStateError
from agentbridge.mcp.central import _claim_host_notifications_with_wait, _get_interaction_with_wait
from tests.authorization_fixtures import authorized_service
from tests.test_write_authorizations import _authorization


class CommitNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = authorized_service(home=Path(self.temp.name), base_url="http://oa.example.test/seeyon/main.do?method=main")
        self.store = self.service.tasks
        self.endpoint, _ = self.store.ensure_endpoint(user_subject="user-a", token_id="token-a", agent_host="openclaw",
            endpoint_key="telegram:a", client_type="telegram", external_subject="a", conversation_ref="conversation-a",
            capabilities=["direct_status", "trusted_interaction"])
        self.task, _ = self.store.ensure_task(user_subject="user-a", agent_host="openclaw", host_task_key="task-one",
            origin_endpoint_id=self.endpoint["endpoint_id"], active_conversation_ref="conversation-a", title="Synthetic notification test")
        self.claim_args = {"user_subject": "user-a", "agent_host": "openclaw", "endpoint_key": "telegram:a"}
        self.drain()

    def drain(self):
        rows = self.store.claim_outbox(user_subject="user-a", endpoint_id=self.endpoint["endpoint_id"], limit=100)
        for row in rows:
            self.store.acknowledge_outbox(delivery_id=row["delivery_id"], user_subject="user-a", endpoint_id=self.endpoint["endpoint_id"], succeeded=True)

    def version(self):
        return notices.current_version(self.service.db_path, user_subject="user-a", topic="notifications")

    def append(self, connection):
        return self.store._append_event(connection, task_id=self.task["task_id"], user_subject="user-a",
            event_type="task.interaction.completed", payload={"state": "completed", "interactionId": "synthetic-interaction"},
            causation_ref="synthetic-decision", created_at="2026-10-16T04:00:00+00:00")

    async def test_commit_wakes_only_after_durable_rows_are_visible(self):
        version = self.version()
        waiter = asyncio.create_task(notices.wait_for_change_async(self.service.db_path, user_subject="user-a",
            topic="notifications", after_version=version, timeout=1))
        await asyncio.sleep(0)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event_id = self.append(connection)
            await asyncio.sleep(0)
            self.assertEqual(self.version(), version)
            self.assertFalse(waiter.done())
            with sqlite3.connect(self.service.db_path) as reader:
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM task_events WHERE event_id=?", (event_id,)).fetchone()[0], 0)
        self.assertTrue(await waiter)
        with sqlite3.connect(self.service.db_path) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM task_events WHERE event_id=?", (event_id,)).fetchone()[0], 1)

    async def test_rollback_discards_notice_and_durable_event(self):
        version = self.version()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                event_id = self.append(connection)
                raise RuntimeError("rollback")
        self.assertEqual(self.version(), version)
        self.assertFalse(await notices.wait_for_change_async(self.service.db_path, user_subject="user-a",
            topic="notifications", after_version=version, timeout=.01))
        with sqlite3.connect(self.service.db_path) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM task_events WHERE event_id=?", (event_id,)).fetchone()[0], 0)

    async def test_shared_draft_transaction_owner_publishes_task_events_after_commit(self):
        version = self.version()
        with self.service.business_drafts._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.append(connection)
            self.assertEqual(self.version(), version)
        self.assertGreater(self.version(), version)

    async def test_broken_hint_delivery_keeps_durable_decision_successful(self):
        authorization, _, token = self.authorization()
        with patch.object(notices, "publish_change", side_effect=RuntimeError("synthetic hint delivery failure")):
            decided = self.service.write_authorizations.decide(authorization["authorization_id"], decision="approve", csrf_token=token, csrf_cookie=token)
        self.assertEqual(decided["state"], "approved")
        self.assertEqual(self.service.claim_host_notifications(**self.claim_args)["count"], 1)

    async def test_waiting_claim_holds_no_database_lock_and_wakes_promptly(self):
        pending = asyncio.create_task(_claim_host_notifications_with_wait(self.service, wait_seconds=5, **self.claim_args))
        await asyncio.sleep(.05)
        self.assertFalse(pending.done())
        with sqlite3.connect(self.service.db_path, timeout=.05) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.append(connection)
        response = await asyncio.wait_for(pending, timeout=1)
        self.assertTrue(response["waitSupported"])
        self.assertEqual(response["waitSeconds"], 5)
        self.assertEqual(response["count"], 1)
        self.assertLess(response["waitedMs"], 1000)

    async def test_missed_or_cross_process_hint_recovers_from_durable_outbox(self):
        pending = asyncio.create_task(_claim_host_notifications_with_wait(self.service, wait_seconds=3, **self.claim_args))
        await asyncio.sleep(.05)
        with patch.dict("os.environ", {"AGENTBRIDGE_NOTIFICATION_WAKEUPS": "0"}):
            with self.store._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self.append(connection)
        response = await asyncio.wait_for(pending, timeout=3)
        self.assertEqual(response["count"], 1)
        self.assertGreaterEqual(response["waitedMs"], 1500)
        self.assertLess(response["waitedMs"], 3000)

    async def test_identity_partition_and_cancellation_do_not_leak_waiters(self):
        version = self.version()
        pending = asyncio.create_task(notices.wait_for_change_async(self.service.db_path, user_subject="user-a",
            topic="notifications", after_version=version, timeout=20))
        await asyncio.sleep(0)
        notices.publish_change(self.service.db_path, user_subject="user-b", topic="notifications")
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertNotIn(notices._key(self.service.db_path, "user-a", "notifications"), notices._waiters)

    async def test_many_longpolls_leave_executor_threads_available(self):
        pending = [asyncio.create_task(_claim_host_notifications_with_wait(self.service, wait_seconds=20, **self.claim_args)) for _ in range(40)]
        try:
            await asyncio.sleep(.15)
            self.assertEqual(await asyncio.wait_for(asyncio.to_thread(lambda: 42), timeout=2), 42)
            self.assertFalse(any(item.done() for item in pending))
        finally:
            for item in pending:
                item.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def authorization(self):
        operation, _ = self.service.operations.create(user_subject="user-a", capability_name="oa.business_trip.prepare",
            capability_version="0.1.0", input_summary={})
        authorization = _authorization(self.service.write_authorizations, prepare_operation_id=operation["operation_id"])
        envelope = self.service._execution_authorization_interaction(authorization)
        record = self.service.interactions.get(envelope["interactionId"], user_subject="user-a")
        self.store.link_interaction(task_id=self.task["task_id"], user_subject="user-a", interaction_record=record, interaction=envelope)
        self.drain()
        token = self.service.write_authorizations.issue_csrf(authorization["authorization_id"])
        return authorization, envelope, token

    async def test_authorization_decision_persists_exact_event_once_and_survives_restart(self):
        authorization, envelope, token = self.authorization()
        decided = self.service.write_authorizations.decide(authorization["authorization_id"], decision="approve", csrf_token=token, csrf_cookie=token)
        self.assertEqual(decided["state"], "approved")
        with self.assertRaises(WriteAuthorizationStateError):
            self.service.write_authorizations.decide(authorization["authorization_id"], decision="approve", csrf_token=token, csrf_cookie=token)
        restarted = authorized_service(home=Path(self.temp.name), base_url="http://oa.example.test/seeyon/main.do?method=main")
        response = restarted.claim_host_notifications(**self.claim_args)
        self.assertEqual(response["count"], 1)
        notification = response["notifications"][0]
        self.assertEqual(notification["event"]["eventType"], "task.interaction.completed")
        self.assertEqual(notification["event"]["payload"]["interactionId"], envelope["interactionId"])
        self.assertEqual(notification["task"]["taskId"], self.task["task_id"])
        self.assertEqual(restarted.write_authorizations.get(authorization["authorization_id"])["state"], "approved")
        with sqlite3.connect(self.service.db_path) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM write_authorizations WHERE state='consumed'").fetchone()[0], 0)

    async def test_decision_event_failure_rolls_back_authorization_and_emits_no_hint(self):
        authorization, _, token = self.authorization()
        version = self.version()
        original = self.store._append_event
        def broken(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic event failure")
        with patch.object(self.store, "_append_event", side_effect=broken):
            with self.assertRaisesRegex(RuntimeError, "synthetic event failure"):
                self.service.write_authorizations.decide(authorization["authorization_id"], decision="approve", csrf_token=token, csrf_cookie=token)
        self.assertEqual(self.service.write_authorizations.get(authorization["authorization_id"])["state"], "pending")
        self.assertEqual(self.version(), version)
        self.assertEqual(self.service.claim_host_notifications(**self.claim_args)["count"], 0)

    async def test_pure_workspace_interaction_wakes_without_companion_or_outbox(self):
        with self.store._connect() as connection:
            connection.execute("UPDATE client_endpoints SET state='inactive' WHERE endpoint_id=?", (self.endpoint["endpoint_id"],))
        self.endpoint, _ = self.store.ensure_endpoint(user_subject="user-a", token_id="workspace-token", agent_host="openclaw",
            endpoint_key="workspace:a", client_type="web", external_subject="a", conversation_ref="workspace:a",
            capabilities=["workspace.timeline.read"])
        self.task, _ = self.store.ensure_task(user_subject="user-a", agent_host="openclaw", host_task_key="workspace-task",
            origin_endpoint_id=self.endpoint["endpoint_id"], active_conversation_ref="workspace:a", title="Pure Workspace")
        authorization, envelope, token = self.authorization()
        pending = asyncio.create_task(_get_interaction_with_wait(self.service, user_subject="user-a",
            interaction_id=envelope["interactionId"], wait_seconds=5))
        await asyncio.sleep(.05)
        self.assertFalse(pending.done())
        self.service.write_authorizations.decide(authorization["authorization_id"], decision="approve", csrf_token=token, csrf_cookie=token)
        response = await asyncio.wait_for(pending, timeout=1)
        self.assertEqual(response["interaction"]["state"], "completed")
        self.assertTrue(response["interaction"]["resume"]["ready"])
        self.assertLess(response["waitedMs"], 1000)
        with sqlite3.connect(self.service.db_path) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM notification_outbox WHERE task_id=?", (self.task["task_id"],)).fetchone()[0], 0)

    async def test_unrelated_hint_does_not_complete_pending_interaction(self):
        _, envelope, _ = self.authorization()
        pending = asyncio.create_task(_get_interaction_with_wait(self.service, user_subject="user-a",
            interaction_id=envelope["interactionId"], wait_seconds=.2))
        await asyncio.sleep(.03)
        notices.publish_change(self.service.db_path, user_subject="user-a", topic="notifications")
        await asyncio.sleep(.03)
        self.assertFalse(pending.done())
        response = await asyncio.wait_for(pending, timeout=1)
        self.assertEqual(response["interaction"]["state"], "pending")
        self.assertFalse(response["interaction"]["resume"]["ready"])


if __name__ == "__main__":
    unittest.main()
