"""Selection crosses real MCP, authorization, cursors and encrypted references."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from agentbridge.core.oa_history_queries import response_chars
from agentbridge.core.business_watches import BusinessWatchError
from tests import test_central_mcp as mcp_fixtures
from tests import test_oa_history_query_mcp as exports
from tests.test_oa_history_query_service import row


@pytest.fixture
def selected_history():
    case = exports.OaHistoryQueryMcpTests()
    case.setUp()
    pages, details = [], []
    engine = case.service.oa_history_query_service
    original_page = engine.read_page

    def read_page(*args, **kwargs):
        pages.append(kwargs["page_number"])
        return original_page(*args, **kwargs)

    def read_nodes(*_args, row, **_kwargs):
        details.append(row["affair_id"])
        return {"current_nodes": [{"name": "财务复核", "handlers": ["合成经办人"]}],
                "current_handlers": ["合成经办人"], "workflow_state": "in_progress",
                "workflow_state_text": "处理中", "observed_at": "2026-11-09T09:01:00+08:00",
                "unavailable_fields": []}

    engine.read_page, engine.read_nodes = read_page, read_nodes
    engine.target_ref_factory = case.service.business_watch.target_reference
    case.pages, case.details = pages, details
    start = datetime(2025, 1, 1)
    case.rows = [row(i, initiated=(start + timedelta(days=i)).date().isoformat()) for i in range(224)]
    for item in case.rows:
        number = int(item["affair_id"])
        item["_source"] = {"affair_id": item["affair_id"], "summary_id": str(1000 + number),
                           "process_id": str(2000 + number), "case_id": str(3000 + number)}
    for index in (7, 175):
        case.rows[index].update(workflow_state="in_progress", workflow_state_text="流程未结束",
                                current_nodes=None, current_handlers=["合成经办人"],
                                unavailable_fields=["current_nodes"])
    case.rows[175]["initiated_at"] = "2026-11-08T12:00:00+08:00"
    for index in range(5):
        case.rows[index]["initiated_at"] = f"2026-11-09T0{index + 1}:00:00+08:00"
    try:
        with mcp_fixtures.CentralMcpFixture() as (service, _store, token, client):
            service.invoke.side_effect = case.invoke
            yield case, client, token
    finally:
        case.doCleanups()


SELECTION = {"kind": "latest", "count": 1, "workflow_state": "in_progress"}


def test_latest_one_delivers_one_complete_owned_row_without_paging_unselected_history(selected_history):
    case, client, token = selected_history
    first = case.request(client, token, "query", {"collection": "sent", "temporal": {"kind": "all"},
                                                "selection": SELECTION})
    assert not first.get("isError"), first
    payload = first["structuredContent"]["result"]
    assert payload["state"] == "complete"
    assert payload["progress"]["scanned_count"] == 224
    assert [item["affair_id"] for item in payload["items"]] == ["175"]
    assert payload["items"][0]["current_nodes"] == [{"name": "财务复核", "handlers": ["合成经办人"]}]
    assert payload["selection"]["selection_complete"] is True
    assert payload["selection"]["selected_count"] == 1
    assert payload["selection"]["date_basis"] == "initiated_at"
    assert payload["coverage"]["details_scope"] == "selection_candidates"
    assert payload["has_more"] is False and payload["next_cursor"] is None
    assert sorted(set(case.pages)) == [1, 2, 3, 4, 5]
    assert case.details == ["175"]
    assert response_chars(payload) <= 10000
    assert len(first["content"][0]["text"].encode("utf-16-le")) // 2 <= 12000
    assert "_source" not in payload["items"][0]
    reference = case.service.business_watch.resolve_reference("user-a", payload["items"][0]["target_ref"])
    assert reference["source"]["affair_id"] == "175"
    with pytest.raises(BusinessWatchError) as rejected:
        case.service.business_watch.resolve_reference("user-b", payload["items"][0]["target_ref"])
    assert getattr(rejected.value, "code", None) == "WATCH_REFERENCE_INVALID"
    previous = (list(case.pages), list(case.details))
    replay = case.request(client, token, "next", {"cursor": payload["replay_cursor"]})
    assert replay["structuredContent"]["result"] == payload
    assert (case.pages, case.details) == previous


@pytest.mark.parametrize("selection", [
    {**SELECTION, "count": True}, {**SELECTION, "count": 0}, {**SELECTION, "count": 51},
    {**SELECTION, "count": "1"}, {**SELECTION, "kind": "first"},
    {**SELECTION, "workflow_state": "pending"}, {"kind": "latest", "count": 1},
    {**SELECTION, "user_subject": "user-b"}, {**SELECTION, "url": "https://outside.invalid"},
])
def test_selection_rejects_untrusted_or_ambiguous_arguments_before_reading(selected_history, selection):
    case, client, token = selected_history
    result = case.request(client, token, "query", {"collection": "sent", "temporal": {"kind": "all"},
                                                 "selection": selection})
    assert result.get("isError") is True
    assert case.pages == case.details == []


def test_omitting_selection_keeps_limit_as_delivery_page_size(selected_history):
    case, client, token = selected_history
    case.rows = deepcopy(case.rows[:3])
    result = case.request(client, token, "query", {"collection": "sent", "temporal": {"kind": "all"}, "limit": 1})
    assert not result.get("isError")
    payload = result["structuredContent"]["result"]
    assert "selection" not in payload
    assert payload["has_more"] is True and len(payload["items"]) == 1
    assert payload["progress"]["matched_count"] == 3
