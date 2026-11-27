"""Revocation cannot fall back to an inherited grant or revive its old deliveries."""

import time

import pytest

from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.support.nanobot_telegram_setup import INSTANCE
from tests.test_nanobot_telegram_pairing import pair_fixture  # noqa: F401
from tests.test_nanobot_workspace_chat_mirror import mirror  # noqa: F401


def repair(links, account, owner, sender):
    request = links.begin(account, INSTANCE, 8001)
    assert request["state"] == "waiting_telegram"
    proof = links.present(owner, INSTANCE, 8001, sender, request["code"])
    assert proof and not links.verified(owner, INSTANCE, 8001, sender)
    return links.confirm(account, INSTANCE, 8001, request["challenge_id"], proof)


def test_nano_endpoint_proof_matches_mirror_and_is_stable_before_explicit_revocation(mirror, pair_fixture):
    service, links, accounts, owner, _, ask, _ = mirror
    fixture = pair_fixture[0]
    before = links.ensure_endpoint(owner, fixture.tokens["a"], INSTANCE, 8001, 501)
    assert len(before["pairingRef"]) == 64
    ask(owner, "initial-grant", "仍按现有授权投递")
    with service.workspace._connect() as db:
        import json
        payload = json.loads(db.execute("SELECT payload_json FROM notification_outbox WHERE endpoint_id=?",
                                        (before["endpointId"],)).fetchone()[0])
    assert payload["pairProof"] == before["pairingRef"]
    assert links.ensure_endpoint(owner, fixture.tokens["a"], INSTANCE, 8001, 501) == before
    assert links.status(accounts[owner], INSTANCE, 8001)["source"] == "existing"


def test_revoking_inherited_nano_pair_stays_revoked_after_reload_and_keeps_openclaw_active(mirror, pair_fixture):
    service, links, accounts, owner, _, _, _ = mirror
    fixture = pair_fixture[0]
    assert links.verified(owner, INSTANCE, 8001, 501)
    assert links.revoke(accounts[owner], INSTANCE, 8001)["state"] == "unlinked"
    reloaded = TelegramPilotLinks(service)
    assert not reloaded.verified(owner, INSTANCE, 8001, 501)
    assert reloaded.mirror_peers(owner, INSTANCE, 8001) == {"senderIds": []}
    with pytest.raises(PermissionError):
        reloaded.ensure_endpoint(owner, fixture.tokens["a"], INSTANCE, 8001, 501)
    with service.workspace._connect() as db:
        assert db.execute("""SELECT 1 FROM client_endpoints WHERE agent_host='openclaw'
            AND user_subject=? AND external_subject='501' AND state='active'""", (owner,)).fetchone()


def test_same_sender_repair_uses_fresh_grant_and_never_revives_old_outbox(mirror, pair_fixture, monkeypatch):
    service, links, accounts, owner, _, ask, _ = mirror
    fixture = pair_fixture[0]
    fixed = time.time()
    monkeypatch.setattr("agentbridge.workspace.telegram_pilot_links.time.time", lambda: fixed)
    before = links.ensure_endpoint(owner, fixture.tokens["a"], INSTANCE, 8001, 501)["pairingRef"]
    ask(owner, "old-grant", "撤销前的旧消息")
    links.revoke(accounts[owner], INSTANCE, 8001)
    assert repair(links, accounts[owner], owner, 501)["source"] == "workspace"
    after = links.ensure_endpoint(owner, fixture.tokens["a"], INSTANCE, 8001, 501)["pairingRef"]
    assert after != before
    ask(owner, "new-grant", "双端重新确认后的新消息")
    claimed = service.claim_host_notifications(user_subject=owner, agent_host="nanobot",
        endpoint_key="telegram:8001:501", host_instance_id=INSTANCE, include_workspace_chat=True)["notifications"]
    assert [row["timeline"]["text"] for row in claimed] == ["双端重新确认后的新消息"]
    links.revoke(accounts[owner], INSTANCE, 8001)
    repair(links, accounts[owner], owner, 501)
    assert links.ensure_endpoint(owner, fixture.tokens["a"], INSTANCE, 8001, 501)["pairingRef"] not in {before, after}


def test_revocation_scope_and_repair_still_reject_foreign_owner(mirror, pair_fixture):
    service, links, accounts, owner, other, _, _ = mirror
    fixture = pair_fixture[0]
    before = links.ensure_endpoint(owner, fixture.tokens["a"], INSTANCE, 8001, 501)["pairingRef"]
    links.revoke(accounts[other], INSTANCE, 8001)
    links.revoke(accounts[owner], INSTANCE, 8002)
    assert links.ensure_endpoint(owner, fixture.tokens["a"], INSTANCE, 8001, 501)["pairingRef"] == before
    links.revoke(accounts[owner], INSTANCE, 8001)
    request = links.begin(accounts[owner], INSTANCE, 8001)
    service.tasks.ensure_endpoint(user_subject=other, token_id=fixture.tokens["b"], agent_host="openclaw",
        endpoint_key="foreign-claim", client_type="telegram", external_subject="501", conversation_ref="invalid")
    assert links.present(owner, INSTANCE, 8001, 501, request["code"]) is None
    assert not links.verified(owner, INSTANCE, 8001, 501)


def test_repair_can_choose_new_sender_without_restoring_old_implicit_sender(mirror):
    _, links, accounts, owner, _, _, _ = mirror
    links.revoke(accounts[owner], INSTANCE, 8001)
    repair(links, accounts[owner], owner, 509)
    assert links.verified(owner, INSTANCE, 8001, 509)
    assert not links.verified(owner, INSTANCE, 8001, 501)
    assert links.mirror_peers(owner, INSTANCE, 8001) == {"senderIds": ["509"]}
