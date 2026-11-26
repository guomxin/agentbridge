"""Recognize intrinsically silent events in the Pi consumer's current claim transaction."""
import json
from datetime import datetime, timezone

from agentbridge.core import actionable_results as actions
from agentbridge.core.task_state_rules import QUIET_COMPANION_EVENTS
from agentbridge.workspace.telegram_pilot_links import mirror_binding, mirror_pair_proof, mirror_sender_ids


def quiet_claims(db, rows, *, endpoint, ownership, subject, instance, pairing_ref, token_id):
    target = actions._telegram_endpoint(db, subject, endpoint["endpoint_id"])
    binding = mirror_binding(db, subject, instance, ownership)
    sender = target["external_subject"]
    if (not binding or target["agent_host"] != "pi" or target.get("_instance_id") != instance
            or not token_id or target["token_id"] != token_id or target["account_id"] != binding["bot"]
            or mirror_sender_ids(db, subject, binding) != [sender]
            or mirror_pair_proof(db, subject, binding, sender) != pairing_ref):
        raise PermissionError("Pi notification claim requires the current private pairing")
    link = db.execute("""SELECT confirmed_at FROM workspace_telegram_pilot_links
        WHERE owner=? AND instance=? AND bot=? AND sender=?""", (subject, instance, binding["bot"], sender)).fetchone()
    if not link:
        raise PermissionError("Missing original Pi pairing")

    # Require the durable event itself, including its task and envelope. A card,
    # watch, file, timeline message or similarly named foreign event cannot qualify.
    ids = [row["delivery_id"] for row in rows if row["payload_type"] == "task_event"]
    candidates = db.execute("""SELECT d.delivery_id, t.task_id, t.origin_endpoint_id, t.active_conversation_ref,
        json_extract(t.summary_json,'$.command.commandKey') AS command_key
        FROM notification_outbox d JOIN task_events e ON e.event_id=d.event_id
          AND e.task_id=d.task_id AND e.user_subject=d.user_subject
        JOIN agent_tasks t ON t.task_id=e.task_id AND t.user_subject=e.user_subject
        WHERE d.delivery_id IN (SELECT value FROM json_each(?))
        AND d.user_subject=? AND d.endpoint_id=? AND t.agent_host='pi' AND t.created_at>=?
        AND e.event_type IN (SELECT value FROM json_each(?))
        AND json_extract(d.payload_json,'$.eventId')=e.event_id
        AND json_extract(d.payload_json,'$.taskId')=e.task_id
        AND json_extract(d.payload_json,'$.eventType')=e.event_type
        AND NOT EXISTS(SELECT 1 FROM json_each(d.payload_json) WHERE key NOT IN ('eventId','taskId','eventType','payload'))
        AND json_extract(d.payload_json,'$.payload')=json(e.payload_json)""",
        (json.dumps(ids), subject, target["endpoint_id"], datetime.fromtimestamp(link[0], timezone.utc).isoformat(),
         json.dumps(sorted(QUIET_COMPANION_EVENTS))))
    enabled = set()
    command_tasks = {}
    for row in candidates:
        if (row["origin_endpoint_id"], row["active_conversation_ref"]) == (binding["endpoint_id"], binding["session_key"]):
            enabled.add(row["delivery_id"])
        elif (row["origin_endpoint_id"], row["active_conversation_ref"]) == (target["endpoint_id"], target["conversation_ref"]):
            task_id = row["task_id"]
            if task_id not in command_tasks:
                try:
                    _, command = actions._load_command(db, subject, task_id, row["command_key"])
                    command_tasks[task_id] = (command.get("originKind") == "telegram"
                                              and command.get("endpointBindingHash") == target["_binding_hash"])
                except actions.ActionableResultError:
                    command_tasks[task_id] = False
            if command_tasks[task_id]:
                enabled.add(row["delivery_id"])
    return enabled
