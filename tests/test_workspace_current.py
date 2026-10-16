from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

import pytest

from agentbridge.workspace.application import WorkspaceApplication
from tests.authorization_fixtures import grant_permissions
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract
from tests.test_workspace import _create_account, _service


def _task(service, account, *, completed=False):
    task, _ = service.tasks.ensure_task(user_subject=account["user_subject"], agent_host="openclaw",
        host_task_key=str(uuid4()), origin_endpoint_id=account["endpoint_id"],
        active_conversation_ref=account["openclaw_session_key"], title="Synthetic task")
    if completed:
        task = service.tasks.complete_task(task_id=task["task_id"], user_subject=account["user_subject"], reason="synthetic")
    return task


def _message(service, account, key):
    return service.tasks.append_timeline_message(user_subject=account["user_subject"],
        source_endpoint_id=account["endpoint_id"], message_key=key, role="assistant", text="Synthetic message")[0]


@pytest.fixture
def app_account(tmp_path):
    service = _service(str(tmp_path))
    account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
    app = WorkspaceApplication(service=service)
    try:
        yield app, account
    finally:
        app.close()


def test_current_is_bounded_durable_and_available_without_gateway(app_account):
    app, account = app_account
    for _ in range(23):
        _task(app.service, account)
    for _ in range(10):
        _task(app.service, account, completed=True)
    for i in range(85):
        _message(app.service, account, f"message-{i}")
    with patch.object(app.service.tasks, "list_events", side_effect=AssertionError("full event history was read")):
        result = app.current(account)
    assert len(result["cards"]) == 28
    assert sum(card["task"]["status"] == "succeeded" for card in result["cards"]) == 8
    assert result["hasMoreActive"] and result["hasMoreRecent"]
    assert len(result["timeline"]["items"]) == 80 and result["timeline"]["hasMore"]
    assert all(card["events"] == [] and card["historyIncluded"] is False for card in result["cards"])


@pytest.mark.parametrize("empty", [False, True])
def test_current_watermark_does_not_skip_commits_during_projection(app_account, empty):
    app, account = app_account
    if not empty:
        _task(app.service, account)
        _message(app.service, account, "before")
    watermark = app.timeline_cursor(account)
    original = app.service.tasks.list_tasks
    injected = []

    def list_tasks(**kwargs):
        if not injected:
            injected.append(_message(app.service, account, "during"))
            _task(app.service, account)
        return original(**kwargs)

    with patch.object(app.service.tasks, "list_tasks", side_effect=list_tasks):
        result = app.current(account)
    assert result["cursors"]["timeline"] == watermark
    assert result["timeline"]["cursor"] == watermark
    assert all(entry["sequence"] <= watermark for entry in result["timeline"]["items"])
    replay = app.list_timeline(account, after_sequence=watermark)
    assert {entry["entry_type"] for entry in replay} == {"chat_message", "task_event"}
    assert injected[0]["sequence"] in {entry["sequence"] for entry in replay}


def test_projection_version_hint_cannot_reuse_expired_query_or_revoked_access(tmp_path):
    fixture = LatencyAcceptanceFixture(Path(tmp_path), seed=False,
        latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
    try:
        case = fixture.add_query_case(case_key="current-query")
        account, app = fixture.account, fixture.app
        result = app.current(account)
        card = next(card for card in result["cards"] if card["task"]["task_id"] == case["task_id"])
        assert card["actionableResults"] and card["task"]["status"] == "succeeded"
        version = card["task"]["version"]
        from agentbridge.core.actionable_results import _now
        with patch("agentbridge.core.actionable_results._now", return_value=_now() + timedelta(minutes=11)):
            refreshed = app.task_projections(account, [case["task_id"]], versions=[version])
        assert refreshed["items"][0]["detail"]["actionableResults"] == []
        assert refreshed["items"][0]["unchanged"] is False
        grant_permissions(fixture.service.user_grants, account["user_subject"], [])
        denied = app.task_projections(account, [case["task_id"]], versions=[version])
        assert denied == {"items": [], "unavailableTaskIds": [case["task_id"]]}
        assert fixture.oa.writes == []
    finally:
        fixture.close()


def test_projection_hides_foreign_and_missing_and_validates_bounds(app_account):
    app, account = app_account
    other = _create_account(app.service, user_subject="user-b", username="bob", endpoint_key="telegram:*:bob")
    owned, foreign = _task(app.service, account), _task(app.service, other)
    missing = str(uuid4())
    result = app.task_projections(account, [owned["task_id"], foreign["task_id"], missing, owned["task_id"]])
    assert [item["taskId"] for item in result["items"]] == [owned["task_id"]]
    assert result["unavailableTaskIds"] == [foreign["task_id"], missing]
    for ids, versions in [([], None), ([missing] * 33, None), (["bad"], None), ([missing], []), ([missing], [True])]:
        with pytest.raises(ValueError):
            app.task_projections(account, ids, versions=versions)


def test_history_tuple_cursor_reads_every_tied_row_once_and_stays_user_scoped(app_account):
    app, account = app_account
    tasks = [_task(app.service, account) for _ in range(70)]
    other = _create_account(app.service, user_subject="user-b", username="bob", endpoint_key="telegram:*:bob")
    foreign = _task(app.service, other)
    # Tie all timestamps so pagination depends on the secondary key.
    with app.service.tasks._connect() as connection:
        connection.execute("UPDATE agent_tasks SET updated_at = ?", ("2026-10-16T01:02:03.000001+00:00",))
    seen, cursor = [], {}
    while True:
        page = app.task_history(account, **cursor)
        assert len(page["items"]) <= 32
        seen.extend(task["task_id"] for task in page["items"])
        if not page["hasMore"]:
            assert page["nextCursor"] is None
            break
        cursor = {"before_updated_at": page["nextCursor"]["updatedAt"], "before_task_id": page["nextCursor"]["taskId"]}
    assert seen == sorted((task["task_id"] for task in tasks), reverse=True)
    assert foreign["task_id"] not in seen
    for cursor in [{"before_task_id": str(uuid4())}, {"before_updated_at": "junk", "before_task_id": str(uuid4())},
                   {"before_updated_at": "2026-10-16T01:02:03+08:00", "before_task_id": str(uuid4())},
                   {"before_updated_at": "2026-10-16T01:02:03+00:00", "before_task_id": "bad"}]:
        with pytest.raises(ValueError):
            app.task_history(account, **cursor)
