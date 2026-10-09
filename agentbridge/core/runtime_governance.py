from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from time import perf_counter
from typing import Any, Iterator, Mapping
from uuid import uuid4


SIDE_EFFECT_BOUNDARIES = (
    "B0_NO_EFFECT",
    "B1_READ_ONLY",
    "B2_INTERACTION_CREATED",
    "B3_PREPARED_AUTHORIZED",
    "B4_COMMIT_ATTEMPTED",
    "B5_VERIFIED",
)
TRACE_STATUSES = {
    "active",
    "waiting",
    "succeeded",
    "failed",
    "unknown",
    "cancelled",
}
INCIDENT_STATES = {
    "open",
    "acknowledged",
    "investigating",
    "resolved",
    "suppressed",
}
INCIDENT_SEVERITIES = {"P0", "P1", "P2", "P3"}
OBSERVATION_STATES = {"active", "completed", "cancelled"}

_TERMINAL_TASK_STATUSES = {
    "succeeded",
    "partially_succeeded",
    "failed",
    "outcome_unknown",
    "canceled",
    "expired",
    "superseded",
}
_ACTIVE_INCIDENT_STATES = {"open", "acknowledged", "investigating"}


class RuntimeGovernanceStore:
    """Persistent, non-sensitive execution and reliability ledger."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        release_id: str = "development",
        clock: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.release_id = str(release_id or "development")[:160]
        self._clock = clock or (lambda: datetime.now(timezone.utc))
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
                CREATE TABLE IF NOT EXISTS runtime_traces (
                    trace_id TEXT PRIMARY KEY,
                    request_id TEXT,
                    user_subject TEXT NOT NULL,
                    task_id TEXT,
                    origin_endpoint_id TEXT,
                    host_type TEXT NOT NULL,
                    host_instance_id TEXT,
                    host_run_id TEXT,
                    request_kind TEXT NOT NULL,
                    system_id TEXT,
                    capability_name TEXT,
                    status TEXT NOT NULL,
                    side_effect_boundary TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS runtime_traces_subject_started
                ON runtime_traces (user_subject, started_at DESC);

                CREATE INDEX IF NOT EXISTS runtime_traces_task_status
                ON runtime_traces (task_id, status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS runtime_traces_request
                ON runtime_traces (user_subject, request_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS runtime_spans (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    user_subject TEXT NOT NULL,
                    task_id TEXT,
                    operation_id TEXT,
                    interaction_id TEXT,
                    artifact_id TEXT,
                    delivery_id TEXT,
                    system_id TEXT,
                    capability_name TEXT,
                    attempt INTEGER NOT NULL,
                    side_effect_boundary TEXT NOT NULL,
                    error_code TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_ms INTEGER,
                    metadata_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS runtime_spans_trace_started
                ON runtime_spans (trace_id, started_at);

                CREATE INDEX IF NOT EXISTS runtime_spans_stage_started
                ON runtime_spans (stage, started_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS runtime_spans_operation_stage
                ON runtime_spans (trace_id, stage, operation_id, attempt)
                WHERE operation_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS runtime_signals (
                    signal_id TEXT PRIMARY KEY,
                    signal_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    system_id TEXT,
                    user_subject TEXT,
                    host_type TEXT,
                    host_instance_id TEXT,
                    trace_id TEXT,
                    value_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS runtime_signals_type_observed
                ON runtime_signals (signal_type, observed_at DESC);

                CREATE TABLE IF NOT EXISTS runtime_incidents (
                    incident_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    state TEXT NOT NULL,
                    symptom_code TEXT NOT NULL,
                    root_cause_code TEXT,
                    actionability TEXT NOT NULL,
                    title TEXT NOT NULL,
                    trace_id TEXT,
                    user_subject TEXT,
                    system_id TEXT,
                    host_type TEXT,
                    object_type TEXT,
                    object_id TEXT,
                    evidence_json TEXT NOT NULL,
                    recommended_action TEXT,
                    occurrence_count INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS runtime_incidents_state_severity
                ON runtime_incidents (state, severity, last_seen_at DESC);

                CREATE INDEX IF NOT EXISTS runtime_incidents_fingerprint
                ON runtime_incidents (fingerprint, last_seen_at DESC);

                CREATE TABLE IF NOT EXISTS runtime_incident_events (
                    event_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT,
                    before_state TEXT,
                    after_state TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS runtime_incident_events_incident
                ON runtime_incident_events (incident_id, created_at);

                CREATE TABLE IF NOT EXISTS runtime_recovery_actions (
                    action_id TEXT PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    side_effect_boundary TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS runtime_recovery_actions_created
                ON runtime_recovery_actions (created_at DESC);

                CREATE TABLE IF NOT EXISTS runtime_slo_rollups (
                    rollup_id TEXT PRIMARY KEY,
                    metric_key TEXT NOT NULL,
                    dimension_key TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    success_count INTEGER,
                    value REAL,
                    target REAL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (metric_key, dimension_key, window_start, window_end)
                );

                CREATE INDEX IF NOT EXISTS runtime_slo_rollups_window
                ON runtime_slo_rollups (window_end DESC, metric_key);

                CREATE TABLE IF NOT EXISTS runtime_observations (
                    observation_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    duration_hours INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    baseline_json TEXT NOT NULL,
                    latest_snapshot_json TEXT NOT NULL,
                    snapshot_count INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    last_snapshot_at TEXT,
                    completed_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS runtime_observations_active_name
                ON runtime_observations (name)
                WHERE state = 'active';

                CREATE INDEX IF NOT EXISTS runtime_observations_state_started
                ON runtime_observations (state, started_at DESC);

                CREATE TABLE IF NOT EXISTS runtime_observation_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    observation_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    slo_json TEXT NOT NULL,
                    incident_counts_json TEXT NOT NULL,
                    collection_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS runtime_observation_snapshots_observation
                ON runtime_observation_snapshots (observation_id, observed_at);
                """
            )

    def ensure_trace(
        self,
        *,
        user_subject: str,
        request_id: str | None = None,
        task_id: str | None = None,
        origin_endpoint_id: str | None = None,
        host_type: str = "unknown",
        host_instance_id: str | None = None,
        host_run_id: str | None = None,
        request_kind: str = "read",
        system_id: str | None = None,
        capability_name: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        user_subject = _bounded_text(user_subject, 256, required=True)
        task_id = _bounded_text(task_id, 128)
        request_id = _bounded_text(request_id, 256)
        now = self._now()
        with self._connect() as connection:
            existing = None
            if task_id:
                existing = connection.execute(
                    """
                    SELECT * FROM runtime_traces
                    WHERE user_subject = ? AND task_id = ?
                      AND status IN ('active', 'waiting')
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (user_subject, task_id),
                ).fetchone()
            elif request_id:
                existing = connection.execute(
                    """
                    SELECT * FROM runtime_traces
                    WHERE user_subject = ? AND request_id = ?
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (user_subject, request_id),
                ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE runtime_traces
                    SET origin_endpoint_id = COALESCE(?, origin_endpoint_id),
                        host_type = CASE WHEN ? = 'unknown' THEN host_type ELSE ? END,
                        host_instance_id = COALESCE(?, host_instance_id),
                        host_run_id = COALESCE(?, host_run_id),
                        system_id = COALESCE(?, system_id),
                        capability_name = COALESCE(?, capability_name),
                        updated_at = ?
                    WHERE trace_id = ?
                    """,
                    (
                        _bounded_text(origin_endpoint_id, 128),
                        host_type,
                        _bounded_text(host_type, 80, required=True),
                        _bounded_text(host_instance_id, 160),
                        _bounded_text(host_run_id, 256),
                        _bounded_text(system_id, 80),
                        _bounded_text(capability_name, 200),
                        now,
                        existing["trace_id"],
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM runtime_traces WHERE trace_id = ?",
                    (existing["trace_id"],),
                ).fetchone()
                return _trace_from_row(row), True

            trace_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO runtime_traces (
                    trace_id, request_id, user_subject, task_id,
                    origin_endpoint_id, host_type, host_instance_id,
                    host_run_id, request_kind, system_id, capability_name,
                    status, side_effect_boundary, release_id,
                    started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'active', 'B0_NO_EFFECT', ?, ?, ?)
                """,
                (
                    trace_id,
                    request_id,
                    user_subject,
                    task_id,
                    _bounded_text(origin_endpoint_id, 128),
                    _bounded_text(host_type, 80, required=True),
                    _bounded_text(host_instance_id, 160),
                    _bounded_text(host_run_id, 256),
                    _bounded_text(request_kind, 40, required=True),
                    _bounded_text(system_id, 80),
                    _bounded_text(capability_name, 200),
                    self.release_id,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
        return _trace_from_row(row), False

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"runtime trace not found: {trace_id}")
        return _trace_from_row(row)

    def trace_for_task(self, task_id: str, *, user_subject: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runtime_traces
                WHERE task_id = ? AND user_subject = ?
                ORDER BY started_at DESC LIMIT 1
                """,
                (task_id, user_subject),
            ).fetchone()
        return _trace_from_row(row) if row is not None else None

    def trace_for_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT trace.* FROM runtime_traces AS trace
                JOIN runtime_spans AS span ON span.trace_id = trace.trace_id
                WHERE span.operation_id = ?
                ORDER BY span.started_at DESC LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
        return _trace_from_row(row) if row is not None else None

    def trace_for_request(
        self, request_id: str, *, user_subject: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runtime_traces
                WHERE request_id = ? AND user_subject = ?
                ORDER BY started_at DESC LIMIT 1
                """,
                (request_id, user_subject),
            ).fetchone()
        return _trace_from_row(row) if row is not None else None

    def update_trace(
        self,
        trace_id: str,
        *,
        status: str | None = None,
        side_effect_boundary: str | None = None,
        finished: bool | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in TRACE_STATUSES:
            raise ValueError(f"unsupported runtime trace status: {status}")
        if side_effect_boundary is not None:
            _validate_boundary(side_effect_boundary)
        now = self._now()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM runtime_traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"runtime trace not found: {trace_id}")
            effective_boundary = _max_boundary(
                str(current["side_effect_boundary"]),
                side_effect_boundary or str(current["side_effect_boundary"]),
            )
            effective_status = status or str(current["status"])
            terminal = (
                finished
                if finished is not None
                else effective_status in {"succeeded", "failed", "unknown", "cancelled"}
            )
            if terminal:
                finished_at = now
            elif current["finished_at"] and status is None:
                finished_at = current["finished_at"]
            else:
                finished_at = None
            connection.execute(
                """
                UPDATE runtime_traces
                SET status = ?, side_effect_boundary = ?, updated_at = ?,
                    finished_at = ?
                WHERE trace_id = ?
                """,
                (
                    effective_status,
                    effective_boundary,
                    now,
                    finished_at,
                    trace_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
        return _trace_from_row(row)

    def start_span(
        self,
        *,
        trace_id: str,
        stage: str,
        parent_span_id: str | None = None,
        operation_id: str | None = None,
        interaction_id: str | None = None,
        artifact_id: str | None = None,
        delivery_id: str | None = None,
        system_id: str | None = None,
        capability_name: str | None = None,
        attempt: int = 1,
        side_effect_boundary: str = "B0_NO_EFFECT",
        metadata: Mapping[str, Any] | None = None,
        started_at: str | None = None,
    ) -> dict[str, Any]:
        _validate_boundary(side_effect_boundary)
        trace = self.get_trace(trace_id)
        span_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_spans (
                    span_id, trace_id, parent_span_id, stage, status,
                    user_subject, task_id, operation_id, interaction_id,
                    artifact_id, delivery_id, system_id, capability_name,
                    attempt, side_effect_boundary, started_at, metadata_json
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span_id,
                    trace_id,
                    _bounded_text(parent_span_id, 128),
                    _bounded_text(stage, 120, required=True),
                    trace["user_subject"],
                    trace.get("task_id"),
                    _bounded_text(operation_id, 256),
                    _bounded_text(interaction_id, 256),
                    _bounded_text(artifact_id, 256),
                    _bounded_text(delivery_id, 256),
                    _bounded_text(system_id or trace.get("system_id"), 80),
                    _bounded_text(capability_name or trace.get("capability_name"), 200),
                    max(int(attempt), 1),
                    side_effect_boundary,
                    started_at or self._now(),
                    _canonical_json(_safe_metadata(metadata)),
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_spans WHERE span_id = ?", (span_id,)
            ).fetchone()
        self.update_trace(trace_id, side_effect_boundary=side_effect_boundary, finished=False)
        return _span_from_row(row)

    def finish_span(
        self,
        span_id: str,
        *,
        status: str,
        error_code: str | None = None,
        side_effect_boundary: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        now = finished_at or self._now()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM runtime_spans WHERE span_id = ?", (span_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"runtime span not found: {span_id}")
            boundary = side_effect_boundary or str(current["side_effect_boundary"])
            _validate_boundary(boundary)
            effective_duration = duration_ms
            if effective_duration is None:
                effective_duration = _duration_ms(str(current["started_at"]), now)
            merged_metadata = _decode_object(current["metadata_json"])
            merged_metadata.update(_safe_metadata(metadata))
            connection.execute(
                """
                UPDATE runtime_spans
                SET status = ?, error_code = ?, side_effect_boundary = ?,
                    finished_at = ?, duration_ms = ?, metadata_json = ?
                WHERE span_id = ?
                """,
                (
                    _bounded_text(status, 40, required=True),
                    _bounded_text(error_code, 120),
                    boundary,
                    now,
                    max(int(effective_duration), 0),
                    _canonical_json(merged_metadata),
                    span_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_spans WHERE span_id = ?", (span_id,)
            ).fetchone()
        self.update_trace(str(row["trace_id"]), side_effect_boundary=boundary, finished=False)
        return _span_from_row(row)

    @contextmanager
    def span(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        started = perf_counter()
        record = self.start_span(**kwargs)
        try:
            yield record
        except Exception as exc:
            self.finish_span(
                record["span_id"],
                status="failed",
                error_code=classify_runtime_error(exc)["code"],
                duration_ms=round((perf_counter() - started) * 1000),
            )
            raise
        else:
            self.finish_span(
                record["span_id"],
                status="succeeded",
                duration_ms=round((perf_counter() - started) * 1000),
            )

    def record_stage_once(
        self,
        *,
        trace_id: str,
        stage: str,
        status: str = "succeeded",
        operation_id: str | None = None,
        interaction_id: str | None = None,
        artifact_id: str | None = None,
        delivery_id: str | None = None,
        system_id: str | None = None,
        capability_name: str | None = None,
        error_code: str | None = None,
        side_effect_boundary: str = "B0_NO_EFFECT",
        metadata: Mapping[str, Any] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        attempt = 1
        with self._connect() as connection:
            filters = ["trace_id = ?", "stage = ?"]
            values: list[Any] = [trace_id, stage]
            for column, value in (
                ("operation_id", operation_id),
                ("interaction_id", interaction_id),
                ("artifact_id", artifact_id),
                ("delivery_id", delivery_id),
            ):
                if value:
                    filters.append(f"{column} = ?")
                    values.append(value)
            existing = connection.execute(
                f"SELECT * FROM runtime_spans WHERE {' AND '.join(filters)} "
                "ORDER BY started_at DESC LIMIT 1",
                values,
            ).fetchone()
        if existing is not None:
            return _span_from_row(existing)
        span = self.start_span(
            trace_id=trace_id,
            stage=stage,
            operation_id=operation_id,
            interaction_id=interaction_id,
            artifact_id=artifact_id,
            delivery_id=delivery_id,
            system_id=system_id,
            capability_name=capability_name,
            attempt=attempt,
            side_effect_boundary=side_effect_boundary,
            metadata=metadata,
            started_at=started_at,
        )
        return self.finish_span(
            span["span_id"],
            status=status,
            error_code=error_code,
            side_effect_boundary=side_effect_boundary,
            metadata=metadata,
            finished_at=finished_at,
            duration_ms=duration_ms,
        )

    def observe_operation(
        self,
        *,
        trace_id: str,
        operation: Mapping[str, Any],
        capability_effect: str,
        commit_capability: bool = False,
    ) -> dict[str, Any]:
        status = str(operation.get("status") or "failed")
        error = operation.get("error") if isinstance(operation.get("error"), Mapping) else {}
        error_code = str(error.get("code") or "") or None
        if capability_effect == "read":
            boundary = "B1_READ_ONLY"
        elif commit_capability:
            boundary = "B5_VERIFIED" if status == "succeeded" else "B4_COMMIT_ATTEMPTED"
        elif status == "requires_user_action":
            boundary = "B2_INTERACTION_CREATED"
        else:
            boundary = "B3_PREPARED_AUTHORIZED"
        span_status = {
            "succeeded": "succeeded",
            "requires_user_action": "waiting",
            "unknown": "unknown",
        }.get(status, "failed")
        recorded = self.record_stage_once(
            trace_id=trace_id,
            stage="capability.invoke",
            status=span_status,
            operation_id=str(operation.get("operation_id") or "") or None,
            capability_name=str(operation.get("capability_name") or "") or None,
            error_code=error_code,
            side_effect_boundary=boundary,
            started_at=str(operation.get("created_at") or "") or None,
            finished_at=str(operation.get("finished_at") or operation.get("updated_at") or "") or None,
        )
        trace_status = {
            "succeeded": "succeeded",
            "requires_user_action": "waiting",
            "unknown": "unknown",
        }.get(status, "failed")
        self.update_trace(
            trace_id,
            status=trace_status,
            side_effect_boundary=boundary,
            finished=trace_status not in {"waiting", "active"},
        )
        if status == "unknown":
            self.upsert_incident(
                rule_id="write_outcome_unknown",
                severity="P1",
                symptom_code="RESULT_UNKNOWN",
                actionability="manual_reconciliation",
                title="业务写操作结果未知",
                trace_id=trace_id,
                user_subject=str(operation.get("user_subject") or "") or None,
                object_type="operation",
                object_id=str(operation.get("operation_id") or "") or None,
                evidence={"errorCode": error_code, "boundary": boundary},
                recommended_action="到权威业务系统核对实际结果，禁止自动再次提交。",
            )
        return recorded

    def observe_interaction(
        self,
        *,
        trace_id: str,
        interaction_id: str,
        interaction_type: str,
        state: str,
        system_id: str | None = None,
    ) -> dict[str, Any]:
        terminal = state in {"completed", "declined", "expired", "failed", "superseded"}
        status = (
            "succeeded"
            if state == "completed"
            else "failed"
            if terminal
            else "waiting"
        )
        stage = "interaction.resume" if terminal else "interaction.wait"
        boundary = (
            "B3_PREPARED_AUTHORIZED"
            if interaction_type == "execution_authorization" and state == "completed"
            else "B2_INTERACTION_CREATED"
        )
        span = self.record_stage_once(
            trace_id=trace_id,
            stage=stage,
            status=status,
            interaction_id=interaction_id,
            system_id=system_id,
            side_effect_boundary=boundary,
            metadata={"interactionType": interaction_type, "state": state},
        )
        if not terminal:
            self.update_trace(
                trace_id,
                status="waiting",
                side_effect_boundary=boundary,
                finished=False,
            )
        return span

    def record_signal(
        self,
        *,
        signal_type: str,
        source: str,
        status: str,
        value: Mapping[str, Any] | None = None,
        system_id: str | None = None,
        user_subject: str | None = None,
        host_type: str | None = None,
        host_instance_id: str | None = None,
        trace_id: str | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        signal_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_signals (
                    signal_id, signal_type, source, status, system_id,
                    user_subject, host_type, host_instance_id, trace_id,
                    value_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    _bounded_text(signal_type, 120, required=True),
                    _bounded_text(source, 160, required=True),
                    _bounded_text(status, 40, required=True),
                    _bounded_text(system_id, 80),
                    _bounded_text(user_subject, 256),
                    _bounded_text(host_type, 80),
                    _bounded_text(host_instance_id, 160),
                    _bounded_text(trace_id, 128),
                    _canonical_json(_safe_metadata(value)),
                    observed_at or self._now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        return _signal_from_row(row)

    def list_traces(
        self,
        *,
        user_subject: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT trace.*,
                   COUNT(span.span_id) AS stage_count,
                   MAX(span.finished_at) AS last_stage_at,
                   SUM(COALESCE(span.duration_ms, 0)) AS recorded_duration_ms
            FROM runtime_traces AS trace
            LEFT JOIN runtime_spans AS span ON span.trace_id = trace.trace_id
        """
        filters: list[str] = []
        parameters: list[Any] = []
        if user_subject:
            filters.append("trace.user_subject = ?")
            parameters.append(user_subject)
        if status:
            filters.append("trace.status = ?")
            parameters.append(status)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " GROUP BY trace.trace_id ORDER BY trace.started_at DESC LIMIT ?"
        parameters.append(min(max(int(limit), 1), 1000))
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_trace_from_row(row) for row in rows]

    def trace_detail(self, trace_id: str) -> dict[str, Any]:
        trace = self.get_trace(trace_id)
        with self._connect() as connection:
            spans = connection.execute(
                """
                SELECT * FROM runtime_spans
                WHERE trace_id = ? ORDER BY started_at, rowid
                """,
                (trace_id,),
            ).fetchall()
            incidents = connection.execute(
                """
                SELECT * FROM runtime_incidents
                WHERE trace_id = ? ORDER BY last_seen_at DESC
                """,
                (trace_id,),
            ).fetchall()
        return {
            "trace": trace,
            "spans": [_span_from_row(row) for row in spans],
            "incidents": [_incident_from_row(row) for row in incidents],
        }

    def list_signals(
        self, *, signal_type: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM runtime_signals"
        parameters: list[Any] = []
        if signal_type:
            query += " WHERE signal_type = ?"
            parameters.append(signal_type)
        query += " ORDER BY observed_at DESC LIMIT ?"
        parameters.append(min(max(int(limit), 1), 1000))
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_signal_from_row(row) for row in rows]

    def upsert_incident(
        self,
        *,
        rule_id: str,
        severity: str,
        symptom_code: str,
        actionability: str,
        title: str,
        root_cause_code: str | None = None,
        trace_id: str | None = None,
        user_subject: str | None = None,
        system_id: str | None = None,
        host_type: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        recommended_action: str | None = None,
    ) -> dict[str, Any]:
        if severity not in INCIDENT_SEVERITIES:
            raise ValueError(f"unsupported incident severity: {severity}")
        fingerprint = ":".join(
            str(value or "-")
            for value in (rule_id, system_id, host_type, object_type, object_id)
        )[:900]
        now = self._now()
        with self._connect() as connection:
            # Serialize detection with manual closure; one immutable write attempt
            # must not become a new incident merely because it is scanned again.
            connection.execute("BEGIN IMMEDIATE")
            if rule_id == "write_outcome_unknown" and object_type == "operation" and object_id:
                settled = connection.execute(
                    """
                    SELECT * FROM runtime_incidents
                    WHERE rule_id = ? AND object_type = 'operation' AND object_id = ?
                      AND state IN ('resolved', 'suppressed')
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (rule_id, object_id),
                ).fetchone()
                if settled is not None:
                    return _incident_from_row(settled)
            current = connection.execute(
                """
                SELECT * FROM runtime_incidents
                WHERE fingerprint = ?
                  AND state IN ('open', 'acknowledged', 'investigating')
                ORDER BY last_seen_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
            if current is not None:
                connection.execute(
                    """
                    UPDATE runtime_incidents
                    SET severity = ?, symptom_code = ?, root_cause_code = ?,
                        actionability = ?, title = ?, trace_id = COALESCE(?, trace_id),
                        user_subject = COALESCE(?, user_subject),
                        system_id = COALESCE(?, system_id),
                        host_type = COALESCE(?, host_type),
                        evidence_json = ?, recommended_action = ?,
                        occurrence_count = occurrence_count + 1,
                        last_seen_at = ?, updated_at = ?
                    WHERE incident_id = ?
                    """,
                    (
                        severity,
                        _bounded_text(symptom_code, 120, required=True),
                        _bounded_text(root_cause_code, 120),
                        _bounded_text(actionability, 40, required=True),
                        _bounded_text(title, 240, required=True),
                        _bounded_text(trace_id, 128),
                        _bounded_text(user_subject, 256),
                        _bounded_text(system_id, 80),
                        _bounded_text(host_type, 80),
                        _canonical_json(_safe_metadata(evidence)),
                        _bounded_text(recommended_action, 500),
                        now,
                        now,
                        current["incident_id"],
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM runtime_incidents WHERE incident_id = ?",
                    (current["incident_id"],),
                ).fetchone()
                return _incident_from_row(row)

            incident_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO runtime_incidents (
                    incident_id, fingerprint, rule_id, severity, state,
                    symptom_code, root_cause_code, actionability, title,
                    trace_id, user_subject, system_id, host_type,
                    object_type, object_id, evidence_json, recommended_action,
                    occurrence_count, first_seen_at, last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          1, ?, ?, ?)
                """,
                (
                    incident_id,
                    fingerprint,
                    _bounded_text(rule_id, 120, required=True),
                    severity,
                    _bounded_text(symptom_code, 120, required=True),
                    _bounded_text(root_cause_code, 120),
                    _bounded_text(actionability, 40, required=True),
                    _bounded_text(title, 240, required=True),
                    _bounded_text(trace_id, 128),
                    _bounded_text(user_subject, 256),
                    _bounded_text(system_id, 80),
                    _bounded_text(host_type, 80),
                    _bounded_text(object_type, 80),
                    _bounded_text(object_id, 256),
                    _canonical_json(_safe_metadata(evidence)),
                    _bounded_text(recommended_action, 500),
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO runtime_incident_events (
                    event_id, incident_id, event_type, actor, after_state, created_at
                ) VALUES (?, ?, 'detected', 'agentbridge', 'open', ?)
                """,
                (str(uuid4()), incident_id, now),
            )
            row = connection.execute(
                "SELECT * FROM runtime_incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
        return _incident_from_row(row)

    def transition_incident(
        self,
        incident_id: str,
        *,
        state: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        if state not in INCIDENT_STATES:
            raise ValueError(f"unsupported incident state: {state}")
        now = self._now()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM runtime_incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"runtime incident not found: {incident_id}")
            before = str(current["state"])
            connection.execute(
                """
                UPDATE runtime_incidents
                SET state = ?, updated_at = ?, resolved_at = ?
                WHERE incident_id = ?
                """,
                (state, now, now if state == "resolved" else None, incident_id),
            )
            connection.execute(
                """
                INSERT INTO runtime_incident_events (
                    event_id, incident_id, event_type, actor, reason,
                    before_state, after_state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    incident_id,
                    state,
                    _bounded_text(actor, 120, required=True),
                    _bounded_text(reason, 500, required=True),
                    before,
                    state,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
        return _incident_from_row(row)

    def list_incidents(
        self,
        *,
        state: str | None = None,
        severity: str | None = None,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        query = "SELECT incident.* FROM runtime_incidents AS incident"
        filters: list[str] = []
        parameters: list[Any] = []
        if state:
            filters.append("state = ?")
            parameters.append(state)
        if severity:
            filters.append("severity = ?")
            parameters.append(severity)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
        query += "WHEN 'P2' THEN 2 ELSE 3 END, last_seen_at DESC LIMIT ?"
        parameters.append(min(max(int(limit), 1), 1000))
        with self._connect() as connection:
            operation_time = (
                "(SELECT created_at FROM operations WHERE operation_id = incident.object_id)"
                if _table_exists(connection, "operations") else "NULL"
            )
            projection = f"""SELECT incident.*,
                CASE WHEN incident.object_type = 'operation' THEN {operation_time}
                  ELSE NULL END AS operation_created_at,
                (SELECT reason FROM runtime_incident_events WHERE incident_id = incident.incident_id
                  AND after_state IN ('resolved', 'suppressed') ORDER BY created_at DESC LIMIT 1)
                  AS disposition_reason,
                (SELECT actor FROM runtime_incident_events WHERE incident_id = incident.incident_id
                  AND after_state IN ('resolved', 'suppressed') ORDER BY created_at DESC LIMIT 1)
                  AS disposition_actor
                FROM runtime_incidents AS incident"""
            query = query.replace("SELECT incident.* FROM runtime_incidents AS incident", projection)
            rows = connection.execute(query, parameters).fetchall()
        return [_incident_from_row(row) for row in rows]

    def incident_events(self, incident_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_incident_events
                WHERE incident_id = ? ORDER BY created_at
                """,
                (incident_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def evaluate_incidents(
        self,
        *,
        task_diagnostics: Mapping[str, Any] | None = None,
        task_stalled_seconds: int = 120,
        operation_stalled_seconds: int = 180,
        delivery_stalled_seconds: int = 300,
    ) -> dict[str, Any]:
        now_dt = self._clock().astimezone(timezone.utc)
        now = now_dt.isoformat()
        observed: set[str] = set()

        def detect(**kwargs: Any) -> None:
            incident = self.upsert_incident(**kwargs)
            if incident["state"] in _ACTIVE_INCIDENT_STATES:
                observed.add(incident["fingerprint"])

        with self._connect() as connection:
            unknown_operations = (
                connection.execute(
                    """
                    SELECT operation.*, span.trace_id
                    FROM operations AS operation
                    LEFT JOIN runtime_spans AS span
                      ON span.operation_id = operation.operation_id
                    WHERE operation.status = 'unknown'
                    GROUP BY operation.operation_id
                    """
                ).fetchall()
                if _table_exists(connection, "operations")
                else []
            )
            for row in unknown_operations:
                detect(
                    rule_id="write_outcome_unknown",
                    severity="P1",
                    symptom_code=row["error_code"] or "RESULT_UNKNOWN",
                    actionability="manual_reconciliation",
                    title="业务写操作结果未知",
                    trace_id=row["trace_id"],
                    user_subject=row["user_subject"],
                    object_type="operation",
                    object_id=row["operation_id"],
                    evidence={"updatedAt": row["updated_at"], "boundary": "B4_COMMIT_ATTEMPTED"},
                    recommended_action="到权威业务系统核对实际结果，禁止自动再次提交。",
                )

            operation_cutoff = (now_dt - timedelta(seconds=operation_stalled_seconds)).isoformat()
            untraced_historical_cutoff = (now_dt - timedelta(hours=24)).isoformat()
            stalled_operations = (
                connection.execute(
                    """
                    SELECT operation.*, span.trace_id
                    FROM operations AS operation
                    LEFT JOIN runtime_spans AS span
                      ON span.operation_id = operation.operation_id
                    WHERE operation.status IN ('pending', 'running')
                      AND operation.updated_at <= ?
                    GROUP BY operation.operation_id
                    """,
                    (operation_cutoff,),
                ).fetchall()
                if _table_exists(connection, "operations")
                else []
            )
            for row in stalled_operations:
                if (
                    row["trace_id"] is None
                    and str(row["updated_at"]) <= untraced_historical_cutoff
                ):
                    continue
                detect(
                    rule_id="operation_stalled",
                    severity="P2",
                    symptom_code="OPERATION_STALLED",
                    actionability="current",
                    title="Operation 长时间没有推进",
                    trace_id=row["trace_id"],
                    user_subject=row["user_subject"],
                    object_type="operation",
                    object_id=row["operation_id"],
                    evidence={"status": row["status"], "updatedAt": row["updated_at"]},
                    recommended_action="查看执行轨迹，确认是否仍在下游调用；不要盲目重放写操作。",
                )

            task_cutoff = (now_dt - timedelta(seconds=task_stalled_seconds)).isoformat()
            if _table_exists(connection, "agent_tasks"):
                stalled_tasks = connection.execute(
                    """
                    SELECT * FROM agent_tasks
                    WHERE status IN ('active', 'running') AND updated_at <= ?
                    """,
                    (task_cutoff,),
                ).fetchall()
                for row in stalled_tasks:
                    trace = connection.execute(
                        """
                        SELECT trace_id FROM runtime_traces
                        WHERE task_id = ? ORDER BY started_at DESC LIMIT 1
                        """,
                        (row["task_id"],),
                    ).fetchone()
                    detect(
                        rule_id="task_stalled",
                        severity="P2",
                        symptom_code="TASK_STALLED",
                        actionability="current",
                        title="任务长时间没有新进展",
                        trace_id=trace["trace_id"] if trace else None,
                        user_subject=row["user_subject"],
                        host_type=row["agent_host"],
                        object_type="task",
                        object_id=row["task_id"],
                        evidence={"status": row["status"], "updatedAt": row["updated_at"]},
                        recommended_action="查看最后阶段；只有尚未进入业务能力时才允许恢复宿主运行。",
                    )

            if _table_exists(connection, "notification_outbox"):
                delivery_cutoff = (now_dt - timedelta(seconds=delivery_stalled_seconds)).isoformat()
                deliveries = connection.execute(
                    """
                    SELECT delivery.*, task.status AS task_status
                    FROM notification_outbox AS delivery
                    LEFT JOIN agent_tasks AS task ON task.task_id = delivery.task_id
                    WHERE delivery.state IN ('pending', 'delivering', 'failed')
                      AND delivery.updated_at <= ?
                    """,
                    (delivery_cutoff,),
                ).fetchall()
                for row in deliveries:
                    historical = (
                        not row["task_status"]
                        or row["task_status"] in _TERMINAL_TASK_STATUSES
                    )
                    if historical:
                        continue
                    detect(
                        rule_id="delivery_stalled",
                        severity="P2",
                        symptom_code="OUTBOX_BACKLOG",
                        actionability="current",
                        title="活动任务的结果投递积压",
                        user_subject=row["user_subject"],
                        object_type="delivery",
                        object_id=row["delivery_id"],
                        evidence={
                            "state": row["state"],
                            "attemptCount": row["attempt_count"],
                            "updatedAt": row["updated_at"],
                        },
                        recommended_action="确认端点状态；只重投已确定的通知，不重做业务操作。",
                    )

        violations = dict((task_diagnostics or {}).get("isolation", {}).get("violations", {}))
        for name, count in violations.items():
            if int(count or 0) <= 0:
                continue
            detect(
                rule_id="identity_isolation_violation",
                severity="P0",
                symptom_code="IDENTITY_ISOLATION_VIOLATION",
                actionability="current",
                title="检测到跨用户运行数据关联",
                object_type="isolation_check",
                object_id=str(name),
                evidence={"count": int(count)},
                recommended_action="立即暂停相关写能力并检查身份、任务和端点关联。",
            )

        resolved = 0
        with self._connect() as connection:
            active = connection.execute(
                """
                SELECT * FROM runtime_incidents
                WHERE state IN ('open', 'acknowledged', 'investigating')
                  AND rule_id IN (
                    'operation_stalled', 'task_stalled', 'delivery_stalled',
                    'identity_isolation_violation'
                  )
                """
            ).fetchall()
        for row in active:
            if row["fingerprint"] in observed:
                continue
            self.transition_incident(
                row["incident_id"],
                state="resolved",
                actor="agentbridge",
                reason="The runtime condition is no longer present.",
            )
            resolved += 1

        return {
            "evaluatedAt": now,
            "observed": len(observed),
            "resolved": resolved,
            "open": sum(
                1
                for item in self.list_incidents(limit=1000)
                if item["state"] in _ACTIVE_INCIDENT_STATES
            ),
        }

    def start_recovery_action(
        self,
        *,
        action_type: str,
        target_type: str,
        target_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
        side_effect_boundary: str,
        before: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        _validate_boundary(side_effect_boundary)
        now = self._now()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM runtime_recovery_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if current is not None:
                return _recovery_from_row(current), True
            action_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO runtime_recovery_actions (
                    action_id, action_type, target_type, target_id, actor,
                    reason, idempotency_key, status, side_effect_boundary,
                    before_json, after_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, '{}', ?)
                """,
                (
                    action_id,
                    _bounded_text(action_type, 120, required=True),
                    _bounded_text(target_type, 80, required=True),
                    _bounded_text(target_id, 256, required=True),
                    _bounded_text(actor, 120, required=True),
                    _bounded_text(reason, 500, required=True),
                    _bounded_text(idempotency_key, 500, required=True),
                    side_effect_boundary,
                    _canonical_json(_safe_metadata(before)),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_recovery_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        return _recovery_from_row(row), False

    def finish_recovery_action(
        self,
        action_id: str,
        *,
        status: str,
        after: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE runtime_recovery_actions
                SET status = ?, after_json = ?, error_code = ?, finished_at = ?
                WHERE action_id = ?
                """,
                (
                    _bounded_text(status, 40, required=True),
                    _canonical_json(_safe_metadata(after)),
                    _bounded_text(error_code, 120),
                    self._now(),
                    action_id,
                ),
            )
            if updated.rowcount != 1:
                raise KeyError(f"runtime recovery action not found: {action_id}")
            row = connection.execute(
                "SELECT * FROM runtime_recovery_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        return _recovery_from_row(row)

    def list_recovery_actions(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_recovery_actions
                ORDER BY created_at DESC LIMIT ?
                """,
                (min(max(int(limit), 1), 1000),),
            ).fetchall()
        return [_recovery_from_row(row) for row in rows]

    def refresh_slo_rollups(self, *, hours: int = 24) -> dict[str, Any]:
        hours = min(max(int(hours), 1), 24 * 31)
        end = self._clock().astimezone(timezone.utc)
        start = end - timedelta(hours=hours)
        start_text = start.isoformat()
        end_text = end.isoformat()
        metrics: list[dict[str, Any]] = []
        with self._connect() as connection:
            trace_counts = connection.execute(
                """
                SELECT COUNT(*) AS samples,
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS successes
                FROM runtime_traces
                WHERE started_at >= ? AND started_at < ?
                  AND status IN ('succeeded', 'failed', 'unknown', 'cancelled')
                """,
                (start_text, end_text),
            ).fetchone()
            isolation = connection.execute(
                """
                SELECT COUNT(*) AS count FROM runtime_incidents
                WHERE rule_id = 'identity_isolation_violation'
                  AND first_seen_at >= ? AND first_seen_at < ?
                """,
                (start_text, end_text),
            ).fetchone()
            unknown = (
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM runtime_incidents AS incident
                    JOIN operations AS operation
                      ON operation.operation_id = incident.object_id
                    WHERE incident.rule_id = 'write_outcome_unknown'
                      AND incident.object_type = 'operation'
                      AND operation.updated_at >= ? AND operation.updated_at < ?
                    """,
                    (start_text, end_text),
                ).fetchone()
                if _table_exists(connection, "operations")
                else {"count": 0}
            )
            verified = connection.execute(
                """
                SELECT COUNT(DISTINCT CASE WHEN side_effect_boundary = 'B5_VERIFIED'
                                           THEN trace_id END) AS verified,
                       COUNT(DISTINCT CASE WHEN side_effect_boundary IN
                         ('B4_COMMIT_ATTEMPTED', 'B5_VERIFIED') THEN trace_id END) AS committed
                FROM runtime_spans WHERE started_at >= ? AND started_at < ?
                """,
                (start_text, end_text),
            ).fetchone()
            latency_rows = connection.execute(
                """
                SELECT stage, duration_ms FROM runtime_spans
                WHERE started_at >= ? AND started_at < ?
                  AND duration_ms IS NOT NULL
                  AND stage IN ('host.first_progress', 'mcp.request', 'capability.invoke')
                ORDER BY stage, duration_ms
                """,
                (start_text, end_text),
            ).fetchall()

        samples = int(trace_counts["samples"] or 0)
        successes = int(trace_counts["successes"] or 0)
        metrics.append(
            _slo_metric(
                "trace_success_rate",
                samples=samples,
                successes=successes,
                value=(successes / samples) if samples else None,
                target=0.99,
                higher_is_better=True,
            )
        )
        committed = int(verified["committed"] or 0)
        verified_count = int(verified["verified"] or 0)
        metrics.append(
            _slo_metric(
                "write_verify_coverage",
                samples=committed,
                successes=verified_count,
                value=(verified_count / committed) if committed else None,
                target=1.0,
                higher_is_better=True,
            )
        )
        for metric_key, count in (
            ("identity_isolation_violations", int(isolation["count"] or 0)),
            ("write_outcome_unknown", int(unknown["count"] or 0)),
        ):
            metrics.append(
                _slo_metric(
                    metric_key,
                    samples=count,
                    successes=None,
                    value=float(count),
                    target=0.0,
                    higher_is_better=False,
                )
            )

        by_stage: dict[str, list[int]] = {}
        for row in latency_rows:
            by_stage.setdefault(str(row["stage"]), []).append(int(row["duration_ms"]))
        latency_targets = {
            "host.first_progress": 15_000.0,
            "mcp.request": 1_000.0,
            "capability.invoke": None,
        }
        for stage, values in by_stage.items():
            p95 = float(_percentile(values, 0.95))
            target = latency_targets.get(stage)
            metrics.append(
                _slo_metric(
                    f"{stage}.p95_ms",
                    samples=len(values),
                    successes=None,
                    value=p95,
                    target=target,
                    higher_is_better=False,
                )
            )

        dimension = _canonical_json({"windowHours": hours})
        created_at = self._now()
        with self._connect() as connection:
            for metric in metrics:
                connection.execute(
                    """
                    INSERT INTO runtime_slo_rollups (
                        rollup_id, metric_key, dimension_key, window_start,
                        window_end, sample_count, success_count, value,
                        target, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(metric_key, dimension_key, window_start, window_end)
                    DO UPDATE SET sample_count = excluded.sample_count,
                                  success_count = excluded.success_count,
                                  value = excluded.value,
                                  target = excluded.target,
                                  status = excluded.status,
                                  created_at = excluded.created_at
                    """,
                    (
                        str(uuid4()),
                        metric["metricKey"],
                        dimension,
                        start_text,
                        end_text,
                        metric["sampleCount"],
                        metric.get("successCount"),
                        metric.get("value"),
                        metric.get("target"),
                        metric["status"],
                        created_at,
                    ),
                )
        return {
            "windowStart": start_text,
            "windowEnd": end_text,
            "windowHours": hours,
            "metrics": metrics,
        }

    def slo_history(self, *, limit: int = 300) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_slo_rollups
                ORDER BY window_end DESC, metric_key LIMIT ?
                """,
                (min(max(int(limit), 1), 1000),),
            ).fetchall()
        return [_rollup_from_row(row) for row in rows]

    def start_observation(
        self,
        *,
        name: str,
        duration_hours: int = 168,
        created_by: str,
        policy: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        normalized_name = _bounded_text(name, 160, required=True)
        normalized_actor = _bounded_text(created_by, 160, required=True)
        duration = min(max(int(duration_hours), 1), 24 * 31)
        now = self._clock().astimezone(timezone.utc)
        now_text = now.isoformat()
        ends_at = (now + timedelta(hours=duration)).isoformat()
        baseline = {
            "summary": self.summary(),
            "slo": self.refresh_slo_rollups(hours=24),
            "incidentCounts": self._incident_counts(),
        }
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM runtime_observations
                WHERE name = ? AND state = 'active'
                ORDER BY started_at DESC LIMIT 1
                """,
                (normalized_name,),
            ).fetchone()
            if existing is not None:
                return _observation_from_row(existing), True
            observation_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO runtime_observations (
                    observation_id, name, state, duration_hours, created_by,
                    policy_json, baseline_json, latest_snapshot_json,
                    snapshot_count, started_at, ends_at
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, '{}', 0, ?, ?)
                """,
                (
                    observation_id,
                    normalized_name,
                    duration,
                    normalized_actor,
                    _canonical_json(_safe_metadata(policy)),
                    _canonical_json(baseline),
                    now_text,
                    ends_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
        return _observation_from_row(row), False

    def capture_observation_snapshots(
        self,
        *,
        slo: Mapping[str, Any] | None = None,
        minimum_interval_minutes: int = 60,
        force: bool = False,
    ) -> dict[str, Any]:
        interval = timedelta(minutes=min(max(int(minimum_interval_minutes), 1), 24 * 60))
        now = self._clock().astimezone(timezone.utc)
        now_text = now.isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_observations
                WHERE state = 'active' ORDER BY started_at
                """
            ).fetchall()
        captured: list[dict[str, Any]] = []
        completed = 0
        effective_slo = dict(slo) if isinstance(slo, Mapping) else None
        for row in rows:
            observation = _observation_from_row(row)
            ends_at = datetime.fromisoformat(observation["ends_at"])
            last_at = (
                datetime.fromisoformat(observation["last_snapshot_at"])
                if observation.get("last_snapshot_at")
                else None
            )
            is_complete = now >= ends_at
            due = force or is_complete or last_at is None or now - last_at >= interval
            if not due:
                continue
            snapshot = {
                "snapshotId": str(uuid4()),
                "observationId": observation["observation_id"],
                "observedAt": now_text,
                "summary": self.summary(),
                "slo": effective_slo or self.refresh_slo_rollups(hours=24),
                "incidentCounts": self._incident_counts(),
                "collection": {
                    "mode": "shadow",
                    "businessCalls": 0,
                    "businessListReads": 0,
                    "businessWrites": 0,
                    "automaticBusinessRecovery": False,
                },
            }
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO runtime_observation_snapshots (
                        snapshot_id, observation_id, observed_at, summary_json,
                        slo_json, incident_counts_json, collection_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot["snapshotId"],
                        snapshot["observationId"],
                        snapshot["observedAt"],
                        _canonical_json(snapshot["summary"]),
                        _canonical_json(snapshot["slo"]),
                        _canonical_json(snapshot["incidentCounts"]),
                        _canonical_json(snapshot["collection"]),
                    ),
                )
                connection.execute(
                    """
                    UPDATE runtime_observations
                    SET latest_snapshot_json = ?, snapshot_count = snapshot_count + 1,
                        last_snapshot_at = ?, state = ?, completed_at = ?
                    WHERE observation_id = ? AND state = 'active'
                    """,
                    (
                        _canonical_json(snapshot),
                        now_text,
                        "completed" if is_complete else "active",
                        now_text if is_complete else None,
                        observation["observation_id"],
                    ),
                )
            captured.append(snapshot)
            completed += int(is_complete)
        return {
            "captured": len(captured),
            "completed": completed,
            "items": captured,
        }

    def list_observations(
        self,
        *,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if state is not None and state not in OBSERVATION_STATES:
            raise ValueError(f"unsupported observation state: {state}")
        clauses: list[str] = []
        parameters: list[Any] = []
        if state is not None:
            clauses.append("state = ?")
            parameters.append(state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(min(max(int(limit), 1), 500))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM runtime_observations {where}
                ORDER BY started_at DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [_observation_from_row(row) for row in rows]

    def observation_detail(self, observation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
            snapshots = connection.execute(
                """
                SELECT * FROM runtime_observation_snapshots
                WHERE observation_id = ? ORDER BY observed_at
                """,
                (observation_id,),
            ).fetchall()
        if row is None:
            raise KeyError(f"runtime observation not found: {observation_id}")
        converted_snapshots = [
            _observation_snapshot_from_row(item) for item in snapshots
        ]
        return {
            "observation": _observation_from_row(row),
            "snapshots": converted_snapshots,
            "dailySummaries": _observation_daily_summaries(converted_snapshots),
        }

    def _incident_counts(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT severity, state, COUNT(*) AS count
                FROM runtime_incidents GROUP BY severity, state
                """
            ).fetchall()
        active = 0
        by_severity: dict[str, int] = {}
        by_state: dict[str, int] = {}
        for row in rows:
            count = int(row["count"] or 0)
            severity = str(row["severity"])
            state = str(row["state"])
            by_severity[severity] = by_severity.get(severity, 0) + count
            by_state[state] = by_state.get(state, 0) + count
            if state in _ACTIVE_INCIDENT_STATES:
                active += count
        return {"active": active, "bySeverity": by_severity, "byState": by_state}

    def summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM runtime_traces
                   WHERE status IN ('active', 'waiting')) AS active_traces,
                  (SELECT COUNT(*) FROM runtime_incidents
                   WHERE state IN ('open', 'acknowledged', 'investigating')) AS open_incidents,
                  (SELECT COUNT(*) FROM runtime_incidents
                   WHERE state IN ('open', 'acknowledged', 'investigating')
                     AND severity IN ('P0', 'P1')) AS critical_incidents,
                  (SELECT COUNT(*) FROM runtime_recovery_actions
                   WHERE status = 'running') AS running_recoveries,
                  (SELECT COUNT(*) FROM runtime_spans) AS span_count,
                  (SELECT MAX(observed_at) FROM runtime_signals) AS last_signal_at,
                  (SELECT COUNT(*) FROM runtime_observations
                   WHERE state = 'active') AS active_observations,
                  (SELECT MAX(last_snapshot_at) FROM runtime_observations)
                    AS last_observation_at
                """
            ).fetchone()
        return {key: row[key] for key in row.keys()}

    def readiness(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        try:
            with self._connect() as connection:
                quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
                schema = connection.execute(
                    """
                    SELECT COUNT(*) FROM sqlite_master
                    WHERE type = 'table' AND name IN (
                      'operations', 'interactions', 'agent_tasks',
                      'runtime_traces', 'runtime_incidents',
                      'runtime_observations', 'runtime_observation_snapshots'
                    )
                    """
                ).fetchone()[0]
            checks.append({"name": "database", "status": "healthy" if quick_check == "ok" else "unavailable"})
            checks.append({"name": "schema", "status": "healthy" if int(schema) == 7 else "unavailable"})
        except Exception as exc:
            checks.append({"name": "database", "status": "unavailable", "errorCode": exc.__class__.__name__})
        ready = all(item["status"] == "healthy" for item in checks)
        return {"status": "ready" if ready else "not_ready", "checks": checks, "checkedAt": self._now()}

    def prune(
        self,
        *,
        raw_days: int = 30,
        rollup_days: int = 180,
    ) -> dict[str, int]:
        raw_cutoff = (self._clock().astimezone(timezone.utc) - timedelta(days=max(raw_days, 1))).isoformat()
        rollup_cutoff = (self._clock().astimezone(timezone.utc) - timedelta(days=max(rollup_days, 1))).isoformat()
        counts: dict[str, int] = {}
        with self._connect() as connection:
            for table, column, cutoff in (
                ("runtime_spans", "started_at", raw_cutoff),
                ("runtime_signals", "observed_at", raw_cutoff),
                ("runtime_slo_rollups", "window_end", rollup_cutoff),
                ("runtime_observation_snapshots", "observed_at", rollup_cutoff),
            ):
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)
                )
                counts[table] = int(cursor.rowcount)
        return counts

    def _now(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()


def classify_runtime_error(error: BaseException | str | None) -> dict[str, str]:
    code = "RUNTIME_FAILURE"
    message = "" if error is None else str(error)
    source_code = str(getattr(error, "code", "") or "").upper()
    class_name = error.__class__.__name__.upper() if isinstance(error, BaseException) else ""
    combined = " ".join((source_code, class_name, message.upper()))
    if "RESULT_UNKNOWN" in combined or "OUTCOMEUNKNOWN" in combined:
        code = "RESULT_UNKNOWN"
    elif "LOGIN_REQUIRED" in combined or "ADAPTERLOGINREQUIRED" in combined:
        code = "SESSION_EXPIRED"
    elif "PERMISSION" in combined or "FORBIDDEN" in combined:
        code = "PERMISSION_DENIED"
    elif "WRITE_PAUSED" in combined:
        code = "WRITE_PAUSED"
    elif "BUSINESS" in combined and "REJECT" in combined:
        code = "BUSINESS_REJECTED"
    elif "TIMEOUT" in combined or "TIMED OUT" in combined:
        code = "DOWNSTREAM_TIMEOUT"
    elif "CONNECTION" in combined or "UNREACHABLE" in combined:
        code = "DOWNSTREAM_UNAVAILABLE"
    elif "SQLITE" in combined or "DATABASE" in combined:
        code = "DATABASE_BUSY"
    elif source_code:
        code = source_code[:120]
    return {"code": code, "message": message[:500]}


def _trace_from_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key in ("stage_count", "recorded_duration_ms"):
        if key in value:
            value[key] = int(value[key] or 0)
    return value


def _span_from_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["metadata"] = _decode_object(value.pop("metadata_json"))
    return value


def _signal_from_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["value"] = _decode_object(value.pop("value_json"))
    return value


def _incident_from_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["evidence"] = _decode_object(value.pop("evidence_json"))
    return value


def _recovery_from_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["before"] = _decode_object(value.pop("before_json"))
    value["after"] = _decode_object(value.pop("after_json"))
    return value


def _rollup_from_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["dimensions"] = _decode_object(value.pop("dimension_key"))
    return value


def _observation_from_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["policy"] = _decode_object(value.pop("policy_json"))
    value["baseline"] = _decode_object(value.pop("baseline_json"))
    value["latest_snapshot"] = _decode_object(value.pop("latest_snapshot_json"))
    value["snapshot_count"] = int(value.get("snapshot_count") or 0)
    return value


def _observation_snapshot_from_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["summary"] = _decode_object(value.pop("summary_json"))
    value["slo"] = _decode_object(value.pop("slo_json"))
    value["incidentCounts"] = _decode_object(value.pop("incident_counts_json"))
    value["collection"] = _decode_object(value.pop("collection_json"))
    return value


def _observation_daily_summaries(
    snapshots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for snapshot in snapshots:
        observed_at = datetime.fromisoformat(snapshot["observed_at"])
        day = observed_at.astimezone(timezone.utc).date().isoformat()
        grouped.setdefault(day, []).append(snapshot)
    summaries: list[dict[str, Any]] = []
    for day in sorted(grouped):
        items = grouped[day]
        incident_counts = [
            int(item.get("incidentCounts", {}).get("active") or 0)
            for item in items
        ]
        breached_metrics = sorted(
            {
                str(metric.get("metricKey") or "")
                for item in items
                for metric in item.get("slo", {}).get("metrics", [])
                if metric.get("status") == "breached" and metric.get("metricKey")
            }
        )
        summaries.append(
            {
                "date": day,
                "snapshotCount": len(items),
                "firstObservedAt": items[0]["observed_at"],
                "lastObservedAt": items[-1]["observed_at"],
                "maxActiveIncidents": max(incident_counts, default=0),
                "latestActiveIncidents": incident_counts[-1] if incident_counts else 0,
                "breachedMetrics": breached_metrics,
                "businessCalls": 0,
                "businessListReads": 0,
                "businessWrites": 0,
            }
        )
    return summaries


def _slo_metric(
    metric_key: str,
    *,
    samples: int,
    successes: int | None,
    value: float | None,
    target: float | None,
    higher_is_better: bool,
) -> dict[str, Any]:
    if value is None or target is None or samples < 1:
        status = "insufficient_data"
    elif (value >= target) if higher_is_better else (value <= target):
        status = "meeting"
    else:
        status = "breached"
    return {
        "metricKey": metric_key,
        "sampleCount": samples,
        "successCount": successes,
        "value": value,
        "target": target,
        "status": status,
    }


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _validate_boundary(value: str) -> None:
    if value not in SIDE_EFFECT_BOUNDARIES:
        raise ValueError(f"unsupported side effect boundary: {value}")


def _max_boundary(left: str, right: str) -> str:
    _validate_boundary(left)
    _validate_boundary(right)
    return SIDE_EFFECT_BOUNDARIES[max(SIDE_EFFECT_BOUNDARIES.index(left), SIDE_EFFECT_BOUNDARIES.index(right))]


def _bounded_text(value: Any, maximum: int, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError("required text is missing")
        return None
    normalized = str(value).strip()
    if not normalized:
        if required:
            raise ValueError("required text is empty")
        return None
    return normalized[:maximum]


def _safe_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:50]:
        name = str(key)[:80]
        lowered = name.lower().replace("_", "")
        if any(
            marker in lowered
            for marker in (
                "password",
                "secret",
                "bearer",
                "cookie",
                "authorization",
                "cardurl",
                "downloadurl",
                "fieldvalue",
                "formvalue",
            )
        ):
            result[name] = "[redacted]"
            continue
        if item is None or isinstance(item, (bool, int, float)):
            result[name] = item
        elif isinstance(item, str):
            result[name] = item[:500]
        elif isinstance(item, (list, tuple, set)):
            result[name] = [
                candidate if isinstance(candidate, (bool, int, float)) or candidate is None else str(candidate)[:160]
                for candidate in list(item)[:30]
            ]
        elif isinstance(item, Mapping):
            result[name] = _safe_metadata(item)
        else:
            result[name] = str(item)[:160]
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {}


def _duration_ms(started_at: str, finished_at: str) -> int:
    start = datetime.fromisoformat(started_at)
    finish = datetime.fromisoformat(finished_at)
    return max(round((finish - start).total_seconds() * 1000), 0)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None
