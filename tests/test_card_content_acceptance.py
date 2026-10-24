"""Independent full-path navigation, expiry, retention and new-decision checks."""
import json
import re
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from threading import Thread
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from agentbridge.core.business_drafts import BusinessDraftService
from tests.support.card_content_acceptance import CardContentAcceptanceFixture, RETAINED_OPINION
from tests.test_full_business_acceptance import drive


@pytest.fixture
def fixture(tmp_path):
    value = CardContentAcceptanceFixture(tmp_path)
    try:
        yield value
    finally:
        value.close()


class CardHttp:
    def __init__(self, fixture):
        self.fixture = fixture
        self.cookies = {}

    def request(self, method, path, body=None, *, proof=None):
        connection = HTTPConnection("127.0.0.1", self.fixture.meta["card_port"], timeout=10)
        headers = {"Origin": self.fixture.card_url, "Sec-Fetch-Site": "same-origin",
            "Cookie": "; ".join(key + "=" + value for key, value in self.cookies.items())}
        if proof is not None:
            headers["X-AgentBridge-Draft-Token"] = proof
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            content = response.read().decode()
            for key, value in response.getheaders():
                if key.lower() == "set-cookie":
                    self.cookies.update({name: morsel.value for name, morsel in SimpleCookie(value).items()})
            return response.status, content
        finally:
            connection.close()


