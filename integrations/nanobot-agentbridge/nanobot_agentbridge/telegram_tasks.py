"""Resolve Telegram navigation against the current private endpoint and central task."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import re

from agentbridge.core.task_state_rules import TERMINAL_TASK_STATUSES

from .chat_mirror import identifier
from .session_pool import open_host_session
from .telegram_identity import verify_telegram_binding
from .telegram_presentation import interaction_can_open, normalize_interaction


def _owned(task, request):
    return (
        isinstance(task, dict)
        and identifier(task.get("taskId"))
        and task.get("userSubject") == request["subject"]
        and task.get("agentHost") == request.get("hostType", "nanobot")
        and task.get("originEndpointId") == request["endpoint"]
        and task.get("activeConversationRef") == request["conversation"]
    )


def _run_task(request, run):
    path = Path(request["userDir"]) / "coordinated-runs.sqlite3"
    if not path.is_file():
        return None
    with sqlite3.connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='tasks'").fetchone():
            return None
        row = db.execute("SELECT task_id FROM tasks WHERE run_id=? AND endpoint=?",
                         (run, request["endpoint"])).fetchone()
    return row[0] if row else None


def _valid_until(value):
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return expires.tzinfo is not None and expires > datetime.now(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return False


async def navigate(host, record, body):
    from aiohttp import web

    operation = body.get("operation")
    if operation not in {"list", "history", "continue", "stop", "public_reference"}:
        raise web.HTTPBadRequest()
    request = host.worker_payload(record, None)
    if not host.coordinator or not request["endpointKey"].startswith("telegram:"):
        raise web.HTTPForbidden()
    async with open_host_session(host, request, lane="control") as (session, meta, level):
        if level != "L3" or not await verify_telegram_binding(
            session, host.instance, request["subject"], meta, request["endpointKey"], host_type=request.get("hostType", "nanobot")
        ):
            raise web.HTTPForbidden()
        endpoint = {"agent_host": request.get("hostType", "nanobot"), "endpoint_key": request["endpointKey"]}

        if operation == "public_reference":
            from .cross_endpoint_reference import answer
            reference = body.get("target")
            if (not isinstance(reference, dict) or set(reference) != {"telegramReceipt"}
                    or type(reference["telegramReceipt"]) is not int or reference["telegramReceipt"] <= 0):
                raise web.HTTPBadRequest()
            text = await answer(session, meta, host=endpoint["agent_host"], endpoint=endpoint["endpoint_key"],
                                reference=reference)
            return {"referenceText": text}

        async def read(name, arguments):
            reply = await session.call_tool(name, arguments, meta=meta)
            if reply.isError:
                raise web.HTTPForbidden()
            return reply.structuredContent or {}

        target = body.get("target") or {}
        kind, key = target.get("kind"), target.get("id")
        if operation == "history" or kind == "history_task":
            if kind == "history_task" and (operation != "continue" or not identifier(key)):
                return {"state": "unavailable"}
            history = await read("agentbridge_host_task_list", {**endpoint, "limit": 100, "terminal_history": True,
                                 **({"history_task_id": key} if kind == "history_task" else {})})
            if (history.get("verifiedTerminalHistory") is not True
                    or (history.get("endpoint") or {}).get("endpointId") != request["endpoint"]):
                raise web.HTTPForbidden()
            tasks = [task for task in history.get("tasks", []) if identifier(task.get("taskId"))
                     and task.get("status") in TERMINAL_TASK_STATUSES]
            if kind == "history_task":
                if len(tasks) == 1 and tasks[0]["taskId"] == key:
                    return {"state": tasks[0]["status"], "taskId": key}
                return {"state": "unavailable"}
            return {"candidates": tasks}
        if operation == "list":
            result = await read("agentbridge_host_task_list", {**endpoint, "active_only": True, "limit": 100})
            if (result.get("endpoint") or {}).get("endpointId") != request["endpoint"]:
                raise web.HTTPForbidden()
            return {"candidates": [
                {"taskId": task["taskId"], "title": str(task.get("title") or "办公任务")[:80],
                 "status": task.get("status"), "createdAt": task.get("createdAt")}
                for task in result.get("tasks", []) if _owned(task, request)
            ]}
        if kind == "interaction":
            valid_key = isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key)
        else:
            valid_key = identifier(key)
        if not valid_key:
            raise web.HTTPBadRequest()
        query_result = kind == "result"
        if kind == "run":
            task_id = _run_task(request, key)
            if not task_id:
                # Standalone login cards predate governed tasks. The original host
                # admission and card path must still match this exact Telegram turn.
                if operation == "continue" and isinstance(target.get("requestKey"), str):
                    entry = host.runs.lookup(target["requestKey"], subject=request["subject"],
                                             endpoint=request["endpoint"], conversation=request["conversation"])
                    if entry and entry["run_id"] == key and entry["state"] == "accepted":
                        if getattr(type(host), "standalone_card", None) is not None:
                            return await host.standalone_card(key, target["requestKey"])
                        from .cards import CredentialCards, present_credential
                        waiting = CredentialCards(Path(request["userDir"])).waiting(key, request["endpoint"])
                        card = await present_credential(session, request, meta, waiting) if waiting else None
                        if card and _valid_until(card["interaction"].get("expiresAt")):
                            return {"interaction": card["interaction"]}
                return {"state": "unavailable"}
            if operation == "continue":
                from .task_context import pending_query_for_run
                query_result = bool(pending_query_for_run(Path(request["userDir"]), key, request["endpoint"]))
        elif kind == "interaction":
            result = await read("agentbridge_host_interaction_present", {**endpoint, "interaction_id": key})
            card = result.get("interaction") or {}
            if ((result.get("endpoint") or {}).get("endpointId") != request["endpoint"]
                    or card.get("interactionId") != key or not identifier(card.get("taskId"))):
                return {"state": "unavailable"}
            task_id = card["taskId"]
        elif kind == "status_delivery":
            if operation != "continue":
                return {"state": "unavailable"}
            path = Path(request["userDir"]) / "coordinated-runs.sqlite3"
            if not path.is_file():
                return {"state": "unavailable"}
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='notification_claims'").fetchone():
                    return {"state": "unavailable"}
                claim = db.execute("SELECT attempt FROM notification_claims WHERE delivery=? AND endpoint=? AND run=?",
                                   (key, request["endpoint"], "status:" + key)).fetchone()
            if not claim or type(claim[0]) is not int or not 1 <= claim[0] <= 5:
                return {"state": "unavailable"}
            prior = await read("agentbridge_host_notification_validate", {**endpoint, "delivery_id": key,
                               "attempt_count": claim[0], "lookup_acknowledged_task": True})
            task_id = prior.get("acknowledgedTaskId")
            if prior.get("allowed") is not False or not identifier(task_id):
                return {"state": "unavailable"}
        elif kind in {"task", "result"}:
            task_id = key
        else:
            raise web.HTTPBadRequest()
        snapshot = await read("agentbridge_host_task_snapshot", {
            **endpoint, "task_id": task_id, "event_limit": 1, "artifact_limit": 1,
        })
        task = snapshot.get("task")
        if (not _owned(task, request) or task["taskId"] != task_id
                or (snapshot.get("endpoint") or {}).get("endpointId") != request["endpoint"]):
            raise web.HTTPForbidden()
        state = task["status"]
        summary = {"taskId": task_id, "state": state}
        if kind == "status_delivery":
            return summary if state in TERMINAL_TASK_STATUSES else {"state": "unavailable"}
        if operation == "stop":
            if state not in {"active", "running", "waiting_user"}:
                return summary
            # The central cancellation transaction decides whether submission won a race.
            canceled = await read("agentbridge_task_cancel", {"task_id": task_id})
            atomic = canceled.get("task") or {}
            plan = canceled.get("plan") or {}
            if (canceled.get("status") == "succeeded" and canceled.get("businessWriteOccurred") is False
                    and _owned(atomic, request) and atomic["taskId"] == task_id
                    and atomic.get("status") in {"canceled", "superseded"}):
                return {**summary, "state": "canceled"}
            if (canceled.get("status") == "canceled" and plan.get("taskId") == task_id
                    and plan.get("state") == "canceled"):
                return {**summary, "state": "canceled"}
            return {**summary, "state": "stop_unconfirmed"}
        card = snapshot.get("interaction") or {}
        if (state == "waiting_user" and card.get("interactionId") == task.get("currentInteractionId")
                and card.get("taskId") == task_id and _valid_until(card.get("expiresAt"))):
            normalized = normalize_interaction(card, frozenset(host.card_origins))
            if normalized and interaction_can_open(normalized):
                return {**summary, "interaction": normalized}
        if query_result or (task.get("summary") or {}).get("command", {}).get("kind") == "oa_pending_query":
            from .notifications import _issue_task_result_view
            view = await _issue_task_result_view(session, request, meta, task_id, frozenset(host.card_origins))
            if view:
                return {**summary, "resultView": view}
        return summary
