"""Task discovery uses isolated ledgers; no downstream or model calls."""
from datetime import datetime, timezone
import threading
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.test_workspace import PASSWORD, _cookies, _create_account, _free_port, _request, _service


@pytest.fixture
def context(tmp_path):
    service = _service(str(tmp_path))
    alice = _create_account(service, user_subject="user-a", username="alice", endpoint_key="search-alice")
    bob = _create_account(service, user_subject="user-b", username="bob", endpoint_key="search-bob")
    app = WorkspaceApplication(service=service)
    try:
        yield app, alice, bob
    finally:
        app.close()


def seed(app, account, *, title="任务", status="succeeded", updated="2026-11-09T12:00:00.123456+00:00",
         created="2026-10-01T00:00:00+00:00", system=None):
    task_id = str(uuid4())
    with app.service.tasks._connect() as db:
        db.execute("INSERT INTO agent_tasks (task_id,user_subject,agent_host,host_task_key,origin_endpoint_id,"
                   "title,status,summary_json,active_conversation_ref,version,created_at,updated_at) "
                   "VALUES (?,?,'openclaw',?,?,?,?,'{}',?,1,?,?)",
                   (task_id, account["user_subject"], task_id, account["endpoint_id"], title, status,
                    account["openclaw_session_key"], created, updated))
    if system:
        operation, _ = app.service.operations.create(user_subject=account["user_subject"],
            capability_name=f"{system}.synthetic.read", capability_version="1.0.0", input_summary={})
        # Keep seeded task timestamps/status independent from observation maintenance.
        with app.service.tasks._connect() as db:
            db.execute("INSERT INTO task_operations (task_id,operation_id,user_subject,linked_at) VALUES (?,?,?,?)",
                       (task_id, operation["operation_id"], account["user_subject"], updated))
    return task_id


def ids(page):
    return [row["task_id"] for row in page["items"]]


def test_more_than_150_tasks_same_timestamp_have_complete_owned_pages(context):
    app, alice, bob = context
    wanted = {seed(app, alice, title=f"历史任务{i}") for i in range(171)}
    seed(app, bob, title="历史任务属于他人")
    seen = []
    page = app.task_page(alice)
    assert len(page["items"]) == 32 and page["hasMore"]
    assert page["timeZone"] == "Asia/Shanghai"
    while True:
        seen.extend(ids(page))
        if not page["hasMore"]:
            assert page["nextCursor"] is None
            break
        page = app.task_page(alice, cursor=page["nextCursor"]["cursor"])
    assert len(seen) == len(set(seen)) == 171
    assert set(seen) == wanted
    assert seen == sorted(seen, reverse=True)
    # Existing nonpaged application and store callers retain list semantics.
    assert isinstance(app.list_tasks(alice, limit=150), list)
    assert len(app.list_tasks(alice, limit=150)) == 150
    assert isinstance(app.service.tasks.list_tasks(user_subject="user-a"), list)


def test_literal_title_search_mixed_filters_and_shanghai_updated_day(context):
    app, alice, bob = context
    target = seed(app, alice, title="核对100%_完成 ABC", status="failed", system="oa",
                  updated="2026-11-09T15:59:59.999999+00:00")
    at_start = seed(app, alice, title="核对100%_完成 abc", status="waiting_user", system="oa",
                    updated="2026-11-08T16:00:00+00:00")
    seed(app, alice, title="核对100XX完成 abc", status="failed", system="oa")
    seed(app, alice, title="核对100%_完成 abc", status="failed", system="taihua")
    seed(app, alice, title="核对100%_完成 abc", status="succeeded", system="oa")
    seed(app, alice, title="核对100%_完成 abc", status="failed", system="oa",
         updated="2026-11-09T16:00:00+00:00")
    seed(app, alice, title="核对100%_完成 abc", status="failed", system="oa",
         updated="2026-11-08T15:59:59.999999+00:00")
    seed(app, bob, title="核对100%_完成 abc", status="failed", system="oa")
    query = dict(q="100%_完成 abC", system_id="oa", status="attention",
                 updated_from="2026-11-09", updated_to="2026-11-09", limit=1)
    page = app.task_page(alice, **query)
    assert ids(page) == [target] and page["hasMore"]
    assert ids(app.task_page(alice, **query, cursor=page["nextCursor"]["cursor"])) == [at_start]
    assert app.task_page(alice, q="' OR 1=1 --")["items"] == []
    assert app.task_page(alice, q="不存在")["items"] == []


