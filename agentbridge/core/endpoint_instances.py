"""Resolve current endpoint ownership without using the user's mutable default host."""

from collections import defaultdict
import json

PRIVATE_BOT_HOSTS = frozenset({"nanobot", "pi"})
LEGACY_OPENCLAW = ("openclaw", "openclaw-default")


def instance_key(host, instance):
    if host == "openclaw" and instance in {"openclaw-gateway", "openclaw-default"}:
        return LEGACY_OPENCLAW
    return host, instance


def endpoint_instances(db, user_subject):
    endpoints = db.execute("SELECT * FROM client_endpoints WHERE user_subject=? AND state='active'",
                           (user_subject,)).fetchall()
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    bindings = {}
    known = defaultdict(set)
    directory = {}
    legacy_web = None
    if "workspace_accounts" in tables:
        legacy_web = db.execute("""SELECT endpoint_id,endpoint_key,openclaw_session_key FROM workspace_accounts
                                WHERE user_subject=? AND state='active'""", (user_subject,)).fetchone()
    if "workspace_host_bindings" in tables:
        directory = {instance_key(row["host_type"], row["instance_id"]): row for row in db.execute(
            """SELECT h.host_type,h.instance_id,h.state,(h.all_subjects OR EXISTS (
                SELECT 1 FROM workspace_host_visibility v WHERE v.instance_id=h.instance_id
                AND v.user_subject=?)) AS visible FROM workspace_host_instances h""", (user_subject,))}
        rows = db.execute("""SELECT b.*, h.host_type,h.state,h.all_subjects,a.state AS account_state,
                    (h.all_subjects OR EXISTS (SELECT 1 FROM workspace_host_visibility v
                     WHERE v.instance_id=h.instance_id AND v.user_subject=a.user_subject)) AS visible
                FROM workspace_host_bindings b JOIN workspace_accounts a USING(account_id)
                JOIN workspace_host_instances h USING(instance_id) WHERE a.user_subject=?""",
                          (user_subject,)).fetchall()
        bindings = {row["endpoint_id"]: row for row in rows}
        for row in rows:
            known[row["host_type"]].add(row["instance_id"])
    registered = defaultdict(set)
    if "agent_host_registrations" in tables:
        for token, host, instance in db.execute("""SELECT token_id,agent_host,host_instance_id
                FROM agent_host_registrations WHERE user_subject=?""", (user_subject,)):
            scope = instance_key(host, instance)
            registered[(host, token)].add(scope)
            if scope != LEGACY_OPENCLAW:
                known[host].add(scope[1])

    result = {}
    private_web = []
    for e in endpoints:
        eid, host = e["endpoint_id"], e["agent_host"]
        if eid in bindings:
            b = bindings[eid]
            if (b["host_type"] == host and b["account_state"] == "active" and b["visible"]
                    and b["state"] != "revoked" and e["client_type"] in {"web", "webchat"}
                    and e["account_id"] == e["external_subject"] == b["account_id"]
                    and (e["endpoint_key"], e["conversation_ref"]) == (b["endpoint_key"], b["session_key"])):
                result[eid] = instance_key(host, b["instance_id"])
                if host in PRIVATE_BOT_HOSTS and b["state"] == "enabled":
                    private_web.append(b)
            continue
        if host in PRIVATE_BOT_HOSTS:
            continue
        if host == "openclaw" and legacy_web and tuple(legacy_web) == (eid, e["endpoint_key"], e["conversation_ref"]):
            result[eid] = LEGACY_OPENCLAW
            continue
        registrations = registered[(host, e["token_id"])]
        if len(registrations) == 1:
            result[eid] = next(iter(registrations))
        elif not registrations and host == "openclaw" and not known[host]:
            # There was only one OpenClaw before the Workspace directory existed.
            result[eid] = LEGACY_OPENCLAW

    if private_web and "workspace_telegram_pilot_links" in tables:
        from agentbridge.workspace.telegram_pilot_links import telegram_conversation_ref, verified_pair
        for e in endpoints:
            if e["agent_host"] not in PRIVATE_BOT_HOSTS or e["client_type"] != "telegram":
                continue
            bot, sender = e["account_id"], e["external_subject"]
            if (not bot or not bot.isdecimal() or not sender.isdecimal() or
                    e["endpoint_key"] != f"telegram:{bot}:{sender}" or
                    json.loads(e["route_json"]) != {"channel": "telegram", "accountId": bot, "to": sender}):
                continue
            for b in private_web:
                if b["host_type"] != e["agent_host"]:
                    continue
                instance = b["instance_id"]
                if e["conversation_ref"] != telegram_conversation_ref(instance, user_subject, b["endpoint_id"], bot, sender):
                    continue
                if "mcp_identity_tokens" in tables and verified_pair(
                        db, user_subject, instance, bot, sender, allow_new_pair=not b["all_subjects"]):
                    result[e["endpoint_id"]] = (e["agent_host"], instance)
    granted = {result[eid] for eid in bindings if eid in result}
    return {eid: scope for eid, scope in result.items()
            if scope not in directory or
            (directory[scope]["state"] != "revoked" and directory[scope]["visible"]
             and (scope == LEGACY_OPENCLAW or scope in granted))}
