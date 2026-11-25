"""Deliver L2 credential cards only on the original authenticated binding."""
import asyncio
from urllib.parse import urlsplit


def valid_origin(value):
    if not isinstance(value, str) or any(c.isspace() or c == "\\" or ord(c) < 32 for c in value):
        return False
    try:
        url = urlsplit(value)
        return bool(url.hostname and url.scheme == "https" and not url.username and not url.password
                    and not url.fragment and value == "https://" + url.netloc)
    except ValueError:
        return False


def valid_card(card, origins):
    if not isinstance(card, dict) or card.get("schemaVersion") != "agentbridge.interaction.v1":
        return False
    view = card.get("presentation")
    if not isinstance(view, dict) or view.get("owner") != "agentbridge" or view.get("modelMustNotCollectValues") is not True:
        return False
    value = view.get("url")
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() or ord(c) < 32 or c == "\\" for c in value):
        return False
    try:
        url = urlsplit(value)
        origin = url.scheme + "://" + url.netloc
        return (not url.fragment and origin in origins and valid_origin(origin)
                and card.get("type") in {"credential", "business_input", "execution_authorization"}
                and card.get("state") in {"pending", "processing"}
                and isinstance(card.get("interactionId"), str) and 16 <= len(card["interactionId"]) <= 128)
    except ValueError:
        return False


class Cards:
    def __init__(self, db, origins):
        self.db = db
        if not isinstance(origins, list) or not all(valid_origin(v) for v in origins):
            raise ValueError("Expected exact HTTPS private interaction origins")
        self.origins = frozenset(origins)
        db.execute("""CREATE TABLE IF NOT EXISTS cards(
            run TEXT PRIMARY KEY, interaction TEXT UNIQUE NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
            task TEXT)""")
        if "task" not in {row[1] for row in db.execute("PRAGMA table_info(cards)")}:
            db.execute("ALTER TABLE cards ADD COLUMN task TEXT")

    def waiting(self, run):
        return self.db.execute("SELECT * FROM cards WHERE run=? AND state='waiting'", (run,)).fetchone()

    async def present(self, central, record, interaction, *, task=None, kind="credential"):
        await central.require_binding()
        response = await central.session.call_tool("agentbridge_host_interaction_present",
                    {"agent_host": "pi", "endpoint_key": record["endpoint_key"], "interaction_id": interaction}, meta=central.meta)
        payload = response.structuredContent or {}
        card = payload.get("interaction")
        if (response.isError or (payload.get("endpoint") or {}).get("endpointId") != record["endpoint_id"]
                or not valid_card(card, self.origins) or card.get("interactionId") != interaction
                or card.get("taskId") != task or card["type"] != kind):
            return None
        return {"available": True, "kind": kind, "url": card["presentation"]["url"], "interaction": card}

    async def resume_credential(self, central, record, run, raw, *, task_meta=None):
        meta = task_meta or central.meta
        task = (meta.get("io.agentbridge/task") or {}).get("taskId")
        # The initial envelope can precede the central task linkage in the response.
        # The fresh endpoint presentation below must contain the exact actual task.
        if not valid_card(raw, self.origins) or raw["type"] != "credential" or raw.get("taskId") not in (None, task):
            return "unknown"
        ident = raw["interactionId"]
        if not await self.present(central, record, ident, task=task):
            return "unknown"
        with self.db:
            admitted = self.db.execute("""INSERT INTO cards (run,interaction,state,revision,task) VALUES(?,?,'waiting',1,?)
                ON CONFLICT(run) DO UPDATE SET interaction=excluded.interaction,state='waiting',revision=revision+1,task=excluded.task
                WHERE cards.state IN ('completed','closed') AND cards.interaction<>excluded.interaction""", (run, ident, task))
            if admitted.rowcount != 1:
                return "unknown"
        state = "unknown"
        try:
            async with asyncio.timeout(210):
                while True:
                    await central.require_binding()
                    observed = await central.session.call_tool("agentbridge_interaction_get",
                        {"interaction_id": ident, "wait_seconds": 4}, meta=meta)
                    card = ((observed.meta or {}).get("io.agentbridge/interaction")
                            or (observed.structuredContent or {}).get("interaction") or {})
                    if observed.isError or card.get("interactionId") != ident or card.get("type") != "credential" or card.get("taskId") != task:
                        break
                    if card.get("state") in {"expired", "declined", "failed", "superseded"}:
                        state = "closed"
                        break
                    resume = card.get("resume") or {}
                    if card.get("state") == "completed" and resume.get("completed") is True:
                        state = "completed"
                        break
                    if card.get("state") == "completed" and resume.get("ready") is True:
                        # Claim before contacting central. A lost reply must not replay a resume.
                        with self.db:
                            if self.db.execute("UPDATE cards SET state='resuming' WHERE run=? AND interaction=? AND state='waiting'",
                                               (run, ident)).rowcount != 1:
                                break
                        await central.require_binding()
                        result = await central.session.call_tool("agentbridge_interaction_resume",
                            {"interaction_id": ident, "idempotency_key": f"pi:{run}:{ident}"},
                            meta={**meta, "io.agentbridge/session-ready-only": True})
                        if not result.isError and (result.structuredContent or {}).get("status") == "succeeded":
                            state = "completed"
                        break
                    await asyncio.sleep(.2)
        finally:
            with self.db:
                self.db.execute("UPDATE cards SET state=? WHERE run=? AND interaction=?", (state, run, ident))
        return state
