"""Persist public dispatch outcomes with the original question's transaction.

Host error text is not a public status. A dispatch identity, rather than an
assistant message key or the current pairing, owns this projection.
"""
from hashlib import sha256
import json
from uuid import uuid4

from agentbridge.core import workspace_text_delivery
from agentbridge.core.business_skills import _json
from agentbridge.core.commit_notifications import mark_transaction_change


TEXT = {
    "failed_before_accept": "智能体未接收本次请求，已安全中止，尚未进入业务系统。请检查连接后重试。",
    "expired": "连接暂未恢复，本次请求未进入业务系统，请稍后重试。",
    "acceptance_unknown": "无法确认智能体是否已接收本次请求，系统已停止重发。请核对原请求状态后再决定是否重试。",
    "failed": "智能体运行未能完成。系统不会自动重发；如已发起业务操作，请先在原任务中核对业务状态。",
    "stopped": "已停止生成回复。停止生成不代表撤销已发起的业务操作，请在原任务中核对业务状态。",
    "canceled": "已取消等待，本次请求未进入业务系统。",
    "result_unknown": "请求已接收，但最终结果未知。这不代表后台任务失败；请核对原任务的业务状态，系统不会自动重发。",
}


def _trial_metadata(db, dispatch, question, snapshot):
    """Read the same exact saved-sample receipt while the terminal is committed."""
    expected = {"op": "test", "draft_id": snapshot["draft_id"], "revision": snapshot["expected_revision"],
                "profile": snapshot["profile"], "prompt": question["text"]}
    sample = None
    command = db.execute("SELECT * FROM skill_authoring_commands WHERE owner_subject=? AND request_key=?",
                         (dispatch["user_subject"], "workspace-sample:" + dispatch["dispatch_id"])).fetchone()
    if command and command["input_hash"] == sha256(_json(expected).encode()).hexdigest():
        test_id = json.loads(command["result_json"]).get("test_id")
        sample = db.execute("SELECT * FROM skill_draft_tests WHERE test_id=? AND owner_subject=?",
                            (test_id, dispatch["user_subject"])).fetchone()
        if sample and any(sample[key] != value for key, value in expected.items() if key != "op"):
            sample = None
    return {"draft_id": snapshot["draft_id"], "name": snapshot.get("name") or snapshot["draft_id"],
            "revision": snapshot["expected_revision"], "profile": snapshot["profile"],
            "dispatch_id": dispatch["dispatch_id"], "test_id": sample["test_id"] if sample else None,
            "status": "reported" if sample and sample["status"] == "reported" and sample["output"] else "not_reported"}


def record(db, dispatch, state, event, now):
    if state not in TEXT:
        return event
    if not db.in_transaction:
        raise RuntimeError("dispatch terminal projection requires an active transaction")
    run_id = dispatch["accepted_run_id"] or dispatch["idempotency_key"]
    public = {"type": "chat", "runId": run_id,
              "state": "aborted" if state in {"stopped", "canceled"} else "error",
              "text": TEXT[state], "safeToRetry": state in {"failed_before_accept", "expired", "canceled"},
              "hadToolActivity": bool(dispatch["had_tool_activity"] or event.get("hadToolActivity") is True)}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_timeline'").fetchone():
        return public
    question = db.execute("""SELECT * FROM user_timeline WHERE user_subject=? AND source_endpoint_id=?
        AND dedupe_key='message:'||? AND entry_type='chat_message' AND role='user'""",
        (dispatch["user_subject"], dispatch["origin_endpoint_id"], dispatch["message_key"])).fetchone()
    if question is None:
        return public
    metadata = json.loads(question["payload_json"])
    payload = {"workspaceDispatch": {"dispatchId": dispatch["dispatch_id"], "questionEntryId": question["entry_id"],
                                     "state": state, "runId": run_id}}
    trial = metadata.get("skillTrial")
    if trial:
        # Trial diagnostics stay on their original Workspace. Host-provided
        # skillTrial fields are never accepted as evidence of a saved sample.
        payload["skillTrial"] = public["skillTrial"] = _trial_metadata(db, dispatch, question, trial)
        supplied = event.get("text")
        if isinstance(supplied, str) and supplied.strip():
            public["text"] = supplied.replace("\0", "").strip()[:200_000]
    db.execute("""INSERT OR IGNORE INTO user_timeline(entry_id,user_subject,entry_type,dedupe_key,
        source_endpoint_id,task_id,role,text,payload_json,created_at)
        VALUES(?,?,'chat_message',?,?,NULL,'assistant',?,?,?)""",
        (str(uuid4()), dispatch["user_subject"], "message:workspace:assistant:error:" + dispatch["dispatch_id"],
         dispatch["origin_endpoint_id"], public["text"], _json(payload), now))
    # Only the original admission pin can authorize a status delivery. Never
    # enqueue this entry through the legacy timeline mirror as well.
    if not question["task_id"] and not (set(metadata) - {"attachments"}):
        workspace_text_delivery.enqueue_status(db, dispatch, state)
    mark_transaction_change(db, user_subject=dispatch["user_subject"])
    return public
