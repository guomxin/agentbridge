"""Shared original-file delivery uses the same persistent, single-begin protocol."""
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.support.nanobot_image_probe import sample_png
from tests.test_workspace_query_delivery import setup, close, drain, deadline, ENDPOINT, DELIVERY, PAIR, ORIGIN
from agentbridge.workspace.gateway import GatewayRequestError
from nanobot_agentbridge.notifications import TerminalMessages, notifications
from nanobot_agentbridge.telegram import TelegramRejected, TelegramUnavailable


def original():
    data=sample_png((34,56,78))
    return {"fileName":"原截图😀.png","mimeType":"image/png","byteSize":len(data),
            "sha256":hashlib.sha256(data).hexdigest(),"contentBase64":base64.b64encode(data).decode(),"expiresAt":deadline()}


@pytest.fixture
def media_setup(setup):
    pilot,terminal,gateway,binding=setup
    gateway.event={"kind":"workspace_media_v2","deliveryId":DELIVERY,"attempt":1,"pairingRef":PAIR,"itemIndex":1,"itemCount":2}
    gateway.result={"available":True,"leaseExpiresAt":deadline(),"resource":original()}
    request=gateway._request
    def media_request(route,body,**opts):
        assert route in {"telegram-notifications","telegram-workspace-media-v2"}
        return request("telegram-workspace-result-view" if route.endswith("-v2") else route,body,**opts)
    gateway._request=media_request
    def send(chat,filename,content_type,data):
        assert chat==501
        pilot.sent.append((filename,content_type,data))
        return {"chat":{"id":501,"type":"private"},"from":{"id":8001},"message_id":7,
                "document":{"file_id":"original-platform-file","file_size":len(data)}}
    pilot.api.send_document=send
    yield setup


def poll(value):
    _,terminal,_,binding=value
    terminal.poll(501,binding,"digest");drain(terminal)


def test_media_bytes_and_name_are_unmodified_and_no_text_or_url_fallback(media_setup):
    pilot,_,gateway,_=media_setup
    poll(media_setup)
    item=gateway.result["resource"]
    assert pilot.sent==[(item["fileName"],item["mimeType"],base64.b64decode(item["contentBase64"]))]
    assert [a["action"] for a in gateway.actions]==["issue","begin","ack"]
    assert all(a["deliveryMode"]=="workspace_media_v2" and a["itemIndex"]==1 and a["itemCount"]==2
               and "sourceTaskId" not in a for a in gateway.actions)
    assert gateway.actions[-1]["outcome"]=="sent"
    gateway.event["attempt"]=2
    poll(media_setup)
    assert len(pilot.sent)==1


@pytest.mark.parametrize("change",[{"sha256":"b"*64},{"mimeType":"application/zip"},{"fileName":"../private.png"},
    {"fileName":"\ud800.png"},{"byteSize":True},{"byteSize":6*1024*1024+1},{"expiresAt":"2000-01-01T00:00:00+00:00"},
    {"contentBase64":"https://example.invalid/original.png"}])
def test_unverified_resource_never_reserves_send(media_setup,change):
    pilot,_,gateway,_=media_setup
    gateway.result["resource"].update(change)
    poll(media_setup)
    assert not pilot.sent and not pilot.db.execute("SELECT 1 FROM telegram_card_deliveries").fetchone()
    assert gateway.actions[-1]["outcome"]=="unavailable"


@pytest.mark.parametrize("field",["publicText","resultView","interaction"])
def test_media_issue_cannot_smuggle_text_or_private_interaction(media_setup,field):
    pilot,_,gateway,_=media_setup
    gateway.result[field]="unexpected"
    poll(media_setup)
    assert not pilot.sent and gateway.actions[-1]["outcome"]=="unavailable"


