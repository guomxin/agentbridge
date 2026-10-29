"""Synthetic coverage for the verified, read-only full pending-list contract."""
from copy import deepcopy
from contextlib import contextmanager
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agentbridge.adapters.seeyon_pending_query import (
    PendingQueryContractMismatch,
    PendingQueryLoginRequired,
    read_pending_more,
    scan_pending_pages,
)
from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter, SeeyonLoginRequired
from agentbridge.browser.read_budget import ReadBudgetExceeded, read_deadline_scope


BASE = "https://oa.example.test/seeyon/"
PARAMS = {
    "aiSortValue": None,
    "sectionName": "pendingSection",
    "isTrack": False,
    "bigTitleIndex": "0",
    "fragmentId": "123456",
    "state": 3,
    "rowStr": "subject,receiveTime,sendUser,category",
    "ordinal": "0",
}


def rows(count, *, start=1):
    return [{
        "id": str(index), "subject": f"合成待办 {index}",
        "createMemberName": "合成申请人", "receiveTime": "2026-10-29 12:00",
        "categoryLabel": "协同", "readState": 1,
        "link": "/collaboration/collaboration.do?method=summary&openFrom=listPending"
                f"&affairId={index}&showTab=true",
    } for index in range(start, start + count)]


def payload(data, *, total=None, page=1):
    total = len(data) if total is None else total
    return {"data": data, "total": total, "pages": (total + 49) // 50,
            "size": 50, "page": page, "showTotal": True}


def scan(*responses, **kwargs):
    frame = Mock()
    frame.evaluate.side_effect = responses
    value = scan_pending_pages(frame, params=deepcopy(PARAMS), base_url=BASE, **kwargs)
    return value, frame


def assert_partial(value):
    assert value["query_evidence"]["complete"] is False
    assert value["query_evidence"]["completionReason"]


def test_twelve_pending_rows_are_not_limited_to_nine_home_rows():
    result, frame = scan(payload(rows(12)))
    assert result["query_evidence"]["complete"] is True
    assert result["total"] == result["count"] == len(result["items"]) == 12
    assert result["source"] == "pending_more_query"
    assert frame.evaluate.call_count == 1
    item = result["items"][0]
    assert item["affair_id"] == "1"
    assert item["title"] == "合成待办 1"
    assert item["sender"] == "合成申请人"
    assert item["date"] == "2026-10-29 12:00"
    assert item["category"] == "协同"
    assert item["read"] is True
    assert item["href"].startswith(BASE + "collaboration/collaboration.do?")


def test_two_pages_require_unchanged_first_page_anchor():
    first = payload(rows(50), total=51)
    result, frame = scan(first, payload(rows(1, start=51), total=51, page=2), first)
    assert result["query_evidence"]["complete"] is True
    assert result["count"] == 51
    assert len({row["affair_id"] for row in result["items"]}) == 51
    calls = [call.args[1] for call in frame.evaluate.call_args_list]
    assert [args["pageNumber"] for args in calls] == [1, 2, 1]


@pytest.mark.parametrize("change", ["id", "subject", "receiveTime", "link"])
def test_equal_total_with_changed_anchor_never_proves_complete(change):
    first = payload(rows(50), total=51)
    changed = deepcopy(first)
    changed["data"][0][change] = {
        "id": "999", "subject": "替换的合成事项", "receiveTime": "2026-10-29 12:01",
        "link": "/collaboration/collaboration.do?method=summary&affairId=999",
    }[change]
    result, _ = scan(first, payload(rows(1, start=51), total=51, page=2), changed)
    assert_partial(result)


def test_zero_pending_rows_have_complete_empty_evidence():
    result, _ = scan(payload([]))
    assert result["items"] == []
    assert result["count"] == result["total"] == 0
    assert result["query_evidence"]["complete"] is True


@pytest.mark.parametrize("total", [100, 101])
def test_hundred_row_bound_never_claims_extra_rows(total):
    first = payload(rows(50), total=total)
    responses = [first, payload(rows(50, start=51), total=total, page=2)]
    if total == 100:
        responses.append(first)
    result, frame = scan(*responses)
    assert result["count"] == 100
    assert result["query_evidence"]["complete"] is (total == 100)
    assert frame.evaluate.call_count == (3 if total == 100 else 2)


@pytest.mark.parametrize("changes", [
    {"total": 52}, {"pages": 3}, {"page": 1}, {"size": 100},
    {"total": True}, {"pages": "2"}, {"page": False}, {"size": "50"},
])
def test_second_page_metadata_drift_or_wrong_types_stays_partial(changes):
    second = {**payload(rows(1, start=51), total=51, page=2), **changes}
    result, _ = scan(payload(rows(50), total=51), second)
    assert_partial(result)


