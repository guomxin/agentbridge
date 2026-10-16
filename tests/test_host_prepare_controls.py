from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.core.host_contract import HostLeaseConflict
from agentbridge.core.tasks import TaskIntegrityError
from agentbridge.core.store_transactions import store_transaction
from agentbridge.workspace.stores import WorkspaceLinkError
from tests.authorization_fixtures import authorized_service
from tests.test_workspace import _create_account


class HostPrepareControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = authorized_service(home=self.temp.name, base_url="http://oa.example.test/seeyon")
        self.account = _create_account(self.service, user_subject="user-a", username="alice", endpoint_key="telegram:a")
        root = Path(__file__).resolve().parents[1]
        profile = deepcopy(json.loads((root / "schemas/agent-host/v1/test-vectors.json").read_text())["profiles"][0]["value"])
        profile["hostInstanceId"] = "openclaw-gateway"
        profile["implementation"] = {"name": "openclaw", "version": json.loads((root / "integrations/openclaw-agentbridge/package.json").read_text())["version"]}
        self.service.host_contract.negotiate(user_subject="user-a", token_id="token-a", profile=profile)
        self.registration = self.service.require_host_registration(user_subject="user-a", agent_host="openclaw",
            host_instance_id="openclaw-gateway", host_version=profile["implementation"]["version"], minimum_level="L3")
        self.ensure_args = dict(user_subject="user-a", token_id="token-a", agent_host="openclaw", host_task_key="prepare-test",
            endpoint_key=self.account["endpoint_key"], client_type="web", external_subject=self.account["account_id"],
            conversation_ref=self.account["openclaw_session_key"], title="Synthetic selected prepare",
            host_instance_id="openclaw-gateway", host_version=profile["implementation"]["version"])

    def prepare_args(self):
        result = self.service.ensure_host_task(**self.ensure_args)
        return dict(user_subject="user-a", task_id=result["task"]["taskId"], command_key="fixed-command",
            session_key=self.account["openclaw_session_key"], endpoint_key=self.account["endpoint_key"], registration=self.registration,
            runtime_context={"coordinatorLeaseVersion": str(result["coordinatorLease"]["version"]),
                "hostRunId": "fixed-prepare", "requestId": "request-one"})

    def test_task_and_lease_are_committed_together_with_a_fresh_full_lease(self):
        original = self.service.host_contract.acquire_coordinator_lease
        def inspect(*args, **kwargs):
            lease = original(*args, **kwargs)
            self.assertTrue(kwargs["connection"].in_transaction)
            with sqlite3.connect(self.service.db_path) as reader:
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM agent_tasks WHERE host_task_key='prepare-test'").fetchone()[0], 0)
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM agent_task_coordinator_leases WHERE task_id=?", (lease["taskId"],)).fetchone()[0], 0)
            return lease
        with patch.object(self.service.host_contract, "acquire_coordinator_lease", side_effect=inspect):
            result = self.service.ensure_host_task(**self.ensure_args)
        self.assertEqual(result["coordinatorLease"]["taskId"], result["task"]["taskId"])
        self.assertGreater((datetime.fromisoformat(result["coordinatorLease"]["expiresAt"]) - datetime.now(timezone.utc)).total_seconds(), 590)

    def test_lease_failure_rolls_back_new_task_and_events(self):
        with patch.object(self.service.host_contract, "acquire_coordinator_lease", side_effect=HostLeaseConflict("synthetic ownership conflict")):
            with self.assertRaises(HostLeaseConflict):
                self.service.ensure_host_task(**self.ensure_args)
        self.assertIsNone(self.service.tasks.find_host_task(user_subject="user-a", agent_host="openclaw", host_task_key="prepare-test"))
        with sqlite3.connect(self.service.db_path) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM task_events WHERE payload_json LIKE '%Synthetic selected prepare%'").fetchone()[0], 0)

    def test_existing_connection_does_not_commit_task_early(self):
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with store_transaction(self.service.tasks) as connection:
                task, _ = self.service.tasks.ensure_task(user_subject="user-a", agent_host="openclaw", host_task_key="shared-create",
                    origin_endpoint_id=self.account["endpoint_id"], active_conversation_ref=self.account["openclaw_session_key"],
                    title="Shared task", connection=connection)
                self.assertTrue(connection.in_transaction)
                raise RuntimeError("rollback")
        self.assertIsNone(self.service.tasks.find_host_task(user_subject="user-a", agent_host="openclaw", host_task_key="shared-create"))

    def test_verified_prepare_passes_only_the_exact_owned_origin_and_runtime(self):
        arguments = self.prepare_args()
        with patch("agentbridge.core.actionable_results.execute_preparation", return_value={"status": "requires_user_action"}) as execute:
            self.assertEqual(self.service.prepare_host_task(**arguments)["status"], "requires_user_action")
        runtime = execute.call_args.kwargs["runtime_context"]
        self.assertEqual(runtime["endpointId"], self.account["endpoint_id"])
        self.assertEqual(runtime["hostInstanceId"], "openclaw-gateway")
        self.assertEqual(runtime["hostType"], "openclaw")
        self.assertEqual(execute.call_args.args[1:4], ("user-a", arguments["task_id"], "fixed-command"))

    def test_wrong_origin_user_session_endpoint_or_lease_never_executes(self):
        base = self.prepare_args()
        cases = [{"user_subject": "user-b"}, {"session_key": "agent:main:workspace:other"},
            {"endpoint_key": "workspace:other"}, {"runtime_context": {}},
            {"runtime_context": {"coordinatorLeaseVersion": "2"}},
            {"registration": {**self.registration, "hostInstanceId": "other-host"}}]
        with patch("agentbridge.core.actionable_results.execute_preparation") as execute:
            for change in cases:
                with self.subTest(change=change), self.assertRaises((ValueError, KeyError, PermissionError, TaskIntegrityError, WorkspaceLinkError)):
                    self.service.prepare_host_task(**{**base, **change})
            execute.assert_not_called()

    def test_disabled_workspace_account_never_executes_prepare(self):
        arguments = self.prepare_args()
        with self.service.workspace._connect() as connection:
            connection.execute("UPDATE workspace_accounts SET state='disabled' WHERE account_id=?", (self.account["account_id"],))
        with patch("agentbridge.core.actionable_results.execute_preparation") as execute:
            with self.assertRaises(WorkspaceLinkError):
                self.service.prepare_host_task(**arguments)
            execute.assert_not_called()


