"""Independent real-unknown cross-channel acceptance, without a live OA account."""
import json
import re
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import threading
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from agentbridge.auth.result_view import TrustedResultViewApplication
from agentbridge.core.business_drafts import BusinessDraftService
from agentbridge.core.oa_pending_recovery import (
    ACK_VERSION, prepare_recovery_attempt, probe_unknown_pending,
)
from tests.support.full_business_acceptance import OWNER
from tests.support.telegram_recovery_acceptance import TelegramRecoveryAcceptanceFixture
from tests.test_full_business_acceptance import decide, drive


def request_body(case):
    return {"taskId": case["task_id"], "requestKey": uuid4().hex}


def tg_prepare(fixture, case):
    token = fixture.meta["telegram_recovery_query"]["url"].rsplit("/", 1)[1]
    response = fixture.views.recovery_probe(token, request_body(case))
    assessment = response["assessment"]
    body = {**request_body(case), "assessmentId": assessment["assessmentId"],
        "assessmentHash": assessment["assessmentHash"], "expectedRevision": assessment["revision"],
        "acknowledgement": ACK_VERSION}
    first = fixture.views.recovery_prepare(token, body)
    repeated = fixture.views.recovery_prepare(token, body)
    assert first["recoveryTaskId"] == repeated["recoveryTaskId"]
    return token, first["recoveryTaskId"]


def workspace_prepare(fixture, case):
    assessment = probe_unknown_pending(fixture.service, subject=OWNER, task_id=case["task_id"],
        endpoint_id=fixture.account["endpoint_id"], request_key=uuid4().hex)
    return prepare_recovery_attempt(fixture.service, subject=OWNER, task_id=case["task_id"],
        endpoint_id=fixture.account["endpoint_id"], request_key=uuid4().hex,
        assessment_id=assessment["assessmentId"], assessment_hash=assessment["assessmentHash"],
        expected_revision=assessment["revision"], acknowledgement=ACK_VERSION)["taskId"]


def approve_opened(fixture, child, interaction):
    record = fixture.service.interactions.get(interaction["interactionId"], user_subject=OWNER)
    drafts = BusinessDraftService(fixture.service)
    presentation = interaction["presentation"]["presentationId"]
    opened = drafts.open(record["resource_id"], presentation_id=presentation)
    assert not opened["state"]["values"].get("opinion")
    credentials = {"source_submission_id": record["resource_id"], "presentation_id": presentation,
        "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
    saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"],
        values={"opinion": "全新且明确的合成跨端恢复决定。"})
    preview = drafts.preview(**credentials, expected_revision=saved["revision"])
    first = decide(drafts, credentials, preview)
    assert decide(drafts, credentials, preview)["receipt"] == first["receipt"]
    case = {"task_id": child, "input_id": record["resource_id"], "input_interaction": interaction["interactionId"]}
    drive(fixture, case)
    drive(fixture, case)
    assert fixture.service.tasks.get_task(child, user_subject=OWNER)["status"] == "succeeded"
    return first["receipt"]


@pytest.mark.parametrize("origin,first_channel", [("telegram-origin", "telegram"), ("workspace-origin", "workspace")])
def test_one_unknown_one_child_across_workspace_and_telegram(tmp_path, origin, first_channel):
    fixture = TelegramRecoveryAcceptanceFixture(tmp_path, cases=(origin,))
    try:
        case = fixture.meta["telegram_recovery_cases"][0]
        initial = fixture.audit()
        if first_channel == "telegram":
            token, child = tg_prepare(fixture, case)
            assert workspace_prepare(fixture, case) == child
        else:
            child = workspace_prepare(fixture, case)
            token, returned = tg_prepare(fixture, case)
            assert returned == child
        task = fixture.service.tasks.get_task(child, user_subject=OWNER)
        original_endpoint = task["origin_endpoint_id"]
        opened = fixture.views.recovery_open(token, request_body(case))
        interaction = opened["interaction"]
        assert interaction["presentation"]["endpointId"] == fixture.telegram_endpoint["endpoint_id"]
        assert parse_qs(urlsplit(interaction["presentation"]["url"]).query) == {"result": [token]}
        back = fixture.views.navigation_back(token, task_id=child, interaction_id=interaction["interactionId"],
            endpoint_id=fixture.telegram_endpoint["endpoint_id"], presentation_id=interaction["presentation"]["presentationId"])
        assert back["url"] == "/results/" + token
        assert fixture.service.tasks.get_task(child, user_subject=OWNER)["origin_endpoint_id"] == original_endpoint
        before_decision = fixture.audit()
        assert before_decision["decisions"] == initial["decisions"]
        assert before_decision["syntheticWrites"] == initial["syntheticWrites"]
        assert before_decision["recoveryChildren"] == 1
        # GET/projection cannot mint further presentations or a decision.
        with fixture.service.tasks._connect() as db:
            presentations = db.execute("SELECT count(*) FROM field_submission_presentations").fetchone()[0]
        for _ in range(2):
            fixture.views.read(token)
            fixture.views.status(token, case["task_id"])
        with fixture.service.tasks._connect() as db:
            assert db.execute("SELECT count(*) FROM field_submission_presentations").fetchone()[0] == presentations
        receipt = approve_opened(fixture, child, interaction)
        receipt_presentation = receipt["flowUrl"].rsplit("/", 1)[1]
        receipt_back = fixture.views.navigation_back(token, task_id=child, interaction_id=receipt["interactionId"],
            endpoint_id=fixture.telegram_endpoint["endpoint_id"], presentation_id=receipt_presentation)
        assert receipt_back["url"] == "/results/" + token
        final = fixture.audit()
        assert final["decisions"] == initial["decisions"] + 1
        assert final["syntheticWrites"] == initial["syntheticWrites"] + 1
        assert final["authorizations"] == {"consumed": initial["decisions"] + 1}
        assert final["recoveryChildren"] == 1 and final["modelDispatches"] == 0
        assert all(row["authorizationUnchanged"] and row["operationStatus"] == "unknown" for row in final["originals"])
    finally:
        fixture.close()


