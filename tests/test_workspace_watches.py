"""Real Workspace HTTP/auth/watch storage; only downstream OA observation is synthetic."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import threading
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import patch
from uuid import uuid4

import pytest

from agentbridge.core.business_content_identity import principal_binding
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.authorization_fixtures import grant_permissions
from tests.test_workspace import PASSWORD, _cookies, _create_account, _free_port, _request


def _source(number=1):
    return {"affair_id":str(1000+number), "summary_id":str(2000+number),
            "process_id":str(3000+number), "case_id":str(4000+number)}


def _snapshot(name="合成核对节点"):
    return {"workflow_state":"in_progress", "current_nodes":[{"name":name,"handlers":["合成经办人"]}],
            "current_handlers":["合成经办人"], "observed_at":datetime.now(timezone.utc).isoformat(),
            "unavailable_fields":[], "completeness":"complete", "read_side_effects":["may_mark_read"],
            "background_read_safe":False, "safety_level":"foreground_only_may_mark_read"}


class _Worker:
    def __init__(self, principal): self.principal=principal
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def restore_session_state(self, _value): pass
    def capture_session_state(self): return {"cookies":[], "synthetic":self.principal}


class _HttpContext(SimpleNamespace):
    def __repr__(self):
        return "SyntheticWorkspaceWatchHttpContext()"


@pytest.fixture
def http_context(tmp_path, monkeypatch):
    key=tmp_path/"session.key"; key.write_bytes(os.urandom(32)); key.chmod(0o600)
    monkeypatch.setenv("AGENTBRIDGE_SESSION_KEY_FILE",str(key))
    workers=[]
    def factory(session, _adapter):
        worker=_Worker(session["expected_principal_ref"]); workers.append(worker); return worker
    service=CentralCapabilityService(home=tmp_path,base_url="https://oa.example.test/seeyon/",worker_factory=factory)
    accounts,sessions={},{}
    for subject,username,principal in (("user-a","watch_http_alice","Alice"),("user-b","watch_http_bob","Bob")):
        grant_permissions(service.user_grants,subject,["oa.workflow.read","oa.workflow.watch"])
        accounts[subject]=_create_account(service,user_subject=subject,username=username,endpoint_key="telegram:*:"+username)
        session=service.sessions.get_or_create(user_subject=subject,system_id="oa",expected_principal_ref=principal)
        sessions[subject]=service.sessions.activate(session["session_id"],observed_principal_ref=principal)
        service.session_states.save(session["session_id"],{"cookies":[],"synthetic":principal})
    app=WorkspaceApplication(service=service)
    port=_free_port(); origin=f"http://127.0.0.1:{port}"
    server=create_workspace_http_server(config=validate_workspace_server_config(host="127.0.0.1",port=port,
        public_base_url=origin,tls_cert=None,tls_key=None),application=app)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True)
    ctx=_HttpContext(service=service,app=app,accounts=accounts,sessions=sessions,workers=workers,
                        port=port,origin=origin,cookies={},node_name="合成核对节点")
    with patch.object(service.adapter,"probe_business_draft_session",
            side_effect=lambda worker:{"authenticated":True,"observed_principal_ref":worker.principal}), \
         patch("agentbridge.adapters.seeyon_watch.read_workflow_state",
               side_effect=lambda *_a,**_k:_snapshot(ctx.node_name)) as read:
        ctx.read=read; thread.start()
        try:
            for subject,account in accounts.items():
                status,headers,_=_request(port,"POST","/api/login",origin=origin,
                    body={"username":account["username"],"password":PASSWORD})
                assert status==200
                ctx.cookies[subject]=_cookies(headers)
            yield ctx
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5);app.close()


def request(ctx, method, path, *, subject="user-a", body=None):
    cookies=ctx.cookies[subject]
    return _request(ctx.port,method,path,body=body,cookies=cookies,
                    origin=ctx.origin if method=="POST" else None,
                    csrf=cookies["agentbridge_workspace_csrf"] if method=="POST" else None)


def reference(ctx, *, subject="user-a", number=1):
    now=datetime.now(timezone.utc)
    session=ctx.service.sessions.find(user_subject=subject,system_id="oa")
    return ctx.service.business_watch.target_reference({"user_subject":subject,"session_id":session["session_id"],
        "principal_ref":principal_binding(session),"created_at":now.isoformat(),
        "expires_at":(now+timedelta(minutes=20)).isoformat(),"query":{"collection":"done"}},
        {"title":f"HTTP合成关注 {number}","_source":_source(number)})


def seed(ctx, *, subject="user-a", number=1):
    session=ctx.service.sessions.find(user_subject=subject,system_id="oa")
    store=ctx.service.business_watch.store
    watch,_=store.create(user_subject=subject,principal_ref=principal_binding(session),
        system_instance=ctx.service.business_watch.system_instance,source={**_source(number),"collection":"done"},
        title=f"HTTP合成关注 {number}",rules=["nodes","handlers","ended"],interval_seconds=3600,
        expires_at=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat(),endpoints=[],allow_mark_read=True,
        include_private=True)
    return store.apply_observation(watch["watch_id"],user_subject=subject,expected_version=watch["version"],
                                   snapshot=_snapshot(),include_private=True)["watch"]


def counts(ctx):
    with closing(sqlite3.connect(ctx.service.db_path)) as db:
        return tuple(db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] for table in
                     ("business_watches","business_watch_observations","business_watch_changes"))


def test_new_routes_reject_missing_login_and_csrf_before_downstream_io(http_context):
    ctx=http_context; watch=seed(ctx); wid=watch["watch_id"]
    create={"target_ref":reference(ctx),"allow_mark_read":True}
    posts=[("/api/business-watches",create),(f"/api/business-watches/{wid}/check",{"expected_version":watch["version"]}),
           (f"/api/business-watches/{wid}/update",{"expected_version":watch["version"],"action":"pause"})]
    before=counts(ctx)
    for path in ("/api/business-watches",f"/api/business-watches/{wid}"):
        assert _request(ctx.port,"GET",path)[0]==401
    for path,body in posts:
        assert _request(ctx.port,"POST",path,origin=ctx.origin,body=body)[0]==401
        for csrf in (None,"incorrect-synthetic-csrf"):
            assert _request(ctx.port,"POST",path,origin=ctx.origin,body=body,
                            cookies=ctx.cookies["user-a"],csrf=csrf)[0]==401
    ctx.read.assert_not_called();assert ctx.workers==[] and counts(ctx)==before


def test_new_mutation_routes_reject_missing_and_foreign_origin_before_dispatch(http_context):
    ctx=http_context; watch=seed(ctx); wid=watch["watch_id"]
    before=counts(ctx); cookies=ctx.cookies["user-a"]
    for path,body in [("/api/business-watches",{"target_ref":reference(ctx),"allow_mark_read":True}),
                      (f"/api/business-watches/{wid}/update",{"expected_version":watch["version"],"action":"stop"}),
                      (f"/api/business-watches/{wid}/check",{"expected_version":watch["version"]})]:
        for origin in (None,"https://untrusted.example.test"):
            status,_,error=_request(ctx.port,"POST",path,origin=origin,body=body,cookies=cookies,
                                    csrf=cookies["agentbridge_workspace_csrf"])
            assert status==403 and error["error"]["code"]=="INVALID_REQUEST_ORIGIN"
    ctx.read.assert_not_called();assert counts(ctx)==before


def test_real_http_create_detail_check_and_lifecycle_routes(http_context):
    ctx=http_context
    status,headers,created=request(ctx,"POST","/api/business-watches",body={"target_ref":reference(ctx),
        "allow_mark_read":True,"interval_seconds":3600,"expires_in_days":7,
        "event_types":["nodes","handlers","ended"],"endpoint_ids":[],"idempotency_key":"http-create-once"})
    assert status==201 and headers.get("Cache-Control")=="no-store"
    watch=created["watch"];wid=watch["watch_id"]
    assert watch["snapshot"]["current_nodes"][0]["name"]=="合成核对节点" and ctx.read.call_count==1
    assert not {"source","principal_ref","system_instance","user_subject","target_ref"}&watch.keys()
    status,_,read=request(ctx,"GET",f"/api/business-watches/{wid}")
    assert status==200 and read["watch"]==watch and read["events"]==[] and ctx.read.call_count==1
    ctx.node_name="合成复核节点"
    status,_,checked=request(ctx,"POST",f"/api/business-watches/{wid}/check",body={"expected_version":watch["version"]})
    assert status==200 and len(checked["events"])==1 and ctx.read.call_count==2
    watch=checked["watch"]
    assert watch["snapshot"]["current_nodes"][0]["name"]==ctx.node_name
    for action,state in (("pause","paused"),("resume","active"),("stop","stopped")):
        status,_,updated=request(ctx,"POST",f"/api/business-watches/{wid}/update",
                                 body={"expected_version":watch["version"],"action":action})
        assert status==200 and updated["watch"]["state"]==state
        assert updated["watch"]["version"]>watch["version"];watch=updated["watch"]
        if state in {"paused","stopped"}:
            status,_,error=request(ctx,"POST",f"/api/business-watches/{wid}/check",body={"expected_version":watch["version"]})
            assert status==409 and error["error"]["code"]=="WATCH_NOT_ACTIVE"
    assert ctx.read.call_count==2


def test_real_http_paging_is_owned_and_foreign_cursor_cannot_cross_accounts(http_context):
    ctx=http_context
    expected={seed(ctx,number=i)["watch_id"] for i in range(1,36)}
    bob=seed(ctx,subject="user-b",number=100)
    status,_,page=request(ctx,"GET","/api/business-watches")
    assert status==200 and len(page["items"])==32 and page["hasMore"]
    cursor=page["nextCursor"];assert isinstance(cursor,str) and cursor
    seen=[item["watch_id"] for item in page["items"]]
    status,_,last=request(ctx,"GET","/api/business-watches?"+urlencode({"cursor":cursor}))
    assert status==200 and len(last["items"])==3 and not last["hasMore"] and last["nextCursor"] is None
    seen.extend(item["watch_id"] for item in last["items"])
    assert len(seen)==len(set(seen))==35 and set(seen)==expected
    status,_,other=request(ctx,"GET","/api/business-watches",subject="user-b")
    assert status==200 and [item["watch_id"] for item in other["items"]]==[bob["watch_id"]]
    status,_,error=request(ctx,"GET","/api/business-watches?"+urlencode({"cursor":cursor}),subject="user-b")
    assert status==400 and error["error"]["code"]=="WATCH_REFERENCE_INVALID"
    for item in page["items"]+last["items"]+other["items"]:
        assert not {"source","principal_ref","system_instance","user_subject","target_key"}&item.keys()
    ctx.read.assert_not_called()


def test_foreign_watch_and_invalid_id_routes_do_not_read_or_mutate_the_owner_record(http_context):
    ctx=http_context;watch=seed(ctx);wid=watch["watch_id"];before=counts(ctx)
    attempts=[("GET",f"/api/business-watches/{wid}",None),
              ("POST",f"/api/business-watches/{wid}/check",{"expected_version":watch["version"]}),
              ("POST",f"/api/business-watches/{wid}/update",{"expected_version":watch["version"],"action":"stop"})]
    for method,path,body in attempts:
        status,_,error=request(ctx,method,path,subject="user-b",body=body)
        assert status==404 and set(error)=={"error"}
        assert "HTTP合成关注" not in json.dumps(error,ensure_ascii=False)
    for bad_id in ("not-an-id",str(uuid4()),"-"*36):
        for suffix in ("","/check","/update"):
            method="POST" if suffix else "GET"
            body={"expected_version":watch["version"],**({"action":"stop"} if suffix=="/update" else {})} if suffix else None
            assert request(ctx,method,f"/api/business-watches/{bad_id}{suffix}",body=body)[0]==404
    ctx.read.assert_not_called();assert counts(ctx)==before
    assert ctx.service.business_watch.store.get(wid,user_subject="user-a",include_private=True)==watch


def test_invalid_create_types_are_rejected_before_any_worker_or_oa_read(http_context):
    ctx=http_context;token=reference(ctx);before=counts(ctx)
    variants=[{"target_ref":None},{"target_ref":[]},{"target_ref":123},{"allow_mark_read":"true"},
              {"interval_seconds":True},{"interval_seconds":3600.0},{"interval_seconds":"3600"},
              {"expires_in_days":True},{"expires_in_days":7.0},{"expires_in_days":[]},
              {"endpoint_ids":{}},{"endpoint_ids":[[]]},{"event_types":[["nodes"]]},
              {"event_types":["nodes","nodes"]},{"event_types":[]},{"idempotency_key":[]},
              {"user_subject":"user-b"},{"source":{"url":"https://untrusted.example.test"}}]
    for changes in variants:
        status,_,error=request(ctx,"POST","/api/business-watches",
            body={"target_ref":token,"allow_mark_read":True,**changes})
        assert status==400 and "error" in error
    for raw in (b"[]",b"null",b"true",b'"not-an-object"'):
        assert request(ctx,"POST","/api/business-watches",body=raw)[0]==400
    ctx.read.assert_not_called();assert ctx.workers==[] and counts(ctx)==before


def test_invalid_check_versions_update_actions_and_identity_overrides_cannot_reach_oa(http_context):
    ctx=http_context;watch=seed(ctx);wid=watch["watch_id"];before=counts(ctx)
    for value in (None,True,False,0,-1,float(watch["version"]),str(watch["version"]),[],{}):
        assert request(ctx,"POST",f"/api/business-watches/{wid}/check",body={"expected_version":value})[0]==400
        assert request(ctx,"POST",f"/api/business-watches/{wid}/update",
                       body={"expected_version":value,"action":"pause"})[0]==400
    for action in (None,[],{},True,"unknown"):
        assert request(ctx,"POST",f"/api/business-watches/{wid}/update",
                       body={"expected_version":watch["version"],"action":action})[0]==400
    for extra in ({"user_subject":"user-b"},{"target_ref":"untrusted"},{"source":_source(2)}):
        assert request(ctx,"POST",f"/api/business-watches/{wid}/check",
                       body={"expected_version":watch["version"],**extra})[0]==400
        assert request(ctx,"POST",f"/api/business-watches/{wid}/update",
                       body={"expected_version":watch["version"],"action":"pause",**extra})[0]==400
    ctx.read.assert_not_called();assert counts(ctx)==before
    assert ctx.service.business_watch.store.get(wid,user_subject="user-a",include_private=True)==watch


def test_missing_mark_read_consent_and_foreign_endpoint_reject_before_navigation(http_context):
    ctx=http_context;token=reference(ctx)
    for options in ({},{"allow_mark_read":False}):
        status,_,error=request(ctx,"POST","/api/business-watches",body={"target_ref":token,**options})
        assert status==409 and error["error"]["code"]=="WATCH_READ_CONSENT_REQUIRED"
    status,_,error=request(ctx,"POST","/api/business-watches",body={"target_ref":token,"allow_mark_read":True,
                           "endpoint_ids":[ctx.accounts["user-b"]["endpoint_id"]]})
    assert status==403 and error["error"]["code"]=="WATCH_ENDPOINT_DENIED"
    ctx.read.assert_not_called();assert counts(ctx)==(0,0,0)


@pytest.mark.parametrize("action,state",[("pause","paused"),("stop","stopped")])
@pytest.mark.parametrize("loss",["principal","session","permission"])
def test_owner_can_pause_or_stop_after_authority_loss_but_http_receipt_is_redacted(http_context,action,state,loss):
    ctx=http_context;watch=seed(ctx);wid=watch["watch_id"];session=ctx.sessions["user-a"]
    if loss=="principal":
        ctx.service.sessions.rebind_expected_principal(session["session_id"],expected_principal_ref="Carol",reason="synthetic rebind")
        ctx.service.sessions.activate(session["session_id"],observed_principal_ref="Carol")
        status,_,page=request(ctx,"GET","/api/business-watches")
        assert status==200 and page["items"]==[]
    elif loss=="session":
        ctx.service.sessions.mark_expired(session["session_id"],message="synthetic expiry")
    else:
        grant_permissions(ctx.service.user_grants,"user-a",["oa.workflow.watch"])
    status,_,blocked=request(ctx,"GET",f"/api/business-watches/{wid}")
    if loss=="session":
        # An expired login cannot perform fresh IO, but the same principal may
        # still read its saved observation without renewing the downstream session.
        assert status==200 and blocked["watch"]["watch_id"]==wid
        assert request(ctx,"GET","/api/business-watches")[0]==401
        status,_,error=request(ctx,"POST",f"/api/business-watches/{wid}/check",
                               body={"expected_version":watch["version"]})
        assert status==401 and error["error"]["code"]=="WATCH_SESSION_UNAVAILABLE"
    else:
        assert status==403 and "HTTP合成关注" not in json.dumps(blocked,ensure_ascii=False)
    status,_,result=request(ctx,"POST",f"/api/business-watches/{wid}/update",
                            body={"expected_version":watch["version"],"action":action})
    assert status==200 and set(result)=={"watch"}
    assert set(result["watch"])=={"watch_id","state","version"} and result["watch"]["state"]==state
    assert result["watch"]["version"]>watch["version"]
    assert "合成" not in json.dumps(result,ensure_ascii=False)
    ctx.read.assert_not_called()


def test_http_watch_options_are_authenticated_owned_and_do_not_grant_missing_permission(http_context):
    ctx=http_context
    assert _request(ctx.port,"GET","/api/business-watches/options")[0]==401
    owned={}
    for subject in ("user-a","user-b"):
        status,headers,result=request(ctx,"GET","/api/business-watches/options",subject=subject)
        assert status==200 and headers.get("Cache-Control")=="no-store" and result["enabled"] is True
        assert result["defaults"]=={"interval_seconds":3600,"expires_in_days":7,"event_types":["nodes","handlers","ended"]}
        assert result["readNotice"]=="检查可能将事项标为已读。" and result["backgroundPaused"] is False
        owned[subject]={item["endpoint_id"] for item in result["endpoints"]}
        assert owned[subject] and all(set(item)=={"endpoint_id","client_type","label"} for item in result["endpoints"])
        assert all(item["client_type"] in {"telegram","openclaw-weixin","wechat","weixin"} for item in result["endpoints"])
    assert owned["user-a"].isdisjoint(owned["user-b"])
    grant_permissions(ctx.service.user_grants,"user-a",["oa.workflow.read"])
    status,_,disabled=request(ctx,"GET","/api/business-watches/options")
    assert status==200 and disabled["enabled"] is False and disabled["endpoints"]==[] and disabled["message"]
    assert ctx.service.user_grants.get("user-a")["permissions"]==["oa.workflow.read"]
    ctx.read.assert_not_called();assert ctx.workers==[] and counts(ctx)==(0,0,0)


def test_http_watch_option_labels_distinguish_owned_devices_without_exposing_private_routes(http_context):
    ctx=http_context
    with ctx.service.tasks._connect() as db:
        db.execute("UPDATE client_endpoints SET label=NULL WHERE user_subject='user-a' AND client_type='telegram'")
    status,_,single=request(ctx,"GET","/api/business-watches/options")
    assert status==200 and len(single["endpoints"])==1 and single["endpoints"][0]["label"]=="Telegram"
    unnamed=single["endpoints"][0]["endpoint_id"]
    created={}
    for key,label in (("office","办公室电脑"),("same-office","办公室电脑"),("unnamed",None),("phone","我的手机")):
        endpoint,_=ctx.service.tasks.ensure_endpoint(user_subject="user-a",token_id="synthetic-private-token-"+key,
            agent_host="synthetic-test",endpoint_key="synthetic:"+key,client_type="telegram",
            external_subject="synthetic-private-external-"+key,conversation_ref="synthetic-private-conversation-"+key,
            label=label,route={"private":"synthetic-private-route-"+key})
        created[key]=endpoint["endpoint_id"]
    status,_,options=request(ctx,"GET","/api/business-watches/options")
    assert status==200
    by_id={item["endpoint_id"]:item["label"] for item in options["endpoints"]}
    assert len(set(by_id.values()))==len(by_id)==5
    assert by_id[created["phone"]]=="Telegram · 我的手机"
    for key in ("office","same-office"):
        assert by_id[created[key]]=="Telegram · 办公室电脑 · "+created[key][:8]
    for endpoint_id in (unnamed,created["unnamed"]):
        assert by_id[endpoint_id]=="Telegram · "+endpoint_id[:8]
    assert request(ctx,"GET","/api/business-watches/options")[2]==options
    assert "synthetic-private-" not in json.dumps(options)
    assert all(set(item)=={"endpoint_id","client_type","label"} for item in options["endpoints"])
    bob=request(ctx,"GET","/api/business-watches/options",subject="user-b")[2]
    assert set(by_id).isdisjoint(item["endpoint_id"] for item in bob["endpoints"])
    ctx.read.assert_not_called();assert ctx.workers==[] and counts(ctx)==(0,0,0)


def test_http_configure_preserves_expiry_when_omitted_and_cancelled_read_consent_pauses_without_oa_io(http_context):
    ctx=http_context
    status,_,created=request(ctx,"POST","/api/business-watches",body={"target_ref":reference(ctx),"allow_mark_read":True})
    assert status==201;watch=created["watch"];wid=watch["watch_id"]
    status,_,options=request(ctx,"GET","/api/business-watches/options")
    endpoint=options["endpoints"][0]["endpoint_id"]
    status,_,changed=request(ctx,"POST",f"/api/business-watches/{wid}/update",body={"expected_version":watch["version"],
        "action":"configure","interval_seconds":900,"event_types":["nodes"],"endpoint_ids":[endpoint],"allow_mark_read":True})
    assert status==200 and changed["watch"]["expires_at"]==watch["expires_at"]
    watch=changed["watch"]
    assert watch["interval_seconds"]==900 and watch["event_types"]==["nodes"] and watch["endpoint_ids"]==[endpoint]
    status,_,paused=request(ctx,"POST",f"/api/business-watches/{wid}/update",body={"expected_version":watch["version"],
        "action":"configure","allow_mark_read":False})
    assert status==200 and paused["watch"]["state"]=="paused" and paused["watch"]["allow_mark_read"] is False
    assert paused["watch"]["expires_at"]==watch["expires_at"] and ctx.read.call_count==1


def test_http_maintenance_pause_projects_read_only_state_and_still_permits_owner_pause_and_stop(http_context):
    ctx=http_context;watch=seed(ctx);wid=watch["watch_id"]
    ctx.service.business_watch.controls.set(scope="user",target="user-a",paused=True,expected_version=0,
        reason="synthetic maintenance",actor="isolated test")
    status,_,result=request(ctx,"GET",f"/api/business-watches/{wid}")
    assert status==200 and result["watch"]["background_paused"] is True and result["watch"]["next_check_at"] is None
    assert result["watch"]["snapshot"]["current_nodes"][0]["name"]=="合成核对节点"
    watch=result["watch"]
    assert request(ctx,"GET","/api/business-watches/options")[2]["backgroundPaused"] is True
    assert request(ctx,"GET","/api/business-watches/options",subject="user-b")[2]["backgroundPaused"] is False
    for suffix,body in (("check",{"expected_version":watch["version"]}),
                        ("update",{"expected_version":watch["version"],"action":"configure","interval_seconds":900})):
        status,_,error=request(ctx,"POST",f"/api/business-watches/{wid}/{suffix}",body=body)
        assert status==409 and error["error"]["code"]=="WATCH_ADMIN_PAUSED"
    status,_,paused=request(ctx,"POST",f"/api/business-watches/{wid}/update",body={"expected_version":watch["version"],"action":"pause"})
    assert status==200 and paused["watch"]["state"]=="paused"
    status,_,error=request(ctx,"POST",f"/api/business-watches/{wid}/update",body={"expected_version":paused["watch"]["version"],"action":"resume"})
    assert status==409 and error["error"]["code"]=="WATCH_ADMIN_PAUSED"
    status,_,stopped=request(ctx,"POST",f"/api/business-watches/{wid}/update",body={"expected_version":paused["watch"]["version"],"action":"stop"})
    assert status==200 and stopped["watch"]["state"]=="stopped"
    ctx.read.assert_not_called();assert ctx.workers==[]
