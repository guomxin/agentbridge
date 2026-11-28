"""Native original presentation -> private state-only MCP -> actual Bot HTTP close."""
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

from agentbridge.core.telegram_interaction_closures import text
from tests.support.workspace_query_acceptance import Fixture, INSTANCES
from tests.support.workspace_query_channel_probe import Channel
from tests.test_telegram_interaction_closures import login
from nanobot_agentbridge.native_card_state import remember
from nanobot_agentbridge.telegram_tasks import navigate
from nanobot_agentbridge.telegram import TelegramAPI, TelegramPilot


@pytest.mark.parametrize("host_type", ["nanobot", "pi"])
@pytest.mark.parametrize("level,standalone", [("L3",False),("L3",True),("L2",True)])
def test_native_actual_private_mcp_and_bot_close_are_distinct_from_companion(tmp_path, monkeypatch, host_type, level, standalone):
    for name in ("AGENTBRIDGE_SESSION_KEY_FILE", "AGENTBRIDGE_NANOBOT_ROUTES_FILE", "AGENTBRIDGE_PI_ROUTES_FILE"):
        monkeypatch.setenv(name, "fixture-will-replace")
    fixture = Fixture(tmp_path / "central", deliveries=False)
    server = channel = pilot = None
    wire, calls = [], []
    try:
        bot, sender = map(int,fixture.channel_identities[host_type])
        class Bot(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def do_POST(self):
                assert self.path.endswith("/sendMessage")
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert body["chat_id"] == sender
                wire.append(body)
                data = json.dumps({"ok":True,"result":{"message_id":100+len(wire),"from":{"id":bot},
                    "chat":{"id":sender,"type":"private"},"reply_to_message":{"message_id":body["reply_parameters"]["message_id"]}}}).encode()
                self.send_response(200);self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
        server = ThreadingHTTPServer(("127.0.0.1",0),Bot)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        api = TelegramAPI("synthetic",connection_factory=lambda:HTTPConnection(*server.server_address,timeout=5))
        channel = Channel(fixture,host_type,api)
        original = channel.pilot.gateway
        req = {**original.req,"instance":INSTANCES[host_type],"hostType":host_type}
        record = original.record
        token = Path(fixture.token_files[host_type,fixture.owner]).read_text().strip()
        profile = json.loads((Path(__file__).parents[1]/"schemas/agent-host/v1/test-vectors.json").read_text())["profiles"][0]["value"]
        from agentbridge.core.workspace_query_delivery import VERSIONS
        version = VERSIONS[host_type]
        profile.update(implementation={"name":host_type,"version":version},hostInstanceId=INSTANCES[host_type])
        if level == "L2": profile["levels"] = ["L1","L2"]
        meta = {"io.agentbridge/host-profile":profile,"io.agentbridge/host-context":{"version":"1","agentHost":host_type,
                "hostInstanceId":INSTANCES[host_type],"hostVersion":version},"io.agentbridge/telegram-pairing-version":2}
        with TestClient(fixture.central.app,base_url="http://127.0.0.1:1") as client:
            async def call_tool(name,args,**options):
                calls.append((name,args))
                response = client.post("/mcp",headers={"Authorization":"Bearer "+token,"Accept":"application/json, text/event-stream",
                    "MCP-Protocol-Version":"2025-06-18"},json={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
                    "name":name,"arguments":args,"_meta":{**meta,**options.get("meta",{})}}})
                assert response.status_code == 200
                result = response.json()["result"]
                assert not result.get("isError"), result
                return SimpleNamespace(isError=False,structuredContent=result.get("structuredContent"))
            class Host:
                instance = INSTANCES[host_type]
                runs = SimpleNamespace(lookup=lambda *_,**__: {"run_id":"native-run"})
                worker_payload = lambda self,*_: req
                @asynccontextmanager
                async def private_session(self,_):
                    yield SimpleNamespace(call_tool=call_tool),meta,level
            host = Host()
            svc = fixture.service
            task = None
            if standalone:
                session = svc.sessions.find(user_subject=fixture.owner,system_id="oa")
                svc.sessions.mark_expired(session["session_id"],"synthetic expiry")
                raw = svc.start_login(user_subject=fixture.owner,expected_principal_ref=fixture.owner,
                    card_base_url=fixture.trusted_card_origin,system_id="oa")["interaction"]
                credential = svc.interactions.get(raw["interactionId"],user_subject=fixture.owner)
            else:
                task,_ = svc.tasks.ensure_task(user_subject=fixture.owner,agent_host=host_type,host_task_key=str(uuid4()),
                    origin_endpoint_id=req["endpoint"],active_conversation_ref=req["conversation"],title="Synthetic native query")
                credential,raw = login(fixture,task["task_id"],subject=fixture.owner)
            presented = asyncio.run(call_tool("agentbridge_host_interaction_present",{
                "agent_host":host_type,"endpoint_key":req["endpointKey"],"interaction_id":raw["interactionId"]})).structuredContent["interaction"]
            pinned = remember(host,record,presented,run="native-run",request_key=f"tg:{bot}:{sender}:9")
            class Gateway:
                def verify_telegram_binding(self,_): return channel.pilot._binding(sender)[0] is not None
                def private_card(self,*_): return {"available":True,"interaction":presented,"nativeCardSource":pinned}
                def _request(self,route,body,**_):
                    assert route == "telegram-task"
                    return asyncio.run(navigate(host,record,body))
            gateway = Gateway();gateway.host_type = host_type
            pilot = TelegramPilot(tmp_path/"native.db",api=api,bot_id=bot,instance=INSTANCES[host_type],gateway=gateway,
                bindings=lambda:{str(sender):channel.binding},sender_subject=lambda n:fixture.owner if n==sender else None)
            binding,digest = pilot._binding(sender)
            pilot._deliver_active_card(sender,9,binding,digest,f"tg:{bot}:{sender}:9","native-run")
            assert len(wire)==1 and len(wire[0]["reply_markup"]["inline_keyboard"][0])==2
            challenge = svc._credential_challenge_id(credential)
            csrf = svc.challenges.issue_csrf(challenge)
            svc.challenges.claim(challenge,csrf_token=csrf,csrf_cookie=csrf)
            svc.challenges.complete(challenge,result={"principal":fixture.owner})
            svc.sessions.activate(credential["session_id"],observed_principal_ref=fixture.owner)
            before = svc.tasks.get_task(task["task_id"],user_subject=fixture.owner) if task else None
            pilot.reconcile()
            assert [v["text"] for v in wire[1:]] == [text("credential","completed")]
            assert wire[1]["reply_parameters"]["message_id"]==101
            if task: assert svc.tasks.get_task(task["task_id"],user_subject=fixture.owner)==before
            assert pilot.db.execute("SELECT closed_state,state,receipt FROM telegram_native_card_closures").fetchone()[:]==("completed","delivered",102)
            with svc.tasks._connect() as db:
                assert not db.execute("SELECT 1 FROM telegram_interaction_closures").fetchone()
            pilot.native_card_closures.next_poll=0;pilot.reconcile()
            assert len(wire)==2
            assert any(name=="agentbridge_host_interaction_present" and args.get("state_only") for name,args in calls)
            assert not any(name in {"agentbridge_interaction_get","agentbridge_interaction_resume","agentbridge_host_task_snapshot"} for name,_ in calls)
    finally:
        if pilot: pilot.db.close()
        if channel: channel.close()
        if server: server.shutdown();server.server_close()
        fixture.close()
