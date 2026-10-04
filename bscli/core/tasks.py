from __future__ import annotations
import re

from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from urllib.parse import quote, urlparse
from uuid import uuid4


TASK_STATUSES = {
    "active",
    "waiting_user",
    "running",
    "succeeded",
    "failed",
    "outcome_unknown",
    "canceled",
    "expired",
    "superseded",
    "partially_succeeded",
}

ACTIVE_TASK_STATUSES = {"active", "waiting_user", "running"}
TERMINAL_TASK_STATUSES = TASK_STATUSES - ACTIVE_TASK_STATUSES
BATCH_STATES = {
    "running",
    "waiting_user",
    "paused",
    "succeeded",
    "partially_succeeded",
    "failed",
    "outcome_unknown",
    "canceled",
    "expired",
}
ACTIVE_BATCH_STATES = {"running", "waiting_user", "paused"}
BATCH_ITEM_STATES = {
    "queued",
    "preparing",
    "waiting_user",
    "succeeded",
    "failed",
    "outcome_unknown",
    "canceled",
    "expired",
    "skipped",
    "superseded",
}
CONTINUATION_STATES = {"awaiting_selection", "selected", "expired", "cleared"}
CONTINUATION_EXECUTION_MODES = {"observe_only", "resume", "follow_up"}
ARTIFACT_DELIVERY_STATES = {
    "attachment_sent",
    "fallback_link_sent",
    "failed",
}
REFRESHABLE_ARTIFACT_TYPES = {
    "certificate_scan",
    "oa_addressbook_report",
    "smartlight_report",
}

PULL_BASED_CLIENT_TYPES = {"web", "webchat"}
ORPHAN_TASK_MAX_AGE_MINUTES = 15


class TaskNotFound(KeyError):
    pass


class TaskIntegrityError(RuntimeError):
    pass


def _is_pull_based_endpoint(endpoint: sqlite3.Row | dict[str, Any]) -> bool:
    return str(endpoint["client_type"] or "").lower() in PULL_BASED_CLIENT_TYPES


