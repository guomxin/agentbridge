"""Owned query selections retain normal prepare/material/authorization paths."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from agentbridge.core import actionable_results as actions
from agentbridge.core.business_skills import SkillRejected, validate_skill_bundle
from tests.authorization_fixtures import grant_permissions
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract, OWNER
from tests.test_seeyon_pending_actions import ALL_PENDING_PROFILES


class ActionableResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = LatencyAcceptanceFixture(Path(self.temp.name), seed=False,
            latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
        self.addCleanup(self.fixture.close)
        self.service, self.account = self.fixture.service, self.fixture.account
        self.service.skills.save("user:" + OWNER, {"oa-pending": {"profiles": ["single", "batch"]}},
            expected_revision=0, actor="test-admin", reason="Explicit fixture Skill assignment")
        self.fixture.oa.add_affair("synthetic-attendance-choice", profile="attendance_confirmation")
        self.source = self.fixture.add_query_case(case_key=uuid4().hex)
        self.context = {"endpointId": self.account["endpoint_id"], "hostType": "openclaw",
            "hostInstanceId": "openclaw-gateway", "hostRunId": "synthetic-actionable-run"}

    def groups(self):
        return actions.list_actionable_results(self.service, OWNER, self.source["task_id"])

    def body(self, indices=(0,), request=None):
        group = self.groups()[0]
        return {"operationId": group["operationId"], "targetKeys": [group["targets"][i]["targetKey"] for i in indices],
            "requestKey": request or uuid4().hex}

    def queue(self, body=None):
        return actions.queue_preparation(self.service, self.account, self.source["task_id"], body or self.body())

    def execute(self, queued):
        return actions.execute_preparation(self.service, OWNER, queued["taskId"], queued["commandKey"], runtime_context=self.context)

    def count(self, table):
        with self.service.tasks._connect() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def mutate_source(self, transform):
        operation = self.service.operations.get(self.source["operation_id"])
        result = deepcopy(operation["result"])
        transform(result)
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE operations SET result_json=? WHERE operation_id=?", (actions._json(result), self.source["operation_id"]))

    def command(self, queued):
        return self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)["summary"]["command"]

    def test_query_choices_are_opaque_and_queue_performs_no_prepare_or_authorization(self):
        group = self.groups()[0]
        self.assertTrue(all(item["enabled"] for item in group["targets"]))
        self.assertEqual(group["coverage"], "complete")
        self.assertEqual(len(group["targets"][0]["targetKey"]), 64)
        self.assertNotIn("affair_id", actions._json(group))
        before = {table: self.count(table) for table in ("operations", "write_authorizations", "business_drafts")}
        with patch.object(self.service, "invoke", side_effect=AssertionError("queue cannot invoke")):
            queued = self.queue()
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(before, {table: self.count(table) for table in before})
        binding = self.service.skills.for_task(OWNER, queued["taskId"])
        self.assertEqual(binding["skill_id"], "oa-pending")
        self.assertEqual(binding["profile"], "single")
        self.assertEqual(self.command(queued)["loadedResources"], ["SKILL.md"])
        self.assertEqual(len(actions.pending_preparations(self.service, OWNER)), 1)
        self.assertEqual(actions.pending_preparations(self.service, "someone-else"), [])

    def test_non_oa_and_historical_queries_remain_readable_without_actions(self):
        task, _ = self.service.tasks.ensure_task(user_subject=OWNER, agent_host="openclaw", host_task_key="non-oa-result",
            origin_endpoint_id=self.account["endpoint_id"], active_conversation_ref=self.account["openclaw_session_key"], title="合成日志查询")
        grant_permissions(self.service.user_grants, OWNER, ["taihua.work_log.create"])
        self.assertEqual(actions.list_actionable_results(self.service, OWNER, task["task_id"]), [])
        self.assertEqual(self.groups(), [])

    def test_request_and_exact_selection_are_idempotent_across_surfaces(self):
        body = self.body()
        first = self.queue(body)
        for another in (body, {**body, "requestKey": uuid4().hex}):
            repeated = self.queue(another)
            self.assertEqual(repeated["taskId"], first["taskId"])
            self.assertEqual(repeated["commandKey"], first["commandKey"])
        self.assertEqual(len(actions.pending_preparations(self.service, OWNER)), 1)
        with self.assertRaisesRegex(actions.ActionableResultError, "另一组"):
            self.queue({**body, "targetKeys": self.body((1,))["targetKeys"]})

    def test_pending_capacity_blocks_only_new_requests_and_releases_after_prepare(self):
        for index in range(34):
            self.fixture.oa.add_affair("synthetic-capacity-" + str(index), profile="attendance_confirmation")
        self.source = self.fixture.add_query_case(case_key=uuid4().hex)
        group = self.groups()[0]
        def selected(index):
            return {"operationId": group["operationId"], "targetKeys": [group["targets"][index]["targetKey"]], "requestKey": uuid4().hex}
        first_body = selected(0)
        first = self.queue(first_body)
        for index in range(1, 32):
            self.queue(selected(index))
        before = {table: self.count(table) for table in ("agent_tasks", "skill_bindings", "skill_task_bindings", "task_events")}
        with self.assertRaises(actions.ActionableResultError) as caught:
            self.queue(selected(32))
        self.assertEqual(caught.exception.code, "ACTION_PREPARATION_BUSY")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(before, {table: self.count(table) for table in before})
        self.assertEqual(self.queue(first_body)["taskId"], first["taskId"])
        self.assertEqual(self.execute(first)["status"], "requires_user_action")
        self.assertEqual(self.queue(selected(32))["status"], "queued")
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_queue_wait_has_real_wall_instants_and_default_request_correlation(self):
        before = actions._now() - timedelta(seconds=3)
        with patch.object(actions, "_now", return_value=before):
            queued = self.queue()
        response = self.execute(queued)
        operation = self.service.operations.get(response["operationId"])
        self.assertEqual(operation["request_id"], queued["commandKey"])
        trace = self.service.runtime_governance.trace_for_task(queued["taskId"], user_subject=OWNER)
        spans = [item for item in self.service.runtime_governance.trace_detail(trace["trace_id"])["spans"]
                 if item["stage"] == "workspace.preparation.queue_wait"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["started_at"], before.isoformat())
        self.assertGreaterEqual(spans[0]["duration_ms"], 3000)
        self.assertEqual(spans[0]["metadata"], {"clockSource": "central.wall", "durationClock": "central.wall",
            "measurementKind": "span", "requestId": queued["commandKey"]})

    def test_queue_timing_failure_never_blocks_real_prepare(self):
        queued = self.queue()
        original = self.service.runtime_governance.ensure_trace
        def unavailable(**kwargs):
            if kwargs.get("lock_timeout") == .02:
                raise TimeoutError("Synthetic diagnostic write contention")
            return original(**kwargs)
        with patch.object(self.service.runtime_governance, "ensure_trace", side_effect=unavailable):
            response = self.execute(queued)
        self.assertEqual(response["status"], "requires_user_action")
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_aliased_request_key_cannot_change_selection(self):
        self.queue()
        alias = self.body()
        self.queue(alias)
        with self.assertRaises(actions.ActionableResultError) as caught:
            self.queue({**alias, "targetKeys": self.body((1,))["targetKeys"]})
        self.assertEqual(caught.exception.code, "ACTION_REQUEST_CONFLICT")

    def test_single_executes_real_prepare_and_keeps_card_authorization_pending(self):
        queued = self.queue()
        response = self.execute(queued)
        self.assertEqual(response["status"], "requires_user_action")
        self.assertEqual(response["interaction"]["type"], "business_input")
        self.assertEqual(len(self.fixture.oa.writes), 0)
        self.assertEqual(self.service.skills.for_task(OWNER, queued["taskId"])["profile"], "single")
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])
        with patch.object(self.service, "invoke", side_effect=AssertionError("existing op cannot invoke")):
            repeated = self.execute(queued)
        self.assertEqual(repeated["operationId"], response["operationId"])
        self.assertTrue(repeated["reused"])

    def test_query_row_recovers_authoritative_child_progress_without_repreparing(self):
        queued = self.queue()
        target = self.groups()[0]["targets"][0]
        self.assertEqual(target["preparation"], {"taskId": queued["taskId"], "state": "queued"})
        self.assertFalse(target["enabled"])
        self.execute(queued)
        target = self.groups()[0]["targets"][0]
        self.assertEqual(target["preparation"]["state"], "waiting_user")
        before = {table: self.count(table) for table in ("operations", "write_authorizations", "business_draft_decisions")}
        for state in ("succeeded", "failed", "expired", "canceled", "superseded", "outcome_unknown"):
            with self.subTest(state=state), self.service.tasks._connect() as connection:
                connection.execute("UPDATE agent_tasks SET status=? WHERE task_id=?", (state, queued["taskId"]))
            detail = self.fixture.app.task_detail(self.account, self.source["task_id"])
            projected = detail["actionableResults"][0]["targets"][0]
            self.assertEqual(projected["preparation"], {"taskId": queued["taskId"], "state": state})
            self.assertFalse(projected["enabled"])
        self.assertEqual(before, {table: self.count(table) for table in before})

    def test_preparation_progress_exposes_only_its_owned_source_for_refresh(self):
        queued = self.queue()
        detail = self.fixture.app.task_detail(self.account, queued["taskId"])
        self.assertEqual(detail["actionableSourceTaskIds"], [self.source["task_id"]])
        self.assertEqual(self.fixture.app.task_detail(self.account, self.source["task_id"])["actionableSourceTaskIds"], [])
        with self.service.tasks._connect() as connection, self.assertRaises(actions.ActionableResultError):
            actions.preparation_source_task_ids(connection, "other-user", queued["taskId"])

    def test_overlapping_new_batch_cannot_reprepare_a_used_query_target(self):
        body = self.body((0, 1))
        self.queue(self.body((0,)))
        before = self.count("agent_tasks")
        with self.assertRaises(actions.ActionableResultError) as error:
            self.queue(body)
        self.assertEqual(error.exception.code, "ACTION_TARGET_ALREADY_PREPARED")
        self.assertEqual(self.count("agent_tasks"), before)

    def test_expired_prepared_query_keeps_readonly_progress_without_extending_action_window(self):
        queued = self.queue()
        future = actions._now() + timedelta(minutes=11)
        with patch.object(actions, "_now", return_value=future):
            group = self.groups()[0]
            self.assertTrue(all(not item["enabled"] for item in group["targets"]))
            self.assertEqual(group["targets"][0]["preparation"]["taskId"], queued["taskId"])
            with self.assertRaises(actions.ActionableResultError) as error:
                self.queue(self.body((1,)))
        self.assertEqual(error.exception.code, "ACTION_SOURCE_EXPIRED")

    def test_batch_rows_follow_their_own_item_outcomes(self):
        queued = self.queue(self.body((0, 1)))
        self.execute(queued)
        with self.service.tasks._connect() as connection:
            batch = connection.execute("SELECT batch_id FROM task_batches WHERE parent_task_id=?", (queued["taskId"],)).fetchone()
            items = connection.execute("SELECT item_id FROM task_batch_items WHERE batch_id=? ORDER BY ordinal", (batch[0],)).fetchall()
            connection.execute("UPDATE task_batch_items SET state='succeeded' WHERE item_id=?", (items[0][0],))
            connection.execute("UPDATE task_batch_items SET state='outcome_unknown' WHERE item_id=?", (items[1][0],))
            connection.execute("UPDATE agent_tasks SET status='outcome_unknown' WHERE task_id=?", (queued["taskId"],))
        targets = self.groups()[0]["targets"]
        self.assertEqual([item["preparation"]["state"] for item in targets[:2]], ["succeeded", "outcome_unknown"])
        self.assertTrue(all(not item["enabled"] for item in targets[:2]))

    def test_stopped_batch_does_not_show_unprocessed_items_as_preparing(self):
        queued = self.queue(self.body((0, 1)))
        self.execute(queued)
        self.service.tasks.fail_current_batch_item(parent_task_id=queued["taskId"], user_subject=OWNER,
            operation_id=None, expected_ordinal=1, item_state="outcome_unknown", error_code="SYNTHETIC_UNKNOWN")
        targets = self.groups()[0]["targets"]
        self.assertEqual([item["preparation"]["state"] for item in targets[:2]], ["outcome_unknown", "skipped"])
        self.assertEqual(self.fixture.oa.writes, [])

    def test_running_prepare_is_not_presented_as_an_authorized_submission(self):
        queued = self.queue()
        self.execute(queued)
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE agent_tasks SET status='running' WHERE task_id=?", (queued["taskId"],))
        self.assertEqual(self.groups()[0]["targets"][0]["preparation"]["state"], "preparing")
        with self.service.tasks._connect() as connection:
            command = self.command(queued)
            connection.execute("UPDATE operations SET capability_name=?,status='running' WHERE operation_id=(SELECT current_operation_id FROM agent_tasks WHERE task_id=?)",
                (command["selectedItems"][0]["display_summary"]["commit_capability"], queued["taskId"]))
        self.assertEqual(self.groups()[0]["targets"][0]["preparation"]["state"], "running")

    def test_mixed_selection_uses_one_real_durable_batch_and_original_order(self):
        queued = self.queue(self.body((1, 0)))
        command = self.command(queued)
        response = self.execute(queued)
        self.assertEqual(command["capabilityName"], "oa.workflow.pending.batch.prepare")
        self.assertEqual(response["status"], "requires_user_action")
        batch = self.service.tasks.get_batch_for_task(parent_task_id=queued["taskId"], user_subject=OWNER, include_resource_refs=True)
        self.assertEqual([item["resource_ref"] for item in batch["items"]], command["affairIds"])
        self.assertEqual(self.service.skills.for_task(OWNER, queued["taskId"])["profile"], "batch")
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_all_14_types_use_existing_exact_selector_and_prepare(self):
        for profile in ALL_PENDING_PROFILES:
            self.fixture.oa.add_affair("synthetic-all-" + profile, profile=profile)
        self.source = self.fixture.add_query_case(case_key=uuid4().hex)
        group = self.groups()[0]
        self.assertEqual({item["workflowType"] for item in group["targets"]}, set(ALL_PENDING_PROFILES))
        for profile in ALL_PENDING_PROFILES:
            with self.subTest(profile=profile):
                target = next(item for item in group["targets"] if item["workflowType"] == profile)
                queued = self.queue({"operationId": group["operationId"], "targetKeys": [target["targetKey"]], "requestKey": uuid4().hex})
                response = self.execute(queued)
                self.assertEqual(response["status"], "requires_user_action")
                self.assertEqual(response["interaction"]["type"], "business_input")
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_unknown_row_is_retained_disabled_and_cannot_be_silently_selected(self):
        self.mutate_source(lambda result: result["items"].append({"affair_id": "unsupported", "title": "【HR】未知专用表单"}))
        group = self.groups()[0]
        self.assertTrue(group["targets"][0]["enabled"])
        self.assertFalse(group["targets"][-1]["enabled"])
        with self.assertRaises(actions.ActionableResultError) as caught:
            self.queue({"operationId": group["operationId"], "targetKeys": [item["targetKey"] for item in group["targets"]], "requestKey": uuid4().hex})
        self.assertEqual(caught.exception.code, "BATCH_UNSUPPORTED_ITEMS")
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])

    def test_reward_notification_selection_uses_its_own_prepare_and_permission(self):
        self.mutate_source(lambda result: result["items"].append({
            "affair_id": "synthetic-reward", "title": "【知识产权】知识产权奖励审批单(合成申请人)",
        }))
        group = self.groups()[0]
        target = group["targets"][-1]
        self.assertEqual(target["workflowType"], "intellectual_property_reward")
        self.assertEqual(target["workflowLabel"], "知识产权奖励")
        self.assertTrue(target["enabled"])
        body = {"operationId": group["operationId"], "targetKeys": [target["targetKey"]], "requestKey": uuid4().hex}
        queued = self.queue(body)
        self.assertEqual(self.command(queued)["capabilityName"],
                         "oa.intellectual_property_reward.acknowledgement.prepare")
        self.assertEqual(self.command(queued)["affairIds"], ["synthetic-reward"])
        grant_permissions(self.service.user_grants, OWNER,
                          ["oa.workflow.read", "oa.intellectual_property_declaration.approve", "oa.standard_collaboration.approve"])
        disabled = self.groups()[0]["targets"][-1]
        self.assertFalse(disabled["enabled"])
        self.assertIn("权限", disabled["disabledReason"])
        with self.assertRaises(PermissionError):
            self.queue({**body, "requestKey": uuid4().hex})
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_business_settlement_selection_requires_its_independent_approval_permission(self):
        self.mutate_source(lambda result: result["items"].append({
            "affair_id": "synthetic-settlement", "title": "【报销】业务结算单-合成申请人",
        }))
        group = self.groups()[0]
        target = group["targets"][-1]
        self.assertEqual(target["workflowType"], "business_settlement")
        self.assertEqual(target["workflowLabel"], "业务结算单")
        self.assertTrue(target["enabled"])
        body = {"operationId": group["operationId"], "targetKeys": [target["targetKey"]], "requestKey": uuid4().hex}
        queued = self.queue(body)
        self.assertEqual(self.command(queued)["capabilityName"], "oa.business_settlement.approval.prepare")
        self.assertEqual(self.command(queued)["affairIds"], ["synthetic-settlement"])
        grant_permissions(self.service.user_grants, OWNER,
                          ["oa.workflow.read", "oa.travel_expense.approve", "oa.standard_collaboration.approve"])
        disabled = self.groups()[0]["targets"][-1]
        self.assertFalse(disabled["enabled"])
        self.assertIn("权限", disabled["disabledReason"])
        with self.assertRaises(PermissionError):
            self.queue({**body, "requestKey": uuid4().hex})
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_unknown_intellectual_property_form_cannot_fall_back_to_generic_collaboration(self):
        self.mutate_source(lambda result: result["items"].append({
            "affair_id": "synthetic-unknown-ip", "title": "【知识产权】未注册奖励变更单(合成申请人)",
        }))
        group = self.groups()[0]
        target = group["targets"][-1]
        self.assertFalse(target["enabled"])
        self.assertIsNone(target["workflowType"])
        with self.assertRaises(actions.ActionableResultError) as caught:
            self.queue({"operationId": group["operationId"], "targetKeys": [target["targetKey"]], "requestKey": uuid4().hex})
        self.assertEqual(caught.exception.code, "BATCH_UNSUPPORTED_ITEMS")
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])

    def test_incomplete_duplicate_and_missing_source_fail_closed(self):
        original = deepcopy(self.service.operations.get(self.source["operation_id"])["result"])
        for change in (lambda r: r.update(coverage={"status": "partial"}),
                       lambda r: r["items"].append(deepcopy(r["items"][0])),
                       lambda r: r["items"][0].pop("affair_id")):
            with self.subTest(change=change):
                with self.service.tasks._connect() as connection:
                    connection.execute("UPDATE operations SET result_json=? WHERE operation_id=?", (actions._json(original), self.source["operation_id"]))
                self.mutate_source(change)
                self.assertEqual(self.groups(), [])
                with patch.object(actions, "_now", return_value=actions._now() + timedelta(minutes=11)):
                    self.assertEqual(self.groups(), [])

    def test_foreign_task_operation_and_target_are_rejected(self):
        body = self.body()
        with self.assertRaises((actions.ActionableResultError, PermissionError)):
            actions.list_actionable_results(self.service, "someone-else", self.source["task_id"])
        for invalid in ({**body, "operationId": str(uuid4())}, {**body, "targetKeys": ["a" * 64]}):
            with self.subTest(invalid=invalid), self.assertRaises(actions.ActionableResultError):
                self.queue(invalid)

    def test_browser_cannot_supply_business_arguments_or_duplicate_targets(self):
        body = self.body()
        for invalid in ({**body, "arguments": {"affair_id": "forged"}}, {**body, "capabilityName": "oa.leave.approve"},
                        {**body, "targetKeys": body["targetKeys"] * 2}):
            with self.subTest(invalid=invalid), self.assertRaises(actions.ActionableResultError):
                self.queue(invalid)

    def test_queue_rolls_back_task_skill_binding_event_and_hint_together(self):
        before = {table: self.count(table) for table in ("agent_tasks", "skill_bindings", "skill_task_bindings", "task_events")}
        original = self.service.tasks._append_event
        def fail(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("event_type") == "task.prepare.requested":
                raise RuntimeError("synthetic queue failure")
            return result
        with patch.object(self.service.tasks, "_append_event", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "synthetic queue failure"):
                self.queue()
        self.assertEqual(before, {table: self.count(table) for table in before})
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])

    def test_expired_source_blocks_first_prepare_without_invocation(self):
        body = self.body()
        queued = self.queue(body)
        future = actions._now() + timedelta(seconds=601)
        with patch.object(actions, "_now", return_value=future), patch.object(self.service, "invoke", side_effect=AssertionError("expired")):
            self.assertTrue(all(not item["enabled"] for item in self.groups()[0]["targets"]))
            response = self.execute(queued)
        self.assertEqual(response["error"]["code"], "ACTION_SOURCE_EXPIRED")
        self.assertEqual(self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)["status"], "failed")
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])

    def test_permission_removal_before_execute_blocks_prepare(self):
        queued = self.queue()
        grant_permissions(self.service.user_grants, OWNER, ["oa.workflow.read"])
        with patch.object(self.service, "invoke", side_effect=AssertionError("revoked permission")):
            response = self.execute(queued)
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"]["code"], "SKILL_DEPENDENCY_UNAVAILABLE")
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_revoked_binding_is_never_restored_by_reassignment(self):
        queued = self.queue()
        self.service.skills.save("user:" + OWNER, {}, expected_revision=1, actor="admin", reason="revoke")
        self.service.skills.save("user:" + OWNER, {"oa-pending": {"profiles": ["single", "batch"]}},
            expected_revision=2, actor="admin", reason="new assignment")
        response = self.execute(queued)
        self.assertEqual(response["error"]["code"], "SKILL_BINDING_REVOKED")

    def test_reviewed_route_pins_execution_metadata_and_resources_before_queueing(self):
        body = self.body()
        original = self.service.skills.current('oa-pending')
        self.assertEqual(original['content_hash'], actions.REVIEWED_SKILL_HASH)
        self.assertFalse(original['manifest']['profiles']['single']['execution']['allow_batch'])
        self.assertTrue(original['manifest']['profiles']['batch']['execution']['allow_batch'])
        before = {table: self.count(table) for table in ('operations', 'agent_tasks', 'skill_bindings', 'write_authorizations')}
        for field in ('execution', 'description', 'instructions'):
            changed = deepcopy(original)
            if field == 'execution':
                changed['manifest']['profiles']['single']['execution']['allow_batch'] = True
            elif field == 'description':
                changed['manifest']['profiles']['single']['description'] += '未复核的修改'
            else:
                changed['resources']['SKILL.md'] += '\n未复核的执行说明。'
            changed = validate_skill_bundle(changed['manifest'], changed['resources'])
            self.assertNotEqual(changed['content_hash'], actions.REVIEWED_SKILL_HASH)
            with self.subTest(field=field), patch.object(self.service.skills, 'current', return_value=changed):
                self.assertTrue(all(not item['enabled'] for item in self.groups()[0]['targets']))
                with self.assertRaises(actions.ActionableResultError) as caught:
                    self.queue(body)
                self.assertEqual(caught.exception.code, 'ACTION_SKILL_CHANGED')
            self.assertEqual(before, {table: self.count(table) for table in before})

    def test_new_skill_instructions_stop_first_prepare(self):
        queued = self.queue()
        original = self.service.skills.current
        def newer(sid, connection=None):
            item = deepcopy(original(sid, connection))
            item["content_hash"] = "unreviewed-new-instructions"
            return item
        with patch.object(self.service.skills, "current", side_effect=newer), patch.object(self.service, "invoke", side_effect=AssertionError("new instructions")):
            response = self.execute(queued)
        self.assertEqual(response["error"]["code"], "ACTION_SKILL_CHANGED")

    def test_source_result_cannot_change_after_queue(self):
        queued = self.queue()
        self.mutate_source(lambda result: result["items"][0].update(title="另一项事项"))
        with patch.object(self.service, "invoke", side_effect=AssertionError("frozen source changed")):
            response = self.execute(queued)
        self.assertEqual(response["error"]["code"], "ACTION_SOURCE_CHANGED")

    def test_final_prepare_rechecks_real_materials_before_showing_a_card(self):
        # The source list is only a routing hint. The existing adapter still
        # checks the current exact affair/node/materials in normal preparation.
        queued = self.queue()
        affair_id = self.command(queued)["affairIds"][0]
        self.fixture.oa.data["affairs"][affair_id]["source"]["title"] = "【HR】未知专用表单"
        response = self.execute(queued)
        self.assertEqual(response["status"], "failed")
        self.assertIsNone(response.get("interaction"))
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_existing_card_still_requires_current_permission_and_original_binding(self):
        queued = self.queue()
        response = self.execute(queued)
        self.assertEqual(response["status"], "requires_user_action")
        grant_permissions(self.service.user_grants, OWNER, ["oa.workflow.read"])
        with patch.object(self.service, "invoke", side_effect=AssertionError("existing op cannot invoke")):
            recovered = self.execute(queued)
        self.assertEqual(recovered["status"], "failed")
        self.assertIsNone(recovered.get("interaction"))

    def test_lost_prepare_reply_recovers_original_card_after_ttl_and_skill_update(self):
        queued = self.queue()
        with patch.object(actions, "_record_result", side_effect=RuntimeError("reply lost")):
            with self.assertRaisesRegex(RuntimeError, "reply lost"):
                self.execute(queued)
        operations_before = self.count("operations")
        future = actions._now() + timedelta(seconds=601)
        original = self.service.skills.current
        def newer(sid, connection=None):
            item = deepcopy(original(sid, connection))
            item["content_hash"] = "unreviewed-new-instructions"
            return item
        with patch.object(actions, "_now", return_value=future), patch.object(self.service.skills, "current", side_effect=newer), \
            patch.object(self.service, "invoke", side_effect=AssertionError("existing op must not invoke")):
            response = self.execute(queued)
        self.assertEqual(response["status"], "requires_user_action")
        self.assertTrue(response["reused"])
        self.assertEqual(self.count("operations"), operations_before)

    def test_failed_and_unknown_existing_operations_are_returned_without_retry(self):
        for status in ("failed", "unknown"):
            with self.subTest(status=status):
                queued = self.queue(self.body((0,) if status == "failed" else (1,)))
                command = self.command(queued)
                operation, _ = self.service.operations.create(user_subject=OWNER, capability_name=command["capabilityName"],
                    capability_version=command["capabilityVersion"], input_summary=command["arguments"],
                    idempotency_key=f"workspace-prepare:{queued['taskId']}:{queued['commandKey']}")
                method = self.service.operations.mark_failed if status == "failed" else self.service.operations.mark_unknown
                method(operation["operation_id"], code="SYNTHETIC_LOST_RESPONSE", message="Synthetic existing result")
                with patch.object(self.service, "invoke", side_effect=AssertionError("cannot retry")):
                    response = self.execute(queued)
                self.assertEqual(response["operationId"], operation["operation_id"])
                self.assertEqual(response["status"], status)

    def test_wrong_origin_context_never_mutates_owned_task(self):
        queued = self.queue()
        with self.assertRaises(actions.ActionableResultError):
            actions.execute_preparation(self.service, OWNER, queued["taskId"], queued["commandKey"], runtime_context={"endpointId": "other"})
        self.assertEqual(self.command(queued)["state"], "pending")

    def test_forged_workspace_account_cannot_queue(self):
        body = self.body()
        for key in ("endpoint_id", "endpoint_key", "openclaw_session_key"):
            with self.subTest(key=key), self.assertRaises(actions.ActionableResultError):
                actions.queue_preparation(self.service, {**self.account, key: "forged"}, self.source["task_id"], body)
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])

    def test_pending_recovery_is_bounded_and_skips_disabled_workspace_accounts(self):
        self.queue(self.body((0,)))
        self.queue(self.body((1,)))
        self.assertEqual(len(actions.pending_preparations(self.service, OWNER, limit=1)), 1)
        self.assertEqual(len(actions.pending_preparations(self.service, OWNER)), 2)
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE workspace_accounts SET state='disabled' WHERE account_id=?", (self.account["account_id"],))
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])

    def test_tampered_intent_or_missing_event_cannot_execute(self):
        queued = self.queue()
        with self.service.tasks._connect() as connection:
            connection.execute("DELETE FROM task_events WHERE task_id=? AND event_type='task.prepare.requested'", (queued["taskId"],))
        with self.assertRaises(actions.ActionableResultError):
            self.execute(queued)
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])

    def test_shared_skill_connection_requires_transaction_and_rolls_back(self):
        before = self.count("skill_bindings")
        with self.service.tasks._connect() as connection:
            with self.assertRaises(ValueError):
                self.service.skills.bind(OWNER, "oa-pending", "single", connection=connection)
            connection.execute("BEGIN IMMEDIATE")
            binding = self.service.skills.bind(OWNER, "oa-pending", "single", connection=connection)
            self.service.skills.attach(OWNER, "synthetic-rolled-back-task", binding, connection=connection)
            connection.rollback()
        self.assertEqual(self.count("skill_bindings"), before)
        self.assertIsNone(self.service.skills.for_task(OWNER, "synthetic-rolled-back-task"))

    def fresh_query(self):
        self.source = self.fixture.add_query_case(case_key=uuid4().hex)
        return self.groups()[0]

    def conflicts(self, queued, *, session=None, exclude=None):
        command = self.command(queued)
        selections = [{"affairId": entry["resource_ref"],
                       "prepareCapability": entry["display_summary"]["prepare_capability"],
                       "commitCapability": entry["display_summary"]["commit_capability"]}
                      for entry in command["selectedItems"]]
        return actions.oa_preparation_conflicts(self.service, user_subject=OWNER,
            session=session or self.service.sessions.find(user_subject=OWNER, system_id="oa"),
            selections=selections, exclude_task_id=exclude)

    def test_fresh_query_reuses_exact_pending_affair_and_preserves_original_skill(self):
        first = self.queue()
        first_binding = self.service.skills.for_task(OWNER, first["taskId"])["binding_id"]
        group = self.fresh_query()
        self.assertEqual(group["targets"][0]["preparation"], {"taskId": first["taskId"], "state": "queued"})
        self.assertFalse(group["targets"][0]["enabled"])
        before = {name: self.count(name) for name in ("agent_tasks", "skill_bindings", "write_authorizations", "business_draft_decisions")}
        repeated = self.queue()
        self.assertEqual(repeated["taskId"], first["taskId"])
        self.assertEqual(repeated["status"], "existing")
        self.assertEqual(self.service.skills.for_task(OWNER, first["taskId"])["binding_id"], first_binding)
        self.assertEqual(before, {name: self.count(name) for name in before})
        self.assertEqual(self.fixture.oa.writes, [])

    def test_unknown_affair_stays_blocked_after_source_expiry_and_same_principal_relogin(self):
        first = self.queue()
        old_source = dict(self.source)
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE agent_tasks SET status='outcome_unknown' WHERE task_id=?", (first["taskId"],))
            connection.execute("UPDATE operations SET finished_at=? WHERE operation_id=?",
                ((actions._now() - timedelta(hours=2)).isoformat(), old_source["operation_id"]))
            connection.execute("UPDATE sessions SET authority_generation=authority_generation+2 WHERE user_subject=? AND system_id='oa'", (OWNER,))
        group = self.fresh_query()
        self.assertEqual(group["targets"][0]["preparation"], {"taskId": first["taskId"], "state": "outcome_unknown"})
        self.assertFalse(group["targets"][0]["enabled"])
        self.assertEqual(self.queue()["taskId"], first["taskId"])
        session = self.service.sessions.find(user_subject=OWNER, system_id="oa")
        conflicts = self.conflicts(first, session={**session, "session_id": "new-session-same-principal"}, exclude=first["taskId"])
        self.assertEqual([(item["taskId"], item["state"]) for item in conflicts], [(first["taskId"], "outcome_unknown")])
        self.assertTrue(conflicts[0]["identityVerified"])
        self.assertEqual(self.count("business_draft_decisions"), 0)
        self.assertEqual(self.count("write_authorizations"), 0)
        self.assertEqual(self.fixture.oa.writes, [])

    def test_cross_query_batch_rejects_overlap_but_allows_remaining_items(self):
        first = self.queue()
        group = self.fresh_query()
        self.assertFalse(group["targets"][0]["enabled"])
        self.assertTrue(group["targets"][1]["enabled"])
        before = self.count("agent_tasks")
        with self.assertRaises(actions.ActionableResultError) as caught:
            self.queue(self.body((0, 1)))
        self.assertEqual(caught.exception.code, "ACTION_TARGET_ALREADY_PREPARED")
        self.assertEqual(self.count("agent_tasks"), before)
        fresh = self.queue(self.body((1,)))
        self.assertNotEqual(fresh["taskId"], first["taskId"])
        self.assertEqual(self.count("write_authorizations"), 0)

    def test_cross_query_batch_preserves_unknown_item_and_releases_unsubmitted_skipped_item(self):
        first = self.queue(self.body((0, 1)))
        self.execute(first)
        self.service.tasks.fail_current_batch_item(parent_task_id=first["taskId"], user_subject=OWNER,
            operation_id=None, expected_ordinal=1, item_state="outcome_unknown", error_code="SYNTHETIC_UNKNOWN")
        group = self.fresh_query()
        self.assertEqual(group["targets"][0]["preparation"]["state"], "outcome_unknown")
        self.assertFalse(group["targets"][0]["enabled"])
        self.assertIsNone(group["targets"][1]["preparation"])
        self.assertTrue(group["targets"][1]["enabled"])
        fresh = self.queue(self.body((1,)))
        self.assertNotEqual(fresh["taskId"], first["taskId"])
        self.assertEqual(self.fixture.oa.writes, [])

    def test_failed_or_expired_without_submit_evidence_can_be_prepared_from_new_query(self):
        first = self.queue()
        for state in ("failed", "expired", "canceled"):
            with self.subTest(state=state):
                with self.service.tasks._connect() as connection:
                    connection.execute("UPDATE agent_tasks SET status=? WHERE task_id=?", (state, first["taskId"]))
                group = self.fresh_query()
                self.assertTrue(group["targets"][0]["enabled"])
                self.assertIsNone(group["targets"][0]["preparation"])
                first = self.queue()
        self.assertEqual(self.count("write_authorizations"), 0)
        self.assertEqual(self.fixture.oa.writes, [])

    def test_same_title_or_affair_under_another_principal_does_not_merge(self):
        first = self.queue()
        session = self.service.sessions.find(user_subject=OWNER, system_id="oa")
        self.service.sessions.rebind_expected_principal(session["session_id"], expected_principal_ref="other-oa-principal", reason="fixture account rotation")
        self.fixture.oa.data["principal"] = "other-oa-principal"
        self.service.sessions.activate(session["session_id"], observed_principal_ref="other-oa-principal")
        group = self.fresh_query()
        self.assertTrue(group["targets"][0]["enabled"])
        second = self.queue()
        self.assertNotEqual(second["taskId"], first["taskId"])
        self.assertEqual(self.command(second)["oaIdentity"]["principalRef"], "other-oa-principal")
        old_session = {**self.service.sessions.find(user_subject=OWNER, system_id="oa"),
                       "expected_principal_ref": OWNER, "downstream_principal_ref": OWNER}
        self.assertEqual([item["taskId"] for item in self.conflicts(first, session=old_session)], [first["taskId"]])

    def test_collision_never_crosses_subject_affair_or_capability(self):
        first = self.queue()
        selected = self.conflicts(first)[0]
        session = self.service.sessions.find(user_subject=OWNER, system_id="oa")
        for mutation in ({"affairId": "different-affair"}, {"prepareCapability": "oa.efficiency_data.approval.prepare"},
                         {"commitCapability": "oa.efficiency_data.approve"}):
            with self.subTest(mutation=mutation):
                selection = {key: selected[key] for key in ("affairId", "prepareCapability", "commitCapability")} | mutation
                self.assertEqual(actions.oa_preparation_conflicts(self.service, user_subject=OWNER, session=session, selections=[selection]), [])
        self.assertEqual(actions.oa_preparation_conflicts(self.service, user_subject="different-user",
            session={**session, "user_subject": "different-user"}, selections=[selected]), [])

    def test_own_undecided_draft_can_recheck_but_approved_or_unknown_cannot_be_excluded(self):
        from agentbridge.core.business_drafts import BusinessDraftService
        first = self.queue()
        response = self.execute(first)
        self.assertEqual(self.conflicts(first, exclude=first["taskId"]), [])
        source_id = response["nextAction"]["inputSubmissionId"]
        drafts = BusinessDraftService(self.service)
        opened = drafts.open(source_id)
        args = {"source_submission_id": source_id, "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        saved = drafts.save(**args, expected_revision=opened["state"]["revision"], values={"opinion": "合成核验意见"})
        ready = drafts.preview(**args, expected_revision=saved["revision"])
        drafts.approve(**args, expected_revision=ready["revision"], preview_id=ready["preview"]["previewId"], request_key=uuid4().hex)
        self.assertEqual(self.conflicts(first, exclude=first["taskId"])[0]["state"], "running")
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE agent_tasks SET status='failed' WHERE task_id=?", (first["taskId"],))
        self.assertEqual(self.conflicts(first, exclude=first["taskId"])[0]["state"], "outcome_unknown")
        self.assertEqual(self.fixture.oa.writes, [])

    def test_direct_prepare_resource_is_found_without_actionable_command(self):
        direct = self.fixture.add_pending_case("efficiency_data", case_key="direct-existing")
        self.fresh_query()
        target = next(item for item in self.groups()[0]["targets"] if item["workflowType"] == "efficiency_data")
        self.assertEqual(target["preparation"], {"taskId": direct["task_id"], "state": "waiting_user"})
        self.assertFalse(target["enabled"])
        with self.assertRaises(actions.ActionableResultError) as caught:
            self.queue({"operationId": self.groups()[0]["operationId"], "targetKeys": [target["targetKey"]], "requestKey": uuid4().hex})
        self.assertEqual(caught.exception.code, "ACTION_TARGET_ALREADY_PREPARED")
        self.assertEqual(self.fixture.oa.writes, [])

    def test_cross_query_reuse_requires_current_grants_and_reviewed_skill(self):
        self.queue()
        self.fresh_query()
        body = self.body()
        original = self.service.skills.current
        def newer(sid, connection=None):
            item = deepcopy(original(sid, connection))
            item["content_hash"] = "unreviewed-instructions"
            return item
        with patch.object(self.service.skills, "current", side_effect=newer), self.assertRaises(actions.ActionableResultError) as caught:
            self.queue(body)
        self.assertEqual(caught.exception.code, "ACTION_SKILL_CHANGED")
        grant_permissions(self.service.user_grants, OWNER, ["oa.workflow.read"])
        with self.assertRaises(PermissionError):
            self.queue(body)

    def test_concurrent_fresh_sources_atomically_converge_without_extra_binding(self):
        first_source, first_body = dict(self.source), self.body()
        self.fresh_query()
        second_source, second_body = dict(self.source), self.body()
        before = self.count("skill_bindings")
        def queue(source, body):
            return actions.queue_preparation(self.service, self.account, source["task_id"], body)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(queue, source, body) for source, body in ((first_source, first_body), (second_source, second_body))]
            results = [future.result() for future in futures]
        self.assertEqual(len({item["taskId"] for item in results}), 1)
        self.assertEqual({item["status"] for item in results}, {"queued", "existing"})
        self.assertEqual(self.count("skill_bindings"), before + 1)
        self.assertEqual(self.count("business_draft_decisions"), 0)
        self.assertEqual(self.count("write_authorizations"), 0)
        self.assertEqual(self.fixture.oa.writes, [])

    def legacy_command(self, queued):
        """Keep the old frozen contract/event consistent while omitting the new hint."""
        with self.service.tasks._connect() as connection:
            row = connection.execute("SELECT summary_json FROM agent_tasks WHERE task_id=?", (queued["taskId"],)).fetchone()
            summary = actions.json.loads(row[0])
            command = summary["command"]
            command.pop("oaIdentity", None)
            command["intentHash"] = actions._hash(actions._intent(command))
            connection.execute("UPDATE agent_tasks SET summary_json=? WHERE task_id=?", (actions._json(summary), queued["taskId"]))
            event = connection.execute("SELECT event_id,payload_json FROM task_events WHERE task_id=? AND event_type='task.prepare.requested'",
                                       (queued["taskId"],)).fetchone()
            payload = actions.json.loads(event["payload_json"])
            payload["intentHash"] = command["intentHash"]
            connection.execute("UPDATE task_events SET payload_json=? WHERE event_id=?", (actions._json(payload), event["event_id"]))

    def test_legacy_command_without_rebind_can_match_existing_verified_principal(self):
        first = self.queue()
        self.legacy_command(first)
        self.execute(first)
        self.fresh_query()
        target = self.groups()[0]["targets"][0]
        self.assertEqual(target["preparation"], {"taskId": first["taskId"], "state": "waiting_user"})
        self.assertEqual(self.queue()["taskId"], first["taskId"])

    def test_legacy_unknown_without_principal_proof_blocks_without_auto_reuse(self):
        first = self.queue()
        self.legacy_command(first)
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE agent_tasks SET status='outcome_unknown' WHERE task_id=?", (first["taskId"],))
        session = self.service.sessions.find(user_subject=OWNER, system_id="oa")
        self.service.sessions.rebind_expected_principal(session["session_id"], expected_principal_ref="rotated-oa-principal", reason="fixture missing historical identity")
        self.fixture.oa.data["principal"] = "rotated-oa-principal"
        self.service.sessions.activate(session["session_id"], observed_principal_ref="rotated-oa-principal")
        self.fresh_query()
        target = self.groups()[0]["targets"][0]
        self.assertEqual(target["preparation"], {"taskId": first["taskId"], "state": "identity_unverified"})
        self.assertFalse(target["enabled"])
        with self.assertRaises(actions.ActionableResultError) as caught:
            self.queue()
        self.assertEqual(caught.exception.code, "ACTION_TARGET_ALREADY_PREPARED")
        self.assertFalse(self.conflicts(first)[0]["identityVerified"])

    def test_new_query_refresh_follows_same_affair_without_queueing_a_child(self):
        first = self.queue()
        original_source = self.source["task_id"]
        self.fresh_query()
        before = self.count("agent_tasks")
        detail = self.fixture.app.task_detail(self.account, first["taskId"])
        self.assertEqual(set(detail["actionableSourceTaskIds"]), {original_source, self.source["task_id"]})
        self.assertEqual(self.count("agent_tasks"), before)
        self.assertEqual(self.count("write_authorizations"), 0)

    def test_new_single_task_persists_exact_business_title_without_using_it_as_identity(self):
        target = self.groups()[0]["targets"][0]
        first = self.queue()
        task = self.service.tasks.get_task(first["taskId"], user_subject=OWNER)
        self.assertEqual(task["title"], "核对并办理：" + target["title"])
        self.assertEqual(self.command(first)["affairIds"], ["latency-query-target"])

    def test_expired_undecided_resource_can_reprepare_but_unknown_cannot(self):
        first = self.queue()
        self.execute(first)
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE field_submissions SET state='expired' WHERE create_operation_id IN "
                "(SELECT operation_id FROM task_operations WHERE task_id=?)", (first["taskId"],))
        self.fresh_query()
        self.assertTrue(self.groups()[0]["targets"][0]["enabled"])
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE agent_tasks SET status='outcome_unknown' WHERE task_id=?", (first["taskId"],))
        self.assertFalse(self.groups()[0]["targets"][0]["enabled"])
        self.assertEqual(self.groups()[0]["targets"][0]["preparation"]["state"], "outcome_unknown")

    def test_fresh_query_proven_pre_submit_failures_remain_failed_and_bound_to_old_decision(self):
        from tests.support.pending_readiness_acceptance import PendingReadinessAcceptanceFixture
        from tests.test_workspace_business_outcomes import _approve
        for mode in ("never", "identity_drift", "material_drift"):
            with self.subTest(mode=mode), TemporaryDirectory() as temporary:
                fixture = PendingReadinessAcceptanceFixture(Path(temporary), modes=())
                try:
                    service, account = fixture.service, fixture.account
                    source = fixture.add_pending_case("efficiency_data", case_key=mode)
                    receipt = _approve(fixture, source)["receipt"]
                    fixture.oa.data["affairs"][source["arguments"]["affair_id"]]["readiness_case"] = mode
                    fixture.oa.persist()
                    response = fixture.resume(receipt["interactionId"])
                    self.assertEqual(response["status"], "failed", response)
                    self.assertEqual(response["error"]["code"], "OA_PENDING_ACTION_NOT_READY" if mode == "never" else "CAPABILITY_EXECUTION_FAILED")
                    task, _ = service.tasks.ensure_task(user_subject=OWNER, agent_host="openclaw", host_task_key=uuid4().hex,
                        origin_endpoint_id=account["endpoint_id"], active_conversation_ref=account["openclaw_session_key"], title="合成失败后只读查询")
                    query = service.invoke(user_subject=OWNER, capability_name="oa.workflow.pending.list", arguments={},
                        task_id=task["task_id"], idempotency_key=uuid4().hex)
                    self.assertEqual(query["status"], "succeeded")
                    service.finish_host_task(user_subject=OWNER, task_id=task["task_id"], outcome="succeeded", reason="synthetic_read_completed")
                    group = actions.list_actionable_results(service, OWNER, task["task_id"])[0]
                    target = next(item for item in group["targets"] if item["preparation"])
                    self.assertEqual(target["preparation"], {"taskId": source["task_id"], "state": "failed"})
                    self.assertFalse(target["enabled"])
                    selection = [{"affairId": source["arguments"]["affair_id"],
                        "prepareCapability": "oa.efficiency_data.approval.prepare", "commitCapability": "oa.efficiency_data.approve"}]
                    conflicts = actions.oa_preparation_conflicts(service, user_subject=OWNER,
                        session=service.sessions.find(user_subject=OWNER, system_id="oa"), selections=selection, exclude_task_id=source["task_id"])
                    self.assertEqual(conflicts[0]["state"], "failed")
                    with service.tasks._connect() as connection:
                        connection.execute("UPDATE write_authorizations SET state='consumed',consumed_at=?,commit_operation_id=? WHERE authorization_id=?",
                            (actions._now().isoformat(), response["operationId"], receipt["authorizationId"]))
                    conflicts = actions.oa_preparation_conflicts(service, user_subject=OWNER,
                        session=service.sessions.find(user_subject=OWNER, system_id="oa"), selections=selection, exclude_task_id=source["task_id"])
                    self.assertEqual(conflicts[0]["state"], "outcome_unknown")
                    self.assertEqual(fixture.oa.writes, [])
                finally:
                    fixture.close()


if __name__ == "__main__":
    unittest.main()
