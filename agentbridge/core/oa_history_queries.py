"""Owned, expiring OA history progress and idempotent continuation receipts.

The service must authorize every request and hold the existing OA session lock
across resolve, downstream reading and advance. This store does not execute OA
requests or grant access. Cached receipts also require the exact original session.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Callable, Iterator


QUERY_TTL_SECONDS = 1800
MAX_SOURCE_ROWS = 10000
MAX_DETAIL_ITEMS = 500
MAX_EXECUTION_SECONDS = 300
MAX_RESULT_CHARS = 10000
MAX_RESULT_PAGE_SIZE = 50
_CURSOR_LENGTH = 47
_STATES = {"scan", "details", "complete", "partial"}
_TERMINAL = {"complete", "partial"}
_STAGES = {"scan", "details", "results"}


class OaHistoryQueryError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _fail(code: str, message: str, *, status: int = 409) -> None:
    raise OaHistoryQueryError(code, message, status=status)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        _fail("OA_HISTORY_QUERY_INVALID", "查询状态必须是有效 JSON。")


def response_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-16-le")) // 2


def budget_remaining(record: dict[str, Any]) -> dict[str, int | float]:
    return {"source_rows": max(0, MAX_SOURCE_ROWS - record["scanned_count"]),
            "detail_items": max(0, MAX_DETAIL_ITEMS - record["detail_count"]),
            "execution_seconds": max(0.0, MAX_EXECUTION_SECONDS - record["execution_seconds"])}


def result_page(
    rows: list[dict[str, Any]], *, offset: int = 0, limit: int = MAX_RESULT_PAGE_SIZE,
    max_chars: int = MAX_RESULT_CHARS, envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return complete rows within the final response budget, reserving cursors.

    Pass the public response metadata in envelope. The returned next_offset is
    an internal delivery offset; only the store-issued opaque cursor resumes it.
    A row that does not fit is never dropped or field-truncated.
    """
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or type(offset) is not int or not 0 <= offset <= len(rows)
            or type(limit) is not int or not 1 <= limit <= MAX_RESULT_PAGE_SIZE
            or type(max_chars) is not int or not 512 <= max_chars <= MAX_RESULT_CHARS
            or (envelope is not None and not isinstance(envelope, dict))):
        _fail("OA_HISTORY_RESULT_ARGUMENTS_INVALID", "结果分页参数不合法。", status=400)
    result = {**deepcopy(envelope or {}), "items": [], "next_offset": None, "has_more": False,
              "returned_count": 0, "total": len(rows)}

    def fits() -> bool:
        measured = {**result, "next_cursor": "x" * _CURSOR_LENGTH if result["has_more"] else None,
                    "replay_cursor": "x" * _CURSOR_LENGTH}
        return response_chars(measured) <= max_chars

    if not fits():
        _fail("OA_HISTORY_RESULT_ENVELOPE_TOO_LARGE", "结果说明超出单次交付预算。")
    for index in range(offset, min(len(rows), offset + limit)):
        result["items"].append(deepcopy(rows[index]))
        result.update(returned_count=len(result["items"]), has_more=index + 1 < len(rows),
                      next_offset=index + 1 if index + 1 < len(rows) else None)
        if not fits():
            result["items"].pop()
            if not result["items"]:
                _fail("OA_HISTORY_RESULT_ROW_TOO_LARGE", "单条结果超出交付预算，未跳过或截断该条结果。")
            result.update(returned_count=len(result["items"]), has_more=True, next_offset=index)
            break
    return result


