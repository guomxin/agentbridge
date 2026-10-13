#!/usr/bin/env python3
"""Serve actual draft/central/Taihua code with network-free downstream HTTP."""
import argparse
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.support.business_draft_acceptance import BusinessDraftAcceptanceFixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="Empty or previously marked acceptance directory")
    parser.add_argument("--new-case", action="store_true")
    args = parser.parse_args()
    home = args.home or Path(tempfile.mkdtemp(prefix="agentbridge-draft-acceptance-"))
    fixture = BusinessDraftAcceptanceFixture(home, seed=not args.new_case)
    if args.new_case:
        fixture.add_case()
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
