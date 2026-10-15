#!/usr/bin/env python3
"""Serve the isolated full-business Workspace acceptance fixture."""
import argparse
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--mixed-batch", action="store_true")
    parser.add_argument("--legacy-batch", action="store_true")
    args = parser.parse_args()
    custom = args.profile or args.mixed_batch or args.legacy_batch
    fixture = FullBusinessAcceptanceFixture(args.home or Path(tempfile.mkdtemp(prefix="agentbridge-full-business-")), seed=not custom)
    for profile in args.profile:
        fixture.add_pending_case(profile)
    if args.mixed_batch:
        fixture.add_pending_batch()
    if args.legacy_batch:
        fixture.add_pending_batch(["missed_punch", "missed_punch"], legacy=True)
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
