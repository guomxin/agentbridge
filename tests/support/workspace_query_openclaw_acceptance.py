"""Opt-in Workspace -> real candidate OpenClaw consumer -> Telegram acceptance.

Default transport is a local HTTP Bot API substitute. --mode live requires a
clean Full candidate and reviewed first-party Bot/sender/config inputs. Neither
mode launches getUpdates, modifies the running Gateway or accesses real OA.
The Gateway wake shell is synthetic; the durable command runner, Central MCP,
query, source projection, Coordinator and SDK outbound send are candidate code.
Cards use the product's existing HTTP loopback URL buttons by default. Supplying
a reviewed localhost TLS certificate/key selects its WebApp plus browser buttons.
Platform acceptance and desktop opening must be observed separately in live mode.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
from uuid import uuid4

import uvicorn

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.auth.flow import TrustedFlowApplication
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
from agentbridge.mcp.resource_budget import BoundedMcpProtocol, bounded_mcp_app
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.authorization_fixtures import grant_permissions
from tests.support.business_draft_acceptance import SyntheticBroker
from tests.support.interaction_acceptance import ReadOnlyGateway, write_metadata
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract, OWNER
from tests.support.workspace_host_acceptance import add_fixture_signin
from tests.test_workspace import PASSWORD

ROOT = Path(__file__).resolve().parents[2]


class DurableWakeGateway(ReadOnlyGateway):
    def __init__(self, home):
        self.home = home

    def call(self, method, params=None, **kwargs):
        if method in {"agentbridge.task.wake", "agentbridge.workspace.bind"}:
            # The candidate runtime reporter independently discovers and fences
            # the already-durable command; the HTTP worker executes no business.
            (self.home / "command-wake").write_text("durable-command-ready")
            return {"status": "accepted"}
        return super().call(method, params, **kwargs)

    def diagnostics(self):
        return {"configured": True}


def serve_http(stack, server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stack.callback(thread.join, 3)
    stack.callback(server.server_close)
    stack.callback(server.shutdown)


def verify_candidate(mode):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    if mode == "live":
        if dirty:
            raise ValueError("Live desktop acceptance requires the clean fixed candidate")
        receipt = json.loads((ROOT / "output/release-validation/full.json").read_text())
        if receipt.get("status") != "succeeded" or receipt.get("inputs", {}).get("commit") != commit:
            raise ValueError("Live desktop acceptance requires this candidate's successful Full receipt")
        artifact = json.loads(subprocess.check_output([
            os.sys.executable, str(ROOT / "scripts/agentbridge_artifact.py"), "verify", "--root", str(ROOT)], cwd=ROOT))
        return {"commit": commit, "clean": True, "wheelSha256": artifact["sha256"]}
    return {"commit": commit, "clean": not dirty, "developmentOnly": True}


def report(fixture, home, mode, candidate, wire, card_origin, card_transport):
    with fixture.service.tasks._connect() as db:
        deliveries = [dict(row) for row in db.execute("""SELECT delivery_id,task_id,operation_id,state,attempt,
            receipt,outcome,card_issued FROM workspace_query_deliveries ORDER BY rowid""")]
        text_deliveries = [dict(row) for row in db.execute("""SELECT delivery_id,entry_id,kind,part_index,part_count,
            state,attempt,receipt,outcome FROM workspace_text_deliveries ORDER BY rowid""")]
        operations = [dict(row) for row in db.execute("SELECT operation_id,capability_name,status FROM operations ORDER BY rowid")]
        entries = [dict(row) for row in db.execute("SELECT entry_id,role,task_id FROM user_timeline WHERE entry_type='chat_message' ORDER BY sequence")]
    receipts = []
    path = home / "telegram-receipts.jsonl"
    if path.exists():
        receipts = [json.loads(line) for line in path.read_text().splitlines() if line]
    audit = fixture.audit()
    value = {"mode": mode, "candidate": candidate, "originalDeliveries": deliveries, "textDeliveries": text_deliveries, "operations": operations,
             "timeline": entries, "telegramReceipts": receipts, "syntheticWire": wire,
             "downstreamWriteCount": audit["downstreamWriteCount"], "decisionCount": audit["decisionCount"],
             "authorizationStates": audit["authorizationStates"], "productionHostModified": False,
             "inboundPollingStarted": False, "desktopObserved": False,
             "cardOrigin": card_origin, "cardTransport": card_transport,
             "nativeWebAppExpected": card_transport == "trusted-tls", "cardOpenObserved": False}
    write_metadata(home / "evidence.json", value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--sdk-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--source-config", type=Path)
    parser.add_argument("--sender", default="501")
    parser.add_argument("--bot-id")
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--card-transport", choices=("loopback-http", "trusted-tls"),
                        help="Defaults to trusted-tls with a certificate/key, otherwise loopback-http URL buttons")
    parser.add_argument("--card-host", choices=("localhost", "127.0.0.1"), default="localhost")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--shutdown-after", type=int, default=1200)
    args = parser.parse_args()
    if not args.sender.isdecimal() or int(args.sender) <= 0 or not 1 <= args.shutdown_after <= 3600:
        parser.error("Use one positive private sender and a bounded 1..3600 second session")
    if args.mode == "live" and (args.self_check or not all((args.source_config, args.bot_id))):
        parser.error("Live mode needs reviewed Bot/config/sender; it cannot auto-query")
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("Provide both the reviewed local TLS certificate and key")
    card_transport = args.card_transport or ("trusted-tls" if args.tls_cert else "loopback-http")
    if (card_transport == "trusted-tls") != bool(args.tls_cert):
        parser.error("trusted-tls requires certificate/key; loopback-http cannot use TLS inputs")
    home = args.home.resolve()
    if args.home.is_symlink() or (home.exists() and any(home.iterdir())):
        parser.error("Use a fresh private home")
    os.umask(0o077)
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    candidate = verify_candidate(args.mode)
    with ExitStack() as stack:
        fixture = LatencyAcceptanceFixture(home / "central", seed=False,
            latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
        stack.callback(fixture.close)
        # Selection eligibility requires the exact synthetic leave permission.
        # The independent invoke guard below still denies every prepare/commit;
        # opening a result page does not create a business decision or write.
        grant_permissions(fixture.service.user_grants, OWNER, ["oa.workflow.read", "oa.leave.approve"])
        invoke = fixture.service.invoke
        def read_only(*, capability_name, **kwargs):
            if capability_name != "oa.workflow.pending.list":
                raise PermissionError("Acceptance allows only the synthetic pending-list read")
            return invoke(capability_name=capability_name, **kwargs)
        fixture.service.invoke = read_only
        fixture.service.skills.save("user:" + OWNER, {"oa-pending": {"profiles": ["single", "batch"]}},
            expected_revision=fixture.service.skills.config("user:" + OWNER)["revision"],
            actor="isolated-acceptance", reason="Read-only result-card visibility")
        fixture.app.close()
        fixture.app = WorkspaceApplication(service=fixture.service, gateway=DurableWakeGateway(home))
        identities = McpIdentityTokenStore(fixture.service.db_path)
        identity = identities.issue(user_subject=OWNER, expected_principal_ref=OWNER, label="isolated-openclaw-query", ttl_seconds=7200)
        stack.callback(identities.revoke, identity["token_id"])
        token_file = home / "fixture.token"
        token_file.write_text(identity["token"])
        token_file.chmod(0o600)
        stack.callback(token_file.unlink, missing_ok=True)
        fixture.service.tasks.ensure_endpoint(user_subject=OWNER, token_id=identity["token_id"], agent_host="openclaw",
            endpoint_key=f"telegram:default:{args.sender}", client_type="telegram", external_subject=args.sender,
            account_id="default", conversation_ref=f"agent:main:telegram:direct:{args.sender}",
            capabilities=["timeline_message", "trusted_interaction"],
            route={"channel": "telegram", "to": args.sender, "accountId": "default"})
        card_scheme = "https" if card_transport == "trusted-tls" else "http"
        card_origin = f"{card_scheme}://{args.card_host}:{fixture.meta['card_port']}"
        fixture.service.trusted_card_base_url = card_origin
        fields = TrustedFieldApplication(submission_store=fixture.service.field_submissions)
        actions = TrustedActionApplication(authorization_store=fixture.service.write_authorizations)
        cards = create_auth_http_server(config=validate_auth_server_config(host="127.0.0.1", port=fixture.meta["card_port"],
            public_base_url=card_origin, tls_cert=args.tls_cert, tls_key=args.tls_key),
            application=TrustedAuthApplication(challenge_store=fixture.service.challenges, broker=SyntheticBroker(fixture.service.challenges)),
            field_application=fields, action_application=actions,
            flow_application=TrustedFlowApplication(service=fixture.service, field_application=fields, action_application=actions))
        serve_http(stack, cards)
        workspace = create_workspace_http_server(config=validate_workspace_server_config(host="127.0.0.1",
            port=fixture.meta["workspace_port"], public_base_url=fixture.workspace_url, tls_cert=None, tls_key=None), application=fixture.app)
        add_fixture_signin(workspace, fixture.workspace_url, {"cardreview": PASSWORD, "cardother": PASSWORD})
        serve_http(stack, workspace)
        listener = stack.enter_context(socket.socket())
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        mcp = create_central_mcp_server(service=fixture.service, identity_store=identities,
            config=validate_central_mcp_server_config(host="127.0.0.1", port=port, public_base_url=base, tls_cert=None, tls_key=None),
            auth_card_base_url=card_origin)
        daemon = uvicorn.Server(uvicorn.Config(bounded_mcp_app(mcp, identities), http=BoundedMcpProtocol,
            ws="none", access_log=False, log_level="error"))
        thread = threading.Thread(target=daemon.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        stack.callback(thread.join, 5)
        stack.callback(setattr, daemon, "should_exit", True)
        deadline = time.monotonic() + 15
        while not daemon.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Isolated MCP startup failed")
            time.sleep(.02)
        wire = []
        spec = {"mode": args.mode, "home": str(home), "mcpUrl": base + "/mcp", "tokenFile": str(token_file),
                "cardOrigin": card_origin, "sdkDir": str(args.sdk_dir.resolve()), "sender": args.sender}
        if args.mode == "offline":
            class Bot(BaseHTTPRequestHandler):
                def log_message(self, *_): pass
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    if self.path.rsplit("/", 1)[-1] != "sendMessage" or str(body.get("chat_id")) != args.sender:
                        self.send_error(403)
                        return
                    receipt = len(wire) + 1
                    buttons = [button for row in body.get("reply_markup", {}).get("inline_keyboard", []) for button in row]
                    wire.append({"messageId": receipt, "buttons": len(buttons),
                                 "urlButtons": sum(bool(button.get("url")) for button in buttons),
                                 "webAppButtons": sum(bool(button.get("web_app", {}).get("url")) for button in buttons)})
                    raw = json.dumps({"ok": True, "result": {"message_id": receipt, "date": int(time.time()),
                        "chat": {"id": int(args.sender), "type": "private"}, "from": {"id": 123456, "is_bot": True}, "text": body["text"]}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
            bot = ThreadingHTTPServer(("127.0.0.1", 0), Bot)
            serve_http(stack, bot)
            spec["botApiRoot"] = f"http://127.0.0.1:{bot.server_port}"
        else:
            spec.update(sourceConfig=str(args.source_config.resolve()), botId=args.bot_id,
                        sourceConfigHash=hashlib.sha256(args.source_config.read_bytes()).hexdigest())
        spec_file = home / "consumer.json"
        write_metadata(spec_file, spec)
        env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "SHELL") if key in os.environ}
        env.update(OPENCLAW_STATE_DIR=str(home / "openclaw-state"), OPENCLAW_CONFIG_PATH=str(home / "openclaw-isolated.json"))
        output = stack.enter_context((home / "consumer.log").open("w"))
        child = subprocess.Popen([str(args.node), str(ROOT / "integrations/openclaw-agentbridge/test/support/workspace-query-acceptance.mjs"), str(spec_file)],
            cwd=ROOT, env=env, stdout=output, stderr=output)
        def stop_child():
            if child.poll() is None:
                child.terminate()
                try: child.wait(10)
                except subprocess.TimeoutExpired: child.kill(); child.wait(5)
        stack.callback(stop_child)
        deadline = time.monotonic() + 25
        while not (home / "host-ready.json").exists():
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Candidate consumer did not become ready; inspect private consumer.log")
            time.sleep(.05)
        print(json.dumps({"signin": fixture.workspace_url + "/fixture-signin", "home": str(home),
            "mode": args.mode, "candidate": candidate, "cardTransport": card_transport,
            "cardOrigin": card_origin, "evidence": str(home / "evidence.json")}), flush=True)
        if args.self_check:
            fixture.app.query_oa_pending(fixture.account, {"requestKey": uuid4().hex, "userMessage": "查看OA待办"})
        stop = threading.Event()
        for number in (signal.SIGTERM, signal.SIGINT): signal.signal(number, lambda *_: stop.set())
        deadline = time.monotonic() + (30 if args.self_check else args.shutdown_after)
        while not stop.wait(.2):
            if child.poll() is not None:
                raise RuntimeError("Candidate consumer exited during acceptance")
            evidence = report(fixture, home, args.mode, candidate, wire, card_origin, card_transport)
            if (args.self_check and evidence["originalDeliveries"] and evidence["textDeliveries"]
                    and all(row["state"] == "sent" for row in evidence["originalDeliveries"] + evidence["textDeliveries"])):
                assert len(evidence["operations"]) == 1 and not evidence["downstreamWriteCount"] and not evidence["decisionCount"]
                assert len(evidence["originalDeliveries"]) == len(evidence["textDeliveries"]) == 1
                assert evidence["originalDeliveries"][0]["card_issued"] == 1
                assert evidence["textDeliveries"][0]["kind"] == "user"
                accepted = [row["receipt"] for row in evidence["originalDeliveries"] + evidence["textDeliveries"]]
                assert len(set(accepted)) == len(wire) == len(evidence["telegramReceipts"]) == 2
                assert set(accepted) == {row["messageId"] for row in wire}
                assert set(accepted) == {int(row["messageId"]) for row in evidence["telegramReceipts"]}
                assert all(row["buttonOriginsMatch"] for row in evidence["telegramReceipts"])
                assert sum(row["urlButtons"] for row in wire) == 1
                assert sum(row["webAppButtons"] for row in wire) == int(card_transport == "trusted-tls")
                print(json.dumps({"selfCheck": "passed", "originalDeliveryCount": len(evidence["originalDeliveries"]),
                    "textDeliveryCount": len(evidence["textDeliveries"]),
                    "syntheticPosts": len(wire), "operations": len(evidence["operations"])}), flush=True)
                break
            if time.monotonic() >= deadline:
                if args.self_check: raise RuntimeError("Original query did not complete its delivery; inspect private evidence")
                break


if __name__ == "__main__":
    main()
