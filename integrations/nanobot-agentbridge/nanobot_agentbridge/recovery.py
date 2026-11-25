"""Reconcile only a recorded task and endpoint. Unknown writes are never issued again."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3

from .session_pool import open_host_session


def verified_answer(subject, endpoint, task_id, operation, expected_capability, batch_snapshot, expected_batch_id):
    if batch_snapshot is not None:
        task=batch_snapshot.get("task") or {}
        proof=batch_snapshot.get("batchVerification") or {}
        count=proof.get("totalCount")
        if (not expected_batch_id or task.get("taskId")!=task_id or task.get("userSubject")!=subject
            or task.get("originEndpointId")!=endpoint or proof.get("taskId")!=task_id
            or proof.get("batchId")!=expected_batch_id or proof.get("state")!="succeeded"
            or proof.get("allVerified") is not True or type(count) is not int or count<1 or count!=proof.get("verifiedCount")):
            raise ValueError("Missing authoritative verification for the complete original batch")
        answer=f"中央已重新核验原批次全部 {count} 项业务操作成功。"
    else:
        operation=operation or {}
        evidence=(operation.get("result") or {}).get("verification") or {}
        if (not task_id or operation.get("taskId") != task_id or operation.get("userSubject") != subject
                or not operation.get("operationId") or not expected_capability or operation.get("capability") != expected_capability
                or operation.get("status") != "succeeded"
                or not (evidence.get("confirmed") is True or evidence.get("matched") is True)):
            raise ValueError("Missing authoritative verification for the original operation")
        answer=("中央已重新核验原任务的请假草稿保存成功，流程尚未提交。" if expected_capability=="oa.leave.save_draft"
                else "中央已重新核验原任务的业务操作成功。")
    return answer


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


async def verified_operation(session, subject, task, meta, expected_commit, *, plan_operation=None):
    if plan_operation:
        operation_ids = [plan_operation]
    else:
        result = await session.call_tool("agentbridge_operation_list", {"limit": 100}, meta=meta)
        items = (result.structuredContent or {}).get("operations", [])
        if result.isError or not isinstance(items, list):
            return None
        operation_ids = [
            candidate["operationId"] for candidate in items
            if isinstance(candidate, dict) and candidate.get("operationId")
            and candidate.get("taskId") == task and candidate.get("userSubject") == subject
            and candidate.get("capability") == expected_commit and candidate.get("status") == "succeeded"
        ]
    for operation_id in operation_ids:
        result = await session.call_tool("agentbridge_operation_get", {"operation_id": operation_id}, meta=meta)
        op = (result.structuredContent or {}).get("operation") or {}
        if (
            not result.isError
            and op.get("operationId") == operation_id
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
    run_state = host.runs.snapshot(run, **bound)["state"]
    if run_state != "unknown" and not (run_state == "completed" and phase == "waiting"):
        return
    async with open_host_session(host, request, lane="control") as (session, meta, level):
        if level != "L3":
            return
        private_db = Path(request["userDir"]) / "coordinated-private.sqlite3"
        with sqlite3.connect(private_db) as db:
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
            plan_binding = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='write_plan_scope'").fetchone():
                plan_binding = db.execute("SELECT plan FROM write_plan_scope WHERE run=?", (run,)).fetchone()
        if not scope:
            return
        contract = request["controlledPrepares"].get(scope[0]) or {}
        if contract.get("commitCapability") != scope[1]:
            return
        if batch:
            response = await session.call_tool(
                "agentbridge_host_task_snapshot",
                {
                    "agent_host": request.get("hostType", "nanobot"),
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
                with sqlite3.connect(private_db) as db:
                    db.execute(
                        "UPDATE coordinated SET phase='verified' WHERE run=? AND endpoint=? AND central_task=?",
                        (run, record["endpoint_id"], task),
                    )
                return
        else:
            plan_operation = None
            if plan_binding:
                if not plan_binding[0]:
                    return
                result = await session.call_tool("agentbridge_task_plan_get", {"plan_id":plan_binding[0]}, meta=meta)
                plan = (result.structuredContent or {}).get("plan") or {}
                if result.isError or (plan.get("planId"), plan.get("taskId")) != (plan_binding[0], task):
                    return
                steps = plan.get("steps") or []
                if (plan.get("state") == "succeeded" and steps and steps[-1].get("state") == "succeeded"
                        and steps[-1].get("capabilityName") == contract.get("prepareCapability")):
                    plan_operation = steps[-1].get("operationId")
                if not plan_operation and phase != "waiting":
                    return
            op = None
            if not plan_binding or plan_operation:
                op = await verified_operation(session, bound["subject"], task, meta, scope[1], plan_operation=plan_operation)
            if op:
                host.runs.reconcile_verified(
                    run,
                    **bound,
                    task_id=task,
                    operation=op,
                    expected_capability=scope[1],
                )
                with sqlite3.connect(private_db) as db:
                    db.execute(
                        "UPDATE coordinated SET phase='verified' WHERE run=? AND endpoint=? AND central_task=?",
                        (run, record["endpoint_id"], task),
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
            if (not row and request.get("hostType") == "pi" and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='planning_repairs'").fetchone()):
                row = db.execute("SELECT version FROM planning_repairs WHERE run_id=? AND endpoint=? AND task_id=?",
                                 (run, record["endpoint_id"], task)).fetchone()
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
                    if original["state"] == "unknown" or (
                        original["state"] == "completed" and phase == "waiting"
                    ):
                        jobs[run] = asyncio.create_task(
                            reconcile(host, record, run, task, phase)
                        )
            await asyncio.sleep(8)
    finally:
        for job in jobs.values():
            job.cancel()
        await asyncio.gather(*jobs.values(), return_exceptions=True)
