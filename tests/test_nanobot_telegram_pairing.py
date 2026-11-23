"""Workspace and Telegram prove separate control before a new private route is usable."""

import pytest

from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.support.nanobot_mcp_fixture import NanoBotMcpFixture
from tests.support.nanobot_telegram_setup import INSTANCE, seed_telegram_peers


@pytest.fixture
def pair_fixture(tmp_path, monkeypatch):
    home = tmp_path / "central"
    monkeypatch.setenv("AGENTBRIDGE_SESSION_KEY_FILE", str(home / "session.key"))
    fixture = NanoBotMcpFixture(home, "http://127.0.0.1:7191", 7191)
    try:
        endpoints = seed_telegram_peers(fixture)
        with fixture.service.workspace._connect() as db:
            db.execute(
                "UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?",
                (endpoints["b"],),
            )
            accounts = dict(
                db.execute("SELECT user_subject,account_id FROM workspace_accounts")
            )
        yield fixture, TelegramPilotLinks(fixture.service), accounts
    finally:
        fixture.close()


def test_pairing_requires_authenticated_workspace_and_matching_dm(pair_fixture):
    fixture, links, accounts = pair_fixture
    a, b = fixture.subjects.values()
    args = (accounts[b], INSTANCE, 8001)
    assert links.begin(accounts[a], INSTANCE, 8001)["source"] == "existing"
    request = links.begin(*args)
    assert request["state"] == "waiting_telegram"
    assert "code" not in links.status(*args)
    assert not links.present(a, INSTANCE, 8001, 509, request["code"])
    assert not links.present(b, INSTANCE, 8002, 509, request["code"])
    assert not links.present(b, INSTANCE, 8001, 501, request["code"])
    proof = links.present(b, INSTANCE, 8001, 509, request["code"])
    assert proof and not links.present(b, INSTANCE, 8001, 508, request["code"])
    assert links.status(*args)["state"] == "waiting_confirmation"
    assert proof not in str(links.status(*args))
    assert not links.verified(b, INSTANCE, 8001, 509)
    with pytest.raises(ValueError):
        links.confirm(accounts[a], INSTANCE, 8001, request["challenge_id"], proof)
    wrong = "00000000" if proof != "00000000" else "99999999"
    with pytest.raises(ValueError):
        links.confirm(*args, request["challenge_id"], wrong)
    assert links.confirm(*args, request["challenge_id"], proof)["source"] == "workspace"
    assert links.verified(b, INSTANCE, 8001, 509) and not links.verified(
        a, INSTANCE, 8001, 509
    )
    assert not links.verified(b, INSTANCE, 8002, 509)
    with pytest.raises(ValueError):
        links.confirm(*args, request["challenge_id"], proof)
    assert links.revoke(*args)["state"] == "unlinked" and not links.verified(
        b, INSTANCE, 8001, 509
    )


def test_pairing_attempt_limit_expiry_and_replacement(pair_fixture, monkeypatch):
    fixture, links, accounts = pair_fixture
    b = fixture.subjects["b"]
    args = (accounts[b], INSTANCE, 8001)
    first = links.begin(*args)
    second = links.begin(*args)
    assert first["challenge_id"] != second["challenge_id"]
    assert not links.present(b, INSTANCE, 8001, 509, first["code"])
    proof = links.present(b, INSTANCE, 8001, 509, second["code"])
    for _ in range(5):
        with pytest.raises(ValueError):
            links.confirm(*args, second["challenge_id"], "wrong")
    assert links.status(*args)["state"] == "unlinked"
    with pytest.raises(ValueError):
        links.confirm(*args, second["challenge_id"], proof)
    pending = links.begin(*args)
    import time

    now = time.time()
    monkeypatch.setattr(
        "agentbridge.workspace.telegram_pilot_links.time.time", lambda: now + 301
    )
    assert links.status(*args)["state"] == "unlinked"
    assert not links.present(b, INSTANCE, 8001, 509, pending["code"])


def test_registered_telegram_route_uses_original_deployment_and_avoids_automatic_cross_host_push(
    pair_fixture, tmp_path, monkeypatch
):
    import json

    fixture, links, _ = pair_fixture
    a, b = fixture.subjects.values()
    route = tmp_path / "routes.json"
    route.write_text(
        json.dumps(
            {
                "routes": {
                    INSTANCE: {
                        "instanceId": INSTANCE,
                        "telegram": {"botId": "8001", "username": "fixture_bot"},
                        "subjects": {
                            a: "tg-fixture-web-cred-a",
                            b: "tg-fixture-web-cred-b",
                        },
                    }
                }
            }
        )
    )
    route.chmod(0o600)
    monkeypatch.setenv("AGENTBRIDGE_NANOBOT_ROUTES_FILE", str(route))
    selected = links.ensure_endpoint(a, fixture.tokens["a"], INSTANCE, "8001", "501")
    again = links.ensure_endpoint(a, fixture.tokens["a"], INSTANCE, 8001, 501)
    assert again == selected
    with pytest.raises(PermissionError):
        links.ensure_endpoint(b, fixture.tokens["b"], INSTANCE, 8001, 501)
    with pytest.raises(PermissionError):
        links.ensure_endpoint(a, fixture.tokens["a"], INSTANCE, 8002, 501)
    with fixture.service.workspace._connect() as db:
        db.execute(
            "UPDATE client_endpoints SET capabilities_json=? WHERE agent_host='openclaw' AND user_subject=?",
            (json.dumps(["direct_status"]), a),
        )
    task, _ = fixture.service.tasks.ensure_task(
        user_subject=a,
        agent_host="nanobot",
        host_task_key="fixture-mcp-task",
        origin_endpoint_id=selected["endpointId"],
        active_conversation_ref=selected["conversationRef"],
        title="synthetic",
    )
    fixture.service.tasks.complete_task(
        task_id=task["task_id"], user_subject=a, reason="synthetic_done"
    )
    with fixture.service.workspace._connect() as db:
        ids = db.execute(
            "SELECT DISTINCT endpoint_id FROM notification_outbox WHERE task_id=?",
            (task["task_id"],),
        ).fetchall()
    assert {row[0] for row in ids} == {selected["endpointId"]}


def test_uncertain_existing_claim_cannot_connect_or_override_new_pairing(pair_fixture):
    fixture, links, accounts = pair_fixture
    a, b = fixture.subjects.values()
    fixture.service.tasks.ensure_endpoint(
        user_subject=b,
        token_id=fixture.tokens["b"],
        agent_host="openclaw",
        endpoint_key="broken-identity-link",
        client_type="telegram",
        external_subject="501",
        conversation_ref="invalid",
    )
    assert links.status(accounts[a], INSTANCE, 8001)["state"] != "linked"
    assert not links.verified(a, INSTANCE, 8001, 501)
    request = links.begin(accounts[b], INSTANCE, 8001)
    assert not links.present(b, INSTANCE, 8001, 501, request["code"])
