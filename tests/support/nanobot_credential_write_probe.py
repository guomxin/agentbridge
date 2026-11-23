"""An expired personal session must produce a private login, then the original draft, with distinct decisions."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.support.nanobot_controlled_write_probe import run
from tests.test_action_matrix_acceptance import preview_case


def exercise(*, fixture, binding, instance, gateway, model):
    service = fixture.service
    subject = binding["user_subject"]
    old = service.sessions.find(user_subject=subject, system_id="oa")
    service.sessions.mark_expired(old["session_id"])
    key = {"idempotency_key": "original-login-draft"}
    stream = gateway.send_dispatch(
        key, binding, "请保存一份合成请假草稿，登录后继续。", [], None
    )
    rid = next(stream)["runId"]
    seen = []
    revision = []
    for event in stream:
        if (
            event.get("kind") != "private_interaction"
            or event.get("status") != "waiting_private"
        ):
            continue
        card = gateway.private_card(key, binding, rid)
        if not card["available"] or len(seen) == 2:
            continue
        assert card["cardRevision"] == event["cardRevision"]
        seen.append(card["kind"])
        revision.append(card["cardRevision"])
        if seen == ["credential"]:
            with sqlite3.connect(service.db_path) as db:
                cid, sid = db.execute(
                    "SELECT challenge_id,session_id FROM auth_challenges WHERE user_subject=? ORDER BY rowid DESC LIMIT 1",
                    (subject,),
                ).fetchone()
            # Two authenticated, same-owner endpoint views of the one private central form.
            peer = "contract-h05-peer"
            account = fixture.account["account_id"]
            service.tasks.ensure_endpoint(
                user_subject=subject,
                token_id=old.get("token_id") or "h05-fixture-token",
                agent_host="nanobot",
                endpoint_key=peer,
                client_type="web",
                external_subject=account,
                account_id=account,
                conversation_ref=peer,
            )
            with sqlite3.connect(service.db_path) as db:
                interaction = db.execute(
                    "SELECT interaction_id FROM interactions WHERE user_subject=? AND interaction_type='credential' ORDER BY rowid DESC LIMIT 1",
                    (subject,),
                ).fetchone()[0]
            presented = [
                service.present_interaction(
                    user_subject=subject,
                    agent_host="nanobot",
                    endpoint_key=e,
                    interaction_id=interaction,
                )
                for e in (binding["endpoint_key"], peer)
            ]
            assert all(
                row["interaction"]["interactionId"] == interaction
                and row["interaction"]["presentation"].get("url")
                for row in presented
            )
            assert len({row["endpoint"]["endpointId"] for row in presented}) == 2
            csrf = service.challenges.issue_csrf(cid)
            service.challenges.claim(cid, csrf_token=csrf, csrf_cookie=csrf)
            service.sessions.activate(sid, observed_principal_ref=subject)
            service.challenges.complete(cid, result={"observed_principal_ref": subject})
            try:
                service.challenges.claim(cid, csrf_token=csrf, csrf_cookie=csrf)
            except RuntimeError:
                pass
            else:
                raise AssertionError("same challenge claimed twice")
            assert not fixture.matrix["effects"]
        elif seen == ["credential", "business_input"]:
            with sqlite3.connect(service.db_path) as db:
                fid = db.execute(
                    "SELECT submission_id FROM field_submissions WHERE user_subject=? ORDER BY rowid DESC LIMIT 1",
                    (subject,),
                ).fetchone()[0]
            drafts, creds, state = preview_case(fixture, {"input_id": fid})
            assert not fixture.matrix["effects"]
            drafts.approve(
                **creds,
                expected_revision=state["revision"],
                preview_id=state["preview"]["previewId"],
                request_key="independent-final-draft-decision",
            )
        else:
            raise AssertionError("Unexpected card sequence")
    assert (
        seen == ["credential", "business_input"]
        and revision == [1, 2]
        and len(fixture.matrix["effects"]) == 1
    ), ("card_sequence", seen, revision)
    assert len(model.results) == 1 and "已核验" in model.results[0]
    return {
        "two_owned_endpoints": True,
        "credential_single_consumption": True,
        "login_then_original_field": True,
        "independent_draft_authorization": True,
        "card_revisions": revision,
        "verified_writes": 1,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--nanobot-python", required=True, type=Path)
    print(json.dumps(run(p.parse_args().nanobot_python, False, exercise=exercise)))
