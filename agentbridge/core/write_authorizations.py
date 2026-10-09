from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Callable, Iterator


class WriteAuthorizationNotFound(KeyError):
    pass


class WriteAuthorizationStateError(RuntimeError):
    pass


class WriteAuthorizationAccessDenied(RuntimeError):
    pass


class WriteAuthorizationIntegrityError(RuntimeError):
    pass


class WriteAuthorizationStore:
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize()

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

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS write_authorizations (
                    authorization_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    system_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    capability_name TEXT NOT NULL,
                    capability_version TEXT NOT NULL,
                    prepare_operation_id TEXT NOT NULL,
                    supersession_key TEXT NOT NULL DEFAULT '',
                    plan_json TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    card_url TEXT NOT NULL,
                    csrf_hash TEXT,
                    state TEXT NOT NULL,
                    commit_operation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decided_at TEXT,
                    consumed_at TEXT,
                    decided_endpoint_id TEXT
                );

                CREATE INDEX IF NOT EXISTS write_authorizations_subject_state
                ON write_authorizations (user_subject, state, created_at);

                CREATE TRIGGER IF NOT EXISTS immutable_write_authorization_plan
                BEFORE UPDATE OF user_subject, system_id, session_id,
                    capability_name, capability_version, prepare_operation_id,
                    plan_json, plan_hash, summary_json, card_url, created_at,
                    expires_at
                ON write_authorizations
                BEGIN
                    SELECT RAISE(ABORT, 'write authorization plan is immutable');
                END;

                CREATE TABLE IF NOT EXISTS write_authorization_presentations (
                    presentation_id TEXT PRIMARY KEY,
                    authorization_id TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    card_url TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    opened_at TEXT,
                    decided_at TEXT,
                    UNIQUE (authorization_id, endpoint_id)
                );

                CREATE INDEX IF NOT EXISTS write_authorization_presentations_auth
                ON write_authorization_presentations (
                    authorization_id, state, created_at
                );

                CREATE TABLE IF NOT EXISTS write_authorization_card_sessions (
                    card_session_id TEXT PRIMARY KEY,
                    authorization_id TEXT NOT NULL,
                    presentation_id TEXT,
                    endpoint_id TEXT,
                    csrf_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS write_authorization_card_sessions_auth
                ON write_authorization_card_sessions (
                    authorization_id, state, created_at
                );
                """
            )
            _ensure_column(
                connection,
                "write_authorizations",
                "decided_endpoint_id",
                "TEXT",
            )
            _ensure_column(
                connection,
                "write_authorizations",
                "supersession_key",
                "TEXT NOT NULL DEFAULT ''",
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS immutable_write_authorization_plan"
            )
            connection.execute(
                """
                CREATE TRIGGER immutable_write_authorization_plan
                BEFORE UPDATE OF user_subject, system_id, session_id,
                    capability_name, capability_version, prepare_operation_id,
                    supersession_key, plan_json, plan_hash, summary_json,
                    card_url, created_at, expires_at
                ON write_authorizations
                BEGIN
                    SELECT RAISE(ABORT, 'write authorization plan is immutable');
                END
                """
            )

    def create(
        self,
        *,
        user_subject: str,
        system_id: str,
        session_id: str,
        capability_name: str,
        capability_version: str,
        prepare_operation_id: str,
        supersession_key: str = "",
        plan: dict[str, Any],
        summary: dict[str, Any],
        card_base_url: str,
        ttl_seconds: int = 600,
    ) -> dict:
        required = {
            "user_subject": user_subject,
            "system_id": system_id,
            "session_id": session_id,
            "capability_name": capability_name,
            "capability_version": capability_version,
            "prepare_operation_id": prepare_operation_id,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"write authorization is missing: {', '.join(missing)}")
        if not isinstance(plan, dict) or not isinstance(summary, dict):
            raise TypeError("write authorization plan and summary must be objects")
        if ttl_seconds < 30 or ttl_seconds > 1800:
            raise ValueError("write authorization TTL must be between 30 and 1800 seconds")
        normalized_supersession_key = str(supersession_key or "").strip()
        if len(normalized_supersession_key) > 128:
            raise ValueError("write authorization supersession key is too long")
        base_url = _validate_card_base_url(card_base_url)
        plan_json = _canonical_json(plan)
        plan_hash = _json_hash(plan_json)
        summary_json = _canonical_json(summary)
        now = _as_utc(self.clock())
        expires_at = now + timedelta(seconds=ttl_seconds)
        authorization_id = secrets.token_urlsafe(32)
        card_url = f"{base_url}/authorize/{authorization_id}"

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE write_authorizations
                SET state = 'superseded', csrf_hash = NULL,
                    updated_at = ?, decided_at = ?
                WHERE user_subject = ? AND capability_name = ?
                  AND supersession_key = ?
                  AND state IN ('pending', 'approved')
                """,
                (
                    _format_time(now),
                    _format_time(now),
                    user_subject,
                    capability_name,
                    normalized_supersession_key,
                ),
            )
            connection.execute(
                """
                UPDATE write_authorization_presentations
                SET state = 'expired', updated_at = ?
                WHERE state = 'active'
                  AND authorization_id IN (
                    SELECT authorization_id FROM write_authorizations
                    WHERE user_subject = ? AND capability_name = ?
                      AND state = 'superseded'
                  )
                """,
                (_format_time(now), user_subject, capability_name),
            )
            connection.execute(
                """
                UPDATE write_authorization_card_sessions
                SET state = 'expired'
                WHERE state = 'pending'
                  AND authorization_id IN (
                    SELECT authorization_id FROM write_authorizations
                    WHERE user_subject = ? AND capability_name = ?
                      AND state = 'superseded'
                  )
                """,
                (user_subject, capability_name),
            )
            connection.execute(
                """
                INSERT INTO write_authorizations (
                    authorization_id, user_subject, system_id, session_id,
                    capability_name, capability_version, prepare_operation_id,
                    supersession_key, plan_json, plan_hash, summary_json,
                    card_url, state,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    authorization_id,
                    user_subject,
                    system_id,
                    session_id,
                    capability_name,
                    capability_version,
                    prepare_operation_id,
                    normalized_supersession_key,
                    plan_json,
                    plan_hash,
                    summary_json,
                    card_url,
                    _format_time(now),
                    _format_time(now),
                    _format_time(expires_at),
                ),
            )
            row = self._select(connection, authorization_id)
        return _authorization_from_row(row, include_plan=False)

    def get(self, authorization_id: str, *, include_plan: bool = False) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, authorization_id))
        return _authorization_from_row(row, include_plan=include_plan)

    def create_presentation(
        self,
        authorization_id: str,
        *,
        user_subject: str,
        endpoint_id: str,
    ) -> dict:
        user_subject = str(user_subject or "").strip()
        endpoint_id = str(endpoint_id or "").strip()
        if not user_subject or not endpoint_id:
            raise ValueError(
                "write authorization presentation identity is required"
            )
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            authorization = self._expire_if_needed(
                connection,
                self._select(connection, authorization_id),
            )
            if authorization["user_subject"] != user_subject:
                raise WriteAuthorizationNotFound(
                    f"write authorization not found: {authorization_id}"
                )
            existing = connection.execute(
                """
                SELECT * FROM write_authorization_presentations
                WHERE authorization_id = ? AND endpoint_id = ?
                """,
                (authorization_id, endpoint_id),
            ).fetchone()
            if existing is not None:
                return _presentation_from_row(existing)
            presentation_id = secrets.token_urlsafe(32)
            card_url = (
                f"{_card_base_url(authorization['card_url'])}"
                f"/authorize/{authorization_id}/present/{presentation_id}"
            )
            presentation_state = (
                "active"
                if authorization["state"] == "pending"
                else "expired"
                if authorization["state"] in {"expired", "superseded"}
                else "decided"
            )
            connection.execute(
                """
                INSERT INTO write_authorization_presentations (
                    presentation_id, authorization_id, user_subject,
                    endpoint_id, card_url, state, created_at, updated_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    presentation_id,
                    authorization_id,
                    user_subject,
                    endpoint_id,
                    card_url,
                    presentation_state,
                    _format_time(now),
                    _format_time(now),
                    authorization["expires_at"],
                ),
            )
            row = self._select_presentation(
                connection,
                authorization_id,
                presentation_id,
            )
        return _presentation_from_row(row)

    def get_presentation(
        self,
        authorization_id: str,
        presentation_id: str,
    ) -> dict:
        with self._connect() as connection:
            row = self._select_presentation(
                connection,
                authorization_id,
                presentation_id,
            )
        return _presentation_from_row(row)

    def issue_csrf(
        self,
        authorization_id: str,
        *,
        presentation_id: str | None = None,
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, authorization_id))
            if row["state"] != "pending":
                raise WriteAuthorizationStateError(
                    f"write authorization is not pending: {row['state']}"
                )
            endpoint_id = None
            if presentation_id is not None:
                presentation = self._select_presentation(
                    connection,
                    authorization_id,
                    presentation_id,
                )
                endpoint_id = presentation["endpoint_id"]
                connection.execute(
                    """
                    UPDATE write_authorization_presentations
                    SET opened_at = COALESCE(opened_at, ?), updated_at = ?
                    WHERE presentation_id = ?
                    """,
                    (
                        _format_time(now),
                        _format_time(now),
                        presentation_id,
                    ),
                )
            connection.execute(
                """
                INSERT INTO write_authorization_card_sessions (
                    card_session_id, authorization_id, presentation_id,
                    endpoint_id, csrf_hash, state, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    secrets.token_urlsafe(24),
                    authorization_id,
                    presentation_id,
                    endpoint_id,
                    _token_hash(token),
                    _format_time(now),
                    row["expires_at"],
                ),
            )
        return token

    def decide(
        self,
        authorization_id: str,
        *,
        decision: str,
        csrf_token: str,
        csrf_cookie: str,
        presentation_id: str | None = None,
    ) -> dict:
        if decision not in {"approve", "reject"}:
            raise ValueError("write authorization decision must be approve or reject")
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, authorization_id))
            if row["state"] != "pending":
                raise WriteAuthorizationStateError(
                    f"write authorization is not pending: {row['state']}"
                )
            supplied_hash = _token_hash(csrf_token) if csrf_token else ""
            if presentation_id is None:
                session = connection.execute(
                    """
                    SELECT * FROM write_authorization_card_sessions
                    WHERE authorization_id = ? AND presentation_id IS NULL
                      AND csrf_hash = ? AND state = 'pending'
                    """,
                    (authorization_id, supplied_hash),
                ).fetchone()
            else:
                self._select_presentation(
                    connection,
                    authorization_id,
                    presentation_id,
                )
                session = connection.execute(
                    """
                    SELECT * FROM write_authorization_card_sessions
                    WHERE authorization_id = ? AND presentation_id = ?
                      AND csrf_hash = ? AND state = 'pending'
                    """,
                    (
                        authorization_id,
                        presentation_id,
                        supplied_hash,
                    ),
                ).fetchone()
            if (
                session is None
                or not csrf_cookie
                or not hmac.compare_digest(csrf_token, csrf_cookie)
                or not hmac.compare_digest(
                    str(session["csrf_hash"]),
                    supplied_hash,
                )
            ):
                raise WriteAuthorizationAccessDenied(
                    "write authorization card CSRF validation failed"
                )
            state = "approved" if decision == "approve" else "rejected"
            cursor = connection.execute(
                """
                UPDATE write_authorizations
                SET state = ?, csrf_hash = NULL, decided_at = ?, updated_at = ?,
                    decided_endpoint_id = ?
                WHERE authorization_id = ? AND state = 'pending'
                """,
                (
                    state,
                    _format_time(now),
                    _format_time(now),
                    session["endpoint_id"],
                    authorization_id,
                ),
            )
            if cursor.rowcount != 1:
                raise WriteAuthorizationStateError(
                    "write authorization could not be decided"
                )
            connection.execute(
                """
                UPDATE write_authorization_card_sessions
                SET state = 'consumed', consumed_at = ?
                WHERE authorization_id = ? AND state = 'pending'
                """,
                (_format_time(now), authorization_id),
            )
            connection.execute(
                """
                UPDATE write_authorization_presentations
                SET state = 'decided', decided_at = ?, updated_at = ?
                WHERE authorization_id = ?
                """,
                (
                    _format_time(now),
                    _format_time(now),
                    authorization_id,
                ),
            )
            row = self._select(connection, authorization_id)
        return _authorization_from_row(row, include_plan=False)

    def supersede(self, authorization_id: str, *, user_subject: str, pending_only: bool = False, before_supersede: Callable[[sqlite3.Connection], None] | None = None) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, authorization_id)
            if row["user_subject"] != user_subject:
                raise WriteAuthorizationAccessDenied("write authorization belongs to another user")
            if pending_only and row["state"] not in {"pending", "superseded"}:
                raise WriteAuthorizationStateError("write authorization is no longer pending")
            if row["state"] not in {"pending", "approved"}:
                return
            if before_supersede:
                before_supersede(connection)
            now = _format_time(_as_utc(self.clock()))
            connection.execute(
                "UPDATE write_authorizations SET state = 'superseded', csrf_hash = NULL, updated_at = ? WHERE authorization_id = ?",
                (now, authorization_id),
            )
            connection.execute(
                "UPDATE write_authorization_presentations SET state = 'expired', updated_at = ? WHERE authorization_id = ? AND state = 'active'",
                (now, authorization_id),
            )
            connection.execute(
                "UPDATE write_authorization_card_sessions SET state = 'expired' WHERE authorization_id = ? AND state = 'pending'",
                (authorization_id,),
            )

    def consume(
        self,
        authorization_id: str,
        *,
        user_subject: str,
        system_id: str,
        session_id: str,
        capability_name: str,
        capability_version: str,
        commit_operation_id: str,
        before_consume: Callable[[sqlite3.Connection], None] | None = None,
    ) -> dict:
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, authorization_id))
            if row["state"] != "approved":
                raise WriteAuthorizationStateError(
                    f"write authorization is not approved: {row['state']}"
                )
            bindings_match = all(
                (
                    row["user_subject"] == user_subject,
                    row["system_id"] == system_id,
                    row["session_id"] == session_id,
                    row["capability_name"] == capability_name,
                    row["capability_version"] == capability_version,
                )
            )
            if not bindings_match:
                raise WriteAuthorizationAccessDenied(
                    "write authorization is not bound to this user, session, or capability"
                )
            plan_json = str(row["plan_json"])
            if not hmac.compare_digest(str(row["plan_hash"]), _json_hash(plan_json)):
                raise WriteAuthorizationIntegrityError(
                    "write authorization plan integrity check failed"
                )
            if before_consume is not None:
                before_consume(connection)
            cursor = connection.execute(
                """
                UPDATE write_authorizations
                SET state = 'consumed', commit_operation_id = ?,
                    consumed_at = ?, updated_at = ?
                WHERE authorization_id = ? AND state = 'approved'
                """,
                (
                    commit_operation_id,
                    _format_time(now),
                    _format_time(now),
                    authorization_id,
                ),
            )
            if cursor.rowcount != 1:
                raise WriteAuthorizationStateError(
                    "write authorization could not be consumed"
                )
            row = self._select(connection, authorization_id)
        return _authorization_from_row(row, include_plan=True)

    def _select(self, connection: sqlite3.Connection, authorization_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM write_authorizations WHERE authorization_id = ?",
            (authorization_id,),
        ).fetchone()
        if row is None:
            raise WriteAuthorizationNotFound(
                f"write authorization not found: {authorization_id}"
            )
        return row

    @staticmethod
    def _select_presentation(
        connection: sqlite3.Connection,
        authorization_id: str,
        presentation_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM write_authorization_presentations
            WHERE authorization_id = ? AND presentation_id = ?
            """,
            (authorization_id, presentation_id),
        ).fetchone()
        if row is None:
            raise WriteAuthorizationNotFound(
                "write authorization presentation not found"
            )
        return row

    def _expire_if_needed(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> sqlite3.Row:
        now = _as_utc(self.clock())
        if row["state"] in {"pending", "approved"} and now >= _parse_time(row["expires_at"]):
            connection.execute(
                """
                UPDATE write_authorizations
                SET state = 'expired', csrf_hash = NULL,
                    decided_at = COALESCE(decided_at, ?), updated_at = ?
                WHERE authorization_id = ?
                """,
                (_format_time(now), _format_time(now), row["authorization_id"]),
            )
            connection.execute(
                """
                UPDATE write_authorization_presentations
                SET state = 'expired', updated_at = ?
                WHERE authorization_id = ? AND state = 'active'
                """,
                (_format_time(now), row["authorization_id"]),
            )
            connection.execute(
                """
                UPDATE write_authorization_card_sessions
                SET state = 'expired'
                WHERE authorization_id = ? AND state = 'pending'
                """,
                (row["authorization_id"],),
            )
            return self._select(connection, row["authorization_id"])
        return row


def _authorization_from_row(row: sqlite3.Row, *, include_plan: bool) -> dict:
    value = dict(row)
    plan = json.loads(value.pop("plan_json"))
    value["summary"] = json.loads(value.pop("summary_json"))
    value.pop("csrf_hash", None)
    if include_plan:
        value["plan"] = plan
    return value


def _presentation_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _card_base_url(card_url: str) -> str:
    marker = "/authorize/"
    if marker not in card_url:
        raise WriteAuthorizationIntegrityError(
            "write authorization card URL is invalid"
        )
    return card_url.rsplit(marker, 1)[0]


def _validate_card_base_url(value: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("write authorization card base URL must be http(s)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("write authorization card base URL is invalid")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_hash(canonical_json: str) -> str:
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("write authorization clock must return timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
        )
