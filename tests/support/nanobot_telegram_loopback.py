"""A loopback Telegram Bot API double, with optional local NanoBot HTTP service."""

import json
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
        "deliveries": [],
        "lost_accept": False,
        "lost_delivery": False,
        "pending": False,
        "webhook": "",
        "bot": BOT,
        "verified": {"501", "502"},
    }

    class Handler(BaseHTTPRequestHandler):
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
                        "chat": {"id": body["chat_id"]},
                        "from": {"id": BOT},
                        "reply_to_message": {
                            "message_id": body["reply_parameters"]["message_id"]
                        },
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
                        "answer": "合成结果 " + binding["user_subject"],
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

    def create():
        return TelegramPilot(
            home / "messages.db",
            api=api,
            bot_id=BOT,
            instance=gateway.instance_id,
            gateway=gateway,
            bindings=lambda: bindings,
            bot_username="fixture_bot",
            allow_pairing=allow_pairing,
        )

    try:
        yield state, bindings, create
    finally:
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
