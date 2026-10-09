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

from agentbridge.core.report_exports import (
    ADDRESSBOOK_REPORT_CONTENT_TYPE,
    ADDRESSBOOK_REPORT_DOCUMENT_TYPE,
    SMARTLIGHT_REPORT_CONTENT_TYPE,
    SMARTLIGHT_REPORT_DOCUMENT_TYPE,
)


PREPARED_DOCUMENT_TTL_SECONDS = 1_800
SUPPORTED_DOCUMENT_TYPES = {
    "patent_certificate",
    "software_copyright_certificate",
    SMARTLIGHT_REPORT_DOCUMENT_TYPE,
    ADDRESSBOOK_REPORT_DOCUMENT_TYPE,
}
SUPPORTED_DOCUMENT_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    SMARTLIGHT_REPORT_CONTENT_TYPE,
    ADDRESSBOOK_REPORT_CONTENT_TYPE,
}


class DocumentDownloadNotFound(KeyError):
    pass


class DocumentDownloadStateError(RuntimeError):
    pass


class DocumentDownloadAccessDenied(RuntimeError):
    pass


class DocumentDownloadIntegrityError(RuntimeError):
    pass


class DocumentDownloadStore:
    def __init__(
        self,
        db_path: Path | str,
        *,
        cache_dir: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir = Path(cache_dir or self.db_path.parent / "document-downloads")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS document_downloads (
                    download_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    system_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    document_hash TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    document_type TEXT NOT NULL,
                    display_size TEXT NOT NULL,
                    card_url TEXT NOT NULL,
                    csrf_hash TEXT,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    completed_at TEXT,
                    content_type TEXT,
                    prepared_size INTEGER,
                    prepared_at TEXT,
                    prepared_expires_at TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(document_downloads)"
                ).fetchall()
            }
            for name, sql_type in (
                ("content_type", "TEXT"),
                ("prepared_size", "INTEGER"),
                ("prepared_at", "TEXT"),
                ("prepared_expires_at", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE document_downloads ADD COLUMN {name} {sql_type}"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS document_downloads_subject_state
                ON document_downloads (user_subject, state, created_at)
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS immutable_document_download_contract
                BEFORE UPDATE OF user_subject, system_id, session_id,
                    document_json, document_hash, filename, document_type,
                    display_size, card_url, created_at, expires_at
                ON document_downloads
                BEGIN
                    SELECT RAISE(ABORT, 'document download contract is immutable');
                END
                """
            )

    def create(
        self,
        *,
        user_subject: str,
        system_id: str,
        session_id: str,
        document: dict[str, Any],
        filename: str,
        document_type: str,
        display_size: str,
        card_base_url: str,
        ttl_seconds: int = 600,
    ) -> dict:
        if not all(str(value or "").strip() for value in (user_subject, system_id, session_id)):
            raise ValueError("document download binding is incomplete")
        if not isinstance(document, dict) or not document:
            raise ValueError("document download reference is required")
        filename = _validate_filename(filename)
        document_type = str(document_type or "").strip()
        if document_type not in SUPPORTED_DOCUMENT_TYPES:
            raise ValueError("unsupported document download type")
        if ttl_seconds < 60 or ttl_seconds > 1800:
            raise ValueError("document download TTL must be between 60 and 1800 seconds")
        base_url = _validate_card_base_url(card_base_url)
        document_json = _canonical_json(document)
        now = _as_utc(self.clock())
        expires_at = now + timedelta(seconds=ttl_seconds)
        download_id = secrets.token_urlsafe(32)
        card_url = f"{base_url}/download/{download_id}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO document_downloads (
                    download_id, user_subject, system_id, session_id,
                    document_json, document_hash, filename, document_type,
                    display_size, card_url, state, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    download_id,
                    user_subject,
                    system_id,
                    session_id,
                    document_json,
                    _json_hash(document_json),
                    filename,
                    document_type,
                    str(display_size or "").strip(),
                    card_url,
                    _format_time(now),
                    _format_time(now),
                    _format_time(expires_at),
                ),
            )
            row = self._select(connection, download_id)
        return _record(row, include_document=False)

    def get(self, download_id: str, *, include_document: bool = False) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, download_id))
            self._verify_integrity(row)
        return _record(row, include_document=include_document)

    def issue_csrf(self, download_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, download_id))
            self._verify_integrity(row)
            if row["state"] != "pending":
                raise DocumentDownloadStateError(
                    f"document download is not pending: {row['state']}"
                )
            connection.execute(
                """
                UPDATE document_downloads
                SET csrf_hash = ?, updated_at = ?
                WHERE download_id = ?
                """,
                (_token_hash(token), _format_time(now), download_id),
            )
        return token

    def claim(
        self,
        download_id: str,
        *,
        csrf_token: str,
        csrf_cookie: str,
    ) -> dict:
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, download_id))
            self._verify_integrity(row)
            if row["state"] != "pending":
                raise DocumentDownloadStateError(
                    f"document download is not pending: {row['state']}"
                )
            expected_hash = str(row["csrf_hash"] or "")
            supplied_hash = _token_hash(csrf_token) if csrf_token else ""
            if (
                not expected_hash
                or not csrf_cookie
                or not hmac.compare_digest(csrf_token, csrf_cookie)
                or not hmac.compare_digest(expected_hash, supplied_hash)
            ):
                raise DocumentDownloadAccessDenied(
                    "document download card CSRF validation failed"
                )
            cursor = connection.execute(
                """
                UPDATE document_downloads
                SET state = 'processing', csrf_hash = NULL, updated_at = ?
                WHERE download_id = ? AND state = 'pending'
                """,
                (_format_time(now), download_id),
            )
            if cursor.rowcount != 1:
                raise DocumentDownloadStateError("document download could not be claimed")
            row = self._select(connection, download_id)
        return _record(row, include_document=True)

    def complete(self, download_id: str) -> dict:
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE document_downloads
                SET state = 'completed', completed_at = ?, updated_at = ?
                WHERE download_id = ? AND state = 'processing'
                """,
                (_format_time(now), _format_time(now), download_id),
            )
            if cursor.rowcount != 1:
                raise DocumentDownloadStateError("document download is not processing")
            row = self._select(connection, download_id)
        return _record(row, include_document=False)

    def release(self, download_id: str) -> dict:
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE document_downloads
                SET state = 'pending', csrf_hash = NULL, updated_at = ?
                WHERE download_id = ? AND state = 'processing'
                """,
                (_format_time(now), download_id),
            )
            if cursor.rowcount != 1:
                raise DocumentDownloadStateError("document download is not processing")
            row = self._expire_if_needed(connection, self._select(connection, download_id))
        return _record(row, include_document=False)

    def claim_for_prepare(self, download_id: str, *, user_subject: str) -> dict:
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, download_id))
            self._verify_integrity(row)
            if not hmac.compare_digest(
                str(row["user_subject"]),
                str(user_subject or ""),
            ):
                raise DocumentDownloadAccessDenied(
                    "document download belongs to another user"
                )
            if row["state"] != "pending":
                raise DocumentDownloadStateError(
                    f"document download is not pending: {row['state']}"
                )
            cursor = connection.execute(
                """
                UPDATE document_downloads
                SET state = 'processing', csrf_hash = NULL, updated_at = ?
                WHERE download_id = ? AND state = 'pending'
                """,
                (_format_time(now), download_id),
            )
            if cursor.rowcount != 1:
                raise DocumentDownloadStateError("document download could not be claimed")
            row = self._select(connection, download_id)
        return _record(row, include_document=True)

    def mark_ready(
        self,
        download_id: str,
        *,
        body: bytes,
        content_type: str,
    ) -> dict:
        if not isinstance(body, bytes) or not body:
            raise ValueError("prepared document body is required")
        if content_type not in SUPPORTED_DOCUMENT_CONTENT_TYPES:
            raise ValueError("prepared document content type is unsupported")
        cache_path = self._cache_path(download_id)
        temporary_path = cache_path.with_suffix(".tmp")
        temporary_path.write_bytes(body)
        temporary_path.replace(cache_path)
        now = _as_utc(self.clock())
        prepared_expires_at = now + timedelta(
            seconds=PREPARED_DOCUMENT_TTL_SECONDS
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE document_downloads
                    SET state = 'ready', content_type = ?, prepared_size = ?,
                        prepared_at = ?, prepared_expires_at = ?, updated_at = ?
                    WHERE download_id = ? AND state = 'processing'
                    """,
                    (
                        content_type,
                        len(body),
                        _format_time(now),
                        _format_time(prepared_expires_at),
                        _format_time(now),
                        download_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DocumentDownloadStateError(
                        "document download is not processing"
                    )
                row = self._select(connection, download_id)
        except Exception:
            cache_path.unlink(missing_ok=True)
            raise
        return _record(row, include_document=False)

    def ready_payload(
        self,
        download_id: str,
        *,
        user_subject: str | None = None,
    ) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, download_id))
            self._verify_integrity(row)
            if user_subject is not None and not hmac.compare_digest(
                str(row["user_subject"]),
                str(user_subject or ""),
            ):
                raise DocumentDownloadAccessDenied(
                    "document download belongs to another user"
                )
            if row["state"] != "ready":
                raise DocumentDownloadStateError(
                    f"document download is not ready: {row['state']}"
                )
        self.require_access(_record(row, include_document=True))
        cache_path = self._cache_path(download_id)
        try:
            body = cache_path.read_bytes()
        except FileNotFoundError as exc:
            raise DocumentDownloadStateError(
                "prepared document cache is unavailable"
            ) from exc
        if len(body) != int(row["prepared_size"] or -1):
            raise DocumentDownloadIntegrityError(
                "prepared document size verification failed"
            )
        self.require_access(_record(row, include_document=True))
        return {
            **_record(row, include_document=False),
            "body": body,
        }

    def require_access(self, record: dict) -> None:
        from agentbridge.core.user_grants import UserGrants
        grants = UserGrants(self.db_path)
        subject = record["user_subject"]
        kind = record["document_type"]
        if kind in {"patent_certificate", "software_copyright_certificate"}:
            grants.require(subject, "oa.certificate.read")
            grants.require(subject, "oa.certificate.download")
        elif kind == ADDRESSBOOK_REPORT_DOCUMENT_TYPE:
            grants.require_tool(subject, "oa_addressbook_export", record["document"]["arguments"])
        elif kind == SMARTLIGHT_REPORT_DOCUMENT_TYPE:
            grants.require_tool(subject, "smartlight_report_export", record["document"]["arguments"])
        else:
            raise PermissionError("unknown download permission mapping")

    def _cache_path(self, download_id: str) -> Path:
        normalized = str(download_id or "")
        if not normalized or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in normalized
        ):
            raise DocumentDownloadNotFound(download_id)
        return self.cache_dir / normalized

    def _select(self, connection: sqlite3.Connection, download_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM document_downloads WHERE download_id = ?",
            (str(download_id or ""),),
        ).fetchone()
        if row is None:
            raise DocumentDownloadNotFound(download_id)
        return row

    def _expire_if_needed(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> sqlite3.Row:
        expiry = (
            row["prepared_expires_at"]
            if row["state"] == "ready" and row["prepared_expires_at"]
            else row["expires_at"]
        )
        if row["state"] in {"pending", "processing", "ready"} and _parse_time(
            expiry
        ) <= _as_utc(self.clock()):
            connection.execute(
                """
                UPDATE document_downloads
                SET state = 'expired', csrf_hash = NULL, updated_at = ?
                WHERE download_id = ?
                """,
                (_format_time(_as_utc(self.clock())), row["download_id"]),
            )
            self._cache_path(row["download_id"]).unlink(missing_ok=True)
            return self._select(connection, row["download_id"])
        return row

    @staticmethod
    def _verify_integrity(row: sqlite3.Row) -> None:
        if not hmac.compare_digest(
            str(row["document_hash"]),
            _json_hash(str(row["document_json"])),
        ):
            raise DocumentDownloadIntegrityError(
                "document download reference integrity check failed"
            )


def _record(row: sqlite3.Row, *, include_document: bool) -> dict:
    effective_expiry = (
        row["prepared_expires_at"]
        if row["state"] == "ready" and row["prepared_expires_at"]
        else row["expires_at"]
    )
    result = {
        "download_id": row["download_id"],
        "user_subject": row["user_subject"],
        "system_id": row["system_id"],
        "session_id": row["session_id"],
        "filename": row["filename"],
        "document_type": row["document_type"],
        "display_size": row["display_size"],
        "card_url": row["card_url"],
        "state": row["state"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": effective_expiry,
        "completed_at": row["completed_at"],
        "content_type": row["content_type"],
        "prepared_size": row["prepared_size"],
        "prepared_at": row["prepared_at"],
    }
    if include_document:
        result["document"] = json.loads(row["document_json"])
    return result


def _validate_filename(value: str) -> str:
    filename = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not filename or filename in {".", ".."}:
        raise ValueError("document download filename is invalid")
    if any(ord(character) < 32 for character in filename):
        raise ValueError("document download filename is invalid")
    if Path(filename).suffix.lower() not in {".pdf", ".jpg", ".jpeg", ".png", ".csv"}:
        raise ValueError("document download filename type is unsupported")
    return filename[:240]


def _validate_card_base_url(value: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("document download card base URL must be http(s)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("document download card base URL is invalid")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _json_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_time(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))
