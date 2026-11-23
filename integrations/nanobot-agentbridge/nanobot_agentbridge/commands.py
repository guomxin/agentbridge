"""Durable host-private delivery of a central source-bound preparation command."""

import asyncio
import json
from pathlib import Path
import sqlite3
from uuid import UUID

from aiohttp import web

from .worker import authenticated_session


class CommandQueue:
    def __init__(self, host):
        self.host = host
        self.path = host.state / "prepare-commands.sqlite3"
        self.jobs = {}
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
        if not previous or previous[4] not in {"done", "waiting"}:
            async with authenticated_session(request) as (mcp, meta, level):
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

    async def execute(self, request):
        command = request["commandKey"]
        try:
            with sqlite3.connect(self.path) as db:
                db.execute(
                    "UPDATE commands SET phase='sending' WHERE command=?", (command,)
                )
            proc = await asyncio.create_subprocess_exec(
                self.host.coordinator,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=request["userDir"],
                env=self.host.worker_environment(Path(request["userDir"])),
            )
            try:
                async with asyncio.timeout(245):
                    result, _ = await proc.communicate(json.dumps(request).encode())
            finally:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
            success = proc.returncode == 0 and json.loads(result).get("outcome") in {
                "verified",
                "closed",
                "prepared",
            }
        except Exception:
            success = False
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE commands SET phase=? WHERE command=?",
                ("done" if success else "unknown", command),
            )

    async def close(self):
        for job in self.jobs.values():
            job.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
