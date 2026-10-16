#!/usr/bin/env python3
"""Serve or run HTTP/Workspace coverage for 28 actions and both batch kinds."""
import argparse
import importlib.util
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-revision")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--group", choices=("all", "oa", "other", "mixed", "legacy"), default="all")
    parser.add_argument("--label", default="action-matrix")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = args.source_root.resolve()
    if not (source / "agentbridge/core/central_service.py").is_file():
        parser.error("Source tree is missing AgentBridge")
    sys.path.insert(0, str(source))
    runner_module = load_module("matrix_benchmark_cli", root / "scripts/workspace-latency-benchmark.py")
    base = load_module("matrix_latency_helpers", root / "tests/support/latency_acceptance.py")
    matrix = load_module("matrix_http_coverage", root / "tests/support/latency_action_matrix.py")
    fingerprint = runner_module.source_fingerprint(source)
    home = args.home or Path(tempfile.mkdtemp(prefix="agentbridge-performance-matrix-"))
    fixture = matrix.ActionMatrixAcceptanceFixture(home, seed=False)
    servers = fixture.servers()
    threads = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        threads.append(thread)
    try:
        metadata = {"sourceRoot": str(source), "sourceRevision": args.source_revision,
                    "sourceContentSha256": fingerprint, "label": args.label, "group": args.group,
                    "home": str(home), "measurementKind": "chrome-manual-fixture" if args.serve else "automated-loopback-http",
                    "performanceContract": "Coverage regression, no production/model latency inference and no fixed downstream-delay comparison"}
        if args.serve:
            matrix.seed_cases(fixture, args.group)
            metadata.update(fixture.public_metadata())
            metadata.update(synthetic_password=base.PASSWORD,
                            oaCases=fixture.meta["full_cases"], matrixCases=fixture.meta["matrix_cases"])
            base.write_metadata(fixture.home / "benchmark-runtime.json", metadata)
            print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
            threading.Thread(target=fixture.coordinate, daemon=True).start()
            signal.signal(signal.SIGTERM, lambda *_: fixture.stop_event.set())
            signal.signal(signal.SIGINT, lambda *_: fixture.stop_event.set())
            fixture.stop_event.wait()
            return
        workload = matrix.MatrixHttpWorkload(fixture, base.JsonHttpClient, base.PASSWORD)
        result = workload.run(args.group)
        end_fingerprint = runner_module.source_fingerprint(source)
        metadata.update(sourceEndContentSha256=end_fingerprint, sourceChangedDuringRun=end_fingerprint != fingerprint)
        report = {"metadata": metadata, **result}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            base.write_metadata(args.output, report)
        print(json.dumps({"output": str(args.output), "rows": len(result["rows"]),
                          "uniqueActions": result["uniqueActions"], "decisions": result["decisions"],
                          "writes": result["writes"], "sourceChangedDuringRun": metadata["sourceChangedDuringRun"]}, indent=2), flush=True)
        if metadata["sourceChangedDuringRun"]:
            raise SystemExit("Source changed during coverage regression")
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(2)
        fixture.close()


if __name__ == "__main__":
    main()
