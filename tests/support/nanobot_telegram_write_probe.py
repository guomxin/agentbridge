"""Simulated private sender, actual host gateway + SDK/MCP + original Workspace approval."""

import argparse
from pathlib import Path
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.support.business_draft_acceptance import OWNER
from tests.support.nanobot_controlled_write_probe import ORIGIN, run
from tests.test_action_matrix_acceptance import decide, effects


def exercise(*, fixture, binding, instance, gateway, model):
    parsed = gateway._request("telegram-route", {"botId": "8001", "senderId": "501"})
    tg = parsed["binding"]
    assert (
        tg
        and tg["user_subject"] == OWNER
        and tg["endpoint_id"] != binding["endpoint_id"]
    )
    other = gateway._request("telegram-route", {"botId": "8001", "senderId": "777"})
    assert not other["binding"]
    req = {"idempotency_key": "synthetic-telegram-private-write"}
    request = "请帮我保存请假草稿，使用我的工作台核验。"
    decided = False
    for event in gateway.send_dispatch(req, tg, request, [], None):
        assert ORIGIN not in json.dumps(event)
        if event["type"] == "accepted":
            run_id = event["runId"]
        if (
            event.get("kind") != "private_interaction"
            or event.get("status") != "waiting_private"
            or decided
        ):
            continue
        decided = True
        tg_card = gateway.private_card(req, tg, run_id)
        assert (
            tg_card["available"]
            and tg_card["url"].startswith(ORIGIN + "/")
            and (tg_card.get("interaction") or {}).get("type") == "business_input"
        )
        with sqlite3.connect(fixture.service.db_path) as db:
            id_, resource = db.execute(
                "SELECT interaction_id,resource_id FROM interactions WHERE user_subject=? AND interaction_type='business_input' ORDER BY rowid DESC LIMIT 1",
                (OWNER,),
            ).fetchone()
        view = fixture.service.present_interaction(
            user_subject=OWNER,
            agent_host="nanobot",
            endpoint_key=binding["endpoint_key"],
            interaction_id=id_,
        )
        task = view["interaction"]["taskId"]
        assert (
            view["endpoint"]["endpointId"] == binding["endpoint_id"]
            and task
            and effects(fixture) == 0
        )
        entry = fixture.service.tasks.get_task(task, user_subject=OWNER)
        assert entry["origin_endpoint_id"] == tg["endpoint_id"]
        assert decide(fixture, {"input_id": resource, "capability": "oa.leave.prepare"})
    assert model.results and "已核验" in model.results[-1] and effects(fixture) == 1
    return {
        "telegram_input": "synthetic private sender",
        "workspace_approved": True,
        "original_telegram_endpoint": True,
        "telegram_private_card_available": True,
        "link_in_telegram_messages": False,
        "central_verified": True,
        "effects": 1,
        "real_bot": False,
        "real_business": False,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--nanobot-python", type=Path, required=True)
    print(
        json.dumps(
            run(p.parse_args().nanobot_python, False, browser=True, exercise=exercise)
        )
    )
