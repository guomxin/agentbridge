"""Only independently verifiable facts cross the original private-interaction boundary."""
from uuid import uuid4

import pytest

from tests.support.latency_acceptance import OWNER
from tests.test_workspace_query_projection import openclaw  # noqa: F401
from tests.test_workspace_text_delivery import admit, control, text_claim


@pytest.mark.parametrize(("state", "label", "same_conversation"), [
    (None, "", True),
    ("succeeded", "原任务状态为已完成。", True),
    ("failed", "原任务状态为失败。", True),
    ("outcome_unknown", "原任务状态为结果尚未确认。", True),
    ("failed", "", False),
])
def test_private_response_uses_only_the_same_tasks_public_state(openclaw, state, label, same_conversation):
    fixture, token, endpoint = openclaw
    service = fixture.service
    dispatch = admit(fixture)
    run = dispatch["idempotency_key"]
    source = dispatch["origin_endpoint_id"]
    with service.tasks._connect() as db:
        claim = db.execute("SELECT claim_token FROM agent_host_dispatches WHERE dispatch_id=?",
                           (dispatch["dispatch_id"],)).fetchone()[0]
    service.workspace.append_host_dispatch_stream_event(dispatch["dispatch_id"], claim_token=claim,
        event_name="host_progress", payload={"kind": "private_interaction", "status": "waiting_private",
                                             "cardRevision": 1, "runId": run})
    if state:
        conversation = dispatch["conversation_ref"] if same_conversation else str(uuid4())
        task, _ = service.tasks.ensure_task(user_subject=OWNER, agent_host="openclaw", host_task_key=run,
            origin_endpoint_id=source, active_conversation_ref=conversation, title="私有事项")
        with service.tasks._connect() as db:
            db.execute("UPDATE agent_tasks SET status=? WHERE task_id=?", (state, task["task_id"]))
    question, = text_claim(service, endpoint, token)
    control(service, endpoint, token, question, "issue")
    assert control(service, endpoint, token, question, "begin")["allowed"]
    assert control(service, endpoint, token, question, "ack", outcome="sent", receipt=101)["recorded"]
    for _ in range(2):
        service.tasks.append_workspace_response(user_subject=OWNER, source_endpoint_id=source,
            message_key="workspace:assistant:" + run, text="私人字段示例。请访问 /results/synthetic-for-projection")
    item, = text_claim(service, endpoint, token)
    text = control(service, endpoint, token, item, "issue")["publicText"]
    assert text == f"工作台回复\n工作台已收到本次运行的回复。{label}请在原会话查看可信交互和业务详情。"
    assert control(service, endpoint, token, item, "begin")["allowed"]
    assert control(service, endpoint, token, item, "ack", outcome="sent", receipt=102)["recorded"]
    assert not text_claim(service, endpoint, token)
    with service.tasks._connect() as db:
        assert not db.execute("SELECT 1 FROM operations").fetchone()
