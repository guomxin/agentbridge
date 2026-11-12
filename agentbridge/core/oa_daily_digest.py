"""Publish one OA daily digest and expose its exact delivery receipt."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
import json
from uuid import UUID
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


def _publication_route(connection, user_subject, agent_host, telegram_endpoint_key):
    workspace = connection.execute(
        """SELECT endpoint.endpoint_id, endpoint.endpoint_key, endpoint.conversation_ref
           FROM workspace_accounts AS account
           JOIN client_endpoints AS endpoint ON endpoint.endpoint_id = account.endpoint_id
           WHERE account.user_subject = ? AND account.state = 'active'
             AND endpoint.user_subject = account.user_subject AND endpoint.state = 'active'
             AND endpoint.agent_host = ? AND endpoint.client_type = 'web'
             AND endpoint.endpoint_key = account.endpoint_key
             AND endpoint.conversation_ref = account.openclaw_session_key
             AND endpoint.external_subject = account.account_id""",
        (user_subject, agent_host),
    ).fetchone()
    if workspace is None:
        raise PermissionError("没有可用的本人工作台端点。")
    recipient = connection.execute(
        """SELECT endpoint_id, capabilities_json FROM client_endpoints
           WHERE user_subject = ? AND agent_host = ? AND endpoint_key = ?""",
        (user_subject, agent_host, telegram_endpoint_key),
    ).fetchone()
    if recipient is None:
        raise PermissionError("没有可用的本人 Telegram 私聊端点。")
    try:
        checked = _telegram_endpoint(connection, user_subject, recipient["endpoint_id"])
    except ActionableResultError as exc:
        raise PermissionError("没有可用的本人 Telegram 私聊端点。") from exc
    account = _telegram_account(checked["account_id"])
    sender = checked["external_subject"]
    route_account = _telegram_account(json.loads(checked["route_json"]).get("accountId"))
    if route_account != account or telegram_endpoint_key not in {f"telegram:{account}:{sender}", f"telegram:*:{sender}"}:
        raise PermissionError("Telegram 端点与原绑定的收件人或机器人账号不一致。")
    if not set(json.loads(recipient["capabilities_json"])) & {"direct_status", "timeline_message"}:
        raise PermissionError("本人 Telegram 私聊端点未启用消息投递。")
    return tuple(workspace), recipient["endpoint_id"], checked["_binding_hash"]


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


def _delivery_binding(service, route, grant_revision, oa_authority):
    snapshot = json.dumps([route, grant_revision, service.business_watch.system_instance, oa_authority],
                          ensure_ascii=False, separators=(",", ":"))
    return sha256(snapshot.encode()).hexdigest()


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
        endpoint = connection.execute(
            """SELECT agent_host, endpoint_key FROM client_endpoints
               WHERE endpoint_id = ? AND user_subject = ?""",
            (delivery["endpoint_id"], owner),
        ).fetchone()
        if endpoint is None:
            return False
        try:
            route = _publication_route(connection, owner, endpoint["agent_host"], endpoint["endpoint_key"])
            grant_revision = _grant_revision(connection, owner)
            oa_authority = _oa_authority(connection, owner)
        except PermissionError:
            return False
    return compare_digest(binding, _delivery_binding(service, route, grant_revision, oa_authority))


def _find_entry(connection, user_subject, *, message_key=None, digest_id=None):
    field, value = ("entry_id", digest_id) if digest_id is not None else ("dedupe_key", f"message:{message_key}")
    return connection.execute(
        f"""SELECT entry_id, dedupe_key, created_at, payload_json FROM user_timeline
            WHERE user_subject = ? AND entry_type = 'chat_message' AND role = 'assistant'
              AND {field} = ? AND dedupe_key GLOB ?""",
        (user_subject, value, f"message:{_KEY_PREFIX}*"),
    ).fetchone()


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
        existing = _find_entry(connection, user_subject, message_key=message_key)
        if existing is not None:
            return publication_receipt(connection, existing, "reused")
        route = _publication_route(connection, **receipt_scope)
        grant_revision = _grant_revision(connection, user_subject)
        oa_authority = _oa_authority(connection, user_subject)
        watermark = connection.execute(
            """SELECT MAX(json_extract(payload_json, '$.oaDailyDigest.watchWatermark')) FROM user_timeline
               WHERE user_subject = ? AND entry_type = 'chat_message' AND role = 'assistant'
                 AND dedupe_key GLOB ?""",
            (user_subject, f"message:{_KEY_PREFIX}*"),
        ).fetchone()[0]
    since = min(now, datetime.fromisoformat(watermark)) if watermark else now - timedelta(days=1)

    # Business reads can take time. No writer transaction is held while they run.
    report = collect_oa_daily_digest(service, user_subject=user_subject, now=now, since=since, trial=trial)
    watch_watermark = now if report.watch_coverage_complete else since

    with service.tasks._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
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
