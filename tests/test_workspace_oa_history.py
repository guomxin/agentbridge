from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from agentbridge.core.business_content_identity import principal_binding
from agentbridge.core.oa_history_queries import OaHistoryQueryStore, OaHistoryQueryError
from agentbridge.workspace.oa_history import presentation, next_page


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
