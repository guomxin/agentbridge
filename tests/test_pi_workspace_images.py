"""Pi image outbox, scoped reads and private channel uploads through normal Workspace admission."""

import base64
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.workspace.pi_timeline_media import delivery
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.test_pi_workspace import pi  # noqa: F401 -- pytest fixture


@pytest.fixture
def paired(pi):
    identities = McpIdentityTokenStore(pi.service.db_path)
    owner = identities.issue(
        user_subject="user-a",
        expected_principal_ref="user-a",
        ttl_seconds=600,
        label="workspace-pi:pi-office:pi-a",
    )
    links = TelegramPilotLinks(pi.service)
    account = pi.accounts["a"]
    args = (account["account_id"], "pi-office", "8801")
    challenge = links.begin(*args)
    confirmation = links.present("user-a", "pi-office", "8801", "501", challenge["code"])
    links.confirm(*args, challenge["challenge_id"], confirmation)
    pair = links.ensure_endpoint("user-a", owner["token_id"], "pi-office", "8801", "501")
    for person in pi.accounts.values():
        pi.app.hosts.set_default(person["account_id"], "pi-office", expected_revision=1)
    return dict(
        subject="user-a",
        instance="pi-office",
        endpoint_key=pair["endpointKey"],
        pairing_ref=pair["pairingRef"],
        token_id=owner['token_id'],
    )


def send(pi, label, key, bodies):
    images = [
        {"fileName": f"{i}.png", "mimeType": "image/png", "content": base64.b64encode(body).decode()}
        for i, body in enumerate(bodies)
    ]
    with patch.object(pi.app, "_ensure_dispatch_worker"):
        pi.app.send_chat_stream(
            pi.accounts[label],
            message="请核对这组图",
            idempotency_key=key,
            attachments=images,
            instance_id="pi-office",
            expected_revision=2,
        )


PNG = b"\x89PNG\r\n\x1a\n"


def test_multi_image_delivery_preserves_order_origin_receipts_and_original_bytes(
    pi, paired, tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "integrations/nanobot-agentbridge"))
    from nanobot_agentbridge.pi_workspace_images import poll
    from nanobot_agentbridge.telegram import TelegramUnavailable

    bodies = [PNG + bytes([i]) for i in range(3)]
    send(pi, "a", "images-a", bodies)
    send(pi, "a", "images-a", bodies)
    send(pi, "b", "images-b", [PNG + b"b"])
    sent = []

    def upload(chat, filename, mime, data):
        sent.append((chat, filename, mime, data))
        if len(sent) == 2:
            raise TelegramUnavailable("ambiguous synthetic Telegram acknowledgement")
        return {
            "message_id": 900 + len(sent),
            "from": {"id": 8801},
            "chat": {"type": "private", "id": chat},
            "document": {"file_id": "document", "file_size": len(data)},
        }

    def request(action, body, **kwargs):
        assert action == "telegram-workspace-media" and body["binding"] == binding
        return delivery(pi.service, **paired, **{k: v for k, v in body.items() if k != "binding"})

    def channel():
        db = sqlite3.connect(tmp_path / "channel.sqlite")
        db.row_factory = sqlite3.Row
        return SimpleNamespace(
            bot_id=8801,
            db=db,
            gateway=SimpleNamespace(_request=request),
            api=SimpleNamespace(send_document=upload),
            _binding=lambda chat: (binding, "proof-hash"),
        )

    binding = {"user_subject": "user-a"}
    pilot = channel()
    poll(pilot, 501, binding, "proof-hash")
    pilot.db.close()
    restarted = channel()
    poll(restarted, 501, binding, "proof-hash")
    restarted.db.close()
    assert sent == [(501, f"workspace-{i + 1}.png", "image/png", data) for i, data in enumerate(bodies)]
    with pi.service.workspace._connect() as db:
        rows = db.execute(
            "SELECT subject,state,receipt FROM pi_workspace_image_deliveries ORDER BY ordinal"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("user-a", "sent", 901),
            ("user-a", "unknown", None),
            ("user-a", "sent", 903),
        ]
        assert not db.execute(
            "SELECT 1 FROM notification_outbox WHERE payload_type='timeline_message'"
        ).fetchall()


