#!/usr/bin/env python3
"""Serve one independently seeded material acceptance home with durable ledgers."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.support.card_material_acceptance import CardMaterialAcceptanceFixture
from tests.support.interaction_acceptance import write_metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--seed", choices=("browser", "coverage", "empty"), default="browser")
    args = parser.parse_args()
    fixture = CardMaterialAcceptanceFixture(args.home, seed=args.seed)
    servers = fixture.servers()
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    coordinator = threading.Thread(target=fixture.coordinate, daemon=True)
    for thread in [*threads, coordinator]:
        thread.start()
    metadata = fixture.public_metadata()
    write_metadata(fixture.home / "public.json", metadata)
    print(json.dumps({key: metadata[key] for key in ("fixture", "home", "workspace_url", "card_origin", "credentials_file")},
                     ensure_ascii=False, indent=2), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: fixture.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: fixture.stop_event.set())
    try:
        while not fixture.stop_event.wait(.5):
            write_metadata(fixture.home / "ledger.json", fixture.audit())
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        coordinator.join(timeout=20)
        write_metadata(fixture.home / "ledger.json", fixture.audit())
        fixture.close()


if __name__ == "__main__":
    main()
