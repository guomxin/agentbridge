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


if __name__ == "__main__":
    unittest.main()
