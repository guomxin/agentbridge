"""Three-host Workspace UI over the candidate central service and synthetic read-only peers.

This fixture never starts a Telegram consumer or a real host/model. Its L3 profiles
are explicitly synthetic; SDK conformance and real Telegram acceptance are separate.
All credentials, databases and sockets belong to one fresh temporary home.
"""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from agentbridge.core import quick_queries
from agentbridge.core import workspace_query_delivery as delivery
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.authorization_fixtures import grant_permissions
from tests.support.interaction_acceptance import free_port
from tests.support.nanobot_mcp_fixture import NanoBotMcpFixture, deny_outgoing_network, private_file
from tests.support.nanobot_telegram_setup import INSTANCE, seed_telegram_peers
from tests.support.workspace_host_acceptance import add_fixture_signin
from tests.test_workspace import FakeGateway, PASSWORD

ROOT = Path(__file__).resolve().parents[2]
INSTANCES = {"openclaw": "openclaw-default", "nanobot": INSTANCE, "pi": "pi-office"}


class SyntheticHost(FakeGateway):
    def __init__(self, fixture, host):
        super().__init__()
        self.fixture, self.host_type = fixture, host
        self.instance_id = INSTANCES[host]
        self.outcomes = {}
        self.capabilities = ["chat"]

    def readiness(self):
        return {"available": True, "recoverable": False, "acceptedLevel": "L3",
                "adapterVersion": delivery.VERSIONS[self.host_type], "capabilities": list(self.capabilities)}

    def diagnostics(self):
        return {"configured": True, "synthetic": True}

    def call(self, method, params=None, **options):
        if method == "chat.history":
            return {"messages": [], "sessionId": "synthetic"}
        if method == "agentbridge.task.wake":
            task = self.fixture.service.tasks.get_task(params["taskId"], user_subject=self.fixture.owner)
            self.prepare_command({"user_subject": self.fixture.owner, "endpoint_id": task["origin_endpoint_id"]},
                                 params["taskId"], params["commandKey"])
            return {"accepted": True}
        return super().call(method, params, **options)

    def prepare_command(self, binding, task_id, command_key):
        subject = binding["user_subject"]
        registered = self.fixture.registrations[self.host_type, subject]
        lease = self.fixture.service.acquire_host_coordinator_lease(
            user_subject=subject, task_id=task_id, registration=registered)
        self.fixture.service.assert_host_coordinator_lease(user_subject=subject, task_id=task_id,
            registration=registered, expected_version=lease["version"])
        quick_queries.execute_query(self.fixture.service, subject, task_id, command_key,
            runtime_context={"endpointId": binding["endpoint_id"], "hostType": self.host_type,
                "hostInstanceId": self.instance_id, "hostRunId": task_id, "requestId": command_key})

    def send_stream(self, *, session_key, endpoint_key, message, idempotency_key, **options):
        with self.fixture.service.workspace._connect() as db:
            row = db.execute("SELECT * FROM workspace_accounts WHERE endpoint_key=? AND state='active'",
                             (endpoint_key,)).fetchone()
        if row is None:
            raise PermissionError("Unknown synthetic Workspace endpoint")
        binding = {"user_subject": row["user_subject"], "endpoint_id": row["endpoint_id"], "session_key": session_key}
        return self.send_dispatch({"idempotency_key": idempotency_key}, binding, message, [], None)

    def send_dispatch(self, dispatch, binding, message, attachments, trial):
        if attachments or trial:
            raise PermissionError("This fixture accepts text-only read requests")
        run = "fixture-" + uuid4().hex
        yield {"type": "accepted", "runId": run, "status": "started"}
        service, subject = self.fixture.service, binding["user_subject"]
        task, _ = service.tasks.ensure_task(user_subject=subject, agent_host=self.host_type, host_task_key=run,
            origin_endpoint_id=binding["endpoint_id"], active_conversation_ref=binding["session_key"], title="合成 OA 待办查询")
        response = service.invoke(user_subject=subject, capability_name="oa.workflow.pending.list", arguments={},
            idempotency_key="fixture-query:" + run, task_id=task["task_id"], request_id=run,
            host_type=self.host_type, host_instance_id=self.instance_id, host_run_id=run,
            origin_endpoint_id=binding["endpoint_id"])
        success = response.get("status") == "succeeded"
        service.finish_host_task(user_subject=subject, task_id=task["task_id"], outcome="succeeded" if success else "failed",
                                 reason="synthetic_workspace_query_acceptance")
        text = "本次合成验收查到 2 项 OA 待办：考勤确认、周报。" if success else "合成账号未获查询权限。"
        self.outcomes[run] = text
        yield {"type": "chat", "runId": run, "state": "final", "text": text}

    def dispatch_evidence(self, dispatch, binding, run):
        return {"state": "final", "text": self.outcomes[run], "runId": run} if run in self.outcomes else {"state": "unknown"}


