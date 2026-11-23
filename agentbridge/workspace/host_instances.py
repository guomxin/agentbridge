"""Administrator-owned Workspace host directory and per-account routing."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager, nullcontext
from pathlib import Path

LEGACY_INSTANCE_ID = "openclaw-default"
_ALIAS = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")
_STATES = {"enabled", "draining", "revoked"}


class HostInstanceConflict(ValueError):
    """The caller's copy of a directory or preference revision is stale."""


def _alias(value, field):
    if not isinstance(value, str) or not _ALIAS.fullmatch(value):
        raise ValueError(f"invalid {field} alias")
    return value


def _label(value, field, maximum):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(not char.isprintable() for char in value)
    ):
        raise ValueError(f"invalid {field}")
    return value.strip()


def _check_revision(expected, actual):
    if type(expected) is not int or expected != actual:
        raise HostInstanceConflict("设置已更新，请刷新后确认。")


class WorkspaceHostInstances:
    """Uses the Workspace and TaskHub database. Construct after WorkspaceStore.

    Public methods take an authenticated account ID. The admin and bind methods
    are for trusted provisioning code. Route and personal credential references
    name entries in deployment-owned configuration; the database holds no keys.
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        with self._transaction() as db:
            for statement in (
                """CREATE TABLE IF NOT EXISTS workspace_host_instances (
                    instance_id TEXT PRIMARY KEY, host_type TEXT NOT NULL, display_name TEXT NOT NULL,
                    route_ref TEXT NOT NULL UNIQUE, adapter_version TEXT NOT NULL, state TEXT NOT NULL,
                    all_subjects INTEGER NOT NULL, revision INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS workspace_host_visibility (
                    instance_id TEXT NOT NULL, user_subject TEXT NOT NULL,
                    PRIMARY KEY (instance_id, user_subject))""",
                """CREATE TABLE IF NOT EXISTS workspace_host_preferences (
                    account_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, revision INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS workspace_host_bindings (
                    account_id TEXT NOT NULL, instance_id TEXT NOT NULL, endpoint_id TEXT NOT NULL UNIQUE,
                    endpoint_key TEXT NOT NULL, session_key TEXT NOT NULL, credential_ref TEXT NOT NULL,
                    PRIMARY KEY (account_id, instance_id), UNIQUE(instance_id, session_key))""",
                """CREATE TABLE IF NOT EXISTS workspace_host_directory_audit (
                    instance_id TEXT NOT NULL, revision INTEGER NOT NULL, actor TEXT NOT NULL,
                    reason TEXT NOT NULL, changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (instance_id, revision))""",
            ):
                db.execute(statement)
            created = db.execute(
                "INSERT OR IGNORE INTO workspace_host_instances VALUES (?, 'openclaw', 'OpenClaw', "
                "?, 'legacy', 'enabled', 1, 1)",
                (LEGACY_INSTANCE_ID, LEGACY_INSTANCE_ID),
            ).rowcount
            if created:
                db.execute(
                    "INSERT INTO workspace_host_preferences SELECT account_id, ?, 1 FROM workspace_accounts",
                    (LEGACY_INSTANCE_ID,),
                )

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def save_instance(
        self,
        instance_id,
        *,
        host_type,
        display_name,
        route_ref,
        adapter_version,
        state,
        subjects,
        expected_revision,
        actor,
        reason,
        connection=None,
    ):
        """Create with revision 0; edits compare revisions. None grants all subjects."""
        _alias(instance_id, "instance")
        _alias(host_type, "host type")
        _alias(route_ref, "route")
        display_name = _label(display_name, "display name", 100)
        adapter_version = _label(adapter_version, "adapter version", 80)
        actor = _label(actor, "actor", 160)
        reason = _label(reason, "reason", 300)
        if state not in _STATES:
            raise ValueError("invalid instance state")
        if subjects is not None:
            if not isinstance(subjects, (list, tuple, set, frozenset)):
                raise ValueError("expected a collection of subjects")
            subjects = sorted(
                {_label(subject, "user subject", 256) for subject in subjects}
            )
        with self._writer(connection) as db:
            previous = db.execute(
                "SELECT * FROM workspace_host_instances WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            revision = previous["revision"] if previous else 0
            _check_revision(expected_revision, revision)
            if previous and (
                previous["host_type"] != host_type or previous["route_ref"] != route_ref
            ):
                raise ValueError("实例类型和连接引用已固定，请为新的路由登记独立实例。")
            owner = db.execute(
                "SELECT instance_id FROM workspace_host_instances WHERE route_ref=?",
                (route_ref,),
            ).fetchone()
            if owner and owner["instance_id"] != instance_id:
                raise ValueError("连接引用已归属其他实例。")
            db.execute(
                """INSERT INTO workspace_host_instances VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET display_name=excluded.display_name,
                adapter_version=excluded.adapter_version, state=excluded.state,
                all_subjects=excluded.all_subjects, revision=excluded.revision""",
                (
                    instance_id,
                    host_type,
                    display_name,
                    route_ref,
                    adapter_version,
                    state,
                    subjects is None,
                    revision + 1,
                ),
            )
            db.execute(
                "DELETE FROM workspace_host_visibility WHERE instance_id=?",
                (instance_id,),
            )
            db.executemany(
                "INSERT INTO workspace_host_visibility VALUES (?, ?)",
                ((instance_id, subject) for subject in subjects or ()),
            )
            db.execute(
                "INSERT INTO workspace_host_directory_audit (instance_id, revision, actor, reason) "
                "VALUES (?, ?, ?, ?)",
                (instance_id, revision + 1, actor, reason),
            )
            return self._admin_row(db, instance_id)

    def _writer(self, connection):
        if connection is None:
            return self._transaction()
        if not connection.in_transaction:
            raise ValueError(
                "an explicit directory connection requires an active transaction"
            )
        return nullcontext(connection)

    @staticmethod
    def _admin_row(db, instance_id):
        row = dict(
            db.execute(
                "SELECT * FROM workspace_host_instances WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
        )
        visible_to_all = row.pop("all_subjects")
        row["subjects"] = (
            None
            if visible_to_all
            else [
                entry[0]
                for entry in db.execute(
                    "SELECT user_subject FROM workspace_host_visibility WHERE instance_id=? ORDER BY user_subject",
                    (instance_id,),
                )
            ]
        )
        return row

    def admin_list(self):
        with self._transaction() as db:
            return [
                self._admin_row(db, row[0])
                for row in db.execute(
                    "SELECT instance_id FROM workspace_host_instances ORDER BY instance_id"
                )
            ]

    @staticmethod
    def _account(db, account_id):
        account = db.execute(
            "SELECT * FROM workspace_accounts WHERE account_id=? AND state='active'",
            (account_id,),
        ).fetchone()
        if account is None:
            raise PermissionError("工作台账号不可用。")
        return account

    @staticmethod
    def _visible(db, account):
        return db.execute(
            """SELECT i.* FROM workspace_host_instances i WHERE i.all_subjects=1 OR EXISTS (
                SELECT 1 FROM workspace_host_visibility v WHERE v.instance_id=i.instance_id
                AND v.user_subject=?) ORDER BY i.instance_id""",
            (account["user_subject"],),
        ).fetchall()

    @staticmethod
    def _endpoint(db, account, instance, endpoint_id):
        if not endpoint_id:
            return None
        return db.execute(
            """SELECT endpoint_id, endpoint_key, conversation_ref FROM client_endpoints
               WHERE endpoint_id=? AND user_subject=? AND agent_host=? AND account_id=?
               AND external_subject=? AND state='active' AND client_type IN ('web','webchat')
               AND endpoint_key<>'' AND conversation_ref<>''""",
            (
                endpoint_id,
                account["user_subject"],
                instance["host_type"],
                account["account_id"],
                account["account_id"],
            ),
        ).fetchone()

    def _binding(self, db, account, instance):
        legacy = instance["instance_id"] == LEGACY_INSTANCE_ID
        if legacy:
            endpoint_id = account["endpoint_id"]
            endpoint_key = account["endpoint_key"]
            session_key = account["openclaw_session_key"]
            credential_ref = None
        else:
            binding = db.execute(
                "SELECT * FROM workspace_host_bindings WHERE account_id=? AND instance_id=?",
                (account["account_id"], instance["instance_id"]),
            ).fetchone()
            if binding is None:
                return None
            endpoint_id = binding["endpoint_id"]
            endpoint_key = binding["endpoint_key"]
            session_key = binding["session_key"]
            credential_ref = binding["credential_ref"]
        endpoint = self._endpoint(db, account, instance, endpoint_id)
        if (
            endpoint is None
            or endpoint["endpoint_key"] != endpoint_key
            or endpoint["conversation_ref"] != session_key
        ):
            return None
        return {
            "instance_id": instance["instance_id"],
            "host_type": instance["host_type"],
            "route_ref": instance["route_ref"],
            "endpoint_id": endpoint_id,
            "endpoint_key": endpoint_key,
            "session_key": session_key,
            "credential_ref": credential_ref,
            "user_subject": account["user_subject"],
        }

    def bind(
        self, account_id, instance_id, *, endpoint_id, credential_ref, connection=None
    ):
        """Trusted provisioning only. Verify endpoint ownership and pin the session."""
        _alias(credential_ref, "credential")
        with self._writer(connection) as db:
            account = self._account(db, account_id)
            instance = next(
                (
                    r
                    for r in self._visible(db, account)
                    if r["instance_id"] == instance_id and r["state"] == "enabled"
                ),
                None,
            )
            if instance is None:
                raise PermissionError("实例未对该主体开放。")
            endpoint = self._endpoint(db, account, instance, endpoint_id)
            if endpoint is None:
                raise PermissionError("未验证该账号的工作台端点。")
            if instance_id == LEGACY_INSTANCE_ID or (
                instance["host_type"] == "openclaw"
                and endpoint_id == account["endpoint_id"]
                and endpoint["endpoint_key"] == account["endpoint_key"]
                and endpoint["conversation_ref"] == account["openclaw_session_key"]
            ):
                raise ValueError("旧版账号的 OpenClaw 端点由原注册流程维护。")
            existing = db.execute(
                "SELECT * FROM workspace_host_bindings WHERE account_id=? AND instance_id=?",
                (account_id, instance_id),
            ).fetchone()
            if existing and (
                existing["endpoint_id"],
                existing["endpoint_key"],
                existing["session_key"],
            ) != (endpoint_id, endpoint["endpoint_key"], endpoint["conversation_ref"]):
                raise ValueError("原绑定已固定；请核验原端点与会话。")
            used_by_other = db.execute(
                "SELECT 1 FROM workspace_host_bindings WHERE credential_ref=? AND account_id<>?",
                (credential_ref, account_id),
            ).fetchone()
            if used_by_other:
                raise ValueError("个人凭据引用已归属其他账号。")
            try:
                db.execute(
                    """INSERT INTO workspace_host_bindings VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, instance_id) DO UPDATE SET credential_ref=excluded.credential_ref""",
                    (
                        account_id,
                        instance_id,
                        endpoint_id,
                        endpoint["endpoint_key"],
                        endpoint["conversation_ref"],
                        credential_ref,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("该端点或会话已有绑定。") from error

    def _choices(self, db, account):
        items = []
        visible_current = set()
        for instance in self._visible(db, account):
            if instance["state"] == "revoked":
                continue
            visible_current.add(instance["instance_id"])
            item = {
                key: instance[key]
                for key in (
                    "instance_id",
                    "host_type",
                    "display_name",
                    "adapter_version",
                    "state",
                )
            }
            item["bound"] = self._binding(db, account, instance) is not None
            item["selectable"] = item["bound"] and item["state"] == "enabled"
            items.append(item)
        account_id = account["account_id"]
        preference = db.execute(
            "SELECT instance_id, revision FROM workspace_host_preferences WHERE account_id=?",
            (account_id,),
        ).fetchone()
        eligible = [item["instance_id"] for item in items if item["selectable"]]
        if preference is None and len(eligible) == 1:
            db.execute(
                "INSERT INTO workspace_host_preferences VALUES (?, ?, 1)",
                (account_id, eligible[0]),
            )
            current, revision = eligible[0], 1
        elif preference is None:
            current, revision = None, 0
        else:
            current, revision = preference["instance_id"], preference["revision"]
        return {
            "default_instance_id": current if current in visible_current else None,
            "revision": revision,
            "default_unavailable": bool(current and current not in eligible),
            "items": items,
        }

    def choices(self, account_id):
        """Public directory projection and optimistic preference version for this account."""
        with self._transaction() as db:
            return self._choices(db, self._account(db, account_id))

    def set_default(self, account_id, instance_id, *, expected_revision):
        with self._transaction() as db:
            account = self._account(db, account_id)
            choices = self._choices(db, account)
            _check_revision(expected_revision, choices["revision"])
            if not any(
                item["instance_id"] == instance_id and item["selectable"]
                for item in choices["items"]
            ):
                raise PermissionError("目标实例当前不可选。")
            if choices["default_instance_id"] != instance_id:
                db.execute(
                    """INSERT INTO workspace_host_preferences VALUES (?, ?, ?)
                              ON CONFLICT(account_id) DO UPDATE SET instance_id=excluded.instance_id,
                              revision=excluded.revision""",
                    (account_id, instance_id, expected_revision + 1),
                )
            return self._choices(db, account)

    def resolve(
        self,
        connection,
        account_id,
        *,
        expected_revision=None,
        instance_id=None,
        existing=False,
    ):
        """Resolve inside the caller's open write transaction, without committing.

        An existing request or original task supplies its pinned instance ID.
        A new request can supply its displayed default for an additional check.
        """
        if not connection.in_transaction:
            raise RuntimeError(
                "Workspace host resolution requires the caller's open transaction"
            )
        account = self._account(connection, account_id)
        if existing:
            if not instance_id:
                raise ValueError("续办须提供原实例。")
        else:
            choices = self._choices(connection, account)
            if expected_revision is not None:
                _check_revision(expected_revision, choices["revision"])
            if (
                instance_id is not None
                and instance_id != choices["default_instance_id"]
            ):
                raise HostInstanceConflict("默认实例已更新，请刷新后确认。")
            instance_id = choices["default_instance_id"]
        instance = next(
            (
                r
                for r in self._visible(connection, account)
                if r["instance_id"] == instance_id
            ),
            None,
        )
        if (
            instance is None
            or instance["state"] == "revoked"
            or (not existing and instance["state"] != "enabled")
        ):
            raise PermissionError("目标实例当前不可用。")
        binding = self._binding(connection, account, instance)
        if binding is None:
            raise PermissionError("工作台账号尚未绑定可用端点。")
        return binding

def context_endpoint_ids(service, subject, host_type, instance, endpoints):
    """Select active endpoints eligible to contribute model context for this registration."""
    with service.workspace._connect() as db:
        rows = db.execute("""SELECT b.endpoint_id, b.instance_id, h.route_ref, b.account_id
            FROM workspace_host_bindings b JOIN workspace_accounts a USING(account_id)
            JOIN workspace_host_instances h USING(instance_id) WHERE a.user_subject=? AND a.state='active'""",
            (subject,)).fetchall()
        bindings = {row["endpoint_id"]: row for row in rows}
        owned = {key for key, e in endpoints.items() if e["agent_host"] == host_type and e["state"] == "active"}
        managed = [row for row in rows if row["instance_id"] == instance]
        if not managed:
            return set() if host_type == "nanobot" else owned.difference(bindings)
        db.execute("BEGIN")
        original = service.workspace.hosts.resolve(db, managed[0]["account_id"], instance_id=instance, existing=True)
    allowed = owned.intersection({original["endpoint_id"]})
    if host_type == "nanobot":
        from agentbridge.workspace.nanobot_gateway import load_nanobot_route_specs
        from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks, telegram_conversation_ref
        spec = load_nanobot_route_specs().get(original["route_ref"]) or {}
        bot = str((spec.get("telegram") or {}).get("botId", ""))
        if spec.get("instanceId") != instance or not bot.isdigit():
            return allowed
        links = TelegramPilotLinks(service)
        for key in owned:
            e = endpoints[key]
            sender = e["external_subject"]
            if e["client_type"] != "telegram" or str(e["account_id"]) != bot or not sender.isdigit():
                continue
            if (e["conversation_ref"] == telegram_conversation_ref(instance, subject, original["endpoint_id"], bot, sender)
                    and links.verified(subject, instance, bot, sender)):
                allowed.add(key)
    return allowed
