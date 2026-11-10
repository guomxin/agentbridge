"""Owned business watches and atomic observations; no downstream execution.

Callers must verify the current business permission, principal and precise OA
identity before creating or observing a watch. Notification callbacks may only
append to the supplied transaction; this store never sends a notification.
"""
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
from typing import Any, Callable, Iterator
from uuid import UUID, uuid4

from agentbridge.core.store_transactions import store_transaction


WATCH_STATES = {"active", "paused", "stopped", "completed", "expired"}
WATCH_RULES = ("nodes", "handlers", "ended")
SOURCE_IDS = ("affair_id", "summary_id", "process_id", "case_id")
MAX_WATCH_DAYS = 30
DEFAULT_WATCH_DAYS = 7
MIN_INTERVAL_SECONDS = 900
MAX_RETRY_SECONDS = 6 * 3600


class BusinessWatchError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class BusinessWatchConflict(BusinessWatchError):
    def __init__(self):
        super().__init__("BUSINESS_WATCH_VERSION_CONFLICT", "关注已更新，请刷新后重试。", status=409)


def _fail(code, message, *, status=400):
    raise BusinessWatchError(code, message, status=status)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        _fail("BUSINESS_WATCH_INVALID", "关注数据格式不合法。")


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value, maximum, label):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or "\x00" in value:
        _fail("BUSINESS_WATCH_INVALID", f"{label}不合法。")
    return value.strip()


def _time(value, label):
    try:
        if not isinstance(value, str):
            raise ValueError()
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _fail("BUSINESS_WATCH_INVALID", f"{label}需要包含时区的有效时间。")


def _rules(value):
    if value is None:
        return list(WATCH_RULES)
    if not isinstance(value, (list, tuple)) or not value or len(value) > len(WATCH_RULES):
        _fail("BUSINESS_WATCH_INVALID", "请选择有效关注条件。")
    if any(not isinstance(item, str) or item not in WATCH_RULES for item in value) or len(set(value)) != len(value):
        _fail("BUSINESS_WATCH_INVALID", "请选择有效关注条件。")
    return [rule for rule in WATCH_RULES if rule in value]


def _interval(value):
    if type(value) is not int or not MIN_INTERVAL_SECONDS <= value <= 2_147_483_647:
        _fail("BUSINESS_WATCH_INVALID", "检查间隔至少为15分钟。")
    return value


def _boolean(value):
    if type(value) is not bool:
        _fail("BUSINESS_WATCH_INVALID", "持续读取同意必须是明确的布尔值。")
    return value


