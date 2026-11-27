"""The outbound rehearsal uses both real candidate consumers without Telegram polling."""

import os
import json
import re
import ssl
from datetime import datetime, timedelta, timezone
import time
from unittest.mock import patch
from uuid import uuid4
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tests.support.workspace_query_acceptance import Fixture, INSTANCES
from tests.support.interaction_acceptance import free_port
from tests.support.workspace_query_channel_probe import Channel, LocalResults, MockBot, OutboundOnly, ROOT, digest, live_scope


def tls_files(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "certificate.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    key_path.chmod(0o600)
    return cert_path, key_path


@pytest.mark.parametrize("host", ["nanobot", "pi"])
@pytest.mark.parametrize("tls", [False, True])
def test_outbound_candidate_query_text_and_receipts_are_once_only(tmp_path, host, tls):
    with patch.dict(os.environ):
        cert, key = tls_files(tmp_path) if tls else (None, None)
        origin = f"https://localhost:{free_port()}" if tls else "https://synthetic.invalid"
        context = ssl.create_default_context(cafile=str(cert)) if tls else None
        fixture = Fixture(tmp_path / "isolated", deliveries=False, trusted_card_origin=origin)
        bot, sender = map(int, fixture.channel_identities[host])
        transport = MockBot(bot, sender)
        cards = LocalResults(fixture, tls_cert=cert, tls_key=key)
        api = OutboundOnly(transport, bot=bot, sender=sender, local_results=cards)
        channel = Channel(fixture, host, api)
        try:
            account = fixture.accounts[fixture.owner]
            choices = fixture.service.workspace.hosts.choices(account["account_id"])
            changed = fixture.service.workspace.hosts.set_default(account["account_id"], INSTANCES[host],
                expected_revision=choices["revision"])
            fixture.app.send_chat_stream(account, message="请列出本次合成待办并说明数量",
                idempotency_key=uuid4().hex, instance_id=INSTANCES[host], expected_revision=changed["revision"])
            fixture.app.query_oa_pending(account, {"requestKey": uuid4().hex,
                "instanceId": INSTANCES[host], "expectedRevision": changed["revision"], "userMessage": "查询OA待办"})
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                channel.poll()
                report = channel.report()
                if (len(report) >= 5 and sum(row["card"] for row in api.receipts) == 2
                        and all(row["acknowledged"] for row in report)):
                    break
                time.sleep(.01)
            assert len(report) == 5, report
            assert len(api.receipts) == 5
            assert sum(row["card"] for row in api.receipts) == 2
            assert all(row["state"] == "delivered" and row["acknowledged"] for row in report)
            assert sum(row["dedupe_key"].startswith("workspace_text:") for row in report) == 3
            for message in transport.messages:
                if "reply_markup" not in message:
                    continue
                button = message["reply_markup"]["inline_keyboard"][0][0]
                assert ("web_app" in button) == tls
                url = button["web_app"]["url"] if tls else button["url"]
                assert url.startswith(cards.origin + "/results/")
                with urlopen(url, timeout=3, context=context) as response:
                    html = response.read().decode()
                    cookie = response.headers["Set-Cookie"].split(";")[0]
                data = json.loads(re.search(r'id="result-data">(.*?)</script>', html, re.S).group(1))
                state = data["state"]
                assert state["selectionAvailable"] is True
                assert 'id="result-prepare" type="button" disabled' in html
                assert state["preparations"] == []
                # Empty selection must fail at the real page/CSRF/backend boundary.
                request = Request(url + "/prepare", data=json.dumps({"operationId": state["groups"][0]["operationId"],
                    "targetKeys": [], "requestKey": uuid4().hex}).encode(), headers={"Content-Type": "application/json",
                    "Cookie": cookie, "Origin": cards.origin, "X-AgentBridge-Result-Token": data["csrfToken"]})
                with pytest.raises(HTTPError) as error:
                    urlopen(request, timeout=3, context=context)
                assert error.value.code == 400
            for _ in range(10):
                channel.poll()
            assert len(api.receipts) == 5
            assert all("text" not in row and "url" not in row for row in api.receipts)
            for method in ("getUpdates", "setWebhook", "sendDocument", "answerCallbackQuery"):
                with pytest.raises(ValueError, match="never reads"):
                    api.call(method)
        finally:
            channel.close()
            cards.close()
            fixture.close()


def test_outbound_scope_guard_rechecks_before_every_send():
    from nanobot_agentbridge.telegram import TelegramUnavailable
    allowed = True
    api = OutboundOnly(MockBot(8001, 501), bot=8001, sender=501, authorized=lambda: allowed)
    api.call("sendMessage", chat_id=501, text="synthetic")
    allowed = False
    with pytest.raises(TelegramUnavailable):
        api.call("sendMessage", chat_id=501, text="synthetic")
    assert len(api.receipts) == 1


@pytest.mark.parametrize("host", ["nanobot", "pi"])
def test_live_scope_pins_candidate_and_original_profile_without_loading_a_credential(tmp_path, monkeypatch, host):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"subjects": {"first": "alias-first", "second": "alias-second"},
        "telegram": {"botId": 8001, "allowedSenders": {"first": "501"}, "botCredential": {"kind": "not-read"}}}))
    profile.chmod(0o600)
    scope = {"schema": "agentbridge.workspace-outbound-acceptance.v1", "hostType": host,
        "source": str(ROOT), "expiresEpoch": time.time() + 300, "botId": 8001, "senderId": 501,
        "profile": {"path": str(profile), "sha256": digest(profile)}, "userSubject": "first",
        "candidateCommit": "candidate", "candidateWheelSha256": "wheel"}
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(scope))
    path.chmod(0o600)
    calls = []
    def verify(command, **kwargs):
        calls.append(command)
        return "candidate" if command[0] == "git" else json.dumps({"sha256": "wheel"})
    monkeypatch.setattr("tests.support.workspace_query_channel_probe.subprocess.check_output", verify)
    _, _, authorized = live_scope(path, host=host)
    assert authorized() and len(calls) == 2
    profile.write_text(profile.read_text() + " ")
    assert not authorized()
    with pytest.raises(ValueError, match="profile changed"):
        live_scope(path, host=host)
    path.write_text(json.dumps({**scope, "expiresEpoch": time.time() - 1}))
    with pytest.raises(ValueError, match="current private"):
        live_scope(path, host=host)
