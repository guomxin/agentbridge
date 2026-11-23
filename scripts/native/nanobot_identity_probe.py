"""Authenticate both fixed Workspace subjects with the same released NanoBot MCP client."""

import asyncio
import json
import sys
from pathlib import Path


async def verify(release, required):
    sys.path.insert(0, str(release))
    from nanobot_agentbridge.worker import authenticated_session

    config = json.loads((release / "host.json").read_text())
    seen = set()
    for binding in config["bindings"]:
        request = {
            "instance": config["instanceId"],
            "subject": binding["user_subject"],
            "endpointKey": binding["endpoint_key"],
            "tokenFile": binding["tokenFile"],
            "mcpUrl": config["mcpUrl"],
            "caFile": config["caFile"],
            "trustedCardOrigins": config["trustedCardOrigins"],
            "coordinatorProgram": config.get("coordinatorProgram"),
        }
        async with authenticated_session(request) as (_, _, level):
            if level != required or binding["user_subject"] in seen:
                raise ValueError(
                    "Pilot identity or approved level differs from the reviewed bindings"
                )
            seen.add(binding["user_subject"])
    if len(seen) != 2:
        raise ValueError("Two distinct authenticated pilot identities required")
    print(json.dumps({"authenticatedSubjects": len(seen), "acceptedLevel": required}))


if __name__ == "__main__":
    try:
        asyncio.run(verify(Path(sys.argv[1]), sys.argv[2]))
    except Exception:
        raise SystemExit("NanoBot central identity preflight failed") from None
