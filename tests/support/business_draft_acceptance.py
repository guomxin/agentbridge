"""Real trusted-draft/central/Taihua execution against a network-free HTTP fixture.

Only the downstream HTTP transport is synthetic. No authorization, operation
outcome, field submission or resume implementation is fabricated here.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.auth.flow import TrustedFlowApplication
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.authorization_fixtures import grant_permissions
from tests.support.interaction_acceptance import (
    FIXTURE_VERSION, OWNER, ReadOnlyGateway, SyntheticBroker, free_port, write_metadata,
)
from tests.test_workspace import PASSWORD, _create_account

PREPARE = "taihua.work_log.create.prepare"
COMMIT = "taihua.work_log.create"
HTTP_ORIGIN = "http://127.0.0.1:1"


class SyntheticTaihuaTransport:
    """Persist synthetic writes so restarting the service cannot erase evidence."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data = json.loads(path.read_text()) if path.exists() else {
            "records": [], "calls": [], "fault": None,
        }

    def arm(self, fault: str | None):
        if fault not in {None, "lost_write_reply", "readback_unavailable", "write_rejected"}:
            raise ValueError("unsupported synthetic HTTP fault")
        with self.lock:
            self.data["fault"] = fault
            write_metadata(self.path, self.data)

    def request(self, method, url, *, body=None, **_kwargs):
        parsed = urlparse(url)
        if f"{parsed.scheme}://{parsed.netloc}" != HTTP_ORIGIN:
            raise AssertionError("Synthetic transport rejects every external origin")
        route = (method, parsed.path)
        allowed = {("GET", "/api/projects"), ("GET", "/api/users/principal"),
                   ("GET", "/api/work-logs/range"), ("POST", "/api/work-logs")}
        if route not in allowed:
            raise AssertionError(f"Synthetic transport rejects route {route!r}")
        with self.lock:
            self.data["calls"].append({"method": method, "path": parsed.path, "body": deepcopy(body)})
            fault = self.data["fault"]
            status = 200
            if route == ("GET", "/api/users/principal"):
                payload = {"id": 1, "username": OWNER, "fullname": OWNER}
            elif route == ("GET", "/api/projects"):
                payload = [{"id": 901, "code": "SYNTHETIC", "name": "合成验收项目"}]
            elif method == "POST":
                if fault == "write_rejected":
                    status, payload = 422, {"message": "合成业务规则拒绝"}
                else:
                    payload = {"id": str(9000 + len(self.data["records"]) + 1), **deepcopy(body)}
                    self.data["records"].append(payload)
                    write_metadata(self.path, self.data)
                    if fault == "lost_write_reply":
                        raise ConnectionError("Synthetic connection lost after accepted write")
            else:
                if fault == "readback_unavailable" and self.data["records"]:
                    write_metadata(self.path, self.data)
                    raise ConnectionError("Synthetic authoritative readback unavailable")
                query = parse_qs(parsed.query)
                start, end = query["startDate"][0], query["endDate"][0]
                payload = [deepcopy(row) for row in self.data["records"] if start <= row["logDate"] <= end]
            write_metadata(self.path, self.data)
            return {"status": status, "url": url, "json": payload, "text": ""}

    @property
    def write_calls(self):
        return sum(call["method"] == "POST" for call in self.data["calls"])


class NetworkFreeHttpWorker:
    def __init__(self, transport):
        self.transport = transport
        self.state = {"cookies": [], "http": {}}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def request(self, *args, **kwargs):
        return self.transport.request(*args, **kwargs)

    def restore_session_state(self, state):
        self.state = deepcopy(state)

    def capture_session_state(self):
        return deepcopy(self.state)

    def get_http_state(self):
        return deepcopy(self.state.get("http", {}))

    def set_http_state(self, state):
        self.state["http"] = deepcopy(state)

    def clear_session_state(self):
        self.state = {"cookies": [], "http": {}}


