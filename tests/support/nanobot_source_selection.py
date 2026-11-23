"""Drive an OA selection through the original NanoBot instance and central command ledger."""
import argparse
import json
from pathlib import Path
import time
from uuid import uuid4

from tests.support.nanobot_controlled_write_probe import run
from agentbridge.core.actionable_results import pending_preparations


def exercise(*, fixture, binding, instance, gateway, model, browser=False):
    service = fixture.service
    account = fixture.account
    subject = account["user_subject"]
    fixture.oa.add_affair("synthetic-selection", profile="attendance_confirmation")
    tool = "mcp_ab_oa_workflow_pending_list"
    call_id = uuid4().hex
    def query(body):
        if tool not in {t["function"]["name"] for t in body["tools"]}:
            raise ValueError("read_not_visible")
        if any(m.get("role") == "tool" and m.get("tool_call_id") == call_id for m in body["messages"]):
            return "已完成本次合成待办查询。", None
        return None, {"id": call_id, "type": "function",
                     "function": {"name": tool, "arguments": "{}"}}
    model.reply = query
    events = list(gateway.send_dispatch({"idempotency_key": "source-selection"}, binding, "查询合成 OA 待办。", [], None))
    assert events[-1]["state"] == "final", ("read_failed", model.failures)
    with service.tasks._connect() as db:
        source = db.execute("SELECT * FROM agent_tasks WHERE host_task_key=? AND user_subject=? AND agent_host='nanobot'",
                            (events[0]["runId"], subject)).fetchone()
    assert source is not None, "missing_source_task"
    assert source["status"] == "succeeded", ("read_task_status", source["status"])
    source_id = source["task_id"]
    from agentbridge.workspace.application import WorkspaceApplication
    app = WorkspaceApplication(service=service, host_routes={instance: gateway})
    groups = app.actionable_results(account, source_id)
    assert groups and groups[0]["targets"], "missing_authoritative_query"
    group = groups[0]
    selected = next(row for row in group["targets"] if row["enabled"])
    command = {"operationId": group["operationId"], "targetKeys": [selected["targetKey"]], "requestKey": uuid4().hex}
    # The original source task remains in charge even after selecting the other default.
    choices = app.hosts.choices(account["account_id"])
    app.hosts.set_default(account["account_id"], "openclaw-default", expected_revision=choices["revision"])
    if browser:
        from tests.support.nanobot_write_workspace import start_workspace
        servers, links = start_workspace(fixture, app)
        try:
            print(json.dumps({"workspace": links["a"]["signin"]}), flush=True)
            while True:
                time.sleep(30)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            app.close()
    receipt = app.prepare_selected_results(account, source_id, command)
    task_id = receipt["taskId"]
    for _ in range(80):
        task = service.tasks.get_task(task_id, user_subject=subject)
        if task["summary"]["command"]["state"] != "pending":
            break
        time.sleep(.25)
    assert task["agent_host"] == "nanobot" and task["origin_endpoint_id"] == binding["endpoint_id"]
    assert task["summary"]["command"]["state"] == "requires_user_action", ("prepare_state", task["summary"]["command"]["state"])
    repeat = app.prepare_selected_results(account, source_id, command)
    assert repeat["taskId"] == task_id
    with service.tasks._connect() as db:
        count = db.execute("SELECT COUNT(*) FROM task_operations WHERE task_id=?", (task_id,)).fetchone()[0]
    assert count == 1 and not fixture.matrix["effects"]
    assert all(item["taskId"] != task_id for item in pending_preparations(service, subject))
    app.close()
    return {"read_task_state": source["status"], "original_instance_prepared": True, "same_command": True,
            "preparation_operations": count, "downstream_writes": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nanobot-python", required=True, type=Path)
    parser.add_argument("--browser", action="store_true")
    options = parser.parse_args()
    from functools import partial
    print(json.dumps(run(options.nanobot_python, False, browser=True, exercise=partial(exercise, browser=options.browser))))
