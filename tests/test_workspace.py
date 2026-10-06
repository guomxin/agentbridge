from __future__ import annotations

from tests.authorization_fixtures import authorized_service
import base64
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import ssl
import threading
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bscli.core.central_service import CentralCapabilityService
from bscli.core.tasks import TaskNotFound
from bscli.workspace.application import (
    WorkspaceApplication,
    _validated_chat_attachments,
    _workspace_gateway_failure_text,
)
from bscli.workspace.gateway import (
    GatewayRequestError,
    OpenClawGatewayClient,
)
from bscli.workspace.server import (
    _CLIENT_DISCONNECT_ERRORS,
    _public_gateway_stream_error,
    create_workspace_http_server,
    validate_workspace_server_config,
)
from bscli.workspace.stores import (
    WorkspaceConflictError,
    WorkspaceLinkError,
    WorkspaceStore,
)


PASSWORD = "AgentBridge!Workspace9"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.stream_events = [
            {
                "type": "progress",
                "runId": "run-web-1",
                "kind": "tool",
                "phase": "start",
                "label": "正在检查 OA 登录状态",
            },
            {
                "type": "chat",
                "runId": "run-web-1",
                "state": "final",
                "text": "OA 登录状态有效。",
            },
        ]

    def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout_seconds: float = 30,
    ) -> dict:
        self.calls.append((method, params or {}))
        if method == "system.info":
            return {"version": "2026.7.1"}
        if method == "chat.history":
            return {
                "sessionId": "session-web",
                "messages": [
                    {
                        "role": "user",
                        "content": "读取我的 OA 待办",
                        "timestamp": 1_785_459_303_372,
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "当前有 3 条待办。"},
                            {"type": "tool", "name": "hidden"},
                        ],
                        "timestamp": "1785459333802",
                    },
                    {"role": "tool", "content": "not exposed"},
                ],
            }
        if method == "agentbridge.workspace.bind":
            return {"status": "bound"}
        if method == "chat.send":
            return {"status": "accepted", "runId": "run-web-1"}
        raise AssertionError(f"unexpected Gateway method: {method}")

    def stream(
        self,
        *,
        session_key: str,
        timeout_seconds: float = 30,
    ):
        self.calls.append(
            (
                "stream",
                {
                    "sessionKey": session_key,
                    "timeoutSeconds": timeout_seconds,
                },
            )
        )
        return iter(self.stream_events)

    def send_stream(
        self,
        *,
        session_key: str,
        endpoint_key: str,
        grant: str,
        binding_grant_provider=None,
        message: str,
        idempotency_key: str,
        attachments: list[dict] | None = None,
        timeout_seconds: float = 150,
        retry_before_accept: bool = True,
        abort_accepted_on_close: bool = True,
    ):
        self.calls.append(
            (
                "send_stream",
                {
                    "sessionKey": session_key,
                    "endpointKey": endpoint_key,
                    "grant": grant,
                    "bindingGrantRefreshConfigured": callable(
                        binding_grant_provider
                    ),
                    "message": message,
                    "idempotencyKey": idempotency_key,
                    "attachments": list(attachments or []),
                    "timeoutSeconds": timeout_seconds,
                    "retryBeforeAccept": retry_before_accept,
                    "abortAcceptedOnClose": abort_accepted_on_close,
                },
            )
        )
        return iter(
            [
                {
                    "type": "accepted",
                    "runId": idempotency_key,
                    "status": "started",
                },
                *[
                    {**event, "runId": idempotency_key}
                    for event in self.stream_events
                ],
            ]
        )

    def abort_chat(
        self,
        *,
        session_key: str,
        run_id: str | None = None,
        preserve_side_runs: bool = True,
        timeout_seconds: float = 8,
        raise_on_error: bool = True,
    ) -> dict:
        self.calls.append(
            (
                "chat.abort",
                {
                    "sessionKey": session_key,
                    "runId": run_id,
                    "preserveSideRuns": preserve_side_runs,
                    "timeoutSeconds": timeout_seconds,
                    "raiseOnError": raise_on_error,
                },
            )
        )
        return {"aborted": False}


