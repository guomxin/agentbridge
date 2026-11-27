"""Consume centrally authorized Telegram notifications (cards, result views, commands, status, chat)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import time
from typing import ClassVar

from agentbridge.core.task_state_rules import QUIET_COMPANION_EVENTS

from .chat_mirror import ChatMirrors, chat_claim, identifier, projected_chat
from .telegram_presentation import (
    interaction_can_open,
    normalize_interaction,
    normalize_result_view,
    render_interaction_message,
    render_result_view_message,
    render_status_message,
)

TERMINAL = {
    "task.completed": "succeeded",
    "task.operation.succeeded": "succeeded",
    "task.operation.failed": "failed",
    "task.failed": "failed",
    "task.canceled": "canceled",
}

def _record_claim(path: Path, delivery: str, endpoint: str, run: str, attempt: int) -> None:
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS notification_claims (
            delivery TEXT PRIMARY KEY, endpoint TEXT NOT NULL, run TEXT NOT NULL, attempt INTEGER NOT NULL)""")
        db.execute(
            """INSERT INTO notification_claims VALUES(?,?,?,?)
                ON CONFLICT(delivery) DO UPDATE SET attempt=excluded.attempt
                WHERE endpoint=excluded.endpoint AND run=excluded.run AND attempt<=excluded.attempt""",
            (delivery, endpoint, run, attempt),
        )


async def _issue_task_result_view(session, req: dict, meta: dict, task_id: str, origins: frozenset[str]) -> dict | None:
    lease_resp = await session.call_tool(
        "agentbridge_host_coordinator_lease_get",
        {"task_id": task_id},
        meta=meta,
    )
    lease = (lease_resp.structuredContent or {}).get("coordinatorLease") or {}
    if lease_resp.isError or lease.get("hostInstanceId") != req["instance"] or not lease.get("version"):
        return None
    task_meta = {
        **meta,
        "io.agentbridge/task": {
            "taskId": task_id,
            "coordinatorLeaseVersion": str(lease["version"]),
            "endpointId": req["endpoint"],
            "conversationRef": req["conversation"],
            "hostRunId": task_id,
        },
    }
    issued = await session.call_tool(
        "agentbridge_result_view",
        {"operation": "issue", "task_id": task_id},
        meta=task_meta,
    )
    if issued.isError:
        return None
    return normalize_result_view(
        (issued.structuredContent or {}).get("resultView"),
        source_task_id=task_id,
        endpoint_id=req["endpoint"],
        allowed_origins=origins,
    )


