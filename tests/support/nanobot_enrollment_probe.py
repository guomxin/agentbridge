"""First-login acceptance with real Workspace, MCP, NanoBot SDK and two new identities."""

import argparse
import json
import os
from pathlib import Path
import secrets
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agentbridge.mcp.resource_budget import BoundedMcpProtocol
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.host_instances import LEGACY_INSTANCE_ID
from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.support.interaction_acceptance import free_port
from tests.support.nanobot_mcp_fixture import NanoBotMcpFixture, private_file
from tests.support.nanobot_sdk_probe import MODEL, QUESTION, Scenario, model_server
from tests.support.nanobot_telegram_loopback import BOT, fixture as telegram_fixture, update


def json_file(path, value):
    private_file(path, json.dumps(value).encode())
    return str(path)


def serve(server, cleanup):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cleanup.callback(server.server_close)
    cleanup.callback(server.shutdown)


def enable_browser_fixture(server, origin, create_pilot):
    from agentbridge.workspace.server import ASSET_VERSION_PLACEHOLDER, STATIC_ROOT, _workspace_asset_version

    script = """
    const send = document.createElement("button");
    send.type = "button"; send.textContent = "从隔离合成私聊发送此指令";
    document.querySelector("#enroll-pending").append(send);
    send.onclick = async () => {
      send.disabled = true;
      const result = await fetch("/fixture-inbox", {method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({text:document.querySelector("#link-command").textContent})});
      if (!result.ok) { send.disabled = false; send.textContent = "合成投递失败，重试"; }
    };
    const fill = document.createElement("button");
    fill.type = "button"; fill.textContent = "填入隔离测试账号资料";
    const form = document.querySelector("#enroll-complete");
    form.prepend(fill);
    fill.onclick = () => {
      form.elements.username.value = "new-browser-alice";
      const secret = crypto.randomUUID();
      form.elements.password.value = secret; form.elements.passwordConfirm.value = secret;
    };
    """
    class BrowserFixture(server.RequestHandlerClass):
        def _fixture_response(self, content, mime):
            data = content.encode()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)

        def _static(self, relative):
            if relative == "index.html":
                content = (STATIC_ROOT / relative).read_text()
                content = content.replace(ASSET_VERSION_PLACEHOLDER, _workspace_asset_version())
                self._fixture_response(content.replace("</body>", '<script src="/fixture-inbox.js"></script></body>'),
                                       "text/html; charset=utf-8")
            else:
                super()._static(relative)

        def do_GET(self):
            if self.path == "/fixture-inbox.js" and self._host_allowed():
                self._fixture_response(script, "application/javascript; charset=utf-8")
            else:
                super().do_GET()

        def do_POST(self):
            if self.path != "/fixture-inbox":
                return super().do_POST()
            if (not self._host_allowed() or self.headers.get("Origin") != origin
                    or self.headers.get("Content-Type") != "application/json"):
                return self.send_error(403)
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length < 100:
                return self.send_error(400)
            message = json.loads(self.rfile.read(length)).get("text")
            if not isinstance(message, str) or not re.fullmatch(r"/agentbridge link [A-Z2-9]{8}", message):
                return self.send_error(400)
            pilot = create_pilot()
            try:
                pilot.ingest(update(1, 501, time.time_ns() // 1_000_000, text=message))
            finally:
                pilot.db.close()
            self._json(200, {"status":"delivered_to_synthetic_bot"})
    server.RequestHandlerClass = BrowserFixture


def run(python, browser=False):
    instance = "nanobot-new-accounts"
    with tempfile.TemporaryDirectory(prefix="nanobot-new-accounts-") as area, ExitStack() as cleanup:
        root = Path(area)
        mcp_port, worker_port, web_port = free_port(), free_port(), free_port()
        base = f"http://127.0.0.1:{mcp_port}"
        previous_key = os.environ.get("AGENTBRIDGE_SESSION_KEY_FILE")
        previous_route = os.environ.get("AGENTBRIDGE_NANOBOT_ROUTES_FILE")
        def restore():
            for name, previous in (("AGENTBRIDGE_SESSION_KEY_FILE", previous_key),
                                   ("AGENTBRIDGE_NANOBOT_ROUTES_FILE", previous_route)):
                if previous is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = previous
        cleanup.callback(restore)
        mcp = NanoBotMcpFixture(root / "mcp", base, mcp_port)
        cleanup.callback(mcp.close)
        subjects = mcp.subjects
        assignments = {subjects["a"]: "501", subjects["b"]: "502"}
        aliases = {subject: "personal-" + label for label, subject in subjects.items()}
        token_path = root / "host.token"
        private_file(token_path, secrets.token_hex(32).encode())
        worker = f"http://127.0.0.1:{worker_port}"
        bot = {"botId":str(BOT),"username":"fixture_bot"}
        os.environ["AGENTBRIDGE_NANOBOT_ROUTES_FILE"] = json_file(root / "routes.json", {"routes": {
            instance: {"instanceId":instance,"url":worker,"adapterVersion":"0.2.0","tokenFile": str(token_path),
                       "subjects":aliases,"telegram":bot,"enrollmentSenders":assignments}}})
        clients = NanoBotGatewayClient.from_environment()
        workspace = WorkspaceApplication(service=mcp.service, gateway=None, host_routes=clients)
        for name, version, typ, revision, allowed in [
                (LEGACY_INSTANCE_ID,"legacy","openclaw",1,[]), (instance,"0.2.0","nanobot",0,list(assignments))]:
            workspace.hosts.save_instance(name, host_type=typ, display_name=name, route_ref=name,
                                          adapter_version=version, state="enabled", subjects=allowed,
                                          expected_revision=revision, actor="test-admin", reason="Two new test identities")
        db = cleanup.enter_context(mcp.service.workspace._connect())
        assert db.execute("SELECT count(*) FROM workspace_accounts").fetchone()[0] == 0
        uv = uvicorn.Server(uvicorn.Config(mcp.app, host="127.0.0.1", port=mcp_port, http=BoundedMcpProtocol,
                                           ws="none", access_log=False, log_level="critical"))
        uvthread = threading.Thread(target=uv.run, daemon=True)
        uvthread.start()
        cleanup.callback(uvthread.join, 5)
        cleanup.callback(setattr, uv, "should_exit", True)
        seeds, models = [], []
        for label, subject in subjects.items():
            case = Scenario(label, label == "a", secrets.token_hex(24))
            models.append(case)
            model = model_server(case)
            serve(model, cleanup)
            config = json_file(root / f"model-{label}.json", {
                "agents":{"defaults":{"model":MODEL,"provider":"custom","maxToolIterations":4}},
                "providers":{"custom":{"apiKey":"synthetic","apiBase":f"http://127.0.0.1:{model.server_port}/v1"}}})
            seeds.append({"user_subject":subject,"endpoint_id":"bootstrap-" + label,
                          "endpoint_key":f"telegram:{BOT}:{assignments[subject]}","session_key":"bootstrap-" + label,
                          "credential_ref":aliases[subject],"tokenFile":str(mcp.home / f"{label}.token"),"modelConfigFile":config})
        spec = json_file(root / "worker.json", {"instanceId":instance,"mcpUrl":base + "/mcp",
                         "stateDir":str(root / "state"),"listenPort":worker_port,"serviceTokenFile":str(token_path),
                         "telegramPilot":bot,"bindings":seeds,"enrollmentSenders":assignments})
        command = "import sys;sys.path.insert(0,sys.argv.pop(1));from nanobot_agentbridge.server import main;main()"
        proc = subprocess.Popen([str(python),"-B","-I","-c",command,str(ROOT / "integrations/nanobot-agentbridge"),
                                 "--config",spec], env={"HOME":area,"PATH":"/usr/bin:/bin","TMPDIR":area},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cleanup.callback(proc.wait, 10)
        cleanup.callback(proc.terminate)
        gateway = clients[instance]
        for _ in range(150):
            if gateway.readiness().get("available") and uv.started:
                break
            assert proc.poll() is None, "isolated host failed to start"
            time.sleep(.1)
        else:
            raise TimeoutError("isolated NanoBot or MCP not ready")
        origin = f"http://127.0.0.1:{web_port}"
        config = validate_workspace_server_config(host="127.0.0.1", port=web_port, public_base_url=origin,
                                                   tls_cert=None, tls_key=None)
        web = create_workspace_http_server(config=config, application=workspace)
        telegram_home = root / "telegram"
        telegram_home.mkdir(mode=0o700)
        state, _, create = cleanup.enter_context(telegram_fixture(telegram_home, actual_gateway=gateway,
                                                                   actual_bindings={}, allow_pairing=True))
        if browser:
            enable_browser_fixture(web, origin, create)
        serve(web, cleanup)
        if browser:
            print(json.dumps({"origin": origin, "accountsBeforeOpen": 0, "assignedOnlyNanoBot": 2}), flush=True)
            while True:
                time.sleep(30)
        pilot = create()
        cleanup.callback(pilot.db.close)
        pilot.verify()
        for label, subject in subjects.items():
            number = int(assignments[subject])
            client = cleanup.enter_context(httpx.Client(base_url=origin, headers={"Origin":origin}, trust_env=False, timeout=60))
            assert not client.get("/api/session").json()["authenticated"]
            request = client.post("/api/enrollment/start", json={})
            request.raise_for_status()
            text = "/agentbridge link " + request.json()["linkCode"]
            pilot.ingest(update(1, 599, number, text=text))
            pilot.ingest(update(2, number, 2, text=text, forward_origin={"type":"hidden_user"}))
            pilot.ingest(update(3, number, 3, text=text, chat={"type":"group","id":-number}))
            assert client.get("/api/enrollment/status").json()["state"] == "pending"
            pilot.ingest(update(4, number, 4, text=text))
            assert client.get("/api/enrollment/status").json()["state"] == "confirmed", "private confirmation failed"
            pilot.ingest(update(5, number, 4, text=text))
            assert len([reply for reply in state["deliveries"] if reply["chat_id"] == number]) == 1
            profile = {"username":"new-user-" + label,"password":secrets.token_urlsafe(24)}
            opened = client.post("/api/enrollment/complete", json=profile)
            opened.raise_for_status()
            account_id = opened.json()["account"]["accountId"]
            account = db.execute("SELECT user_subject FROM workspace_accounts WHERE account_id=?", (account_id,)).fetchone()
            assert account[0] == subject
            client.cookies.clear()
            login = client.post("/api/login", json=profile)
            login.raise_for_status()
            assert login.json()["account"]["accountId"] == account_id
            offered = client.get("/api/hosts").json()
            assert [item["instance_id"] for item in offered["items"]] == [instance] and offered["default_instance_id"] == instance
            client.headers["X-AgentBridge-CSRF"] = client.cookies["agentbridge_workspace_csrf"]
            if label == "a":
                # Revoking the optional Telegram delivery channel must preserve Workspace use.
                from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
                TelegramPilotLinks(mcp.service).revoke(account_id, instance, str(BOT))
            messages = client.post("/api/chat/send-stream",json={"message":QUESTION,"idempotencyKey":"enroll-read-" + label})
            messages.raise_for_status()
            assert ("采购申请" in messages.text and "验收申请" in messages.text) if label == "a" else "未获" in messages.text, (
                "new user's Workspace chat failed", label, [x for x in messages.text.splitlines() if x.startswith("event:")])
        assert all(s.verified_model_input and not s.failures for s in models)
        assert db.execute("SELECT count(*) FROM client_endpoints WHERE agent_host='openclaw'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM workspace_link_challenges WHERE state='consumed'").fetchone()[0] == 2
        return {"new_accounts":2,"first_confirmations":2,"private_replies":len(state["deliveries"]),
                "offered_instances_per_account":1,"personal_authorization_verified":2,"openclaw_endpoints":0,
                "telegram_revocation_preserves_web":True,"downstream":"isolated model, Bot API and office fixtures"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-python", required=True, type=Path)
    parser.add_argument("--browser", action="store_true", help="Serve one fresh isolated browser entry until interrupted")
    args = parser.parse_args()
    print(json.dumps(run(args.sdk_python, args.browser), ensure_ascii=False))
