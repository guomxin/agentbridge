"""Actual private handler/MCP/consumer/Bot HTTP over isolated synthetic identities."""
import asyncio
from contextlib import asynccontextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from agentbridge.core import telegram_interaction_closures as closures
from agentbridge.core import telegram_notification_delivery as delivery
from agentbridge.core.endpoint_instances import endpoint_instances
from tests.support.workspace_query_acceptance import Fixture, INSTANCES
from tests.support.workspace_query_channel_probe import Channel
from tests.test_telegram_interaction_closures import login
from tests.test_workspace_query_delivery import drain
from nanobot_agentbridge.notifications import notifications, TerminalMessages
from nanobot_agentbridge.telegram import TelegramAPI


@pytest.mark.parametrize("host_type", ["nanobot", "pi"])
def test_actual_private_mcp_consumer_closes_received_card_once(tmp_path, monkeypatch, host_type):
    # Fixture environment is isolated and restored; no production identity,
    # model, browser, Telegram service or business endpoint is contacted.
    with monkeypatch.context() as env:
        for key in ("AGENTBRIDGE_SESSION_KEY_FILE", "AGENTBRIDGE_NANOBOT_ROUTES_FILE", "AGENTBRIDGE_PI_ROUTES_FILE"):
            env.setenv(key, "fixture-will-replace")
        fixture = Fixture(tmp_path / "three-host-central", deliveries=False)
        try:
            _exercise(fixture, host_type)
        finally:
            fixture.close()


def _exercise(fixture, host_type):
    service, subject = fixture.service, fixture.owner
    bot, sender = (int(n) for n in fixture.channel_identities[host_type])
    wire, calls = [], []
    class Bot(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            assert self.path.endswith("/sendMessage")
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["chat_id"] == sender
            wire.append(body)
            data = json.dumps({"ok": True, "result": {"message_id": len(wire) + 100,
                "from": {"id": bot}, "chat": {"id": sender, "type": "private"}}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Bot)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    api = TelegramAPI("synthetic-token", connection_factory=lambda: HTTPConnection(*server.server_address, timeout=5),
                      private_sender=lambda chat: chat == sender)
    channel = Channel(fixture, host_type, api)
    original = channel.pilot.gateway
    req = {**original.req, "workspaceEndpointId": original.record["workspaceEndpointId"]}
    token = Path(fixture.token_files[host_type, subject]).read_text().strip()
    profile = json.loads((Path(__file__).parents[1] / "schemas/agent-host/v1/test-vectors.json").read_text())["profiles"][0]["value"]
    profile.update(implementation={"name": host_type, "version": {"nanobot": "0.3.1", "pi": "0.1.8"}[host_type]},
                   hostInstanceId=INSTANCES[host_type])
    meta = {"io.agentbridge/host-context": {"version": "1", "agentHost": host_type,
        "hostInstanceId": INSTANCES[host_type], "hostVersion": profile["implementation"]["version"]},
        "io.agentbridge/host-profile": profile}
    try:
        with TestClient(fixture.central.app, base_url="http://127.0.0.1:1") as client:
            async def call_tool(name, args, **options):
                calls.append((name, dict(args)))
                response = client.post("/mcp", headers={"Authorization": "Bearer " + token,
                    "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                        "name": name, "arguments": args, "_meta": {**meta, **options.get("meta", {})}}})
                assert response.status_code == 200
                value = response.json()["result"]
                assert not value.get("isError"), value
                return SimpleNamespace(isError=False, structuredContent=value.get("structuredContent"))
            @asynccontextmanager
            async def paired(_):
                yield SimpleNamespace(call_tool=call_tool), meta, "L3"
            host = SimpleNamespace(coordinator=True, worker_payload=lambda *_: req,
                                   card_origins=frozenset({fixture.trusted_card_origin}))
            def request(route, body, **_):
                return asyncio.run(notifications(host, original.record, route, body, paired_session=paired))
            channel.pilot.gateway = SimpleNamespace(_request=request)
            channel.poll(); drain(channel.terminal)  # Prospective activation through the real MCP.
            with service.tasks._connect() as db:
                ownership = endpoint_instances(db, subject)
                source, = [dict(row) for row in db.execute("SELECT * FROM client_endpoints WHERE user_subject=? AND agent_host=? AND client_type IN ('web','webchat')", (subject, host_type))
                           if ownership.get(row["endpoint_id"]) == (host_type, INSTANCES[host_type])]
            task, _ = service.tasks.ensure_task(user_subject=subject, agent_host=host_type, host_task_key=str(uuid4()),
                origin_endpoint_id=source["endpoint_id"], active_conversation_ref=source["conversation_ref"], title="Private synthetic title")
            record, raw = login(fixture, task["task_id"], subject=subject)
            channel.poll(); drain(channel.terminal)
            assert len(wire) == 1 and len(wire[0]["reply_markup"]["inline_keyboard"][0]) == 2
            challenge = service._credential_challenge_id(record)
            csrf = service.challenges.issue_csrf(challenge)
            service.challenges.claim(challenge, csrf_token=csrf, csrf_cookie=csrf)
            service.challenges.complete(challenge, result={"principal": subject})
            service.sessions.activate(record["session_id"], observed_principal_ref=subject)
            # This is the actual trusted-interaction observer, not a forged
            # task-event insertion or a model-authored completion message.
            service._load_interaction(user_subject=subject, interaction_id=raw["interactionId"])
            channel.poll(); drain(channel.terminal)
            assert [entry["text"] for entry in wire[1:]] == [closures.text("credential", "completed")]
            assert "reply_markup" not in wire[1]
            with service.tasks._connect() as db:
                rows = db.execute("SELECT kind,state,receipt FROM telegram_notification_deliveries WHERE task_id=? ORDER BY rowid", (task["task_id"],)).fetchall()
                assert [tuple(row) for row in rows] == [("trusted_interaction", "sent", 101), ("public_status", "sent", 102)]
                assert not db.execute("SELECT 1 FROM telegram_interaction_closures c JOIN telegram_notification_deliveries d USING(delivery_id) WHERE d.subject!=?", (subject,)).fetchone()
            channel.terminal.workspace_queries.executor.shutdown()
            if channel.terminal.workspace_results: channel.terminal.workspace_results.executor.shutdown()
            channel.terminal = TerminalMessages(channel.pilot)
            for _ in range(2):
                channel.poll(); drain(channel.terminal)
            assert len(wire) == 2
            assert any(name == "agentbridge_host_notification_delivery" and args.get("action") == "begin" for name, args in calls)
    finally:
        channel.close(); api.close(); server.shutdown(); server.server_close(); thread.join(timeout=5)
