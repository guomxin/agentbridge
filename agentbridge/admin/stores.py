from __future__ import annotations

import base64
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Callable, Iterator
from uuid import uuid4


ADMIN_ROLES = frozenset({"admin", "auditor"})
POLICY_SCOPE_TYPES = frozenset({"global", "system", "user", "capability"})
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,63}$")
_PASSWORD_MIN_LENGTH = 12
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class GovernancePolicyDenied(PermissionError):
    def __init__(self, policy: dict) -> None:
        self.policy = policy
        reason = policy.get("reason") or "Write operations are paused by an administrator."
        super().__init__(str(reason))


class _SqliteStore:
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class AdminAccountStore(_SqliteStore):
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(db_path, clock=clock)
        self._initialize()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_accounts (
                    account_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    state TEXT NOT NULL,
                    must_change_password INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    password_changed_at TEXT,
                    last_login_at TEXT
                )
                """
            )

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM admin_accounts"
            ).fetchone()
        return int(row["count"])

    def create(
        self,
        *,
        username: str,
        password: str,
        role: str = "admin",
        must_change_password: bool = True,
    ) -> dict:
        normalized_username = _validate_username(username)
        normalized_role = _validate_role(role)
        password_hash = hash_admin_password(password)
        now = _format_time(self._now())
        account_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_accounts (
                    account_id, username, password_hash, role, state,
                    must_change_password, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    account_id,
                    normalized_username,
                    password_hash,
                    normalized_role,
                    1 if must_change_password else 0,
                    now,
                    now,
                ),
            )
        return self.get(account_id)

    def authenticate(self, *, username: str, password: str) -> dict | None:
        normalized_username = str(username or "").strip()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM admin_accounts WHERE username = ? COLLATE NOCASE",
                (normalized_username,),
            ).fetchone()
        if row is None:
            verify_admin_password(
                password,
                hash_admin_password("AgentBridge-dummy-password"),
            )
            return None
        account = _account_from_row(row)
        if account["state"] != "active":
            return None
        return account if verify_admin_password(password, row["password_hash"]) else None

    def record_login(self, account_id: str) -> dict:
        now = _format_time(self._now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE admin_accounts
                SET last_login_at = ?, updated_at = ?
                WHERE account_id = ? AND state = 'active'
                """,
                (now, now, account_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"admin account not found: {account_id}")
        return self.get(account_id)

    def change_password(
        self,
        *,
        account_id: str,
        current_password: str,
        new_password: str,
    ) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM admin_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is None or row["state"] != "active":
                raise KeyError(f"admin account not found: {account_id}")
            if not verify_admin_password(current_password, row["password_hash"]):
                raise PermissionError("current password is incorrect")
            if verify_admin_password(new_password, row["password_hash"]):
                raise ValueError("new password must be different")
            new_hash = hash_admin_password(new_password)
            now = _format_time(self._now())
            connection.execute(
                """
                UPDATE admin_accounts
                SET password_hash = ?, must_change_password = 0,
                    password_changed_at = ?, updated_at = ?
                WHERE account_id = ?
                """,
                (new_hash, now, now, account_id),
            )
        return self.get(account_id)

    def get(self, account_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM admin_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"admin account not found: {account_id}")
        return _account_from_row(row)

    def list(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM admin_accounts ORDER BY created_at"
            ).fetchall()
        return [_account_from_row(row) for row in rows]


ADMIN_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
ADMIN_SESSION_IDLE_SECONDS = 24 * 60 * 60


