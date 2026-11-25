"""Verify both personal identities using the released Pi adapter and its private model-free path."""

import asyncio
import json
from pathlib import Path
import sys


async def verify(release, level):
    sys.path[:0] = [str(release), str(release / "agentbridge.whl")]
    from pi_agentbridge.central import connect
    from pi_agentbridge.server import bearer
    from pi_agentbridge.runtime import private_file

    config = json.loads(private_file(release / "host.json"))
    seen = set()
    for binding in config["bindings"]:
        subject = binding["user_subject"]
        async with connect(config, binding, bearer(binding["tokenFile"])) as client:
            if subject in seen or getattr(client, "level", "L1") != level:
                raise ValueError("Pi principal or capability level did not match")
            seen.add(subject)
    if len(seen) != 2:
        raise ValueError("Expected two authenticated Pi Workspace principals")
    print(json.dumps({"authenticatedSubjects": len(seen), "acceptedLevel": level}))


if __name__ == "__main__":
    try:
        asyncio.run(verify(Path(sys.argv[1]), sys.argv[2]))
    except Exception:
        raise SystemExit("Pi central identity preflight failed") from None
