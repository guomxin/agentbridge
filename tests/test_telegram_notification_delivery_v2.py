"""Prospective generic notifications: immutable event, paired send permit and recovery."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentbridge.core import actionable_results as actions
from agentbridge.core import telegram_notification_delivery as delivery
from tests.test_workspace_query_projection import openclaw, OWNER  # noqa: F401
from tests.test_workspace_query_delivery import (setup, poll, drain, close, deadline, ORIGIN, TASK, ENDPOINT, PAIR)  # noqa: F401
from nanobot_agentbridge.notifications import TerminalMessages, notifications
from nanobot_agentbridge.telegram import TelegramRejected, TelegramUnavailable
from agentbridge.workspace.gateway import GatewayRequestError


def new_task(fixture):
    return fixture.service.tasks.ensure_task(user_subject=OWNER,agent_host="openclaw",host_task_key=str(uuid4()),
        origin_endpoint_id=fixture.account["endpoint_id"],active_conversation_ref=fixture.account["openclaw_session_key"],
        title="Sensitive title must never become public notification")[0]["task_id"]


def claim(fixture,token,endpoint,activate=True):
    return [item for item in fixture.service.claim_host_notifications(user_subject=OWNER,agent_host="openclaw",
        endpoint_key=endpoint["endpoint_key"],host_instance_id="openclaw-gateway",include_workspace_chat=True,
        **({"notification_token_id":token} if activate else {}))["notifications"] if item["deliveryMode"] == delivery.MODE]


def scope(token,endpoint,item):
    return dict(subject=OWNER,host="openclaw",instance="openclaw-gateway",token_id=token,endpoint_key=endpoint["endpoint_key"],
                pairing_ref=item["pairingRef"],delivery_id=item["deliveryId"],attempt_count=item["attemptCount"])


def complete(fixture,task):
    fixture.service.tasks.complete_task(task_id=task,user_subject=OWNER,reason="synthetic completion")


def test_activation_never_migrates_old_queue_and_new_events_require_unique_begin(openclaw):
    fixture,token,endpoint=openclaw
    old=new_task(fixture)
    complete(fixture,old)
    assert not claim(fixture,token,endpoint)
    task=new_task(fixture)
    complete(fixture,task)
    assert not claim(fixture,token,endpoint,activate=False)  # Old consumer cannot claim the new protocol.
    item,=claim(fixture,token,endpoint)
    assert item["sourceTaskId"] == task and item["notificationKind"] == "public_status"
    args=scope(token,endpoint,item)
    call=lambda action,**kw:delivery.handle(fixture.service,**(args|kw),action=action)
    assert call("begin") == {"allowed":False}
    issued=call("issue")
    assert issued["sourceEventId"] == item["sourceEventId"]
    assert issued["publicText"] == delivery.PUBLIC["task.completed"]
    assert "Sensitive" not in str(issued) and "interaction" not in issued
    assert call("begin") == {"allowed":True}
    assert call("begin") == {"allowed":False}
    assert call("ack",outcome="sent",receipt=4) == {"recorded":True}
    assert call("ack",outcome="sent",receipt=5) == {"recorded":False}
    with fixture.service.tasks._connect() as db:
        assert db.execute("SELECT count(*) FROM notification_outbox WHERE task_id=? AND json_extract(payload_json,'$.eventType')='task.completed'",(old,)).fetchone()[0] >= 1
        assert not db.execute("SELECT 1 FROM telegram_notification_deliveries WHERE task_id=?",(old,)).fetchone()


@pytest.mark.parametrize("change",["pair","subject","token","instance","attempt"])
def test_original_scope_and_revoked_receipt_boundaries(openclaw,change):
    fixture,token,endpoint=openclaw
    claim(fixture,token,endpoint)
    task=new_task(fixture);complete(fixture,task)
    item,=claim(fixture,token,endpoint)
    args=scope(token,endpoint,item)
    assert delivery.handle(fixture.service,**args,action="issue")["available"]
    assert delivery.handle(fixture.service,**args,action="begin")["allowed"]
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?",(endpoint["endpoint_id"],))
    assert delivery.handle(fixture.service,**args,action="issue") == {"available":False}
    assert delivery.handle(fixture.service,**args,action="begin") == {"allowed":False}
    changed={"pair":{"pairing_ref":"b"*64},"subject":{"subject":"second"},"token":{"token_id":"other"},
             "instance":{"instance":"other"},"attempt":{"attempt_count":2}}[change]
    assert delivery.handle(fixture.service,**(args|changed),action="ack",outcome="sent",receipt=4)=={"recorded":False}
    assert delivery.handle(fixture.service,**args,action="ack",outcome="sent",receipt=4)=={"recorded":True}


def test_unknown_is_closed_and_authoritative_later_event_has_a_new_identity(openclaw):
    fixture,token,endpoint=openclaw
    claim(fixture,token,endpoint)
    task=new_task(fixture)
    fixture.service.tasks.mark_task_outcome_unknown(task_id=task,user_subject=OWNER,error_code="VERIFY_UNAVAILABLE")
    old,=claim(fixture,token,endpoint)
    args=scope(token,endpoint,old)
    assert delivery.handle(fixture.service,**args,action="issue")["available"]
    assert delivery.handle(fixture.service,**args,action="begin")["allowed"]
    assert delivery.handle(fixture.service,**args,action="ack",outcome="delivery_unknown")["recorded"]
    # The operation observer emits a distinct event after authoritative verification;
    # transport cannot reopen or rewrite the original unknown task/receipt.
    op,_=fixture.service.operations.create(user_subject=OWNER,capability_name="synthetic.read",capability_version="1",input_summary={})
    verified=fixture.service.operations.mark_succeeded(op["operation_id"],{"verified":True})
    fixture.service.tasks.link_operation(task_id=task,user_subject=OWNER,operation=verified)
    new,=claim(fixture,token,endpoint)
    assert new["sourceEventId"] != old["sourceEventId"] and new["deliveryId"] != old["deliveryId"]
    assert new["sourceTaskId"] == old["sourceTaskId"]
    with fixture.service.tasks._connect() as db:
        assert db.execute("SELECT state,outcome FROM telegram_notification_deliveries WHERE delivery_id=?",(old["deliveryId"],)).fetchone()[:] == ("unknown","delivery_unknown")
        assert db.execute("SELECT status FROM agent_tasks WHERE task_id=?",(task,)).fetchone()[0] == "outcome_unknown"
    assert delivery.handle(fixture.service,**args,action="begin") == {"allowed":False}


def test_explicit_retry_retains_event_and_never_resends_unknown(openclaw):
    fixture,token,endpoint=openclaw;claim(fixture,token,endpoint)
    task=new_task(fixture);complete(fixture,task)
    item,=claim(fixture,token,endpoint);args=scope(token,endpoint,item)
    assert delivery.handle(fixture.service,**args,action="issue")["available"]
    assert delivery.handle(fixture.service,**args,action="begin")["allowed"]
    assert delivery.handle(fixture.service,**args,action="ack",outcome="retry_after",retry_after_seconds=60)["recorded"]
    assert not claim(fixture,token,endpoint)
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE telegram_notification_deliveries SET due=?",((actions._now()-timedelta(seconds=1)).isoformat(),))
    retry,=claim(fixture,token,endpoint)
    assert retry == item|{"attemptCount":2}


def test_trusted_credential_uses_original_governance_and_rechecks_expiry(openclaw):
    fixture,token,endpoint=openclaw;claim(fixture,token,endpoint)
    task=new_task(fixture)
    session=fixture.service.sessions.find(user_subject=OWNER,system_id="oa")
    fixture.service.sessions.mark_expired(session["session_id"],"synthetic expiry")
    response=fixture.service.start_login(user_subject=OWNER,expected_principal_ref=OWNER,card_base_url="https://fixture.invalid",system_id="oa",task_id=task)
    raw=response["interaction"]
    record=fixture.service.interactions.get(raw["interactionId"],user_subject=OWNER)
    fixture.service.tasks.link_interaction(task_id=task,user_subject=OWNER,interaction_record=record,interaction=raw)
    item,=claim(fixture,token,endpoint);args=scope(token,endpoint,item)
    issued=delivery.handle(fixture.service,**args,action="issue")
    assert issued["notificationKind"] == "trusted_interaction" and "publicText" not in issued
    assert issued["interaction"]["interactionId"] == raw["interactionId"]
    assert issued["interaction"]["presentation"]["endpointId"] == endpoint["endpoint_id"]
    challenge=fixture.service._credential_challenge_id(record)
    assert fixture.service.challenges.get(challenge)["state"] == "pending"
    with fixture.service.tasks._connect() as db:
        db.execute("UPDATE auth_challenges SET expires_at='2001-01-01T00:00:00+00:00' WHERE challenge_id=?",(challenge,))
    assert delivery.handle(fixture.service,**args,action="begin") == {"allowed":False}


def notification(setup,kind="public_status"):
    pilot,terminal,gateway,binding=setup
    event=str(uuid4())
    gateway.event={"kind":"host_notification_v2","deliveryId":gateway.event["deliveryId"],"attempt":1,"pairingRef":PAIR,
                   "sourceEventId":event,"sourceTaskId":TASK,"notificationKind":kind}
    gateway.result={"available":True,"leaseExpiresAt":deadline(),"sourceEventId":event,"sourceTaskId":TASK,"notificationKind":kind}
    if kind == "public_status":
        gateway.result["publicText"]="本次任务已完成，请在原工作台查看结果。"
    else:
        gateway.card_origins={ORIGIN}
        gateway.result["interaction"]={"schemaVersion":"agentbridge.interaction.v1","interactionId":str(uuid4()),
            "type":"credential","state":"pending","title":"登录合成系统","message":"请通过可信卡继续",
            "expiresAt":deadline(),"presentation":{"owner":"agentbridge","url":ORIGIN+"/auth/"+"a"*43,
            "modelMustNotCollectValues":True,"endpointId":ENDPOINT}}
    return pilot,terminal,gateway,binding


@pytest.mark.parametrize("kind",["public_status","trusted_interaction"])
def test_both_hosts_new_notification_uses_event_dedupe_and_original_message(setup,kind):
    pilot,terminal,gateway,binding=notification(setup,kind)
    poll(setup)
    assert len(pilot.sent)==1
    if kind=="trusted_interaction":
        assert len(pilot.sent[0]["reply_markup"]["inline_keyboard"][0])==2
    assert gateway.actions[-1]["outcome"]=="sent"
    poll(setup);assert len(pilot.sent)==1
    gateway.event["sourceEventId"]=str(uuid4());gateway.event["deliveryId"]=str(uuid4())
    gateway.result["sourceEventId"]=gateway.event["sourceEventId"]
    poll(setup);assert len(pilot.sent)==2  # Same task, new authoritative source event.


@pytest.mark.parametrize("failure",["429","unknown","wrong_event","cross_kind"])
def test_both_hosts_finite_error_classification_and_envelope_separation(setup,failure):
    pilot,_,gateway,_=notification(setup)
    send=pilot.api.call
    if failure in {"429","unknown"}:
        def fail(method,**params):
            if failure=="429":raise TelegramRejected(429,5)
            pilot.sent.append(params);raise TelegramUnavailable("receipt lost")
        pilot.api.call=fail
    elif failure=="wrong_event":gateway.result["sourceEventId"]=str(uuid4())
    else:gateway.result["interaction"]={"presentation":{"url":ORIGIN+"/auth/secret"}}
    poll(setup)
    assert gateway.actions[-1]["outcome"] == {"429":"retry_after","unknown":"delivery_unknown"}.get(failure,"unavailable")
    pilot.api.call=send;gateway.event["attempt"]=2
    with pilot.db:pilot.db.execute("UPDATE telegram_workspace_query_attempts SET not_before=0")
    poll(setup)
    assert len(pilot.sent)==(1 if failure in {"429","unknown"} else 0)


def test_new_notification_recovers_receipt_without_claim_or_pair(setup):
    pilot,terminal,gateway,_=notification(setup)
    request=gateway._request
    def lost(route,body,**options):
        if body.get("action")=="ack":raise GatewayRequestError("GATEWAY_TIMEOUT","receipt lost")
        return request(route,body,**options)
    gateway._request=lost;poll(setup)
    gateway._request=request;pilot.authorized=False
    with pilot.db:pilot.db.execute("UPDATE telegram_workspace_query_attempts SET next_ack=0")
    restarted=TerminalMessages(pilot)
    try:
        restarted.flush_deferred();drain(restarted)
        assert len(pilot.sent)==1 and gateway.actions[-1]["outcome"]=="sent"
        assert pilot.db.execute("SELECT acknowledged FROM telegram_workspace_query_attempts").fetchone()[0]==1
    finally:close(restarted)


@pytest.mark.parametrize("host_type",["nanobot","pi"])
@pytest.mark.parametrize("kind",["public_status","trusted_interaction"])
def test_private_handler_requires_claim_source_and_separates_public_and_trusted(tmp_path,host_type,kind):
    from aiohttp import web
    event=str(uuid4());did=str(uuid4())
    req={"hostType":host_type,"subject":"first","endpoint":ENDPOINT,"endpointKey":"telegram:8001:501",
         "instance":"fixture","workspaceEndpointId":"web-first","userDir":str(tmp_path),"pairingRef":PAIR}
    host=SimpleNamespace(coordinator=True,card_origins={ORIGIN},worker_payload=lambda *_:req)
    response={"available":True,"leaseExpiresAt":deadline(),"sourceEventId":event,"sourceTaskId":TASK,"notificationKind":kind}
    if kind=="public_status":response["publicText"]="任务已完成。"
    else:
        response["interaction"]={"schemaVersion":"agentbridge.interaction.v1","interactionId":str(uuid4()),
            "type":"credential","state":"pending","title":"合成登录","expiresAt":deadline(),
            "presentation":{"owner":"agentbridge","url":ORIGIN+"/auth/"+"a"*43,"endpointId":ENDPOINT,
                            "modelMustNotCollectValues":True}}
    calls=[]
    async def call(name,args,meta):
        calls.append((name,args))
        if name=="agentbridge_host_notification_claim":
            assert args["include_notification_v2"] is True
            value={"endpoint":{"endpointId":ENDPOINT},"notifications":[{"deliveryMode":delivery.MODE,"deliveryId":did,
                "attemptCount":1,"sourceEventId":event,"sourceTaskId":TASK,"notificationKind":kind,"pairingRef":PAIR}]}
        else:
            assert name=="agentbridge_host_notification_delivery" and args["agent_host"]==host_type
            value=response if args["action"]=="issue" else {"recorded":True}
        return SimpleNamespace(isError=False,structuredContent=value)
    @asynccontextmanager
    async def paired(_):yield SimpleNamespace(call_tool=call),{},"L3"
    def route(body):
        return asyncio.run(notifications(host,{"workspaceEndpointId":"web-first"},"telegram-workspace-result-view",body,paired_session=paired))
    item,=asyncio.run(notifications(host,{"workspaceEndpointId":"web-first"},"telegram-notifications",{},paired_session=paired))["deliveries"]
    assert route(item|{"action":"issue"})["available"]
    for changed in ({"sourceTaskId":str(uuid4())},{"sourceEventId":str(uuid4())},{"notificationKind":"public_status" if kind=="trusted_interaction" else "trusted_interaction"}):
        with pytest.raises(web.HTTPForbidden):route(item|changed|{"action":"issue"})
    response["resultView"]={}
    with pytest.raises(web.HTTPBadGateway):route(item|{"action":"issue"})
    response.pop("resultView")
    if kind=="public_status":response["interaction"]={}
    else:response["publicText"]="不能混入公开镜像"
    with pytest.raises(web.HTTPBadGateway):route(item|{"action":"issue"})


def test_three_real_central_pair_types_pin_and_settle_separately(tmp_path):
    import os
    from unittest.mock import patch
    from agentbridge.core.endpoint_instances import endpoint_instances
    from tests.support.workspace_query_acceptance import Fixture,INSTANCES
    with patch.dict(os.environ):
        fixture=Fixture(tmp_path/"three-host-notifications",deliveries=False)
        try:
            service=fixture.service;subject=fixture.owner
            ids=[]
            for host,instance in INSTANCES.items():
                token=fixture.tokens[host,subject]
                with service.tasks._connect() as db:
                    ownership=endpoint_instances(db,subject)
                    endpoints=[dict(row) for row in db.execute("SELECT * FROM client_endpoints WHERE user_subject=? AND agent_host=?",(subject,host))
                               if ownership.get(row["endpoint_id"])==(host,instance)]
                target,= [e for e in endpoints if e["client_type"]=="telegram"]
                source,= [e for e in endpoints if e["client_type"] in {"web","webchat"}]
                args=dict(user_subject=subject,agent_host=host,host_instance_id=instance,endpoint_key=target["endpoint_key"],
                          notification_token_id=token,include_workspace_chat=True)
                service.claim_host_notifications(**args)
                task,_=service.tasks.ensure_task(user_subject=subject,agent_host=host,host_task_key=str(uuid4()),
                    origin_endpoint_id=source["endpoint_id"],active_conversation_ref=source["conversation_ref"],title="合成公共通知")
                service.tasks.complete_task(task_id=task["task_id"],user_subject=subject,reason="synthetic complete")
                event,=[i for i in service.claim_host_notifications(**args)["notifications"] if i["deliveryMode"]==delivery.MODE]
                control=dict(subject=subject,host=host,instance=instance,endpoint_key=target["endpoint_key"],token_id=token,
                    pairing_ref=event["pairingRef"],delivery_id=event["deliveryId"],attempt_count=event["attemptCount"])
                assert delivery.handle(service,**control,action="issue")["available"]
                assert delivery.handle(service,**(control|{"subject":fixture.central.subjects["b"]}),action="issue")=={"available":False}
                assert delivery.handle(service,**control,action="begin")=={"allowed":True}
                assert delivery.handle(service,**control,action="ack",outcome="sent",receipt=7)=={"recorded":True}
                ids.append(event["deliveryId"])
            assert len(set(ids))==3
        finally:fixture.close()


def test_query_responsibility_keeps_same_terminal_silent_but_preserves_event(openclaw):
    from agentbridge.core import quick_queries
    fixture,token,endpoint=openclaw;claim(fixture,token,endpoint)
    result=quick_queries.queue_query(fixture.service,fixture.account,{"requestKey":uuid4().hex})
    response=actions.execute_preparation(fixture.service,OWNER,result["taskId"],result["commandKey"],runtime_context={
        "endpointId":fixture.account["endpoint_id"],"hostType":"openclaw","hostInstanceId":"openclaw-gateway"})
    assert response["status"]=="succeeded"
    assert claim(fixture,token,endpoint)==[]
    with fixture.service.tasks._connect() as db:
        rows=db.execute("SELECT * FROM telegram_notification_deliveries WHERE task_id=?",(result["taskId"],)).fetchall()
        assert rows and all(r["state"]=="covered" and r["attempt"]==0 for r in rows)
        original=db.execute("SELECT delivery_id FROM workspace_query_deliveries WHERE task_id=?",(result["taskId"],)).fetchone()[0]
        assert all(r["covered_by"]==original for r in rows)


def test_repeated_authoritative_observation_preserves_each_event_without_duplicate_delivery(openclaw):
    fixture,token,endpoint=openclaw;claim(fixture,token,endpoint)
    task=new_task(fixture)
    fixture.service.tasks.mark_task_outcome_unknown(task_id=task,user_subject=OWNER,error_code="VERIFY_UNAVAILABLE")
    unknown,=claim(fixture,token,endpoint)
    op,_=fixture.service.operations.create(user_subject=OWNER,capability_name="synthetic.read",capability_version="1",input_summary={})
    verified=fixture.service.operations.mark_succeeded(op["operation_id"],{"verified":True})
    for _ in range(2):fixture.service.tasks.link_operation(task_id=task,user_subject=OWNER,operation=verified)
    event,=claim(fixture,token,endpoint)
    assert event["sourceEventId"]!=unknown["sourceEventId"]
    with fixture.service.tasks._connect() as db:
        rows=db.execute("SELECT state,covered_by FROM telegram_notification_deliveries WHERE observation_hash IS NOT NULL ORDER BY rowid").fetchall()
        assert [r["state"] for r in rows]==["leased","covered"]
        assert rows[1]["covered_by"]==event["deliveryId"]
        assert db.execute("SELECT count(*) FROM task_events WHERE task_id=? AND event_type='task.operation.succeeded'",(task,)).fetchone()[0]==2


@pytest.mark.parametrize("late,broken_parts",[(False,False),(True,False),(False,True)])
def test_original_public_answer_owns_terminal_only_with_complete_delivery_proof(openclaw,late,broken_parts):
    from tests.test_workspace_text_delivery import admit
    fixture,token,endpoint=openclaw;claim(fixture,token,endpoint)
    service=fixture.service;dispatch=admit(fixture)
    task,_=service.tasks.ensure_task(user_subject=OWNER,agent_host="openclaw",host_task_key=dispatch["idempotency_key"],
        origin_endpoint_id=dispatch["origin_endpoint_id"],active_conversation_ref=dispatch["conversation_ref"],title="原公开答复")
    complete(fixture,task["task_id"])
    if late:
        item,=claim(fixture,token,endpoint);args=scope(token,endpoint,item)
        assert delivery.handle(service,**args,action="issue")["available"]
    service.tasks.append_workspace_response(user_subject=OWNER,source_endpoint_id=dispatch["origin_endpoint_id"],
        message_key="workspace:assistant:"+dispatch["idempotency_key"],text="独立投递的原公开答复")
    with service.tasks._connect() as db:
        original=db.execute("SELECT claim_token FROM agent_host_dispatches WHERE dispatch_id=?",(dispatch["dispatch_id"],)).fetchone()[0]
        if broken_parts:
            db.execute("UPDATE workspace_text_deliveries SET part_count=2 WHERE kind='assistant'")
    service.workspace.finish_host_dispatch(dispatch["dispatch_id"],claim_token=original,state="completed",event={"text":"独立投递的原公开答复"})
    if late:
        assert delivery.handle(service,**args,action="begin")=={"allowed":False}
        assert delivery.handle(service,**args,action="ack",outcome="unavailable")=={"recorded":True}
        assert delivery.handle(service,**args,action="ack",outcome="sent",receipt=3)=={"recorded":False}
    else:
        assert len(claim(fixture,token,endpoint))==(1 if broken_parts else 0)
    if not broken_parts:
        with service.tasks._connect() as db:
            state,covered=db.execute("SELECT state,covered_by FROM telegram_notification_deliveries WHERE task_id=?",(task["task_id"],)).fetchone()
            assert state=="covered"
            assert db.execute("SELECT 1 FROM workspace_text_deliveries WHERE delivery_id=? AND kind='assistant'",(covered,)).fetchone()