@pytest.mark.parametrize("second", [
    payload([], total=51, page=2),
    payload(rows(2, start=51), total=51, page=2),
    {**payload(rows(1, start=51), total=51, page=2), "data": None},
    RuntimeError("synthetic read unavailable"),
    TimeoutError("synthetic read timeout"),
])
def test_interrupted_or_malformed_second_page_preserves_safe_prefix_only(second):
    result, _ = scan(payload(rows(50), total=51), second)
    assert_partial(result)
    assert result["count"] == 50


@pytest.mark.parametrize("change", [
    "duplicate", "missing_id", "missing_title", "blank_title", "nonnumeric_id",
    "cross_origin", "wrong_affair", "javascript_href", "missing_href",
])
def test_invalid_or_conflicting_rows_never_prove_complete(change):
    data = rows(12)
    if change == "duplicate":
        data[1] = deepcopy(data[0])
    elif change == "missing_id":
        data[0].pop("id")
    elif change == "missing_title":
        data[0].pop("subject")
    elif change == "blank_title":
        data[0]["subject"] = " "
    elif change == "nonnumeric_id":
        data[0]["id"] = "other-id"
    elif change == "cross_origin":
        data[0]["link"] = "https://other.example.test/seeyon/collaboration/collaboration.do?affairId=1"
    elif change == "wrong_affair":
        data[0]["link"] = "/collaboration/collaboration.do?method=summary&affairId=999"
    elif change == "javascript_href":
        data[0]["link"] = "javascript:alert(1)"
    else:
        data[0].pop("link")
    result, _ = scan(payload(data))
    assert_partial(result)


def test_meeting_pending_row_keeps_affair_identity_and_registered_meeting_link():
    data = rows(1)
    data[0].update(categoryLabel="会议", link="/meeting.do?method=view&meetingId=700")
    result, _ = scan(payload(data))
    assert result["query_evidence"]["complete"] is True
    assert result["items"][0]["affair_id"] == "1"
    assert result["items"][0]["category"] == "会议"
    assert result["items"][0]["href"] == BASE + "meeting.do?method=view&meetingId=700"


def test_first_transport_failure_is_not_an_empty_complete_list():
    with pytest.raises(PendingQueryContractMismatch):
        scan(RuntimeError("synthetic first read failed"))


def test_existing_read_deadline_is_never_extended():
    with read_deadline_scope(time.monotonic() + .05):
        with patch("agentbridge.adapters.seeyon_pending_query.time.monotonic", return_value=time.monotonic() + 1):
            with pytest.raises(ReadBudgetExceeded):
                scan(payload(rows(12)))


def test_output_limit_remains_partial_even_after_full_pending_read():
    parsed, _ = scan(payload(rows(12)))
    adapter = SeeyonCentralAdapter(base_url=BASE)
    with patch.object(adapter, "_fetch_workflow_collection", return_value=parsed):
        result = adapter.list_workflows(Mock(), collection="pending", arguments={"limit": 9})
    assert result["count"] == 9
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["completionReason"] == "result_limit_truncated"


MORE = BASE + "collaboration/pending.do?method=morePendingCenter&section=pendingSection&state=3"
FRAME = BASE + "collaboration/pending.do?method=morePendingAll&sectionName=pendingSection"


def read_fixture(*, more_link=MORE, frame_url=FRAME, params=None, context=None):
    page, frame = Mock(), Mock()
    frame.url = frame_url
    frame.evaluate.side_effect = [context if context is not None else {
        "managerName": "pendingManager", "managerMethod": "getMoreList4SectionContion",
        "params": deepcopy(PARAMS if params is None else params),
    }, payload(rows(12))]
    page.locator.return_value.element_handle.return_value.content_frame.return_value = frame
    worker = SimpleNamespace(page_url=MORE, page=page, goto=Mock(return_value=page))
    projection = {"Data": {"moreButton": {"link": more_link}}}
    return worker, frame, projection


def test_verified_more_route_reads_fixed_manager_scope_without_mutating_it():
    worker, frame, projection = read_fixture()
    result = read_pending_more(worker, base_url=BASE, projection=projection)
    assert result["query_evidence"]["complete"] is True
    worker.goto.assert_called_once()
    assert worker.goto.call_args.args[0] == MORE
    assert frame.evaluate.call_args_list[1].args[1]["params"] == PARAMS


