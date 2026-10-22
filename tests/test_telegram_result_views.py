"""Telegram selection owns the same material/authorization/lease boundaries."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from agentbridge.core import actionable_results as actions
from agentbridge.core.business_draft_profiles import PROFILES
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.telegram_result_views import ResultViewService, TICKET_TTL_SECONDS
from agentbridge.core.tasks import TaskIntegrityError
from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS
from agentbridge.core.host_contract import HostLeaseConflict
from tests.authorization_fixtures import grant_permissions
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract, OWNER
from tests.test_seeyon_pending_actions import ALL_PENDING_PROFILES
from tests.support.telegram_result_acceptance import add_quarterly_affair, add_reward_affair

CURRENT_PROFILES = (*ALL_PENDING_PROFILES, "quarterly_performance", "intellectual_property_reward")


class TelegramResultViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = LatencyAcceptanceFixture(Path(self.temp.name), seed=False,
            latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        self.store = McpIdentityTokenStore(self.service.db_path)
        self.token = self.store.issue(user_subject=OWNER, expected_principal_ref=OWNER, ttl_seconds=7200)
        self.session = "agent:main:telegram:direct:1001"
        self.endpoint, _ = self.service.tasks.ensure_endpoint(user_subject=OWNER, token_id=self.token["token_id"],
            agent_host="openclaw", endpoint_key="telegram:default:1001", client_type="telegram", external_subject="1001",
            conversation_ref=self.session, capabilities=["trusted_interaction", "continuous_flow_v1"],
            route={"channel": "telegram", "to": "1001"})
        self.service.skills.save("user:" + OWNER, {"oa-pending": {"profiles": ["single", "batch"]}},
            expected_revision=0, actor="test-admin", reason="Explicit Telegram fixture Skill assignment")
        for profile in ALL_PENDING_PROFILES:
            self.fixture.oa.add_affair("synthetic-tg-" + profile, profile=profile)
        add_quarterly_affair(self.fixture)
        add_reward_affair(self.fixture)
        self.source = self.query()
        self.views = ResultViewService(self.service)

    def query(self):
        task, _ = self.service.tasks.ensure_task(user_subject=OWNER, agent_host="openclaw", host_task_key=uuid4().hex,
            origin_endpoint_id=self.endpoint["endpoint_id"], active_conversation_ref=self.session, title="合成 Telegram OA 待办")
        result = self.service.invoke(user_subject=OWNER, capability_name="oa.workflow.pending.list", arguments={},
            task_id=task["task_id"], idempotency_key=uuid4().hex)
        self.assertEqual(result["status"], "succeeded", result)
        self.service.finish_host_task(user_subject=OWNER, task_id=task["task_id"], outcome="succeeded", reason="synthetic_query_completed")
        return {"task_id": task["task_id"], "operation_id": result["operationId"]}

    def issue(self):
        value = self.views.issue(OWNER, self.source["task_id"], self.endpoint["endpoint_id"],
            {"endpointId": self.endpoint["endpoint_id"], "tokenId": self.token["token_id"]})
        return value["resultView"]["presentation"]["url"].rsplit("/", 1)[1], value

    def body(self, token, profiles=("attendance_confirmation",), request=None):
        group = self.views.read(token)["groups"][0]
        return {"operationId": group["operationId"], "targetKeys": [next(item["targetKey"] for item in group["targets"]
            if item["workflowType"] == profile) for profile in profiles], "requestKey": request or uuid4().hex}

    def execute(self, queued):
        return actions.execute_preparation(self.service, OWNER, queued["taskId"], queued["commandKey"],
            runtime_context={"endpointId": self.endpoint["endpoint_id"], "hostType": "openclaw", "hostInstanceId": "synthetic-host"})

    def append_telegram_timeline(self, *, account=None, route=None, **changes):
        # The actual host timeline writer refreshes the endpoint as part of
        # appending the message, after the query's result card was issued.
        return self.service.append_host_timeline_message(**{
            "user_subject": OWNER, "token_id": self.token["token_id"],
            "agent_host": "openclaw", "endpoint_key": self.endpoint["endpoint_key"],
            "client_type": "telegram", "external_subject": "1001", "account_id": account,
            "conversation_ref": self.session, "message_key": uuid4().hex, "role": "assistant",
            "text": "Synthetic query result card delivered", "task_id": self.source["task_id"],
            "route": route if route is not None else {"channel": "telegram", "to": "1001"},
            **changes,
        })

    def register_host(self):
        root = Path(__file__).resolve().parents[1]
        profile = deepcopy(json.loads((root / "schemas/agent-host/v1/test-vectors.json").read_text())["profiles"][0]["value"])
        profile["hostInstanceId"] = "openclaw-gateway"
        profile["implementation"] = {"name": "openclaw", "version": json.loads((root / "integrations/openclaw-agentbridge/package.json").read_text())["version"]}
        self.service.host_contract.negotiate(user_subject=OWNER, token_id=self.token["token_id"], profile=profile)
        return self.service.require_host_registration(user_subject=OWNER, agent_host="openclaw",
            host_instance_id="openclaw-gateway", host_version=profile["implementation"]["version"], minimum_level="L3")

    def counts(self):
        with self.service.tasks._connect() as connection:
            return {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in ("agent_tasks", "task_events", "operations", "business_drafts", "business_draft_decisions", "write_authorizations")}

    def test_ticket_stores_only_digest_and_opaque_targets_and_reads_never_invoke(self):
        before = self.counts()
        token, value = self.issue()
        self.assertEqual(len(token), 43)
        self.assertEqual(value["resultView"]["schemaVersion"], "agentbridge.result-view.v1")
        with patch.object(self.service, "invoke", side_effect=AssertionError("read cannot invoke")), \
             patch.object(self.service, "resume_interaction", side_effect=AssertionError("read cannot resume")):
            page = self.views.read(token)
        self.assertEqual(page["preparations"], [])
        self.assertEqual({row["workflowType"] for row in page["groups"][0]["targets"]}, set(CURRENT_PROFILES))
        self.assertNotIn("affair_id", json.dumps(page))
        with self.service.tasks._connect() as connection:
            serialized = " ".join(row[0] for row in connection.execute("SELECT payload_json FROM task_events"))
            self.assertNotIn(token, serialized)
            self.assertNotIn(self.token["token"], serialized)
        after = self.counts()
        self.assertEqual(after["task_events"], before["task_events"] + 1)
        self.assertTrue(all(after[key] == before[key] for key in before if key != "task_events"))

    def test_issue_rejects_foreign_workspace_group_or_wrong_host_token(self):
        workspace = self.fixture.add_query_case(case_key=uuid4().hex)
        for subject, task_id, endpoint_id, host in (
            ("foreign", self.source["task_id"], self.endpoint["endpoint_id"], None),
            (OWNER, workspace["task_id"], self.endpoint["endpoint_id"], None),
            (OWNER, self.source["task_id"], self.fixture.account["endpoint_id"], None),
            (OWNER, self.source["task_id"], self.endpoint["endpoint_id"], {"endpointId": self.endpoint["endpoint_id"], "tokenId": "wrong"}),
        ):
            with self.subTest(task_id=task_id, endpoint_id=endpoint_id), self.assertRaises(actions.ActionableResultError):
                self.views.issue(subject, task_id, endpoint_id, host)
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE client_endpoints SET conversation_ref='agent:main:telegram:group:-1001' WHERE endpoint_id=?", (self.endpoint["endpoint_id"],))
        with self.assertRaises(actions.ActionableResultError):
            self.issue()

    def test_live_endpoint_token_route_and_user_grants_are_checked_after_issue(self):
        token, _ = self.issue()
        cases = [("UPDATE client_endpoints SET route_json=? WHERE endpoint_id=?", (json.dumps({"channel": "telegram", "to": "2002"}), self.endpoint["endpoint_id"])),
            ("UPDATE client_endpoints SET state='inactive' WHERE endpoint_id=?", (self.endpoint["endpoint_id"],)),
            ("UPDATE mcp_identity_tokens SET state='revoked' WHERE token_id=?", (self.token["token_id"],))]
        for sql, arguments in cases:
            with self.subTest(sql=sql):
                with self.service.tasks._connect() as connection:
                    connection.execute(sql, arguments)
                with self.assertRaises(actions.ActionableResultError):
                    self.views.read(token)
                with self.service.tasks._connect() as connection:
                    connection.execute("UPDATE client_endpoints SET state='active',route_json=? WHERE endpoint_id=?", (json.dumps({"channel": "telegram", "to": "1001"}, sort_keys=True, separators=(",", ":")), self.endpoint["endpoint_id"]))
                    connection.execute("UPDATE mcp_identity_tokens SET state='active' WHERE token_id=?", (self.token["token_id"],))
        grant_permissions(self.service.user_grants, OWNER, [])
        with self.assertRaises(PermissionError):
            self.views.read(token)

    def test_initial_account_mismatch_cannot_issue_and_default_accounts_are_canonical(self):
        for endpoint_account, route_account in ((None, "other"), ("primary", "other"), ("primary", None)):
            with self.subTest(endpoint=endpoint_account, route=route_account):
                with self.service.tasks._connect() as connection:
                    connection.execute("UPDATE client_endpoints SET account_id=?,route_json=? WHERE endpoint_id=?",
                        (endpoint_account, actions._json({"channel": "telegram", "to": "1001", "accountId": route_account}), self.endpoint["endpoint_id"]))
                before = self.counts()
                with self.assertRaises(actions.ActionableResultError) as error:
                    self.issue()
                self.assertEqual(error.exception.code, "ACTION_ENDPOINT_INVALID")
                self.assertEqual(before, self.counts())
        for endpoint_account, route_account in ((None, None), ("", None), ("default", None), (None, "default"), ("primary", "primary")):
            with self.subTest(endpoint=endpoint_account, route=route_account):
                with self.service.tasks._connect() as connection:
                    connection.execute("UPDATE client_endpoints SET account_id=?,route_json=? WHERE endpoint_id=?",
                        (endpoint_account, actions._json({"channel": "telegram", "to": "1001", "accountId": route_account}), self.endpoint["endpoint_id"]))
                token, _ = self.issue()
                self.assertTrue(self.views.read(token)["selectionAvailable"])

    def test_account_change_invalidates_old_ticket_before_any_queue(self):
        token, _ = self.issue()
        body = self.body(token)
        before = self.counts()
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE client_endpoints SET account_id='other' WHERE endpoint_id=?", (self.endpoint["endpoint_id"],))
        for operation in (lambda: self.views.read(token), lambda: self.views.prepare(token, body)):
            with self.assertRaises(actions.ActionableResultError) as error:
                operation()
            self.assertEqual(error.exception.code, "ACTION_ENDPOINT_INVALID")
        self.assertEqual(before, self.counts())

        # Even a later consistent rebind to another bot account cannot inherit
        # a ticket issued for the original endpoint/account fingerprint.
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE client_endpoints SET route_json=? WHERE endpoint_id=?",
                (actions._json({"channel": "telegram", "to": "1001", "accountId": "other"}), self.endpoint["endpoint_id"]))
        with self.assertRaises(actions.ActionableResultError):
            self.views.prepare(token, body)
        self.assertEqual(before, self.counts())

    def test_real_timeline_route_aliases_keep_single_and_batch_host_prepare_and_status(self):
        # Match the production ordering: host issues while the endpoint names
        # the default account explicitly, then the assistant timeline refresh
        # writes account=None and a telegram:-prefixed recipient.
        registration = self.register_host()
        ensured = self.service.ensure_host_task(user_subject=OWNER, token_id=self.token["token_id"],
            agent_host="openclaw", host_task_key=uuid4().hex, endpoint_key=self.endpoint["endpoint_key"],
            client_type="telegram", external_subject="1001", account_id="default", conversation_ref=self.session,
            title="合成 Telegram 待办查询", route={"channel": "telegram", "to": "1001", "threadId": None},
            host_instance_id=registration["hostInstanceId"], host_version=registration["hostVersion"])
        query = self.service.invoke(user_subject=OWNER, capability_name="oa.workflow.pending.list", arguments={},
            task_id=ensured["task"]["taskId"], idempotency_key=uuid4().hex)
        self.assertEqual(query["status"], "succeeded", query)
        self.service.finish_host_task(user_subject=OWNER, task_id=ensured["task"]["taskId"],
            outcome="succeeded", reason="synthetic_query_completed")
        self.source = {"task_id": ensured["task"]["taskId"], "operation_id": query["operationId"]}
        token, _ = self.issue()
        bodies = [self.body(token), self.body(token, ("weekly_report", "attendance_confirmation"))]
        self.append_telegram_timeline(route={"channel": "telegram", "to": "telegram:1001"})
        self.assertTrue(self.views.read(token)["selectionAvailable"])
        queued = [self.views.prepare(token, body) for body in bodies]
        self.assertEqual({item["taskId"] for item in actions.pending_preparations(self.service, OWNER)},
            {item["taskId"] for item in queued})
        self.append_telegram_timeline(account="default", route={
            "channel": "telegram", "to": "1001", "accountId": "default", "threadId": None})
        self.assertEqual({item["taskId"] for item in actions.pending_preparations(self.service, OWNER)},
            {item["taskId"] for item in queued})
        for item in queued:
            lease = self.service.acquire_host_coordinator_lease(user_subject=OWNER,
                task_id=item["taskId"], registration=registration)
            with patch.object(self.service.workspace, "resolve_gateway_session", side_effect=AssertionError("Telegram is not Workspace")):
                response = self.service.prepare_host_task(user_subject=OWNER, task_id=item["taskId"],
                    command_key=item["commandKey"], session_key=self.session,
                    endpoint_key=self.endpoint["endpoint_key"], registration=registration,
                    runtime_context={"endpointId": self.endpoint["endpoint_id"], "coordinatorLeaseVersion": str(lease["version"])})
            self.assertEqual(response["status"], "requires_user_action")
            self.service.present_interaction(user_subject=OWNER, agent_host="openclaw",
                endpoint_key=self.endpoint["endpoint_key"], interaction_id=response["interaction"]["interactionId"],
                continuous_flow=True)
        variants = (
            (None, {"channel": "telegram", "to": "1001"}),
            ("default", {"channel": "telegram", "to": "telegram:1001", "accountId": None, "threadId": ""}),
            ("", {"channel": "telegram", "to": "1001", "accountId": "", "threadId": None}),
            (None, {"channel": "telegram", "to": "telegram:1001", "accountId": "default"}),
        )
        for account, route in (*variants, *reversed(variants)):
            with self.subTest(account=account, route=route):
                self.append_telegram_timeline(account=account, route=route)
                before = self.counts()
                with patch.object(self.service, "invoke", side_effect=AssertionError("restoration cannot invoke")), \
                     patch.object(self.service, "present_interaction", side_effect=AssertionError("status cannot issue")):
                    page = self.views.read(token)
                    self.assertEqual(len(page["preparations"]), 2)
                    for body, item in zip(bodies, queued):
                        self.assertEqual(self.views.prepare(token, body)["taskId"], item["taskId"])
                        reused = self.execute(item)
                        self.assertTrue(reused["reused"])
                        self.assertEqual(reused["status"], "requires_user_action")
                        status = self.views.status(token, item["taskId"])
                        self.assertFalse(status["terminal"])
                        self.assertIn("/flow/input/", status["interaction"]["presentation"]["url"])
                        self.assertEqual(status["interaction"]["presentation"]["endpointId"], self.endpoint["endpoint_id"])
                self.assertEqual(before, self.counts())
        self.assertEqual(self.views.status(token, queued[1]["taskId"])["batch"]["totalCount"], 2)
        self.assertEqual(self.counts()["business_draft_decisions"], 0)
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_real_timeline_dangerous_route_or_identity_changes_stop_every_ticket_action(self):
        token, _ = self.issue()
        body = self.body(token)
        queued = self.views.prepare(token, body)
        rotated = self.store.issue(user_subject=OWNER, expected_principal_ref=OWNER, ttl_seconds=7200)
        changes = (
            {"account": "other", "route": {"channel": "telegram", "to": "1001", "accountId": "other"}},
            {"external_subject": "2002", "conversation_ref": "agent:main:telegram:direct:2002",
                "route": {"channel": "telegram", "to": "2002"}},
            {"route": {"channel": "telegram", "to": "1001", "threadId": "77"}},
            {"route": {"channel": "web", "to": "1001"}},
            {"route": {"channel": "telegram", "to": "1001", "deliveryMode": "unexpected"}},
            {"route": {"channel": "telegram", "to": "2002"}},
            {"token_id": rotated["token_id"]},
            {"client_type": "web"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.append_telegram_timeline(**change)
                before = self.counts()
                with patch.object(self.service, "invoke", side_effect=AssertionError("changed binding cannot invoke")):
                    for operation in (lambda: self.views.read(token), lambda: self.views.prepare(token, body),
                            lambda: self.views.status(token, queued["taskId"]), lambda: self.execute(queued)):
                        with self.assertRaises(actions.ActionableResultError) as error:
                            operation()
                        self.assertEqual(error.exception.code, "ACTION_ENDPOINT_INVALID")
                    self.assertEqual(actions.pending_preparations(self.service, OWNER), [])
                self.assertEqual(before, self.counts())
                self.append_telegram_timeline()
                self.assertTrue(self.views.read(token)["selectionAvailable"])
                self.assertEqual(self.views.prepare(token, body)["taskId"], queued["taskId"])
        self.store.revoke(self.token["token_id"])
        before = self.counts()
        with patch.object(self.service, "invoke", side_effect=AssertionError("revoked identity cannot invoke")):
            for operation in (lambda: self.views.read(token), lambda: self.views.prepare(token, body),
                    lambda: self.views.status(token, queued["taskId"]), lambda: self.execute(queued)):
                with self.assertRaises(actions.ActionableResultError):
                    operation()
        self.assertEqual(before, self.counts())
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_nondefault_account_and_unknown_route_keys_remain_strict_after_canonicalization(self):
        self.append_telegram_timeline(account="primary", route={
            "channel": "telegram", "to": "1001", "accountId": "primary", "deliveryMode": "original"})
        token, _ = self.issue()
        body = self.body(token)
        queued = self.views.prepare(token, body)
        variants = (
            ("Primary", {"channel": "telegram", "to": "1001", "accountId": "Primary", "deliveryMode": "original"}),
            ("other", {"channel": "telegram", "to": "1001", "accountId": "other", "deliveryMode": "original"}),
            ("primary", {"channel": "telegram", "to": "1001", "accountId": " primary ", "deliveryMode": "original"}),
            ("primary", {"channel": "telegram", "to": "1001", "accountId": "primary"}),
            ("primary", {"channel": "telegram", "to": "1001", "accountId": "primary", "deliveryMode": "changed"}),
        )
        for account, route in variants:
            with self.subTest(account=account, route=route):
                self.append_telegram_timeline(account=account, route=route)
                before = self.counts()
                for operation in (lambda: self.views.read(token), lambda: self.views.prepare(token, body),
                        lambda: self.views.status(token, queued["taskId"]), lambda: self.execute(queued)):
                    with self.assertRaises(actions.ActionableResultError) as error:
                        operation()
                    self.assertEqual(error.exception.code, "ACTION_ENDPOINT_INVALID")
                self.assertEqual(before, self.counts())
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_ticket_shape_expiry_and_incomplete_source_fail_closed(self):
        token, _ = self.issue()
        for invalid in ("x", token + "a", token[:-1] + "/", "a" * 43):
            with self.subTest(token=invalid), self.assertRaises(actions.ActionableResultError):
                self.views.read(invalid)
        future = actions._now() + timedelta(seconds=TICKET_TTL_SECONDS + 1)
        with patch.object(actions, "_now", return_value=future), self.assertRaises(actions.ActionableResultError) as error:
            self.views.read(token)
        self.assertEqual(error.exception.code, "RESULT_VIEW_EXPIRED")
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE operations SET result_json=json_set(result_json,'$.coverage.status','partial') WHERE operation_id=?", (self.source["operation_id"],))
        with self.assertRaises(actions.ActionableResultError):
            self.views.read(token)
        with self.assertRaises(actions.ActionableResultError):
            self.issue()

    def test_single_queue_is_atomic_recoverable_and_keeps_authorization_pending(self):
        token, _ = self.issue()
        body = self.body(token)
        before = self.counts()
        with patch.object(self.service, "invoke", side_effect=AssertionError("queue cannot invoke")):
            queued = self.views.prepare(token, body)
        self.assertEqual(queued["status"], "queued")
        after = self.counts()
        for key in ("operations", "business_drafts", "business_draft_decisions", "write_authorizations"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.views.read(token)["preparations"][0]["taskId"], queued["taskId"])
        pending = actions.pending_preparations(self.service, OWNER)
        self.assertEqual(pending, [{"taskId": queued["taskId"], "commandKey": queued["commandKey"],
            "sessionKey": self.session, "endpointKey": self.endpoint["endpoint_key"], "endpoint": {
                "endpointId": self.endpoint["endpoint_id"], "clientType": "telegram", "externalSubject": "1001", "accountId": None,
                "conversationRef": self.session, "route": {"channel": "telegram", "to": "1001"}}}])
        response = self.execute(queued)
        self.assertEqual(response["status"], "requires_user_action")
        self.assertEqual(response["interaction"]["type"], "business_input")
        self.assertEqual(response["interaction"]["display"]["actionView"]["capabilityId"], "oa.attendance_confirmation.confirm")
        self.assertEqual(len(self.fixture.oa.writes), 0)
        # Only the normal host creates the endpoint presentation. Status reads it.
        self.service.present_interaction(user_subject=OWNER, agent_host="openclaw", endpoint_key=self.endpoint["endpoint_key"],
            interaction_id=response["interaction"]["interactionId"], continuous_flow=True)
        before_get = self.counts()
        with patch.object(self.service, "invoke", side_effect=AssertionError("status cannot invoke")), \
             patch.object(self.service, "present_interaction", side_effect=AssertionError("status cannot issue")):
            status = self.views.status(token, queued["taskId"])
        self.assertIn("/flow/input/", status["interaction"]["presentation"]["url"])
        self.assertEqual(status["interaction"]["presentation"]["endpointId"], self.endpoint["endpoint_id"])
        self.assertFalse(status["terminal"])
        self.assertEqual(before_get, self.counts())
        self.assertNotIn("selectedItems", json.dumps(status))
        self.assertNotIn("form_schema", json.dumps(status))

    def test_lost_reply_and_new_ticket_converge_without_duplicate_prepare(self):
        token, _ = self.issue()
        body = self.body(token)
        one = self.views.prepare(token, body)
        two = self.views.prepare(token, {**body, "requestKey": uuid4().hex})
        second_token, _ = self.issue()
        three = self.views.prepare(second_token, {**body, "requestKey": uuid4().hex})
        self.assertEqual(one["taskId"], two["taskId"])
        self.assertEqual(one["taskId"], three["taskId"])
        self.assertEqual(self.views.read(second_token)["preparations"][0]["taskId"], one["taskId"])
        first = self.execute(one)
        with patch.object(self.service, "invoke", side_effect=AssertionError("duplicate cannot invoke")):
            second = self.execute(one)
        self.assertTrue(second["reused"])
        self.assertEqual(first["operationId"], second["operationId"])
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_source_ttl_stops_new_selection_but_retains_same_request_and_existing_card(self):
        token, _ = self.issue()
        body = self.body(token)
        another = self.body(token, ("weekly_report",))
        queued = self.views.prepare(token, body)
        future = actions._now() + timedelta(seconds=601)
        with patch.object(actions, "_now", return_value=future):
            self.assertFalse(self.views.read(token)["selectionAvailable"])
            self.assertEqual(self.views.prepare(token, body)["taskId"], queued["taskId"])
            with self.assertRaises(actions.ActionableResultError) as error:
                self.views.prepare(token, another)
            self.assertEqual(error.exception.code, "ACTION_SOURCE_EXPIRED")
            self.assertEqual(self.views.status(token, queued["taskId"])["taskId"], queued["taskId"])
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_mixed_ordered_targets_use_one_persistent_batch_and_live_endpoint_projection(self):
        token, _ = self.issue()
        queued = self.views.prepare(token, self.body(token, ("intellectual_property_reward", "attendance_confirmation")))
        response = self.execute(queued)
        self.assertEqual(response["status"], "requires_user_action")
        self.assertEqual(self.service.skills.for_task(OWNER, queued["taskId"])["profile"], "batch")
        status = self.views.status(token, queued["taskId"])
        self.assertEqual(status["batch"]["totalCount"], 2)
        self.assertEqual(status["batch"]["currentIndex"], 1)
        current = self.service.get_interaction(user_subject=OWNER, interaction_id=response["interaction"]["interactionId"])
        self.assertEqual(current["interaction"]["display"]["batch"]["currentIndex"], 1)
        self.assertEqual(current["interaction"]["display"]["actionView"]["capabilityId"], "oa.intellectual_property_reward.acknowledge")
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def add_query_operation(self):
        # Simulate another successful tool in the same host turn before its
        # final finish. This fixture transition does not alter any operation.
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE agent_tasks SET status='active',finished_at=NULL WHERE task_id=?", (self.source["task_id"],))
        result = self.service.invoke(user_subject=OWNER, capability_name="oa.workflow.pending.list", arguments={},
            task_id=self.source["task_id"], idempotency_key=uuid4().hex)
        self.assertEqual(result["status"], "succeeded", result)
        self.service.finish_host_task(user_subject=OWNER, task_id=self.source["task_id"], outcome="succeeded", reason="synthetic_multi_query_completed")
        return result["operationId"]

    def test_multi_source_ticket_freezes_groups_and_recovers_each_operation_separately(self):
        second_operation = self.add_query_operation()
        token, _ = self.issue()
        groups = self.views.read(token)["groups"]
        self.assertEqual([group["operationId"] for group in groups], [self.source["operation_id"], second_operation])
        queued = []
        bodies = []
        for group in groups:
            body = {"operationId": group["operationId"], "targetKeys": [group["targets"][0]["targetKey"]], "requestKey": uuid4().hex}
            bodies.append(body)
            queued.append(self.views.prepare(token, body))
        self.assertNotEqual(queued[0]["taskId"], queued[1]["taskId"])
        self.assertEqual({item["taskId"] for item in self.views.read(token)["preparations"]}, {item["taskId"] for item in queued})
        for body, task in zip(bodies, queued):
            self.assertEqual(self.views.prepare(token, body)["taskId"], task["taskId"])
            self.assertEqual(self.views.status(token, task["taskId"])["taskId"], task["taskId"])
        before = self.counts()
        cross = {**bodies[0], "targetKeys": [groups[0]["targets"][0]["targetKey"], groups[1]["targets"][0]["targetKey"]],
            "requestKey": uuid4().hex}
        with self.assertRaises(actions.ActionableResultError) as error:
            self.views.prepare(token, cross)
        self.assertEqual(error.exception.code, "ACTION_TARGET_UNAVAILABLE")
        self.assertEqual(before, self.counts())
        later_operation = self.add_query_operation()
        self.assertEqual([group["operationId"] for group in self.views.read(token)["groups"]], [self.source["operation_id"], second_operation])
        later_group = next(group for group in actions.list_actionable_results(self.service, OWNER, self.source["task_id"])
            if group["operationId"] == later_operation)
        with self.assertRaises(actions.ActionableResultError) as error:
            self.views.prepare(token, {"operationId": later_operation, "targetKeys": [later_group["targets"][0]["targetKey"]], "requestKey": uuid4().hex})
        self.assertEqual(error.exception.code, "ACTION_SELECTION_INVALID")
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_each_frozen_source_keeps_its_original_ttl_and_existing_recovery(self):
        earlier = actions._now() - timedelta(seconds=500)
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE operations SET finished_at=? WHERE operation_id=?", (earlier.isoformat(), self.source["operation_id"]))
        second_operation = self.add_query_operation()
        token, _ = self.issue()
        groups = self.views.read(token)["groups"]
        first_body = {"operationId": groups[0]["operationId"], "targetKeys": [groups[0]["targets"][0]["targetKey"]], "requestKey": uuid4().hex}
        queued = self.views.prepare(token, first_body)
        future = actions._now() + timedelta(seconds=110)
        with patch.object(actions, "_now", return_value=future):
            page = self.views.read(token)
            self.assertTrue(page["selectionAvailable"])
            self.assertEqual([group["operationId"] for group in page["groups"]], [second_operation])
            self.assertEqual(self.views.prepare(token, first_body)["taskId"], queued["taskId"])
            with self.assertRaises(actions.ActionableResultError) as error:
                self.views.prepare(token, {**first_body, "targetKeys": [groups[0]["targets"][1]["targetKey"]], "requestKey": uuid4().hex})
            self.assertEqual(error.exception.code, "ACTION_SOURCE_EXPIRED")
            second = page["groups"][0]
            self.assertEqual(self.views.prepare(token, {"operationId": second_operation,
                "targetKeys": [second["targets"][0]["targetKey"]], "requestKey": uuid4().hex})["status"], "queued")
            self.assertEqual(self.views.status(token, queued["taskId"])["taskId"], queued["taskId"])

    def test_more_than_16_source_groups_is_explicitly_rejected_without_silently_truncating(self):
        groups = actions.list_actionable_results(self.service, OWNER, self.source["task_id"])
        before = self.counts()
        with patch.object(actions, "list_actionable_results", return_value=groups * 17), self.assertRaises(actions.ActionableResultError) as error:
            self.issue()
        self.assertEqual(error.exception.code, "ACTION_SOURCE_LIMIT")
        self.assertEqual(before, self.counts())

    def test_prepare_hint_is_durable_origin_only_and_never_invokes_before_host_lease(self):
        token, _ = self.issue()
        queued = self.views.prepare(token, self.body(token))
        with patch.object(self.service, "invoke", side_effect=AssertionError("notification cannot execute")):
            result = self.service.claim_host_notifications(user_subject=OWNER, agent_host="openclaw",
                endpoint_key=self.endpoint["endpoint_key"], limit=50)
        hints = [item for item in result["notifications"] if item["deliveryMode"] == "prepared_command"]
        self.assertEqual(len(hints), 1)
        hint = hints[0]
        self.assertEqual(hint["task"]["taskId"], queued["taskId"])
        self.assertEqual(hint["task"]["originEndpointId"], self.endpoint["endpoint_id"])
        self.assertEqual(hint["task"]["activeConversationRef"], self.session)
        self.assertEqual(hint["event"]["eventType"], "task.prepare.requested")
        self.assertEqual(hint["event"]["payload"], {"commandKey": queued["commandKey"]})
        with self.service.tasks._connect() as connection:
            ids = [row[0] for row in connection.execute("SELECT endpoint_id FROM notification_outbox WHERE event_id=?", (hint["event"]["eventId"],))]
        self.assertEqual(ids, [self.endpoint["endpoint_id"]])
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_frozen_command_source_and_permissions_are_live_before_host_prepare(self):
        token, _ = self.issue()
        queued = self.views.prepare(token, self.body(token))
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE client_endpoints SET route_json=? WHERE endpoint_id=?", (json.dumps({"channel": "telegram", "to": "2002"}), self.endpoint["endpoint_id"]))
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])
        with patch.object(self.service, "invoke", side_effect=AssertionError("invalid endpoint cannot invoke")), self.assertRaises(actions.ActionableResultError):
            self.execute(queued)
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_foreign_or_other_ticket_task_status_cannot_be_read(self):
        token, _ = self.issue()
        queued = self.views.prepare(token, self.body(token))
        other, _ = self.issue()
        with self.assertRaises(actions.ActionableResultError):
            self.views.status(other, queued["taskId"])
        for task_id in (self.source["task_id"], str(uuid4())):
            with self.subTest(task_id=task_id), self.assertRaises(actions.ActionableResultError):
                self.views.status(token, task_id)

    def test_unknown_state_never_offers_active_interaction_or_replays(self):
        token, _ = self.issue()
        queued = self.views.prepare(token, self.body(token))
        response = self.execute(queued)
        self.service.present_interaction(user_subject=OWNER, agent_host="openclaw", endpoint_key=self.endpoint["endpoint_key"],
            interaction_id=response["interaction"]["interactionId"], continuous_flow=True)
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE agent_tasks SET status='outcome_unknown' WHERE task_id=?", (queued["taskId"],))
        before = self.counts()
        status = self.views.status(token, queued["taskId"])
        self.assertTrue(status["terminal"])
        self.assertNotIn("interaction", status)
        self.assertIn("不要重复提交", status["message"])
        self.assertEqual(before, self.counts())
        self.assertEqual(self.views.prepare(token, self.body(token))["taskId"], queued["taskId"])
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_real_l3_lease_prepare_uses_exact_telegram_endpoint_without_web_account(self):
        registration = self.register_host()
        token, _ = self.issue()
        queued = self.views.prepare(token, self.body(token))
        lease = self.service.acquire_host_coordinator_lease(user_subject=OWNER, task_id=queued["taskId"], registration=registration)
        arguments = dict(user_subject=OWNER, task_id=queued["taskId"], command_key=queued["commandKey"], session_key=self.session,
            endpoint_key=self.endpoint["endpoint_key"], registration=registration,
            runtime_context={"endpointId": self.endpoint["endpoint_id"], "coordinatorLeaseVersion": str(lease["version"])})
        with patch.object(self.service.workspace, "resolve_gateway_session", side_effect=AssertionError("Telegram is not Workspace")):
            response = self.service.prepare_host_task(**arguments)
        self.assertEqual(response["status"], "requires_user_action")
        for change in ({"session_key": self.fixture.account["openclaw_session_key"]}, {"endpoint_key": "telegram:default:2002"},
                       {"runtime_context": {"endpointId": self.endpoint["endpoint_id"], "coordinatorLeaseVersion": "999"}}):
            with self.subTest(change=change), patch.object(self.service, "invoke", side_effect=AssertionError("wrong binding cannot invoke")), \
                 self.assertRaises((ValueError, KeyError, PermissionError, TaskIntegrityError, HostLeaseConflict)):
                self.service.prepare_host_task(**{**arguments, **change})
        self.assertEqual(len(self.fixture.oa.writes), 0)

    def test_all_16_types_prepare_without_decision_and_registered_action_labels_are_closed(self):
        token, _ = self.issue()
        for profile in CURRENT_PROFILES:
            with self.subTest(profile=profile):
                queued = self.views.prepare(token, self.body(token, (profile,)))
                response = self.execute(queued)
                self.assertEqual(response["status"], "requires_user_action")
                hint = response["interaction"]["display"]["actionView"]
                self.assertIn(hint["capabilityId"], {item.commit for item in PROFILES})
        self.assertEqual({item.commit for item in PROFILES} - {row["commit_capability"] for row in _TRUSTED_WRITE_DEFINITIONS.values()}, set())
        self.assertEqual(len(self.fixture.oa.writes), 0)
        self.assertEqual(self.counts()["business_draft_decisions"], 0)


class TelegramResultViewMcpTests(unittest.TestCase):
    def test_tool_is_host_private_requires_exact_l3_task_endpoint_and_never_a_model_business_tool(self):
        from agentbridge.mcp.central import AGENT_FACING_TOOL_SCOPE_REQUIREMENTS, HOST_COORDINATION_TOOL_NAMES
        from tests.test_central_mcp import CentralMcpFixture, CentralMcpTests
        self.assertNotIn("agentbridge_result_view", AGENT_FACING_TOOL_SCOPE_REQUIREMENTS)
        self.assertIn("agentbridge_result_view", HOST_COORDINATION_TOOL_NAMES)
        with CentralMcpFixture() as (service, store, token, client):
            params = {"name": "agentbridge_result_view", "arguments": {"operation": "issue", "task_id": "task-1234567890-abcdef"}}
            host = {"version": "1", "agentHost": "openclaw", "hostInstanceId": "openclaw-gateway", "hostVersion": "0.4.65"}
            with patch.object(ResultViewService, "issue", return_value={"status": "succeeded", "resultView": {}}) as issue:
                for index, task_meta in enumerate(({}, {"taskId": "different-task-id", "endpointId": "endpoint-one"}, {"taskId": "task-1234567890-abcdef"})):
                    response = CentralMcpTests._request(client, "tools/call", request_id=8000+index, token=token,
                        params={**params, "_meta": {"io.agentbridge/host-context": host, "io.agentbridge/task": task_meta}})
                    self.assertTrue(response.json()["result"]["isError"])
                issue.assert_not_called()
                response = CentralMcpTests._request(client, "tools/call", request_id=8010, token=token,
                    params={**params, "_meta": {"io.agentbridge/host-context": host, "io.agentbridge/task": {
                        "taskId": "task-1234567890-abcdef", "endpointId": "endpoint-one"}}})
                self.assertFalse(response.json()["result"]["isError"], response.json())
                self.assertEqual(service.require_host_registration.call_args.kwargs["minimum_level"], "L3")
                self.assertEqual(issue.call_args.args[:3], ("user-a", "task-1234567890-abcdef", "endpoint-one"))
                self.assertEqual(issue.call_args.args[3]["tokenId"], store.list()[0]["token_id"])
