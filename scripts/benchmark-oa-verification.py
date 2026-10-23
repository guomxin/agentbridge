#!/usr/bin/env python3
"""Run the identical external OA verification workload against a fixed tree."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading


def source_manifest(root):
    """Hash the declared runtime roots, excluding generated dependency trees."""
    blocked = {"__pycache__", "node_modules", "dist", "build", ".git", ".pytest_cache"}
    suffixes = {".py", ".js", ".mjs", ".css", ".html", ".json", ".toml", ".yaml", ".yml"}
    files = []
    for folder in ("agentbridge", "bscli", "integrations", "schemas"):
        path = root / folder
        if path.exists():
            files.extend(item for item in path.rglob("*") if item.is_file()
                and item.suffix in suffixes and not blocked.intersection(item.relative_to(root).parts))
    if (root / "pyproject.toml").exists():
        files.append(root / "pyproject.toml")
    rows = [{"path": item.relative_to(root).as_posix(), "sha256": hashlib.sha256(item.read_bytes()).hexdigest()}
        for item in sorted(set(files))]
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {"algorithm": "sha256(sorted JSON relative path and per-file SHA256)",
        "roots": ["agentbridge", "bscli", "integrations", "schemas", "pyproject.toml"],
        "excludedDirectories": sorted(blocked), "suffixes": sorted(suffixes),
        "fileCount": len(rows), "sha256": hashlib.sha256(encoded).hexdigest(), "files": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--scenario", action="append", choices=("first-page", "second-page", "budget", "material-drift"))
    parser.add_argument("--execution-timeout", type=float, default=1.2)
    parser.add_argument("--run-label", default="independent-sequential")
    args = parser.parse_args()
    if args.samples < 1:
        raise ValueError("samples must be positive")
    source = args.source_root.resolve()
    sys.path.insert(0, str(source))
    module_path = Path(__file__).resolve().parents[1] / "tests/support/oa_verification_acceptance.py"
    harness_hash = hashlib.sha256(module_path.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location("independent_oa_verification", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    initial_source = source_manifest(source)
    (output / "runtime-manifest-start.json").write_text(json.dumps(initial_source, ensure_ascii=False, indent=2) + "\n")
    fixture = module.OaVerificationAcceptanceFixture(output / "home", contract=module.VerificationContract(execution_timeout=args.execution_timeout))
    servers = fixture.servers()
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    workload = module.VerificationWorkload(fixture)
    samples = []
    scenarios = args.scenario or module.SCENARIOS
    try:
        with (output / "raw_samples.jsonl").open("w") as stream:
            for scenario in scenarios:
                for index in range(args.samples):
                    sample = {"sample": index + 1, **workload.process(scenario)}
                    samples.append(sample)
                    stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
                    stream.flush()
                print(json.dumps({"scenario": scenario, "summary": module.summarize(samples)[scenario]}, ensure_ascii=False), flush=True)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        final_source = source_manifest(source)
        (output / "runtime-manifest-end.json").write_text(json.dumps(final_source, ensure_ascii=False, indent=2) + "\n")
        report = {"metadata": {"contract": module.CONTRACT, "sourceRoot": str(source),
            "runLabel": args.run_label, "python": sys.version, "platform": sys.platform,
            "harnessSha256": harness_hash,
            "sourceRuntimeStartSha256": initial_source["sha256"], "sourceRuntimeEndSha256": final_source["sha256"],
            "sourceRuntimeFileCount": initial_source["fileCount"],
            "sourceRuntimeUnchanged": initial_source["sha256"] == final_source["sha256"],
            "latencyContract": fixture.contract.public(), "requestedSamplesPerGroup": args.samples},
            "summary": module.summarize(samples), "ledger": fixture.audit()}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        fixture.close()
    if initial_source["sha256"] != final_source["sha256"] or any(row.get("error") or not row.get("ledgerValid") for row in samples):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
