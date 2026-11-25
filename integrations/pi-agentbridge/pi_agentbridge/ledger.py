"""One owner per Pi instance. Only public conversation text enters the run journal."""

import asyncio
import fcntl
import json
import math
import sqlite3
from uuid import uuid4


class Conflict(ValueError):
    pass


class UnconfirmedOperation(Exception):
    """The private coordinator could not verify the original business effect."""


class Ledger:
    def __init__(self, directory, instance):
        self.lock = (directory / "pi.lock").open("ab")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(directory / "pi-runs.sqlite3")
            self.db.row_factory = sqlite3.Row
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS owner(instance TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, subject TEXT NOT NULL, binding TEXT NOT NULL,
                    key TEXT NOT NULL, digest TEXT NOT NULL, prompt TEXT NOT NULL, answer TEXT,
                    state TEXT NOT NULL, UNIQUE(subject,key));
                CREATE TABLE IF NOT EXISTS events(
                    sequence INTEGER PRIMARY KEY, run TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS run_events ON events(run,sequence);
                CREATE TABLE IF NOT EXISTS history_usage(run TEXT PRIMARY KEY, usage TEXT NOT NULL);
            """)
            with self.db:
                owner = self.db.execute("SELECT instance FROM owner").fetchone()
                if owner and owner[0] != instance:
                    raise ValueError("Pi state belongs to a different instance")
                self.db.execute("INSERT OR IGNORE INTO owner VALUES(?)", (instance,))
                self.db.execute("INSERT INTO events(run,kind,text) SELECT id,'unknown','' FROM runs WHERE state='accepted'")
                self.db.execute("UPDATE runs SET state='unknown' WHERE state='accepted'")
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.close()
            raise
        self.active = {}
        self.locks = {}

    def lookup(self, binding, key):
        return self.db.execute("SELECT * FROM runs WHERE binding=? AND key=?", (binding, key)).fetchone()

    def snapshot(self, run, *, subject, endpoint, conversation):
        row = self.db.execute("SELECT * FROM runs WHERE id=? AND subject=?", (run, subject)).fetchone()
        if row is None:
            raise KeyError("Unknown run")
        principal, ep, _, session, _ = json.loads(row["binding"])
        if (principal, ep, session) != (subject, endpoint, conversation):
            raise KeyError("Unknown original binding")
        return row

    def reconcile_verified(self, run, *, subject, endpoint, conversation, task_id, operation=None, expected_capability="",
                           batch_snapshot=None, expected_batch_id=None):
        from nanobot_agentbridge.recovery import verified_answer
        row = self.snapshot(run, subject=subject, endpoint=endpoint, conversation=conversation)
        answer = verified_answer(subject, endpoint, task_id, operation, expected_capability, batch_snapshot, expected_batch_id)
        with self.db:
            changed = self.db.execute("UPDATE runs SET state='completed',answer=? WHERE id=? AND binding=? AND state='unknown'",
                                      (answer, run, row["binding"]))
            if changed.rowcount:
                self.event(run, "completed", "")

    def accept(self, subject, binding, key, digest, prompt, execute):
        with self.db:
            found = self.db.execute("SELECT * FROM runs WHERE subject=? AND key=?", (subject, key)).fetchone()
            if found:
                if found["binding"] != binding or found["digest"] != digest:
                    raise Conflict("Pi request payload or binding changed")
                return found
            run = str(uuid4())
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,NULL,'accepted')", (run, subject, binding, key, digest, prompt))
            self.event(run, "accepted", "")
        self.active[run] = asyncio.create_task(self._run(run, binding, execute))
        self.active[run].add_done_callback(lambda _: self.active.pop(run, None))
        return self.lookup(binding, key)

    async def _run(self, run, binding, execute):
        try:
            async with self.locks.setdefault(binding, asyncio.Lock()):
                rows = self.db.execute("""SELECT r.prompt,r.answer,u.usage FROM runs r LEFT JOIN history_usage u ON u.run=r.id
                    WHERE r.binding=? AND r.state='completed' ORDER BY r.rowid DESC LIMIT 20""", (binding,)).fetchall()
                history, size = [], 0
                for row in rows:
                    size += len(row["prompt"]) + len(row["answer"])
                    if size > 64_000:
                        break
                    history.append({**dict(row), "usage": json.loads(row["usage"]) if row["usage"] else None})
                def emit(kind, text):
                    with self.db:
                        self.event(run, kind, text)
                answer = await execute(run, history[::-1], emit)
        except asyncio.CancelledError:
            self.finish(run, "unknown")
            raise
        except UnconfirmedOperation:
            self.finish(run, "unknown", "中央尚未核验原任务的业务结果，请在原任务中核对。")
        except Exception:
            self.finish(run, "unknown")
        else:
            self.finish(run, "completed", answer)

    def event(self, run, kind, text):
        self.db.execute("INSERT INTO events(run,kind,text) VALUES(?,?,?)", (run, kind, text))

    def record_usage(self, run, usage):
        if not isinstance(usage, dict) or not isinstance(usage.get("cost"), dict):
            return
        counts = {name: usage.get(name) for name in ("input", "output", "totalTokens", "cacheRead", "cacheWrite")}
        costs = {name: usage["cost"].get(name) for name in ("input", "output", "cacheRead", "cacheWrite", "total")}
        if any(type(value) is not int or value < 0 for value in counts.values()) or any(
                type(value) not in (float, int) or not math.isfinite(value) or value < 0 for value in costs.values()):
            return
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO history_usage VALUES(?,?)", (run, json.dumps({**counts, "cost": costs})))

    def finish(self, run, state, answer=None):
        with self.db:
            changed = self.db.execute("UPDATE runs SET state=?,answer=? WHERE id=? AND state='accepted'", (state, answer, run))
            if changed.rowcount:
                self.event(run, state, "")

    def events(self, run, after):
        rows = self.db.execute("SELECT sequence,kind,text FROM events WHERE run=? AND sequence>? ORDER BY sequence LIMIT 200", (run, after))
        return [dict(row) for row in rows]

    async def stop(self, run, state):
        job = self.active.get(run)
        if job is not None:
            self.finish(run, state)
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def close(self):
        await asyncio.gather(*(self.stop(run, "unknown") for run in tuple(self.active)))
        self.db.close()
        self.lock.close()
