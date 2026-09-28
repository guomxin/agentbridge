from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping
from uuid import uuid4


HOST_CONTRACT_SCHEMA = "agentbridge.host.v1"
HOST_CONTEXT_META_KEY = "io.agentbridge/host-context"
LEGACY_HOST_CONTEXT_META_KEY = "io.agentbridge/host"
HOST_PROFILE_META_KEY = "io.agentbridge/host-profile"
TASK_CONTEXT_META_KEY = "io.agentbridge/task"

HOST_LEVELS = ("L1", "L2", "L3")
HOST_CAPABILITIES = (
    "mcpApps",
    "privateResultMeta",
    "interactionPollResume",
    "taskTimeline",
    "proactiveDelivery",
    "artifactDelivery",
    "restartRecovery",
    "coordinatorLease",
    "batchTaskTimeline",
    "runtimeSignals",
    "boundedTransportRecovery",
)
LEVEL_REQUIREMENTS = {
    "L1": frozenset(),
    "L2": frozenset({"privateResultMeta", "interactionPollResume"}),
    "L3": frozenset(
        {
            "privateResultMeta",
            "interactionPollResume",
            "taskTimeline",
            "proactiveDelivery",
            "artifactDelivery",
            "restartRecovery",
            "coordinatorLease",
            "batchTaskTimeline",
            "runtimeSignals",
            "boundedTransportRecovery",
        }
    ),
}


class HostContractError(ValueError):
    code = "HOST_CONTRACT_INVALID"


class HostRegistrationRequired(PermissionError):
    code = "HOST_REGISTRATION_REQUIRED"


class HostLeaseConflict(PermissionError):
    code = "HOST_COORDINATOR_LEASE_CONFLICT"


def normalize_host_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HostContractError("host capability profile must be an object")
    allowed = {
        "schema",
        "hostInstanceId",
        "implementation",
        "levels",
        "capabilities",
        "endpointTypes",
    }
    unknown = set(value) - allowed
    if unknown:
        raise HostContractError(
            f"host capability profile contains unsupported fields: {sorted(unknown)}"
        )
    if value.get("schema") != HOST_CONTRACT_SCHEMA:
        raise HostContractError("host capability profile schema is invalid")
    host_instance_id = _required_text(
        value.get("hostInstanceId"), "hostInstanceId", 160
    )
    implementation = value.get("implementation")
    if not isinstance(implementation, Mapping):
        raise HostContractError("host implementation is required")
    if set(implementation) - {"name", "version"}:
        raise HostContractError("host implementation contains unsupported fields")
    implementation_name = _required_text(
        implementation.get("name"), "implementation.name", 80
    )
    implementation_version = _required_text(
        implementation.get("version"), "implementation.version", 80
    )

    raw_levels = value.get("levels")
    if not isinstance(raw_levels, list) or not raw_levels:
        raise HostContractError("host levels are required")
    levels: list[str] = []
    for item in raw_levels:
        level = str(item or "").strip().upper()
        if level not in HOST_LEVELS:
            raise HostContractError(f"unsupported host level: {level or item}")
        if level not in levels:
            levels.append(level)
    if "L1" not in levels:
        raise HostContractError("every host must declare L1")
    highest = max(HOST_LEVELS.index(level) for level in levels)
    expected = list(HOST_LEVELS[: highest + 1])
    if sorted(levels, key=HOST_LEVELS.index) != expected:
        raise HostContractError("host levels must be contiguous from L1")

    raw_capabilities = value.get("capabilities")
    if not isinstance(raw_capabilities, Mapping):
        raise HostContractError("host capabilities are required")
    unknown_capabilities = set(raw_capabilities) - set(HOST_CAPABILITIES)
    if unknown_capabilities:
        raise HostContractError(
            "host profile contains unsupported capabilities: "
            f"{sorted(unknown_capabilities)}"
        )
    capabilities = {
        name: _required_bool(raw_capabilities.get(name), f"capabilities.{name}")
        for name in HOST_CAPABILITIES
    }
    missing = sorted(
        name
        for name in LEVEL_REQUIREMENTS[expected[-1]]
        if capabilities.get(name) is not True
    )
    if missing:
        raise HostContractError(
            f"declared {expected[-1]} host is missing capabilities: {missing}"
        )

    raw_endpoint_types = value.get("endpointTypes")
    if not isinstance(raw_endpoint_types, list) or not raw_endpoint_types:
        raise HostContractError("host endpointTypes are required")
    endpoint_types: list[str] = []
    for item in raw_endpoint_types:
        endpoint_type = _required_text(item, "endpointType", 80)
        if endpoint_type not in endpoint_types:
            endpoint_types.append(endpoint_type)

    return {
        "schema": HOST_CONTRACT_SCHEMA,
        "hostInstanceId": host_instance_id,
        "implementation": {
            "name": implementation_name,
            "version": implementation_version,
        },
        "levels": expected,
        "capabilities": capabilities,
        "endpointTypes": endpoint_types,
    }


