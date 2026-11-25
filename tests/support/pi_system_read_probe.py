"""Trace reads after private login across four synthetic systems through the real Pi service."""

import argparse
import copy
import json
from pathlib import Path
import sqlite3
import time

from tests.authorization_fixtures import grant_permissions
from tests.support.nanobot_system_login_probe import CASES as EXTRA_SYSTEMS
from tests.support.pi_governed_probe import ORIGIN, exercise
from tests.support.pi_notification_failure import pair_before_run
from tests.support.skill_host_acceptance import SyntheticWorker

SYSTEMS = {
    "oa": ("oa_workflow_pending_list", "oa.workflow.read", "oa.workflow.pending.list"),
    **EXTRA_SYSTEMS,
}


class Model:
    def __init__(self, central):
        self.failures = []
        self.system = ""
        self.tool_id = ""
        self.expired = False
        self.replies = 0
        self.public_status = ""

    def reply(self, payload):
        assert ORIGIN not in json.dumps(payload) and self.system
        names = {t["function"]["name"] for t in payload["tools"]}
        tool = SYSTEMS[self.system][0]
        assert tool in names and self.system + "_session_login" in names
        self.replies += 1
        last = payload["messages"][-1]
        if last["role"] == "tool":
            value = str(last["content"])
            required = "关闭或过期" if self.expired else "synthetic-proof-" + self.system
            if required not in value:
                self.public_status = value[:250]
                raise ValueError("missing_synthetic_read_or_expiry")
            return "核验到" + required, None
        return None, {"id": self.tool_id, "type": "function", "function": {"name": tool, "arguments": "{}"}}


