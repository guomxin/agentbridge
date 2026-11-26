"""Real Workspace, Pi SDK, central MCP and channel consumer with a synthetic model and Telegram transport."""

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import zlib

from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.nanobot_gateway import PiGatewayClient
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.support.interaction_acceptance import free_port
from tests.support.pi_sdk_probe import ROOT, host_fixture, private
from tests.support.pi_workspace_acceptance import central_fixture


def solid_png(rgb):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))

    width = 32
    pixels = (b"\0" + bytes(rgb) * width) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, width, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


class Model:
    def __init__(self):
        self.seen = []
        self.failures = []

    def reply(self, data):
        content = next((m["content"] for m in reversed(data["messages"]) if m["role"] == "user"), "")
        images = [
            p["image_url"]["url"] for p in content if isinstance(p, dict) and p.get("type") == "image_url"
        ]
        if not images:
            return "请附上原图。", None
        self.seen.append(
            [
                hashlib.sha256(base64.b64decode(url.split(",", 1)[1], validate=True)).hexdigest()
                for url in images
            ]
        )
        return f"本次模型收到 {len(images)} 张原图。", None


class Bot:
    def __init__(self):
        self.images = []

    def call(self, method, **params):
        if method == "getUpdates":
            return []
        if method == "getMe":
            return {"id": 8801, "is_bot": True, "username": "fixture_pi_bot"}
        if method == "getWebhookInfo":
            return {"url": ""}
        if method == "sendChatAction":
            return True
        raise AssertionError("Unexpected Telegram action " + method)

    def send_document(self, chat, name, mime, body):
        assert chat == 501 and mime == "image/png"
        self.images.append(hashlib.sha256(body).hexdigest())
        return {
            "message_id": 100 + len(self.images),
            "chat": {"type": "private", "id": chat},
            "from": {"id": 8801},
            "document": {"file_id": str(len(self.images)), "file_size": len(body)},
        }


