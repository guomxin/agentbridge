"""Run governed preparation in the existing private Python coordinator."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

PROTECTED = {"idempotency_key", "authorization_id", "input_submission_id", "approved", "decision"}


def executable(value):
    path = Path(value)
    info = path.lstat()
    if (not path.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_mode & 0o022 or not os.access(path, os.X_OK)):
        raise PermissionError("Expected a trusted private Pi coordinator launcher")
    return path


class Coordinator:
    def __init__(self, host):
        self.host = host
        self.governed_runs = set()
        self.program = executable(host.spec["coordinatorProgram"])
        if not host.spec.get("trustedCardOrigins"):
            raise ValueError("The coordinator requires trusted card origins")

    def __fspath__(self):
        return str(self.program)

    def waiting(self, record, run):
        path = self.host.user_directory(record) / "coordinated-private.sqlite3"
        if not path.is_file():
            return None
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='card_versions'").fetchone():
                return None
            return db.execute("""SELECT c.central_task AS task,c.kind,c.interaction,v.revision FROM coordinated c
                JOIN card_versions v ON v.run=c.run AND v.interaction=c.interaction
                WHERE c.run=? AND c.endpoint=? AND c.phase='waiting'""", (run, record["endpoint_id"])).fetchone()

    async def recover(self):
        from nanobot_agentbridge.recovery import reconcile, recovery_rows
        jobs = {}
        try:
            while True:
                rows = self.host.ledger.db.execute("SELECT DISTINCT binding FROM runs WHERE state IN ('completed','unknown')").fetchall()
                for row in rows:
                    key = tuple(json.loads(row[0]))
                    record = self.host.bindings.get(key)
                    if record is None and key[2].startswith("telegram:"):
                        try:
                            record = await self.host.telegram.verified(key)
                        except Exception:
                            continue
                    if not record:
                        continue
                    self.host.commands.recover_queries(record)
                    for run, task, phase in recovery_rows(self.host.user_directory(record), key[1]):
                        if run in jobs and not jobs[run].done():
                            continue
                        if run in jobs:
                            await asyncio.gather(jobs.pop(run), return_exceptions=True)
                        try:
                            item = self.host.ledger.snapshot(run, subject=key[0], endpoint=key[1], conversation=key[3])
                        except KeyError:
                            continue
                        if item["binding"] == row[0] and (item["state"] == "unknown" or item["state"] == "completed" and phase == "waiting"):
                            jobs[run] = asyncio.create_task(reconcile(self.host, record, run, task, phase))
                await asyncio.sleep(8)
        finally:
            for job in jobs.values():
                job.cancel()
            await asyncio.gather(*jobs.values(), return_exceptions=True)

    async def dispatch(self, central, record, run, tool, arguments, *, plan=False, skill_pin=None):
        if central.level != "L3" or tool not in central.governed or PROTECTED.intersection(arguments):
            raise PermissionError("Governed Pi scope required")
        central.task_context.delegated = True
        fingerprint = hashlib.sha256(json.dumps([tool, arguments, plan, skill_pin], ensure_ascii=False,
                                                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if central.governed_result:
            return central.governed_result[0] if central.governed_fingerprint == fingerprint else "conflict"
        # Stopping generation cannot certify cancellation of an authorized write.
        self.governed_runs.add(run)
        asyncio.current_task().add_done_callback(lambda _: self.governed_runs.discard(run))
        central.governed_fingerprint = fingerprint
        central.governed_result = ("unknown", tool)
        meta = await central.task_context.get()
        request = self.host.worker_payload(record, run)
        request.update(task=meta["io.agentbridge/task"], tool=tool, arguments=arguments)
        if plan:
            request["mode"] = "durable_plan"
        if skill_pin:
            request["skillPin"] = skill_pin
        outcome = await self.invoke(request)
        central.governed_result = (outcome, tool)
        return outcome

    async def invoke(self, payload):
        process = await asyncio.create_subprocess_exec(str(self.program), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, cwd=payload["userDir"],
            env={"HOME": payload["userDir"], "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"})
        try:
            async with asyncio.timeout(245):
                data, _ = await process.communicate(json.dumps(payload).encode())
                if process.returncode or len(data) > 256:
                    return "unknown"
                return json.loads(data).get("outcome", "unknown")
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
