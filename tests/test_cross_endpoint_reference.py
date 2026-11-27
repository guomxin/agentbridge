from datetime import timedelta

import pytest

from agentbridge.core import actionable_results as actions
from agentbridge.core.cross_endpoint_reference import resolve
from tests.support.latency_acceptance import OWNER
from tests.test_workspace_query_projection import openclaw  # noqa: F401
from tests.test_workspace_text_delivery import admit, control, text_claim


def call(fixture, original_token, key, ref, **changes):
    return resolve(fixture.service, **{**dict(subject=OWNER, host="openclaw", instance="openclaw-gateway",
                   token=original_token, endpoint_key=key, reference=ref), **changes})


def test_select_only_exact_current_receipted_workspace_message(openclaw):
    fixture, token, tg = openclaw
    service = fixture.service
    endpoint = tg["endpoint_key"]
    admit(fixture)
    one, = text_claim(service, tg, token)
    assert call(fixture, token, endpoint, {})["decision"] == "unavailable"
    for action in ("issue", "begin"):
        control(service, tg, token, one, action)
    control(service, tg, token, one, "ack", outcome="sent", receipt=7)
    expected = call(fixture, token, endpoint, {"telegramReceipt": 7})
    assert expected["decision"] == "selected" and expected["allowNewOperation"] is False
    entry, = expected["entries"]
    assert entry["entryId"] == one["sourceEntryId"] and entry["text"] == "请列出我的待办记录"
    for ref in ({}, {"entryId": entry["entryId"]}):
        assert call(fixture, token, endpoint, ref) == expected
    for changes in ({"subject": "someone-else"}, {"instance":"a-different-instance"}, {"token":"another-token"}):
        assert call(fixture, token, endpoint, {"telegramReceipt":7}, **changes)["decision"] == "unavailable"
    with service.tasks._connect() as db:
        db.execute("UPDATE user_timeline SET created_at=? WHERE entry_id=?",
                   ((actions._now()-timedelta(days=2)).isoformat(),entry["entryId"]))
    assert call(fixture, token, endpoint, {"telegramReceipt":7})["decision"] == "unavailable"


def test_telegram_projection_identity_age_and_explicit_choice(openclaw):
    fixture, token, tg = openclaw
    service = fixture.service
    web = fixture.account["endpoint_key"]
    body = dict(user_subject=OWNER, token_id=token, agent_host="openclaw", host_instance_id="openclaw-gateway",
        endpoint_key=tg["endpoint_key"], client_type="telegram", external_subject="501",
        account_id=None, conversation_ref=tg["conversation_ref"], route={"channel":"telegram","to":"501"},
        role="user", projection_v2=True)
    ids = [service.append_host_timeline_message(**body,message_key=f"tg:explicit:{i}",text=text)["entry"]["entryId"]
           for i,text in enumerate(("第一条公开消息", "另一条公开消息"))]
    choices = call(fixture, token, web,{})
    assert choices["decision"] == "ambiguous" and {e["entryId"] for e in choices["entries"]} == set(ids)
    one = call(fixture, token, web,{"entryId":ids[0]})
    assert one["decision"] == "selected" and one["entries"][0]["text"] == "第一条公开消息"
    assert call(fixture, token, web,{"telegramReceipt":7})["decision"] == "unavailable"
    with service.tasks._connect() as db:
        assert not db.execute("SELECT 1 FROM operations UNION ALL SELECT 1 FROM task_continuations").fetchone()
        db.execute("UPDATE user_timeline SET text='不同原文' WHERE entry_id=?", (ids[0],))
        db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?", (tg["endpoint_id"],))
    assert call(fixture,token,web,{"entryId":ids[0]})["decision"] == "unavailable"
    assert call(fixture,token,web,{"entryId":ids[1]})["decision"] == "unavailable"


@pytest.mark.parametrize("bad",[{"telegramReceipt":True},{"unknown":"ANY"},{"taskId":"x","entryId":"y"},None])
def test_invalid_reference_is_not_treated_as_recent_history(openclaw,bad):
    fixture, token, tg = openclaw
    with pytest.raises(ValueError):
        call(fixture,token,tg["endpoint_key"],bad)
