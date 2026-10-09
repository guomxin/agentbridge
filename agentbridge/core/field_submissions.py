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


class FieldSubmissionNotFound(KeyError):
    pass


class FieldSubmissionStateError(RuntimeError):
    pass


class FieldSubmissionAccessDenied(RuntimeError):
    pass


class FieldSubmissionIntegrityError(RuntimeError):
    pass


class FieldSubmissionStore:
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS field_submissions (
                    submission_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    system_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    capability_name TEXT NOT NULL,
                    capability_version TEXT NOT NULL,
                    create_operation_id TEXT NOT NULL,
                    supersession_key TEXT NOT NULL DEFAULT '',
                    schema_json TEXT NOT NULL,
                    schema_hash TEXT NOT NULL,
                    values_json TEXT,
                    values_hash TEXT,
                    card_url TEXT NOT NULL,
                    csrf_hash TEXT,
                    state TEXT NOT NULL,
                    consume_operation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    submitted_at TEXT,
                    consumed_at TEXT
                )
                """
            )
            _ensure_column(
                connection,
                "field_submissions",
                "supersession_key",
                "TEXT NOT NULL DEFAULT ''",
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS field_submissions_subject_state
                ON field_submissions (user_subject, state, created_at)
                """
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS immutable_field_submission_contract"
            )
            connection.execute(
                """
                CREATE TRIGGER immutable_field_submission_contract
                BEFORE UPDATE OF user_subject, system_id, session_id,
                    capability_name, capability_version, create_operation_id,
                    supersession_key, schema_json, schema_hash, card_url,
                    created_at, expires_at
                ON field_submissions
                BEGIN
                    SELECT RAISE(ABORT, 'field submission contract is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS immutable_submitted_field_values
                BEFORE UPDATE OF values_json, values_hash
                ON field_submissions
                WHEN NOT (
                    OLD.state = 'pending'
                    AND NEW.state = 'submitted'
                    AND OLD.values_json IS NULL
                    AND OLD.values_hash IS NULL
                    AND NEW.values_json IS NOT NULL
                    AND NEW.values_hash IS NOT NULL
                )
                BEGIN
                    SELECT RAISE(ABORT, 'submitted field values are immutable');
                END
                """
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS field_submission_presentations (
                    presentation_id TEXT PRIMARY KEY,
                    submission_id TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    card_url TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    opened_at TEXT,
                    submitted_at TEXT,
                    UNIQUE (submission_id, endpoint_id)
                );

                CREATE INDEX IF NOT EXISTS field_submission_presentations_input
                ON field_submission_presentations (
                    submission_id, state, created_at
                );

                CREATE TABLE IF NOT EXISTS field_submission_card_sessions (
                    card_session_id TEXT PRIMARY KEY,
                    submission_id TEXT NOT NULL,
                    presentation_id TEXT,
                    endpoint_id TEXT,
                    csrf_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS field_submission_card_sessions_input
                ON field_submission_card_sessions (
                    submission_id, state, created_at
                );
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
        create_operation_id: str,
        supersession_key: str = "",
        form_schema: dict[str, Any],
        card_base_url: str,
        ttl_seconds: int = 900,
    ) -> dict:
        required = {
            "user_subject": user_subject,
            "system_id": system_id,
            "session_id": session_id,
            "capability_name": capability_name,
            "capability_version": capability_version,
            "create_operation_id": create_operation_id,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"field submission is missing: {', '.join(missing)}")
        if not isinstance(form_schema, dict):
            raise TypeError("field submission schema must be an object")
        fields = form_schema.get("fields")
        if not isinstance(fields, list) or not fields:
            raise ValueError("field submission schema must define fields")
        if ttl_seconds < 30 or ttl_seconds > 1800:
            raise ValueError("field submission TTL must be between 30 and 1800 seconds")
        normalized_supersession_key = str(supersession_key or "").strip()
        if len(normalized_supersession_key) > 128:
            raise ValueError("field submission supersession key is too long")
        base_url = _validate_card_base_url(card_base_url)
        schema_json = _canonical_json(form_schema)
        schema_hash = _json_hash(schema_json)
        now = _as_utc(self.clock())
        expires_at = now + timedelta(seconds=ttl_seconds)
        submission_id = secrets.token_urlsafe(32)
        card_url = f"{base_url}/input/{submission_id}"

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE field_submissions
                SET state = 'superseded', csrf_hash = NULL, updated_at = ?
                WHERE user_subject = ? AND capability_name = ?
                  AND supersession_key = ?
                  AND state IN ('pending', 'submitted')
                """,
                (
                    _format_time(now),
                    user_subject,
                    capability_name,
                    normalized_supersession_key,
                ),
            )
            connection.execute(
                """
                UPDATE field_submission_presentations
                SET state = 'expired', updated_at = ?
                WHERE state = 'active'
                  AND submission_id IN (
                    SELECT submission_id FROM field_submissions
                    WHERE user_subject = ? AND capability_name = ?
                      AND state = 'superseded'
                  )
                """,
                (_format_time(now), user_subject, capability_name),
            )
            connection.execute(
                """
                UPDATE field_submission_card_sessions
                SET state = 'expired'
                WHERE state = 'pending'
                  AND submission_id IN (
                    SELECT submission_id FROM field_submissions
                    WHERE user_subject = ? AND capability_name = ?
                      AND state = 'superseded'
                  )
                """,
                (user_subject, capability_name),
            )
            connection.execute(
                """
                INSERT INTO field_submissions (
                    submission_id, user_subject, system_id, session_id,
                    capability_name, capability_version, create_operation_id,
                    supersession_key, schema_json, schema_hash, card_url, state,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    submission_id,
                    user_subject,
                    system_id,
                    session_id,
                    capability_name,
                    capability_version,
                    create_operation_id,
                    normalized_supersession_key,
                    schema_json,
                    schema_hash,
                    card_url,
                    _format_time(now),
                    _format_time(now),
                    _format_time(expires_at),
                ),
            )
            row = self._select(connection, submission_id)
        return _submission_from_row(row, include_values=False)

    def get(self, submission_id: str, *, include_values: bool = False) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, submission_id))
            self._verify_integrity(row, include_values=include_values)
        return _submission_from_row(row, include_values=include_values)

    def create_presentation(
        self,
        submission_id: str,
        *,
        user_subject: str,
        endpoint_id: str,
    ) -> dict:
        user_subject = str(user_subject or "").strip()
        endpoint_id = str(endpoint_id or "").strip()
        if not user_subject or not endpoint_id:
            raise ValueError("field submission presentation identity is required")
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            submission = self._expire_if_needed(
                connection,
                self._select(connection, submission_id),
            )
            if submission["user_subject"] != user_subject:
                raise FieldSubmissionNotFound(
                    f"field submission not found: {submission_id}"
                )
            existing = connection.execute(
                """
                SELECT * FROM field_submission_presentations
                WHERE submission_id = ? AND endpoint_id = ?
                """,
                (submission_id, endpoint_id),
            ).fetchone()
            if existing is not None:
                return _presentation_from_row(existing)
            presentation_id = secrets.token_urlsafe(32)
            card_url = (
                f"{_card_base_url(submission['card_url'])}"
                f"/input/{submission_id}/present/{presentation_id}"
            )
            presentation_state = (
                "active"
                if submission["state"] == "pending"
                else "expired"
                if submission["state"] in {"expired", "superseded"}
                else "submitted"
            )
            connection.execute(
                """
                INSERT INTO field_submission_presentations (
                    presentation_id, submission_id, user_subject,
                    endpoint_id, card_url, state, created_at, updated_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    presentation_id,
                    submission_id,
                    user_subject,
                    endpoint_id,
                    card_url,
                    presentation_state,
                    _format_time(now),
                    _format_time(now),
                    submission["expires_at"],
                ),
            )
            row = self._select_presentation(
                connection,
                submission_id,
                presentation_id,
            )
        return _presentation_from_row(row)

    def get_presentation(
        self,
        submission_id: str,
        presentation_id: str,
    ) -> dict:
        with self._connect() as connection:
            row = self._select_presentation(
                connection,
                submission_id,
                presentation_id,
            )
        return _presentation_from_row(row)

    def issue_csrf(
        self,
        submission_id: str,
        *,
        presentation_id: str | None = None,
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, submission_id))
            self._verify_integrity(row, include_values=False)
            if row["state"] != "pending":
                raise FieldSubmissionStateError(
                    f"field submission is not pending: {row['state']}"
                )
            endpoint_id = None
            if presentation_id is not None:
                presentation = self._select_presentation(
                    connection,
                    submission_id,
                    presentation_id,
                )
                endpoint_id = presentation["endpoint_id"]
                connection.execute(
                    """
                    UPDATE field_submission_presentations
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
                INSERT INTO field_submission_card_sessions (
                    card_session_id, submission_id, presentation_id,
                    endpoint_id, csrf_hash, state, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    secrets.token_urlsafe(24),
                    submission_id,
                    presentation_id,
                    endpoint_id,
                    _token_hash(token),
                    _format_time(now),
                    row["expires_at"],
                ),
            )
        return token

    def submit(
        self,
        submission_id: str,
        *,
        csrf_token: str,
        csrf_cookie: str,
        values: dict[str, Any],
        presentation_id: str | None = None,
    ) -> dict:
        if not isinstance(values, dict):
            raise TypeError("submitted field values must be an object")
        values_json = _canonical_json(values)
        values_hash = _json_hash(values_json)
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, submission_id))
            self._verify_integrity(row, include_values=False)
            if row["state"] != "pending":
                raise FieldSubmissionStateError(
                    f"field submission is not pending: {row['state']}"
                )
            supplied_hash = _token_hash(csrf_token) if csrf_token else ""
            if presentation_id is None:
                session = connection.execute(
                    """
                    SELECT * FROM field_submission_card_sessions
                    WHERE submission_id = ? AND presentation_id IS NULL
                      AND csrf_hash = ? AND state = 'pending'
                    """,
                    (submission_id, supplied_hash),
                ).fetchone()
            else:
                self._select_presentation(
                    connection,
                    submission_id,
                    presentation_id,
                )
                session = connection.execute(
                    """
                    SELECT * FROM field_submission_card_sessions
                    WHERE submission_id = ? AND presentation_id = ?
                      AND csrf_hash = ? AND state = 'pending'
                    """,
                    (submission_id, presentation_id, supplied_hash),
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
                raise FieldSubmissionAccessDenied(
                    "field submission card CSRF validation failed"
                )
            cursor = connection.execute(
                """
                UPDATE field_submissions
                SET state = 'submitted', values_json = ?, values_hash = ?,
                    csrf_hash = NULL, submitted_at = ?, updated_at = ?
                WHERE submission_id = ? AND state = 'pending'
                """,
                (
                    values_json,
                    values_hash,
                    _format_time(now),
                    _format_time(now),
                    submission_id,
                ),
            )
            if cursor.rowcount != 1:
                raise FieldSubmissionStateError("field submission could not be submitted")
            connection.execute(
                """
                UPDATE field_submission_card_sessions
                SET state = 'consumed', consumed_at = ?
                WHERE submission_id = ? AND state = 'pending'
                """,
                (_format_time(now), submission_id),
            )
            connection.execute(
                """
                UPDATE field_submission_presentations
                SET state = 'submitted', submitted_at = ?, updated_at = ?
                WHERE submission_id = ?
                """,
                (
                    _format_time(now),
                    _format_time(now),
                    submission_id,
                ),
            )
            row = self._select(connection, submission_id)
            self._verify_integrity(row, include_values=True)
        return _submission_from_row(row, include_values=False)

    def supersede(self, submission_id: str, *, user_subject: str, pending_only: bool = False, before_supersede: Callable[[sqlite3.Connection], None] | None = None) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, submission_id)
            if row["user_subject"] != user_subject:
                raise FieldSubmissionAccessDenied("field submission belongs to another user")
            if pending_only and row["state"] not in {"pending", "superseded"}:
                raise FieldSubmissionStateError("field submission is no longer pending")
            if row["state"] not in {"pending", "submitted"}:
                return
            if before_supersede:
                before_supersede(connection)
            now = _format_time(_as_utc(self.clock()))
            connection.execute(
                "UPDATE field_submissions SET state = 'superseded', csrf_hash = NULL, updated_at = ? WHERE submission_id = ?",
                (now, submission_id),
            )
            connection.execute(
                "UPDATE field_submission_presentations SET state = 'expired', updated_at = ? WHERE submission_id = ? AND state = 'active'",
                (now, submission_id),
            )
            connection.execute(
                "UPDATE field_submission_card_sessions SET state = 'expired' WHERE submission_id = ? AND state = 'pending'",
                (submission_id,),
            )

    def consume(
        self,
        submission_id: str,
        *,
        user_subject: str,
        system_id: str,
        session_id: str,
        capability_name: str,
        capability_version: str,
        consume_operation_id: str,
    ) -> dict:
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, submission_id))
            if row["state"] != "submitted":
                raise FieldSubmissionStateError(
                    f"field submission is not submitted: {row['state']}"
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
                raise FieldSubmissionAccessDenied(
                    "field submission is not bound to this user, session, or capability"
                )
            self._verify_integrity(row, include_values=True)
            cursor = connection.execute(
                """
                UPDATE field_submissions
                SET state = 'consumed', consume_operation_id = ?,
                    consumed_at = ?, updated_at = ?
                WHERE submission_id = ? AND state = 'submitted'
                """,
                (
                    consume_operation_id,
                    _format_time(now),
                    _format_time(now),
                    submission_id,
                ),
            )
            if cursor.rowcount != 1:
                raise FieldSubmissionStateError("field submission could not be consumed")
            row = self._select(connection, submission_id)
        return _submission_from_row(row, include_values=True)

    def _select(self, connection: sqlite3.Connection, submission_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM field_submissions WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        if row is None:
            raise FieldSubmissionNotFound(f"field submission not found: {submission_id}")
        return row

    @staticmethod
    def _select_presentation(
        connection: sqlite3.Connection,
        submission_id: str,
        presentation_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM field_submission_presentations
            WHERE submission_id = ? AND presentation_id = ?
            """,
            (submission_id, presentation_id),
        ).fetchone()
        if row is None:
            raise FieldSubmissionNotFound(
                "field submission presentation not found"
            )
        return row

    def _expire_if_needed(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> sqlite3.Row:
        now = _as_utc(self.clock())
        if row["state"] in {"pending", "submitted"} and now >= _parse_time(row["expires_at"]):
            connection.execute(
                """
                UPDATE field_submissions
                SET state = 'expired', csrf_hash = NULL, updated_at = ?
                WHERE submission_id = ?
                """,
                (_format_time(now), row["submission_id"]),
            )
            connection.execute(
                """
                UPDATE field_submission_presentations
                SET state = 'expired', updated_at = ?
                WHERE submission_id = ? AND state = 'active'
                """,
                (_format_time(now), row["submission_id"]),
            )
            connection.execute(
                """
                UPDATE field_submission_card_sessions
                SET state = 'expired'
                WHERE submission_id = ? AND state = 'pending'
                """,
                (row["submission_id"],),
            )
            return self._select(connection, row["submission_id"])
        return row

    @staticmethod
    def _verify_integrity(row: sqlite3.Row, *, include_values: bool) -> None:
        schema_json = str(row["schema_json"])
        if not hmac.compare_digest(str(row["schema_hash"]), _json_hash(schema_json)):
            raise FieldSubmissionIntegrityError("field submission schema integrity check failed")
        if not include_values or row["values_json"] is None:
            return
        values_json = str(row["values_json"])
        if not hmac.compare_digest(str(row["values_hash"] or ""), _json_hash(values_json)):
            raise FieldSubmissionIntegrityError("field submission values integrity check failed")


def _submission_from_row(row: sqlite3.Row, *, include_values: bool) -> dict:
    value = dict(row)
    value["form_schema"] = json.loads(value.pop("schema_json"))
    values_json = value.pop("values_json")
    value.pop("csrf_hash", None)
    if include_values and values_json is not None:
        value["values"] = json.loads(values_json)
    value.pop("values_hash", None)
    return value


def _presentation_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _card_base_url(card_url: str) -> str:
    return card_url.split("/input/", 1)[0].rstrip("/")


def _validate_card_base_url(value: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("field submission card base URL must be http(s)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("field submission card base URL is invalid")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_hash(canonical_json: str) -> str:
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


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


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("field submission clock must return timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)