def test_loopback_result_csrf_recovery_open_and_navigation_are_real_http(tmp_path):
    fixture = TelegramRecoveryAcceptanceFixture(tmp_path, cases=("workspace-origin",))
    servers = fixture.servers()
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    cookies = {}
    host = urlsplit(fixture.card_url)

    def request(method, path, body=None, *, proof=None):
        connection = HTTPConnection(host.hostname, host.port, timeout=40)
        headers = {"Origin": fixture.card_url, "Sec-Fetch-Site": "same-origin"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
        if proof:
            headers["X-AgentBridge-Result-Token"] = proof
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
            response = connection.getresponse()
            content = response.read().decode()
            for key, value in response.getheaders():
                if key.lower() == "set-cookie":
                    cookies.update({name: morsel.value for name, morsel in SimpleCookie(value).items()})
            return response.status, content
        finally:
            connection.close()

    try:
        case = fixture.meta["telegram_recovery_cases"][0]
        path = urlsplit(fixture.meta["telegram_recovery_query"]["url"]).path
        initial = fixture.audit()
        status, page = request("GET", path)
        assert status == 200
        config = json.loads(re.search(r'id="result-data">([^<]+)</script>', page)[1])
        for suffix in ("recovery-probe", "recovery-prepare", "recovery-open", "open"):
            status, _ = request("GET", path + "/" + suffix)
            assert status == 405
        status, denied = request("POST", path + "/recovery-probe", request_body(case))
        assert status == 403 and json.loads(denied)["error"]["code"] == "RESULT_CSRF_INVALID"
        status, probed = request("POST", path + "/recovery-probe", request_body(case), proof=config["csrfToken"])
        assert status in {200, 202}, probed
        assessment = json.loads(probed)["assessment"]
        body = {**request_body(case), "assessmentId": assessment["assessmentId"],
            "assessmentHash": assessment["assessmentHash"], "expectedRevision": assessment["revision"],
            "acknowledgement": ACK_VERSION}
        status, prepared = request("POST", path + "/recovery-prepare", body, proof=config["csrfToken"])
        assert status in {200, 202}, prepared
        child = json.loads(prepared)["recoveryTaskId"]
        status, opened = request("POST", path + "/recovery-open", request_body(case), proof=config["csrfToken"])
        assert status in {200, 202}, opened
        interaction = json.loads(opened)["interaction"]
        url = urlsplit(interaction["presentation"]["url"])
        status, form = request("GET", url.path + "?" + url.query)
        assert status == 200, form[:300]
        assert "返回待办列表" in form
        assert fixture.service.tasks.get_task(child, user_subject=OWNER)["status"] == "waiting_user"
        final = fixture.audit()
        assert final["decisions"] == initial["decisions"]
        assert final["syntheticWrites"] == initial["syntheticWrites"]
        assert final["recoveryChildren"] == 1
        assert final["originals"][0]["authorizationUnchanged"]
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        fixture.close()


def test_real_batch_recovers_only_second_unknown_item_and_never_advances_third(tmp_path):
    fixture = TelegramRecoveryAcceptanceFixture(tmp_path, cases=("batch-current",))
    try:
        case = fixture.meta["telegram_recovery_cases"][0]
        initial = fixture.audit()
        token, child = tg_prepare(fixture, case)
        opened = fixture.views.recovery_open(token, request_body(case))
        task = fixture.service.tasks.get_task(child, user_subject=OWNER)
        assert task["summary"]["oaPendingRecovery"]["affairId"] == case["affair_ids"][1]
        approve_opened(fixture, child, opened["interaction"])
        final = fixture.audit()
        assert final["decisions"] == initial["decisions"] + 1
        assert final["syntheticWrites"] == initial["syntheticWrites"] + 1
        batch = final["originals"][0]["batch"]
        assert batch == {"state": "outcome_unknown", "current_ordinal": 2, "succeeded_count": 1, "total_count": 3}
        assert fixture.oa.data["affairs"][case["affair_ids"][0]]["processed"] is True
        assert fixture.oa.data["affairs"][case["affair_ids"][2]]["processed"] is False
        assert final["originals"][0]["authorizationUnchanged"]
    finally:
        fixture.close()


def test_readonly_complete_conflict_and_csrf_do_not_create_business_decisions(tmp_path):
    fixture = TelegramRecoveryAcceptanceFixture(tmp_path, cases=("complete", "conflict"))
    try:
        token = fixture.meta["telegram_recovery_query"]["url"].rsplit("/", 1)[1]
        application = TrustedResultViewApplication(service=fixture.service)
        initial = fixture.audit()
        for case, expected in zip(fixture.meta["telegram_recovery_cases"], ("verified_complete", "blocked")):
            denied = application.prepare(token, token="0" * 64, cookie="x" * 43,
                content_type="application/json", body=json.dumps(request_body(case)).encode(), action="recovery-probe")
            assert denied.status == 403
            result = fixture.views.recovery_probe(token, request_body(case))
            assert result["assessment"]["status"] == expected
            assert result["oaPendingRecovery"]["canPrepare"] is False
        final = fixture.audit()
        assert final["decisions"] == initial["decisions"]
        assert final["syntheticWrites"] == initial["syntheticWrites"]
        assert final["recoveryChildren"] == 0
        assert all(row["authorizationUnchanged"] for row in final["originals"])
    finally:
        fixture.close()
