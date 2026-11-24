"""Reconcile only a recorded task and endpoint. Unknown writes are never issued again."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3

from .worker import authenticated_session


def recovery_rows(directory, endpoint):
    source = directory / "coordinated-private.sqlite3"
    if not source.is_file():
        return []
    with sqlite3.connect(source, timeout=8) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordinated'"
        ).fetchone():
            return []
        return db.execute(
            "SELECT run,central_task,phase FROM coordinated WHERE endpoint=? ORDER BY rowid DESC LIMIT 150",
            (endpoint,),
        ).fetchall()


async def verified_operation(session, subject, task, meta, expected_commit):
    result = await session.call_tool(
        "agentbridge_operation_list", {"limit": 100}, meta=meta
    )
    items = (result.structuredContent or {}).get("operations", [])
    if result.isError or not isinstance(items, list):
        return None
    for candidate in items:
        if (
            not isinstance(candidate, dict)
            or candidate.get("taskId") != task
            or candidate.get("userSubject") != subject
            or candidate.get("capability") != expected_commit
            or candidate.get("status") != "succeeded"
        ):
            continue
        result = await session.call_tool(
            "agentbridge_operation_get",
            {"operation_id": candidate["operationId"]},
            meta=meta,
        )
        op = (result.structuredContent or {}).get("operation") or {}
        if (
            not result.isError
            and op.get("taskId") == task
            and op.get("userSubject") == subject
            and op.get("status") == "succeeded"
            and op.get("capability") == expected_commit
            and any(
                ((op.get("result") or {}).get("verification") or {}).get(flag) is True
                for flag in ("confirmed", "matched")
            )
        ):
            return op
    return None


async def reconcile(host, record, run, task, phase):
    request = host.worker_payload(record, run)
    bound = dict(
        subject=record["user_subject"],
        endpoint=record["endpoint_id"],
        conversation=record["session_key"],
    )
    if host.runs.snapshot(run, **bound)["state"] != "unknown":
        return
    async with authenticated_session(request) as (session, meta, level):
        if level != "L3":
            return
        with sqlite3.connect(
            Path(request["userDir"]) / "coordinated-private.sqlite3"
        ) as db:
            scope = db.execute(
                "SELECT prepare,expected_commit FROM write_scope WHERE run=?", (run,)
            ).fetchone()
            batch = (
                db.execute(
                    "SELECT batch FROM batch_scope WHERE run=?", (run,)
                ).fetchone()
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='batch_scope'"
                ).fetchone()
                else None
            )
        if not scope:
            return
        contract = request["controlledPrepares"].get(scope[0]) or {}
        if contract.get("commitCapability") != scope[1]:
            return
        if batch:
            response = await session.call_tool(
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
            snapshot = response.structuredContent or {}
            if response.isError:
                return
            if (snapshot.get("batchVerification") or {}).get("allVerified") is True:
                host.runs.reconcile_verified(
                    run,
                    **bound,
                    task_id=task,
                    batch_snapshot=snapshot,
                    expected_batch_id=batch[0],
                )
                return
        else:
            op = await verified_operation(
                session, bound["subject"], task, meta, scope[1]
            )
            if op:
                host.runs.reconcile_verified(
                    run,
                    **bound,
                    task_id=task,
                    operation=op,
                    expected_capability=scope[1],
                )
                return
        if phase != "waiting":
            return
        # Only a previously bound task whose interaction is still awaiting a user can reopen monitoring.
        ledger = Path(request["userDir"]) / "coordinated-runs.sqlite3"
        if not ledger.is_file():
            return
        with sqlite3.connect(ledger) as db:
            row = db.execute(
                "SELECT version FROM tasks WHERE run_id=? AND endpoint=? AND task_id=?",
                (run, record["endpoint_id"], task),
            ).fetchone()
        if not row:
            return
        request.update(
            recoveryOnly=True,
            tool=scope[0],
            task={
                "taskId": task,
                "coordinatorLeaseVersion": str(row[0]),
                "endpointId": record["endpoint_id"],
                "conversationRef": record["session_key"],
                "hostRunId": run,
            },
        )
    process = await asyncio.create_subprocess_exec(
        host.coordinator,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=request["userDir"],
        env=host.worker_environment(Path(request["userDir"])),
    )
    try:
        async with asyncio.timeout(245):
            await process.communicate(json.dumps(request).encode())
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def recover(host):
    jobs = {}
    try:
        while True:
            for record in (
                *host.bindings.values(),
                *host.telegram_routes.known.values(),
            ):
                host.commands.recover_queries(record)
                directory = Path(host.worker_payload(record, None)["userDir"])
                for run, task, phase in recovery_rows(directory, record["endpoint_id"]):
                    existing = jobs.get(run)
                    if existing and not existing.done():
                        continue
                    if existing:
                        jobs.pop(run)
                        await asyncio.gather(existing, return_exceptions=True)
                    try:
                        original = host.runs.snapshot(
                            run,
                            subject=record["user_subject"],
                            endpoint=record["endpoint_id"],
                            conversation=record["session_key"],
                        )
                    except KeyError:
                        # Source-bound preparation commands use their own receipt and lifecycle.
                        continue
                    if original["state"] == "unknown":
                        jobs[run] = asyncio.create_task(
                            reconcile(host, record, run, task, phase)
                        )
            await asyncio.sleep(8)
    finally:
        for job in jobs.values():
            job.cancel()
        await asyncio.gather(*jobs.values(), return_exceptions=True)
