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


def eligible(service, db, subject, instance, *, new_link=False):
    account = db.execute(
        "SELECT account_id FROM workspace_accounts WHERE state='active' AND user_subject=?",
        (subject,),
    ).fetchone()
    enabled = db.execute(
        "SELECT all_subjects FROM workspace_host_instances WHERE instance_id=? AND host_type='nanobot' AND state='enabled'",
        (instance,),
    ).fetchone()
    if not account or not enabled or new_link and enabled["all_subjects"]:
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


def verified_pair(db, subject, instance, bot, sender, *, allow_new_pair):
    # Conservatively reject two active owners even if one older row is malformed.
    claimed = active_claimants(db, sender)
    if claimed:
        return claimed == {subject} and (subject, sender) in old_peers(
            db, sender=sender
        )
    if not allow_new_pair or old_peers(db, subject=subject):
        return False
    return bool(
        db.execute(
            "SELECT 1 FROM workspace_telegram_pilot_links WHERE instance=? AND bot=? AND owner=? AND sender=?",
            (instance, bot, subject, sender),
        ).fetchone()
    )

def mirror_binding(db, subject, instance, ownership):
    """Return the current deployed Bot and exact private Web binding, failing closed."""
    from agentbridge.workspace.nanobot_gateway import load_nanobot_route_specs

    binding = db.execute("""SELECT b.*, h.route_ref,h.all_subjects,e.token_id
        FROM workspace_host_bindings b JOIN workspace_host_instances h USING(instance_id)
        JOIN workspace_accounts a ON a.account_id=b.account_id JOIN client_endpoints e ON e.endpoint_id=b.endpoint_id
        WHERE a.user_subject=? AND b.instance_id=? AND h.host_type='nanobot' AND h.state='enabled'""",
        (subject, instance)).fetchone()
    if not binding or ownership.get(binding["endpoint_id"]) != ("nanobot", instance):
        return None
    try:
        spec = load_nanobot_route_specs().get(binding["route_ref"], {})
        if (spec.get("instanceId") != instance or not isinstance(spec.get("subjects"), dict)
                or spec["subjects"].get(subject) != binding["credential_ref"]
                or not isinstance(spec.get("telegram"), dict)):
            return None
        bot = numeric_id(spec["telegram"].get("botId"))
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
    if active_claimants(db, sender):
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
    return hashlib.sha256(json.dumps(basis, separators=(",", ":")).encode()).hexdigest()