class OaHistoryQueryStore:
    def __init__(self, db_path: Path | str, *, clock: Callable[[], datetime] | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize()

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("history query clock must include its timezone")
        return now.astimezone(timezone.utc)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
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
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS oa_history_queries (
                    query_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    principal_ref TEXT NOT NULL,
                    query_json TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('scan','details','complete','partial')),
                    state_json TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version > 0),
                    scanned_count INTEGER NOT NULL DEFAULT 0 CHECK(scanned_count >= 0),
                    detail_count INTEGER NOT NULL DEFAULT 0 CHECK(detail_count >= 0),
                    execution_seconds REAL NOT NULL DEFAULT 0 CHECK(execution_seconds >= 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS oa_history_queries_owner
                    ON oa_history_queries(user_subject, created_at);
                CREATE TABLE IF NOT EXISTS oa_history_query_receipts (
                    cursor_hash TEXT PRIMARY KEY,
                    query_id TEXT NOT NULL REFERENCES oa_history_queries(query_id),
                    version INTEGER NOT NULL CHECK(version > 0),
                    stage TEXT NOT NULL CHECK(stage IN ('scan','details','results')),
                    delivery_offset INTEGER NOT NULL CHECK(delivery_offset >= 0),
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS oa_history_query_receipts_query
                    ON oa_history_query_receipts(query_id, version);
                CREATE TRIGGER IF NOT EXISTS immutable_oa_history_query
                BEFORE UPDATE OF query_id,user_subject,session_id,principal_ref,
                    query_json,query_hash,created_at,expires_at ON oa_history_queries
                BEGIN SELECT RAISE(ABORT, 'history query binding is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_oa_history_terminal_results
                BEFORE UPDATE ON oa_history_queries
                WHEN OLD.state IN ('complete','partial') AND (
                    NEW.state != OLD.state OR NEW.state_json != OLD.state_json OR
                    NEW.scanned_count != OLD.scanned_count OR NEW.detail_count != OLD.detail_count OR
                    NEW.execution_seconds != OLD.execution_seconds)
                BEGIN SELECT RAISE(ABORT, 'terminal history query facts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_oa_history_cursor
                BEFORE UPDATE OF cursor_hash,query_id,version,stage,delivery_offset,created_at
                    ON oa_history_query_receipts
                BEGIN SELECT RAISE(ABORT, 'history cursor binding is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_oa_history_receipt
                BEFORE UPDATE OF response_json,completed_at ON oa_history_query_receipts
                WHEN OLD.response_json IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'history receipt is immutable'); END;
            """)

    @staticmethod
    def verify_session(record: dict[str, Any], *, session_id: str, principal_ref: str) -> None:
        if record["session_id"] != session_id or record["principal_ref"] != principal_ref:
            _fail("OA_HISTORY_SESSION_CHANGED", "OA 会话或账号已变化，请在当前会话重新查询。")

    @staticmethod
    def _validate_binding(user_subject: str, session_id: str, principal_ref: str) -> None:
        for value in (user_subject, session_id, principal_ref):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                _fail("OA_HISTORY_QUERY_INVALID", "查询身份绑定不完整。", status=400)

    def _owned(self, connection: sqlite3.Connection, query_id: str, *, user_subject: str,
               session_id: str, principal_ref: str) -> dict[str, Any]:
        self._validate_binding(user_subject, session_id, principal_ref)
        row = connection.execute("SELECT * FROM oa_history_queries WHERE query_id=? AND user_subject=?",
                                 (query_id, user_subject)).fetchone()
        if row is None:
            _fail("OA_HISTORY_QUERY_NOT_FOUND", "未找到当前账号可访问的查询。", status=404)
        if self._now() >= datetime.fromisoformat(row["expires_at"]):
            _fail("OA_HISTORY_QUERY_EXPIRED", "查询保留时间已到，请重新查询。", status=410)
        record = dict(row)
        self.verify_session(record, session_id=session_id, principal_ref=principal_ref)
        if hashlib.sha256(record["query_json"].encode("utf-8")).hexdigest() != record["query_hash"]:
            _fail("OA_HISTORY_QUERY_INTEGRITY", "冻结的查询条件校验失败。")
        record["query"] = json.loads(record.pop("query_json"))
        record["state_data"] = json.loads(record.pop("state_json"))
        record.pop("query_hash")
        return record

    @staticmethod
    def _cursor_hash(cursor: str) -> str:
        if not isinstance(cursor, str) or not re.fullmatch(r"ohc_[A-Za-z0-9_-]{43}", cursor):
            _fail("OA_HISTORY_CURSOR_INVALID", "查询续取标识无效。", status=400)
        return hashlib.sha256(cursor.encode("ascii")).hexdigest()

    def _issue_cursor(self, connection: sqlite3.Connection, *, query_id: str, version: int,
                      stage: str, delivery_offset: int, now: str) -> str:
        cursor = "ohc_" + secrets.token_urlsafe(32)
        connection.execute("INSERT INTO oa_history_query_receipts "
            "(cursor_hash,query_id,version,stage,delivery_offset,created_at) VALUES(?,?,?,?,?,?)",
            (self._cursor_hash(cursor), query_id, version, stage, delivery_offset, now))
        return cursor

    def create(self, *, user_subject: str, session_id: str, principal_ref: str,
               query: dict[str, Any], state_data: dict[str, Any] | None = None) -> dict[str, Any]:
        self._validate_binding(user_subject, session_id, principal_ref)
        if not isinstance(query, dict) or not query or (state_data is not None and not isinstance(state_data, dict)):
            _fail("OA_HISTORY_QUERY_INVALID", "需要规范化的查询条件和初始状态。", status=400)
        query_json, state_json = _json(query), _json(state_data or {})
        now = self._now()
        query_id = "ohq_" + secrets.token_urlsafe(18)
        binding = dict(user_subject=user_subject, session_id=session_id, principal_ref=principal_ref)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO oa_history_queries "
                "(query_id,user_subject,session_id,principal_ref,query_json,query_hash,state,state_json,version,created_at,updated_at,expires_at) "
                "VALUES(?,?,?,?,?,?,'scan',?,1,?,?,?)", (query_id, user_subject, session_id, principal_ref,
                query_json, hashlib.sha256(query_json.encode("utf-8")).hexdigest(), state_json,
                now.isoformat(), now.isoformat(), (now + timedelta(seconds=QUERY_TTL_SECONDS)).isoformat()))
            cursor = self._issue_cursor(connection, query_id=query_id, version=1, stage="scan",
                                        delivery_offset=0, now=now.isoformat())
            record = self._owned(connection, query_id, **binding)
        return {"record": record, "cursor": cursor}

    def get(self, query_id: str, *, user_subject: str, session_id: str, principal_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._owned(connection, query_id, user_subject=user_subject,
                               session_id=session_id, principal_ref=principal_ref)

    def latest_response(self, query_id: str, *, user_subject: str, session_id: str,
                        principal_ref: str) -> dict[str, Any] | None:
        """Recover progress, or the stable first result page, for an owned task."""
        with self._connect() as connection:
            connection.execute("BEGIN")
            record = self._owned(connection, query_id, user_subject=user_subject,
                                 session_id=session_id, principal_ref=principal_ref)
            rows = connection.execute("SELECT response_json FROM oa_history_query_receipts "
                "WHERE query_id=? AND response_json IS NOT NULL ORDER BY version", (query_id,)).fetchall()
            responses = [json.loads(row["response_json"]) for row in rows]
            if record["state"] in _TERMINAL:
                for response in responses:
                    if response.get("state") in _TERMINAL and (response.get("delivery") or {}).get("offset") == 0:
                        return response
            return responses[-1] if responses else None

    def _resolve(self, connection: sqlite3.Connection, cursor: str, **binding: str) -> dict[str, Any]:
        receipt = connection.execute("SELECT * FROM oa_history_query_receipts WHERE cursor_hash=?",
                                     (self._cursor_hash(cursor),)).fetchone()
        if receipt is None:
            _fail("OA_HISTORY_QUERY_NOT_FOUND", "未找到当前账号可访问的查询。", status=404)
        record = self._owned(connection, receipt["query_id"], **binding)
        cached = json.loads(receipt["response_json"]) if receipt["response_json"] is not None else None
        if cached is None and receipt["version"] != record["version"]:
            _fail("OA_HISTORY_REVISION_CONFLICT", "查询进度已变化，不能重复推进旧的续取标识。")
        return {"record": record, "stage": receipt["stage"], "delivery_offset": receipt["delivery_offset"],
                "cached_response": cached}

    def resolve(self, cursor: str, *, user_subject: str, session_id: str, principal_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            # Keep the receipt and query version in one read snapshot while a
            # different process may atomically finish the same continuation.
            connection.execute("BEGIN")
            return self._resolve(connection, cursor, user_subject=user_subject,
                                 session_id=session_id, principal_ref=principal_ref)

    def advance(
        self, cursor: str, *, user_subject: str, session_id: str, principal_ref: str,
        expected_version: int, state: str, state_data: dict[str, Any], response: dict[str, Any],
        next_stage: str | None = None, next_offset: int = 0, scanned_delta: int = 0,
        detail_delta: int = 0, elapsed_seconds: float = 0,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            resolved = self._resolve(connection, cursor, user_subject=user_subject,
                                     session_id=session_id, principal_ref=principal_ref)
            if resolved["cached_response"] is not None:
                return resolved["cached_response"]
            record = resolved["record"]
            if type(expected_version) is not int or expected_version != record["version"]:
                _fail("OA_HISTORY_REVISION_CONFLICT", "查询版本已变化，不能重复推进。")
            if (not isinstance(state, str) or state not in _STATES
                    or not isinstance(state_data, dict) or not isinstance(response, dict)
                    or (next_stage is not None and (not isinstance(next_stage, str) or next_stage not in _STAGES))
                    or type(next_offset) is not int or next_offset < 0
                    or type(scanned_delta) is not int or scanned_delta < 0
                    or type(detail_delta) is not int or detail_delta < 0
                    or isinstance(elapsed_seconds, bool) or not isinstance(elapsed_seconds, (int, float))
                    or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0):
                _fail("OA_HISTORY_QUERY_INVALID", "查询进度参数不合法。", status=400)
            state_json = _json(state_data)
            if (record["state"] == "details" and state == "scan") or (
                record["state"] in _TERMINAL and (state != record["state"] or state_data != record["state_data"]
                                                  or scanned_delta or detail_delta or elapsed_seconds)
            ):
                _fail("OA_HISTORY_STATE_INVALID", "不能回退查询进度或修改已终结的结果。")
            scanned = record["scanned_count"] + scanned_delta
            detailed = record["detail_count"] + detail_delta
            executed = record["execution_seconds"] + elapsed_seconds
            reason = None
            if record["state"] not in _TERMINAL:
                if scanned > MAX_SOURCE_ROWS or (state == "scan" and scanned == MAX_SOURCE_ROWS):
                    reason = "source_row_budget"
                elif detailed > MAX_DETAIL_ITEMS or (state == "details" and detailed == MAX_DETAIL_ITEMS):
                    reason = "detail_item_budget"
                elif executed > MAX_EXECUTION_SECONDS or (state not in _TERMINAL and executed == MAX_EXECUTION_SECONDS):
                    reason = "execution_time_budget"
            public_response = deepcopy(response)
            if reason:
                state = "partial"
                state_data = {**state_data, "completion_reason": reason}
                state_json = _json(state_data)
                public_response.update(state="partial", completion_reason=reason)
                next_stage, next_offset = "results", 0
            if state not in _TERMINAL:
                next_stage = next_stage or state
                if next_stage != state or next_offset:
                    _fail("OA_HISTORY_STATE_INVALID", "活动查询的续取阶段不匹配。")
            elif next_stage not in {None, "results"}:
                _fail("OA_HISTORY_STATE_INVALID", "终结的查询只能继续交付已有结果。")
            if resolved["stage"] == "results" and record["state"] not in _TERMINAL:
                _fail("OA_HISTORY_STATE_INVALID", "查询尚未形成可交付的终态结果。")
            version = expected_version + 1
            now = self._now().isoformat()
            next_cursor = self._issue_cursor(connection, query_id=record["query_id"], version=version,
                stage=next_stage, delivery_offset=next_offset, now=now) if next_stage else None
            public_response["next_cursor"] = next_cursor
            # A plan may return only a bounded reference to this saved page.
            # Replaying this receipt delivers that exact page, even when it is
            # the only/final page and has no continuation cursor.
            public_response["replay_cursor"] = cursor
            response_json = _json(public_response)
            if response_chars(public_response) > MAX_RESULT_CHARS:
                _fail("OA_HISTORY_RESULT_ENVELOPE_TOO_LARGE", "完整响应超出交付预算，查询进度尚未推进。")
            updated = connection.execute("UPDATE oa_history_queries SET state=?,state_json=?,version=?,"
                "scanned_count=?,detail_count=?,execution_seconds=?,updated_at=? WHERE query_id=? AND version=?",
                (state, state_json, version, scanned, detailed, executed, now, record["query_id"], expected_version))
            if updated.rowcount != 1:
                _fail("OA_HISTORY_REVISION_CONFLICT", "查询版本已变化，不能重复推进。")
            connection.execute("UPDATE oa_history_query_receipts SET response_json=?,completed_at=? WHERE cursor_hash=?",
                               (response_json, now, self._cursor_hash(cursor)))
            return json.loads(response_json)
