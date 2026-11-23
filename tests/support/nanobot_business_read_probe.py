"""SDK-level non-OA read matrix with real central authorization and synthetic systems."""

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentbridge.adapters.yuque import YuqueCentralAdapter
from agentbridge.database.independent import IndependentDatabase
from tests.database_fixtures import configured_source
from tests.support.business_draft_acceptance import NetworkFreeHttpWorker
from tests.support.nanobot_controlled_write_probe import run
from tests.test_yuque_adapter import FakeYuqueWorker

CASES = {
    "database": (
        "database_execute",
        {"source_id": "matrix_read", "capability": "database.schema", "arguments": {}},
        "matrix_devices",
    ),
    "yuque": ("yuque_document_search", {"query": "设备", "limit": 2}, "对接设备清单"),
    "smartlight": ("smartlight_alarm_list", {"size": 2}, "合成验收控制柜"),
    "taihua": (
        "taihua_work_log_my_list",
        {"start_date": "2026-07-25", "end_date": "2026-07-25", "limit": 2},
        "合成巡查记录",
    ),
}


def exercise(*, fixture, binding, instance, gateway, model):
    db_runtime = IndependentDatabase(fixture.home)
    configured_source(fixture.home, "matrix_read", pack="generic")
    owner = binding["user_subject"]
    db_runtime.grants.set(owner, ["database.schema"], source_id="matrix_read")
    fixture.smartlight.add_alarm("101")
    fixture.transport.data["records"].append(
        {
            "id": 510,
            "logDate": "2026-07-25",
            "hours": 1,
            "content": "合成巡查记录",
            "projectName": "合成巡查项目",
        }
    )
    service = fixture.service

    # The production Yuque parser sees the existing synthetic transport's private snippet.
    class Reader(FakeYuqueWorker, NetworkFreeHttpWorker):
        def __init__(self):
            FakeYuqueWorker.__init__(self)
            NetworkFreeHttpWorker.__init__(self, self)

        def request(self, method, url, **opts):
            response = super().request(method, url, **opts)
            if "/api/mine" in url:
                response["json"]["data"].update(login=owner, name=owner)
            return response

    adapter = YuqueCentralAdapter(
        base_url="https://tc-aiot.yuque.com", organization_id=20020375
    )
    service._adapters_by_system["yuque"] = adapter
    service._adapter_systems["yuque-central"] = "yuque"
    service._worker_factories_by_system["yuque"] = lambda *_: Reader()
    session = service.sessions.get_or_create(
        user_subject=owner, system_id="yuque", expected_principal_ref=owner
    )
    service.sessions.activate(session["session_id"], observed_principal_ref=owner)
    service.session_states.save(session["session_id"], {"cookies": [], "http": {}})
    with service.workspace._connect() as db:
        account = db.execute(
            "SELECT account_id FROM workspace_accounts WHERE user_subject=?",
            ("interaction-other",),
        ).fetchone()[0]
        db.execute("BEGIN")
        other = service.workspace.hosts.resolve(
            db, account, instance_id=instance, existing=True
        )
    result = {}
    # Patch only the SQL driver. Source discovery, grants, user identity and the task are real.
    with patch("agentbridge.database.sources.connect") as execute:
        connection = execute.return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = dict(
            readonly=True,
            repeatable=True,
            default_readonly=True,
            privileged=False,
            writable=False,
            sequence_update=False,
            sequence_usage=False,
            temp=False,
        )
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [SimpleNamespace(name="table_name")]
        cursor.fetchmany.side_effect = [[{"table_name": "matrix_devices"}], []]
        for group, (name, args, expected) in CASES.items():
            for positive, subject_binding in ((True, binding), (False, other)):
                tag = ("allowed-" if positive else "denied-") + group
                key = "read-matrix-" + tag
                verified = []
                call_id = "call-" + tag

                def reply(data):
                    encoded = json.dumps(data, ensure_ascii=False)
                    assert (
                        "super-secret" not in encoded
                        and "synthetic-acceptance-only" not in encoded
                    )
                    names = {item["function"]["name"] for item in data.get("tools", [])}
                    tool = "mcp_ab_" + name
                    seen = [
                        m
                        for m in data["messages"]
                        if m.get("role") == "tool" and m.get("tool_call_id") == call_id
                    ]
                    if seen:
                        text = str(seen[-1]["content"])
                        if (
                            not positive
                            and group == "database"
                            and "DATABASE_CAPABILITY_DENIED" not in text
                        ):
                            raise ValueError("database_permission_denial_not_confirmed")
                        if (expected in text) is not positive:
                            raise ValueError("unexpected_projected_read_outcome")
                        verified.append(
                            "central-data" if positive else "central-denial"
                        )
                        return "合成结果-" + tag, None
                    if tool not in names:
                        if positive:
                            raise ValueError("authorized_tool_missing")
                        verified.append("not-projected")
                        return "合成结果-" + tag, None
                    return None, {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": tool, "arguments": json.dumps(args)},
                    }

                model.reply = reply
                before = {t["task_id"] for t in service.tasks.list_tasks(user_subject=subject_binding["user_subject"])}
                try:
                    events = list(
                        gateway.send_dispatch(
                            {"idempotency_key": key},
                            subject_binding,
                            "请执行合成只读检查 " + tag,
                            [],
                            None,
                        )
                    )
                except Exception as exc:
                    raise AssertionError(
                        (tag, model.failures, execute.call_count)
                    ) from exc
                assert events[-1].get("text") == "合成结果-" + tag and verified, (
                    tag,
                    events[-1].get("state"),
                    model.failures,
                )
                result[tag] = verified[-1]
                if group == "database":
                    created = [t for t in service.tasks.list_tasks(user_subject=subject_binding["user_subject"])
                               if t["task_id"] not in before]
                    assert [t["status"] for t in created] == ["succeeded" if positive else "failed"], (
                        tag, [t["status"] for t in created])
        # The session-status endpoint has no central task or write workflow.
        before = {t["task_id"] for t in service.tasks.list_tasks(user_subject=owner)}
        observed = []
        def status_reply(data):
            tool = "mcp_ab_oa_session_status"
            responses = [m for m in data["messages"] if m.get("tool_call_id") == "session-only-probe"]
            if responses:
                assert "userSubject" in str(responses[-1]["content"]) and owner in str(responses[-1]["content"])
                observed.append(True)
                return "会话状态已读取。", None
            return None, {"id":"session-only-probe","type":"function","function":{"name":tool,"arguments":"{}"}}
        model.reply = status_reply
        events = list(gateway.send_dispatch({"idempotency_key":"session-status-only-check"}, binding,
            "请查看我的办公会话状态。", [], None))
        assert observed and events[-1].get("text") == "会话状态已读取。"
        assert {t["task_id"] for t in service.tasks.list_tasks(user_subject=owner)} == before
        assert execute.call_count == 1
    assert not fixture.smartlight.data["writes"] and not fixture.transport.write_calls
    return {
        "sdk": "nanobot-ai 0.4.2",
        "source": "isolated synthetic downstreams",
        "matrix": result,
        "sqlReads": 1,
        "databaseAllowedTask": "succeeded",
        "databaseDeniedTask": "failed",
        "sessionStatusTasksCreated": 0,
        "businessWrites": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk-python", required=True, type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.sdk_python, False, browser=True, exercise=exercise),
            ensure_ascii=False,
        )
    )
