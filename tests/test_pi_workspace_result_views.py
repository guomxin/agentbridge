from datetime import timedelta
from pathlib import Path
import json
from unittest.mock import patch
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from agentbridge.core import actionable_results as actions
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.telegram_result_views import ResultViewService
from agentbridge.core.workspace_result_views import delivery
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.authorization_fixtures import grant_permissions
from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
from tests.test_pi_workspace import private_json, pi  # noqa: F401


def paired_query(pi, *, items=None, coverage="complete", identity=None, extra_result=None, mixed=False):
    svc = pi.service
    owner, instance, bot, sender = "user-a", "pi-office", "8801", "501"
    account = pi.accounts["a"]["account_id"]
    identity = identity or McpIdentityTokenStore(svc.db_path).issue(
        user_subject=owner, expected_principal_ref=owner, label="workspace-pi:pi-office:pi-a")
    grant_permissions(svc.user_grants, owner, ["oa.workflow.read", "oa.attendance_confirmation.approve"])
    svc.skills.save("user:"+owner, {"oa-pending":{"profiles":["single","batch"]}},
                    expected_revision=0, actor="test", reason="synthetic user assignment")
    sess = svc.sessions.get_or_create(user_subject=owner,system_id="oa",expected_principal_ref=owner)
    svc.sessions.activate(sess["session_id"],observed_principal_ref=owner)
    links = TelegramPilotLinks(svc)

    def pair():
        original = (account,instance,bot)
        challenge = links.begin(*original)
        confirmation = links.present(owner,instance,bot,sender,challenge["code"])
        links.confirm(*original,challenge["challenge_id"],confirmation)
        return links.ensure_endpoint(owner,identity["token_id"],instance,bot,sender)

    endpoint = pair()
    svc.workspace.hosts.set_default(account,instance,expected_revision=1)
    disp, _ = svc.workspace.accept_chat_request(account_id=account,user_subject=owner,message="查本人的OA待办",
        idempotency_key=uuid4().hex,attachments=[], tasks=svc.tasks,attachment_store=svc.timeline_attachments,
        media_base_url="https://fixture.invalid",instance_id=instance)
    claim = svc.workspace.claim_next_host_dispatch(account_id=account,claim_owner="fixture")
    run = uuid4().hex
    svc.workspace.mark_host_dispatch_accepted(dispatch_id=disp["dispatch_id"],claim_token=claim["claim_token"],run_id=run)
    task,_ = svc.tasks.ensure_task(user_subject=owner,agent_host="pi",host_task_key=run,
        origin_endpoint_id=disp["origin_endpoint_id"],active_conversation_ref=disp["conversation_ref"],title="合成待办")
    task_id = task["task_id"]
    operation,_ = svc.operations.create(user_subject=owner,capability_name="oa.workflow.pending.list",
        capability_version="1",input_summary={})
    rows = [{"affair_id":"synthetic-01","title":"【HR】月度考勤确认单-合成申请人","category":"协同",
             "sender":"合成申请人","date":"2026-11-26"}] if items is None else items
    if mixed:
        extra, _ = svc.operations.create(user_subject=owner, capability_name="oa.workflow.history.query",
                                         capability_version="1", input_summary={})
        extra = svc.operations.mark_succeeded(extra["operation_id"], {"items":[]})
        svc.tasks.link_operation(task_id=task_id,user_subject=owner,operation=extra)
    completed = svc.operations.mark_succeeded(operation["operation_id"],{
        "items":rows,"coverage":{"status":coverage},**(extra_result or {})})
    svc.tasks.link_operation(task_id=task_id,user_subject=owner,operation=completed)
    for _ in range(2):
        svc.tasks.append_workspace_response(user_subject=owner,source_endpoint_id=disp["origin_endpoint_id"],
            message_key="workspace:assistant:"+run,text="已查询本人待办。")
    scope = {"user_subject":owner,"agent_host":"pi","host_instance_id":instance,"endpoint_key":"telegram:8801:501"}
    normal = svc.claim_host_notifications(**scope,include_workspace_chat=True)["notifications"]
    assert [line["timeline"]["role"] for line in normal if line["deliveryMode"]=="timeline_message"]==["user","assistant"]
    assert not any(item["deliveryMode"]=="workspace_result_view" for item in normal)
    claimed = svc.claim_host_notifications(**scope,include_workspace_result_views=True)["notifications"]
    cards = [x for x in claimed if x["deliveryMode"]=="workspace_result_view"]
    args={"subject":owner,"instance":instance,"endpoint_key":scope["endpoint_key"],"token_id":identity["token_id"],
          "pairing_ref":endpoint["pairingRef"]}
    return cards,args,task_id,operation["operation_id"],endpoint,links,pair


