"""Synthetic Workspace/credible-card acceptance environment with a fake coordinator.

The HTTP applications, CSRF, stores and task projection are real. Business outcomes,
the coordinator and optional login broker are fixtures, not business verification.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading
from uuid import uuid4

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.gateway import GatewayRequestError
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.authorization_fixtures import grant_permissions
from tests.test_workspace import PASSWORD, _create_account

OWNER = "interaction-review"
PREPARE = "oa.leave.prepare"
COMMIT = "oa.leave.save_draft"
FIXTURE_VERSION = "synthetic-interaction-v1"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_metadata(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


class IsolatedService(CentralCapabilityService):
    """Reject business execution even if a future UI accidentally requests it."""
    def invoke(self, *args, **kwargs):
        raise RuntimeError("Synthetic acceptance environment cannot invoke business capabilities")

    def resume_interaction(self, *, user_subject, interaction_id, idempotency_key=None):
        return self.fixture.resume(user_subject, interaction_id)


class SyntheticBroker:
    def __init__(self, store):
        self.store = store

    def authenticate(self, *, challenge_id, csrf_token, csrf_cookie, credentials):
        self.store.claim(challenge_id, csrf_token=csrf_token, csrf_cookie=csrf_cookie)
        credentials.clear()
        self.store.complete(challenge_id, result={"observed_principal_ref": OWNER})
        return {"status": "succeeded", "challengeId": challenge_id}


class ReadOnlyGateway:
    """Only the two Workspace read operations exist; no chat/model execution."""
    def call(self, method, params=None, **kwargs):
        if method == "chat.history":
            return {"sessionId": "synthetic-read-only", "messages": []}
        if method == "system.info":
            return {"version": "synthetic-read-only"}
        raise GatewayRequestError("FIXTURE_READ_ONLY", "Synthetic gateway supports read-only history and status")

    def send_stream(self, **kwargs):
        raise GatewayRequestError("FIXTURE_READ_ONLY", "Synthetic gateway cannot send messages")

    def stream(self, **kwargs):
        return iter(())


class AcceptanceFixture:
    def __init__(self, home: Path):
        self.home = home.resolve()
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.meta_path = self.home / "acceptance.json"
        if any(self.home.iterdir()) and not self.meta_path.exists():
            raise ValueError("Use an empty isolated directory, never an existing runtime")
        if self.meta_path.exists():
            self.meta = json.loads(self.meta_path.read_text())
            if self.meta.get("fixture") != FIXTURE_VERSION:
                raise ValueError("Not an interaction acceptance environment")
        else:
            self.meta = {"fixture": FIXTURE_VERSION, "workspace_port": free_port(), "card_port": free_port()}
            write_metadata(self.meta_path, self.meta)
        key = self.home / "session.key"
        if not key.exists():
            key.write_bytes(os.urandom(32))
            key.chmod(0o600)
        self.previous_key = os.environ.get("AGENTBRIDGE_SESSION_KEY_FILE")
        os.environ["AGENTBRIDGE_SESSION_KEY_FILE"] = str(key)
        self.card_url = f"http://127.0.0.1:{self.meta['card_port']}"
        self.workspace_url = f"http://127.0.0.1:{self.meta['workspace_port']}"
        self.service = IsolatedService(home=self.home, base_url="http://127.0.0.1:1/disabled",
            trusted_card_base_url=self.card_url, worker_factory=self._deny_worker)
        self.service.fixture = self
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        if "account_id" not in self.meta:
            self._seed()
        self.account = self.service.workspace.get_account(self.meta["account_id"])
        self.app = WorkspaceApplication(service=self.service, gateway=ReadOnlyGateway())

    @staticmethod
    def _deny_worker(*args, **kwargs):
        raise RuntimeError("No downstream browser or network is available in this fixture")

    def _task(self, key, title):
        result = self.service.ensure_host_task(user_subject=OWNER, token_id="fixture-token", agent_host="openclaw",
            host_task_key=f"acceptance|{key}", endpoint_key=self.account["endpoint_key"], client_type="web",
            external_subject=self.account["account_id"], conversation_ref=self.account["openclaw_session_key"], title=title)
        task_id = result["task"]["taskId"]
        self.service.tasks.append_timeline_message(user_subject=OWNER, source_endpoint_id=self.account["endpoint_id"],
            message_key=f"fixture:{key}", role="assistant", text=title + "（合成验收，不连接业务系统）", task_id=task_id)
        return task_id

    def _operation(self, key, capability=PREPARE):
        return self.service.operations.create(user_subject=OWNER, capability_name=capability, capability_version="1",
            input_summary={"synthetic": True}, idempotency_key=key)[0]

    def _observe(self, task_id, operation, interaction=None):
        self.service.observe_host_task(user_subject=OWNER, task_id=task_id,
            operation_ids=[operation["operation_id"]], interaction_ids=[interaction["interactionId"]] if interaction else [])

    def _authorization(self, task_id, key, values=None, *, operation=None, resume_arguments=None):
        operation = operation or self._operation(key)
        authorization = self.service.write_authorizations.create(user_subject=OWNER, system_id="oa", session_id="fixture-session",
            capability_name=COMMIT, capability_version="1", prepare_operation_id=operation["operation_id"],
            supersession_key=key[:128],
            plan={"synthetic": True, "values": values or {}, "user_subject": OWNER, "prepare_capability": PREPARE,
                "resume_arguments": resume_arguments or {}, "session_binding": {"session_id": "fixture-session",
                    "expected_principal_ref": OWNER, "downstream_principal_ref": None, "last_verified_at": None}},
            summary={"title": "确认保存合成事项", "system": "合成 OA",
            "effect": "只更新隔离验收账本，不访问真实系统", "fields": [{"label": "材料", "value": str((values or {}).get("reason", "合成事项"))}]},
            card_base_url=self.card_url, ttl_seconds=1800)
        interaction = self.service._execution_authorization_interaction(authorization)
        self.service.operations.mark_requires_user_action(operation["operation_id"], code="AUTHORIZATION_REQUIRED",
            message="等待合成授权", next_action={"interaction": interaction})
        self._observe(task_id, operation, interaction)
        return authorization, interaction, operation

    def _seed(self):
        self.account = _create_account(self.service, user_subject=OWNER, username="cardreview", endpoint_key="telegram:*:cardreview")
        _create_account(self.service, user_subject="interaction-other", username="cardother", endpoint_key="telegram:*:cardother")
        # Minimal grant only supports projection of this fixture's named operations.
        from agentbridge.core.user_grants import PERMISSIONS
        permissions = [key for key, value in PERMISSIONS.items() if "oa:write:draft" in value["legacy_scopes"] or "oa:write:approval" in value["legacy_scopes"]]
        grant_permissions(self.service.user_grants, OWNER, permissions)
        self.meta["account_id"] = self.account["account_id"]
        self.meta["flows"] = []
        self.add_case("initial")

    def add_case(self, case_key=None):
        """Add fresh immutable inputs for another acceptance batch, retaining history."""
        case_key = case_key or uuid4().hex[:8]
        task_id = self._task(f"form-{case_key}", f"填写与授权接续验收 · {case_key}")
        operation = self._operation(f"fixture-form-{case_key}")
        schema = {"title": "填写合成事项", "system": "合成 OA", "notice": "只填写合成材料，切勿输入真实秘密。",
            "effect": "准备隔离验收记录", "fields": [
                {"name": "reason", "label": "事项说明", "control": "textarea", "required": True, "max_length": 200},
                {"name": "category", "label": "类别", "control": "select", "required": True,
                 "options": [{"value": "review", "label": "验收"}, {"value": "preview", "label": "预览"}], "value": "review"}]}
        submission = self.service.field_submissions.create(user_subject=OWNER, system_id="oa", session_id="fixture-session",
            capability_name=PREPARE, capability_version="1", create_operation_id=operation["operation_id"],
            supersession_key=f"fixture-form-{case_key}",
            form_schema=schema, card_base_url=self.card_url, ttl_seconds=1800)
        interaction = self.service._business_input_interaction(submission)
        self.service.operations.mark_requires_user_action(operation["operation_id"], code="FIELD_INPUT_REQUIRED",
            message="等待合成输入", next_action={"interaction": interaction})
        self._observe(task_id, operation, interaction)
        self.meta["flows"].append({"task_id": task_id, "input_id": submission["submission_id"], "input_interaction": interaction["interactionId"]})
        batch_task = self._task(f"batch-{case_key}", f"合成批次：2 成功、1 未知、2 未处理 · {case_key}")
        batch, _ = self.service.tasks.create_batch(parent_task_id=batch_task, user_subject=OWNER, system_id="oa",
            capability_name="oa.workflow.pending.batch.prepare", selection_summary={"synthetic": True}, failure_policy="stop_on_failure",
            items=[{"resource_ref": f"synthetic-{i}", "display_summary": {"title": f"合成事项 {i}"}} for i in range(1, 6)])
        for ordinal in range(1, 4):
            authorization, interaction, operation = self._authorization(batch_task, f"fixture-batch-{case_key}-{ordinal}",
                resume_arguments={"batch_id": batch["batch_id"], "affair_id": f"synthetic-{ordinal}"})
            token = self.service.write_authorizations.issue_csrf(authorization["authorization_id"])
            self.service.write_authorizations.decide(authorization["authorization_id"], decision="approve", csrf_token=token, csrf_cookie=token)
            committed = self._operation(f"fixture-batch-commit-{case_key}-{ordinal}", COMMIT)
            self.service.write_authorizations.consume(authorization["authorization_id"], user_subject=OWNER, system_id="oa",
                session_id="fixture-session", capability_name=COMMIT, capability_version="1", commit_operation_id=committed["operation_id"])
            self.service.tasks.record_batch_item_activity(parent_task_id=batch_task, user_subject=OWNER,
                operation_id=committed["operation_id"], interaction_id=interaction["interactionId"])
            if ordinal < 3:
                self.service.operations.mark_succeeded(committed["operation_id"], {"synthetic": True})
                self.service.tasks.complete_current_batch_item(parent_task_id=batch_task, user_subject=OWNER,
                    operation_id=committed["operation_id"], expected_ordinal=ordinal, result_summary={"synthetic": True})
            else:
                self.service.operations.mark_unknown(committed["operation_id"], code="RESULT_UNKNOWN", message="合成核验无法确认；不得重放")
                self.service.tasks.fail_current_batch_item(parent_task_id=batch_task, user_subject=OWNER,
                    operation_id=committed["operation_id"], expected_ordinal=ordinal, item_state="outcome_unknown", error_code="RESULT_UNKNOWN")
            self._observe(batch_task, committed, interaction)
        self.meta.update(form_task_id=task_id, batch_task_id=batch_task)
        write_metadata(self.meta_path, self.meta)

    def resume(self, user_subject, interaction_id):
        if user_subject != OWNER:
            raise KeyError("fixture interaction not found")
        with self.lock:
            flow = next((f for f in self.meta["flows"] if interaction_id in (f["input_interaction"], f.get("authorization_interaction"))), None)
            if flow is None:
                raise RuntimeError("Only the synthetic form can be resumed")
            self._advance(flow)
            if flow.get("result"):
                return flow["result"]
            current = flow.get("authorization_interaction", flow["input_interaction"])
            return self.service.get_interaction(user_subject=OWNER, interaction_id=current)

    def _advance(self, flow):
        if flow.get("result"):
            return
        submission = self.service.field_submissions.get(flow["input_id"])
        if submission["state"] == "submitted" and not flow.get("authorization_id"):
            prepared = self._operation(f"fixture-freeze-{flow['input_id']}")
            values = self.service.field_submissions.consume(flow["input_id"], user_subject=OWNER, system_id="oa",
                session_id="fixture-session", capability_name=PREPARE, capability_version="1", consume_operation_id=prepared["operation_id"])
            authorization, interaction, _ = self._authorization(flow["task_id"], f"fixture-authorize-{flow['input_id']}", values.get("values"),
                operation=prepared, resume_arguments=submission["form_schema"].get("_agentbridge_resume_arguments") or {})
            flow.update(authorization_id=authorization["authorization_id"], authorization_interaction=interaction["interactionId"])
            write_metadata(self.meta_path, self.meta)
        if flow.get("authorization_id"):
            authorization = self.service.write_authorizations.get(flow["authorization_id"])
            if authorization["state"] == "approved":
                operation = self._operation(f"fixture-commit-{flow['input_id']}", COMMIT)
                self.service.write_authorizations.consume(flow["authorization_id"], user_subject=OWNER, system_id="oa",
                    session_id="fixture-session", capability_name=COMMIT, capability_version="1", commit_operation_id=operation["operation_id"])
                self.service.operations.mark_succeeded(operation["operation_id"], {"synthetic": True, "verified": True})
                self._observe(flow["task_id"], operation)
                self.service.tasks.complete_task(task_id=flow["task_id"], user_subject=OWNER, reason="synthetic_fixture_verified")
                flow["result"] = {"status": "succeeded", "taskId": flow["task_id"], "synthetic": True}
                write_metadata(self.meta_path, self.meta)

    def servers(self):
        fields = TrustedFieldApplication(submission_store=self.service.field_submissions)
        actions = TrustedActionApplication(authorization_store=self.service.write_authorizations)
        kwargs = {}
        try:
            from agentbridge.auth.flow import TrustedFlowApplication
        except ImportError:
            pass
        else:
            kwargs["flow_application"] = TrustedFlowApplication(service=self.service, field_application=fields, action_application=actions)
        cards = create_auth_http_server(config=validate_auth_server_config(host="127.0.0.1", port=self.meta["card_port"],
            public_base_url=self.card_url, tls_cert=None, tls_key=None),
            application=TrustedAuthApplication(challenge_store=self.service.challenges, broker=SyntheticBroker(self.service.challenges)),
            field_application=fields, action_application=actions, **kwargs)
        workspace = create_workspace_http_server(config=validate_workspace_server_config(host="127.0.0.1", port=self.meta["workspace_port"],
            public_base_url=self.workspace_url, tls_cert=None, tls_key=None), application=self.app)
        return cards, workspace

    def coordinate(self):
        while not self.stop_event.wait(0.3):
            with self.lock:
                for flow in self.meta["flows"]:
                    self._advance(flow)

    def public_metadata(self):
        return {"fixture": FIXTURE_VERSION, "workspace_url": self.workspace_url, "card_origin": self.card_url,
            "username": "cardreview", "other_username": "cardother", "synthetic_password": PASSWORD,
            "form_task_id": self.meta["form_task_id"], "batch_task_id": self.meta["batch_task_id"],
            "input_path": f"/input/{self.meta['flows'][-1]['input_id']}",
            "flow_path": f"/flow/input/{self.meta['flows'][-1]['input_id']}",
            "evidence_boundary": "Real UI/stores; synthetic coordinator/outcomes; no model or business systems"}

    def close(self):
        self.stop_event.set()
        self.app.close()
        if os.environ.get("AGENTBRIDGE_SESSION_KEY_FILE") == str(self.home / "session.key"):
            if self.previous_key is None:
                os.environ.pop("AGENTBRIDGE_SESSION_KEY_FILE", None)
            else:
                os.environ["AGENTBRIDGE_SESSION_KEY_FILE"] = self.previous_key
