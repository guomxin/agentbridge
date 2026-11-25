from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.host_contract import (
    HOST_CONTEXT_META_KEY,
    HOST_PROFILE_META_KEY,
    HostContractError,
    HostContractStore,
    HostLeaseConflict,
    HostRegistrationRequired,
    normalize_host_call_context,
    normalize_host_profile,
    normalize_host_runtime_context,
)


ROOT = Path(__file__).parents[1]
SCHEMA_DIR = ROOT / "schemas" / "agent-host" / "v1"


class HostContractSchemaTests(unittest.TestCase):
    def test_business_view_schema_covers_exact_registered_profile_set(self):
        from agentbridge.core.business_draft_profiles import PROFILES
        schema = json.loads((SCHEMA_DIR / "interaction-presentation.schema.json").read_text())
        hints = schema["properties"]["display"]["properties"]["businessView"]["properties"]
        self.assertEqual(set(hints["profileId"]["enum"]), {profile.profile_id for profile in PROFILES})
        self.assertEqual(set(hints["viewKind"]["enum"]), {profile.view_kind for profile in PROFILES})
        for profile in PROFILES:
            self.assertEqual(profile.presentation()["profileVersion"], hints["profileVersion"]["const"])

    def test_every_contract_schema_is_valid_json(self) -> None:
        names = {
            "host-capability-profile.schema.json",
            "host-runtime-context.schema.json",
            "host-call-context.schema.json",
            "interaction-presentation.schema.json",
            "coordinator-lease.schema.json",
            "task-correlation.schema.json",
            "task-batch.schema.json",
            "timeline-event.schema.json",
            "artifact-delivery.schema.json",
            "host-runtime-snapshot.schema.json",
            "transport-recovery.schema.json",
            "error-envelope.schema.json",
            "test-vectors.json",
        }
        self.assertEqual(names, {path.name for path in SCHEMA_DIR.glob("*.json")})
        for path in SCHEMA_DIR.glob("*.json"):
            with self.subTest(path=path.name):
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(value, dict)
        vectors = json.loads(
            (SCHEMA_DIR / "test-vectors.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [f"H{number:02d}" for number in range(1, 30)],
            [item["id"] for item in vectors["conformanceCases"]],
        )

    def test_python_validator_accepts_shared_profile_vectors(self) -> None:
        vectors = json.loads(
            (SCHEMA_DIR / "test-vectors.json").read_text(encoding="utf-8")
        )
        for vector in vectors["profiles"]:
            with self.subTest(vector=vector["name"]):
                if vector["valid"]:
                    normalized = normalize_host_profile(vector["value"])
                    self.assertEqual("agentbridge.host.v1", normalized["schema"])
                else:
                    with self.assertRaises(HostContractError):
                        normalize_host_profile(vector["value"])

    def test_runtime_and_call_contexts_are_bounded(self) -> None:
        context = normalize_host_runtime_context(
            {
                "version": "1",
                "agentHost": "reference-host",
                "hostInstanceId": "reference-host-test",
                "hostVersion": "0.1.0",
            }
        )
        self.assertEqual("reference-host-test", context["hostInstanceId"])
        call = normalize_host_call_context(
            {
                "taskId": "task-1",
                "hostRunId": "run-1",
                "coordinatorLeaseVersion": 2,
            }
        )
        self.assertEqual("2", call["coordinatorLeaseVersion"])
        self.assertEqual("io.agentbridge/host-context", HOST_CONTEXT_META_KEY)
        self.assertEqual("io.agentbridge/host-profile", HOST_PROFILE_META_KEY)


class HostContractStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.store = HostContractStore(Path(self.temp.name) / "agentbridge.db")
        vectors = json.loads(
            (SCHEMA_DIR / "test-vectors.json").read_text(encoding="utf-8")
        )
        self.profile = deepcopy(vectors["profiles"][0]["value"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_version_registration_negotiates_l3(self) -> None:
        result = self.store.negotiate(
            user_subject="user-a",
            token_id="token-a",
            profile=self.profile,
        )
        self.assertEqual("L3", result["acceptedLevel"])
        self.assertEqual("approved", result["compatibilityStatus"])
        registration = self.store.require_registration(
            user_subject="user-a",
            agent_host="reference-host",
            host_instance_id="reference-host-test-01",
            host_version="0.1.0",
            minimum_level="L3",
        )
        self.assertEqual("L3", registration["acceptedLevel"])

    def test_openclaw_package_version_is_an_approved_l3_baseline(self) -> None:
        package = json.loads(
            (
                ROOT
                / "integrations"
                / "openclaw-agentbridge"
                / "package.json"
            ).read_text(encoding="utf-8")
        )
        for filename, constant in (
            ("host-contract.js", "OPENCLAW_HOST_VERSION"),
            ("plugin.js", "PLUGIN_VERSION"),
        ):
            source = (ROOT / "integrations" / "openclaw-agentbridge" / "lib" / filename).read_text(encoding="utf-8")
            match = re.search(rf'{constant} = "([^"]+)"', source)
            self.assertIsNotNone(match)
            self.assertEqual(package["version"], match.group(1))
        profile = deepcopy(self.profile)
        profile["hostInstanceId"] = "openclaw-gateway"
        profile["implementation"] = {
            "name": "openclaw",
            "version": package["version"],
        }

        result = self.store.negotiate(
            user_subject="user-a",
            token_id="token-a",
            profile=profile,
        )

        self.assertEqual("L3", result["acceptedLevel"])
        self.assertEqual("approved", result["compatibilityStatus"])

    def test_unknown_version_is_l1_and_cannot_use_l3(self) -> None:
        self.profile["implementation"]["version"] = "0.1.1"
        result = self.store.negotiate(
            user_subject="user-a",
            token_id="token-a",
            profile=self.profile,
        )
        self.assertEqual("L1", result["acceptedLevel"])
        self.assertEqual("unrecognized", result["compatibilityStatus"])
        with self.assertRaises(HostRegistrationRequired):
            self.store.require_registration(
                user_subject="user-a",
                agent_host="reference-host",
                host_instance_id="reference-host-test-01",
                host_version="0.1.1",
                minimum_level="L3",
            )

    def test_coordinator_lease_is_single_owner_and_takeover_is_explicit(self) -> None:
        first = self.store.acquire_coordinator_lease(
            task_id="task-one",
            user_subject="user-a",
            host_instance_id="host-one",
            agent_host="reference-host",
            lease_seconds=60,
        )
        self.assertEqual(1, first["version"])
        renewed = self.store.acquire_coordinator_lease(
            task_id="task-one",
            user_subject="user-a",
            host_instance_id="host-one",
            agent_host="reference-host",
            lease_seconds=60,
        )
        self.assertEqual(1, renewed["version"])
        with self.assertRaises(HostLeaseConflict):
            self.store.acquire_coordinator_lease(
                task_id="task-one",
                user_subject="user-a",
                host_instance_id="host-two",
                agent_host="openclaw",
                takeover=True,
                expected_version=1,
            )

    def test_runtime_snapshot_contains_no_unbounded_payload(self) -> None:
        self.store.negotiate(
            user_subject="user-a",
            token_id="token-a",
            profile=self.profile,
        )
        registration = self.store.require_registration(
            user_subject="user-a",
            agent_host="reference-host",
            host_instance_id="reference-host-test-01",
            host_version="0.1.0",
            minimum_level="L3",
        )
        result = self.store.record_runtime_snapshot(
            user_subject="user-a",
            registration=registration,
            snapshot={
                "status": "healthy",
                "observedAt": datetime.now(timezone.utc).isoformat(),
                "uptimeSeconds": 10.5,
                "activeTaskCount": 1,
            },
        )
        self.assertEqual("succeeded", result["status"])

        overview = self.store.runtime_overview()
        self.assertEqual(1, overview["summary"]["registrations"])
        self.assertEqual(1, overview["summary"]["healthyRegistrations"])
        self.assertEqual(
            "reference-host-test-01",
            overview["registrations"][0]["hostInstanceId"],
        )
        serialized = json.dumps(overview)
        self.assertNotIn("token-a", serialized)
        self.assertNotIn("declared_profile_json", serialized)

    def test_registration_touch_and_negotiate_throttle_writes_until_interval_or_change(self) -> None:
        from datetime import timedelta
        now = [datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)]
        store = HostContractStore(Path(self.temp.name) / "throttled.db", clock=lambda: now[0])

        first, changed = store.negotiate_with_change(
            user_subject="user-a",
            token_id="token-a",
            profile=self.profile,
        )
        self.assertTrue(changed)
        first_seen = store.require_registration(
            user_subject="user-a",
            agent_host="reference-host",
            host_instance_id="reference-host-test-01",
            host_version="0.1.0",
            minimum_level="L3",
        )["lastSeenAt"]

        # Within 60s window with identical profile/token: skip write & keep lastSeenAt.
        now[0] += timedelta(seconds=15)
        second, changed_second = store.negotiate_with_change(
            user_subject="user-a",
            token_id="token-a",
            profile=self.profile,
        )
        self.assertFalse(changed_second)
        self.assertEqual(second["registrationId"], first["registrationId"])
        reg_within = store.require_registration(
            user_subject="user-a",
            agent_host="reference-host",
            host_instance_id="reference-host-test-01",
            host_version="0.1.0",
            minimum_level="L3",
        )
        self.assertEqual(reg_within["lastSeenAt"], first_seen)

        # Token change triggers immediate write even within 60s.
        _, changed_token = store.negotiate_with_change(
            user_subject="user-a",
            token_id="token-a-rotated",
            profile=self.profile,
        )
        self.assertTrue(changed_token)

        # Past 60s window: require_registration updates lastSeenAt.
        now[0] += timedelta(seconds=61)
        store.require_registration(
            user_subject="user-a",
            agent_host="reference-host",
            host_instance_id="reference-host-test-01",
            host_version="0.1.0",
            minimum_level="L3",
        )
        refreshed = store.require_registration(
            user_subject="user-a",
            agent_host="reference-host",
            host_instance_id="reference-host-test-01",
            host_version="0.1.0",
            minimum_level="L3",
        )
        self.assertEqual(refreshed["lastSeenAt"], now[0].isoformat())

    def test_prune_runtime_snapshots_bounded_and_preserves_latest_per_host(self) -> None:
        from datetime import timedelta
        import sqlite3

        now = [datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)]
        db_path = Path(self.temp.name) / "prune.db"
        store = HostContractStore(db_path, clock=lambda: now[0])
        store.negotiate(user_subject="user-a", token_id="token-a", profile=self.profile)
        reg_a = store.require_registration(
            user_subject="user-a",
            agent_host="reference-host",
            host_instance_id="reference-host-test-01",
            host_version="0.1.0",
            minimum_level="L3",
        )
        profile_b = deepcopy(self.profile)
        profile_b["hostInstanceId"] = "reference-host-test-02"
        store.negotiate(user_subject="user-b", token_id="token-b", profile=profile_b)
        reg_b = store.require_registration(
            user_subject="user-b",
            agent_host="reference-host",
            host_instance_id="reference-host-test-02",
            host_version="0.1.0",
            minimum_level="L3",
        )

        # Host A: 3 old snapshots + 1 fresh snapshot.
        for days_ago in (25, 22, 18, 1):
            store.record_runtime_snapshot(
                user_subject="user-a",
                registration=reg_a,
                snapshot={
                    "status": "healthy",
                    "observedAt": (now[0] - timedelta(days=days_ago)).isoformat(),
                },
            )
        # Host B: 2 old snapshots only — latest one MUST still be kept!
        for days_ago in (30, 20):
            store.record_runtime_snapshot(
                user_subject="user-b",
                registration=reg_b,
                snapshot={
                    "status": "healthy",
                    "observedAt": (now[0] - timedelta(days=days_ago)).isoformat(),
                },
            )

        first_removed = store.prune_runtime_snapshots(retention_days=14, batch_limit=2)
        self.assertEqual(first_removed, 2)
        second_removed = store.prune_runtime_snapshots(retention_days=14, batch_limit=5)
        self.assertEqual(second_removed, 2)
        self.assertEqual(store.prune_runtime_snapshots(retention_days=14, batch_limit=5), 0)

        with sqlite3.connect(db_path) as conn:
            remaining = conn.execute(
                "SELECT user_subject, COUNT(*) FROM agent_host_runtime_snapshots GROUP BY user_subject"
            ).fetchall()
        self.assertEqual(dict(remaining), {"user-a": 1, "user-b": 1})


if __name__ == "__main__":
    unittest.main()