def test_companion_uses_original_web_read_and_current_pair(pi):
    svc=pi.service
    cards,args,task,operation,endpoint,links,pair = paired_query(pi)
    assert len(cards)==1 and cards[0]["sourceTaskId"]==task
    assert set(cards[0])=={"deliveryMode","sourceTaskId","deliveryId","attemptCount","requiresRouteCheck"}
    args.update(delivery_id=cards[0]["deliveryId"],attempt_count=cards[0]["attemptCount"])
    for wrong in ({"subject":"user-b"},{"pairing_ref":"0"*64},{"instance":"other-pi"},
                  {"endpoint_key":"telegram:8801:502"},{"token_id":"unapproved"},{"attempt_count":2}):
        assert delivery(svc,action="issue",**{**args,**wrong})=={"available":False}
    views=ResultViewService(svc)
    with pytest.raises(actions.ActionableResultError):
        views.issue("user-a",task,endpoint["endpointId"])
    assert delivery(svc,action="ack",outcome="sent",receipt=771,**args)=={"recorded":False}
    issued=delivery(svc,action="issue",**args)
    view=issued["resultView"]
    assert issued["available"] and issued["leaseExpiresAt"] and view["sourceTaskId"]==task
    assert view["presentation"]["endpointId"]==endpoint["endpointId"]
    url=view["presentation"]["url"]
    token=url.rsplit("/",1)[1]
    with svc.tasks._connect() as db:
        assert not db.execute("SELECT 1 FROM task_events WHERE instr(payload_json,?)", (token,)).fetchone()
        assert not db.execute("SELECT 1 FROM notification_outbox WHERE instr(payload_json,?)",(token,)).fetchone()
        assert not db.execute("SELECT 1 FROM user_timeline WHERE instr(text,?)",(token,)).fetchone()
    with patch.object(svc,"invoke",side_effect=AssertionError("Reading a card must not query or submit")):
        response=views.read(token)
        assert response["selectionAvailable"] and len(response["groups"])==1
        target=response["groups"][0]["targets"][0]
        assert target["enabled"] and target["title"]=="【HR】月度考勤确认单-合成申请人"
        body={"operationId":operation,"targetKeys":[target["targetKey"]],"requestKey":uuid4().hex}
        with pytest.raises(actions.ActionableResultError):
            views.prepare(token,{**body,"targetKeys":["foreign-target"]})
        follow=views.prepare(token,body)
        assert views.prepare(token,body)["taskId"]==follow["taskId"]
        assert svc.tasks.get_task(follow["taskId"],user_subject="user-a")["origin_endpoint_id"]==endpoint["endpointId"]
        assert views.refresh(token,{"requestKey":uuid4().hex})["query"]["state"]=="active"
    assert delivery(svc,action="ack",outcome="sent",receipt=771,**args)=={"recorded":True}
    assert delivery(svc,action="ack",outcome="sent",receipt=771,**args)=={"recorded":True}
    assert delivery(svc,action="ack",outcome="sent",receipt=772,**args)=={"recorded":False}
    assert svc.tasks.get_delivery(args["delivery_id"])["payload"]["telegramReceipt"]==771
    account=pi.accounts["a"]["account_id"]
    links.revoke(account,"pi-office","8801")
    assert pair()["pairingRef"]!=args["pairing_ref"]
    for call in (lambda:views.read(token),lambda:views.prepare(token,body),lambda:views.refresh(token,{"requestKey":uuid4().hex})):
        with pytest.raises(actions.ActionableResultError):
            call()
    with svc.tasks._connect() as db:
        with pytest.raises(actions.ActionableResultError):
            actions._load_command(db,"user-a",follow["taskId"],follow["commandKey"])
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==1


@pytest.mark.parametrize(("items","coverage"),[([],"complete"),(None,"partial")])
def test_empty_and_partial_web_sources_get_no_companion_card(pi,items,coverage):
    cards,*_=paired_query(pi,items=items,coverage=coverage)
    assert not cards


