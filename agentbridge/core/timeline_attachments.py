from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Callable, Iterator
from urllib.parse import urlparse


TIMELINE_ATTACHMENT_TTL_SECONDS = 7 * 24 * 60 * 60
_SUPPORTED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class TimelineAttachmentNotFound(KeyError):
    pass


class TimelineAttachmentExpired(RuntimeError):
    pass


class TimelineAttachmentIntegrityError(RuntimeError):
    pass


class TimelineAttachmentStore:
    """User-bound, opaque media storage for cross-end timeline messages."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        cache_dir: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir = Path(
            cache_dir or self.db_path.parent / "timeline-attachments"
        )
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS timeline_attachments (
                    attachment_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    message_key TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    media_url TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    UNIQUE (user_subject, message_key, ordinal)
                );

                CREATE INDEX IF NOT EXISTS timeline_attachments_subject_message
                ON timeline_attachments (user_subject, message_key, ordinal);

                CREATE INDEX IF NOT EXISTS timeline_attachments_state_expiry
                ON timeline_attachments (state, expires_at);
                """
            )

    def create_many(
        self,
        *,
        user_subject: str,
        message_key: str,
        attachments: list[dict[str, Any]],
        media_base_url: str,
        ttl_seconds: int = TIMELINE_ATTACHMENT_TTL_SECONDS,
    ) -> list[dict]:
        created_paths: list[Path] = []
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self._create_many_in_connection(
                    connection,
                    user_subject=user_subject,
                    message_key=message_key,
                    attachments=attachments,
                    media_base_url=media_base_url,
                    ttl_seconds=ttl_seconds,
                    created_paths=created_paths,
                )
        except BaseException:
            self._discard_created_files(created_paths)
            raise

    def _create_many_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        user_subject: str,
        message_key: str,
        attachments: list[dict[str, Any]],
        media_base_url: str,
        ttl_seconds: int = TIMELINE_ATTACHMENT_TTL_SECONDS,
        created_paths: list[Path],
    ) -> list[dict]:
        """Register media in the caller's transaction and track only new files.

        The caller must discard these paths if its outer transaction rolls back.
        A hard process exit can leave unreferenced files for prune_expired().
        """
        if not connection.in_transaction:
            raise RuntimeError("timeline attachments require an active transaction")
        user_subject = _required_text(user_subject, "user_subject", 256)
        message_key = _required_text(message_key, "message_key", 768)
        base_url = _validate_media_base_url(media_base_url)
        if ttl_seconds < 300 or ttl_seconds > 30 * 24 * 60 * 60:
            raise ValueError("timeline attachment TTL is invalid")

        prepared = [
            _prepared_attachment(value, ordinal)
            for ordinal, value in enumerate(attachments)
        ]
        now = _as_utc(self.clock())
        expires_at = now + timedelta(seconds=ttl_seconds)
        existing = connection.execute(
            """
            SELECT * FROM timeline_attachments
            WHERE user_subject = ? AND message_key = ?
            ORDER BY ordinal
            """,
            (user_subject, message_key),
        ).fetchall()
        if existing:
            self._verify_reused(existing, prepared)
            return [_record(row) for row in existing]

        for item in prepared:
            attachment_id = secrets.token_urlsafe(32)
            final_path = self._cache_path(attachment_id)
            temporary_path = final_path.with_suffix(".tmp")
            created_paths.extend((temporary_path, final_path))
            temporary_path.write_bytes(item["body"])
            temporary_path.replace(final_path)
            media_url = f"{base_url}/media/{attachment_id}/file"
            connection.execute(
                """
                INSERT INTO timeline_attachments (
                    attachment_id, user_subject, message_key, ordinal,
                    filename, content_type, byte_size, content_hash,
                    media_url, state, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
                """,
                (
                    attachment_id,
                    user_subject,
                    message_key,
                    item["ordinal"],
                    item["filename"],
                    item["content_type"],
                    len(item["body"]),
                    item["content_hash"],
                    media_url,
                    _format_time(now),
                    _format_time(expires_at),
                ),
            )
        rows = connection.execute(
            """
            SELECT * FROM timeline_attachments
            WHERE user_subject = ? AND message_key = ?
            ORDER BY ordinal
            """,
            (user_subject, message_key),
        ).fetchall()
        return [_record(row) for row in rows]

    @staticmethod
    def _discard_created_files(paths: list[Path]) -> None:
        for path in paths:
            path.unlink(missing_ok=True)

    def ready_payload(self, attachment_id: str) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, attachment_id)
            if row["state"] == "ready" and _parse_time(
                row["expires_at"]
            ) <= _as_utc(self.clock()):
                connection.execute(
                    """
                    UPDATE timeline_attachments SET state = 'expired'
                    WHERE attachment_id = ?
                    """,
                    (attachment_id,),
                )
                self._cache_path(attachment_id).unlink(missing_ok=True)
                raise TimelineAttachmentExpired(attachment_id)
            if row["state"] != "ready":
                raise TimelineAttachmentExpired(attachment_id)

        try:
            body = self._cache_path(attachment_id).read_bytes()
        except FileNotFoundError as exc:
            raise TimelineAttachmentIntegrityError(
                "timeline attachment body is unavailable"
            ) from exc
        if len(body) != int(row["byte_size"]):
            raise TimelineAttachmentIntegrityError(
                "timeline attachment size verification failed"
            )
        if not hmac.compare_digest(
            hashlib.sha256(body).hexdigest(), str(row["content_hash"])
        ):
            raise TimelineAttachmentIntegrityError(
                "timeline attachment hash verification failed"
            )
        return {**_record(row), "body": body}

    def prune_expired(self, *, now: datetime | None = None) -> int:
        cutoff = _format_time(_as_utc(now or self.clock()))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT attachment_id FROM timeline_attachments
                WHERE state = 'ready' AND expires_at <= ?
                """,
                (cutoff,),
            ).fetchall()
            if rows:
                connection.execute(
                    """
                    UPDATE timeline_attachments SET state = 'expired'
                    WHERE state = 'ready' AND expires_at <= ?
                    """,
                    (cutoff,),
                )
            # Attachment creation holds the same database write transaction
            # while writing these files. Holding it here prevents collecting a
            # concurrent creator's files before its rows have committed.
            for path in self.cache_dir.iterdir():
                if not re.fullmatch(r"[A-Za-z0-9_-]{43}(?:\.tmp)?", path.name):
                    continue
                if not path.is_file():
                    continue
                attachment_id = path.name.removesuffix(".tmp")
                referenced = connection.execute(
                    "SELECT 1 FROM timeline_attachments WHERE attachment_id = ?",
                    (attachment_id,),
                ).fetchone()
                if referenced is None:
                    path.unlink(missing_ok=True)
        for row in rows:
            self._cache_path(row["attachment_id"]).unlink(missing_ok=True)
        return len(rows)

    def _verify_reused(
        self,
        rows: list[sqlite3.Row],
        prepared: list[dict],
    ) -> None:
        if len(rows) != len(prepared):
            raise TimelineAttachmentIntegrityError(
                "timeline attachment retry changed the attachment set"
            )
        for row, item in zip(rows, prepared, strict=True):
            if (
                int(row["ordinal"]) != item["ordinal"]
                or row["filename"] != item["filename"]
                or row["content_type"] != item["content_type"]
                or int(row["byte_size"]) != len(item["body"])
                or not hmac.compare_digest(
                    str(row["content_hash"]), item["content_hash"]
                )
            ):
                raise TimelineAttachmentIntegrityError(
                    "timeline attachment retry changed attachment content"
                )

    def _select(
        self,
        connection: sqlite3.Connection,
        attachment_id: str,
    ) -> sqlite3.Row:
        normalized = str(attachment_id or "")
        row = connection.execute(
            "SELECT * FROM timeline_attachments WHERE attachment_id = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            raise TimelineAttachmentNotFound(normalized)
        return row

    def _cache_path(self, attachment_id: str) -> Path:
        normalized = str(attachment_id or "")
        if not normalized or any(
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in normalized
        ):
            raise TimelineAttachmentNotFound(normalized)
        return self.cache_dir / normalized


def public_attachment(record: dict) -> dict:
    return {
        "attachmentId": record["attachment_id"],
        "type": "image",
        "mimeType": record["content_type"],
        "fileName": record["filename"],
        "size": record["byte_size"],
        "mediaUrl": record["media_url"] if record["state"] == "ready" else None,
        "state": record["state"],
        "createdAt": record["created_at"],
        "expiresAt": record["expires_at"],
        "ordinal": record["ordinal"],
    }


def _prepared_attachment(value: dict[str, Any], ordinal: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError("timeline attachment is invalid")
    content_type = str(value.get("mimeType") or "").strip().lower()
    suffix = _SUPPORTED_IMAGE_TYPES.get(content_type)
    if suffix is None:
        raise ValueError("timeline attachment content type is unsupported")
    content = value.get("content")
    if not isinstance(content, str) or not content:
        raise ValueError("timeline attachment content is required")
    import base64
    import binascii

    try:
        body = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("timeline attachment content is invalid") from exc
    if not body:
        raise ValueError("timeline attachment content is empty")
    supplied_name = str(value.get("fileName") or "").replace("\\", "/")
    filename = supplied_name.rsplit("/", 1)[-1].strip()
    if not filename:
        filename = f"image-{ordinal + 1}{suffix}"
    if any(ord(character) < 32 for character in filename):
        raise ValueError("timeline attachment filename is invalid")
    return {
        "ordinal": ordinal,
        "filename": filename[:120],
        "content_type": content_type,
        "body": body,
        "content_hash": hashlib.sha256(body).hexdigest(),
    }


def _record(row: sqlite3.Row) -> dict:
    return {
        "attachment_id": row["attachment_id"],
        "user_subject": row["user_subject"],
        "message_key": row["message_key"],
        "ordinal": int(row["ordinal"]),
        "filename": row["filename"],
        "content_type": row["content_type"],
        "byte_size": int(row["byte_size"]),
        "content_hash": row["content_hash"],
        "media_url": row["media_url"],
        "state": row["state"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


def _validate_media_base_url(value: str) -> str:
    parsed = urlparse(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("timeline attachment base URL must be http(s)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("timeline attachment base URL is invalid")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _required_text(value: Any, name: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} is invalid")
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_time(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))
