"""Exercise the private Pi consumer at its receipt and live authorization boundaries."""

import json
from types import SimpleNamespace

import pytest

from tests.support.nanobot_telegram_loopback import BOT, fixture, update
from nanobot_agentbridge.pi_channel import private_sender_policy
from nanobot_agentbridge.telegram import TelegramPilot


def policy(tmp_path):
    filename = tmp_path / "pi-channel.json"
    settings = {"enabled": True, "preparationOnly": False, "allowWorkspacePairing": True,
                "allowedSenders": {"alpha": "501"}, "bindings": {}}
    filename.touch(mode=0o600)
    filename.write_text(json.dumps(settings))
    return filename, settings, private_sender_policy(filename, settings)


def test_policy_requires_one_sender_and_current_configuration(tmp_path):
    path, settings, check = policy(tmp_path)
    assert check(501) == "alpha" and check(502) is None
    for delta in ({"enabled": False}, {"preparationOnly": True}, {"allowWorkspacePairing": False},
                  {"allowedSenders": {}}, {"allowedSenders": {"alpha": "501", "beta": "502"}},
                  {"allowedSenders": {"alpha": "wrong"}}, {"bindings": {"501": {}}}):
        with pytest.raises(ValueError):
            private_sender_policy(path, {**settings, **delta})
    path.write_text(json.dumps({**settings, "allowedSenders": {"beta": "501"}}))
    assert check(501) is None
    path.unlink()
    assert check(501) is None


def test_private_pi_rejects_before_download_pairing_or_dispatch(tmp_path):
    path, settings, allowed = policy(tmp_path)
    with fixture(tmp_path) as (state, bindings, create):
        original = create()
        calls = []
        class Gateway:
            host_type = "pi"

            def verify_telegram_binding(self, binding):
                calls.append(binding)
                return True

            def readiness(self):
                return {"available": True}

        try:
            with pytest.raises(ValueError):
                TelegramPilot(tmp_path / "unused.db", api=original.api, bot_id=BOT, instance="pi",
                              gateway=SimpleNamespace(host_type="pi"), bindings=lambda: bindings)
            pi = TelegramPilot(tmp_path / "pi.sqlite3", api=original.api, bot_id=BOT, instance="pi",
                               gateway=Gateway(), bindings=lambda: bindings, sender_subject=allowed)
            try:
                for i, body in enumerate(({"text": "/start " + "A"*32}, {"text": "/agentbridge link AAAAAAAA"},
                                          {"photo": [{"file_id": "unauthorized"}]}, {"text": "查本人待办"})):
                    pi.ingest(update(i, who=502, message=i+1, **body))
                assert not calls and not state["downloads"] and not state["deliveries"]
                assert pi.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
                pi.ingest(update(21, message=11, text="/agentbridge status"))
                pi.ingest(update(22, message=11, text="/agentbridge status"))
                assert len(state["deliveries"]) == 1 and "Pi 私聊已绑定" in state["deliveries"][0]["text"]
                assert not state["admissions"]
                bindings["501"]["user_subject"] = "beta"
                assert pi._binding(501) == (None, None)
                path.write_text(json.dumps({**settings, "enabled": False}))
                pi.ingest(update(23, message=12, text="/start"))
                assert len(state["deliveries"]) == 1
            finally:
                pi.db.close()
        finally:
            original.db.close()


def test_private_destination_is_checked_at_transport_after_a_local_revocation(tmp_path):
    from nanobot_agentbridge.telegram import TelegramAPI, TelegramUnavailable

    path, settings, check = policy(tmp_path)
    def unexpected_connection():
        pytest.fail("Unauthorized Telegram request reached the network")

    api = TelegramAPI("synthetic", unexpected_connection, private_sender=check)
    with pytest.raises(TelegramUnavailable):
        api.call("sendMessage", chat_id=502, text="synthetic")
    path.write_text(json.dumps({**settings, "enabled": False}))
    with pytest.raises(TelegramUnavailable):
        api.call("sendChatAction", chat_id=501, action="typing")
    with pytest.raises(TelegramUnavailable):
        api.send_document(501, "sample.csv", "text/csv", b"a,b")
