from __future__ import annotations

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
from urllib.parse import urlparse


class ChallengeNotFound(KeyError):
    pass


class ChallengeStateError(RuntimeError):
    pass


class ChallengeAccessDenied(RuntimeError):
    pass


class AuthChallengeStore:
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
                CREATE TABLE IF NOT EXISTS auth_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    challenge_type TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    system_id TEXT NOT NULL,
                    system_name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    expected_principal_ref TEXT,
                    origin TEXT NOT NULL,
                    page_fingerprint TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    card_url TEXT NOT NULL,
                    csrf_hash TEXT,
                    state TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    claimed_at TEXT,
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS auth_challenges_session_state
                ON auth_challenges (session_id, state, created_at)
                """
            )

    def create(
        self,
        *,
        user_subject: str,
        system_id: str,
        session_id: str,
        origin: str,
        page_fingerprint: str,
        nonce: str | None,
        fields: list[dict],
        card_base_url: str,
        system_name: str = "Legacy system",
        expected_principal_ref: str | None = None,
        ttl_seconds: int = 300,
        challenge_type: str = "legacy_form_login",
    ) -> dict:
        challenge, _reused = self._create(
            user_subject=user_subject,
            system_id=system_id,
            session_id=session_id,
            origin=origin,
            page_fingerprint=page_fingerprint,
            nonce=nonce,
            fields=fields,
            card_base_url=card_base_url,
            system_name=system_name,
            expected_principal_ref=expected_principal_ref,
            ttl_seconds=ttl_seconds,
            challenge_type=challenge_type,
            reuse_active=False,
        )
        return challenge

    def create_or_reuse(
        self,
        *,
        user_subject: str,
        system_id: str,
        session_id: str,
        origin: str,
        page_fingerprint: str,
        nonce: str | None,
        fields: list[dict],
        card_base_url: str,
        system_name: str = "Legacy system",
        expected_principal_ref: str | None = None,
        ttl_seconds: int = 300,
        challenge_type: str = "legacy_form_login",
    ) -> tuple[dict, bool]:
        return self._create(
            user_subject=user_subject,
            system_id=system_id,
            session_id=session_id,
            origin=origin,
            page_fingerprint=page_fingerprint,
            nonce=nonce,
            fields=fields,
            card_base_url=card_base_url,
            system_name=system_name,
            expected_principal_ref=expected_principal_ref,
            ttl_seconds=ttl_seconds,
            challenge_type=challenge_type,
            reuse_active=True,
        )

    def _create(
        self,
        *,
        user_subject: str,
        system_id: str,
        session_id: str,
        origin: str,
        page_fingerprint: str,
        nonce: str | None,
        fields: list[dict],
        card_base_url: str,
        system_name: str,
        expected_principal_ref: str | None,
        ttl_seconds: int,
        challenge_type: str,
        reuse_active: bool,
    ) -> tuple[dict, bool]:
        if not user_subject or not system_id or not session_id:
            raise ValueError("challenge user, system, and session are required")
        normalized_origin = _validate_origin(origin)
        normalized_card_base_url = _validate_card_base_url(card_base_url)
        if challenge_type not in {"legacy_form_login", "interactive_browser_login"}:
            raise ValueError("unsupported authentication challenge type")
        normalized_fields = _validate_fields(
            fields,
            allow_empty=challenge_type == "interactive_browser_login",
        )
        fields_json = _canonical_json(normalized_fields)
        if not page_fingerprint:
            raise ValueError("challenge page fingerprint is required")
        if ttl_seconds < 30 or ttl_seconds > 900:
            raise ValueError("challenge TTL must be between 30 and 900 seconds")

        now = _as_utc(self.clock())
        challenge_id = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=ttl_seconds)
        card_url = f"{normalized_card_base_url}/auth/{challenge_id}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_rows = connection.execute(
                """
                SELECT * FROM auth_challenges
                WHERE session_id = ? AND state IN ('pending', 'processing')
                ORDER BY created_at DESC
                """,
                (session_id,),
            ).fetchall()
            active_rows = [
                self._expire_if_needed(connection, row)
                for row in active_rows
            ]
            active_rows = [
                row
                for row in active_rows
                if row["state"] in {"pending", "processing"}
            ]
            reusable = next(
                (
                    row
                    for row in active_rows
                    if _challenge_contract_matches(
                        row,
                        user_subject=user_subject,
                        system_id=system_id,
                        system_name=system_name,
                        expected_principal_ref=expected_principal_ref,
                        origin=normalized_origin,
                        page_fingerprint=page_fingerprint,
                        fields_json=fields_json,
                        card_base_url=normalized_card_base_url,
                        challenge_type=challenge_type,
                    )
                ),
                None,
            )
            if reuse_active and reusable is not None:
                return _challenge_from_row(reusable), True

            processing = next(
                (row for row in active_rows if row["state"] == "processing"),
                None,
            )
            if processing is not None:
                raise ChallengeStateError(
                    "an authentication challenge is already processing"
                )
            connection.execute(
                """
                UPDATE auth_challenges
                SET state = 'superseded', updated_at = ?, completed_at = ?
                WHERE session_id = ? AND state = 'pending'
                """,
                (_format_time(now), _format_time(now), session_id),
            )
            connection.execute(
                """
                INSERT INTO auth_challenges (
                    challenge_id, challenge_type, user_subject, system_id,
                    system_name, session_id, expected_principal_ref, origin,
                    page_fingerprint, nonce, fields_json, card_url, state,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'pending', ?, ?, ?)
                """,
                (
                    challenge_id,
                    challenge_type,
                    user_subject,
                    system_id,
                    system_name,
                    session_id,
                    expected_principal_ref,
                    normalized_origin,
                    page_fingerprint,
                    nonce or secrets.token_urlsafe(24),
                    fields_json,
                    card_url,
                    _format_time(now),
                    _format_time(now),
                    _format_time(expires_at),
                ),
            )
            row = self._select(connection, challenge_id)
        return _challenge_from_row(row), False

    def get(self, challenge_id: str) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, challenge_id)
            row = self._expire_if_needed(connection, row)
        return _challenge_from_row(row)

    def issue_csrf(self, challenge_id: str) -> str:
        csrf_token = secrets.token_urlsafe(32)
        csrf_hash = _token_hash(csrf_token)
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, challenge_id))
            if row["state"] != "pending":
                raise ChallengeStateError(
                    f"authentication challenge is not pending: {row['state']}"
                )
            connection.execute(
                """
                UPDATE auth_challenges
                SET csrf_hash = ?, updated_at = ?
                WHERE challenge_id = ?
                """,
                (csrf_hash, _format_time(now), challenge_id),
            )
        return csrf_token

    def claim(
        self,
        challenge_id: str,
        *,
        csrf_token: str,
        csrf_cookie: str,
    ) -> dict:
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._expire_if_needed(connection, self._select(connection, challenge_id))
            if row["state"] != "pending":
                raise ChallengeStateError(
                    f"authentication challenge is not pending: {row['state']}"
                )
            expected_hash = str(row["csrf_hash"] or "")
            supplied_hash = _token_hash(csrf_token) if csrf_token else ""
            if (
                not expected_hash
                or not csrf_cookie
                or not hmac.compare_digest(csrf_token, csrf_cookie)
                or not hmac.compare_digest(expected_hash, supplied_hash)
            ):
                raise ChallengeAccessDenied("authentication card CSRF validation failed")
            cursor = connection.execute(
                """
                UPDATE auth_challenges
                SET state = 'processing', csrf_hash = NULL,
                    claimed_at = ?, updated_at = ?
                WHERE challenge_id = ? AND state = 'pending'
                """,
                (_format_time(now), _format_time(now), challenge_id),
            )
            if cursor.rowcount != 1:
                raise ChallengeStateError("authentication challenge could not be claimed")
            row = self._select(connection, challenge_id)
        return _challenge_from_row(row)

    def complete(self, challenge_id: str, *, result: dict) -> dict:
        return self._finish(
            challenge_id,
            state="succeeded",
            result=result,
            error_code=None,
            error_message=None,
        )

    def fail(self, challenge_id: str, *, code: str, message: str) -> dict:
        if not code or not message:
            raise ValueError("challenge failure code and message are required")
        return self._finish(
            challenge_id,
            state="failed",
            result=None,
            error_code=code,
            error_message=message,
        )

    def _finish(
        self,
        challenge_id: str,
        *,
        state: str,
        result: dict | None,
        error_code: str | None,
        error_message: str | None,
    ) -> dict:
        now = _as_utc(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, challenge_id)
            if row["state"] != "processing":
                raise ChallengeStateError(
                    f"authentication challenge is not processing: {row['state']}"
                )
            connection.execute(
                """
                UPDATE auth_challenges
                SET state = ?, result_json = ?, error_code = ?, error_message = ?,
                    completed_at = ?, updated_at = ?
                WHERE challenge_id = ?
                """,
                (
                    state,
                    _canonical_json(result) if result is not None else None,
                    error_code,
                    error_message,
                    _format_time(now),
                    _format_time(now),
                    challenge_id,
                ),
            )
            row = self._select(connection, challenge_id)
        return _challenge_from_row(row)

    def _select(self, connection: sqlite3.Connection, challenge_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM auth_challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        if row is None:
            raise ChallengeNotFound(f"authentication challenge not found: {challenge_id}")
        return row

    def _expire_if_needed(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> sqlite3.Row:
        now = _as_utc(self.clock())
        if row["state"] in {"pending", "processing"} and now >= _parse_time(row["expires_at"]):
            connection.execute(
                """
                UPDATE auth_challenges
                SET state = 'expired', csrf_hash = NULL,
                    error_code = 'CHALLENGE_EXPIRED',
                    error_message = 'Authentication challenge expired.',
                    completed_at = ?, updated_at = ?
                WHERE challenge_id = ?
                """,
                (_format_time(now), _format_time(now), row["challenge_id"]),
            )
            return self._select(connection, row["challenge_id"])
        return row


def _challenge_contract_matches(
    row: sqlite3.Row,
    *,
    user_subject: str,
    system_id: str,
    system_name: str,
    expected_principal_ref: str | None,
    origin: str,
    page_fingerprint: str,
    fields_json: str,
    card_base_url: str,
    challenge_type: str,
) -> bool:
    expected_card_parent = f"{card_base_url}/auth"
    actual_card_parent = str(row["card_url"]).rsplit("/", 1)[0]
    return all(
        (
            row["challenge_type"] == challenge_type,
            row["user_subject"] == user_subject,
            row["system_id"] == system_id,
            row["system_name"] == system_name,
            str(row["expected_principal_ref"] or "")
            == str(expected_principal_ref or ""),
            row["origin"] == origin,
            row["page_fingerprint"] == page_fingerprint,
            row["fields_json"] == fields_json,
            actual_card_parent == expected_card_parent,
        )
    )


def _challenge_from_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["fields"] = json.loads(value.pop("fields_json"))
    value["result"] = json.loads(value.pop("result_json")) if value.get("result_json") else None
    value.pop("result_json", None)
    value.pop("csrf_hash", None)
    value["error"] = (
        {"code": value["error_code"], "message": value["error_message"]}
        if value.get("error_code")
        else None
    )
    return value


def _validate_fields(
    fields: list[dict],
    *,
    allow_empty: bool = False,
) -> list[dict]:
    if not isinstance(fields, list) or (not fields and not allow_empty):
        raise ValueError("authentication challenge fields are required")
    normalized: list[dict] = []
    seen: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("authentication field must be an object")
        name = str(field.get("name") or "")
        input_type = str(field.get("input_type") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or name in seen:
            raise ValueError(f"invalid authentication field name: {name}")
        if input_type not in {"text", "password", "otp"}:
            raise ValueError(f"invalid authentication field type: {input_type}")
        seen.add(name)
        normalized.append(
            {
                "name": name,
                "label": str(field.get("label") or name)[:80],
                "input_type": input_type,
                "autocomplete": str(field.get("autocomplete") or "off")[:80],
                "required": bool(field.get("required", True)),
            }
        )
    return normalized


def _validate_origin(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("challenge origin must be http(s)")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("challenge origin must not include a path")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _validate_card_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("authentication card base URL must be http(s)")
    if parsed.query or parsed.fragment:
        raise ValueError("authentication card base URL must not include query or fragment")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("challenge clock must return timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)
