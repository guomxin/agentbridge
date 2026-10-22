"""The source business category bounds every shared pending-selection entry."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentbridge.adapters.seeyon_pending_batch import (
    PendingBatchSelectionError, select_pending_batch_items,
)


def row(affair_id="selected", title="合成事项", **extra):
    return {"affair_id": affair_id, "title": title, "category": "协同", **extra}


def pending(*rows):
    return {"items": list(rows), "coverage": {"status": "complete"}}


@pytest.fixture
def registry():
    return SimpleNamespace(get=Mock(return_value=SimpleNamespace(version="0.1.0")))


@pytest.mark.parametrize("category", [
    "会议邀请", "Meeting", "公文", "公告", "未知类型", "审批", "表单", "协同/会议",
    False, 0, [], {},
])
def test_explicit_other_business_category_never_acquires_generic_collaboration(category, registry):
    with pytest.raises(PendingBatchSelectionError) as caught:
        select_pending_batch_items(pending(row(category=category)), {}, registry)
    assert caught.value.code == "BATCH_UNSUPPORTED_ITEMS"
    registry.get.assert_not_called()


@pytest.mark.parametrize("title", [
    "合成进度汇报", "合成效能数据", "【HR】补签申请单-合成申请", "周报发送流程-合成周报",
    "【知识产权】知识产权奖励审批单", "【绩效】部门季度绩效考核汇总-合成部门",
])
def test_meeting_cannot_impersonate_a_registered_title(title, registry):
    selected = select_pending_batch_items(pending(row(title=title, category="会议")), {}, registry)
    assert selected[0]["display_summary"]["profile"] == "meeting_reply"
    assert selected[0]["display_summary"]["commit_capability"] == "oa.meeting.reply"



@pytest.mark.parametrize("category", ["协同", " 协同 ", "Collaboration", "COLLABORATION"])
def test_verified_collaboration_category_uses_existing_routing(category, registry):
    # English Collaboration is already covered by the OA pending parser tests.
    selected = select_pending_batch_items(pending(row(category=category)), {}, registry)
    assert selected[0]["display_summary"]["profile"] == "standard_collaboration"
    assert selected[0]["display_summary"]["category"] == category


@pytest.mark.parametrize("category", [None, "", " \t\n"])
@pytest.mark.parametrize("title,profile", [("合成事项", "standard_collaboration"),
                                          ("合成效能数据", "efficiency_data")])
def test_empty_legacy_category_retains_routing_and_its_existing_preflight(category, title, profile, registry):
    selected = select_pending_batch_items(pending(row(category=category, title=title)), {}, registry)
    assert selected[0]["display_summary"]["profile"] == profile


def test_omitted_legacy_category_and_meeting_word_in_title_do_not_invent_a_business_type(registry):
    legacy = row(title="合成会议准备材料")
    legacy.pop("category")
    assert select_pending_batch_items(pending(legacy), {}, registry)[0]["display_summary"]["profile"] == "standard_collaboration"
    verified = row(title="合成会议准备材料", category="协同")
    assert select_pending_batch_items(pending(verified), {}, registry)[0]["display_summary"]["profile"] == "standard_collaboration"


@pytest.mark.parametrize("arguments", [
    {"affair_ids": ["meeting"]},
    {"affair_ids": ["meeting"], "workflow_types": ["standard_collaboration"]},
    {"affair_ids": ["meeting"], "workflow_types": ["weekly_report"]},
    {"affair_ids": ["meeting"], "keyword": "另一个范围"},
    {"affair_ids": ["valid", "meeting"], "workflow_types": ["weekly_report"]},
])
def test_explicit_unsupported_target_cannot_be_hidden_by_additional_filters(arguments, registry):
    source = pending(row("valid"), row("meeting", category="公文"))
    with pytest.raises(PendingBatchSelectionError) as caught:
        select_pending_batch_items(source, arguments, registry)
    assert caught.value.code == "BATCH_UNSUPPORTED_ITEMS"
    registry.get.assert_not_called()


@pytest.mark.parametrize("arguments", [{}, {"workflow_types": ["standard_collaboration"]}, {"keyword": "合成"}])
def test_in_scope_mixed_category_selection_stops_instead_of_processing_only_supported_subset(arguments, registry):
    source = pending(row("valid"), row("meeting", category="公文"))
    with pytest.raises(PendingBatchSelectionError) as caught:
        select_pending_batch_items(source, arguments, registry)
    assert caught.value.code == "BATCH_UNSUPPORTED_ITEMS"


def test_explicit_supported_subset_and_filters_may_exclude_unrequested_categories(registry):
    source = pending(row("valid", title="合成效能数据"), row("meeting", title="另一个范围", category="公文"))
    for arguments in ({"affair_ids": ["valid"]}, {"workflow_types": ["efficiency_data"]}, {"keyword": "效能数据"}):
        selected = select_pending_batch_items(source, arguments, registry)
        assert [item["resource_ref"] for item in selected] == ["valid"]


@pytest.mark.parametrize("surface", ["workspace", "telegram"])
def test_shared_query_choices_disable_unsupported_categories_and_reject_forged_selection_without_preparing(tmp_path, surface):
    from agentbridge.core import actionable_results as actions
    from agentbridge.core.mcp_identities import McpIdentityTokenStore
    from agentbridge.core.telegram_result_views import ResultViewService
    from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract, OWNER

    fixture = LatencyAcceptanceFixture(tmp_path, seed=False,
        latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
    try:
        service = fixture.service
        fixture.oa.add_affair("synthetic-meeting", profile="standard_collaboration")
        if surface == "workspace":
            source = fixture.add_query_case(case_key=uuid4().hex)
        else:
            token = McpIdentityTokenStore(service.db_path).issue(user_subject=OWNER, expected_principal_ref=OWNER, ttl_seconds=7200)
            session = "agent:main:telegram:direct:1001"
            endpoint, _ = service.tasks.ensure_endpoint(user_subject=OWNER, token_id=token["token_id"],
                agent_host="openclaw", endpoint_key="telegram:default:1001", client_type="telegram", external_subject="1001",
                conversation_ref=session, capabilities=["trusted_interaction", "continuous_flow_v1"],
                route={"channel": "telegram", "to": "1001"})
            service.skills.save("user:" + OWNER, {"oa-pending": {"profiles": ["single", "batch"]}},
                expected_revision=0, actor="synthetic", reason="Synthetic Telegram category check")
            task, _ = service.tasks.ensure_task(user_subject=OWNER, agent_host="openclaw", host_task_key=uuid4().hex,
                origin_endpoint_id=endpoint["endpoint_id"], active_conversation_ref=session, title="合成待办查询")
            result = service.invoke(user_subject=OWNER, capability_name="oa.workflow.pending.list", arguments={},
                task_id=task["task_id"], idempotency_key=uuid4().hex)
            assert result["status"] == "succeeded"
            service.finish_host_task(user_subject=OWNER, task_id=task["task_id"], outcome="succeeded", reason="synthetic_query_completed")
            source = {"task_id": task["task_id"], "operation_id": result["operationId"]}
        operation = service.operations.get(source["operation_id"])
        projection = deepcopy(operation["result"])
        next(item for item in projection["items"] if item["affair_id"] == "synthetic-meeting")["category"] = "公文"
        with service.tasks._connect() as connection:
            connection.execute("UPDATE operations SET result_json=? WHERE operation_id=?", (actions._json(projection), source["operation_id"]))
        if surface == "workspace":
            group = actions.list_actionable_results(service, OWNER, source["task_id"])[0]
            queue = lambda body: actions.queue_preparation(service, fixture.account, source["task_id"], body)
        else:
            views = ResultViewService(service)
            value = views.issue(OWNER, source["task_id"], endpoint["endpoint_id"],
                                {"endpointId": endpoint["endpoint_id"], "tokenId": token["token_id"]})
            view_token = value["resultView"]["presentation"]["url"].rsplit("/", 1)[1]
            group = views.read(view_token)["groups"][0]
            queue = lambda body: views.prepare(view_token, body)
        meeting = next(item for item in group["targets"] if item["category"] == "公文")
        assert meeting["enabled"] is False
        assert meeting["workflowType"] is None
        assert "公文" in meeting["disabledReason"]
        assert any(item["enabled"] for item in group["targets"])
        with service.tasks._connect() as connection:
            before = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                      for table in ("agent_tasks", "operations", "business_drafts", "write_authorizations", "business_draft_decisions")}
        for selected in ([meeting], group["targets"]):
            with pytest.raises(actions.ActionableResultError) as caught:
                queue({"operationId": group["operationId"], "targetKeys": [item["targetKey"] for item in selected], "requestKey": uuid4().hex})
            assert caught.value.code == "BATCH_UNSUPPORTED_ITEMS"
        with service.tasks._connect() as connection:
            after = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in before}
        assert before == after
        assert fixture.oa.writes == []
    finally:
        fixture.close()