class Systems:
    model = Model

    def __init__(self, level):
        self.level = level

    def check(self, central, post, model, accounts):
        service = central.service
        owner = central.subjects["a"]
        grant_permissions(service.user_grants, owner, [data[1] for data in SYSTEMS.values()])
        pair_before_run(central, post, accounts["a"])
        binding = post("telegram-route", botId="8801", senderId="501").json()["binding"]
        assert post("telegram-route", botId="8801", senderId="503").status_code == 403
        reads = []
        level = post("ready").json()["acceptedLevel"]
        assert level == self.level
        for system, (_, _, capability) in SYSTEMS.items():
            adapter = copy.copy(service.adapter)

            def probe(worker):
                return {
                    "authenticated": True,
                    "observed_principal_ref": worker.session["expected_principal_ref"],
                }

            def read(name, worker, arguments, expected=capability, system=system):
                assert (
                    worker.session["user_subject"] == owner
                    and name == expected
                    and worker.session["system_id"] == system
                )
                reads.append((system, name))
                return {"items": [{"title": "synthetic-proof-" + system}]}

            adapter.probe_session = probe
            adapter.invoke_capability = read
            service._adapters_by_system[system] = adapter
            service._worker_factories_by_system[system] = lambda session, _adapter: SyntheticWorker(session)
        outcomes = []
        for index, system in enumerate(["taihua", "yuque", "smartlight", "oa", "taihua"]):
            expire = index == len(SYSTEMS)
            _, _, cap = SYSTEMS[system]
            session = service.sessions.find(
                user_subject=owner, system_id=system
            ) or service.sessions.get_or_create(
                user_subject=owner, system_id=system, expected_principal_ref=owner
            )
            service.sessions.mark_expired(session["session_id"])
            model.system, model.tool_id, model.expired, model.replies = (
                system,
                "fixture-read-" + str(index),
                expire,
                0,
            )
            before = len(reads)
            with sqlite3.connect(service.db_path) as db:
                highest = db.execute("SELECT COALESCE(MAX(rowid),0) FROM operations").fetchone()[0]
            key = {"binding": binding, "requestKey": model.tool_id}
            run = post("accept", **key, message="请查本人的 " + system + " 合成业务资料。").json()["runId"]
            task = None
            for _ in range(320):
                card = post("card", **key, runId=run).json()
                if card.get("available") and not task:
                    foreign = post("card", binding=accounts["b"], requestKey=key["requestKey"], runId=run)
                    assert card["kind"] == "credential" and (
                        foreign.status_code == 403
                        or foreign.status_code == 200
                        and not foreign.json().get("available")
                    )
                    task = card["interaction"].get("taskId")
                    if level == "L3":
                        assert task, (system, "missing original central task")
                        original = service.tasks.get_task(task, user_subject=owner)
                        assert (original["agent_host"], original["origin_endpoint_id"]) == (
                            "pi",
                            binding["endpoint_id"],
                        )
                    else:
                        assert task is None
                    with sqlite3.connect(service.db_path) as db:
                        challenge, sid = db.execute(
                            "SELECT challenge_id,session_id FROM auth_challenges WHERE user_subject=? AND system_id=? AND state='pending'",
                            (owner, system),
                        ).fetchone()
                        if expire:
                            db.execute(
                                "UPDATE auth_challenges SET expires_at='2000-01-01T00:00:00Z' WHERE challenge_id=?",
                                (challenge,),
                            )
                    if not expire:
                        csrf = service.challenges.issue_csrf(challenge)
                        service.challenges.claim(challenge, csrf_token=csrf, csrf_cookie=csrf)
                        service.sessions.activate(
                            sid, observed_principal_ref=session["expected_principal_ref"]
                        )
                        service.session_states.save(sid, {"cookies": []})
                        service.challenges.complete(
                            challenge, result={"observed_principal_ref": session["expected_principal_ref"]}
                        )
                    # L2 session-ready cards may be scoped to the original run without an agent-task ID.
                    if not task:
                        task = "host-run"
                row = post("lookup", **key).json()
                if row["state"] != "accepted":
                    break
                time.sleep(0.12)
            assert row["state"] == "completed" and row["answer"].startswith("核验到"), (
                system,
                row["state"],
                bool(task),
                model.public_status,
            )
            assert not post("card", **key, runId=run).json().get("available")
            with sqlite3.connect(service.db_path) as db:
                operations = db.execute(
                    """SELECT o.status,t.task_id,t.origin_endpoint_id FROM operations o
                    LEFT JOIN task_operations l ON l.user_subject=o.user_subject AND l.operation_id=o.operation_id
                    LEFT JOIN agent_tasks t ON t.task_id=l.task_id WHERE o.rowid>? AND o.user_subject=? AND o.capability_name=?""",
                    (highest, owner, cap),
                ).fetchall()
            expected = 0 if expire else 1
            assert (
                task
                and reads[before:] == ([(system, cap)] if expected else [])
                and len(operations) == expected
            ), (system, task, reads[before:], operations)
            if operations:
                assert operations[0][0] == "succeeded"
                if level == "L3":
                    assert operations[0][1:] == (task, binding["endpoint_id"])
                else:
                    assert operations[0][1:] == (None, None)
            outcomes.append(
                {
                    "system": system,
                    "expired": expire,
                    "samePiRun": True,
                    "sameTask": bool(operations and operations[0][1] == task),
                    "businessReads": expected,
                    "successfulOperations": len(operations),
                    "modelCalls": model.replies,
                }
            )
        assert (
            not central.matrix.matrix["effects"]
            and not central.matrix.oa.writes
            and not central.matrix.smartlight.data["writes"]
        )
        return {
            "level": level,
            "cases": outcomes,
            "businessWrites": 0,
            "syntheticBroker": True,
            "syntheticBusinessData": True,
            "privateTelegramRoute": True,
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for k in ("node", "sdk-dir", "engine-python"):
        p.add_argument("--" + k, required=True, type=Path)
    p.add_argument("--level", choices=("L2", "L3"), default="L3")
    a = p.parse_args()
    print(
        json.dumps(exercise(a.node, a.sdk_dir, a.engine_python, "systems", scenario=Systems(a.level))),
        flush=True,
    )
