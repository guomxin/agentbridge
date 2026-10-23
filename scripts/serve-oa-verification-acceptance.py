#!/usr/bin/env python3
"""Serve a fresh real Workspace with observable synthetic OA verification IO."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.support.oa_verification_acceptance import OaVerificationAcceptanceFixture, VerificationContract
from tests.support.interaction_acceptance import write_metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--execution-timeout", type=float, default=12)
    args = parser.parse_args()
    fixture = OaVerificationAcceptanceFixture(args.home,
        contract=VerificationContract(execution_timeout=args.execution_timeout, fixed_wait_scale=1, observable=True), seed=True)
    servers = fixture.servers()
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=fixture.coordinate, daemon=True).start()
    write_metadata(fixture.home / "public.json", fixture.public_metadata())
    print(json.dumps({key: fixture.public_metadata()[key] for key in ("fixture", "workspace_url", "card_origin", "home", "credentials_file")}, indent=2), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: fixture.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: fixture.stop_event.set())
    try:
        while not fixture.stop_event.wait(.5):
            write_metadata(fixture.home / "ledger.json", fixture.audit())
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        write_metadata(fixture.home / "ledger.json", fixture.audit())
        fixture.close()


if __name__ == "__main__":
    main()
