"""Original media/caption facts survive independently, without a second model handoff."""
import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
import sys
import re
import sqlite3
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.support.nanobot_image_probe import sample_png
from tests.support.nanobot_telegram_loopback import fixture, update
from tests.test_nanobot_telegram_media import incoming
from nanobot_agentbridge.media import MAX_FILE
from nanobot_agentbridge.telegram_media_receipts import (
    BINDING_FIELDS, TTL, prepare_source, publish_media, recover_route, validate_receipt,
)


def drain_media(pilot):
    """Await the real gateway worker; no platform or model retry is needed."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with pilot.db:
            pilot.db.execute("UPDATE telegram_media_projection SET next_attempt=0")
        pilot.media_projection.drain()
        if not pilot.db.execute("SELECT 1 FROM telegram_media_projection WHERE state='pending'").fetchone():
            return
        time.sleep(.01)
    raise AssertionError("Media did not settle")


@pytest.mark.parametrize("mime,name,data", [
    ("image/png", "原图😀.png", sample_png((1, 2, 3))),
    ("text/plain", "说明.txt", "合成文件😀".encode()),
    ("application/pdf", "说明.pdf", b"%PDF-1.4\nsynthetic only"),
])
def test_caption_file_and_answer_have_independent_original_receipts(tmp_path, mime, name, data):
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        state["files"]["original"] = {"file_path": "documents/original.bin", "bytes": data}
        state["updates"] = [incoming(1, caption="仅分析原件😀", document={"file_id":"original", "mime_type":mime, "file_name":name})]
        pilot.poll_once()
        drain_media(pilot)
        assert list(state["timeline"].values()) == ["仅分析原件😀", "合成结果 alpha"]
        receipt, = state["media_receipts"].values()
        assert receipt["sha256"] == hashlib.sha256(data).hexdigest() and receipt["fileName"] == name
        assert receipt["receivedAt"] == datetime.fromtimestamp(state["updates"][0]["message"]["date"], timezone.utc).isoformat()
        row = pilot.db.execute("SELECT * FROM telegram_media_projection").fetchone()
        assert row["state"] == "acknowledged" and row["content"] is None
        pilot.ingest(state["updates"][0])
        assert len(state["downloads"]) == len(state["admissions"]) == len(state["media_receipts"]) == 1
        pilot.db.close()


@pytest.mark.parametrize("kind", ["unsupported", "too_large", "unreadable", "expired"])
def test_caption_survives_refused_attachment_without_fake_assistant_history(tmp_path, kind):
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        document={"file_id":"bad", "file_name":"bad.png", "mime_type":"image/png"}
        extra={}
        if kind == "unsupported":
            document["mime_type"]="application/zip"
        elif kind == "too_large":
            document["file_size"]=MAX_FILE+1
        elif kind == "expired":
            extra["date"]=int(time.time())-TTL-1
        else:
            state["files"]["bad"]={"file_path":"documents/bad.png", "bytes":b"not a PNG"}
        state["updates"]=[incoming(1,caption="这段说明仍应保留",document=document,**extra)]
        pilot.poll_once()
        drain_media(pilot)
        assert list(state["timeline"].values()) == ["这段说明仍应保留"]
        receipt,=state["media_receipts"].values()
        assert receipt["state"]=="unavailable" and receipt["failure"]==kind and receipt["attachmentId"] is None
        assert receipt["fileName"]=="附件" and receipt["sha256"] is None and receipt["byteSize"]==0
        assert not state["admissions"] and len(state["deliveries"])==1
        assert len(state["downloads"]) == (1 if kind=="unreadable" else 0)
        pilot.db.close()


def test_lost_append_receipt_recovers_original_after_restart_without_download_or_model(tmp_path):
    with fixture(tmp_path) as (state, _, create):
        pilot=create()
        state["files"]["photo"]={"file_path":"photos/original.png", "bytes":sample_png((3,4,5))}
        state.update(updates=[incoming(1,caption="原说明",document={"file_id":"photo","mime_type":"image/png"})],lost_media_receipt=True)
        pilot.poll_once()
        pilot.media_projection.close()  # Remote append finished, response was lost.
        assert len(state["media_receipts"])==1
        first=dict(next(iter(state["media_receipts"].values())))
        pilot.db.close()
        pilot=create()
        state["lost_media_receipt"]=False
        drain_media(pilot)
        assert next(iter(state["media_receipts"].values()))==first
        requests=[v for p,v in state["calls"] if p=="/v1/telegram-media-append"]
        assert len(requests)==2 and "contentBase64" in requests[0] and "contentBase64" not in requests[1]
        assert len(state["admissions"])==len(state["downloads"])==1
        pilot.poll_once()
        assert len(state["admissions"])==len(state["downloads"])==1
        pilot.db.close()


def test_unknown_model_admission_does_not_discard_original_caption_or_file(tmp_path, monkeypatch):
    with fixture(tmp_path) as (state, _, create):
        pilot=create()
        state["files"]["text"]={"file_path":"documents/original.txt", "bytes":b"original"}
        state["updates"]=[incoming(1,caption="已接收的文字",document={"file_id":"text","mime_type":"text/plain"})]
        admitted=[]
        def unavailable(*args):
            admitted.append(args)
            raise RuntimeError("Unknown model admission")
        monkeypatch.setattr(pilot.gateway,"send_dispatch",unavailable)
        monkeypatch.setattr(pilot.gateway,"dispatch_evidence",lambda *_:{"prompt_observed":False})
        pilot.poll_once(); drain_media(pilot); pilot.poll_once()
        assert len(admitted)==len(state["downloads"])==len(state["media_receipts"])==1
        assert list(state["timeline"].values())==["已接收的文字"] and not state["deliveries"]
        assert pilot._record(501,1)["state"]=="admission_unknown"
        pilot.db.close()


def test_crash_during_file_read_preserves_received_caption_without_replaying_model(tmp_path, monkeypatch):
    with fixture(tmp_path) as (state, _, create):
        pilot=create()
        value=incoming(1,caption="下载中断仍保留",document={"file_id":"text","mime_type":"text/plain"})
        monkeypatch.setattr("nanobot_agentbridge.telegram_media.download", lambda *_: (_ for _ in ()).throw(SystemExit()))
        with pytest.raises(SystemExit):
            pilot.ingest(value)
        pilot.db.close()
        pilot=create()
        state["updates"]=[value]
        pilot.poll_once(); drain_media(pilot)
        assert pilot._record(501,1)["state"]=="media_interrupted"
        assert list(state["timeline"].values())==["下载中断仍保留"]
        assert not state["admissions"] and not state["downloads"] and not state["deliveries"]
        assert next(iter(state["media_receipts"].values()))["failure"]=="unreadable"
        pilot.db.close()


@pytest.fixture
def media_host(tmp_path, monkeypatch):
    token=tmp_path/"original.token"; token.write_text("synthetic-original-personal-credential"); token.chmod(0o600)
    owner={"user_subject":"first","endpoint_id":str(uuid4()),"endpoint_key":"workspace:original",
           "session_key":"web-original","credential_ref":"web-generation","tokenFile":str(token)}
    record={**owner,"endpoint_id":str(uuid4()),"endpoint_key":"telegram:8001:501","session_key":"private-original",
            "credential_ref":"tg-generation","workspaceEndpointId":owner["endpoint_id"],"workspaceEndpointKey":owner["endpoint_key"]}
    state={"paired":True,"pair":"a"*64,"calls":[],"receipts":{},"host_type":"nanobot"}
    def payload(r,_):
        return {"userDir":str(tmp_path),"endpoint":r["endpoint_id"],"hostType":state["host_type"],"tokenFile":r["tokenFile"]}
    host=SimpleNamespace(instance="synthetic-instance",bindings={tuple(owner[k] for k in BINDING_FIELDS):owner},worker_payload=payload)
    async def call(name,args,**_):
        state["calls"].append((name,args))
        if name.endswith("_source"):
            value={"recorded":True,"sourceEndpointId":record["endpoint_id"],"pairingRef":state["pair"],"receivedAt":args["received_at"]}
        else:
            key=(args["source_message_id"],args["ordinal"])
            if key not in state["receipts"]:
                if not state["paired"]:
                    return SimpleNamespace(isError=True,structuredContent={})
                if not args.get("content_base64") and args.get("failure") is None:
                    return SimpleNamespace(isError=False,structuredContent={"recorded":False})
                state["receipts"][key]={"sourceEndpointId":record["endpoint_id"],"sourceMessageId":args["source_message_id"],"ordinal":args["ordinal"],
                    "fileName":args["file_name"],"mimeType":args["mime_type"],"byteSize":args["byte_size"],"sha256":args["sha256"],
                    "receivedAt":args["received_at"],"expiresAt":(datetime.fromisoformat(args["received_at"])+timedelta(seconds=TTL)).isoformat(),
                    "entryId":str(uuid4()),"attachmentId":secrets.token_urlsafe(32),"state":"stored","failure":None}
            value={"recorded":True,"mediaReceipt":state["receipts"][key]}
        return SimpleNamespace(isError=False,structuredContent=value)
    session=SimpleNamespace(call_tool=call)
    @asynccontextmanager
    async def opened(*_,**__):
        yield session,{},"L3"
    @asynccontextmanager
    async def borrowed(*_,**__):
        yield SimpleNamespace(session=session,meta={},level="L3")
    async def verified(*_,**__):
        return state["paired"]
    monkeypatch.setattr("nanobot_agentbridge.telegram_media_receipts.open_host_session",opened)
    monkeypatch.setattr("nanobot_agentbridge.telegram_media_receipts.verify_telegram_binding",verified)
    host.control_sessions=SimpleNamespace(borrow=borrowed)
    body={"binding":{k:record[k] for k in BINDING_FIELDS},"messageId":7,"ordinal":0,"fileName":"原件.txt","mimeType":"text/plain",
          "byteSize":3,"sha256":hashlib.sha256(b"abc").hexdigest(),"receivedAt":datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
          "pairingRef":state["pair"],"contentBase64":"YWJj","failure":None}
    return host,owner,record,body,state,token


@pytest.mark.parametrize("host_type",["nanobot","pi"])
def test_exact_original_media_receipt_only_after_revocation(media_host,host_type):
    from aiohttp import web
    host,owner,record,body,state,token=media_host
    state["host_type"]=host_type
    async def run():
        assert await recover_route(host,body,host_type=host_type) is None
        source=await prepare_source(host,record,body)
        assert source["pairingRef"]==body["pairingRef"]
        first=await publish_media(host,record,body)
        state["paired"]=False
        probe={k:v for k,v in body.items() if k!="contentBase64"}
        assert await recover_route(host,probe,host_type=host_type)==first
        assert state["calls"][-1][1]["content_base64"] is None
        with pytest.raises(web.HTTPForbidden):
            await publish_media(host,record,body)
        for changed in ({"fileName":"changed.txt"},{"sha256":"b"*64},{"pairingRef":"b"*64},
                        {"receivedAt":(datetime.fromisoformat(body["receivedAt"])-timedelta(seconds=1)).isoformat()},
                        {"binding":{**body["binding"],"credential_ref":"new-pair"}}):
            with pytest.raises(web.HTTPForbidden):
                await recover_route(host,{**probe,**changed},host_type=host_type)
        assert await recover_route(host,{**probe,"ordinal":1},host_type=host_type) is None
        token.write_text("synthetic-rotated-personal-credential")
        with pytest.raises(web.HTTPForbidden):
            await recover_route(host,probe,host_type=host_type)
        assert len(state["receipts"])==1
    asyncio.run(run())


def test_source_generation_cannot_change_and_receipt_fields_are_exact(media_host):
    from aiohttp import web
    host,_,record,body,state,_=media_host
    async def run():
        await prepare_source(host,record,body)
        result=await publish_media(host,record,body)
        state["pair"]="b"*64
        with pytest.raises(web.HTTPForbidden):
            await prepare_source(host,record,body)
        metadata={k:v for k,v in body.items() if k not in {"binding","contentBase64"}}
        assert validate_receipt(result,record,metadata)
        for changed in ({"sourceMessageId":8},{"sha256":"b"*64},{"byteSize":True},{"receivedAt":"2000-01-01T00:00:00+00:00"},
                        {"expiresAt":"2099-01-01T00:00:00+00:00"},{"attachmentId":str(uuid4())},{"failure":"expired"}):
            assert not validate_receipt({"recorded":True,"mediaReceipt":{**result["mediaReceipt"],**changed}},record,metadata)
    asyncio.run(run())


def test_actual_nano_http_media_admission_and_public_caption_use_distinct_proofs(tmp_path,monkeypatch):
    """Regression: the actual accept digest includes files, while history matches its caption."""
    from aiohttp.test_utils import TestClient,TestServer
    # This HTTP/digest test never invokes the SDK, card presenter, or worker.
    # Replace only those unused imports; actual Host/RunCoordinator/timeline code
    # runs below. Native SDK qualification remains a separate registered gate.
    loaded_server="nanobot_agentbridge.server" in sys.modules
    monkeypatch.setitem(sys.modules,"nanobot_agentbridge.cards",SimpleNamespace(
        CredentialCards=None,coordinated_waiting=None,present_coordinated=None,present_credential=None))
    monkeypatch.setitem(sys.modules,"nanobot_agentbridge.worker",SimpleNamespace(
        channel_for=lambda key:"telegram" if re.fullmatch(r"telegram:[0-9]+:[0-9]+",key) else "workspace"))
    from nanobot_agentbridge.server import Host,create_app
    def private(name,text):
        path=tmp_path/name;path.write_text(text);path.chmod(0o600);return str(path)
    service="synthetic-private-http-service-credential"
    record={"user_subject":"first","endpoint_id":str(uuid4()),"endpoint_key":"telegram:8001:501",
            "session_key":"original-private-chat","credential_ref":"original-private-generation",
            "tokenFile":private("personal.token","synthetic-personal-credential"),"modelConfigFile":private("model.json","{}")}
    spec={"instanceId":"actual-nano-media-test","mcpUrl":"http://127.0.0.1:1/mcp","serviceTokenFile":private("service.token",service),
          "stateDir":str(tmp_path/"state"),"bindings":[record]}
    executions=[];projections=[];failures=[]
    original_action=Host.action
    async def checked_action(self,*args,**kwargs):
        try:
            return await original_action(self,*args,**kwargs)
        except Exception as exc:
            failures.append(repr(exc))
            raise
    monkeypatch.setattr(Host,"action",checked_action)
    async def execute(self,record,run,message,emit,**options):
        executions.append((run,message,options["images"]))
        return "合成原答复"
    monkeypatch.setattr(Host,"execute",execute)
    async def verified(*_,**__):
        return True
    async def call(name,args,**_):
        assert name=="agentbridge_host_timeline_append" and args["projection_v2"] is True
        projections.append(args)
        entry_id=str(uuid4())
        return SimpleNamespace(isError=False,structuredContent={"status":"succeeded","endpoint":{"endpointId":record["endpoint_id"]},
            "entry":{"entryId":entry_id,"role":args["role"],"text":args["text"]},
            "projectionReceipt":{"entryId":entry_id,"role":args["role"],"messageKey":args["message_key"],
                "sourceEndpointId":record["endpoint_id"],"textSha256":hashlib.sha256(args["text"].encode()).hexdigest()}})
    @asynccontextmanager
    async def session(*_,**__):
        yield SimpleNamespace(call_tool=call),{},"L3"
    monkeypatch.setattr("nanobot_agentbridge.telegram_identity.verify_telegram_binding",verified)
    monkeypatch.setattr("nanobot_agentbridge.session_pool.open_host_session",session)
    def attachment(color):
        return {"type":"image","mimeType":"image/png","fileName":"原图.png","content":base64.b64encode(sample_png(color)).decode()}
    images=[attachment((1,2,3)),attachment((4,5,6))]
    prompt="原图片说明😀"
    binding={k:record[k] for k in BINDING_FIELDS}
    request={"binding":binding,"requestKey":"tg:8001:501:7","message":prompt,"attachments":images}
    async def run():
        client=TestClient(TestServer(create_app(spec)));await client.start_server()
        async def post(action,body):
            return await client.post("/v1/"+action,json={"instance":spec["instanceId"],**body},headers={"Authorization":"Bearer "+service})
        try:
            accepted=await post("accept",request);assert accepted.status==200,failures
            run_id=(await accepted.json())["runId"]
            await asyncio.sleep(0)
            again=await post("accept",request);assert again.status==200 and (await again.json())["runId"]==run_id
            for changed in ({"message":prompt+"changed"},{"attachments":list(reversed(images))},
                            {"attachments":[attachment((9,9,9)),images[1]]}):
                assert (await post("accept",{**request,**changed})).status==409
            history={"binding":binding,"messageId":7,"role":"user","text":prompt,"runId":run_id,
                     "deliveredMessageIds":[],"projectionMode":"telegram_received_v2","sourceState":None}
            response=await post("telegram-timeline",history)
            assert response.status==200 and (await response.json())["recorded"] is True
            assert (await post("telegram-timeline",{**history,"text":prompt+"changed"})).status==403
            assert (await post("telegram-timeline",{**history,"role":"assistant","text":"合成原答复",
                "projectionMode":"host_result_v2","sourceState":"completed"})).status==200
            assert len(executions)==1 and executions[0][1]==prompt and len(executions[0][2])==2
            assert [p["text"] for p in projections]==[prompt,"合成原答复"]
        finally:
            await client.close()
    try:
        asyncio.run(run())
    finally:
        if not loaded_server:
            sys.modules.pop("nanobot_agentbridge.server",None)
            import nanobot_agentbridge
            if hasattr(nanobot_agentbridge,"server"):
                delattr(nanobot_agentbridge,"server")


def test_legacy_media_run_never_backfills_a_caption_proof(tmp_path):
    from nanobot_agentbridge.runs import RunCoordinator
    async def run():
        runs=RunCoordinator(tmp_path/"original")
        async def execute(*_):
            return "old result"
        args={"subject":"first","endpoint":"original","conversation":"original-chat","request_key":"old-media",
              "payload_digest":hashlib.sha256(b"legacy complete attachment payload").hexdigest(),"execute":execute}
        try:
            value=runs.accept(**args)
            again=runs.accept(**args,public_prompt="old caption")
            assert again["run_id"]==value["run_id"] and not runs.matches_message(value["run_id"],"old caption")
            assert not runs._db.execute("SELECT 1 FROM run_public_prompts").fetchone()
        finally:
            await runs.close()
    asyncio.run(run())