def _ensure_telegram_endpoint(db, tasks, subject, binding, bot, sender, token_id, *, refresh):
    key = f"telegram:{bot}:{sender}"
    conversation = telegram_conversation_ref(binding["instance_id"], subject, binding["endpoint_id"], bot, sender)
    route = {"channel": "telegram", "to": sender, "accountId": bot}
    old = db.execute("SELECT * FROM client_endpoints WHERE agent_host='nanobot' AND endpoint_key=?", (key,)).fetchone()
    if old and (old["state"] != "active" or old["user_subject"] != subject or old["conversation_ref"] != conversation
                or old["client_type"] != "telegram" or old["external_subject"] != sender or old["account_id"] != bot
                or json.loads(old["route_json"]) != route):
        raise PermissionError("Telegram endpoint ownership changed")
    if old and not refresh and "timeline_message" in json.loads(old["capabilities_json"]):
        return old["endpoint_id"]
    endpoint, _ = tasks.ensure_endpoint(
        user_subject=subject, token_id=old["token_id"] if old and not refresh else token_id,
        agent_host="nanobot", endpoint_key=key, client_type="telegram", external_subject=sender,
        conversation_ref=conversation, account_id=bot, route=route, capabilities=["timeline_message"], connection=db)
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
            """)


class TelegramPilotLinks:
    def __init__(self, service):
        self.service = service
        self.workspace = service.workspace
        self.hosts = self.workspace.hosts

    @staticmethod
    def _hash(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _owner(self, db, account, instance):
        row = db.execute(
            "SELECT user_subject FROM workspace_accounts WHERE account_id=? AND state='active'",
            (account,),
        ).fetchone()
        if not row or not eligible(self.service, db, row["user_subject"], instance):
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
        candidates = old_peers(db, subject=owner)
        for _, sender in candidates:
            if active_claimants(db, sender) == {owner}:
                return {**view, "state": "linked", "source": "existing"}
        row = db.execute(
            "SELECT * FROM workspace_telegram_pilot_links WHERE instance=? AND bot=? AND owner=?",
            (instance, bot, owner),
        ).fetchone()
        if (
            row
            and not candidates
            and not active_claimants(db, row["sender"])
            and eligible(self.service, db, owner, instance, new_link=True)
        ):
            return {**view, "state": "linked", "source": "workspace"}
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
        return view

    def status(self, account_id, instance_id, bot_id):
        with self.workspace._connect() as db:
            db.execute("BEGIN")
            return self._status(
                db,
                self._owner(db, account_id, instance_id),
                instance_id,
                numeric_id(bot_id),
            )

    def begin(self, account_id, instance_id, bot_id):
        bot = numeric_id(bot_id)
        with self.workspace._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._owner(db, account_id, instance_id)
            state = self._status(db, owner, instance_id, bot)
            if state["state"] == "linked":
                return state
            if not eligible(self.service, db, owner, instance_id, new_link=True):
                raise PermissionError("管理员尚未为此账号指名开放试点")
            code = secrets.token_urlsafe(24)
            db.execute(
                "DELETE FROM workspace_telegram_pilot_challenges WHERE (instance=? AND bot=? AND owner=?) OR expires_at<=?",
                (instance_id, bot, owner, time.time()),
            )
            db.execute(
                """INSERT INTO workspace_telegram_pilot_challenges(challenge_id,instance,bot,owner,code_hash,expires_at)
                         VALUES(?,?,?,?,?,?)""",
                (
                    str(uuid4()),
                    instance_id,
                    bot,
                    owner,
                    self._hash(code),
                    time.time() + 300,
                ),
            )
            return {**self._status(db, owner, instance_id, bot), "code": code}

    def present(self, subject, instance_id, bot_id, sender_id, code):
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
            if (
                active_claimants(db, sender)
                or db.execute(
                    "SELECT 1 FROM workspace_telegram_pilot_links WHERE instance=? AND bot=? AND sender=?",
                    (instance_id, bot, sender),
                ).fetchone()
            ):
                return None
            confirmation = f"{secrets.randbelow(10**8):08d}"
            db.execute(
                "UPDATE workspace_telegram_pilot_challenges SET sender=?,confirm_hash=? WHERE challenge_id=?",
                (
                    sender,
                    self._hash(row["challenge_id"] + confirmation),
                    row["challenge_id"],
                ),
            )
            return confirmation

    def confirm(self, account_id, instance_id, bot_id, challenge_id, confirmation_code):
        bot = numeric_id(bot_id)
        failed = False
        with self.workspace._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._owner(db, account_id, instance_id)
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
                if (
                    old_peers(db, subject=owner)
                    or active_claimants(db, row["sender"])
                    or db.execute(
                        "SELECT 1 FROM workspace_telegram_pilot_links WHERE instance=? AND bot=? AND (owner=? OR sender=?)",
                        (instance_id, bot, owner, row["sender"]),
                    ).fetchone()
                ):
                    raise ValueError("绑定已变化，请重新核对")
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
            owner = self._owner(db, account_id, instance_id)
            for table in (
                "workspace_telegram_pilot_links",
                "workspace_telegram_pilot_challenges",
            ):
                db.execute(
                    f"DELETE FROM {table} WHERE instance=? AND bot=? AND owner=?",
                    (instance_id, bot, owner),
                )
            return self._status(db, owner, instance_id, bot)

    def verified(self, subject, instance, bot_id, sender_id):
        sender, bot = numeric_id(sender_id), numeric_id(bot_id)
        with self.workspace._connect() as db:
            db.execute("BEGIN")
            return self._verified(db, subject, instance, bot, sender)

    def _verified(self, db, subject, instance, bot, sender):
        if not eligible(self.service, db, subject, instance):
            return False
        return verified_pair(db, subject, instance, bot, sender,
                             allow_new_pair=eligible(self.service, db, subject, instance, new_link=True))

    def mirror_peers(self, subject, instance, bot_id):
        from agentbridge.core.endpoint_instances import endpoint_instances

        bot = numeric_id(bot_id)
        with self.workspace._connect() as db:
            db.execute("BEGIN")
            binding = mirror_binding(db, subject, instance, endpoint_instances(db, subject))
            senders = mirror_sender_ids(db, subject, binding) if binding and binding["bot"] == bot else []
            return {"senderIds": senders}

    def ensure_endpoint(self, subject, token_id, instance, bot_id, sender_id):
        from agentbridge.workspace.nanobot_gateway import load_nanobot_route_specs

        bot, sender = numeric_id(bot_id), numeric_id(sender_id)
        routes = load_nanobot_route_specs()
        with self.workspace._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._verified(db, subject, instance, bot, sender):
                raise PermissionError("No current Telegram pairing")
            account = db.execute(
                "SELECT account_id FROM workspace_accounts WHERE state='active' AND user_subject=?",
                (subject,),
            ).fetchone()
            binding = self.hosts.resolve(
                db, account["account_id"], instance_id=instance, existing=True
            )
            route = routes.get(binding["route_ref"]) or {}
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
            return {
                "endpointId": endpoint,
                "endpointKey": key,
                "conversationRef": conversation,
                "workspaceEndpointId": binding["endpoint_id"],
                "workspaceEndpointKey": binding["endpoint_key"],
            }
