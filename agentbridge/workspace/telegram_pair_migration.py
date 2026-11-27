"""Evidence for migrating an existing inherited private NanoBot pairing."""
import hashlib
from datetime import datetime
import math
import json

from agentbridge.core.workspace_query_delivery import VERSIONS, registered


PROTOCOL_META = "io.agentbridge/telegram-pairing-version"


def initialize(db):
    columns = {r[1] for r in db.execute("PRAGMA table_info(workspace_telegram_pilot_challenges)")}
    if "present_token_id" not in columns:
        db.execute("ALTER TABLE workspace_telegram_pilot_challenges ADD COLUMN present_token_id TEXT")
    if "legacy_proof" not in columns:
        db.execute("ALTER TABLE workspace_telegram_pilot_challenges ADD COLUMN legacy_proof TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS workspace_telegram_independent_pairs(
        instance TEXT NOT NULL, bot TEXT NOT NULL, owner TEXT NOT NULL, minimum_protocol INTEGER NOT NULL,
        PRIMARY KEY(instance,bot,owner))""")


def independent(db, subject, instance, bot):
    return db.execute("""SELECT minimum_protocol FROM workspace_telegram_independent_pairs
        WHERE instance=? AND bot=? AND owner=?""", (instance, bot, subject)).fetchone()


def deployment_token(db, subject, instance, bot):
    """Match the registered personal consumer to a Web or exact native private endpoint."""
    from .nanobot_gateway import route_specs_for
    from .telegram_pilot_links import telegram_conversation_ref

    row = db.execute("""SELECT e.endpoint_id,e.token_id,h.route_ref,h.adapter_version,b.credential_ref
        FROM workspace_host_bindings b JOIN workspace_accounts a USING(account_id)
        JOIN client_endpoints e ON e.endpoint_id=b.endpoint_id JOIN workspace_host_instances h USING(instance_id)
        WHERE a.user_subject=? AND b.instance_id=? AND h.host_type='nanobot' AND a.state='active'
        AND h.state='enabled' AND h.all_subjects=0 AND e.user_subject=a.user_subject AND e.state='active'
        AND EXISTS(SELECT 1 FROM workspace_host_visibility v WHERE v.instance_id=h.instance_id AND v.user_subject=a.user_subject)""",
        (subject, instance)).fetchone()
    if not row or row["adapter_version"] != VERSIONS["nanobot"]:
        return None
    try:
        spec = route_specs_for("nanobot").get(row["route_ref"]) or {}
        if (spec.get("instanceId") != instance or (spec.get("subjects") or {}).get(subject) != row["credential_ref"]
                or str((spec.get("telegram") or {}).get("botId")) != str(bot)):
            return None
    except (OSError, ValueError, TypeError):
        return None
    if not registered(db, subject, "nanobot", ("nanobot", instance), None):
        return None
    candidates = db.execute("""SELECT DISTINCT r.token_id FROM agent_host_registrations r
        JOIN mcp_identity_tokens t ON t.token_id=r.token_id AND t.user_subject=r.user_subject
        WHERE r.user_subject=? AND r.agent_host='nanobot' AND r.host_instance_id=? AND r.host_version=?
        AND r.accepted_level='L3' AND r.compatibility_status='approved' AND t.state='active'
        AND julianday(t.expires_at)>julianday('now')""", (subject, instance, VERSIONS["nanobot"])).fetchall()
    if len(candidates) != 1:
        return None
    token = candidates[0][0]
    if token == row["token_id"]:
        return token
    # Provisioned installations can assign different personal identities to the Web and Telegram endpoints.
    for endpoint in db.execute("""SELECT external_subject,endpoint_key,conversation_ref,route_json FROM client_endpoints
            WHERE user_subject=? AND agent_host='nanobot' AND token_id=? AND client_type='telegram' AND account_id=?
            AND state='active'""", (subject, token, bot)):
        sender = endpoint["external_subject"]
        if (isinstance(sender, str) and sender.isdecimal()
                and endpoint["endpoint_key"] == f"telegram:{bot}:{sender}"
                and endpoint["conversation_ref"] == telegram_conversation_ref(instance, subject, row["endpoint_id"], bot, sender)
                and json.loads(endpoint["route_json"]) == {"channel": "telegram", "accountId": bot, "to": sender}):
            return token
    return None


def supports_independent(db, subject, instance, bot, token=None):
    current = deployment_token(db, subject, instance, bot)
    return current is not None and (token is None or token == current)


def inherited_proof(db, subject, instance, bot, sender=None, *, token=None):
    from agentbridge.core.endpoint_instances import endpoint_instances
    from .telegram_pilot_links import mirror_binding, mirror_pair_proof, mirror_sender_ids, _revocation
    if (_revocation(db, subject, instance, bot) or independent(db, subject, instance, bot)
            or not supports_independent(db, subject, instance, bot,token)):
        return None
    binding = mirror_binding(db, subject, instance, endpoint_instances(db, subject))
    peers = mirror_sender_ids(db, subject, binding) if binding and binding["bot"] == bot else []
    if len(peers) != 1 or sender is not None and sender != peers[0]:
        return None
    proof = mirror_pair_proof(db, subject, binding, peers[0])
    identity = deployment_token(db, subject, instance, bot)
    if not proof or not identity:
        return None
    return hashlib.sha256(json.dumps([proof, identity], separators=(",", ":")).encode()).hexdigest()


def permits_caller(db, subject, instance, bot, token, protocol, *, migration_challenge=False):
    """Migrated routes require a current enrolled host and its explicit protocol on each private call."""
    floor = independent(db, subject, instance, bot)
    if not floor and not migration_challenge:
        return True
    return (type(protocol) is int and protocol == 2 and (not floor or floor[0] == protocol)
            and bool(token) and supports_independent(db, subject, instance, bot, token))


def permits_delivery(db, host, subject, instance, key, token, protocol):
    if host != "nanobot" or not key.startswith("telegram:"):
        return True
    parts = key.split(":")
    if len(parts) != 3:
        return False
    _, bot, sender = parts
    if not independent(db, subject, instance, bot):
        return True
    from .telegram_pilot_links import verified_pair
    return (permits_caller(db, subject, instance, bot, token, protocol)
            and verified_pair(db, subject, instance, bot, sender, allow_new_pair=True))


def grant_generation(db, owner, instance, bot, sender):
    row = db.execute("""SELECT r.generation, l.confirmed_at FROM workspace_telegram_pilot_revocations r
        JOIN workspace_telegram_pilot_links l USING(instance,bot,owner)
        JOIN workspace_telegram_independent_pairs p USING(instance,bot,owner)
        WHERE r.owner=? AND r.instance=? AND r.bot=? AND l.sender=?""", (owner, instance, bot, sender)).fetchone()
    return row


def reception_boundary(db, subject, instance, bot, sender, token):
    """Explicit grants need a provably later original platform second."""
    link=db.execute("""SELECT confirmed_at FROM workspace_telegram_pilot_links
        WHERE owner=? AND instance=? AND bot=? AND sender=?""",(subject,instance,bot,sender)).fetchone()
    if not link:
        return {"generationScoped":False,"receptionFloor":None}
    identity=db.execute("SELECT created_at FROM mcp_identity_tokens WHERE token_id=? AND user_subject=?",
                        (token,subject)).fetchone()
    if not identity:
        raise PermissionError("Original personal reception identity unavailable")
    effective=max(float(link[0]),datetime.fromisoformat(identity[0]).timestamp())
    if not math.isfinite(effective) or effective<=0:
        raise PermissionError("Original reception boundary unavailable")
    return {"generationScoped":True,"receptionFloor":math.floor(effective)}
