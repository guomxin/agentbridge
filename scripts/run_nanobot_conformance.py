"""Run the pinned NanoBot SDK against isolated synthetic central services and the shared H01–H29 vectors."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "schemas/agent-host/v1/test-vectors.json"
# Every entry starts its own central DB, OAuth identity, model, hosted adapter and ephemeral ports.
# All entries that substantiate a vector must pass.
PROBES = (
    (
        "authority",
        "nanobot_coordinator_contract_probe",
        (),
        ("H01", "H03", "H15", "H21", "H22", "H24"),
    ),
    (
        "credential",
        "nanobot_credential_probe",
        ("--initial-read",),
        ("H02", "H04", "H06", "H12", "H16", "H17", "H18", "H26"),
    ),
    ("expired", "nanobot_credential_probe", ("--expire",), ("H07",)),
    (
        "known-unknown",
        "nanobot_controlled_write_probe",
        ("--twice",),
        ("H06", "H09", "H10", "H11", "H20", "H27"),
    ),
    (
        "late-claim",
        "nanobot_controlled_write_probe",
        ("--pending-restart",),
        ("H06", "H11", "H19", "H20", "H27"),
    ),
    ("two-endpoints", "nanobot_telegram_write_probe", (), ("H05", "H09", "H16", "H18")),
    (
        "runtime",
        "nanobot_governed_matrix_probe",
        ("oa.missed_punch.prepare",),
        ("H09", "H25"),
    ),
    (
        "credential-to-draft",
        "nanobot_credential_write_probe",
        (),
        ("H05", "H08", "H09", "H20"),
    ),
    ("batch", "nanobot_batch_probe", (), ("H06", "H09", "H23")),
    (
        "read-plan",
        "nanobot_controlled_write_probe",
        ("--plan", "--restart"),
        ("H17", "H28", "H29"),
    ),
    ("files-notification", "nanobot_b4_contract", (), ("H12", "H13", "H14", "H18")),
)


def execute(spec, python):
    name, module, args, cases = spec
    cmd = [
        sys.executable,
        "-m",
        "tests.support." + module,
        "--nanobot-python",
        str(python),
        *args,
    ]
    with tempfile.TemporaryDirectory(prefix="nanobot-conformance-") as td:
        key = Path(td) / "session.key"
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        env = {
            k: os.environ[k]
            for k in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR")
            if k in os.environ
        }
        env.update(
            HOME=td,
            AGENTBRIDGE_SESSION_KEY_FILE=str(key),
            PYTHONNOUSERSITE="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        location = None
        try:
            result = subprocess.run(
                cmd, cwd=ROOT, env=env, capture_output=True, timeout=310
            )
            proof = (
                json.loads(result.stdout.splitlines()[-1])
                if result.returncode == 0
                else None
            )
            passed = isinstance(proof, dict) and bool(proof)
            if not passed:
                frames = re.findall(
                    r'File "[^"]*/([a-zA-Z0-9_]+\.py)", line ([0-9]+)',
                    result.stderr.decode(errors="replace"),
                )
                location = ":".join(frames[-1]) if frames else "child_exit"
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            proof = None
            passed = False
    # Deliberately exclude raw SDK diagnostics, endpoint URLs, credentials and per-run identifiers.
    digest = (
        hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()
        if proof
        else None
    )
    return {
        "probe": name,
        "passed": passed,
        "evidenceSha256": digest,
        "cases": list(cases),
        **({"failureLocation": location} if not passed else {}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nanobot-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    opts = parser.parse_args()
    python = opts.nanobot_python.absolute()
    version = subprocess.check_output(
        [
            python,
            "-I",
            "-c",
            "from importlib.metadata import version;print(version('nanobot-ai'))",
        ],
        text=True,
    ).strip()
    if version != "0.4.2":
        raise SystemExit("NanoBot SDK 0.4.2 is required")
    expected = [
        item["id"] for item in json.loads(VECTORS.read_bytes())["conformanceCases"]
    ]
    if {c for *_, cs in PROBES for c in cs} != set(expected):
        raise RuntimeError("Conformance vector coverage changed")
    with ThreadPoolExecutor(max_workers=3) as pool:
        checks = list(pool.map(lambda spec: execute(spec, python), PROBES))
    cases = [
        {"caseId": c, "passed": all(p["passed"] for p in checks if c in p["cases"])}
        for c in expected
    ]
    report = {
        "schemaVersion": "agentbridge.nanobot-conformance.v1",
        "host": "nanobot",
        "adapter": "0.3.0",
        "sdk": version,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "contractSha256": hashlib.sha256(VECTORS.read_bytes()).hexdigest(),
        "syntheticDownstreams": True,
        "probes": checks,
        "cases": cases,
        "passed": all(row["passed"] for row in cases),
    }
    opts.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    opts.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "passedCases": sum(row["passed"] for row in cases),
                "failedProbes": [p["probe"] for p in checks if not p["passed"]],
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