class HostPrepareMcpTests(unittest.TestCase):
    def test_private_prepare_requires_matching_task_and_fenced_lease_metadata(self):
        from tests.test_central_mcp import CentralMcpFixture, CentralMcpTests
        with CentralMcpFixture() as (service, _store, token, client):
            service.prepare_host_task.return_value = {"protocolVersion": "0.1", "status": "succeeded", "operationId": "operation-one"}
            params = {"name": "agentbridge_host_task_prepare", "arguments": {"task_id": "task-1234567890-abcdef",
                "command_key": "command-one", "session_key": "workspace-session", "endpoint_key": "workspace:account-one"}}
            host = {"version": "1", "agentHost": "openclaw", "hostInstanceId": "openclaw-gateway", "hostVersion": "0.4.65"}
            for index, task_meta in enumerate(({}, {"taskId": "different-task-id", "coordinatorLeaseVersion": "1"},
                                               {"taskId": "task-1234567890-abcdef"})):
                response = CentralMcpTests._request(client, "tools/call", request_id=7000 + index, token=token,
                    params={**params, "_meta": {"io.agentbridge/host-context": host, "io.agentbridge/task": task_meta}})
                self.assertTrue(response.json()["result"]["isError"])
            service.prepare_host_task.assert_not_called()
            response = CentralMcpTests._request(client, "tools/call", request_id=7010, token=token,
                params={**params, "_meta": {"io.agentbridge/host-context": host,
                    "io.agentbridge/task": {"taskId": "task-1234567890-abcdef", "coordinatorLeaseVersion": "1"}}})
            self.assertFalse(response.json()["result"]["isError"], response.json())
            self.assertEqual(service.prepare_host_task.call_args.kwargs["user_subject"], "user-a")
            self.assertEqual(service.prepare_host_task.call_args.kwargs["runtime_context"]["coordinatorLeaseVersion"], "1")


if __name__ == "__main__":
    unittest.main()
