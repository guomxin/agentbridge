from tests.authorization_fixtures import authorized_service
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.capability_runtime import RequiresUserAction
from agentbridge.core.login_continuation import read_continuation_message


def service_at(home):
    return authorized_service(home=home, base_url="http://oa.test/seeyon")


def logged_in_query(home, *, capability="oa.workflow.pending.list", arguments=None, client_type="web"):
    service = service_at(home)
    task = service.ensure_host_task(
        user_subject="user-a", token_id="token-a", agent_host="openclaw",
        host_task_key="login-read", endpoint_key="origin", client_type=client_type,
        external_subject="alice", conversation_ref="private:alice", title="查看OA待办",
        capabilities=["direct_status", "timeline_message"],
    )["task"]
    task_id = task["taskId"]
    original = service.invoke(
        user_subject="user-a", capability_name=capability, arguments=arguments or {},
        task_id=task_id,
    )
    assert original["error"]["code"] == "LOGIN_REQUIRED"
    login = service.start_login(
        user_subject="user-a", expected_principal_ref="Alice",
        card_base_url="http://127.0.0.1:8780",
    )
    interaction_id = login["interaction"]["interactionId"]
    service.observe_host_task(user_subject="user-a", task_id=task_id, interaction_ids=[interaction_id])
    challenge_id = login["challenge"]["challengeId"]
    csrf = service.challenges.issue_csrf(challenge_id)
    service.challenges.claim(challenge_id, csrf_token=csrf, csrf_cookie=csrf)
    service.challenges.complete(challenge_id, result={"principal": "Alice"})
    session = service.sessions.find(user_subject="user-a", system_id="oa")
    service.sessions.activate(session["session_id"], observed_principal_ref="Alice")
    return service, task_id, interaction_id, original


def messages(service):
    return [entry for entry in service.tasks.list_timeline(user_subject="user-a")
            if entry["entry_type"] == "chat_message"]


@pytest.mark.parametrize("collection", ["pending", "done", "sent", "tracked"])
def test_resume_uses_persisted_exact_read_after_restart(tmp_path, collection):
    arguments = {"keyword": "申请", "limit": 100}
    if collection in {"done", "sent"}:
        arguments.update(start_date="2026-09-01", end_date="2026-09-02", limit=1000)
    _, task_id, interaction_id, original = logged_in_query(
        tmp_path, capability=f"oa.workflow.{collection}.list", arguments=arguments,
    )
    service = service_at(tmp_path)
    service._invoke_adapter = MagicMock(return_value={
        "collection": collection, "items": [{"title": "测试申请", "sender": "Alice"}],
    })
    response = service.resume_interaction(user_subject="user-a", interaction_id=interaction_id)
    assert response["status"] == "succeeded"
    assert response["nextAction"]["type"] == "original_request_completed"
    assert response["taskStatus"] == "succeeded"
    assert service._invoke_adapter.call_args.kwargs["arguments"] == arguments
    assert service.tasks.get_task(task_id, user_subject="user-a")["status"] == "succeeded"
    assert len(messages(service)) == 1
    assert "测试申请" in messages(service)[0]["text"]
    assert service.operations.get(original["operationId"])["status"] == "requires_user_action"
    again = service.resume_interaction(user_subject="user-a", interaction_id=interaction_id)
    assert again["status"] == "ignored"
    assert service._invoke_adapter.call_count == 1
    assert len(messages(service)) == 1


def test_concurrent_resume_deduplicates_read_and_origin_notification(tmp_path):
    service, task_id, interaction_id, _ = logged_in_query(tmp_path, client_type="telegram")
    other_endpoint, _ = service.tasks.ensure_endpoint(
        user_subject="user-b", token_id="token-b", agent_host="openclaw",
        endpoint_key="other", client_type="telegram", external_subject="bob", conversation_ref="private:bob",
    )
    service._invoke_adapter = MagicMock(return_value={"collection": "pending", "items": []})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: service.resume_interaction(
            user_subject="user-a", interaction_id=interaction_id), range(2)))
    assert sorted(item["status"] for item in responses) == ["ignored", "succeeded"]
    assert service._invoke_adapter.call_count == 1
    assert len(messages(service)) == 1
    task = service.tasks.get_task(task_id, user_subject="user-a")
    delivered = service.tasks.list_outbox(user_subject="user-a", endpoint_id=task["origin_endpoint_id"])
    assert len([item for item in delivered if item["payload_type"] == "timeline_message"]) == 1
    assert service.tasks.list_outbox(user_subject="user-b", endpoint_id=other_endpoint["endpoint_id"]) == []