class WorkspaceStoreTests(unittest.TestCase):
    def test_identity_link_account_session_and_one_use_gateway_grant(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account_a = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            account_b = _create_account(
                service,
                user_subject="user-b",
                username="bob",
                endpoint_key="openclaw-weixin:*:bob",
                client_type="openclaw-weixin",
            )

            self.assertNotEqual(
                account_a["openclaw_session_key"],
                account_b["openclaw_session_key"],
            )
            self.assertNotEqual(account_a["endpoint_id"], account_b["endpoint_id"])
            authenticated = service.workspace.authenticate(
                username="ALICE",
                password=PASSWORD,
            )
            self.assertEqual(authenticated["user_subject"], "user-a")
            self.assertIsNone(
                service.workspace.authenticate(
                    username="alice",
                    password="not-the-password",
                )
            )

            session = service.workspace.create_session(account_a["account_id"])
            verified = service.workspace.verify_session(
                session["session_token"],
                csrf_token=session["csrf_token"],
            )
            self.assertEqual(verified["user_subject"], "user-a")
            self.assertEqual(verified["created_at"], account_a["created_at"])
            self.assertIsNone(
                service.workspace.verify_session(
                    session["session_token"],
                    csrf_token="wrong",
                )
            )

            grant = service.workspace.issue_gateway_grant(account_a["account_id"])
            with self.assertRaises(PermissionError):
                service.workspace.redeem_gateway_grant(
                    grant=grant["grant"],
                    user_subject="user-b",
                    endpoint_key=grant["endpoint_key"],
                    session_key=grant["session_key"],
                )
            redeemed = service.redeem_workspace_gateway_grant(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_key=grant["endpoint_key"],
                session_key=grant["session_key"],
                grant=grant["grant"],
                turn_ref="request-123456",
            )
            self.assertEqual(redeemed["status"], "succeeded")
            self.assertEqual(
                redeemed["binding"]["turnRef"],
                "request-123456",
            )
            turn = service.workspace.resolve_gateway_turn(
                user_subject="user-a",
                endpoint_key=grant["endpoint_key"],
                session_key=grant["session_key"],
            )
            self.assertEqual(turn["turn_ref"], "request-123456")
            resolved = service.resolve_workspace_gateway_session(
                user_subject="user-a",
                agent_host="openclaw",
                session_key=account_a["openclaw_session_key"],
            )
            self.assertEqual(resolved["status"], "succeeded")
            self.assertEqual(
                resolved["binding"]["endpointKey"],
                account_a["endpoint_key"],
            )
            with self.assertRaises(WorkspaceLinkError):
                service.resolve_workspace_gateway_session(
                    user_subject="user-b",
                    agent_host="openclaw",
                    session_key=account_a["openclaw_session_key"],
                )
            with self.assertRaises(WorkspaceLinkError):
                service.workspace.redeem_gateway_grant(
                    grant=grant["grant"],
                    user_subject="user-a",
                    endpoint_key=grant["endpoint_key"],
                    session_key=grant["session_key"],
                )

    def test_workspace_user_turn_tasks_share_persisted_turn(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            grant = service.workspace.issue_gateway_grant(account["account_id"])
            service.redeem_workspace_gateway_grant(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_key=account["endpoint_key"],
                session_key=account["openclaw_session_key"],
                grant=grant["grant"],
                turn_ref="request-one",
            )
            common = {
                "user_subject": "user-a",
                "token_id": "token-a",
                "agent_host": "openclaw",
                "endpoint_key": account["endpoint_key"],
                "client_type": "web",
                "external_subject": account["account_id"],
                "conversation_ref": account["openclaw_session_key"],
                "account_id": account["account_id"],
            }

            first = service.ensure_host_task(
                **common,
                host_task_key="session|tool-search",
                title="Search certificate scans",
                task_scope="user_turn",
            )
            second = service.ensure_host_task(
                **common,
                host_task_key="session|tool-deliver",
                title="Deliver certificate scans",
                task_scope="user_turn",
            )
            independent = service.ensure_host_task(
                **common,
                host_task_key="session|tool-revoke",
                title="Revoke workflow",
                task_scope="independent",
            )

            self.assertEqual(first["task"]["taskId"], second["task"]["taskId"])
            self.assertNotEqual(
                first["task"]["taskId"],
                independent["task"]["taskId"],
            )
            stored = service.tasks.get_task(
                first["task"]["taskId"],
                user_subject="user-a",
            )
            self.assertEqual(
                stored["host_task_key"],
                f"{account['openclaw_session_key']}|workspace:request-one",
            )

            query_ids = []
            for index, tool in enumerate(["database_capabilities", "database_execute", "database_execute"]):
                query = service.ensure_host_task(
                    **common, host_task_key=f"session|query-{index}",
                    title="数据库查询", tool_name=tool,
                )
                query_id = query["task"]["taskId"]
                query_ids.append(query_id)
                service.finish_host_task(user_subject="user-a", task_id=query_id, outcome="succeeded")
                saved_query = service.tasks.get_task(query_id, user_subject="user-a")
                self.assertEqual(saved_query["summary"]["workspaceQueryGroup"], {
                    "turnRef": "request-one", "toolName": tool,
                })
                self.assertEqual(saved_query["status"], "succeeded")
            self.assertEqual(len(set(query_ids)), 3)
            self.assertNotIn("workspaceQueryGroup", stored["summary"])

            service.finish_host_task(
                user_subject="user-a", task_id=first["task"]["taskId"],
                outcome="failed", error_code="PLAN_REQUIRED",
            )
            repair = service.ensure_host_task(
                **common, host_task_key="session|request-one:plan-repair",
                title="Prepare task plan", task_scope="independent",
            )
            repeated_repair = service.ensure_host_task(
                **common, host_task_key="session|request-one:plan-repair",
                title="Prepare task plan", task_scope="independent",
            )
            self.assertNotEqual(first["task"]["taskId"], repair["task"]["taskId"])
            self.assertEqual(repair["task"]["taskId"], repeated_repair["task"]["taskId"])
            self.assertNotEqual(repair["task"]["status"], "failed")
            self.assertEqual(service.tasks.get_task(
                first["task"]["taskId"], user_subject="user-a",
            )["status"], "failed")

            next_grant = service.workspace.issue_gateway_grant(
                account["account_id"]
            )
            service.redeem_workspace_gateway_grant(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_key=account["endpoint_key"],
                session_key=account["openclaw_session_key"],
                grant=next_grant["grant"],
                turn_ref="request-two",
            )
            next_turn = service.ensure_host_task(
                **common,
                host_task_key="session|tool-next",
                title="Next request",
                task_scope="user_turn",
            )
            self.assertNotEqual(
                first["task"]["taskId"],
                next_turn["task"]["taskId"],
            )
            next_query = service.ensure_host_task(
                **common, host_task_key="session|query-next",
                title="数据库查询", tool_name="database_execute",
            )
            self.assertEqual(service.tasks.get_task(
                next_query["task"]["taskId"], user_subject="user-a",
            )["summary"]["workspaceQueryGroup"]["turnRef"], "request-two")

    def test_session_idle_timeout_and_logout_do_not_unlink_identity(self) -> None:
        clock = MutableClock()
        with TemporaryDirectory() as tmp:
            store = WorkspaceStore(
                Path(tmp) / "agentbridge.db",
                clock=clock,
                session_ttl_seconds=600,
                session_idle_seconds=120,
            )
            link = store.start_link()
            store.confirm_link(
                link_code=link["link_code"],
                user_subject="user-a",
                approver_endpoint_id="endpoint-a",
            )
            account = store.create_account(
                enrollment_token=link["enrollment_token"],
                username="alice",
                password=PASSWORD,
            )
            session = store.create_session(account["account_id"])

            store.revoke_session(session["session_token"])
            self.assertIsNone(store.verify_session(session["session_token"]))
            self.assertEqual(
                store.authenticate(username="alice", password=PASSWORD)[
                    "user_subject"
                ],
                "user-a",
            )

            fresh = store.create_session(account["account_id"])
            clock.value += timedelta(seconds=121)
            self.assertIsNone(store.verify_session(fresh["session_token"]))

    def test_account_completion_recovers_after_endpoint_registration_gap(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            link = service.workspace.start_link()
            service.workspace.confirm_link(
                link_code=link["link_code"],
                user_subject="user-a",
                approver_endpoint_id="endpoint-a",
            )
            created = service.workspace.create_account(
                enrollment_token=link["enrollment_token"],
                username="alice",
                password=PASSWORD,
            )

            completed = WorkspaceApplication(
                service=service
            ).complete_enrollment(
                enrollment_token=link["enrollment_token"],
                username="alice",
                password=PASSWORD,
            )

            self.assertEqual(
                completed["account"]["accountId"],
                created["account_id"],
            )
            self.assertIsNotNone(
                service.workspace.get_account(created["account_id"])[
                    "endpoint_id"
                ]
            )

    def test_workspace_tasks_events_and_endpoints_are_user_isolated(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account_a = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            account_b = _create_account(
                service,
                user_subject="user-b",
                username="bob",
                endpoint_key="telegram:*:bob",
            )
            app = WorkspaceApplication(service=service)
            task_a = service.ensure_host_task(
                user_subject="user-a",
                token_id="token-a",
                agent_host="openclaw",
                host_task_key="session-a|run-a",
                endpoint_key="telegram:*:alice",
                client_type="telegram",
                external_subject="alice",
                conversation_ref="agent:main:telegram:direct:alice",
                title="Alice OA task",
            )
            service.ensure_host_task(
                user_subject="user-b",
                token_id="token-b",
                agent_host="openclaw",
                host_task_key="session-b|run-b",
                endpoint_key="telegram:*:bob",
                client_type="telegram",
                external_subject="bob",
                conversation_ref="agent:main:telegram:direct:bob",
                title="Bob OA task",
            )

            self.assertEqual(
                [item["title"] for item in app.list_tasks(account_a)],
                ["Alice OA task"],
            )
            self.assertEqual(
                [item["title"] for item in app.list_tasks(account_b)],
                ["Bob OA task"],
            )
            self.assertTrue(
                all(
                    "user_subject" not in item
                    for item in app.list_events(account_a)
                )
            )
            self.assertEqual(
                app.latest_event_id(account_a),
                app.list_events(account_a)[-1]["event_id"],
            )
            self.assertTrue(
                all(
                    "user_subject" not in item
                    and "token_id" not in item
                    and "conversation_ref" not in item
                    for item in app.list_endpoints(account_a)
                )
            )
            with self.assertRaises(TaskNotFound):
                app.task_detail(
                    account_b,
                    task_a["task"]["taskId"],
                )

    def test_password_hash_and_tokens_are_not_stored_in_plaintext(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            session = service.workspace.create_session(account["account_id"])
            with closing(sqlite3.connect(service.db_path)) as connection:
                password_hash = connection.execute(
                    "SELECT password_hash FROM workspace_accounts"
                ).fetchone()[0]
                token_hash, csrf_hash = connection.execute(
                    "SELECT token_hash, csrf_hash FROM workspace_sessions "
                    "WHERE session_id = ?",
                    (session["session_id"],),
                ).fetchone()
            self.assertNotIn(PASSWORD, password_hash)
            self.assertNotEqual(token_hash, session["session_token"])
            self.assertNotEqual(csrf_hash, session["csrf_token"])

    def test_host_dispatch_idempotency_and_user_isolation(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account_a = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            account_b = _create_account(
                service,
                user_subject="user-b",
                username="bob",
                endpoint_key="telegram:*:bob",
            )
            values = {
                "account_id": account_a["account_id"],
                "user_subject": "user-a",
                "agent_host": "openclaw",
                "host_binding_ref": account_a["endpoint_key"],
                "origin_endpoint_id": account_a["endpoint_id"],
                "conversation_ref": account_a["openclaw_session_key"],
                "message_key": "workspace:user:dispatch-1",
                "payload_hash": "a" * 64,
                "idempotency_key": "dispatch-1",
            }

            created, reused = service.workspace.create_host_dispatch(**values)
            same, reused_again = service.workspace.create_host_dispatch(**values)

            self.assertFalse(reused)
            self.assertTrue(reused_again)
            self.assertEqual(created["dispatch_id"], same["dispatch_id"])
            self.assertEqual(created["retry_count"], 0)
            with self.assertRaisesRegex(
                WorkspaceConflictError,
                "IDEMPOTENCY_PAYLOAD_MISMATCH",
            ):
                service.workspace.create_host_dispatch(
                    **{**values, "payload_hash": "b" * 64}
                )
            self.assertEqual(
                len(
                    service.workspace.list_host_dispatches(
                        user_subject="user-a"
                    )
                ),
                1,
            )
            self.assertEqual(
                service.workspace.list_host_dispatches(
                    user_subject=account_b["user_subject"]
                ),
                [],
            )
            canceled = service.workspace.cancel_host_dispatch(
                created["dispatch_id"],
                user_subject="user-a",
            )
            self.assertEqual(canceled["state"], "canceled")
            self.assertEqual(
                service.workspace.list_host_dispatches(
                    user_subject="user-a",
                    active_only=True,
                ),
                [],
            )
            self.assertEqual(
                service.workspace.list_host_dispatch_events(
                    created["dispatch_id"]
                )[-1]["payload"]["state"],
                "aborted",
            )


class WorkspaceApplicationTests(unittest.TestCase):
    def test_pre_accept_readiness_deferrals_use_persistent_backoff(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            clock = MutableClock()
            service.workspace.clock = clock
            dispatch, _ = service.workspace.create_host_dispatch(
                account_id=account["account_id"],
                user_subject=account["user_subject"],
                agent_host="openclaw",
                host_binding_ref=account["endpoint_key"],
                origin_endpoint_id=account["endpoint_id"],
                conversation_ref=account["openclaw_session_key"],
                message_key="workspace:user:backoff-1",
                payload_hash="a" * 64,
                idempotency_key="backoff-1",
            )
            app = WorkspaceApplication(service=service, gateway=None)

            for retry_count, expected_delay in enumerate((1, 2, 3), 1):
                claimed = service.workspace.claim_next_host_dispatch(
                    account_id=account["account_id"],
                    claim_owner="test-worker",
                )
                self.assertIsNotNone(claimed)
                started_at = clock.value
                app._defer_dispatch(claimed, "GATEWAY_CONNECTION_FAILED")
                dispatch = service.workspace.get_host_dispatch(
                    dispatch["dispatch_id"]
                )
                next_attempt_at = datetime.fromisoformat(
                    dispatch["next_attempt_at"].replace("Z", "+00:00")
                )
                self.assertEqual(dispatch["retry_count"], retry_count)
                self.assertEqual(dispatch["attempt_count"], 0)
                self.assertEqual(
                    next_attempt_at - started_at,
                    timedelta(seconds=expected_delay),
                )
                clock.value = next_attempt_at + timedelta(milliseconds=1)

    def test_workspace_images_reject_mime_spoofing_and_oversized_sets(self) -> None:
        png_content = base64.b64encode(b"not-a-png").decode("ascii")
        with self.assertRaisesRegex(ValueError, "does not match"):
            _validated_chat_attachments(
                [
                    {
                        "mimeType": "image/png",
                        "fileName": "spoofed.png",
                        "content": png_content,
                    }
                ]
            )
        with self.assertRaisesRegex(ValueError, "at most 4"):
            _validated_chat_attachments([{}] * 5)

    def test_authenticated_request_touches_workspace_endpoint(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            session = service.workspace.create_session(account["account_id"])
            stale = "2026-01-01T00:00:00+00:00"
            with closing(sqlite3.connect(service.db_path)) as connection:
                connection.execute(
                    """
                    UPDATE client_endpoints
                    SET updated_at = ?, last_seen_at = ?
                    WHERE endpoint_id = ?
                    """,
                    (stale, stale, account["endpoint_id"]),
                )

            app = WorkspaceApplication(service=service)
            verified = app.session(session["session_token"])
            endpoint = service.tasks.endpoint_for_key(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_key=account["endpoint_key"],
            )

            self.assertIsNotNone(verified)
            self.assertNotEqual(endpoint["last_seen_at"], stale)
            self.assertEqual(endpoint["user_subject"], "user-a")

    def test_continue_task_binds_owned_task_to_workspace_endpoint(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            task = service.ensure_host_task(
                user_subject="user-a",
                token_id="workspace-token",
                agent_host="openclaw",
                host_task_key="workspace-task|continue-1",
                endpoint_key=account["endpoint_key"],
                client_type="web",
                external_subject=account["account_id"],
                conversation_ref=account["openclaw_session_key"],
                title="Review pending workflow",
            )
            app = WorkspaceApplication(service=service)

            result = app.continue_task(account, task["task"]["taskId"])
            continuation = service.tasks.get_continuation(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_id=account["endpoint_id"],
            )

            self.assertEqual(result["status"], "selected")
            self.assertEqual(result["task"]["task_id"], task["task"]["taskId"])
            self.assertEqual(continuation["state"], "selected")
            self.assertEqual(continuation["execution_mode"], "resume")
            self.assertEqual(
                continuation["selected_task_id"],
                task["task"]["taskId"],
            )

    def test_task_detail_uses_the_workspace_endpoint_authorization_url(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            task = service.ensure_host_task(
                user_subject="user-a",
                token_id="workspace-token",
                agent_host="openclaw",
                host_task_key="workspace-task|run-1",
                endpoint_key=account["endpoint_key"],
                client_type="web",
                external_subject=account["account_id"],
                conversation_ref=account["openclaw_session_key"],
                title="Submit leave request",
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
            interaction = service._execution_authorization_interaction(
                authorization
            )
            service.observe_host_task(
                user_subject="user-a",
                task_id=task["task"]["taskId"],
                interaction_ids=[interaction["interactionId"]],
            )
            app = WorkspaceApplication(service=service)

            detail = app.task_detail(account, task["task"]["taskId"])

            presentation = detail["interaction"]["presentation"]
            self.assertEqual(len(detail["interactions"]), 1)
            self.assertEqual(
                detail["interactions"][0]["interactionId"],
                detail["interaction"]["interactionId"],
            )
            self.assertTrue(presentation["individualized"])
            self.assertEqual(presentation["endpointId"], account["endpoint_id"])
            self.assertIn("/present/", presentation["url"])


    def test_task_detail_exposes_all_parallel_interactions(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            task = service.ensure_host_task(
                user_subject="user-a",
                token_id="workspace-token",
                agent_host="openclaw",
                host_task_key="workspace-task|parallel-1",
                endpoint_key=account["endpoint_key"],
                client_type="web",
                external_subject=account["account_id"],
                conversation_ref=account["openclaw_session_key"],
                title="Handle two pending workflows",
            )
            interactions = []
            for suffix, title in (
                ("efficiency", "Approve efficiency data"),
                ("weekly", "Acknowledge weekly report"),
            ):
                capability = "oa.efficiency_data.approve" if suffix == "efficiency" else "oa.weekly_report.acknowledge"
                operation, _ = service.operations.create(user_subject="user-a", capability_name=capability,
                    capability_version="1", input_summary={})
                authorization = service.write_authorizations.create(
                    user_subject="user-a",
                    system_id="oa",
                    session_id="session-a",
                    capability_name=capability,
                    capability_version="1",
                    prepare_operation_id=operation["operation_id"],
                    plan={"target": suffix},
                    summary={"title": title, "fields": []},
                    card_base_url="https://cards.example.test",
                )
                interactions.append(
                    service._execution_authorization_interaction(authorization)
                )
            service.observe_host_task(
                user_subject="user-a",
                task_id=task["task"]["taskId"],
                interaction_ids=[
                    interaction["interactionId"] for interaction in interactions
                ],
            )
            app = WorkspaceApplication(service=service)

            detail = app.task_detail(account, task["task"]["taskId"])

            self.assertEqual(
                [item["interactionId"] for item in detail["interactions"]],
                [item["interactionId"] for item in interactions],
            )
            self.assertEqual(
                detail["interaction"]["interactionId"],
                interactions[-1]["interactionId"],
            )
            self.assertTrue(
                all(
                    item["presentation"]["individualized"]
                    for item in detail["interactions"]
                )
            )
            self.assertTrue(
                all(item.get("linkedAt") for item in detail["interactions"])
            )


    def test_task_detail_exposes_owned_task_plan_without_private_arguments(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="workspace:alice",
            )
            task = service.ensure_host_task(
                user_subject="user-a",
                token_id="workspace-token",
                agent_host="openclaw",
                host_task_key="workspace-task|plan-1",
                endpoint_key=account["endpoint_key"],
                client_type="web",
                external_subject=account["account_id"],
                conversation_ref=account["openclaw_session_key"],
                title="Cross-system test",
            )["task"]
            service.task_plans.create(
                user_subject="user-a",
                parent_task_id=task["taskId"],
                compiled_plan={
                    "goal": "汇总工作形成草稿",
                    "planHash": "a" * 64,
                    "riskSummary": {
                        "systems": ["oa", "taihua"],
                        "writeSinkCount": 0,
                    },
                    "steps": [
                        {
                            "stepKey": "read",
                            "ordinal": 1,
                            "kind": "capability",
                            "title": "读取 OA 已办",
                            "capabilityName": "oa.workflow.done.list",
                            "version": "1",
                            "dependsOn": [],
                            "arguments": {"secret": "not-public"},
                            "bindings": {},
                            "effect": "read",
                            "systemId": "oa",
                        }
                    ],
                },
                proposal_source="agent_host",
                coordinator_lease_version=1,
                idempotency_key="workspace-plan-1",
            )

            detail = WorkspaceApplication(service=service).task_detail(
                account, task["taskId"]
            )

            self.assertEqual(detail["plan"]["state"], "validated")
            self.assertEqual(detail["plan"]["steps"][0]["stepKey"], "read")
            self.assertNotIn("arguments", detail["plan"]["steps"][0])
            self.assertNotIn("not-public", json.dumps(detail, ensure_ascii=False))

    def test_task_detail_exposes_only_owned_short_lived_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            task = service.ensure_host_task(
                user_subject="user-a",
                token_id="workspace-token",
                agent_host="openclaw",
                host_task_key="workspace-task|artifact-1",
                endpoint_key=account["endpoint_key"],
                client_type="web",
                external_subject=account["account_id"],
                conversation_ref=account["openclaw_session_key"],
                title="Download OA certificate",
            )
            service.tasks.link_artifact(
                task_id=task["task"]["taskId"],
                user_subject="user-a",
                artifact={
                    "artifact_type": "certificate_scan",
                    "source_ref": "download-a",
                    "filename": "certificate.pdf",
                    "content_type": "application/pdf",
                    "byte_size": 2048,
                    "download_url": (
                        "https://10.10.50.213:8780/download/download-a/file"
                    ),
                    "expires_at": "2099-07-30T00:30:00+00:00",
                },
            )
            app = WorkspaceApplication(service=service)

            detail = app.task_detail(account, task["task"]["taskId"])

            self.assertEqual(len(detail["artifacts"]), 1)
            artifact = detail["artifacts"][0]
            self.assertEqual(artifact["filename"], "certificate.pdf")
            self.assertTrue(artifact["download_url"].endswith("/file"))
            self.assertNotIn("user_subject", artifact)
            self.assertNotIn("source_ref", artifact)

    def test_chat_binds_identity_before_send_and_hides_tool_messages(self) -> None:
        with TemporaryDirectory() as tmp, ExitStack() as cleanup:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            service.tasks.ensure_endpoint(
                user_subject="user-a",
                token_id="token-alice",
                agent_host="openclaw",
                endpoint_key="telegram:*:alice",
                client_type="telegram",
                external_subject="alice",
                conversation_ref="agent:main:telegram:direct:alice",
                capabilities=["direct_status", "timeline_message"],
                route={"channel": "telegram", "to": "alice"},
            )
            gateway = FakeGateway()
            app = WorkspaceApplication(service=service, gateway=gateway)
            cleanup.callback(app.close)

            history = app.chat_history(account)
            streamed = list(app.chat_stream(account))
            send_streamed = list(
                app.send_chat_stream(
                    account,
                    message="读取我的 OA 待办",
                    idempotency_key="web-message-stream-1",
                )
            )
            result = app.send_chat(
                account,
                message="读取我的 OA 待办",
                idempotency_key="web-message-1",
            )

            self.assertEqual(
                [item["role"] for item in history["messages"]],
                ["user", "assistant"],
            )
            self.assertEqual(
                history["messages"][0]["timestamp"],
                datetime.fromtimestamp(
                    1_785_459_303.372,
                    timezone.utc,
                ).isoformat(timespec="milliseconds"),
            )
            self.assertEqual(
                history["messages"][1]["timestamp"],
                datetime.fromtimestamp(
                    1_785_459_333.802,
                    timezone.utc,
                ).isoformat(timespec="milliseconds"),
            )
            self.assertEqual(result.run_id, "web-message-1")
            self.assertEqual(streamed[-1]["state"], "final")
            self.assertEqual(send_streamed[0]["type"], "progress")
            self.assertTrue(
                any(item["type"] == "accepted" for item in send_streamed)
            )
            methods = [method for method, _params in gateway.calls]
            # Readiness may be rechecked when execution exceeds its cache TTL.
            business_calls = [
                (method, params)
                for method, params in gateway.calls
                if method != "system.info"
            ]
            self.assertEqual(
                [method for method, _params in business_calls],
                [
                    "chat.history",
                    "stream",
                    "send_stream",
                    "send_stream",
                ],
            )
            self.assertLess(methods.index("system.info"), methods.index("send_stream"))
            streamed_send = business_calls[2][1]
            compatibility_send = business_calls[3][1]
            self.assertEqual(
                streamed_send["sessionKey"],
                account["openclaw_session_key"],
            )
            self.assertEqual(
                streamed_send["idempotencyKey"],
                "web-message-stream-1",
            )
            self.assertEqual(streamed_send["timeoutSeconds"], 300)
            self.assertFalse(streamed_send["retryBeforeAccept"])
            self.assertFalse(streamed_send["abortAcceptedOnClose"])
            self.assertEqual(
                compatibility_send["sessionKey"],
                account["openclaw_session_key"],
            )
            self.assertEqual(
                compatibility_send["idempotencyKey"],
                "web-message-1",
            )
            timeline = app.list_timeline(account)
            messages = [
                entry
                for entry in timeline
                if entry["entry_type"] == "chat_message"
            ]
            self.assertEqual(
                [entry["role"] for entry in messages],
                ["user", "assistant", "user", "assistant"],
            )
            self.assertTrue(
                all(entry["source"]["is_origin"] for entry in messages)
            )
            self.assertEqual(
                [entry["message_key"] for entry in messages],
                [
                    "workspace:user:web-message-stream-1",
                    "workspace:assistant:web-message-stream-1",
                    "workspace:user:web-message-1",
                    "workspace:assistant:web-message-1",
                ],
            )
            self.assertEqual(
                [entry["sequence"] for entry in messages],
                sorted(entry["sequence"] for entry in messages),
            )
            telegram_endpoint = service.tasks.endpoint_for_key(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_key="telegram:*:alice",
            )
            timeline_deliveries = [
                delivery
                for delivery in service.tasks.list_outbox(
                    user_subject="user-a",
                    endpoint_id=telegram_endpoint["endpoint_id"],
                )
                if delivery["payload_type"] == "timeline_message"
            ]
            self.assertEqual(len(timeline_deliveries), 4)

    def test_workspace_chat_persists_and_forwards_governed_timeline_image(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
                client_type="telegram",
            )
            service.tasks.ensure_endpoint(
                user_subject="user-a",
                token_id="token-alice",
                agent_host="openclaw",
                endpoint_key="telegram:*:alice",
                client_type="telegram",
                external_subject="alice",
                conversation_ref="agent:main:telegram:direct:alice",
                capabilities=["direct_status", "timeline_message"],
                route={"channel": "telegram", "to": "alice"},
            )
            gateway = FakeGateway()
            app = WorkspaceApplication(service=service, gateway=gateway)
            image_content = base64.b64encode(
                b"\x89PNG\r\n\x1a\nsmall-test-image"
            ).decode("ascii")

            events = list(
                app.send_chat_stream(
                    account,
                    message="读取图片中的软著名称",
                    idempotency_key="web-image-1",
                    attachments=[
                        {
                            "type": "image",
                            "mimeType": "image/png",
                            "fileName": "clipboard.png",
                            "content": image_content,
                        }
                    ],
                )
            )

            self.assertEqual(events[0]["type"], "progress")
            self.assertTrue(any(item["type"] == "accepted" for item in events))
            sent = next(
                params
                for method, params in gateway.calls
                if method == "send_stream"
            )
            self.assertEqual(sent["attachments"][0]["mimeType"], "image/png")
            self.assertEqual(sent["attachments"][0]["content"], image_content)
            timeline_items = app.list_timeline(account)
            self.assertEqual(timeline_items[0]["text"], "读取图片中的软著名称")
            attachments = timeline_items[0]["payload"]["attachments"]
            self.assertEqual(len(attachments), 1)
            self.assertEqual(attachments[0]["fileName"], "clipboard.png")
            self.assertRegex(
                attachments[0]["mediaUrl"],
                r"^http://127\.0\.0\.1:8780/media/.+/file$",
            )
            attachment = app.timeline_attachment(
                account,
                attachments[0]["attachmentId"],
            )
            self.assertEqual(attachment["body"], base64.b64decode(image_content))
            other_account = _create_account(
                service,
                user_subject="user-b",
                username="bob",
                endpoint_key="telegram:*:bob",
            )
            with self.assertRaises(PermissionError):
                app.timeline_attachment(
                    other_account,
                    attachments[0]["attachmentId"],
                )
            timeline = json.dumps(timeline_items, ensure_ascii=False)
            self.assertNotIn(image_content, timeline)
            telegram = service.tasks.endpoint_for_key(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_key="telegram:*:alice",
            )
            deliveries = [
                item
                for item in service.tasks.list_outbox(
                    user_subject="user-a",
                    endpoint_id=telegram["endpoint_id"],
                )
                if item["payload_type"] == "timeline_message"
            ]
            self.assertEqual(
                deliveries[0]["payload"]["attachments"][0]["fileName"],
                "clipboard.png",
            )

    def test_timeline_is_isolated_by_workspace_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account_a = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            account_b = _create_account(
                service,
                user_subject="user-b",
                username="bob",
                endpoint_key="wechat:direct:bob",
                client_type="wechat",
            )
            service.tasks.append_timeline_message(
                user_subject="user-a",
                source_endpoint_id=account_a["endpoint_id"],
                message_key="a-1",
                role="user",
                text="Message for Alice",
            )
            service.tasks.append_timeline_message(
                user_subject="user-b",
                source_endpoint_id=account_b["endpoint_id"],
                message_key="b-1",
                role="user",
                text="Message for Bob",
            )
            app = WorkspaceApplication(service=service)

            self.assertEqual(
                [entry["text"] for entry in app.list_timeline(account_a)],
                ["Message for Alice"],
            )
            self.assertEqual(
                [entry["text"] for entry in app.list_timeline(account_b)],
                ["Message for Bob"],
            )

    def test_gateway_failure_is_projected_to_the_cross_end_timeline(self) -> None:
        class FailingGateway(FakeGateway):
            def send_stream(self, **kwargs):
                super().send_stream(**kwargs)
                raise GatewayRequestError(
                    "GATEWAY_RUN_TIMEOUT_ABORTED",
                    "run stopped",
                    {"hadToolActivity": False},
                )

        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            app = WorkspaceApplication(
                service=service,
                gateway=FailingGateway(),
            )

            events = list(
                app.send_chat_stream(
                    account,
                    message="List five sent workflows",
                    idempotency_key="failed-run-1",
                )
            )
            self.assertEqual(events[-1]["state"], "error")
            self.assertTrue(events[-1]["safeToRetry"])
            timeline = app.list_timeline(account)
            self.assertEqual(
                [entry["role"] for entry in timeline],
                ["user", "assistant"],
            )
            self.assertIn("\u5b89\u5168\u4e2d\u6b62", timeline[-1]["text"])

    def test_terminal_abort_keeps_tool_aware_text_in_the_timeline(self) -> None:
        class AbortedGateway(FakeGateway):
            def send_stream(self, **kwargs):
                self.calls.append(("send_stream", kwargs))
                return iter(
                    [
                        {
                            "type": "accepted",
                            "runId": "aborted-run-1",
                            "status": "started",
                        },
                        {
                            "type": "chat",
                            "runId": "aborted-run-1",
                            "state": "aborted",
                            "hadToolActivity": True,
                            "safeToRetry": False,
                            "text": "Run stopped after a business tool call.",
                        },
                    ]
                )

        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            app = WorkspaceApplication(
                service=service,
                gateway=AbortedGateway(),
            )

            list(
                app.send_chat_stream(
                    account,
                    message="Read OA pending workflows",
                    idempotency_key="aborted-run-1",
                )
            )

            self.assertEqual(
                app.list_timeline(account)[-1]["text"],
                "Run stopped after a business tool call.",
            )

    def test_second_workspace_run_waits_in_durable_account_order(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingGateway(FakeGateway):
            def send_stream(self, **kwargs):
                self.calls.append(("send_stream", kwargs))

                def events():
                    yield {
                        "type": "accepted",
                        "runId": kwargs["idempotency_key"],
                        "status": "started",
                    }
                    entered.set()
                    release.wait(timeout=5)
                    yield {
                        "type": "chat",
                        "runId": kwargs["idempotency_key"],
                        "state": "final",
                        "text": "done",
                    }

                return events()

        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            gateway = BlockingGateway()
            app = WorkspaceApplication(service=service, gateway=gateway)
            first_errors: list[Exception] = []

            def consume_first() -> None:
                try:
                    list(
                        app.send_chat_stream(
                            account,
                            message="first",
                            idempotency_key="concurrent-run-1",
                        )
                    )
                except Exception as exc:  # pragma: no cover - assertion aid
                    first_errors.append(exc)

            thread = threading.Thread(target=consume_first)
            thread.start()
            self.assertTrue(entered.wait(timeout=3))
            try:
                second_stream = app.send_chat_stream(
                    account,
                    message="second",
                    idempotency_key="concurrent-run-2",
                )
                self.assertEqual(
                    len(app.list_chat_dispatches(account)),
                    2,
                )
                self.assertEqual(
                    sum(method == "send_stream" for method, _ in gateway.calls),
                    1,
                )
            finally:
                release.set()
                thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(first_errors, [])
            second_events = list(second_stream)
            self.assertTrue(
                any(item["type"] == "accepted" for item in second_events)
            )
            self.assertEqual(
                sum(method == "send_stream" for method, _ in gateway.calls),
                2,
            )
            timeline = app.list_timeline(account)
            self.assertEqual(
                [entry["text"] for entry in timeline if entry["role"] == "user"],
                ["first", "second"],
            )
            app.close()

    def test_browser_stream_disconnect_does_not_abort_durable_run(self) -> None:
        accepted = threading.Event()
        release = threading.Event()

        class SlowGateway(FakeGateway):
            def send_stream(self, **kwargs):
                self.calls.append(("send_stream", kwargs))

                def events():
                    yield {
                        "type": "accepted",
                        "runId": kwargs["idempotency_key"],
                        "status": "started",
                    }
                    accepted.set()
                    release.wait(timeout=5)
                    yield {
                        "type": "chat",
                        "runId": kwargs["idempotency_key"],
                        "state": "final",
                        "text": "durable result",
                    }

                return events()

        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            gateway = SlowGateway()
            app = WorkspaceApplication(service=service, gateway=gateway)
            stream = app.send_chat_stream(
                account,
                message="keep running after browser disconnect",
                idempotency_key="browser-disconnect-1",
            )

            self.assertEqual(next(stream)["type"], "progress")
            stream.close()
            self.assertTrue(accepted.wait(timeout=3))
            release.set()
            self.assertTrue(
                _wait_until(
                    lambda: not app.list_chat_dispatches(account),
                    timeout=5,
                )
            )
            self.assertEqual(app.list_timeline(account)[-1]["text"], "durable result")
            self.assertFalse(
                any(method == "chat.abort" for method, _ in gateway.calls)
            )

    def test_pre_accept_transport_failure_recovers_with_one_run(self) -> None:
        class RecoveringGateway(FakeGateway):
            def __init__(self) -> None:
                super().__init__()
                self.send_attempts = 0

            def send_stream(self, **kwargs):
                self.send_attempts += 1
                if self.send_attempts == 1:
                    self.calls.append(("send_stream", kwargs))
                    raise GatewayRequestError(
                        "GATEWAY_CONNECTION_FAILED",
                        "reverse tunnel is reconnecting",
                        {"stage": "connect"},
                    )
                return super().send_stream(**kwargs)

        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            gateway = RecoveringGateway()
            with closing(WorkspaceApplication(service=service, gateway=gateway)) as app:
                events = list(
                    app.send_chat_stream(
                        account,
                        message="recover this read request",
                        idempotency_key="preaccept-recovery-1",
                    )
                )

                accepted_events = [
                    item for item in events if item.get("type") == "accepted"
                ]
                self.assertEqual(len(accepted_events), 1)
                self.assertTrue(
                    any(item.get("phase") == "waiting_host" for item in events)
                )
                self.assertEqual(gateway.send_attempts, 2)
                completed = service.workspace.list_host_dispatches(
                    user_subject="user-a"
                )[0]
                self.assertEqual(completed["state"], "completed")
                self.assertEqual(completed["attempt_count"], 2)
                self.assertEqual(completed["retry_count"], 1)

    def test_agentbridge_restart_resumes_waiting_dispatch(self) -> None:
        class OfflineGateway(FakeGateway):
            def call(self, method, params=None, *, timeout_seconds=30):
                if method == "system.info":
                    raise GatewayRequestError(
                        "GATEWAY_CONNECTION_FAILED",
                        "offline",
                    )
                return super().call(
                    method,
                    params,
                    timeout_seconds=timeout_seconds,
                )

        with TemporaryDirectory() as tmp, ExitStack() as cleanup:
            service = _service(tmp)
            clock = MutableClock()
            clock.value = datetime.now(timezone.utc)
            service.workspace.clock = clock
            # This test verifies persisted restart recovery, not thread speed.
            # Capture scheduling, then execute each claimed attempt synchronously.
            # The neighboring dispatch tests cover actual background workers.
            scheduled = cleanup.enter_context(
                patch.object(WorkspaceApplication, "_ensure_dispatch_worker")
            )
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            first = WorkspaceApplication(
                service=service,
                gateway=OfflineGateway(),
            )
            cleanup.callback(first.close)
            first.send_chat_stream(
                account,
                message="survive central restart",
                idempotency_key="restart-recovery-1",
            )
            claimed = service.workspace.claim_next_host_dispatch(
                account_id=account["account_id"],
                claim_owner=first._dispatch_instance_id,
            )
            self.assertIsNotNone(claimed)
            first._process_host_dispatch(claimed)
            waiting = service.workspace.list_host_dispatches(user_subject="user-a")[0]
            self.assertEqual(waiting["state"], "waiting_host")
            first.close()

            clock.value = datetime.fromisoformat(waiting["next_attempt_at"])
            scheduled.reset_mock()
            gateway = FakeGateway()
            second = WorkspaceApplication(service=service, gateway=gateway)
            cleanup.callback(second.close)
            scheduled.assert_called_once_with(account["account_id"])
            resumed = service.workspace.claim_next_host_dispatch(
                account_id=account["account_id"],
                claim_owner=second._dispatch_instance_id,
            )
            self.assertIsNotNone(resumed)
            self.assertEqual(resumed["dispatch_id"], claimed["dispatch_id"])
            second._process_host_dispatch(resumed)
            self.assertEqual(second.list_chat_dispatches(account), [])
            dispatch = service.workspace.list_host_dispatches(
                user_subject="user-a"
            )[0]
            self.assertEqual(dispatch["state"], "completed")
            self.assertEqual(
                sum(method == "send_stream" for method, _ in gateway.calls),
                1,
            )
            self.assertEqual(
                second.list_timeline(account)[-1]["text"],
                "OA 登录状态有效。",
            )
            second.close()

    def test_post_accept_disconnect_reconciles_without_redispatch(self) -> None:
        class AcceptedDisconnectGateway(FakeGateway):
            def send_stream(self, **kwargs):
                self.calls.append(("send_stream", kwargs))

                def events():
                    yield {
                        "type": "accepted",
                        "runId": kwargs["idempotency_key"],
                        "status": "started",
                    }
                    raise GatewayRequestError(
                        "GATEWAY_CONNECTION_CLOSED",
                        "connection closed",
                        {"stage": "stream"},
                    )

                return events()

            def run_history_evidence(self, **kwargs):
                self.calls.append(("run_history_evidence", kwargs))
                return {
                    "prompt_observed": True,
                    "had_tool_activity": True,
                    "final_text": "recovered final result",
                    "observed_run_id": kwargs["run_ref"],
                }

        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            account = _create_account(
                service,
                user_subject="user-a",
                username="alice",
                endpoint_key="telegram:*:alice",
            )
            gateway = AcceptedDisconnectGateway()
            with closing(WorkspaceApplication(service=service, gateway=gateway)) as app:
                events = list(
                    app.send_chat_stream(
                        account,
                        message="read only once",
                        idempotency_key="accepted-disconnect-1",
                    )
                )

                self.assertEqual(
                    sum(method == "send_stream" for method, _ in gateway.calls),
                    1,
                )
                self.assertEqual(events[-1]["state"], "final")
                self.assertEqual(events[-1]["text"], "recovered final result")
                self.assertEqual(app.list_timeline(account)[-1]["text"], "recovered final result")

    def test_delayed_result_survives_old_timeout_and_restart_without_resend(self) -> None:
        class DelayedGateway(FakeGateway):
            ready = False
            def send_stream(self, **kwargs):
                self.calls.append(("send_stream", kwargs))
                yield {"type": "accepted", "runId": kwargs["idempotency_key"]}
                raise GatewayRequestError("GATEWAY_CONNECTION_CLOSED", "disconnected")
            def run_history_evidence(self, **kwargs):
                self.calls.append(("run_history_evidence", kwargs))
                return {"prompt_observed": True, "had_tool_activity": True,
                        "final_text": "late final" if self.ready else ""}

        with TemporaryDirectory() as tmp, patch.object(WorkspaceApplication, "_ensure_dispatch_worker"):
            service = _service(tmp)
            clock = MutableClock()
            service.workspace.clock = clock
            account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
            gateway = DelayedGateway()
            app = WorkspaceApplication(service=service, gateway=gateway)
            app.send_chat_stream(account, message="only once", idempotency_key="late-run")
            claim = lambda: service.workspace.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="test")
            dispatch = claim()
            app._process_host_dispatch(dispatch)
            for _ in range(18):
                self.assertIsNone(claim())  # Backoff survives independent worker ticks.
                clock.value += timedelta(seconds=10)
                app._process_host_dispatch(claim())
            current = service.workspace.get_host_dispatch(dispatch["dispatch_id"])
            self.assertEqual(current["state"], "accepted")
            self.assertEqual(current["attempt_count"], 1)
            self.assertTrue(current["had_tool_activity"])
            self.assertEqual(app.list_chat_dispatches(account)[0]["lastErrorCode"], "HOST_RUN_RESULT_PENDING")
            events = service.workspace.list_host_dispatch_events(dispatch["dispatch_id"])
            self.assertEqual(sum(e["event_name"] == "host_result_reconciling" for e in events), 1)
            self.assertFalse(any(e["payload"].get("state") == "error" for e in events))
            with self.assertRaises(WorkspaceConflictError):
                service.workspace.cancel_host_dispatch(dispatch["dispatch_id"], user_subject="user-a")
            app.close()
            recovered = WorkspaceApplication(service=service, gateway=gateway)
            try:
                gateway.ready = True
                clock.value += timedelta(seconds=10)
                recovered._process_host_dispatch(claim())
                self.assertEqual(service.workspace.get_host_dispatch(dispatch["dispatch_id"])["state"], "completed")
                replay = list(recovered.send_chat_stream(account, message="only once", idempotency_key="late-run"))
                self.assertEqual(replay[-1]["text"], "late final")
                self.assertEqual(sum(m == "send_stream" for m, _ in gateway.calls), 1)
                self.assertEqual(sum(m["text"] == "late final" for m in recovered.list_timeline(account)), 1)
            finally:
                recovered.close()

    def test_result_observation_timeout_is_unknown_not_failed_or_retryable(self) -> None:
        class UnavailableGateway(FakeGateway):
            def run_history_evidence(self, **kwargs):
                raise GatewayRequestError("GATEWAY_TIMEOUT", "offline")
        with TemporaryDirectory() as tmp, patch.object(WorkspaceApplication, "_ensure_dispatch_worker"):
            service = _service(tmp)
            clock = MutableClock()
            service.workspace.clock = clock
            account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
            with closing(WorkspaceApplication(service=service, gateway=UnavailableGateway())) as app:
                app.send_chat_stream(account, message="observe only", idempotency_key="unknown-run")
                d = service.workspace.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="test")
                d = service.workspace.mark_host_dispatch_accepted(d["dispatch_id"], claim_token=d["claim_token"], run_id="unknown-run")
                clock.value += timedelta(minutes=31)
                app._process_host_dispatch(d)
                self.assertEqual(service.workspace.get_host_dispatch(d["dispatch_id"])["state"], "result_unknown")
                events = list(app.send_chat_stream(account, message="observe only", idempotency_key="unknown-run"))
                self.assertFalse(events[-1]["safeToRetry"])
                self.assertIn("不代表后台任务失败", events[-1]["text"])
                self.assertEqual(app.list_chat_dispatches(account), [])
                self.assertFalse(any(m == "send_stream" for m, _ in app.gateway.calls))

    def test_internal_error_after_acceptance_never_becomes_preaccept_failure(self) -> None:
        with TemporaryDirectory() as tmp, patch.object(WorkspaceApplication, "_ensure_dispatch_worker"):
            service = _service(tmp)
            account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
            with closing(WorkspaceApplication(service=service, gateway=FakeGateway())) as app:
                app.send_chat_stream(account, message="keep accepted", idempotency_key="internal-run")
                d = service.workspace.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="test")
                d = service.workspace.mark_host_dispatch_accepted(d["dispatch_id"], claim_token=d["claim_token"], run_id="internal-run")
                app._finish_dispatch_internal_error(d, RuntimeError("synthetic"))
                self.assertEqual(service.workspace.get_host_dispatch(d["dispatch_id"])["state"], "accepted")
                self.assertFalse(any(m == "send_stream" for m, _ in app.gateway.calls))

    def test_confirmed_zero_tool_startup_recovery_tracks_its_final_result(self) -> None:
        import hashlib
        key = "recovered-startup"
        recovered = key + "-recovery-1-" + hashlib.sha256(f"{key}:1".encode()).hexdigest()[:12]
        class RecoveredGateway(FakeGateway):
            def send_stream(self, **kwargs):
                yield {"type":"accepted", "runId":key}
                yield {"type":"accepted", "runId":recovered, "recoveredFromRunId":key}
                yield {"type":"chat", "runId":recovered, "state":"final", "text":"recovered result"}
        with TemporaryDirectory() as tmp, patch.object(WorkspaceApplication, "_ensure_dispatch_worker"):
            service = _service(tmp)
            account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
            with closing(WorkspaceApplication(service=service, gateway=RecoveredGateway())) as app:
                app.send_chat_stream(account, message="read only", idempotency_key=key)
                d = service.workspace.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="test")
                app._process_host_dispatch(d)
                result = service.workspace.get_host_dispatch(d["dispatch_id"])
                self.assertEqual(result["accepted_run_id"], recovered)
                self.assertEqual(result["state"], "completed")

    def test_duplicate_acceptance_remains_rejected_without_safe_recovery_link(self) -> None:
        import hashlib
        for had_tool, previous, run in [(False,None,"other"),(False,"wrong","valid"),(True,"first","valid")]:
            with self.subTest(had_tool=had_tool, previous=previous), TemporaryDirectory() as tmp, patch.object(WorkspaceApplication, "_ensure_dispatch_worker"):
                service = _service(tmp)
                account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
                with closing(WorkspaceApplication(service=service, gateway=FakeGateway())) as app:
                    app.send_chat_stream(account,message="read only",idempotency_key="first")
                    d=service.workspace.claim_next_host_dispatch(account_id=account["account_id"],claim_owner="test")
                    service.workspace.mark_host_dispatch_accepted(d["dispatch_id"],claim_token=d["claim_token"],run_id="first")
                    if had_tool:
                        service.workspace.append_host_dispatch_stream_event(d["dispatch_id"],claim_token=d["claim_token"],event_name="host_progress",payload={},had_tool_activity=True)
                    target = "first-recovery-1-" + hashlib.sha256(b"first:1").hexdigest()[:12] if run=="valid" else run
                    with self.assertRaises(WorkspaceConflictError):
                        service.workspace.mark_host_dispatch_accepted(d["dispatch_id"],claim_token=d["claim_token"],run_id=target,recovered_from_run_id=previous)

    def test_terminal_dispatch_drains_events_committed_between_reads(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            app = WorkspaceApplication(service=service, gateway=FakeGateway())
            self.addCleanup(app.close)
            event_pages = [
                [
                    {
                        "sequence": 1,
                        "payload": {"type": "progress", "kind": "accepted"},
                    }
                ],
                [
                    {
                        "sequence": 2,
                        "payload": {
                            "type": "chat",
                            "state": "final",
                            "text": "committed final result",
                        },
                    }
                ],
                [],
            ]

            with (
                patch.object(
                    app.store,
                    "list_host_dispatch_events",
                    side_effect=lambda *args, **kwargs: event_pages.pop(0),
                ),
                patch.object(
                    app.store,
                    "get_host_dispatch",
                    return_value={"state": "completed"},
                ),
            ):
                events = list(
                    app._stream_host_dispatch(
                        user_subject="user-a",
                        dispatch_id="dispatch-race",
                    )
                )

            self.assertEqual([event["type"] for event in events], ["progress", "chat"])
            self.assertEqual(events[-1]["state"], "final")
            self.assertEqual(events[-1]["text"], "committed final result")


class WorkspaceGatewayClientTests(unittest.TestCase):
    def test_gateway_client_advertises_tool_event_streaming(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "bscli"
            / "workspace"
            / "gateway_client.mjs"
        ).read_text(encoding="utf-8")

        self.assertIn('caps: ["tool-events"]', source)
        self.assertIn('method: "chat.abort"', source)
        self.assertIn("preflightAbortRequestId", source)
        self.assertIn("requestWorkspaceBind", source)
        self.assertIn('code === "FORBIDDEN" && bindRetryCount < 1', source)
        self.assertIn('requestStage = "bind_retry_wait"', source)
        self.assertIn('method: "sessions.list"', source)
        self.assertIn('requestStreamAbort(recover ? "startup_recovery"', source)
        self.assertIn("startRecoveryAttempt", source)
        self.assertIn("GATEWAY_RUN_TIMEOUT_ABORTED", source)

    def test_gateway_token_is_passed_only_through_the_child_environment(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "gateway.token"
            token_file.write_text(
                "gateway-secret-token-value",
                encoding="utf-8",
            )
            client = OpenClawGatewayClient(
                url="ws://127.0.0.1:18789",
                token_file=token_file,
                state_dir=root / "state",
                script_path=root / "gateway_client.mjs",
            )
            with patch(
                "bscli.workspace.gateway.subprocess.run"
            ) as run:
                run.return_value.stdout = json.dumps(
                    {"ok": True, "payload": {"version": "2026.7.1"}}
                )
                run.return_value.returncode = 0

                result = client.call("system.info", {})

            self.assertEqual(result["version"], "2026.7.1")
            command = run.call_args.args[0]
            self.assertNotIn("gateway-secret-token-value", command)
            self.assertNotIn(
                "gateway-secret-token-value",
                run.call_args.kwargs["input"],
            )
            self.assertEqual(
                run.call_args.kwargs["env"]["AB_GATEWAY_TOKEN"],
                "gateway-secret-token-value",
            )
            diagnostics = client.diagnostics()
            self.assertEqual(diagnostics["target"], "ws://127.0.0.1:18789")
            self.assertIsNotNone(diagnostics["last_success_at"])
            self.assertIsNone(diagnostics["last_error_code"])
            self.assertNotIn("gateway-secret-token-value", str(diagnostics))

    def test_gateway_error_code_is_preserved_without_stderr_leakage(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "gateway.token"
            token_file.write_text(
                "gateway-secret-token-value",
                encoding="utf-8",
            )
            client = OpenClawGatewayClient(
                url="ws://127.0.0.1:18789",
                token_file=token_file,
                state_dir=root / "state",
            )
            with patch(
                "bscli.workspace.gateway.subprocess.run"
            ) as run:
                run.return_value.stdout = json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "PAIRING_REQUIRED",
                            "message": "Device approval is required.",
                        },
                    }
                )
                run.return_value.returncode = 0
                with self.assertRaises(GatewayRequestError) as caught:
                    client.call("system.info", {})

            self.assertEqual(caught.exception.code, "PAIRING_REQUIRED")
            self.assertNotIn(
                "gateway-secret-token-value",
                str(caught.exception),
            )
            diagnostics = client.diagnostics()
            self.assertEqual(diagnostics["last_error_code"], "PAIRING_REQUIRED")
            self.assertIsNotNone(diagnostics["last_error_at"])

    def test_gateway_send_stream_keeps_secrets_out_of_child_arguments(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "gateway.token"
            token_file.write_text(
                "gateway-secret-token-value",
                encoding="utf-8",
            )
            client = OpenClawGatewayClient(
                url="ws://127.0.0.1:18789",
                token_file=token_file,
                state_dir=root / "state",
            )
            input_pipe = _CaptureInput()
            with patch(
                "bscli.workspace.gateway.subprocess.Popen"
            ) as popen:
                process = popen.return_value
                process.stdin = input_pipe
                process.stdout = iter(
                    [
                        '{"type":"accepted","runId":"run-1",'
                        '"status":"started"}\n',
                        '{"type":"progress","runId":"run-1",'
                        '"kind":"tool","phase":"start",'
                        '"label":"正在检查 OA 登录状态"}\n',
                        '{"type":"chat","runId":"run-1",'
                        '"state":"final","text":"已完成"}\n',
                        '{"type":"eof"}\n',
                    ]
                )
                process.poll.return_value = 0

                events = list(
                    client.send_stream(
                        session_key=(
                            "agent:main:agentbridge-workspace:direct:account-a"
                        ),
                        endpoint_key="workspace:account-a",
                        grant="g" * 48,
                        message="检查 OA 登录状态",
                        idempotency_key="run-1",
                        attachments=[
                            {
                                "type": "image",
                                "mimeType": "image/png",
                                "fileName": "clipboard.png",
                                "content": "iVBORw0KGgo=",
                            }
                        ],
                        timeout_seconds=150,
                    )
                )

            self.assertEqual(
                [event["type"] for event in events],
                ["accepted", "progress", "chat"],
            )
            request = json.loads(input_pipe.value)
            self.assertEqual(request["mode"], "send-stream")
            self.assertEqual(request["idempotencyKey"], "run-1")
            self.assertEqual(
                request["attachments"][0]["fileName"],
                "clipboard.png",
            )
            self.assertTrue(request["preflightAbort"])
            self.assertEqual(request["acceptTimeoutMs"], 35_000)
            self.assertEqual(request["startupProgressTimeoutMs"], 15_000)
            self.assertEqual(request["sessionIdleTimeoutMs"], 15_000)
            self.assertEqual(request["sessionIdlePollMs"], 250)
            self.assertNotIn("gateway-secret-token-value", input_pipe.value)
            command = popen.call_args.args[0]
            self.assertNotIn("gateway-secret-token-value", command)
            self.assertEqual(
                popen.call_args.kwargs["env"]["AB_GATEWAY_TOKEN"],
                "gateway-secret-token-value",
            )

    def test_closing_an_accepted_stream_aborts_the_openclaw_run(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "gateway.token"
            token_file.write_text(
                "gateway-secret-token-value",
                encoding="utf-8",
            )
            client = OpenClawGatewayClient(
                url="ws://127.0.0.1:18789",
                token_file=token_file,
                state_dir=root / "state",
            )
            input_pipe = _CaptureInput()
            with (
                patch("bscli.workspace.gateway.subprocess.Popen") as popen,
                patch.object(client, "abort_chat") as abort_chat,
            ):
                process = popen.return_value
                process.stdin = input_pipe
                process.stdout = iter(
                    [
                        '{"type":"accepted","runId":"run-close-1",'
                        '"status":"started"}\n',
                    ]
                )
                process.poll.return_value = 0
                stream = client.send_stream(
                    session_key=(
                        "agent:main:agentbridge-workspace:direct:account-a"
                    ),
                    endpoint_key="workspace:account-a",
                    grant="g" * 48,
                    message="List five sent workflows",
                    idempotency_key="run-close-1",
                    timeout_seconds=120,
                )

                self.assertEqual(next(stream)["type"], "accepted")
                stream.close()

            abort_chat.assert_called_once_with(
                session_key=(
                    "agent:main:agentbridge-workspace:direct:account-a"
                ),
                run_id="run-close-1",
                timeout_seconds=5,
                raise_on_error=False,
            )

    def test_gateway_send_stream_retries_one_pre_accept_handshake_failure(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "gateway.token"
            token_file.write_text(
                "gateway-secret-token-value",
                encoding="utf-8",
            )
            client = OpenClawGatewayClient(
                url="ws://127.0.0.1:18789",
                token_file=token_file,
                state_dir=root / "state",
                retry_sleep=lambda _seconds: None,
            )
            payloads = []

            def stream_payload(payload):
                payloads.append(payload)
                if len(payloads) == 1:
                    def failed_stream():
                        raise GatewayRequestError(
                            "GATEWAY_CONNECTION_CLOSED",
                            "connection closed",
                            {"stage": "connect", "accepted": False},
                        )
                        yield  # pragma: no cover

                    return failed_stream()
                return iter(
                    [
                        {
                            "type": "accepted",
                            "runId": "run-retry-1",
                            "status": "started",
                        },
                        {
                            "type": "chat",
                            "runId": "run-retry-1",
                            "state": "final",
                            "text": "done",
                        },
                    ]
                )

            with patch.object(
                client,
                "_stream_payload",
                side_effect=stream_payload,
            ):
                events = list(
                    client.send_stream(
                        session_key=(
                            "agent:main:agentbridge-workspace:direct:account-a"
                        ),
                        endpoint_key="workspace:account-a",
                        grant="g" * 48,
                        message="List five sent workflows",
                        idempotency_key="run-retry-1",
                        timeout_seconds=120,
                    )
                )

            self.assertEqual(len(payloads), 2)
            self.assertEqual(
                [event["type"] for event in events],
                ["progress", "accepted", "chat"],
            )
            self.assertEqual(events[0]["phase"], "retry")

    def test_gateway_send_stream_waits_for_a_recovering_reverse_tunnel(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "gateway.token"
            token_file.write_text(
                "gateway-secret-token-value",
                encoding="utf-8",
            )
            retry_delays = []
            client = OpenClawGatewayClient(
                url="ws://127.0.0.1:18789",
                token_file=token_file,
                state_dir=root / "state",
                retry_sleep=retry_delays.append,
            )
            payloads = []

            def stream_payload(payload):
                payloads.append(payload)
                if len(payloads) <= 3:
                    def failed_stream():
                        raise GatewayRequestError(
                            "GATEWAY_CONNECTION_FAILED",
                            "reverse tunnel is reconnecting",
                            {"stage": "connect", "accepted": False},
                        )
                        yield  # pragma: no cover

                    return failed_stream()
                return iter(
                    [
                        {
                            "type": "accepted",
                            "runId": "run-tunnel-recovery-1",
                            "status": "started",
                        },
                        {
                            "type": "chat",
                            "runId": "run-tunnel-recovery-1",
                            "state": "final",
                            "text": "done",
                        },
                    ]
                )

            with patch.object(
                client,
                "_stream_payload",
                side_effect=stream_payload,
            ):
                events = list(
                    client.send_stream(
                        session_key=(
                            "agent:main:agentbridge-workspace:direct:account-a"
                        ),
                        endpoint_key="workspace:account-a",
                        grant="g" * 48,
                        message="List five pending workflows",
                        idempotency_key="run-tunnel-recovery-1",
                        timeout_seconds=120,
                    )
                )

            self.assertEqual(len(payloads), 4)
            self.assertEqual(retry_delays, [2.0, 5.0, 10.0])
            self.assertEqual(
                [event["type"] for event in events],
                ["progress", "progress", "progress", "accepted", "chat"],
            )

    def test_gateway_send_stream_refreshes_one_use_grant_before_each_retry(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "gateway.token"
            token_file.write_text(
                "gateway-secret-token-value",
                encoding="utf-8",
            )
            client = OpenClawGatewayClient(
                url="ws://127.0.0.1:18789",
                token_file=token_file,
                state_dir=root / "state",
                retry_sleep=lambda _seconds: None,
            )
            payloads = []
            refreshed = iter(["h" * 48, "i" * 48, "j" * 48])

            def stream_payload(payload):
                payloads.append(payload)
                if len(payloads) <= 3:
                    def failed_stream():
                        raise GatewayRequestError(
                            "GATEWAY_CONNECTION_FAILED",
                            "reverse tunnel is reconnecting",
                            {"stage": "connect", "accepted": False},
                        )
                        yield  # pragma: no cover

                    return failed_stream()
                return iter(
                    [
                        {
                            "type": "accepted",
                            "runId": "run-fresh-grant-1",
                            "status": "started",
                        },
                        {
                            "type": "chat",
                            "runId": "run-fresh-grant-1",
                            "state": "final",
                            "text": "done",
                        },
                    ]
                )

            with patch.object(
                client,
                "_stream_payload",
                side_effect=stream_payload,
            ):
                events = list(
                    client.send_stream(
                        session_key=(
                            "agent:main:agentbridge-workspace:direct:account-a"
                        ),
                        endpoint_key="workspace:account-a",
                        grant="g" * 48,
                        binding_grant_provider=lambda: next(refreshed),
                        message="List five pending workflows",
                        idempotency_key="run-fresh-grant-1",
                        timeout_seconds=120,
                    )
                )

            self.assertEqual(
                [payload["grant"] for payload in payloads],
                ["g" * 48, "h" * 48, "i" * 48, "j" * 48],
            )
            self.assertEqual(events[-1]["text"], "done")

    def test_gateway_send_stream_reconciles_an_accepted_run_from_history(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "gateway.token"
            token_file.write_text(
                "gateway-secret-token-value",
                encoding="utf-8",
            )
            retry_delays = []
            client = OpenClawGatewayClient(
                url="ws://127.0.0.1:18789",
                token_file=token_file,
                state_dir=root / "state",
                retry_sleep=retry_delays.append,
            )

            def failed_stream():
                yield {
                    "type": "accepted",
                    "runId": "run-history-1",
                    "status": "started",
                }
                yield {
                    "type": "progress",
                    "runId": "run-history-1",
                    "kind": "tool",
                    "phase": "result",
                }
                raise GatewayRequestError(
                    "GATEWAY_CONNECTION_CLOSED",
                    "reverse tunnel changed",
                    {
                        "stage": "run",
                        "accepted": True,
                        "hadToolActivity": True,
                    },
                )

            history_calls = 0

            def history(_method, _params, *, timeout_seconds=30):
                nonlocal history_calls
                history_calls += 1
                if history_calls == 1:
                    raise GatewayRequestError(
                        "GATEWAY_CONNECTION_FAILED",
                        "tunnel is reconnecting",
                    )
                return {
                    "messages": [
                        {
                            "role": "user",
                            "content": "Read pending details",
                            "idempotencyKey": "run-history-1:user",
                        },
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "toolCall", "name": "oa_read"},
                            ],
                        },
                        {
                            "role": "toolResult",
                            "content": "done",
                        },
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "Authoritative answer"},
                            ],
                        },
                    ],
                }

            with (
                patch.object(
                    client,
                    "_stream_payload",
                    return_value=failed_stream(),
                ),
                patch.object(client, "call", side_effect=history),
                patch.object(client, "abort_chat") as abort_chat,
            ):
                events = list(
                    client.send_stream(
                        session_key=(
                            "agent:main:agentbridge-workspace:direct:account-a"
                        ),
                        endpoint_key="workspace:account-a",
                        grant="g" * 48,
                        message="Read pending details",
                        idempotency_key="run-history-1",
                        timeout_seconds=120,
                    )
                )

            self.assertEqual(history_calls, 2)
            self.assertEqual(retry_delays, [2.0])
            self.assertEqual(events[-1]["state"], "final")
            self.assertEqual(events[-1]["text"], "Authoritative answer")
            self.assertTrue(events[-1]["recovered"])
            abort_chat.assert_not_called()

    def test_gateway_send_stream_waits_for_final_text_after_tool_result(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "gateway.token"
            token_file.write_text(
                "gateway-secret-token-value",
                encoding="utf-8",
            )
            retry_delays = []
            client = OpenClawGatewayClient(
                url="ws://127.0.0.1:18789",
                token_file=token_file,
                state_dir=root / "state",
                retry_sleep=retry_delays.append,
            )

            def failed_stream():
                yield {
                    "type": "accepted",
                    "runId": "run-history-delayed-final",
                    "status": "started",
                }
                yield {
                    "type": "progress",
                    "runId": "run-history-delayed-final",
                    "kind": "tool",
                    "phase": "result",
                }
                raise GatewayRequestError(
                    "GATEWAY_CONNECTION_CLOSED",
                    "reverse tunnel changed",
                    {
                        "stage": "run",
                        "accepted": True,
                        "hadToolActivity": True,
                    },
                )

            history_calls = 0

            def history(_method, _params, *, timeout_seconds=30):
                nonlocal history_calls
                history_calls += 1
                messages = [
                    {
                        "role": "user",
                        "content": "Read pending details",
                        "idempotencyKey": (
                            "run-history-delayed-final:user"
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "name": "oa_read"},
                        ],
                    },
                    {
                        "role": "toolResult",
                        "content": "done",
                    },
                ]
                if history_calls >= 4:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Delayed authoritative answer",
                                },
                            ],
                        }
                    )
                return {"messages": messages}

            with (
                patch.object(
                    client,
                    "_stream_payload",
                    return_value=failed_stream(),
                ),
                patch.object(client, "call", side_effect=history),
                patch.object(client, "abort_chat") as abort_chat,
            ):
                events = list(
                    client.send_stream(
                        session_key=(
                            "agent:main:agentbridge-workspace:direct:account-a"
                        ),
                        endpoint_key="workspace:account-a",
                        grant="g" * 48,
                        message="Read pending details",
                        idempotency_key="run-history-delayed-final",
                        timeout_seconds=120,
                    )
                )

            self.assertEqual(history_calls, 4)
            self.assertEqual(retry_delays, [2.0, 5.0, 10.0])
            self.assertEqual(events[-1]["state"], "final")
            self.assertEqual(
                events[-1]["text"],
                "Delayed authoritative answer",
            )
            self.assertTrue(events[-1]["recovered"])
            self.assertTrue(events[-1]["hadToolActivity"])
            abort_chat.assert_not_called()

    def test_gateway_send_stream_never_replays_an_unreconciled_run(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "gateway.token"
            token_file.write_text(
                "gateway-secret-token-value",
                encoding="utf-8",
            )
            client = OpenClawGatewayClient(
                url="ws://127.0.0.1:18789",
                token_file=token_file,
                state_dir=root / "state",
                retry_sleep=lambda _seconds: None,
            )

            def failed_stream():
                yield {
                    "type": "accepted",
                    "runId": "run-history-missing",
                    "status": "started",
                }
                raise GatewayRequestError(
                    "GATEWAY_CONNECTION_CLOSED",
                    "connection closed",
                    {"stage": "run", "accepted": True},
                )

            history = {
                "messages": [
                    {
                        "role": "user",
                        "content": "Read pending details",
                        "idempotencyKey": "run-history-missing:user",
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "name": "oa_read"},
                        ],
                    },
                ],
            }
            with (
                patch.object(
                    client,
                    "_stream_payload",
                    return_value=failed_stream(),
                ) as stream_payload,
                patch.object(client, "call", return_value=history) as call,
                patch.object(client, "abort_chat") as abort_chat,
                self.assertRaises(GatewayRequestError) as caught,
            ):
                list(
                    client.send_stream(
                        session_key=(
                            "agent:main:agentbridge-workspace:direct:account-a"
                        ),
                        endpoint_key="workspace:account-a",
                        grant="g" * 48,
                        message="Read pending details",
                        idempotency_key="run-history-missing",
                        timeout_seconds=120,
                    )
                )

            stream_payload.assert_called_once()
            self.assertEqual(call.call_count, 5)
            self.assertTrue(caught.exception.details["reconciliationAttempted"])
            self.assertTrue(caught.exception.details["hadToolActivity"])
            self.assertFalse(caught.exception.details["safeToRetry"])
            abort_chat.assert_called_once()

    def test_gateway_failure_text_does_not_deny_observed_tool_activity(
        self,
    ) -> None:
        text = _workspace_gateway_failure_text(
            GatewayRequestError(
                "GATEWAY_CONNECTION_CLOSED",
                "closed",
                {"hadToolActivity": True},
            )
        )

        self.assertIn("\u5df2\u8c03\u7528\u4e1a\u52a1\u80fd\u529b", text)
        self.assertIn("\u4e0d\u4f1a\u81ea\u52a8\u91cd\u653e", text)
        self.assertNotIn("\u672a\u7ee7\u7eed\u8fdb\u5165\u4e1a\u52a1\u7cfb\u7edf", text)

    def test_gateway_send_stream_does_not_retry_after_send_started(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "gateway.token"
            token_file.write_text(
                "gateway-secret-token-value",
                encoding="utf-8",
            )
            client = OpenClawGatewayClient(
                url="ws://127.0.0.1:18789",
                token_file=token_file,
                state_dir=root / "state",
            )
            calls = 0

            def stream_payload(_payload):
                nonlocal calls
                calls += 1

                def failed_stream():
                    raise GatewayRequestError(
                        "GATEWAY_CONNECTION_CLOSED",
                        "connection closed",
                        {"stage": "send_accept", "accepted": False},
                    )
                    yield  # pragma: no cover

                return failed_stream()

            with (
                patch.object(
                    client,
                    "_stream_payload",
                    side_effect=stream_payload,
                ),
                self.assertRaises(GatewayRequestError),
            ):
                list(
                    client.send_stream(
                        session_key=(
                            "agent:main:agentbridge-workspace:direct:account-a"
                        ),
                        endpoint_key="workspace:account-a",
                        grant="g" * 48,
                        message="List five sent workflows",
                        idempotency_key="run-no-retry-1",
                        timeout_seconds=120,
                    )
                )

            self.assertEqual(calls, 1)


class WorkspaceHttpServerTests(unittest.TestCase):
    def test_image_rejection_precedes_sse_and_late_failure_stays_sse(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            _create_account(service, user_subject="user-a", username="alice",
                            endpoint_key="alice", client_type="web")
            application = WorkspaceApplication(service=service, gateway=FakeGateway())
            port = _free_port()
            origin = f"http://127.0.0.1:{port}"
            server = create_workspace_http_server(
                config=validate_workspace_server_config(
                    host="127.0.0.1", port=port, public_base_url=origin,
                    tls_cert=None, tls_key=None), application=application)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, headers, _ = _request(port, "POST", "/api/login",
                    body={"username": "alice", "password": PASSWORD}, origin=origin)
                self.assertEqual(status, 200)
                cookies = _cookies(headers)
                args = dict(origin=origin, cookies=cookies,
                            csrf=cookies["agentbridge_workspace_csrf"])
                status, headers, payload = _request(port, "POST", "/api/chat/send-stream",
                    body={"message": "image", "idempotencyKey": "invalid-image",
                          "attachments": [{"mimeType": "image/jpeg", "fileName": "renamed.jpg",
                                           "content": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()}]}, **args)
                self.assertEqual(status, 400)
                self.assertEqual(headers.get_content_type(), "application/json")
                self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
                self.assertEqual(service.workspace.list_host_dispatches(user_subject="user-a"), [])

                for failure in [ValueError("private-validation"), RuntimeError("private-storage"),
                                GatewayRequestError("GATEWAY_CONNECTION_CLOSED", "private-gateway")]:
                    with self.subTest(failure=type(failure).__name__):
                        closed = []
                        def broken_stream():
                            try:
                                yield {"type": "accepted", "runId": "accepted"}
                                raise failure
                            finally:
                                closed.append(True)
                        with patch.object(application, "send_chat_stream", return_value=broken_stream()):
                            status, headers, wire = _raw_request(port, "POST", "/api/chat/send-stream",
                                body={"message": "test"}, **args)
                        self.assertEqual(status, 200)
                        self.assertEqual(headers.get_content_type(), "text/event-stream")
                        self.assertIn("event: stream-error", wire)
                        self.assertIn('"safeToRetry":false', wire)
                        self.assertNotIn("HTTP/", wire)
                        self.assertNotIn("private-", wire)
                        self.assertEqual(closed, [True])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                application.close()

    def test_tls_eof_is_classified_as_a_normal_client_disconnect(self) -> None:
        self.assertIn(ssl.SSLEOFError, _CLIENT_DISCONNECT_ERRORS)

    def test_stream_error_exposes_only_safe_retry_diagnostics(self) -> None:
        payload = _public_gateway_stream_error(
            GatewayRequestError(
                "GATEWAY_RUN_TIMEOUT_ABORTED",
                "stopped",
                {
                    "runId": "private-run-id",
                    "aborted": True,
                    "hadToolActivity": False,
                    "recoveryUsed": True,
                    "recoveryAttempt": 1,
                    "acceptedElapsedMs": 1234,
                },
            )
        )

        self.assertTrue(payload["safeToRetry"])
        self.assertNotIn("runId", payload["details"])
        self.assertEqual(payload["details"]["recoveryAttempt"], 1)

    def test_enrollment_login_csrf_and_read_only_workspace_routes(self) -> None:
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            gateway = FakeGateway()
            application = WorkspaceApplication(service=service, gateway=gateway)
            port = _free_port()
            origin = f"http://127.0.0.1:{port}"
            server = create_workspace_http_server(
                config=validate_workspace_server_config(
                    host="127.0.0.1",
                    port=port,
                    public_base_url=origin,
                    tls_cert=None,
                    tls_key=None,
                ),
                application=application,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, _, health = _request(port, "GET", "/healthz")
                self.assertEqual(status, 200)
                self.assertEqual(health["status"], "ok")
                status, _, ready = _request(port, "GET", "/readyz")
                self.assertEqual(status, 200)
                self.assertEqual(ready["status"], "ready")
                self.assertTrue(ready["gatewayConfigured"])
                status, version_headers, version = _request(
                    port,
                    "GET",
                    "/api/client-version",
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(version["version"]), 16)
                self.assertEqual(version_headers.get("Cache-Control"), "no-store")
                status, page_headers, page = _raw_request(
                    port,
                    "GET",
                    "/",
                )
                self.assertEqual(status, 200)
                self.assertEqual(page_headers.get("Cache-Control"), "no-store")
                self.assertIn(
                    "img-src 'self' data: http://127.0.0.1:8780",
                    page_headers.get("Content-Security-Policy", ""),
                )
                self.assertNotIn("__WORKSPACE_ASSET_VERSION__", page)
                self.assertIn(
                    f'/assets/workspace.js?v={version["version"]}',
                    page,
                )

                status, headers, link = _request(
                    port,
                    "POST",
                    "/api/enrollment/start",
                    body={},
                    origin=origin,
                )
                self.assertEqual(status, 201)
                cookies = _cookies(headers)
                service.confirm_workspace_link(
                    user_subject="user-a",
                    token_id="token-a",
                    agent_host="openclaw",
                    endpoint_key="telegram:*:alice",
                    client_type="telegram",
                    external_subject="alice",
                    conversation_ref="agent:main:telegram:direct:alice",
                    link_code=link["linkCode"],
                )
                status, headers, completed = _request(
                    port,
                    "POST",
                    "/api/enrollment/complete",
                    body={"username": "alice", "password": PASSWORD},
                    origin=origin,
                    cookies=cookies,
                )
                self.assertEqual(status, 201)
                self.assertTrue(completed["authenticated"])
                cookies.update(_cookies(headers))

                service.skills.record_load("user-a", "oa-work-log", "preview", "SKILL.md",
                    {"status": "succeeded", "binding_id": "owned", "version": "1.0.0"})
                service.skills.record_load("user-b", "oa-pending", "batch", "SKILL.md",
                    {"status": "rejected", "error": {"code": "SKILL_UNAVAILABLE"}})
                denied, _, _ = _request(port, "GET", "/api/skills/history")
                self.assertEqual(denied, 401)
                status, _, history = _request(port, "GET", "/api/skills/history", cookies=cookies)
                self.assertEqual(status, 200)
                self.assertEqual([x["skill_id"] for x in history["items"]], ["oa-work-log"])

                self.assertEqual(_request(port, "GET", "/api/skill-drafts")[0], 401)
                proposal={"name":"周报助手","description":"归纳输入","selection":{"use_when":"周报","not_for":"业务写入","output":"摘要"},"instructions":"根据输入归纳，不编造。"}
                draft_body={"action":"save","data":{"proposal":proposal,"request_key":"http-draft"}}
                self.assertEqual(_request(port,"POST","/api/skill-drafts",origin=origin,cookies=cookies,body=draft_body)[0],401)
                status,_,saved=_request(port,"POST","/api/skill-drafts",origin=origin,cookies=cookies,csrf=cookies["agentbridge_workspace_csrf"],body=draft_body)
                self.assertEqual(status,200)
                other=service.skill_authoring.save("user-b",proposal=proposal,request_key="other")
                self.assertEqual(_request(port,"GET","/api/skill-drafts?id="+other["draft_id"],cookies=cookies)[0],404)
                status,_,drafts=_request(port,"GET","/api/skill-drafts",cookies=cookies)
                self.assertEqual([d["draft_id"] for d in drafts["items"]],[saved["draft_id"]])
                self.assertEqual(_request(port,"POST","/api/skill-drafts",origin=origin,cookies=cookies,csrf=cookies["agentbridge_workspace_csrf"],body={"action":"decide","data":{}})[0],400)

                original_path = '/api/database/logs/taihua_primary/123'
                with patch.object(application, 'database_original', return_value={
                    'author': '测试作者', 'paragraphs': [{'number': 1, 'text': '<img src=x onerror=evil()>'}],
                }) as original:
                    denied, _, _ = _request(port, 'GET', original_path)
                    self.assertEqual(denied, 401)
                    original.assert_not_called()
                    status, original_headers, body = _request(port, 'GET', original_path, cookies=cookies)
                    self.assertEqual(status, 200)
                    self.assertEqual(original_headers.get('Cache-Control'), 'no-store')
                    self.assertEqual(body['author'], '测试作者')
                    self.assertEqual(original.call_args.args[0]['user_subject'], 'user-a')
                    from bscli.database.independent import DatabaseRejected
                    for code, expected_status in [('DATABASE_LOG_NOT_FOUND', 404), ('DATABASE_CAPABILITY_DENIED', 403),
                                                  ('DATABASE_AUTHORIZATION_CHANGED', 403)]:
                        original.side_effect = DatabaseRejected(code)
                        status, _, body = _request(port, 'GET', original_path, cookies=cookies)
                        self.assertEqual(status, expected_status)
                        self.assertEqual(body['code'], code)
                status, original_headers, shell = _raw_request(port, 'GET', '/database/logs/taihua_primary/123')
                self.assertEqual(status, 200)
                self.assertNotIn('测试作者', shell)
                self.assertNotIn('__WORKSPACE_ASSET_VERSION__', shell)
                self.assertIn('/assets/original.js?v=', shell)

                csv_path = "/api/database/reports/taihua_primary/" + "a" * 32 + "/download"
                with patch.object(application, "database_report", return_value={
                    "body": b"date,hours\r\n2026-09-01,1.5\r\n",
                    "filename": "taihua-personal-daily.csv", "content_type": "text/csv; charset=utf-8",
                }) as csv_download:
                    denied, _, _ = _raw_request(port, "GET", csv_path)
                    self.assertEqual(denied, 401)
                    csv_download.assert_not_called()
                    status, csv_headers, body = _raw_request(port, "GET", csv_path, cookies=cookies)
                    self.assertEqual(status, 200)
                    self.assertIn("no-store", csv_headers["Cache-Control"])
                    self.assertIn("1.5", body)
                    self.assertEqual(csv_download.call_args.args[0]["user_subject"], "user-a")

                status, response_headers, session = _request(
                    port,
                    "GET",
                    "/api/session",
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                self.assertTrue(session["authenticated"])
                self.assertEqual(
                    response_headers.get("X-Frame-Options"),
                    "DENY",
                )
                self.assertNotIn(
                    "userSubject",
                    json.dumps(session, ensure_ascii=False),
                )

                stored_attachment = service.timeline_attachments.create_many(
                    user_subject="user-a",
                    message_key="workspace:user:http-download",
                    attachments=[
                        {
                            "type": "image",
                            "mimeType": "image/png",
                            "fileName": "证书列表.png",
                            "content": base64.b64encode(
                                b"\x89PNG\r\n\x1a\nworkspace-download"
                            ).decode("ascii"),
                        }
                    ],
                    media_base_url="http://127.0.0.1:8780",
                )[0]
                status, download_headers, downloaded = _bytes_request(
                    port,
                    "GET",
                    (
                        "/api/timeline/attachments/"
                        f"{stored_attachment['attachment_id']}/download"
                    ),
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    download_headers.get_content_type(),
                    "image/png",
                )
                self.assertIn(
                    "attachment;",
                    download_headers.get("Content-Disposition", ""),
                )
                self.assertIn(
                    "filename*=UTF-8''",
                    download_headers.get("Content-Disposition", ""),
                )
                self.assertEqual(
                    downloaded,
                    b"\x89PNG\r\n\x1a\nworkspace-download",
                )

                endpoint = service.tasks.endpoint_for_key(
                    user_subject="user-a",
                    agent_host="openclaw",
                    endpoint_key="telegram:*:alice",
                )
                task, _ = service.tasks.ensure_task(
                    user_subject="user-a",
                    agent_host="openclaw",
                    host_task_key="workspace|historical-file",
                    origin_endpoint_id=endpoint["endpoint_id"],
                    active_conversation_ref=endpoint["conversation_ref"],
                    title="Prepare and Deliver One OA Certificate Scan",
                )
                grant = service.document_downloads.create(
                    user_subject="user-a",
                    system_id="oa",
                    session_id="session-a",
                    document={"resource_id": "resource-a"},
                    filename="certificate.pdf",
                    document_type="patent_certificate",
                    display_size="2 MB",
                    card_base_url="https://10.10.50.213:8780",
                )
                artifact, _ = service.tasks.link_artifact(
                    task_id=task["task_id"],
                    user_subject="user-a",
                    artifact={
                        "artifact_type": "certificate_scan",
                        "source_ref": grant["download_id"],
                        "filename": "certificate.pdf",
                        "content_type": "application/pdf",
                        "byte_size": 2048,
                        "download_url": f"{grant['card_url']}/file",
                        "expires_at": "2099-07-30T00:30:00+00:00",
                    },
                )
                with service.tasks._connect() as connection:
                    connection.execute(
                        "UPDATE task_artifacts SET state = 'expired' "
                        "WHERE artifact_id = ?",
                        (artifact["artifact_id"],),
                    )

                status, _, history = _request(
                    port,
                    "GET",
                    "/api/artifacts/history?limit=20",
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(history["items"]), 1)
                historical = history["items"][0]["artifacts"][0]
                self.assertEqual(historical["state"], "expired")
                self.assertIsNone(historical["download_url"])
                self.assertNotIn("source_ref", historical)

                reissue_path = (
                    f"/api/tasks/{task['task_id']}/artifacts/"
                    f"{artifact['artifact_id']}/reissue"
                )
                status, _, error = _request(
                    port,
                    "POST",
                    reissue_path,
                    body={},
                    origin=origin,
                    cookies=cookies,
                )
                self.assertEqual(status, 401)
                self.assertEqual(
                    error["error"]["code"],
                    "AUTHENTICATION_REQUIRED",
                )
                status, _, error = _request(
                    port,
                    "POST",
                    reissue_path,
                    body={},
                    origin=origin,
                    cookies=cookies,
                    csrf=cookies["agentbridge_workspace_csrf"],
                )
                self.assertEqual(status, 409)
                self.assertEqual(error["error"]["code"], "LOGIN_REQUIRED")

                status, _, error = _request(
                    port,
                    "POST",
                    "/api/chat/send",
                    body={"message": "读取待办"},
                    origin=origin,
                    cookies=cookies,
                )
                self.assertEqual(status, 401)
                self.assertEqual(
                    error["error"]["code"],
                    "AUTHENTICATION_REQUIRED",
                )

                status, _, accepted = _request(
                    port,
                    "POST",
                    "/api/chat/send",
                    body={"message": "读取待办"},
                    origin=origin,
                    cookies=cookies,
                    csrf=cookies["agentbridge_workspace_csrf"],
                )
                self.assertEqual(status, 202)
                self.assertTrue(accepted["runId"])

                status, response_headers, stream = _raw_request(
                    port,
                    "POST",
                    "/api/chat/send-stream",
                    body={
                        "message": "读取待办",
                        "idempotencyKey": "web-stream-1",
                    },
                    origin=origin,
                    cookies=cookies,
                    csrf=cookies["agentbridge_workspace_csrf"],
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    response_headers.get_content_type(),
                    "text/event-stream",
                )
                self.assertIn("event: progress", stream)
                self.assertIn("event: chat", stream)
                self.assertNotIn("user_subject", stream)

                status, _, timeline = _request(
                    port,
                    "GET",
                    "/api/timeline?limit=20",
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    [
                        item["role"]
                        for item in timeline["items"]
                        if item["entry_type"] == "chat_message"
                    ],
                    ["user", "assistant", "user", "assistant"],
                )
                self.assertEqual(
                    timeline["cursor"],
                    max(item["sequence"] for item in timeline["items"]),
                )
                self.assertNotIn(
                    "user_subject",
                    json.dumps(timeline, ensure_ascii=False),
                )

                status, _, chat_page = _request(
                    port,
                    "GET",
                    "/api/timeline?entry_type=chat_message&limit=2",
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(chat_page["items"]), 2)
                self.assertTrue(chat_page["hasMore"])
                self.assertTrue(
                    all(
                        item["entry_type"] == "chat_message"
                        for item in chat_page["items"]
                    )
                )
                self.assertEqual(
                    chat_page["oldestSequence"],
                    chat_page["items"][0]["sequence"],
                )

                status, _, older_chat_page = _request(
                    port,
                    "GET",
                    (
                        "/api/timeline?entry_type=chat_message&limit=2&before="
                        f"{chat_page['oldestSequence']}"
                    ),
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(older_chat_page["items"]), 2)
                self.assertFalse(older_chat_page["hasMore"])
                self.assertLess(
                    older_chat_page["items"][-1]["sequence"],
                    chat_page["oldestSequence"],
                )

                status, _, endpoints = _request(
                    port,
                    "GET",
                    "/api/endpoints",
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                self.assertTrue(endpoints["items"])
                self.assertTrue(
                    all(
                        "user_subject" not in item
                        and "token_id" not in item
                        and "route" not in item
                        for item in endpoints["items"]
                    )
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class WorkspaceStaticAssetTests(unittest.TestCase):
    def test_assets_are_csp_clean_and_mobile_detail_has_back_control(self) -> None:
        root = Path(__file__).resolve().parents[1] / "bscli" / "workspace" / "static"
        page = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "workspace.js").read_text(encoding="utf-8") + "\n" + "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(root.glob("*.mjs"))
        )
        stylesheet = (root / "workspace.css").read_text(encoding="utf-8")

        self.assertNotIn('style="', page)
        self.assertNotIn("font-size: clamp(", stylesheet)
        self.assertNotIn("linear-gradient(", stylesheet)
        self.assertIn("mobile-detail-back", script)
        self.assertIn("mobile-detail-back", stylesheet)
        self.assertIn('class="task-detail mobile-empty"', page)
        self.assertIn("const loginForm = event.currentTarget;", script)
        self.assertIn("const enrollmentForm = event.currentTarget;", script)
        self.assertIn("页面已刷新，请重新生成配对码。", script)
        self.assertNotIn("setBusy(event.currentTarget", script)
        self.assertIn('fetch("/api/chat/send-stream"', script)
        self.assertIn("fetchChatStreamResponse", script)
        self.assertIn("attempt < 2", script)
        self.assertIn("error instanceof TypeError", script)
        self.assertIn("idempotencyKey", script)
        self.assertIn('id="image-input"', page)
        self.assertIn('id="composer-attachments"', page)
        self.assertIn('addEventListener("paste", handleComposerPaste)', script)
        self.assertIn("MAX_COMPOSER_IMAGES_TOTAL_BYTES", script)
        self.assertIn("attachments: attachments.map", script)
        self.assertIn("readAsDataURL", script)
        self.assertIn("composer-attachments", stylesheet)
        self.assertIn("message-image-list", stylesheet)
        self.assertIn('id="image-viewer"', page)
        self.assertIn('id="image-viewer-download"', page)
        self.assertIn("openImageViewer", script)
        self.assertIn("closeImageViewer", script)
        self.assertIn("message-image-button", stylesheet)
        self.assertIn("image-viewer-canvas", stylesheet)
        self.assertIn("download = fileName", script)
        self.assertIn("/api/timeline/attachments/", script)
        self.assertIn("response.body.getReader()", script)
        self.assertIn("activeStreams: new Map()", script)
        self.assertIn("gatewayStatusTimer: null", script)
        self.assertIn("GATEWAY_STATUS_ONLINE_POLL_MS = 30000", script)
        self.assertIn("GATEWAY_STATUS_OFFLINE_POLL_MS = 5000", script)
        self.assertIn("state.gatewayStatusCheckActive", script)
        self.assertIn("state.gatewayStatusPolling = true", script)
        self.assertIn("state.gatewayStatusPolling = false", script)
        self.assertIn("loadGatewayStatus();", script)
        self.assertIn("reconcileOriginChatMessage", script)
        self.assertIn("timelineEntryImages", script)
        self.assertIn("removeMatchingHistoryMessage", script)
        self.assertIn('"plan.result.ready": "组合任务结果已生成"', script)
        self.assertIn("renderTaskPlanResult(plan.resultProjection)", script)
        self.assertIn("task-plan-result", stylesheet)
        self.assertIn("请处理附加图片中的内容。", script)
        self.assertIn("activeStream.controller.abort()", script)
        self.assertIn("await closeReader();", script)
        self.assertIn("parseSseBlock", script)
        self.assertIn("handleChatProgress", script)
        self.assertIn("handleChatDelta", script)
        self.assertIn("const previousRunId = runId", script)
        self.assertIn("adoptLiveMessage(previousRunId, runId", script)
        self.assertIn("streamFailure.safeToRetry === true", script)
        self.assertIn("hydrateTaskCards", script)
        self.assertIn("upsertTaskCard", script)
        self.assertIn(
            'api("/api/timeline?entry_type=task_event&limit=240")',
            script,
        )
        self.assertIn(
            'api("/api/timeline?entry_type=chat_message&limit=500")',
            script,
        )
        self.assertIn("loadOlderChatMessages", script)
        self.assertIn("加载更早消息", script)
        self.assertIn("chatTimelineOldestSequence", script)
        self.assertIn("chat-history-control", stylesheet)
        self.assertNotIn("images.length === 0 || !entry.entry_id", script)
        self.assertIn("openTimelineStream", script)
        self.assertIn("renderChatTimeline", script)
        self.assertEqual(script.count("dismissTerminalLiveMessages();"), 2)
        self.assertIn('classList.contains("failed")', script)
        self.assertIn("parseTimestampMilliseconds", script)
        self.assertIn(r"/^\d{10,13}$/", script)
        self.assertIn("state.taskCards.get(cardKey)", script)
        self.assertIn("upsertTaskCardVariant", script)
        self.assertIn("result.interactions", script)
        self.assertIn(":interaction:${interaction.interactionId}", script)
        self.assertIn("taskCardStatusForInteraction", script)
        self.assertIn("displayTaskTitle", script)
        self.assertIn("OA 出差申请提交", script)
        self.assertIn('source.addEventListener("cursor"', script)
        self.assertIn("reconcileTimeline", script)
        self.assertIn('api("/api/client-version")', script)
        self.assertIn("location.reload();", script)
        self.assertIn("timelineReconnectTimer", script)
        self.assertIn("__WORKSPACE_ASSET_VERSION__", page)
        self.assertIn("task.completed", script)
        self.assertIn('api("/api/artifacts/history?limit=20")', script)
        self.assertIn("重新生成下载", script)
        self.assertIn("reissueArtifact", script)
        self.assertIn("task.artifact.refreshed", script)
        self.assertIn("task.artifact.delivery", script)
        self.assertIn("taskCardArtifactDeliveryMessage", script)
        self.assertIn("artifactDeliveryAggregate", script)
        self.assertIn("份已改发下载链接", script)
        self.assertIn("OA 证书查询与下载", script)
        self.assertIn("task-artifact-reissue", stylesheet)
        self.assertNotIn('"任务状态已更新",', script)
        self.assertIn("childElementCount > 2", script)
        self.assertIn("application-card", stylesheet)
        self.assertIn('continueButton.textContent = "继续任务"', script)
        self.assertIn("/continue`,", script)
        self.assertIn("detail-heading-actions", stylesheet)


def _service(tmp: str) -> CentralCapabilityService:
    return authorized_service(
        home=tmp,
        base_url="http://127.0.0.1:8000/seeyon",
    )


def _create_account(
    service: CentralCapabilityService,
    *,
    user_subject: str,
    username: str,
    endpoint_key: str,
    client_type: str = "telegram",
) -> dict:
    link = service.workspace.start_link()
    service.confirm_workspace_link(
        user_subject=user_subject,
        token_id=f"token-{username}",
        agent_host="openclaw",
        endpoint_key=endpoint_key,
        client_type=client_type,
        external_subject=username,
        conversation_ref=f"agent:main:{client_type}:direct:{username}",
        link_code=link["link_code"],
    )
    account = service.workspace.create_account(
        enrollment_token=link["enrollment_token"],
        username=username,
        password=PASSWORD,
    )
    return service.register_workspace_endpoint(
        account_id=account["account_id"],
    )["account"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    origin: str | None = None,
    cookies: dict[str, str] | None = None,
    csrf: str | None = None,
) -> tuple[int, http.client.HTTPMessage, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if origin:
        headers["Origin"] = origin
    if cookies:
        headers["Cookie"] = "; ".join(
            f"{name}={value}" for name, value in cookies.items()
        )
    if csrf:
        headers["X-AgentBridge-CSRF"] = csrf
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    result = (response.status, response.headers, parsed)
    connection.close()
    return result


def _raw_request(
    port: int,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    origin: str | None = None,
    cookies: dict[str, str] | None = None,
    csrf: str | None = None,
) -> tuple[int, http.client.HTTPMessage, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if origin:
        headers["Origin"] = origin
    if cookies:
        headers["Cookie"] = "; ".join(
            f"{name}={value}" for name, value in cookies.items()
        )
    if csrf:
        headers["X-AgentBridge-CSRF"] = csrf
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    result = (response.status, response.headers, raw)
    connection.close()
    return result


def _bytes_request(
    port: int,
    method: str,
    path: str,
    *,
    cookies: dict[str, str] | None = None,
) -> tuple[int, http.client.HTTPMessage, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}"}
    if cookies:
        headers["Cookie"] = "; ".join(
            f"{name}={value}" for name, value in cookies.items()
        )
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    result = (response.status, response.headers, raw)
    connection.close()
    return result


def _cookies(headers: http.client.HTTPMessage) -> dict[str, str]:
    result = {}
    for value in headers.get_all("Set-Cookie") or []:
        pair = value.split(";", 1)[0]
        name, cookie_value = pair.split("=", 1)
        result[name] = cookie_value
    return result


def _wait_until(predicate, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


class _CaptureInput:
    def __init__(self) -> None:
        self.value = ""

    def write(self, value: str) -> int:
        self.value += value
        return len(value)

    def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
