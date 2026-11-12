"""Publish one OA daily digest and expose its exact delivery receipt."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
import json
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from agentbridge.core.actionable_results import ActionableResultError, _telegram_endpoint
from agentbridge.core.oa_daily_digest_report import collect_oa_daily_digest
from agentbridge.core.tasks import TaskIntegrityError


_TIMEZONE = ZoneInfo("Asia/Shanghai")
_KEY_PREFIX = "oa-daily-digest:"
_METADATA_KEY = "oaDailyDigest"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _message_key(local_date: str, trial: bool) -> str:
    if type(trial) is not bool:
        raise ValueError("日报类型无效。")
    if date.fromisoformat(local_date).isoformat() != local_date:
        raise ValueError("日报日期须为 YYYY-MM-DD。")
    return f"{_KEY_PREFIX}{'trial' if trial else 'scheduled'}:{local_date}"


def _telegram_account(value):
    return "default" if value is None or value.strip() in {"", "default"} else value


def _workspace_publication_endpoint(connection, user_subject, agent_host, account_id=None):
    workspace = connection.execute(
        """SELECT endpoint.endpoint_id, endpoint.endpoint_key, endpoint.conversation_ref
           FROM workspace_accounts AS account
           JOIN client_endpoints AS endpoint ON endpoint.endpoint_id = account.endpoint_id
           WHERE account.user_subject = ? AND account.state = 'active'
             AND endpoint.user_subject = account.user_subject AND endpoint.state = 'active'
             AND endpoint.agent_host = ? AND endpoint.client_type = 'web'
             AND endpoint.endpoint_key = account.endpoint_key
             AND endpoint.conversation_ref = account.openclaw_session_key
             AND endpoint.external_subject = account.account_id
             AND (? IS NULL OR account.account_id = ?)""",
        (user_subject, agent_host, account_id, account_id),
    ).fetchone()
    if workspace is None:
        raise PermissionError("没有可用的本人工作台端点。")
    return tuple(workspace)


def check_digest_telegram_endpoint(connection, user_subject, endpoint_id):
    """Resolve one currently bound, same-owner Telegram private destination."""
    try:
        checked = _telegram_endpoint(connection, user_subject, endpoint_id)
    except ActionableResultError as exc:
        raise PermissionError("没有可用的本人 Telegram 私聊端点。") from exc
    account = _telegram_account(checked["account_id"])
    sender = checked["external_subject"]
    route_account = _telegram_account(json.loads(checked["route_json"]).get("accountId"))
    if route_account != account or checked["endpoint_key"] not in {
        f"telegram:{account}:{sender}", f"telegram:*:{sender}",
    }:
        raise PermissionError("Telegram 端点与原绑定的收件人或机器人账号不一致。")
    if not set(json.loads(checked["capabilities_json"])) & {"direct_status", "timeline_message"}:
        raise PermissionError("本人 Telegram 私聊端点未启用消息投递。")
    return checked


def _publication_route(connection, user_subject, agent_host, telegram_endpoint_key):
    workspace = _workspace_publication_endpoint(connection, user_subject, agent_host)
    recipient = connection.execute(
        """SELECT endpoint_id FROM client_endpoints
           WHERE user_subject = ? AND agent_host = ? AND endpoint_key = ?""",
        (user_subject, agent_host, telegram_endpoint_key),
    ).fetchone()
    if recipient is None:
        raise PermissionError("没有可用的本人 Telegram 私聊端点。")
    checked = check_digest_telegram_endpoint(connection, user_subject, recipient["endpoint_id"])
    return workspace, checked["endpoint_id"], checked["_binding_hash"]


def _grant_revision(connection, user_subject):
    grant = connection.execute(
        "SELECT revision, permissions_json FROM user_business_grants WHERE user_subject = ?",
        (user_subject,),
    ).fetchone()
    if grant is None or "oa.workflow.read" not in json.loads(grant["permissions_json"]):
        raise PermissionError("没有 OA 流程查询权限。")
    return grant["revision"]


def _oa_authority(connection, user_subject):
    session = connection.execute(
        """SELECT session_id, authority_generation, expected_principal_ref, downstream_principal_ref
           FROM sessions WHERE user_subject = ? AND system_id = 'oa'""",
        (user_subject,),
    ).fetchone()
    return tuple(session) if session is not None else None


def _delivery_binding(service, route, grant_revision, oa_authority, *, settings_version=None):
    material = [route, grant_revision, service.business_watch.system_instance, oa_authority]
    if settings_version is not None:
        material.append(settings_version)
    snapshot = json.dumps(material,
                          ensure_ascii=False, separators=(",", ":"))
    return sha256(snapshot.encode()).hexdigest()


def _has_table(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,),
    ).fetchone() is not None


def _setting_for_owner(connection, owner):
    if not _has_table(connection, "oa_digest_settings"):
        return None
    return connection.execute(
        "SELECT * FROM oa_digest_settings WHERE user_subject = ?", (owner,),
    ).fetchone()


def _require_legacy_schedule(connection, owner, trial):
    if not trial and _setting_for_owner(connection, owner) is not None:
        raise PermissionError("此账号的日报由 Workspace 设置管理。")


def is_current_digest_destination(service, delivery) -> bool:
    """Verify the stored destination for a digest; ordinary delivery is unchanged."""
    payload = delivery["payload"]
    if delivery["payload_type"] != "timeline_message" or "targetEndpointBinding" not in payload:
        return True
    binding = payload["targetEndpointBinding"]
    if not isinstance(binding, str) or payload.get("entryId") != delivery["event_id"]:
        return False
    owner = delivery["user_subject"]
    with service.tasks._connect() as connection:
        connection.execute("BEGIN")
        setting = _setting_for_owner(connection, owner)
        run = None
        if setting is not None:
            run = connection.execute(
                """SELECT settings_version FROM oa_digest_runs
                   WHERE user_subject = ? AND telegram_delivery_id = ?""",
                (owner, delivery["delivery_id"]),
            ).fetchone()
            if run is None and connection.execute(
                """SELECT 1 FROM user_timeline WHERE user_subject = ? AND entry_id = ?
                   AND dedupe_key GLOB ?""",
                (owner, delivery["event_id"], f"message:{_KEY_PREFIX}scheduled:*"),
            ).fetchone():
                # Queued legacy recurring sends are superseded by self-service settings.
                return False
        endpoint = connection.execute(
            """SELECT agent_host, endpoint_key FROM client_endpoints
               WHERE endpoint_id = ? AND user_subject = ?""",
            (delivery["endpoint_id"], owner),
        ).fetchone()
        if endpoint is None:
            return False
        try:
            if run is not None:
                if (not setting["enabled"] or setting["version"] != run["settings_version"]
                        or setting["telegram_endpoint_id"] != delivery["endpoint_id"]):
                    return False
                workspace = _workspace_publication_endpoint(connection, owner, "openclaw", setting["account_id"])
                recipient = _scheduled_telegram(connection, setting)
                if recipient is None:
                    return False
                route = workspace, *recipient
            else:
                route = _publication_route(connection, owner, endpoint["agent_host"], endpoint["endpoint_key"])
            grant_revision = _grant_revision(connection, owner)
            oa_authority = _oa_authority(connection, owner)
        except PermissionError:
            return False
    return compare_digest(binding, _delivery_binding(
        service, route, grant_revision, oa_authority,
        settings_version=run["settings_version"] if run is not None else None,
    ))


def _find_entry(connection, user_subject, *, message_key=None, digest_id=None):
    field, value = ("entry_id", digest_id) if digest_id is not None else ("dedupe_key", f"message:{message_key}")
    return connection.execute(
        f"""SELECT entry_id, dedupe_key, created_at, payload_json FROM user_timeline
            WHERE user_subject = ? AND entry_type = 'chat_message' AND role = 'assistant'
              AND {field} = ? AND dedupe_key GLOB ?""",
        (user_subject, value, f"message:{_KEY_PREFIX}*"),
    ).fetchone()


def _legacy_watch_watermark(connection, owner):
    return connection.execute(
        """SELECT MAX(json_extract(payload_json, '$.oaDailyDigest.watchWatermark')) FROM user_timeline
           WHERE user_subject = ? AND entry_type = 'chat_message' AND role = 'assistant'
             AND dedupe_key GLOB ?""",
        (owner, f"message:{_KEY_PREFIX}*"),
    ).fetchone()[0]


def _receipt(connection, entry, *, user_subject, agent_host, telegram_endpoint_key, status):
    metadata = json.loads(entry["payload_json"]).get(_METADATA_KEY)
    if not isinstance(metadata, dict):
        return None
    if entry["dedupe_key"] != f"message:{_message_key(metadata['localDate'], metadata['trial'])}":
        raise TaskIntegrityError("daily digest metadata does not match its timeline key")
    delivery = connection.execute(
        """SELECT outbox.delivery_id, outbox.state, outbox.acknowledged_at, outbox.attempt_count
           FROM notification_outbox AS outbox
           JOIN client_endpoints AS endpoint ON endpoint.endpoint_id = outbox.endpoint_id
           WHERE outbox.event_id = ? AND outbox.user_subject = ? AND outbox.payload_type = 'timeline_message'
             AND endpoint.user_subject = outbox.user_subject AND endpoint.client_type = 'telegram'
             AND endpoint.agent_host = ? AND endpoint.endpoint_key = ?""",
        (entry["entry_id"], user_subject, agent_host, telegram_endpoint_key),
    ).fetchone()
    if delivery is None:
        return None
    return {
        "status": status,
        "digestId": entry["entry_id"],
        "localDate": metadata["localDate"],
        "trial": metadata["trial"],
        "timelineEntryId": entry["entry_id"],
        "deliveryId": delivery["delivery_id"],
        "deliveryState": delivery["state"],
        "attemptCount": delivery["attempt_count"],
        "publishedAt": entry["created_at"],
        "acknowledgedAt": delivery["acknowledged_at"],
    }


def publish_oa_daily_digest(service, *, user_subject, agent_host, telegram_endpoint_key, trial: bool) -> dict:
    now = _now()
    local_date = now.astimezone(_TIMEZONE).date().isoformat()
    message_key = _message_key(local_date, trial)
    receipt_scope = dict(user_subject=user_subject, agent_host=agent_host, telegram_endpoint_key=telegram_endpoint_key)

    def publication_receipt(connection, entry, status):
        receipt = _receipt(connection, entry, status=status, **receipt_scope)
        if receipt is None:
            raise TaskIntegrityError("daily digest is not bound to the requested Telegram delivery")
        return receipt

    with service.tasks._connect() as connection:
        _require_legacy_schedule(connection, user_subject, trial)
        existing = _find_entry(connection, user_subject, message_key=message_key)
        if existing is not None:
            return publication_receipt(connection, existing, "reused")
        route = _publication_route(connection, **receipt_scope)
        grant_revision = _grant_revision(connection, user_subject)
        oa_authority = _oa_authority(connection, user_subject)
        watermark = _legacy_watch_watermark(connection, user_subject)
    since = min(now, datetime.fromisoformat(watermark)) if watermark else now - timedelta(days=1)

    # Business reads can take time. No writer transaction is held while they run.
    report = collect_oa_daily_digest(service, user_subject=user_subject, now=now, since=since, trial=trial)
    watch_watermark = now if report.watch_coverage_complete else since

    with service.tasks._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_legacy_schedule(connection, user_subject, trial)
        existing = _find_entry(connection, user_subject, message_key=message_key)
        if existing is not None:
            return publication_receipt(connection, existing, "reused")
        try:
            current_route = _publication_route(connection, **receipt_scope)
        except PermissionError as exc:
            raise PermissionError("发布前工作台或 Telegram 会话绑定发生变化，请重新核对。") from exc
        if current_route != route:
            raise PermissionError("发布前工作台或 Telegram 会话绑定发生变化，请重新核对。")
        if _grant_revision(connection, user_subject) != grant_revision:
            raise PermissionError("发布前 OA 权限发生变化，请重新核对。")
        if _oa_authority(connection, user_subject) != oa_authority:
            raise PermissionError("发布前 OA 会话身份发生变化，请重新核对。")
        workspace, telegram_endpoint_id, _ = route
        entry, _ = service.tasks._append_timeline_message_in_connection(
            connection,
            user_subject=user_subject,
            source_endpoint_id=workspace[0],
            message_key=message_key,
            role="assistant",
            text=report.text,
            payload={_METADATA_KEY: {
                "localDate": local_date,
                "trial": trial,
                "watchWatermark": watch_watermark.astimezone(timezone.utc).isoformat(),
            }},
            target_endpoint_id=telegram_endpoint_id,
            target_endpoint_binding=_delivery_binding(service, route, grant_revision, oa_authority),
        )
        recorded = _find_entry(connection, user_subject, digest_id=entry["entry_id"])
        return publication_receipt(connection, recorded, "published")


def initialize_digest_runs(tasks) -> None:
    """Keep a daily receipt and watch watermark even when the report is private."""
    with tasks._connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS oa_digest_runs (
                user_subject TEXT NOT NULL,
                local_date TEXT NOT NULL,
                settings_version INTEGER NOT NULL,
                published_at TEXT NOT NULL,
                watch_watermark TEXT NOT NULL,
                timeline_entry_id TEXT,
                telegram_delivery_id TEXT UNIQUE,
                PRIMARY KEY (user_subject, local_date),
                CHECK (timeline_entry_id IS NOT NULL OR telegram_delivery_id IS NOT NULL)
            )
        """)