class AdminSessionStore(_SqliteStore):
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        ttl_seconds: int = ADMIN_SESSION_TTL_SECONDS,
        idle_seconds: int = ADMIN_SESSION_IDLE_SECONDS,
    ) -> None:
        super().__init__(db_path, clock=clock)
        if ttl_seconds < 300 or idle_seconds < 60 or idle_seconds > ttl_seconds:
            raise ValueError("invalid admin session lifetime")
        self.ttl_seconds = ttl_seconds
        self.idle_seconds = idle_seconds
        self._initialize()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    session_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked_at TEXT,
                    request_ip TEXT,
                    user_agent TEXT,
                    FOREIGN KEY (account_id) REFERENCES admin_accounts(account_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS admin_sessions_account_active
                ON admin_sessions (account_id, revoked_at, expires_at)
                """
            )

    def create(
        self,
        *,
        account_id: str,
        request_ip: str,
        user_agent: str,
    ) -> dict:
        now = self._now()
        session_id = str(uuid4())
        token = f"abadmin_{secrets.token_urlsafe(32)}"
        csrf_token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_sessions (
                    session_id, account_id, token_hash, csrf_hash,
                    created_at, expires_at, last_seen_at, request_ip, user_agent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    account_id,
                    _secret_hash(token),
                    _secret_hash(csrf_token),
                    _format_time(now),
                    _format_time(expires_at),
                    _format_time(now),
                    str(request_ip or "")[:128],
                    str(user_agent or "")[:512],
                ),
            )
        return {
            "session_id": session_id,
            "token": token,
            "csrf_token": csrf_token,
            "expires_at": _format_time(expires_at),
        }

    def verify(self, token: str, *, csrf_token: str | None = None) -> dict | None:
        if not isinstance(token, str) or not token.startswith("abadmin_"):
            return None
        now = self._now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, a.username, a.role, a.state AS account_state,
                       a.must_change_password
                FROM admin_sessions s
                JOIN admin_accounts a ON a.account_id = s.account_id
                WHERE s.token_hash = ?
                """,
                (_secret_hash(token),),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                return None
            if row["account_state"] != "active":
                return None
            if _parse_time(row["expires_at"]) <= now:
                return None
            if _parse_time(row["last_seen_at"]) + timedelta(seconds=self.idle_seconds) <= now:
                return None
            if csrf_token is not None and not hmac.compare_digest(
                row["csrf_hash"],
                _secret_hash(csrf_token),
            ):
                return None
            connection.execute(
                "UPDATE admin_sessions SET last_seen_at = ? WHERE session_id = ?",
                (_format_time(now), row["session_id"]),
            )
        return {
            "session_id": row["session_id"],
            "account_id": row["account_id"],
            "username": row["username"],
            "role": row["role"],
            "must_change_password": bool(row["must_change_password"]),
            "expires_at": row["expires_at"],
        }

    def revoke(self, token: str) -> None:
        if not token:
            return
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE admin_sessions
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE token_hash = ?
                """,
                (_format_time(self._now()), _secret_hash(token)),
            )

    def revoke_account_sessions(
        self,
        account_id: str,
        *,
        except_session_id: str | None = None,
    ) -> int:
        query = """
            UPDATE admin_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE account_id = ? AND revoked_at IS NULL
        """
        parameters: list[str] = [_format_time(self._now()), account_id]
        if except_session_id:
            query += " AND session_id <> ?"
            parameters.append(except_session_id)
        with self._connect() as connection:
            cursor = connection.execute(query, parameters)
        return cursor.rowcount


class AdminAuditStore(_SqliteStore):
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(db_path, clock=clock)
        self._initialize()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_audit_events (
                    event_id TEXT PRIMARY KEY,
                    actor_account_id TEXT,
                    actor_username TEXT,
                    actor_role TEXT,
                    action TEXT NOT NULL,
                    target_type TEXT,
                    target_id TEXT,
                    request_ip TEXT,
                    reason TEXT,
                    before_json TEXT,
                    after_json TEXT,
                    result TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS admin_audit_created
                ON admin_audit_events (created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS immutable_admin_audit
                BEFORE UPDATE ON admin_audit_events
                BEGIN
                    SELECT RAISE(ABORT, 'admin audit events are immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS undeletable_admin_audit
                BEFORE DELETE ON admin_audit_events
                BEGIN
                    SELECT RAISE(ABORT, 'admin audit events cannot be deleted');
                END
                """
            )

    def append(
        self,
        *,
        actor: dict | None,
        action: str,
        result: str,
        request_ip: str = "",
        target_type: str | None = None,
        target_id: str | None = None,
        reason: str | None = None,
        before: Any = None,
        after: Any = None,
        error: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict:
        event_id = str(uuid4())
        created_at = _format_time(self._now())
        external_connection = connection is not None
        with (nullcontext(connection) if external_connection else self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO admin_audit_events (
                    event_id, actor_account_id, actor_username, actor_role,
                    action, target_type, target_id, request_ip, reason,
                    before_json, after_json, result, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    actor.get("account_id") if actor else None,
                    actor.get("username") if actor else None,
                    actor.get("role") if actor else None,
                    str(action)[:128],
                    str(target_type)[:64] if target_type else None,
                    str(target_id)[:256] if target_id else None,
                    str(request_ip or "")[:128],
                    _normalize_reason(reason, required=False),
                    _json_dump(before),
                    _json_dump(after),
                    str(result)[:32],
                    str(error)[:1000] if error else None,
                    created_at,
                ),
            )
        return {"event_id": event_id} if external_connection else self.get(event_id)

    def get(self, event_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM admin_audit_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"admin audit event not found: {event_id}")
        return _audit_from_row(row)

    def list(self, *, limit: int = 200) -> list[dict]:
        limit = min(max(int(limit), 1), 1000)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM admin_audit_events
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_audit_from_row(row) for row in rows]


