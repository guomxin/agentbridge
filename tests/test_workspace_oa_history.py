from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from agentbridge.core.business_content_identity import principal_binding
from agentbridge.core.oa_history_queries import OaHistoryQueryStore, OaHistoryQueryError
from agentbridge.workspace.oa_history import grouping, presentation, next_page


class WorkspaceHistoryBoundaryTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="workspace-history-")
        self.addCleanup(directory.cleanup)
        self.store = OaHistoryQueryStore(Path(directory.name)/"query.db")
        self.session = dict(user_subject="alice",system_id="oa",session_id="session-a",state="active",
            expected_principal_ref="alice-oa", downstream_principal_ref="alice-oa")
        self.binding = dict(user_subject="alice",session_id="session-a",principal_ref=principal_binding(self.session))
        self.created = self.store.create(**self.binding, query={"collection":"done"})
        self.result = self.store.advance(self.created["cursor"], **self.binding, expected_version=1,
            state="scan",state_data={"rows":[]},response={"schema_version":"agentbridge.oa_history_query.v1",
                "query_id":self.created["record"]["query_id"],"state":"scanning","items":[],"has_more":True},next_stage="scan")
        self.service = SimpleNamespace(oa_history_queries=self.store, sessions=Mock(),tasks=Mock(),operations=Mock(),
            require_task_result_access=Mock(),invoke=Mock(return_value={"status":"succeeded","result":self.result}))
        self.service.sessions.find.return_value=self.session
        self.service.tasks._connect.side_effect=lambda:nullcontext(None)
        self.service.tasks.operation_ids_for_task.return_value=["operation-a"]
        self.service.operations.get.return_value={"user_subject":"alice","capability_name":"oa.workflow.history.query","result":self.result}
        self.account={"user_subject":"alice","endpoint_id":"endpoint-a"}
        self.endpoint=patch("agentbridge.core.actionable_results._account",return_value=self.account)
        self.endpoint.start();self.addCleanup(self.endpoint.stop)

    def test_refresh_uses_saved_owned_projection_without_downstream_read(self):
        self.assertEqual(presentation(self.service,"alice","task-a"),self.result)
        self.service.invoke.assert_not_called()
        self.service.require_task_result_access.assert_called_with(user_subject="alice",task_id="task-a")

    def test_next_calls_central_read_with_only_the_bound_cursor(self):
        cursor=self.result["next_cursor"]
        self.assertEqual(next_page(self.service,self.account,"task-a",{"cursor":cursor}),{"oaHistoryQuery":self.result})
        self.service.invoke.assert_called_once_with(user_subject="alice",capability_name="oa.workflow.history.next",
            arguments={"cursor":cursor},host_type="workspace",origin_endpoint_id="endpoint-a")

    def test_other_task_query_and_forged_conditions_cannot_continue(self):
        other=self.store.create(**self.binding,query={"collection":"sent"})
        with self.assertRaises(OaHistoryQueryError):
            next_page(self.service,self.account,"task-a",{"cursor":other["cursor"]})
        with self.assertRaises(ValueError):
            next_page(self.service,self.account,"task-a",{"cursor":self.result["next_cursor"],"keyword":"changed"})
        self.service.invoke.assert_not_called()

    def test_session_switch_clears_saved_data_and_prevents_continuation(self):
        self.session["downstream_principal_ref"]="bob"
        value=presentation(self.service,"alice","task-a")
        self.assertEqual(value["state"],"unavailable")
        self.assertEqual(value["items"],[])
        self.assertIsNone(value["next_cursor"])
        with self.assertRaises(OaHistoryQueryError) as caught:
            next_page(self.service,self.account,"task-a",{"cursor":self.result["next_cursor"]})
        self.assertEqual(caught.exception.status,401)
        self.service.invoke.assert_not_called()

    def test_revocation_is_checked_on_refresh_and_next(self):
        self.service.require_task_result_access.side_effect=PermissionError("revoked")
        with self.assertRaises(PermissionError):presentation(self.service,"alice","task-a")
        with self.assertRaises(PermissionError):next_page(self.service,self.account,"task-a",{"cursor":self.result["next_cursor"]})
        self.service.invoke.assert_not_called()

    def test_central_login_failure_is_not_an_empty_success(self):
        self.service.invoke.return_value={"status":"waiting_user","error":{"code":"LOGIN_REQUIRED","message":"请登录"}}
        with self.assertRaises(OaHistoryQueryError) as caught:
            next_page(self.service,self.account,"task-a",{"cursor":self.result["next_cursor"]})
        self.assertEqual(caught.exception.status,401)

    def continuation_task(self):
        self.service.operations.get.return_value = {"user_subject": "alice", "status": "succeeded",
            "capability_name": "oa.workflow.history.next", "result": deepcopy(self.result)}
        return {"task_id": "task-a", "user_subject": "alice", "status": "succeeded",
                "current_operation_id": "operation-a"}

    def test_grouping_marks_only_one_successful_owned_continuation(self):
        task = self.continuation_task()
        for state in ("scanning", "details", "complete", "partial"):
            with self.subTest(state=state):
                query = {**self.result, "state": state}
                self.assertEqual(grouping(self.service, "alice", task, query),
                    {"queryId": self.result["query_id"], "eligible": True})
        self.service.invoke.assert_not_called()

    def test_grouping_keeps_initial_queries_plans_batches_and_mixed_operations_visible(self):
        task = self.continuation_task()
        for capability in ("oa.workflow.history.query", "oa.workflow.pending.list", "transform.oa_history_report.v1",
                           "oa.workflow.approve", "unknown.future.read"):
            with self.subTest(capability=capability):
                self.service.operations.get.return_value["capability_name"] = capability
                self.assertIsNone(grouping(self.service, "alice", task, self.result))
        self.continuation_task()
        for context in ({"plan": {}}, {"batch": {}}):
            with self.subTest(context=context):
                self.assertIsNone(grouping(self.service, "alice", task, self.result, **context))
        for operations in ([], ["operation-a", "operation-b"]):
            with self.subTest(operations=operations):
                self.service.tasks.operation_ids_for_task.return_value = operations
                self.assertIsNone(grouping(self.service, "alice", task, self.result))

    def test_grouping_never_hides_failed_unknown_or_incomplete_operations(self):
        task = self.continuation_task()
        for status in ("failed", "outcome_unknown", "canceled", "expired", "running", "waiting_user"):
            with self.subTest(task_status=status):
                self.assertIsNone(grouping(self.service, "alice", {**task, "status": status}, self.result))
        for status in ("failed", "unknown", "pending", "running", "requires_user_action"):
            with self.subTest(operation_status=status):
                self.service.operations.get.return_value["status"] = status
                self.assertIsNone(grouping(self.service, "alice", task, self.result))

    def test_grouping_requires_exact_task_owner_operation_and_query_binding(self):
        task = self.continuation_task()
        self.assertIsNone(grouping(self.service, "alice", {**task, "user_subject": "bob"}, self.result))
        self.assertIsNone(grouping(self.service, "alice", {**task, "current_operation_id": "other"}, self.result))
        for changes in ({"user_subject": "bob"}, {"result": {**self.result, "query_id": "other-query"}},
                        {"result": {**self.result, "schema_version": "unknown"}}, {"result": None}):
            with self.subTest(changes=changes):
                self.continuation_task()
                self.service.operations.get.return_value.update(changes)
                self.assertIsNone(grouping(self.service, "alice", task, self.result))

    def test_grouping_keeps_unavailable_audit_cards_foldable_without_granting_result_access(self):
        task = self.continuation_task()
        for query in (None, {**self.result, "state": "error"}):
            with self.subTest(query=query):
                self.assertIsNone(grouping(self.service, "alice", task, query))
        for changes in ({"state": "expired"}, {"session_id": "new-session"}, {"downstream_principal_ref": "bob"}):
            with self.subTest(session=changes):
                self.service.sessions.find.return_value = {**self.session, **changes}
                query = presentation(self.service, "alice", "task-a")
                self.assertEqual(query["state"], "unavailable")
                self.assertEqual(query["items"], [])
                self.assertIsNone(query["next_cursor"])
                self.assertEqual(grouping(self.service, "alice", task, query),
                    {"queryId": self.result["query_id"], "eligible": True})
        self.service.sessions.find.return_value = self.session
        self.store.clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=31)
        query = presentation(self.service, "alice", "task-a")
        self.assertEqual(query["error"]["code"], "OA_HISTORY_QUERY_EXPIRED")
        self.assertEqual(query["items"], [])
        self.assertIsNone(query["next_cursor"])
        self.assertEqual(grouping(self.service, "alice", task, query),
            {"queryId": self.result["query_id"], "eligible": True})
        self.service.invoke.assert_not_called()

    def test_grouping_rechecks_current_permission_even_for_unavailable_results(self):
        task = self.continuation_task()
        self.service.require_task_result_access.side_effect = PermissionError("revoked")
        for state in ("scanning", "unavailable"):
            with self.subTest(state=state), self.assertRaises(PermissionError):
                grouping(self.service, "alice", task, {**self.result, "state": state})

    def test_workspace_projects_the_marker_in_detail_and_bounded_refresh_without_reading_oa(self):
        from agentbridge.workspace.application import WorkspaceApplication
        from tests.test_workspace import _create_account, _service
        with TemporaryDirectory(prefix="workspace-history-grouping-") as home:
            service = _service(home)
            account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
            app = WorkspaceApplication(service=service)
            self.addCleanup(app.close)
            session = service.sessions.get_or_create(user_subject="user-a", system_id="oa", expected_principal_ref="alice-oa")
            session = service.sessions.activate(session["session_id"], observed_principal_ref="alice-oa")
            binding = dict(user_subject="user-a", session_id=session["session_id"], principal_ref=principal_binding(session))
            created = service.oa_history_queries.create(**binding, query={"collection": "done"})
            response = service.oa_history_queries.advance(created["cursor"], **binding, expected_version=1,
                state="scan", state_data={"rows": []}, response={**self.result, "query_id": created["record"]["query_id"]})
            task, _ = service.tasks.ensure_task(user_subject="user-a", agent_host="openclaw", host_task_key="history-next",
                origin_endpoint_id=account["endpoint_id"], active_conversation_ref=account["openclaw_session_key"], title="Continue history query")
            operation, _ = service.operations.create(user_subject="user-a", capability_name="oa.workflow.history.next",
                capability_version="0.1.0", input_summary={"cursor": created["cursor"]}, idempotency_key="history-next")
            operation = service.operations.mark_succeeded(operation["operation_id"], response)
            service.tasks.link_operation(task_id=task["task_id"], user_subject="user-a", operation=operation)
            expected = {"queryId": response["query_id"], "eligible": True}
            with patch.object(service, "invoke", side_effect=AssertionError("projection must not read OA")):
                detail = app.task_detail(account, task["task_id"])
                self.assertEqual(detail["oaHistoryGrouping"], expected)
                bounded = app.task_projections(account, [task["task_id"]])["items"][0]["detail"]
                self.assertEqual(bounded["oaHistoryGrouping"], expected)
                service.sessions.mark_expired(session["session_id"], "synthetic expiry")
                unavailable = app.task_detail(account, task["task_id"])
                self.assertEqual(unavailable["oaHistoryGrouping"], expected)
                self.assertEqual(unavailable["oaHistoryQuery"]["state"], "unavailable")
                self.assertEqual(unavailable["oaHistoryQuery"]["items"], [])
                self.assertIsNone(unavailable["oaHistoryQuery"]["next_cursor"])
