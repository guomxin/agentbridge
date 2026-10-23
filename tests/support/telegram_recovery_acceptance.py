"""Independent cross-channel recovery with real consumed OA authorizations.

Only OA transport/DOM is synthetic. Tokens, private endpoints, host leases,
queries, business decisions, unknown operations and recovery edges are real.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from agentbridge.core import actionable_results as actions
from agentbridge.core.business_drafts import BusinessDraftService
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.oa_pending_recovery import recovery_child_for_origin
from agentbridge.core.telegram_result_views import ResultViewService
from tests.support.full_business_acceptance import OWNER
from tests.support.interaction_acceptance import write_metadata
from tests.support.oa_pending_recovery_acceptance import PendingRecoveryAcceptanceFixture
from tests.test_full_business_acceptance import decide, drive
from tests.test_workspace import PASSWORD


CASES = ("telegram-origin", "workspace-origin", "batch-current", "complete", "conflict")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class TelegramRecoveryAcceptanceFixture(PendingRecoveryAcceptanceFixture):
    def __init__(self, home, *, cases=CASES):
        super().__init__(home, modes=())
        if any(name not in CASES for name in cases):
            raise ValueError("Unknown isolated Telegram recovery case")
        self.credentials_path = self.home / "browser-credentials.json"
        write_metadata(self.credentials_path, {"username": "cardreview", "password": PASSWORD,
            "otherUsername": "cardother", "otherPassword": PASSWORD})
        self.views = ResultViewService(self.service)
        self.telegram_session = "agent:main:telegram:direct:1001"
        identity = self.meta.get("telegram_recovery_identity")
        if identity is None:
            token = McpIdentityTokenStore(self.service.db_path).issue(
                user_subject=OWNER, expected_principal_ref=OWNER, ttl_seconds=7200)
            identity = {"token_id": token["token_id"]}
            self.meta["telegram_recovery_identity"] = identity
        self.telegram_endpoint, _ = self.service.tasks.ensure_endpoint(user_subject=OWNER,
            token_id=identity["token_id"], agent_host="openclaw", endpoint_key="telegram:default:1001",
            client_type="telegram", external_subject="1001", conversation_ref=self.telegram_session,
            capabilities=["trusted_interaction", "continuous_flow_v1"], route={"channel": "telegram", "to": "1001"})
        root = Path(__file__).resolve().parents[2]
        profile = deepcopy(json.loads((root / "schemas/agent-host/v1/test-vectors.json").read_text())["profiles"][0]["value"])
        profile["hostInstanceId"] = "synthetic-telegram-recovery-host"
        profile["implementation"] = {"name": "openclaw", "version": json.loads(
            (root / "integrations/openclaw-agentbridge/package.json").read_text())["version"]}
        self.service.host_contract.negotiate(user_subject=OWNER, token_id=identity["token_id"], profile=profile)
        self.registration = self.service.require_host_registration(user_subject=OWNER, agent_host="openclaw",
            host_instance_id=profile["hostInstanceId"], host_version=profile["implementation"]["version"], minimum_level="L3")
        self.meta.setdefault("telegram_recovery_cases", [])
        if not self.meta.get("telegram_recovery_initialized"):
            if self.meta["telegram_recovery_cases"]:
                raise ValueError("Interrupted seeding: use a fresh independent fixture home")
            for name in cases:
                self.seed_case(name)
            # Both channels now observe the same exact unknown affairs.
            self.meta["telegram_recovery_query"] = self.query_telegram()
            for case in self.meta["telegram_recovery_cases"]:
                if case["case"] == "complete":
                    self.oa.data["affairs"][case["arguments"]["affair_id"]]["processed"] = True
            self.oa.persist()
            self.meta["telegram_recovery_initialized"] = True
            write_metadata(self.meta_path, self.meta)
            write_metadata(self.home / "baseline-ledger.json", self.audit())

    def query_telegram(self):
        task, _ = self.service.tasks.ensure_task(user_subject=OWNER, agent_host="openclaw", host_task_key=uuid4().hex,
            origin_endpoint_id=self.telegram_endpoint["endpoint_id"], active_conversation_ref=self.telegram_session,
            title="合成 Telegram 恢复验收 · 查询 OA 待办")
        result = self.service.invoke(user_subject=OWNER, capability_name="oa.workflow.pending.list", arguments={},
            task_id=task["task_id"], idempotency_key=uuid4().hex)
        if result["status"] != "succeeded":
            raise AssertionError(result)
        self.service.finish_host_task(user_subject=OWNER, task_id=task["task_id"], outcome="succeeded",
            reason="synthetic_telegram_query_completed")
        issued = self.views.issue(OWNER, task["task_id"], self.telegram_endpoint["endpoint_id"],
            {"endpointId": self.telegram_endpoint["endpoint_id"], "tokenId": self.meta["telegram_recovery_identity"]["token_id"]})
        return {"task_id": task["task_id"], "operation_id": result["operationId"],
            "url": issued["resultView"]["presentation"]["url"]}

    def execute_telegram_preparation(self, queued):
        lease = self.service.acquire_host_coordinator_lease(user_subject=OWNER, task_id=queued["taskId"],
            registration=self.registration)
        return self.service.prepare_host_task(user_subject=OWNER, task_id=queued["taskId"],
            command_key=queued["commandKey"], session_key=self.telegram_session,
            endpoint_key=self.telegram_endpoint["endpoint_key"], registration=self.registration,
            runtime_context={"endpointId": self.telegram_endpoint["endpoint_id"],
                "coordinatorLeaseVersion": str(lease["version"])})

    def prepare_telegram(self, affairs):
        query = self.query_telegram()
        token = query["url"].rsplit("/", 1)[1]
        group = self.views.read(token)["groups"][0]
        titles = {self.oa.data["affairs"][key]["source"]["title"] for key in affairs}
        keys = [item["targetKey"] for item in group["targets"] if item["title"] in titles]
        if len(keys) != len(affairs):
            raise AssertionError("Telegram source did not expose the exact requested synthetic affairs")
        queued = self.views.prepare(token, {"operationId": group["operationId"],
            "targetKeys": keys, "requestKey": uuid4().hex})
        result = self.execute_telegram_preparation(queued)
        if result["status"] != "requires_user_action":
            raise AssertionError(result)
        case = {"task_id": queued["taskId"], "input_id": result["nextAction"]["inputSubmissionId"],
            "input_interaction": result["interaction"]["interactionId"], "arguments": {"affair_id": affairs[0]}}
        self.meta["draft_cases"].append(case)
        return case

    def decide_case(self, case, opinion="独立合成决定；恢复不得复制此旧意见。"):
        drafts = BusinessDraftService(self.service)
        opened = drafts.open(case["input_id"])
        credentials = {"source_submission_id": case["input_id"], "presentation_id": None,
            "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"], values={"opinion": opinion})
        preview = drafts.preview(**credentials, expected_revision=saved["revision"])
        decision = decide(drafts, credentials, preview)
        assert decide(drafts, credentials, preview)["receipt"] == decision["receipt"]
        return decision

    def seed_case(self, name):
        if name == "batch-current":
            case = self.add_pending_batch(("weekly_report", "efficiency_data", "attendance_confirmation"))
            first = self.decide_case(case)
            result = drive(self, case)
            assert self.service.write_authorizations.get(first["receipt"]["authorizationId"])["state"] == "consumed"
            case = {**case, "input_id": result["nextAction"]["inputSubmissionId"],
                "input_interaction": result["interaction"]["interactionId"],
                "arguments": {"affair_id": case["affair_ids"][1]}}
        elif name == "telegram-origin":
            affair = "synthetic-telegram-recovery-" + name
            self.oa.add_affair(affair, profile="efficiency_data")
            self.oa.data["affairs"][affair]["source"]["title"] += "（TG 原始任务）"
            self.oa.data["affairs"][affair]["detail"]["title"] = self.oa.data["affairs"][affair]["source"]["title"]
            self.oa.persist()
            case = self.prepare_telegram([affair])
        else:
            case = self.add_pending_case("efficiency_data", case_key="telegram-recovery-" + name)
        affair = case["arguments"]["affair_id"]
        self.oa.data["affairs"][affair]["readiness_case"] = "unknown"
        decision = self.decide_case(case)
        drive(self, case)
        auth = self.service.write_authorizations.get(decision["receipt"]["authorizationId"])
        assert auth["state"] == "consumed"
        assert self.service.operations.get(auth["commit_operation_id"])["status"] == "unknown"
        assert self.service.tasks.get_task(case["task_id"], user_subject=OWNER)["status"] == "outcome_unknown"
        self.oa.data["affairs"][affair]["readiness_case"] = "late"
        self.oa.data["affairs"][affair]["processed"] = False
        self.oa.data["affairs"][affair]["done_conflict"] = name == "conflict"
        self.oa.persist()
        record = {**case, "case": name, "original_authorization_id": auth["authorization_id"],
            "original_operation_id": auth["commit_operation_id"], "original_authorization_hash": digest(auth)}
        self.meta["telegram_recovery_cases"].append(record)
        write_metadata(self.meta_path, self.meta)
        return record

    def _coordinate_preparations(self):
        while not self.stop_event.wait(.1):
            for command in actions.pending_preparations(self.service, OWNER):
                try:
                    if (command.get("endpoint") or {}).get("endpointId") == self.telegram_endpoint["endpoint_id"]:
                        result = self.execute_telegram_preparation(command)
                    else:
                        result = actions.execute_preparation(self.service, OWNER, command["taskId"], command["commandKey"],
                            runtime_context={"endpointId": self.account["endpoint_id"], "hostType": "openclaw",
                                "hostInstanceId": "synthetic-workspace-coordinator"})
                    if result.get("interaction"):
                        with self.lock:
                            if not any(case["task_id"] == command["taskId"] for case in self.meta["draft_cases"]):
                                self.meta["draft_cases"].append({"task_id": command["taskId"]})
                except Exception as error:
                    self.coordinator_errors.append(type(error).__name__ + ": " + str(error))
                    self.coordinator_errors[:] = self.coordinator_errors[-10:]

    def audit(self):
        with self.service.tasks._connect() as db:
            count = lambda sql: db.execute(sql).fetchone()[0]
            result = {"decisions": count("SELECT count(*) FROM business_draft_decisions"),
                "authorizations": dict(db.execute("SELECT state,count(*) FROM write_authorizations GROUP BY state")),
                "modelDispatches": count("SELECT count(*) FROM agent_host_dispatches"),
                "recoveryChildren": count("SELECT count(*) FROM task_events WHERE event_type='task.oa_recovery.child_created'"),
                "syntheticWrites": len(self.oa.writes), "originals": [], "coordinatorErrors": list(self.coordinator_errors)}
        for case in self.meta["telegram_recovery_cases"]:
            auth = self.service.write_authorizations.get(case["original_authorization_id"])
            child = recovery_child_for_origin(self.service, subject=OWNER, task_id=case["task_id"])
            batch = self.service.tasks.get_batch_for_task(parent_task_id=case["task_id"], user_subject=OWNER)
            result["originals"].append({"case": case["case"], "taskId": case["task_id"],
                "authorizationUnchanged": digest(auth) == case["original_authorization_hash"],
                "authorizationState": auth["state"], "operationStatus": self.service.operations.get(case["original_operation_id"])["status"],
                "childTaskId": child, "childStatus": self.service.tasks.get_task(child, user_subject=OWNER)["status"] if child else None,
                "batch": {key: batch[key] for key in ("state", "current_ordinal", "succeeded_count", "total_count")} if batch else None,
                "unprocessedLaterAffairs": [key for key in case.get("affair_ids", [])[2:] if not self.oa.data["affairs"][key]["processed"]]})
        return result

    def public_metadata(self):
        return {"fixture": "telegram-recovery-real-central-synthetic-oa-v1", "home": str(self.home),
            "workspace_url": self.workspace_url, "card_origin": self.card_url,
            "credentials_file": str(self.credentials_path), "telegram_endpoint_id": self.telegram_endpoint["endpoint_id"],
            "telegram_query": self.meta["telegram_recovery_query"], "cases": self.meta["telegram_recovery_cases"],
            "evidence_boundary": "Real Central/Auth/Workspace/private Telegram ticket/L3 lease/decisions; closed synthetic OA; browser bridge is not a native Telegram client"}