def _short_code(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", value):
        _fail("BUSINESS_WATCH_INVALID", f"{label}不合法。")
    return value


def _retry_seconds(interval, failures):
    # Failed observations must not poll more often than the selected interval.
    return min(max(interval, MAX_RETRY_SECONDS), interval * 2 ** min(failures - 1, 16))


def _source(value):
    if not isinstance(value, dict) or not isinstance(value.get("collection"), str) or value["collection"] not in {"sent", "done"}:
        _fail("BUSINESS_WATCH_INVALID_SOURCE", "关注来源须为已发或已办的精确流程。")
    result = {"collection": value["collection"]}
    for key in SOURCE_IDS:
        item = value.get(key)
        if not isinstance(item, str) or not re.fullmatch(r"-?\d{1,20}", item):
            _fail("BUSINESS_WATCH_INVALID_SOURCE", "关注来源缺少精确流程身份。")
        result[key] = item
    return result


def _endpoint_ids(value):
    if not isinstance(value, (list, tuple)) or len(value) > 20:
        _fail("BUSINESS_WATCH_INVALID", "提醒端点不合法。")
    return sorted({_text(item, 256, "提醒端点") for item in value})


def _normal_snapshot(value, rules):
    if not isinstance(value, dict):
        _fail("BUSINESS_WATCH_SNAPSHOT_INVALID", "核验结果格式不合法。")
    state = value.get("workflow_state")
    if not isinstance(state, str) or state not in {"in_progress", "ended", "unknown"}:
        _fail("BUSINESS_WATCH_SNAPSHOT_INVALID", "核验结果缺少有效流程状态。")
    unavailable = value.get("unavailable_fields", [])
    if not isinstance(unavailable, list) or len(unavailable) > 50 or any(not isinstance(item, str) for item in unavailable):
        _fail("BUSINESS_WATCH_SNAPSHOT_INVALID", "核验结果的可用字段说明不合法。")
    observed = _time(value["observed_at"], "来源观察时间").isoformat() if value.get("observed_at") is not None else None
    if state == "unknown":
        return None, "unknown", observed
    completeness = value.get("completeness")
    if completeness is not None and completeness not in ("complete", "partial"):
        _fail("BUSINESS_WATCH_SNAPSHOT_INVALID", "核验结果完整性说明不合法。")
    if completeness == "partial":
        return None, "incomplete", observed
    if observed is None:
        _fail("BUSINESS_WATCH_SNAPSHOT_INVALID", "核验结果缺少来源观察时间。")
    required = {"workflow_state"}
    if "nodes" in rules:
        required.add("current_nodes")
    if "handlers" in rules:
        required.update(("current_nodes", "current_handlers"))
    if required.intersection(unavailable):
        return None, "incomplete", observed
    raw_nodes, raw_handlers = value.get("current_nodes"), value.get("current_handlers")
    if raw_nodes is None and "current_nodes" in required:
        return None, "incomplete", observed
    if raw_handlers is None and "current_handlers" in required:
        return None, "incomplete", observed
    nodes = None
    if raw_nodes is not None:
        if not isinstance(raw_nodes, list) or len(raw_nodes) > 100:
            _fail("BUSINESS_WATCH_SNAPSHOT_INVALID", "核验节点格式不合法。")
        nodes = []
        for node in raw_nodes:
            if not isinstance(node, dict) or not isinstance(node.get("handlers"), list) or len(node["handlers"]) > 100:
                _fail("BUSINESS_WATCH_SNAPSHOT_INVALID", "核验节点处理人格式不合法。")
            name = _text(node.get("name"), 300, "节点名称")
            handlers = sorted({_text(item, 120, "处理人") for item in node["handlers"]})
            if state == "in_progress" and not handlers:
                return None, "incomplete", observed
            nodes.append({"name": name, "handlers": handlers})
        # Preserve parallel-node multiplicity. Display names are not node IDs.
        nodes.sort(key=lambda node: (node["name"], node["handlers"]))
    handlers = None
    if raw_handlers is not None:
        if not isinstance(raw_handlers, list) or len(raw_handlers) > 1000:
            _fail("BUSINESS_WATCH_SNAPSHOT_INVALID", "当前处理人格式不合法。")
        handlers = sorted({_text(item, 120, "处理人") for item in raw_handlers})
    if state == "ended":
        if nodes or handlers:
            _fail("BUSINESS_WATCH_SNAPSHOT_INVALID", "已结束流程仍有活动节点，不能确认结束。")
        nodes, handlers = [], []
    elif ("current_nodes" in required and not nodes) or ("current_handlers" in required and not handlers):
        return None, "incomplete", observed
    if nodes is not None and handlers is not None and set(handlers) != {person for node in nodes for person in node["handlers"]}:
        _fail("BUSINESS_WATCH_SNAPSHOT_INVALID", "节点和处理人核验结果不一致。")
    return {"workflow_state": state, "current_nodes": nodes, "current_handlers": handlers,
            "observed_at": observed}, "verified", observed


def _diff(before, after, rules):
    def projection(snapshot, rule):
        if rule == "ended":
            return snapshot["workflow_state"] == "ended"
        if rule == "nodes":
            return [node["name"] for node in snapshot["current_nodes"] or []]
        return {"current_handlers": snapshot["current_handlers"], "nodes": snapshot["current_nodes"]}
    result = {}
    for rule in rules:
        old, new = projection(before, rule), projection(after, rule)
        if old != new:
            result[rule] = {"before": old, "after": new}
    return result


class BusinessWatchStore:
    def __init__(self, db_path: Path | str, *, clock: Callable[[], datetime] | None = None,
                 on_change: Callable | None = None, max_active_per_owner: int = 50):
        if type(max_active_per_owner) is not int or not 1 <= max_active_per_owner <= 10000:
            raise ValueError("max_active_per_owner must be an integer between 1 and 10000")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.on_change = on_change
        self.max_active_per_owner = max_active_per_owner
        self._initialize()

    def _now(self):
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("business watch clock must include timezone")
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

    def _initialize(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS business_watches (
                    watch_id TEXT PRIMARY KEY, user_subject TEXT NOT NULL, principal_ref TEXT NOT NULL,
                    system_instance TEXT NOT NULL, source_json TEXT NOT NULL, target_key TEXT NOT NULL,
                    title TEXT NOT NULL, rules_json TEXT NOT NULL, interval_seconds INTEGER NOT NULL,
                    expires_at TEXT NOT NULL, endpoints_json TEXT NOT NULL, allow_mark_read INTEGER NOT NULL,
                    state TEXT NOT NULL, pause_reason TEXT, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, next_check_at TEXT,
                    last_checked_at TEXT, last_verified_at TEXT, last_source_observed_at TEXT,
                    last_check_status TEXT NOT NULL, last_snapshot_json TEXT, last_snapshot_hash TEXT,
                    snapshot_rules_json TEXT NOT NULL, observation_seq INTEGER NOT NULL, change_seq INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS business_watches_active_target
                    ON business_watches (user_subject,target_key) WHERE state IN ('active','paused');
                CREATE INDEX IF NOT EXISTS business_watches_owner_created
                    ON business_watches (user_subject,created_at DESC,watch_id DESC);
                CREATE INDEX IF NOT EXISTS business_watches_due
                    ON business_watches (state,next_check_at);
                CREATE TABLE IF NOT EXISTS business_watch_create_requests (
                    user_subject TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                    watch_id TEXT NOT NULL REFERENCES business_watches(watch_id), created_at TEXT NOT NULL,
                    PRIMARY KEY (user_subject,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS business_watch_observations (
                    observation_id TEXT PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES business_watches(watch_id),
                    user_subject TEXT NOT NULL, sequence INTEGER NOT NULL, watch_version INTEGER NOT NULL,
                    status TEXT NOT NULL, source_observed_at TEXT, checked_at TEXT NOT NULL,
                    snapshot_hash TEXT, UNIQUE (watch_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS business_watch_changes (
                    event_id TEXT PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES business_watches(watch_id),
                    user_subject TEXT NOT NULL, sequence INTEGER NOT NULL, event_type TEXT NOT NULL,
                    change_types_json TEXT NOT NULL, diff_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (watch_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS business_watch_checks (
                    check_id TEXT PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES business_watches(watch_id),
                    user_subject TEXT NOT NULL, claimed_version INTEGER NOT NULL,
                    lease_token_hash TEXT NOT NULL, lease_expires_at TEXT NOT NULL,
                    status TEXT NOT NULL, claimed_at TEXT NOT NULL, finished_at TEXT,
                    result_code TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS business_watch_checks_one_running
                    ON business_watch_checks(watch_id) WHERE status='running';
                CREATE INDEX IF NOT EXISTS business_watch_checks_lease
                    ON business_watch_checks(status,lease_expires_at);
            """)
            connection.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(business_watches)")}
            for name, definition in (("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
                                     ("last_error_code", "TEXT"), ("last_success_at", "TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE business_watches ADD COLUMN {name} {definition}")

    def _owned(self, connection, watch_id, user_subject):
        row = connection.execute("SELECT * FROM business_watches WHERE watch_id=? AND user_subject=?",
                                 (watch_id, user_subject)).fetchone()
        if row is None:
            _fail("BUSINESS_WATCH_NOT_FOUND", "未找到该关注。", status=404)
        return row

    def _state(self, row, now):
        return "expired" if row["state"] in {"active", "paused"} and row["expires_at"] <= now.isoformat() else row["state"]

    def _view(self, row, *, include_private=False, now=None):
        result = {key: row[key] for key in ("watch_id", "title", "interval_seconds", "expires_at", "version",
            "created_at", "updated_at", "pause_reason", "last_checked_at", "last_verified_at",
            "last_source_observed_at", "last_check_status", "change_seq", "consecutive_failures",
            "last_error_code", "last_success_at")}
        result.update(state=self._state(row, now or self._now()), rules=json.loads(row["rules_json"]),
            endpoints=json.loads(row["endpoints_json"]), allow_mark_read=bool(row["allow_mark_read"]),
            last_snapshot=json.loads(row["last_snapshot_json"]) if row["last_snapshot_json"] else None)
        if include_private:
            result.update({key: row[key] for key in ("user_subject", "principal_ref", "system_instance", "target_key",
                "next_check_at", "last_snapshot_hash", "observation_seq")})
            result["source"] = json.loads(row["source_json"])
        return result

    def _endpoints(self, connection, user_subject, endpoints):
        for endpoint_id in endpoints:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_endpoints'").fetchone()
            row = connection.execute("SELECT 1 FROM client_endpoints WHERE endpoint_id=? AND user_subject=? AND state='active'",
                (endpoint_id, user_subject)).fetchone() if exists else None
            if row is None:
                _fail("BUSINESS_WATCH_ENDPOINT_DENIED", "提醒端点不可用或不属于当前账号。", status=403)

    def _expiry(self, value, now):
        expiry = now + timedelta(days=DEFAULT_WATCH_DAYS) if value is None else _time(value, "关注期限")
        if not now < expiry <= now + timedelta(days=MAX_WATCH_DAYS):
            _fail("BUSINESS_WATCH_INVALID", "关注期限须在未来30天内。")
        return expiry.isoformat()

    def _discard_checks(self, connection, watch_id, reason, now, *, exclude_check_id=None):
        query = "UPDATE business_watch_checks SET status='discarded',finished_at=?,result_code=? WHERE watch_id=? AND status='running'"
        values = [now.isoformat(), reason, watch_id]
        if exclude_check_id is not None:
            query += " AND check_id<>?"
            values.append(exclude_check_id)
        connection.execute(query, values)

    def create(self, *, user_subject, principal_ref, system_instance, source, title, rules=None,
               interval_seconds=3600, expires_at=None, endpoints=(), allow_mark_read=False,
               idempotency_key=None, include_private=False):
        owner = _text(user_subject, 256, "账号")
        principal = _text(principal_ref, 256, "业务主体")
        instance = _text(system_instance, 512, "业务系统实例")
        source, title, rules = _source(source), _text(title, 1000, "关注标题"), _rules(rules)
        interval, endpoints, consent = _interval(interval_seconds), _endpoint_ids(endpoints), _boolean(allow_mark_read)
        key = _text(idempotency_key, 200, "请求标识") if idempotency_key is not None else None
        now = self._now()
        # The workflow is stable across the user's sent/done affairs. All four
        # source IDs remain private verification anchors, not a permanent URL.
        target = _hash([principal, instance, *(source[field] for field in ("summary_id", "process_id", "case_id"))])
        requested_expiry = _time(expires_at, "关注期限").isoformat() if expires_at is not None else None
        request_hash = _hash([principal, instance, source, title, rules, interval, requested_expiry, endpoints, consent])
        with store_transaction(self) as connection:
            if key:
                receipt = connection.execute("SELECT * FROM business_watch_create_requests WHERE user_subject=? AND idempotency_key=?",
                    (owner, key)).fetchone()
                if receipt:
                    if receipt["request_hash"] != request_hash:
                        _fail("BUSINESS_WATCH_IDEMPOTENCY_CONFLICT", "请求标识已用于不同关注内容。", status=409)
                    return self._view(self._owned(connection, receipt["watch_id"], owner), include_private=include_private, now=now), True
            expiry = self._expiry(expires_at, now)
            self._endpoints(connection, owner, endpoints)
            connection.execute("UPDATE business_watches SET state='expired',version=version+1,updated_at=?,next_check_at=NULL "
                "WHERE user_subject=? AND target_key=? AND state IN ('active','paused') AND expires_at<=?",
                (now.isoformat(), owner, target, now.isoformat()))
            existing = connection.execute("SELECT * FROM business_watches WHERE user_subject=? AND target_key=? "
                "AND state IN ('active','paused')", (owner, target)).fetchone()
            reused = existing is not None
            watch_id = existing["watch_id"] if existing else str(uuid4())
            if not existing:
                count = connection.execute("SELECT COUNT(*) FROM business_watches WHERE user_subject=? "
                    "AND state IN ('active','paused') AND expires_at>?", (owner, now.isoformat())).fetchone()[0]
                if count >= self.max_active_per_owner:
                    _fail("BUSINESS_WATCH_LIMIT_REACHED", "本人有效关注数量已达上限，请先停止不再需要的关注。", status=409)
                connection.execute("INSERT INTO business_watches (watch_id,user_subject,principal_ref,system_instance,source_json,target_key,"
                    "title,rules_json,interval_seconds,expires_at,endpoints_json,allow_mark_read,state,version,created_at,updated_at,"
                    "next_check_at,last_check_status,snapshot_rules_json,observation_seq,change_seq) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'active',1,?,?,?,'never_checked','[]',0,0)",
                    (watch_id, owner, principal, instance, _json(source), target, title, _json(rules), interval,
                     expiry, _json(endpoints), int(consent), now.isoformat(), now.isoformat(), now.isoformat()))
            if key:
                connection.execute("INSERT INTO business_watch_create_requests VALUES (?,?,?,?,?)",
                    (owner, key, request_hash, watch_id, now.isoformat()))
            return self._view(self._owned(connection, watch_id, owner), include_private=include_private, now=now), reused

    def get(self, watch_id, *, user_subject, include_private=False):
        with self._connect() as connection:
            return self._view(self._owned(connection, watch_id, user_subject), include_private=include_private)

    def list(self, *, user_subject, state=None, before_created_at=None, before_watch_id=None,
             limit=32, include_private=False):
        if (type(limit) is not int or not 1 <= limit <= 100
                or (state is not None and (not isinstance(state, str) or state not in WATCH_STATES | {"all"}))):
            _fail("BUSINESS_WATCH_INVALID", "关注列表条件不合法。")
        if (before_created_at is None) != (before_watch_id is None):
            _fail("BUSINESS_WATCH_INVALID", "翻页信息不完整。")
        now = self._now()
        query, parameters = "SELECT * FROM business_watches WHERE user_subject=?", [user_subject]
        if state and state != "all":
            query += " AND CASE WHEN state IN ('active','paused') AND expires_at<=? THEN 'expired' ELSE state END=?"
            parameters.extend((now.isoformat(), state))
        if before_created_at is not None:
            before_created_at = _time(before_created_at, "翻页时间").isoformat()
            try:
                if str(UUID(before_watch_id)) != before_watch_id:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                _fail("BUSINESS_WATCH_INVALID", "翻页标识不合法。")
            query += " AND (created_at<? OR (created_at=? AND watch_id<?))"
            parameters.extend((before_created_at, before_created_at, before_watch_id))
        query += " ORDER BY created_at DESC,watch_id DESC LIMIT ?"
        parameters.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        more, rows = len(rows) > limit, rows[:limit]
        return {"items": [self._view(row, include_private=include_private, now=now) for row in rows], "has_more": more,
            "next_cursor": {"before_created_at": rows[-1]["created_at"], "before_watch_id": rows[-1]["watch_id"]} if more else None}

    def update(self, watch_id, *, user_subject, expected_version, rules=None, interval_seconds=None,
               expires_at=None, endpoints=None, allow_mark_read=None, state=None, pause_reason=None, include_private=False):
        if type(expected_version) is not int or expected_version < 1:
            _fail("BUSINESS_WATCH_INVALID", "关注版本不合法。")
        if state is not None and (not isinstance(state, str) or state not in {"active", "paused", "stopped"}):
            _fail("BUSINESS_WATCH_INVALID", "关注状态不合法。")
        if pause_reason is not None and (not isinstance(pause_reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", pause_reason)):
            _fail("BUSINESS_WATCH_INVALID", "暂停原因不合法。")
        now = self._now()
        with store_transaction(self) as connection:
            row = self._owned(connection, watch_id, user_subject)
            if row["version"] != expected_version:
                raise BusinessWatchConflict()
            if self._state(row, now) not in {"active", "paused"}:
                _fail("BUSINESS_WATCH_TERMINAL", "该关注已结束，不能恢复或修改。", status=409)
            next_state = state or row["state"]
            next_rules = json.loads(row["rules_json"]) if rules is None else _rules(rules)
            next_endpoints = json.loads(row["endpoints_json"]) if endpoints is None else _endpoint_ids(endpoints)
            # A disconnected notification endpoint must never prevent the owner
            # from pausing or stopping. New selections and active watches still
            # require currently owned, active endpoints.
            if endpoints is not None or next_state == "active":
                self._endpoints(connection, user_subject, next_endpoints)
            next_interval = row["interval_seconds"] if interval_seconds is None else _interval(interval_seconds)
            next_expiry = row["expires_at"] if expires_at is None else self._expiry(expires_at, now)
            consent = bool(row["allow_mark_read"]) if allow_mark_read is None else _boolean(allow_mark_read)
            reason = None if next_state == "active" else pause_reason or row["pause_reason"] or "manual"
            snapshot_rules = [rule for rule in json.loads(row["snapshot_rules_json"]) if rule in next_rules]
            next_check = (now.isoformat() if next_state == "active" and row["state"] != "active" else
                (now + timedelta(seconds=next_interval)).isoformat() if interval_seconds is not None else row["next_check_at"])
            if next_state != "active":
                next_check = None
            connection.execute("UPDATE business_watches SET rules_json=?,interval_seconds=?,expires_at=?,endpoints_json=?,"
                "allow_mark_read=?,state=?,pause_reason=?,snapshot_rules_json=?,next_check_at=?,version=version+1,updated_at=? WHERE watch_id=?",
                (_json(next_rules), next_interval, next_expiry, _json(next_endpoints), int(consent), next_state, reason,
                 _json(snapshot_rules), next_check, now.isoformat(), watch_id))
            self._discard_checks(connection, watch_id, "watch_updated", now)
            return self._view(self._owned(connection, watch_id, user_subject), include_private=include_private, now=now)

    def apply_observation(self, watch_id, *, user_subject, expected_version, snapshot, include_private=False):
        if type(expected_version) is not int or expected_version < 1:
            _fail("BUSINESS_WATCH_INVALID", "关注版本不合法。")
        now = self._now()
        with store_transaction(self) as connection:
            row = self._owned(connection, watch_id, user_subject)
            if row["version"] != expected_version:
                raise BusinessWatchConflict()
            if self._state(row, now) != "active":
                _fail("BUSINESS_WATCH_NOT_ACTIVE", "该关注当前不能接收核验结果。", status=409)
            return self._apply_observation(connection, row, snapshot, now, include_private=include_private)

    def _apply_observation(self, connection, row, snapshot, now, *, include_private=False, check_id=None):
        """Use the caller's writer transaction, including the optional outbox hook."""
        watch_id, user_subject = row["watch_id"], row["user_subject"]
        rules = json.loads(row["rules_json"])
        normalized, status, observed = _normal_snapshot(snapshot, rules)
        if normalized and row["last_source_observed_at"] and observed < row["last_source_observed_at"]:
            normalized, status = None, "older_observation"
        baseline_created = normalized is not None and row["last_snapshot_json"] is None
        before = json.loads(row["last_snapshot_json"]) if row["last_snapshot_json"] else None
        changes = _diff(before, normalized, json.loads(row["snapshot_rules_json"])) if before and normalized else {}
        sequence = row["change_seq"] + int(bool(changes))
        observation_seq = row["observation_seq"] + 1
        snapshot_hash = _hash({key: value for key, value in normalized.items() if key != "observed_at"}) if normalized else None
        state = "completed" if normalized and normalized["workflow_state"] == "ended" else "active"
        failures = 0 if normalized else row["consecutive_failures"] + 1
        error = None if normalized else {"unknown": "WATCH_STATE_UNKNOWN", "incomplete": "WATCH_STATE_INCOMPLETE",
                                        "older_observation": "WATCH_OBSERVATION_OLDER"}[status]
        wait = row["interval_seconds"] if normalized else _retry_seconds(row["interval_seconds"], failures)
        connection.execute("INSERT INTO business_watch_observations VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid4()), watch_id, user_subject, observation_seq, row["version"], status, observed, now.isoformat(), snapshot_hash))
        connection.execute("UPDATE business_watches SET version=version+1,updated_at=?,last_checked_at=?,last_check_status=?,"
            "observation_seq=?,next_check_at=?,state=?,consecutive_failures=?,last_error_code=? WHERE watch_id=?",
            (now.isoformat(), now.isoformat(), status, observation_seq,
             (now + timedelta(seconds=wait)).isoformat() if state == "active" else None, state, failures, error, watch_id))
        if normalized:
            connection.execute("UPDATE business_watches SET last_verified_at=?,last_success_at=?,last_source_observed_at=?,last_snapshot_json=?,"
                "last_snapshot_hash=?,snapshot_rules_json=?,change_seq=? WHERE watch_id=?",
                (now.isoformat(), now.isoformat(), observed, _json(normalized), snapshot_hash, _json(rules), sequence, watch_id))
        self._discard_checks(connection, watch_id, "watch_observed", now, exclude_check_id=check_id)
        event = None
        if changes:
            event = {"event_id": str(uuid4()), "watch_id": watch_id, "sequence": sequence,
                "event_type": "watch.completed" if "ended" in changes and normalized["workflow_state"] == "ended" else "watch.changed",
                "change_types": [rule for rule in WATCH_RULES if rule in changes], "diff": changes,
                "observed_at": observed, "created_at": now.isoformat()}
            connection.execute("INSERT INTO business_watch_changes VALUES (?,?,?,?,?,?,?,?,?)",
                (event["event_id"], watch_id, user_subject, sequence, event["event_type"], _json(event["change_types"]),
                 _json(changes), observed, now.isoformat()))
            if self.on_change:
                self.on_change(connection, event, self._view(self._owned(connection, watch_id, user_subject), include_private=True, now=now))
        return {"watch": self._view(self._owned(connection, watch_id, user_subject), include_private=include_private, now=now),
            "baseline_created": baseline_created, "changed": bool(changes), "event": event,
            "ignored": normalized is None, "reason": None if normalized else status}

    def claim_due(self, *, limit=4, lease_seconds=90):
        """Reserve bounded checks. Never hold this transaction during downstream IO.

        Returned lease tokens are private worker authority, never API fields.
        The caller still verifies grants, principal and read consent before IO.
        """
        if type(limit) is not int or not 1 <= limit <= 100 or type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            _fail("BUSINESS_WATCH_INVALID", "检查领取参数不合法。")
        result = []
        with store_transaction(self) as connection:
            now = self._now()
            connection.execute("UPDATE business_watches SET state='expired',version=version+1,updated_at=?,next_check_at=NULL "
                "WHERE state IN ('active','paused') AND expires_at<=?", (now.isoformat(), now.isoformat()))
            connection.execute("UPDATE business_watch_checks SET status='discarded',finished_at=?,result_code='watch_changed' "
                "WHERE status='running' AND EXISTS (SELECT 1 FROM business_watches w WHERE w.watch_id=business_watch_checks.watch_id "
                "AND (w.version<>business_watch_checks.claimed_version OR w.state<>'active' OR w.allow_mark_read=0))", (now.isoformat(),))
            connection.execute("UPDATE business_watch_checks SET status='lease_expired',finished_at=?,result_code='lease_expired' "
                "WHERE status='running' AND lease_expires_at<=?", (now.isoformat(), now.isoformat()))
            rows = connection.execute("SELECT w.* FROM business_watches w WHERE w.state='active' AND w.allow_mark_read=1 "
                "AND w.expires_at>? AND w.next_check_at<=? AND NOT EXISTS (SELECT 1 FROM business_watch_checks c "
                "WHERE c.watch_id=w.watch_id AND c.status='running') ORDER BY w.next_check_at,w.created_at,w.watch_id LIMIT ?",
                (now.isoformat(), now.isoformat(), limit)).fetchall()
            for row in rows:
                check_id, token = str(uuid4()), secrets.token_urlsafe(32)
                connection.execute("INSERT INTO business_watch_checks "
                    "(check_id,watch_id,user_subject,claimed_version,lease_token_hash,lease_expires_at,status,claimed_at) "
                    "VALUES (?,?,?,?,?,?,'running',?)", (check_id, row["watch_id"], row["user_subject"], row["version"],
                    hashlib.sha256(token.encode()).hexdigest(), (now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat()))
                result.append({"watch": self._view(row, include_private=True, now=now), "check_id": check_id,
                    "lease_token": token, "claimed_version": row["version"]})
        return result

    def finish_check(self, check_id, lease_token, *, snapshot=None, error_code=None, pause_reason=None):
        """Apply only the live lease's unchanged watch, with one atomic commit.

        An obsolete completion can only close its own check record. It never
        advances the watch, baseline, changes, or a replacement worker's lease.
        """
        if not isinstance(check_id, str) or not isinstance(lease_token, str) or not 1 <= len(lease_token) <= 256:
            _fail("BUSINESS_WATCH_INVALID", "检查租约不合法。")
        if error_code is not None:
            error_code = _short_code(error_code, "检查错误码")
        if pause_reason is not None:
            pause_reason = _short_code(pause_reason, "暂停原因")
        if (snapshot is not None) == (error_code is not None or pause_reason is not None):
            _fail("BUSINESS_WATCH_INVALID", "请提供检查结果或安全错误码。")
        with store_transaction(self) as connection:
            now = self._now()
            run = connection.execute("SELECT * FROM business_watch_checks WHERE check_id=?", (check_id,)).fetchone()
            if run is None or not hmac.compare_digest(run["lease_token_hash"], hashlib.sha256(lease_token.encode()).hexdigest()):
                return {"status": "discarded", "reason": "lease_not_owned", "watch": None, "outcome": None}
            row = self._owned(connection, run["watch_id"], run["user_subject"])
            reason = (run["result_code"] or "already_finished") if run["status"] != "running" else (
                "lease_expired" if run["lease_expires_at"] <= now.isoformat() else
                "version_changed" if row["version"] != run["claimed_version"] else
                "watch_expired" if row["expires_at"] <= now.isoformat() else
                "watch_not_active" if row["state"] != "active" else
                "read_consent_required" if not row["allow_mark_read"] else None)
            if reason:
                if run["status"] == "running":
                    connection.execute("UPDATE business_watch_checks SET status='discarded',finished_at=?,result_code=? WHERE check_id=?",
                        (now.isoformat(), reason, check_id))
                return {"status": "discarded", "reason": reason,
                    "watch": self._view(row, include_private=True, now=now), "outcome": None}
            outcome = None
            if snapshot is not None:
                outcome = self._apply_observation(connection, row, snapshot, now, include_private=True, check_id=check_id)
                result_code = outcome["reason"]
                run_status = "unverified" if outcome["ignored"] else "succeeded"
            else:
                failures = row["consecutive_failures"] + 1
                result_code, run_status = error_code or pause_reason, "failed"
                wait = min(row["interval_seconds"], 60) if result_code == "WATCH_SESSION_BUSY" else _retry_seconds(row["interval_seconds"], failures)
                connection.execute("UPDATE business_watches SET version=version+1,updated_at=?,last_checked_at=?,last_check_status='failed',"
                    "consecutive_failures=?,last_error_code=?,state=?,pause_reason=?,next_check_at=? WHERE watch_id=?",
                    (now.isoformat(), now.isoformat(), failures, result_code, "paused" if pause_reason else "active", pause_reason,
                     None if pause_reason else (now + timedelta(seconds=wait)).isoformat(), row["watch_id"]))
            connection.execute("UPDATE business_watch_checks SET status=?,finished_at=?,result_code=? WHERE check_id=?",
                (run_status, now.isoformat(), result_code, check_id))
            return {"status": "finished", "reason": result_code,
                "watch": self._view(self._owned(connection, row["watch_id"], row["user_subject"]), include_private=True, now=now), "outcome": outcome}

    def history(self, watch_id, *, user_subject, limit=100, before_seq=None):
        if type(limit) is not int or not 1 <= limit <= 100 or (before_seq is not None and (type(before_seq) is not int or before_seq < 1)):
            _fail("BUSINESS_WATCH_INVALID", "关注历史条件不合法。")
        with self._connect() as connection:
            self._owned(connection, watch_id, user_subject)
            query, values = "SELECT * FROM business_watch_changes WHERE watch_id=? AND user_subject=?", [watch_id, user_subject]
            if before_seq is not None:
                query += " AND sequence<?"
                values.append(before_seq)
            rows = connection.execute(query + " ORDER BY sequence DESC LIMIT ?", [*values, limit]).fetchall()
        return [{**{key: row[key] for key in ("event_id", "watch_id", "sequence", "event_type", "observed_at", "created_at")},
                 "change_types": json.loads(row["change_types_json"]), "diff": json.loads(row["diff_json"])} for row in rows]
