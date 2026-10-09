from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Iterator


INTERACTION_SCHEMA_VERSION = "agentbridge.interaction.v1"
INTERACTION_TYPES = {
    "credential",
    "business_input",
    "execution_authorization",
}


class InteractionNotFound(KeyError):
    pass


class InteractionIntegrityError(RuntimeError):
    pass


class InteractionStore:
    """Persistent opaque IDs that project existing trusted-card records."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS interactions (
                    interaction_id TEXT PRIMARY KEY,
                    interaction_type TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    system_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    operation_id TEXT,
                    resource_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    display_json TEXT NOT NULL,
                    resume_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    UNIQUE (interaction_type, resource_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS interactions_subject_created
                ON interactions (user_subject, created_at)
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS immutable_interaction_contract
                BEFORE UPDATE ON interactions
                BEGIN
                    SELECT RAISE(ABORT, 'interaction contract is immutable');
                END
                """
            )

    def register(
        self,
        *,
        interaction_type: str,
        user_subject: str,
        system_id: str,
        session_id: str,
        resource_id: str,
        title: str,
        message: str,
        display: dict[str, Any],
        resume_spec: dict[str, Any],
        created_at: str,
        expires_at: str,
        operation_id: str | None = None,
    ) -> dict:
        if interaction_type not in INTERACTION_TYPES:
            raise ValueError(f"unsupported interaction type: {interaction_type}")
        required = {
            "user_subject": user_subject,
            "system_id": system_id,
            "session_id": session_id,
            "resource_id": resource_id,
            "title": title,
            "message": message,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"interaction is missing: {', '.join(missing)}")
        if not isinstance(display, dict):
            raise TypeError("interaction display must be an object")
        if not isinstance(resume_spec, dict) or not resume_spec.get("kind"):
            raise ValueError("interaction resume specification is required")
        display_json = _canonical_json(display)
        resume_json = _canonical_json(resume_spec)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM interactions
                WHERE interaction_type = ? AND resource_id = ?
                """,
                (interaction_type, resource_id),
            ).fetchone()
            if existing is not None:
                self._verify_existing(
                    existing,
                    user_subject=user_subject,
                    system_id=system_id,
                    session_id=session_id,
                    operation_id=operation_id,
                    title=title,
                    message=message,
                    display_json=display_json,
                    resume_json=resume_json,
                    created_at=created_at,
                    expires_at=expires_at,
                )
                return _interaction_from_row(existing)

            interaction_id = secrets.token_urlsafe(24)
            connection.execute(
                """
                INSERT INTO interactions (
                    interaction_id, interaction_type, user_subject, system_id,
                    session_id, operation_id, resource_id, title, message,
                    display_json, resume_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction_id,
                    interaction_type,
                    user_subject,
                    system_id,
                    session_id,
                    operation_id,
                    resource_id,
                    title,
                    message,
                    display_json,
                    resume_json,
                    created_at,
                    expires_at,
                ),
            )
            row = self._select(connection, interaction_id)
        return _interaction_from_row(row)

    def get(self, interaction_id: str, *, user_subject: str) -> dict:
        with self._connect() as connection:
            row = self._select(connection, interaction_id)
        if row["user_subject"] != user_subject:
            raise InteractionNotFound(f"interaction not found: {interaction_id}")
        return _interaction_from_row(row)

    def find_by_resource(
        self,
        *,
        interaction_type: str,
        resource_id: str,
        user_subject: str,
    ) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM interactions
                WHERE interaction_type = ? AND resource_id = ?
                """,
                (interaction_type, resource_id),
            ).fetchone()
        if row is None or row["user_subject"] != user_subject:
            raise InteractionNotFound(
                f"interaction not found for {interaction_type} resource"
            )
        return _interaction_from_row(row)

    def list(self, *, user_subject: str, limit: int = 100) -> list[dict]:
        if limit < 1 or limit > 500:
            raise ValueError("interaction list limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM interactions
                WHERE user_subject = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_subject, limit),
            ).fetchall()
        return [_interaction_from_row(row) for row in rows]

    def list_all(self, *, limit: int = 100) -> list[dict]:
        if limit < 1 or limit > 500:
            raise ValueError("interaction list limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM interactions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_interaction_from_row(row) for row in rows]

    @staticmethod
    def _verify_existing(
        row: sqlite3.Row,
        *,
        user_subject: str,
        system_id: str,
        session_id: str,
        operation_id: str | None,
        title: str,
        message: str,
        display_json: str,
        resume_json: str,
        created_at: str,
        expires_at: str,
    ) -> None:
        expected = {
            "user_subject": user_subject,
            "system_id": system_id,
            "session_id": session_id,
            "operation_id": operation_id,
            "title": title,
            "message": message,
            "display_json": display_json,
            "resume_json": resume_json,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise InteractionIntegrityError(
                "existing interaction does not match its trusted resource"
            )

    @staticmethod
    def _select(
        connection: sqlite3.Connection,
        interaction_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM interactions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        if row is None:
            raise InteractionNotFound(f"interaction not found: {interaction_id}")
        return row


def build_interaction_envelope(record: dict, resource: dict) -> dict:
    interaction_type = record["interaction_type"]
    state, ready, resume_completed = _project_state(
        interaction_type,
        str(resource["state"]),
    )
    return {
        "schemaVersion": INTERACTION_SCHEMA_VERSION,
        "interactionId": record["interaction_id"],
        "type": interaction_type,
        "state": state,
        "title": record["title"],
        "message": record["message"],
        "operationId": record.get("operation_id"),
        "presentation": {
            "owner": "agentbridge",
            "preferred": "embedded_secure_web_app",
            "fallback": "url",
            "url": resource["card_url"],
            "modelMustNotCollectValues": True,
        },
        "display": record["display"],
        "expiresAt": record["expires_at"],
        "poll": {
            "tool": "agentbridge_interaction_get",
            "recommendedIntervalSeconds": 2,
        },
        "resume": {
            "tool": "agentbridge_interaction_resume",
            "ready": ready,
            "completed": resume_completed,
        },
    }


def _project_state(
    interaction_type: str,
    resource_state: str,
) -> tuple[str, bool, bool]:
    mappings = {
        "credential": {
            "pending": ("pending", False, False),
            "processing": ("processing", False, False),
            "succeeded": ("completed", True, False),
            "failed": ("failed", False, True),
            "expired": ("expired", False, True),
            "superseded": ("superseded", False, True),
        },
        "business_input": {
            "pending": ("pending", False, False),
            "submitted": ("completed", True, False),
            "consumed": ("completed", False, True),
            "expired": ("expired", False, True),
            "superseded": ("superseded", False, True),
        },
        "execution_authorization": {
            "pending": ("pending", False, False),
            "approved": ("completed", True, False),
            "rejected": ("declined", False, True),
            "consumed": ("completed", False, True),
            "expired": ("expired", False, True),
            "superseded": ("superseded", False, True),
        },
    }
    try:
        return mappings[interaction_type][resource_state]
    except KeyError as exc:
        raise InteractionIntegrityError(
            f"unsupported {interaction_type} resource state: {resource_state}"
        ) from exc


def _interaction_from_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["display"] = json.loads(value.pop("display_json"))
    value["resume_spec"] = json.loads(value.pop("resume_json"))
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