@pytest.mark.parametrize("projection", [
    {}, {"Data": None}, {"Data": {}}, {"Data": {"moreButton": None}},
    {"Data": {"moreButton": {"link": None}}},
    {"Data": {"moreButton": {"link": 123}}},
])
def test_absent_or_nonstring_more_link_never_navigates(projection):
    worker, _, _ = read_fixture()
    with pytest.raises(PendingQueryContractMismatch):
        read_pending_more(worker, base_url=BASE, projection=projection)
    worker.goto.assert_not_called()


@pytest.mark.parametrize("link", [
    "https://other.example.test/seeyon/collaboration/pending.do?method=morePendingCenter",
    "javascript:alert(1)",
    "/collaboration/collaboration.do?method=morePendingCenter",
    "/collaboration/pending.do?method=otherMethod",
    "/collaboration/pending.do?method=morePendingCenter&method=otherMethod",
    "https://user:pass@oa.example.test/seeyon/collaboration/pending.do?method=morePendingCenter",
])
def test_more_route_must_match_origin_path_and_method_before_navigation(link):
    worker, _, projection = read_fixture(more_link=link)
    with pytest.raises(PendingQueryContractMismatch):
        read_pending_more(worker, base_url=BASE, projection=projection)
    worker.goto.assert_not_called()


@pytest.mark.parametrize("frame_url", [
    "https://other.example.test/seeyon/collaboration/pending.do?method=morePendingAll&sectionName=pendingSection",
    BASE + "collaboration/pending.do?method=morePendingCenter&sectionName=pendingSection",
    BASE + "collaboration/pending.do?method=morePendingAll&sectionName=otherSection",
    BASE + "collaboration/pending.do?method=morePendingAll&sectionName=pendingSection&sectionName=otherSection",
])
def test_unproven_frame_is_never_used_for_manager_reads(frame_url):
    worker, frame, projection = read_fixture(frame_url=frame_url)
    with pytest.raises(PendingQueryContractMismatch):
        read_pending_more(worker, base_url=BASE, projection=projection)
    frame.evaluate.assert_not_called()


def test_initial_blank_frame_can_finish_loading_before_scope_is_read():
    worker, frame, projection = read_fixture(frame_url="about:blank")
    frame.wait_for_function.side_effect = lambda *a, **kw: setattr(frame, "url", FRAME)
    result = read_pending_more(worker, base_url=BASE, projection=projection)
    assert result["query_evidence"]["complete"] is True


@pytest.mark.parametrize("changes", [
    {"condition": "subject"}, {"state": "3"}, {"state": 4}, {"isTrack": True},
    {"sectionName": "otherSection"}, {"aiSortValue": 1},
    {"rowStr": "subject"}, {"fragmentId": "unknown"},
    {"ordinal": ""}, {"bigTitleIndex": 0},
])
def test_unknown_or_changed_query_scope_never_calls_manager(changes):
    worker, frame, projection = read_fixture(params={**PARAMS, **changes})
    with pytest.raises(PendingQueryContractMismatch):
        read_pending_more(worker, base_url=BASE, projection=projection)
    assert frame.evaluate.call_count == 1


@pytest.mark.parametrize("key", list(PARAMS))
def test_missing_query_scope_field_is_rejected(key):
    params = deepcopy(PARAMS)
    params.pop(key)
    worker, frame, projection = read_fixture(params=params)
    with pytest.raises(PendingQueryContractMismatch):
        read_pending_more(worker, base_url=BASE, projection=projection)
    assert frame.evaluate.call_count == 1


@pytest.mark.parametrize("context", [
    {"managerName": "otherManager", "managerMethod": "getMoreList4SectionContion", "params": PARAMS},
    {"managerName": "pendingManager", "managerMethod": "otherMethod", "params": PARAMS},
])
def test_unknown_manager_contract_is_rejected(context):
    worker, frame, projection = read_fixture(context=context)
    with pytest.raises(PendingQueryContractMismatch):
        read_pending_more(worker, base_url=BASE, projection=projection)
    assert frame.evaluate.call_count == 1


def test_login_redirect_is_preserved_instead_of_becoming_empty_pending():
    worker, frame, projection = read_fixture()
    worker.page_url = BASE + "login/index.jsp"
    with pytest.raises(PendingQueryLoginRequired):
        read_pending_more(worker, base_url=BASE, projection=projection)
    frame.evaluate.assert_not_called()


def test_login_loss_during_initial_manager_read_still_requires_authentication():
    worker, frame, projection = read_fixture()
    context = {"managerName": "pendingManager", "managerMethod": "getMoreList4SectionContion",
               "params": deepcopy(PARAMS)}
    calls = [0]

    def query(*args):
        calls[0] += 1
        if calls[0] == 1:
            return context
        frame.url = BASE + "login/index.jsp"
        raise TimeoutError("synthetic logout callback timeout")

    frame.evaluate.side_effect = query
    with pytest.raises(PendingQueryLoginRequired):
        read_pending_more(worker, base_url=BASE, projection=projection)


