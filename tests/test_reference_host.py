from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest

import httpx


ROOT = Path(__file__).parents[1]
REFERENCE_ROOT = ROOT / "integrations" / "reference-host"
if str(REFERENCE_ROOT) not in sys.path:
    sys.path.insert(0, str(REFERENCE_ROOT))

from reference_host.artifact_driver import ArtifactDriver
from reference_host.app import ReferenceHostHttpServer
from reference_host.conformance import CASES, ConformanceReport, validate_case_ids
from reference_host.host_profile import (
    HOST_CONTEXT_META_KEY,
    PRIVATE_INTERACTION_META_KEY,
    TASK_CONTEXT_META_KEY,
)
from reference_host.identity import IdentityConfig, IdentityRegistry
from reference_host.mcp_client import AgentBridgeMcpClient, McpCallResult
from reference_host.recovery import ReferenceHostRecovery
from reference_host.runtime_collector import ReferenceHostRuntimeCollector
from reference_host.state import ReferenceHostState
from reference_host.task_driver import ReferenceTaskDriver


def result(
    payload: dict | None = None,
    *,
    private: dict | None = None,
    is_error: bool = False,
) -> McpCallResult:
    return McpCallResult(
        content=[],
        structured=payload or {},
        private_meta=private or {},
        is_error=is_error,
    )


class ScriptedClient:
    def __init__(
        self,
        *,
        interactive: bool = False,
        lease_owner: str = "reference-host-test",
        artifact_status: str = "READY",
        runtime_rejected: bool = False,
        recovery_items: list[dict] | None = None,
    ) -> None:
        self.interactive = interactive
        self.lease_owner = lease_owner
        self.artifact_status = artifact_status
        self.runtime_rejected = runtime_rejected
        self.recovery_items = recovery_items or []
        self.calls: list[tuple[str, dict, dict]] = []
        self.business_calls = 0
        self.resume_calls = 0
        self.artifact_url = "https://agentbridge.invalid/download/secret"
        self.interaction_url = "https://agentbridge.invalid/input/secret"

    async def list_tools(self) -> list[dict]:
        return [
            {
                "name": "oa_workflow_pending_list",
                "title": "读取 OA 待办",
                "description": "测试读取",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            }
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        meta: dict | None = None,
        recovery_class: str = "unsafe",
    ) -> McpCallResult:
        arguments = dict(arguments or {})
        meta = dict(meta or {})
        self.calls.append((name, arguments, meta))
        if name == "agentbridge_server_profile":
            return result(
                {
                    "negotiation": {"acceptedLevel": "L3"},
                    "planning": {
                        "schemaVersion": "agentbridge.composed-task-planning-policy.v1",
                        "modelContext": "Use a durable plan for source-dependent results.",
                    },
                    "agentToolAccess": {
                        "allowedToolNames": ["oa_workflow_pending_list"]
                    },
                }
            )
        if name == "agentbridge_host_identity_profile":
            return result(
                {
                    "host": {"acceptedLevel": "L3"},
                    "agentToolAccess": {
                        "allowedToolNames": ["oa_workflow_pending_list"]
                    },
                }
            )
        if name == "agentbridge_host_task_ensure":
            return result(
                {
                    "task": {"taskId": "task-reference-1234567890"},
                    "endpoint": {"endpointId": "endpoint-reference-1234567890"},
                    "coordinatorLease": {
                        "hostInstanceId": "reference-host-test",
                        "version": 1,
                    },
                }
            )
        if name == "agentbridge_host_coordinator_lease_get":
            return result(
                {
                    "coordinatorLease": {
                        "hostInstanceId": self.lease_owner,
                        "version": 1,
                        "state": "active",
                    }
                }
            )
        if name == "agentbridge_host_coordinator_lease_acquire":
            return result(
                {
                    "coordinatorLease": {
                        "hostInstanceId": "reference-host-test",
                        "version": 1,
                        "state": "active",
                    }
                }
            )
        if name == "oa_workflow_pending_list":
            self.business_calls += 1
            if self.interactive and self.business_calls == 1:
                interaction = {
                    "interactionId": "interaction-reference-1234567890",
                    "type": "credential_login",
                    "state": "pending",
                    "title": "登录 OA",
                }
                return result(
                    {
                        "status": "requires_user_action",
                        "interaction": {
                            "interactionId": interaction["interactionId"]
                        },
                    },
                    private={PRIVATE_INTERACTION_META_KEY: interaction},
                )
            return result({"status": "succeeded", "result": {"count": 2}})
        if name == "agentbridge_host_task_observe":
            return result({"status": "succeeded"})
        if name == "agentbridge_host_interaction_present":
            return result(
                {
                    "interaction": {
                        "interactionId": "interaction-reference-1234567890",
                        "type": "credential_login",
                        "state": "pending",
                        "title": "登录 OA",
                        "presentation": {"url": self.interaction_url},
                    }
                }
            )
        if name == "agentbridge_interaction_get":
            return result(
                {
                    "status": "succeeded",
                    "interaction": {
                        "interactionId": "interaction-reference-1234567890",
                        "type": "credential_login",
                        "state": "completed",
                        "resume": {
                            "ready": True,
                            "completed": False,
                            "tool": "agentbridge_interaction_resume",
                        },
                    },
                }
            )
        if name == "agentbridge_interaction_resume":
            self.resume_calls += 1
            return result(
                {
                    "status": "succeeded",
                    "nextAction": {"type": "retry_original_request"},
                }
            )
        if name == "agentbridge_host_task_snapshot":
            return result(
                {
                    "status": "succeeded",
                    "task": {
                        "taskId": "task-reference-1234567890",
                        "status": "active",
                    },
                    "events": [],
                    "artifacts": [
                        {
                            "artifactId": "artifact-reference-1234567890",
                            "fileName": "证书.pdf",
                            "mediaType": "application/pdf",
                            "size": 123,
                            "status": self.artifact_status,
                            "downloadUrl": self.artifact_url,
                            "regenerable": True,
                        }
                    ],
                }
            )
        if name == "agentbridge_host_task_recovery_list":
            return result({"status": "succeeded", "recoveries": self.recovery_items})
        if name == "agentbridge_host_runtime_snapshot" and self.runtime_rejected:
            return result(
                {
                    "status": "failed",
                    "error": {"code": "HOST_REGISTRATION_REQUIRED"},
                },
                is_error=True,
            )
        if name in {
            "agentbridge_host_task_finish",
            "agentbridge_host_runtime_snapshot",
            "agentbridge_host_artifact_reissue",
        }:
            return result({"status": "succeeded"})
        raise AssertionError(f"unexpected fake MCP call: {name}")


class ReferenceHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.identity = IdentityConfig(
            label="用户甲",
            token_env="REFERENCE_HOST_TEST_TOKEN",
            endpoint_key="reference-host:user-a",
            external_subject="reference-host:user-a",
            conversation_ref="reference-host:conversation:user-a",
        )
        os.environ["REFERENCE_HOST_TEST_TOKEN"] = "secret-test-token"

    def tearDown(self) -> None:
        os.environ.pop("REFERENCE_HOST_TEST_TOKEN", None)
        self.temp.cleanup()

    def driver(self, client: ScriptedClient) -> ReferenceTaskDriver:
        state = ReferenceHostState(Path(self.temp.name) / "state.db")
        driver = ReferenceTaskDriver(
            identities=IdentityRegistry([self.identity]),
            state=state,
            mcp_url="https://agentbridge.invalid/mcp",
            poll_interval_seconds=0.05,
            maximum_interaction_wait_seconds=5,
        )
        driver.instance_id = "reference-host-test"
        driver._clients[self.identity.label] = client
        return driver

    async def test_read_task_uses_registered_l3_context_and_finishes(self) -> None:
        client = ScriptedClient()
        driver = self.driver(client)
        task = await driver.start_task(
            identity_label=self.identity.label,
            tool_name="oa_workflow_pending_list",
            arguments={},
        )
        self.assertEqual("succeeded", task["status"])
        business = next(call for call in client.calls if call[0] == "oa_workflow_pending_list")
        self.assertEqual(
            "task-reference-1234567890",
            business[2][TASK_CONTEXT_META_KEY]["taskId"],
        )
        self.assertEqual(
            "reference-host-test",
            business[2][HOST_CONTEXT_META_KEY]["hostInstanceId"],
        )
        self.assertTrue(
            any(call[0] == "agentbridge_host_task_finish" for call in client.calls)
        )
        await driver.close()

    async def test_identity_profile_preserves_central_planning_policy(self) -> None:
        client = ScriptedClient()
        driver = self.driver(client)

        profile = await driver.ensure_identity(self.identity.label)

        self.assertEqual(
            profile["planning"]["schemaVersion"],
            "agentbridge.composed-task-planning-policy.v1",
        )
        await driver.close()

    async def test_login_completion_resumes_once_and_replays_read(self) -> None:
        client = ScriptedClient(interactive=True)
        driver = self.driver(client)
        task = await driver.start_task(
            identity_label=self.identity.label,
            tool_name="oa_workflow_pending_list",
            arguments={},
        )
        self.assertEqual("waiting_user", task["status"])
        for _ in range(100):
            current = driver.view_task(task["localTaskId"])
            if current["status"] == "succeeded":
                break
            await asyncio.sleep(0.02)
        self.assertEqual("succeeded", current["status"])
        self.assertEqual(1, client.resume_calls)
        self.assertEqual(2, client.business_calls)
        resume = next(call for call in client.calls if call[0] == "agentbridge_interaction_resume")
        self.assertEqual("1", resume[2][TASK_CONTEXT_META_KEY]["coordinatorLeaseVersion"])
        state_bytes = (Path(self.temp.name) / "state.db").read_bytes()
        self.assertNotIn(client.interaction_url.encode(), state_bytes)
        await driver.close()

    async def test_other_host_lease_is_observe_only(self) -> None:
        client = ScriptedClient(lease_owner="openclaw-gateway")
        driver = self.driver(client)
        await driver.ensure_identity(self.identity.label)
        local = driver.state.create_task(
            identity_label=self.identity.label,
            tool_name="oa_workflow_pending_list",
            conversation_ref=self.identity.conversation_ref,
            title="恢复测试",
            restartable_read_arguments={},
        )
        driver.state.bind_central_task(
            local_task_id=local["local_task_id"],
            task_id="task-reference-1234567890",
            endpoint_id="endpoint-reference-1234567890",
            lease_version=1,
        )
        recovery = ReferenceHostRecovery(driver)
        outcome = await recovery._recover_bound_task(local["local_task_id"])
        self.assertEqual("observe_only", outcome)
        self.assertEqual(
            "observe_only",
            driver.state.get_task(local["local_task_id"])["status"],
        )
        self.assertFalse(
            any(call[0] == "agentbridge_interaction_resume" for call in client.calls)
        )
        await driver.close()

    async def test_artifact_url_stays_private_and_can_be_refreshed(self) -> None:
        client = ScriptedClient()
        driver = self.driver(client)
        task = await driver.start_task(
            identity_label=self.identity.label,
            tool_name="oa_workflow_pending_list",
            arguments={},
        )
        artifacts = task["artifacts"]
        self.assertEqual(1, len(artifacts))
        self.assertNotIn("downloadUrl", artifacts[0])
        url = await driver.artifacts.download_url(
            task["localTaskId"],
            artifacts[0]["artifactId"],
        )
        self.assertEqual(client.artifact_url, url)
        await driver.artifacts.reissue(
            task["localTaskId"],
            artifacts[0]["artifactId"],
        )
        await driver.close()

    async def test_expired_artifact_keeps_reissue_but_not_download_url(self) -> None:
        client = ScriptedClient(artifact_status="EXPIRED")
        driver = self.driver(client)
        task = await driver.start_task(
            identity_label=self.identity.label,
            tool_name="oa_workflow_pending_list",
            arguments={},
        )
        artifact = task["artifacts"][0]
        self.assertEqual("EXPIRED", artifact["status"])
        self.assertTrue(artifact["regenerable"])
        with self.assertRaises(KeyError):
            await driver.artifacts.download_url(
                task["localTaskId"],
                artifact["artifactId"],
            )
        await driver.close()

    async def test_runtime_rejection_is_reported_without_failing_other_work(self) -> None:
        client = ScriptedClient(runtime_rejected=True)
        driver = self.driver(client)
        await driver.ensure_identity(self.identity.label)
        collector = ReferenceHostRuntimeCollector(driver, interval_seconds=60)

        report = await collector.collect_once()

        self.assertEqual("failed", report[0]["status"])
        self.assertEqual("HOST_REGISTRATION_REQUIRED", report[0]["errorCode"])
        await driver.close()

    async def test_incomplete_remote_recovery_does_not_create_orphan_task(self) -> None:
        client = ScriptedClient(
            recovery_items=[
                {
                    "task": {
                        "taskId": "task-incomplete-recovery-12345",
                        "title": "缺少端点的恢复项",
                    }
                }
            ]
        )
        driver = self.driver(client)
        await driver.ensure_identity(self.identity.label)

        report = await ReferenceHostRecovery(driver).recover_identity(
            self.identity.label
        )

        self.assertEqual(
            ["central:task-incomplete-recovery-12345"],
            report["failed"],
        )
        self.assertEqual([], driver.state.list_tasks())
        await driver.close()

    async def test_one_identity_failure_does_not_block_another_identity(self) -> None:
        second = IdentityConfig(
            label="用户乙",
            token_env="REFERENCE_HOST_TEST_TOKEN_B",
            endpoint_key="reference-host:user-b",
            external_subject="reference-host:user-b",
            conversation_ref="reference-host:conversation:user-b",
        )
        os.environ["REFERENCE_HOST_TEST_TOKEN_B"] = "secret-test-token-b"
        driver = ReferenceTaskDriver(
            identities=IdentityRegistry([self.identity, second]),
            state=ReferenceHostState(Path(self.temp.name) / "partial.db"),
            mcp_url="https://agentbridge.invalid/mcp",
        )
        driver.instance_id = "reference-host-test"
        driver._clients[self.identity.label] = ScriptedClient()

        class BrokenClient:
            async def call_tool(self, *_args, **_kwargs):
                raise RuntimeError("identity registration rejected")

        driver._clients[second.label] = BrokenClient()
        try:
            initialized = await driver.initialize()
        finally:
            os.environ.pop("REFERENCE_HOST_TEST_TOKEN_B", None)
        self.assertEqual([self.identity.label], initialized["ready"])
        self.assertEqual("RUNTIMEERROR", initialized["failed"][second.label])
        await driver.close()

    async def test_transport_retry_is_bounded_by_side_effect_class(self) -> None:
        token = lambda: "test-token"
        client = AgentBridgeMcpClient(
            mcp_url="https://agentbridge.invalid/mcp",
            token_provider=token,
        )
        attempts = 0

        async def flaky(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ReadTimeout("temporary")
            return result({"status": "succeeded"})

        client._call_tool_once = flaky
        recovered = await client.call_tool("read", {}, recovery_class="read")
        self.assertEqual(3, recovered.attempts)
        attempts = 0
        with self.assertRaises(httpx.ReadTimeout):
            await client.call_tool("write", {}, recovery_class="unsafe")
        self.assertEqual(1, attempts)

    async def test_state_separates_identities_and_strips_secrets(self) -> None:
        state = ReferenceHostState(Path(self.temp.name) / "isolated.db")
        first = state.create_task(
            identity_label="用户甲",
            tool_name="read",
            conversation_ref="a",
            title="甲",
            restartable_read_arguments={
                "query": "照明",
                "password": "never-store",
                "source_url": "https://secret.invalid",
            },
        )
        state.create_task(
            identity_label="用户乙",
            tool_name="read",
            conversation_ref="b",
            title="乙",
        )
        stored = state.get_task(first["local_task_id"])
        self.assertEqual({"query": "照明"}, stored["restartable_read_arguments"])
        self.assertEqual(1, len(state.list_tasks(identity_label="用户甲")))
        self.assertEqual(1, len(state.list_tasks(identity_label="用户乙")))

    async def test_conformance_catalog_requires_all_h01_h29(self) -> None:
        expected = {f"H{number:02d}" for number in range(1, 30)}
        validate_case_ids(case.case_id for case in CASES)
        self.assertEqual(expected, {case.case_id for case in CASES})
        report = ConformanceReport(host_name="reference-host", host_version="0.1.0")
        for case in CASES:
            report.record(case.case_id, passed=True, evidence="unit-test")
        report.require_complete()
        self.assertTrue(report.as_dict()["passed"])

    async def test_remote_ui_requires_one_time_bootstrap_token(self) -> None:
        runtime = SimpleNamespace(
            driver=SimpleNamespace(instance_id="reference-host-ui-test")
        )
        server = ReferenceHostHttpServer(
            ("127.0.0.1", 0),
            runtime,
            ui_token="one-time-ui-secret",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with httpx.Client(base_url=base_url, follow_redirects=False) as client:
                health = client.get("/healthz")
                denied = client.get("/")
                bootstrap = client.get("/?access_token=one-time-ui-secret")
                allowed = client.get(bootstrap.headers["location"])
                favicon = client.get("/favicon.ico")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(200, health.status_code)
        self.assertEqual(401, denied.status_code)
        self.assertEqual(303, bootstrap.status_code)
        self.assertEqual(200, allowed.status_code)
        self.assertEqual(204, favicon.status_code)
        self.assertIn("AgentBridge Reference Host", allowed.text)
        self.assertIn("frame-ancestors 'none'", allowed.headers["content-security-policy"])

    async def test_mcp_client_reuses_sessions_per_lane_rotates_token_and_never_retries_unsafe(self) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import patch

        token_box = ["tok-1"]
        opened: list[tuple[str, str]] = []
        closed: list[int] = []
        fail_next: list[Exception | None] = []

        class FakeSession:
            def __init__(self, idx: int) -> None:
                self.idx = idx
                self.calls: list[tuple[str, dict]] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def initialize(self) -> None:
                return None

            async def list_tools(self):
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            model_dump=lambda **_kw: {
                                "name": "oa_workflow_pending_list",
                                "annotations": {"readOnlyHint": True},
                            }
                        )
                    ]
                )

            async def call_tool(self, name, arguments, *, read_timeout_seconds=None, meta=None):
                self.calls.append((name, dict(arguments or {})))
                if fail_next:
                    err = fail_next.pop(0)
                    if err is not None:
                        raise err
                return SimpleNamespace(
                    content=[],
                    isError=False,
                    model_dump=lambda **_kw: {"structuredContent": {"sessionIdx": self.idx, "tool": name}},
                )

        @asynccontextmanager
        async def fake_stream(url, *, headers, **_kwargs):
            idx = len(opened) + 1
            opened.append((url, headers["Authorization"]))
            try:
                yield ("read", "write", f"sid-{idx}")
            finally:
                closed.append(idx)

        def fake_client_session(_read, _write):
            return FakeSession(len(opened))

        with (
            patch("reference_host.mcp_client.streamablehttp_client", fake_stream),
            patch("reference_host.mcp_client.ClientSession", fake_client_session),
        ):
            client = AgentBridgeMcpClient(
                mcp_url="https://agentbridge.invalid/mcp",
                token_provider=lambda: token_box[0],
            )
            try:
                # 1. list_tools and default call_tool reuse the same default session.
                tools = await client.list_tools()
                self.assertEqual("oa_workflow_pending_list", tools[0]["name"])
                r1 = await client.call_tool("oa_session_status", {}, recovery_class="read")
                self.assertEqual(1, r1.payload()["sessionIdx"])
                self.assertEqual(1, len(opened))

                # 2. Positive wait_seconds routes to isolated long_poll lane.
                r_poll = await client.call_tool(
                    "agentbridge_interaction_get",
                    {"interaction_id": "inter-1234567890123456", "wait_seconds": 15.0},
                    recovery_class="read",
                )
                self.assertEqual(2, r_poll.payload()["sessionIdx"])
                self.assertEqual(2, len(opened))

                # 3. Default lane stays warm while long_poll lane exists.
                r2 = await client.call_tool("oa_session_status", {}, recovery_class="read")
                self.assertEqual(1, r2.payload()["sessionIdx"])
                self.assertEqual(2, len(opened))

                # 4. Token rotation closes old session and opens a fresh one immediately.
                token_box[0] = "tok-2"
                r3 = await client.call_tool("oa_session_status", {}, recovery_class="read")
                self.assertEqual(3, r3.payload()["sessionIdx"])
                self.assertIn(1, closed)

                # 5. Transient error on read evicts broken session and retries on a fresh session.
                fail_next.append(httpx.RemoteProtocolError("stale"))
                r4 = await client.call_tool("oa_session_status", {}, recovery_class="read")
                self.assertEqual(4, r4.payload()["sessionIdx"])
                self.assertEqual(2, r4.attempts)
                self.assertIn(3, closed)

                # 6. Transient error on unsafe evicts session and NEVER retries!
                fail_next.append(httpx.RemoteProtocolError("lost-during-commit"))
                with self.assertRaises(httpx.RemoteProtocolError):
                    await client.call_tool("agentbridge_interaction_resume", {}, recovery_class="unsafe")
                self.assertIn(4, closed)
                self.assertEqual(4, len(opened))
            finally:
                await client.close()
            self.assertEqual(len(opened), len(closed))

    async def test_interaction_driver_passes_bounded_wait_seconds_and_collector_waits_initial(self) -> None:
        client = ScriptedClient(interactive=True)
        driver = self.driver(client)
        collector = ReferenceHostRuntimeCollector(driver, interval_seconds=60)
        collector.start()
        await collector.wait_initial(timeout=2.0)
        await collector.stop()
        snapshot_calls = [c for c in client.calls if c[0] == "agentbridge_host_runtime_snapshot"]
        self.assertEqual(1, len(snapshot_calls))

        task = await driver.start_task(
            identity_label=self.identity.label,
            tool_name="oa_workflow_pending_list",
            arguments={},
        )
        for _ in range(100):
            current = driver.view_task(task["localTaskId"])
            if current["status"] == "succeeded":
                break
            await asyncio.sleep(0.02)
        self.assertEqual("succeeded", current["status"])
        poll_call = next(c for c in client.calls if c[0] == "agentbridge_interaction_get")
        self.assertIn("wait_seconds", poll_call[1])
        self.assertGreater(poll_call[1]["wait_seconds"], 0.0)
        self.assertLessEqual(poll_call[1]["wait_seconds"], 15.0)
        await driver.close()


if __name__ == "__main__":
    unittest.main()
