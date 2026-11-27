"""Public state of a new native Telegram card, read by its registered host."""
import json

from .actionable_results import _telegram_endpoint
from .endpoint_instances import endpoint_instances, instance_key
from .interactions import project_interaction_state


def read_state(service, *, subject, token, registration, host_instance, host, endpoint_key, interaction_id):
    endpoint = service.tasks.endpoint_for_key(user_subject=subject, agent_host=host, endpoint_key=endpoint_key)
    if (host not in {"openclaw", "nanobot", "pi"} or endpoint["client_type"] != "telegram"
            or endpoint["token_id"] != token or endpoint["state"] != "active"):
        raise PermissionError("An active original private Telegram endpoint is required")
    with service.tasks._connect() as db:
        db.execute("BEGIN")
        endpoint = _telegram_endpoint(db, subject, endpoint["endpoint_id"])
        registered = db.execute("""SELECT 1 FROM agent_host_registrations WHERE registration_id=?
            AND user_subject=? AND agent_host=? AND host_instance_id=? AND token_id=? AND accepted_level=?""",
            (registration["registrationId"], subject, host, host_instance, token, registration["acceptedLevel"])).fetchone()
        owner = ((host, endpoint["_instance_id"]) if "_instance_id" in endpoint
                 else endpoint_instances(db, subject).get(endpoint["endpoint_id"]))
        if not registered or endpoint["token_id"] != token or owner != instance_key(host, host_instance):
            raise PermissionError("The original host registration and endpoint must still match")

    record = service.interactions.get(interaction_id, user_subject=subject)
    kind = record["interaction_type"]
    task_id = service.tasks.task_id_for_interaction(interaction_id, user_subject=subject)
    task_info = None
    if task_id:
        task = service.tasks.get_task(task_id, user_subject=subject)
        if (task["agent_host"] != host or task["origin_endpoint_id"] != endpoint["endpoint_id"]
                or task["active_conversation_ref"] != endpoint["conversation_ref"]):
            raise PermissionError("The interaction belongs to another task origin")
        service.require_task_result_access(user_subject=subject, task_id=task_id)
        task_info = {"taskId": task_id, "userSubject": subject, "agentHost": host,
                     "originEndpointId": task["origin_endpoint_id"],
                     "activeConversationRef": task["active_conversation_ref"]}
    elif kind != "credential" or registration["acceptedLevel"] != "L2":
        raise PermissionError("A native task is required for this interaction")

    if record.get("operation_id"):
        op = service.operations.get(record["operation_id"])
        service.user_grants.require_capability(subject, op["capability_name"], op.get("input_summary"))
    # Store getters apply the resource's existing expiry. Rendering, presentation
    # issuance, coordinator progression and resume are all outside this read.
    resource = service._interaction_resource(record)
    state, _, _ = project_interaction_state(kind, str(resource["state"]))
    route = json.loads(endpoint["route_json"])
    public_endpoint = {"endpointId": endpoint["endpoint_id"], "userSubject": subject, "agentHost": host,
                       "clientType": "telegram", "externalSubject": endpoint["external_subject"],
                       "accountId": endpoint["account_id"], "conversationRef": endpoint["conversation_ref"],
                       "route": {k: route[k] for k in ("channel", "to", "accountId", "threadId") if k in route},
                       "state": endpoint["state"]}
    return {"status": "succeeded", "protocolVersion": "0.1", "endpoint": public_endpoint, "task": task_info,
            "interaction": {"interactionId": interaction_id, "type": kind, "state": state, "taskId": task_id}}
