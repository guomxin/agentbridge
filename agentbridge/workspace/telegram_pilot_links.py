"""Bind a Telegram private sender by proving control of two separately authenticated endpoints."""

import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentbridge.core.actionable_results import _telegram_endpoint
from . import telegram_pair_migration as migration
from .nanobot_gateway import pi_telegram_sender, route_specs_for


def numeric_id(value):
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,15}", text) or int(text) >= 2**53:
        raise ValueError("数字身份无效")
    return text


def telegram_conversation_ref(instance, subject, workspace_endpoint_id, bot, sender):
    basis = json.dumps(
        [instance, subject, workspace_endpoint_id, str(bot), str(sender)],
        separators=(",", ":"),
    )
    return "agentbridge:telegram:" + str(uuid5(NAMESPACE_URL, basis))


def _channel_assignment(db, subject, instance, bot=None):
    """Resolve deployment permission separately from the live private pairing."""
    row = db.execute("""SELECT h.host_type,h.route_ref,h.state,h.all_subjects,b.credential_ref
        FROM workspace_host_instances h
        LEFT JOIN workspace_accounts a ON a.user_subject=? AND a.state='active'
        LEFT JOIN workspace_host_bindings b ON b.instance_id=h.instance_id AND b.account_id=a.account_id
        WHERE h.instance_id=?""", (subject, instance)).fetchone()
    if not row or row["state"] != "enabled":
        return None, None
    if row["host_type"] == "nanobot":
        return "nanobot", None
    if row["host_type"] != "pi" or row["all_subjects"] or not row["credential_ref"]:
        return None, None
    try:
        spec = route_specs_for("pi").get(row["route_ref"]) or {}
        subjects = spec.get("subjects")
        if (spec.get("instanceId") == instance and isinstance(subjects, dict)
                and subjects.get(subject) == row["credential_ref"]):
            sender = pi_telegram_sender(spec, subject, bot)
            if sender:
                return "pi", sender
    except (OSError, ValueError, TypeError):
        pass
    return None, None


def eligible(service, db, subject, instance, *, new_link=False, bot=None):
    account = db.execute(
        "SELECT account_id FROM workspace_accounts WHERE state='active' AND user_subject=?",
        (subject,),
    ).fetchone()
    enabled = db.execute(
        "SELECT all_subjects FROM workspace_host_instances WHERE instance_id=? AND state='enabled'",
        (instance,),
    ).fetchone()
    if not account or not enabled or new_link and enabled["all_subjects"] or not _channel_assignment(db, subject, instance, bot)[0]:
        return False
    try:
        service.workspace.hosts.resolve(
            db, account["account_id"], instance_id=instance, existing=True
        )
    except (PermissionError, ValueError):
        return False
    return True


def old_peers(db, *, subject=None, sender=None):
    field, value = (
        ("user_subject", subject)
        if subject is not None
        else ("external_subject", sender)
    )
    rows = db.execute(
        f"""SELECT user_subject,external_subject,endpoint_id FROM client_endpoints
        WHERE agent_host='openclaw' AND lower(client_type)='telegram' AND state='active' AND {field}=?""",
        (value,),
    ).fetchall()
    valid = set()
    for row in rows:
        try:
            numeric_id(row["external_subject"])
            _telegram_endpoint(db, row["user_subject"], row["endpoint_id"])
        except (TypeError, ValueError):
            continue
        valid.add((row["user_subject"], row["external_subject"]))
    return valid


def active_claimants(db, sender):
    rows = db.execute(
        """SELECT DISTINCT user_subject FROM client_endpoints
        WHERE agent_host='openclaw' AND lower(client_type)='telegram' AND state='active'
        AND external_subject=?""",
        (sender,),
    )
    return {row["user_subject"] for row in rows}


def _revocation(db, subject, instance, bot):
    return db.execute("""SELECT generation FROM workspace_telegram_pilot_revocations
        WHERE instance=? AND bot=? AND owner=?""", (instance, bot, subject)).fetchone()