def test_status_groups_keep_unknown_distinct_from_success(context):
    app, alice, _ = context
    now = datetime.now(timezone.utc).isoformat()
    states = {name: seed(app, alice, status=name, created=now, updated=now)
              for name in ("active", "running", "waiting_user", "succeeded", "failed", "outcome_unknown",
                           "partially_succeeded", "expired", "canceled", "superseded")}
    assert set(ids(app.task_page(alice, status="attention"))) == {states[s] for s in ("waiting_user", "failed", "outcome_unknown")}
    assert set(ids(app.task_page(alice, status="active"))) == {states[s] for s in ("active", "running", "waiting_user")}
    assert ids(app.task_page(alice, status="completed")) == [states["succeeded"]]
    assert ids(app.task_page(alice, status="outcome_unknown")) == [states["outcome_unknown"]]
    assert ids(app.task_page(alice, status="canceled")) == [states["canceled"]]
    assert ids(app.task_page(alice, status="partially_succeeded")) == [states["partially_succeeded"]]
    assert app.task_page(alice, status="failed", q="不存在")["activeCount"] == 3
    with app.service.tasks._connect() as db:
        db.executemany("INSERT INTO agent_tasks (task_id,user_subject,agent_host,host_task_key,origin_endpoint_id,"
            "title,status,summary_json,active_conversation_ref,version,created_at,updated_at) "
            "VALUES (?,'user-a','openclaw',?,?,'较多运行任务','running','{}',?,1,?,?)",
            ((key, key, alice["endpoint_id"], alice["openclaw_session_key"], now, now)
             for key in (str(uuid4()) for _ in range(501))))
    assert app.task_page(alice, status="failed", q="不存在", limit=1)["activeCount"] == 504


def test_cursor_is_bound_to_account_all_filters_and_unchanged_raw_boundary(context):
    app, alice, bob = context
    for _ in range(3):
        seed(app, alice, title="待核对", status="failed", system="oa", updated="2026-11-09T12:00:00.123+00:00")
    query = dict(q="待核对", status="failed", system_id="oa", updated_from="2026-11-09", updated_to="2026-11-09", limit=1)
    page = app.task_page(alice, **query)
    boundary = page["nextCursor"]
    # Exact fractional timestamp is preserved, avoiding same-time page loops.
    assert boundary["updatedAt"] == "2026-11-09T12:00:00.123+00:00"
    assert len(ids(app.task_page(alice, **query, cursor=boundary["cursor"],
        before_updated_at=boundary["updatedAt"], before_task_id=boundary["taskId"]))) == 1
    for changed in ({"q":"核对"}, {"status":"attention"}, {"system_id":"all"},
                    {"updated_from":"2026-11-08"}, {"updated_to":"2026-11-10"}):
        with pytest.raises(ValueError, match="翻页"):
            app.task_page(alice, **{**query, **changed}, cursor=boundary["cursor"])
    with pytest.raises(ValueError, match="翻页"):
        app.task_page(bob, **query, cursor=boundary["cursor"])
    token = boundary["cursor"]
    with pytest.raises(ValueError, match="翻页"):
        app.task_page(alice, **query, cursor=("A" if token[0] != "A" else "B") + token[1:])
    for arguments in ({"before_updated_at":boundary["updatedAt"]},
                      {"before_updated_at":boundary["updatedAt"], "before_task_id":boundary["taskId"]},
                      {"cursor":token, "before_updated_at":boundary["updatedAt"], "before_task_id":str(uuid4())}):
        with pytest.raises(ValueError, match="翻页"):
            app.task_page(alice, **query, **arguments)
    # A restarted app reuses the same protector, with no cursor table or secret file.
    restarted = WorkspaceApplication(service=app.service)
    try:
        assert restarted.task_page(alice, **query, cursor=token)["items"]
    finally:
        restarted.close()


def test_system_filter_uses_owned_associations_and_other_has_no_title_guess(context):
    app, alice, bob = context
    generic = seed(app, alice, title="OA 数据库分析", status="failed")
    oa = seed(app, alice, title="看不出系统", system="oa")
    database = seed(app, alice, title="另一个普通标题", system="database")
    # Corrupt/foreign association cannot turn Alice's generic task into Bob's OA task.
    foreign = app.service.operations.create(user_subject=bob["user_subject"], capability_name="oa.synthetic.read",
        capability_version="1.0.0", input_summary={})[0]
    with app.service.tasks._connect() as db:
        db.execute("INSERT INTO task_operations VALUES (?,?,?,?)", (generic, foreign["operation_id"],
            bob["user_subject"], "2026-11-09T00:00:00+00:00"))
    assert ids(app.task_page(alice, system_id="oa")) == [oa]
    assert ids(app.task_page(alice, system_id="database")) == [database]
    assert ids(app.task_page(alice, system_id="other")) == [generic]
    # One task can have legitimate associations to multiple systems, with no duplicates.
    op = app.service.operations.create(user_subject=alice["user_subject"], capability_name="taihua.synthetic.read",
        capability_version="1.0.0", input_summary={})[0]
    with app.service.tasks._connect() as db:
        db.execute("INSERT INTO task_operations VALUES (?,?,?,?)", (oa, op["operation_id"], alice["user_subject"],
            "2026-11-09T00:00:00+00:00"))
    assert ids(app.task_page(alice, system_id="taihua")) == [oa]
    assert ids(app.task_page(alice, system_id="oa")) == [oa]


