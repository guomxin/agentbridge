"""Endpoint-bound credential cards. The run ledger stores only opaque references."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3

from .runtime import PRIVATE_INTERACTION, _valid_card


class CredentialCards:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "card-references.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS credentials (
                run_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL,
                interaction_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS credential_card_versions (
                run_id TEXT PRIMARY KEY, interaction_id TEXT NOT NULL, task_id TEXT, revision INTEGER NOT NULL)""")

    def connect(self):
        return sqlite3.connect(self.path, timeout=8)

    def remember(self, run_id, endpoint, interaction_id, task_id=None):
        with self.connect() as db:
            row = db.execute(
                """INSERT INTO credentials VALUES (?,?,?,'waiting') ON CONFLICT(run_id) DO UPDATE SET
                   interaction_id=excluded.interaction_id, state='waiting'
                   WHERE credentials.endpoint=excluded.endpoint AND credentials.state IN ('completed','closed')
                   AND credentials.interaction_id<>excluded.interaction_id""",
                (run_id, endpoint, interaction_id),
            )
            if row.rowcount != 1:
                raise ValueError("A previous credential is still unresolved")
            db.execute("""INSERT INTO credential_card_versions VALUES(?,?,?,1)
                ON CONFLICT(run_id) DO UPDATE SET task_id=excluded.task_id, interaction_id=excluded.interaction_id,
                revision=credential_card_versions.revision+1""", (run_id, interaction_id, task_id))

    def transition(self, run_id: str, previous: str, state: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "UPDATE credentials SET state=? WHERE run_id=? AND state=?",
                    (state, run_id, previous),
                ).rowcount
                == 1
            )

    def waiting_card(self, run_id, endpoint):
        with self.connect() as db:
            row = db.execute(
                """SELECT c.interaction_id, v.task_id, COALESCE(v.revision, 1) FROM credentials c
                   LEFT JOIN credential_card_versions v ON c.run_id=v.run_id AND c.interaction_id=v.interaction_id
                   WHERE c.run_id=? AND c.endpoint=? AND c.state='waiting'""", (run_id, endpoint),
            ).fetchone()
        return {"interaction": row[0], "task": row[1], "revision": row[2]} if row else None

    def waiting(self, run_id, endpoint):
        card = self.waiting_card(run_id, endpoint)
        return card["interaction"] if card else None


async def present_credential(
    session, request: dict, context: dict, interaction_id: str, *, task_id: str | None = None
) -> dict | None:
    result = await session.call_tool(
        "agentbridge_host_interaction_present",
        {
            "agent_host": "nanobot",
            "endpoint_key": request["endpointKey"],
            "interaction_id": interaction_id,
        },
        meta=context,
    )
    payload = result.structuredContent or {}
    card = payload.get("interaction")
    endpoint = payload.get("endpoint") or {}
    if (
        result.isError
        or not isinstance(card, dict)
        or card.get("interactionId") != interaction_id
        or card.get("taskId") != task_id
        or card.get("type") != "credential"
        or endpoint.get("endpointId") != request["endpoint"]
        or not _valid_card(card, frozenset(request["trustedCardOrigins"]))
    ):
        return None
    return {
        "available": True,
        "kind": "credential",
        "url": card["presentation"]["url"],
        "interaction": card,
    }


async def wait_for_credential(
    session, request: dict, context: dict, interaction: dict
) -> str:
    if interaction["type"] != "credential":
        return "unavailable"
    ident = interaction["interactionId"]
    task_id = (context.get("io.agentbridge/task") or {}).get("taskId")
    if not await present_credential(session, request, context, ident, task_id=task_id):
        return "unavailable"
    journal = CredentialCards(Path(request["userDir"]))
    run_id = request["runId"]
    journal.remember(run_id, request["endpoint"], ident, task_id)
    try:
        async with asyncio.timeout(235):
            while True:
                result = await session.call_tool(
                    "agentbridge_interaction_get",
                    {"interaction_id": ident, "wait_seconds": 4},
                    meta=context,
                )
                card = (
                    (result.meta or {}).get(PRIVATE_INTERACTION)
                    or (result.structuredContent or {}).get("interaction")
                    or {}
                )
                if (
                    result.isError
                    or card.get("interactionId") != ident
                    or card.get("type") != "credential"
                ):
                    return "unknown"
                phase = card.get("state")
                resume = card.get("resume") or {}
                if phase in {"expired", "declined", "failed", "superseded"}:
                    return "closed"
                if phase == "completed" and resume.get("completed") is True:
                    journal.transition(run_id, "waiting", "completed")
                    return "completed"
                if phase == "completed" and resume.get("ready") is True:
                    # Consume the local right to call resume before the MCP round trip.
                    if not journal.transition(run_id, "waiting", "resuming"):
                        return "unknown"
                    resumed = await session.call_tool(
                        "agentbridge_interaction_resume",
                        {
                            "interaction_id": ident,
                            "idempotency_key": f"nanobot:{run_id}:{ident}",
                        },
                        meta={**context, "io.agentbridge/session-ready-only": True},
                    )
                    payload = resumed.structuredContent or {}
                    outcome = (
                        "completed"
                        if not resumed.isError and payload.get("status") == "succeeded"
                        else "unknown"
                    )
                    journal.transition(run_id, "resuming", outcome)
                    return outcome
                await asyncio.sleep(0.2)
    finally:
        journal.transition(run_id, "waiting", "closed")


def coordinated_waiting(directory: Path, run_id: str, endpoint: str) -> dict | None:
    path = directory / "coordinated-private.sqlite3"
    if not path.is_file():
        return None
    with sqlite3.connect(path, timeout=8) as db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "coordinated" not in tables:
            return None
        row = db.execute(
            "SELECT central_task,interaction,kind FROM coordinated WHERE run=? AND endpoint=? AND phase='waiting'",
            (run_id, endpoint),
        ).fetchone()
        if not row:
            return None
        revision = db.execute("SELECT revision FROM card_versions WHERE run=? AND interaction=?",
                              (run_id,row[1])).fetchone() if "card_versions" in tables else None
    return {"task": row[0], "interaction": row[1], "kind": row[2],"revision":revision[0] if revision else 1}


async def present_coordinated(session, request, context, waiting):
    result = await session.call_tool(
        "agentbridge_host_interaction_present",
        {
            "agent_host": "nanobot",
            "endpoint_key": request["endpointKey"],
            "interaction_id": waiting["interaction"],
        },
        meta=context,
    )
    payload = result.structuredContent or {}
    card = payload.get("interaction") or {}
    if (
        result.isError
        or (payload.get("endpoint") or {}).get("endpointId") != request["endpoint"]
        or card.get("taskId") != waiting["task"]
        or card.get("interactionId") != waiting["interaction"]
        or card.get("type") != waiting["kind"]
        or not _valid_card(card, frozenset(request["trustedCardOrigins"]))
    ):
        return None
    return {
        "available": True,
        "kind": waiting["kind"],
        "url": card["presentation"]["url"],
        "interaction": card,
    }
