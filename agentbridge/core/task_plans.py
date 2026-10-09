from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator
from uuid import uuid4


ACTIVE_PLAN_STATES = frozenset({"validated", "running", "waiting_user"})
TERMINAL_PLAN_STATES = frozenset(
    {"succeeded", "failed", "outcome_unknown", "canceled"}
)
ACTIVE_STEP_STATES = frozenset({"queued", "running", "waiting_user"})
TERMINAL_STEP_STATES = frozenset(
    {"succeeded", "failed", "outcome_unknown", "canceled", "skipped"}
)


class TaskPlanNotFound(KeyError):
    pass


class TaskPlanConflict(RuntimeError):
    pass


class TaskPlanIntegrityError(RuntimeError):
    pass


class TaskPlanCommitInProgress(TaskPlanConflict):
    pass


class TaskPlanStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_plans (
                    plan_id TEXT PRIMARY KEY,
                    parent_task_id TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    proposal_source TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    proposal_summary_json TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    current_step_key TEXT,
                    risk_summary_json TEXT NOT NULL,
                    coordinator_lease_version INTEGER,
                    idempotency_key TEXT,
                    terminal_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS task_plans_idempotency
                ON task_plans (user_subject, parent_task_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL;

                CREATE UNIQUE INDEX IF NOT EXISTS task_plans_active_task
                ON task_plans (parent_task_id)
                WHERE state IN ('validated', 'running', 'waiting_user');

                CREATE INDEX IF NOT EXISTS task_plans_user_updated
                ON task_plans (user_subject, updated_at DESC);

                CREATE TABLE IF NOT EXISTS task_plan_steps (
                    step_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    step_key TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    capability_name TEXT,
                    transform_name TEXT,
                    target_version TEXT NOT NULL,
                    depends_on_json TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    binding_json TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    system_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    operation_id TEXT,
                    interaction_id TEXT,
                    input_hash TEXT,
                    output_hash TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE (plan_id, step_key),
                    UNIQUE (plan_id, ordinal)
                );

                CREATE INDEX IF NOT EXISTS task_plan_steps_interaction
                ON task_plan_steps (interaction_id)
                WHERE interaction_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS task_plan_steps_plan_state
                ON task_plan_steps (plan_id, state, ordinal);
                """
            )
            self._migrate_v2(connection)

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(task_plans)").fetchall()
        }
        additions = {
            "schema_version": "TEXT",
            "temporal_context_json": "TEXT",
            "authority_snapshot_json": "TEXT",
            "result_projection_json": "TEXT",
            "effect_outcome": "TEXT",
            "operation_key_prefix": "TEXT",
            "commit_operation_id": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE task_plans ADD COLUMN {name} {sql_type}"
                )

    def create(
        self,
        *,
        user_subject: str,
        parent_task_id: str,
        compiled_plan: dict[str, Any],
        proposal_source: str,
        coordinator_lease_version: int | None,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        user_subject = _required_text(user_subject, "user_subject", 256)
        parent_task_id = _required_text(parent_task_id, "parent_task_id", 128)
        proposal_source = _required_text(proposal_source, "proposal_source", 80)
        if proposal_source not in {"agent_host", "template", "central_planner"}:
            raise ValueError("unsupported proposal source")
        goal = _required_text(compiled_plan.get("goal"), "goal", 500)
        plan_hash = _required_text(compiled_plan.get("planHash"), "plan_hash", 64)
        steps = compiled_plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("compiled plan steps are required")
        normalized_key = str(idempotency_key or "").strip() or None
        if normalized_key and len(normalized_key) > 256:
            raise ValueError("plan idempotency key is too long")
        risk_summary = compiled_plan.get("riskSummary") or {}
        now = _utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if normalized_key:
                existing = connection.execute(
                    """
                    SELECT * FROM task_plans
                    WHERE user_subject = ? AND parent_task_id = ?
                      AND idempotency_key = ?
                    """,
                    (user_subject, parent_task_id, normalized_key),
                ).fetchone()
                if existing is not None:
                    if existing["plan_hash"] != plan_hash:
                        raise TaskPlanConflict(
                            "plan idempotency key was reused with different content"
                        )
                    return self._snapshot(connection, existing), True
            active = connection.execute(
                """
                SELECT plan_id FROM task_plans
                WHERE parent_task_id = ?
                  AND state IN ('validated', 'running', 'waiting_user')
                """,
                (parent_task_id,),
            ).fetchone()
            if active is not None:
                raise TaskPlanConflict("task already has an active plan")
            revision_row = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS value FROM task_plans WHERE parent_task_id = ?",
                (parent_task_id,),
            ).fetchone()
            revision = int(revision_row["value"] or 0) + 1
            plan_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO task_plans (
                    plan_id, parent_task_id, user_subject, proposal_source,
                    goal, proposal_summary_json, plan_hash, revision, state,
                    current_step_key, risk_summary_json,
                    coordinator_lease_version, idempotency_key, schema_version,
                    temporal_context_json, authority_snapshot_json,
                    created_at, updated_at, operation_key_prefix
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'validated', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    parent_task_id,
                    user_subject,
                    proposal_source,
                    goal,
                    _canonical_json(
                        {
                            "schemaVersion": "agentbridge.task-plan.summary.v1",
                            "goal": goal,
                            "stepCount": len(steps),
                        }
                    ),
                    plan_hash,
                    revision,
                    _canonical_json(risk_summary),
                    coordinator_lease_version,
                    normalized_key,
                    str(
                        compiled_plan.get("proposalSchemaVersion")
                        or "agentbridge.task-plan.proposal.v1"
                    ),
                    _canonical_json(compiled_plan.get("temporalContext")),
                    _canonical_json(compiled_plan.get("authoritySnapshot")),
                    now,
                    now,
                    f"task-plan:{plan_id}:{revision}",
                ),
            )
            for step in steps:
                connection.execute(
                    """
                    INSERT INTO task_plan_steps (
                        step_id, plan_id, step_key, ordinal, kind, title,
                        capability_name, transform_name, target_version,
                        depends_on_json, arguments_json, binding_json,
                        effect, system_id, state, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                    """,
                    (
                        str(uuid4()),
                        plan_id,
                        step["stepKey"],
                        int(step["ordinal"]),
                        step["kind"],
                        str(step.get("title") or step["stepKey"])[:240],
                        step.get("capabilityName"),
                        step.get("transformName"),
                        str(step.get("version") or "1")[:40],
                        _canonical_json(step.get("dependsOn") or []),
                        _canonical_json(step.get("arguments") or {}),
                        _canonical_json(step.get("bindings") or {}),
                        step["effect"],
                        step["systemId"],
                        now,
                    ),
                )
            row = self._select_plan(connection, plan_id)
        return self._snapshot(connection=None, plan_row=row), False

    def get(self, plan_id: str, *, user_subject: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._select_owned_plan(connection, plan_id, user_subject)
            return self._snapshot(connection, row)

    def get_for_task(
        self,
        *,
        parent_task_id: str,
        user_subject: str,
        active_only: bool = False,
    ) -> dict[str, Any] | None:
        query = (
            "SELECT * FROM task_plans WHERE parent_task_id = ? AND user_subject = ?"
        )
        parameters: list[Any] = [parent_task_id, user_subject]
        if active_only:
            query += " AND state IN ('validated', 'running', 'waiting_user')"
        query += " ORDER BY revision DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
            return self._snapshot(connection, row) if row is not None else None

    def find_by_interaction(
        self, interaction_id: str, *, user_subject: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT plan.* FROM task_plans AS plan
                JOIN task_plan_steps AS step ON step.plan_id = plan.plan_id
                WHERE step.interaction_id = ? AND plan.user_subject = ?
                ORDER BY plan.revision DESC LIMIT 1
                """,
                (interaction_id, user_subject),
            ).fetchone()
            return self._snapshot(connection, row) if row is not None else None

    def begin_next_step(
        self, plan_id: str, *, user_subject: str
    ) -> dict[str, Any] | None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = self._select_owned_plan(connection, plan_id, user_subject)
            if plan["state"] in TERMINAL_PLAN_STATES:
                return None
            if plan["current_step_key"]:
                current = self._select_step(
                    connection, plan_id, plan["current_step_key"]
                )
                if current["state"] in {"running", "waiting_user"}:
                    result = _step_from_row(current, include_private=True)
                    result["just_started"] = False
                    return result
            rows = connection.execute(
                "SELECT * FROM task_plan_steps WHERE plan_id = ? ORDER BY ordinal",
                (plan_id,),
            ).fetchall()
            states = {row["step_key"]: row["state"] for row in rows}
            selected = next(
                (
                    row
                    for row in rows
                    if row["state"] == "queued"
                    and all(states.get(dep) == "succeeded" for dep in _json(row["depends_on_json"]))
                ),
                None,
            )
            if selected is None:
                return None
            connection.execute(
                """
                UPDATE task_plan_steps
                SET state = 'running', attempt_count = attempt_count + 1,
                    started_at = COALESCE(started_at, ?), updated_at = ?,
                    error_code = NULL, error_message = NULL
                WHERE step_id = ? AND state = 'queued'
                """,
                (now, now, selected["step_id"]),
            )
            connection.execute(
                """
                UPDATE task_plans
                SET state = 'running', current_step_key = ?, updated_at = ?
                WHERE plan_id = ?
                """,
                (selected["step_key"], now, plan_id),
            )
            refreshed = self._select_step(connection, plan_id, selected["step_key"])
            result = _step_from_row(refreshed, include_private=True)
            result["just_started"] = True
            return result

    def mark_step_waiting(
        self,
        plan_id: str,
        *,
        user_subject: str,
        step_key: str,
        operation_id: str,
        interaction_id: str,
        input_hash: str | None,
    ) -> dict[str, Any]:
        return self._update_step(
            plan_id,
            user_subject=user_subject,
            step_key=step_key,
            state="waiting_user",
            plan_state="waiting_user",
            operation_id=operation_id,
            interaction_id=interaction_id,
            input_hash=input_hash,
            terminal=False,
        )

    def mark_step_succeeded(
        self,
        plan_id: str,
        *,
        user_subject: str,
        step_key: str,
        operation_id: str,
        input_hash: str | None,
        output_hash: str | None,
    ) -> dict[str, Any]:
        return self._update_step(
            plan_id,
            user_subject=user_subject,
            step_key=step_key,
            state="succeeded",
            plan_state="running",
            operation_id=operation_id,
            interaction_id=None,
            input_hash=input_hash,
            output_hash=output_hash,
            terminal=True,
            clear_current=True,
        )

    def mark_step_failed(
        self,
        plan_id: str,
        *,
        user_subject: str,
        step_key: str,
        operation_id: str | None,
        state: str,
        error_code: str,
        error_message: str,
        effect_outcome: str | None = None,
    ) -> dict[str, Any]:
        if state not in {"failed", "outcome_unknown"}:
            raise ValueError("unsupported failed step state")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = self._select_owned_plan(connection, plan_id, user_subject)
            if plan["state"] in TERMINAL_PLAN_STATES:
                return self._snapshot(connection, plan)
            self._select_step(connection, plan_id, step_key)
            connection.execute(
                """
                UPDATE task_plan_steps
                SET state = ?, operation_id = COALESCE(?, operation_id),
                    interaction_id = NULL, error_code = ?, error_message = ?,
                    updated_at = ?, finished_at = ?
                WHERE plan_id = ? AND step_key = ?
                """,
                (
                    state,
                    operation_id,
                    str(error_code)[:120],
                    str(error_message)[:500],
                    now,
                    now,
                    plan_id,
                    step_key,
                ),
            )
            connection.execute(
                """
                UPDATE task_plan_steps SET state = 'canceled', updated_at = ?, finished_at = ?
                WHERE plan_id = ? AND state = 'queued'
                """,
                (now, now, plan_id),
            )
            connection.execute(
                """
                UPDATE task_plans
                SET state = ?, terminal_reason = ?, current_step_key = ?,
                    effect_outcome = ?, updated_at = ?, finished_at = ?
                WHERE plan_id = ?
                """,
                (
                    state,
                    str(error_code)[:120],
                    step_key,
                    str(effect_outcome or "")[:80] or None,
                    now,
                    now,
                    plan_id,
                ),
            )
            return self._snapshot(connection, self._select_plan(connection, plan_id))

    def reset_step_after_session(
        self,
        plan_id: str,
        *,
        user_subject: str,
        interaction_id: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = self._select_owned_plan(connection, plan_id, user_subject)
            if plan["state"] in TERMINAL_PLAN_STATES:
                return self._snapshot(connection, plan)
            step = connection.execute(
                """
                SELECT * FROM task_plan_steps
                WHERE plan_id = ? AND interaction_id = ?
                """,
                (plan_id, interaction_id),
            ).fetchone()
            if step is None:
                raise TaskPlanIntegrityError("interaction is not bound to this plan")
            connection.execute(
                """
                UPDATE task_plan_steps
                SET state = 'queued', operation_id = NULL, interaction_id = NULL,
                    input_hash = NULL, output_hash = NULL, updated_at = ?,
                    finished_at = NULL
                WHERE step_id = ?
                """,
                (now, step["step_id"]),
            )
            connection.execute(
                """
                UPDATE task_plans
                SET state = 'running', current_step_key = NULL, updated_at = ?
                WHERE plan_id = ?
                """,
                (now, plan_id),
            )
            return self._snapshot(connection, self._select_plan(connection, plan_id))

    def complete(
        self,
        plan_id: str,
        *,
        user_subject: str,
        reason: str,
        skip_queued: bool = False,
        effect_outcome: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = self._select_owned_plan(connection, plan_id, user_subject)
            if plan["state"] == "succeeded":
                return self._snapshot(connection, plan)
            if plan["state"] in TERMINAL_PLAN_STATES:
                raise TaskPlanIntegrityError("terminal plan cannot be completed")
            if skip_queued:
                connection.execute(
                    """
                    UPDATE task_plan_steps
                    SET state = 'skipped', updated_at = ?, finished_at = ?
                    WHERE plan_id = ? AND state = 'queued'
                    """,
                    (now, now, plan_id),
                )
            remaining = connection.execute(
                """
                SELECT COUNT(*) AS value FROM task_plan_steps
                WHERE plan_id = ? AND state IN ('queued', 'running', 'waiting_user')
                """,
                (plan_id,),
            ).fetchone()
            if int(remaining["value"] or 0):
                raise TaskPlanIntegrityError("plan still has active steps")
            connection.execute(
                """
                UPDATE task_plans
                SET state = 'succeeded', terminal_reason = ?,
                    effect_outcome = ?, current_step_key = NULL,
                    updated_at = ?, finished_at = ?
                WHERE plan_id = ?
                """,
                (str(reason)[:120], str(effect_outcome or "")[:80] or None, now, now, plan_id),
            )
            return self._snapshot(connection, self._select_plan(connection, plan_id))

    def set_result_projection(
        self,
        plan_id: str,
        *,
        user_subject: str,
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._select_owned_plan(connection, plan_id, user_subject)
            connection.execute(
                """
                UPDATE task_plans
                SET result_projection_json = ?, updated_at = ?
                WHERE plan_id = ?
                """,
                (_canonical_json(projection), now, plan_id),
            )
            return self._snapshot(connection, self._select_plan(connection, plan_id))

    def cancel(
        self, plan_id: str, *, user_subject: str, reason: str
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = self._select_owned_plan(connection, plan_id, user_subject)
            if plan["state"] in TERMINAL_PLAN_STATES:
                return self._snapshot(connection, plan)
            if plan["commit_operation_id"]:
                raise TaskPlanCommitInProgress(
                    "计划已进入提交边界，须等待权威核验，不能标记为已取消。"
                )
            connection.execute(
                """
                UPDATE task_plan_steps
                SET state = 'canceled', updated_at = ?, finished_at = ?
                WHERE plan_id = ? AND state IN ('queued', 'running', 'waiting_user')
                """,
                (now, now, plan_id),
            )
            connection.execute(
                """
                UPDATE task_plans
                SET state = 'canceled', terminal_reason = ?,
                    current_step_key = NULL, updated_at = ?, finished_at = ?
                WHERE plan_id = ?
                """,
                (str(reason)[:120], now, now, plan_id),
            )
            return self._snapshot(connection, self._select_plan(connection, plan_id))

    def guard_authorization_consumption(
        self,
        connection: sqlite3.Connection,
        *,
        authorization_id: str,
        user_subject: str,
        operation_id: str,
        validate: Callable[[dict], None],
    ) -> None:
        """Called inside the authorization's BEGIN IMMEDIATE transaction.

        The marker and the one-shot authorization are committed together. Cancel
        takes the same SQLite writer lock, including across service instances.
        """
        row = connection.execute(
            """
            SELECT plan.* FROM task_plans AS plan
            JOIN task_plan_steps AS step ON step.plan_id = plan.plan_id
            JOIN interactions AS interaction ON interaction.interaction_id = step.interaction_id
            WHERE interaction.resource_id = ?
              AND interaction.interaction_type = 'execution_authorization'
              AND plan.user_subject = ?
            """,
            (authorization_id, user_subject),
        ).fetchone()
        if row is None:
            return  # An atomic, non-plan write has no parent plan.
        if row["state"] not in ACTIVE_PLAN_STATES:
            raise TaskPlanIntegrityError("任务计划已结束，不能消费执行授权。")
        if row["commit_operation_id"]:
            raise TaskPlanCommitInProgress("任务计划已有提交在途，不能再次提交。")
        validate(self._snapshot(connection, row))
        now = _utc_now()
        connection.execute(
            "UPDATE task_plans SET commit_operation_id = ?, state = 'running', updated_at = ? WHERE plan_id = ?",
            (operation_id, now, row["plan_id"]),
        )
        connection.execute(
            "UPDATE task_plan_steps SET state = 'running', updated_at = ? WHERE plan_id = ? AND step_key = ?",
            (now, row["plan_id"], row["current_step_key"]),
        )

    def step_for_interaction(
        self, interaction_id: str, *, user_subject: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT step.*, plan.user_subject AS plan_user_subject
                FROM task_plan_steps AS step
                JOIN task_plans AS plan ON plan.plan_id = step.plan_id
                WHERE step.interaction_id = ? AND plan.user_subject = ?
                ORDER BY plan.revision DESC LIMIT 1
                """,
                (interaction_id, user_subject),
            ).fetchone()
            if row is None:
                return None
            plan = self._select_owned_plan(connection, row["plan_id"], user_subject)
            return (
                self._snapshot(connection, plan),
                _step_from_row(row, include_private=True),
            )

    def step_output_operation(
        self, plan_id: str, *, user_subject: str, step_key: str
    ) -> str:
        with self._connect() as connection:
            self._select_owned_plan(connection, plan_id, user_subject)
            row = self._select_step(connection, plan_id, step_key)
            if row["state"] != "succeeded" or not row["operation_id"]:
                raise TaskPlanIntegrityError(
                    f"step output is unavailable: {step_key}"
                )
            return str(row["operation_id"])

    def list(
        self, *, user_subject: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = min(max(int(limit), 1), 500)
        query = "SELECT * FROM task_plans"
        parameters: list[Any] = []
        if user_subject:
            query += " WHERE user_subject = ?"
            parameters.append(user_subject)
        query += " ORDER BY updated_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._snapshot(connection, row) for row in rows]

    def recovery_candidates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = min(max(int(limit), 1), 500)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_plans
                WHERE state IN ('validated', 'running', 'waiting_user')
                ORDER BY CASE WHEN state = 'waiting_user' THEN 1 ELSE 0 END,
                         updated_at, created_at
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._snapshot(connection, row) for row in rows]

    def reset_running_step_for_recovery(
        self,
        plan_id: str,
        *,
        user_subject: str,
        step_key: str,
        expected_attempt_count: int,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = self._select_owned_plan(connection, plan_id, user_subject)
            if plan["state"] in TERMINAL_PLAN_STATES:
                return self._snapshot(connection, plan)
            step = self._select_step(connection, plan_id, step_key)
            if (
                plan["current_step_key"] != step_key
                or step["state"] != "running"
                or int(step["attempt_count"]) != int(expected_attempt_count)
            ):
                return self._snapshot(connection, plan)
            connection.execute(
                """
                UPDATE task_plan_steps
                SET state = 'queued', operation_id = NULL, interaction_id = NULL,
                    input_hash = NULL, output_hash = NULL,
                    error_code = NULL, error_message = NULL,
                    updated_at = ?, finished_at = NULL
                WHERE step_id = ?
                """,
                (now, step["step_id"]),
            )
            connection.execute(
                """
                UPDATE task_plans
                SET state = 'running', current_step_key = NULL, updated_at = ?
                WHERE plan_id = ?
                """,
                (now, plan_id),
            )
            return self._snapshot(connection, self._select_plan(connection, plan_id))

    def _update_step(
        self,
        plan_id: str,
        *,
        user_subject: str,
        step_key: str,
        state: str,
        plan_state: str,
        operation_id: str,
        interaction_id: str | None,
        input_hash: str | None,
        output_hash: str | None = None,
        terminal: bool,
        clear_current: bool = False,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = self._select_owned_plan(connection, plan_id, user_subject)
            if plan["state"] in TERMINAL_PLAN_STATES:
                return self._snapshot(connection, plan)
            step = self._select_step(connection, plan_id, step_key)
            if step["state"] not in {"running", "waiting_user"}:
                raise TaskPlanIntegrityError(
                    f"step cannot transition from {step['state']} to {state}"
                )
            connection.execute(
                """
                UPDATE task_plan_steps
                SET state = ?, operation_id = ?, interaction_id = ?,
                    input_hash = COALESCE(?, input_hash),
                    output_hash = COALESCE(?, output_hash),
                    error_code = NULL, error_message = NULL,
                    updated_at = ?, finished_at = ?
                WHERE step_id = ?
                """,
                (
                    state,
                    operation_id,
                    interaction_id,
                    input_hash,
                    output_hash,
                    now,
                    now if terminal else None,
                    step["step_id"],
                ),
            )
            connection.execute(
                """
                UPDATE task_plans
                SET state = ?, current_step_key = ?, updated_at = ?,
                    commit_operation_id = CASE WHEN ? = 'waiting_user' THEN NULL ELSE commit_operation_id END
                WHERE plan_id = ?
                """,
                (plan_state, None if clear_current else step_key, now, state, plan_id),
            )
            return self._snapshot(connection, self._select_plan(connection, plan_id))

    def _snapshot(
        self,
        connection: sqlite3.Connection | None,
        plan_row: sqlite3.Row,
    ) -> dict[str, Any]:
        if connection is None:
            with self._connect() as owned:
                row = self._select_plan(owned, plan_row["plan_id"])
                return self._snapshot(owned, row)
        plan = _plan_from_row(plan_row)
        rows = connection.execute(
            "SELECT * FROM task_plan_steps WHERE plan_id = ? ORDER BY ordinal",
            (plan["plan_id"],),
        ).fetchall()
        plan["steps"] = [_step_from_row(row) for row in rows]
        counts: dict[str, int] = {}
        for step in plan["steps"]:
            counts[step["state"]] = counts.get(step["state"], 0) + 1
        plan["step_counts"] = counts
        return plan

    @staticmethod
    def _select_plan(connection: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM task_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise TaskPlanNotFound(f"task plan not found: {plan_id}")
        return row

    def _select_owned_plan(
        self, connection: sqlite3.Connection, plan_id: str, user_subject: str
    ) -> sqlite3.Row:
        row = self._select_plan(connection, plan_id)
        if row["user_subject"] != user_subject:
            raise TaskPlanNotFound(f"task plan not found: {plan_id}")
        return row

    @staticmethod
    def _select_step(
        connection: sqlite3.Connection, plan_id: str, step_key: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM task_plan_steps WHERE plan_id = ? AND step_key = ?",
            (plan_id, step_key),
        ).fetchone()
        if row is None:
            raise TaskPlanIntegrityError(f"task plan step not found: {step_key}")
        return row


def task_plan_response(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": "agentbridge.task-plan.v2",
        "proposalSchemaVersion": plan.get("schema_version")
        or "agentbridge.task-plan.proposal.v1",
        "planId": plan["plan_id"],
        "taskId": plan["parent_task_id"],
        "goal": plan["goal"],
        "revision": plan["revision"],
        "state": plan["state"],
        "currentStepKey": plan.get("current_step_key"),
        "riskSummary": plan["risk_summary"],
        "terminalReason": plan.get("terminal_reason"),
        "effectOutcome": plan.get("effect_outcome"),
        "temporalContext": plan.get("temporal_context"),
        "resultProjection": plan.get("result_projection"),
        "createdAt": plan["created_at"],
        "updatedAt": plan["updated_at"],
        "finishedAt": plan.get("finished_at"),
        "stepCounts": plan.get("step_counts") or {},
        "steps": [
            {
                "stepKey": step["step_key"],
                "ordinal": step["ordinal"],
                "kind": step["kind"],
                "title": step["title"],
                "capabilityName": step.get("capability_name"),
                "transformName": step.get("transform_name"),
                "systemId": step["system_id"],
                "effect": step["effect"],
                "state": step["state"],
                "operationId": step.get("operation_id"),
                "interactionId": step.get("interaction_id"),
                "errorCode": step.get("error_code"),
            }
            for step in plan["steps"]
        ],
    }


def _plan_from_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["proposal_summary"] = _json(value.pop("proposal_summary_json"))
    value["risk_summary"] = _json(value.pop("risk_summary_json"))
    value["temporal_context"] = _json(value.pop("temporal_context_json", None))
    value["authority_snapshot"] = _json(value.pop("authority_snapshot_json", None))
    value["result_projection"] = _json(value.pop("result_projection_json", None))
    return value


def _step_from_row(
    row: sqlite3.Row, *, include_private: bool = False
) -> dict[str, Any]:
    value = dict(row)
    value.pop("plan_user_subject", None)
    value["depends_on"] = _json(value.pop("depends_on_json"))
    value["bindings"] = _json(value.pop("binding_json"))
    if include_private:
        value["arguments"] = _json(value.pop("arguments_json"))
    else:
        value.pop("arguments_json", None)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def step_idempotency_key(plan: dict, step: dict) -> str:
    # NULL identifies pre-migration plans: their in-flight Operations must keep
    # the original lookup key. New plans always persist an instance-specific key.
    prefix = plan.get("operation_key_prefix") or f"task-plan:{plan['plan_hash']}"
    return f"{prefix}:{step['step_key']}:attempt:{step['attempt_count']}"


def _required_text(value: Any, name: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    if len(normalized) > maximum:
        raise ValueError(f"{name} is too long")
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
