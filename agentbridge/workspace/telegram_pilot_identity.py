"""Read-only counterpart check for a registered private Telegram endpoint."""

from .telegram_pilot_links import TelegramPilotLinks


def verified_sender(service, *, subject, instance, evidence, token_id=None, protocol=None):
    if not isinstance(evidence, dict) or set(evidence) != {"senderId", "botId"}:
        return False
    if not all(isinstance(value, str) for value in evidence.values()):
        return False
    try:
        return TelegramPilotLinks(service).verified(
            subject, instance, evidence["botId"], evidence["senderId"], token_id=token_id, protocol=protocol
        )
    except (TypeError, ValueError):
        return False


def historical_private_binding(service, endpoint, instance):
    """Establish the identities and grant lifetime which a historical read must match."""
    from datetime import datetime

    from agentbridge.core.actionable_results import _telegram_endpoint
    from .telegram_pilot_links import _revocation, active_claimants

    subject, sender = endpoint["user_subject"], endpoint["external_subject"]
    if not instance or endpoint["agent_host"] not in {"nanobot", "pi"} or endpoint["client_type"] != "telegram":
        return None
    with service.tasks._connect() as db:
        db.execute("BEGIN")
        try:
            current = _telegram_endpoint(db, subject, endpoint["endpoint_id"])
            if current.get("_instance_id") != instance:
                return None
            inherited = (endpoint["agent_host"] == "nanobot" and not _revocation(db, subject, instance, endpoint["account_id"])
                         and bool(active_claimants(db, sender)))
            if inherited:
                # Inherited pairings may be refreshed routinely. Issuance and creation
                # dates remain stable across those refreshes and change for a new grant.
                rows = db.execute("""SELECT e.created_at,t.created_at FROM client_endpoints e
                    JOIN mcp_identity_tokens t ON t.token_id=e.token_id AND t.user_subject=e.user_subject
                    WHERE e.state='active' AND t.state='active' AND e.agent_host='openclaw'
                    AND lower(e.client_type)='telegram' AND e.user_subject=? AND e.external_subject=?""",
                    (subject, sender)).fetchall()
                if not rows:
                    return None
                since = max(datetime.fromisoformat(value).timestamp() for row in rows for value in row)
            else:
                link = db.execute("""SELECT confirmed_at FROM workspace_telegram_pilot_links
                    WHERE owner=? AND instance=? AND bot=? AND sender=?""",
                    (subject, instance, endpoint["account_id"], sender)).fetchone()
                if not link:
                    return None
                since = link[0]
            return current["_binding_hash"], since, inherited
        except (TypeError, ValueError, OverflowError):
            return None


def historical_task_matches(task, endpoint, scope, *, exact_receipt=False):
    from datetime import datetime

    from agentbridge.core.task_state_rules import TERMINAL_TASK_STATUSES

    if (not scope or task["user_subject"] != endpoint["user_subject"] or task["agent_host"] not in {"nanobot", "pi"}
            or task["agent_host"] != endpoint["agent_host"]
            or task["origin_endpoint_id"] != endpoint["endpoint_id"]
            or task["active_conversation_ref"] != endpoint["conversation_ref"] or task["status"] not in TERMINAL_TASK_STATUSES):
        return False
    command = (task.get("summary") or {}).get("command") or {}
    if not isinstance(command, dict):
        return False
    route_hash, granted_at, inherited = scope
    if command.get("originKind") == "telegram":
        if command.get("endpointBindingHash") != route_hash:
            return False
    elif exact_receipt and inherited:
        # Without the original route signature, show only the explicit chooser.
        return False
    try:
        created = datetime.fromisoformat(task["created_at"])
        return created.tzinfo is not None and created.timestamp() >= granted_at
    except (TypeError, ValueError, OverflowError):
        return False
