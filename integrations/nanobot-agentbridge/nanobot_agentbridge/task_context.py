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
        self.meta = None
        self.read_outcome = None
        self.delegated = False
        self.db = Path(request["userDir"]) / "coordinated-runs.sqlite3"
        with sqlite3.connect(self.db) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                run_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE, version INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS pending_queries (
                run_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
                item_count INTEGER NOT NULL, complete INTEGER NOT NULL)""")

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
        self.meta = {
            **self.host_meta,
            "io.agentbridge/task": {
                "taskId": task_id,
                "coordinatorLeaseVersion": str(version),
                "endpointId": req["endpoint"],
                "conversationRef": req["conversation"],
                "hostRunId": req["runId"],
            },
        }
        return self.meta

    def observe_read(self, tool, result):
        """Only explicit central read receipts can close an otherwise unowned run."""
        if result is None or result.isError or result.meta:
            self.delegated = True
            return
        data = result.structuredContent or {}
        if (
            tool == "oa_workflow_pending_list"
            and self.meta
            and data.get("status") == "succeeded"
            and re.fullmatch(r"telegram:[0-9]+:[0-9]+", self.request.get("endpointKey", ""))
        ):
            payload = data.get("result") if isinstance(data.get("result"), dict) else data
            items = payload.get("items") if isinstance(payload, dict) else None
            coverage = payload.get("coverage") if isinstance(payload, dict) else None
            if isinstance(items, list) and len(items) > 0:
                complete = isinstance(coverage, dict) and coverage.get("status") == "complete"
                task_id = self.meta["io.agentbridge/task"]["taskId"]
                with sqlite3.connect(self.db) as db:
                    db.execute(
                        "INSERT OR REPLACE INTO pending_queries VALUES(?,?,?,?,?)",
                        (self.request["runId"], self.request["endpoint"], task_id, len(items), int(complete)),
                    )
        if data.get("interaction") or data.get("operationId"):
            self.delegated = True
        elif tool == "database_execute" and (data.get("status") == "succeeded" or
                (data.get("status") == "rejected" and data.get("code") == "DATABASE_CAPABILITY_DENIED")):
            outcome = "succeeded" if data["status"] == "succeeded" else "failed"
            if self.read_outcome and self.read_outcome != outcome:
                self.delegated = True
            self.read_outcome = outcome
        elif data.get("status") in {"failed", "rejected", "unknown", "requires_user_action", "partial"}:
            self.delegated = True

    async def finish_confirmed_read(self):
        if not self.meta or self.delegated or self.read_outcome is None:
            return
        task = self.meta["io.agentbridge/task"]
        result = await self.session.call_tool("agentbridge_host_task_finish", {
            "agent_host": "nanobot", "task_id": task["taskId"], "outcome": self.read_outcome,
            "reason": "confirmed_read_receipt", "causation_ref": self.request["runId"],
        }, meta=self.meta)
        if result.isError:
            raise PermissionError("Central completion of the read could not be confirmed")


def pending_query_for_run(directory: Path, run_id: str, endpoint: str) -> dict | None:
    path = directory / "coordinated-runs.sqlite3"
    if not path.is_file():
        return None
    with sqlite3.connect(path, timeout=8) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_queries'").fetchone():
            return None
        row = db.execute(
            """SELECT p.task_id, t.version, p.item_count, p.complete
               FROM pending_queries p JOIN tasks t ON t.run_id=p.run_id AND t.endpoint=p.endpoint AND t.task_id=p.task_id
               WHERE p.run_id=? AND p.endpoint=?""",
            (run_id, endpoint),
        ).fetchone()
    if not row:
        return None
    return {"taskId": row[0], "version": row[1], "count": row[2], "complete": bool(row[3])}