def test_response_after_local_deadline_cannot_be_claimed_complete():
    clock = [0.0]
    frame = Mock()

    def late(*args):
        clock[0] = 16
        return payload(rows(12))

    frame.evaluate.side_effect = late
    with patch("agentbridge.adapters.seeyon_pending_query.time.monotonic", lambda: clock[0]):
        result = scan_pending_pages(frame, params=PARAMS, base_url=BASE)
    assert_partial(result)
    assert result["count"] == 0


@pytest.mark.parametrize("link", [
    "/collaboration/collaboration.do?method=finishWorkItem&affairId=1&openFrom=listPending",
    "/collaboration/collaboration.do?method=summary&affairId=1",
    "/collaboration/collaboration.do?method=summary&affairId=1&openFrom=listDone",
    "/other/collaboration/collaboration.do?method=summary&affairId=1&openFrom=listPending",
    "/collaboration/collaboration.do?method=summary&affairId=1&affairId=2&openFrom=listPending",
])
def test_collaboration_link_must_be_exact_pending_summary_route(link):
    data = rows(1)
    data[0]["link"] = link
    result, _ = scan(payload(data))
    assert_partial(result)


@pytest.mark.parametrize("link", [
    "/meeting.do?method=reply&meetingId=700",
    "/meeting.do?method=view&meetingId=unknown",
    "/meeting.do?method=view",
    "/other/meeting.do?method=view&meetingId=700",
    "/meeting.do?method=view&meetingId=700&meetingId=701",
])
def test_meeting_link_must_be_exact_registered_read_route(link):
    data = rows(1)
    data[0].update(categoryLabel="会议", link=link)
    result, _ = scan(payload(data))
    assert_partial(result)


def test_unknown_category_keeps_facts_without_exposing_an_action_link():
    data = rows(1)
    data[0].update(categoryLabel="未注册外部类型", link="/unknown/action.do?method=unknown")
    result, _ = scan(payload(data))
    assert result["query_evidence"]["complete"] is True
    assert result["items"][0]["category"] == "未注册外部类型"
    assert result["items"][0]["href"] == ""


@pytest.mark.parametrize("category", [None, "", " "])
def test_missing_category_never_proves_complete(category):
    data = rows(1)
    data[0]["categoryLabel"] = category
    result, _ = scan(payload(data))
    assert_partial(result)


def test_navigation_failure_becomes_contract_unavailable():
    worker, _, projection = read_fixture()
    worker.goto.side_effect = RuntimeError("synthetic navigation unavailable")
    with pytest.raises(PendingQueryContractMismatch):
        read_pending_more(worker, base_url=BASE, projection=projection)


def test_login_navigation_failure_preserves_authentication_signal():
    worker, _, projection = read_fixture()

    def navigate(*args, **kwargs):
        worker.page_url = BASE + "login/index.jsp"
        raise TimeoutError("synthetic login navigation timeout")

    worker.goto.side_effect = navigate
    with pytest.raises(PendingQueryLoginRequired):
        read_pending_more(worker, base_url=BASE, projection=projection)


@pytest.mark.parametrize("logout_call", [3, 4])
def test_login_loss_during_later_page_or_anchor_is_not_returned_as_partial(logout_call):
    worker, frame, projection = read_fixture()
    context = {"managerName": "pendingManager", "managerMethod": "getMoreList4SectionContion",
               "params": deepcopy(PARAMS)}
    first = payload(rows(50), total=51)
    normal = [context, first, payload(rows(1, start=51), total=51, page=2), first]
    calls = [0]

    def query(*args):
        calls[0] += 1
        if calls[0] == logout_call:
            frame.url = BASE + "login/index.jsp"
            raise TimeoutError("synthetic later logout callback timeout")
        return normal[calls[0] - 1]

    frame.evaluate.side_effect = query
    with pytest.raises(PendingQueryLoginRequired):
        read_pending_more(worker, base_url=BASE, projection=projection)