class GovernancePolicyStore(_SqliteStore):
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(db_path, clock=clock)
        self._initialize()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS governance_policies (
                    policy_id TEXT PRIMARY KEY,
                    scope_type TEXT NOT NULL,
                    scope_value TEXT NOT NULL,
                    capability_version TEXT NOT NULL DEFAULT '*',
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (scope_type, scope_value, capability_version)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS governance_policies_state_scope
                ON governance_policies (state, scope_type, scope_value)
                """
            )

    def pause(
        self,
        *,
        scope_type: str,
        scope_value: str,
        reason: str,
        actor: str,
        capability_version: str = "*",
    ) -> dict:
        normalized_type, normalized_value, normalized_version = _validate_policy_scope(
            scope_type,
            scope_value,
            capability_version,
        )
        normalized_reason = _normalize_reason(reason, required=True)
        normalized_actor = str(actor or "").strip()
        if not normalized_actor:
            raise ValueError("policy actor is required")
        now = _format_time(self._now())
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT policy_id FROM governance_policies
                WHERE scope_type = ? AND scope_value = ? AND capability_version = ?
                """,
                (normalized_type, normalized_value, normalized_version),
            ).fetchone()
            if existing is None:
                policy_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO governance_policies (
                        policy_id, scope_type, scope_value, capability_version,
                        state, reason, created_by, created_at, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, 'paused', ?, ?, ?, ?, ?)
                    """,
                    (
                        policy_id,
                        normalized_type,
                        normalized_value,
                        normalized_version,
                        normalized_reason,
                        normalized_actor,
                        now,
                        normalized_actor,
                        now,
                    ),
                )
            else:
                policy_id = existing["policy_id"]
                connection.execute(
                    """
                    UPDATE governance_policies
                    SET state = 'paused', reason = ?, updated_by = ?, updated_at = ?
                    WHERE policy_id = ?
                    """,
                    (normalized_reason, normalized_actor, now, policy_id),
                )
        return self.get(policy_id)

    def resume(self, policy_id: str, *, reason: str, actor: str) -> dict:
        normalized_reason = _normalize_reason(reason, required=True)
        normalized_actor = str(actor or "").strip()
        if not normalized_actor:
            raise ValueError("policy actor is required")
        now = _format_time(self._now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE governance_policies
                SET state = 'active', reason = ?, updated_by = ?, updated_at = ?
                WHERE policy_id = ?
                """,
                (normalized_reason, normalized_actor, now, policy_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"governance policy not found: {policy_id}")
        return self.get(policy_id)

    def get(self, policy_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM governance_policies WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"governance policy not found: {policy_id}")
        return dict(row)

    def list(self, *, state: str | None = None) -> list[dict]:
        query = "SELECT * FROM governance_policies"
        parameters: list[str] = []
        if state:
            query += " WHERE state = ?"
            parameters.append(state)
        query += " ORDER BY CASE state WHEN 'paused' THEN 0 ELSE 1 END, updated_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def matching_pause(
        self,
        *,
        system_id: str,
        user_subject: str,
        capability_name: str,
        capability_version: str,
    ) -> dict | None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM governance_policies
                WHERE state = 'paused' AND (
                    (scope_type = 'global' AND scope_value = '*')
                    OR (scope_type = 'system' AND scope_value = ?)
                    OR (scope_type = 'user' AND scope_value = ?)
                    OR (
                        scope_type = 'capability' AND scope_value = ?
                        AND capability_version IN ('*', ?)
                    )
                )
                ORDER BY CASE scope_type
                    WHEN 'capability' THEN 0
                    WHEN 'user' THEN 1
                    WHEN 'system' THEN 2
                    ELSE 3
                END, updated_at DESC
                LIMIT 1
                """,
                (
                    system_id,
                    user_subject,
                    capability_name,
                    capability_version,
                ),
            ).fetchall()
        return dict(rows[0]) if rows else None

    def assert_write_allowed(
        self,
        *,
        system_id: str,
        user_subject: str,
        capability_name: str,
        capability_version: str,
    ) -> None:
        policy = self.matching_pause(
            system_id=system_id,
            user_subject=user_subject,
            capability_name=capability_name,
            capability_version=capability_version,
        )
        if policy is not None:
            raise GovernancePolicyDenied(policy)


def hash_admin_password(password: str) -> str:
    _validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_admin_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_b64decode(expected)),
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(digest, _b64decode(expected))


def _account_from_row(row: sqlite3.Row) -> dict:
    return {
        "account_id": row["account_id"],
        "username": row["username"],
        "role": row["role"],
        "state": row["state"],
        "must_change_password": bool(row["must_change_password"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "password_changed_at": row["password_changed_at"],
        "last_login_at": row["last_login_at"],
    }


def _audit_from_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["before"] = _json_load(value.pop("before_json"))
    value["after"] = _json_load(value.pop("after_json"))
    return value


def _validate_username(value: str) -> str:
    value = str(value or "").strip()
    if not _USERNAME_RE.fullmatch(value):
        raise ValueError(
            "admin username must be 3-64 characters and start with a letter"
        )
    return value


def _validate_role(value: str) -> str:
    value = str(value or "").strip().lower()
    if value not in ADMIN_ROLES:
        raise ValueError("admin role must be admin or auditor")
    return value


def _validate_password(value: str) -> None:
    if not isinstance(value, str) or len(value) < _PASSWORD_MIN_LENGTH:
        raise ValueError("admin password must contain at least 12 characters")
    if len(value) > 1024 or any(ord(character) < 32 for character in value):
        raise ValueError("admin password is invalid")
    categories = (
        any(character.islower() for character in value),
        any(character.isupper() for character in value),
        any(character.isdigit() for character in value),
        any(not character.isalnum() for character in value),
    )
    if sum(categories) < 3:
        raise ValueError("admin password must use at least three character classes")


def _validate_policy_scope(
    scope_type: str,
    scope_value: str,
    capability_version: str,
) -> tuple[str, str, str]:
    normalized_type = str(scope_type or "").strip().lower()
    if normalized_type not in POLICY_SCOPE_TYPES:
        raise ValueError("unsupported governance policy scope")
    normalized_value = str(scope_value or "").strip()
    if normalized_type == "global":
        normalized_value = "*"
    if not normalized_value or len(normalized_value) > 256:
        raise ValueError("governance policy scope value is invalid")
    normalized_version = str(capability_version or "*").strip() or "*"
    if normalized_type != "capability":
        normalized_version = "*"
    if len(normalized_version) > 64:
        raise ValueError("governance policy capability version is invalid")
    return normalized_type, normalized_value, normalized_version


def _normalize_reason(value: str | None, *, required: bool) -> str | None:
    normalized = str(value or "").strip()
    if required and len(normalized) < 3:
        raise ValueError("a reason of at least 3 characters is required")
    if len(normalized) > 500 or any(ord(character) < 32 for character in normalized):
        raise ValueError("reason is invalid")
    return normalized or None


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str | None) -> Any:
    return json.loads(value) if value is not None else None
