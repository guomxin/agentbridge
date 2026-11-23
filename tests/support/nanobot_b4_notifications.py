"""Verify terminal notifications on an isolated simulated Telegram API, authentic central and NanoBot SDK."""

import argparse
import json
import sqlite3
import time
from contextlib import ExitStack
from pathlib import Path

import httpx


def run(home):
    ports = json.loads(
        (home / "b4host" / "telegram" / "fixture-ports.json").read_text()
    )
    with ExitStack() as stack:
        users = []
        for who, hostname in (("501", "127.0.0.1"), ("502", "localhost")):
            url = f"http://{hostname}:{ports[who]}"
            client = stack.enter_context(
                httpx.Client(
                    base_url=url, headers={"Origin": url}, trust_env=False, timeout=35
                )
            )
            assert client.get("/").is_success
            users.append(client)
        a, b = users
        other_before = b.get("/state").json()["notifications"]
        first = a.post("/action", json={"command": "lost-notification"})
        assert first.status_code == 200
        message = first.json()["rows"][-1]["message"]
        deadline = time.monotonic() + 85
        while time.monotonic() < deadline:
            records = a.get("/state").json()["notifications"]
            match = next((r for r in records if r["message"] == message), None)
            if match and match.get("centralDelivery") == "failed":
                break
            time.sleep(1.5)
        else:
            raise AssertionError("No final central delivery failure")
        assert match["business"] == "succeeded" and match["state"] == "delivery_unknown"
        assert match["attempts"] == 1 and match["centralAttempts"] == 5
        assert b.get("/state").json()["notifications"] == other_before
        with sqlite3.connect(home / "mcp" / "central" / "agentbridge.db") as db:
            # This is a separate check of the original task vs historical peer endpoints.
            leaks = db.execute(
                """SELECT COUNT(*) FROM notification_outbox o JOIN agent_tasks t ON t.task_id=o.task_id
                JOIN client_endpoints e ON e.endpoint_id=o.endpoint_id
                WHERE t.agent_host='nanobot' AND e.agent_host<>'nanobot' AND t.host_task_key=?""",
                (
                    next(
                        r
                        for r in a.get("/state").json()["rows"]
                        if r["message"] == message
                    )["run"],
                ),
            ).fetchone()[0]
        assert leaks == 0
        print(
            json.dumps(
                {
                    "business": "succeeded",
                    "centralDelivery": "failed",
                    "telegram": "delivery_unknown",
                    "botSends": 1,
                    "ackAttempts": 5,
                    "otherSubjectUnchanged": True,
                    "automaticCrossHostDestinations": 0,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    run(parser.parse_args().home)
