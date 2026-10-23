"""Telegram recovery denies stale authority and keeps return navigation read-only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import shutil
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from agentbridge.core import actionable_results as actions
from agentbridge.core import oa_pending_recovery as recovery
from agentbridge.core.business_drafts import BusinessDraftService
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from tests.authorization_fixtures import grant_permissions
from tests.support.full_business_acceptance import OWNER
from tests.support.telegram_recovery_acceptance import TelegramRecoveryAcceptanceFixture
from tests.test_full_business_acceptance import decide, drive


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    home = tmp_path_factory.mktemp("telegram-recovery-security")
    fixture = TelegramRecoveryAcceptanceFixture(home, cases=("workspace-origin",))
    fixture.close()
    return home


@pytest.fixture
def fixture(tmp_path, template):
    home = tmp_path / "isolated"
    shutil.copytree(template, home)
    fixture = TelegramRecoveryAcceptanceFixture(home, cases=())
    try:
        yield fixture
    finally:
        fixture.close()


def request(fixture):
    return {"taskId": fixture.meta["telegram_recovery_cases"][0]["task_id"], "requestKey": uuid4().hex}


def ticket(fixture):
    return fixture.meta["telegram_recovery_query"]["url"].rsplit("/", 1)[1]


def assessment_body(fixture):
    value = fixture.views.recovery_probe(ticket(fixture), request(fixture))["assessment"]
    return {**request(fixture), "assessmentId": value["assessmentId"], "assessmentHash": value["assessmentHash"],
        "expectedRevision": value["revision"], "acknowledgement": recovery.ACK_VERSION}


def prepare(fixture):
    return fixture.views.recovery_prepare(ticket(fixture), assessment_body(fixture))["recoveryTaskId"]


def ledger(fixture):
    with fixture.service.tasks._connect() as db:
        return tuple(db.execute("SELECT (SELECT count(*) FROM business_draft_decisions),"
            "(SELECT count(*) FROM write_authorizations),(SELECT count(*) FROM field_submission_presentations),"
            "(SELECT count(*) FROM task_events WHERE event_type='task.oa_recovery.child_created')").fetchone())


@pytest.mark.parametrize("mutation", ["missing", "extra", "bad_task", "foreign", "blank_nonce", "raw_url"])
def test_closed_request_cannot_select_an_arbitrary_target(fixture, mutation):
    body = request(fixture)
    if mutation == "missing": body.pop("taskId")
    elif mutation == "extra": body["affairId"] = "another-affair"
    elif mutation == "bad_task": body["taskId"] = "../other"
    elif mutation == "foreign": body["taskId"] = fixture.meta["telegram_recovery_query"]["task_id"]
    elif mutation == "blank_nonce": body["requestKey"] = ""
    else: body["returnUrl"] = "https://untrusted.example/"
    before = ledger(fixture)
    with pytest.raises((actions.ActionableResultError, recovery.OaPendingRecoveryError, PermissionError)):
        fixture.views.recovery_probe(ticket(fixture), body)
    assert ledger(fixture) == before


@pytest.mark.parametrize("mutation", ["ack", "hash", "revision", "assessment", "expired", "workspace_bound"])
def test_new_child_requires_exact_fresh_endpoint_bound_assessment_and_explicit_ack(fixture, mutation):
    body = assessment_body(fixture)
    if mutation == "ack": body["acknowledgement"] = "true"
    elif mutation == "hash": body["assessmentHash"] = "sha256:" + "0" * 64
    elif mutation == "revision": body["expectedRevision"] += 1
    elif mutation == "assessment": body["assessmentId"] = uuid4().hex
    elif mutation == "workspace_bound":
        value = recovery.probe_unknown_pending(fixture.service, subject=OWNER, task_id=body["taskId"],
            endpoint_id=fixture.account["endpoint_id"], request_key=uuid4().hex)
        body.update(assessmentId=value["assessmentId"], assessmentHash=value["assessmentHash"], expectedRevision=value["revision"])
    before = ledger(fixture)
    with patch.object(recovery, "_now", return_value=recovery._now() + timedelta(seconds=121 if mutation == "expired" else 0)):
        with pytest.raises(recovery.OaPendingRecoveryError):
            fixture.views.recovery_prepare(ticket(fixture), body)
    assert ledger(fixture) == before


@pytest.mark.parametrize("mutation", ["route", "token", "principal", "grants", "skill"])
def test_revoked_current_authority_blocks_probe_prepare_and_cross_endpoint_open(fixture, mutation):
    prepare(fixture)
    body = assessment_body(fixture)
    if mutation == "route":
        with fixture.service.tasks._connect() as db:
            db.execute("UPDATE client_endpoints SET route_json=? WHERE endpoint_id=?",
                (json.dumps({"channel": "telegram", "to": "2002"}), fixture.telegram_endpoint["endpoint_id"]))
    elif mutation == "token":
        McpIdentityTokenStore(fixture.service.db_path).revoke(fixture.meta["telegram_recovery_identity"]["token_id"])
    elif mutation == "principal":
        session = fixture.service.sessions.find(user_subject=OWNER, system_id="oa")
        fixture.service.sessions.rebind_expected_principal(session["session_id"], expected_principal_ref="different-account", reason="test")
        fixture.service.sessions.activate(session["session_id"], observed_principal_ref="different-account")
    elif mutation == "grants":
        grant_permissions(fixture.service.user_grants, OWNER, ["oa.workflow.read", "oa.leave.approve"])
    else:
        binding = fixture.service.skills.for_task(OWNER, body["taskId"])
        # A directly prepared Workspace task need not originally use a Skill.
        if binding is None:
            binding_id = fixture.service.skills.bind(OWNER, "oa-pending", "single")
            fixture.service.skills.attach(OWNER, body["taskId"], binding_id)
        else:
            binding_id = binding["binding_id"]
        with fixture.service.tasks._connect() as db:
            db.execute("UPDATE skill_bindings SET revoked=1 WHERE binding_id=?", (binding_id,))
    before = ledger(fixture)
    for method, value in ((fixture.views.recovery_probe, request(fixture)),
                          (fixture.views.recovery_prepare, body), (fixture.views.recovery_open, request(fixture))):
        with pytest.raises((actions.ActionableResultError, recovery.OaPendingRecoveryError, PermissionError, ValueError)):
            method(ticket(fixture), value)
    assert ledger(fixture) == before


def test_concurrent_open_is_one_presentation_and_never_an_execution_decision(fixture):
    child = prepare(fixture)
    body, token = request(fixture), ticket(fixture)
    before = ledger(fixture)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: fixture.views.recovery_open(token, body), range(2)))
    presentations = [value["interaction"]["presentation"] for value in results]
    assert presentations[0] == presentations[1]
    assert ledger(fixture)[:2] == before[:2]
    with fixture.service.tasks._connect() as db:
        assert db.execute("SELECT count(*) FROM task_events WHERE task_id=? AND event_type='task.result_view.opened'", (child,)).fetchone()[0] == 1
        assert token not in " ".join(row[0] for row in db.execute("SELECT payload_json FROM task_events"))
    assert fixture.service.tasks.get_task(child, user_subject=OWNER)["origin_endpoint_id"] == fixture.telegram_endpoint["endpoint_id"]


def test_navigation_binds_exact_opened_presentation_and_immutable_confirmation_receipt(fixture):
    child = prepare(fixture)
    token = ticket(fixture)
    opened = fixture.views.recovery_open(token, request(fixture))["interaction"]
    second = fixture.views.recovery_open(token, request(fixture))["interaction"]
    assert second["presentation"] == opened["presentation"]
    record = fixture.service.interactions.get(opened["interactionId"], user_subject=OWNER)
    source, presentation = record["resource_id"], opened["presentation"]["presentationId"]
    workspace = fixture.service.field_submissions.create_presentation(source, user_subject=OWNER, endpoint_id=fixture.account["endpoint_id"])
    kwargs = dict(task_id=child, interaction_id=opened["interactionId"], endpoint_id=fixture.telegram_endpoint["endpoint_id"])
    for invalid in (workspace["presentation_id"], "x" * 43, None):
        with pytest.raises(actions.ActionableResultError):
            fixture.views.navigation_back(token, **kwargs, presentation_id=invalid)
    foreign_ticket = fixture.query_telegram()["url"].rsplit("/", 1)[1]
    with pytest.raises(actions.ActionableResultError):
        fixture.views.navigation_back(foreign_ticket, **kwargs, presentation_id=presentation)
    drafts = BusinessDraftService(fixture.service)
    state = drafts.open(source, presentation_id=presentation)
    credentials = dict(source_submission_id=source, presentation_id=presentation,
        csrf_token=state["csrfToken"], csrf_cookie=state["csrfToken"])
    saved = drafts.save(**credentials, expected_revision=state["state"]["revision"], values={"opinion": "新的明确意见"})
    preview = drafts.preview(**credentials, expected_revision=saved["revision"])
    receipt = decide(drafts, credentials, preview)["receipt"]
    auth_presentation = urlsplit(receipt["flowUrl"]).path.rsplit("/", 1)[1]
    before = ledger(fixture)
    assert fixture.views.navigation_back(token, task_id=child, interaction_id=receipt["interactionId"],
        endpoint_id=fixture.telegram_endpoint["endpoint_id"], presentation_id=auth_presentation)["url"] == "/results/" + token
    assert ledger(fixture) == before
    drive(fixture, {"task_id": child, "input_id": source, "input_interaction": opened["interactionId"]})
    assert fixture.service.tasks.get_task(child, user_subject=OWNER)["status"] == "succeeded"
    before = ledger(fixture)
    with patch.object(fixture.service, "present_interaction", side_effect=AssertionError("Terminal opens may not issue")):
        terminal = fixture.views.recovery_open(token, request(fixture))
    assert terminal["interaction"] is None
    assert terminal["oaPendingRecovery"]["status"] == "recovery_succeeded"
    assert ledger(fixture) == before


def test_get_never_restores_executable_assessment_or_private_permit(fixture):
    fixture.views.recovery_probe(ticket(fixture), request(fixture))
    before = ledger(fixture)
    with patch.object(fixture.service, "present_interaction", side_effect=AssertionError("GET may not issue")), \
            patch.object(recovery, "_live", side_effect=AssertionError("GET may not read OA")):
        result = fixture.views.status(ticket(fixture), request(fixture)["taskId"])
    projection = result["oaPendingRecovery"]
    assert projection["canPrepare"] is projection["canExecute"] is False
    assert not {"assessmentId", "assessmentHash", "sessionHash", "scopeHash", "permit"}.intersection(projection)
    assert "assessment" not in result and "interaction" not in result
    assert ledger(fixture) == before
