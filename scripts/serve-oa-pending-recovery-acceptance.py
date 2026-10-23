#!/usr/bin/env python3
"""Serve true consumed-unknown recovery cases for Chrome-extension acceptance."""
import argparse
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.support.oa_pending_recovery_acceptance import PendingRecoveryAcceptanceFixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--mode", action="append", choices=("unresolved", "complete", "conflict"))
    args = parser.parse_args()
    fixture = PendingRecoveryAcceptanceFixture(args.home or Path(tempfile.mkdtemp(prefix="agentbridge-oa-recovery-")),
        **({"modes": tuple(args.mode)} if args.mode else {}))
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