async def notifications(host, record, action, body, *, paired_session=None):
    from aiohttp import web

    from .session_pool import open_host_session
    from .telegram_identity import verify_telegram_binding

    if not host.coordinator or not record.get("workspaceEndpointId"):
        return (
            {"deliveries": []}
            if action == "telegram-notifications"
            else {"recorded": False}
        )
    req = host.worker_payload(record, None)
    origins = frozenset(getattr(host, "card_origins", None) or req.get("trustedCardOrigins") or ())
    lane = "notifications" if action == "telegram-notifications" else "control"
    context = paired_session(req) if paired_session is not None else open_host_session(host, req, lane=lane)
    async with context as (session, meta, level):
        if level != "L3" or (paired_session is None and not await verify_telegram_binding(
            session, req["instance"], req["subject"], meta, req["endpointKey"], host_type=req.get("hostType", "nanobot")
        )):
            raise web.HTTPForbidden()
        endpoint_params = {"agent_host": req.get("hostType", "nanobot"), "endpoint_key": req["endpointKey"]}

        async def ack(delivery, attempt, succeeded, artifact_outcome=None):
            reply = await session.call_tool(
                "agentbridge_host_notification_ack",
                {
                    **endpoint_params,
                    "delivery_id": delivery,
                    "attempt_count": attempt,
                    "succeeded": succeeded,
                    "retry_after_seconds": 5,
                    **({"artifact_outcome": artifact_outcome} if artifact_outcome else {}),
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
        if action == "telegram-workspace-result-view":
            if "pairingRef" in body:
                from .workspace_query_delivery import handle_request
            else:
                from .pi_workspace_result_cards import handle_request
            return await handle_request(session, meta, req, path, body, origins)
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
        if action in {"telegram-artifact-file", "telegram-watch-validate", "telegram-notification-receipt"}:
            delivery, attempt, ok = (
                body.get("deliveryId"),
                body.get("attempt"),
                body.get("delivered"),
            )
            if (
                type(attempt) is not int
                or not 1 <= attempt <= 5
                or (action == "telegram-notification-receipt" and type(ok) is not bool)
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
            if claim[0].startswith("workspace_result_view:"):
                raise web.HTTPForbidden()
            if action == "telegram-artifact-file":
                with_body = body.get("withBody")
                if not claim[0].startswith("artifact:") or type(with_body) is not bool:
                    raise web.HTTPForbidden()
                reply = await session.call_tool("agentbridge_host_notification_validate", {
                    **endpoint_params, "delivery_id": delivery, "attempt_count": attempt, "include_artifact": with_body,
                }, meta=meta)
                result = reply.structuredContent or {}
                if reply.isError or result.get("allowed") is not True or claim[0] != f"artifact:{result.get('artifactId')}":
                    return {"allowed": False}
                return {key: result[key] for key in ("allowed", "artifactId", "file") if key in result}
            if action == "telegram-watch-validate":
                watch_id = claim[0].removeprefix("watch:")
                if not claim[0].startswith("watch:") or not identifier(watch_id):
                    raise web.HTTPForbidden()
                reply = await session.call_tool("agentbridge_host_notification_validate", {
                    **endpoint_params, "delivery_id": delivery, "attempt_count": attempt}, meta=meta)
                current = reply.structuredContent or {}
                if reply.isError or type(current.get("allowed")) is not bool:
                    raise web.HTTPServiceUnavailable()
                if not current["allowed"]:
                    return {"allowed": False, "stale": current.get("stale") is True}
                if current.get("watchId") != watch_id or not isinstance(current.get("message"), str):
                    raise web.HTTPForbidden()
                return {"allowed": True, "message": current["message"]}
            outcome = body.get("artifactOutcome")
            if outcome is not None and (not claim[0].startswith("artifact:") or outcome not in ("attachment_sent", "delivery_unknown")
                                        or ok != (outcome == "attachment_sent")):
                raise web.HTTPBadRequest()
            return {"recorded": await ack(delivery, attempt, ok, outcome)}
        claim_args = {
            **endpoint_params,
            "limit": 32,
            "lease_seconds": 60,
            "include_workspace_chat": True,
            "include_business_watch": True,
            "include_task_files": True,
            "include_oa_digest": True,
            "include_workspace_query_v2": True,
            "include_notification_v2": True,
        }
        claim_meta = meta
        if req.get("hostType") == "pi":
            claim_args["include_workspace_result_views"] = True
            # Older central servers ignore this opt-in and keep returning the
            # quiet events for the original per-delivery acknowledgement below.
            claim_meta = {**meta, "io.agentbridge/pi-quiet-ack": req["pairingRef"]}
        raw_wait = body.get("waitSeconds") if isinstance(body, dict) else None
        if isinstance(raw_wait, (int, float)) and not isinstance(raw_wait, bool) and raw_wait > 0:
            claim_args["wait_seconds"] = min(float(raw_wait), 15.0)
        reply = await session.call_tool(
            "agentbridge_host_notification_claim",
            claim_args,
            meta=claim_meta,
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
            mode = item.get("deliveryMode")
            if mode in {"workspace_query_v2", "workspace_text_v2", "host_notification_v2"}:
                from .workspace_query_delivery import pairing_ref, record_claim, source_fields
                source, pairing = source_fields(item), item.get("pairingRef")
                if source and source[1] == mode and identifier(source[0]) and pairing_ref(pairing):
                    task, _, part_index, part_count = source
                    record_claim(path, req["endpoint"], delivery, attempt, task, pairing, record=record,
                                 mode=mode, part_index=part_index, part_count=part_count,
                                 event_task=item.get("sourceTaskId") if mode == "host_notification_v2" else None,
                                 notification_kind=item.get("notificationKind") if mode == "host_notification_v2" else None)
                    fields = {"sourceEntryId": task, "partIndex": part_index, "partCount": part_count} if mode == "workspace_text_v2" else {"sourceTaskId": task}
                    if mode == "host_notification_v2":
                        fields = {"sourceEventId":task,"sourceTaskId":item["sourceTaskId"],"notificationKind":item["notificationKind"]}
                    deliveries.append({"kind": mode, **fields, "deliveryId": delivery, "attempt": attempt, "pairingRef": pairing})
                continue
            if mode == "workspace_result_view":
                task = item.get("sourceTaskId")
                if req.get("hostType") == "pi" and identifier(task):
                    _record_claim(path, delivery, req["endpoint"], "workspace_result_view:" + task, attempt)
                    deliveries.append({"kind": mode, "sourceTaskId": task, "deliveryId": delivery, "attempt": attempt})
                continue
            if mode == "business_watch":
                # System watch tasks can originate at another personal Workspace.
                # Only a central, endpoint-scoped watch claim is eligible here.
                watch_id = item.get("watchId")
                event = item.get("event") or {}
                if (identifier(watch_id) and (item.get("task") or {}).get("userSubject") == req["subject"]
                        and (event.get("payload") or {}).get("watchId") == watch_id
                        and event.get("eventType") in {"business_watch.changed", "business_watch.completed"}):
                    run_ref = f"watch:{watch_id}"
                    _record_claim(path, delivery, req["endpoint"], run_ref, attempt)
                    deliveries.append({"kind": "business_watch", "runId": run_ref, "deliveryId": delivery, "attempt": attempt})
                continue
            if mode == "timeline_message":
                message = projected_chat(item, req["workspaceEndpointId"], host_type=req.get("hostType", "nanobot"))
                if message and chat_claim(req["userDir"], req["endpoint"], delivery, attempt, register=True):
                    deliveries.append({"kind": "workspace_chat", "deliveryId": delivery, "attempt": attempt, **message})
                continue
            if mode == "no_op":
                await ack(delivery, attempt, True)
                continue
            task, event = item.get("task") or {}, item.get("event") or {}
            if task.get("userSubject") != req["subject"] or task.get("agentHost") != req.get("hostType", "nanobot"):
                continue
            if mode == "artifact":
                artifact_id = (item.get("artifact") or {}).get("artifactId")
                if (identifier(artifact_id) and event.get("eventType") == "task.artifact.ready"
                        and task.get("originEndpointId") == req["endpoint"]
                        and task.get("activeConversationRef") == req["conversation"]):
                    _record_claim(path, delivery, req["endpoint"], f"artifact:{artifact_id}", attempt)
                    deliveries.append({"kind": "artifact", "artifactId": artifact_id, "deliveryId": delivery, "attempt": attempt})
                else:
                    await ack(delivery, attempt, task.get("originEndpointId") != req["endpoint"])
                continue
            if mode == "prepared_command":
                command_key = (event.get("payload") or {}).get("commandKey")
                commands = getattr(host, "commands", None)
                if (
                    commands is None
                    or event.get("eventType") != "task.prepare.requested"
                    or not isinstance(command_key, str)
                    or task.get("originEndpointId") != req["endpoint"]
                    or task.get("activeConversationRef") != req["conversation"]
                ):
                    continue
                try:
                    command = {"taskId": task["taskId"], "commandKey": command_key}
                    if paired_session is not None and req.get("hostType") == "pi":
                        # A long poll may outlive the original token or pairing. Recheck both
                        # on the claimed transport before using it for the snapshot and lease.
                        async with paired_session(req) as current:
                            await commands.accept(record, command, verified_session=current)
                    else:
                        await commands.accept(record, command)
                except Exception:
                    continue
                await ack(delivery, attempt, True)
                continue
            if mode == "trusted_interaction":
                card = normalize_interaction(item.get("interaction"), origins)
                if not card or not interaction_can_open(card):
                    await ack(delivery, attempt, True)
                    continue
                run_ref = f"interaction:{card['interactionId']}"
                _record_claim(path, delivery, req["endpoint"], run_ref, attempt)
                deliveries.append({
                    "kind": "trusted_interaction",
                    "deliveryId": delivery,
                    "attempt": attempt,
                    "runId": run_ref,
                    "interaction": card,
                    "taskId": task.get("taskId"),
                })
                continue
            if mode in {"status", "plan_result"}:
                event_type = event.get("eventType")
                if mode == "status" and event_type in QUIET_COMPANION_EVENTS:
                    await ack(delivery, attempt, True)
                    continue
                text = item.get("message")
                if not isinstance(text, str) or not text.strip():
                    await ack(delivery, attempt, True)
                    continue
                unknown = event_type in {"task.operation.outcome_unknown", "plan.outcome_unknown"}
                tone = (
                    "warning" if unknown
                    else ("danger" if event_type in {"task.failed", "task.operation.failed", "plan.step.failed", "task.interaction.expired", "task.interaction.failed"} else "info")
                )
                run_ref = f"status:{delivery}"
                _record_claim(path, delivery, req["endpoint"], run_ref, attempt)
                deliveries.append({
                    "kind": "status",
                    "deliveryId": delivery,
                    "attempt": attempt,
                    "runId": run_ref,
                    "text": text.strip(),
                    "tone": tone,
                    "unknown": unknown,
                    "taskId": task.get("taskId"),
                })
                continue
            if (
                mode != "origin_handled"
                or task.get("activeConversationRef") != req["conversation"]
                or task.get("originEndpointId") != req["endpoint"]
            ):
                continue
            local = None
            if path.exists():
                with sqlite3.connect(path) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
                        local = db.execute(
                            "SELECT run_id FROM tasks WHERE endpoint=? AND task_id=?",
                            (req["endpoint"], task["taskId"]),
                        ).fetchone()
            command = (task.get("summary") or {}).get("command") or {}
            tg_command = command.get("originKind") == "telegram"
            if not local and not tg_command:
                continue
            local_run = local[0] if local else None
            kind = event.get("eventType")
            if kind == "task.interaction.waiting":
                interaction_id = (event.get("payload") or {}).get("interactionId")
                if not isinstance(interaction_id, str) or not origins:
                    await ack(delivery, attempt, True)
                    continue
                presented = await session.call_tool(
                    "agentbridge_host_interaction_present",
                    {
                        "agent_host": req.get("hostType", "nanobot"),
                        "endpoint_key": req["endpointKey"],
                        "interaction_id": interaction_id,
                    },
                    meta=meta,
                )
                pres_payload = presented.structuredContent or {}
                card = (
                    normalize_interaction(pres_payload.get("interaction"), origins)
                    if not presented.isError
                    and (pres_payload.get("endpoint") or {}).get("endpointId") == req["endpoint"]
                    else None
                )
                if not card or not interaction_can_open(card):
                    await ack(delivery, attempt, True)
                    continue
                run_ref = f"interaction:{card['interactionId']}"
                _record_claim(path, delivery, req["endpoint"], run_ref, attempt)
                deliveries.append({
                    "kind": "trusted_interaction",
                    "deliveryId": delivery,
                    "attempt": attempt,
                    "runId": run_ref,
                    "replyRunId": local_run,
                    "interaction": card,
                    "taskId": task.get("taskId"),
                })
                continue
            if kind == "task.operation.outcome_unknown":
                run_ref = f"status:{delivery}"
                _record_claim(path, delivery, req["endpoint"], run_ref, attempt)
                deliveries.append({
                    "kind": "status",
                    "deliveryId": delivery,
                    "attempt": attempt,
                    "runId": run_ref,
                    "replyRunId": local_run,
                    "text": "业务结果待核实，请先到业务系统核对，不要重复提交。",
                    "tone": "warning",
                    "unknown": True,
                    "taskId": task.get("taskId"),
                })
                continue
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
            if tg_command and command.get("kind") == "oa_pending_query":
                if TERMINAL[kind] == "succeeded":
                    view = await _issue_task_result_view(session, req, meta, task["taskId"], origins)
                    if view:
                        run_ref = f"result_view:{task['taskId']}"
                        _record_claim(path, delivery, req["endpoint"], run_ref, attempt)
                        deliveries.append({
                            "kind": "result_view",
                            "deliveryId": delivery,
                            "attempt": attempt,
                            "runId": run_ref,
                            "resultView": view,
                            "summary": "已查询最新 OA 待办，可从下方进入选择与办理。",
                            "taskId": task["taskId"],
                        })
                        continue
                    msg = "已查询 OA 待办，当前没有可直接办理的待办事项。"
                    tone = "info"
                else:
                    msg = "查询 OA 待办未完成，请稍后重试。"
                    tone = "danger"
                run_ref = f"status:{delivery}"
                _record_claim(path, delivery, req["endpoint"], run_ref, attempt)
                deliveries.append({
                    "kind": "status",
                    "deliveryId": delivery,
                    "attempt": attempt,
                    "runId": run_ref,
                    "text": msg,
                    "tone": tone,
                    "unknown": False,
                    "taskId": task["taskId"],
                })
                continue
            if local_run:
                _record_claim(path, delivery, req["endpoint"], local_run, attempt)
                deliveries.append(
                    {
                        "deliveryId": delivery,
                        "attempt": attempt,
                        "runId": local_run,
                        "taskId": task["taskId"],
                        "status": TERMINAL[kind],
                    }
                )
            elif tg_command:
                run_ref = f"status:{delivery}"
                _record_claim(path, delivery, req["endpoint"], run_ref, attempt)
                deliveries.append({
                    "kind": "status",
                    "deliveryId": delivery,
                    "attempt": attempt,
                    "runId": run_ref,
                    "text": TerminalMessages.TEXT[TERMINAL[kind]],
                    "tone": "info" if TERMINAL[kind] == "succeeded" else "danger",
                    "unknown": False,
                    "taskId": task["taskId"],
                })
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
        from .workspace_query_delivery import WorkspaceQueries
        self.workspace_queries = WorkspaceQueries(self)
        self.workspace_results = None
        if getattr(pilot, "host_type", "nanobot") == "pi":
            from .pi_workspace_result_cards import WorkspaceResultCards
            self.workspace_results = WorkspaceResultCards(self)
        self._deferred_terminal: dict[tuple[int, str], tuple[dict, str, dict, float]] = {}
        with pilot.db:
            pilot.db.executescript("""
                CREATE TABLE IF NOT EXISTS telegram_artifact_deliveries(
                    bot INTEGER NOT NULL, chat INTEGER NOT NULL, artifact TEXT NOT NULL, state TEXT NOT NULL, receipt INTEGER,
                    PRIMARY KEY(bot,chat,artifact), UNIQUE(bot,chat,receipt));
                UPDATE telegram_artifact_deliveries SET state='delivery_unknown' WHERE state='sending';
                CREATE TABLE IF NOT EXISTS notification_receipts(
                    bot INTEGER, chat INTEGER, delivery TEXT, run TEXT, state TEXT NOT NULL,
                    PRIMARY KEY(bot,chat,delivery));
                CREATE UNIQUE INDEX IF NOT EXISTS terminal_once_per_run ON notification_receipts(bot,chat,run);
                UPDATE notification_receipts SET state='delivery_unknown' WHERE state='sending';
                CREATE TABLE IF NOT EXISTS telegram_card_deliveries(
                    bot INTEGER NOT NULL, chat INTEGER NOT NULL, dedupe_key TEXT NOT NULL,
                    delivery TEXT, state TEXT NOT NULL, receipt INTEGER,
                    PRIMARY KEY(bot,chat,dedupe_key));
                UPDATE telegram_card_deliveries SET state='delivery_unknown' WHERE state IN ('sending','beginning');
            """)

    def _reply_message_id(self, chat, binding, digest, run_id):
        if not run_id:
            return None
        row = self.pilot.db.execute(
            """SELECT message FROM messages WHERE bot=? AND chat=? AND subject=?
               AND binding_hash=? AND run_id=? AND state IN ('working','delivered','delivery_unknown')""",
            (self.pilot.bot_id, chat, binding["user_subject"], digest, run_id),
        ).fetchone()
        return row["message"] if row else None

    def send_once(self, chat, binding, digest, dedupe_key, params, *, delivery=None, reply_to=None, task_id=None, send_before=None):
        from .telegram import TelegramUnavailable, numeric

        pilot = self.pilot
        active, proof = pilot._binding(chat)
        if active != binding or proof != digest or (send_before is not None and time.time() >= send_before):
            return False
        with pilot.db:
            inserted = pilot.db.execute(
                "INSERT OR IGNORE INTO telegram_card_deliveries VALUES(?,?,?,?,'sending',NULL)",
                (pilot.bot_id, chat, dedupe_key, delivery),
            ).rowcount
        if inserted:
            status = "delivery_unknown"
            receipt = None
            try:
                if send_before is not None and time.time() >= send_before:
                    with pilot.db:
                        pilot.db.execute("DELETE FROM telegram_card_deliveries WHERE bot=? AND chat=? AND dedupe_key=? AND state='sending'",
                                         (pilot.bot_id, chat, dedupe_key))
                    return False
                else:
                    call_params = {"chat_id": chat, **params}
                    if reply_to is not None:
                        call_params["reply_parameters"] = {
                            "message_id": reply_to,
                            "allow_sending_without_reply": True,
                        }
                    result = pilot.api.call("sendMessage", **call_params)
                    if (
                        isinstance(result, dict)
                        and numeric(result.get("message_id"))
                        and (result.get("from") or {}).get("id") == pilot.bot_id
                        and (result.get("chat") or {}).get("id") == chat
                        and (result.get("chat") or {}).get("type") == "private"
                    ):
                        receipt = result["message_id"]
                        status = "delivered"
            except TelegramUnavailable:
                pass
            with pilot.db:
                if task_id:
                    kind = "result" if dedupe_key.startswith("result_view:") else "task"
                    pilot.navigation.remember(chat, receipt, digest, {"kind": kind, "id": task_id})
                pilot.db.execute(
                    "UPDATE telegram_card_deliveries SET state=?,receipt=? WHERE bot=? AND chat=? AND dedupe_key=?",
                    (status, receipt, pilot.bot_id, chat, dedupe_key),
                )
        row = pilot.db.execute(
            "SELECT state FROM telegram_card_deliveries WHERE bot=? AND chat=? AND dedupe_key=?",
            (pilot.bot_id, chat, dedupe_key),
        ).fetchone()
        return bool(row and row[0] == "delivered")

    def _ack_receipt(self, binding, delivery, run_id, attempt, delivered):
        self.pilot.gateway._request(
            "telegram-notification-receipt",
            {
                "binding": binding,
                "deliveryId": delivery,
                "runId": run_id,
                "attempt": attempt,
                "delivered": bool(delivered),
            },
        )

    def _deliver_terminal_event(self, chat, binding, digest, event, original_message):
        from .telegram import TelegramUnavailable, numeric

        pilot = self.pilot
        delivery = event["deliveryId"]
        active, proof = pilot._binding(chat)
        if active != binding or proof != digest:
            return
        with pilot.db:
            inserted = pilot.db.execute(
                "INSERT OR IGNORE INTO notification_receipts VALUES(?,?,?,?,'sending')",
                (pilot.bot_id, chat, delivery, event["runId"]),
            ).rowcount
        if inserted:
            status = "delivery_unknown"
            receipt = None
            try:
                result = pilot.api.call(
                    "sendMessage",
                    chat_id=chat,
                    text=self.TEXT[event["status"]],
                    reply_parameters={
                        "message_id": original_message,
                        "allow_sending_without_reply": False,
                    },
                )
                if (
                    isinstance(result, dict)
                    and numeric(result.get("message_id"))
                    and (result.get("from") or {}).get("id") == pilot.bot_id
                    and (result.get("chat") or {}).get("id") == chat
                    and (result.get("chat") or {}).get("type") == "private"
                    and (result.get("reply_to_message") or {}).get("message_id")
                    == original_message
                ):
                    status = "delivered"
                    receipt = result["message_id"]
            except TelegramUnavailable:
                pass
            with pilot.db:
                if receipt:
                    target = {"kind": "run", "id": event["runId"], "requestKey": pilot._key(chat, original_message)}
                    if identifier(event.get("taskId")):
                        target["terminalTaskId"] = event["taskId"]
                    pilot.navigation.remember(chat, receipt, digest, target)
                pilot.db.execute(
                    "UPDATE notification_receipts SET state=? WHERE bot=? AND chat=? AND delivery=?",
                    (status, pilot.bot_id, chat, delivery),
                )
        row = pilot.db.execute(
            "SELECT state FROM notification_receipts WHERE bot=? AND chat=? AND run=?",
            (pilot.bot_id, chat, event["runId"]),
        ).fetchone()
        if row:
            self._ack_receipt(binding, delivery, event["runId"], event["attempt"], row[0] == "delivered")

    def flush_deferred(self, *, chat=None, run_id=None):
        self.workspace_queries.drain()
        if self.workspace_results:
            self.workspace_results.drain()
        from agentbridge.workspace.gateway import GatewayRequestError

        if not self._deferred_terminal:
            return
        now = time.monotonic()
        pilot = self.pilot
        for key, (binding, digest, event, expires_at) in list(self._deferred_terminal.items()):
            item_chat, _delivery_id = key
            if now >= expires_at:
                self._deferred_terminal.pop(key, None)
                continue
            if chat is not None and item_chat != chat:
                continue
            if run_id is not None and event.get("runId") != run_id:
                continue
            original = pilot.db.execute(
                """SELECT message FROM messages WHERE bot=? AND chat=? AND subject=?
                        AND binding_hash=? AND run_id=? AND state IN ('delivered','delivery_unknown')""",
                (pilot.bot_id, item_chat, binding["user_subject"], digest, event["runId"]),
            ).fetchone()
            if not original:
                continue
            self._deferred_terminal.pop(key, None)
            try:
                self._deliver_terminal_event(item_chat, binding, digest, event, original["message"])
            except GatewayRequestError:
                pass

    def poll(self, chat, binding, digest, *, wait_seconds=0):
        pilot = self.pilot
        payload = {"binding": binding}
        if isinstance(wait_seconds, (int, float)) and not isinstance(wait_seconds, bool) and wait_seconds > 0:
            payload["waitSeconds"] = min(float(wait_seconds), 15.0)
            reply = pilot.gateway._request(
                "telegram-notifications",
                payload,
                timeout=max(8, int(payload["waitSeconds"]) + 8),
            )
        else:
            reply = pilot.gateway._request("telegram-notifications", payload)
        for event in reply.get("deliveries", []):
            kind = event.get("kind")
            if kind in {"workspace_query_v2", "workspace_text_v2", "host_notification_v2"}:
                self.workspace_queries.claim(chat, binding, digest, event)
                continue
            if kind == "workspace_result_view":
                if self.workspace_results:
                    self.workspace_results.claim(chat, binding, digest, event)
                continue
            if kind == "artifact":
                from .telegram_files import deliver_file
                from .telegram import TelegramUnavailable
                from agentbridge.workspace.gateway import GatewayRequestError
                try:
                    deliver_file(pilot, chat, binding, digest, event)
                except (GatewayRequestError, TelegramUnavailable):
                    # Other files keep their own leases while this claim awaits reconciliation.
                    pass
                continue
            if kind == "business_watch":
                from .watch_delivery import deliver_watch
                deliver_watch(self, chat, binding, digest, event)
                continue
            if kind == "workspace_chat":
                self.chat.poll(chat, binding, digest, event)
                continue
            if kind == "trusted_interaction":
                card = event.get("interaction")
                if not isinstance(card, dict) or not card.get("interactionId"):
                    continue
                from urllib.parse import urlsplit
                parsed_url = urlsplit(str((card.get("presentation") or {}).get("url") or ""))
                origins = frozenset({f"https://{parsed_url.netloc}"}) if parsed_url.scheme == "https" and parsed_url.netloc else frozenset()
                params = render_interaction_message(card, origins)
                if not params:
                    self._ack_receipt(binding, event["deliveryId"], event["runId"], event["attempt"], True)
                    continue
                reply_to = self._reply_message_id(chat, binding, digest, event.get("replyRunId"))
                delivered = self.send_once(
                    chat, binding, digest, f"interaction:{card['interactionId']}", params,
                    delivery=event["deliveryId"], reply_to=reply_to, task_id=event.get("taskId"),
                )
                self._ack_receipt(binding, event["deliveryId"], event["runId"], event["attempt"], delivered)
                continue
            if kind == "result_view":
                view = event.get("resultView")
                if not isinstance(view, dict) or not view.get("sourceTaskId"):
                    continue
                params = render_result_view_message(
                    view,
                    summary=event.get("summary") or "OA 待办已查询，可从下方进入选择与办理。",
                )
                delivered = self.send_once(
                    chat, binding, digest, f"result_view:{view['sourceTaskId']}", params,
                    delivery=event["deliveryId"], task_id=view["sourceTaskId"],
                )
                self._ack_receipt(binding, event["deliveryId"], event["runId"], event["attempt"], delivered)
                continue
            if kind == "status":
                text = event.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                params = render_status_message(
                    text,
                    tone=event.get("tone") or "info",
                    unknown=bool(event.get("unknown")),
                )
                reply_to = self._reply_message_id(chat, binding, digest, event.get("replyRunId"))
                delivered = self.send_once(
                    chat, binding, digest, f"status:{event['deliveryId']}", params,
                    delivery=event["deliveryId"], reply_to=reply_to, task_id=event.get("taskId"),
                )
                self._ack_receipt(binding, event["deliveryId"], event["runId"], event["attempt"], delivered)
                continue
            if event.get("status") not in self.TEXT:
                continue
            original = pilot.db.execute(
                """SELECT message, state FROM messages WHERE bot=? AND chat=? AND subject=?
                        AND binding_hash=? AND run_id=? AND state IN ('accepting','working','delivered','delivery_unknown')""",
                (pilot.bot_id, chat, binding["user_subject"], digest, event["runId"]),
            ).fetchone()
            if not original:
                continue
            if original["state"] in ("accepting", "working"):
                # Keep this leased delivery in memory so reconcile() flushes it immediately
                # once the LLM reply completes instead of waiting for the 60s central lease timeout.
                key = (chat, event["deliveryId"])
                if len(self._deferred_terminal) >= 64 and key not in self._deferred_terminal:
                    oldest_key = next(iter(self._deferred_terminal))
                    self._deferred_terminal.pop(oldest_key, None)
                self._deferred_terminal[key] = (
                    dict(binding),
                    digest,
                    dict(event),
                    time.monotonic() + 50.0,
                )
                continue
            self._deliver_terminal_event(chat, binding, digest, event, original["message"])
