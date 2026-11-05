"""Unified chronological Workspace history, using isolated local ledgers."""
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from agentbridge.core.timeline_attachments import public_attachment
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.server import SESSION_COOKIE
from tests.authorization_fixtures import grant_permissions
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract
from tests.test_workspace import _create_account, _request, _service
from tests.test_workspace_current import _message, _task
from tests.test_workspace_streams import _server


@pytest.fixture
def context(tmp_path):
    service = _service(str(tmp_path))
    account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
    app = WorkspaceApplication(service=service)
    try:
        yield app, account
    finally:
        app.close()


def _cursor(value):
    return {"before_created_at": value["createdAt"], "before_kind": value["kind"], "before_id": value["id"]}


def _keys(page):
    return sorted([
        *((entry["created_at"], "message", entry["entry_id"]) for entry in page["timeline"]["items"]),
        *((card["task"]["created_at"], "task", card["task"]["task_id"]) for card in page["cards"]),
    ], reverse=True)


def _turn_message(app, account, key, role, stamp):
    with patch("agentbridge.core.tasks._utc_now", return_value=stamp):
        return app.service.tasks.append_timeline_message(user_subject=account["user_subject"],
            source_endpoint_id=account["endpoint_id"], message_key=key, role=role, text="Synthetic turn message")[0]


def test_current_and_older_pages_keep_normal_user_task_answer_turns_together(context):
    app, account = context
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    turns = []
    for index in range(41):
        stamp = start + timedelta(seconds=index)
        user = _turn_message(app, account, f"turn-{index}-user", "user", stamp.isoformat(timespec="microseconds"))
        with patch("agentbridge.core.tasks._utc_now", return_value=(stamp + timedelta(microseconds=1)).isoformat()):
            task = _task(app.service, account, completed=True)
        answer = _turn_message(app, account, f"turn-{index}-answer", "assistant",
                               (stamp + timedelta(microseconds=2)).isoformat())
        turns.append({user["entry_id"], task["task_id"], answer["entry_id"]})
    page = app.current(account, history_limit=32)
    assert len(_keys(page)) == 30  # The orphaned answer/card at the bound stay on the next page.
    page.update(page["history"])
    seen = set()
    for _ in range(6):
        keys = _keys(page)
        ids = {key[2] for key in keys}
        assert 0 < len(keys) <= 32
        assert not seen & ids
        assert all(not ids & turn or turn <= ids for turn in turns)
        assert page["timeline"]["items"][0]["role"] == "user"
        seen.update(ids)
        if not page["hasMore"]:
            break
        assert page["nextCursor"]["kind"] == "message"
        assert page["nextCursor"]["id"] == page["timeline"]["items"][0]["entry_id"]
        page = app.history_page(account, **_cursor(page["nextCursor"]))
    else:
        pytest.fail("bounded turn history did not reach its end")
    assert seen == set().union(*turns)


@pytest.mark.parametrize("kind", ["task", "assistant", "large_turn"])
def test_pages_without_a_user_boundary_return_bounded_slices_and_always_progress(context, kind):
    app, account = context
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    expected = set()
    if kind == "large_turn":
        user = _turn_message(app, account, "large-user", "user", start.isoformat())
        expected.add(user["entry_id"])
    for index in range(70):
        stamp = (start + timedelta(seconds=index + 1)).isoformat()
        if kind == "assistant":
            value = _turn_message(app, account, f"only-answer-{index}", "assistant", stamp)
            expected.add(value["entry_id"])
        else:
            with patch("agentbridge.core.tasks._utc_now", return_value=stamp):
                value = _task(app.service, account, completed=True)
            expected.add(value["task_id"])
    if kind == "large_turn":
        answer = _turn_message(app, account, "large-answer", "assistant", (start + timedelta(seconds=72)).isoformat())
        expected.add(answer["entry_id"])
    seen, cursor = set(), {}
    for _ in range(4):
        page = app.history_page(account, **cursor)
        keys = _keys(page)
        ids = {key[2] for key in keys}
        assert 0 < len(keys) <= 32 and not ids & seen
        seen.update(ids)
        if not page["hasMore"]:
            break
        assert len(keys) == 32
        cursor = _cursor(page["nextCursor"])
    else:
        pytest.fail("history without a boundary stopped making progress")
    assert seen == expected


