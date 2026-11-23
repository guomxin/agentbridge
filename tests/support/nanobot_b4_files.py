"""Verify file renewal and ownership through the isolated signed-in Workspace and original NanoBot run."""

import argparse
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from tests.support.nanobot_b4_setup import PDF
from tests.test_workspace import PASSWORD


@contextmanager
def login(url, username):
    parsed = urlsplit(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    client = httpx.Client(base_url=base, trust_env=False, timeout=40)
    client.headers["Origin"] = base
    assert (
        client.post(
            "/api/login", json={"username": username, "password": PASSWORD}
        ).status_code
        == 200
    )
    client.headers["X-AgentBridge-CSRF"] = client.cookies["agentbridge_workspace_csrf"]
    try:
        yield client
    finally:
        client.close()


def run(home, *, expired_lease=False):
    info = json.loads((home / "public.json").read_text())
    with (
        login(info["signin"]["a"], "pilota") as a,
        login(info["signin"]["b"], "pilotb") as b,
    ):
        answer = a.post(
            "/api/chat/send-stream",
            json={"message": "查询我的待办", "idempotencyKey": "files-" + str(uuid4())},
        )
        assert answer.status_code == 200 and "两项待办" in answer.text
        seed = a.post("/fixture-file", json={})
        assert seed.status_code == 200, ("file fixture", seed.status_code)
        task = seed.json()["taskId"]
        with sqlite3.connect(home / "mcp/central/agentbridge.db") as db:
            files = dict(
                db.execute(
                    "SELECT artifact_type,artifact_id FROM task_artifacts WHERE task_id=?",
                    (task,),
                )
            )
        for kind in ("certificate_scan", "database_csv"):
            route = f"/api/tasks/{task}/artifacts/{files[kind]}/reissue"
            denied = b.post(route, json={})
            assert denied.status_code in {403, 404}, (
                "cross-subject",
                kind,
                denied.status_code,
            )
            res = a.post(route, json={})
            assert res.status_code == 200, (
                kind,
                res.status_code,
                res.json().get("error", {}).get("code")
                if isinstance(res.json().get("error"), dict)
                else None,
            )
            item = next(
                f for f in res.json()["artifacts"] if f["artifact_id"] == files[kind]
            )
            assert item["state"] == "ready" and item["download_url"]
            url = item["download_url"]
            if kind == "database_csv":
                raw = a.get(url)
                assert raw.status_code == 200 and "合成历史报告" in raw.content.decode(
                    "utf-8-sig"
                )
                denied_file = b.get(url)
                assert denied_file.status_code in {403, 404, 409}
                assert denied_file.json().get("error", {}).get("code") in {
                    "DATABASE_REPORT_UNAVAILABLE",
                    "DATABASE_REPORT_DENIED",
                }
            else:
                assert urlsplit(url).hostname == "127.0.0.1"
                raw = a.get(url)
                assert (
                    raw.status_code == 200
                    and hashlib.sha256(raw.content).digest()
                    == hashlib.sha256(PDF).digest()
                )
            extra = {}
            if kind == "certificate_scan" and expired_lease:
                database = home / "mcp/central/agentbridge.db"
                old = "2001-01-01T00:00:00+00:00"
                with sqlite3.connect(database) as db:
                    db.execute(
                        "UPDATE agent_task_coordinator_leases SET expires_at=? WHERE task_id=?",
                        (old, task),
                    )
                    db.execute(
                        "UPDATE task_artifacts SET state='expired',expires_at=? WHERE artifact_id=?",
                        (old, files[kind]),
                    )
                    before = db.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
                # A new authenticated Workspace action on the retained file is allowed; the closed
                # business task and its expired coordinator lease must never be reopened or consumed.
                renewed = a.post(route, json={})
                assert renewed.status_code == 200
                with sqlite3.connect(database) as db:
                    assert db.execute(
                        "SELECT expires_at FROM agent_task_coordinator_leases WHERE task_id=?",
                        (task,),
                    ).fetchone() == (old,)
                    assert db.execute("SELECT COUNT(*) FROM operations").fetchone() == (
                        before,
                    )
                from agentbridge.core.user_grants import UserGrants

                owner = json.loads((home / "bindings.json").read_text())[0][
                    "user_subject"
                ]
                grants = UserGrants(database)
                current = grants.get(owner)
                grants.save(
                    owner,
                    [
                        x
                        for x in current["permissions"]
                        if x != "oa.certificate.download"
                    ],
                    expected_revision=current["revision"],
                    actor="isolated-test",
                    reason="file revoke",
                )
                denied_renewal = a.post(route, json={})
                assert denied_renewal.status_code in {403, 409}
                assert a.get(url).status_code in {403, 410}
                extra = {
                    "expiredLeaseStayedExpired": True,
                    "businessReplays": 0,
                    "revocationEnforced": True,
                }
            print(
                json.dumps(
                    {
                        "artifact": kind,
                        "sameOriginalTask": True,
                        "originalBytesConfirmed": True,
                        "crossOwnerBlocked": True,
                        **extra,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    run(parser.parse_args().home)
