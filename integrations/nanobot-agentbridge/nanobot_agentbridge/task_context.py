"""Create one server-bound task for an accepted run and validate its current lease."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import re


class TaskContext:
    def __init__(self, session, request, host_meta):
        self.session = session
        self.request = request
        self.host_meta = host_meta
        self.db = Path(request["userDir"]) / "coordinated-runs.sqlite3"
        with sqlite3.connect(self.db) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                run_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE, version INTEGER NOT NULL)""")

    async def get(self, *, existing_only=False):
        req = self.request
        prefix = "workspace:" + req["instance"] + ":"
        sender = re.fullmatch(r"telegram:([0-9]+):([0-9]+)",req["endpointKey"])
        if sender:
            from .telegram_identity import verify_telegram_binding
            if not req.get("workspaceEndpointId") or not req.get("workspaceEndpointKey","").startswith(prefix):
                raise PermissionError("No bound Workspace counterpart")
            if not await verify_telegram_binding(self.session,req["instance"],req["subject"],self.host_meta,req["endpointKey"]):
                raise PermissionError("Original Telegram pairing no longer active")
        elif not req["endpointKey"].startswith(prefix):
            raise PermissionError("No private endpoint binding")
        with sqlite3.connect(self.db) as db:
            row = db.execute(
                "SELECT task_id,version FROM tasks WHERE run_id=? AND endpoint=?",
                (req["runId"], req["endpoint"]),
            ).fetchone()
        if row is None:
            if existing_only:
                raise PermissionError("No previously bound task")
            account = (req["workspaceEndpointKey"] if sender else req["endpointKey"])[len(prefix):]
            result = await self.session.call_tool(
                "agentbridge_host_task_ensure",
                {
                    "agent_host": "nanobot",
                    "host_task_key": req["runId"],
                    "endpoint_key": req["endpointKey"],
                    "client_type": "telegram" if sender else "web",
                    "external_subject": sender.group(2) if sender else account,
                    "account_id": sender.group(1) if sender else account,
                    **({"route":{"channel":"telegram","to":sender.group(2),"accountId":sender.group(1)}} if sender else {}),
                    "conversation_ref": req["conversation"],
                    "title": "NanoBot 工作台任务",
                    "task_scope": "independent",
                },
                meta=self.host_meta,
            )
            payload = result.structuredContent or {}
            endpoint = payload.get("endpoint") or {}
            task_id = (payload.get("task") or {}).get("taskId")
            lease = payload.get("coordinatorLease") or {}
            if (
                result.isError
                or not task_id
                or endpoint.get("endpointId") != req["endpoint"]
                or lease.get("hostInstanceId") != req["instance"]
            ):
                raise PermissionError("Central endpoint or coordinator unavailable")
            version = int(lease["version"])
            with sqlite3.connect(self.db) as db:
                db.execute(
                    "INSERT OR IGNORE INTO tasks VALUES(?,?,?,?)",
                    (req["runId"], req["endpoint"], task_id, version),
                )
                row = db.execute(
                    "SELECT task_id,version FROM tasks WHERE run_id=? AND endpoint=?",
                    (req["runId"], req["endpoint"]),
                ).fetchone()
            if not row or row[0] != task_id:
                raise PermissionError("Conflicting task binding")
        task_id, version = row
        result = await self.session.call_tool(
            "agentbridge_host_coordinator_lease_acquire",
            {
                "task_id": task_id,
                "lease_seconds": 600,
                "expected_version": version,
                "takeover": False,
            },
            meta=self.host_meta,
        )
        lease = (result.structuredContent or {}).get("coordinatorLease") or {}
        if (
            result.isError
            or lease.get("hostInstanceId") != req["instance"]
            or int(lease.get("version", 0)) != version
        ):
            raise PermissionError("Original coordinator lease is unavailable")
        return {
            **self.host_meta,
            "io.agentbridge/task": {
                "taskId": task_id,
                "coordinatorLeaseVersion": str(version),
                "endpointId": req["endpoint"],
                "conversationRef": req["conversation"],
                "hostRunId": req["runId"],
            },
        }
