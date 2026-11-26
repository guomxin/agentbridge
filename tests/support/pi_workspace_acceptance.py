"""Two real Workspace bindings, an isolated central MCP and a loopback Pi SDK/model.

Run with --node and --sdk-dir. The three printed URLs open fixture sign-in screens.
No production inputs, Telegram consumer or system business writes are used.
"""
from contextlib import contextmanager, ExitStack
from pathlib import Path
import argparse
import json
import os
import secrets
import signal
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit
from unittest.mock import patch

import uvicorn

from agentbridge.admin.application import AdminControlPlane
from agentbridge.admin.server import create_admin_http_server, validate_admin_server_config
from agentbridge.mcp.resource_budget import BoundedMcpProtocol
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.nanobot_gateway import PiGatewayClient
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.support.interaction_acceptance import free_port
from tests.support.nanobot_mcp_fixture import NanoBotMcpFixture, private_file
from tests.support.workspace_host_acceptance import add_fixture_signin
from tests.test_workspace import FakeGateway, PASSWORD, _create_account


@contextmanager
def central_fixture(directory, *, host_url, token_file, central_options=None, configure=None, desk_bot=None, maximum_level="L1"):
    directory = Path(directory)
    if urlsplit(host_url).hostname != "127.0.0.1":
        raise ValueError("Pi fixture needs a loopback host")
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ))
        os.environ.pop("AGENTBRIDGE_NANOBOT_ROUTES_FILE", None)
        listener = stack.enter_context(socket.socket())
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        port = listener.getsockname()[1]
        central = NanoBotMcpFixture(directory / "central", f"http://127.0.0.1:{port}", port, **(central_options or {}))
        stack.callback(central.close)
        alias = "pi-office"
        refs = {subject: f"pi-credential-{label}" for label, subject in central.subjects.items()}
        bot, username, first_sender = desk_bot or ("8801", "fixture_pi_bot", "501")
        route = {"url": host_url, "instanceId": alias, "adapterVersion": "0.1.5", "tokenFile": str(token_file),
                 "subjects": refs, "telegram": {"botId": str(bot), "username": username,
                                               "allowedSenders": {central.subjects["a"]: first_sender}}}
        route["requiredLevel"] = maximum_level
        route_file = directory / "pi-routes.json"
        private_file(route_file, json.dumps({"routes": {alias: route}}).encode())
        os.environ["AGENTBRIDGE_PI_ROUTES_FILE"] = str(route_file)
        gateway = FakeGateway()
        gateway.diagnostics = lambda: {"configured": True}
        control = AdminControlPlane(service=central.service, identity_store=central.identities, workspace_gateway=gateway)
        admin_name, password = "piadmin", secrets.token_urlsafe(24) + "Ab9!"
        actor = control.accounts.create(username=admin_name, password=password, role="admin", must_change_password=False)
        control.configure_workspace_host(actor=actor, request_ip="127.0.0.1", action="save",
            body={"instance_id": alias, "route_ref": alias, "display_name": "Pi", "state": "enabled",
                  "subjects": list(central.subjects.values()), "expected_revision": 0, "reason": "隔离宿主验收"})
        accounts, files, workspace_names = {}, {}, {}
        for label, subject in central.subjects.items():
            account = _create_account(central.service, user_subject=subject, username="piuser" + label,
                                      client_type="web", endpoint_key="web:synthetic:" + label)
            workspace_names[label] = account["username"]
            control.workspace_hosts.directory.choices(account["account_id"])
            web = control.configure_workspace_host(actor=actor, request_ip="127.0.0.1", action="bind",
                body={"instance_id": alias, "account_id": account["account_id"],
                      "expected_revision": 1, "reason": "为独立验证主体绑定 Pi"})
            accounts[label] = {k: web[k] for k in ("user_subject", "endpoint_id", "endpoint_key", "session_key", "credential_ref")}
            minted = central.identities.issue(user_subject=subject, expected_principal_ref=subject, ttl_seconds=7200,
                                              label=f"workspace-pi:{alias}:{web['credential_ref']}")
            stack.callback(central.identities.revoke, minted["token_id"])
            filename = directory / ("pi-" + label + ".token")
            private_file(filename, minted["token"].encode())
            stack.callback(filename.unlink, missing_ok=True)
            files[label] = str(filename)
            if label == "a":
                for number in range(160):
                    central.service.tasks.append_timeline_message(user_subject=subject, source_endpoint_id=web["endpoint_id"],
                        message_key=f"pi-history-{number}", role="assistant", text=f"隔离会话历史第{number+1}条。" +
                        "用于检查切换宿主后长历史的内部滚动、输入位置和刷新状态。" * 4)
        if configure:
            configure(central)
        app = WorkspaceApplication(service=central.service, gateway=gateway, host_routes=PiGatewayClient.from_environment())
        stack.callback(app.close)
        signin = {}
        for label, domain in (("admin", "127.0.0.1"), ("a", "127.0.0.1"), ("b", "localhost")):
            web_port = free_port()
            origin = f"http://{domain}:{web_port}"
            settings = dict(host="127.0.0.1", port=web_port, public_base_url=origin, tls_cert=None, tls_key=None)
            if label == "admin":
                server = create_admin_http_server(config=validate_admin_server_config(**settings), control_plane=control)
                users = {admin_name: password}
            else:
                server = create_workspace_http_server(config=validate_workspace_server_config(**settings), application=app)
                users = {workspace_names[label]: PASSWORD}
            add_fixture_signin(server, origin, users)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            signin[label] = origin + "/fixture-signin"
        daemon = uvicorn.Server(uvicorn.Config(central.app, http=BoundedMcpProtocol, ws="none",
                                               access_log=False, log_level="error"))
        thread = threading.Thread(target=daemon.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        stack.callback(thread.join, 5)
        stack.callback(setattr, daemon, "should_exit", True)
        deadline = time.monotonic() + 8
        while not daemon.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("Pi isolated MCP did not start")
            time.sleep(.05)
        meta = {**central.meta, "tokenFiles": files, "centralHome": str(central.service.home),
                "routeFile": str(route_file), "signin": signin}
        private_file(directory / "public-web.json", json.dumps({"signin": signin}).encode())
        yield meta, accounts


def main():
    from tests.support.pi_sdk_probe import pi_fixture
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-dir", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    args = parser.parse_args()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="pi-workspace-") as tmp:
        with pi_fixture(Path(tmp), args.node.resolve(), args.sdk_dir.resolve()):
            print((Path(tmp) / "public-web.json").read_text(), flush=True)
            stopped.wait()


if __name__ == "__main__":
    main()
