"""Read-only Mac readiness checks; never installs, starts or reconfigures a host."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys


def version(command):
    if not shutil.which(command):
        return None
    try:
        result = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    # Keep version only, not command output or plugin/credential diagnostics.
    pattern = (r"(?m)^OpenClaw\s+(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)\b" if command == "openclaw"
               else r"(?m)^v?(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)\s*$")
    match = re.search(pattern, result.stdout.strip())
    return match.group(1) if match else None


def node_supported(value):
    if value is None:
        return False
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        return False
    major, minor, patch = map(int, value.split("."))
    return ((major == 22 and (minor, patch) >= (22, 3))
            or (major == 24 and (minor, patch) >= (15, 0))
            or (major == 25 and (minor, patch) >= (9, 0)) or major > 25)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server", default="10.10.50.213")
    args = parser.parse_args()
    node, openclaw = version("node"), version("openclaw")
    checks = {"macOS": platform.system() == "Darwin", "python312": sys.version_info >= (3, 12),
              "nodeSupported": node_supported(node), "openclawPinned": openclaw == "2026.7.1"}
    for name in ("git", "ssh", "npm", "launchctl", "plutil", "zsh"):
        checks[name] = shutil.which(name) is not None
    for port in (22, 8780, 8781, 8782, 8783, 8790):
        try:
            with socket.create_connection((args.server, port), timeout=3):
                checks[f"tcp{port}"] = True
        except OSError:
            checks[f"tcp{port}"] = False
    report = {"schema": "agentbridge.mac-migration.preflight.v1", "checks": checks,
              "node": node, "openclaw": openclaw, "architecture": platform.machine(),
              "readyForFurtherValidation": all(checks.values()), "migrationComplete": False,
              "notChecked": ["TLS trust", "identity routing", "channels", "CA issuance", "launchd",
                             "SSH authentication", "publishing", "Windows-off acceptance"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if all(checks.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
