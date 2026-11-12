"""Owned Workspace preferences and durable scheduling for the OA daily digest."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta, timezone
import json
import logging
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from agentbridge.core.business_watches import BusinessWatchError
from agentbridge.core.store_transactions import store_transaction


_LOG = logging.getLogger(__name__)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_TIME = "08:30"
_TIME_FORMAT = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_FIELDS = {"enabled", "time", "workspace", "telegramEndpointId", "expectedVersion"}
_LEASE_SECONDS = 120
_PUBLISHED = {"published", "partial", "reused"}
_OUTCOMES = _PUBLISHED | {"endpoint_unavailable", "permission_unavailable", "failed", "stale"}


def _next_run(send_time: str, now: datetime, *, skip_today=False) -> str:
    local_now = now.astimezone(_SHANGHAI)
    candidate = datetime.combine(local_now.date(), time.fromisoformat(send_time), tzinfo=_SHANGHAI)
    if candidate <= local_now or skip_today:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).isoformat()


def _workspace_account(connection, account):
    from agentbridge.core.oa_daily_digest import _workspace_publication_endpoint

    try:
        owner, account_id = account.get("user_subject"), account.get("account_id")
        if not owner or not account_id:
            raise PermissionError()
        endpoint = _workspace_publication_endpoint(connection, owner, "openclaw", account_id)
        if endpoint != tuple(account.get(key) for key in ("endpoint_id", "endpoint_key", "openclaw_session_key")):
            raise PermissionError()
    except PermissionError as exc:
        raise BusinessWatchError("DIGEST_WORKSPACE_UNAVAILABLE", "当前工作台账号绑定不可用，请刷新后重试。", status=403) from exc
    return account


def _can_configure(connection, subject):
    grant = connection.execute(
        "SELECT permissions_json FROM user_business_grants WHERE user_subject = ?", (subject,),
    ).fetchone()
    return grant is not None and "oa.workflow.read" in json.loads(grant[0])


def _owned_telegram_options(connection, subject):
    from agentbridge.core.oa_daily_digest import check_digest_telegram_endpoint

    routes = {}
    for row in connection.execute(
        """SELECT endpoint_id FROM client_endpoints WHERE user_subject = ?
           AND state = 'active' AND agent_host = 'openclaw' AND client_type = 'telegram'
           ORDER BY created_at, endpoint_id""", (subject,),
    ):
        endpoint_id = row["endpoint_id"]
        try:
            routes[endpoint_id] = check_digest_telegram_endpoint(connection, subject, endpoint_id)
        except PermissionError:
            continue
    return routes


def _options_view(routes):
    names = {}
    for endpoint_id, endpoint in routes.items():
        label = " ".join((endpoint.get("label") or "").split())[:100]
        names[endpoint_id] = f"Telegram 私聊 · {label}" if label and label != "Telegram 私聊" else "Telegram 私聊"
    counts = Counter(names.values())
    return [
        {"id": endpoint_id, "label": f"{label} · {endpoint_id[:8]}" if counts[label] > 1 else label}
        for endpoint_id, label in names.items()
    ]


class OADigestSettings:
    """Save one schedule per Workspace owner. A lease only authorizes its saved version."""

    def __init__(self, service, clock=None):
        from agentbridge.core.oa_daily_digest import initialize_digest_runs

        self.service = service
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with service.tasks._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS oa_digest_settings (
                    user_subject TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    send_time TEXT NOT NULL,
                    send_workspace INTEGER NOT NULL CHECK (send_workspace IN (0, 1)),
                    telegram_endpoint_id TEXT,
                    telegram_binding TEXT,
                    version INTEGER NOT NULL CHECK (version > 0),
                    next_run_at TEXT,
                    lease_token TEXT,
                    lease_until TEXT,
                    last_run_at TEXT,
                    last_status TEXT,
                    updated_at TEXT NOT NULL,
                    CHECK ((telegram_endpoint_id IS NULL) = (telegram_binding IS NULL)),
                    CHECK (enabled = 0 OR send_workspace = 1 OR telegram_endpoint_id IS NOT NULL),
                    CHECK ((enabled = 0) = (next_run_at IS NULL)),
                    CHECK ((lease_token IS NULL) = (lease_until IS NULL)),
                    CHECK (enabled = 1 OR lease_token IS NULL)
                );
                CREATE INDEX IF NOT EXISTS idx_oa_digest_settings_due
                    ON oa_digest_settings (next_run_at, user_subject) WHERE enabled = 1;
            """)
        initialize_digest_runs(service.tasks)

    def _now(self):
        value = self.clock()
        if value.utcoffset() is None:
            raise ValueError("daily digest clock must include a timezone")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _owned(connection, account):
        return connection.execute(
            "SELECT * FROM oa_digest_settings WHERE user_subject = ? AND account_id = ?",
            (account["user_subject"], account["account_id"]),
        ).fetchone()

    @staticmethod
    def _view(row, routes, can_configure):
        telegram_id = row["telegram_endpoint_id"] if row is not None else None
        route = routes.get(telegram_id)
        telegram_available = route is not None and route["_binding_hash"] == row["telegram_binding"]
        can_send = row is not None and can_configure and (row["send_workspace"] or telegram_available)
        return {
            "enabled": bool(row["enabled"]) if row is not None else False,
            "time": row["send_time"] if row is not None else _DEFAULT_TIME,
            "workspace": bool(row["send_workspace"]) if row is not None else True,
            "telegramEndpointId": telegram_id,
            "telegramAvailable": telegram_available,
            "version": row["version"] if row is not None else 0,
            "nextRunAt": row["next_run_at"] if can_send else None,
            "lastRunAt": row["last_run_at"] if row is not None else None,
            "lastStatus": row["last_status"] if row is not None else None,
            "telegramOptions": _options_view(routes),
            "canConfigure": can_configure,
        }

    def get(self, account):
        with self.service.tasks._connect() as connection:
            connection.execute("BEGIN")
            current = _workspace_account(connection, account)
            subject = current["user_subject"]
            return self._view(self._owned(connection, current), _owned_telegram_options(connection, subject),
                              _can_configure(connection, subject))

    def update(self, account, body):
        if not isinstance(body, dict) or set(body) != _FIELDS:
            raise BusinessWatchError("DIGEST_INVALID_SETTINGS", "请提供完整的日报设置及当前版本。")
        enabled, send_time, workspace, telegram_id, expected = (body[field] for field in (
            "enabled", "time", "workspace", "telegramEndpointId", "expectedVersion",
        ))
        if (type(enabled) is not bool or type(workspace) is not bool or type(expected) is not int or expected < 0
                or not isinstance(send_time, str) or not _TIME_FORMAT.fullmatch(send_time)
                or (telegram_id is not None and (not isinstance(telegram_id, str) or not 1 <= len(telegram_id) <= 128))):
            raise BusinessWatchError("DIGEST_INVALID_SETTINGS", "请使用北京时间的有效时分和本人通知渠道。")
        if enabled and not workspace and telegram_id is None:
            raise BusinessWatchError("DIGEST_CHANNEL_REQUIRED", "开启日报前请至少选择一个收件渠道。")

        with store_transaction(self.service.tasks) as connection:
            current = _workspace_account(connection, account)
            subject = current["user_subject"]
            row = self._owned(connection, current)
            version = row["version"] if row is not None else 0
            if expected != version:
                raise BusinessWatchError("DIGEST_VERSION_CONFLICT", "日报设置已更新，请刷新后重试。", status=409)
            can_configure = _can_configure(connection, subject)
            if not can_configure:
                stopping = (row is not None and not enabled and send_time == row["send_time"]
                            and workspace == bool(row["send_workspace"]) and telegram_id == row["telegram_endpoint_id"])
                if not stopping:
                    raise BusinessWatchError("DIGEST_PERMISSION_REQUIRED", "当前账号没有 OA 待办读取权限。", status=403)

            routes = _owned_telegram_options(connection, subject)
            binding = None
            if telegram_id is not None:
                if not enabled and row is not None and row["telegram_endpoint_id"] == telegram_id:
                    # Stopping remains possible after the selected destination expires or changes.
                    binding = row["telegram_binding"]
                else:
                    route = routes.get(telegram_id)
                    if route is None:
                        raise BusinessWatchError("DIGEST_TELEGRAM_UNAVAILABLE", "请选择当前已绑定的本人 Telegram 私聊。", status=409)
                    binding = route["_binding_hash"]

            fields = (int(enabled), send_time, int(workspace), telegram_id, binding)
            if row is not None and fields == tuple(row[field] for field in (
                "enabled", "send_time", "send_workspace", "telegram_endpoint_id", "telegram_binding",
            )):
                return self._view(row, routes, can_configure)

            now = self._now()
            last_run_at = row["last_run_at"] if row is not None else None
            last_status = row["last_status"] if row is not None else None
            already_published = bool(last_run_at and last_status in _PUBLISHED and
                                     datetime.fromisoformat(last_run_at).astimezone(_SHANGHAI).date() ==
                                     now.astimezone(_SHANGHAI).date())
            next_run_at = _next_run(send_time, now, skip_today=already_published) if enabled else None
            connection.execute(
                """INSERT INTO oa_digest_settings (
                     user_subject, account_id, enabled, send_time, send_workspace,
                     telegram_endpoint_id, telegram_binding, version, next_run_at,
                     last_run_at, last_status, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (user_subject) DO UPDATE SET
                     account_id=excluded.account_id, enabled=excluded.enabled,
                     send_time=excluded.send_time, send_workspace=excluded.send_workspace,
                     telegram_endpoint_id=excluded.telegram_endpoint_id, telegram_binding=excluded.telegram_binding,
                     version=excluded.version, next_run_at=excluded.next_run_at,
                     lease_token=NULL, lease_until=NULL,
                     last_run_at=excluded.last_run_at, last_status=excluded.last_status,
                     updated_at=excluded.updated_at""",
                (subject, current["account_id"], *fields, version + 1, next_run_at, last_run_at, last_status, now.isoformat()),
            )
            return self._view(self._owned(connection, current), routes, can_configure)

    def _claim_due(self, now):
        start = datetime.combine(now.astimezone(_SHANGHAI).date(), time.min, tzinfo=_SHANGHAI)
        start_at = start.astimezone(timezone.utc).isoformat()
        now_at = now.isoformat()
        with store_transaction(self.service.tasks) as connection:
            # Coalesce older dates on restart. A job may recover within its own date only.
            missed = connection.execute(
                """SELECT user_subject, send_time FROM oa_digest_settings
                   WHERE enabled = 1 AND next_run_at < ? AND (lease_until IS NULL OR lease_until <= ?)
                   ORDER BY next_run_at, user_subject LIMIT 100""", (start_at, now_at),
            ).fetchall()
            for row in missed:
                connection.execute(
                    """UPDATE oa_digest_settings SET next_run_at = ?, lease_token = NULL, lease_until = NULL
                       WHERE user_subject = ?""", (_next_run(row["send_time"], now), row["user_subject"]),
                )
            row = connection.execute(
                """SELECT * FROM oa_digest_settings WHERE enabled = 1 AND next_run_at >= ? AND next_run_at <= ?
                     AND (lease_until IS NULL OR lease_until <= ?) ORDER BY next_run_at, user_subject LIMIT 1""",
                (start_at, now_at, now_at),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid4())
            expires = (now + timedelta(seconds=_LEASE_SECONDS)).isoformat()
            connection.execute("UPDATE oa_digest_settings SET lease_token = ?, lease_until = ? WHERE user_subject = ?",
                               (token, expires, row["user_subject"]))
            return {**dict(row), "lease_token": token, "lease_until": expires}

    def _finish(self, setting, status, claimed_at, now):
        if status == "stale":
            # A changed configuration has already cleared its lease. Otherwise let the
            # bounded lease expire so a new worker can retry without spinning this cycle.
            return False
        scheduled_date = datetime.fromisoformat(setting["next_run_at"]).astimezone(_SHANGHAI).date()
        same_day = scheduled_date == now.astimezone(_SHANGHAI).date()
        with store_transaction(self.service.tasks) as connection:
            updated = connection.execute(
                """UPDATE oa_digest_settings SET next_run_at = ?, lease_token = NULL, lease_until = NULL,
                     last_run_at = ?, last_status = ?
                   WHERE user_subject = ? AND account_id = ? AND enabled = 1 AND version = ?
                     AND next_run_at = ? AND lease_token = ? AND lease_until > ?""",
                (_next_run(setting["send_time"], now, skip_today=same_day), claimed_at.isoformat(), status,
                 setting["user_subject"], setting["account_id"], setting["version"],
                 setting["next_run_at"], setting["lease_token"], now.isoformat()),
            )
            return updated.rowcount == 1

    def run_due(self, limit=4, should_stop=None):
        """Claim bounded due schedules without holding a writer during OA collection."""
        from agentbridge.core.oa_daily_digest import publish_scheduled_oa_digest

        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("daily digest cycle limit must be 1 through 20")
        stats = {"claimed": 0, "published": 0, "partial": 0, "reused": 0, "failed": 0, "discarded": 0}
        for _ in range(limit):
            if should_stop is not None and should_stop():
                break
            now = self._now()
            setting = self._claim_due(now)
            if setting is None:
                break
            stats["claimed"] += 1
            try:
                status = publish_scheduled_oa_digest(self.service, setting, now=now, clock=self._now)["status"]
                if status not in _OUTCOMES:
                    raise ValueError("invalid digest publication outcome")
            except PermissionError:
                status = "permission_unavailable"
            except Exception as exc:
                _LOG.warning("OA digest publication failed: %s", type(exc).__name__)
                status = "failed"
            if self._finish(setting, status, now, self._now()):
                stats[status if status in _PUBLISHED else "failed"] += 1
            else:
                stats["discarded"] += 1
        return stats
