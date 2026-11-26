"""Exercise the original Pi private-chat query through the SDK, central issuer and Bot API consumer."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import tempfile
from threading import Event
from time import monotonic, sleep
from urllib.parse import urlsplit
from uuid import uuid4

from agentbridge.core.telegram_result_views import ResultViewService
from agentbridge.workspace.nanobot_gateway import PiGatewayClient
from tests.authorization_fixtures import grant_permissions
from tests.support.pi_catalog_history_probe import packet, tool
from tests.support.pi_governed_probe import ORIGIN, exercise
from tests.support.pi_notification_failure import pair_before_run
from tests.support.pi_sdk_probe import ROOT, host_fixture, private
from tests.support.pi_workspace_images_probe import Bot


class ReadModel:
    def __init__(self):
        self.failures = []
        self.calls = 0
        self.arguments = {}
        self.result = None

    def reply(self, body):
        self.calls += 1
        assert ORIGIN not in json.dumps(body)
        if "oa_workflow_pending_list" not in {entry["function"]["name"] for entry in body.get("tools", [])}:
            return "本人没有待办查询权限。", None
        if body["messages"][-1]["role"] == "tool":
            self.result = packet(body["messages"][-1]["content"])
            assert self.result["status"] == "succeeded"
            return "已读取本人的待办。", None
        return tool("oa_workflow_pending_list", self.arguments)


class ReceiptBot(Bot):
    def __init__(self):
        super().__init__()
        self.messages = []

    def call(self, method, **params):
        if method != "sendMessage":
            return super().call(method, **params)
        assert params["chat_id"] == 501
        self.messages.append(params)
        receipt = {"message_id": 200 + len(self.messages), "chat": {"id": 501, "type": "private"}, "from": {"id": 8801}}
        if params.get("reply_parameters"):
            receipt["reply_to_message"] = {"message_id": params["reply_parameters"]["message_id"]}
        return receipt


def enable_result_skill(central):
    svc, owner = central.service, "user:" + central.subjects["a"]
    settings = svc.skills.config(owner)
    svc.skills.save(owner, {**settings["value"], "oa-pending": {"profiles": ["single", "batch"]}},
                    expected_revision=settings["revision"], actor="fixture", reason="Synthetic result selection")


@contextmanager
def desktop_fixture(directory, args, owners, desktop):
    """Use the reviewed subject labels with new credentials, a read-only central and synthetic OA data."""
    from tests.support.interaction_acceptance import free_port
    from tests.support.pi_workspace_acceptance import central_fixture
    sys.path.insert(0, str(ROOT / "integrations/pi-agentbridge"))
    from pi_agentbridge import VERSION

    def configure(central):
        with central.service.tasks._connect() as db:
            db.execute("INSERT OR REPLACE INTO agent_host_compatibility VALUES ('pi',?,'L3','isolated-result-view','fixture',CURRENT_TIMESTAMP)", (VERSION,))
        grant_permissions(central.service.user_grants, owners["a"], ["oa.workflow.read", "oa.attendance_confirmation.approve"])
        enable_result_skill(central)

        def read(capability, worker, arguments):
            if capability != "oa.workflow.pending.list" or worker.session["user_subject"] != owners["a"]:
                raise PermissionError("Only the first isolated subject's synthetic pending read is eligible")
            row = {"affair_id": "synthetic-result", "title": "【HR】月度考勤确认单-合成申请人",
                   "sender": "合成申请人", "category": "协同", "date": "2026-11-24"}
            return {"items": [row], "coverage": {"status": "complete"}}
        central.service.adapter.invoke_capability = read

    program = directory / "coordinator"
    program.write_text("#!" + sys.executable + " -I\nimport sys\nsys.path[:0]="
                       + repr([str(ROOT / "integrations/reference-host"), str(ROOT)])
                       + "\nsys.argv[1:]=['--host-type','pi']\nfrom reference_host.bound_coordinator import main\nmain()\n")
    program.chmod(0o700)
    port = free_port()
    credential = private(directory / "route.token", secrets.token_urlsafe(40))
    with central_fixture(directory, host_url=f"http://127.0.0.1:{port}", token_file=credential,
                         central_options={"subjects": owners, "trusted_card_origin": ORIGIN}, configure=configure,
                         desk_bot=desktop.central_bot, maximum_level="L3") as (meta, accounts):
        assert {label: binding["user_subject"] for label, binding in accounts.items()} == owners
        with host_fixture(directory, args.node, args.sdk_dir, meta, accounts, port=port, token_file=credential,
                          model=ReadModel(), engine_python=args.engine_python,
                          options={**desktop.pilot(accounts), "requiredLevel":"L3", "trustedCardOrigins":[ORIGIN],
                                   "coordinatorProgram":str(program)}) as (post, model, _, _):
            yield meta, accounts, post, model, port, credential


class ResultViews:
    @staticmethod
    def model(central):
        central.matrix.oa.add_affair("pi-synthetic-result", profile="attendance_confirmation")
        enable_result_skill(central)
        return ReadModel()

    def check(self, central, post, model, accounts):
        sys.path.insert(0, str(ROOT / "integrations/nanobot-agentbridge"))
        from nanobot_agentbridge.telegram import TelegramPilot

        pair_before_run(central, post, accounts["a"])
        svc = central.service
        gateway = PiGatewayClient.from_environment()["pi-office"]
        bot = ReceiptBot()
        channel = TelegramPilot(svc.home.parent / "bot.sqlite", api=bot, bot_id=8801, instance="pi-office",
                                gateway=gateway, bindings=dict, allow_pairing=True,
                                sender_subject=lambda chat: central.subjects["a"] if chat == 501 else None)
        try:
            tg = gateway.resolve_telegram_sender(8801, 501)
            assert tg and post("telegram-route", botId="8801", senderId="502").status_code == 403
            def read(message_id, arguments, expected, partial=False):
                model.arguments = arguments
                update = {"message": {"message_id":message_id, "chat":{"type":"private","id":501},
                                      "from":{"id":501,"is_bot":False}, "text":"请查阅本人的办公待办。"}}
                before = len(bot.messages)
                channel.ingest(update)
                row = channel._record(501, message_id)
                run, key = row["run_id"], channel._key(501, message_id)
                request = {"binding":tg, "requestKey":key}
                deadline = monotonic() + 45
                while monotonic() < deadline:
                    row = post("lookup", **request).json()
                    if row["state"] != "accepted":
                        break
                    sleep(.1)
                assert row["state"] == "completed" and not model.failures, (row["state"], model.failures)
                result = model.result["result"]
                assert bool(result["items"]) == expected and result["coverage"]["status"] == ("partial" if partial else "complete")
                assert row["hasResultView"] == expected, (row["hasResultView"], list(model.result))
                assert post("events", **request, runId=run).json()["hasResultView"] == expected
                # Deliberately wait for completion before the first consumer poll.
                channel.reconcile()
                channel.ingest(update)
                channel.reconcile()
                deliveries = bot.messages[before:]
                assert sum(item["text"] == "已读取本人的待办。" for item in deliveries) == 1
                assert channel._record(501, message_id)["state"] == "delivered"
                cards = [item for item in deliveries if item.get("reply_markup")]
                assert len(cards) == int(expected and not partial), (len(cards), len(deliveries))
                assert post("result-view", binding=accounts["b"], requestKey=key, runId=run).status_code == 403
                assert post("result-view", binding=accounts["a"], requestKey=key, runId=run).status_code == 403
                if cards:
                    buttons = [b for line in cards[0]["reply_markup"]["inline_keyboard"] for b in line]
                    assert len(buttons) == 2 and buttons[0]["web_app"]["url"] == buttons[1]["url"]
                    origin = urlsplit(buttons[1]["url"])
                    assert "https://" + origin.netloc == ORIGIN and origin.path.startswith("/results/")
                    assert str(len(result["items"])) in cards[0]["text"]
                else:
                    assert post("result-view", **request, runId=run).json()["available"] is False
                return len(cards)

            issued = read(11, {}, True)
            key = {"binding": tg, "requestKey": channel._key(501, 11), "runId": channel._record(501, 11)["run_id"]}
            first_view = next(item for item in bot.messages if item.get("reply_markup"))
            ticket = first_view["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"].rsplit("/", 1)[1]
            views = ResultViewService(svc)
            assert views.read(ticket)["selectionAvailable"]
            queued = views.refresh(ticket, {"requestKey": uuid4().hex})
            before = len(bot.messages)
            deadline = monotonic() + 45
            _, digest = channel._binding(501)
            while monotonic() < deadline:
                channel.terminal.poll(501, tg, digest)
                if any(item.get("reply_markup") for item in bot.messages[before:]):
                    break
                sleep(.1)
            channel.terminal.poll(501, tg, digest)
            fresh = [item for item in bot.messages[before:] if item.get("reply_markup")]
            assert len(fresh) == 1 and model.calls == 2
            assert views.query_status(ticket, queued["taskId"])["state"] == "succeeded"
            fresh_ticket = fresh[0]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"].rsplit("/", 1)[1]
            assert fresh_ticket != ticket and views.read(fresh_ticket)["selectionAvailable"]
            empty = read(12, {"keyword":"未命中合成关键字"}, False)
            central.matrix.oa.arm("pending_partial")
            partial = read(13, {}, True, partial=True)
            grant_permissions(svc.user_grants, central.subjects["a"], [])
            assert post("result-view", **key).json()["available"] is False
            assert not central.matrix.oa.writes and not central.matrix.matrix["effects"]
            return {"completePrivateCards":issued, "refreshedPrivateCards":len(fresh), "emptyCards":empty,
                    "partialActionCards":partial, "permissionLossRejected":True,
                    "sameRunReconciled":True, "otherEndpointsRejected":True, "modelCalls":model.calls, "writes":0}
        finally:
            channel.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("--node", "--sdk-dir", "--engine-python"):
        parser.add_argument(flag, required=True, type=Path)
    parser.add_argument("--serve-desktop", action="store_true")
    parser.add_argument("--preparation", type=Path)
    args = parser.parse_args()
    if args.serve_desktop != bool(args.preparation):
        parser.error("--serve-desktop and --preparation are used together")
    os.umask(0o077)
    if args.serve_desktop:
        from tests.support.pi_telegram_desktop_acceptance import DesktopChannel, exclusive_channel, prepared_channel
        tg, sender, owners = prepared_channel(args.preparation.resolve())
        desktop = DesktopChannel(tg, sender, args.engine_python)
        stop = Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        with exclusive_channel(args.preparation.resolve(), tg), tempfile.TemporaryDirectory(prefix="pi-results-") as tmp:
            directory = Path(tmp).resolve()
            with desktop_fixture(directory, args, owners, desktop) as (meta, accounts, _, _, port, credential):
                with desktop.consumer(directory, port, credential, accounts) as check:
                    print(json.dumps({"signin":meta["signin"], "fixture":str(directory)}), flush=True)
                    while not stop.wait(.5):
                        check()
    else:
        print(json.dumps(exercise(args.node, args.sdk_dir, args.engine_python, "result-views", scenario=ResultViews())))
