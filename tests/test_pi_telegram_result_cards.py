"""Pi-only Telegram result delivery across network failure and lost process state."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentbridge.workspace.gateway import GatewayRequestError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/nanobot-agentbridge"))
from nanobot_agentbridge import telegram_result_cards
from nanobot_agentbridge.telegram import TelegramPilot


class PowerLoss(BaseException):
    pass


class Gateway:
    host_type = "pi"
    result_calls = 0
    timeout = False
    permitted = True
    expired = False

    def verify_telegram_binding(self, binding):
        return True

    def dispatch_evidence(self, request, binding, run):
        return {
            "prompt_observed": True,
            "observed_run_id": run,
            "state": "completed",
            "final_text": "已读取本人的待办。",
            "has_result_view": True,
        }

    def result_view(self, request, binding, run):
        self.result_calls += 1
        assert request == {"idempotency_key": "tg:8801:501:33"} and run == "original-run"
        if self.timeout:
            raise GatewayRequestError("GATEWAY_CONNECTION_CLOSED", "synthetic timeout")
        if not self.permitted:
            return {"available": False}
        expiry = datetime.now(timezone.utc) + timedelta(minutes=-1 if self.expired else 5)
        return {
            "available": True,
            "count": 1,
            "complete": True,
            "resultView": {
                "sourceTaskId": "original-task",
                "url": "https://trusted.example/results/private-fixture",
                "sourceExpiresAt": expiry.isoformat(),
                "selectionAvailable": True,
            },
        }

    def publish_telegram_timeline(self, *args):
        pass


class Bot:
    def __init__(self):
        self.cards = 0
        self.replies = 0
        self.crash = False

    def call(self, method, **args):
        assert method == "sendMessage"
        if args.get("reply_markup"):
            self.cards += 1
            if self.crash:
                raise PowerLoss()
        else:
            self.replies += 1
        return {
            "from": {"id": 8801},
            "chat": {"id": 501, "type": "private"},
            "message_id": self.cards + self.replies + 100,
            "reply_to_message": {"message_id": 33},
        }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=datetime.now(timezone.utc).timestamp())
    monkeypatch.setattr(telegram_result_cards, "time", SimpleNamespace(time=lambda: clock.now))
    gateway, bot = Gateway(), Bot()
    bindings = {"501": {"user_subject": "first", "endpoint_id": "tg-first"}}
    opened = []

    def start():
        if opened:
            opened[-1].db.close()
        channel = TelegramPilot(
            tmp_path / "bot.sqlite",
            api=bot,
            bot_id=8801,
            instance="pi-office",
            gateway=gateway,
            bindings=lambda: bindings,
            sender_subject=lambda chat: "first" if chat == 501 else None,
        )
        opened.append(channel)
        return channel

    channel = start()
    _, digest = channel._binding(501)
    with channel.db:
        channel.db.execute(
            "INSERT INTO messages VALUES(8801,501,33,'first',?,'working','original-run',NULL)", (digest,)
        )
    yield channel, start, gateway, bot, bindings, clock
    opened[-1].db.close()


def state(channel):
    return channel.db.execute("SELECT state FROM pi_result_card_requests").fetchone()[0]


def test_timeout_recovers_after_restart_without_replaying_ordinary_reply(setup):
    channel, start, gateway, bot, _, clock = setup
    gateway.timeout = True
    channel.reconcile()
    assert (bot.replies, bot.cards, gateway.result_calls, state(channel)) == (1, 0, 1, "pending")
    assert (
        channel.db.execute("SELECT last_error FROM pi_result_card_requests").fetchone()[0]
        == "GATEWAY_CONNECTION_CLOSED"
    )
    channel = start()
    gateway.timeout = False
    channel.reconcile()
    assert gateway.result_calls == 1
    clock.now += 5
    channel.reconcile()
    assert (bot.replies, bot.cards, gateway.result_calls, state(channel)) == (1, 1, 2, "handled")
    channel = start()
    channel.reconcile()
    assert gateway.result_calls == 2 and bot.cards == 1
    assert "private-fixture" not in "\n".join(channel.db.iterdump())


@pytest.mark.parametrize(
    "stop", ["before_reply", "before_issue", "after_issue", "while_sending", "after_send"]
)
def test_intent_survives_power_cut_at_each_boundary(setup, monkeypatch, stop):
    channel, start, gateway, bot, _, clock = setup

    def lose_power(*args, **kwargs):
        raise PowerLoss()

    if stop == "before_reply":
        monkeypatch.setattr(channel, "deliver", lose_power)
    if stop == "before_issue":
        monkeypatch.setattr(channel.result_cards, "flush_one", lose_power)
    if stop == "after_issue":
        monkeypatch.setattr(channel, "_send_result_view", lose_power)
    if stop == "while_sending":
        bot.crash = True
    if stop == "after_send":
        send = channel._send_result_view

        def sent_then_lost(*args, **kwargs):
            send(*args, **kwargs)
            raise PowerLoss()

        monkeypatch.setattr(channel, "_send_result_view", sent_then_lost)
    with pytest.raises(PowerLoss):
        channel.reconcile()
    assert bot.replies == (0 if stop == "before_reply" else 1)
    channel = start()
    bot.crash = False
    clock.now += 10
    channel.reconcile()
    channel.reconcile()
    status = channel.db.execute("SELECT state FROM telegram_card_deliveries").fetchone()[0]
    assert status == ("delivery_unknown" if stop == "while_sending" else "delivered")
    assert (state(channel), bot.cards, bot.replies) == ("handled", 1, 1)
    assert gateway.result_calls == (2 if stop == "after_issue" else 1)


@pytest.mark.parametrize("cause", ["permission", "binding", "expired_source", "expired_job", "retry_limit"])
def test_closed_requests_never_send_or_repeat_business(setup, cause):
    channel, start, gateway, bot, bindings, clock = setup
    gateway.timeout = True
    channel.reconcile()
    channel = start()
    gateway.timeout = False
    if cause == "permission":
        gateway.permitted = False
    elif cause == "binding":
        bindings["501"]["endpoint_id"] = "replacement"
    elif cause == "expired_source":
        gateway.expired = True
    elif cause == "expired_job":
        clock.now += channel.result_cards.WINDOW
    else:
        gateway.timeout = True
    for seconds in (5, 20, 60, 60):
        clock.now += seconds
        channel.reconcile()
    expected = {
        "permission": "unavailable",
        "binding": "binding_changed",
        "expired_source": "expired",
        "expired_job": "expired",
        "retry_limit": "exhausted",
    }
    assert state(channel) == expected[cause]
    assert bot.replies == 1 and bot.cards == 0
    assert (
        gateway.result_calls
        == {"permission": 2, "binding": 1, "expired_source": 2, "expired_job": 1, "retry_limit": 4}[cause]
    )


def test_binding_revoked_while_original_result_is_being_issued(setup, monkeypatch):
    channel, _, gateway, bot, bindings, _ = setup
    issue = gateway.result_view

    def revoked_while_issuing(*args):
        result = issue(*args)
        bindings.clear()
        return result

    monkeypatch.setattr(gateway, "result_view", revoked_while_issuing)
    channel.reconcile()
    assert state(channel) == "binding_changed"
    assert channel.db.execute("SELECT COUNT(*) FROM telegram_card_deliveries").fetchone()[0] == 0
    assert bot.cards == 0 and bot.replies == 1