def test_crash_after_feedback_before_task_close_recovers_without_repeating_read(tmp_path):
    service, task_id, interaction_id, _ = logged_in_query(tmp_path)
    service._invoke_adapter = MagicMock(return_value={"collection": "pending", "items": []})
    with patch.object(service, "observe_host_task", side_effect=RuntimeError("runtime stopped")):
        with pytest.raises(RuntimeError, match="runtime stopped"):
            service.resume_interaction(user_subject="user-a", interaction_id=interaction_id)
    restarted = service_at(tmp_path)
    restarted._invoke_adapter = MagicMock(side_effect=AssertionError("must reuse operation"))
    response = restarted.resume_interaction(user_subject="user-a", interaction_id=interaction_id)
    assert response["status"] == "succeeded"
    assert response["reused"] is True
    assert len(messages(restarted)) == 1
    assert restarted.tasks.get_task(task_id, user_subject="user-a")["status"] == "succeeded"
    restarted._invoke_adapter.assert_not_called()


@pytest.mark.parametrize("error", [RuntimeError("network failed"), RequiresUserAction(
    "LOGIN_REQUIRED", "expired again", next_action={"type": "login"},
)])
def test_read_failure_gets_durable_feedback_and_terminal_task(tmp_path, error):
    service, task_id, interaction_id, _ = logged_in_query(tmp_path)
    service._invoke_adapter = MagicMock(side_effect=error)
    response = service.resume_interaction(user_subject="user-a", interaction_id=interaction_id)
    assert response["status"] != "succeeded"
    assert service.tasks.get_task(task_id, user_subject="user-a")["status"] == "failed"
    assert "原查询未完成" in messages(service)[0]["text"]
    service.resume_interaction(user_subject="user-a", interaction_id=interaction_id)
    assert service._invoke_adapter.call_count == 1


def test_other_user_and_canceled_task_cannot_resume(tmp_path):
    service, task_id, interaction_id, _ = logged_in_query(tmp_path)
    service._invoke_adapter = MagicMock(side_effect=AssertionError("must not execute"))
    with pytest.raises(KeyError):
        service.resume_interaction(user_subject="user-b", interaction_id=interaction_id)
    service.fail_host_task(user_subject="user-a", task_id=task_id, error_code="CANCELED", message="stopped")
    assert service.resume_interaction(user_subject="user-a", interaction_id=interaction_id)["status"] == "ignored"
    assert messages(service) == []
    service._invoke_adapter.assert_not_called()


def test_write_inputs_are_not_replayed_from_ledger(tmp_path):
    service, _, interaction_id, _ = logged_in_query(
        tmp_path, capability="oa.business_trip.prepare", arguments={},
    )
    service._invoke_adapter = MagicMock(side_effect=AssertionError("must not execute"))
    response = service.resume_interaction(user_subject="user-a", interaction_id=interaction_id)
    assert response["nextAction"]["type"] == "retry_original_request"
    service._invoke_adapter.assert_not_called()


def test_resume_preserves_original_fine_grained_read_scope(tmp_path):
    service, _, interaction_id, _ = logged_in_query(
        tmp_path, capability="oa.addressbook.person.search", arguments={"query": "Alice"},
    )
    assert service.interaction_required_scopes(user_subject="user-a", interaction_id=interaction_id) == {
        "oa:read", "oa:read:addressbook",
    }


def test_large_read_message_is_bounded_and_marks_partial_coverage():
    message = read_continuation_message("OA", "oa.workflow.done.list", {
        "status": "succeeded", "result": {"collection": "done", "coverage": {"status": "partial"},
        "items": [{"title": "长标题" * 300} for _ in range(1000)]},
    })
    assert len(message) < 3500
    assert "1000 条" in message and "未覆盖全部" in message and "未在本条消息展开" in message