def verified_pair(db, subject, instance, bot, sender, *, allow_new_pair):
    # Conservatively reject two active owners even if one older row is malformed.
    claimed = active_claimants(db, sender)
    host_type, allowed_sender = _channel_assignment(db, subject, instance, bot)
    if not host_type:
        return False
    if host_type == "pi":
        if sender != allowed_sender or claimed - {subject}:
            return False
    elif migration.independent(db, subject, instance, bot):
        return bool(allow_new_pair and not claimed - {subject}
                    and migration.grant_generation(db, subject, instance, bot, sender)
                    and migration.supports_independent(db, subject, instance, bot))
    elif _revocation(db, subject, instance, bot):
        # Revocation permanently disables implicit inheritance for this host/Bot.
        # Only a fresh two-endpoint proof may restore an explicit grant.
        return bool(allow_new_pair and not claimed - {subject} and db.execute(
            "SELECT 1 FROM workspace_telegram_pilot_links WHERE instance=? AND bot=? AND owner=? AND sender=?",
            (instance, bot, subject, sender)).fetchone())
    elif claimed:
        return claimed == {subject} and (subject, sender) in old_peers(
            db, sender=sender
        )
    if not allow_new_pair or host_type == "nanobot" and old_peers(db, subject=subject):
        return False
    return bool(
        db.execute(
            "SELECT 1 FROM workspace_telegram_pilot_links WHERE instance=? AND bot=? AND owner=? AND sender=?",
            (instance, bot, subject, sender),
        ).fetchone()
    )

def mirror_binding(db, subject, instance, ownership):
    """Return the current deployed Bot and exact private Web binding, failing closed."""
    binding = db.execute("""SELECT b.*, h.host_type,h.route_ref,h.all_subjects,e.token_id
        FROM workspace_host_bindings b JOIN workspace_host_instances h USING(instance_id)
        JOIN workspace_accounts a ON a.account_id=b.account_id JOIN client_endpoints e ON e.endpoint_id=b.endpoint_id
        WHERE a.user_subject=? AND b.instance_id=? AND h.host_type IN ('nanobot','pi') AND h.state='enabled'""",
        (subject, instance)).fetchone()
    if not binding or ownership.get(binding["endpoint_id"]) != (binding["host_type"], instance):
        return None
    try:
        spec = route_specs_for(binding["host_type"]).get(binding["route_ref"], {})
        if (spec.get("instanceId") != instance or not isinstance(spec.get("subjects"), dict)
                or spec["subjects"].get(subject) != binding["credential_ref"]
                or not isinstance(spec.get("telegram"), dict)):
            return None
        bot = numeric_id(spec["telegram"].get("botId"))
        if binding["host_type"] == "pi" and not pi_telegram_sender(spec, subject, bot):
            return None
    except (OSError, ValueError):
        return None
    return {**dict(binding), "bot": bot}


def mirror_sender_ids(db, subject, binding):
    instance, bot = binding["instance_id"], binding["bot"]
    candidates = {sender for _, sender in old_peers(db, subject=subject)}
    candidates.update(row[0] for row in db.execute(
        "SELECT sender FROM workspace_telegram_pilot_links WHERE instance=? AND bot=? AND owner=?",
        (instance, bot, subject)))
    verified = [sender for sender in candidates if verified_pair(
        db, subject, instance, bot, sender, allow_new_pair=not binding["all_subjects"])]
    # Inbound authentication can prove a specific sender. An automatic mirror
    # needs a single destination when older installations hold multiple IDs.
    return verified if len(verified) == 1 else []


def mirror_pair_proof(db, subject, binding, sender):
    """Pin a delivery to the same grant that authorized its creation."""
    if not verified_pair(db, subject, binding["instance_id"], binding["bot"], sender,
                         allow_new_pair=not binding.get("all_subjects", False)):
        return None
    revoked = _revocation(db, subject, binding["instance_id"], binding["bot"])
    if binding["host_type"] == "nanobot" and active_claimants(db, sender) and not revoked:
        grants = []
        for row in db.execute("""SELECT endpoint_id FROM client_endpoints WHERE user_subject=? AND agent_host='openclaw'
                AND lower(client_type)='telegram' AND state='active' AND external_subject=? ORDER BY endpoint_id""",
                (subject, sender)):
            try:
                grants.append(_telegram_endpoint(db, subject, row[0])["_binding_hash"])
            except (TypeError, ValueError):
                continue
        if not grants:
            return None
    else:
        row = db.execute("""SELECT confirmed_at FROM workspace_telegram_pilot_links
            WHERE instance=? AND bot=? AND owner=? AND sender=?""",
            (binding["instance_id"], binding["bot"], subject, sender)).fetchone()
        if not row:
            return None
        grants = [row[0]]
    basis = [binding["instance_id"], binding["endpoint_id"], binding["credential_ref"], binding["bot"], subject, sender, grants]
    if revoked:
        basis.append(revoked[0])
        if migration.independent(db, subject, binding["instance_id"], binding["bot"]):
            basis.append(migration.deployment_token(db, subject, binding["instance_id"], binding["bot"]))
    return hashlib.sha256(json.dumps(basis, separators=(",", ":")).encode()).hexdigest()