def test_database_group_marker_and_trusted_interaction_are_system_sources(context):
    app, alice, _ = context
    marker = seed(app, alice)
    other = seed(app, alice, title="database_execute")
    login = seed(app, alice)
    interaction = app.service.interactions.register(interaction_type="credential", user_subject=alice["user_subject"],
        system_id="yuque", session_id="synthetic", resource_id="synthetic-login", title="登录", message="请登录",
        display={}, resume_spec={"kind":"synthetic"}, created_at="2026-11-09T00:00:00+00:00", expires_at="2026-11-10T00:00:00+00:00")
    with app.service.tasks._connect() as db:
        db.execute("UPDATE agent_tasks SET summary_json=? WHERE task_id=?", (
            '{"workspaceQueryGroup":{"toolName":"database_execute","turnRef":"synthetic"}}', marker))
        db.execute("INSERT INTO task_interactions (task_id,interaction_id,user_subject,linked_at) VALUES (?,?,?,?)",
            (login, interaction["interaction_id"], alice["user_subject"], "2026-11-09T00:00:00+00:00"))
    assert ids(app.task_page(alice, system_id="database")) == [marker]
    assert ids(app.task_page(alice, system_id="yuque")) == [login]
    assert ids(app.task_page(alice, system_id="other")) == [other]


def test_invalid_filters_are_rejected_before_query(context):
    app, alice, _ = context
    invalid = [{"limit":0}, {"limit":101}, {"limit":True}, {"q":"字" * 201}, {"q":"a\x00b"},
               {"system_id":"oa' OR 1=1"}, {"status":"unknown"}, {"status":[]}, {"system_id":[]},
               {"updated_from":"2026-2-1"}, {"updated_to":"2026-02-30"},
               {"updated_from":"2026-11-10", "updated_to":"2026-11-09"},
               {"updated_to":"9999-12-31"}, {"updated_from":"0001-01-01"},
               {"cursor":"%%%"}, {"cursor":"A" * 1025}, {"active_only":True, "status":"failed"}]
    for values in invalid:
        with pytest.raises(ValueError):
            app.task_page(alice, **values)


def test_task_search_http_auth_paging_filters_and_validation(context):
    app, alice, bob = context
    for _ in range(34):
        seed(app, alice, title="历史100%_", status="failed", system="oa")
    seed(app, bob, title="历史100%_", status="failed", system="oa")
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    server = create_workspace_http_server(config=validate_workspace_server_config(host="127.0.0.1", port=port,
        public_base_url=origin, tls_cert=None, tls_key=None), application=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _request(port, "GET", "/api/tasks")[0] == 401
        status, headers, _ = _request(port, "POST", "/api/login", origin=origin,
            body={"username":"alice", "password":PASSWORD})
        assert status == 200
        cookies = _cookies(headers)
        query = {"q":"100%_", "status":"failed", "system_id":"oa", "updated_from":"2026-11-09", "updated_to":"2026-11-09"}
        status, _, page = _request(port, "GET", "/api/tasks?" + urlencode(query), cookies=cookies)
        assert status == 200 and len(page["items"]) == 32 and page["hasMore"]
        status, _, last = _request(port, "GET", "/api/tasks?" + urlencode({**query,"cursor":page["nextCursor"]["cursor"]}), cookies=cookies)
        assert status == 200 and len(last["items"]) == 2 and not last["hasMore"]
        assert not set(ids(page)) & set(ids(last))
        for invalid in ({"limit":"101"}, {"limit":"many"}, {"status":"unknown"},
                        {"system_id":"not-system"}, {"updated_from":"2026-02-30"}, {"q":"字" * 201}):
            status, _, error = _request(port, "GET", "/api/tasks?" + urlencode(invalid), cookies=cookies)
            assert status == 400 and error["error"]["code"] == "INVALID_REQUEST"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
