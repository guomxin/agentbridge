"""Consume only original terminal events for a centrally verified Telegram private endpoint."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import ClassVar
from .chat_mirror import ChatMirrors, chat_claim, identifier, projected_chat

TERMINAL = {
    "task.completed": "succeeded",
    "task.operation.succeeded": "succeeded",
    "task.operation.failed": "failed",
    "task.failed": "failed",
    "task.canceled": "canceled",
}


async def notifications(host, record, action, body):
    from aiohttp import web

    from .telegram_identity import verify_telegram_binding
    from .worker import authenticated_session

    if not host.coordinator or not record.get("workspaceEndpointId"):
        return (
            {"deliveries": []}
            if action == "telegram-notifications"
            else {"recorded": False}
        )
    req = host.worker_payload(record, None)
    async with authenticated_session(req) as (session, meta, level):
        if level != "L3" or not await verify_telegram_binding(
            session, req["instance"], req["subject"], meta, req["endpointKey"]
        ):
            raise web.HTTPForbidden()
        endpoint_params = {"agent_host": "nanobot", "endpoint_key": req["endpointKey"]}

        async def ack(delivery, attempt, succeeded):
            reply = await session.call_tool(
                "agentbridge_host_notification_ack",
                {
                    **endpoint_params,
                    "delivery_id": delivery,
                    "attempt_count": attempt,
                    "succeeded": succeeded,
                    "retry_after_seconds": 5,
                },
                meta=meta,
            )
            data = (reply.structuredContent or {}).get("delivery") or {}
            return (
                not reply.isError
                and data.get("deliveryId") == delivery
                and data.get("attemptCount") == attempt
            )

        path = Path(req["userDir"]) / "coordinated-runs.sqlite3"
        if action in ("telegram-chat-validate", "telegram-chat-receipt"):
            delivery, attempt = body.get("deliveryId"), body.get("attempt")
            if not identifier(delivery) or type(attempt) is not int or not 1 <= attempt <= 5:
                raise web.HTTPBadRequest()
            if not chat_claim(req["userDir"], req["endpoint"], delivery, attempt):
                raise web.HTTPForbidden()
            if action == "telegram-chat-validate":
                result = await session.call_tool("agentbridge_host_notification_validate", {
                    **endpoint_params, "delivery_id": delivery, "attempt_count": attempt}, meta=meta)
                return {"allowed": not result.isError and (result.structuredContent or {}).get("allowed") is True}
            if type(body.get("delivered")) is not bool:
                raise web.HTTPBadRequest()
            return {"recorded": await ack(delivery, attempt, body["delivered"])}
        if path.exists():
            with sqlite3.connect(path) as db:
                db.execute("""CREATE TABLE IF NOT EXISTS notification_claims (
                    delivery TEXT PRIMARY KEY, endpoint TEXT NOT NULL, run TEXT NOT NULL, attempt INTEGER NOT NULL)""")
        if action == "telegram-notification-receipt":
            delivery, attempt, ok = (
                body.get("deliveryId"),
                body.get("attempt"),
                body.get("delivered"),
            )
            if (
                type(attempt) is not int
                or not 1 <= attempt <= 5
                or type(ok) is not bool
                or not isinstance(delivery, str)
            ):
                raise web.HTTPBadRequest()
            if not path.exists():
                raise web.HTTPForbidden()
            with sqlite3.connect(path) as db:
                claim = db.execute(
                    "SELECT run FROM notification_claims WHERE delivery=? AND endpoint=? AND attempt=?",
                    (delivery, req["endpoint"], attempt),
                ).fetchone()
            if not claim or claim[0] != body.get("runId"):
                raise web.HTTPForbidden()
            return {"recorded": await ack(delivery, attempt, ok)}
        reply = await session.call_tool(
            "agentbridge_host_notification_claim",
            {
                **endpoint_params,
                "limit": 32,
                "lease_seconds": 60,
                "include_workspace_chat": True,
            },
            meta=meta,
        )
        payload = reply.structuredContent or {}
        if (
            reply.isError
            or payload.get("endpoint", {}).get("endpointId") != req["endpoint"]
        ):
            raise web.HTTPForbidden()
        deliveries = []
        for item in payload.get("notifications", []):
            delivery, attempt = item.get("deliveryId"), item.get("attemptCount")
            if not identifier(delivery) or type(attempt) is not int or not 1 <= attempt <= 5:
                continue
            if item.get("deliveryMode") == "timeline_message":
                message = projected_chat(item, req["workspaceEndpointId"])
                if message and chat_claim(req["userDir"], req["endpoint"], delivery, attempt, register=True):
                    deliveries.append({"kind": "workspace_chat", "deliveryId": delivery, "attempt": attempt, **message})
                continue
            task, event = item.get("task") or {}, item.get("event") or {}
            if (
                item.get("deliveryMode") != "origin_handled"
                or task.get("userSubject") != req["subject"]
                or task.get("agentHost") != "nanobot"
                or task.get("activeConversationRef") != req["conversation"]
                or task.get("originEndpointId") != req["endpoint"]
            ):
                continue
            if not path.exists():
                continue
            with sqlite3.connect(path) as db:
                local = db.execute(
                    "SELECT run_id FROM tasks WHERE endpoint=? AND task_id=?",
                    (req["endpoint"], task["taskId"]),
                ).fetchone()
            if not local:
                continue
            kind = event.get("eventType")
            if kind not in TERMINAL:
                # Nonterminal origin events are already presented in the original turn.
                await ack(delivery, attempt, True)
                continue
            if kind.startswith("task.operation.") and (event.get("payload") or {}).get(
                "operationId"
            ) != task.get("currentOperationId"):
                await ack(delivery, attempt, True)
                continue
            if task.get("status") != TERMINAL[kind]:
                continue
            with sqlite3.connect(path) as db:
                db.execute(
                    """INSERT INTO notification_claims VALUES(?,?,?,?)
                        ON CONFLICT(delivery) DO UPDATE SET attempt=excluded.attempt
                        WHERE endpoint=excluded.endpoint AND run=excluded.run""",
                    (delivery, req["endpoint"], local[0], attempt),
                )
            deliveries.append(
                {
                    "deliveryId": delivery,
                    "attempt": attempt,
                    "runId": local[0],
                    "status": TERMINAL[kind],
                }
            )
    return {"deliveries": deliveries}


class TerminalMessages:
    TEXT: ClassVar[dict[str, str]] = {
        "succeeded": "原任务已完成。",
        "failed": "原任务未完成，请在工作台查看状态。",
        "canceled": "原任务已取消。",
    }

    def __init__(self, pilot):
        self.pilot = pilot
        self.chat = ChatMirrors(pilot)
        with pilot.db:
            pilot.db.execute("""CREATE TABLE IF NOT EXISTS notification_receipts(
                    bot INTEGER, chat INTEGER, delivery TEXT, run TEXT, state TEXT NOT NULL,
                    PRIMARY KEY(bot,chat,delivery))""")
            pilot.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS terminal_once_per_run ON notification_receipts(bot,chat,run)"
            )
            pilot.db.execute(
                "UPDATE notification_receipts SET state='delivery_unknown' WHERE state='sending'"
            )

    def poll(self, chat, binding, digest):
        from .telegram import TelegramUnavailable, numeric

        pilot = self.pilot
        reply = pilot.gateway._request("telegram-notifications", {"binding": binding})
        for event in reply.get("deliveries", []):
            if event.get("kind") == "workspace_chat":
                self.chat.poll(chat, binding, digest, event)
                continue
            if event.get("status") not in self.TEXT:
                continue
            original = pilot.db.execute(
                """SELECT message FROM messages WHERE bot=? AND chat=? AND subject=?
                        AND binding_hash=? AND run_id=? AND state IN ('delivered','delivery_unknown')""",
                (pilot.bot_id, chat, binding["user_subject"], digest, event["runId"]),
            ).fetchone()
            if not original:
                continue
            delivery = event["deliveryId"]
            with pilot.db:
                inserted = pilot.db.execute(
                    "INSERT OR IGNORE INTO notification_receipts VALUES(?,?,?,?,'sending')",
                    (pilot.bot_id, chat, delivery, event["runId"]),
                ).rowcount
            if inserted:
                status = "delivery_unknown"
                try:
                    active, proof = pilot._binding(chat)
                    if active != binding or proof != digest:
                        status = "binding_changed"
                    else:
                        result = pilot.api.call(
                            "sendMessage",
                            chat_id=chat,
                            text=self.TEXT[event["status"]],
                            reply_parameters={
                                "message_id": original["message"],
                                "allow_sending_without_reply": False,
                            },
                            link_preview_options={"is_disabled": True},
                        )
                        if (
                            isinstance(result, dict)
                            and numeric(result.get("message_id"))
                            and (result.get("from") or {}).get("id") == pilot.bot_id
                            and (result.get("chat") or {}).get("id") == chat
                            and (result.get("reply_to_message") or {}).get("message_id")
                            == original["message"]
                        ):
                            status = "delivered"
                except TelegramUnavailable:
                    pass
                with pilot.db:
                    pilot.db.execute(
                        "UPDATE notification_receipts SET state=? WHERE bot=? AND chat=? AND delivery=?",
                        (status, pilot.bot_id, chat, delivery),
                    )
            row = pilot.db.execute(
                "SELECT state FROM notification_receipts WHERE bot=? AND chat=? AND run=?",
                (pilot.bot_id, chat, event["runId"]),
            ).fetchone()
            if row:
                pilot.gateway._request(
                    "telegram-notification-receipt",
                    {
                        "binding": binding,
                        "deliveryId": delivery,
                        "runId": event["runId"],
                        "attempt": event["attempt"],
                        "delivered": row[0] == "delivered",
                    },
                )
