"""A private Workspace tab reads one instance, while explicit continuation stays with the original."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agentbridge.core.tasks import TaskIntegrityError, TaskNotFound
from agentbridge.workspace.host_instances import LEGACY_INSTANCE_ID
from tests.test_workspace_host_dispatch_routing import workspace


def make_task(app, account, instance, title):
    with app.store._connect() as db:
        db.execute("BEGIN")
        binding = app.hosts.resolve(
            db, account["account_id"], instance_id=instance, existing=True
        )
    run = str(uuid4())
    task, _ = app.service.tasks.ensure_task(
        user_subject=account["user_subject"],
        agent_host=binding["host_type"],
        host_task_key=run,
        origin_endpoint_id=binding["endpoint_id"],
        active_conversation_ref=binding["session_key"],
        title=title,
    )
    return task


def check_sources(app, account, visible, hidden):
    visible, hidden = visible["task_id"], hidden["task_id"]
    assert {t["task_id"] for t in app.list_tasks(account)} == {visible}
    assert {t["task_id"] for t in app.task_history(account)["items"]} == {visible}
    page = app.task_page(account)
    assert page["activeCount"] == 1 and {t["task_id"] for t in page["items"]} == {
        visible
    }
    history = app.history_page(account)
    assert {c["task"]["task_id"] for c in history["cards"]} == {visible}
    for current in (app.current(account), app.current(account, history_limit=1)):
        assert {c["task"]["task_id"] for c in current["cards"]} == {visible}
    assert {e["task_id"] for e in app.list_events(account)} == {visible}
    assert {
        e["task_id"] for e in app.list_timeline(account, entry_type="task_event")
    } == {visible}
    projections = app.task_projections(account, [visible, hidden])
    assert [c["taskId"] for c in projections["items"]] == [visible]
    assert projections["unavailableTaskIds"] == [hidden]


def test_default_explicit_view_original_task_cursor_and_revocation(workspace):
    app, account = workspace
    alice = account["account_id"]
    oc = make_task(app, account, LEGACY_INSTANCE_ID, "原宿主活动")
    nano = make_task(app, account, "office", "NanoBot 工作台任务")
    check_sources(app, account, oc, nano)
    check_sources(app, {**account, "_workspace_view_instance": "office"}, nano, oc)
    old = app.hosts.set_default(alice, "office", expected_revision=1)
    check_sources(app, account, nano, oc)
    assert app.task_detail(account, oc["task_id"])["task"]["task_id"] == oc["task_id"]
    app.hosts.set_default(alice, LEGACY_INSTANCE_ID, expected_revision=old["revision"])
    assert (
        app.task_detail(account, nano["task_id"])["task"]["task_id"] == nano["task_id"]
    )
    assert app.store.task_host(account, nano["task_id"])["instance_id"] == "office"
    app.hosts.save_instance(
        "office",
        host_type="nanobot",
        display_name="办公实例",
        route_ref="route-office",
        adapter_version="0.2.0",
        state="draining",
        subjects=["user-a"],
        expected_revision=1,
        actor="admin",
        reason="fixture",
    )
    assert (
        app.store.view_instance_id({**account, "_workspace_view_instance": "office"})
        == "office"
    )
    app.hosts.save_instance(
        "office",
        host_type="nanobot",
        display_name="办公实例",
        route_ref="route-office",
        adapter_version="0.2.0",
        state="revoked",
        subjects=["user-a"],
        expected_revision=2,
        actor="admin",
        reason="fixture",
    )
    assert app.list_tasks({**account, "_workspace_view_instance": "office"}) == []
    with pytest.raises(TaskNotFound):
        app.task_detail(account, nano["task_id"])
    with pytest.raises(TaskNotFound):
        app.reissue_artifact(account, task_id=nano["task_id"], artifact_id=str(uuid4()))
    with pytest.raises(TaskNotFound):
        app.continue_task(account, nano["task_id"])


def test_two_instances_of_one_host_type_do_not_mix_and_cursor_pins_the_view(workspace):
    app, account = workspace
    aid = account["account_id"]
    app.hosts.save_instance(
        "other",
        host_type="nanobot",
        display_name="第二实例",
        route_ref="route-other",
        adapter_version="0.3.0",
        state="enabled",
        subjects=["user-a"],
        expected_revision=0,
        actor="admin",
        reason="fixture",
    )
    endpoint, _ = app.service.tasks.ensure_endpoint(
        user_subject="user-a",
        agent_host="nanobot",
        account_id=aid,
        token_id="other",
        client_type="web",
        external_subject=aid,
        endpoint_key="workspace:other:" + aid,
        conversation_ref="other-conversation",
    )
    app.hosts.bind(
        aid, "other", endpoint_id=endpoint["endpoint_id"], credential_ref="other"
    )
    first = make_task(app, account, "office", "一")
    second = make_task(app, account, "other", "二")
    first_view = {**account, "_workspace_view_instance": "office"}
    second_view = {**account, "_workspace_view_instance": "other"}
    check_sources(app, first_view, first, second)
    check_sources(app, second_view, second, first)
    make_task(app, account, "office", "同宿主第二项")
    cursor = app.task_page(first_view, limit=1)["nextCursor"]["cursor"]
    with pytest.raises(ValueError, match="翻页"):
        app.task_page(second_view, cursor=cursor)
    artifact = app.service.tasks.link_artifact(
        task_id=first["task_id"],
        user_subject="user-a",
        artifact={
            "artifact_type": "file",
            "filename": "first.txt",
            "content_type": "text/plain",
            "byte_size": 1,
            "download_url": "https://central.example/a",
            "source_ref": "1",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
        },
    )[0]
    assert any(
        row["task"]["task_id"] == artifact["task_id"]
        for row in app.artifact_history(first_view)
    )
    assert app.artifact_history(second_view) == []


def test_confirmed_receipt_matches_run_lease_and_untouched_task_while_history_stays_authoritative(
    workspace,
):
    app, account = workspace
    s = app.service

    def setup():
        t = make_task(app, account, "office", "NanoBot 工作台任务")
        l = s.host_contract.acquire_coordinator_lease(
            task_id=t["task_id"],
            user_subject="user-a",
            host_instance_id="office",
            agent_host="nanobot",
        )
        c = {
            "hostRunId": t["host_task_key"],
            "hostInstanceId": "office",
            "endpointId": t["origin_endpoint_id"],
            "coordinatorLeaseVersion": str(l["version"]),
        }
        return t, c

    t, c = setup()

    def finish(task, context, outcome="succeeded"):
        return s.tasks.finish_nanobot_read(
            user_subject="user-a",
            task_id=task["task_id"],
            context=context,
            outcome=outcome,
        )["status"]

    for changed in (
        {"hostInstanceId": "other"},
        {"hostRunId": str(uuid4())},
        {"endpointId": str(uuid4())},
        {"coordinatorLeaseVersion": "2"},
    ):
        with pytest.raises(TaskIntegrityError):
            finish(t, {**c, **changed})
    assert finish(t, c) == finish(t, c) == "succeeded"
    denied, denied_context = setup()
    assert finish(denied, denied_context, "failed") == "failed"
    assert (
        sum(
            e["event_type"] == "task.completed"
            for e in s.tasks.list_events(task_id=t["task_id"], user_subject="user-a")
        )
        == 1
    )
    for state in ("expired", "outcome_unknown", "waiting_user", "running"):
        original, context = setup()
        with s.tasks._connect() as db:
            db.execute(
                "UPDATE agent_tasks SET status=? WHERE task_id=?",
                (state, original["task_id"]),
            )
        assert finish(original, context) == state
    t, c = setup()
    with s.tasks._connect() as db:
        db.execute(
            "UPDATE agent_tasks SET summary_json=? WHERE task_id=?",
            ('{"command":{"state":"pending"}}', t["task_id"]),
        )
    assert finish(t, c) == "active"

    for table, column in (
        ("task_interactions", "interaction_id"),
        ("task_operations", "operation_id"),
    ):
        original, context = setup()
        with s.tasks._connect() as db:
            db.execute(
                f"INSERT INTO {table}(task_id,{column},user_subject,linked_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                (original["task_id"], str(uuid4()), "user-a"),
            )
        assert finish(original, context) == "active"
    original, context = setup()
    with s.tasks._connect() as db:
        db.execute(
            "UPDATE agent_task_coordinator_leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
            (original["task_id"],),
        )
    with pytest.raises(TaskIntegrityError):
        finish(original, context)
    assert (
        s.tasks.get_task(original["task_id"], user_subject="user-a")["status"]
        == "active"
    )
