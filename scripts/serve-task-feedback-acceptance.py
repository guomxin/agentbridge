#!/usr/bin/env python3
"""Serve isolated Workspace tasks with real private-skill version attribution."""
import argparse
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.support.task_feedback_acceptance import TaskFeedbackAcceptanceFixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="Empty or previously marked isolated acceptance directory")
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--new-case", action="store_true")
    controls.add_argument("--revoke", action="store_true", help="Revoke only the latest synthetic method assignment")
    parser.add_argument("--with-business-cases", action="store_true", help="Append fresh room, leave, legacy and work-log inputs at startup")
    args = parser.parse_args()
    if args.revoke and not args.home:
        parser.error("--revoke requires an existing isolated --home")
    if args.revoke and args.with_business_cases:
        parser.error("--revoke cannot create new business cases")
    fixture = TaskFeedbackAcceptanceFixture(args.home or Path(tempfile.mkdtemp(prefix="agentbridge-feedback-acceptance-")),
                                            seed=not (args.new_case or args.revoke))
    if args.revoke:
        try:
            fixture.revoke_current_skill()
            print("Synthetic method revoked; historical task bindings remain immutable.")
        finally:
            fixture.close()
        return
    if args.new_case:
        fixture.add_feedback_cases()
    if args.with_business_cases:
        fixture.add_pending_business_cases()
    servers = fixture.servers()
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=fixture.coordinate, daemon=True).start()
    print(json.dumps({**fixture.public_metadata(), "home": str(fixture.home)}, ensure_ascii=False, indent=2), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: fixture.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: fixture.stop_event.set())
    try:
        fixture.stop_event.wait()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        fixture.close()


if __name__ == "__main__":
    main()
