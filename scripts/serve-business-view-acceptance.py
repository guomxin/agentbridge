#!/usr/bin/env python3
"""Serve isolated real OA business-view flows with synthetic downstream IO."""
import argparse
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.support.business_view_acceptance import BusinessViewAcceptanceFixture
from tests.support.interaction_acceptance import FIXTURE_VERSION, write_metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="Empty or previously marked acceptance directory")
    parser.add_argument("--new-case", action="store_true")
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--change-material", metavar="CASE_KEY", help="Change only a live synthetic leave fixture's evidence")
    controls.add_argument("--fault", choices=["none", "room_readback_missing", "approval_reply_lost", "pending_partial", "done_identity_changed"])
    args = parser.parse_args()
    if args.change_material or args.fault:
        if not args.home:
            parser.error("Local controls require --home pointing to a marked isolated fixture")
        metadata = json.loads((args.home / "acceptance.json").read_text())
        if metadata.get("fixture") != FIXTURE_VERSION:
            raise ValueError("Refusing to modify an unmarked environment")
        control = {"id": uuid4().hex}
        if args.change_material:
            case = next(case for case in metadata["view_cases"] if case["key"] == args.change_material and case["profile"] == "leave")
            control.update(action="change_material", affair_id=case["arguments"]["affair_id"])
        else:
            control.update(action="fault", fault=None if args.fault == "none" else args.fault)
        temporary = args.home / f"business-view-control.{control['id']}.tmp"
        write_metadata(temporary, control)
        temporary.replace(args.home / "business-view-control.json")
        print("Synthetic-only control queued; it applies on the next fixture read.")
        return
    fixture = BusinessViewAcceptanceFixture(args.home or Path(tempfile.mkdtemp(prefix="agentbridge-view-acceptance-")), seed=not args.new_case)
    if args.new_case:
        fixture.add_view_cases()
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