class Fixture:
    def __init__(self, home, *, deliveries=True, channel_identities=None, trusted_card_origin="https://synthetic.invalid"):
        self.home, self.stack = Path(home), ExitStack()
        self.home.mkdir(mode=0o700, exist_ok=True)
        self.stop, self.lock = threading.Event(), threading.Lock()
        self.receipts, self.errors, self.registrations = [], [], {}
        self.trusted_card_origin = trusted_card_origin
        self.channel_identities = {"nanobot": ("8001", "501"), "pi": ("8801", "501"), **(channel_identities or {})}
        from agentbridge.workspace.telegram_pilot_links import numeric_id
        self.channel_identities = {host: tuple(numeric_id(v) for v in pair) for host, pair in self.channel_identities.items()}
        if set(self.channel_identities) != {"nanobot", "pi"} or any(len(pair) != 2 for pair in self.channel_identities.values()):
            raise ValueError("Expected exact private channel identities")
        self.central = NanoBotMcpFixture(self.home / "central", "http://127.0.0.1:1", 1,
                                         trusted_card_origin=trusted_card_origin)
        self.stack.callback(self.central.close)
        self.service, self.owner = self.central.service, self.central.subjects["a"]
        seed_telegram_peers(self.central)
        with self.service.workspace._connect() as db:
            db.execute("UPDATE client_endpoints SET capabilities_json=? WHERE agent_host='openclaw' AND client_type='telegram'",
                       (json.dumps(["timeline_message", "direct_status", "trusted_interaction", "continuous_flow_v1"]),))
            sender = self.channel_identities["nanobot"][1]
            db.execute("""UPDATE client_endpoints SET external_subject=?,conversation_ref=?,route_json=?
                WHERE agent_host='openclaw' AND endpoint_key='test-old-private-501' AND user_subject=?""",
                (sender, "agent:main:telegram:direct:" + sender,
                 json.dumps({"channel": "telegram", "to": sender, "accountId": "default"}), self.owner))
            self.accounts = {row["user_subject"]: self.service.workspace.get_account(row["account_id"])
                             for row in db.execute("SELECT account_id,user_subject FROM workspace_accounts")}
        self.tokens = {(host, subject): self.central.tokens[label]
                       for host in INSTANCES for label, subject in self.central.subjects.items()}
        self.token_files = {(host, subject): self.central.meta["tokenFiles"][label]
                            for host in INSTANCES for label, subject in self.central.subjects.items()}
        self._configure()
        self.hosts = {host: SyntheticHost(self, host) for host in INSTANCES}
        self.app = WorkspaceApplication(service=self.service, gateway=self.hosts["openclaw"],
            host_routes={INSTANCES[host]: self.hosts[host] for host in ("nanobot", "pi")})
        self.stack.callback(self.app.close)
        self.signin = {}
        for label, domain in (("a", "127.0.0.1"), ("b", "localhost")):
            port = free_port()
            origin = f"http://{domain}:{port}"
            server = create_workspace_http_server(config=validate_workspace_server_config(
                host="127.0.0.1", port=port, public_base_url=origin, tls_cert=None, tls_key=None), application=self.app)
            account = self.accounts[self.central.subjects[label]]
            add_fixture_signin(server, origin, {account["username"]: PASSWORD})
            self._receipt_route(server, origin)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.stack.callback(server.server_close)
            self.stack.callback(server.shutdown)
            self.signin[label] = origin + "/fixture-signin"
        self.thread = None
        if deliveries:
            self.thread = threading.Thread(target=self._deliver, name="synthetic-query-receipts", daemon=True)
            self.thread.start()

    def _configure(self):
        service, directory = self.service, self.service.workspace.hosts
        refs = {subject: "pi-fixture-" + label for label, subject in self.central.subjects.items()}
        private_file(self.home / "route.token", b"synthetic-loopback-route-only")
        for host, (bot, sender) in self.channel_identities.items():
            instance = INSTANCES[host]
            subjects = {s: "tg-fixture-web-cred-" + label for label, s in self.central.subjects.items()} if host == "nanobot" else refs
            spec = {"instanceId": instance, "adapterVersion": delivery.VERSIONS[host], "requiredLevel": "L3",
                    "url": "http://127.0.0.1:1", "tokenFile": str(self.home / "route.token"), "subjects": subjects,
                    "telegram": {"botId": bot, "username": "synthetic_" + host + "_bot"}}
            if host == "pi":
                spec["telegram"]["allowedSenders"] = {self.owner: sender}
            path = self.home / (host + "-routes.json")
            private_file(path, json.dumps({"routes": {instance: spec}}).encode())
            os.environ["AGENTBRIDGE_" + host.upper() + "_ROUTES_FILE"] = str(path)
            directory.save_instance(instance, host_type=host, display_name=host.title(), route_ref=instance,
                adapter_version=delivery.VERSIONS[host], state="enabled", subjects=list(subjects),
                expected_revision=1 if host == "nanobot" else 0, actor="fixture", reason="isolated query UI acceptance")
        for label, subject in self.central.subjects.items():
            account = self.accounts[subject]
            directory.choices(account["account_id"])
            minted = self.central.identities.issue(user_subject=subject, expected_principal_ref=subject,
                ttl_seconds=7200, label="workspace-pi:pi-office:" + refs[subject])
            self.tokens["pi", subject] = minted["token_id"]
            token_path = self.home / ("pi-" + label + ".token")
            private_file(token_path, minted["token"].encode())
            self.token_files["pi", subject] = str(token_path)
            self.stack.callback(token_path.unlink, missing_ok=True)
            self.stack.callback(self.central.identities.revoke, minted["token_id"])
            endpoint, _ = service.tasks.ensure_endpoint(user_subject=subject, token_id=minted["token_id"], agent_host="pi",
                endpoint_key="pi-web:" + label, client_type="web", external_subject=account["account_id"],
                account_id=account["account_id"], conversation_ref="pi-fixture-web:" + label)
            directory.bind(account["account_id"], INSTANCES["pi"], endpoint_id=endpoint["endpoint_id"], credential_ref=refs[subject])
        profile = json.loads((ROOT / "schemas/agent-host/v1/test-vectors.json").read_text())["profiles"][0]["value"]
        for host, instance in INSTANCES.items():
            # Exactly as existing SDK fixtures: isolated conformance is evidence
            # for this synthetic peer, never a production release qualification.
            with service.tasks._connect() as db:
                db.execute("INSERT OR REPLACE INTO agent_host_compatibility VALUES(?,?,'L3','synthetic-ui-peer','fixture',CURRENT_TIMESTAMP)",
                           (host, delivery.VERSIONS[host]))
            for subject in self.accounts:
                host_profile = {**profile, "hostInstanceId": instance,
                    "implementation": {"name": host, "version": delivery.VERSIONS[host]}}
                service.negotiate_host(user_subject=subject, token_id=self.tokens[host, subject], profile=host_profile)
                self.registrations[host, subject] = service.require_host_registration(user_subject=subject,
                    agent_host=host, host_instance_id=instance, host_version=delivery.VERSIONS[host], minimum_level="L3")
        grant_permissions(service.user_grants, self.owner, ["oa.workflow.read", "oa.attendance_confirmation.approve", "oa.weekly_report.approve"])
        service.skills.save("user:" + self.owner, {"oa-pending": {"profiles": ["single", "batch"]}},
                            expected_revision=0, actor="fixture", reason="synthetic selectable read results")
        links = TelegramPilotLinks(service)
        bot, sender = self.channel_identities["pi"]
        challenge = links.begin(self.accounts[self.owner]["account_id"], INSTANCES["pi"], bot)
        proof = links.present(self.owner, INSTANCES["pi"], bot, sender, challenge["code"])
        links.confirm(self.accounts[self.owner]["account_id"], INSTANCES["pi"], bot, challenge["challenge_id"], proof)
        for host, (bot, sender) in self.channel_identities.items():
            links.ensure_endpoint(self.owner, self.tokens[host, self.owner], INSTANCES[host], bot, sender)

        def synthetic_read(capability, worker, arguments):
            if capability != "oa.workflow.pending.list" or worker.session["user_subject"] != self.owner:
                raise PermissionError("Fixture only permits its owner's synthetic pending read")
            return {"items": [
                {"affair_id": "synthetic-attendance", "title": "【HR】月度考勤确认单-合成人员", "category": "协同", "sender": "合成人员"},
                {"affair_id": "synthetic-weekly", "title": "【行政】周报-合成人员", "category": "协同", "sender": "合成人员"}],
                "coverage": {"status": "complete", "hasMore": False}}
        service.adapter.invoke_capability = synthetic_read

    def _deliver(self):
        while not self.stop.wait(.1):
            endpoints = {"openclaw": "test-old-private-501", **{host: "telegram:" + ":".join(pair) for host, pair in self.channel_identities.items()}}
            for host, endpoint in endpoints.items():
                try:
                    events = self.service.claim_host_notifications(user_subject=self.owner, agent_host=host,
                        host_instance_id=INSTANCES[host], endpoint_key=endpoint,
                        query_token_id=self.tokens[host, self.owner])["notifications"]
                    for event in events:
                        mode = event.get("deliveryMode")
                        if mode not in {delivery.MODE, "workspace_text_v2"}:
                            continue
                        scope = dict(subject=self.owner, instance=INSTANCES[host], host=host, endpoint_key=endpoint,
                            token_id=self.tokens[host, self.owner], pairing_ref=event["pairingRef"],
                            delivery_id=event["deliveryId"], attempt_count=event["attemptCount"])
                        wrong = delivery.handle(self.service, action="issue", **{**scope, "pairing_ref": "0" * 64})
                        issued = delivery.handle(self.service, action="issue", **scope)
                        allowed = delivery.handle(self.service, action="begin", **scope)
                        repeated = delivery.handle(self.service, action="begin", **scope)
                        if wrong != {"available": False} or not issued.get("available") or allowed != {"allowed": True} or repeated != {"allowed": False}:
                            raise AssertionError("Synthetic delivery authorization boundary failed")
                        recorded = delivery.handle(self.service, action="ack", outcome="sent", receipt=len(self.receipts) + 1, **scope)
                        with self.lock:
                            self.receipts.append({"host": host, "mode": mode,
                                **({"taskId": event["sourceTaskId"]} if mode == delivery.MODE else {
                                    "sourceEntryId": event["sourceEntryId"], "partIndex": event["partIndex"], "partCount": event["partCount"]}),
                                "attempt": event["attemptCount"],
                                "card": "resultView" in issued, "publicText": "publicText" in issued,
                                "wrongPairDenied": True, "duplicateBeginDenied": True, "acknowledged": recorded.get("recorded") is True})
                except Exception as exc:
                    with self.lock:
                        label = host + ":" + type(exc).__name__
                        if label not in self.errors:
                            self.errors.append(label)

    def report(self):
        with self.service.tasks._connect() as db:
            tasks = [dict(row) for row in db.execute("""SELECT agent_host AS host,status,
                json_extract(summary_json,'$.command.kind') AS command,COUNT(*) AS count FROM agent_tasks
                WHERE user_subject=? GROUP BY 1,2,3""", (self.owner,))]
        with self.lock:
            return {"boundary": "synthetic peers/OA/Telegram; candidate central and Workspace", "versions": delivery.VERSIONS,
                "l3Evidence": "isolated synthetic peer; not SDK conformance", "realOaCalls": 0, "realTelegramCalls": 0,
                "tasks": tasks, "receipts": list(self.receipts), "errors": list(self.errors)}

    def _receipt_route(self, server, origin):
        base, fixture = server.RequestHandlerClass, self
        class Receipt(base):
            def do_GET(self):
                if self.path != "/fixture-receipt":
                    return super().do_GET()
                if self.headers.get("Host") != urlsplit(origin).netloc:
                    return self.send_error(403)
                account = self._authenticate()
                if account and account["user_subject"] == fixture.owner:
                    return self._json(200, fixture.report())
                if account:
                    return self.send_error(403)
        server.RequestHandlerClass = Receipt

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(5)
        self.stack.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="New empty private fixture directory")
    args = parser.parse_args()
    os.umask(0o077)
    home = args.home or Path(tempfile.mkdtemp(prefix="workspace-query-w-"))
    if args.home and args.home.exists():
        parser.error("--home must not already exist")
    sys.addaudithook(deny_outgoing_network)
    fixture = Fixture(home)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    public = {"home": str(home), "signin": fixture.signin,
        "receipt": fixture.signin["a"].replace("/fixture-signin", "/fixture-receipt"),
        "ordinaryPrompt": "请列出本次合成待办并说明数量", "quickPrompt": "查询OA待办", **fixture.report()}
    private_file(home / "public.json", json.dumps(public, ensure_ascii=False).encode())
    print(json.dumps(public, ensure_ascii=False), flush=True)
    try:
        stopped.wait()
    finally:
        fixture.close()


if __name__ == "__main__":
    main()