@pytest.fixture
def http(fixture):
    servers = fixture.servers()
    threads = [Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield CardHttp(fixture)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def open_card(http, case):
    path = urlsplit(case["presentation"]["url"]).path
    status, body = http.request("GET", path)
    assert status == 200, body[:300]
    config = json.loads(re.search(r'mountBusinessDraft\((\{[^\n]*\}), document, window\);', body)[1])
    return path, config


def content_request(state, candidate="restore"):
    content = state["contentDraft"][candidate]
    return {"expectedRevision": state["revision"], "contentId": content["contentId"],
        "contentVersion": content["version"], "requestKey": uuid4().hex}


def test_expired_source_new_form_restore_recheck_and_one_fresh_decision(fixture, http):
    old, fresh = (fixture.meta["card_content_expiry"][key] for key in ("old", "fresh"))
    status, body = http.request("GET", urlsplit(old["presentation"]["url"]).path)
    assert status == 403
    assert fixture.workspace_url + "/" in body
    assert old["input_id"] not in body and RETAINED_OPINION not in body
    path, config = open_card(http, fresh)
    state, token = config["state"], config["csrfToken"]
    assert state["navigation"]["back"] == {"kind": "workspace", "url": fixture.workspace_url + "/", "label": "返回 Workspace"}
    assert not state["values"].get("opinion") and state["contentDraft"]["restore"]
    baseline = fixture.audit()
    assert baseline["decisions"] == baseline["syntheticWrites"] == 0
    payload = content_request(state)
    assert http.request("GET", path + "/draft/content-restore")[0] == 405
    assert http.request("POST", path + "/draft/content-restore", payload)[0] == 403
    status, body = http.request("POST", path + "/draft/content-restore", payload, proof=token)
    assert status == 200, body
    restored = json.loads(body)
    assert restored["values"]["opinion"] == RETAINED_OPINION and restored["preview"] is None
    assert restored["revision"] == state["revision"] + 1
    status, body = http.request("POST", path + "/draft/content-restore", payload, proof=token)
    assert status == 200 and json.loads(body)["revision"] == restored["revision"]
    assert fixture.audit()["decisions"] == fixture.audit()["syntheticWrites"] == 0
    status, body = http.request("POST", path + "/draft/preview", {"expectedRevision": restored["revision"]}, proof=token)
    assert status == 200, body
    preview = json.loads(body)
    approval = {"expectedRevision": preview["revision"], "previewId": preview["preview"]["previewId"], "requestKey": uuid4().hex}
    status, body = http.request("POST", path + "/draft/approve", approval, proof=token)
    assert status == 200, body
    assert http.request("POST", path + "/draft/approve", approval, proof=token)[0] == 200
    assert fixture.audit()["syntheticWrites"] == 0
    drive(fixture, fresh)
    drive(fixture, fresh)
    final = fixture.audit()
    assert final["decisions"] == final["syntheticWrites"] == 1
    assert final["authorizations"] == {"consumed": 1} and final["modelDispatches"] == 0
    assert len(final["contentActions"]) == 1
    assert fixture.service.field_submissions.get(old["input_id"])["state"] == "expired"


def test_restore_rejects_changed_exact_business_identity_without_copying_text(fixture, http):
    case = fixture.meta["card_content_expiry"]["fresh"]
    path, config = open_card(http, case)
    fixture.oa.data["affairs"][case["arguments"]["affair_id"]]["signals"]["identity"]["process_id"] = "different-live-process"
    fixture.oa.persist()
    status, body = http.request("POST", path + "/draft/content-restore", content_request(config["state"]), proof=config["csrfToken"])
    assert status == 409 and json.loads(body)["error"]["code"] == "DRAFT_CONTENT_TARGET_CHANGED"
    state = BusinessDraftService(fixture.service).open(case["input_id"])["state"]
    assert not state["values"].get("opinion") and state["preview"] is None
    assert fixture.audit()["decisions"] == fixture.audit()["syntheticWrites"] == 0


def test_discard_is_explicit_idempotent_and_does_not_change_live_input(fixture, http):
    case = fixture.meta["card_content_discard"]
    path, config = open_card(http, case)
    state = config["state"]
    payload = content_request(state, "saved")
    for _ in range(2):
        status, body = http.request("POST", path + "/draft/content-discard", payload, proof=config["csrfToken"])
        assert status == 200, body
        current = json.loads(body)
        assert current["values"] == state["values"] and current["contentDraft"]["saved"] is None
    ledger = fixture.audit()
    assert ledger["contentRows"] == 1 and len(ledger["contentActions"]) == 1
    assert ledger["decisions"] == ledger["syntheticWrites"] == 0


def test_same_home_restart_does_not_reseed_sources_or_content(tmp_path):
    first = CardContentAcceptanceFixture(tmp_path)
    audit, queries = first.audit(), first.meta["card_content_queries"]
    first.close()
    second = CardContentAcceptanceFixture(tmp_path)
    try:
        assert second.audit() == audit
        assert second.meta["card_content_queries"] == queries
    finally:
        second.close()


def test_telegram_result_navigation_wins_and_cannot_attach_to_workspace_presentation(tmp_path):
    from agentbridge.auth.action_card import TrustedActionApplication
    from agentbridge.auth.field_card import TrustedFieldApplication
    from agentbridge.auth.flow import TrustedFlowApplication
    from tests.support.full_business_acceptance import OWNER
    from tests.support.telegram_recovery_acceptance import TelegramRecoveryAcceptanceFixture
    from tests.test_telegram_recovery_acceptance import tg_prepare, request_body
    fixture = TelegramRecoveryAcceptanceFixture(tmp_path, cases=("telegram-origin",))
    try:
        case = fixture.meta["telegram_recovery_cases"][0]
        token, child = tg_prepare(fixture, case)
        interaction = fixture.views.recovery_open(token, request_body(case))["interaction"]
        source = fixture.service.interactions.get(interaction["interactionId"], user_subject=OWNER)["resource_id"]
        flow = TrustedFlowApplication(service=fixture.service,
            field_application=TrustedFieldApplication(submission_store=fixture.service.field_submissions),
            action_application=TrustedActionApplication(authorization_store=fixture.service.write_authorizations),
            workspace_base_url=fixture.workspace_url)
        baseline = fixture.audit()
        response = flow.get_card("input", source, presentation_id=interaction["presentation"]["presentationId"],
            csrf_cookie="", secure_cookie=False, result_token=token)
        assert response.status == 200
        config = json.loads(re.search(r'mountBusinessDraft\((\{[^\n]*\}), document, window\);', response.body.decode())[1])
        assert config["state"]["navigation"]["back"]["kind"] == "result_list"
        assert fixture.workspace_url not in config["state"]["navigation"]["back"]["url"]
        workspace = fixture.service.present_interaction(user_subject=OWNER, agent_host="openclaw",
            endpoint_key=fixture.account["endpoint_key"], interaction_id=interaction["interactionId"],
            continuous_flow=True)["interaction"]["presentation"]
        response = flow.get_card("input", source, presentation_id=workspace["presentationId"], csrf_cookie="",
            secure_cookie=False, result_token=token)
        assert response.status == 403
        assert token.encode() not in response.body
        assert b"mountBusinessDraft(" not in response.body
        assert fixture.audit() == baseline
    finally:
        fixture.close()
