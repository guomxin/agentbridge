from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4


class OperationConflictError(RuntimeError):
    pass


class OperationStore:
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
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    capability_name TEXT NOT NULL,
                    capability_version TEXT NOT NULL,
                    input_summary_json TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    idempotency_key TEXT,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    next_action_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS operations_idempotency
                ON operations (
                    user_subject,
                    capability_name,
                    capability_version,
                    idempotency_key
                )
                WHERE idempotency_key IS NOT NULL
                """
            )

    def create(
        self,
        *,
        user_subject: str,
        capability_name: str,
        capability_version: str,
        input_summary: dict,
        input_identity: dict | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> tuple[dict, bool]:
        if not user_subject:
            raise ValueError("user_subject is required")
        canonical_summary = _canonical_json(input_summary)
        canonical_identity = _canonical_json(
            input_identity if input_identity is not None else input_summary
        )
        input_hash = hashlib.sha256(canonical_identity.encode("utf-8")).hexdigest()
        stripped_key = idempotency_key.strip() if idempotency_key else ""
        normalized_key = stripped_key or None

        with self._connect() as connection:
            existing = self._find_idempotent(
                connection,
                user_subject=user_subject,
                capability_name=capability_name,
                capability_version=capability_version,
                idempotency_key=normalized_key,
            )
            if existing is not None:
                return self._reuse(existing, input_hash)

            now = _utc_now()
            operation_id = str(uuid4())
            try:
                connection.execute(
                    """
                    INSERT INTO operations (
                        operation_id, request_id, user_subject, capability_name,
                        capability_version, input_summary_json, input_hash,
                        idempotency_key, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        operation_id,
                        request_id or str(uuid4()),
                        user_subject,
                        capability_name,
                        capability_version,
                        canonical_summary,
                        input_hash,
                        normalized_key,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self._find_idempotent(
                    connection,
                    user_subject=user_subject,
                    capability_name=capability_name,
                    capability_version=capability_version,
                    idempotency_key=normalized_key,
                )
                if existing is None:
                    raise
                return self._reuse(existing, input_hash)
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return _operation_from_row(row), False

    def _find_idempotent(
        self,
        connection: sqlite3.Connection,
        *,
        user_subject: str,
        capability_name: str,
        capability_version: str,
        idempotency_key: str | None,
    ) -> sqlite3.Row | None:
        if idempotency_key is None:
            return None
        return connection.execute(
            """
            SELECT * FROM operations
            WHERE user_subject = ? AND capability_name = ?
              AND capability_version = ? AND idempotency_key = ?
            """,
            (user_subject, capability_name, capability_version, idempotency_key),
        ).fetchone()

    @staticmethod
    def _reuse(row: sqlite3.Row, input_hash: str) -> tuple[dict, bool]:
        operation = _operation_from_row(row)
        if operation["input_hash"] != input_hash:
            raise OperationConflictError(
                "idempotency key was already used with different capability input"
            )
        return operation, True

    def mark_running(self, operation_id: str) -> dict:
        return self._update(operation_id, status="running")

    def mark_succeeded(self, operation_id: str, result: Any) -> dict:
        return self._update(
            operation_id,
            status="succeeded",
            result_json=_canonical_json(result),
            finished=True,
        )

    def mark_failed(self, operation_id: str, *, code: str, message: str) -> dict:
        return self._update(
            operation_id,
            status="failed",
            error_code=code,
            error_message=message,
            finished=True,
        )

    def mark_unknown(self, operation_id: str, *, code: str, message: str) -> dict:
        return self._update(
            operation_id,
            status="unknown",
            error_code=code,
            error_message=message,
            finished=True,
        )

    def mark_requires_user_action(
        self,
        operation_id: str,
        *,
        code: str,
        message: str,
        next_action: dict,
    ) -> dict:
        return self._update(
            operation_id,
            status="requires_user_action",
            error_code=code,
            error_message=message,
            next_action_json=_canonical_json(next_action),
            finished=True,
        )

    def _update(
        self,
        operation_id: str,
        *,
        status: str,
        result_json: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        next_action_json: str | None = None,
        finished: bool = False,
    ) -> dict:
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE operations
                SET status = ?, result_json = ?, error_code = ?, error_message = ?,
                    next_action_json = ?, updated_at = ?, finished_at = ?
                WHERE operation_id = ?
                """,
                (
                    status,
                    result_json,
                    error_code,
                    error_message,
                    next_action_json,
                    now,
                    now if finished else None,
                    operation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"operation not found: {operation_id}")
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return _operation_from_row(row)

    def get(self, operation_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"operation not found: {operation_id}")
        return _operation_from_row(row)

    def find_idempotent(
        self,
        *,
        user_subject: str,
        capability_name: str,
        capability_version: str,
        idempotency_key: str,
    ) -> dict | None:
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key:
            return None
        with self._connect() as connection:
            row = self._find_idempotent(
                connection,
                user_subject=user_subject,
                capability_name=capability_name,
                capability_version=capability_version,
                idempotency_key=normalized_key,
            )
        return _operation_from_row(row) if row is not None else None

    def list(self, *, user_subject: str | None = None, limit: int = 100) -> list[dict]:
        limit = min(max(limit, 1), 1000)
        query = "SELECT * FROM operations"
        parameters: list[Any] = []
        if user_subject:
            query += " WHERE user_subject = ?"
            parameters.append(user_subject)
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_operation_from_row(row) for row in rows]


def _operation_from_row(row: sqlite3.Row | None) -> dict:
    if row is None:
        raise KeyError("operation not found")
    value = dict(row)
    value["input_summary"] = _decode_json(value.pop("input_summary_json"))
    value["result"] = _decode_json(value.pop("result_json"))
    value["next_action"] = _decode_json(value.pop("next_action_json"))
    if value.get("error_code") or value.get("error_message"):
        value["error"] = {
            "code": value.get("error_code") or "OPERATION_ERROR",
            "message": value.get("error_message") or "Operation failed.",
        }
    else:
        value["error"] = None
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
