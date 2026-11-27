"""Reading an original native card never issues another presentation or advances its task."""
from uuid import uuid4

import pytest

from agentbridge.core.native_telegram_card_state import read_state
from tests.test_telegram_interaction_closures import login
from tests.test_telegram_notification_delivery_v2 import new_task
from tests.test_workspace_query_projection import openclaw, OWNER  # noqa: F401


def setup_card(openclaw, *, web_origin=False, standalone=False):
    fixture, token, endpoint = openclaw
    service = fixture.service
    with service.tasks._connect() as db:
        row = db.execute("SELECT registration_id,accepted_level FROM agent_host_registrations WHERE token_id=?", (token,)).fetchone()
    registration = {"registrationId": row[0], "acceptedLevel": row[1]}
    task = None
    if standalone:
        session = service.sessions.find(user_subject=OWNER, system_id="oa")
        service.sessions.mark_expired(session["session_id"])
        card = service.start_login(user_subject=OWNER, system_id="oa", expected_principal_ref=OWNER,
                                   card_base_url="https://fixture.invalid")["interaction"]
    else:
        task = new_task(fixture) if web_origin else service.tasks.ensure_task(
            user_subject=OWNER, agent_host="openclaw", host_task_key=str(uuid4()), title="Isolated original",
            origin_endpoint_id=endpoint["endpoint_id"], active_conversation_ref=endpoint["conversation_ref"],
        )[0]["task_id"]
        _, card = login(fixture, task)
    args = dict(subject=OWNER, token=token, registration=registration, host_instance="openclaw-gateway",
                host="openclaw", endpoint_key=endpoint["endpoint_key"], interaction_id=card["interactionId"])
    return service, args, task


def test_native_read_observes_original_expiry_without_task_effects(openclaw, monkeypatch):
    service, args, task = setup_card(openclaw)
    def forbidden(*args, **kwargs):
        raise AssertionError("A card-state read entered rendering or task progression")
    for obj, name in ((service, "_present_interaction_for_endpoint"), (service, "resume_interaction"),
                      (service.tasks, "link_interaction"), (service.task_plan_runtime, "handle_terminal_interaction")):
        monkeypatch.setattr(obj, name, forbidden)
    initial = read_state(service, **args)
    assert initial["interaction"] == {"interactionId": args["interaction_id"], "type": "credential",
                                      "state": "pending", "taskId": task}
    assert initial["task"]["originEndpointId"] == initial["endpoint"]["endpointId"]
    row = service.interactions.get(args["interaction_id"], user_subject=OWNER)
    with service.challenges._connect() as db:
        db.execute("UPDATE auth_challenges SET expires_at=? WHERE challenge_id=?",
                   ("2000-01-01T00:00:00+00:00", service._credential_challenge_id(row)))
    result = read_state(service, **args)
    assert result["interaction"] == {**initial["interaction"], "state": "expired"}
    assert service.tasks.get_task(task, user_subject=OWNER)["status"] == "waiting_user"


@pytest.mark.parametrize("change", ["subject", "token", "host_instance", "endpoint", "origin", "revoked", "group"])
def test_native_state_refuses_other_routes(openclaw, change):
    service, args, _ = setup_card(openclaw, web_origin=change=="origin")
    if change in {"subject", "token", "host_instance"}:
        args[change] = "other"
    if change == "endpoint":
        args["endpoint_key"] = "telegram:default:502"
    if change in {"revoked", "group"}:
        with service.tasks._connect() as db:
            if change == "revoked":
                db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_key=?", (args["endpoint_key"],))
            else:
                db.execute("UPDATE client_endpoints SET route_json=? WHERE endpoint_key=?",
                           ('{"channel":"telegram","to":"-100","accountId":"default"}', args["endpoint_key"]))
    with pytest.raises((KeyError, PermissionError, ValueError)):
        read_state(service, **args)


@pytest.mark.parametrize("level", ["L2", "L3"])
def test_standalone_native_login_at_supported_levels(openclaw, level):
    service, args, _ = setup_card(openclaw, standalone=True)
    with service.tasks._connect() as db:
        db.execute("UPDATE agent_host_registrations SET accepted_level=? WHERE token_id=?", (level, args["token"]))
    args["registration"]["acceptedLevel"] = level
    observed = read_state(service, **args)
    assert observed["task"] is None
    assert observed["interaction"] == {"interactionId": args["interaction_id"], "type": "credential",
                                       "state": "pending", "taskId": None}


@pytest.mark.parametrize("kind", ["business_input", "execution_authorization"])
def test_native_read_does_not_mint_field_or_authorization_presentations(openclaw, kind):
    from agentbridge.core.user_grants import PERMISSIONS
    from tests.authorization_fixtures import grant_permissions
    from tests.support.interaction_acceptance import COMMIT, PREPARE

    service, args, task = setup_card(openclaw)
    grant_permissions(service.user_grants, OWNER, [name for name, value in PERMISSIONS.items()
                      if "oa:write:draft" in value["legacy_scopes"] or "oa:write:approval" in value["legacy_scopes"]])
    op, _ = service.operations.create(user_subject=OWNER, capability_name=PREPARE,
                                      capability_version="1", input_summary={})
    common = dict(user_subject=OWNER, system_id="oa", session_id="fixture-session",
                  capability_version="1", card_base_url="https://fixture.invalid")
    if kind == "business_input":
        resource = service.field_submissions.create(**common, create_operation_id=op["operation_id"],
                    capability_name=PREPARE, form_schema={"fields": [{"name": "reason", "label": "事项"}]})
        card = service._business_input_interaction(resource)
    else:
        resource = service.write_authorizations.create(**common, prepare_operation_id=op["operation_id"],
                    capability_name=COMMIT, plan={"synthetic": True}, summary={"title": "合成授权"})
        card = service._execution_authorization_interaction(resource)
    args["interaction_id"] = card["interactionId"]
    record = service.interactions.get(card["interactionId"], user_subject=OWNER)
    service.tasks.link_interaction(task_id=task, user_subject=OWNER, interaction_record=record, interaction=card)
    with service.tasks._connect() as db:
        def counts():
            return tuple(db.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in
                         ("field_submission_presentations", "write_authorization_presentations", "operations", "task_events"))
        before = counts()
        for _ in range(2):
            answer = read_state(service, **args)
            assert answer["interaction"] == {"interactionId": card["interactionId"], "type": kind,
                                            "state": "pending", "taskId": task}
        assert counts() == before
