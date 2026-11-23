"""Verify saved trial, skill feedback and isolation via the real local Workspace HTTP service."""

import argparse
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from tests.test_workspace import PASSWORD, _cookies, _raw_request, _request


def run(home):
    spec = json.loads((home / "public.json").read_text())
    a = urlsplit(spec["signin"]["a"])
    origin = f"{a.scheme}://{a.netloc}"
    status, headers, _ = _request(
        a.port,
        "POST",
        "/api/login",
        origin=origin,
        body={"username": "pilota", "password": PASSWORD},
    )
    assert status == 200
    cookies = _cookies(headers)
    auth = {
        "origin": origin,
        "cookies": cookies,
        "csrf": cookies["agentbridge_workspace_csrf"],
    }

    def send(path, body):
        status, _, raw = _raw_request(a.port, "POST", path, body=body, **auth)
        assert status == 200, (path, status)
        return raw.decode() if isinstance(raw, bytes) else raw

    db = sqlite3.connect(home / "mcp" / "central" / "agentbridge.db")
    db.row_factory = sqlite3.Row
    source = db.execute(
        "SELECT draft_id,revision FROM skill_drafts WHERE owner_subject=?",
        ("skill-host-alice",),
    ).fetchone()
    trial_key = "b4-trial-" + str(uuid4())
    send(
        "/api/skill-trials/send-stream",
        {
            "message": "下周计划：修复登录问题。",
            "idempotencyKey": trial_key,
            "skillTrial": {
                "draft_id": source["draft_id"],
                "expected_revision": source["revision"],
                "profile": "preview",
            },
        },
    )
    end = time.monotonic() + 60
    sample = None
    while time.monotonic() < end:
        sample = db.execute(
            """SELECT t.status,t.output FROM agent_host_dispatches d
            JOIN skill_authoring_commands c ON c.owner_subject=d.user_subject AND c.request_key='workspace-sample:' || d.dispatch_id
            JOIN skill_draft_tests t ON t.owner_subject=c.owner_subject AND t.test_id=json_extract(c.result_json,'$.test_id')
            WHERE d.idempotency_key=? AND t.draft_id=? AND t.revision=? AND d.user_subject=?""",
            (trial_key, source["draft_id"], source["revision"], "skill-host-alice"),
        ).fetchone()
        if sample and sample["status"] == "reported":
            break
        dispatch = db.execute(
            "SELECT state,last_error_code FROM agent_host_dispatches WHERE idempotency_key=?",
            (trial_key,),
        ).fetchone()
        if dispatch and dispatch["state"] in {
            "outcome_unknown",
            "failed",
            "failed_before_accept",
            "acceptance_unknown",
        }:
            raise AssertionError(
                (
                    "trial-state",
                    dispatch["state"],
                    dispatch["last_error_code"],
                    "running sample",
                    bool(sample),
                )
            )
        time.sleep(0.25)
    assert (
        sample and sample["status"] == "reported" and "尚未完成" in sample["output"]
    ), "No completed sample for the submitted revision and dispatch"
    print(
        json.dumps({"trial": "reported", "boundOutput": True, "storedSamples": 1}),
        flush=True,
    )
    key = "b4-skill-" + str(uuid4())
    wire = send(
        "/api/chat/send-stream",
        {"message": "使用工作日志技能核对待办", "idempotencyKey": key},
    )
    end = time.monotonic() + 60
    while time.monotonic() < end:
        receipt = db.execute(
            """SELECT e.* FROM agent_host_dispatches d JOIN agent_tasks t ON t.user_subject=d.user_subject
            AND t.agent_host=d.agent_host AND t.host_task_key=d.accepted_run_id
            JOIN skill_task_bindings b ON b.task_id=t.task_id AND b.user_subject=t.user_subject
            JOIN skill_load_events e ON e.user_subject=b.user_subject AND e.dedupe_key=b.binding_id
            WHERE d.user_subject=? AND d.idempotency_key=?""",
            ("skill-host-alice", key),
        ).fetchone()
        if receipt:
            break
        time.sleep(0.2)
    assert receipt, "No central skill load receipt for this accepted run"
    fields = json.loads(receipt["payload_json"])
    assert (
        fields["status"] == "succeeded"
        and fields["loaded_resources"] == ["SKILL.md"]
        and fields["version"] == "1.1.2"
    ), fields
    status, _, _ = _request(
        a.port,
        "POST",
        f"/api/skills/history/{receipt['event_id']}/feedback",
        body={
            "rating": "useful",
            "comment": "隔离反馈",
            "requestKey": "b4-check-" + str(uuid4()),
        },
        **auth,
    )
    print(
        json.dumps(
            {
                "skill": fields["skill_id"],
                "version": fields["version"],
                "resourceCount": len(fields["loaded_resources"]),
                "feedbackHttp": status,
                "completedText": ("改选填写模式" in wire),
            }
        ),
        flush=True,
    )
    db.close()
    assert status == 200 and "改选填写模式" in wire


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--home", type=Path, required=True)
    run(p.parse_args().home)
