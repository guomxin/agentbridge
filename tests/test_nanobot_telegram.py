"""Replay synthetic Telegram updates over real loopback HTTP."""

import pytest

from tests.support.nanobot_telegram_loopback import BOT, fixture, update


def test_restart_replay_and_reply_use_pinned_private_route(tmp_path):
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        pilot.verify()
        state["lost_accept"] = True
        state["pending"] = True
        state["updates"] = [update(10), update(11, 502), update(12, 501)]
        pilot.poll_once()
        assert len(state["admissions"]) == 2 and not state["deliveries"]
        pilot.db.close()
        pilot = create()
        state["pending"] = False
        pilot.poll_once()
        assert [
            (item["chat_id"], item["text"], item["reply_parameters"])
            for item in state["deliveries"]
        ] == [
            (
                501,
                "合成结果 alpha",
                {"message_id": 9, "allow_sending_without_reply": False},
            ),
            (
                502,
                "合成结果 beta",
                {"message_id": 9, "allow_sending_without_reply": False},
            ),
        ]
        state["updates"] = [
            update(
                13,
                text="/status",
                message=10,
                reply_to_message={"message_id": 701, "from": {"id": BOT}},
            )
        ]
        pilot.poll_once()
        assert len(state["admissions"]) == 2
        assert state["deliveries"][-1]["text"].endswith("delivered")
        pilot.db.close()


def test_lost_delivery_never_resent_and_revocation_blocks_routing(tmp_path):
    with fixture(tmp_path) as (state, bindings, create):
        pilot = create()
        state["updates"] = [update(1)]
        state["lost_delivery"] = True
        pilot.poll_once()
        assert pilot._record(501, 9)["state"] == "delivery_unknown"
        pilot.db.close()
        pilot = create()
        state["updates"] = [update(2)]
        pilot.poll_once()
        assert len(state["deliveries"]) == 1 and len(state["admissions"]) == 1
        state.update(pending=True, updates=[update(3, 502)])
        pilot.poll_once()
        bindings["502"]["credential_ref"] = "new-route"
        state["pending"] = False
        pilot.poll_once()
        assert pilot._record(502, 9)["state"] == "binding_changed"
        assert len(state["deliveries"]) == 1
        pilot.db.close()


def test_rejects_foreign_bots_groups_clicks_and_unbound_senders(tmp_path):
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        for bad_id, webhook in [(BOT + 1, ""), (BOT, "https://example.invalid/hook")]:
            state.update(bot=bad_id, webhook=webhook)
            with pytest.raises(ValueError):
                pilot.verify()
        state.update(
            bot=BOT,
            webhook="",
            updates=[
                update(1, 505),
                update(2, chat={"type": "group", "id": 501}),
                update(3, **{"from": {"id": 502, "is_bot": False}}),
                update(4, forward_origin={"type": "user"}),
                {"update_id": 5, "callback_query": {"data": "approve"}},
                update(6, text="/approve"),
                update(7, text="/start", message=12),
            ],
        )
        pilot.verify()
        pilot.poll_once()
        assert not state["admissions"] and len(state["deliveries"]) == 1
        assert state["deliveries"][0]["reply_parameters"]["message_id"] == 12
        assert (
            pilot.db.execute("SELECT next_update FROM channel_meta").fetchone()[0] == 8
        )
        pilot.db.close()


def test_existing_numeric_identity_required_for_start_and_again_for_reply(tmp_path):
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        state.update(
            verified={"502"}, updates=[update(1, text="/start"), update(2, 502)]
        )
        state["pending"] = True
        pilot.poll_once()
        assert pilot._record(501, 9) is None and not state["deliveries"]
        assert len(state["admissions"]) == 1
        state.update(verified={"501"}, pending=False)
        pilot.poll_once()
        assert (
            pilot._record(502, 9)["state"] == "binding_changed"
            and not state["deliveries"]
        )
        state["updates"] = [update(3, text="/start", message=10)]
        pilot.poll_once()
        assert state["deliveries"][0]["chat_id"] == 501
        pilot.db.close()


def test_systemd_credential_follows_one_named_private_descriptor(tmp_path, monkeypatch):
    from nanobot_agentbridge.telegram_credentials import bot_token

    monkeypatch.setattr(
        "nanobot_agentbridge.telegram_credentials.platform.system", lambda: "Linux"
    )
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    token = tmp_path / "telegram.bot"
    token.touch(mode=0o600)
    token.write_text("simulated-value")
    config = {"botCredential": {"kind": "systemd", "name": "telegram.bot"}}
    assert bot_token(config) == "simulated-value"
    token.chmod(0o644)
    with pytest.raises(PermissionError):
        bot_token(config)
    for ref in (
        {"kind": "systemd", "name": "../sibling"},
        {"kind": "systemd", "name": "telegram.bot", "value": "inline"},
    ):
        with pytest.raises(ValueError):
            bot_token({"botCredential": ref})
    with pytest.raises(ValueError):
        bot_token({**config, "botTokenFile": str(token)})


