"""Exercise the published worker argv against a disposable, authenticated loopback host."""

import argparse
from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/native"))
from nanobot_release import jobs


def worker_argv(root, python, instance, port, config):
    release = root / "releases" / "synthetic"
    release.mkdir(parents=True)
    shutil.copytree(
        ROOT / "integrations/nanobot-agentbridge/nanobot_agentbridge",
        release / "nanobot_agentbridge",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(config, release / "host.json")
    (root / "current").symlink_to(release, target_is_directory=True)
    # Only the worker definition is executed. The companion SSH definition is inert.
    definitions = jobs(
        {"runtimeDir": str(root), "enginePython": str(python), "instanceId": instance,
         "listenPort": port, "remotePort": port, "tunnelIdentityFile": str(root / "unused-key")},
        {"tunnel": {"host": "unused.invalid", "knownHostsFile": str(root / "unused-hosts")}},
    )
    return definitions["ai.agentbridge.nanobot." + instance + ".worker"]["ProgramArguments"]


def exercise(*, gateway, instance, **_):
    ready = gateway.readiness()
    assert ready["available"] and ready["adapterVersion"] == "0.3.0"
    connection = HTTPConnection("127.0.0.1", gateway.port, timeout=3)
    try:
        connection.request("POST", "/v1/ready", body=json.dumps({"instance": instance}),
                           headers={"Content-Type": "application/json"})
        status = connection.getresponse().status
    finally:
        connection.close()
    assert status == 401
    return {"available": True, "adapterVersion": ready["adapterVersion"],
            "unauthenticatedStatus": status, "telegramStarted": False}


if __name__ == "__main__":
    from tests.support.nanobot_controlled_write_probe import run

    parser = argparse.ArgumentParser()
    parser.add_argument("--nanobot-python", required=True, type=Path)
    print(json.dumps(run(parser.parse_args().nanobot_python, False, exercise=exercise)))
