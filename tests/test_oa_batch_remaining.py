"""Stopped batches may re-query untouched rows, never replay their decisions."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from agentbridge.core import actionable_results as actions
from agentbridge.core.oa_batch_remaining import queue_remaining_query, remaining_presentation
from tests.support.full_business_acceptance import OWNER
from tests.support.telegram_recovery_acceptance import TelegramRecoveryAcceptanceFixture
from tests.authorization_fixtures import grant_permissions
from tests.test_full_business_acceptance import drive


class RemainingBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = TelegramRecoveryAcceptanceFixture(Path(self.temp.name), cases=("batch-current",))
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        self.case = self.fixture.meta["telegram_recovery_cases"][0]
        self.task_id = self.case["task_id"]
        self.version = self.batch()["version"]
        self.runtime = {"endpointId":self.fixture.account["endpoint_id"], "hostType":"openclaw", "hostInstanceId":"remaining-synthetic-host"}

    def batch(self):
        return self.service.tasks.get_batch_for_task(parent_task_id=self.task_id, user_subject=OWNER, include_resource_refs=True)

    def queue(self, body=None):
        return queue_remaining_query(self.service, self.fixture.account, self.task_id,
            body or {"requestKey":uuid4().hex, "expectedBatchVersion":self.version})

    def execute(self, queued, endpoint=None):
        return actions.execute_preparation(self.service, OWNER, queued["taskId"], queued["commandKey"],
            runtime_context={**self.runtime, **({"endpointId":endpoint} if endpoint else {})})

    def count(self, table):
        with self.service.tasks._connect() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_real_unknown_only_untouched_tail_requeries_with_no_new_decision(self):
        original = self.batch()
        audit = self.fixture.audit()
        view = remaining_presentation(self.service, OWNER, self.task_id)
        self.assertTrue(view["available"], view)
        self.assertEqual([item["ordinal"] for item in view["items"]], [3])
        with patch.object(self.service, "invoke", side_effect=AssertionError("HTTP must not invoke")):
            queued = self.queue()
        response = self.execute(queued)
        self.assertEqual(response["status"], "succeeded", response)
        self.assertEqual(response["queryResult"]["remaining"]["matchedCount"], 1)
        groups = actions.list_actionable_results(self.service, OWNER, queued["taskId"])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["targets"]), 1)
        self.assertEqual(groups[0]["remaining"]["items"][0]["ordinal"], 3)
        self.assertTrue(groups[0]["targets"][0]["enabled"])
        with patch.object(self.service, "invoke", side_effect=AssertionError("no duplicate read")):
            self.assertTrue(self.execute(queued)["reused"])
        self.assertEqual(self.batch(), original)
        self.assertEqual(self.fixture.audit()["decisions"], audit["decisions"])
        self.assertEqual(self.fixture.audit()["syntheticWrites"], audit["syntheticWrites"])
        self.assertTrue(all(row["authorizationUnchanged"] for row in self.fixture.audit()["originals"]))
        grant_permissions(self.service.user_grants, OWNER, [])
        denied = self.execute(queued)
        self.assertEqual(denied["error"]["code"], "ACTION_PERMISSION_DENIED")
        completed = self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["summary"]["command"]["state"], "succeeded")

    def test_server_rejects_current_unknown_or_unrelated_target_even_if_client_invents_key(self):
        self.fixture.oa.add_affair("synthetic-not-in-original-batch", profile="leave")
        queued = self.queue()
        response = self.execute(queued)
        operation = self.service.operations.get(response["operationId"])
        groups = actions.list_actionable_results(self.service, OWNER, queued["taskId"])
        allowed = groups[0]["targets"][0]["targetKey"]
        for row in operation["result"]["items"]:
            key = actions._target_key(operation["operation_id"], row)
            if key == allowed:
                continue
            with self.assertRaises(actions.ActionableResultError) as caught:
                actions.queue_preparation(self.service, self.fixture.account, queued["taskId"],
                    {"operationId":operation["operation_id"], "targetKeys":[key], "requestKey":uuid4().hex})
            self.assertEqual(caught.exception.code, "BATCH_REMAINING_SELECTION_INVALID")
        # A separately linked read with identical content cannot borrow the
        # original attestation or renew the original result's actionable TTL.
        with patch("agentbridge.adapters.seeyon_central.datetime", wraps=datetime) as source_clock:
            source_clock.now.return_value = datetime.fromisoformat(operation["result"]["coverage"]["observedAt"])
            another = self.service.invoke(user_subject=OWNER, capability_name="oa.workflow.pending.list", arguments={},
                task_id=queued["taskId"], idempotency_key=uuid4().hex)
        self.assertEqual(another["result"], operation["result"])
        projected = actions.list_actionable_results(self.service, OWNER, queued["taskId"])
        extra = next(group for group in projected if group["operationId"] == another["operationId"])
        self.assertEqual(extra["targets"], [])
        self.assertTrue(extra["remaining"]["blocked"])
        with self.assertRaises(actions.ActionableResultError) as caught:
            actions.queue_preparation(self.service, self.fixture.account, queued["taskId"],
                {"operationId":another["operationId"], "targetKeys":[actions._target_key(another["operationId"], another["result"]["items"][0])],
                 "requestKey":uuid4().hex})
        self.assertEqual(caught.exception.code, "BATCH_REMAINING_UNVERIFIED")
        selected = actions.queue_preparation(self.service, self.fixture.account, queued["taskId"],
            {"operationId":operation["operation_id"], "targetKeys":[allowed], "requestKey":uuid4().hex})
        result = self.execute(selected)
        self.assertEqual(result["status"], "requires_user_action", result)
        self.assertNotEqual(selected["taskId"], self.task_id)
        self.assertEqual(self.batch()["current_ordinal"], 2)
        self.assertEqual([item["state"] for item in self.batch()["items"]], ["succeeded", "outcome_unknown", "queued"])

    def test_changed_and_missing_rows_are_reported_without_being_called_success(self):
        queued = self.queue()
        affair = self.batch()["items"][2]["resource_ref"]
        self.fixture.oa.data["affairs"][affair]["source"]["title"] += " 已变更"
        self.fixture.oa.persist()
        response = self.execute(queued)
        self.assertEqual(response["status"], "succeeded", response)
        group = actions.list_actionable_results(self.service, OWNER, queued["taskId"])[0]
        self.assertEqual(group["targets"], [])
        self.assertEqual(group["remaining"]["changedCount"], 1)
        second = self.queue()
        self.fixture.oa.data["affairs"][affair]["processed"] = True
        self.fixture.oa.persist()
        self.assertEqual(self.execute(second)["status"], "succeeded")
        group = actions.list_actionable_results(self.service, OWNER, second["taskId"])[0]
        self.assertEqual(group["remaining"]["missingCount"], 1)
        self.assertIn("不代表", group["remaining"]["items"][0]["reason"])

    def test_batch_version_or_current_identity_change_before_execution_denies_read(self):
        queued = self.queue()
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE task_batches SET version=version+1 WHERE parent_task_id=?", (self.task_id,))
        with patch.object(self.service, "invoke", side_effect=AssertionError("changed batch cannot read")):
            response = self.execute(queued)
        self.assertEqual(response["error"]["code"], "BATCH_REMAINING_CHANGED")
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE sessions SET expected_principal_ref='other',downstream_principal_ref='other' WHERE system_id='oa'")
        self.assertFalse(remaining_presentation(self.service, OWNER, self.task_id)["available"])
        with self.assertRaises(actions.ActionableResultError):
            self.queue({"requestKey":uuid4().hex, "expectedBatchVersion":self.version+1})

    def test_real_probe_mismatch_and_change_after_read_never_open_selection(self):
        queued = self.queue()
        adapter = self.service._adapters_by_system["oa"]
        with patch.object(adapter, "probe_business_draft_session", return_value={"observed_principal_ref":"other"}):
            response = self.execute(queued)
        self.assertEqual(response["error"]["code"], "BATCH_REMAINING_IDENTITY_CHANGED")
        second = self.queue()
        invoke = self.service.invoke
        def changed_after_read(**kwargs):
            result = invoke(**kwargs)
            with self.service.tasks._connect() as connection:
                connection.execute("UPDATE task_batches SET version=version+1 WHERE parent_task_id=?", (self.task_id,))
            return result
        with patch.object(self.service, "invoke", side_effect=changed_after_read):
            response = self.execute(second)
        self.assertEqual(response["error"]["code"], "BATCH_REMAINING_CHANGED")
        groups = actions.list_actionable_results(self.service, OWNER, second["taskId"])
        self.assertTrue(not groups or all(not group["targets"] for group in groups))

    def test_queue_is_idempotent_and_extra_or_stale_input_fails_closed(self):
        body = {"requestKey":uuid4().hex, "expectedBatchVersion":self.version}
        first = self.queue(body)
        self.assertEqual(self.queue(body)["taskId"], first["taskId"])
        for invalid in ({**body, "affairIds":["current"]}, {**body, "expectedBatchVersion":True},
                        {**body, "expectedBatchVersion":self.version-1}):
            with self.assertRaises(actions.ActionableResultError):
                self.queue(invalid)
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE task_batch_items SET interaction_id='touched' WHERE batch_id=? AND ordinal=3", (self.batch()["batch_id"],))
        self.assertFalse(remaining_presentation(self.service, OWNER, self.task_id)["available"])

    def test_telegram_explicit_post_issues_new_scoped_view_and_get_never_mints(self):
        token = self.fixture.meta["telegram_recovery_query"]["url"].rsplit("/",1)[1]
        body = {"taskId":self.task_id, "requestKey":uuid4().hex, "expectedBatchVersion":self.version}
        before = self.count("write_authorizations")
        queued = self.fixture.views.remaining_query(token, body)
        self.assertNotIn("resultView", queued)
        response = self.execute(queued, endpoint=self.fixture.telegram_endpoint["endpoint_id"])
        self.assertEqual(response["status"], "succeeded", response)
        with self.service.tasks._connect() as connection:
            issued = connection.execute("SELECT count(*) FROM task_events WHERE event_type='task.result_view.issued'").fetchone()[0]
        for _ in range(2):
            status = self.fixture.views.query_status(token, queued["taskId"])
            self.assertTrue(status["entryAvailable"])
        with self.service.tasks._connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM task_events WHERE event_type='task.result_view.issued'").fetchone()[0], issued)
        completed = self.fixture.views.remaining_query(token, body)
        new_token = completed["resultView"]["presentation"]["url"].rsplit("/",1)[1]
        state = self.fixture.views.read(new_token)
        self.assertEqual(len(state["groups"][0]["targets"]), 1)
        self.assertEqual(self.count("write_authorizations"), before)

    def test_original_unknown_assessment_and_task_version_do_not_change_remaining_scope(self):
        from agentbridge.core.oa_pending_recovery import probe_unknown_pending
        from agentbridge.core.task_projections import trusted_batch_projection
        queued = self.queue()
        before = self.service.tasks.get_task(self.task_id, user_subject=OWNER)
        assessment = probe_unknown_pending(self.service, subject=OWNER, task_id=self.task_id,
            request_key=uuid4().hex, endpoint_id=self.fixture.account["endpoint_id"])
        self.assertEqual(assessment["status"], "unresolved_still_pending")
        # Task metadata may advance independently of the immutable batch scope.
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE agent_tasks SET version=version+1 WHERE task_id=?", (self.task_id,))
        self.assertGreater(self.service.tasks.get_task(self.task_id, user_subject=OWNER)["version"], before["version"])
        self.assertEqual(self.batch()["version"], self.version)
        self.assertEqual(self.execute(queued)["queryResult"]["count"], 1)
        view = trusted_batch_projection(self.batch())
        self.assertEqual([item["state"] for item in view["items"]], ["succeeded", "outcome_unknown", "queued"])
        for item in view["items"]:
            self.assertEqual(set(item), {"ordinal", "title", "workflowLabel", "state", "isCurrent"})

    def test_authority_and_scope_changes_during_real_probe_or_read_fail_closed(self):
        original_permissions = self.service.user_grants.get(OWNER)["permissions"]
        adapter = self.service._adapters_by_system["oa"]
        original_probe, original_invoke = adapter.probe_business_draft_session, self.service.invoke
        cases = (("probe", "grant", "ACTION_PERMISSION_DENIED"),
                 ("read", "grant", "ACTION_PERMISSION_DENIED"),
                 ("read", "principal", "BATCH_REMAINING_IDENTITY_CHANGED"))
        for phase, change, expected in cases:
            with self.subTest(phase=phase, change=change):
                queued = self.queue()
                def mutate():
                    if change == "grant":
                        grant_permissions(self.service.user_grants, OWNER, [])
                    else:
                        with self.service.tasks._connect() as connection:
                            connection.execute("UPDATE sessions SET expected_principal_ref='other',downstream_principal_ref='other' WHERE system_id='oa'")
                def probe_then_change(*args, **kwargs):
                    result = original_probe(*args, **kwargs)
                    mutate()
                    return result
                def read_then_change(**kwargs):
                    result = original_invoke(**kwargs)
                    mutate()
                    return result
                try:
                    with patch.object(adapter, "probe_business_draft_session", side_effect=probe_then_change if phase == "probe" else original_probe), \
                         patch.object(self.service, "invoke", side_effect=read_then_change if phase == "read" else AssertionError("revoked probe cannot query")):
                        result = self.execute(queued)
                    self.assertEqual(result["error"]["code"], expected, result)
                    self.assertTrue(all(not group["targets"] for group in actions.list_actionable_results(self.service, OWNER, queued["taskId"])))
                finally:
                    grant_permissions(self.service.user_grants, OWNER, original_permissions)
                    with self.service.tasks._connect() as connection:
                        connection.execute("UPDATE sessions SET expected_principal_ref=?,downstream_principal_ref=? WHERE system_id='oa'", (OWNER, OWNER))

    def test_incomplete_query_and_batch_change_after_selection_never_prepare(self):
        from agentbridge.core.oa_batch_remaining import match_remaining
        queued = self.queue()
        response = self.execute(queued)
        operation = self.service.operations.get(response["operationId"])
        with self.service.tasks._connect() as connection:
            _, command = actions._load_command(connection, OWNER, queued["taskId"], queued["commandKey"])
            for corrupt in ({"coverage":{"status":"partial"}}, {"coverage":{"status":"complete", "hasMore":True}}, {"count":999}):
                with self.subTest(corrupt=corrupt), self.assertRaises(actions.ActionableResultError) as caught:
                    match_remaining(self.service, connection, OWNER, command, {**operation["result"], **corrupt})
                self.assertEqual(caught.exception.code, "ACTION_SOURCE_INCOMPLETE")
        group = actions.list_actionable_results(self.service, OWNER, queued["taskId"])[0]
        selected = actions.queue_preparation(self.service, self.fixture.account, queued["taskId"],
            {"operationId":group["operationId"], "targetKeys":[group["targets"][0]["targetKey"]], "requestKey":uuid4().hex})
        before = self.count("field_submissions")
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE task_batches SET version=version+1 WHERE parent_task_id=?", (self.task_id,))
        with patch.object(self.service, "invoke", side_effect=AssertionError("changed scope cannot prepare")):
            result = self.execute(selected)
        self.assertEqual(result["error"]["code"], "BATCH_REMAINING_CHANGED")
        self.assertEqual(self.count("field_submissions"), before)

    def test_cancel_during_read_stays_terminal_even_after_late_observation(self):
        queued = self.queue()
        invoke = self.service.invoke
        def canceled_after_read(**kwargs):
            result = invoke(**kwargs)
            self.service.tasks.cancel_task(task_id=queued["taskId"], user_subject=OWNER, reason="synthetic_cancel_during_scope_check")
            return result
        with patch.object(self.service, "invoke", side_effect=canceled_after_read):
            result = self.execute(queued)
        self.assertEqual(result["error"]["code"], "BATCH_REMAINING_CHANGED")
        task = self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)
        self.assertEqual(task["status"], "canceled")
        self.service.observe_host_task(user_subject=OWNER, task_id=queued["taskId"], operation_ids=[task["current_operation_id"]])
        self.assertEqual(self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)["status"], "canceled")
        self.assertEqual(actions.list_actionable_results(self.service, OWNER, queued["taskId"]), [])

    def test_legacy_missed_punch_real_unknown_preserves_legacy_fingerprint_contract(self):
        case = self.fixture.add_pending_batch(("missed_punch",) * 3, legacy=True)
        self.fixture.decide_case(case)
        second = drive(self.fixture, case)
        current = {**case, "input_id":second["nextAction"]["inputSubmissionId"],
            "input_interaction":second["interaction"]["interactionId"]}
        self.fixture.oa.data["affairs"][case["affair_ids"][1]]["readiness_case"] = "unknown"
        self.fixture.oa.persist()
        self.fixture.decide_case(current)
        drive(self.fixture, current)
        self.task_id = case["task_id"]
        self.version = self.batch()["version"]
        self.assertEqual(self.batch()["state"], "outcome_unknown")
        view = remaining_presentation(self.service, OWNER, self.task_id)
        self.assertTrue(view["available"], view)
        queued = self.queue()
        result = self.execute(queued)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["queryResult"]["remaining"]["matchedCount"], 1)
        self.assertEqual(result["queryResult"]["remaining"]["changedCount"], 0)
        target = actions.list_actionable_results(self.service, OWNER, queued["taskId"])[0]["targets"][0]
        self.assertTrue(target["enabled"])
        self.assertEqual(target["workflowType"], "missed_punch")

    def test_workspace_and_telegram_crash_recover_with_real_lease_without_repeating_read(self):
        class ProcessStopped(BaseException):
            pass
        from agentbridge.core import oa_batch_remaining
        def execute_host(queued, telegram):
            endpoint = self.fixture.telegram_endpoint if telegram else {
                "endpoint_id":self.fixture.account["endpoint_id"], "endpoint_key":self.fixture.account["endpoint_key"]}
            session = self.fixture.telegram_session if telegram else self.fixture.account["openclaw_session_key"]
            lease = self.service.acquire_host_coordinator_lease(user_subject=OWNER, task_id=queued["taskId"], registration=self.fixture.registration)
            return self.service.prepare_host_task(user_subject=OWNER, task_id=queued["taskId"], command_key=queued["commandKey"],
                session_key=session, endpoint_key=endpoint["endpoint_key"], registration=self.fixture.registration,
                runtime_context={"endpointId":endpoint["endpoint_id"], "coordinatorLeaseVersion":str(lease["version"])})
        for telegram in (False, True):
            for point in ("after_read", "before_complete"):
                with self.subTest(telegram=telegram, point=point):
                    body = {"requestKey":uuid4().hex, "expectedBatchVersion":self.version}
                    token = self.fixture.meta["telegram_recovery_query"]["url"].rsplit("/", 1)[1]
                    queued = self.fixture.views.remaining_query(token, {**body, "taskId":self.task_id}) if telegram else self.queue(body)
                    target, method = (oa_batch_remaining, "observe_query_result") if point == "after_read" else (self.service.tasks, "complete_task")
                    with patch.object(target, method, side_effect=ProcessStopped), self.assertRaises(ProcessStopped):
                        execute_host(queued, telegram)
                    task = self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)
                    self.assertEqual(task["status"], "running")
                    self.assertEqual(task["summary"]["command"]["state"], "pending")
                    self.assertIn(queued["taskId"], [item["taskId"] for item in actions.pending_preparations(self.service, OWNER)])
                    with self.service.tasks._connect() as connection:
                        self.assertEqual(connection.execute("SELECT count(*) FROM task_events WHERE task_id=? AND event_type='task.batch_remaining.observed'", (queued["taskId"],)).fetchone()[0], 0)
                        reads = connection.execute("SELECT count(*) FROM operations WHERE capability_name='oa.workflow.pending.list'").fetchone()[0]
                    if telegram:
                        status = self.fixture.views.query_status(token, queued["taskId"])
                        self.assertEqual(status["state"], "running")
                        self.assertFalse(status["entryAvailable"])
                    # Recreate every service/store/worker against the persisted
                    # isolated home. No ledger rows are edited for recovery.
                    self.fixture.close()
                    self.fixture = TelegramRecoveryAcceptanceFixture(Path(self.temp.name), cases=("batch-current",))
                    self.addCleanup(self.fixture.close)
                    self.service = self.fixture.service
                    repeated = self.fixture.views.remaining_query(token, {**body, "taskId":self.task_id}) if telegram else self.queue(body)
                    self.assertEqual(repeated["taskId"], queued["taskId"])
                    probe = self.service._adapters_by_system["oa"].probe_business_draft_session
                    with patch.object(self.service, "invoke", side_effect=AssertionError("completed IO must not replay")), \
                         patch.object(self.service._adapters_by_system["oa"], "probe_business_draft_session", wraps=probe) as probed:
                        result = execute_host(queued, telegram)
                    self.assertEqual(result["status"], "succeeded", result)
                    self.assertEqual(probed.call_count, 1)
                    self.assertEqual(self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)["status"], "succeeded")
                    with self.service.tasks._connect() as connection:
                        self.assertEqual(connection.execute("SELECT count(*) FROM operations WHERE capability_name='oa.workflow.pending.list'").fetchone()[0], reads)
                        self.assertEqual(connection.execute("SELECT count(*) FROM task_events WHERE task_id=? AND event_type='task.batch_remaining.observed'", (queued["taskId"],)).fetchone()[0], 1)
                    with patch.object(self.service, "invoke", side_effect=AssertionError("no duplicate read")), \
                         patch.object(self.service._adapters_by_system["oa"], "probe_business_draft_session", side_effect=AssertionError("attested completion is durable")):
                        self.assertTrue(execute_host(queued, telegram)["reused"])
