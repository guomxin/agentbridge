"""A loopback Telegram Bot API double, with optional local NanoBot HTTP service."""

import json
from datetime import datetime,timedelta
import secrets
from uuid import uuid4
import sys
import threading
import time
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "integrations/nanobot-agentbridge")
)
from nanobot_agentbridge.telegram import TelegramAPI, TelegramPilot

BOT = 8001


def update(number, who=501, message=9, **extra):
    return {
        "update_id": number,
        "message": {
            "message_id": message,
            "date": int(time.time()),
            "chat": {"type": "private", "id": who},
            "from": {"id": who, "is_bot": False},
            "text": "查询我的待办",
            **extra,
        },
    }


@contextmanager
def fixture(home, *, actual_gateway=None, actual_bindings=None, allow_pairing=False):
    bindings = {
        str(uid): {
            "user_subject": subject,
            "endpoint_id": "test-endpoint-" + str(uid),
            "endpoint_key": f"telegram:{BOT}:{uid}",
            "session_key": "test-chat-" + str(uid),
            "credential_ref": "test-credential-" + subject,
        }
        for uid, subject in ((501, "alpha"), (502, "beta"))
    }
    if actual_bindings is not None:
        bindings = actual_bindings
    token_file = home / "gateway-token"
    token_file.touch(mode=0o600)
    token_file.write_text("local-only-synthetic-token-for-test")
    state = {
        "updates": [],
        "calls": [],
        "admissions": {},
        "timeline": {},
        "media_sources": {},
        "media_receipts": {},
        "media_metadata": {},
        "deliveries": [],
        "files": {},
        "downloads": [],
        "lost_accept": False,
        "lost_delivery": False,
        "pending": False,
        "webhook": "",
        "bot": BOT,
        "verified": {"501", "502"},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            item = next((f for f in state["files"].values()
                         if self.path == "/file/bottest-token/" + f["file_path"]), None)
            if item is None:
                self.send_error(404)
                return
            state["downloads"].append(self.path)
            self.send_response(item.get("status", 200))
            self.send_header("Content-Length", str(len(item["bytes"])))
            self.end_headers()
            self.wfile.write(item["bytes"])

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append((self.path, body))
            if self.path.startswith("/bottest-token/"):
                method = self.path.split("/")[-1]
                if method == "getMe":
                    result = {
                        "id": state["bot"],
                        "is_bot": True,
                        "username": "fixture_bot",
                    }
                elif method == "getWebhookInfo":
                    result = {"url": state["webhook"]}
                elif method == "getFile":
                    item = state["files"].get(body["file_id"])
                    if item is None:
                        self.send_error(404)
                        return
                    result = {"file_id": body["file_id"], "file_size": len(item["bytes"]), "file_path": item["file_path"]}
                elif method == "getUpdates":
                    assert body["allowed_updates"] == ["message"]
                    result = [
                        item
                        for item in state["updates"]
                        if item["update_id"] >= body["offset"]
                    ]
                elif method == "sendMessage":
                    state["deliveries"].append(body)
                    if state["lost_delivery"] or (
                        "lost_delivery_filter" in state
                        and state["lost_delivery_filter"](body)
                    ):
                        self.close_connection = True
                        return
                    result = {
                        "message_id": 700 + len(state["deliveries"]),
                        "chat": {"id": body["chat_id"], "type": "private"},
                        "from": {"id": BOT},
                        **({"reply_to_message": {"message_id": body["reply_parameters"]["message_id"]}}
                           if body.get("reply_parameters") else {}),
                    }
                else:
                    self.send_error(404)
                    return
                answer = {"ok": True, "result": result}
            else:
                if (
                    self.headers.get("Authorization")
                    != "Bearer " + token_file.read_text()
                ):
                    self.send_error(403)
                    return
                assert body["instance"] == "nanobot-pilot"
                if self.path == "/v1/ready":
                    raw = json.dumps({"instance": "nanobot-pilot", "available": True, "capabilities": ["chat", "attachments"]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                binding, key = body["binding"], body.get("requestKey")
                if binding not in bindings.values():
                    self.send_error(403)
                    return
                if self.path == "/v1/telegram-binding":
                    if binding["endpoint_key"].split(":")[-1] in state.get(
                        "identity_unavailable", set()
                    ):
                        self.send_error(503)
                        return
                    answer = {
                        "verified": binding["endpoint_key"].split(":")[-1]
                        in state["verified"]
                    }
                elif self.path == "/v1/telegram-timeline":
                    key = (binding["endpoint_id"], body["messageId"], body["role"])
                    state["timeline"].setdefault(key, body["text"])
                    if state.get("lost_timeline"):
                        self.close_connection = True
                        return
                    answer = {"recorded": True}
                elif self.path == "/v1/telegram-media-source":
                    key=(binding["endpoint_id"],body["messageId"])
                    source={"recorded":True,"pairingRef":"1"*64,"receivedAt":body["receivedAt"]}
                    if key in state["media_sources"] and state["media_sources"][key]!=source:
                        self.send_error(403);return
                    state["media_sources"][key]=source
                    answer=dict(source)
                elif self.path == "/v1/telegram-media-append":
                    key=(binding["endpoint_id"],body["messageId"],body["ordinal"])
                    metadata={k:body.get(k) for k in ("fileName","mimeType","byteSize","sha256","receivedAt","failure")}
                    if key in state["media_metadata"] and state["media_metadata"][key]!=metadata:
                        self.send_error(409);return
                    if key not in state["media_receipts"] and body.get("contentBase64") is None and not body.get("failure"):
                        answer={"recorded":False}
                    else:
                        state["media_metadata"][key]=metadata
                        state["media_receipts"].setdefault(key,{"sourceEndpointId":key[0],"sourceMessageId":key[1],"ordinal":key[2],
                            **metadata,"entryId":str(uuid4()),"attachmentId":None if body.get("failure") else secrets.token_urlsafe(32),
                            "state":"unavailable" if body.get("failure") else "stored",
                            "expiresAt":(datetime.fromisoformat(body["receivedAt"])+timedelta(days=7)).isoformat()})
                        if state.get("lost_media_receipt"):
                            self.close_connection=True;return
                        answer={"recorded":True,"mediaReceipt":state["media_receipts"][key]}
                elif self.path == "/v1/accept":
                    if key in state["admissions"]:
                        self.send_error(409)
                        return
                    run = "run-" + str(len(state["admissions"]) + 1)
                    state["admissions"][key] = (binding, run)
                    if state["lost_accept"]:
                        self.close_connection = True
                        return
                    answer = {"runId": run}
                elif self.path == "/v1/lookup":
                    matched = state["admissions"].get(key)
                    assert matched is not None and matched[0] == binding
                    answer = {
                        "runId": matched[1],
                        "state": "accepted" if state["pending"] else "completed",
                        "answer": state.get("final_answer", "合成结果 " + binding["user_subject"]),
                    }
                else:
                    self.send_error(404)
                    return
                answer["instance"] = "nanobot-pilot"
            raw = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api = TelegramAPI(
        "test-token", lambda: HTTPConnection("127.0.0.1", port, timeout=1)
    )
    gateway = actual_gateway or NanoBotGatewayClient(
        url=f"http://127.0.0.1:{port}",
        token_file=token_file,
        instance_id="nanobot-pilot",
    )

    pilots=[]
    def create():
        pilot=TelegramPilot(
            home / "messages.db",
            api=api,
            bot_id=BOT,
            instance=gateway.instance_id,
            gateway=gateway,
            bindings=lambda: bindings,
            bot_username="fixture_bot",
            allow_pairing=allow_pairing,
        )
        pilots.append(pilot)
        return pilot

    try:
        yield state, bindings, create
    finally:
        for pilot in pilots:
            pilot.media_projection.close()
        server.shutdown()
        thread.join()
        server.server_close()


def exercise_real_host(home, gateway, authorized_bindings, question):
    """Replay real Bot API JSON with a real host client, SDK, and central MCP."""
    by_sender = {
        str(uid): binding
        for uid, binding in zip((501, 502), authorized_bindings, strict=True)
    }
    with fixture(home, actual_gateway=gateway, actual_bindings=by_sender) as (
        state,
        _,
        create,
    ):
        pilot = create()
        try:
            pilot.verify()
            state["updates"] = [
                update(1, 501, text=question),
                update(2, 502, text=question),
            ]
            pilot.poll_once()
        finally:
            pilot.db.close()
        # Same two messages under fresh update envelopes after a consumer restart.
        pilot = create()
        try:
            state["updates"] = [
                update(3, 501, text=question),
                update(4, 502, text=question),
            ]
            deadline = time.monotonic() + 60
            while len(state["deliveries"]) < 2 and time.monotonic() < deadline:
                pilot.poll_once()
                time.sleep(0.1)
            answers = {reply["chat_id"]: reply["text"] for reply in state["deliveries"]}
            assert len(state["deliveries"]) == len(answers) == 2
            assert (
                "采购申请" in answers[501]
                and "验收申请" in answers[501]
                and "未获" in answers[502]
            )
            assert all(
                x["reply_parameters"]
                == {"message_id": 9, "allow_sending_without_reply": False}
                for x in state["deliveries"]
            )
            for sender in (501, 502):
                assert pilot._record(sender, 9)["state"] == "delivered"
            return {
                "telegram_remote": "simulated loopback Bot API",
                "host": "real NanoBot HTTP + SDK",
                "central": "real MCP with synthetic OA",
                "admitted_chats": 2,
                "receipt_count": len(answers),
                "replayed_messages": 2,
                "users_separated": True,
                "answers": answers,
            }
        finally:
            pilot.db.close()
