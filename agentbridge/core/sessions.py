from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Iterator
from uuid import uuid4


class SessionPrincipalMismatch(RuntimeError):
    pass


class SessionRegistry:
    def __init__(
        self, db_path: Path | str, profile_root: Path | str, *, maintain_on_startup: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self.profile_root = Path(profile_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if maintain_on_startup:
            self.run_startup_maintenance()

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
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    system_id TEXT NOT NULL,
                    expected_principal_ref TEXT,
                    downstream_principal_ref TEXT,
                    profile_path TEXT NOT NULL,
                    state TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_verified_at TEXT,
                    last_user_activity_at TEXT,
                    last_keepalive_at TEXT,
                    expired_at TEXT,
                    UNIQUE (user_subject, system_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    system_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    previous_state TEXT,
                    new_state TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session_events_session_created
                ON session_events (session_id, created_at DESC)
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "authority_generation" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN authority_generation INTEGER NOT NULL DEFAULT 1")
            for column in (
                "last_user_activity_at",
                "last_keepalive_at",
                "expired_at",
            ):
                if column not in columns:
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")

    def run_startup_maintenance(self) -> None:
        """Backfill legacy activity timestamps without probing downstream sessions."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET last_user_activity_at = updated_at
                WHERE last_user_activity_at IS NULL AND state = 'active'
                """
            )
            connection.execute(
                """
                UPDATE sessions
                SET expired_at = updated_at
                WHERE expired_at IS NULL AND state = 'expired'
                """
            )

    def get_or_create(
        self,
        *,
        user_subject: str,
        system_id: str,
        expected_principal_ref: str | None = None,
    ) -> dict:
        if not user_subject or not system_id:
            raise ValueError("user_subject and system_id are required")
        existing = self.find(user_subject=user_subject, system_id=system_id)
        if existing is not None:
            bound_principal = (existing.get("expected_principal_ref") or "").strip()
            requested_principal = (expected_principal_ref or "").strip()
            if bound_principal and requested_principal and bound_principal != requested_principal:
                message = "session is already bound to a different expected principal"
                self.quarantine(existing["session_id"], message)
                raise SessionPrincipalMismatch(message)
            if requested_principal and not bound_principal:
                existing = self._update_expected(existing["session_id"], expected_principal_ref)
            return existing

        profile_path = self._profile_path(user_subject=user_subject, system_id=system_id)
        now = _utc_now()
        session_id = str(uuid4())
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, user_subject, system_id, expected_principal_ref,
                        profile_path, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'new', ?, ?)
                    """,
                    (
                        session_id,
                        user_subject,
                        system_id,
                        expected_principal_ref,
                        str(profile_path),
                        now,
                        now,
                    ),
                )
                self._insert_event(
                    connection,
                    session_id=session_id,
                    user_subject=user_subject,
                    system_id=system_id,
                    event_type="session_created",
                    source="registry",
                    previous_state=None,
                    new_state="new",
                    reason=None,
                    created_at=now,
                )
            except sqlite3.IntegrityError:
                existing = self.find(user_subject=user_subject, system_id=system_id)
                if existing is None:
                    raise
                return existing
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return _session_from_row(row)

    def ensure_principal_bindings(
        self,
        *,
        user_subject: str,
        system_ids: set[str],
        principal_bindings: dict[str, str] | None = None,
        fallback_principal_ref: str | None = None,
    ) -> dict[str, str]:
        if not user_subject:
            raise ValueError("user_subject is required")
        normalized_system_ids = {
            str(system_id or "").strip()
            for system_id in system_ids
            if str(system_id or "").strip()
        }
        if not normalized_system_ids:
            raise ValueError("at least one system identity binding is required")
        requested = {
            str(system_id or "").strip(): _validate_principal_ref(principal_ref)
            for system_id, principal_ref in (principal_bindings or {}).items()
            if str(system_id or "").strip() and str(principal_ref or "").strip()
        }
        unknown_systems = set(requested) - normalized_system_ids
        if unknown_systems:
            raise ValueError(
                "principal binding contains systems outside the token scopes: "
                + ", ".join(sorted(unknown_systems))
            )
        fallback = (
            _validate_principal_ref(fallback_principal_ref)
            if str(fallback_principal_ref or "").strip()
            else None
        )

        resolved: dict[str, str] = {}
        existing_by_system: dict[str, dict | None] = {}
        for system_id in sorted(normalized_system_ids):
            existing = self.find(user_subject=user_subject, system_id=system_id)
            existing_by_system[system_id] = existing
            bound = str((existing or {}).get("expected_principal_ref") or "").strip()
            candidate = requested.get(system_id)
            if bound and candidate and bound != candidate:
                raise SessionPrincipalMismatch(
                    f"{system_id} is already bound to a different expected principal"
                )
            resolved_principal = bound or candidate or fallback
            if not resolved_principal:
                raise ValueError(
                    f"expected downstream principal is required for new {system_id} binding"
                )
            resolved[system_id] = resolved_principal

        for system_id, principal_ref in resolved.items():
            existing = existing_by_system[system_id]
            if existing is None or not str(
                existing.get("expected_principal_ref") or ""
            ).strip():
                self.get_or_create(
                    user_subject=user_subject,
                    system_id=system_id,
                    expected_principal_ref=principal_ref,
                )
        return resolved

    def find(self, *, user_subject: str, system_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE user_subject = ? AND system_id = ?",
                (user_subject, system_id),
            ).fetchone()
        return _session_from_row(row) if row is not None else None


    def get(self, session_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"session not found: {session_id}")
        return _session_from_row(row)

    def list(
        self,
        *,
        user_subject: str | None = None,
        system_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        if limit < 1 or limit > 1000:
            raise ValueError("session list limit must be between 1 and 1000")
        clauses: list[str] = []
        parameters: list[object] = []
        if user_subject is not None:
            clauses.append("user_subject = ?")
            parameters.append(user_subject)
        if system_id is not None:
            clauses.append("system_id = ?")
            parameters.append(system_id)
        query = "SELECT * FROM sessions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, session_id LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_session_from_row(row) for row in rows]
    def list_active(self, *, system_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM sessions WHERE state = 'active'"
        parameters: tuple[str, ...] = ()
        if system_id is not None:
            query += " AND system_id = ?"
            parameters = (system_id,)
        query += " ORDER BY COALESCE(last_user_activity_at, updated_at), session_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_session_from_row(row) for row in rows]

    def list_events(self, *, session_id: str, limit: int = 50) -> list[dict]:
        if limit < 1 or limit > 500:
            raise ValueError("session event limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM session_events
                WHERE session_id = ?
                ORDER BY created_at DESC, event_id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_event(self, session_id: str) -> dict | None:
        events = self.list_events(session_id=session_id, limit=1)
        return events[0] if events else None

    def record_event(
        self,
        session_id: str,
        *,
        event_type: str,
        source: str,
        reason: str | None = None,
    ) -> dict:
        session = self.get(session_id)
        created_at = _utc_now()
        with self._connect() as connection:
            event_id = self._insert_event(
                connection,
                session_id=session_id,
                user_subject=session["user_subject"],
                system_id=session["system_id"],
                event_type=event_type,
                source=source,
                previous_state=session["state"],
                new_state=session["state"],
                reason=reason,
                created_at=created_at,
            )
            row = connection.execute(
                "SELECT * FROM session_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return dict(row)

    def touch_activity(self, session_id: str) -> dict:
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET updated_at = ?, last_user_activity_at = ?
                WHERE session_id = ? AND state = 'active'
                """,
                (now, now, session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("only an active session can record activity")
        return self.get(session_id)

    def record_keepalive(self, session_id: str) -> dict:
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET last_keepalive_at = ?
                WHERE session_id = ? AND state = 'active'
                """,
                (now, session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("only an active session can record keepalive")
        return self.get(session_id)

    def mark_awaiting_login(
        self,
        session_id: str,
        *,
        source: str = "authentication",
    ) -> dict:
        return self._set_state(
            session_id,
            "awaiting_login",
            last_error=None,
            source=source,
        )

    def mark_expired(
        self,
        session_id: str,
        message: str = "Downstream login expired.",
        *,
        source: str = "runtime",
    ) -> dict:
        return self._set_state(
            session_id,
            "expired",
            last_error=message,
            source=source,
        )

    def quarantine(
        self,
        session_id: str,
        message: str,
        *,
        source: str = "identity_validation",
    ) -> dict:
        return self._set_state(
            session_id,
            "quarantined",
            last_error=message,
            source=source,
        )

    def rebind_expected_principal(
        self,
        session_id: str,
        *,
        expected_principal_ref: str,
        reason: str,
    ) -> dict:
        expected = _validate_principal_ref(expected_principal_ref)
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("reason is required")
        now = _utc_now()
        message = f"Downstream principal binding changed; login required. Reason: {reason}"
        with self._connect() as connection:
            before = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if before is None:
                raise KeyError(f"session not found: {session_id}")
            cursor = connection.execute(
                """
                UPDATE sessions
                SET expected_principal_ref = ?, downstream_principal_ref = NULL,
                    authority_generation = authority_generation + 1,
                    state = 'expired', last_error = ?, updated_at = ?,
                    last_verified_at = NULL, last_user_activity_at = NULL,
                    last_keepalive_at = NULL, expired_at = ?
                WHERE session_id = ?
                """,
                (expected, message, now, now, session_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"session not found: {session_id}")
            self._insert_event(
                connection,
                session_id=session_id,
                user_subject=before["user_subject"],
                system_id=before["system_id"],
                event_type="state_changed",
                source="admin_rebind",
                previous_state=before["state"],
                new_state="expired",
                reason=message,
                created_at=now,
            )
        return self.get(session_id)

    def activate(
        self,
        session_id: str,
        *,
        observed_principal_ref: str | None,
        source: str = "authentication",
    ) -> dict:
        session = self.get(session_id)
        expected = (session.get("expected_principal_ref") or "").strip()
        observed = (observed_principal_ref or "").strip()
        if not expected:
            message = "expected downstream principal is not configured"
            self._set_state(
                session_id,
                "quarantined",
                last_error=message,
                source="identity_validation",
            )
            raise SessionPrincipalMismatch(message)
        if not observed:
            message = "downstream principal could not be verified"
            self._set_state(session_id, "quarantined", last_error=message)
            raise SessionPrincipalMismatch(message)
        if expected and expected != observed:
            message = "observed downstream principal does not match the expected principal"
            self._set_state(
                session_id,
                "quarantined",
                downstream_principal_ref=observed or None,
                last_error=message,
                source="identity_validation",
            )
            raise SessionPrincipalMismatch(message)

        if observed:
            with self._connect() as connection:
                conflicting = connection.execute(
                    """
                    SELECT session_id FROM sessions
                    WHERE system_id = ? AND downstream_principal_ref = ?
                      AND user_subject <> ? AND state = 'active'
                    """,
                    (session["system_id"], observed, session["user_subject"]),
                ).fetchone()
            if conflicting is not None:
                message = "downstream principal is already bound to another active user"
                self._set_state(
                    session_id,
                    "quarantined",
                    downstream_principal_ref=observed,
                    last_error=message,
                    source="identity_validation",
                )
                raise SessionPrincipalMismatch(message)

        return self._set_state(
            session_id,
            "active",
            downstream_principal_ref=observed or None,
            last_error=None,
            verified=True,
            source=source,
        )

    def _update_expected(self, session_id: str, expected_principal_ref: str) -> dict:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET expected_principal_ref = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (expected_principal_ref, now, session_id),
            )
        return self.get(session_id)

    def _set_state(
        self,
        session_id: str,
        state: str,
        *,
        downstream_principal_ref: str | None = None,
        last_error: str | None,
        verified: bool = False,
        source: str = "runtime",
    ) -> dict:
        now = _utc_now()
        with self._connect() as connection:
            before = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if before is None:
                raise KeyError(f"session not found: {session_id}")
            if before["state"] != state:
                connection.execute("UPDATE sessions SET authority_generation=authority_generation+1 WHERE session_id=?", (session_id,))
            cursor = connection.execute(
                """
                UPDATE sessions
                SET state = ?, downstream_principal_ref = COALESCE(?, downstream_principal_ref),
                    last_error = ?, updated_at = ?,
                    last_verified_at = CASE WHEN ? THEN ? ELSE last_verified_at END,
                    last_user_activity_at = CASE
                        WHEN ? THEN ? ELSE last_user_activity_at
                    END,
                    last_keepalive_at = CASE
                        WHEN ? AND state <> 'active' THEN NULL ELSE last_keepalive_at
                    END,
                    expired_at = CASE
                        WHEN ? = 'expired' THEN ?
                        WHEN ? = 'active' THEN NULL
                        ELSE expired_at
                    END
                WHERE session_id = ?
                """,
                (
                    state,
                    downstream_principal_ref,
                    last_error,
                    now,
                    1 if verified else 0,
                    now,
                    1 if verified else 0,
                    now,
                    1 if verified else 0,
                    state,
                    now,
                    state,
                    session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"session not found: {session_id}")
            if before["state"] != state:
                self._insert_event(
                    connection,
                    session_id=session_id,
                    user_subject=before["user_subject"],
                    system_id=before["system_id"],
                    event_type="state_changed",
                    source=source,
                    previous_state=before["state"],
                    new_state=state,
                    reason=last_error,
                    created_at=now,
                )
        return self.get(session_id)

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        user_subject: str,
        system_id: str,
        event_type: str,
        source: str,
        previous_state: str | None,
        new_state: str | None,
        reason: str | None,
        created_at: str,
    ) -> str:
        event_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO session_events (
                event_id, session_id, user_subject, system_id, event_type,
                source, previous_state, new_state, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                user_subject,
                system_id,
                str(event_type or "unknown")[:128],
                str(source or "unknown")[:256],
                previous_state,
                new_state,
                str(reason)[:2000] if reason is not None else None,
                created_at,
            ),
        )
        return event_id

    def _profile_path(self, *, user_subject: str, system_id: str) -> Path:
        safe_system = re.sub(r"[^a-zA-Z0-9_-]", "_", system_id)[:64] or "system"
        user_key = hashlib.sha256(user_subject.encode("utf-8")).hexdigest()[:24]
        return (self.profile_root / safe_system / user_key).resolve()


def _session_from_row(row: sqlite3.Row | None) -> dict:
    if row is None:
        raise KeyError("session not found")
    return dict(row)


def _validate_principal_ref(value: str | None) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 256
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("expected_principal_ref is invalid")
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