NANOBOT_TELEGRAM_CAPABILITIES = [
    "timeline_message",
    "trusted_interaction",
    "direct_status",
    "continuous_flow_v1",
]


def _ensure_telegram_endpoint(db, tasks, subject, binding, bot, sender, token_id, *, refresh):
    key = f"telegram:{bot}:{sender}"
    conversation = telegram_conversation_ref(binding["instance_id"], subject, binding["endpoint_id"], bot, sender)
    route = {"channel": "telegram", "to": sender, "accountId": bot}
    old = db.execute("SELECT * FROM client_endpoints WHERE agent_host=? AND endpoint_key=?", (binding["host_type"], key)).fetchone()
    if old and (old["state"] != "active" or old["user_subject"] != subject or old["conversation_ref"] != conversation
                or old["client_type"] != "telegram" or old["external_subject"] != sender or old["account_id"] != bot
                or json.loads(old["route_json"]) != route):
        raise PermissionError("Telegram endpoint ownership changed")
    if old and not refresh and set(NANOBOT_TELEGRAM_CAPABILITIES).issubset(set(json.loads(old["capabilities_json"]))):
        return old["endpoint_id"]
    endpoint, _ = tasks.ensure_endpoint(
        user_subject=subject, token_id=old["token_id"] if old and not refresh else token_id,
        agent_host=binding["host_type"], endpoint_key=key, client_type="telegram", external_subject=sender,
        conversation_ref=conversation, account_id=bot, route=route,
        capabilities=NANOBOT_TELEGRAM_CAPABILITIES, connection=db)
    return endpoint["endpoint_id"]


def mirror_destinations(db, tasks, subject, binding):
    # Materialize on the first new message even when this Bot has no local model-run history.
    endpoints = []
    for sender in mirror_sender_ids(db, subject, binding):
        try:
            proof = mirror_pair_proof(db, subject, binding, sender)
            if proof:
                endpoint_id = _ensure_telegram_endpoint(db, tasks, subject, binding, binding["bot"], sender,
                                                        binding["token_id"], refresh=False)
                endpoints.append((endpoint_id, proof))
        except PermissionError:
            continue
    return endpoints


def initialize_telegram_pilot_links(connection):
    connection.executescript("""
                CREATE TABLE IF NOT EXISTS workspace_telegram_pilot_links(
                    instance TEXT NOT NULL, bot TEXT NOT NULL, owner TEXT NOT NULL, sender TEXT NOT NULL,
                    confirmed_at REAL NOT NULL, PRIMARY KEY(instance,bot,owner),UNIQUE(instance,bot,sender));
                CREATE TABLE IF NOT EXISTS workspace_telegram_pilot_challenges(
                    challenge_id TEXT PRIMARY KEY, instance TEXT NOT NULL, bot TEXT NOT NULL, owner TEXT NOT NULL,
                    code_hash TEXT NOT NULL UNIQUE,expires_at REAL NOT NULL,sender TEXT,confirm_hash TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,UNIQUE(instance,bot,owner));
                CREATE TABLE IF NOT EXISTS workspace_telegram_pilot_revocations(
                    instance TEXT NOT NULL, bot TEXT NOT NULL, owner TEXT NOT NULL,
                    generation TEXT NOT NULL, PRIMARY KEY(instance,bot,owner));
            """)
    migration.initialize(connection)