@pytest.mark.parametrize("options", [
    {"extra_result":{"total":2}},
    {"extra_result":{"coverage":{"status":"complete","hasMore":True}}},
    {"mixed":True},
])
def test_ambiguous_or_mixed_workspace_reads_do_not_create_a_card(pi, options):
    cards, *_ = paired_query(pi, **options)
    assert not cards


def test_expired_source_permission_loss_and_unknown_delivery(pi):
    svc=pi.service
    cards,args,*_=paired_query(pi)
    args.update(delivery_id=cards[0]["deliveryId"],attempt_count=cards[0]["attemptCount"])
    future=actions._now()+timedelta(minutes=11)
    with svc.tasks._connect() as db:
        db.execute("UPDATE notification_outbox SET next_attempt_at=? WHERE delivery_id=?",
                   ((future+timedelta(minutes=1)).isoformat(),args["delivery_id"]))
    with patch.object(actions,"_now",return_value=future):
        assert delivery(svc,action="issue",**args)=={"available":False}
    assert delivery(svc,action="issue",**args)["available"]
    grant_permissions(svc.user_grants,args["subject"],[])
    assert delivery(svc,action="issue",**args)=={"available":False}
    assert delivery(svc,action="ack",outcome="delivery_unknown",**args)=={"recorded":True}
    assert svc.tasks.get_delivery(args["delivery_id"])["state"]=="failed"
    assert delivery(svc,action="issue",**args)=={"available":False}


def test_registered_pi_mcp_controls_are_private_and_require_level_three(pi):
    service = pi.service
    identities = McpIdentityTokenStore(service.db_path)
    first = identities.issue(user_subject="user-a", expected_principal_ref="user-a",
                             label="workspace-pi:pi-office:pi-a")
    second = identities.issue(user_subject="user-b", expected_principal_ref="user-b",
                              label="workspace-pi:pi-office:pi-b")
    items, scope, *_ = paired_query(pi, identity=first)
    item = items[0]
    args = {"action": "issue", "endpoint_key": scope["endpoint_key"], "pairing_ref": scope["pairing_ref"],
            "delivery_id": item["deliveryId"], "attempt_count": item["attemptCount"]}
    version = pi.spec["adapterVersion"]
    definitions = json.loads((Path(__file__).parents[1] / "schemas/agent-host/v1/test-vectors.json").read_text())
    profile = {**definitions["profiles"][0]["value"], "implementation": {"name": "pi", "version": version},
               "hostInstanceId": "pi-office"}
    context = {"version": "1", "agentHost": "pi", "hostInstanceId": "pi-office", "hostVersion": version}
    meta = {"io.agentbridge/host-context": context, "io.agentbridge/host-profile": profile}
    tool = "agentbridge_host_workspace_result_view"
    config = validate_central_mcp_server_config(host="127.0.0.1", port=8790, public_base_url="http://testserver",
                                                tls_cert=None, tls_key=None)
    app = create_central_mcp_server(service=service, identity_store=identities, config=config,
                                    auth_card_base_url="http://127.0.0.1:8780")
    with service.tasks._connect() as db:
        db.execute("INSERT OR REPLACE INTO agent_host_compatibility VALUES ('pi',?,'L3','isolated','fixture','now')",
                   (version,))
    with TestClient(app.streamable_http_app()) as client:
        def call(identity, name, arguments=None, metadata=meta):
            response = client.post("/mcp", headers={"Authorization": "Bearer " + identity["token"],
                "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": name, "arguments": arguments or {}, "_meta": metadata}})
            assert response.status_code == 200
            return response.json()["result"]
        assert call(first, "agentbridge_host_register")["structuredContent"]["acceptedLevel"] == "L1"
        assert call(first, tool, args)["isError"]
        pi.spec["requiredLevel"] = "L3"
        private_json(pi.route_file, pi.manifest)
        for user in (first, second):
            assert call(user, "agentbridge_host_register")["structuredContent"]["acceptedLevel"] == "L3"
        planes = call(first, "agentbridge_server_profile")["structuredContent"]["toolPlanes"]
        assert tool in planes["hostControl"]["levels"]["L3"] and tool not in planes["model"]["toolNames"]
        assert call(first, tool, args, metadata={})["isError"]
        assert call(second, tool, args)["structuredContent"] == {"available": False}
        issued = call(first, tool, args)["structuredContent"]
        assert issued["available"] and issued["resultView"]["sourceTaskId"] == item["sourceTaskId"]
        assert call(first, tool, {**args, "action": "ack", "outcome": "delivery_unknown"})["structuredContent"]["recorded"]
        assert call(first, tool, args)["structuredContent"] == {"available": False}


