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


def migration_setup(mirror, pair_fixture):
    from agentbridge.core.workspace_query_delivery import VERSIONS
    from tests.test_workspace_query_projection import register
    service, links, accounts, owner, other, ask, deploy = mirror
    credential = pair_fixture[0].tokens["a"]
    with service.tasks._connect() as db:
        db.execute("UPDATE workspace_host_instances SET adapter_version=? WHERE instance_id=?",
                   (VERSIONS["nanobot"], INSTANCE))
    register(service, owner, "nanobot", INSTANCE, credential)
    return service, links, accounts, owner, other, credential


def test_migrating_inherited_grant_requires_both_current_identities_and_invalidates_old_cards(mirror, pair_fixture):
    from agentbridge.core.actionable_results import _telegram_endpoint, ActionableResultError
    service, links, accounts, owner, other, token = migration_setup(mirror, pair_fixture)
    args = accounts[owner], INSTANCE, 8001
    exact = dict(token_id=token, protocol=2)
    assert links.status(*args)["can_migrate"]
    old = links.ensure_endpoint(owner, token, INSTANCE, 8001, 501)
    with service.tasks._connect() as db:
        old_hash = _telegram_endpoint(db, owner, old["endpointId"])["_binding_hash"]
    challenge = links.begin(*args, migrate=True)
    assert challenge["state"] == "waiting_telegram" and links.verified(owner, INSTANCE, 8001, 501)
    assert not links.present(owner, INSTANCE, 8001, 501, challenge["code"], token_id=token)
    assert not links.present(owner, INSTANCE, 8001, 502, challenge["code"], **exact)
    assert not links.present(owner, INSTANCE, 8001, 501, challenge["code"], token_id="wrong", protocol=2)
    assert not links.present(other, INSTANCE, 8001, 501, challenge["code"], **exact)
    confirmation = links.present(owner, INSTANCE, 8001, 501, challenge["code"], **exact)
    assert confirmation and links.verified(owner, INSTANCE, 8001, 501)
    with pytest.raises(ValueError):
        links.confirm(accounts[other], INSTANCE, 8001, challenge["challenge_id"], confirmation)
    assert links.confirm(*args, challenge["challenge_id"], confirmation)["source"] == "workspace"
    assert not links.verified(owner, INSTANCE, 8001, 501)
    assert links.verified(owner, INSTANCE, 8001, 501, **exact)
    assert links.mirror_peers(owner, INSTANCE, 8001)["senderIds"] == []
    with pytest.raises(PermissionError):
        links.ensure_endpoint(owner, token, INSTANCE, 8001, 501)
    with service.tasks._connect() as db:
        db.execute("UPDATE client_endpoints SET state='revoked' WHERE agent_host='openclaw' AND user_subject=?", (owner,))
    assert links.mirror_peers(owner, INSTANCE, 8001, **exact)["senderIds"] == ["501"]
    current = links.ensure_endpoint(owner, token, INSTANCE, 8001, 501, protocol=2)
    assert old["pairingRef"] != current["pairingRef"]
    with service.tasks._connect() as db:
        assert _telegram_endpoint(db, owner, current["endpointId"])["_binding_hash"] != old_hash
    assert links.revoke(*args)["state"] == "unlinked"
    with service.tasks._connect() as db:
        db.execute("UPDATE client_endpoints SET state='active' WHERE agent_host='openclaw' AND user_subject=?", (owner,))
        with pytest.raises(ActionableResultError):
            _telegram_endpoint(db, owner, old["endpointId"])
    assert not links.verified(owner, INSTANCE, 8001, 501, **exact)


