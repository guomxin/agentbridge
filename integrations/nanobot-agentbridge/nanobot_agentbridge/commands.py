"""Durable host-private delivery of a central source-bound preparation command."""

import asyncio
import json
from pathlib import Path
import sqlite3
import time
from uuid import UUID

from aiohttp import web

from .session_pool import open_host_session


class CommandQueue:
    def __init__(self, host):
        self.host = host
        self.path = host.state / "prepare-commands.sqlite3"
        self.jobs = {}
        self.next_query_check = {}
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS commands (command TEXT PRIMARY KEY, subject TEXT NOT NULL,
                endpoint TEXT NOT NULL, session TEXT NOT NULL, task TEXT NOT NULL, phase TEXT NOT NULL)""")

    async def accept(self, record, body):
        task, command = body["taskId"], body["commandKey"]
        try:
            if any(str(UUID(v)) != v for v in (task, command)):
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise web.HTTPBadRequest() from None
        if not self.host.coordinator:
            raise web.HTTPForbidden()
        request = self.host.worker_payload(record, command)
        subject, endpoint, session = (
            record["user_subject"],
            record["endpoint_id"],
            record["session_key"],
        )
        scope = (subject, endpoint, session, task)
        with sqlite3.connect(self.path) as db:
            previous = db.execute(
                "SELECT subject,endpoint,session,task,phase FROM commands WHERE command=?",
                (command,),
            ).fetchone()
        if previous and previous[:4] != scope:
            raise web.HTTPForbidden()
        if not previous or previous[4] == "queued":
            async with open_host_session(self.host, request, lane="control") as (mcp, meta, level):
                if level != "L3":
                    raise web.HTTPForbidden()
                result = await mcp.call_tool(
                    "agentbridge_host_task_snapshot",
                    {
                        "agent_host": "nanobot",
                        "endpoint_key": record["endpoint_key"],
                        "task_id": task,
                        "event_limit": 1,
                        "artifact_limit": 1,
                    },
                    meta=meta,
                )
                observed = (result.structuredContent or {}).get("task") or {}
                if (
                    result.isError
                    or observed.get("taskId") != task
                    or observed.get("userSubject") != subject
                    or observed.get("originEndpointId") != endpoint
                    or observed.get("activeConversationRef") != session
                ):
                    raise web.HTTPForbidden()
                lease = await mcp.call_tool(
                    "agentbridge_host_coordinator_lease_get",
                    {"task_id": task},
                    meta=meta,
                )
                payload = lease.structuredContent or {}
                current = payload.get("coordinatorLease") or {}
                if (
                    lease.isError
                    or current.get("hostInstanceId") != request["instance"]
                    or not current.get("version")
                ):
                    raise web.HTTPForbidden()
            with sqlite3.connect(self.path) as db:
                db.execute(
                    "INSERT OR IGNORE INTO commands VALUES(?,?,?,?,?,'queued')",
                    (command, *scope),
                )
                stored = db.execute(
                    "SELECT subject,endpoint,session,task FROM commands WHERE command=?",
                    (command,),
                ).fetchone()
                if stored != scope:
                    raise web.HTTPForbidden()
            if command not in self.jobs or self.jobs[command].done():
                request.update(
                    mode="prepare_command",
                    commandKey=command,
                    task={
                        "taskId": task,
                        "endpointId": endpoint,
                        "conversationRef": session,
                        "coordinatorLeaseVersion": str(current["version"]),
                        "hostRunId": command,
                    },
                )
                self.jobs[command] = asyncio.create_task(self.execute(request))
        return {"taskId": task, "commandKey": command, "accepted": True}

    def recover_queries(self, record):
        """Only restart observation of already presented, source-bound read credentials."""
        request = self.host.worker_payload(record, None)
        root = Path(request["userDir"])
        journal, reference = root / "coordinated-private.sqlite3", root / "coordinator-state.sqlite3"
        if not journal.is_file() or not reference.is_file():
            return
        with sqlite3.connect(journal) as db:
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='query_commands'").fetchone():
                return
            rows = db.execute("""SELECT q.run,c.central_task,q.local_task,c.phase FROM query_commands q
                JOIN coordinated c ON c.run=q.run AND c.endpoint=q.endpoint AND c.local_task=q.local_task
                WHERE q.endpoint=? AND c.kind='credential' AND c.phase IN ('waiting','resuming','query_observe')""",
                (record["endpoint_id"],)).fetchall()
        for command, task, local, phase in rows:
            job = self.jobs.get(command)
            if job is not None and not job.done():
                continue
            if phase != "waiting" and time.monotonic() < self.next_query_check.get(command, 0):
                continue
            with sqlite3.connect(self.path) as db:
                owned = db.execute("""SELECT 1 FROM commands WHERE command=? AND subject=? AND endpoint=?
                    AND session=? AND task=? AND phase IN ('waiting','sending','unknown')""",
                    (command, record["user_subject"], record["endpoint_id"], record["session_key"], task)).fetchone()
            if not owned:
                continue
            with sqlite3.connect(reference) as db:
                lease = db.execute("""SELECT lease_version FROM reference_tasks WHERE local_task_id=? AND task_id=?
                    AND endpoint_id=? AND conversation_ref=? AND lease_version>0""",
                    (local, task, record["endpoint_id"], record["session_key"])).fetchone()
            if lease:
                work = {**request, "runId": command, "commandKey": command, "mode": "prepare_command",
                        "recoveryOnly": True, "task": {"taskId": task, "hostRunId": command,
                        "endpointId": record["endpoint_id"], "conversationRef": record["session_key"],
                        "coordinatorLeaseVersion": str(lease[0])}}
                # Ambiguous resumes may only observe their authoritative result. Space those checks.
                if phase != "waiting":
                    self.next_query_check[command] = time.monotonic() + 120
                self.jobs[command] = asyncio.create_task(self.execute(work))

    async def execute(self, request):
        command = request["commandKey"]
        try:
            with sqlite3.connect(self.path) as db:
                db.execute(
                    "UPDATE commands SET phase='sending' WHERE command=?", (command,)
                )
            while True:
                phase = await self.observe(request)
                if phase != "waiting":
                    break
                # A live command may keep watching its own recorded card until central expires it.
                # The sidecar refuses recovery without a waiting journal row and never repeats prepare.
                request = {**request, "recoveryOnly": True}
        except Exception:
            phase = "unknown"
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE commands SET phase=? WHERE command=?",
                (phase, command),
            )

    async def observe(self, request):
        proc = await asyncio.create_subprocess_exec(
            self.host.coordinator, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=request["userDir"],
            env=self.host.worker_environment(Path(request["userDir"])),
        )
        try:
            async with asyncio.timeout(245):
                result, _ = await proc.communicate(json.dumps(request).encode())
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
        outcome = json.loads(result).get("outcome") if proc.returncode == 0 else None
        return {"verified": "done", "prepared": "done", "closed": "done", "waiting": "waiting",
                "failed": "failed", "canceled": "canceled", "expired": "expired"}.get(outcome, "unknown")

    async def close(self):
        for job in self.jobs.values():
            job.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