def test_reclaim_and_changed_source_cannot_resign_or_confirm_an_old_attempt(pi):
    service = pi.service
    cards, args, _, operation, *_ = paired_query(pi)
    item = cards[0]
    args.update(delivery_id=item["deliveryId"], attempt_count=item["attemptCount"])
    scope = dict(user_subject="user-a", agent_host="pi", host_instance_id="pi-office",
                 endpoint_key=args["endpoint_key"], include_workspace_result_views=True)

    def claim_cards():
        return [row for row in service.claim_host_notifications(**scope)["notifications"]
                if row["deliveryMode"] == "workspace_result_view"]

    assert claim_cards() == []
    issued = delivery(service, action="issue", **args)
    assert issued["available"]
    assert delivery(service, action="issue", **args)["available"]
    # Signing is local and read-only. Only the Bot's acknowledged message is a delivery.
    with service.tasks._connect() as db:
        db.execute("UPDATE notification_outbox SET next_attempt_at=? WHERE delivery_id=?",
                   ((actions._now() - timedelta(seconds=1)).isoformat(), item["deliveryId"]))
    reclaimed, = claim_cards()
    assert reclaimed["deliveryId"] == item["deliveryId"] and reclaimed["attemptCount"] == 2
    assert delivery(service, action="issue", **args) == {"available": False}
    assert delivery(service, action="ack", outcome="sent", receipt=123, **args) == {"recorded": False}
    current = {**args, "attempt_count": 2}
    assert delivery(service, action="issue", **current)["available"]
    with service.tasks._connect() as db:
        db.execute("UPDATE operations SET result_json=json_set(result_json,'$.items[0].sender','different') "
                   "WHERE operation_id=?", (operation,))
    assert delivery(service, action="issue", **current) == {"available": False}
    token = issued["resultView"]["presentation"]["url"].rsplit("/", 1)[1]
    with pytest.raises(actions.ActionableResultError):
        ResultViewService(service).read(token)


def test_refresh_descendant_remains_bound_to_original_web_pair(pi):
    service = pi.service
    cards, args, _, original_operation, endpoint, links, pair = paired_query(pi)
    args.update(delivery_id=cards[0]["deliveryId"], attempt_count=cards[0]["attemptCount"])
    views = ResultViewService(service)

    def token(view):
        return view["presentation"]["url"].rsplit("/", 1)[1]

    first = token(delivery(service, action="issue", **args)["resultView"])
    refresh_body = {"requestKey": uuid4().hex}
    refresh = views.refresh(first, refresh_body)
    # Settle only this explicitly requested second synthetic read, without executing a write.
    operation, _ = service.operations.create(user_subject="user-a", capability_name="oa.workflow.pending.list",
                                             capability_version="1", input_summary={})
    with service.tasks._connect() as db:
        original = db.execute("SELECT result_json FROM operations WHERE operation_id=?", (original_operation,)).fetchone()
    completed = service.operations.mark_succeeded(operation["operation_id"], json.loads(original["result_json"]))
    service.tasks.link_operation(task_id=refresh["taskId"], user_subject="user-a", operation=completed)
    second = token(views.refresh(first, refresh_body)["resultView"])
    group, = views.read(second)["groups"]
    command = views.prepare(second, {"requestKey": uuid4().hex, "operationId": operation["operation_id"],
                                     "targetKeys": [group["targets"][0]["targetKey"]]})
    links.revoke(pi.accounts["a"]["account_id"], "pi-office", "8801")
    assert pair()["pairingRef"] != args["pairing_ref"]
    for inquiry in (lambda: views.read(second), lambda: views.refresh(second, {"requestKey": uuid4().hex}),
                    lambda: views.issue("user-a", refresh["taskId"], endpoint["endpointId"])):
        with pytest.raises(actions.ActionableResultError):
            inquiry()
    with service.tasks._connect() as db:
        with pytest.raises(actions.ActionableResultError):
            actions._load_command(db, "user-a", command["taskId"], command["commandKey"])
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 2
