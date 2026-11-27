"""Two Pi Workspace accounts and one independently authorized Telegram private pairing."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentbridge.admin.stores import AdminAuditStore
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.gateway import GatewayRequestError
from agentbridge.workspace.host_instances import HostInstanceConflict, LEGACY_INSTANCE_ID
from agentbridge.workspace.host_provision import WorkspaceHostProvision
from agentbridge.workspace.nanobot_gateway import PiGatewayClient, configured_adapters
from agentbridge.workspace.stores import WorkspaceConflictError
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.test_workspace import FakeGateway, _create_account, _service


def private_json(path, data):
    path.write_text(json.dumps(data))
    path.chmod(0o600)


@pytest.fixture
def pi(tmp_path, monkeypatch, request):
    monkeypatch.delenv("AGENTBRIDGE_NANOBOT_ROUTES_FILE", raising=False)
    session_key = tmp_path / "workspace.key"
    session_key.write_bytes(os.urandom(32))
    session_key.chmod(0o600)
    monkeypatch.setenv("AGENTBRIDGE_SESSION_KEY_FILE", str(session_key))
    token = tmp_path / "pi-service"
    token.write_text("k" * 40)
    token.chmod(0o600)
    route_file = tmp_path / "routes.json"
    monkeypatch.setenv("AGENTBRIDGE_PI_ROUTES_FILE", str(route_file))
    spec = {
        "instanceId": "pi-office",
        "adapterVersion": "0.1.7",
        "tokenFile": str(token),
        "url": "http://127.0.0.1:1",
        "subjects": {"user-a": "pi-a", "user-b": "pi-b"},
        "telegram": {"botId": "8801", "username": "pi_test_bot", "allowedSenders": {"user-a": "501"}},
    }
    if hasattr(request, "param"):
        wire = request.getfixturevalue("pi_wire")
        wire.answers["adapterVersion"] = request.param
        spec.update(url=wire.url, adapterVersion=request.param)
    manifest = {"routes": {"pi-route": spec}}
    private_json(route_file, manifest)
    service = _service(str(tmp_path))
    accounts = {
        name: _create_account(
            service,
            user_subject="user-" + name,
            username="pilot" + name,
            endpoint_key="web:test:" + name,
            client_type="web",
        )
        for name in ("a", "b")
    }
    provision = WorkspaceHostProvision(service, AdminAuditStore(service.db_path), FakeGateway())
    for account in accounts.values():
        assert provision.directory.choices(account["account_id"])["default_instance_id"] == LEGACY_INSTANCE_ID
    admin = {"username": "fixture", "role": "admin"}
    config = dict(
        instance_id="pi-office",
        route_ref="pi-route",
        display_name="Pi",
        state="enabled",
        subjects=["user-a", "user-b"],
        expected_revision=0,
        reason="fixture",
    )
    provision.save(admin, "", config)
    for account in accounts.values():
        provision.bind(
            admin,
            "",
            dict(
                instance_id="pi-office",
                account_id=account["account_id"],
                expected_revision=1,
                reason="fixture",
            ),
        )
    app = WorkspaceApplication(
        service=service,
        gateway=FakeGateway(),
        host_routes={alias: client for alias, _, client in configured_adapters()},
    )
    try:
        yield SimpleNamespace(
            service=service,
            accounts=accounts,
            provision=provision,
            app=app,
            manifest=manifest,
            spec=spec,
            admin=admin,
            config=config,
            route_file=route_file,
        )
    finally:
        app.close()


def test_two_web_bindings_pin_request_and_drain_original(pi):
    app, a, b = pi.app, pi.accounts["a"], pi.accounts["b"]
    assert all(
        app.hosts.choices(person["account_id"])["default_instance_id"] == LEGACY_INSTANCE_ID
        for person in (a, b)
    )
    choices = app.hosts.set_default(a["account_id"], "pi-office", expected_revision=1)
    assert app.hosts.choices(b["account_id"])["default_instance_id"] == LEGACY_INSTANCE_ID
    with patch.object(app, "_ensure_dispatch_worker"):
        app.send_chat_stream(
            a,
            message="查我的状态",
            idempotency_key="saved",
            instance_id="pi-office",
            expected_revision=choices["revision"],
        )
        dispatch = app.store.list_host_dispatches(user_subject=a["user_subject"], instance_id="pi-office")[0]
        original = app.store.dispatch_binding(dispatch)
        assert (original["host_type"], original["credential_ref"]) == ("pi", "pi-a")
        app.hosts.set_default(a["account_id"], LEGACY_INSTANCE_ID, expected_revision=choices["revision"])
        app.send_chat_stream(
            a,
            message="查我的状态",
            idempotency_key="saved",
            instance_id="pi-office",
            expected_revision=choices["revision"],
        )
        with pytest.raises(WorkspaceConflictError):
            app.send_chat_stream(a, message="不同载荷", idempotency_key="saved")
        with pytest.raises(HostInstanceConflict):
            app.send_chat_stream(
                a,
                message="新请求",
                idempotency_key="fresh",
                instance_id="pi-office",
                expected_revision=choices["revision"],
            )
    assert not app.history_page(b)["timeline"]["items"]
    pi.provision.save(pi.admin, "", {**pi.config, "state": "draining", "expected_revision": 1})
    assert app.store.dispatch_binding(dispatch) == original
    with pytest.raises(PermissionError):
        app.hosts.set_default(a["account_id"], "pi-office", expected_revision=3)
    pi.provision.save(pi.admin, "", {**pi.config, "state": "revoked", "expected_revision": 2})
    with pytest.raises(PermissionError):
        app.store.dispatch_binding(dispatch)


def test_pairing_requires_current_first_account_allowlist_at_every_stage(pi):
    app, a, b = pi.app, pi.accounts["a"], pi.accounts["b"]
    links = TelegramPilotLinks(pi.service)
    a_args, b_args = (a["account_id"], "pi-office", "8801"), (b["account_id"], "pi-office", "8801")
    assert set(app.telegram_hosts(a)) == {"pi-office"} and app.telegram_hosts(b) == {}
    with pytest.raises(PermissionError):
        links.begin(*b_args)
    with pytest.raises((PermissionError, ValueError)):
        app.telegram_link_action(b, "begin", {"instance_id": "pi-office"})
    with pytest.raises(PermissionError):
        links.begin(a["account_id"], "pi-office", "8802")
    challenge = links.begin(*a_args)
    assert not links.present("user-b", "pi-office", "8801", "501", challenge["code"])
    assert not links.present("user-a", "pi-office", "8801", "502", challenge["code"])
    proof = links.present("user-a", "pi-office", "8801", "501", challenge["code"])
    assert proof
    del pi.spec["telegram"]["allowedSenders"]
    private_json(pi.route_file, pi.manifest)
    with pytest.raises(PermissionError):
        links.confirm(*a_args, challenge["challenge_id"], proof)
    pi.spec["telegram"]["allowedSenders"] = {"user-a": "501"}
    private_json(pi.route_file, pi.manifest)
    assert links.confirm(*a_args, challenge["challenge_id"], proof)["state"] == "linked"
    assert links.verified("user-a", "pi-office", "8801", "501")
    assert not links.verified("user-b", "pi-office", "8801", "501")
    identities = McpIdentityTokenStore(pi.service.db_path)
    token_ids = {
        subject: identities.issue(
            user_subject=subject, expected_principal_ref=subject, label="pairing-test", ttl_seconds=600
        )["token_id"]
        for subject in ("user-a", "user-b")
    }
    with pytest.raises(PermissionError):
        links.ensure_endpoint("user-a", "unrecognized", "pi-office", "8801", "501")
    endpoint = links.ensure_endpoint("user-a", token_ids["user-a"], "pi-office", "8801", "501")
    assert len(endpoint["pairingRef"]) == 64 and int(endpoint["pairingRef"], 16) > 0
    assert pi.service.tasks.get_endpoint(endpoint["endpointId"], user_subject="user-a")["agent_host"] == "pi"
    with pytest.raises(PermissionError):
        links.ensure_endpoint("user-b", token_ids["user-b"], "pi-office", "8801", "501")
    assert links.revoke(*a_args)["state"] == "unlinked"
    assert not links.verified("user-a", "pi-office", "8801", "501")
    with pytest.raises(PermissionError):
        links.ensure_endpoint("user-a", token_ids["user-a"], "pi-office", "8801", "501")
    challenge = links.begin(*a_args)
    proof = links.present("user-a", "pi-office", "8801", "501", challenge["code"])
    links.confirm(*a_args, challenge["challenge_id"], proof)
    renewed = links.ensure_endpoint("user-a", token_ids["user-a"], "pi-office", "8801", "501")
    assert renewed["pairingRef"] != endpoint["pairingRef"]
    assert renewed["endpointId"] == endpoint["endpointId"]


def test_stop_generation_follows_accepted_original_run(pi):
    app, account, other = pi.app, pi.accounts["a"], pi.accounts["b"]
    choices = app.hosts.set_default(account["account_id"], "pi-office", expected_revision=1)
    with patch.object(app, "_ensure_dispatch_worker"):
        app.send_chat_stream(
            account,
            message="独立的原运行",
            idempotency_key="original",
            instance_id="pi-office",
            expected_revision=choices["revision"],
        )
    dispatch = app.store.list_host_dispatches(user_subject="user-a", instance_id="pi-office")[0]
    dispatch_id = dispatch["dispatch_id"]
    with pytest.raises(WorkspaceConflictError):
        app.stop_chat_generation(account, dispatch_id)
    original = app.store.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="test-worker")
    accepted = app.store.mark_host_dispatch_accepted(
        dispatch_id, claim_token=original["claim_token"], run_id="pinned"
    )
    app.hosts.set_default(account["account_id"], LEGACY_INSTANCE_ID, expected_revision=choices["revision"])
    bound = app.store.dispatch_binding(accepted)
    client = app.host_routes[bound["route_ref"]]
    with patch.object(client, "_request", return_value={"runId": "wrong", "state": "stopped"}):
        with pytest.raises(GatewayRequestError):
            app.stop_chat_generation(account, dispatch_id)
    with patch.object(client, "_request", return_value={"runId": "pinned", "state": "stopped"}) as transport:
        with pytest.raises(KeyError):
            app.stop_chat_generation(other, dispatch_id)
        transport.assert_not_called()
        assert app.stop_chat_generation(account, dispatch_id) == {
            "dispatchId": dispatch_id,
            "runState": "stopped",
        }
        transport.assert_called_once_with(
            "cancel", {"binding": client._binding(bound), "requestKey": "original", "runId": "pinned"}
        )
        app._observe_accepted_dispatch(accepted)
        assert [call.args[0] for call in transport.call_args_list] == ["cancel", "lookup"]
    finished = app.store.get_host_dispatch(dispatch_id, user_subject="user-a")
    assert (finished["state"], finished["accepted_run_id"]) == ("stopped", "pinned")
    with pytest.raises(WorkspaceConflictError):
        app.stop_chat_generation(account, dispatch_id)


@pytest.fixture
def pi_wire():
    answers = {
        "available": True,
        "adapterVersion": "0.1.7",
        "capabilities": ["chat"],
        "hostType": "pi",
        "sdkVersion": "1.6.0",
        "acceptedLevel": "L1",
        "modelConfiguration": "configured",
        "modelInvocation": "unverified",
        "centralConnection": "verified",
        "configuredBindings": 2,
        "verifiedBindings": 2,
    }
    requests = []
    switch_on_accept = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["Authorization"] == "Bearer " + "k" * 40
            requests.append((self.path, body))
            run_id = "run-" + body.get("requestKey", "")
            if self.path == "/v1/accept":
                if body["requestKey"] in switch_on_accept:
                    answers["adapterVersion"] = switch_on_accept.pop(body["requestKey"])
                reply = {"runId": run_id, "state": "accepted"}
            elif self.path == "/v1/events":
                reply = {
                    "answer": "原答复",
                    "events": [
                        {"sequence": 1, "kind": "tool_started", "arguments": "private-marker"},
                        {"sequence": 2, "kind": "tool_finished", "result": "private-marker"},
                        {"sequence": 3, "kind": "delta", "text": "原"},
                        {"sequence": 4, "kind": "completed"},
                    ],
                }
            elif self.path == "/v1/lookup":
                reply = {"runId": run_id, "state": "completed", "answer": "原答复"}
            else:
                reply = answers
            data = json.dumps(
                {
                    "instance": "pi-office",
                    "hostType": "pi",
                    "adapterVersion": answers["adapterVersion"],
                    **reply,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", answers=answers, requests=requests,
                              switch_on_accept=switch_on_accept)
    finally:
        server.shutdown()
        server.server_close()


def test_pi_authenticated_transport_progress_and_lookup(pi, pi_wire):
    pi.spec["url"] = pi_wire.url
    answers, requests = pi_wire.answers, pi_wire.requests
    private_json(pi.route_file, pi.manifest)
    client = PiGatewayClient.from_environment()["pi-route"]
    app, account = pi.app, pi.accounts["a"]
    probe = pi.provision.probe({"instance_id": "pi-office"})
    assert (probe["configuredBindings"], probe["verifiedBindings"]) == (2, 2)
    answers.update(verifiedBindings=1, centralConnection="partial")
    degraded = client.readiness()
    assert degraded["available"] and degraded["verifiedBindings"] == 1 and degraded["centralConnection"] == "partial"
    answers.update(verifiedBindings=True)
    assert "verifiedBindings" not in client.readiness()
    answers.update(available=False, verifiedBindings=0, centralConnection="unavailable")
    offline = client.readiness()
    assert not offline["available"] and offline["verifiedBindings"] == 0 and offline["centralConnection"] == "unavailable"
    answers.update(available=True, verifiedBindings=2, centralConnection="verified")
    assert (probe["capabilities"], probe["modelInvocation"], probe["centralConnection"]) == (
        ["chat"],
        "unverified",
        "verified",
    )
    route = next(item for item in pi.provision.catalog()["routes"] if item["host_type"] == "pi")
    assert route["telegram_subjects"] == ["user-a"] and "501" not in str(route)
    app.host_routes["pi-route"] = client
    selected = app.hosts.set_default(account["account_id"], "pi-office", expected_revision=1)
    events = list(
        app.send_chat_stream(
            account,
            message="原问题",
            idempotency_key="original",
            instance_id="pi-office",
            expected_revision=selected["revision"],
        )
    )
    assert "private-marker" not in str(events)
    assert any(e.get("state") == "final" and e.get("text") == "原答复" for e in events)
    assert [e["phase"] for e in events if e.get("kind") == "tool"] == ["start", "result"]
    dispatch = app.store.list_host_dispatches(user_subject="user-a", instance_id="pi-office")[0]
    assert app._run_evidence(dispatch, dispatch["accepted_run_id"])["final_text"] == "原答复"
    assert sum(path == "/v1/accept" for path, _ in requests) == 1
    answers["adapterVersion"] = "other"
    assert client.readiness()["code"] == "PI_ADAPTER_VERSION_MISMATCH"
    with pytest.raises(GatewayRequestError, match="版本"):
        app._run_evidence(dispatch, dispatch["accepted_run_id"])
    answers["hostType"] = "nanobot"
    assert not client.readiness()["available"]


@pytest.mark.parametrize("pi", ["0.1.3"], indirect=True)
def test_live_directory_and_manifest_switch_both_ways_on_original_route(pi, pi_wire):
    app = pi.app
    for person in pi.accounts.values():
        app.hosts.set_default(person["account_id"], "pi-office", expected_revision=1)

    def send(account, key):
        return list(app.send_chat_stream(pi.accounts[account], message="原查询", idempotency_key=key))

    def stage(version):
        pi.spec["adapterVersion"] = version
        replacement = pi.route_file.with_suffix(".next")
        private_json(replacement, pi.manifest)
        replacement.replace(pi.route_file)
        pi_wire.answers["adapterVersion"] = version

    def approve(revision):
        pi.provision.save(pi.admin, "", {**pi.config, "expected_revision": revision})

    assert any(e.get("text") == "原答复" for e in send("a", "before"))
    dispatch = app.store.list_host_dispatches(user_subject="user-a", instance_id="pi-office")[0]
    for revision, version, key in [(1, "0.1.7", "upgraded"), (2, "0.1.3", "restored")]:
        stage(version)
        count = len(pi_wire.requests)
        assert any(e.get("state") == "error" for e in send("b", "unapproved-" + key))
        assert len(pi_wire.requests) == count
        with pytest.raises(GatewayRequestError):
            app._run_evidence(dispatch, dispatch["accepted_run_id"])
        approve(revision)
        assert app.gateway_status(pi.accounts["b"])["adapterVersion"] == version
        assert any(e.get("text") == "原答复" for e in send("b", key))
        assert app._run_evidence(dispatch, dispatch["accepted_run_id"])["final_text"] == "原答复"
        assert pi_wire.requests[-1][0] == "/v1/lookup"
        assert pi_wire.requests[-1][1]["requestKey"] == "before"
        assert any(e.get("state") == "error" for e in send("b", "unapproved-" + key))
        assert any(e.get("text") == "原答复" for e in send("a", "before"))
    accepted = [(body["requestKey"], body["binding"]["user_subject"], body["binding"]["credential_ref"])
                for path, body in pi_wire.requests if path == "/v1/accept"]
    assert accepted == [("before", "user-a", "pi-a"), ("upgraded", "user-b", "pi-b"), ("restored", "user-b", "pi-b")]


@pytest.mark.parametrize("pi", ["0.1.3"], indirect=True)
def test_version_switch_during_accept_checks_original_without_replaying(pi, pi_wire):
    app, account = pi.app, pi.accounts["a"]
    app.hosts.set_default(account["account_id"], "pi-office", expected_revision=1)
    pi_wire.switch_on_accept["uncertain"] = "0.1.7"

    def same_request():
        return list(app.send_chat_stream(account, message="查询", idempotency_key="uncertain"))

    assert any(e.get("safeToRetry") is False for e in same_request())
    dispatch = app.store.list_host_dispatches(user_subject="user-a", instance_id="pi-office")[0]
    assert dispatch["state"] == "acceptance_unknown"
    pi.spec["adapterVersion"] = "0.1.7"
    private_json(pi.route_file, pi.manifest)
    pi.provision.save(pi.admin, "", {**pi.config, "expected_revision": 1})
    assert app.gateway_status(account)["available"]
    assert any(e.get("safeToRetry") is False for e in same_request())
    assert [path for path, body in pi_wire.requests if body.get("requestKey") == "uncertain"] == [
        "/v1/accept", "/v1/lookup"]


def test_deployed_pi_refuses_malformed_manifest_or_changed_authority(pi, tmp_path):
    app = pi.app
    account = pi.accounts["b"]
    app.hosts.set_default(account["account_id"], "pi-office", expected_revision=1)
    binding = app.store.view_host(account)
    assert app._bound_client(binding) is app.host_routes["pi-route"]
    with pytest.raises(GatewayRequestError):
        app._bound_client({**binding, "user_subject": "user-a", "credential_ref": "pi-a"})
    cases = [
        {"routes": {"renamed": pi.spec}},
        {"routes": {"pi-route": pi.spec, "duplicate": pi.spec}},
        {"routes": {"pi-route": {**pi.spec, "instanceId": "other-pi"}}},
        {"routes": {"pi-route": {**pi.spec, "url": "http://127.0.0.1:3"}}},
        {"routes": {"pi-route": {**pi.spec, "url": 34}}},
        {"routes": {"pi-route": {**pi.spec, "tokenFile": str(tmp_path / "other-token")}}},
        {"routes": {"pi-route": {**pi.spec, "subjects": {"user-a": "pi-a"}}}},
        {"routes": {"pi-route": {**pi.spec, "subjects": {"user-b": "pi-a", "user-a": "pi-b"}}}},
        {"routes": {"pi-route": {**pi.spec, "subjects": {"user-b": "pi-b", "user-a": "pi-b"}}}},
        {"routes": {"pi-route": {**pi.spec, "subjects": []}}},
    ]
    for document in cases:
        private_json(pi.route_file, document)
        with pytest.raises(GatewayRequestError, match="绑定"):
            app._bound_client(binding)
    private_json(pi.route_file, pi.manifest)
    pi.route_file.chmod(0o644)
    with pytest.raises(GatewayRequestError):
        app._bound_client(binding)
    target = pi.route_file.rename(tmp_path / "saved")
    target.chmod(0o600)
    pi.route_file.symlink_to(target)
    with pytest.raises(GatewayRequestError):
        app._bound_client(binding)
    pi.route_file.unlink()
    with pytest.raises(GatewayRequestError):
        app._bound_client(binding)
    pi.route_file.write_text("{")
    pi.route_file.chmod(0o600)
    with pytest.raises(GatewayRequestError):
        app._bound_client(binding)