def adapter_fixture(*, count=9, total=12):
    from tests.test_seeyon_central_workflows import FakeWorkflowWorker

    adapter = SeeyonCentralAdapter(base_url=BASE)
    worker, reader = FakeWorkflowWorker(), FakeWorkflowWorker()
    worker.page.url = BASE + "collaboration/collaboration.do?method=summary&affairId=700"
    home = {"source": "section_api", "count": count, "total": total, "page": 0,
            "items": [{"affair_id": str(index), "title": f"合成待办 {index}",
                       "category": "协同", "sender": "合成申请人", "date": "2026-10-29"}
                      for index in range(1, count + 1)]}
    projection = {"Data": {"moreButton": {"link": MORE}}}
    worker.request = Mock(return_value={"status": 200, "url": BASE + "ajax.do",
                                        "json": projection})
    closed = []

    @contextmanager
    def fork():
        try:
            yield reader
        finally:
            closed.append(True)

    worker.fork_page = Mock(side_effect=fork)
    adapter.list_templates = Mock()
    adapter._discover_pending_section_url = Mock(return_value=BASE + "ajax.do")
    return adapter, worker, reader, home, closed


def test_full_pending_fallback_navigates_only_fork_and_restores_capability_scope():
    adapter, worker, reader, home, closed = adapter_fixture()
    action_url = worker.page_url
    full, _ = scan(payload(rows(12)))

    def read(child, **kwargs):
        assert child is reader
        assert adapter._scope_for(reader)["worker"] is reader
        assert adapter._scope_for(worker) is None
        child.goto(MORE)
        return full

    with adapter.capability_scope(worker):
        parent_scope = adapter._scope_for(worker)
        with patch("agentbridge.adapters.seeyon_central.parse_pending_projection", return_value=home), \
                patch("agentbridge.adapters.seeyon_pending_query.read_pending_more", side_effect=read):
            result = adapter._fetch_workflow_collection(worker, "pending")
        assert adapter._scope_for(worker) is parent_scope
    assert result is full
    assert result["count"] == 12
    assert worker.page_url == action_url
    assert worker.goto_calls == []
    assert reader.goto_calls == [MORE]
    assert closed == [True]


@pytest.mark.parametrize("count", [0, 9])
def test_complete_home_source_does_not_open_another_page(count):
    adapter, worker, _, home, closed = adapter_fixture(count=count, total=count)
    with patch("agentbridge.adapters.seeyon_central.parse_pending_projection", return_value=home), \
            patch("agentbridge.adapters.seeyon_pending_query.read_pending_more") as more:
        result = adapter._fetch_workflow_collection(worker, "pending")
    assert result is home
    more.assert_not_called()
    worker.fork_page.assert_not_called()
    assert closed == []


def test_invalid_full_metadata_keeps_known_home_rows_partial():
    adapter, worker, _, home, closed = adapter_fixture()
    invalid, _ = scan({**payload(rows(12)), "total": None})
    with patch("agentbridge.adapters.seeyon_central.parse_pending_projection", return_value=home), \
            patch("agentbridge.adapters.seeyon_pending_query.read_pending_more", return_value=invalid):
        result = adapter._fetch_workflow_collection(worker, "pending")
    assert result["count"] == len(result["items"]) == 9
    assert result["total"] == 12
    assert_partial(result)
    assert result["query_evidence"]["completionReason"] == "pending_query_metadata_invalid"
    assert closed == [True]


@pytest.mark.parametrize("error", [
    PendingQueryContractMismatch("synthetic unverified full-list route"),
    PendingQueryLoginRequired("synthetic session expired"),
    ReadBudgetExceeded("synthetic parent read deadline expired"),
])
def test_full_reader_failure_preserves_contract_authentication_and_budget_boundaries(error):
    adapter, worker, _, home, closed = adapter_fixture()
    action_url = worker.page_url
    with patch("agentbridge.adapters.seeyon_central.parse_pending_projection", return_value=home), \
            patch("agentbridge.adapters.seeyon_pending_query.read_pending_more", side_effect=error):
        if isinstance(error, PendingQueryLoginRequired):
            with pytest.raises(SeeyonLoginRequired):
                adapter._fetch_workflow_collection(worker, "pending")
        elif isinstance(error, ReadBudgetExceeded):
            with pytest.raises(ReadBudgetExceeded):
                adapter._fetch_workflow_collection(worker, "pending")
        else:
            result = adapter._fetch_workflow_collection(worker, "pending")
            assert result["count"] == 9
            assert_partial(result)
            assert result["query_evidence"]["completionReason"] == "pending_full_list_unavailable"
    assert worker.page_url == action_url
    assert closed == [True]


def test_existing_collaboration_category_alias_keeps_the_same_strict_route_contract():
    data = rows(1)
    data[0]['categoryLabel'] = 'collaboration'
    result, _ = scan(payload(data))
    assert result['query_evidence']['complete'] is True
    assert result['items'][0]['href'].startswith(BASE + 'collaboration/collaboration.do?')
    data[0]['link'] = '/collaboration/collaboration.do?method=finishWorkItem'
    result, _ = scan(payload(data))
    assert_partial(result)
