#!/usr/bin/env python3
"""Run or serve the same isolated Workspace workload against either source tree."""
import argparse
import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import hashlib


def source_fingerprint(source):
    fingerprint = hashlib.sha256()
    for path in sorted((source / "agentbridge").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".js", ".mjs", ".css", ".html"}:
            fingerprint.update(str(path.relative_to(source)).encode())
            fingerprint.update(path.read_bytes())
    return fingerprint.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--home", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path, help="Compare an equivalent baseline JSON report")
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--source-revision", help="Exact commit for a source archive, when available")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--serve-case", action="append", choices=("single", "mixed", "legacy", "query"),
                        help="Chrome seed cases; default single+mixed. Legacy uses a separate fixture.")
    parser.add_argument("--history-messages", type=int, default=0)
    parser.add_argument("--history-tasks", type=int, default=0)
    parser.add_argument("--observe-workspace-http", action="store_true")
    parser.add_argument("--repeat", type=int, default=30)
    parser.add_argument("--selected-preparation", action="store_true",
                        help="Fresh query selection to trusted input; separate from the original six scenarios")
    parser.add_argument("--query-profile", action="append", default=[],
                        help="Additional synthetic pending types for query-only Chrome selection; does not seed an active batch")
    parser.add_argument("--scenario", action="append", choices=("query", "single", "mixed", "legacy", "drift", "unknown"))
    parser.add_argument("--io-delay", type=float, default=.02)
    parser.add_argument("--worker-start-delay", type=float, default=.01)
    parser.add_argument("--fixed-wait-scale", type=float, default=0,
                        help="0 measures control/material paths; 1 emulates actual requested browser waits")
    args = parser.parse_args()
    source = args.source_root.resolve()
    if not (source / "agentbridge/core/central_service.py").is_file():
        parser.error("--source-root must contain an AgentBridge checkout or immutable git archive")
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    if args.selected_preparation and (args.serve or args.scenario):
        parser.error("Selected preparation is an independent automated workload")
    if args.selected_preparation and not (source / "agentbridge/core/actionable_results.py").is_file():
        parser.error("This source revision does not implement direct selected-result preparation; no comparable baseline exists")
    if args.serve_case and "legacy" in args.serve_case and "mixed" in args.serve_case:
        parser.error("Use separate homes for active legacy and mixed batches")
    sys.path.insert(0, str(source))
    fingerprint = source_fingerprint(source)
    git_head, git_dirty = args.source_revision, None
    if (source / ".git").exists():
        git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        git_dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True).strip())
    spec = importlib.util.spec_from_file_location("latency_acceptance_workload", Path(__file__).resolve().parents[1] / "tests/support/latency_acceptance.py")
    harness_hash = hashlib.sha256(Path(__file__).read_bytes() + b"\0" + Path(spec.origin).read_bytes()).hexdigest()
    workload = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workload)
    home = args.home or Path(tempfile.mkdtemp(prefix="agentbridge-latency-"))
    latency = workload.LatencyContract(io_delay_seconds=args.io_delay, worker_start_seconds=args.worker_start_delay,
                                      fixed_wait_scale=args.fixed_wait_scale)
    fixture = workload.LatencyAcceptanceFixture(home, latency=latency, seed=False)
    if args.serve and (args.history_messages or args.history_tasks):
        fixture.seed_history(messages=args.history_messages, tasks=args.history_tasks)
    if args.serve and not fixture.meta["full_cases"] and not any(
            case.get("key") == "browser-query" for case in fixture.meta.get("query_cases", [])):
        for name in args.serve_case or ("single", "mixed"):
            if name == "single":
                fixture.add_pending_case("attendance_confirmation", case_key="browser-single")
            elif name == "query":
                fixture.add_query_case(extra_profiles=args.query_profile)
            else:
                fixture.add_pending_batch(("missed_punch", "missed_punch") if name == "legacy"
                                          else ("missed_punch", "weekly_report"), legacy=name == "legacy")
    servers = fixture.servers()
    if args.observe_workspace_http:
        fixture.observe_workspace_http(servers[1])
    threads = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        threads.append(thread)
    try:
        metadata = {**fixture.public_metadata(), "label": args.label, "sourceRoot": str(source),
                    "harnessContentSha256": harness_hash,
                    "sourceContentSha256": fingerprint, "sourceRevision": git_head, "sourceGitDirty": git_dirty,
                    "sourceIsArchive": not (source / ".git").exists(), "home": str(home),
                    "measurementKind": "chrome-manual-fixture" if args.serve else "automated-loopback-http",
                    "humanThinkTimeIncluded": False, "warmupsExcluded": 0,
                    "entryBoundary": "Prepare/query enter real Central in-process; draft/card/projection calls use real loopback HTTP. No model/gateway benchmark."}
        if args.selected_preparation:
            metadata.update(benchmarkContract="latency-selected-preparation-v2",
                entryBoundary="After a fresh source query/public choice exists: real Workspace CSRF POST to queued intent, synthetic Python host preparation loop, real OA preflight, HTTP task polling and trusted draft GET. No OpenClaw private RPC/lease or model performance claim.",
                preparationHost="Synthetic Python fixture coordinator", preparationHostPollIntervalSeconds=.05,
                queryPreparationExcluded=True, existingPreparedTaskSamplesAllowed=False,
                freshExactAffairPerSample=True, pendingChoicesPerSample=1,
                syntheticPendingRotationExcluded=True)
        workload.write_metadata(fixture.home / "benchmark-runtime.json", metadata)
        if args.serve:
            threading.Thread(target=fixture.coordinate, daemon=True).start()
            print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
            signal.signal(signal.SIGTERM, lambda *_: fixture.stop_event.set())
            signal.signal(signal.SIGINT, lambda *_: fixture.stop_event.set())
            fixture.stop_event.wait()
            return
        samples = []
        if args.selected_preparation:
            threading.Thread(target=fixture.coordinate, daemon=True).start()
        runner = workload.SelectedPreparationWorkload(fixture) if args.selected_preparation else workload.HttpWorkload(fixture)
        for scenario in (("selected_preparation",) if args.selected_preparation else args.scenario or workload.SCENARIOS):
            for index in range(args.repeat):
                try:
                    sample = runner.selected_preparation() if args.selected_preparation else runner.process(scenario)
                except Exception as exc:
                    sample = {"error": type(exc).__name__ + ": " + str(exc)}
                samples.append({"scenario": scenario, "sample": index + 1, **sample})
        end_fingerprint = source_fingerprint(source)
        metadata.update(sourceEndContentSha256=end_fingerprint, sourceChangedDuringRun=end_fingerprint != fingerprint)
        result = {"metadata": metadata, "summary": workload.summarize(samples), "samples": samples,
                  "ledgerAudit": fixture.audit()}
        if args.selected_preparation:
            result["selectionAudit"] = {"uniqueSourceTasks": len(runner.source_tasks),
                "uniqueSourceOperations": len(runner.source_operations), "uniquePreparedTasks": len(runner.prepared_tasks),
                "uniqueSourceAffairs": len(runner.source_affairs),
                "uniqueSourceHashes": len({sample["sourceHash"] for sample in samples if "sourceHash" in sample}),
                "modelCalls": sum(sample.get("modelCalls", 0) for sample in samples),
                "gatewayMethods": sorted(set(runner.gateway_calls)),
                "preparationHostBoundary": "Python fixture only; OpenClaw private RPC L3/lease are verified separately"}
        if args.compare:
            result["comparison"] = workload.compare_reports(json.loads(args.compare.read_text()), result)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            workload.write_metadata(args.output, result)
        print(json.dumps({"label": args.label, "output": str(args.output) if args.output else None,
                          "summary": result["summary"]}, ensure_ascii=False, indent=2), flush=True)
        if any("error" in sample for sample in samples):
            raise SystemExit(1)
        if metadata["sourceChangedDuringRun"]:
            raise SystemExit("Source changed during measurement; this report is prevalidation only")
    finally:
        workload.write_metadata(fixture.home / "benchmark-audit.json", fixture.audit())
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(2)
        fixture.close()


if __name__ == "__main__":
    main()