class TelegramPilotLinks:
    def __init__(self, service):
        self.service = service
        self.workspace = service.workspace
        self.hosts = self.workspace.hosts

    @staticmethod
    def _hash(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _owner(self, db, account, instance, bot):
        row = db.execute(
            "SELECT user_subject FROM workspace_accounts WHERE account_id=? AND state='active'",
            (account,),
        ).fetchone()
        if not row or not eligible(self.service, db, row["user_subject"], instance, bot=bot):
            raise PermissionError("当前账号无法绑定此试点实例")
        return row["user_subject"]

    def _status(self, db, owner, instance, bot):
        view = {
            "state": "unlinked",
            "source": None,
            "challenge_id": None,
            "expires_at": None,
            "sender_hint": None,
        }
        host_type, _ = _channel_assignment(db, owner, instance, bot)
        candidates = old_peers(db, subject=owner) if (
            host_type == "nanobot" and not migration.independent(db, owner, instance, bot)
            and not _revocation(db, owner, instance, bot)) else set()
        pending = db.execute(
            "SELECT * FROM workspace_telegram_pilot_challenges WHERE instance=? AND bot=? AND owner=? AND expires_at>? AND attempts<5",
            (instance, bot, owner, time.time()),
        ).fetchone()
        if pending:
            return {
                **view,
                "state": "waiting_confirmation"
                if pending["sender"]
                else "waiting_telegram",
                "challenge_id": pending["challenge_id"],
                "expires_at": datetime.fromtimestamp(
                    pending["expires_at"], UTC
                ).isoformat(),
                "sender_hint": "••" + pending["sender"][-2:]
                if pending["sender"]
                else None,
            }
        for _, sender in candidates:
            if active_claimants(db, sender) == {owner}:
                return {**view, "state": "linked", "source": "existing",
                        "can_migrate": bool(migration.inherited_proof(db, owner, instance, bot))}
        row = db.execute(
            "SELECT * FROM workspace_telegram_pilot_links WHERE instance=? AND bot=? AND owner=?",
            (instance, bot, owner),
        ).fetchone()
        if (
            row
            and not candidates
            and verified_pair(db, owner, instance, bot, row["sender"], allow_new_pair=True)
            and eligible(self.service, db, owner, instance, new_link=True)
        ):
            return {**view, "state": "linked", "source": "workspace"}
        return view

    def status(self, account_id, instance_id, bot_id):
        with self.workspace._connect() as db:
            db.execute("BEGIN")
            return self._status(
                db,
                self._owner(db, account_id, instance_id, bot_id),
                instance_id,
                numeric_id(bot_id),
            )

    def begin(self, account_id, instance_id, bot_id, *, migrate=False):
        bot = numeric_id(bot_id)
        with self.workspace._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._owner(db, account_id, instance_id, bot)
            state = self._status(db, owner, instance_id, bot)
            previous = db.execute("""SELECT legacy_proof FROM workspace_telegram_pilot_challenges
                WHERE instance=? AND bot=? AND owner=? AND expires_at>?""", (instance_id,bot,owner,time.time())).fetchone()
            migrate = migrate or bool(previous and previous["legacy_proof"])
            if state["state"] == "linked" and (not migrate or state["source"] != "existing"):
                return state
            proof = migration.inherited_proof(db, owner, instance_id, bot) if migrate else None
            if migrate and not proof:
                raise PermissionError("当前私聊或宿主版本不满足独立配对条件。")
            if not eligible(self.service, db, owner, instance_id, new_link=True):
                raise PermissionError("管理员尚未为此账号指名开放试点")
            code = secrets.token_urlsafe(24)
            db.execute(
                "DELETE FROM workspace_telegram_pilot_challenges WHERE (instance=? AND bot=? AND owner=?) OR expires_at<=?",
                (instance_id, bot, owner, time.time()),
            )
            db.execute(
                """INSERT INTO workspace_telegram_pilot_challenges(challenge_id,instance,bot,owner,code_hash,expires_at,legacy_proof)
                         VALUES(?,?,?,?,?,?,?)""",
                (
                    str(uuid4()),
                    instance_id,
                    bot,
                    owner,
                    self._hash(code),
                    time.time() + 300,
                    proof,
                ),
            )
            return {**self._status(db, owner, instance_id, bot), "code": code}

    def present(self, subject, instance_id, bot_id, sender_id, code, *, token_id=None, protocol=None):
        bot, sender = numeric_id(bot_id), numeric_id(sender_id)
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", code):
            return None
        with self.workspace._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM workspace_telegram_pilot_challenges WHERE instance=? AND bot=? AND owner=?
                AND code_hash=? AND sender IS NULL AND expires_at>?""",
                (instance_id, bot, subject, self._hash(code), time.time()),
            ).fetchone()
            if not row or not eligible(
                self.service, db, subject, instance_id, new_link=True
            ):
                return None
            host_type, allowed_sender = _channel_assignment(db, subject, instance_id, bot)
            claimed = active_claimants(db, sender)
            revoked = _revocation(db, subject, instance_id, bot)
            legacy = row["legacy_proof"]
            if legacy and migration.inherited_proof(db, subject, instance_id, bot, sender, token=token_id) != legacy:
                return None
            if not migration.permits_caller(db, subject, instance_id, bot, token_id, protocol, migration_challenge=bool(legacy)):
                return None
            if (
                not host_type or (host_type == "pi" and (sender != allowed_sender or claimed - {subject}))
                or (host_type == "nanobot" and (claimed - {subject} if revoked or legacy else claimed))
                or db.execute(
                    "SELECT 1 FROM workspace_telegram_pilot_links WHERE instance=? AND bot=? AND sender=?",
                    (instance_id, bot, sender),
                ).fetchone()
            ):
                return None
            confirmation = f"{secrets.randbelow(10**8):08d}"
            db.execute(
                "UPDATE workspace_telegram_pilot_challenges SET sender=?,confirm_hash=?,present_token_id=? WHERE challenge_id=?",
                (
                    sender,
                    self._hash(row["challenge_id"] + confirmation),
                    token_id,
                    row["challenge_id"],
                ),
            )
            return confirmation

    def confirm(self, account_id, instance_id, bot_id, challenge_id, confirmation_code):
        bot = numeric_id(bot_id)
        failed = False
        with self.workspace._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._owner(db, account_id, instance_id, bot)
            row = db.execute(
                """SELECT * FROM workspace_telegram_pilot_challenges WHERE instance=? AND bot=? AND owner=?
                AND challenge_id=? AND expires_at>? AND sender IS NOT NULL AND attempts<5""",
                (instance_id, bot, owner, challenge_id, time.time()),
            ).fetchone()
            if not row or not eligible(
                self.service, db, owner, instance_id, new_link=True
            ):
                raise ValueError("配对已失效，请重新发起")
            if (
                not isinstance(confirmation_code, str)
                or not re.fullmatch(r"[0-9]{8}", confirmation_code)
                or not hmac.compare_digest(
                    row["confirm_hash"], self._hash(challenge_id + confirmation_code)
                )
            ):
                db.execute(
                    "UPDATE workspace_telegram_pilot_challenges SET attempts=attempts+1 WHERE challenge_id=?",
                    (challenge_id,),
                )
                failed = True
            else:
                host_type, allowed_sender = _channel_assignment(db, owner, instance_id, bot)
                claimed = active_claimants(db, row["sender"])
                revoked = _revocation(db, owner, instance_id, bot)
                legacy = row["legacy_proof"]
                if legacy and migration.inherited_proof(db, owner, instance_id, bot, row["sender"]) != legacy:
                    raise ValueError("原配对或授权版本已变化，请重新核对。")
                if migration.independent(db, owner, instance_id, bot) and not (row["present_token_id"] and
                        migration.supports_independent(db, owner, instance_id, bot, row["present_token_id"])):
                    raise ValueError("当前宿主不能接收独立配对。")
                if (
                    not host_type or (host_type == "pi" and (row["sender"] != allowed_sender or claimed - {owner}))
                    or (host_type == "nanobot" and (
                        claimed - {owner} if revoked or legacy else old_peers(db, subject=owner) or claimed))
                    or db.execute(
                        "SELECT 1 FROM workspace_telegram_pilot_links WHERE instance=? AND bot=? AND (owner=? OR sender=?)",
                        (instance_id, bot, owner, row["sender"]),
                    ).fetchone()
                ):
                    raise ValueError("绑定已变化，请重新核对")
                if legacy:
                    db.execute("INSERT INTO workspace_telegram_independent_pairs VALUES(?,?,?,2)",
                               (instance_id,bot,owner))
                    db.execute("INSERT INTO workspace_telegram_pilot_revocations VALUES(?,?,?,?)",
                               (instance_id,bot,owner,secrets.token_hex(32)))
                db.execute(
                    "INSERT INTO workspace_telegram_pilot_links VALUES(?,?,?,?,?)",
                    (instance_id, bot, owner, row["sender"], time.time()),
                )
                db.execute(
                    "DELETE FROM workspace_telegram_pilot_challenges WHERE challenge_id=?",
                    (challenge_id,),
                )
                return self._status(db, owner, instance_id, bot)
        if failed:
            raise ValueError("确认码无效或已过期")

    def revoke(self, account_id, instance_id, bot_id):
        bot = numeric_id(bot_id)
        with self.workspace._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._owner(db, account_id, instance_id, bot)
            db.execute("""INSERT INTO workspace_telegram_pilot_revocations VALUES(?,?,?,?)
                ON CONFLICT(instance,bot,owner) DO UPDATE SET generation=excluded.generation""",
                (instance_id, bot, owner, secrets.token_hex(32)))
            for table in (
                "workspace_telegram_pilot_links",
                "workspace_telegram_pilot_challenges",
            ):
                db.execute(
                    f"DELETE FROM {table} WHERE instance=? AND bot=? AND owner=?",
                    (instance_id, bot, owner),
                )
            return self._status(db, owner, instance_id, bot)

    def verified(self, subject, instance, bot_id, sender_id, *, token_id=None, protocol=None):
        sender, bot = numeric_id(sender_id), numeric_id(bot_id)
        with self.workspace._connect() as db:
            db.execute("BEGIN")
            return (migration.permits_caller(db, subject, instance, bot, token_id, protocol)
                    and self._verified(db, subject, instance, bot, sender))

    def _verified(self, db, subject, instance, bot, sender):
        if not eligible(self.service, db, subject, instance):
            return False
        return verified_pair(db, subject, instance, bot, sender,
                             allow_new_pair=eligible(self.service, db, subject, instance, new_link=True))

    def mirror_peers(self, subject, instance, bot_id, *, token_id=None, protocol=None):
        from agentbridge.core.endpoint_instances import endpoint_instances

        bot = numeric_id(bot_id)
        with self.workspace._connect() as db:
            db.execute("BEGIN")
            if not migration.permits_caller(db, subject, instance, bot, token_id, protocol):
                return {"senderIds": []}
            binding = mirror_binding(db, subject, instance, endpoint_instances(db, subject))
            senders = mirror_sender_ids(db, subject, binding) if binding and binding["bot"] == bot else []
            return {"senderIds": senders}

    def ensure_endpoint(self, subject, token_id, instance, bot_id, sender_id, *, protocol=None):
        bot, sender = numeric_id(bot_id), numeric_id(sender_id)
        with self.workspace._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not migration.permits_caller(db, subject, instance, bot, token_id, protocol):
                raise PermissionError("当前宿主身份不能接收独立配对。")
            if not self._verified(db, subject, instance, bot, sender):
                raise PermissionError("No current Telegram pairing")
            account = db.execute(
                "SELECT account_id FROM workspace_accounts WHERE state='active' AND user_subject=?",
                (subject,),
            ).fetchone()
            binding = self.hosts.resolve(
                db, account["account_id"], instance_id=instance, existing=True
            )
            route = route_specs_for(binding["host_type"]).get(binding["route_ref"]) or {}
            if (
                route.get("instanceId") != instance
                or str((route.get("telegram") or {}).get("botId")) != bot
                or (route.get("subjects") or {}).get(subject)
                != binding["credential_ref"]
            ):
                raise PermissionError("No authorized deployment route")
            key = f"telegram:{bot}:{sender}"
            conversation = telegram_conversation_ref(
                instance, subject, binding["endpoint_id"], bot, sender
            )
            endpoint = _ensure_telegram_endpoint(db, self.service.tasks, subject, binding, bot, sender,
                                                 token_id, refresh=True)
            proof = mirror_pair_proof(db, subject, {**binding, "bot": bot}, sender)
            if not proof:
                raise PermissionError("未取得当前私聊配对证明。")
            return {
                "endpointId": endpoint,
                "endpointKey": key,
                "conversationRef": conversation,
                "workspaceEndpointId": binding["endpoint_id"],
                "workspaceEndpointKey": binding["endpoint_key"],
                "pairingRef": proof,
                **migration.reception_boundary(db,subject,instance,bot,sender,token_id),
            }