class BusinessDraftAcceptanceFixture:
    def __init__(self, home: Path, *, seed=True):
        self.home = home.resolve()
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.meta_path = self.home / "acceptance.json"
        if any(self.home.iterdir()) and not self.meta_path.exists():
            raise ValueError("Use an empty or marked isolated acceptance directory")
        if self.meta_path.exists():
            self.meta = json.loads(self.meta_path.read_text())
            if self.meta.get("fixture") != FIXTURE_VERSION:
                raise ValueError("Not an isolated acceptance environment")
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
        self.transport = SyntheticTaihuaTransport(self.home / "synthetic-taihua.json")
        self.service = CentralCapabilityService(home=self.home, base_url=HTTP_ORIGIN,
            taihua_base_url=HTTP_ORIGIN, trusted_card_base_url=self.card_url,
            worker_factory=self._deny_worker)
        self.service._worker_factories_by_system["taihua"] = lambda *_: NetworkFreeHttpWorker(self.transport)
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.coordinator_errors = []
        if "account_id" not in self.meta:
            account = _create_account(self.service, user_subject=OWNER, username="cardreview", endpoint_key="telegram:*:cardreview")
            _create_account(self.service, user_subject="interaction-other", username="cardother", endpoint_key="telegram:*:cardother")
            self.meta["account_id"] = account["account_id"]
        self.account = self.service.workspace.get_account(self.meta["account_id"])
        if not self.meta.get("draft_initialized"):
            existing = self.service.user_grants.get(OWNER) or {}
            grant_permissions(self.service.user_grants, OWNER,
                set(existing.get("permissions", [])) | {"taihua.work_log.create", "oa.workflow.read"})
            self.meta["draft_initialized"] = True
        session = self.service.sessions.find(user_subject=OWNER, system_id="taihua")
        if session is None:
            session = self.service.sessions.get_or_create(user_subject=OWNER, system_id="taihua", expected_principal_ref=OWNER)
            session = self.service.sessions.activate(session["session_id"], observed_principal_ref=OWNER)
            self.service.session_states.save(session["session_id"], {"cookies": [], "http": {
                "authorization": "Bearer synthetic-acceptance-only", "refresh_token": "synthetic-unused"}})
        self.session_id = session["session_id"]
        self.meta.setdefault("draft_cases", [])
        self.app = WorkspaceApplication(service=self.service, gateway=ReadOnlyGateway())
        if seed and not self.meta["draft_cases"]:
            self.add_case()
        write_metadata(self.meta_path, self.meta)

    @staticmethod
    def _deny_worker(*_args, **_kwargs):
        raise RuntimeError("Real downstream network/browser workers are disabled")

    def add_case(self, case_key=None):
        case_key = case_key or uuid4().hex[:8]
        task = self.service.ensure_host_task(user_subject=OWNER, token_id="fixture-token", agent_host="openclaw",
            host_task_key=f"draft-acceptance|{case_key}", endpoint_key=self.account["endpoint_key"], client_type="web",
            external_subject=self.account["account_id"], conversation_ref=self.account["openclaw_session_key"],
            title=f"日志单次确认验收 · {case_key}")["task"]["taskId"]
        self.service.tasks.append_timeline_message(user_subject=OWNER, source_endpoint_id=self.account["endpoint_id"],
            message_key=f"draft:{case_key}", role="assistant", text="填写、预览并明确提交合成工作日志。下游为隔离 HTTP 测试替身。", task_id=task)
        result = self.service.invoke(user_subject=OWNER, capability_name=PREPARE, arguments={},
            task_id=task, idempotency_key=f"draft-source:{case_key}")
        if result["status"] != "requires_user_action" or result["interaction"]["type"] != "business_input":
            raise RuntimeError(f"Actual Taihua prepare did not produce an input: {result}")
        case = {"key": case_key, "task_id": task, "input_id": result["nextAction"]["inputSubmissionId"],
                "input_interaction": result["interaction"]["interactionId"]}
        self.meta["draft_cases"].append(case)
        write_metadata(self.meta_path, self.meta)
        return case

    def resume(self, interaction_id):
        return self.service.resume_interaction(user_subject=OWNER, interaction_id=interaction_id,
            idempotency_key=f"acceptance-original:{interaction_id}")

    def coordinate(self):
        """Follow durable current interactions, using only the original central API."""
        while not self.stop_event.wait(0.3):
            with self.lock:
                for case in self.meta["draft_cases"]:
                    try:
                        task = self.service.tasks.get_task(case["task_id"], user_subject=OWNER)
                        interaction_id = task.get("current_interaction_id")
                        if not interaction_id or task["status"] not in {"active", "waiting_user", "running"}:
                            continue
                        interaction = self.service.get_interaction(user_subject=OWNER, interaction_id=interaction_id)["interaction"]
                        if interaction["resume"]["ready"]:
                            self.resume(interaction_id)
                    except Exception as exc:
                        # Keep an actionable diagnostic without silently replacing
                        # a production failure with a fabricated result.
                        self.coordinator_errors.append(type(exc).__name__ + ": " + str(exc))
                        self.coordinator_errors[:] = self.coordinator_errors[-10:]

    def servers(self):
        fields = TrustedFieldApplication(submission_store=self.service.field_submissions)
        actions = TrustedActionApplication(authorization_store=self.service.write_authorizations)
        flow = TrustedFlowApplication(service=self.service, field_application=fields, action_application=actions)
        cards = create_auth_http_server(config=validate_auth_server_config(host="127.0.0.1", port=self.meta["card_port"],
            public_base_url=self.card_url, tls_cert=None, tls_key=None),
            application=TrustedAuthApplication(challenge_store=self.service.challenges, broker=SyntheticBroker(self.service.challenges)),
            field_application=fields, action_application=actions, flow_application=flow)
        workspace = create_workspace_http_server(config=validate_workspace_server_config(host="127.0.0.1", port=self.meta["workspace_port"],
            public_base_url=self.workspace_url, tls_cert=None, tls_key=None), application=self.app)
        return cards, workspace

    def public_metadata(self):
        case = self.meta["draft_cases"][-1]
        return {"fixture": "actual-draft-synthetic-http-v1", "workspace_url": self.workspace_url,
            "card_origin": self.card_url, "username": "cardreview", "other_username": "cardother",
            "synthetic_password": PASSWORD, "task_id": case["task_id"], "input_id": case["input_id"],
            "flow_url": f"{self.card_url}/flow/input/{case['input_id']}", "http_write_count": self.transport.write_calls,
            "evidence_boundary": "Real central/prepare/commit/draft/UI; synthetic downstream HTTP only"}

    def close(self):
        self.stop_event.set()
        self.app.close()
        if os.environ.get("AGENTBRIDGE_SESSION_KEY_FILE") == str(self.home / "session.key"):
            if self.previous_key is None:
                os.environ.pop("AGENTBRIDGE_SESSION_KEY_FILE", None)
            else:
                os.environ["AGENTBRIDGE_SESSION_KEY_FILE"] = self.previous_key