def test_keychain_uses_exact_item_and_never_reports_secret(monkeypatch):
    from nanobot_agentbridge import telegram_credentials
    monkeypatch.setattr(telegram_credentials.platform, "system", lambda: "Darwin")
    seen = []
    def read(service,account):
        seen.append((service,account))
        return "simulated-only"
    monkeypatch.setattr(telegram_credentials, "_login_keychain_item", read)
    source = {"kind":"macos-keychain","service":"fixture service","account":"fixture-account"}
    assert telegram_credentials.bot_token({"botCredential":source}) == "simulated-only"
    assert seen == [("fixture service","fixture-account")]
    def denied(*args):
        raise ValueError("simulated-only")
    monkeypatch.setattr(telegram_credentials, "_login_keychain_item", denied)
    with pytest.raises(ValueError) as error:
        telegram_credentials.bot_token({"botCredential":source})
    assert "simulated-only" not in str(error.value)


def test_central_verified_identity_requires_live_original_private_route_and_workspace_peer(
    tmp_path, monkeypatch
):
    from agentbridge.workspace.telegram_pilot_identity import verified_sender
    from tests.support.nanobot_mcp_fixture import NanoBotMcpFixture
    from tests.support.nanobot_telegram_setup import INSTANCE, seed_telegram_peers

    # The standalone test server normally owns its process environment.
    # Capture and restore its session-key setting when reusing it in pytest.
    monkeypatch.setenv(
        "AGENTBRIDGE_SESSION_KEY_FILE", str(tmp_path / "central-seed" / "session.key")
    )
    fixture = NanoBotMcpFixture(
        tmp_path / "central-seed", "http://127.0.0.1:7181", 7181
    )
    try:
        endpoints = seed_telegram_peers(fixture)
        auth = lambda subject, sender: verified_sender(
            fixture.service,
            subject=subject,
            instance=INSTANCE,
            evidence={"senderId": sender, "botId": "8001"},
        )
        a, b = fixture.subjects.values()
        assert auth(a, "501") and auth(b, "502")
        assert not auth(a, "502") and not auth(b, "501") and not auth(a, "0501")
        db = fixture.service.workspace
        with db._connect() as connection:
            connection.execute(
                "UPDATE client_endpoints SET conversation_ref='agent:main:telegram:group:501' WHERE endpoint_id=?",
                (endpoints["a"],),
            )
        assert not auth(a, "501")
        with db._connect() as connection:
            connection.execute(
                "UPDATE client_endpoints SET conversation_ref='agent:main:telegram:direct:501' WHERE endpoint_id=?",
                (endpoints["a"],),
            )
            connection.execute(
                "UPDATE workspace_accounts SET state='disabled' WHERE user_subject=?",
                (a,),
            )
        assert not auth(a, "501")
        assert auth(b, "502")
        with db._connect() as connection:
            connection.execute(
                "UPDATE workspace_accounts SET state='active' WHERE user_subject=?",
                (a,),
            )
            connection.execute(
                "UPDATE client_endpoints SET external_subject='501' WHERE endpoint_id=?",
                (endpoints["b"],),
            )
        assert not auth(a, "501")
        with db._connect() as connection:
            connection.execute(
                "UPDATE client_endpoints SET external_subject='502' WHERE endpoint_id=?",
                (endpoints["b"],),
            )
            connection.execute(
                "UPDATE workspace_host_instances SET state='draining' WHERE instance_id=?",
                (INSTANCE,),
            )
        assert not auth(a, "501") and not auth(b, "502")
        with db._connect() as connection:
            connection.execute(
                "UPDATE workspace_host_instances SET state='enabled' WHERE instance_id=?",
                (INSTANCE,),
            )
        fixture.identities.revoke(fixture.tokens["a"])
        assert not auth(a, "501") and auth(b, "502")
    finally:
        fixture.close()


def test_identity_outage_retains_offset_without_replaying_the_other_user(tmp_path):
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        state.update(identity_unavailable={"501"}, updates=[update(1), update(2, 502)])
        pilot.poll_once()
        assert not pilot._record(501, 9)
        assert pilot._record(502, 9)["state"] == "delivered"
        assert not pilot.db.execute("SELECT * FROM channel_meta").fetchone()
        state["identity_unavailable"].clear()
        pilot.poll_once()
        assert sorted(item["chat_id"] for item in state["deliveries"]) == [501, 502]
        assert len(state["admissions"]) == 2
        assert (
            pilot.db.execute("SELECT next_update FROM channel_meta").fetchone()[0] == 3
        )
        pilot.db.close()
