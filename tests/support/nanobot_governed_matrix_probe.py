"""Exercise several central prepare/authorization/verifier pairs through the actual NanoBot SDK."""

import argparse
import json
from pathlib import Path
import secrets
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.support.nanobot_controlled_write_probe import run
from tests.support.business_draft_acceptance import OWNER
from tests.test_action_matrix_acceptance import decide, effects
from tests.support.action_matrix_acceptance import FIELDLESS


def scenario(capability):
    def exercise(*, fixture, binding, instance, gateway, model):
        name = capability.replace(".", "_")
        target = "mcp_ab_" + name
        call_id = "governed-" + secrets.token_hex(8)
        args = {}
        if capability in {
            "oa.meeting.create.prepare",
            "oa.meeting_room.application.prepare",
        }:
            args = {"start_time": "2026-10-21 10:00", "end_time": "2026-10-21 11:00"}
        if capability.startswith("smartlight."):
            args = {"alarm_id": "synthetic-alarm-matrix"}
            fixture.smartlight.add_alarm(args["alarm_id"], submitted=False)
        received = []

        def reply(data):
            names = {t["function"]["name"] for t in data["tools"]}
            assert target in names, (target, "missing")
            tool = [
                m
                for m in data["messages"]
                if m.get("role") == "tool" and m.get("tool_call_id") == call_id
            ]
            if not tool:
                return None, {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": target, "arguments": json.dumps(args)},
                }
            received.append(tool[-1]["content"])
            return received[-1], None

        model.reply = reply
        dispatch = {"idempotency_key": "matrix-" + name}
        decision = None
        for event in gateway.send_dispatch(
            dispatch, binding, "请办理本次合成测试事项并在确认后核验。", [], None
        ):
            if event["type"] == "accepted":
                run_id = event["runId"]
            if (
                event.get("kind") != "private_interaction"
                or event.get("status") != "waiting_private"
                or decision is not None
            ):
                continue
            card = gateway.private_card(dispatch, binding, run_id)
            assert card["available"] and effects(fixture) == 0
            with sqlite3.connect(fixture.service.db_path) as db:
                if capability in FIELDLESS:
                    identifier = db.execute(
                        "SELECT authorization_id FROM write_authorizations WHERE user_subject=? ORDER BY rowid DESC LIMIT 1",
                        (OWNER,),
                    ).fetchone()[0]
                    case = {"capability": capability, "authorization_id": identifier}
                else:
                    identifier = db.execute(
                        "SELECT submission_id FROM field_submissions WHERE user_subject=? AND capability_name=? ORDER BY rowid DESC LIMIT 1",
                        (OWNER, capability),
                    ).fetchone()[0]
                    case = {"capability": capability, "input_id": identifier}
            decision = decide(fixture, case)
            assert decision
        assert len(received) == 1 and "已核验" in received[0], (
            name,
            received,
            model.failures,
        )
        assert effects(fixture) == 1
        with sqlite3.connect(fixture.service.db_path) as db:
            signals = db.execute(
                "SELECT COUNT(*) FROM agent_host_runtime_snapshots WHERE user_subject=? AND host_instance_id=? AND host_version='0.3.2' AND status='healthy'",
                (OWNER, instance),
            ).fetchone()[0]
            assert signals
        return {
            "prepare": capability,
            "effects": 1,
            "central_verified": True,
            "real_runtime_signals": signals,
            "real_business": False,
        }

    return exercise


if __name__ == "__main__":
    opts = argparse.ArgumentParser()
    opts.add_argument("--nanobot-python", type=Path, required=True)
    opts.add_argument("capability")
    args = opts.parse_args()
    print(
        json.dumps(run(args.nanobot_python, False, exercise=scenario(args.capability)))
    )