class TaskHubStore:
    """Persistent, non-sensitive task continuity ledger."""

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
                CREATE TABLE IF NOT EXISTS client_endpoints (
                    endpoint_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    agent_host TEXT NOT NULL,
                    endpoint_key TEXT NOT NULL,
                    client_type TEXT NOT NULL,
                    external_subject TEXT NOT NULL,
                    account_id TEXT,
                    conversation_ref TEXT NOT NULL,
                    label TEXT,
                    capabilities_json TEXT NOT NULL,
                    route_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE (agent_host, endpoint_key)
                );

                CREATE INDEX IF NOT EXISTS client_endpoints_subject_state
                ON client_endpoints (user_subject, state, updated_at);

                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    agent_host TEXT NOT NULL,
                    host_task_key TEXT NOT NULL,
                    origin_endpoint_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    current_operation_id TEXT,
                    current_interaction_id TEXT,
                    active_conversation_ref TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE (user_subject, agent_host, host_task_key)
                );

                CREATE INDEX IF NOT EXISTS agent_tasks_subject_status
                ON agent_tasks (user_subject, status, updated_at);

                CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    causation_ref TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS task_events_task_created
                ON task_events (task_id, created_at);

                CREATE TABLE IF NOT EXISTS task_operations (
                    task_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    user_subject TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, operation_id)
                );

                CREATE TABLE IF NOT EXISTS task_interactions (
                    task_id TEXT NOT NULL,
                    interaction_id TEXT NOT NULL UNIQUE,
                    user_subject TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    last_state TEXT,
                    last_observed_at TEXT,
                    PRIMARY KEY (task_id, interaction_id)
                );

                CREATE TABLE IF NOT EXISTS task_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    download_url TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS task_artifacts_task_created
                ON task_artifacts (task_id, created_at);

                CREATE INDEX IF NOT EXISTS task_artifacts_subject_state
                ON task_artifacts (user_subject, state, expires_at);

                CREATE TABLE IF NOT EXISTS task_subscriptions (
                    subscription_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    event_filters_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (task_id, endpoint_id)
                );

                CREATE TABLE IF NOT EXISTS notification_outbox (
                    delivery_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    payload_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    next_attempt_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    UNIQUE (event_id, endpoint_id, payload_type)
                );

                CREATE INDEX IF NOT EXISTS notification_outbox_endpoint_state
                ON notification_outbox (endpoint_id, state, next_attempt_at);

                CREATE TABLE IF NOT EXISTS user_timeline (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id TEXT NOT NULL UNIQUE,
                    user_subject TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    source_endpoint_id TEXT,
                    task_id TEXT,
                    role TEXT,
                    text TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (user_subject, dedupe_key)
                );

                CREATE INDEX IF NOT EXISTS user_timeline_subject_sequence
                ON user_timeline (user_subject, sequence);

                CREATE TABLE IF NOT EXISTS task_continuations (
                    endpoint_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    agent_host TEXT NOT NULL,
                    selected_task_id TEXT,
                    candidate_task_ids_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    reason TEXT,
                    expires_at TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS task_continuations_subject_state
                ON task_continuations (user_subject, state, updated_at);

                CREATE TABLE IF NOT EXISTS task_batches (
                    batch_id TEXT PRIMARY KEY,
                    parent_task_id TEXT NOT NULL UNIQUE,
                    user_subject TEXT NOT NULL,
                    system_id TEXT NOT NULL,
                    capability_name TEXT NOT NULL,
                    selection_summary_json TEXT NOT NULL,
                    source_snapshot_hash TEXT NOT NULL,
                    failure_policy TEXT NOT NULL,
                    state TEXT NOT NULL,
                    current_ordinal INTEGER NOT NULL,
                    total_count INTEGER NOT NULL,
                    succeeded_count INTEGER NOT NULL,
                    failed_count INTEGER NOT NULL,
                    skipped_count INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS task_batches_subject_state
                ON task_batches (user_subject, state, updated_at);

                CREATE TABLE IF NOT EXISTS task_batch_items (
                    item_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    resource_ref TEXT NOT NULL,
                    resource_ref_hash TEXT NOT NULL,
                    display_summary_json TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    operation_id TEXT,
                    interaction_id TEXT,
                    result_summary_json TEXT NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE (batch_id, ordinal),
                    UNIQUE (batch_id, resource_ref_hash)
                );

                CREATE INDEX IF NOT EXISTS task_batch_items_batch_state
                ON task_batch_items (batch_id, state, ordinal);
                """
            )
            self._migrate_task_interaction_observations(connection)
            self._repair_terminal_task_statuses(connection)
            self._expire_orphan_task_shells(connection)
            self._reconcile_pull_based_deliveries(connection)

    @staticmethod
    def _migrate_task_interaction_observations(
        connection: sqlite3.Connection,
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(task_interactions)"
            ).fetchall()
        }
        if "last_state" not in columns:
            connection.execute(
                "ALTER TABLE task_interactions ADD COLUMN last_state TEXT"
            )
        if "last_observed_at" not in columns:
            connection.execute(
                "ALTER TABLE task_interactions ADD COLUMN last_observed_at TEXT"
            )
        event_states = {
            "task.interaction.waiting": "pending",
            "task.interaction.completed": "completed",
            "task.canceled": "declined",
            "task.interaction.expired": "expired",
            "task.interaction.failed": "failed",
            "task.interaction.superseded": "superseded",
        }
        rows = connection.execute(
            """
            SELECT interaction_id
            FROM task_interactions
            WHERE last_state IS NULL
            """
        ).fetchall()
        for row in rows:
            event = connection.execute(
                """
                SELECT event_type, created_at
                FROM task_events
                WHERE causation_ref = ?
                  AND (
                      event_type LIKE 'task.interaction.%'
                      OR event_type = 'task.canceled'
                  )
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (row["interaction_id"],),
            ).fetchone()
            state = event_states.get(str(event["event_type"])) if event else None
            if state:
                connection.execute(
                    """
                    UPDATE task_interactions
                    SET last_state = ?, last_observed_at = ?
                    WHERE interaction_id = ?
                    """,
                    (state, event["created_at"], row["interaction_id"]),
                )

    @staticmethod
    def _repair_terminal_task_statuses(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            UPDATE agent_tasks
            SET status = 'succeeded',
                version = version + 1,
                updated_at = COALESCE(
                    (
                        SELECT MAX(task_artifacts.created_at)
                        FROM task_artifacts
                        WHERE task_artifacts.task_id = agent_tasks.task_id
                          AND task_artifacts.artifact_type = 'certificate_scan'
                    ),
                    updated_at
                ),
                finished_at = COALESCE(
                    (
                        SELECT MAX(task_artifacts.created_at)
                        FROM task_artifacts
                        WHERE task_artifacts.task_id = agent_tasks.task_id
                          AND task_artifacts.artifact_type = 'certificate_scan'
                    ),
                    updated_at
                )
            WHERE status IN ('active', 'waiting_user', 'running')
              AND title = 'Prepare and Deliver One OA Certificate Scan'
              AND EXISTS (
                  SELECT 1 FROM task_artifacts
                  WHERE task_artifacts.task_id = agent_tasks.task_id
                    AND task_artifacts.artifact_type = 'certificate_scan'
              )
            """
        )
        interactions_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'interactions'
            """
        ).fetchone()
        if interactions_table is not None:
            connection.execute(
                """
                UPDATE agent_tasks
                SET status = 'succeeded',
                    version = version + 1,
                    updated_at = COALESCE(
                        (
                            SELECT task_interactions.last_observed_at
                            FROM task_interactions
                            WHERE task_interactions.interaction_id =
                                agent_tasks.current_interaction_id
                        ),
                        updated_at
                    ),
                    finished_at = COALESCE(
                        (
                            SELECT task_interactions.last_observed_at
                            FROM task_interactions
                            WHERE task_interactions.interaction_id =
                                agent_tasks.current_interaction_id
                        ),
                        updated_at
                    )
                WHERE status IN ('active', 'waiting_user', 'running')
                  AND current_operation_id IS NULL
                  AND current_interaction_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM task_interactions
                      JOIN interactions
                        ON interactions.interaction_id =
                           task_interactions.interaction_id
                      WHERE task_interactions.interaction_id =
                            agent_tasks.current_interaction_id
                        AND task_interactions.last_state = 'completed'
                        AND interactions.interaction_type = 'credential'
                  )
                """
            )
        operations_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'operations'
            """
        ).fetchone()
        if operations_table is None:
            return
        connection.execute(
            """
            UPDATE agent_tasks
            SET status = 'succeeded',
                version = version + 1,
                current_interaction_id = COALESCE(
                    (
                        SELECT task_interactions.interaction_id
                        FROM task_interactions
                        WHERE task_interactions.task_id =
                            agent_tasks.task_id
                          AND (
                              task_interactions.last_state IS NULL
                              OR task_interactions.last_state NOT IN (
                                  'pending', 'processing'
                              )
                          )
                        ORDER BY task_interactions.linked_at DESC
                        LIMIT 1
                    ),
                    current_interaction_id
                ),
                finished_at = COALESCE(
                    (
                        SELECT operations.finished_at
                        FROM operations
                        WHERE operations.operation_id =
                            agent_tasks.current_operation_id
                    ),
                    updated_at
                )
            WHERE status IN ('active', 'waiting_user', 'running')
              AND current_operation_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM operations
                  WHERE operations.operation_id =
                        agent_tasks.current_operation_id
                    AND operations.status = 'succeeded'
              )
            """
        )
        connection.execute(
            """
            UPDATE agent_tasks
            SET status = 'superseded',
                version = version + 1,
                updated_at = COALESCE(
                    (
                        SELECT task_interactions.last_observed_at
                        FROM task_interactions
                        WHERE task_interactions.interaction_id =
                            agent_tasks.current_interaction_id
                    ),
                    updated_at
                ),
                finished_at = COALESCE(
                    (
                        SELECT task_interactions.last_observed_at
                        FROM task_interactions
                        WHERE task_interactions.interaction_id =
                            agent_tasks.current_interaction_id
                    ),
                    updated_at
                )
            WHERE status IN ('active', 'waiting_user', 'running')
              AND current_interaction_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM task_interactions
                  WHERE task_interactions.interaction_id =
                        agent_tasks.current_interaction_id
                    AND task_interactions.last_state = 'superseded'
              )
            """
        )

    @staticmethod
    def _expire_orphan_task_shells(
        connection: sqlite3.Connection,
        *,
        max_age_minutes: int = ORPHAN_TASK_MAX_AGE_MINUTES,
    ) -> int:
        """Expire stale task rows that have no remaining user action."""
        cutoff = _utc_before(minutes=max_age_minutes)
        cursor = connection.execute(
            """
            UPDATE agent_tasks
            SET status = 'expired',
                version = version + 1,
                finished_at = COALESCE(finished_at, updated_at)
            WHERE status IN ('active', 'waiting_user', 'running')
              AND created_at <= ?
              AND current_operation_id IS NULL
              AND current_interaction_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM task_operations
                  WHERE task_operations.task_id = agent_tasks.task_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM task_interactions
                  WHERE task_interactions.task_id = agent_tasks.task_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM task_artifacts
                  WHERE task_artifacts.task_id = agent_tasks.task_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM task_events
                  WHERE task_events.task_id = agent_tasks.task_id
                    AND task_events.event_type <> 'task.created'
              )
            """,
            (cutoff,),
        )
        expired = max(int(cursor.rowcount or 0), 0)
        operations_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'operations'
            """
        ).fetchone()
        if operations_table is None:
            return expired
        cursor = connection.execute(
            """
            UPDATE agent_tasks
            SET status = 'expired',
                version = version + 1,
                finished_at = COALESCE(finished_at, updated_at)
            WHERE status = 'waiting_user'
              AND updated_at <= ?
              AND current_operation_id IS NOT NULL
              AND current_interaction_id IS NULL
              AND EXISTS (
                  SELECT 1 FROM operations
                  WHERE operations.operation_id =
                        agent_tasks.current_operation_id
                    AND operations.status = 'requires_user_action'
                    AND operations.finished_at IS NOT NULL
              )
              AND NOT EXISTS (
                  SELECT 1 FROM task_interactions
                  WHERE task_interactions.task_id = agent_tasks.task_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM task_batches
                  WHERE task_batches.parent_task_id = agent_tasks.task_id
              )
            """,
            (cutoff,),
        )
        return expired + max(int(cursor.rowcount or 0), 0)

    @staticmethod
    def _reconcile_pull_based_deliveries(
        connection: sqlite3.Connection,
    ) -> None:
        now = _utc_now()
        placeholders = ", ".join("?" for _ in PULL_BASED_CLIENT_TYPES)
        client_types = tuple(sorted(PULL_BASED_CLIENT_TYPES))
        connection.execute(
            f"""
            UPDATE task_subscriptions
            SET state = 'inactive', updated_at = ?
            WHERE state = 'active'
              AND endpoint_id IN (
                  SELECT endpoint_id FROM client_endpoints
                  WHERE LOWER(client_type) IN ({placeholders})
              )
            """,
            (now, *client_types),
        )
        connection.execute(
            f"""
            UPDATE notification_outbox
            SET state = 'acknowledged', updated_at = ?,
                acknowledged_at = COALESCE(acknowledged_at, ?)
            WHERE payload_type = 'task_event'
              AND state IN ('pending', 'delivering', 'failed')
              AND endpoint_id IN (
                  SELECT endpoint_id FROM client_endpoints
                  WHERE LOWER(client_type) IN ({placeholders})
              )
            """,
            (now, now, *client_types),
        )

    def ensure_endpoint(
        self,
        *,
        user_subject: str,
        token_id: str,
        agent_host: str,
        endpoint_key: str,
        client_type: str,
        external_subject: str,
        conversation_ref: str,
        account_id: str | None = None,
        label: str | None = None,
        capabilities: list[str] | None = None,
        route: dict[str, Any] | None = None,
    ) -> tuple[dict, bool]:
        values = {
            "user_subject": _required_text(user_subject, "user_subject", 256),
            "token_id": _required_text(token_id, "token_id", 256),
            "agent_host": _required_text(agent_host, "agent_host", 80),
            "endpoint_key": _required_text(endpoint_key, "endpoint_key", 768),
            "client_type": _required_text(client_type, "client_type", 80),
            "external_subject": _required_text(
                external_subject,
                "external_subject",
                768,
            ),
            "conversation_ref": _required_text(
                conversation_ref,
                "conversation_ref",
                1024,
            ),
        }
        normalized_account = _optional_text(account_id, "account_id", 512)
        normalized_label = _optional_text(label, "label", 120)
        capabilities_json = _canonical_json(
            sorted(
                {
                    _required_text(item, "capability", 120)
                    for item in (capabilities or [])
                }
            )
        )
        route_json = _canonical_json(_safe_object(route))
        now = _utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM client_endpoints
                WHERE agent_host = ? AND endpoint_key = ?
                """,
                (values["agent_host"], values["endpoint_key"]),
            ).fetchone()
            if existing is not None:
                if existing["user_subject"] != values["user_subject"]:
                    raise TaskIntegrityError(
                        "client endpoint is already bound to another user"
                    )
                endpoint_id = existing["endpoint_id"]
                existing_capabilities = set(
                    json.loads(existing["capabilities_json"])
                )
                merged_capabilities_json = _canonical_json(
                    sorted(
                        existing_capabilities
                        | set(json.loads(capabilities_json))
                    )
                )
                connection.execute(
                    """
                    UPDATE client_endpoints
                    SET token_id = ?, client_type = ?, external_subject = ?,
                        account_id = ?, conversation_ref = ?, label = ?,
                        capabilities_json = ?, route_json = ?, state = 'active',
                        updated_at = ?, last_seen_at = ?
                    WHERE endpoint_id = ?
                    """,
                    (
                        values["token_id"],
                        values["client_type"],
                        values["external_subject"],
                        normalized_account,
                        values["conversation_ref"],
                        normalized_label,
                        merged_capabilities_json,
                        route_json,
                        now,
                        now,
                        endpoint_id,
                    ),
                )
                row = self._select_endpoint(connection, endpoint_id)
                return _endpoint_from_row(row), True

            endpoint_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO client_endpoints (
                    endpoint_id, user_subject, token_id, agent_host,
                    endpoint_key, client_type, external_subject, account_id,
                    conversation_ref, label, capabilities_json, route_json,
                    state, created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    endpoint_id,
                    values["user_subject"],
                    values["token_id"],
                    values["agent_host"],
                    values["endpoint_key"],
                    values["client_type"],
                    values["external_subject"],
                    normalized_account,
                    values["conversation_ref"],
                    normalized_label,
                    capabilities_json,
                    route_json,
                    now,
                    now,
                    now,
                ),
            )
            row = self._select_endpoint(connection, endpoint_id)
        return _endpoint_from_row(row), False

    def touch_endpoint(
        self,
        *,
        endpoint_id: str,
        user_subject: str,
    ) -> dict:
        endpoint_id = _required_text(endpoint_id, "endpoint_id", 256)
        user_subject = _required_text(user_subject, "user_subject", 256)
        now = _utc_now()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE client_endpoints
                SET updated_at = ?, last_seen_at = ?
                WHERE endpoint_id = ? AND user_subject = ?
                  AND state = 'active'
                """,
                (now, now, endpoint_id, user_subject),
            )
            if updated.rowcount != 1:
                raise TaskNotFound("client endpoint not found")
            row = self._select_endpoint(connection, endpoint_id)
        return _endpoint_from_row(row)

    def get_endpoint(
        self,
        endpoint_id: str,
        *,
        user_subject: str,
    ) -> dict:
        endpoint_id = _required_text(endpoint_id, "endpoint_id", 256)
        user_subject = _required_text(user_subject, "user_subject", 256)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM client_endpoints
                WHERE endpoint_id = ? AND user_subject = ? AND state = 'active'
                """,
                (endpoint_id, user_subject),
            ).fetchone()
        if row is None:
            raise TaskNotFound("client endpoint not found")
        return _endpoint_from_row(row)

    def ensure_task(
        self,
        *,
        user_subject: str,
        agent_host: str,
        host_task_key: str,
        origin_endpoint_id: str,
        active_conversation_ref: str,
        title: str,
        summary: dict[str, Any] | None = None,
    ) -> tuple[dict, bool]:
        user_subject = _required_text(user_subject, "user_subject", 256)
        agent_host = _required_text(agent_host, "agent_host", 80)
        host_task_key = _required_text(host_task_key, "host_task_key", 1024)
        active_conversation_ref = _required_text(
            active_conversation_ref,
            "active_conversation_ref",
            1024,
        )
        title = _required_text(title, "title", 240)
        summary_json = _canonical_json(_safe_object(summary))
        now = _utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            endpoint = self._select_endpoint(connection, origin_endpoint_id)
            if endpoint["user_subject"] != user_subject:
                raise TaskIntegrityError("task endpoint belongs to another user")
            if endpoint["agent_host"] != agent_host:
                raise TaskIntegrityError("task endpoint belongs to another agent host")
            existing = connection.execute(
                """
                SELECT * FROM agent_tasks
                WHERE user_subject = ? AND agent_host = ? AND host_task_key = ?
                """,
                (user_subject, agent_host, host_task_key),
            ).fetchone()
            if existing is not None:
                if existing["origin_endpoint_id"] != origin_endpoint_id:
                    raise TaskIntegrityError(
                        "host task key is already bound to another endpoint"
                    )
                connection.execute(
                    """
                    UPDATE agent_tasks
                    SET active_conversation_ref = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        active_conversation_ref,
                        now,
                        existing["task_id"],
                    ),
                )
                row = self._select_task(connection, existing["task_id"])
                return _task_from_row(row), True

            task_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO agent_tasks (
                    task_id, user_subject, agent_host, host_task_key,
                    origin_endpoint_id, title, status, summary_json,
                    active_conversation_ref, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, 1, ?, ?)
                """,
                (
                    task_id,
                    user_subject,
                    agent_host,
                    host_task_key,
                    origin_endpoint_id,
                    title,
                    summary_json,
                    active_conversation_ref,
                    now,
                    now,
                ),
            )
            if not _is_pull_based_endpoint(endpoint):
                connection.execute(
                    """
                    INSERT INTO task_subscriptions (
                        subscription_id, task_id, endpoint_id, user_subject,
                        event_filters_json, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        str(uuid4()),
                        task_id,
                        origin_endpoint_id,
                        user_subject,
                        _canonical_json(["*"]),
                        now,
                        now,
                    ),
                )
            self._subscribe_companion_endpoints(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                created_at=now,
            )
            self._append_event(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                event_type="task.created",
                payload={"status": "active", "title": title},
                causation_ref=host_task_key,
                created_at=now,
            )
            row = self._select_task(connection, task_id)
        return _task_from_row(row), False

    def find_host_task(self, *, user_subject: str, agent_host: str, host_task_key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_tasks WHERE user_subject = ? AND agent_host = ? AND host_task_key = ?",
                (user_subject, agent_host, host_task_key),
            ).fetchone()
        return _task_from_row(row) if row is not None else None

    def get_task(self, task_id: str, *, user_subject: str) -> dict:
        with self._connect() as connection:
            row = self._select_task(connection, task_id)
        if row["user_subject"] != user_subject:
            raise TaskNotFound(f"task not found: {task_id}")
        return _task_from_row(row)

    def create_batch(
        self,
        *,
        parent_task_id: str,
        user_subject: str,
        system_id: str,
        capability_name: str,
        selection_summary: dict[str, Any],
        failure_policy: str,
        items: list[dict[str, Any]],
    ) -> tuple[dict, bool]:
        if not items:
            raise ValueError("batch items are required")
        if len(items) > 100:
            raise ValueError("batch item count exceeds 100")
        failure_policy = _required_text(failure_policy, "failure_policy", 80)
        if failure_policy != "stop_on_failure":
            raise ValueError("unsupported batch failure policy")
        user_subject = _required_text(user_subject, "user_subject", 256)
        system_id = _required_text(system_id, "system_id", 80)
        capability_name = _required_text(
            capability_name,
            "capability_name",
            200,
        )
        normalized_items = []
        seen_refs: set[str] = set()
        for ordinal, source in enumerate(items, start=1):
            resource_ref = _required_text(
                source.get("resource_ref"),
                "resource_ref",
                512,
            )
            resource_ref_hash = hashlib.sha256(
                resource_ref.encode("utf-8")
            ).hexdigest()
            if resource_ref_hash in seen_refs:
                raise ValueError("batch contains a duplicate target")
            seen_refs.add(resource_ref_hash)
            display_summary = _safe_object(source.get("display_summary"))
            source_fingerprint = str(source.get("source_fingerprint") or "").strip()
            if not source_fingerprint:
                source_fingerprint = hashlib.sha256(
                    _canonical_json(display_summary).encode("utf-8")
                ).hexdigest()
            normalized_items.append(
                {
                    "ordinal": ordinal,
                    "resource_ref": resource_ref,
                    "resource_ref_hash": resource_ref_hash,
                    "display_summary": display_summary,
                    "source_fingerprint": source_fingerprint[:256],
                }
            )
        snapshot_hash = hashlib.sha256(
            _canonical_json(
                [
                    {
                        "ordinal": item["ordinal"],
                        "resourceRefHash": item["resource_ref_hash"],
                        "sourceFingerprint": item["source_fingerprint"],
                    }
                    for item in normalized_items
                ]
            ).encode("utf-8")
        ).hexdigest()
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(
                connection,
                parent_task_id,
                user_subject,
            )
            existing = connection.execute(
                "SELECT * FROM task_batches WHERE parent_task_id = ?",
                (parent_task_id,),
            ).fetchone()
            if existing is not None:
                return self._batch_snapshot(
                    connection,
                    existing,
                    include_resource_refs=True,
                ), True
            active = connection.execute(
                """
                SELECT batch_id FROM task_batches
                WHERE user_subject = ? AND system_id = ? AND capability_name = ?
                  AND state IN ('running', 'waiting_user', 'paused')
                LIMIT 1
                """,
                (user_subject, system_id, capability_name),
            ).fetchone()
            if active is not None:
                raise TaskIntegrityError(
                    "an active batch already exists for this user and capability"
                )
            for item in normalized_items:
                overlap = connection.execute(
                    """SELECT 1 FROM task_batch_items AS item
                       JOIN task_batches AS batch ON batch.batch_id = item.batch_id
                       WHERE batch.user_subject = ? AND batch.system_id = ?
                         AND batch.state IN ('running', 'waiting_user', 'paused')
                         AND item.resource_ref_hash = ?
                         AND item.state NOT IN ('succeeded', 'skipped', 'canceled') LIMIT 1""",
                    (user_subject, system_id, item["resource_ref_hash"]),
                ).fetchone()
                if overlap is not None:
                    raise TaskIntegrityError("a selected target is already owned by another active batch")
            batch_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO task_batches (
                    batch_id, parent_task_id, user_subject, system_id,
                    capability_name, selection_summary_json,
                    source_snapshot_hash, failure_policy, state,
                    current_ordinal, total_count, succeeded_count,
                    failed_count, skipped_count, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', 1, ?, 0, 0, 0, 1, ?, ?)
                """,
                (
                    batch_id,
                    parent_task_id,
                    user_subject,
                    system_id,
                    capability_name,
                    _canonical_json(_safe_object(selection_summary)),
                    snapshot_hash,
                    failure_policy,
                    len(normalized_items),
                    now,
                    now,
                ),
            )
            for item in normalized_items:
                connection.execute(
                    """
                    INSERT INTO task_batch_items (
                        item_id, batch_id, ordinal, resource_ref,
                        resource_ref_hash, display_summary_json,
                        source_fingerprint, state, result_summary_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', '{}', ?, ?)
                    """,
                    (
                        str(uuid4()),
                        batch_id,
                        item["ordinal"],
                        item["resource_ref"],
                        item["resource_ref_hash"],
                        _canonical_json(item["display_summary"]),
                        item["source_fingerprint"],
                        now,
                        now,
                    ),
                )
            batch = connection.execute(
                "SELECT * FROM task_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            self._sync_batch_task_summary(connection, task, batch, now=now)
            self._append_event(
                connection,
                task_id=parent_task_id,
                user_subject=user_subject,
                event_type="batch.created",
                payload={
                    "batchId": batch_id,
                    "systemId": system_id,
                    "capability": capability_name,
                    "totalCount": len(normalized_items),
                    "failurePolicy": failure_policy,
                },
                causation_ref=batch_id,
                created_at=now,
            )
            self._append_event(
                connection,
                task_id=parent_task_id,
                user_subject=user_subject,
                event_type="batch.candidates.frozen",
                payload={
                    "batchId": batch_id,
                    "totalCount": len(normalized_items),
                    "sourceSnapshotHash": snapshot_hash,
                },
                causation_ref=batch_id,
                created_at=now,
            )
            stored = connection.execute(
                "SELECT * FROM task_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            return self._batch_snapshot(
                connection,
                stored,
                include_resource_refs=True,
            ), False

    def get_batch_for_task(
        self,
        *,
        parent_task_id: str,
        user_subject: str,
        include_resource_refs: bool = False,
    ) -> dict | None:
        with self._connect() as connection:
            self._select_owned_task(connection, parent_task_id, user_subject)
            row = connection.execute(
                "SELECT * FROM task_batches WHERE parent_task_id = ?",
                (parent_task_id,),
            ).fetchone()
            if row is None:
                return None
            return self._batch_snapshot(
                connection,
                row,
                include_resource_refs=include_resource_refs,
            )

    def record_batch_item_activity(
        self,
        *,
        parent_task_id: str,
        user_subject: str,
        operation_id: str | None,
        interaction_id: str | None,
    ) -> dict | None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(
                connection,
                parent_task_id,
                user_subject,
            )
            batch = connection.execute(
                "SELECT * FROM task_batches WHERE parent_task_id = ?",
                (parent_task_id,),
            ).fetchone()
            if batch is None or batch["state"] not in ACTIVE_BATCH_STATES:
                return None
            item = self._select_current_batch_item(connection, batch)
            if operation_id:
                previous_operation_item = connection.execute(
                    """
                    SELECT * FROM task_batch_items
                    WHERE batch_id = ? AND operation_id = ?
                    ORDER BY ordinal
                    LIMIT 1
                    """,
                    (batch["batch_id"], operation_id),
                ).fetchone()
                if (
                    previous_operation_item is not None
                    and int(previous_operation_item["ordinal"])
                    != int(item["ordinal"])
                ):
                    return self._batch_snapshot(
                        connection,
                        batch,
                        include_resource_refs=True,
                    )
            previous_state = str(item["state"])
            item_state = "waiting_user" if interaction_id else "preparing"
            connection.execute(
                """
                UPDATE task_batch_items
                SET state = ?, operation_id = COALESCE(?, operation_id),
                    interaction_id = COALESCE(?, interaction_id),
                    updated_at = ?
                WHERE item_id = ?
                """,
                (
                    item_state,
                    operation_id,
                    interaction_id,
                    now,
                    item["item_id"],
                ),
            )
            batch_state = "waiting_user" if interaction_id else "running"
            connection.execute(
                """
                UPDATE task_batches
                SET state = ?, version = version + 1, updated_at = ?
                WHERE batch_id = ?
                """,
                (batch_state, now, batch["batch_id"]),
            )
            refreshed_batch = connection.execute(
                "SELECT * FROM task_batches WHERE batch_id = ?",
                (batch["batch_id"],),
            ).fetchone()
            self._sync_batch_task_summary(
                connection,
                task,
                refreshed_batch,
                now=now,
                task_status="waiting_user" if interaction_id else "running",
                current_operation_id=operation_id,
                current_interaction_id=interaction_id,
            )
            if previous_state == "queued":
                self._append_event(
                    connection,
                    task_id=parent_task_id,
                    user_subject=user_subject,
                    event_type="batch.item.started",
                    payload={
                        "batchId": batch["batch_id"],
                        "ordinal": int(batch["current_ordinal"]),
                        "totalCount": int(batch["total_count"]),
                    },
                    causation_ref=operation_id or interaction_id,
                    created_at=now,
                )
            stored = connection.execute(
                "SELECT * FROM task_batches WHERE batch_id = ?",
                (batch["batch_id"],),
            ).fetchone()
            return self._batch_snapshot(
                connection,
                stored,
                include_resource_refs=True,
            )

    def complete_current_batch_item(
        self,
        *,
        parent_task_id: str,
        user_subject: str,
        operation_id: str,
        expected_ordinal: int,
        result_summary: dict[str, Any] | None = None,
    ) -> dict:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(
                connection,
                parent_task_id,
                user_subject,
            )
            batch = self._select_owned_batch_for_task(
                connection,
                parent_task_id,
                user_subject,
            )
            item = connection.execute(
                """
                SELECT * FROM task_batch_items
                WHERE batch_id = ? AND ordinal = ?
                """,
                (batch["batch_id"], int(expected_ordinal)),
            ).fetchone()
            if item is None:
                raise TaskIntegrityError("expected batch item is missing")
            if item["state"] == "succeeded":
                next_item = (
                    self._select_current_batch_item(connection, batch)
                    if batch["state"] in ACTIVE_BATCH_STATES
                    and int(batch["current_ordinal"]) > int(expected_ordinal)
                    else None
                )
                return {
                    "batch": self._batch_snapshot(
                        connection,
                        batch,
                        include_resource_refs=True,
                    ),
                    "nextItem": (
                        _batch_item_from_row(next_item, include_resource_ref=True)
                        if next_item is not None
                        else None
                    ),
                    "reused": True,
                }
            if batch["state"] not in ACTIVE_BATCH_STATES:
                raise TaskIntegrityError("batch is already terminal")
            if int(batch["current_ordinal"]) != int(expected_ordinal):
                raise TaskIntegrityError("batch item is no longer current")
            connection.execute(
                """
                UPDATE task_batch_items
                SET state = 'succeeded', operation_id = ?,
                    result_summary_json = ?, error_code = NULL,
                    updated_at = ?, finished_at = ?
                WHERE item_id = ?
                """,
                (
                    operation_id,
                    _canonical_json(_safe_object(result_summary)),
                    now,
                    now,
                    item["item_id"],
                ),
            )
            succeeded_count = int(batch["succeeded_count"]) + 1
            next_item = connection.execute(
                """
                SELECT * FROM task_batch_items
                WHERE batch_id = ? AND ordinal > ? AND state = 'queued'
                ORDER BY ordinal
                LIMIT 1
                """,
                (batch["batch_id"], batch["current_ordinal"]),
            ).fetchone()
            terminal = next_item is None
            next_ordinal = (
                int(next_item["ordinal"])
                if next_item is not None
                else int(batch["current_ordinal"])
            )
            batch_state = "succeeded" if terminal else "running"
            connection.execute(
                """
                UPDATE task_batches
                SET state = ?, current_ordinal = ?, succeeded_count = ?,
                    version = version + 1, updated_at = ?, finished_at = ?
                WHERE batch_id = ?
                """,
                (
                    batch_state,
                    next_ordinal,
                    succeeded_count,
                    now,
                    now if terminal else None,
                    batch["batch_id"],
                ),
            )
            if next_item is not None:
                connection.execute(
                    """
                    UPDATE task_batch_items
                    SET state = 'preparing', updated_at = ?
                    WHERE item_id = ?
                    """,
                    (now, next_item["item_id"]),
                )
            refreshed_batch = connection.execute(
                "SELECT * FROM task_batches WHERE batch_id = ?",
                (batch["batch_id"],),
            ).fetchone()
            self._sync_batch_task_summary(
                connection,
                task,
                refreshed_batch,
                now=now,
                task_status="succeeded" if terminal else "running",
                current_operation_id=operation_id,
                current_interaction_id=None if not terminal else task["current_interaction_id"],
            )
            self._append_event(
                connection,
                task_id=parent_task_id,
                user_subject=user_subject,
                event_type="batch.item.succeeded",
                payload={
                    "batchId": batch["batch_id"],
                    "ordinal": int(item["ordinal"]),
                    "totalCount": int(batch["total_count"]),
                    "succeededCount": succeeded_count,
                },
                causation_ref=operation_id,
                created_at=now,
            )
            if terminal:
                self._append_event(
                    connection,
                    task_id=parent_task_id,
                    user_subject=user_subject,
                    event_type="batch.completed",
                    payload={
                        "batchId": batch["batch_id"],
                        "status": "succeeded",
                        "totalCount": int(batch["total_count"]),
                        "succeededCount": succeeded_count,
                        "failedCount": int(batch["failed_count"]),
                        "skippedCount": int(batch["skipped_count"]),
                    },
                    causation_ref=operation_id,
                    created_at=now,
                )
                self._append_event(
                    connection,
                    task_id=parent_task_id,
                    user_subject=user_subject,
                    event_type="task.completed",
                    payload={
                        "status": "succeeded",
                        "reason": "batch_completed",
                    },
                    causation_ref=operation_id,
                    created_at=now,
                )
            stored = connection.execute(
                "SELECT * FROM task_batches WHERE batch_id = ?",
                (batch["batch_id"],),
            ).fetchone()
            next_stored = (
                connection.execute(
                    "SELECT * FROM task_batch_items WHERE item_id = ?",
                    (next_item["item_id"],),
                ).fetchone()
                if next_item is not None
                else None
            )
            return {
                "batch": self._batch_snapshot(
                    connection,
                    stored,
                    include_resource_refs=True,
                ),
                "nextItem": (
                    _batch_item_from_row(next_stored, include_resource_ref=True)
                    if next_stored is not None
                    else None
                ),
                "reused": False,
            }

    def fail_current_batch_item(
        self,
        *,
        parent_task_id: str,
        user_subject: str,
        operation_id: str | None,
        expected_ordinal: int,
        item_state: str,
        error_code: str,
    ) -> dict:
        if item_state not in {
            "failed",
            "outcome_unknown",
            "canceled",
            "expired",
            "superseded",
        }:
            raise ValueError("unsupported terminal batch item state")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(
                connection,
                parent_task_id,
                user_subject,
            )
            batch = self._select_owned_batch_for_task(
                connection,
                parent_task_id,
                user_subject,
            )
            if batch["state"] not in ACTIVE_BATCH_STATES:
                return self._batch_snapshot(
                    connection,
                    batch,
                    include_resource_refs=True,
                )
            item = connection.execute(
                """
                SELECT * FROM task_batch_items
                WHERE batch_id = ? AND ordinal = ?
                """,
                (batch["batch_id"], int(expected_ordinal)),
            ).fetchone()
            if item is None:
                raise TaskIntegrityError("expected batch item is missing")
            if int(batch["current_ordinal"]) != int(expected_ordinal):
                if item["state"] not in {"queued", "preparing", "waiting_user"}:
                    return self._batch_snapshot(
                        connection,
                        batch,
                        include_resource_refs=True,
                    )
                raise TaskIntegrityError("batch item is no longer current")
            connection.execute(
                """
                UPDATE task_batch_items
                SET state = ?, operation_id = COALESCE(?, operation_id),
                    error_code = ?, updated_at = ?, finished_at = ?
                WHERE item_id = ?
                """,
                (
                    item_state,
                    operation_id,
                    _required_text(error_code, "error_code", 120),
                    now,
                    now,
                    item["item_id"],
                ),
            )
            succeeded_count = int(batch["succeeded_count"])
            failed_count = int(batch["failed_count"]) + 1
            if item_state == "outcome_unknown":
                batch_state = "outcome_unknown"
            elif succeeded_count:
                batch_state = "partially_succeeded"
            elif item_state in {"canceled", "expired"}:
                batch_state = item_state
            else:
                batch_state = "failed"
            connection.execute(
                """
                UPDATE task_batches
                SET state = ?, failed_count = ?, version = version + 1,
                    updated_at = ?, finished_at = ?
                WHERE batch_id = ?
                """,
                (
                    batch_state,
                    failed_count,
                    now,
                    now,
                    batch["batch_id"],
                ),
            )
            refreshed_batch = connection.execute(
                "SELECT * FROM task_batches WHERE batch_id = ?",
                (batch["batch_id"],),
            ).fetchone()
            self._sync_batch_task_summary(
                connection,
                task,
                refreshed_batch,
                now=now,
                task_status=batch_state,
                current_operation_id=operation_id,
                current_interaction_id=task["current_interaction_id"],
            )
            event_suffix = (
                "outcome_unknown" if item_state == "outcome_unknown" else "failed"
            )
            self._append_event(
                connection,
                task_id=parent_task_id,
                user_subject=user_subject,
                event_type=f"batch.item.{event_suffix}",
                payload={
                    "batchId": batch["batch_id"],
                    "ordinal": int(item["ordinal"]),
                    "totalCount": int(batch["total_count"]),
                    "errorCode": error_code,
                },
                causation_ref=operation_id,
                created_at=now,
            )
            self._append_event(
                connection,
                task_id=parent_task_id,
                user_subject=user_subject,
                event_type="batch.completed",
                payload={
                    "batchId": batch["batch_id"],
                    "status": batch_state,
                    "totalCount": int(batch["total_count"]),
                    "succeededCount": succeeded_count,
                    "failedCount": failed_count,
                    "skippedCount": int(batch["skipped_count"]),
                    "errorCode": error_code,
                },
                causation_ref=operation_id,
                created_at=now,
            )
            stored = connection.execute(
                "SELECT * FROM task_batches WHERE batch_id = ?",
                (batch["batch_id"],),
            ).fetchone()
            return self._batch_snapshot(
                connection,
                stored,
                include_resource_refs=True,
            )

    def endpoint_for_key(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_key: str,
    ) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM client_endpoints
                WHERE user_subject = ? AND agent_host = ? AND endpoint_key = ?
                  AND state = 'active'
                """,
                (user_subject, agent_host, endpoint_key),
            ).fetchone()
        if row is None:
            raise TaskNotFound("client endpoint not found")
        return _endpoint_from_row(row)

    def link_operation(
        self,
        *,
        task_id: str,
        user_subject: str,
        operation: dict[str, Any],
    ) -> dict:
        operation_id = _required_text(
            operation.get("operation_id"),
            "operation_id",
            256,
        )
        if operation.get("user_subject") != user_subject:
            raise TaskIntegrityError("operation belongs to another user")
        status = str(operation.get("status") or "")
        task_status = _task_status_for_operation(status)
        event_type = _event_type_for_operation(status)
        now = _utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(connection, task_id, user_subject)
            batch = connection.execute(
                "SELECT * FROM task_batches WHERE parent_task_id = ?",
                (task_id,),
            ).fetchone()
            if batch is not None:
                if batch["state"] in ACTIVE_BATCH_STATES:
                    task_status = (
                        "waiting_user"
                        if batch["state"] in {"waiting_user", "paused"}
                        else "running"
                    )
                else:
                    task_status = str(batch["state"])
            linked = connection.execute(
                "SELECT task_id FROM task_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if linked is not None and linked["task_id"] != task_id:
                raise TaskIntegrityError("operation is already linked to another task")
            if linked is None:
                connection.execute(
                    """
                    INSERT INTO task_operations (
                        task_id, operation_id, user_subject, linked_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (task_id, operation_id, user_subject, now),
                )
                self._append_event(
                    connection,
                    task_id=task_id,
                    user_subject=user_subject,
                    event_type="task.operation.linked",
                    payload={
                        "operationId": operation_id,
                        "capability": operation.get("capability_name"),
                        **(
                            {"capabilityEffect": operation["capability_effect"]}
                            if operation.get("capability_effect")
                            else {}
                        ),
                    },
                    causation_ref=operation_id,
                    created_at=now,
                )
            if task["status"] != "outcome_unknown" or task_status == "outcome_unknown":
                self._update_task_state(
                    connection,
                    task_id=task_id,
                    status=task_status,
                    current_operation_id=operation_id,
                    current_interaction_id=task["current_interaction_id"],
                    now=now,
                )
            if linked is None or task["status"] != task_status:
                self._append_event(
                    connection,
                    task_id=task_id,
                    user_subject=user_subject,
                    event_type=event_type,
                    payload={
                        "operationId": operation_id,
                        "capability": operation.get("capability_name"),
                        **(
                            {"capabilityEffect": operation["capability_effect"]}
                            if operation.get("capability_effect")
                            else {}
                        ),
                        "status": status,
                        "errorCode": (operation.get("error") or {}).get("code"),
                    },
                    causation_ref=operation_id,
                    created_at=now,
                )
            row = self._select_task(connection, task_id)
        return _task_from_row(row)

    def link_plan_operation(
        self,
        *,
        task_id: str,
        user_subject: str,
        operation: dict[str, Any],
        plan_id: str,
        step_key: str,
    ) -> dict:
        """Link a child operation without treating it as the parent task terminal."""
        operation_id = _required_text(
            operation.get("operation_id"),
            "operation_id",
            256,
        )
        if operation.get("user_subject") != user_subject:
            raise TaskIntegrityError("operation belongs to another user")
        plan_id = _required_text(plan_id, "plan_id", 128)
        step_key = _required_text(step_key, "step_key", 80)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(connection, task_id, user_subject)
            linked = connection.execute(
                "SELECT task_id FROM task_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if linked is not None and linked["task_id"] != task_id:
                raise TaskIntegrityError(
                    "operation is already linked to another task"
                )
            if linked is None:
                connection.execute(
                    """
                    INSERT INTO task_operations (
                        task_id, operation_id, user_subject, linked_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (task_id, operation_id, user_subject, now),
                )
                self._append_event(
                    connection,
                    task_id=task_id,
                    user_subject=user_subject,
                    event_type="task.operation.linked",
                    payload={
                        "operationId": operation_id,
                        "capability": operation.get("capability_name"),
                        "planId": plan_id,
                        "stepKey": step_key,
                    },
                    causation_ref=operation_id,
                    created_at=now,
                )
            if task["status"] in ACTIVE_TASK_STATUSES:
                self._update_task_state(
                    connection,
                    task_id=task_id,
                    status="running",
                    current_operation_id=operation_id,
                    current_interaction_id=task["current_interaction_id"],
                    now=now,
                )
            row = self._select_task(connection, task_id)
        return _task_from_row(row)

    def operation_ids_for_task(self, *, task_id: str, user_subject: str) -> list[str]:
        with self._connect() as connection:
            self._select_owned_task(connection, task_id, user_subject)
            return [row[0] for row in connection.execute(
                "SELECT operation_id FROM task_operations WHERE task_id = ? AND user_subject = ?",
                (task_id, user_subject),
            )]

    def operation_before_interaction(
        self, *, task_id: str, user_subject: str, interaction_id: str
    ) -> dict | None:
        """Find the operation preceding this task's card, not a shared login's birth."""
        with self._connect() as connection:
            self._select_owned_task(connection, task_id, user_subject)
            row = connection.execute(
                """
                SELECT operation.* FROM task_operations AS link
                JOIN operations AS operation ON operation.operation_id = link.operation_id
                JOIN task_interactions AS card ON card.task_id = link.task_id
                JOIN interactions AS interaction ON interaction.interaction_id = card.interaction_id
                WHERE link.task_id = ? AND link.user_subject = ?
                  AND operation.user_subject = ? AND card.user_subject = ?
                  AND interaction.user_subject = ? AND card.interaction_id = ?
                  AND link.linked_at <= CASE
                      WHEN interaction.interaction_type = 'credential' THEN card.linked_at
                      ELSE interaction.created_at END
                ORDER BY link.linked_at DESC, operation.created_at DESC LIMIT 1
                """,
                (task_id, user_subject, user_subject, user_subject, user_subject, interaction_id),
            ).fetchone()
        if row is None:
            return None
        return {"operation_id": row["operation_id"]}

    def task_has_succeeded_capability(
        self,
        *,
        task_id: str,
        user_subject: str,
        capability_names: set[str],
    ) -> bool:
        if not capability_names:
            return False
        placeholders = ",".join("?" for _ in capability_names)
        with self._connect() as connection:
            self._select_owned_task(connection, task_id, user_subject)
            row = connection.execute(
                f"""
                SELECT 1
                FROM task_operations AS link
                JOIN operations AS operation
                  ON operation.operation_id = link.operation_id
                WHERE link.task_id = ? AND link.user_subject = ?
                  AND operation.status = 'succeeded'
                  AND operation.capability_name IN ({placeholders})
                LIMIT 1
                """,
                (task_id, user_subject, *sorted(capability_names)),
            ).fetchone()
        return row is not None

    def record_plan_event(
        self,
        *,
        task_id: str,
        user_subject: str,
        event_type: str,
        payload: dict[str, Any],
        causation_ref: str | None = None,
    ) -> dict:
        if not str(event_type or "").startswith("plan."):
            raise ValueError("plan event type must start with plan.")
        if not isinstance(payload, dict):
            raise TypeError("plan event payload must be an object")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(connection, task_id, user_subject)
            self._append_event(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                event_type=event_type,
                payload=payload,
                causation_ref=causation_ref,
                created_at=now,
            )
            if task["status"] in ACTIVE_TASK_STATUSES:
                status = (
                    "waiting_user"
                    if event_type in {
                        "plan.step.waiting",
                        "plan.authorization.waiting",
                    }
                    else "running"
                )
                self._update_task_state(
                    connection,
                    task_id=task_id,
                    status=status,
                    current_operation_id=task["current_operation_id"],
                    current_interaction_id=task["current_interaction_id"],
                    now=now,
                )
            row = self._select_task(connection, task_id)
        return _task_from_row(row)

    def record_analysis_phase(self, *, task_id: str, user_subject: str, phase: str, request_id: str) -> None:
        if phase not in {"identity", "visibility", "query", "verify", "deliver"}:
            raise ValueError("unknown analysis phase")
        with self._connect() as connection:
            task = self._select_owned_task(connection, task_id, user_subject)
            if task["status"] not in ACTIVE_TASK_STATUSES:
                raise TaskIntegrityError("analysis task is no longer active")
            self._append_event(connection, task_id=task_id, user_subject=user_subject,
                event_type="task.analysis.progress", payload={"phase": phase},
                causation_ref=request_id + ":" + phase, created_at=_utc_now())

    def link_interaction(
        self,
        *,
        task_id: str,
        user_subject: str,
        interaction_record: dict[str, Any],
        interaction: dict[str, Any],
    ) -> dict:
        interaction_id = _required_text(
            interaction_record.get("interaction_id"),
            "interaction_id",
            256,
        )
        if interaction_record.get("user_subject") != user_subject:
            raise TaskIntegrityError("interaction belongs to another user")
        scoped_task = (interaction_record.get("resume_spec") or {}).get("taskId")
        if scoped_task and scoped_task != task_id:
            raise TaskIntegrityError("interaction belongs to another task")
        state = str(interaction.get("state") or "")
        event_type = _event_type_for_interaction(state)
        now = _utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(connection, task_id, user_subject)
            linked = connection.execute(
                "SELECT * FROM task_interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if linked is not None and linked["task_id"] != task_id:
                raise TaskIntegrityError("interaction is already linked to another task")
            task_status = _task_status_for_interaction(
                state,
                interaction_type=str(interaction.get("type") or ""),
                has_current_operation=bool(task["current_operation_id"]),
            )
            if linked is None:
                connection.execute(
                    """
                    INSERT INTO task_interactions (
                        task_id, interaction_id, user_subject, linked_at,
                        last_state, last_observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        interaction_id,
                        user_subject,
                        now,
                        state,
                        now,
                    ),
                )
            elif linked["last_state"] != state:
                connection.execute(
                    """
                    UPDATE task_interactions
                    SET last_state = ?, last_observed_at = ?
                    WHERE interaction_id = ?
                    """,
                    (state, now, interaction_id),
                )
            state_changed = linked is None or linked["last_state"] != state
            event_changed = (
                linked is None
                or _event_type_for_interaction(
                    str(linked["last_state"] or "")
                )
                != event_type
            )
            may_update_task = _interaction_may_update_task(
                task=task,
                interaction_id=interaction_id,
                newly_linked=linked is None,
            )
            if may_update_task and state_changed:
                self._update_task_state(
                    connection,
                    task_id=task_id,
                    status=task_status,
                    current_operation_id=task["current_operation_id"],
                    current_interaction_id=interaction_id,
                    now=now,
                )
            if may_update_task and state_changed and event_changed:
                if event_type == "task.interaction.waiting":
                    self._subscribe_companion_endpoints(
                        connection,
                        task_id=task_id,
                        user_subject=user_subject,
                        created_at=now,
                    )
                self._append_event(
                    connection,
                    task_id=task_id,
                    user_subject=user_subject,
                    event_type=event_type,
                    payload={
                        "interactionId": interaction_id,
                        "interactionType": interaction.get("type"),
                        "state": state,
                    },
                    causation_ref=interaction_id,
                    created_at=now,
                )
            row = self._select_task(connection, task_id)
        return _task_from_row(row)

    def link_artifact(
        self,
        *,
        task_id: str,
        user_subject: str,
        artifact: dict[str, Any],
    ) -> tuple[dict, bool]:
        artifact_type = _required_text(
            artifact.get("artifact_type") or "file",
            "artifact_type",
            80,
        )
        source_ref = _required_text(
            artifact.get("source_ref"),
            "source_ref",
            256,
        )
        filename = _required_text(artifact.get("filename"), "filename", 240)
        content_type = _required_text(
            artifact.get("content_type"),
            "content_type",
            120,
        )
        byte_size = int(artifact.get("byte_size") or 0)
        if byte_size <= 0 or byte_size > 32 * 1024 * 1024:
            raise ValueError("artifact byte_size is invalid")
        if artifact_type == "database_csv":
            report_id = source_ref.split(":", 1)[0]
            download_url = artifact.get("download_url")
            if not isinstance(download_url,str) or not re.fullmatch(r"/api/database/reports/[a-z][a-z0-9_-]{0,63}/"+re.escape(report_id)+r"/download",download_url) or not re.fullmatch(r'[0-9a-f]{32}',report_id):
                raise ValueError('database CSV download reference is invalid')
        elif artifact_type == "taihua_personal_csv":
            raise ValueError("legacy database CSV delivery is retired")
        else:
            download_url = _artifact_download_url(artifact.get("download_url"))
        expires_at = _required_future_time(
            artifact.get("expires_at"),
            "expires_at",
        )
        now = _utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._select_owned_task(connection, task_id, user_subject)
            existing = connection.execute(
                "SELECT * FROM task_artifacts WHERE source_ref = ?",
                (source_ref,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["task_id"] != task_id
                    or existing["user_subject"] != user_subject
                ):
                    raise TaskIntegrityError(
                        "artifact is already linked to another task or user"
                    )
                return _artifact_from_row(existing), True

            self._subscribe_companion_endpoints(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                created_at=now,
            )
            artifact_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO task_artifacts (
                    artifact_id, task_id, user_subject, artifact_type,
                    source_ref, filename, content_type, byte_size,
                    download_url, state, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?)
                """,
                (
                    artifact_id,
                    task_id,
                    user_subject,
                    artifact_type,
                    source_ref,
                    filename,
                    content_type,
                    byte_size,
                    download_url,
                    now,
                    now,
                    expires_at,
                ),
            )
            self._append_event(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                event_type="task.artifact.ready",
                payload={
                    "artifactId": artifact_id,
                    "artifactType": artifact_type,
                    "filename": filename,
                    "contentType": content_type,
                    "size": byte_size,
                    "downloadUrl": download_url,
                    "expiresAt": expires_at,
                },
                causation_ref=source_ref,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM task_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        return _artifact_from_row(row), False

    def record_artifact_delivery(
        self,
        *,
        task_id: str,
        user_subject: str,
        agent_host: str,
        delivery_ref: str,
        channel: str,
        files: list[dict[str, Any]],
    ) -> tuple[dict, dict, bool]:
        agent_host = _required_text(agent_host, "agent_host", 80)
        delivery_ref = _required_text(delivery_ref, "delivery_ref", 512)
        channel = _required_text(channel, "channel", 80)
        if not isinstance(files, list) or not 1 <= len(files) <= 20:
            raise ValueError("files must contain between 1 and 20 items")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, dict):
                raise TypeError("artifact delivery item must be an object")
            artifact_id = _required_text(
                item.get("artifact_id"),
                "artifact_id",
                128,
            )
            if artifact_id in seen:
                raise ValueError("artifact delivery contains duplicate IDs")
            seen.add(artifact_id)
            state = _required_text(item.get("state"), "state", 40)
            if state not in ARTIFACT_DELIVERY_STATES:
                raise ValueError("artifact delivery state is invalid")
            attempt_count = int(item.get("attempt_count") or 0)
            if attempt_count < 0 or attempt_count > 3:
                raise ValueError("artifact delivery attempt_count is invalid")
            error_code = item.get("error_code")
            normalized.append(
                {
                    "artifactId": artifact_id,
                    "state": state,
                    "attemptCount": attempt_count,
                    "errorCode": (
                        _required_text(error_code, "error_code", 80)
                        if error_code
                        else None
                    ),
                }
            )
        now = _utc_now()
        event_type = "task.artifact.delivery"

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(connection, task_id, user_subject)
            if task["agent_host"] != agent_host:
                raise TaskIntegrityError(
                    "artifact delivery belongs to another agent host"
                )
            existing = connection.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = ? AND user_subject = ?
                  AND event_type = ? AND causation_ref = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (task_id, user_subject, event_type, delivery_ref),
            ).fetchone()
            if existing is not None:
                return (
                    _task_from_row(task),
                    _event_from_row(existing),
                    True,
                )

            placeholders = ",".join("?" for _ in normalized)
            artifacts = connection.execute(
                f"""
                SELECT * FROM task_artifacts
                WHERE artifact_id IN ({placeholders})
                """,
                [item["artifactId"] for item in normalized],
            ).fetchall()
            artifacts_by_id = {row["artifact_id"]: row for row in artifacts}
            if len(artifacts_by_id) != len(normalized):
                raise TaskNotFound("artifact delivery contains an unknown artifact")
            for item in normalized:
                artifact = artifacts_by_id[item["artifactId"]]
                if (
                    artifact["task_id"] != task_id
                    or artifact["user_subject"] != user_subject
                ):
                    raise TaskNotFound("artifact delivery contains an unknown artifact")
                item["filename"] = artifact["filename"]

            summary = json.loads(task["summary_json"])
            by_channel = summary.get("artifactDeliveryByChannel")
            if not isinstance(by_channel, dict):
                by_channel = {}
            previous = by_channel.get(channel)
            previous_files = (
                previous.get("files") if isinstance(previous, dict) else []
            )
            merged_files = {
                item.get("artifactId"): item
                for item in previous_files
                if isinstance(item, dict) and item.get("artifactId")
            }
            for item in normalized:
                merged_files[item["artifactId"]] = item
            outcomes = list(merged_files.values())
            attachment_count = sum(
                item["state"] == "attachment_sent" for item in outcomes
            )
            fallback_count = sum(
                item["state"] == "fallback_link_sent" for item in outcomes
            )
            failed_count = sum(item["state"] == "failed" for item in outcomes)
            delivered_count = attachment_count + fallback_count
            delivery_state = (
                "delivered"
                if failed_count == 0
                else "partial"
                if delivered_count
                else "failed"
            )
            message_parts = [
                f"{len(outcomes)} 份文件已准备",
                f"{attachment_count} 份已作为附件发送",
            ]
            if fallback_count:
                message_parts.append(f"{fallback_count} 份已改发下载链接")
            if failed_count:
                message_parts.append(f"{failed_count} 份未能送达")
            report = {
                "deliveryRef": delivery_ref,
                "channel": channel,
                "state": delivery_state,
                "completionMeaning": "endpoint_delivery_reported",
                "preparedCount": len(outcomes),
                "attachmentSentCount": attachment_count,
                "fallbackLinkSentCount": fallback_count,
                "failedCount": failed_count,
                "files": outcomes,
                "userMessage": "，".join(message_parts) + "。",
                "reportedAt": now,
            }
            by_channel[channel] = report
            summary["artifactDeliveryByChannel"] = by_channel
            summary["artifactDelivery"] = report
            summary["artifactDeliveryAggregate"] = _artifact_delivery_aggregate(
                by_channel
            )
            connection.execute(
                """
                UPDATE agent_tasks
                SET summary_json = ?, version = version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (_canonical_json(summary), now, task_id),
            )
            event_id = self._append_event(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                event_type=event_type,
                payload=report,
                causation_ref=delivery_ref,
                created_at=now,
            )
            row = self._select_task(connection, task_id)
            event = connection.execute(
                "SELECT * FROM task_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return _task_from_row(row), _event_from_row(event), False

    def get_artifact(
        self,
        *,
        task_id: str,
        artifact_id: str,
        user_subject: str,
        include_source_ref: bool = False,
    ) -> dict:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._select_owned_task(connection, task_id, user_subject)
            connection.execute(
                """
                UPDATE task_artifacts
                SET state = 'expired', updated_at = ?
                WHERE artifact_id = ? AND task_id = ? AND user_subject = ?
                  AND state = 'ready' AND expires_at <= ?
                """,
                (now, artifact_id, task_id, user_subject, now),
            )
            row = connection.execute(
                """
                SELECT * FROM task_artifacts
                WHERE artifact_id = ? AND task_id = ? AND user_subject = ?
                """,
                (artifact_id, task_id, user_subject),
            ).fetchone()
        if row is None:
            raise TaskNotFound(f"artifact not found: {artifact_id}")
        return _artifact_from_row(
            row,
            include_source_ref=include_source_ref,
        )

    def refresh_artifact(
        self,
        *,
        task_id: str,
        artifact_id: str,
        user_subject: str,
        expected_source_ref: str,
        artifact: dict[str, Any],
    ) -> dict:
        source_ref = _required_text(
            artifact.get("source_ref"),
            "source_ref",
            256,
        )
        filename = _required_text(artifact.get("filename"), "filename", 240)
        content_type = _required_text(
            artifact.get("content_type"),
            "content_type",
            120,
        )
        byte_size = int(artifact.get("byte_size") or 0)
        if byte_size <= 0 or byte_size > 32 * 1024 * 1024:
            raise ValueError("artifact byte_size is invalid")
        download_url = _artifact_download_url(artifact.get("download_url"))
        expires_at = _required_future_time(
            artifact.get("expires_at"),
            "expires_at",
        )
        expected_source_ref = _required_text(
            expected_source_ref,
            "expected_source_ref",
            256,
        )
        now = _utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._select_owned_task(connection, task_id, user_subject)
            row = connection.execute(
                """
                SELECT * FROM task_artifacts
                WHERE artifact_id = ? AND task_id = ? AND user_subject = ?
                """,
                (artifact_id, task_id, user_subject),
            ).fetchone()
            if row is None:
                raise TaskNotFound(f"artifact not found: {artifact_id}")
            if row["source_ref"] != expected_source_ref:
                raise TaskIntegrityError(
                    "artifact source changed while the download was being refreshed"
                )
            if row["artifact_type"] not in REFRESHABLE_ARTIFACT_TYPES:
                raise TaskIntegrityError("artifact type cannot be refreshed")
            if row["state"] != "expired":
                raise TaskIntegrityError("only an expired artifact can be refreshed")
            conflict = connection.execute(
                """
                SELECT artifact_id FROM task_artifacts
                WHERE source_ref = ? AND artifact_id <> ?
                """,
                (source_ref, artifact_id),
            ).fetchone()
            if conflict is not None:
                raise TaskIntegrityError(
                    "replacement download is already linked to another artifact"
                )
            connection.execute(
                """
                UPDATE task_artifacts
                SET source_ref = ?, filename = ?, content_type = ?,
                    byte_size = ?, download_url = ?, state = 'ready',
                    updated_at = ?, expires_at = ?
                WHERE artifact_id = ?
                """,
                (
                    source_ref,
                    filename,
                    content_type,
                    byte_size,
                    download_url,
                    now,
                    expires_at,
                    artifact_id,
                ),
            )
            connection.execute(
                """
                UPDATE agent_tasks
                SET version = version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (now, task_id),
            )
            self._append_event(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                event_type="task.artifact.refreshed",
                payload={
                    "artifactId": artifact_id,
                    "artifactType": row["artifact_type"],
                    "filename": filename,
                    "contentType": content_type,
                    "size": byte_size,
                    "downloadUrl": download_url,
                    "expiresAt": expires_at,
                },
                causation_ref=source_ref,
                created_at=now,
            )
            refreshed = connection.execute(
                "SELECT * FROM task_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        return _artifact_from_row(refreshed, include_source_ref=True)

    def complete_task(
        self,
        *,
        task_id: str,
        user_subject: str,
        reason: str,
        causation_ref: str | None = None,
    ) -> dict:
        completion_reason = _required_text(reason, "reason", 120)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(connection, task_id, user_subject)
            if task["status"] == "succeeded":
                return _task_from_row(task)
            if task["status"] not in ACTIVE_TASK_STATUSES:
                raise TaskIntegrityError(
                    f"terminal task cannot be completed: {task['status']}"
                )
            self._subscribe_companion_endpoints(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                created_at=now,
            )
            self._update_task_state(
                connection,
                task_id=task_id,
                status="succeeded",
                current_operation_id=task["current_operation_id"],
                current_interaction_id=task["current_interaction_id"],
                now=now,
            )
            self._append_event(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                event_type="task.completed",
                payload={"status": "succeeded", "reason": completion_reason},
                causation_ref=causation_ref,
                created_at=now,
            )
            row = self._select_task(connection, task_id)
        return _task_from_row(row)

    def fail_task(
        self,
        *,
        task_id: str,
        user_subject: str,
        error_code: str,
        message: str,
        causation_ref: str | None = None,
    ) -> dict:
        code = _required_text(error_code, "error_code", 120)
        failure_message = _required_text(message, "message", 500)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(connection, task_id, user_subject)
            if task["status"] == "failed":
                return _task_from_row(task)
            if task["status"] not in ACTIVE_TASK_STATUSES:
                raise TaskIntegrityError(
                    f"terminal task cannot be failed: {task['status']}"
                )
            summary = json.loads(task["summary_json"])
            summary["failure"] = {
                "code": code,
                "message": failure_message,
            }
            connection.execute(
                "UPDATE agent_tasks SET summary_json = ? WHERE task_id = ?",
                (_canonical_json(summary), task_id),
            )
            self._subscribe_companion_endpoints(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                created_at=now,
            )
            self._update_task_state(
                connection,
                task_id=task_id,
                status="failed",
                current_operation_id=task["current_operation_id"],
                current_interaction_id=task["current_interaction_id"],
                now=now,
            )
            self._append_event(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                event_type="task.failed",
                payload={"status": "failed", "errorCode": code},
                causation_ref=causation_ref,
                created_at=now,
            )
            row = self._select_task(connection, task_id)
        return _task_from_row(row)

    def mark_task_outcome_unknown(
        self,
        *,
        task_id: str,
        user_subject: str,
        error_code: str,
        causation_ref: str | None = None,
    ) -> dict:
        code = _required_text(error_code, "error_code", 120)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(connection, task_id, user_subject)
            if task["status"] == "outcome_unknown":
                return _task_from_row(task)
            if task["status"] not in ACTIVE_TASK_STATUSES:
                raise TaskIntegrityError(
                    f"terminal task cannot become outcome_unknown: {task['status']}"
                )
            self._subscribe_companion_endpoints(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                created_at=now,
            )
            self._update_task_state(
                connection,
                task_id=task_id,
                status="outcome_unknown",
                current_operation_id=task["current_operation_id"],
                current_interaction_id=task["current_interaction_id"],
                now=now,
            )
            self._append_event(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                event_type="task.operation.outcome_unknown",
                payload={"status": "outcome_unknown", "errorCode": code},
                causation_ref=causation_ref,
                created_at=now,
            )
            row = self._select_task(connection, task_id)
        return _task_from_row(row)

    def cancel_task(
        self,
        *,
        task_id: str,
        user_subject: str,
        reason: str,
        causation_ref: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict:
        normalized_reason = _required_text(reason, "reason", 120)
        now = _utc_now()
        own_connection = connection is None
        with (self._connect() if own_connection else nullcontext(connection)) as connection:
            if own_connection:
                connection.execute("BEGIN IMMEDIATE")
            task = self._select_owned_task(connection, task_id, user_subject)
            if task["status"] == "canceled":
                return _task_from_row(task)
            if task["status"] not in ACTIVE_TASK_STATUSES:
                raise TaskIntegrityError(
                    f"terminal task cannot be canceled: {task['status']}"
                )
            self._subscribe_companion_endpoints(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                created_at=now,
            )
            self._update_task_state(
                connection,
                task_id=task_id,
                status="canceled",
                current_operation_id=task["current_operation_id"],
                current_interaction_id=task["current_interaction_id"],
                now=now,
            )
            batch = connection.execute("SELECT * FROM task_batches WHERE parent_task_id=? AND user_subject=?",
                                       (task_id, user_subject)).fetchone()
            if batch is not None and batch["state"] in ACTIVE_BATCH_STATES:
                connection.execute(
                    "UPDATE task_batch_items SET state='canceled', updated_at=?, finished_at=? "
                    "WHERE batch_id=? AND state IN ('queued','preparing','waiting_user')",
                    (now, now, batch["batch_id"]),
                )
                connection.execute(
                    "UPDATE task_batches SET state='canceled', version=version+1, updated_at=?, finished_at=? WHERE batch_id=?",
                    (now, now, batch["batch_id"]),
                )
                batch = self._select_owned_batch_for_task(connection, task_id, user_subject)
                self._sync_batch_task_summary(connection, task, batch, now=now, task_status="canceled",
                    current_operation_id=task["current_operation_id"], current_interaction_id=task["current_interaction_id"])
            self._append_event(
                connection,
                task_id=task_id,
                user_subject=user_subject,
                event_type="task.canceled",
                payload={"status": "canceled", "reason": normalized_reason},
                causation_ref=causation_ref,
                created_at=now,
            )
            row = self._select_task(connection, task_id)
        return _task_from_row(row)

    def list_artifacts(
        self,
        *,
        task_id: str,
        user_subject: str,
        limit: int = 100,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 500)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._select_owned_task(connection, task_id, user_subject)
            connection.execute(
                """
                UPDATE task_artifacts
                SET state = 'expired', updated_at = ?
                WHERE task_id = ? AND user_subject = ?
                  AND state = 'ready' AND expires_at <= ?
                """,
                (now, task_id, user_subject, now),
            )
            rows = connection.execute(
                """
                SELECT * FROM task_artifacts
                WHERE task_id = ? AND user_subject = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (task_id, user_subject, limit),
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def list_user_artifacts(
        self,
        *,
        user_subject: str,
        limit: int = 100,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 500)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE task_artifacts
                SET state = 'expired', updated_at = ?
                WHERE user_subject = ? AND state = 'ready' AND expires_at <= ?
                """,
                (now, user_subject, now),
            )
            rows = connection.execute(
                """
                SELECT * FROM task_artifacts
                WHERE user_subject = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_subject, limit),
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def append_timeline_message(
        self,
        *,
        user_subject: str,
        source_endpoint_id: str,
        message_key: str,
        role: str,
        text: str,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
        notify_source: bool = False,
    ) -> tuple[dict, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._append_timeline_message_in_connection(
                connection,
                user_subject=user_subject,
                source_endpoint_id=source_endpoint_id,
                message_key=message_key,
                role=role,
                text=text,
                task_id=task_id,
                payload=payload,
                notify_source=notify_source,
            )

    def _append_timeline_message_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        user_subject: str,
        source_endpoint_id: str,
        message_key: str,
        role: str,
        text: str,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
        notify_source: bool = False,
    ) -> tuple[dict, bool]:
        """Append with the caller's transaction; never commit independently."""
        if not connection.in_transaction:
            raise RuntimeError("timeline append requires an active transaction")
        user_subject = _required_text(user_subject, "user_subject", 256)
        message_key = _required_text(message_key, "message_key", 768)
        if role not in {"user", "assistant"}:
            raise ValueError("timeline role is invalid")
        normalized_text = _timeline_text(text)
        normalized_task_id = (
            _required_text(task_id, "task_id", 128) if task_id else None
        )
        payload_json = _canonical_json(_safe_object(payload))
        now = _utc_now()
        dedupe_key = f"message:{message_key}"

        endpoint = self._select_endpoint(connection, source_endpoint_id)
        if (
            endpoint["user_subject"] != user_subject
            or endpoint["state"] != "active"
        ):
            raise TaskNotFound("client endpoint not found")
        if normalized_task_id:
            self._select_owned_task(
                connection,
                normalized_task_id,
                user_subject,
            )
        existing = connection.execute(
            """
            SELECT * FROM user_timeline
            WHERE user_subject = ? AND dedupe_key = ?
            """,
            (user_subject, dedupe_key),
        ).fetchone()
        if existing is not None:
            return _timeline_from_row(existing), True

        entry_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO user_timeline (
                entry_id, user_subject, entry_type, dedupe_key,
                source_endpoint_id, task_id, role, text,
                payload_json, created_at
            ) VALUES (?, ?, 'chat_message', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                user_subject,
                dedupe_key,
                source_endpoint_id,
                normalized_task_id,
                role,
                normalized_text,
                payload_json,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM user_timeline WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        self._enqueue_timeline_message(
            connection,
            entry=row,
            source_endpoint_id=source_endpoint_id,
            notify_source=notify_source,
        )
        return _timeline_from_row(row), False

    def get_timeline_message(
        self,
        *,
        user_subject: str,
        message_key: str,
    ) -> dict:
        user_subject = _required_text(user_subject, "user_subject", 256)
        message_key = _required_text(message_key, "message_key", 768)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM user_timeline
                WHERE user_subject = ? AND dedupe_key = ?
                  AND entry_type = 'chat_message'
                """,
                (user_subject, f"message:{message_key}"),
            ).fetchone()
        if row is None:
            raise TaskNotFound("timeline message not found")
        return _timeline_from_row(row)

    def list_timeline(
        self,
        *,
        user_subject: str,
        after_sequence: int | None = None,
        before_sequence: int | None = None,
        entry_type: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        if after_sequence is not None and before_sequence is not None:
            raise ValueError("after_sequence and before_sequence are mutually exclusive")
        if entry_type not in {None, "chat_message", "task_event"}:
            raise ValueError("entry_type must be chat_message or task_event")
        limit = min(max(int(limit), 1), 1000)
        with self._connect() as connection:
            if after_sequence is not None:
                after_sequence = max(int(after_sequence), 0)
                if entry_type is None:
                    rows = connection.execute(
                        """
                        SELECT * FROM user_timeline
                        WHERE user_subject = ? AND sequence > ?
                        ORDER BY sequence
                        LIMIT ?
                        """,
                        (user_subject, after_sequence, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT * FROM user_timeline
                        WHERE user_subject = ? AND sequence > ? AND entry_type = ?
                        ORDER BY sequence
                        LIMIT ?
                        """,
                        (user_subject, after_sequence, entry_type, limit),
                    ).fetchall()
            elif before_sequence is not None:
                before_sequence = max(int(before_sequence), 1)
                if entry_type is None:
                    rows = connection.execute(
                        """
                        SELECT * FROM user_timeline
                        WHERE user_subject = ? AND sequence < ?
                        ORDER BY sequence DESC
                        LIMIT ?
                        """,
                        (user_subject, before_sequence, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT * FROM user_timeline
                        WHERE user_subject = ? AND sequence < ? AND entry_type = ?
                        ORDER BY sequence DESC
                        LIMIT ?
                        """,
                        (user_subject, before_sequence, entry_type, limit),
                    ).fetchall()
                rows = list(reversed(rows))
            elif entry_type is None:
                rows = connection.execute(
                    """
                    SELECT * FROM user_timeline
                    WHERE user_subject = ?
                    ORDER BY sequence DESC
                    LIMIT ?
                    """,
                    (user_subject, limit),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM user_timeline
                    WHERE user_subject = ? AND entry_type = ?
                    ORDER BY sequence DESC
                    LIMIT ?
                    """,
                    (user_subject, entry_type, limit),
                ).fetchall()
                rows = list(reversed(rows))
        return [_timeline_from_row(row) for row in rows]

    def latest_timeline_sequence(self, *, user_subject: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(sequence) AS sequence
                FROM user_timeline
                WHERE user_subject = ?
                """,
                (user_subject,),
            ).fetchone()
        return int(row["sequence"] or 0)

    def task_id_for_operation(
        self,
        operation_id: str,
        *,
        user_subject: str,
    ) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT task_id FROM task_operations
                WHERE operation_id = ? AND user_subject = ?
                """,
                (operation_id, user_subject),
            ).fetchone()
        return str(row["task_id"]) if row else None

    def task_id_for_interaction(
        self,
        interaction_id: str,
        *,
        user_subject: str,
    ) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT task_id FROM task_interactions
                WHERE interaction_id = ? AND user_subject = ?
                """,
                (interaction_id, user_subject),
            ).fetchone()
        return str(row["task_id"]) if row else None

    def list_task_interactions(
        self,
        *,
        task_id: str,
        user_subject: str,
        limit: int = 100,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 500)
        with self._connect() as connection:
            self._select_owned_task(connection, task_id, user_subject)
            rows = connection.execute(
                """
                SELECT * FROM task_interactions
                WHERE task_id = ? AND user_subject = ?
                ORDER BY linked_at, rowid
                LIMIT ?
                """,
                (task_id, user_subject, limit),
            ).fetchall()
        return [
            {
                "task_id": row["task_id"],
                "interaction_id": row["interaction_id"],
                "user_subject": row["user_subject"],
                "linked_at": row["linked_at"],
                "last_state": row["last_state"],
                "last_observed_at": row["last_observed_at"],
            }
            for row in rows
        ]

    def recovery_candidates(
        self,
        *,
        user_subject: str,
        endpoint_id: str,
        limit: int = 100,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 500)
        with self._connect() as connection:
            self._expire_orphan_task_shells(connection)
            endpoint = self._select_endpoint(connection, endpoint_id)
            if endpoint["user_subject"] != user_subject:
                raise TaskNotFound("client endpoint not found")
            rows = connection.execute(
                """
                SELECT * FROM agent_tasks
                WHERE user_subject = ? AND origin_endpoint_id = ?
                  AND current_interaction_id IS NOT NULL
                  AND status IN ('active', 'waiting_user', 'running')
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_subject, endpoint_id, limit),
            ).fetchall()
        selected_endpoint = _endpoint_from_row(endpoint)
        return [
            {
                "task": _task_from_row(row),
                "endpoint": selected_endpoint,
                "interaction_id": row["current_interaction_id"],
            }
            for row in rows
        ]

    def list_tasks(
        self,
        *,
        user_subject: str,
        endpoint_id: str | None = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 500)
        query = "SELECT * FROM agent_tasks WHERE user_subject = ?"
        parameters: list[Any] = [user_subject]
        if endpoint_id:
            query += " AND origin_endpoint_id = ?"
            parameters.append(endpoint_id)
        if active_only:
            query += " AND status IN ('active', 'waiting_user', 'running')"
        query += " ORDER BY updated_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            self._expire_orphan_task_shells(connection)
            rows = connection.execute(query, parameters).fetchall()
        return [_task_from_row(row) for row in rows]

    def continuation_candidates(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_id: str,
        active_only: bool = False,
        cross_endpoint_only: bool = False,
        source_client_type: str | None = None,
        max_age_minutes: int = 1_440,
        limit: int = 8,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 20)
        max_age_minutes = min(max(int(max_age_minutes), 1), 10_080)
        cutoff = _utc_before(minutes=max_age_minutes)
        normalized_source = _optional_text(
            source_client_type,
            "source_client_type",
            80,
        )
        source_types = _client_type_family(normalized_source)
        query = """
            SELECT task.*
            FROM agent_tasks AS task
            JOIN client_endpoints AS origin
              ON origin.endpoint_id = task.origin_endpoint_id
            WHERE task.user_subject = ? AND task.agent_host = ?
              AND task.updated_at >= ?
        """
        parameters: list[Any] = [user_subject, agent_host, cutoff]
        if active_only:
            query += " AND task.status IN ('active', 'waiting_user', 'running')"
        if cross_endpoint_only:
            query += " AND task.origin_endpoint_id <> ?"
            parameters.append(endpoint_id)
        if source_types:
            placeholders = ", ".join("?" for _ in source_types)
            query += f" AND LOWER(origin.client_type) IN ({placeholders})"
            parameters.extend(source_types)
        query += " ORDER BY task.updated_at DESC, task.created_at DESC LIMIT ?"
        parameters.append(limit)

        with self._connect() as connection:
            self._expire_orphan_task_shells(connection)
            endpoint = self._select_endpoint(connection, endpoint_id)
            if (
                endpoint["user_subject"] != user_subject
                or endpoint["agent_host"] != agent_host
                or endpoint["state"] != "active"
            ):
                raise TaskNotFound("client endpoint not found")
            rows = connection.execute(query, parameters).fetchall()
            candidates = []
            for row in rows:
                origin = self._select_endpoint(
                    connection,
                    row["origin_endpoint_id"],
                )
                candidates.append(
                    {
                        "task": _task_from_row(row),
                        "origin_endpoint": _endpoint_from_row(origin),
                    }
                )
        return candidates

    def set_continuation_candidates(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_id: str,
        candidate_task_ids: list[str],
        reason: str | None = None,
        ttl_seconds: int = 600,
    ) -> tuple[dict, bool]:
        normalized_ids = list(
            dict.fromkeys(
                _required_text(value, "candidate_task_id", 128)
                for value in candidate_task_ids
            )
        )
        if not normalized_ids or len(normalized_ids) > 20:
            raise ValueError("candidate_task_ids are invalid")
        normalized_reason = _optional_text(reason, "reason", 120)
        candidate_json = _canonical_json(normalized_ids)
        now = _utc_now()
        expires_at = _utc_after(min(max(int(ttl_seconds), 60), 3_600))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            endpoint = self._select_endpoint(connection, endpoint_id)
            if (
                endpoint["user_subject"] != user_subject
                or endpoint["agent_host"] != agent_host
                or endpoint["state"] != "active"
            ):
                raise TaskNotFound("client endpoint not found")
            for task_id in normalized_ids:
                task = self._select_owned_task(connection, task_id, user_subject)
                if task["agent_host"] != agent_host:
                    raise TaskNotFound(f"task not found: {task_id}")
            existing = connection.execute(
                "SELECT * FROM task_continuations WHERE endpoint_id = ?",
                (endpoint_id,),
            ).fetchone()
            reused = bool(
                existing is not None
                and existing["state"] == "awaiting_selection"
                and existing["candidate_task_ids_json"] == candidate_json
                and existing["reason"] == normalized_reason
                and existing["expires_at"] > now
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO task_continuations (
                        endpoint_id, user_subject, agent_host,
                        selected_task_id, candidate_task_ids_json, state,
                        execution_mode, reason, expires_at, version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, ?, 'awaiting_selection',
                              'observe_only', ?, ?, 1, ?, ?)
                    """,
                    (
                        endpoint_id,
                        user_subject,
                        agent_host,
                        candidate_json,
                        normalized_reason,
                        expires_at,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE task_continuations
                    SET selected_task_id = NULL,
                        candidate_task_ids_json = ?,
                        state = 'awaiting_selection',
                        execution_mode = 'observe_only', reason = ?,
                        expires_at = ?, updated_at = ?,
                        version = version + ?
                    WHERE endpoint_id = ?
                    """,
                    (
                        candidate_json,
                        normalized_reason,
                        expires_at,
                        now,
                        0 if reused else 1,
                        endpoint_id,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM task_continuations WHERE endpoint_id = ?",
                (endpoint_id,),
            ).fetchone()
        return _continuation_from_row(row), reused

    def select_continuation(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_id: str,
        task_id: str,
        execution_mode: str,
        reason: str | None = None,
        ttl_seconds: int = 21_600,
    ) -> tuple[dict, dict, dict, bool]:
        task_id = _required_text(task_id, "task_id", 128)
        if execution_mode not in CONTINUATION_EXECUTION_MODES:
            raise ValueError("execution_mode is invalid")
        normalized_reason = _optional_text(reason, "reason", 120)
        now = _utc_now()
        expires_at = _utc_after(min(max(int(ttl_seconds), 300), 86_400))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            endpoint = self._select_endpoint(connection, endpoint_id)
            if (
                endpoint["user_subject"] != user_subject
                or endpoint["agent_host"] != agent_host
                or endpoint["state"] != "active"
            ):
                raise TaskNotFound("client endpoint not found")
            task = self._select_owned_task(connection, task_id, user_subject)
            if task["agent_host"] != agent_host:
                raise TaskNotFound(f"task not found: {task_id}")
            existing = connection.execute(
                "SELECT * FROM task_continuations WHERE endpoint_id = ?",
                (endpoint_id,),
            ).fetchone()
            reused = bool(
                existing is not None
                and existing["state"] == "selected"
                and existing["selected_task_id"] == task_id
                and existing["execution_mode"] == execution_mode
                and existing["reason"] == normalized_reason
                and existing["expires_at"] > now
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO task_continuations (
                        endpoint_id, user_subject, agent_host,
                        selected_task_id, candidate_task_ids_json, state,
                        execution_mode, reason, expires_at, version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '[]', 'selected', ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        endpoint_id,
                        user_subject,
                        agent_host,
                        task_id,
                        execution_mode,
                        normalized_reason,
                        expires_at,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE task_continuations
                    SET selected_task_id = ?, candidate_task_ids_json = '[]',
                        state = 'selected', execution_mode = ?, reason = ?,
                        expires_at = ?, updated_at = ?,
                        version = version + ?
                    WHERE endpoint_id = ?
                    """,
                    (
                        task_id,
                        execution_mode,
                        normalized_reason,
                        expires_at,
                        now,
                        0 if reused else 1,
                        endpoint_id,
                    ),
                )
            if task["active_conversation_ref"] != endpoint["conversation_ref"]:
                connection.execute(
                    """
                    UPDATE agent_tasks
                    SET active_conversation_ref = ?, version = version + 1,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (endpoint["conversation_ref"], now, task_id),
                )
            if not reused:
                self._append_event(
                    connection,
                    task_id=task_id,
                    user_subject=user_subject,
                    event_type="task.continuation.selected",
                    payload={
                        "endpointId": endpoint_id,
                        "executionMode": execution_mode,
                    },
                    causation_ref=endpoint_id,
                    created_at=now,
                )
            row = connection.execute(
                "SELECT * FROM task_continuations WHERE endpoint_id = ?",
                (endpoint_id,),
            ).fetchone()
            selected_task = self._select_task(connection, task_id)
        return (
            _continuation_from_row(row),
            _task_from_row(selected_task),
            _endpoint_from_row(endpoint),
            reused,
        )

    def select_continuation_candidate(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_id: str,
        ordinal: int,
        execution_mode: str,
        reason: str | None = None,
        ttl_seconds: int = 21_600,
    ) -> tuple[dict, dict, dict, bool]:
        continuation = self.get_continuation(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_id=endpoint_id,
        )
        if continuation is None or continuation["state"] != "awaiting_selection":
            raise TaskNotFound("task continuation choices not found")
        candidates = continuation["candidate_task_ids"]
        index = int(ordinal) - 1
        if index < 0 or index >= len(candidates):
            raise TaskNotFound("task continuation choice not found")
        return self.select_continuation(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_id=endpoint_id,
            task_id=candidates[index],
            execution_mode=execution_mode,
            reason=reason,
            ttl_seconds=ttl_seconds,
        )

    def get_continuation(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_id: str,
    ) -> dict | None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            endpoint = self._select_endpoint(connection, endpoint_id)
            if (
                endpoint["user_subject"] != user_subject
                or endpoint["agent_host"] != agent_host
                or endpoint["state"] != "active"
            ):
                raise TaskNotFound("client endpoint not found")
            row = connection.execute(
                "SELECT * FROM task_continuations WHERE endpoint_id = ?",
                (endpoint_id,),
            ).fetchone()
            if row is None:
                return None
            if row["state"] in {"awaiting_selection", "selected"} and row[
                "expires_at"
            ] <= now:
                connection.execute(
                    """
                    UPDATE task_continuations
                    SET state = 'expired', selected_task_id = NULL,
                        candidate_task_ids_json = '[]', updated_at = ?,
                        version = version + 1
                    WHERE endpoint_id = ?
                    """,
                    (now, endpoint_id),
                )
                row = connection.execute(
                    "SELECT * FROM task_continuations WHERE endpoint_id = ?",
                    (endpoint_id,),
                ).fetchone()
        return _continuation_from_row(row)

    def list_endpoints(
        self,
        *,
        user_subject: str,
        active_only: bool = True,
        limit: int = 100,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 500)
        query = "SELECT * FROM client_endpoints WHERE user_subject = ?"
        parameters: list[Any] = [user_subject]
        if active_only:
            query += " AND state = 'active'"
        query += " ORDER BY updated_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_endpoint_from_row(row) for row in rows]

    def list_continuations(
        self,
        *,
        user_subject: str,
        limit: int = 100,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 500)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE task_continuations
                SET state = 'expired', selected_task_id = NULL,
                    candidate_task_ids_json = '[]', updated_at = ?,
                    version = version + 1
                WHERE user_subject = ?
                  AND state IN ('awaiting_selection', 'selected')
                  AND expires_at <= ?
                """,
                (now, user_subject, now),
            )
            rows = connection.execute(
                """
                SELECT * FROM task_continuations
                WHERE user_subject = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_subject, limit),
            ).fetchall()
        return [_continuation_from_row(row) for row in rows]

    def runtime_diagnostics(self) -> dict[str, Any]:
        return self.inspect_runtime(self.db_path)

    @staticmethod
    def inspect_runtime(db_path: Path | str) -> dict[str, Any]:
        """Read task-hub health without exposing message or business payloads."""

        resolved = Path(db_path).resolve().as_posix()
        now = _utc_now()
        connection = sqlite3.connect(
            f"file:{quote(resolved, safe='/:')}?mode=ro",
            uri=True,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        try:
            users = {
                str(row["user_subject"])
                for table in (
                    "client_endpoints",
                    "agent_tasks",
                    "task_artifacts",
                    "user_timeline",
                    "notification_outbox",
                    "task_continuations",
                )
                for row in connection.execute(
                    f"SELECT DISTINCT user_subject FROM {table}"
                ).fetchall()
            }
            has_workspace_accounts = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'workspace_accounts'
                """
            ).fetchone() is not None
            has_identity_tokens = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'mcp_identity_tokens'
                """
            ).fetchone() is not None
            has_host_dispatches = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'agent_host_dispatches'
                """
            ).fetchone() is not None
            if has_workspace_accounts:
                users.update(
                    str(row["user_subject"])
                    for row in connection.execute(
                        "SELECT DISTINCT user_subject FROM workspace_accounts"
                    ).fetchall()
                )
            if has_host_dispatches:
                users.update(
                    str(row["user_subject"])
                    for row in connection.execute(
                        "SELECT DISTINCT user_subject FROM agent_host_dispatches"
                    ).fetchall()
                )

            user_records = []
            for user_subject in sorted(users):
                endpoint_rows = connection.execute(
                    """
                    SELECT client_type, state, COUNT(*) AS count,
                           MAX(last_seen_at) AS last_seen_at
                    FROM client_endpoints
                    WHERE user_subject = ?
                    GROUP BY client_type, state
                    ORDER BY client_type, state
                    """,
                    (user_subject,),
                ).fetchall()
                task_rows = connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM agent_tasks
                    WHERE user_subject = ?
                    GROUP BY status ORDER BY status
                    """,
                    (user_subject,),
                ).fetchall()
                outbox_rows = connection.execute(
                    """
                    SELECT state, COUNT(*) AS count
                    FROM notification_outbox
                    WHERE user_subject = ?
                    GROUP BY state ORDER BY state
                    """,
                    (user_subject,),
                ).fetchall()
                continuation_rows = connection.execute(
                    """
                    SELECT state, execution_mode, COUNT(*) AS count FROM (
                        SELECT
                            CASE
                                WHEN state IN ('awaiting_selection', 'selected')
                                 AND expires_at <= ?
                                THEN 'expired'
                                ELSE state
                            END AS state,
                            execution_mode
                        FROM task_continuations
                        WHERE user_subject = ?
                    )
                    GROUP BY state, execution_mode
                    ORDER BY state, execution_mode
                    """,
                    (now, user_subject),
                ).fetchall()
                artifact_rows = connection.execute(
                    """
                    SELECT state, COUNT(*) AS count
                    FROM task_artifacts
                    WHERE user_subject = ?
                    GROUP BY state ORDER BY state
                    """,
                    (user_subject,),
                ).fetchall()
                timeline = connection.execute(
                    """
                    SELECT COUNT(*) AS count, MAX(sequence) AS latest_sequence,
                           MAX(created_at) AS latest_at
                    FROM user_timeline WHERE user_subject = ?
                    """,
                    (user_subject,),
                ).fetchone()
                oldest_outstanding = connection.execute(
                    """
                    SELECT MIN(created_at) AS oldest_at
                    FROM notification_outbox
                    WHERE user_subject = ?
                      AND state IN ('pending', 'delivering', 'deferred')
                    """,
                    (user_subject,),
                ).fetchone()
                workspace_count = 0
                host_dispatch_states: dict[str, int] = {}
                oldest_host_wait_at = None
                if has_workspace_accounts:
                    workspace_count = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count FROM workspace_accounts
                            WHERE user_subject = ? AND state = 'active'
                            """,
                            (user_subject,),
                        ).fetchone()["count"]
                    )
                if has_host_dispatches:
                    host_dispatch_states = {
                        str(row["state"]): int(row["count"])
                        for row in connection.execute(
                            """
                            SELECT state, COUNT(*) AS count
                            FROM agent_host_dispatches
                            WHERE user_subject = ?
                            GROUP BY state ORDER BY state
                            """,
                            (user_subject,),
                        ).fetchall()
                    }
                    oldest_host_wait_at = connection.execute(
                        """
                        SELECT MIN(created_at) AS oldest_at
                        FROM agent_host_dispatches
                        WHERE user_subject = ? AND state IN (
                            'queued', 'waiting_host', 'dispatching',
                            'reconciling_acceptance'
                        )
                        """,
                        (user_subject,),
                    ).fetchone()["oldest_at"]
                user_records.append(
                    {
                        "user_subject": user_subject,
                        "endpoints": [
                            {
                                "client_type": row["client_type"],
                                "state": row["state"],
                                "count": int(row["count"]),
                                "last_seen_at": row["last_seen_at"],
                            }
                            for row in endpoint_rows
                        ],
                        "active_workspace_accounts": workspace_count,
                        "host_dispatch_states": host_dispatch_states,
                        "oldest_host_dispatch_wait_at": oldest_host_wait_at,
                        "task_statuses": {
                            str(row["status"]): int(row["count"])
                            for row in task_rows
                        },
                        "timeline_entries": int(timeline["count"] or 0),
                        "latest_timeline_sequence": int(
                            timeline["latest_sequence"] or 0
                        ),
                        "latest_timeline_at": timeline["latest_at"],
                        "outbox_states": {
                            str(row["state"]): int(row["count"])
                            for row in outbox_rows
                        },
                        "task_continuations": [
                            {
                                "state": str(row["state"]),
                                "execution_mode": str(row["execution_mode"]),
                                "count": int(row["count"]),
                            }
                            for row in continuation_rows
                        ],
                        "artifact_states": {
                            str(row["state"]): int(row["count"])
                            for row in artifact_rows
                        },
                        "oldest_outstanding_delivery_at": oldest_outstanding[
                            "oldest_at"
                        ],
                    }
                )

            violation_queries = {
                "task_origin_user_mismatch": """
                    SELECT COUNT(*) AS count
                    FROM agent_tasks AS task
                    LEFT JOIN client_endpoints AS endpoint
                      ON endpoint.endpoint_id = task.origin_endpoint_id
                    WHERE endpoint.endpoint_id IS NULL
                       OR endpoint.user_subject <> task.user_subject
                """,
                "task_event_user_mismatch": """
                    SELECT COUNT(*) AS count
                    FROM task_events AS event
                    LEFT JOIN agent_tasks AS task ON task.task_id = event.task_id
                    WHERE task.task_id IS NULL
                       OR task.user_subject <> event.user_subject
                """,
                "task_operation_user_mismatch": """
                    SELECT COUNT(*) AS count
                    FROM task_operations AS link
                    LEFT JOIN agent_tasks AS task ON task.task_id = link.task_id
                    WHERE task.task_id IS NULL
                       OR task.user_subject <> link.user_subject
                """,
                "task_interaction_user_mismatch": """
                    SELECT COUNT(*) AS count
                    FROM task_interactions AS link
                    LEFT JOIN agent_tasks AS task ON task.task_id = link.task_id
                    WHERE task.task_id IS NULL
                       OR task.user_subject <> link.user_subject
                """,
                "task_artifact_user_mismatch": """
                    SELECT COUNT(*) AS count
                    FROM task_artifacts AS artifact
                    LEFT JOIN agent_tasks AS task
                      ON task.task_id = artifact.task_id
                    WHERE task.task_id IS NULL
                       OR task.user_subject <> artifact.user_subject
                """,
                "subscription_user_mismatch": """
                    SELECT COUNT(*) AS count
                    FROM task_subscriptions AS subscription
                    LEFT JOIN agent_tasks AS task
                      ON task.task_id = subscription.task_id
                    LEFT JOIN client_endpoints AS endpoint
                      ON endpoint.endpoint_id = subscription.endpoint_id
                    WHERE task.task_id IS NULL OR endpoint.endpoint_id IS NULL
                       OR task.user_subject <> subscription.user_subject
                       OR endpoint.user_subject <> subscription.user_subject
                """,
                "outbox_user_mismatch": """
                    SELECT COUNT(*) AS count
                    FROM notification_outbox AS delivery
                    LEFT JOIN agent_tasks AS task
                      ON task.task_id = NULLIF(delivery.task_id, '')
                    LEFT JOIN client_endpoints AS endpoint
                      ON endpoint.endpoint_id = delivery.endpoint_id
                    WHERE endpoint.endpoint_id IS NULL
                       OR endpoint.user_subject <> delivery.user_subject
                       OR (
                           delivery.task_id <> ''
                           AND (
                               task.task_id IS NULL
                               OR task.user_subject <> delivery.user_subject
                           )
                       )
                """,
                "timeline_user_mismatch": """
                    SELECT COUNT(*) AS count
                    FROM user_timeline AS timeline
                    LEFT JOIN client_endpoints AS endpoint
                      ON endpoint.endpoint_id = timeline.source_endpoint_id
                    LEFT JOIN agent_tasks AS task ON task.task_id = timeline.task_id
                    WHERE (
                        timeline.source_endpoint_id IS NOT NULL
                        AND (
                            endpoint.endpoint_id IS NULL
                            OR endpoint.user_subject <> timeline.user_subject
                        )
                    ) OR (
                        timeline.task_id IS NOT NULL
                        AND (
                            task.task_id IS NULL
                            OR task.user_subject <> timeline.user_subject
                        )
                    )
                """,
                "continuation_binding_mismatch": """
                    SELECT COUNT(*) AS count
                    FROM task_continuations AS continuation
                    LEFT JOIN client_endpoints AS endpoint
                      ON endpoint.endpoint_id = continuation.endpoint_id
                    LEFT JOIN agent_tasks AS selected_task
                      ON selected_task.task_id = continuation.selected_task_id
                    WHERE endpoint.endpoint_id IS NULL
                       OR endpoint.user_subject <> continuation.user_subject
                       OR endpoint.agent_host <> continuation.agent_host
                       OR (
                           continuation.selected_task_id IS NOT NULL
                           AND (
                               selected_task.task_id IS NULL
                               OR selected_task.user_subject <>
                                  continuation.user_subject
                               OR selected_task.agent_host <>
                                  continuation.agent_host
                           )
                       )
                       OR EXISTS (
                           SELECT 1
                           FROM json_each(
                               continuation.candidate_task_ids_json
                           ) AS candidate
                           LEFT JOIN agent_tasks AS candidate_task
                             ON candidate_task.task_id = candidate.value
                           WHERE candidate_task.task_id IS NULL
                              OR candidate_task.user_subject <>
                                 continuation.user_subject
                              OR candidate_task.agent_host <>
                                 continuation.agent_host
                       )
                """,
            }
            if has_identity_tokens:
                violation_queries["endpoint_token_user_mismatch"] = """
                    SELECT COUNT(*) AS count
                    FROM client_endpoints AS endpoint
                    LEFT JOIN mcp_identity_tokens AS token
                      ON token.token_id = endpoint.token_id
                    WHERE endpoint.token_id NOT LIKE 'workspace-account:%'
                      AND (
                          token.token_id IS NULL
                          OR token.user_subject <> endpoint.user_subject
                      )
                """
            if has_workspace_accounts:
                violation_queries["workspace_endpoint_user_mismatch"] = """
                    SELECT COUNT(*) AS count
                    FROM workspace_accounts AS account
                    LEFT JOIN client_endpoints AS endpoint
                      ON endpoint.endpoint_id = account.endpoint_id
                    WHERE account.endpoint_id IS NULL
                       OR endpoint.endpoint_id IS NULL
                       OR endpoint.user_subject <> account.user_subject
                """
            if has_host_dispatches:
                violation_queries["host_dispatch_user_mismatch"] = """
                    SELECT COUNT(*) AS count
                    FROM agent_host_dispatches AS dispatch
                    LEFT JOIN workspace_accounts AS account
                      ON account.account_id = dispatch.account_id
                    LEFT JOIN client_endpoints AS endpoint
                      ON endpoint.endpoint_id = dispatch.origin_endpoint_id
                    WHERE account.account_id IS NULL
                       OR endpoint.endpoint_id IS NULL
                       OR account.user_subject <> dispatch.user_subject
                       OR endpoint.user_subject <> dispatch.user_subject
                """
                violation_queries["host_dispatch_acceptance_mismatch"] = """
                    SELECT COUNT(*) AS count
                    FROM agent_host_dispatches
                    WHERE (
                        state IN ('accepted', 'completed')
                        AND accepted_run_id IS NULL
                    ) OR (
                        accepted_run_id IS NOT NULL
                        AND accepted_at IS NULL
                    )
                """
            violations = {
                name: int(connection.execute(query).fetchone()["count"])
                for name, query in violation_queries.items()
            }
            totals = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM client_endpoints
                     WHERE state = 'active') AS active_endpoints,
                    (SELECT COUNT(*) FROM agent_tasks
                     WHERE status IN ('active', 'waiting_user', 'running'))
                        AS active_tasks,
                    (SELECT COUNT(*) FROM notification_outbox
                     WHERE state IN ('pending', 'delivering', 'deferred'))
                        AS outstanding_deliveries,
                    (SELECT COUNT(*) FROM notification_outbox
                     WHERE state = 'deferred') AS deferred_deliveries,
                    (SELECT COUNT(*) FROM notification_outbox
                     WHERE state = 'failed') AS failed_deliveries,
                    (SELECT COUNT(*) FROM task_continuations
                     WHERE state IN ('awaiting_selection', 'selected')
                       AND expires_at > ?)
                        AS active_task_continuations,
                    (SELECT COUNT(*) FROM task_artifacts
                     WHERE state = 'ready'
                       AND datetime(expires_at) > datetime('now'))
                        AS ready_artifacts,
                    (SELECT COUNT(*) FROM task_artifacts
                     WHERE state = 'expired'
                        OR datetime(expires_at) <= datetime('now'))
                        AS expired_artifacts,
                    (SELECT COUNT(*) FROM user_timeline) AS timeline_entries
                """,
                (now,),
            ).fetchone()
            host_dispatch_totals = {
                "active_host_dispatches": 0,
                "waiting_host_dispatches": 0,
                "acceptance_unknown_dispatches": 0,
            }
            if has_host_dispatches:
                row = connection.execute(
                    """
                    SELECT
                        SUM(CASE WHEN state IN (
                            'queued', 'waiting_host', 'dispatching',
                            'reconciling_acceptance', 'accepted'
                        ) THEN 1 ELSE 0 END) AS active_count,
                        SUM(CASE WHEN state = 'waiting_host'
                            THEN 1 ELSE 0 END) AS waiting_count,
                        SUM(CASE WHEN state = 'acceptance_unknown'
                            THEN 1 ELSE 0 END) AS unknown_count
                    FROM agent_host_dispatches
                    """
                ).fetchone()
                host_dispatch_totals = {
                    "active_host_dispatches": int(row["active_count"] or 0),
                    "waiting_host_dispatches": int(row["waiting_count"] or 0),
                    "acceptance_unknown_dispatches": int(
                        row["unknown_count"] or 0
                    ),
                }
        finally:
            connection.close()

        violation_count = sum(violations.values())
        return {
            "generated_at": _utc_now(),
            "summary": {
                "users": len(user_records),
                "active_endpoints": int(totals["active_endpoints"]),
                "active_tasks": int(totals["active_tasks"]),
                "outstanding_deliveries": int(totals["outstanding_deliveries"]),
                "deferred_deliveries": int(totals["deferred_deliveries"]),
                "failed_deliveries": int(totals["failed_deliveries"]),
                "active_task_continuations": int(
                    totals["active_task_continuations"]
                ),
                "ready_artifacts": int(totals["ready_artifacts"]),
                "expired_artifacts": int(totals["expired_artifacts"]),
                "timeline_entries": int(totals["timeline_entries"]),
                **host_dispatch_totals,
                "isolation_violation_count": violation_count,
            },
            "users": user_records,
            "isolation": {
                "passed": violation_count == 0,
                "violations": violations,
            },
        }

    def list_user_events(
        self,
        *,
        user_subject: str,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 500)
        with self._connect() as connection:
            parameters: list[Any] = [user_subject]
            query = "SELECT * FROM task_events WHERE user_subject = ?"
            if after_event_id:
                if after_event_id.startswith("time:"):
                    cursor_time = after_event_id.removeprefix("time:")
                    try:
                        parsed = datetime.fromisoformat(cursor_time)
                    except ValueError as exc:
                        raise TaskNotFound(
                            f"task event cursor is invalid: {after_event_id}"
                        ) from exc
                    if parsed.tzinfo is None:
                        raise TaskNotFound(
                            f"task event cursor is invalid: {after_event_id}"
                        )
                    query += " AND created_at > ?"
                    parameters.append(parsed.isoformat())
                else:
                    cursor = connection.execute(
                        """
                        SELECT created_at, rowid FROM task_events
                        WHERE event_id = ? AND user_subject = ?
                        """,
                        (after_event_id, user_subject),
                    ).fetchone()
                    if cursor is None:
                        raise TaskNotFound(
                            f"task event not found: {after_event_id}"
                        )
                    query += (
                        " AND (created_at > ? OR "
                        "(created_at = ? AND rowid > ?))"
                    )
                    parameters.extend(
                        [
                            cursor["created_at"],
                            cursor["created_at"],
                            cursor["rowid"],
                        ]
                    )
            query += " ORDER BY created_at, rowid LIMIT ?"
            parameters.append(limit)
            rows = connection.execute(query, parameters).fetchall()
        return [_event_from_row(row) for row in rows]

    def latest_user_event_id(self, *, user_subject: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT event_id FROM task_events
                WHERE user_subject = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (user_subject,),
            ).fetchone()
        return str(row["event_id"]) if row is not None else None

    def current_user_event_cursor(self, *, user_subject: str) -> str:
        return (
            self.latest_user_event_id(user_subject=user_subject)
            or f"time:{_utc_now()}"
        )

    def list_events(
        self,
        *,
        task_id: str,
        user_subject: str,
        limit: int = 100,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 500)
        with self._connect() as connection:
            self._select_owned_task(connection, task_id, user_subject)
            rows = connection.execute(
                """
                SELECT event.*, timeline.sequence
                FROM task_events AS event
                LEFT JOIN user_timeline AS timeline
                  ON timeline.entry_id = event.event_id
                WHERE event.task_id = ? AND event.user_subject = ?
                ORDER BY COALESCE(timeline.sequence, 0), event.created_at,
                         event.rowid
                LIMIT ?
                """,
                (task_id, user_subject, limit),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def list_outbox(
        self,
        *,
        user_subject: str,
        endpoint_id: str | None = None,
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 500)
        query = "SELECT * FROM notification_outbox WHERE user_subject = ?"
        parameters: list[Any] = [user_subject]
        if endpoint_id:
            query += " AND endpoint_id = ?"
            parameters.append(endpoint_id)
        direction = "DESC" if newest_first else "ASC"
        query += f" ORDER BY created_at {direction}, rowid {direction} LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_outbox_from_row(row) for row in rows]

    def claim_outbox(
        self,
        *,
        user_subject: str,
        endpoint_id: str,
        limit: int = 10,
        lease_seconds: int = 30,
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 100)
        lease_seconds = min(max(int(lease_seconds), 5), 300)
        now = _utc_now()
        lease_until = _utc_after(lease_seconds)
        with self._connect() as connection:
            endpoint = self._select_endpoint(connection, endpoint_id)
            if (
                endpoint["user_subject"] != user_subject
                or endpoint["state"] != "active"
            ):
                raise TaskNotFound("client endpoint not found")
            candidate = connection.execute(
                """
                SELECT 1 FROM notification_outbox
                WHERE user_subject = ? AND endpoint_id = ?
                  AND (
                    (
                      attempt_count < 5
                      AND (
                        state IN ('pending', 'delivering')
                        AND next_attempt_at <= ?
                      )
                    )
                    OR (
                      state = 'delivering'
                      AND attempt_count >= 5
                      AND next_attempt_at <= ?
                    )
                  )
                LIMIT 1
                """,
                (user_subject, endpoint_id, now, now),
            ).fetchone()
            if candidate is None:
                return []

            connection.execute("BEGIN IMMEDIATE")
            endpoint = self._select_endpoint(connection, endpoint_id)
            if (
                endpoint["user_subject"] != user_subject
                or endpoint["state"] != "active"
            ):
                raise TaskNotFound("client endpoint not found")
            connection.execute(
                """
                UPDATE notification_outbox
                SET state = 'failed', updated_at = ?
                WHERE user_subject = ? AND endpoint_id = ?
                  AND state = 'delivering'
                  AND attempt_count >= 5
                  AND next_attempt_at <= ?
                """,
                (now, user_subject, endpoint_id, now),
            )
            rows = connection.execute(
                """
                SELECT * FROM notification_outbox
                WHERE user_subject = ? AND endpoint_id = ?
                  AND attempt_count < 5
                  AND (
                    state IN ('pending', 'delivering')
                    AND next_attempt_at <= ?
                  )
                ORDER BY created_at, rowid
                LIMIT ?
                """,
                (user_subject, endpoint_id, now, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                cursor = connection.execute(
                    """
                    UPDATE notification_outbox
                    SET state = 'delivering',
                        attempt_count = attempt_count + 1,
                        next_attempt_at = ?, updated_at = ?
                    WHERE delivery_id = ?
                      AND attempt_count < 5
                      AND (
                        state IN ('pending', 'delivering')
                        AND next_attempt_at <= ?
                      )
                    """,
                    (lease_until, now, row["delivery_id"], now),
                )
                if cursor.rowcount == 1:
                    claimed.append(
                        connection.execute(
                            """
                            SELECT * FROM notification_outbox
                            WHERE delivery_id = ?
                            """,
                            (row["delivery_id"],),
                        ).fetchone()
                    )
        return [_outbox_from_row(row) for row in claimed]

    def acknowledge_outbox(
        self,
        *,
        user_subject: str,
        endpoint_id: str,
        delivery_id: str,
        succeeded: bool,
        retry_after_seconds: int = 5,
        defer_until_activity: bool = False,
    ) -> dict:
        if succeeded and defer_until_activity:
            raise ValueError(
                "successful delivery cannot be deferred until endpoint activity"
            )
        retry_after_seconds = min(max(int(retry_after_seconds), 1), 300)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM notification_outbox
                WHERE delivery_id = ? AND user_subject = ? AND endpoint_id = ?
                """,
                (delivery_id, user_subject, endpoint_id),
            ).fetchone()
            if row is None:
                raise TaskNotFound("notification delivery not found")
            if succeeded:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET state = 'acknowledged', updated_at = ?,
                        acknowledged_at = ?
                    WHERE delivery_id = ?
                    """,
                    (now, now, delivery_id),
                )
            elif defer_until_activity:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET state = 'deferred', next_attempt_at = ?, updated_at = ?,
                        acknowledged_at = NULL
                    WHERE delivery_id = ?
                    """,
                    (now, now, delivery_id),
                )
            elif int(row["attempt_count"]) >= 5:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET state = 'failed', updated_at = ?,
                        acknowledged_at = NULL
                    WHERE delivery_id = ?
                    """,
                    (now, delivery_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET state = 'pending', next_attempt_at = ?, updated_at = ?,
                        acknowledged_at = NULL
                    WHERE delivery_id = ?
                    """,
                    (_utc_after(retry_after_seconds), now, delivery_id),
                )
            updated = connection.execute(
                """
                SELECT * FROM notification_outbox WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
        return _outbox_from_row(updated)

    def reactivate_deferred_outbox(
        self,
        *,
        user_subject: str,
        endpoint_id: str,
        delay_seconds: int = 5,
    ) -> int:
        delay_seconds = min(max(int(delay_seconds), 0), 300)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            endpoint = self._select_endpoint(connection, endpoint_id)
            if (
                endpoint["user_subject"] != user_subject
                or endpoint["state"] != "active"
            ):
                raise TaskNotFound("client endpoint not found")
            # Missed chat synchronization remains readable in the timeline.
            # An inbound WeChat query must not replay a backlog of old chats.
            if str(endpoint["client_type"]).lower() in {
                "openclaw-weixin", "wechat", "weixin"
            }:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET state = 'archived', updated_at = ?
                    WHERE user_subject = ? AND endpoint_id = ?
                      AND state = 'deferred' AND payload_type = 'timeline_message'
                    """,
                    (now, user_subject, endpoint_id),
                )
                # Closed tasks are still available in Task Hub. Do not replay
                # obsolete status notices when a new conversation starts.
                statuses = sorted(TERMINAL_TASK_STATUSES)
                placeholders = ",".join("?" for _ in statuses)
                connection.execute(
                    f"""
                    UPDATE notification_outbox
                    SET state = 'archived', updated_at = ?
                    WHERE user_subject = ? AND endpoint_id = ?
                      AND state = 'deferred' AND payload_type = 'task_event'
                      AND json_extract(payload_json, '$.eventType') IN
                        ('task.operation.failed', 'task.operation.succeeded',
                         'task.completed', 'task.canceled', 'plan.completed')
                      AND task_id IN (
                        SELECT task_id FROM agent_tasks
                        WHERE user_subject = ? AND status IN ({placeholders})
                      )
                    """,
                    (now, user_subject, endpoint_id, user_subject, *statuses),
                )
            cursor = connection.execute(
                """
                UPDATE notification_outbox
                SET state = 'pending', attempt_count = 0,
                    next_attempt_at = ?, updated_at = ?,
                    acknowledged_at = NULL
                WHERE user_subject = ? AND endpoint_id = ?
                  AND state = 'deferred'
                """,
                (
                    _utc_after(delay_seconds),
                    now,
                    user_subject,
                    endpoint_id,
                ),
            )
        return int(cursor.rowcount)

    def get_delivery(self, delivery_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise TaskNotFound("notification delivery not found")
        return _outbox_from_row(row)

    def requeue_delivery(self, delivery_id: str) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise TaskNotFound("notification delivery not found")
            if row["state"] not in {"failed", "deferred", "archived"}:
                raise TaskIntegrityError(
                    "only failed, deferred, or archived delivery can be requeued"
                )
            connection.execute(
                """
                UPDATE notification_outbox
                SET state = 'pending', attempt_count = 0,
                    next_attempt_at = ?, updated_at = ?, acknowledged_at = NULL
                WHERE delivery_id = ?
                """,
                (now, now, delivery_id),
            )
            updated = connection.execute(
                "SELECT * FROM notification_outbox WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return _outbox_from_row(updated)

    def archive_delivery(self, delivery_id: str) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise TaskNotFound("notification delivery not found")
            if row["state"] not in {"failed", "acknowledged"}:
                raise TaskIntegrityError(
                    "only failed or acknowledged delivery can be archived"
                )
            connection.execute(
                """
                UPDATE notification_outbox
                SET state = 'archived', updated_at = ?
                WHERE delivery_id = ?
                """,
                (now, delivery_id),
            )
            updated = connection.execute(
                "SELECT * FROM notification_outbox WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return _outbox_from_row(updated)

    @staticmethod
    def _subscribe_companion_endpoints(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        user_subject: str,
        created_at: str,
    ) -> None:
        endpoints = connection.execute(
            """
            SELECT endpoint_id, client_type, capabilities_json
            FROM client_endpoints
            WHERE user_subject = ? AND state = 'active'
            """,
            (user_subject,),
        ).fetchall()
        for endpoint in endpoints:
            if _is_pull_based_endpoint(endpoint):
                continue
            capabilities = set(json.loads(endpoint["capabilities_json"]))
            if not capabilities.intersection(
                {
                    "direct_status",
                    "trusted_interaction",
                    "workspace.task.read",
                }
            ):
                continue
            connection.execute(
                """
                INSERT INTO task_subscriptions (
                    subscription_id, task_id, endpoint_id, user_subject,
                    event_filters_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(task_id, endpoint_id) DO UPDATE SET
                    event_filters_json = excluded.event_filters_json,
                    state = 'active',
                    updated_at = excluded.updated_at
                """,
                (
                    str(uuid4()),
                    task_id,
                    endpoint["endpoint_id"],
                    user_subject,
                    _canonical_json(
                        [
                            "task.created",
                            "task.operation.linked",
                            "task.operation.running",
                            "task.interaction.waiting",
                            "task.interaction.completed",
                            "task.interaction.expired",
                            "task.interaction.failed",
                            "task.interaction.superseded",
                            "task.canceled",
                            "task.artifact.ready",
                            "task.completed",
                            "task.operation.succeeded",
                            "task.operation.failed",
                            "task.failed",
                            "task.operation.outcome_unknown",
                            "plan.result.ready",
                            "plan.step.waiting",
                            "plan.authorization.waiting",
                            "plan.completed",
                            "plan.canceled",
                            "plan.step.failed",
                            "plan.outcome_unknown",
                        ]
                    ),
                    created_at,
                    created_at,
                ),
            )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        user_subject: str,
        event_type: str,
        payload: dict[str, Any],
        causation_ref: str | None,
        created_at: str,
    ) -> str:
        event_id = str(uuid4())
        payload_json = _canonical_json(payload)
        connection.execute(
            """
            INSERT INTO task_events (
                event_id, task_id, user_subject, event_type, payload_json,
                causation_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                user_subject,
                event_type,
                payload_json,
                causation_ref,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO user_timeline (
                entry_id, user_subject, entry_type, dedupe_key,
                source_endpoint_id, task_id, role, text,
                payload_json, created_at
            ) VALUES (?, ?, 'task_event', ?, NULL, ?, NULL, NULL, ?, ?)
            """,
            (
                event_id,
                user_subject,
                f"task-event:{event_id}",
                task_id,
                _canonical_json(
                    {
                        "eventId": event_id,
                        "eventType": event_type,
                        "payload": payload,
                    }
                ),
                created_at,
            ),
        )
        subscriptions = connection.execute(
            """
            SELECT * FROM task_subscriptions
            WHERE task_id = ? AND user_subject = ? AND state = 'active'
            """,
            (task_id, user_subject),
        ).fetchall()
        for subscription in subscriptions:
            filters = json.loads(subscription["event_filters_json"])
            if "*" not in filters and event_type not in filters:
                continue
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_outbox (
                    delivery_id, event_id, task_id, endpoint_id, user_subject,
                    payload_type, payload_json, state, attempt_count,
                    next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'task_event', ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    event_id,
                    task_id,
                    subscription["endpoint_id"],
                    user_subject,
                    _canonical_json(
                        {
                            "eventId": event_id,
                            "taskId": task_id,
                            "eventType": event_type,
                            "payload": payload,
                        }
                    ),
                    created_at,
                    created_at,
                    created_at,
                ),
            )
        return event_id

    @staticmethod
    def _enqueue_timeline_message(
        connection: sqlite3.Connection,
        *,
        entry: sqlite3.Row,
        source_endpoint_id: str,
        notify_source: bool = False,
    ) -> None:
        source = connection.execute(
            """
            SELECT endpoint_id, client_type, label
            FROM client_endpoints
            WHERE endpoint_id = ?
            """,
            (source_endpoint_id,),
        ).fetchone()
        endpoints = connection.execute(
            """
            SELECT endpoint_id, client_type, capabilities_json
            FROM client_endpoints
            WHERE user_subject = ? AND state = 'active'
              AND (endpoint_id != ? OR ?)
            """,
            (entry["user_subject"], source_endpoint_id, notify_source),
        ).fetchall()
        entry_payload = json.loads(entry["payload_json"] or "{}")
        payload = _canonical_json(
            {
                "entryId": entry["entry_id"],
                "sequence": entry["sequence"],
                "role": entry["role"],
                "text": entry["text"],
                "attachments": list(entry_payload.get("attachments") or []),
                "createdAt": entry["created_at"],
                "source": {
                    "endpointId": source_endpoint_id,
                    "clientType": source["client_type"] if source else "unknown",
                    "label": source["label"] if source else None,
                },
            }
        )
        for endpoint in endpoints:
            capabilities = set(json.loads(endpoint["capabilities_json"]))
            if endpoint["client_type"] in {"web", "webchat"}:
                continue
            if not capabilities.intersection(
                {"direct_status", "timeline_message"}
            ):
                continue
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_outbox (
                    delivery_id, event_id, task_id, endpoint_id, user_subject,
                    payload_type, payload_json, state, attempt_count,
                    next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'timeline_message', ?,
                          'pending', 0, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    entry["entry_id"],
                    entry["task_id"] or "",
                    endpoint["endpoint_id"],
                    entry["user_subject"],
                    payload,
                    entry["created_at"],
                    entry["created_at"],
                    entry["created_at"],
                ),
            )

    def _select_owned_batch_for_task(
        self,
        connection: sqlite3.Connection,
        parent_task_id: str,
        user_subject: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM task_batches
            WHERE parent_task_id = ? AND user_subject = ?
            """,
            (parent_task_id, user_subject),
        ).fetchone()
        if row is None:
            raise TaskNotFound("batch task not found")
        return row

    @staticmethod
    def _select_current_batch_item(
        connection: sqlite3.Connection,
        batch: sqlite3.Row,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM task_batch_items
            WHERE batch_id = ? AND ordinal = ?
            """,
            (batch["batch_id"], batch["current_ordinal"]),
        ).fetchone()
        if row is None:
            raise TaskIntegrityError("batch current item is missing")
        return row

    def _batch_snapshot(
        self,
        connection: sqlite3.Connection,
        batch: sqlite3.Row,
        *,
        include_resource_refs: bool,
    ) -> dict:
        rows = connection.execute(
            """
            SELECT * FROM task_batch_items
            WHERE batch_id = ?
            ORDER BY ordinal
            """,
            (batch["batch_id"],),
        ).fetchall()
        value = _batch_from_row(batch)
        value["items"] = [
            _batch_item_from_row(
                row,
                include_resource_ref=include_resource_refs,
            )
            for row in rows
        ]
        return value

    def _sync_batch_task_summary(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        batch: sqlite3.Row,
        *,
        now: str,
        task_status: str | None = None,
        current_operation_id: str | None = None,
        current_interaction_id: str | None = None,
    ) -> None:
        current_item = self._select_current_batch_item(connection, batch)
        summary = json.loads(task["summary_json"] or "{}")
        summary["batch"] = {
            "batchId": batch["batch_id"],
            "systemId": batch["system_id"],
            "capability": batch["capability_name"],
            "state": batch["state"],
            "currentOrdinal": int(batch["current_ordinal"]),
            "totalCount": int(batch["total_count"]),
            "succeededCount": int(batch["succeeded_count"]),
            "failedCount": int(batch["failed_count"]),
            "skippedCount": int(batch["skipped_count"]),
            "failurePolicy": batch["failure_policy"],
            "currentItem": {
                "ordinal": int(current_item["ordinal"]),
                "state": current_item["state"],
                "display": json.loads(current_item["display_summary_json"] or "{}"),
            },
        }
        connection.execute(
            "UPDATE agent_tasks SET summary_json = ? WHERE task_id = ?",
            (_canonical_json(summary), task["task_id"]),
        )
        if task_status is None:
            task_status = {
                "waiting_user": "waiting_user",
                "running": "running",
                "paused": "waiting_user",
                "partially_succeeded": "partially_succeeded",
                "outcome_unknown": "outcome_unknown",
                "failed": "failed",
                "canceled": "canceled",
                "expired": "expired",
                "succeeded": "succeeded",
            }.get(str(batch["state"]), "active")
        self._update_task_state(
            connection,
            task_id=task["task_id"],
            status=task_status,
            current_operation_id=(
                current_operation_id
                if current_operation_id is not None
                else task["current_operation_id"]
            ),
            current_interaction_id=(
                current_interaction_id
                if current_interaction_id is not None
                else task["current_interaction_id"]
            ),
            now=now,
        )

    @staticmethod
    def _update_task_state(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        status: str,
        current_operation_id: str | None,
        current_interaction_id: str | None,
        now: str,
    ) -> None:
        if status not in TASK_STATUSES:
            raise ValueError(f"unsupported task status: {status}")
        finished_at = now if status in {
            "succeeded",
            "partially_succeeded",
            "failed",
            "outcome_unknown",
            "canceled",
            "expired",
            "superseded",
        } else None
        connection.execute(
            """
            UPDATE agent_tasks
            SET status = ?, current_operation_id = ?,
                current_interaction_id = ?, version = version + 1,
                updated_at = ?, finished_at = ?
            WHERE task_id = ?
            """,
            (
                status,
                current_operation_id,
                current_interaction_id,
                now,
                finished_at,
                task_id,
            ),
        )

    @staticmethod
    def _select_endpoint(
        connection: sqlite3.Connection,
        endpoint_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM client_endpoints WHERE endpoint_id = ?",
            (endpoint_id,),
        ).fetchone()
        if row is None:
            raise TaskNotFound(f"client endpoint not found: {endpoint_id}")
        return row

    @staticmethod
    def _select_task(
        connection: sqlite3.Connection,
        task_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM agent_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise TaskNotFound(f"task not found: {task_id}")
        return row

    def _select_owned_task(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        user_subject: str,
    ) -> sqlite3.Row:
        row = self._select_task(connection, task_id)
        if row["user_subject"] != user_subject:
            raise TaskNotFound(f"task not found: {task_id}")
        return row


def _task_status_for_operation(status: str) -> str:
    return {
        "pending": "running",
        "running": "running",
        "requires_user_action": "waiting_user",
        "succeeded": "succeeded",
        "failed": "failed",
        "unknown": "outcome_unknown",
    }.get(status, "active")


def _event_type_for_operation(status: str) -> str:
    return {
        "pending": "task.operation.running",
        "running": "task.operation.running",
        "requires_user_action": "task.operation.requires_user_action",
        "succeeded": "task.operation.succeeded",
        "failed": "task.operation.failed",
        "unknown": "task.operation.outcome_unknown",
    }.get(status, "task.operation.updated")


def _task_status_for_interaction(
    state: str,
    *,
    interaction_type: str = "",
    has_current_operation: bool = False,
) -> str:
    if (
        state == "completed"
        and interaction_type == "credential"
        and not has_current_operation
    ):
        return "succeeded"
    return {
        "pending": "waiting_user",
        "processing": "waiting_user",
        "completed": "active",
        "declined": "canceled",
        "expired": "expired",
        "failed": "failed",
        "superseded": "superseded",
    }.get(state, "active")


def _event_type_for_interaction(state: str) -> str:
    return {
        "pending": "task.interaction.waiting",
        "processing": "task.interaction.waiting",
        "completed": "task.interaction.completed",
        "declined": "task.canceled",
        "expired": "task.interaction.expired",
        "failed": "task.interaction.failed",
        "superseded": "task.interaction.superseded",
    }.get(state, "task.interaction.updated")


def _interaction_may_update_task(
    *,
    task: sqlite3.Row,
    interaction_id: str,
    newly_linked: bool,
) -> bool:
    if task["status"] in TERMINAL_TASK_STATUSES:
        return False
    if newly_linked:
        return True
    return task["current_interaction_id"] == interaction_id


def _endpoint_from_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["capabilities"] = json.loads(value.pop("capabilities_json"))
    value["route"] = json.loads(value.pop("route_json"))
    return value


def _task_from_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["summary"] = json.loads(value.pop("summary_json"))
    return value


def _batch_from_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["selection_summary"] = json.loads(
        value.pop("selection_summary_json") or "{}"
    )
    return value


def _batch_item_from_row(
    row: sqlite3.Row,
    *,
    include_resource_ref: bool,
) -> dict:
    value = dict(row)
    value["display_summary"] = json.loads(
        value.pop("display_summary_json") or "{}"
    )
    value["result_summary"] = json.loads(
        value.pop("result_summary_json") or "{}"
    )
    if not include_resource_ref:
        value.pop("resource_ref", None)
    return value


def _artifact_from_row(
    row: sqlite3.Row,
    *,
    include_source_ref: bool = False,
) -> dict:
    result = {
        "artifact_id": row["artifact_id"],
        "task_id": row["task_id"],
        "user_subject": row["user_subject"],
        "artifact_type": row["artifact_type"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "byte_size": int(row["byte_size"]),
        "download_url": row["download_url"],
        "state": row["state"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
    }
    if include_source_ref:
        result["source_ref"] = row["source_ref"]
    return result


def _event_from_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


def _outbox_from_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


def _timeline_from_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


def _continuation_from_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["candidate_task_ids"] = json.loads(
        value.pop("candidate_task_ids_json")
    )
    value["allow_new_operation"] = value["execution_mode"] in {
        "resume",
        "follow_up",
    }
    return value


def _artifact_delivery_aggregate(by_channel: dict[str, dict]) -> dict:
    artifacts: set[str] = set()
    parts: list[str] = []
    delivered_channels = 0
    failed_channels = 0
    for channel, report in sorted(by_channel.items()):
        files = report.get("files") if isinstance(report, dict) else []
        for item in (files if isinstance(files, list) else []):
            artifact_id = item.get("artifactId") if isinstance(item, dict) else None
            if artifact_id:
                artifacts.add(str(artifact_id))
        attachment_count = max(0, int(report.get("attachmentSentCount") or 0))
        fallback_count = max(0, int(report.get("fallbackLinkSentCount") or 0))
        failed_count = max(0, int(report.get("failedCount") or 0))
        label = {
            "telegram": "Telegram",
            "openclaw-weixin": "微信",
            "weixin": "微信",
            "wechat": "微信",
            "web": "网页端",
            "webchat": "网页端",
            "agentbridge-workspace": "网页端",
        }.get(str(channel).casefold(), str(channel))
        channel_parts = []
        if attachment_count:
            channel_parts.append(f"{attachment_count} 份附件已送达")
        if fallback_count:
            channel_parts.append(
                f"{fallback_count} 个下载入口可用"
                if label == "网页端"
                else f"{fallback_count} 份已通过下载链接送达"
            )
        if failed_count:
            channel_parts.append(f"{failed_count} 份未送达")
        if channel_parts:
            parts.append(f"{label}：{'，'.join(channel_parts)}")
        if report.get("state") == "delivered":
            delivered_channels += 1
        elif report.get("state") in {"partial", "failed"}:
            failed_channels += 1
    prepared_count = len(artifacts)
    prefix = f"{prepared_count} 份文件已准备" if prepared_count else "文件已准备"
    return {
        "state": "partial" if failed_channels else "delivered",
        "completionMeaning": "cross_endpoint_delivery_reported",
        "preparedCount": prepared_count,
        "channelCount": len(by_channel),
        "deliveredChannelCount": delivered_channels,
        "failedChannelCount": failed_channels,
        "channels": by_channel,
        "userMessage": f"{prefix}；{'；'.join(parts)}。" if parts else f"{prefix}。",
    }


def _safe_object(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("value must be an object")
    return value


def _required_text(value: Any, name: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(f"{name} is invalid")
    return normalized


def _optional_text(value: Any, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return _required_text(normalized, name, maximum)


def _required_future_time(value: Any, name: str) -> str:
    normalized = _required_text(value, name, 80)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} is invalid")
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= datetime.now(timezone.utc):
        raise ValueError(f"{name} is expired")
    return parsed.isoformat()


def _artifact_download_url(value: Any) -> str:
    normalized = _required_text(value, "download_url", 2_048)
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("download_url is invalid")
    return normalized


def _timeline_text(value: Any) -> str:
    normalized = str(value or "").replace("\0", "").strip()
    if not normalized or len(normalized) > 50_000:
        raise ValueError("timeline text is invalid")
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_after(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat()


def _utc_before(*, minutes: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat()


def _client_type_family(value: str | None) -> tuple[str, ...]:
    normalized = str(value or "").strip().lower()
    if normalized in {"web", "webchat"}:
        return ("web", "webchat")
    if normalized in {"wechat", "weixin", "openclaw-weixin"}:
        return ("wechat", "weixin", "openclaw-weixin")
    if normalized:
        return (normalized,)
    return ()
