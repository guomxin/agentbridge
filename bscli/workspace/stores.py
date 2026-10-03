from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Callable, Iterator
from uuid import uuid4


_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,63}$")
_LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_LINK_CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{8}$")
_PASSWORD_MIN_LENGTH = 12
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_HOST_DISPATCH_ACTIVE_STATES = {
    "queued",
    "waiting_host",
    "dispatching",
    "reconciling_acceptance",
    "accepted",
}
_HOST_DISPATCH_TERMINAL_STATES = {
    "completed",
    "failed",
    "expired",
    "failed_before_accept",
    "acceptance_unknown",
    "result_unknown",
    "canceled",
}


class WorkspaceConflictError(RuntimeError):
    pass


class WorkspaceLinkError(RuntimeError):
    pass


class WorkspaceStore:
    """Persistent accounts, browser sessions, and trusted endpoint linking."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        session_ttl_seconds: int = 30 * 24 * 3600,
        session_idle_seconds: int = 7 * 24 * 3600,
        link_ttl_seconds: int = 10 * 60,
        gateway_grant_ttl_seconds: int = 90,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.session_ttl_seconds = session_ttl_seconds
        self.session_idle_seconds = session_idle_seconds
        self.link_ttl_seconds = link_ttl_seconds
        self.gateway_grant_ttl_seconds = gateway_grant_ttl_seconds
        if session_idle_seconds < 60 or session_ttl_seconds < session_idle_seconds:
            raise ValueError("invalid workspace session lifetime")
        if link_ttl_seconds < 60 or gateway_grant_ttl_seconds < 15:
            raise ValueError("invalid workspace challenge lifetime")
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
                CREATE TABLE IF NOT EXISTS workspace_accounts (
                    account_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    user_subject TEXT NOT NULL UNIQUE,
                    endpoint_id TEXT,
                    endpoint_key TEXT NOT NULL UNIQUE,
                    openclaw_session_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS workspace_sessions (
                    session_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    idle_expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );

                CREATE INDEX IF NOT EXISTS workspace_sessions_account_state
                ON workspace_sessions (account_id, revoked_at, expires_at);

                CREATE TABLE IF NOT EXISTS workspace_link_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    enrollment_hash TEXT NOT NULL UNIQUE,
                    code_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    user_subject TEXT,
                    approver_endpoint_id TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    consumed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS workspace_links_state_expiry
                ON workspace_link_challenges (state, expires_at);

                CREATE TABLE IF NOT EXISTS workspace_gateway_grants (
                    grant_id TEXT PRIMARY KEY,
                    grant_hash TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    endpoint_key TEXT NOT NULL,
                    openclaw_session_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS workspace_gateway_turns (
                    user_subject TEXT NOT NULL,
                    endpoint_key TEXT NOT NULL,
                    openclaw_session_key TEXT NOT NULL,
                    turn_ref TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_subject, endpoint_key)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS workspace_gateway_turns_session
                ON workspace_gateway_turns (user_subject, openclaw_session_key);

                CREATE TABLE IF NOT EXISTS agent_host_dispatches (
                    dispatch_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    agent_host TEXT NOT NULL,
                    host_binding_ref TEXT NOT NULL,
                    origin_endpoint_id TEXT NOT NULL,
                    conversation_ref TEXT NOT NULL,
                    message_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    attachment_refs_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    accepted_run_id TEXT,
                    attempt_count INTEGER NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    request_sent_at TEXT,
                    next_attempt_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    claim_owner TEXT,
                    claim_token TEXT,
                    claim_expires_at TEXT,
                    last_error_code TEXT,
                    had_tool_activity INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    accepted_at TEXT,
                    finished_at TEXT,
                    UNIQUE (user_subject, agent_host, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS agent_host_dispatches_account_state
                ON agent_host_dispatches (
                    account_id, state, next_attempt_at, created_at
                );

                CREATE INDEX IF NOT EXISTS agent_host_dispatches_subject_state
                ON agent_host_dispatches (user_subject, state, created_at);

                CREATE TABLE IF NOT EXISTS agent_host_dispatch_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    dispatch_id TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS agent_host_dispatch_events_dispatch
                ON agent_host_dispatch_events (dispatch_id, sequence);
                """
            )
            dispatch_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(agent_host_dispatches)"
                )
            }
            if "retry_count" not in dispatch_columns:
                connection.execute(
                    "ALTER TABLE agent_host_dispatches "
                    "ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
                )

    def start_link(self) -> dict:
        now = self._now()
        expires = now + timedelta(seconds=self.link_ttl_seconds)
        for _attempt in range(8):
            challenge_id = str(uuid4())
            enrollment_token = f"abwe_{secrets.token_urlsafe(32)}"
            code = "".join(
                secrets.choice(_LINK_CODE_ALPHABET) for _ in range(8)
            )
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO workspace_link_challenges (
                            challenge_id, enrollment_hash, code_hash, state,
                            created_at, expires_at
                        ) VALUES (?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            challenge_id,
                            _sha256(enrollment_token),
                            _sha256(code),
                            _format_time(now),
                            _format_time(expires),
                        ),
                    )
                break
            except sqlite3.IntegrityError:
                continue
        else:
            raise RuntimeError("could not allocate a workspace link code")
        return {
            "challenge_id": challenge_id,
            "enrollment_token": enrollment_token,
            "link_code": code,
            "state": "pending",
            "expires_at": _format_time(expires),
        }

    def link_status(self, enrollment_token: str) -> dict | None:
        now = self._now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM workspace_link_challenges
                WHERE enrollment_hash = ?
                """,
                (_sha256(enrollment_token),),
            ).fetchone()
            if row is None:
                return None
            if row["state"] in {"pending", "confirmed"} and _parse_time(
                row["expires_at"]
            ) <= now:
                connection.execute(
                    """
                    UPDATE workspace_link_challenges
                    SET state = 'expired'
                    WHERE challenge_id = ?
                    """,
                    (row["challenge_id"],),
                )
                return {
                    "challenge_id": row["challenge_id"],
                    "state": "expired",
                    "expires_at": row["expires_at"],
                }
        return _link_from_row(row)

    def confirm_link(
        self,
        *,
        link_code: str,
        user_subject: str,
        approver_endpoint_id: str,
    ) -> dict:
        code = str(link_code or "").strip().upper().replace("-", "")
        if not _LINK_CODE_RE.fullmatch(code):
            raise WorkspaceLinkError("invalid workspace link code")
        user_subject = _required_text(user_subject, "user_subject", 256)
        approver_endpoint_id = _required_text(
            approver_endpoint_id,
            "approver_endpoint_id",
            128,
        )
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM workspace_link_challenges
                WHERE code_hash = ?
                """,
                (_sha256(code),),
            ).fetchone()
            if row is None:
                raise WorkspaceLinkError("workspace link code was not found")
            if row["state"] != "pending":
                raise WorkspaceLinkError(
                    f"workspace link is no longer pending: {row['state']}"
                )
            if _parse_time(row["expires_at"]) <= now:
                connection.execute(
                    """
                    UPDATE workspace_link_challenges
                    SET state = 'expired'
                    WHERE challenge_id = ?
                    """,
                    (row["challenge_id"],),
                )
                raise WorkspaceLinkError("workspace link code has expired")
            existing = connection.execute(
                """
                SELECT account_id FROM workspace_accounts
                WHERE user_subject = ? AND state = 'active'
                """,
                (user_subject,),
            ).fetchone()
            if existing is not None:
                raise WorkspaceConflictError(
                    "this AgentBridge identity already has a workspace account"
                )
            confirmed_at = _format_time(now)
            connection.execute(
                """
                UPDATE workspace_link_challenges
                SET state = 'confirmed', user_subject = ?,
                    approver_endpoint_id = ?, confirmed_at = ?
                WHERE challenge_id = ?
                """,
                (
                    user_subject,
                    approver_endpoint_id,
                    confirmed_at,
                    row["challenge_id"],
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM workspace_link_challenges
                WHERE challenge_id = ?
                """,
                (row["challenge_id"],),
            ).fetchone()
        return _link_from_row(row)

    def create_account(
        self,
        *,
        enrollment_token: str,
        username: str,
        password: str,
    ) -> dict:
        username = _validate_username(username)
        password_hash = _hash_password(password)
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            link = connection.execute(
                """
                SELECT * FROM workspace_link_challenges
                WHERE enrollment_hash = ?
                """,
                (_sha256(enrollment_token),),
            ).fetchone()
            if link is None:
                raise WorkspaceLinkError(
                    "workspace identity link has not been confirmed"
                )
            if link["state"] == "consumed":
                existing = connection.execute(
                    """
                    SELECT * FROM workspace_accounts
                    WHERE user_subject = ? AND state = 'active'
                    """,
                    (link["user_subject"],),
                ).fetchone()
                if (
                    existing is not None
                    and existing["username"].casefold() == username.casefold()
                    and _verify_password(password, existing["password_hash"])
                ):
                    return _account_from_row(existing)
                raise WorkspaceConflictError(
                    "workspace enrollment was already consumed"
                )
            if link["state"] != "confirmed":
                raise WorkspaceLinkError(
                    "workspace identity link has not been confirmed"
                )
            if _parse_time(link["expires_at"]) <= now:
                connection.execute(
                    """
                    UPDATE workspace_link_challenges
                    SET state = 'expired'
                    WHERE challenge_id = ?
                    """,
                    (link["challenge_id"],),
                )
                raise WorkspaceLinkError("workspace identity link has expired")
            account_id = str(uuid4())
            endpoint_key = f"workspace:{account_id}"
            session_key = (
                f"agent:main:agentbridge-workspace:direct:{account_id}"
            )
            timestamp = _format_time(now)
            try:
                connection.execute(
                    """
                    INSERT INTO workspace_accounts (
                        account_id, username, password_hash, user_subject,
                        endpoint_key, openclaw_session_key, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        account_id,
                        username,
                        password_hash,
                        link["user_subject"],
                        endpoint_key,
                        session_key,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkspaceConflictError(
                    "workspace username or identity is already registered"
                ) from exc
            connection.execute(
                """
                UPDATE workspace_link_challenges
                SET state = 'consumed', consumed_at = ?
                WHERE challenge_id = ?
                """,
                (timestamp, link["challenge_id"]),
            )
        return self.get_account(account_id)

    def attach_endpoint(self, *, account_id: str, endpoint_id: str) -> dict:
        now = _format_time(self._now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE workspace_accounts
                SET endpoint_id = ?, updated_at = ?
                WHERE account_id = ? AND state = 'active'
                """,
                (endpoint_id, now, account_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"workspace account not found: {account_id}")
        return self.get_account(account_id)

    def authenticate(self, *, username: str, password: str) -> dict | None:
        username = str(username or "").strip()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM workspace_accounts
                WHERE username = ? COLLATE NOCASE
                """,
                (username,),
            ).fetchone()
        if row is None:
            _verify_password(
                password,
                _hash_password("AgentBridge-dummy-password"),
            )
            return None
        if row["state"] != "active":
            return None
        return (
            _account_from_row(row)
            if _verify_password(password, row["password_hash"])
            else None
        )

    def record_login(self, account_id: str) -> dict:
        now = _format_time(self._now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE workspace_accounts
                SET last_login_at = ?, updated_at = ?
                WHERE account_id = ? AND state = 'active'
                """,
                (now, now, account_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"workspace account not found: {account_id}")
        return self.get_account(account_id)

    def get_account(self, account_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM workspace_accounts
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"workspace account not found: {account_id}")
        return _account_from_row(row)

    def resolve_gateway_session(
        self,
        *,
        user_subject: str,
        session_key: str,
    ) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM workspace_accounts
                WHERE user_subject = ? AND openclaw_session_key = ?
                  AND state = 'active'
                """,
                (user_subject, session_key),
            ).fetchone()
        if row is None:
            raise WorkspaceLinkError(
                "workspace gateway session does not belong to this identity"
            )
        return _account_from_row(row)

    def create_session(self, account_id: str) -> dict:
        account = self.get_account(account_id)
        if account["state"] != "active":
            raise PermissionError("workspace account is disabled")
        now = self._now()
        session_token = f"abws_{secrets.token_urlsafe(32)}"
        csrf_token = secrets.token_urlsafe(32)
        session_id = str(uuid4())
        expires_at = now + timedelta(seconds=self.session_ttl_seconds)
        idle_expires_at = min(
            expires_at,
            now + timedelta(seconds=self.session_idle_seconds),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workspace_sessions (
                    session_id, account_id, token_hash, csrf_hash,
                    created_at, last_seen_at, expires_at, idle_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    account_id,
                    _sha256(session_token),
                    _sha256(csrf_token),
                    _format_time(now),
                    _format_time(now),
                    _format_time(expires_at),
                    _format_time(idle_expires_at),
                ),
            )
        return {
            "session_id": session_id,
            "session_token": session_token,
            "csrf_token": csrf_token,
            "account": account,
            "expires_at": _format_time(expires_at),
            "idle_expires_at": _format_time(idle_expires_at),
        }

    def verify_session(
        self,
        session_token: str | None,
        *,
        csrf_token: str | None = None,
        touch: bool = True,
    ) -> dict | None:
        if not session_token:
            return None
        now = self._now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    a.account_id,
                    a.username,
                    a.user_subject,
                    a.endpoint_id,
                    a.endpoint_key,
                    a.openclaw_session_key,
                    a.state,
                    a.created_at,
                    a.updated_at,
                    a.last_login_at,
                    s.session_id,
                    s.csrf_hash,
                    s.expires_at AS session_expires_at,
                    s.idle_expires_at AS session_idle_expires_at,
                    s.revoked_at
                FROM workspace_sessions AS s
                JOIN workspace_accounts AS a ON a.account_id = s.account_id
                WHERE s.token_hash = ?
                """,
                (_sha256(session_token),),
            ).fetchone()
            if (
                row is None
                or row["revoked_at"] is not None
                or row["state"] != "active"
                or _parse_time(row["session_expires_at"]) <= now
                or _parse_time(row["session_idle_expires_at"]) <= now
            ):
                return None
            if csrf_token is not None and not hmac.compare_digest(
                row["csrf_hash"],
                _sha256(csrf_token),
            ):
                return None
            if touch:
                idle_expires = min(
                    _parse_time(row["session_expires_at"]),
                    now + timedelta(seconds=self.session_idle_seconds),
                )
                connection.execute(
                    """
                    UPDATE workspace_sessions
                    SET last_seen_at = ?, idle_expires_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        _format_time(now),
                        _format_time(idle_expires),
                        row["session_id"],
                    ),
                )
        account = _account_from_row(row)
        return {
            **account,
            "session_id": row["session_id"],
            "session_expires_at": row["session_expires_at"],
            "session_idle_expires_at": row["session_idle_expires_at"],
        }

    def revoke_session(self, session_token: str | None) -> None:
        if not session_token:
            return
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE workspace_sessions
                SET revoked_at = ?
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (_format_time(self._now()), _sha256(session_token)),
            )

    def issue_gateway_grant(self, account_id: str) -> dict:
        account = self.get_account(account_id)
        if not account["endpoint_id"]:
            raise WorkspaceLinkError("workspace endpoint is not registered")
        now = self._now()
        expires = now + timedelta(seconds=self.gateway_grant_ttl_seconds)
        raw_grant = f"abwg_{secrets.token_urlsafe(32)}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workspace_gateway_grants (
                    grant_id, grant_hash, account_id, user_subject,
                    endpoint_key, openclaw_session_key, state,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    str(uuid4()),
                    _sha256(raw_grant),
                    account["account_id"],
                    account["user_subject"],
                    account["endpoint_key"],
                    account["openclaw_session_key"],
                    _format_time(now),
                    _format_time(expires),
                ),
            )
        return {
            "grant": raw_grant,
            "endpoint_key": account["endpoint_key"],
            "session_key": account["openclaw_session_key"],
            "expires_at": _format_time(expires),
        }

    def redeem_gateway_grant(
        self,
        *,
        grant: str,
        user_subject: str,
        endpoint_key: str,
        session_key: str,
        turn_ref: str | None = None,
    ) -> dict:
        normalized_turn_ref = (
            _required_text(turn_ref, "turn_ref", 128)
            if turn_ref is not None
            else None
        )
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM workspace_gateway_grants
                WHERE grant_hash = ?
                """,
                (_sha256(grant),),
            ).fetchone()
            if row is None or row["state"] != "pending":
                raise WorkspaceLinkError("workspace gateway grant is invalid")
            if _parse_time(row["expires_at"]) <= now:
                connection.execute(
                    """
                    UPDATE workspace_gateway_grants
                    SET state = 'expired'
                    WHERE grant_id = ?
                    """,
                    (row["grant_id"],),
                )
                raise WorkspaceLinkError("workspace gateway grant has expired")
            if (
                row["user_subject"] != user_subject
                or row["endpoint_key"] != endpoint_key
                or row["openclaw_session_key"] != session_key
            ):
                raise PermissionError(
                    "workspace gateway grant belongs to another identity"
                )
            consumed_at = _format_time(now)
            connection.execute(
                """
                UPDATE workspace_gateway_grants
                SET state = 'consumed', consumed_at = ?
                WHERE grant_id = ?
                """,
                (consumed_at, row["grant_id"]),
            )
            if normalized_turn_ref is not None:
                connection.execute(
                    """
                    INSERT INTO workspace_gateway_turns (
                        user_subject, endpoint_key, openclaw_session_key,
                        turn_ref, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_subject, endpoint_key) DO UPDATE SET
                        openclaw_session_key = excluded.openclaw_session_key,
                        turn_ref = excluded.turn_ref,
                        updated_at = excluded.updated_at
                    """,
                    (
                        row["user_subject"],
                        row["endpoint_key"],
                        row["openclaw_session_key"],
                        normalized_turn_ref,
                        consumed_at,
                    ),
                )
        return {
            "status": "succeeded",
            "account_id": row["account_id"],
            "endpoint_key": row["endpoint_key"],
            "session_key": row["openclaw_session_key"],
            "turn_ref": normalized_turn_ref,
        }

    def resolve_gateway_turn(
        self,
        *,
        user_subject: str,
        endpoint_key: str,
        session_key: str,
    ) -> dict | None:
        user_subject = _required_text(user_subject, "user_subject", 256)
        endpoint_key = _required_text(endpoint_key, "endpoint_key", 768)
        session_key = _required_text(session_key, "session_key", 1024)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT t.*
                FROM workspace_gateway_turns AS t
                JOIN workspace_accounts AS a
                  ON a.user_subject = t.user_subject
                 AND a.endpoint_key = t.endpoint_key
                 AND a.openclaw_session_key = t.openclaw_session_key
                WHERE t.user_subject = ? AND t.endpoint_key = ?
                  AND t.openclaw_session_key = ? AND a.state = 'active'
                """,
                (user_subject, endpoint_key, session_key),
            ).fetchone()
        if row is None:
            return None
        return {
            "user_subject": row["user_subject"],
            "endpoint_key": row["endpoint_key"],
            "session_key": row["openclaw_session_key"],
            "turn_ref": row["turn_ref"],
            "updated_at": row["updated_at"],
        }

    def create_host_dispatch(
        self,
        *,
        account_id: str,
        user_subject: str,
        agent_host: str,
        host_binding_ref: str,
        origin_endpoint_id: str,
        conversation_ref: str,
        message_key: str,
        payload_hash: str,
        idempotency_key: str,
        attachment_refs: list[str] | None = None,
        deadline_seconds: int = 60,
    ) -> tuple[dict, bool]:
        account_id = _required_text(account_id, "account_id", 128)
        user_subject = _required_text(user_subject, "user_subject", 256)
        agent_host = _required_text(agent_host, "agent_host", 80)
        host_binding_ref = _required_text(
            host_binding_ref,
            "host_binding_ref",
            1024,
        )
        origin_endpoint_id = _required_text(
            origin_endpoint_id,
            "origin_endpoint_id",
            128,
        )
        conversation_ref = _required_text(
            conversation_ref,
            "conversation_ref",
            1024,
        )
        message_key = _required_text(message_key, "message_key", 768)
        payload_hash = _required_text(payload_hash, "payload_hash", 128)
        idempotency_key = _required_text(
            idempotency_key,
            "idempotency_key",
            128,
        )
        refs = [
            _required_text(value, "attachment_ref", 128)
            for value in list(attachment_refs or [])
        ]
        deadline_seconds = min(max(int(deadline_seconds), 5), 600)
        now = self._now()
        now_text = _format_time(now)
        deadline_at = _format_time(
            now + timedelta(seconds=deadline_seconds)
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                """
                SELECT * FROM workspace_accounts
                WHERE account_id = ? AND user_subject = ? AND state = 'active'
                """,
                (account_id, user_subject),
            ).fetchone()
            if account is None:
                raise WorkspaceLinkError(
                    "workspace dispatch identity is not active"
                )
            existing = connection.execute(
                """
                SELECT * FROM agent_host_dispatches
                WHERE user_subject = ? AND agent_host = ?
                  AND idempotency_key = ?
                """,
                (user_subject, agent_host, idempotency_key),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(
                    str(existing["payload_hash"]),
                    payload_hash,
                ):
                    raise WorkspaceConflictError(
                        "IDEMPOTENCY_PAYLOAD_MISMATCH"
                    )
                return _host_dispatch_from_row(existing), True

            placeholders = ",".join("?" for _ in _HOST_DISPATCH_ACTIVE_STATES)
            conversation_count = connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM agent_host_dispatches
                WHERE user_subject = ? AND agent_host = ?
                  AND conversation_ref = ? AND state IN ({placeholders})
                """,
                (
                    user_subject,
                    agent_host,
                    conversation_ref,
                    *sorted(_HOST_DISPATCH_ACTIVE_STATES),
                ),
            ).fetchone()
            if int(conversation_count["count"] or 0) >= 3:
                raise WorkspaceConflictError(
                    "WORKSPACE_HOST_QUEUE_CONVERSATION_LIMIT"
                )
            user_count = connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM agent_host_dispatches
                WHERE user_subject = ? AND state IN ({placeholders})
                """,
                (user_subject, *sorted(_HOST_DISPATCH_ACTIVE_STATES)),
            ).fetchone()
            if int(user_count["count"] or 0) >= 10:
                raise WorkspaceConflictError("WORKSPACE_HOST_QUEUE_USER_LIMIT")

            dispatch_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO agent_host_dispatches (
                    dispatch_id, account_id, user_subject, agent_host,
                    host_binding_ref, origin_endpoint_id, conversation_ref,
                    message_key, payload_hash, idempotency_key,
                    attachment_refs_json, state, attempt_count,
                    retry_count, next_attempt_at, deadline_at,
                    had_tool_activity,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 0,
                          ?, ?, 0, ?, ?)
                """,
                (
                    dispatch_id,
                    account_id,
                    user_subject,
                    agent_host,
                    host_binding_ref,
                    origin_endpoint_id,
                    conversation_ref,
                    message_key,
                    payload_hash,
                    idempotency_key,
                    json.dumps(refs, separators=(",", ":")),
                    now_text,
                    deadline_at,
                    now_text,
                    now_text,
                ),
            )
            self._append_host_dispatch_event(
                connection,
                dispatch_id=dispatch_id,
                event_name="host_dispatch_created",
                payload={
                    "type": "progress",
                    "runId": idempotency_key,
                    "kind": "system",
                    "phase": "queued",
                    "label": "请求已保存，正在连接智能体",
                },
                created_at=now_text,
            )
            row = connection.execute(
                "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return _host_dispatch_from_row(row), False

    def get_host_dispatch(
        self,
        dispatch_id: str,
        *,
        user_subject: str | None = None,
    ) -> dict:
        with self._connect() as connection:
            if user_subject is None:
                row = connection.execute(
                    "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?",
                    (dispatch_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM agent_host_dispatches
                    WHERE dispatch_id = ? AND user_subject = ?
                    """,
                    (dispatch_id, user_subject),
                ).fetchone()
        if row is None:
            raise KeyError(f"host dispatch not found: {dispatch_id}")
        return _host_dispatch_from_row(row)

    def list_host_dispatches(
        self,
        *,
        user_subject: str,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 500)
        with self._connect() as connection:
            if active_only:
                placeholders = ",".join(
                    "?" for _ in _HOST_DISPATCH_ACTIVE_STATES
                )
                rows = connection.execute(
                    f"""
                    SELECT * FROM agent_host_dispatches
                    WHERE user_subject = ? AND state IN ({placeholders})
                    ORDER BY created_at
                    LIMIT ?
                    """,
                    (
                        user_subject,
                        *sorted(_HOST_DISPATCH_ACTIVE_STATES),
                        limit,
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM agent_host_dispatches
                    WHERE user_subject = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (user_subject, limit),
                ).fetchall()
        return [_host_dispatch_from_row(row) for row in rows]

    def list_host_dispatch_events(
        self,
        dispatch_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> list[dict]:
        after_sequence = max(int(after_sequence), 0)
        limit = min(max(int(limit), 1), 1000)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_host_dispatch_events
                WHERE dispatch_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (dispatch_id, after_sequence, limit),
            ).fetchall()
        return [_host_dispatch_event_from_row(row) for row in rows]

    def recover_host_dispatches(self) -> list[str]:
        now = _format_time(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale = connection.execute(
                """
                SELECT * FROM agent_host_dispatches
                WHERE state IN (
                    'queued', 'waiting_host', 'dispatching',
                    'reconciling_acceptance', 'accepted'
                )
                ORDER BY created_at
                """
            ).fetchall()
            for row in stale:
                state = row["state"]
                if state == "dispatching":
                    state = (
                        "reconciling_acceptance"
                        if row["request_sent_at"]
                        else "waiting_host"
                    )
                connection.execute(
                    """
                    UPDATE agent_host_dispatches
                    SET state = ?, claim_owner = NULL, claim_token = NULL,
                        claim_expires_at = NULL, updated_at = ?
                    WHERE dispatch_id = ?
                    """,
                    (state, now, row["dispatch_id"]),
                )
                self._append_host_dispatch_event(
                    connection,
                    dispatch_id=row["dispatch_id"],
                    event_name="host_dispatch_recovered",
                    payload={
                        "type": "progress",
                        "runId": row["accepted_run_id"]
                        or row["idempotency_key"],
                        "kind": "system",
                        "phase": "recovered",
                        "label": "服务已恢复，正在继续原请求",
                    },
                    created_at=now,
                )
        return sorted({str(row["account_id"]) for row in stale})

    def claim_next_host_dispatch(
        self,
        *,
        account_id: str,
        claim_owner: str,
        lease_seconds: int = 360,
    ) -> dict | None:
        now_value = self._now()
        now = _format_time(now_value)
        expires = _format_time(
            now_value + timedelta(seconds=max(int(lease_seconds), 30))
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM agent_host_dispatches
                WHERE account_id = ? AND state IN (
                    'queued', 'waiting_host', 'dispatching',
                    'reconciling_acceptance', 'accepted'
                )
                ORDER BY created_at
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
            if row is None:
                return None
            if (
                row["claim_token"]
                and row["claim_expires_at"]
                and _parse_time(row["claim_expires_at"]) > now_value
            ):
                return None
            if (
                row["state"] in {"waiting_host", "reconciling_acceptance", "accepted"}
                and _parse_time(row["next_attempt_at"]) > now_value
            ):
                return None
            token = str(uuid4())
            next_state = (
                "dispatching"
                if row["state"] in {"queued", "waiting_host"}
                else row["state"]
            )
            connection.execute(
                """
                UPDATE agent_host_dispatches
                SET state = ?, claim_owner = ?, claim_token = ?,
                    claim_expires_at = ?, updated_at = ?
                WHERE dispatch_id = ?
                """,
                (
                    next_state,
                    claim_owner,
                    token,
                    expires,
                    now,
                    row["dispatch_id"],
                ),
            )
            if next_state == "dispatching" and row["state"] != "dispatching":
                self._append_host_dispatch_event(
                    connection,
                    dispatch_id=row["dispatch_id"],
                    event_name="host_dispatching",
                    payload={
                        "type": "progress",
                        "runId": row["idempotency_key"],
                        "kind": "system",
                        "phase": "dispatching",
                        "label": "正在交给智能体处理",
                    },
                    created_at=now,
                )
            claimed = connection.execute(
                "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?",
                (row["dispatch_id"],),
            ).fetchone()
        return _host_dispatch_from_row(claimed)

    def record_host_dispatch_attempt(
        self,
        dispatch_id: str,
        *,
        claim_token: str,
    ) -> dict:
        now = _format_time(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_claimed_host_dispatch(
                connection,
                dispatch_id,
                claim_token,
            )
            connection.execute(
                """
                UPDATE agent_host_dispatches
                SET state = 'dispatching',
                    attempt_count = attempt_count + 1,
                    request_sent_at = ?, updated_at = ?
                WHERE dispatch_id = ? AND claim_token = ?
                """,
                (now, now, dispatch_id, claim_token),
            )
            self._append_host_dispatch_event(
                connection,
                dispatch_id=dispatch_id,
                event_name="host_dispatch_attempted",
                payload={
                    "type": "progress",
                    "runId": row["idempotency_key"],
                    "kind": "system",
                    "phase": "dispatching",
                    "label": "正在交给智能体处理",
                },
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return _host_dispatch_from_row(updated)

    def append_host_dispatch_stream_event(
        self,
        dispatch_id: str,
        *,
        claim_token: str,
        event_name: str,
        payload: dict,
        had_tool_activity: bool = False,
    ) -> dict:
        now = _format_time(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._select_claimed_host_dispatch(
                connection,
                dispatch_id,
                claim_token,
            )
            if had_tool_activity:
                connection.execute(
                    """
                    UPDATE agent_host_dispatches
                    SET had_tool_activity = 1, updated_at = ?
                    WHERE dispatch_id = ? AND claim_token = ?
                    """,
                    (now, dispatch_id, claim_token),
                )
            self._append_host_dispatch_event(
                connection,
                dispatch_id=dispatch_id,
                event_name=event_name,
                payload=payload,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return _host_dispatch_from_row(row)

    def mark_host_dispatch_waiting(
        self,
        dispatch_id: str,
        *,
        claim_token: str,
        error_code: str,
        delay_seconds: float,
    ) -> dict:
        now_value = self._now()
        now = _format_time(now_value)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_claimed_host_dispatch(
                connection,
                dispatch_id,
                claim_token,
            )
            if _parse_time(row["deadline_at"]) <= now_value:
                self._finish_host_dispatch_in_connection(
                    connection,
                    row=row,
                    state="expired",
                    error_code="HOST_ACCEPT_TIMEOUT",
                    event={
                        "type": "chat",
                        "runId": row["idempotency_key"],
                        "state": "error",
                        "text": (
                            "连接暂未恢复，本次请求未进入业务系统，"
                            "请稍后重试。"
                        ),
                        "safeToRetry": True,
                    },
                    now=now,
                )
            else:
                next_attempt = _format_time(
                    min(
                        _parse_time(row["deadline_at"]),
                        now_value
                        + timedelta(seconds=max(float(delay_seconds), 0.1)),
                    )
                )
                connection.execute(
                    """
                    UPDATE agent_host_dispatches
                    SET state = 'waiting_host', retry_count = retry_count + 1,
                        next_attempt_at = ?,
                        claim_owner = NULL, claim_token = NULL,
                        claim_expires_at = NULL, last_error_code = ?,
                        request_sent_at = NULL, updated_at = ?
                    WHERE dispatch_id = ? AND claim_token = ?
                    """,
                    (
                        next_attempt,
                        str(error_code or "HOST_TRANSPORT_UNAVAILABLE")[:128],
                        now,
                        dispatch_id,
                        claim_token,
                    ),
                )
                self._append_host_dispatch_event(
                    connection,
                    dispatch_id=dispatch_id,
                    event_name="host_waiting",
                    payload={
                        "type": "progress",
                        "runId": row["idempotency_key"],
                        "kind": "system",
                        "phase": "waiting_host",
                        "label": (
                            "智能体连接正在恢复，恢复后会自动继续"
                        ),
                    },
                    created_at=now,
                )
            updated = connection.execute(
                "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return _host_dispatch_from_row(updated)

    def mark_host_dispatch_reconciling(
        self,
        dispatch_id: str,
        *,
        claim_token: str,
        error_code: str,
    ) -> dict:
        now = _format_time(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_claimed_host_dispatch(
                connection,
                dispatch_id,
                claim_token,
            )
            connection.execute(
                """
                UPDATE agent_host_dispatches
                SET state = 'reconciling_acceptance', last_error_code = ?,
                    updated_at = ?
                WHERE dispatch_id = ? AND claim_token = ?
                """,
                (str(error_code or "HOST_ACCEPTANCE_UNKNOWN")[:128], now,
                 dispatch_id, claim_token),
            )
            self._append_host_dispatch_event(
                connection,
                dispatch_id=dispatch_id,
                event_name="host_acceptance_reconciling",
                payload={
                    "type": "progress",
                    "runId": row["idempotency_key"],
                    "kind": "system",
                    "phase": "reconciling_acceptance",
                    "label": "正在确认智能体是否已接收",
                },
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return _host_dispatch_from_row(updated)

    def defer_host_dispatch_reconciliation(
        self,
        dispatch_id: str,
        *,
        claim_token: str,
        error_code: str,
        delay_seconds: float,
    ) -> dict:
        now_value = self._now()
        now = _format_time(now_value)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_claimed_host_dispatch(
                connection,
                dispatch_id,
                claim_token,
            )
            if _parse_time(row["deadline_at"]) <= now_value:
                self._finish_host_dispatch_in_connection(
                    connection,
                    row=row,
                    state="acceptance_unknown",
                    error_code="HOST_ACCEPTANCE_UNKNOWN",
                    event={
                        "type": "chat",
                        "runId": row["idempotency_key"],
                        "state": "error",
                        "text": (
                            "无法确认智能体是否已接收，已停止重发，"
                            "请等待系统核对。"
                        ),
                        "safeToRetry": False,
                    },
                    now=now,
                )
            else:
                next_attempt = _format_time(
                    min(
                        _parse_time(row["deadline_at"]),
                        now_value
                        + timedelta(seconds=max(float(delay_seconds), 0.1)),
                    )
                )
                connection.execute(
                    """
                    UPDATE agent_host_dispatches
                    SET state = 'reconciling_acceptance',
                        next_attempt_at = ?, claim_owner = NULL,
                        claim_token = NULL, claim_expires_at = NULL,
                        last_error_code = ?, updated_at = ?
                    WHERE dispatch_id = ? AND claim_token = ?
                    """,
                    (
                        next_attempt,
                        str(error_code or "HOST_ACCEPTANCE_UNKNOWN")[:128],
                        now,
                        dispatch_id,
                        claim_token,
                    ),
                )
                self._append_host_dispatch_event(
                    connection,
                    dispatch_id=dispatch_id,
                    event_name="host_acceptance_reconciling",
                    payload={
                        "type": "progress",
                        "runId": row["idempotency_key"],
                        "kind": "system",
                        "phase": "reconciling_acceptance",
                        "label": "正在确认智能体是否已接收",
                    },
                    created_at=now,
                )
            updated = connection.execute(
                "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return _host_dispatch_from_row(updated)

    def mark_host_dispatch_accepted(
        self,
        dispatch_id: str,
        *,
        claim_token: str,
        run_id: str,
        status: str = "started",
        recovered_from_run_id: str | None = None,
    ) -> dict:
        run_id = _required_text(run_id, "run_id", 256)
        now = _format_time(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_claimed_host_dispatch(
                connection,
                dispatch_id,
                claim_token,
            )
            if row["accepted_run_id"] and row["accepted_run_id"] != run_id:
                # The gateway only emits this link after confirmed abort and
                # authoritative zero-tool evidence for the startup attempt.
                base = row["idempotency_key"]
                suffix = "-recovery-1-" + hashlib.sha256(f"{base}:1".encode()).hexdigest()[:12]
                expected_recovery = base[:128 - len(suffix)] + suffix
                if (recovered_from_run_id != row["accepted_run_id"]
                    or row["had_tool_activity"]
                    or run_id != expected_recovery):
                    raise WorkspaceConflictError("HOST_ACCEPT_DUPLICATE_RUN_VIOLATION")
            connection.execute(
                """
                UPDATE agent_host_dispatches
                SET state = 'accepted', accepted_run_id = ?, accepted_at = ?,
                    last_error_code = NULL, updated_at = ?
                WHERE dispatch_id = ? AND claim_token = ?
                """,
                (run_id, now, now, dispatch_id, claim_token),
            )
            self._append_host_dispatch_event(
                connection,
                dispatch_id=dispatch_id,
                event_name="host_accepted",
                payload={
                    "type": "accepted",
                    "runId": run_id,
                    "status": status,
                    **({"recoveredFromRunId": recovered_from_run_id} if recovered_from_run_id else {}),
                },
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return _host_dispatch_from_row(updated)

    def defer_host_dispatch_result(
        self, dispatch_id: str, *, claim_token: str,
        had_tool_activity: bool = False, delay_seconds: float = 10,
    ) -> dict:
        """Release the observation lease without making an accepted run resendable."""
        now_value = self._now()
        now = _format_time(now_value)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_claimed_host_dispatch(connection, dispatch_id, claim_token)
            if row["state"] != "accepted":
                raise WorkspaceConflictError("HOST_DISPATCH_NOT_ACCEPTED")
            connection.execute(
                """UPDATE agent_host_dispatches
                   SET next_attempt_at = ?, claim_owner = NULL, claim_token = NULL,
                       claim_expires_at = NULL, updated_at = ?,
                       last_error_code = 'HOST_RUN_RESULT_PENDING',
                       had_tool_activity = MAX(had_tool_activity, ?)
                   WHERE dispatch_id = ? AND claim_token = ?""",
                (_format_time(now_value + timedelta(seconds=max(delay_seconds, 0.1))),
                 now, int(had_tool_activity), dispatch_id, claim_token),
            )
            if row["last_error_code"] != "HOST_RUN_RESULT_PENDING":
                self._append_host_dispatch_event(
                    connection, dispatch_id=dispatch_id, event_name="host_result_reconciling",
                    payload={"type": "progress", "runId": row["accepted_run_id"],
                             "kind": "system", "phase": "reconciling_result",
                             "label": "请求已接收，正在恢复结果；不会重复执行"},
                    created_at=now,
                )
            updated = connection.execute(
                "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
        return _host_dispatch_from_row(updated)

    def host_dispatch_result_deadline_passed(self, dispatch: dict) -> bool:
        # The pre-accept deadline must not truncate a running host request.
        accepted = _parse_time(dispatch["accepted_at"] or dispatch["created_at"])
        return self._now() >= accepted + timedelta(minutes=30)

    def finish_host_dispatch(
        self,
        dispatch_id: str,
        *,
        claim_token: str,
        state: str,
        event: dict,
        error_code: str | None = None,
    ) -> dict:
        if state not in _HOST_DISPATCH_TERMINAL_STATES:
            raise ValueError("host dispatch terminal state is invalid")
        now = _format_time(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_claimed_host_dispatch(
                connection,
                dispatch_id,
                claim_token,
            )
            self._finish_host_dispatch_in_connection(
                connection,
                row=row,
                state=state,
                error_code=error_code,
                event=event,
                now=now,
            )
            # Transactional outbox: a crash after completion cannot lose opted-in
            # method capture. Standalone stores without the workbench stay compatible.
            if state == 'completed' and event.get('text') and connection.execute("SELECT 1 FROM sqlite_master WHERE name='skill_auto_capture'").fetchone():
                from bscli.core.skill_workbench import SkillWorkbench
                if SkillWorkbench._auto_allowed(connection, row['user_subject'], 'workspace:' + row['account_id']):
                    connection.execute('INSERT OR IGNORE INTO skill_auto_capture VALUES (?,?,?,?,?)',
                        (dispatch_id, row['user_subject'], str(event['text'])[:12000], 'queued', now))
            updated = connection.execute(
                "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return _host_dispatch_from_row(updated)

    def host_dispatch_work_pending(self, account_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM agent_host_dispatches
                WHERE account_id = ? AND state IN (
                    'queued', 'waiting_host', 'dispatching',
                    'reconciling_acceptance', 'accepted'
                )
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        return row is not None

    def host_dispatch_deadline_passed(self, dispatch: dict) -> bool:
        return _parse_time(str(dispatch["deadline_at"])) <= self._now()

    def cancel_host_dispatch(
        self,
        dispatch_id: str,
        *,
        user_subject: str,
    ) -> dict:
        now = _format_time(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM agent_host_dispatches
                WHERE dispatch_id = ? AND user_subject = ?
                """,
                (dispatch_id, user_subject),
            ).fetchone()
            if row is None:
                raise KeyError(f"host dispatch not found: {dispatch_id}")
            if row["state"] == "canceled":
                return _host_dispatch_from_row(row)
            if row["state"] not in {"queued", "waiting_host"}:
                raise WorkspaceConflictError(
                    "HOST_DISPATCH_CANCEL_NOT_ALLOWED"
                )
            self._finish_host_dispatch_in_connection(
                connection,
                row=row,
                state="canceled",
                error_code="HOST_DISPATCH_CANCELED",
                event={
                    "type": "chat",
                    "runId": row["idempotency_key"],
                    "state": "aborted",
                    "text": "已取消等待，本次请求未进入业务系统。",
                    "safeToRetry": True,
                },
                now=now,
            )
            updated = connection.execute(
                "SELECT * FROM agent_host_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return _host_dispatch_from_row(updated)

    def _select_claimed_host_dispatch(
        self,
        connection: sqlite3.Connection,
        dispatch_id: str,
        claim_token: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM agent_host_dispatches
            WHERE dispatch_id = ? AND claim_token = ?
            """,
            (dispatch_id, claim_token),
        ).fetchone()
        if row is None:
            raise WorkspaceConflictError("HOST_DISPATCH_CLAIM_LOST")
        return row

    def _finish_host_dispatch_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        state: str,
        error_code: str | None,
        event: dict,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE agent_host_dispatches
            SET state = ?, last_error_code = ?, finished_at = ?,
                claim_owner = NULL, claim_token = NULL,
                claim_expires_at = NULL, updated_at = ?
            WHERE dispatch_id = ?
            """,
            (
                state,
                str(error_code)[:128] if error_code else None,
                now,
                now,
                row["dispatch_id"],
            ),
        )
        self._append_host_dispatch_event(
            connection,
            dispatch_id=row["dispatch_id"],
            event_name=f"host_dispatch_{state}",
            payload=event,
            created_at=now,
        )

    def _append_host_dispatch_event(
        self,
        connection: sqlite3.Connection,
        *,
        dispatch_id: str,
        event_name: str,
        payload: dict,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO agent_host_dispatch_events (
                event_id, dispatch_id, event_name, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                dispatch_id,
                _required_text(event_name, "event_name", 128),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                created_at,
            ),
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _account_from_row(row: sqlite3.Row) -> dict:
    return {
        "account_id": row["account_id"],
        "username": row["username"],
        "user_subject": row["user_subject"],
        "endpoint_id": row["endpoint_id"],
        "endpoint_key": row["endpoint_key"],
        "openclaw_session_key": row["openclaw_session_key"],
        "state": row["state"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }


def _link_from_row(row: sqlite3.Row) -> dict:
    return {
        "challenge_id": row["challenge_id"],
        "state": row["state"],
        "user_subject": row["user_subject"],
        "approver_endpoint_id": row["approver_endpoint_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "confirmed_at": row["confirmed_at"],
        "consumed_at": row["consumed_at"],
    }


def _host_dispatch_from_row(row: sqlite3.Row) -> dict:
    try:
        attachment_refs = json.loads(row["attachment_refs_json"])
    except (json.JSONDecodeError, TypeError):
        attachment_refs = []
    return {
        "dispatch_id": row["dispatch_id"],
        "account_id": row["account_id"],
        "user_subject": row["user_subject"],
        "agent_host": row["agent_host"],
        "host_binding_ref": row["host_binding_ref"],
        "origin_endpoint_id": row["origin_endpoint_id"],
        "conversation_ref": row["conversation_ref"],
        "message_key": row["message_key"],
        "payload_hash": row["payload_hash"],
        "idempotency_key": row["idempotency_key"],
        "attachment_refs": (
            attachment_refs if isinstance(attachment_refs, list) else []
        ),
        "state": row["state"],
        "accepted_run_id": row["accepted_run_id"],
        "attempt_count": int(row["attempt_count"]),
        "retry_count": int(row["retry_count"]),
        "request_sent_at": row["request_sent_at"],
        "next_attempt_at": row["next_attempt_at"],
        "deadline_at": row["deadline_at"],
        "claim_owner": row["claim_owner"],
        "claim_token": row["claim_token"],
        "claim_expires_at": row["claim_expires_at"],
        "last_error_code": row["last_error_code"],
        "had_tool_activity": bool(row["had_tool_activity"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "accepted_at": row["accepted_at"],
        "finished_at": row["finished_at"],
    }


def _host_dispatch_event_from_row(row: sqlite3.Row) -> dict:
    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return {
        "sequence": int(row["sequence"]),
        "event_id": row["event_id"],
        "dispatch_id": row["dispatch_id"],
        "event_name": row["event_name"],
        "payload": payload if isinstance(payload, dict) else {},
        "created_at": row["created_at"],
    }


def _validate_username(value: str) -> str:
    normalized = str(value or "").strip()
    if not _USERNAME_RE.fullmatch(normalized):
        raise ValueError(
            "username must start with a letter and contain 3-64 letters, digits, dots, underscores, or hyphens"
        )
    return normalized


def _hash_password(password: str) -> str:
    password = str(password or "")
    if len(password) < _PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"password must contain at least {_PASSWORD_MIN_LENGTH} characters"
        )
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
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(
            digest,
            base64.urlsafe_b64decode(expected),
        )
    except (ValueError, TypeError):
        return False


def _required_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, (str, int)):
        raise ValueError(f"{name} is required")
    normalized = str(value).strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} is invalid")
    return normalized


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