def test_image_claim_rechecks_binding_and_keeps_unknown_send_closed(pi, paired):
    send(pi, "a", "source", [PNG + b"a", PNG + b"b"])

    def call(action, **kwargs):
        return delivery(pi.service, **paired, action=action, **kwargs)

    for wrong in (
        {"subject": "user-b"},
        {"endpoint_key": "telegram:8801:502"},
        {"instance": "other"},
        {"pairing_ref": "0" * 64},
    ):
        with pytest.raises(PermissionError):
            delivery(pi.service, **{**paired, **wrong}, action="claim")
    item = call("claim")
    assert item["available"]
    lease = dict(delivery_id=item["deliveryId"], attempt=item["attempt"])
    with pytest.raises(PermissionError):
        call("read", delivery_id=item["deliveryId"], attempt=item["attempt"] + 1)
    assert call("read", **lease)["contentType"] == "image/png"
    assert call("begin", **lease)["available"]
    assert not call("begin", **lease)["available"] and not call("claim")["available"]
    with pi.service.workspace._connect() as db:
        db.execute("UPDATE pi_workspace_image_deliveries SET lease_until=0 WHERE state='sending'")
        db.execute("UPDATE timeline_attachments SET expires_at='2000-01-01T00:00:00Z' WHERE ordinal=1")
    assert not call("claim")["available"]
    assert not call("begin", **lease)["available"]
    send(pi, "a", "before-unlink", [PNG + b"new"])
    item = call("claim")
    fresh = dict(delivery_id=item["deliveryId"], attempt=item["attempt"])
    links = TelegramPilotLinks(pi.service)
    links.revoke(pi.accounts["a"]["account_id"], "pi-office", "8801")
    with pytest.raises(PermissionError):
        call("read", **fresh)


def test_original_image_receipt_after_revocation_settles_only_the_frozen_attempt(pi, paired):
    send(pi, 'a', 'late-original-image', [PNG+b'a', PNG+b'b'])
    item = delivery(pi.service, **paired, action='claim')
    target = {'delivery_id':item['deliveryId'], 'attempt':item['attempt']}
    assert delivery(pi.service, **paired, action='read', **target)['available']
    assert delivery(pi.service, **paired, action='begin', **target)['available']
    TelegramPilotLinks(pi.service).revoke(pi.accounts['a']['account_id'], 'pi-office', '8801')
    for wrong in ({'subject':'user-b'}, {'token_id':'other-token'}, {'instance':'other'},
                  {'endpoint_key':'telegram:8801:502'}, {'pairing_ref':'0'*64}, {'attempt':2}):
        values = {**paired, **target, **wrong}
        with pytest.raises(PermissionError):
            delivery(pi.service, **values, action='ack', receipt=901)
    assert delivery(pi.service, **paired, action='ack', **target)['recorded']
    assert delivery(pi.service, **paired, action='ack', receipt=901, **target)['recorded']
    assert delivery(pi.service, **paired, action='ack', receipt=901, **target)['recorded']
    assert not delivery(pi.service, **paired, action='ack', receipt=902, **target)['recorded']
    for action in ('read', 'begin'):
        with pytest.raises(PermissionError):
            delivery(pi.service, **paired, action=action, **target)
    with pytest.raises(PermissionError):
        delivery(pi.service, **paired, action='claim')
    with pi.service.workspace._connect() as db:
        assert [tuple(row) for row in db.execute('SELECT state,attempt,receipt FROM pi_workspace_image_deliveries ORDER BY ordinal')] == [
            ('sent',1,901), ('pending',0,None)]


def test_legacy_image_without_original_credential_does_not_gain_revoked_ack_access(pi, paired):
    send(pi, 'a', 'legacy-image', [PNG+b'a'])
    with pi.service.workspace._connect() as db:
        db.execute('UPDATE pi_workspace_image_deliveries SET token_id=NULL,endpoint_key=NULL')
    item = delivery(pi.service, **paired, action='claim')
    target = {'delivery_id':item['deliveryId'], 'attempt':item['attempt']}
    assert delivery(pi.service, **paired, action='begin', **target)['available']
    TelegramPilotLinks(pi.service).revoke(pi.accounts['a']['account_id'], 'pi-office', '8801')
    with pytest.raises(PermissionError):
        delivery(pi.service, **paired, action='ack', receipt=901, **target)


@pytest.mark.parametrize("text_unavailable", [False, True])
def test_pi_polls_text_before_images_and_an_unavailable_text_queue_does_not_starve_images(
    tmp_path, monkeypatch, text_unavailable
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "integrations/nanobot-agentbridge"))
    from nanobot_agentbridge import pi_workspace_images
    from nanobot_agentbridge.telegram import TelegramPilot, TelegramUnavailable
    from tests.test_nanobot_telegram_chat_mirror import Api, BOT, Gateway

    gateway = Gateway()
    gateway.host_type = "pi"
    pilot = TelegramPilot(
        tmp_path / "channel.db", api=Api(), bot_id=BOT, instance="pi-test", gateway=gateway,
        bindings=dict, allow_pairing=True, sender_subject=lambda chat: "first" if chat == 501 else None,
    )
    observed = []

    def poll_text(chat, binding, digest, **kwargs):
        observed.append(("text", chat, binding, digest))
        if text_unavailable:
            raise TelegramUnavailable("Unavailable during this synthetic poll")

    def poll_images(pilot, chat, binding, digest):
        observed.append(("images", chat, binding, digest))

    monkeypatch.setattr(pilot.terminal, "poll", poll_text)
    monkeypatch.setattr(pi_workspace_images, "poll", poll_images)
    try:
        pilot.poll_once()
        text, images = observed
        assert (text[0], images[0]) == ("text", "images")
        assert text[1:] == images[1:]
        assert text[1:3] == (501, gateway.binding) and text[3]
    finally:
        pilot.terminal.workspace_results.executor.shutdown()
        pilot.db.close()