def _current_scheduled_setting(connection, claimed, now, local_date):
    row = _setting_for_owner(connection, claimed["user_subject"])
    if (row is None or not row["enabled"] or not row["lease_token"] or not row["lease_until"]
            or not row["next_run_at"]
            or any(row[key] != claimed[key] for key in (
                "account_id", "version", "lease_token", "next_run_at",
            ))
            or datetime.fromisoformat(row["lease_until"]) <= now):
        return None
    due = datetime.fromisoformat(row["next_run_at"])
    if due > now or not (due.astimezone(_TIMEZONE).date() == now.astimezone(_TIMEZONE).date() == local_date):
        return None
    return row


def _scheduled_telegram(connection, setting):
    endpoint_id = setting["telegram_endpoint_id"]
    if endpoint_id is None:
        return None
    try:
        recipient = check_digest_telegram_endpoint(connection, setting["user_subject"], endpoint_id)
    except PermissionError:
        return None
    if recipient["_binding_hash"] != setting["telegram_binding"]:
        return None
    return endpoint_id, recipient["_binding_hash"]


def _published_scheduled_receipt(connection, owner, local_date):
    run = connection.execute(
        """SELECT published_at, timeline_entry_id, telegram_delivery_id
           FROM oa_digest_runs WHERE user_subject = ? AND local_date = ?""",
        (owner, local_date.isoformat()),
    ).fetchone()
    if run is not None:
        return {
            "status": "reused", "localDate": local_date.isoformat(), "publishedAt": run["published_at"],
            "timelineEntryId": run["timeline_entry_id"], "deliveryId": run["telegram_delivery_id"],
        }
    # A legacy scheduler may already have sent today's digest. A trial is a
    # separate receipt and never occupies the scheduled date.
    entry = _find_entry(connection, owner, message_key=_message_key(local_date.isoformat(), False))
    if entry is not None:
        return {
            "status": "reused", "localDate": local_date.isoformat(), "publishedAt": entry["created_at"],
            "timelineEntryId": entry["entry_id"],
        }
    return None


