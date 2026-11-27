"""Original-card closure facts, real central receipts and unchanged host consumers."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from agentbridge.core import actionable_results as actions
from agentbridge.core import telegram_interaction_closures as closures
from agentbridge.core import telegram_notification_delivery as delivery
from tests.test_workspace_query_projection import openclaw, OWNER  # noqa: F401
from tests.test_telegram_notification_delivery_v2 import claim, scope, new_task


def login(fixture, task, *, subject=OWNER):
    session = fixture.service.sessions.find(user_subject=subject, system_id="oa")
    fixture.service.sessions.mark_expired(session["session_id"], "synthetic expiry")
    response = fixture.service.start_login(user_subject=subject, expected_principal_ref=subject,
        card_base_url=getattr(fixture,"trusted_card_origin","https://fixture.invalid"), system_id="oa", task_id=task, continue_after_login=True)
    raw = response["interaction"]
    record = fixture.service.interactions.get(raw["interactionId"], user_subject=subject)
    fixture.service.tasks.link_interaction(task_id=task, user_subject=subject, interaction_record=record, interaction=raw)
    return record, raw


def observe(fixture, task, record, raw, state):
    fixture.service.tasks.link_interaction(task_id=task, user_subject=record["user_subject"],
        interaction_record=record, interaction={**raw, "state": state})


def send(fixture, args, receipt=101):
    assert delivery.handle(fixture.service, **args, action="issue")["available"]
    assert delivery.handle(fixture.service, **args, action="begin") == {"allowed": True}
    assert delivery.handle(fixture.service, **args, action="ack", outcome="sent", receipt=receipt) == {"recorded": True}


@pytest.mark.parametrize("state", sorted(closures.STATES))
def test_original_card_closes_once_independent_of_task_and_result(openclaw, state):
    fixture, token, endpoint = openclaw
    claim(fixture, token, endpoint)
    task = new_task(fixture)
    record, raw = login(fixture, task)
    card, = claim(fixture, token, endpoint)
    send(fixture, scope(token, endpoint, card))
    observe(fixture, task, record, raw, state)
    closed, = claim(fixture, token, endpoint)
    args = scope(token, endpoint, closed)
    issued = delivery.handle(fixture.service, **args, action="issue")
    assert closed["notificationKind"] == "public_status" and closed["sourceEventId"] != card["sourceEventId"]
    assert issued["publicText"] == closures.text("credential", state)
    assert not {"interaction", "resultView"} & issued.keys()
    assert all(value not in issued["publicText"] for value in (raw["interactionId"], record["resource_id"], "https://"))
    send(fixture, args, 102)
    for _ in range(3):
        observe(fixture, task, record, raw, state)
        assert not claim(fixture, token, endpoint)
    with fixture.service.tasks._connect() as db:
        assert db.execute("SELECT count(*) FROM telegram_interaction_closures").fetchone()[0] == 1
        assert db.execute("SELECT state FROM telegram_notification_deliveries WHERE delivery_id=?", (card["deliveryId"],)).fetchone()[0] == "sent"
    # Reinitialization never backfills or sends another historical close.
    from agentbridge.core.tasks import TaskHubStore
    TaskHubStore(fixture.service.db_path)
    assert not claim(fixture, token, endpoint)


@pytest.mark.parametrize("initial", ["sending", "unknown"])
def test_closure_waits_for_exact_late_original_receipt_without_resending(openclaw, initial):
    fixture, token, endpoint = openclaw
    claim(fixture, token, endpoint)
    task = new_task(fixture); record, raw = login(fixture, task)
    card, = claim(fixture, token, endpoint); args = scope(token, endpoint, card)
    assert delivery.handle(fixture.service, **args, action="issue")["available"]
    assert delivery.handle(fixture.service, **args, action="begin")["allowed"]
    if initial == "unknown":
        assert delivery.handle(fixture.service, **args, action="ack", outcome="delivery_unknown")["recorded"]
    observe(fixture, task, record, raw, "completed")
    assert not claim(fixture, token, endpoint)
    assert delivery.handle(fixture.service, **args, action="begin") == {"allowed": False}
    assert delivery.handle(fixture.service, **(args | {"attempt_count": 2}), action="ack", outcome="sent", receipt=17) == {"recorded": False}
    assert delivery.handle(fixture.service, **args, action="ack", outcome="sent", receipt=17)["recorded"]
    closed, = claim(fixture, token, endpoint)
    send(fixture, scope(token, endpoint, closed), 18)
    assert delivery.handle(fixture.service, **args, action="ack", outcome="sent", receipt=17)["recorded"]
    assert not claim(fixture, token, endpoint)


@pytest.mark.parametrize("initial", ["unclaimed", "leased", "issued", "rejected", "legacy"])
def test_no_close_for_a_card_that_was_never_confirmably_sent(openclaw, initial):
    fixture, token, endpoint = openclaw
    if initial != "legacy":
        claim(fixture, token, endpoint)
    task = new_task(fixture); record, raw = login(fixture, task)
    if initial in {"leased", "issued", "rejected"}:
        card, = claim(fixture, token, endpoint); args = scope(token, endpoint, card)
        if initial != "leased":
            assert delivery.handle(fixture.service, **args, action="issue")["available"]
        if initial == "rejected":
            assert delivery.handle(fixture.service, **args, action="ack", outcome="unavailable")["recorded"]
    observe(fixture, task, record, raw, "completed")
    assert not claim(fixture, token, endpoint)
    with fixture.service.tasks._connect() as db:
        assert not db.execute("SELECT 1 FROM telegram_interaction_closures").fetchone()


@pytest.mark.parametrize("change", ["subject", "instance", "token", "pair", "revoked", "replace_pair"])
def test_close_cannot_leave_original_identity_or_pairing(openclaw, change):
    fixture, token, endpoint = openclaw; claim(fixture, token, endpoint)
    task = new_task(fixture); record, raw = login(fixture, task)
    card, = claim(fixture, token, endpoint); send(fixture, scope(token, endpoint, card))
    observe(fixture, task, record, raw, "completed")
    closed, = claim(fixture, token, endpoint); args = scope(token, endpoint, closed)
    if change in {"revoked", "replace_pair"}:
        with fixture.service.tasks._connect() as db:
            if change == "revoked":
                db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?", (endpoint["endpoint_id"],))
            else:
                db.execute("UPDATE client_endpoints SET external_subject='999' WHERE endpoint_id=?", (endpoint["endpoint_id"],))
    else:
        args.update({"subject": {"subject": "other"}, "instance": {"instance": "other"},
                     "token": {"token_id": "other"}, "pair": {"pairing_ref": "0" * 64}}[change])
    assert delivery.handle(fixture.service, **args, action="issue") == {"available": False}
    assert delivery.handle(fixture.service, **args, action="begin") == {"allowed": False}


def test_old_interaction_close_does_not_change_new_current_task_or_borrow_its_receipt(openclaw):
    fixture, token, endpoint = openclaw; claim(fixture, token, endpoint)
    task = new_task(fixture); first_record, first = login(fixture, task)
    card, = claim(fixture, token, endpoint); send(fixture, scope(token, endpoint, card))
    # A distinct registered original challenge in the same task; only the first
    # has a receipt. The active task must remain attached to the second card.
    challenge = fixture.service.challenges.create(user_subject=OWNER, system_id="oa",
        session_id=first_record["session_id"], expected_principal_ref=OWNER,
        origin="https://oa.example.test", page_fingerprint="second-original-form", nonce=None,
        fields=[{"name":"username","input_type":"text"}], card_base_url="https://fixture.invalid")
    second = fixture.service._credential_interaction(challenge, task_id=task, continue_after_login=True)
    second_record = fixture.service.interactions.get(second["interactionId"], user_subject=OWNER)
    observe(fixture, task, second_record, second, "pending")
    observe(fixture, task, first_record, first, "superseded")
    events = claim(fixture, token, endpoint)
    closed, = [item for item in events if item["notificationKind"] == "public_status"]
    assert delivery.handle(fixture.service, **scope(token, endpoint, closed), action="issue")["publicText"] == closures.text("credential", "superseded")
    current = fixture.service.tasks.get_task(task, user_subject=OWNER)
    assert current["current_interaction_id"] == second["interactionId"] and current["status"] == "waiting_user"
    observe(fixture, task, second_record, second, "failed")
    with fixture.service.tasks._connect() as db:
        assert db.execute("SELECT count(*) FROM telegram_interaction_closures").fetchone()[0] == 1


def test_close_send_unknown_is_not_replayed_and_old_ack_only_reconciles(openclaw):
    fixture, token, endpoint = openclaw; claim(fixture, token, endpoint)
    task = new_task(fixture); record, raw = login(fixture, task)
    card, = claim(fixture, token, endpoint); send(fixture, scope(token, endpoint, card))
    observe(fixture, task, record, raw, "completed")
    closed, = claim(fixture, token, endpoint); args = scope(token, endpoint, closed)
    assert delivery.handle(fixture.service, **args, action="issue")["available"]
    assert delivery.handle(fixture.service, **args, action="begin")["allowed"]
    assert delivery.handle(fixture.service, **args, action="ack", outcome="delivery_unknown")["recorded"]
    assert not claim(fixture, token, endpoint)
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?", (endpoint["endpoint_id"],))
    assert delivery.handle(fixture.service, **args, action="ack", outcome="sent", receipt=102)["recorded"]
    assert delivery.handle(fixture.service, **args, action="begin") == {"allowed": False}


def test_actual_login_resume_keeps_closure_and_original_result_independent(openclaw):
    from agentbridge.core import quick_queries
    from agentbridge.core import workspace_query_delivery as queries
    from tests.test_workspace_query_projection import claim as query_claim
    fixture, token, endpoint = openclaw; claim(fixture, token, endpoint)
    service = fixture.service
    session = service.sessions.find(user_subject=OWNER, system_id="oa")
    service.sessions.mark_expired(session["session_id"], "synthetic expiry")
    query = quick_queries.queue_query(service, fixture.account, {"requestKey": uuid4().hex, "userMessage": "查询我的 OA 待办"})
    response = actions.execute_preparation(service, OWNER, query["taskId"], query["commandKey"], runtime_context={
        "endpointId": fixture.account["endpoint_id"], "hostType": "openclaw", "hostInstanceId": "openclaw-gateway"})
    record = service.interactions.get(response["interaction"]["interactionId"], user_subject=OWNER)
    original, = claim(fixture, token, endpoint); send(fixture, scope(token, endpoint, original))
    challenge = service._credential_challenge_id(record)
    csrf = service.challenges.issue_csrf(challenge)
    service.challenges.claim(challenge, csrf_token=csrf, csrf_cookie=csrf)
    service.challenges.complete(challenge, result={"principal": OWNER})
    service.sessions.activate(record["session_id"], observed_principal_ref=OWNER)
    assert service.resume_interaction(user_subject=OWNER, interaction_id=record["interaction_id"])["status"] == "succeeded"
    closed, = claim(fixture, token, endpoint)
    send(fixture, scope(token, endpoint, closed), 102)
    result, = query_claim(service,OWNER,"openclaw","openclaw-gateway",token,endpoint["endpoint_key"])
    args = scope(token, endpoint, result)
    assert queries.handle(service, **args, action="issue")["resultView"]["sourceTaskId"] == query["taskId"]
    assert queries.handle(service, **args, action="begin")["allowed"]
    assert queries.handle(service, **args, action="ack", outcome="sent", receipt=103)["recorded"]
    assert not claim(fixture, token, endpoint)
    with service.tasks._connect() as db:
        assert db.execute("SELECT count(*) FROM agent_tasks WHERE task_id=?", (query["taskId"],)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM operations WHERE status='succeeded'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM write_authorizations").fetchone()[0] == 0


def test_already_finished_task_can_close_original_card_without_reopening(openclaw):
    fixture, token, endpoint = openclaw; claim(fixture, token, endpoint)
    task = new_task(fixture); record, raw = login(fixture, task)
    card, = claim(fixture, token, endpoint); send(fixture, scope(token, endpoint, card))
    fixture.service.tasks.complete_task(task_id=task, user_subject=OWNER, reason="synthetic task already complete")
    observe(fixture, task, record, raw, "expired")
    assert fixture.service.tasks.get_task(task, user_subject=OWNER)["status"] == "succeeded"
    events = claim(fixture, token, endpoint)
    texts = [delivery.handle(fixture.service, **scope(token, endpoint, item), action="issue")["publicText"] for item in events]
    assert closures.text("credential", "expired") in texts


def test_preupgrade_positive_receipts_are_not_retroactively_armed(openclaw):
    fixture, token, endpoint = openclaw; claim(fixture, token, endpoint)
    task = new_task(fixture); record, raw = login(fixture, task)
    card, = claim(fixture, token, endpoint); send(fixture, scope(token, endpoint, card))
    with fixture.service.tasks._connect() as db:
        db.execute("DELETE FROM telegram_interaction_card_watches")  # Pre-upgrade schema had no opt-in.
    observe(fixture, task, record, raw, "completed")
    assert not claim(fixture, token, endpoint)
    with fixture.service.tasks._connect() as db:
        assert not db.execute("SELECT 1 FROM telegram_interaction_closures").fetchone()


def test_stale_pair_watches_cannot_starve_current_original_card(openclaw):
    fixture, token, endpoint = openclaw; claim(fixture, token, endpoint)
    service = fixture.service
    task = new_task(fixture); record, raw = login(fixture, task)
    card, = claim(fixture, token, endpoint); send(fixture, scope(token, endpoint, card))
    with service.tasks._connect() as db:
        original = dict(db.execute("SELECT * FROM telegram_notification_deliveries WHERE delivery_id=?", (card["deliveryId"],)).fetchone())
        for _ in range(17):
            old = {**original, "delivery_id": str(uuid4()), "event_id": str(uuid4()), "pair_proof": "0" * 64}
            db.execute("INSERT INTO telegram_notification_deliveries (" + ",".join(old) + ") VALUES (" + ",".join("?" for _ in old) + ")", tuple(old.values()))
            db.execute("INSERT INTO telegram_interaction_card_watches VALUES(?,?,?)", (old["delivery_id"], original["created_at"], "2001-01-01T00:00:00+00:00"))
    challenge = service._credential_challenge_id(record); csrf = service.challenges.issue_csrf(challenge)
    service.challenges.claim(challenge, csrf_token=csrf, csrf_cookie=csrf)
    service.challenges.complete(challenge, result={"principal": OWNER})
    # Bad scopes are never read; their checked_at advances for fair bounded polling.
    assert not claim(fixture, token, endpoint)
    closed, = claim(fixture, token, endpoint)
    assert delivery.handle(service, **scope(token, endpoint, closed), action="issue")["publicText"] == closures.text("credential", "completed")


@pytest.mark.parametrize("ack_late", [False, True])
def test_unopened_expired_card_is_observed_once_and_never_needs_old_send_permission(openclaw, monkeypatch, ack_late):
    fixture, token, endpoint = openclaw; claim(fixture, token, endpoint)
    service = fixture.service
    task = new_task(fixture); record, raw = login(fixture, task)
    card, = claim(fixture, token, endpoint); args = scope(token, endpoint, card)
    assert delivery.handle(service, **args, action="issue")["available"]
    assert delivery.handle(service, **args, action="begin")["allowed"]
    if not ack_late:
        assert delivery.handle(service, **args, action="ack", outcome="sent", receipt=101)["recorded"]
    with service.tasks._connect() as db:
        db.execute("UPDATE auth_challenges SET expires_at='2001-01-01T00:00:00+00:00' WHERE challenge_id=?",
                   (service._credential_challenge_id(record),))
    observed = []
    load = service._load_interaction
    def counted(**kw):
        observed.append(kw["interaction_id"])
        return load(**kw)
    monkeypatch.setattr(service, "_load_interaction", counted)
    events = claim(fixture, token, endpoint)
    assert observed == [raw["interactionId"]]
    assert bool(events) is not ack_late
    with service.tasks._connect() as db:
        checked = db.execute("SELECT checked_at FROM telegram_interaction_card_watches").fetchone()[0]
    for _ in range(3):
        assert not claim(fixture, token, endpoint)
    assert observed == [raw["interactionId"]]
    with service.tasks._connect() as db:
        assert db.execute("SELECT checked_at FROM telegram_interaction_card_watches").fetchone()[0] == checked
    if ack_late:
        assert delivery.handle(service, **args, action="ack", outcome="sent", receipt=101)["recorded"]
        events = claim(fixture, token, endpoint)
    closed, = events
    assert delivery.handle(service, **args, action="begin") == {"allowed": False}
    issued = delivery.handle(service, **scope(token, endpoint, closed), action="issue")
    assert issued["publicText"] == closures.text("credential", "expired") and "interaction" not in issued
