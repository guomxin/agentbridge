"""Real NanoBot SDK and central MCP, three synthetic downstream logins in a private Telegram route."""

import argparse
import json
from pathlib import Path
import sqlite3

from tests.authorization_fixtures import grant_permissions
from tests.support.business_draft_acceptance import OWNER
from tests.support.nanobot_controlled_write_probe import ORIGIN, run


CASES = {
    "taihua": ("taihua_work_log_my_list", "taihua.work_log.my.read", "taihua.work_log.my.list"),
    "yuque": ("yuque_public_books_list", "yuque.content.read", "yuque.public_books.list"),
    "smartlight": ("smartlight_system_overview", "smartlight.asset.read", "smartlight.system.overview"),
}


def exercise(*, fixture, gateway, model, **_):
    service = fixture.service
    grant_permissions(service.user_grants, OWNER, [v[1] for v in CASES.values()])
    private = gateway._request("telegram-route", {"botId": "8001", "senderId": "501"})["binding"]
    reads = []
    original_invoke = service.invoke

    def synthetic_read(**params):
        cap = params["capability_name"]
        if cap not in {item[2] for item in CASES.values()}:
            return original_invoke(**params)
        system = cap.partition(".")[0]
        session = service.sessions.find(user_subject=OWNER, system_id=system)
        assert params["user_subject"] == OWNER and session["state"] == "active"
        reads.append((cap, params["task_id"]))
        return {"protocolVersion": "0.1", "status": "succeeded", "result": {"sample": "fixture-" + system}}

    service.invoke = synthetic_read
    scenarios = [*CASES, "taihua"]
    for index, system in enumerate(scenarios):
        expire = index == len(CASES)
        tool, _, capability = CASES[system]
        # Reuse the fixture's synthetic authentication contract. The public service
        # still creates the real challenge, stores ownership, validates the lease and resumes.
        service._adapters_by_system[system] = service._adapters_by_system["oa"]
        service._worker_factories_by_system[system] = service._worker_factories_by_system["oa"]
        session = service.sessions.find(user_subject=OWNER, system_id=system) or service.sessions.get_or_create(
            user_subject=OWNER, system_id=system, expected_principal_ref=OWNER)
        service.sessions.mark_expired(session["session_id"])
        tool_id = "fixture-credential-" + system + ("-expire" if expire else "")

        def reply(data):
            assert ORIGIN not in json.dumps(data)
            names = {t["function"]["name"] for t in data["tools"]}
            assert "mcp_ab_" + system + "_session_login" in names
            evidence = [m for m in data["messages"] if m.get("tool_call_id") == tool_id]
            if evidence:
                if expire:
                    assert "关闭或过期" in str(evidence[-1]["content"]), "expired card was not reported"
                    return "合成凭证已过期。", None
                assert "fixture-" + system in str(evidence[-1]["content"]), "read did not continue"
                return "已获得本次合成查询结果。", None
            return None, {"id": tool_id, "type": "function", "function": {"name": "mcp_ab_" + tool, "arguments": "{}"}}

        model.reply = reply
        request = {"idempotency_key": tool_id}
        observed_task = None
        for event in gateway.send_dispatch(request, private, "核验本次合成查询 " + system, [], None):
            if event["type"] == "accepted":
                run_id = event["runId"]
            if event.get("kind") != "private_interaction" or event.get("status") != "waiting_private":
                continue
            assert observed_task is None
            card = gateway.private_card(request, private, run_id)
            assert card["available"] and card["kind"] == "credential"
            task_id = card["interaction"]["taskId"]
            assert service.tasks.get_task(task_id, user_subject=OWNER)["origin_endpoint_id"] == private["endpoint_id"]
            assert card["url"].startswith(ORIGIN) and card["url"] not in json.dumps(event)
            observed_task = task_id
            with sqlite3.connect(service.db_path) as db:
                challenge, sid = db.execute(
                    "SELECT challenge_id,session_id FROM auth_challenges WHERE user_subject=? AND system_id=? AND state='pending'",
                    (OWNER, system),
                ).fetchone()
                if expire:
                    db.execute("UPDATE auth_challenges SET expires_at='2000-01-01T00:00:00Z' WHERE challenge_id=?", (challenge,))
                    continue
            csrf = service.challenges.issue_csrf(challenge)
            service.challenges.claim(challenge, csrf_token=csrf, csrf_cookie=csrf)
            service.sessions.activate(sid, observed_principal_ref=session["expected_principal_ref"])
            service.challenges.complete(challenge, result={"observed_principal_ref": session["expected_principal_ref"]})
        assert observed_task and reads.count((capability, observed_task)) == int(not expire), (system, bool(observed_task), len(reads))
    assert len(reads) == 3 and not fixture.matrix["effects"]
    return {"originalTaskReads": len(reads), "systems": sorted(CASES), "expiredWithoutRead": True, "realBusinessWrites": 0, "syntheticDownstreams": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--nanobot-python", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().nanobot_python, False, browser=True, exercise=exercise)))
