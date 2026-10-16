"""Real HTTP continuation waits over original ledgers and card credentials."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import http.client
import json
import os
import threading
import time
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import pytest

from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.core.commit_notifications import publish_change
from tests.authorization_fixtures import grant_permissions
from tests import test_trusted_flow as helpers


def eventually(predicate, *, timeout=1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.fixture
def flow():
    fixture = helpers.TrustedFlowTests(methodName="runTest")
    fixture.setUp()
    fixture.service.invoke = Mock(side_effect=AssertionError("status cannot invoke business"))
    fixture.service.resume_interaction = Mock(side_effect=AssertionError("status cannot coordinate"))
    config = validate_auth_server_config(host="127.0.0.1", port=0, public_base_url=None,
                                        tls_cert=None, tls_key=None)
    server = create_auth_http_server(config=config, application=Mock(),
        field_application=fixture.field_app, action_application=fixture.action_app, flow_application=fixture.app)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    fixture.http_server = server
    fixture.origin = f"http://127.0.0.1:{server.server_port}"
    try:
        yield fixture
        fixture.service.invoke.assert_not_called()
        fixture.service.resume_interaction.assert_not_called()
        assert fixture.app._wait_count == 0
        assert fixture.app._wait_owners == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        fixture.doCleanups()


def path(flow, *, kind="input", resource=None, presentation=None):
    resource = resource or flow.source
    identifier = resource["submission_id" if kind == "input" else "authorization_id"]
    return f"/flow/{kind}/{identifier}" + (f"/present/{presentation}" if presentation else "") + "/status"


def headers(flow, token, **extra):
    return {"X-AgentBridge-Flow-Token": token, "Cookie": "agentbridge_csrf=" + token,
            "Origin": flow.origin, "Sec-Fetch-Site": "same-origin", **extra}


def get(flow, token, *, after="", wait=0, kind="input", resource=None, presentation=None, extra=None):
    connection = http.client.HTTPConnection("127.0.0.1", flow.http_server.server_port, timeout=5)
    try:
        query = urlencode({"after": after, "wait": wait})
        connection.request("GET", path(flow, kind=kind, resource=resource, presentation=presentation) + "?" + query,
                           headers=headers(flow, token, **(extra or {})))
        response = connection.getresponse()
        return response.status, json.loads(response.read()), dict(response.getheaders())
    finally:
        connection.close()


def wake(flow, subject="user-a"):
    publish_change(flow.service.tasks.db_path, user_subject=subject, topic="timeline")


def test_committed_successor_wakes_real_http_wait_and_preserves_exact_next_url(flow):
    _, token = flow.submit_source()
    initial = get(flow, token)[1]
    assert initial["state"] == "waiting_coordinator"
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(get, flow, token, after=initial["stateVersion"], wait=3)
        eventually(lambda: flow.app._wait_count == 1)
        started = time.monotonic()
        authorization, _ = flow.successor()
        status, value, response_headers = pending.result(timeout=1.5)
    assert time.monotonic() - started < 1.5
    assert status == 200
    assert value["nextUrl"] == "/flow/authorize/" + authorization["authorization_id"]
    assert value["stateVersion"] != initial["stateVersion"]
    assert response_headers["Connection"] == "close"
    assert "no-store" in response_headers["Cache-Control"]


def test_commit_between_snapshot_and_wait_cannot_be_lost(flow):
    _, token = flow.submit_source()
    after = get(flow, token)[1]["stateVersion"]
    original, raced = flow.app.reader.status, []

    def snapshot_then_commit(context):
        view = original(context)
        if not raced:
            raced.append(flow.successor()[0]["authorization_id"])
        return view

    with patch.object(flow.app.reader, "status", side_effect=snapshot_then_commit):
        started = time.monotonic()
        status, value, _ = get(flow, token, after=after, wait=3)
    assert time.monotonic() - started < 1.5
    assert status == 200
    assert value["nextUrl"].endswith(raced[0])


def test_missed_process_hint_recovers_from_durable_state_within_two_seconds(flow):
    _, token = flow.submit_source()
    after = get(flow, token)[1]["stateVersion"]
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(get, flow, token, after=after, wait=4)
        eventually(lambda: flow.app._wait_count == 1)
        started = time.monotonic()
        with patch.dict(os.environ, {"AGENTBRIDGE_NOTIFICATION_WAKEUPS": "0"}):
            authorization, _ = flow.successor()
        status, value, _ = pending.result(timeout=3.5)
    assert status == 200
    assert value["nextUrl"].endswith(authorization["authorization_id"])
    assert 1.5 <= time.monotonic() - started < 3.5


@pytest.mark.parametrize("invalidated", ["grant", "card_session", "resource_expiry"])
def test_every_wake_rechecks_original_authority_and_releases_capacity(flow, invalidated):
    _, token = flow.submit_source()
    after = get(flow, token)[1]["stateVersion"]
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(get, flow, token, after=after, wait=3)
        eventually(lambda: flow.app._wait_count == 1)
        if invalidated == "grant":
            grant_permissions(flow.service.user_grants, "user-a", [])
        elif invalidated == "card_session":
            with flow.service.field_submissions._connect() as connection:
                connection.execute("UPDATE field_submission_card_sessions SET expires_at=? WHERE csrf_hash=?",
                                   ("2000-01-01T00:00:00+00:00", hashlib.sha256(token.encode()).hexdigest()))
        else:
            flow.service.field_submissions.clock = lambda: datetime.fromisoformat(flow.source["expires_at"]) + timedelta(seconds=1)
        wake(flow)
        status, value, _ = pending.result(timeout=1.5)
    assert status == 403
    assert value["state"] == "unavailable"
    assert "nextUrl" not in value
    assert flow.app._wait_count == 0


@pytest.mark.parametrize("budget", ["owner", "global"])
def test_wait_capacity_is_bounded_and_unauthorized_requests_do_not_take_slots(flow, budget):
    _, token = flow.submit_source()
    after = get(flow, token)[1]["stateVersion"]
    if budget == "owner":
        flow.app._max_waits_per_owner = 1
    else:
        flow.app._max_waits = 1
    connection = http.client.HTTPConnection("127.0.0.1", flow.http_server.server_port)
    connection.request("GET", path(flow) + "?" + urlencode({"after": after, "wait": 20}), headers=headers(flow, token))
    try:
        eventually(lambda: flow.app._wait_count == 1)
        assert get(flow, token, after=after, wait=1)[0] == 429
        assert get(flow, "wrong", after=after, wait=1)[0] == 403
        assert get(flow, token, after=after, wait=1, extra={"Cookie": ""})[0] == 403
        assert get(flow, token, after=after, wait=1, extra={"Origin": "https://foreign.example"})[0] == 403
        assert flow.app._wait_count == 1
    finally:
        connection.close()
    eventually(lambda: flow.app._wait_count == 0)


def test_closed_tabs_release_slots_promptly_and_other_owner_hints_do_not_complete_wait(flow):
    _, token = flow.submit_source()
    after = get(flow, token)[1]["stateVersion"]
    for _ in range(3):
        connection = http.client.HTTPConnection("127.0.0.1", flow.http_server.server_port)
        connection.request("GET", path(flow) + "?" + urlencode({"after": after, "wait": 20}), headers=headers(flow, token))
        eventually(lambda: flow.app._wait_count == 1)
        wake(flow, "another-owner")
        time.sleep(.05)
        assert flow.app._wait_count == 1
        connection.close()
        eventually(lambda: flow.app._wait_count == 0, timeout=1)


def test_cursor_is_opaque_and_bound_to_source_card_credential_and_presentation(flow):
    _, token = flow.submit_source()
    first = get(flow, token)[1]
    assert len(first["stateVersion"]) == 64
    assert first["stateVersion"] == get(flow, token)[1]["stateVersion"]
    assert token not in first["stateVersion"]
    assert "sensitive" not in json.dumps(first)
    other = flow.source_for(flow.task_id)
    _, other_token = flow.open(resource=other)
    started = time.monotonic()
    status, value, _ = get(flow, other_token, resource=other, after=first["stateVersion"], wait=3)
    assert status == 200 and value["stateVersion"] != first["stateVersion"]
    assert time.monotonic() - started < 1
    presentation = flow.service.field_submissions.create_presentation(flow.source["submission_id"],
        user_subject="user-a", endpoint_id=flow.endpoint_id)
    assert get(flow, token, presentation=presentation["presentation_id"], after=first["stateVersion"], wait=1)[0] == 403


def test_unknown_wakes_terminal_without_retry_or_successor(flow):
    flow.submit_source()
    authorization, _ = flow.successor()
    token = flow.approve(authorization)
    operation = flow.consume(authorization)
    after = get(flow, token, kind="authorize", resource=authorization)[1]["stateVersion"]
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(get, flow, token, after=after, wait=3, kind="authorize", resource=authorization)
        eventually(lambda: flow.app._wait_count == 1)
        flow.service.operations.mark_unknown(operation["operation_id"], code="UNKNOWN", message="private raw payload")
        wake(flow)
        status, value, _ = pending.result(timeout=1.5)
    assert status == 200 and value["state"] == "unknown" and value["terminal"]
    assert "nextUrl" not in value and "continueUrl" not in value
    assert "private" not in json.dumps(value)


def test_permission_revoked_during_projection_is_checked_again_before_return(flow):
    _, token = flow.submit_source()
    original = flow.app.reader.status

    def read_then_revoke(context):
        value = original(context)
        grant_permissions(flow.service.user_grants, "user-a", [])
        return value

    with patch.object(flow.app.reader, "status", side_effect=read_then_revoke):
        status, value, _ = get(flow, token)
    assert status == 403 and value["state"] == "unavailable"


def test_request_origin_is_rechecked_before_return_not_just_at_admission(flow):
    _, token = flow.submit_source()
    allowed = iter([True, True, False])
    response = flow.app.status("input", flow.source["submission_id"], presentation_id=None,
        token=token, csrf_cookie=token, request_allowed=lambda: next(allowed))
    assert response.status == 403
    assert json.loads(response.body)["state"] == "unavailable"


@pytest.mark.parametrize("wait,after", [(21, ""), (-1, ""), ("nan", ""), (1, "bad"), ("inf", "")])
def test_invalid_wait_parameters_do_not_hold_capacity(flow, wait, after):
    _, token = flow.submit_source()
    assert get(flow, token, wait=wait, after=after)[0] == 400
    assert flow.app._wait_count == 0
