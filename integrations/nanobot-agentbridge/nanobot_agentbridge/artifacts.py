"""Renew one file on its original coordinated run. Only the Workspace backend calls this."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from aiohttp import web

from .worker import authenticated_session, channel_for


async def reissue(host, binding, body, run_id):
    if not host.coordinator or channel_for(binding["endpoint_key"]) != "workspace":
        raise web.HTTPForbidden()
    try:
        artifact_id = str(UUID(body["artifactId"]))
        task_id = str(UUID(body["taskId"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise web.HTTPBadRequest() from None
    payload = host.worker_payload(binding, run_id)
    async with authenticated_session(payload) as (session, meta, level):
        if level != "L3":
            raise web.HTTPForbidden()
        # Read the original coordinate stamp; a terminal task cannot reacquire a
        # business lease. The central artifact call independently validates this
        # stamp and the user's current file privileges before issuing anything.
        path = Path(payload["userDir"]) / "coordinated-runs.sqlite3"
        if not path.is_file():
            raise web.HTTPForbidden()
        with sqlite3.connect(path) as db:
            row = db.execute(
                "SELECT version FROM tasks WHERE run_id=? AND endpoint=? AND task_id=?",
                (run_id, payload["endpoint"], task_id),
            ).fetchone()
        if not row:
            raise web.HTTPForbidden()
        current = await session.call_tool(
            "agentbridge_host_coordinator_lease_get", {"task_id": task_id}, meta=meta
        )
        lease = (current.structuredContent or {}).get("coordinatorLease") or {}
        if (
            current.isError
            or lease.get("agentHost") != "nanobot"
            or lease.get("hostInstanceId") != host.instance
            or lease.get("version") != row[0]
            or lease.get("taskId") != task_id
        ):
            raise web.HTTPForbidden()
        if lease.get("state") != "active" or datetime.fromisoformat(
            lease["expiresAt"]
        ) <= datetime.now(UTC):
            return {
                "renewed": False,
                "signedInOwnerRequired": True,
                "artifactId": artifact_id,
                "taskId": task_id,
            }
        task_meta = {
            **meta,
            "io.agentbridge/task": {
                "taskId": task_id,
                "coordinatorLeaseVersion": str(row[0]),
                "endpointId": payload["endpoint"],
                "conversationRef": payload["conversation"],
                "hostRunId": run_id,
            },
        }
        result = await session.call_tool(
            "agentbridge_host_artifact_reissue",
            {"agent_host": "nanobot", "task_id": task_id, "artifact_id": artifact_id},
            meta=task_meta,
        )
    data = result.structuredContent or {}
    file = data.get("file") or {}
    if (
        result.isError
        or data.get("status") != "succeeded"
        or file.get("artifactId") != artifact_id
    ):
        return {"renewed": False}
    # The signed-in client retrieves the file through its own authorized result.
    return {"renewed": True, "artifactId": artifact_id, "taskId": task_id}
