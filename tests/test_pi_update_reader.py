"""Pi can deliver while getUpdates is blocked, without a second offset writer or Bot sender."""
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import partial

import pytest

from tests.test_nanobot_telegram_chat_mirror import Api, BOT, Gateway, event
from nanobot_agentbridge.pi_telegram_updates import PiUpdateReader
from nanobot_agentbridge.telegram import TelegramAPI, TelegramPilot, TelegramUnavailable


class PiGateway(Gateway):
    host_type = "pi"


def pilot(tmp_path, read, send):
    gw = PiGateway()
    gw.binding.update(endpoint_id="test-ep", session_key="test-session", credential_ref="tg-" + "a" * 64)
    gw.events.append(event(kind="workspace_chat", role="assistant"))
    channel = TelegramPilot(tmp_path / "messages.db", api=send, bot_id=BOT, instance="test", gateway=gw,
        bindings=lambda: {}, allow_pairing=True, sender_subject=lambda chat: "first" if chat == 501 else None,
        update_reader=read)
    channel.next_workspace_images = float("inf")
    return channel


def test_single_inflight_receive_does_not_block_visible_send_and_preserves_failed_offset(tmp_path):
    replies, started, release = [], threading.Event(), threading.Event()
    owner = threading.get_ident()

    def fetch(**query):
        assert threading.get_ident() != owner
        replies.append(query["offset"])
        started.set()
        assert release.wait(4)
        return [{"update_id": index} for index in (40, 41, 42)]

    class OwnerApi(Api):
        def call(self, method, **data):
            assert threading.get_ident() == owner and method == "sendMessage"
            return super().call(method, **data)

    reader = PiUpdateReader(fetch)
    bot = OwnerApi()
    channel = pilot(tmp_path, reader, bot)
    with channel.db:
        channel.db.execute("INSERT INTO channel_meta VALUES(?,?,?)", (BOT, 40, time.time()))
    ingested = []
    def ingest(update):
        assert threading.get_ident() == owner
        ingested.append(update["update_id"])
        if update["update_id"] == 41:
            raise TelegramUnavailable("synthetic unavailable")
    channel.ingest = ingest
    try:
        channel.poll_once()
        assert started.wait(2) and len(bot.sent) == 1 and len(channel.gateway.acks) == 1
        for _ in range(3):
            channel.poll_once()
        assert replies == [40] and not ingested
        release.set()
        reader.pending.result(timeout=2)
        channel.poll_once()
        assert ingested == [40, 41, 42] and replies == [40]
        assert channel.db.execute("SELECT next_update FROM channel_meta").fetchone()[0] == 41
        channel.poll_once()
        reader.pending.result(timeout=2)
        assert replies == [40, 41] and len(bot.sent) == 1
    finally:
        release.set()
        reader.close()
        channel.db.close()


def test_getupdates_failure_has_bounded_backoff_without_suppressing_or_replaying_sends(tmp_path):
    def unavailable(**query):
        raise TelegramUnavailable("synthetic unavailable")

    reader = PiUpdateReader(unavailable)
    bot = Api()
    channel = pilot(tmp_path, reader, bot)
    try:
        channel.poll_once()
        with pytest.raises(TelegramUnavailable):
            reader.pending.result(timeout=2)
        channel.poll_once()
        channel.next_notifications = 0
        channel.poll_once()
        assert reader.pending is None and reader.retry_at > time.monotonic()
        assert len(bot.sent) == 1 and not channel.db.execute("SELECT 1 FROM channel_meta").fetchone()
        assert len(channel.gateway.acks) == 2
    finally:
        reader.close()
        channel.db.close()


def test_shutdown_interrupts_blocked_loopback_read_before_releasing_owner():
    arrived, release = threading.Event(), threading.Event()
    calls = []
    class HoldingServer(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/botfixture/getUpdates"
            calls.append(self.path)
            arrived.set()
            release.wait(6)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), HoldingServer)
    background = threading.Thread(target=server.serve_forever, daemon=True)
    background.start()
    api = TelegramAPI("fixture", lambda: HTTPConnection("127.0.0.1", server.server_port, timeout=5),
                      private_sender=lambda _: False)
    reader = PiUpdateReader(partial(api.call, "getUpdates"), interrupt=api.interrupt_updates)
    try:
        reader.poll(offset=39, timeout=1)
        assert arrived.wait(2)
        start = time.monotonic()
        reader.close()
        assert reader.pending.done() and len(calls) == 1 and time.monotonic() - start < 2
    finally:
        release.set()
        reader.close()
        api.close()
        server.shutdown()
        server.server_close()
        background.join(2)