def test_migration_challenge_rechecks_original_pair_and_minimum_deployment(mirror, pair_fixture):
    service, links, accounts, owner, _, token = migration_setup(mirror, pair_fixture)
    args = accounts[owner], INSTANCE, 8001
    challenge = links.begin(*args, migrate=True)
    secret = links.present(owner, INSTANCE, 8001, 501, challenge["code"], token_id=token, protocol=2)
    assert secret
    with service.tasks._connect() as db:
        db.execute("UPDATE workspace_host_instances SET adapter_version='0.2.0' WHERE instance_id=?", (INSTANCE,))
    with pytest.raises(ValueError):
        links.confirm(*args, challenge["challenge_id"], secret)
    assert links.verified(owner, INSTANCE, 8001, 501)


def test_delivery_consumers_must_honor_migrated_pair_until_settling_an_existing_send(mirror, pair_fixture):
    from agentbridge.core import workspace_query_delivery
    service, links, accounts, owner, _, token = migration_setup(mirror, pair_fixture)
    ask = mirror[5]
    args = accounts[owner], INSTANCE, 8001
    endpoint = links.ensure_endpoint(owner, token, INSTANCE, 8001, 501)
    ask(owner, "old-request", "原配对中已接纳")
    challenge = links.begin(*args, migrate=True)
    confirmation = links.present(owner, INSTANCE, 8001, 501, challenge["code"], token_id=token, protocol=2)
    links.confirm(*args, challenge["challenge_id"], confirmation)
    ask(owner, "new-request", "新配对中的公开问题")
    def claim(protocol):
        return service.claim_host_notifications(user_subject=owner, agent_host="nanobot", host_instance_id=INSTANCE,
            endpoint_key=endpoint["endpointKey"], query_token_id=token, pairing_token_id=token, pairing_protocol=protocol,
            include_workspace_chat=True)["notifications"]
    assert not claim(None)
    current, = claim(2)
    assert current["pairingRef"] != endpoint["pairingRef"]
    scope = dict(subject=owner, instance=INSTANCE, host="nanobot", endpoint_key=endpoint["endpointKey"],
                 token_id=token, pairing_ref=current["pairingRef"], delivery_id=current["deliveryId"],
                 attempt_count=current["attemptCount"])
    assert workspace_query_delivery.handle(service, **scope, action="issue") == {"available": False}
    assert "新配对中的公开问题" in workspace_query_delivery.handle(service, **scope, action="issue", pairing_protocol=2)["publicText"]
    assert workspace_query_delivery.handle(service, **scope, action="begin") == {"allowed": False}
    assert workspace_query_delivery.handle(service, **scope, action="begin", pairing_protocol=2)["allowed"]
    links.revoke(*args)
    assert workspace_query_delivery.handle(service, **scope, action="ack", outcome="sent", receipt=314)["recorded"]
    from agentbridge.core.tasks import TaskNotFound
    with pytest.raises(TaskNotFound):
        claim(2)


def test_migration_uses_original_private_consumer_when_web_has_a_different_identity(mirror, pair_fixture):
    from agentbridge.core.mcp_identities import McpIdentityTokenStore
    from tests.test_workspace_query_projection import register
    service, links, accounts, owner, _, web_token = migration_setup(mirror, pair_fixture)
    native_token = McpIdentityTokenStore(service.db_path).issue(user_subject=owner, expected_principal_ref=owner,
                                                                label="isolated-native-consumer")["token_id"]
    links.ensure_endpoint(owner, native_token, INSTANCE, 8001, 501)
    register(service, owner, "nanobot", INSTANCE, native_token)
    with service.tasks._connect() as db:
        db.execute("DELETE FROM agent_host_registrations WHERE agent_host='nanobot' AND token_id=?", (web_token,))
    challenge = links.begin(accounts[owner], INSTANCE, 8001, migrate=True)
    assert not links.present(owner, INSTANCE, 8001, 501, challenge["code"], token_id=web_token, protocol=2)
    confirmation = links.present(owner, INSTANCE, 8001, 501, challenge["code"], token_id=native_token, protocol=2)
    assert confirmation
    assert links.confirm(accounts[owner], INSTANCE, 8001, challenge["challenge_id"], confirmation)["source"] == "workspace"
    assert links.verified(owner, INSTANCE, 8001, 501, token_id=native_token, protocol=2)
