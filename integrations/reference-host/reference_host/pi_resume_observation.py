"""Validate the public Pi resume-observation wire contract without central imports.

Keep the client predicate aligned with the central issuer through the shared
positive and adversarial vectors in tests/test_pi_resume_observation.py.
"""

META_KEY = "io.agentbridge/pi-resume-observation"
SCHEMA = "agentbridge.pi-resume-observation.v1"


def fixed_references(payload):
    """Only the simple field→authorization and verified commit responses are eligible."""
    if not isinstance(payload, dict):
        return None
    operation = payload.get("operationId")
    if not isinstance(operation, str) or not operation or payload.get("plan") or payload.get("batch"):
        return None
    successor = None
    if payload.get("status") == "requires_user_action":
        interaction = payload.get("interaction")
        next_action = payload.get("nextAction")
        if (not isinstance(interaction, dict) or interaction.get("type") != "execution_authorization"
                or not isinstance(next_action, dict) or next_action.get("type") != "resume_interaction"):
            return None
        successor = interaction.get("interactionId")
        if not isinstance(successor, str) or not successor:
            return None
    elif (payload.get("status") != "succeeded" or payload.get("interaction") is not None
          or payload.get("nextAction") is not None):
        return None

    # Unlike the general model-facing reference collector, never optimize a
    # partial traversal. Oversized or extended result shapes use the original RPC.
    work = [payload]
    remaining = 4096
    while work:
        item = work.pop()
        if isinstance(item, (dict, list)):
            remaining -= len(item)
            if remaining < 0:
                return None
            if isinstance(item, dict):
                if ("operationId" in item and item["operationId"] != operation
                        or "interactionId" in item and item["interactionId"] != successor):
                    return None
                work.extend(item.values())
            else:
                work.extend(item)
    return [operation], [successor] if successor else []


def matches(receipt, payload, *, private_interaction, nonce, resumed_from, task_id, subject,
            endpoint, conversation, minimum_version):
    if (not isinstance(payload, dict) or not isinstance(receipt, dict) or not nonce or receipt.get("schema") != SCHEMA
            or receipt.get("nonce") != nonce or receipt.get("resumedFrom") != resumed_from
            or payload.get("resumedFromInteractionId") != resumed_from or payload.get("taskId") != task_id):
        return False
    refs = fixed_references(payload)
    if not refs or (receipt.get("operationIds"), receipt.get("interactionIds")) != refs:
        return False
    if refs[1]:
        if (not isinstance(private_interaction, dict) or private_interaction.get("interactionId") != refs[1][0]
                or private_interaction.get("type") != "execution_authorization"):
            return False
    elif private_interaction is not None:
        return False
    task = receipt.get("task")
    version = receipt.get("taskVersion")
    if (not isinstance(task, dict) or type(version) is not int or minimum_version < 1 or version < minimum_version
            or type(task.get("version")) is not int or task["version"] != version):
        return False
    identities = (task.get("agentHost"), task.get("userSubject"), task.get("taskId"),
                  task.get("originEndpointId"), task.get("activeConversationRef"))
    if identities != ("pi", subject, task_id, endpoint, conversation) or task.get("currentOperationId") != refs[0][0]:
        return False
    if refs[1]:
        return task.get("status") == "waiting_user" and task.get("currentInteractionId") == refs[1][0]
    return (task.get("status") == "succeeded" and "currentInteractionId" in task
            and task["currentInteractionId"] in (None, resumed_from))