@contextmanager
def fixture(directory, node, sdk, engine, *, notification_settings=False):
    sys.path.insert(0, str(ROOT / "integrations/nanobot-agentbridge"))
    from nanobot_agentbridge.telegram import TelegramPilot

    ref = {}

    def setup(central):
        ref["central"] = central
        if notification_settings:
            from tests.authorization_fixtures import grant_permissions
            from tests.test_workspace_watches import seed

            for number, subject in enumerate(central.subjects.values(), 1):
                grant_permissions(
                    central.service.user_grants, subject, ["oa.workflow.read", "oa.workflow.watch"]
                )
                session = central.service.sessions.get_or_create(
                    user_subject=subject, system_id="oa", expected_principal_ref=subject
                )
                central.service.sessions.activate(session["session_id"], observed_principal_ref=subject)
                central.service.session_states.save(
                    session["session_id"], {"cookies": [], "synthetic": subject}
                )
                seed(SimpleNamespace(service=central.service), subject=subject, number=number)
        with sqlite3.connect(central.service.db_path) as db:
            db.execute(
                "INSERT OR REPLACE INTO agent_host_compatibility VALUES ('pi','0.1.6','L3','isolated-web-image-test','isolated-fixture',CURRENT_TIMESTAMP)"
            )

    token = private(directory / "route.token", secrets.token_urlsafe(40))
    origin = "https://pi-image-fixture.invalid"
    prog = directory / "coordinator"
    prog.write_text(
        "#!"
        + sys.executable
        + " -I\nimport sys\nsys.path[:0]="
        + repr([str(ROOT / "integrations/reference-host"), str(ROOT)])
        + "\nsys.argv[1:]=['--host-type','pi']\nfrom reference_host.bound_coordinator import main\nmain()\n"
    )
    prog.chmod(0o700)
    port = free_port()
    bot, model = Bot(), Model()
    with central_fixture(
        directory,
        host_url=f"http://127.0.0.1:{port}",
        token_file=token,
        maximum_level="L3", configure=setup,
        central_options={"trusted_card_origin": origin},
    ) as (meta, accounts):
        subject = accounts["a"]["user_subject"]
        with host_fixture(
            directory,
            node,
            sdk,
            meta,
            accounts,
            port=port,
            token_file=token,
            engine_python=engine,
            model=model,
            options={
                "coordinatorProgram": str(prog),
                "trustedCardOrigins": [origin],
                "requiredLevel": "L3",
                "telegramPilot": {"botId": "8801", "allowedSenders": {subject: "501"}},
            },
        ) as (post, _, _, _):
            central = ref["central"]
            service = central.service
            links = TelegramPilotLinks(service)
            with service.workspace._connect() as db:
                owners = {
                    row["user_subject"]: dict(row) for row in db.execute("SELECT * FROM workspace_accounts")
                }
            args = (owners[subject]["account_id"], "pi-office", "8801")
            challenge = links.begin(*args)
            confirmation = (
                post("telegram-present", botId="8801", senderId="501", code=challenge["code"])
                .json()
                .get("confirmationCode")
            )
            assert confirmation
            links.confirm(*args, challenge["challenge_id"], confirmation)
            gateway = PiGatewayClient.from_environment()["pi-office"]
            assert gateway.readiness()["acceptedLevel"] == "L3"
            assert gateway.resolve_telegram_sender(8801, 501)["user_subject"] == subject
            stopped = threading.Event()
            failed = []

            def consume():
                pilot = TelegramPilot(
                    directory / "telegram.sqlite",
                    api=bot,
                    bot_id=8801,
                    instance="pi-office",
                    gateway=gateway,
                    bindings=lambda: {},
                    allow_pairing=True,
                    sender_subject=lambda chat: subject if chat == 501 else None,
                )
                try:
                    pilot.verify()
                    while not stopped.is_set():
                        pilot.poll_once()
                        stopped.wait(0.2)
                except Exception as exc:
                    failed.append(type(exc).__name__ + ": " + str(exc)[:80])
                finally:
                    pilot.db.close()

            thread = threading.Thread(target=consume, daemon=True)
            thread.start()
            paths = []
            for name, color in (("red", (235, 41, 41)), ("blue", (25, 91, 211))):
                p = directory / (name + ".png")
                p.write_bytes(solid_png(color))
                paths.append(p)
            try:
                yield meta, owners, gateway, service, model, bot, paths, failed
            finally:
                stopped.set()
                thread.join(20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "sdk-dir", "engine-python"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument(
        "--notification-settings",
        action="store_true",
        help="Grant both synthetic owners read/watch and seed one watch each.",
    )
    args = parser.parse_args()
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="pi-web-images-") as tmp:
        with fixture(
            Path(tmp),
            args.node.resolve(),
            args.sdk_dir.resolve(),
            args.engine_python,
            notification_settings=args.notification_settings,
        ) as (meta, owners, gateway, service, model, bot, paths, failed):
            if args.serve:
                print(
                    json.dumps(
                        {"signin": meta["signin"], "images": [str(p) for p in paths]}, ensure_ascii=False
                    ),
                    flush=True,
                )
                stopped = threading.Event()
                signal.signal(signal.SIGTERM, lambda *_: stopped.set())
                signal.signal(signal.SIGINT, lambda *_: stopped.set())
                while not stopped.wait(1):
                    if failed:
                        raise RuntimeError(failed)
                return
            account = owners[meta["subjects"]["a"]]
            app = WorkspaceApplication(service=service, gateway=None, host_routes={"pi-office": gateway})
            try:
                choice = app.hosts.set_default(account["account_id"], "pi-office", expected_revision=1)
                attachments = [
                    {
                        "fileName": p.name,
                        "mimeType": "image/png",
                        "content": base64.b64encode(p.read_bytes()).decode(),
                    }
                    for p in paths
                ]
                events = list(
                    app.send_chat_stream(
                        account,
                        message="请数一下原图",
                        attachments=attachments,
                        idempotency_key="two-real-images",
                        instance_id="pi-office",
                        expected_revision=choice["revision"],
                    )
                )
                assert any(e.get("state") == "final" and "2 张" in e.get("text", "") for e in events), [
                    (e.get("state"), e.get("text")) for e in events
                ]
                expected = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
                deadline = time.monotonic() + 60
                while len(bot.images) < 2 and time.monotonic() < deadline and not failed:
                    time.sleep(0.25)
                assert not failed and bot.images == expected and model.seen == [expected], (
                    failed,
                    len(bot.images),
                    len(model.seen),
                )
                with service.workspace._connect() as db:
                    for _ in range(150):
                        states = [
                            r[0]
                            for r in db.execute(
                                "SELECT state FROM pi_workspace_image_deliveries ORDER BY ordinal"
                            )
                        ]
                        if states == ["sent", "sent"]:
                            break
                        time.sleep(0.1)
                assert states == ["sent", "sent"]
                print(
                    json.dumps(
                        {
                            "passed": True,
                            "sdk": "1.6.0",
                            "acceptedLevel": "L3",
                            "modelReceived": 2,
                            "originalUploads": 2,
                            "privateReceipts": 2,
                            "syntheticModel": True,
                            "syntheticTelegram": True,
                        }
                    ),
                    flush=True,
                )
            finally:
                app.close()


if __name__ == "__main__":
    main()
