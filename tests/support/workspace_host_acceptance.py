"""Loopback-only Workspace/admin pilot with real HTTP, stores and a synthetic OA/MCP backend."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import secrets
import signal
import tempfile
import threading
from urllib.parse import urlsplit

import uvicorn

from agentbridge.admin.application import AdminControlPlane
from agentbridge.admin.server import create_admin_http_server, validate_admin_server_config
from agentbridge.mcp.resource_budget import BoundedMcpProtocol
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.support.interaction_acceptance import free_port
from tests.support.nanobot_mcp_fixture import NanoBotMcpFixture, private_file
from tests.test_workspace import FakeGateway, PASSWORD, _create_account


def add_fixture_signin(server, origin, users):
    """Only the synthetic local servers add this convenience UI. Production login issues the cookies."""
    base = server.RequestHandlerClass

    class Signin(base):
        def do_GET(self):
            if self.path != "/fixture-signin":
                return super().do_GET()
            if self.headers.get("Host") != urlsplit(origin).netloc:
                return self.send_error(403)
            buttons = "".join(f'<button value="{name}" type="button">{name}</button>' for name in users)
            page = ('<!doctype html><html lang="zh-CN"><meta charset=utf-8><title>隔离验收账号</title>'
                    '<h1>登录隔离验收账号</h1>' + buttons + '<p id="status"></p><script>'
                    'document.onclick=async e=>{if(e.target.tagName!=="BUTTON")return;'
                    'const r=await fetch("/fixture-signin",{method:"POST",headers:{"Content-Type":"application/json"},'
                    'body:JSON.stringify({account:e.target.value})});'
                    'if(r.ok)location.replace("/");else document.querySelector("#status").textContent="登录失败";};'
                    '</script></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            if self.path != "/fixture-signin":
                return super().do_POST()
            if self.headers.get("Origin") != origin or self.headers.get("Host") != urlsplit(origin).netloc:
                return self.send_error(403)
            if self.headers.get("Content-Type") != "application/json":
                return self.send_error(400)
            amount = int(self.headers.get("Content-Length", "0"))
            if not 0 < amount < 100:
                return self.send_error(400)
            name = json.loads(self.rfile.read(amount)).get("account")
            if name not in users:
                return self.send_error(400)
            self.path = "/api/login"
            data = json.dumps({"username": name, "password": users[name]}).encode()
            self.headers.replace_header("Content-Length", str(len(data)))
            original = self.rfile
            self.rfile = io.BytesIO(data)
            try:
                return super().do_POST()
            finally:
                self.rfile = original

    server.RequestHandlerClass = Signin


def main():
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--parent", type=Path, help="Existing private directory for a new fixture")
    options.add_argument("--resume", type=Path, help="Reopen this private fixture on its original ports")
    options.add_argument("--credential-nanobot-python", type=Path, help="Start isolated L2 SDK/model/broker instead of the L1 fixture")
    options.add_argument("--fixture-card-cert", type=Path, help="Optional isolated Chrome-trusted TLS certificate for localhost")
    options.add_argument("--fixture-card-key", type=Path, help="Matching isolated TLS key")
    options.add_argument("--b4",action="store_true",help="Seed skill, Telegram and file acceptance data")
    options.add_argument("--host-isolation", action="store_true", help="Show an active task and a message in each host")
    args = options.parse_args()
    if args.resume and args.credential_nanobot_python:
        options.error("Start a fresh credential acceptance fixture")
    os.umask(0o077)
    root = args.resume or Path(tempfile.mkdtemp(prefix="agentbridge-workspace-hosts-", dir=args.parent))
    old = json.loads((root / "public.json").read_text()) if args.resume else {}
    if old and (old["home"] != str(root) or not root.name.startswith("agentbridge-workspace-hosts-")):
        raise ValueError("Only an existing synthetic host fixture can be resumed")
    mcp_port = urlsplit(old["mcp"]).port if old else free_port()
    host_port = old["worker_port"] if old else free_port()
    credential_fixture = None
    mcp_options = {}
    args.b4 = args.b4 or bool(old and (root/"b4host").is_dir())
    if args.b4:
        media_port_file=root/"b4-media.port"
        if not media_port_file.exists():
            private_file(media_port_file,str(free_port()).encode())
        media_port=int(media_port_file.read_text())
        mcp_options["trusted_card_origin"] = f"http://127.0.0.1:{media_port}"
    if args.credential_nanobot_python:
        from tests.support.nanobot_credential_probe import credential_service
        from tests.support.nanobot_credential_workspace import CredentialWorkspace
        credential_fixture = CredentialWorkspace(root, args.credential_nanobot_python, args.fixture_card_cert, args.fixture_card_key)
        mcp_options = {"service_class":credential_service(),"trusted_card_origin":credential_fixture.trusted_origin}
    mcp = NanoBotMcpFixture(root / "mcp", f"http://127.0.0.1:{mcp_port}", mcp_port, reuse_existing=bool(old), **mcp_options)
    alias = "nanobot-office"
    route_file = root / "routes.json"
    if not old:
        private_file(root / "service.token", secrets.token_urlsafe(36).encode())
        private_file(route_file, json.dumps({"routes":{alias:{
            "url":f"http://127.0.0.1:{host_port}", "instanceId":alias, "tokenFile":str(root/"service.token"),
            **({"telegram":{"botId":"8001","username":"fixture_bot"}} if args.b4 else {}),
            "adapterVersion":"0.3.0" if args.b4 else "0.2.0" if credential_fixture else "0.1.0", "subjects":{subject: "cred-"+label for label,subject in mcp.subjects.items()}
        }}}).encode())
    os.environ["AGENTBRIDGE_NANOBOT_ROUTES_FILE"] = str(route_file)
    gateway = FakeGateway()
    gateway.diagnostics = lambda: {"configured": True}
    control = AdminControlPlane(service=mcp.service, identity_store=mcp.identities, workspace_gateway=gateway)
    credential = root / "fixture-admin.json"
    if credential.is_file():
        admin_name,admin_password = json.loads(credential.read_text())
        actor = control.accounts.authenticate(username=admin_name,password=admin_password)
    else:
        # Older fixture runs only retained the original password in process memory.
        admin_name = "hostreview" if old else "hostadmin"
        admin_password = secrets.token_urlsafe(28) + "Ab9!"
        actor = control.accounts.create(username=admin_name, password=admin_password, role="admin", must_change_password=False)
        private_file(credential,json.dumps([admin_name,admin_password]).encode())
    if not old:
        control.configure_workspace_host(actor=actor, request_ip="127.0.0.1", action="save", body={
            "instance_id":alias, "route_ref":alias, "display_name":"NanoBot", "state":"enabled",
            "subjects":list(mcp.subjects.values()), "expected_revision":0, "reason":"隔离验收配置"})
        bindings = []
        for label,subject in mcp.subjects.items():
            name = "pilot"+label
            account = _create_account(mcp.service, user_subject=subject, username=name, endpoint_key="telegram:"+name)
            # Select the isolated OpenClaw Gateway before adding the second binding.
            control.workspace_hosts.directory.choices(account["account_id"])
            binding = control.configure_workspace_host(actor=actor, request_ip="127.0.0.1", action="bind", body={
                "instance_id":alias, "account_id":account["account_id"], "expected_revision":1, "reason":"合成主体开通"})
            bindings.append({k:v for k,v in binding.items() if k != "instance_id"})
            if args.host_isolation:
                for host_type, endpoint, session in (
                    ("openclaw", account["endpoint_id"], account["openclaw_session_key"]),
                    ("nanobot", binding["endpoint_id"], binding["session_key"]),
                ):
                    title = f"{host_type} 独立验收任务 {label}"
                    mcp.service.tasks.ensure_task(user_subject=subject, agent_host=host_type, host_task_key=title,
                        origin_endpoint_id=endpoint, active_conversation_ref=session, title=title)
                    mcp.service.tasks.append_timeline_message(user_subject=subject, source_endpoint_id=endpoint,
                        message_key=f"accept-{host_type}-{label}", role="user", text=f"{host_type} 独立验收消息 {label}")
            if label == "b" or credential_fixture or args.b4:
                control.workspace_hosts.directory.set_default(account["account_id"], alias, expected_revision=1)
        private_file(root / "bindings.json", json.dumps(bindings).encode())
    if args.b4 and not old:
        from tests.support.nanobot_b4_setup import seed
        seed(mcp)
    if credential_fixture:
        credential_fixture.start(mcp,bindings,alias,host_port)
    workspace = WorkspaceApplication(service=mcp.service, gateway=gateway, host_routes=NanoBotGatewayClient.from_environment())
    servers, links = [], {}
    if args.b4:
        from tests.support.nanobot_b4_setup import start_media
        media=start_media(mcp,media_port)
        servers.append(media)
        threading.Thread(target=media.serve_forever,daemon=True).start()
    for name,domain in (("admin","127.0.0.1"),("a","127.0.0.1"),("b","localhost")):
        port = urlsplit(old["signin"][name]).port if old else free_port()
        origin = f"http://{domain}:{port}"
        params = dict(host="127.0.0.1", port=port, public_base_url=origin, tls_cert=None, tls_key=None)
        if name == "admin":
            server = create_admin_http_server(config=validate_admin_server_config(**params), control_plane=control)
            users = {admin_name: admin_password}
        else:
            server = create_workspace_http_server(config=validate_workspace_server_config(**params), application=workspace)
            users = {"pilot"+name:PASSWORD}
        add_fixture_signin(server, origin, users)
        if args.b4 and name != "admin":
            from tests.support.nanobot_b4_setup import attach_file_fixture
            attach_file_fixture(server,mcp,origin)
        if credential_fixture and name != "admin":
            credential_fixture.add_entry(server,origin)
        servers.append(server)
        links[name] = origin + "/fixture-signin"
        threading.Thread(target=server.serve_forever, daemon=True).start()
    daemon = uvicorn.Server(uvicorn.Config(mcp.app, host="127.0.0.1", port=mcp_port, http=BoundedMcpProtocol,
                                           ws="none", access_log=False, log_level="error"))
    threading.Thread(target=daemon.run, daemon=True).start()
    meta = {"home":str(root),"mcp":mcp.meta["endpoint"],"worker_port":host_port,"signin":links}
    if credential_fixture:
        meta["fixture_broker_entry"] = {k: links[k].replace("/fixture-signin","/fixture-credential") for k in ("a","b")}
        meta["native_card_browser_tls_configured"] = bool(credential_fixture.https_port)
    if not old:
        private_file(root/"public.json", json.dumps(meta).encode())
    print(json.dumps(meta), flush=True)
    stopped = threading.Event()
    reload_requested = threading.Event()
    def reload_handler(*_):
        reload_requested.set()
        stopped.set()
    signal.signal(signal.SIGHUP, reload_handler)
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        stopped.wait()
    finally:
        daemon.should_exit = True
        for server in servers:
            server.shutdown()
            server.server_close()
        workspace.close()
        if credential_fixture:
            credential_fixture.close()
        if reload_requested.is_set():
            os.execv(os.sys.executable,[os.sys.executable,"-m","tests.support.workspace_host_acceptance","--resume",str(root)])
        mcp.close()
        credential.unlink(missing_ok=True)
        (root/"service.token").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