def _scheduled_watch_since(connection, owner, now):
    run_watermark = connection.execute(
        "SELECT MAX(watch_watermark) FROM oa_digest_runs WHERE user_subject = ?", (owner,),
    ).fetchone()[0]
    watermarks = [datetime.fromisoformat(value) for value in
                  (run_watermark, _legacy_watch_watermark(connection, owner)) if value]
    return min(now, max(watermarks)) if watermarks else now - timedelta(days=1)


def _queue_private_telegram_digest(service, connection, owner, workspace, telegram, report, binding, now):
    # The outbox event ID need not reference the shared timeline. In
    # Telegram-only mode the body exists solely in the exact recipient's queue.
    event_id = str(uuid4())
    service.tasks._enqueue_timeline_message(
        connection,
        entry={
            "entry_id": event_id, "user_subject": owner, "sequence": None, "role": "assistant",
            "text": report.text, "payload_json": "{}", "task_id": None, "created_at": now,
        },
        source_endpoint_id=workspace[0], target_endpoint_id=telegram[0],
        target_endpoint_binding=binding, timeline_visible=False,
    )
    return event_id


def publish_scheduled_oa_digest(service, setting, *, now, clock=None) -> dict:
    """Use a current Workspace schedule lease to publish exactly the selected channels."""
    clock = clock or _now
    owner = setting["user_subject"]
    local_date = now.astimezone(_TIMEZONE).date()

    def outcome(status):
        return {"status": status, "localDate": local_date.isoformat()}

    with service.tasks._connect() as connection:
        connection.execute("BEGIN")
        current = _current_scheduled_setting(connection, setting, now, local_date)
        if current is None:
            return outcome("stale")
        previous = _published_scheduled_receipt(connection, owner, local_date)
        if previous is not None:
            return previous
        try:
            grant_revision = _grant_revision(connection, owner)
        except PermissionError:
            return outcome("permission_unavailable")
        try:
            workspace = _workspace_publication_endpoint(connection, owner, "openclaw", current["account_id"])
        except PermissionError:
            return outcome("endpoint_unavailable")
        telegram = _scheduled_telegram(connection, current)
        if not current["send_workspace"] and telegram is None:
            return outcome("endpoint_unavailable")
        oa_authority = _oa_authority(connection, owner)
        since = _scheduled_watch_since(connection, owner, now)

    report = collect_oa_daily_digest(service, user_subject=owner, now=now, since=since, trial=False)
    watermark = now if report.watch_coverage_complete else since

    with service.tasks._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        finished_at = clock()
        current = _current_scheduled_setting(connection, setting, finished_at, local_date)
        if current is None:
            return outcome("stale")
        previous = _published_scheduled_receipt(connection, owner, local_date)
        if previous is not None:
            return previous
        try:
            if (_grant_revision(connection, owner) != grant_revision
                    or _oa_authority(connection, owner) != oa_authority):
                return outcome("permission_unavailable")
        except PermissionError:
            return outcome("permission_unavailable")
        try:
            if _workspace_publication_endpoint(connection, owner, "openclaw", current["account_id"]) != workspace:
                return outcome("endpoint_unavailable")
        except PermissionError:
            return outcome("endpoint_unavailable")
        if _scheduled_telegram(connection, current) != telegram:
            telegram = None
        if not current["send_workspace"] and telegram is None:
            return outcome("endpoint_unavailable")

        binding = None
        if telegram is not None:
            binding = _delivery_binding(service, (workspace, *telegram), grant_revision, oa_authority,
                                        settings_version=current["version"])
        timeline_entry_id = None
        published_at = finished_at.astimezone(timezone.utc).isoformat()
        if current["send_workspace"]:
            entry, _ = service.tasks._append_timeline_message_in_connection(
                connection, user_subject=owner, source_endpoint_id=workspace[0],
                message_key=_message_key(local_date.isoformat(), False), role="assistant", text=report.text,
                payload={_METADATA_KEY: {
                    "localDate": local_date.isoformat(), "trial": False,
                    "watchWatermark": watermark.astimezone(timezone.utc).isoformat(),
                }},
                target_endpoint_id=telegram[0] if telegram is not None else None,
                target_endpoint_binding=binding, enqueue_notifications=telegram is not None,
            )
            timeline_entry_id = entry["entry_id"]
        delivery_id = None
        if telegram is not None:
            event_id = timeline_entry_id or _queue_private_telegram_digest(
                service, connection, owner, workspace, telegram, report, binding, published_at,
            )
            delivery = connection.execute(
                """SELECT delivery_id FROM notification_outbox
                   WHERE user_subject = ? AND event_id = ? AND endpoint_id = ? AND payload_type = 'timeline_message'""",
                (owner, event_id, telegram[0]),
            ).fetchone()
            if delivery is None:
                raise TaskIntegrityError("daily digest did not queue its selected Telegram delivery")
            delivery_id = delivery["delivery_id"]
        connection.execute(
            """INSERT INTO oa_digest_runs (
                user_subject, local_date, settings_version, published_at, watch_watermark,
                timeline_entry_id, telegram_delivery_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (owner, local_date.isoformat(), current["version"], published_at,
             watermark.astimezone(timezone.utc).isoformat(), timeline_entry_id, delivery_id),
        )
        status = "partial" if current["telegram_endpoint_id"] and delivery_id is None else "published"
        return {
            **outcome(status), "publishedAt": published_at,
            "timelineEntryId": timeline_entry_id, "deliveryId": delivery_id,
        }


def get_oa_daily_digest_receipt(
    service, *, user_subject, agent_host, telegram_endpoint_key,
    digest_id: str | None = None, local_date: str | None = None, trial: bool = False,
) -> dict:
    if digest_id is not None:
        if local_date is not None or str(UUID(digest_id)) != digest_id:
            raise ValueError("请提供一个有效的日报编号或日期。")
        message_key = None
    else:
        local_date = local_date or _now().astimezone(_TIMEZONE).date().isoformat()
        message_key = _message_key(local_date, trial)
    with service.tasks._connect() as connection:
        entry = _find_entry(connection, user_subject, message_key=message_key, digest_id=digest_id)
        if entry is not None:
            receipt = _receipt(connection, entry, user_subject=user_subject, agent_host=agent_host,
                               telegram_endpoint_key=telegram_endpoint_key, status="succeeded")
            if receipt is not None:
                return receipt
    return {"status": "not_found"}
