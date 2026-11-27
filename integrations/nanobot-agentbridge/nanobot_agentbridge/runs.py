"""One NanoBot instance owns this ledger and its background SDK runs."""

import asyncio
from collections.abc import Awaitable, Callable
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


Execute = Callable[[str, Callable[[str], None]], Awaitable[str]]


class RunCoordinator:
    """Use from one event loop. A browser observer never owns an execution task."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
            raise PermissionError("NanoBot run state requires a private, owner-only directory")
        self._lock = (directory / "owner.lock").open("a+b")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise RuntimeError("NanoBot instance already has a run coordinator") from None
        self._tasks: dict[str, asyncio.Task] = {}
        self._closed = False
        try:
            self._db = sqlite3.connect(directory / "runs.sqlite3")
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL, endpoint TEXT NOT NULL, conversation TEXT NOT NULL,
                    request_key TEXT NOT NULL, payload_digest TEXT NOT NULL,
                    state TEXT NOT NULL, answer TEXT,
                    UNIQUE (subject, request_key)
                );
                CREATE TABLE IF NOT EXISTS run_public_prompts (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id), text_digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    kind TEXT NOT NULL, text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS run_event_order ON run_events(run_id, sequence);
            """)
            # The prior owner might have crossed a business side-effect boundary.
            with self._db:
                self._db.execute("""INSERT INTO run_events(run_id, kind, text)
                    SELECT run_id, 'unknown', '' FROM runs WHERE state='accepted'""")
                self._db.execute("UPDATE runs SET state='unknown' WHERE state='accepted'")
        except BaseException:
            self._lock.close()
            raise

    def accept(self, *, subject: str, endpoint: str, conversation: str,
               request_key: str, payload_digest: str, execute: Execute, public_prompt: str | None = None) -> dict:
        if self._closed:
            raise RuntimeError("NanoBot run coordinator is closing")
        if not all(isinstance(value, str) and value.strip() for value in
                   (subject, endpoint, conversation, request_key, payload_digest)):
            raise ValueError("A verified binding, request key and payload digest are required")
        loop = asyncio.get_running_loop()
        with self._db:
            existing = self._db.execute("SELECT * FROM runs WHERE subject=? AND request_key=?",
                                        (subject, request_key)).fetchone()
            if existing is not None:
                if (existing["endpoint"], existing["conversation"], existing["payload_digest"]) != (
                    endpoint, conversation, payload_digest
                ):
                    raise ValueError("IDEMPOTENCY_PAYLOAD_MISMATCH")
                if public_prompt is not None:
                    proof = self._db.execute("SELECT text_digest FROM run_public_prompts WHERE run_id=?",(existing["run_id"],)).fetchone()
                    if proof and (not isinstance(public_prompt,str) or hashlib.sha256(public_prompt.encode()).hexdigest()!=proof[0]):
                        raise ValueError("IDEMPOTENCY_PAYLOAD_MISMATCH")
                return self._public(existing)
            run_id = str(uuid4())
            self._db.execute("""INSERT INTO runs
                (run_id,subject,endpoint,conversation,request_key,payload_digest,state)
                VALUES (?,?,?,?,?,?,'accepted')""",
                (run_id, subject, endpoint, conversation, request_key, payload_digest))
            if public_prompt is not None:
                if not isinstance(public_prompt,str) or not public_prompt.strip():
                    raise ValueError("Invalid original public prompt")
                self._db.execute("INSERT INTO run_public_prompts VALUES(?,?)",
                                 (run_id,hashlib.sha256(public_prompt.encode()).hexdigest()))
            self._event(run_id, "accepted")
        self._tasks[run_id] = loop.create_task(self._execute(run_id, execute))
        self._tasks[run_id].add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return {"run_id": run_id, "state": "accepted", "answer": None}

    async def _execute(self, run_id: str, execute: Execute) -> None:
        def emit(public_text: str) -> None:
            if not isinstance(public_text, str):
                raise TypeError("Only public text may enter the Workspace stream")
            with self._db:
                self._db.execute("""INSERT INTO run_events(run_id,kind,text)
                    SELECT run_id,'delta',? FROM runs WHERE run_id=? AND state='accepted'""",
                    (public_text, run_id))
        try:
            answer = await execute(run_id, emit)
            if not isinstance(answer, str):
                raise TypeError("Invalid NanoBot answer")
        except asyncio.CancelledError:
            self._finish(run_id, "unknown")
            raise
        except Exception:
            # SDK/provider exceptions can contain tool payloads. Record only the safe outcome.
            self._finish(run_id, "unknown")
        else:
            self._finish(run_id, "completed", answer)

    def _event(self, run_id: str, kind: str, text: str = "") -> None:
        self._db.execute("INSERT INTO run_events(run_id,kind,text) VALUES (?,?,?)",
                         (run_id, kind, text))

    def _finish(self, run_id: str, state: str, answer: str | None = None) -> None:
        with self._db:
            updated = self._db.execute(
                "UPDATE runs SET state=?, answer=? WHERE run_id=? AND state='accepted'",
                (state, answer, run_id))
            if updated.rowcount:
                self._event(run_id, state)

    def reconcile_verified(self, run_id: str, *, subject: str, endpoint: str, conversation: str,
                           task_id: str, operation: dict | None=None, expected_capability: str="oa.leave.save_draft",
                           batch_snapshot: dict | None=None, expected_batch_id: str | None=None) -> dict:
        binding = dict(subject=subject, endpoint=endpoint, conversation=conversation)
        original = self.snapshot(run_id, **binding)
        from .recovery import verified_answer
        answer = verified_answer(subject, endpoint, task_id, operation, expected_capability, batch_snapshot, expected_batch_id)
        if original["state"] == "unknown":
            with self._db:
                updated = self._db.execute("""UPDATE runs SET state='completed',answer=? WHERE run_id=? AND
                    subject=? AND endpoint=? AND conversation=? AND state='unknown'""",
                    (answer,run_id,subject,endpoint,conversation))
                if updated.rowcount:
                    self._event(run_id, "completed")
        return self.snapshot(run_id, **binding)

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        return {name: row[name] for name in ("run_id", "state", "answer")}

    def lookup(self, request_key: str, *, subject: str, endpoint: str, conversation: str) -> dict | None:
        row = self._db.execute("""SELECT run_id,state,answer FROM runs
            WHERE request_key=? AND subject=? AND endpoint=? AND conversation=?""",
            (request_key, subject, endpoint, conversation)).fetchone()
        return self._public(row) if row else None

    def matches_message(self, run_id: str, message: str) -> bool:
        from hashlib import sha256
        digest = sha256(message.encode()).hexdigest()
        return self._db.execute("""SELECT 1 FROM runs r LEFT JOIN run_public_prompts p USING(run_id)
            WHERE r.run_id=? AND COALESCE(p.text_digest,r.payload_digest)=?""", (run_id,digest)).fetchone() is not None

    def snapshot(self, run_id: str, *, subject: str, endpoint: str, conversation: str) -> dict:
        row = self._db.execute("""SELECT run_id,state,answer FROM runs
            WHERE run_id=? AND subject=? AND endpoint=? AND conversation=?""",
            (run_id, subject, endpoint, conversation)).fetchone()
        if row is None:
            raise KeyError("Unknown run for this binding")
        return self._public(row)

    def events(self, run_id: str, *, after: int = 0, subject: str, endpoint: str,
               conversation: str) -> list[dict]:
        self.snapshot(run_id, subject=subject, endpoint=endpoint, conversation=conversation)
        return [dict(row) for row in self._db.execute(
            """SELECT sequence,kind,text FROM run_events WHERE run_id=? AND sequence>?
               ORDER BY sequence LIMIT 200""", (run_id, after))]

    async def cancel(self, run_id: str, *, subject: str, endpoint: str, conversation: str) -> dict:
        binding = dict(subject=subject, endpoint=endpoint, conversation=conversation)
        self.snapshot(run_id, **binding)
        task = self._tasks.get(run_id)
        if task is not None:
            # Describes generation only. The central Operation ledger owns business status.
            self._finish(run_id, "stopped")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.snapshot(run_id, **binding)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = list(self._tasks.items())
        for run_id, task in pending:
            self._finish(run_id, "unknown")
            task.cancel()
        await asyncio.gather(*(task for _, task in pending), return_exceptions=True)
        self._db.close()
        self._lock.close()
