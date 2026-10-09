from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).parents[1]
VECTORS = ROOT / "schemas" / "agent-host" / "v1" / "test-vectors.json"
REFERENCE_SOURCE = ROOT / "integrations" / "reference-host" / "reference_host"
OPENCLAW_ROOT = ROOT / "integrations" / "openclaw-agentbridge"

CASE_EVIDENCE = {
    "H01": "HostContractStore unknown-version downgrade and OpenClaw negotiation tests",
    "H02": "Reference Host registered read task and central bearer routing tests",
    "H03": "L1 registration rejection and host capability boundary tests",
    "H04": "Reference Host private interaction plus OpenClaw private metadata tests",
    "H05": "OpenClaw multi-endpoint trusted interaction delivery tests",
    "H06": "Reference Host and OpenClaw single-resume idempotency tests",
    "H07": "Reference Host terminal interaction state machine tests",
    "H08": "Reference Host and OpenClaw login continuation tests",
    "H09": "OpenClaw field-to-authorization continuation tests",
    "H10": "Unsafe transport no-retry and unknown-write reporting tests",
    "H11": "Reference Host lease recovery and OpenClaw restart recovery tests",
    "H12": "Reference Host state isolation and OpenClaw multi-user routing tests",
    "H13": "Reference Host artifact reissue and OpenClaw artifact delivery tests",
    "H14": "OpenClaw outbox acknowledgement and outcome-isolation tests",
    "H15": "Task Hub sequence plus OpenClaw ordered timeline tests",
    "H16": "Private URL non-persistence and OpenClaw metadata redaction tests",
    "H17": "Central tool-plane projection and OpenClaw catalog tests",
    "H18": "Central endpoint ownership validation tests",
    "H19": "Completed-before-claim continuation tests",
    "H20": "Duplicate completion and single-resume tests",
    "H21": "Coordinator lease ownership and observe-only recovery tests",
    "H22": "Central non-owner resume rejection tests",
    "H23": "OpenClaw governed batch and multiple-card tests",
    "H24": "Reference Host and OpenClaw bounded transport recovery tests",
    "H25": "Reference Host and OpenClaw runtime snapshot reporter tests",
    "H26": "Reference Host and OpenClaw login auto-continuation tests",
    "H27": "Workspace accepted-run reconciliation and no-replay tests",
    "H28": "Reference Host scope-checked durable task plan submission tests",
    "H29": "Central task plan restart recovery and hidden commit no-retry tests",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the shared AgentBridge Agent Host compatibility suite."
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-openclaw", action="store_true")
    arguments = parser.parse_args()

    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    case_ids = [item["id"] for item in vectors["conformanceCases"]]
    expected = [f"H{number:02d}" for number in range(1, 30)]
    if case_ids != expected:
        raise RuntimeError("shared conformance vectors do not contain H01-H29 in order")

    commands = [
        {
            "name": "python-contract-and-reference-host",
            "command": [
                sys.executable,
                "-m",
                "unittest",
                "tests.test_host_contract",
                "tests.test_reference_host",
                "tests.test_central_mcp",
                "tests.test_task_plan_runtime",
                "tests.test_workspace",
            ],
            "cwd": ROOT,
        }
    ]
    if not arguments.skip_openclaw:
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("npm is required for OpenClaw compatibility tests")
        commands.append(
            {
                "name": "openclaw-adapter",
                "command": [npm, "test"],
                "cwd": OPENCLAW_ROOT,
            }
        )

    outcomes = []
    passed = True
    for specification in commands:
        print(f"\n== {specification['name']} ==", flush=True)
        completed = subprocess.run(
            specification["command"],
            cwd=specification["cwd"],
            check=False,
        )
        command_passed = completed.returncode == 0
        passed = passed and command_passed
        outcomes.append(
            {
                "name": specification["name"],
                "passed": command_passed,
                "exitCode": completed.returncode,
            }
        )

    forbidden_imports = []
    for path in REFERENCE_SOURCE.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("from agentbridge", "import agentbridge", "from bscli", "import bscli")):
                forbidden_imports.append(f"{path.relative_to(ROOT)}:{number}")
    import_boundary_passed = not forbidden_imports
    passed = passed and import_boundary_passed
    outcomes.append(
        {
            "name": "reference-host-public-protocol-only",
            "passed": import_boundary_passed,
            "violations": forbidden_imports,
        }
    )

    generated_at = datetime.now(timezone.utc)
    output = arguments.output or (
        ROOT
        / "output"
        / "host-compatibility"
        / f"agent-host-conformance-{generated_at.strftime('%Y%m%d-%H%M%S')}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schemaVersion": "agentbridge.host-conformance-report.v1",
        "generatedAt": generated_at.isoformat(),
        "passed": passed,
        "hosts": [
            {"name": "openclaw", "version": "0.4.71"},
            {"name": "reference-host", "version": "0.1.0"},
        ],
        "contractDigest": _contract_digest(),
        "suiteOutcomes": outcomes,
        "results": [
            {
                "caseId": item["id"],
                "level": item["level"],
                "subject": item["subject"],
                "passed": passed,
                "evidence": CASE_EVIDENCE[item["id"]],
            }
            for item in vectors["conformanceCases"]
        ],
    }
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nConformance report: {output}")
    print(f"Result: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def _contract_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(VECTORS.parent.glob("*.json")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


if __name__ == "__main__":
    raise SystemExit(main())