def test_trimming_the_last_candidate_page_defers_older_prefix_without_losing_it(context):
    app, account = context
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    prefix = _turn_message(app, account, "legacy-prefix", "assistant", start.isoformat())
    user = _turn_message(app, account, "last-user", "user", (start + timedelta(seconds=1)).isoformat())
    answer = _turn_message(app, account, "last-answer", "assistant", (start + timedelta(seconds=2)).isoformat())
    page = app.history_page(account)
    assert {key[2] for key in _keys(page)} == {user["entry_id"], answer["entry_id"]}
    assert page["hasMore"] is True and page["nextCursor"]["id"] == user["entry_id"]
    last = app.history_page(account, **_cursor(page["nextCursor"]))
    assert {key[2] for key in _keys(last)} == {prefix["entry_id"]}
    assert last["hasMore"] is False and last["nextCursor"] is None


def test_mixed_history_pages_share_one_range_without_missing_or_duplicate_rows(context):
    app, account = context
    expected = []
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for index in range(120):
        stamp = (start + timedelta(seconds=index // 3)).isoformat(timespec="microseconds")
        with patch("agentbridge.core.tasks._utc_now", return_value=stamp):
            task = _task(app.service, account, completed=True)
            message = _message(app.service, account, f"mixed-{index}")
        expected.extend([(stamp, "task", task["task_id"]), (stamp, "message", message["entry_id"])])
    # Even a legacy task with no timeline rows must retain its own card anchor.
    with app.service.tasks._connect() as connection:
        connection.execute("DELETE FROM user_timeline WHERE task_id = ?", (task["task_id"],))
        connection.execute("UPDATE agent_tasks SET updated_at = ? WHERE task_id = ?",
                           ("2099-01-01T00:00:00+00:00", expected[0][2]))
    other = _create_account(app.service, user_subject="user-b", username="bob", endpoint_key="telegram:*:bob")
    _task(app.service, other, completed=True)
    _message(app.service, other, "foreign")
    seen, cursor = [], {}
    with patch.object(app.service.tasks, "list_tasks", side_effect=AssertionError("updated-at tasks were scanned")):
        while True:
            page = app.history_page(account, **cursor)
            assert page["schemaVersion"] == "agentbridge.workspace-history.v1"
            keys = _keys(page)
            assert len(keys) <= 32 and not page["unavailableTaskIds"]
            assert all(card["events"] == [] and card["historyIncluded"] is False for card in page["cards"])
            if seen:
                assert keys[0] < seen[-1]
            seen.extend(keys)
            if not page["hasMore"]:
                assert page["nextCursor"] is None
                break
            assert len(keys) == 32
            assert tuple(page["nextCursor"][key] for key in ("createdAt", "kind", "id")) == keys[-1]
            cursor = _cursor(page["nextCursor"])
    assert seen == sorted(expected, reverse=True)
    assert len(set(seen)) == 240


def test_current_opt_in_uses_one_history_range_and_bounded_deduplicated_active_cards(context):
    app, account = context
    for index in range(25):
        _task(app.service, account)
        _message(app.service, account, f"current-{index}")
    original = app.task_projections
    projected = []

    def project(account, task_ids, **kwargs):
        projected.extend(task_ids)
        return original(account, task_ids, **kwargs)

    with patch.object(app, "task_projections", side_effect=project):
        result = app.current(account, history_limit=32)
    expected = app.history_page(account)
    assert result["history"] == {key: expected[key] for key in ("schemaVersion", "hasMore", "nextCursor")}
    assert result["timeline"]["items"] == expected["timeline"]["items"]
    ids = [card["task"]["task_id"] for card in result["cards"]]
    assert len(ids) == len(set(ids)) and len(ids) <= 52
    assert len(projected) == len(set(projected)) <= 52
    assert result["hasMoreActive"] is True
    # The default API is unchanged for older clients.
    old = app.current(account)
    assert "history" not in old and len(old["timeline"]["items"]) == 25


@pytest.mark.parametrize("empty", [False, True])
def test_current_history_keeps_pre_read_sse_watermark_during_concurrent_commits(context, empty):
    app, account = context
    if not empty:
        _task(app.service, account, completed=True)
        _message(app.service, account, "existing")
    watermark = app.timeline_cursor(account)
    original = app.service.tasks.list_workspace_history
    injected = []

    def read(**kwargs):
        injected.append(_message(app.service, account, "concurrent"))
        _task(app.service, account)
        return original(**kwargs)

    with patch.object(app.service.tasks, "list_workspace_history", side_effect=read):
        result = app.current(account, history_limit=32)
    assert result["cursors"]["timeline"] == result["timeline"]["cursor"] == watermark
    assert all(entry["sequence"] <= watermark for entry in result["timeline"]["items"])
    replay = app.list_timeline(account, after_sequence=watermark)
    assert injected[0]["entry_id"] in {entry["entry_id"] for entry in replay}
    assert {entry["entry_type"] for entry in replay} == {"chat_message", "task_event"}


def test_history_live_access_failure_does_not_disclose_card_and_keeps_pagination_progress(tmp_path):
    fixture = LatencyAcceptanceFixture(Path(tmp_path), seed=False,
        latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
    try:
        case = fixture.add_query_case(case_key="history-grant")
        _message(fixture.service, fixture.account, "visible-text")
        allowed = fixture.app.history_page(fixture.account, limit=1)
        assert allowed["hasMore"]
        cursor = _cursor(allowed["nextCursor"])
        grant_permissions(fixture.service.user_grants, fixture.account["user_subject"], [])
        denied = fixture.app.history_page(fixture.account, **cursor)
        assert denied["cards"] == [] and denied["unavailableTaskIds"] == [case["task_id"]]
        assert not denied["hasMore"] and denied["nextCursor"] is None
        assert fixture.oa.writes == []
    finally:
        fixture.close()


def test_history_messages_reuse_current_attachment_expiry_and_source_projection(context):
    app, account = context
    media = app.service.timeline_attachments
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with patch.object(media, "clock", return_value=now):
        attachment = media.create_many(user_subject=account["user_subject"], message_key="history-image",
            media_base_url="http://127.0.0.1:8780", ttl_seconds=300,
            attachments=[{"fileName": "historical.png", "mimeType": "image/png",
                          "content": base64.b64encode(b"synthetic PNG").decode()}])[0]
    entry, _ = app.service.tasks.append_timeline_message(user_subject=account["user_subject"],
        source_endpoint_id=account["endpoint_id"], message_key="history-image", role="user", text="Image record",
        payload={"attachments": [public_attachment(attachment)]})
    with patch.object(media, "clock", return_value=now + timedelta(minutes=6)):
        result = app.history_page(account)
        existing = app.list_timeline(account, entry_type="chat_message")
    assert result["timeline"]["items"] == existing
    image_message = result["timeline"]["items"][0]
    assert image_message["entry_id"] == entry["entry_id"] and image_message["source"]["is_origin"] is True
    assert image_message["payload"]["attachments"][0]["state"] == "expired"
    assert image_message["payload"]["attachments"][0]["mediaUrl"] is None


@pytest.mark.parametrize("cursor", [
    {"before_id": str(uuid4())},
    {"before_created_at": "2026-11-01T00:00:00+00:00", "before_kind": "task"},
    {"before_created_at": "2026-11-01T00:00:00+08:00", "before_kind": "task", "before_id": str(uuid4())},
    {"before_created_at": "2026-13-01T00:00:00+00:00", "before_kind": "task", "before_id": str(uuid4())},
    {"before_created_at": "2026-11-01T00:00:00+00:00", "before_kind": "event", "before_id": str(uuid4())},
    {"before_created_at": "2026-11-01T00:00:00+00:00", "before_kind": "task", "before_id": "not-a-uuid"},
])
def test_invalid_history_cursors_are_rejected_before_reading(context, cursor):
    app, account = context
    with patch.object(app.service.tasks, "list_workspace_history", side_effect=AssertionError("invalid cursor was read")):
        with pytest.raises(ValueError):
            app.history_page(account, **cursor)


def test_history_route_authentication_bounds_and_current_opt_in(tmp_path):
    with _server(tmp_path) as (app, account, session, server, _):
        task = _task(app.service, account, completed=True)
        message = _message(app.service, account, "http-history")
        cookies = {SESSION_COOKIE: session["session_token"]}
        assert _request(server.server_port, "GET", "/api/history")[0] == 401
        status, _, page = _request(server.server_port, "GET", "/api/history?limit=1", cookies=cookies)
        assert status == 200 and page["timeline"]["items"][0]["entry_id"] == message["entry_id"]
        status, _, older = _request(server.server_port, "GET", "/api/history?" + urlencode(_cursor(page["nextCursor"])), cookies=cookies)
        assert status == 200 and older["cards"][0]["task"]["task_id"] == task["task_id"]
        status, _, snapshot = _request(server.server_port, "GET", "/api/current?history_limit=32", cookies=cookies)
        assert status == 200 and snapshot["history"]["schemaVersion"] == page["schemaVersion"]
        for path in ["/api/history?before_kind=task", "/api/history?limit=invalid", "/api/current?history_limit=invalid"]:
            assert _request(server.server_port, "GET", path, cookies=cookies)[0] == 400
        with patch.object(app.service.tasks, "list_workspace_history", wraps=app.service.tasks.list_workspace_history) as history:
            assert _request(server.server_port, "GET", "/api/history?limit=9999", cookies=cookies)[0] == 200
        assert history.call_args.kwargs["limit"] <= 33
