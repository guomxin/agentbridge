"""Exact local presentation provenance for read-only native card closure checks."""
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from .chat_mirror import identifier
from .session_pool import open_host_session
from .telegram_identity import verify_telegram_binding

KINDS = {"credential", "business_input", "execution_authorization"}
STATES = {"pending", "processing", "completed", "failed", "expired", "superseded", "declined"}


def scope(request, record):
    return hashlib.sha256(json.dumps([request.get("hostType", "nanobot"), request["instance"],
        request["subject"], request["endpoint"], request["endpointKey"], request["conversation"],
        record["credential_ref"]], ensure_ascii=False).encode()).hexdigest()


@contextmanager
def connect(request):
    db = sqlite3.connect(Path(request["userDir"]) / "native-card-sources.sqlite3")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS native_card_sources(
        ref TEXT PRIMARY KEY, scope TEXT NOT NULL, interaction TEXT NOT NULL, kind TEXT NOT NULL,
        task TEXT, run TEXT, request_key TEXT, UNIQUE(scope,interaction))""")
    try:
        with db:
            yield db
    finally:
        db.close()


def remember(host, record, card, *, run=None, request_key=None):
    """Called only after the existing private presentation validated the original source."""
    request = host.worker_payload(record, run)
    if not request["endpointKey"].startswith("telegram:"):
        return None
    ident, kind, task = card.get("interactionId"), card.get("type"), card.get("taskId")
    if (not isinstance(ident, str) or not 16 <= len(ident) <= 128 or kind not in KINDS
            or card.get("state") not in {"pending", "processing"}
            or (task is not None and not identifier(task))
            or (not task and (kind != "credential" or not (run and request_key)))):
        return None
    binding = scope(request, record)
    with connect(request) as db:
        db.execute("INSERT OR IGNORE INTO native_card_sources VALUES(?,?,?,?,?,?,?)",
                   (str(uuid4()), binding, ident, kind, task, run, request_key))
        row = db.execute("SELECT * FROM native_card_sources WHERE scope=? AND interaction=?", (binding, ident)).fetchone()
        if row["kind"] != kind or row["task"] != task:
            return None
        return {"sourceRef": row["ref"], "interactionId": ident, "type": kind, "taskId": task,
                "runId": row["run"]}


async def read(host, record, body):
    from aiohttp import web
    request = host.worker_payload(record, None)
    source = body.get("source")
    if (not request["endpointKey"].startswith("telegram:") or not isinstance(source, dict)
            or set(source) != {"sourceRef", "interactionId", "type", "taskId", "runId"}
            or not identifier(source.get("sourceRef"))):
        raise web.HTTPBadRequest()
    with connect(request) as db:
        row = db.execute("SELECT * FROM native_card_sources WHERE ref=? AND scope=?",
                         (source["sourceRef"], scope(request, record))).fetchone()
    if not row or (row["interaction"], row["kind"], row["task"], row["run"]) != (
            source["interactionId"], source["type"], source["taskId"], source["runId"]):
        raise web.HTTPForbidden()
    if row["run"]:
        entry = host.runs.lookup(row["request_key"], subject=request["subject"], endpoint=request["endpoint"],
                                 conversation=request["conversation"])
        if not entry or entry["run_id"] != row["run"]:
            raise web.HTTPForbidden()
    elif not row["task"]:
        raise web.HTTPForbidden()
    if row["task"] is None and row["kind"] != "credential":
        raise web.HTTPForbidden()
    async with open_host_session(host, request, lane="control") as (session, meta, level):
        if (level not in {"L2", "L3"} or (level == "L2" and (row["kind"] != "credential" or row["task"]))
                or not await verify_telegram_binding(session, request["instance"], request["subject"], meta,
                    request["endpointKey"], host_type=request.get("hostType", "nanobot"))):
            raise web.HTTPForbidden()
        if request.get("hostType", "nanobot") == "nanobot":
            from .telegram_reception import scope as current_pair
            await current_pair(session, meta, record)
        result = await session.call_tool("agentbridge_host_interaction_present", {
            "agent_host": request.get("hostType", "nanobot"), "endpoint_key": request["endpointKey"],
            "interaction_id": row["interaction"], "state_only": True}, meta=meta)
        payload = result.structuredContent or {}
        card = payload.get("interaction") or {}
        if (result.isError or (payload.get("endpoint") or {}).get("endpointId") != request["endpoint"]
                or card.get("interactionId") != row["interaction"] or card.get("type") != row["kind"]
                or card.get("taskId") != row["task"] or card.get("state") not in STATES):
            raise web.HTTPForbidden()
        if row["task"]:
            from .telegram_tasks import _owned
            task = payload.get("task")
            if not _owned(task, request) or task.get("taskId") != row["task"]:
                raise web.HTTPForbidden()
        # Explicit allowlist: no presentation, credential, resume or business data leaves this route.
        return {"source": source, "state": card["state"]}