def normalize_host_runtime_context(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise HostContractError("host runtime context must be an object")
    if value.get("version") != "1":
        raise HostContractError("host runtime context version is invalid")
    return {
        "version": "1",
        "agentHost": _required_text(value.get("agentHost"), "agentHost", 80),
        "hostInstanceId": _required_text(
            value.get("hostInstanceId"), "hostInstanceId", 160
        ),
        "hostVersion": _required_text(
            value.get("hostVersion"), "hostVersion", 80
        ),
    }


def normalize_host_call_context(value: Mapping[str, Any] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise HostContractError("host call context must be an object")
    allowed = {
        "taskId",
        "hostRunId",
        "toolCallId",
        "endpointId",
        "conversationRef",
        "clientMessageRef",
        "coordinatorLeaseVersion",
    }
    unknown = set(value) - allowed
    if unknown:
        raise HostContractError(
            f"host call context contains unsupported fields: {sorted(unknown)}"
        )
    limits = {
        "taskId": 128,
        "hostRunId": 256,
        "toolCallId": 256,
        "endpointId": 128,
        "conversationRef": 1024,
        "clientMessageRef": 256,
        "coordinatorLeaseVersion": 32,
    }
    normalized: dict[str, str] = {}
    for name, maximum in limits.items():
        if value.get(name) is not None:
            normalized[name] = _required_text(value.get(name), name, maximum)
    return normalized


def highest_accepted_level(profile: Mapping[str, Any], approved_level: str) -> str:
    normalized = normalize_host_profile(profile)
    approved = str(approved_level or "L1").upper()
    if approved not in HOST_LEVELS:
        approved = "L1"
    declared_index = max(HOST_LEVELS.index(level) for level in normalized["levels"])
    approved_index = HOST_LEVELS.index(approved)
    accepted_index = min(declared_index, approved_index)
    while accepted_index > 0:
        level = HOST_LEVELS[accepted_index]
        if all(
            normalized["capabilities"].get(name) is True
            for name in LEVEL_REQUIREMENTS[level]
        ):
            break
        accepted_index -= 1
    return HOST_LEVELS[accepted_index]


class HostContractStore:
    """Persistent host compatibility, registration, runtime, and lease ledger."""

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
                CREATE TABLE IF NOT EXISTS agent_host_compatibility (
                    implementation_name TEXT NOT NULL,
                    implementation_version TEXT NOT NULL,
                    accepted_level TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    PRIMARY KEY (implementation_name, implementation_version)
                );

                CREATE TABLE IF NOT EXISTS agent_host_registrations (
                    registration_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    host_instance_id TEXT NOT NULL,
                    agent_host TEXT NOT NULL,
                    host_version TEXT NOT NULL,
                    declared_profile_json TEXT NOT NULL,
                    accepted_level TEXT NOT NULL,
                    compatibility_status TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE (user_subject, host_instance_id)
                );

                CREATE INDEX IF NOT EXISTS agent_host_registrations_subject
                ON agent_host_registrations (user_subject, accepted_level, last_seen_at);

                CREATE TABLE IF NOT EXISTS agent_host_runtime_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    host_instance_id TEXT NOT NULL,
                    host_type TEXT NOT NULL,
                    host_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS agent_host_runtime_snapshots_host
                ON agent_host_runtime_snapshots (
                    user_subject, host_instance_id, observed_at
                );

                CREATE TABLE IF NOT EXISTS agent_task_coordinator_leases (
                    task_id TEXT PRIMARY KEY,
                    user_subject TEXT NOT NULL,
                    host_instance_id TEXT NOT NULL,
                    agent_host TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    renewed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    released_at TEXT
                );

                CREATE INDEX IF NOT EXISTS agent_task_coordinator_leases_owner
                ON agent_task_coordinator_leases (
                    user_subject, host_instance_id, state, expires_at
                );
                """
            )
            self._bootstrap_compatibility(connection)

    @staticmethod
    def _bootstrap_compatibility(connection: sqlite3.Connection) -> None:
        now = _utc_now()
        builtins = (
            ("openclaw", "0.4.71", "L3", "shared-contract-h01-h29-task-plan-v1"),
            (
                "openclaw",
                "0.4.72",
                "L3",
                "shared-contract-h01-h29-task-plan-terminal-v1",
            ),
            (
                "openclaw",
                "0.4.73",
                "L3",
                "shared-contract-h01-h29-task-plan-v2",
            ),
            (
                "openclaw",
                "0.4.74",
                "L3",
                "shared-contract-h01-h29-task-plan-repair-v1",
            ),
            (
                "openclaw",
                "0.4.75",
                "L3",
                "shared-contract-h01-h29-task-plan-guidance-v1",
            ),
            (
                "openclaw",
                "0.4.76",
                "L3",
                "shared-contract-h01-h29-task-plan-partial-preview-v1",
            ),
            (
                "openclaw",
                "0.4.77",
                "L3",
                "shared-contract-h01-h29-task-plan-query-contract-v1",
            ),
            (
                "openclaw",
                "0.4.78",
                "L3",
                "shared-contract-h01-h29-task-plan-workspace-repair-v1",
            ),
            (
                "openclaw",
                "0.4.79",
                "L3",
                "shared-contract-h01-h29-persistent-login-read-resume-v1",
            ),
            (
                "openclaw",
                "0.4.80",
                "L3",
                "shared-contract-h01-h29-native-card-origin-recovery-v1",
            ),
            (
                "openclaw",
                "0.4.81",
                "L3",
                "shared-contract-h01-h29-oa-meeting-room-read-v1",
            ),
            (
                "openclaw",
                "0.4.82",
                "L3",
                "shared-contract-h01-h29-oa-meeting-room-application-v1",
            ),
            (
                "openclaw",
                "0.4.83",
                "L3",
                "shared-contract-h01-h29-oa-meeting-room-owner-cancel-verification-v1",
            ),
            (
                "openclaw",
                "0.4.84",
                "L3",
                "shared-contract-h01-h29-oa-meeting-intent-routing-guard-v1",
            ),
            (
                "openclaw",
                "0.4.85",
                "L3",
                "shared-contract-h01-h29-governed-write-task-boundary-v1",
            ),
            (
                "openclaw",
                "0.4.86",
                "L3",
                "shared-contract-h01-h29-generic-pending-batch-v1",
            ),
            (
                "openclaw",
                "0.4.87",
                "L3",
                "shared-contract-h01-h29-composed-task-preflight-cancel-v1",
            ),
            ("reference-host", "0.1.0", "L3", "shared-contract-h01-h25"),
            (
                "openclaw", "0.4.88", "L3",
                "shared-contract-h01-h29-current-turn-continuation-v1",
            ),
            (
                "openclaw", "0.4.89", "L3",
                "shared-contract-h01-h29-shared-planning-policy-v1",
            ),
            (
                "openclaw", "0.4.90", "L3",
                "shared-contract-h01-h29-cancel-target-verification-v1",
            ),
            (
                "openclaw", "0.4.91", "L3",
                "shared-contract-h01-h29-work-handover-approval-v1",
            ),
            (
                "openclaw", "0.4.92", "L3",
                "shared-contract-h01-h29-independent-database-v1",
            ),
            (
                "openclaw", "0.4.93", "L3",
                "shared-contract-h01-h29-database-content-v1",
            ),
            (
                "openclaw", "0.4.94", "L3",
                "shared-contract-h01-h29-multisource-database-v1",
            ),
            (
                "openclaw", "0.4.95", "L3",
                "shared-contract-h01-h29-atomic-read-task-isolation-v1",
            ),
            (
                "openclaw", "0.4.96", "L3",
                "shared-contract-h01-h29-native-login-read-continuation-v1",
            ),
            (
                "openclaw", "0.4.100", "L3",
                "shared-contract-h01-h29-modular-read-continuation-v1",
            ),
            (
                "openclaw", "0.4.101", "L3",
                "shared-contract-h01-h29-business-skills-v1",
            ),
            (
                "openclaw", "0.4.102", "L3",
                "shared-contract-h01-h29-skill-workspace-turn-v1",
            ),
            (
                "openclaw", "0.4.103", "L3",
                "shared-contract-h01-h29-skill-bundle-selection-v1",
            ),
            (
                "openclaw", "0.4.104", "L3",
                "shared-contract-h01-h29-skill-selection-explanation-v1",
            ),
        )
        for name, version, level, evidence in builtins:
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_host_compatibility (
                    implementation_name, implementation_version, accepted_level,
                    evidence_digest, approved_by, approved_at
                ) VALUES (?, ?, ?, ?, 'project_release', ?)
                """,
                (name, version, level, evidence, now),
            )

    def negotiate(
        self,
        *,
        user_subject: str,
        token_id: str,
        profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized = normalize_host_profile(profile)
        implementation = normalized["implementation"]
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            compatibility = connection.execute(
                """
                SELECT * FROM agent_host_compatibility
                WHERE implementation_name = ? AND implementation_version = ?
                """,
                (implementation["name"], implementation["version"]),
            ).fetchone()
            approved_level = compatibility["accepted_level"] if compatibility else "L1"
            accepted_level = highest_accepted_level(normalized, approved_level)
            status = "approved" if compatibility else "unrecognized"
            existing = connection.execute(
                """
                SELECT * FROM agent_host_registrations
                WHERE user_subject = ? AND host_instance_id = ?
                """,
                (user_subject, normalized["hostInstanceId"]),
            ).fetchone()
            registration_id = (
                str(existing["registration_id"]) if existing else str(uuid4())
            )
            connection.execute(
                """
                INSERT INTO agent_host_registrations (
                    registration_id, user_subject, token_id, host_instance_id,
                    agent_host, host_version, declared_profile_json,
                    accepted_level, compatibility_status, registered_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_subject, host_instance_id) DO UPDATE SET
                    token_id = excluded.token_id,
                    agent_host = excluded.agent_host,
                    host_version = excluded.host_version,
                    declared_profile_json = excluded.declared_profile_json,
                    accepted_level = excluded.accepted_level,
                    compatibility_status = excluded.compatibility_status,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    registration_id,
                    user_subject,
                    token_id,
                    normalized["hostInstanceId"],
                    implementation["name"],
                    implementation["version"],
                    _canonical_json(normalized),
                    accepted_level,
                    status,
                    now,
                    now,
                ),
            )
        missing = sorted(
            name
            for name in LEVEL_REQUIREMENTS[approved_level]
            if normalized["capabilities"].get(name) is not True
        )
        return {
            "schemaVersion": "agentbridge.host-negotiation.v1",
            "status": "succeeded",
            "registrationId": registration_id,
            "hostInstanceId": normalized["hostInstanceId"],
            "implementation": implementation,
            "acceptedLevel": accepted_level,
            "compatibilityStatus": status,
            "missingCapabilities": missing,
            "mustReregisterOnVersionChange": True,
        }

    def require_registration(
        self,
        *,
        user_subject: str,
        agent_host: str,
        host_instance_id: str,
        host_version: str,
        minimum_level: str = "L1",
    ) -> dict[str, Any]:
        minimum_level = str(minimum_level or "L1").upper()
        if minimum_level not in HOST_LEVELS:
            raise ValueError("minimum host level is invalid")
        now = _utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_host_registrations
                WHERE user_subject = ? AND host_instance_id = ?
                """,
                (user_subject, host_instance_id),
            ).fetchone()
            if row is None:
                raise HostRegistrationRequired("Agent host is not registered")
            if row["agent_host"] != agent_host or row["host_version"] != host_version:
                raise HostRegistrationRequired(
                    "Agent host registration does not match the current runtime"
                )
            if HOST_LEVELS.index(row["accepted_level"]) < HOST_LEVELS.index(minimum_level):
                raise HostRegistrationRequired(
                    f"Agent host requires compatibility level {minimum_level}"
                )
            connection.execute(
                """
                UPDATE agent_host_registrations
                SET last_seen_at = ? WHERE registration_id = ?
                """,
                (now, row["registration_id"]),
            )
        return _registration_from_row(row)

    def record_runtime_snapshot(
        self,
        *,
        user_subject: str,
        registration: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(snapshot, Mapping):
            raise HostContractError("host runtime snapshot must be an object")
        status = _required_text(snapshot.get("status"), "status", 40).lower()
        if status not in {"healthy", "degraded", "failed", "stopping"}:
            raise HostContractError("host runtime snapshot status is invalid")
        observed_at = _iso_time(snapshot.get("observedAt"), "observedAt")
        safe_snapshot = {
            "schema": "agentbridge.host-runtime-snapshot.v1",
            "status": status,
            "observedAt": observed_at,
            "uptimeSeconds": _optional_nonnegative_number(
                snapshot.get("uptimeSeconds"), "uptimeSeconds"
            ),
            "activeTaskCount": _optional_nonnegative_int(
                snapshot.get("activeTaskCount"), "activeTaskCount"
            ),
            "waitingInteractionCount": _optional_nonnegative_int(
                snapshot.get("waitingInteractionCount"), "waitingInteractionCount"
            ),
            "transportErrorCount": _optional_nonnegative_int(
                snapshot.get("transportErrorCount"), "transportErrorCount"
            ),
            "lastErrorCode": _optional_text(snapshot.get("lastErrorCode"), 120),
        }
        safe_snapshot = {
            key: value for key, value in safe_snapshot.items() if value is not None
        }
        snapshot_id = str(uuid4())
        recorded_at = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_host_runtime_snapshots (
                    snapshot_id, user_subject, host_instance_id, host_type,
                    host_version, status, snapshot_json, observed_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    user_subject,
                    registration["hostInstanceId"],
                    registration["agentHost"],
                    registration["hostVersion"],
                    status,
                    _canonical_json(safe_snapshot),
                    observed_at,
                    recorded_at,
                ),
            )
        return {
            "schemaVersion": "agentbridge.host-runtime-snapshot.v1",
            "status": "succeeded",
            "snapshotId": snapshot_id,
            "hostInstanceId": registration["hostInstanceId"],
            "recordedAt": recorded_at,
        }

    def acquire_coordinator_lease(
        self,
        *,
        task_id: str,
        user_subject: str,
        host_instance_id: str,
        agent_host: str,
        lease_seconds: int = 60,
        takeover: bool = False,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        lease_seconds = min(max(int(lease_seconds), 15), 600)
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_task_coordinator_leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                version = 1
                lease_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO agent_task_coordinator_leases (
                        task_id, user_subject, host_instance_id, agent_host,
                        lease_id, version, state, acquired_at, renewed_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        task_id,
                        user_subject,
                        host_instance_id,
                        agent_host,
                        lease_id,
                        version,
                        now_text,
                        now_text,
                        expires_at,
                    ),
                )
            else:
                if row["user_subject"] != user_subject:
                    raise HostLeaseConflict("Coordinator lease belongs to another user")
                row_expired = datetime.fromisoformat(row["expires_at"]) <= now
                same_owner = row["host_instance_id"] == host_instance_id
                if expected_version is not None and int(row["version"]) != int(
                    expected_version
                ):
                    raise HostLeaseConflict("Coordinator lease version changed")
                if not same_owner and not row_expired:
                    raise HostLeaseConflict("Coordinator lease is held by another host")
                if not same_owner and not takeover:
                    raise HostLeaseConflict(
                        "Expired coordinator lease requires explicit reconciled takeover"
                    )
                version = int(row["version"]) + (0 if same_owner else 1)
                lease_id = str(row["lease_id"]) if same_owner else str(uuid4())
                connection.execute(
                    """
                    UPDATE agent_task_coordinator_leases
                    SET host_instance_id = ?, agent_host = ?, lease_id = ?,
                        version = ?, state = 'active', renewed_at = ?, expires_at = ?,
                        released_at = NULL
                    WHERE task_id = ?
                    """,
                    (
                        host_instance_id,
                        agent_host,
                        lease_id,
                        version,
                        now_text,
                        expires_at,
                        task_id,
                    ),
                )
            selected = connection.execute(
                "SELECT * FROM agent_task_coordinator_leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return _lease_from_row(selected)

    def assert_coordinator_lease(
        self,
        *,
        task_id: str,
        user_subject: str,
        host_instance_id: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_task_coordinator_leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None or row["user_subject"] != user_subject:
            raise HostLeaseConflict("Coordinator lease is not available")
        if row["state"] != "active" or datetime.fromisoformat(row["expires_at"]) <= now:
            raise HostLeaseConflict("Coordinator lease expired")
        if row["host_instance_id"] != host_instance_id:
            raise HostLeaseConflict("Coordinator lease is held by another host")
        if expected_version is not None and int(row["version"]) != int(expected_version):
            raise HostLeaseConflict("Coordinator lease version changed")
        return _lease_from_row(row)

    def release_coordinator_lease(
        self,
        *,
        task_id: str,
        user_subject: str,
        host_instance_id: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        lease = self.assert_coordinator_lease(
            task_id=task_id,
            user_subject=user_subject,
            host_instance_id=host_instance_id,
            expected_version=expected_version,
        )
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_task_coordinator_leases
                SET state = 'released', released_at = ?, renewed_at = ?
                WHERE task_id = ? AND version = ?
                """,
                (now, now, task_id, lease["version"]),
            )
            row = connection.execute(
                "SELECT * FROM agent_task_coordinator_leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return _lease_from_row(row)

    def get_coordinator_lease(
        self, *, task_id: str, user_subject: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_task_coordinator_leases
                WHERE task_id = ? AND user_subject = ?
                """,
                (task_id, user_subject),
            ).fetchone()
        return _lease_from_row(row) if row is not None else None

    def runtime_overview(
        self,
        *,
        registration_limit: int = 100,
        lease_limit: int = 100,
    ) -> dict[str, Any]:
        """Return bounded, secret-free host governance data for administrators."""
        registration_limit = min(max(int(registration_limit), 1), 500)
        lease_limit = min(max(int(lease_limit), 1), 500)
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            compatibility_rows = connection.execute(
                """
                SELECT * FROM agent_host_compatibility
                ORDER BY implementation_name, implementation_version
                """
            ).fetchall()
            registration_rows = connection.execute(
                """
                SELECT r.*, s.status AS runtime_status,
                       s.snapshot_json AS runtime_snapshot_json,
                       s.observed_at AS runtime_observed_at,
                       s.recorded_at AS runtime_recorded_at
                FROM agent_host_registrations r
                LEFT JOIN agent_host_runtime_snapshots s
                  ON s.snapshot_id = (
                    SELECT latest.snapshot_id
                    FROM agent_host_runtime_snapshots latest
                    WHERE latest.user_subject = r.user_subject
                      AND latest.host_instance_id = r.host_instance_id
                    ORDER BY latest.observed_at DESC, latest.recorded_at DESC
                    LIMIT 1
                  )
                ORDER BY r.last_seen_at DESC
                LIMIT ?
                """,
                (registration_limit,),
            ).fetchall()
            lease_rows = connection.execute(
                """
                SELECT * FROM agent_task_coordinator_leases
                ORDER BY renewed_at DESC LIMIT ?
                """,
                (lease_limit,),
            ).fetchall()

        registrations = []
        for row in registration_rows:
            registration = _registration_from_row(row)
            runtime = _runtime_snapshot_from_row(row)
            registration["runtime"] = runtime
            registrations.append(registration)
        leases = []
        for row in lease_rows:
            lease = _lease_from_row(row)
            lease["effectiveState"] = (
                "expired"
                if lease["state"] == "active"
                and datetime.fromisoformat(lease["expiresAt"]) <= now
                else lease["state"]
            )
            leases.append(lease)
        compatibility = [
            {
                "agentHost": row["implementation_name"],
                "hostVersion": row["implementation_version"],
                "acceptedLevel": row["accepted_level"],
                "evidenceDigest": row["evidence_digest"],
                "approvedBy": row["approved_by"],
                "approvedAt": row["approved_at"],
            }
            for row in compatibility_rows
        ]
        return {
            "schemaVersion": "agentbridge.host-runtime-overview.v1",
            "generatedAt": now.isoformat(),
            "summary": {
                "compatibilityVersions": len(compatibility),
                "registrations": len(registrations),
                "hostInstances": len(
                    {item["hostInstanceId"] for item in registrations}
                ),
                "healthyRegistrations": sum(
                    (item.get("runtime") or {}).get("status") == "healthy"
                    for item in registrations
                ),
                "activeLeases": sum(
                    item["effectiveState"] == "active" for item in leases
                ),
                "expiredLeases": sum(
                    item["effectiveState"] == "expired" for item in leases
                ),
            },
            "compatibility": compatibility,
            "registrations": registrations,
            "leases": leases,
        }


def _registration_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "registrationId": row["registration_id"],
        "userSubject": row["user_subject"],
        "hostInstanceId": row["host_instance_id"],
        "agentHost": row["agent_host"],
        "hostVersion": row["host_version"],
        "acceptedLevel": row["accepted_level"],
        "compatibilityStatus": row["compatibility_status"],
        "lastSeenAt": row["last_seen_at"],
    }


def _lease_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema": "agentbridge.coordinator-lease.v1",
        "taskId": row["task_id"],
        "hostInstanceId": row["host_instance_id"],
        "agentHost": row["agent_host"],
        "leaseId": row["lease_id"],
        "version": int(row["version"]),
        "state": row["state"],
        "acquiredAt": row["acquired_at"],
        "renewedAt": row["renewed_at"],
        "expiresAt": row["expires_at"],
        "releasedAt": row["released_at"],
    }


def _runtime_snapshot_from_row(row: sqlite3.Row) -> dict[str, Any] | None:
    if not row["runtime_status"]:
        return None
    try:
        snapshot = json.loads(row["runtime_snapshot_json"] or "{}")
    except json.JSONDecodeError:
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    return {
        "status": row["runtime_status"],
        "observedAt": row["runtime_observed_at"],
        "recordedAt": row["runtime_recorded_at"],
        "uptimeSeconds": snapshot.get("uptimeSeconds"),
        "activeTaskCount": snapshot.get("activeTaskCount"),
        "waitingInteractionCount": snapshot.get("waitingInteractionCount"),
        "transportErrorCount": snapshot.get("transportErrorCount"),
        "lastErrorCode": snapshot.get("lastErrorCode"),
    }


def _required_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, (str, int)):
        raise HostContractError(f"{name} is required")
    normalized = str(value).strip()
    if not normalized or len(normalized) > maximum:
        raise HostContractError(f"{name} is invalid")
    return normalized


def _optional_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, "optional text", maximum)


def _required_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise HostContractError(f"{name} must be boolean")
    return value


def _optional_nonnegative_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HostContractError(f"{name} must be a non-negative integer")
    return value


def _optional_nonnegative_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise HostContractError(f"{name} must be a non-negative number")
    return float(value)


def _iso_time(value: Any, name: str) -> str:
    text = _required_text(value, name, 80)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HostContractError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise HostContractError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
