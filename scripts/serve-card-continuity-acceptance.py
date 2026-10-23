#!/usr/bin/env python3
"""Serve independent card continuity acceptance; choose a fresh home per batch."""
import argparse
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.support.card_continuity_acceptance import CardContinuityAcceptanceFixture
from tests.support.business_view_acceptance import write_metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--sources", type=int, default=6)
    args = parser.parse_args()
    fixture = CardContinuityAcceptanceFixture(args.home or Path(tempfile.mkdtemp(prefix="agentbridge-continuity-")), sources=args.sources)
    servers = fixture.servers()
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=fixture.coordinate, daemon=True).start()
    metadata = fixture.public_metadata()
    write_metadata(fixture.home / "public.json", metadata)
    print(json.dumps({key: metadata[key] for key in ("fixture", "workspace_url", "card_origin", "credentials_file", "home", "audit_file")}, ensure_ascii=False, indent=2), flush=True)
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