@pytest.mark.parametrize("fault",["begin_lost","send_lost","revoked","429","400"])
def test_media_uncertainty_and_explicit_rejection_have_distinct_retry_rules(media_setup,fault):
    pilot,_,gateway,_=media_setup
    send=pilot.api.send_document;calls=[]
    def fail(*args,**kwargs):
        calls.append(1)
        if fault=="send_lost":
            send(*args,**kwargs)
            raise TelegramUnavailable()
        raise TelegramRejected(429,3) if fault=="429" else TelegramRejected(400)
    if fault=="begin_lost":
        gateway.begin=GatewayRequestError("GATEWAY_TIMEOUT","Original permit receipt unavailable")
    elif fault=="revoked":
        pilot.authorized=False
    else:
        pilot.api.send_document=fail
    poll(media_setup)
    if fault in {"begin_lost","send_lost"}:
        assert gateway.actions[-1]["outcome"]=="delivery_unknown"
    elif fault in {"429","400"}:
        assert gateway.actions[-1]["outcome"]==("retry_after" if fault=="429" else "rejected")
    pilot.api.send_document=send
    gateway.event["attempt"]=2
    with pilot.db:
        pilot.db.execute("UPDATE telegram_workspace_query_attempts SET not_before=0")
    poll(media_setup)
    assert len(pilot.sent)==(1 if fault in {"send_lost","429"} else 0)
    assert [a["action"] for a in gateway.actions].count("begin")== (0 if fault=="revoked" else 2 if fault=="429" else 1)


def test_media_lost_ack_recovers_without_claim_or_current_binding(media_setup):
    pilot,_,gateway,_=media_setup
    request=gateway._request
    def lose(route,body,**opts):
        if body.get("action")=="ack":
            raise GatewayRequestError("GATEWAY_TIMEOUT","Original acknowledgement unavailable")
        return request(route,body,**opts)
    gateway._request=lose
    poll(media_setup)
    assert len(pilot.sent)==1
    gateway._request=request;pilot.authorized=False
    with pilot.db:
        pilot.db.execute("UPDATE telegram_workspace_query_attempts SET next_ack=0")
    restarted=TerminalMessages(pilot)
    try:
        restarted.flush_deferred();drain(restarted)
        assert len(pilot.sent)==1 and gateway.actions[-1]["outcome"]=="sent"
        assert [a["action"] for a in gateway.actions]==["issue","begin","ack"]
        assert pilot.db.execute("SELECT acknowledged FROM telegram_workspace_query_attempts").fetchone()[0]==1
    finally:
        close(restarted)


@pytest.mark.parametrize("host_type",["nanobot","pi"])
def test_private_media_route_pins_original_claim_and_validates_original_bytes(tmp_path,host_type):
    from aiohttp import web
    req={"hostType":host_type,"subject":"first","endpoint":ENDPOINT,"endpointKey":"telegram:8001:501",
         "instance":"test-host","workspaceEndpointId":"web-first","userDir":str(tmp_path),"pairingRef":PAIR}
    host=SimpleNamespace(coordinator=True,card_origins={ORIGIN},worker_payload=lambda *_:req)
    state={"resource":original(),"calls":[]}
    async def call(name,args,**_):
        state["calls"].append((name,args))
        if name=="agentbridge_host_notification_claim":
            assert args["include_workspace_media_v2"] is True
            result={"endpoint":{"endpointId":ENDPOINT},"notifications":[{"deliveryId":DELIVERY,"attemptCount":1,
                "deliveryMode":"workspace_media_v2","pairingRef":PAIR,"itemIndex":2,"itemCount":2}]}
        else:
            assert name=="agentbridge_host_workspace_media_delivery" and args["agent_host"]==host_type
            result={"available":True,"leaseExpiresAt":deadline(),"resource":state["resource"]} if args["action"]=="issue" else {"allowed":True}
        return SimpleNamespace(isError=False,structuredContent=result)
    @asynccontextmanager
    async def paired(_):
        yield SimpleNamespace(call_tool=call),{},"L3"
    def route(action,body):
        return asyncio.run(notifications(host,{"workspaceEndpointId":"web-first"},action,body,paired_session=paired))
    event,=route("telegram-notifications",{})["deliveries"]
    action="telegram-workspace-media-v2"
    assert route(action,{**event,"action":"issue"})["resource"]==state["resource"]
    assert route(action,{**event,"action":"begin"})=={"allowed":True}
    for changed in ({"itemIndex":1},{"pairingRef":"b"*64},{"attempt":2}):
        with pytest.raises(web.HTTPForbidden):
            route(action,{**event,"action":"issue",**changed})
    state["resource"]["sha256"]="b"*64
    with pytest.raises(web.HTTPBadGateway):
        route(action,{**event,"action":"issue"})
