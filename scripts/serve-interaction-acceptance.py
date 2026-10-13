#!/usr/bin/env python3
"""Run an isolated card acceptance fixture. Restart with the same --home to retain state."""
from pathlib import Path
import argparse
import json
import signal
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.support.interaction_acceptance import AcceptanceFixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="Empty isolated directory, or a prior fixture directory")
    parser.add_argument("--new-case", action="store_true", help="Keep history and add new form/batch samples on restart")
    args = parser.parse_args()
    home = args.home or Path(tempfile.mkdtemp(prefix="agentbridge-card-acceptance-"))
    fixture = AcceptanceFixture(home)
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
