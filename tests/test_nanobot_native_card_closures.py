"""Prospective original Telegram cards: exact local source and positive HTTP receipts."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiohttp import web

from tests.support.nanobot_telegram_loopback import fixture
from tests.test_nanobot_telegram_presentation import _interaction
from nanobot_agentbridge.native_card_state import remember, read
from nanobot_agentbridge.telegram import TelegramRejected
from agentbridge.core.telegram_interaction_closures import text


def source(task=True):
    return {"sourceRef": str(uuid4()), "interactionId": str(uuid4()), "type": "credential",
            "taskId": str(uuid4()) if task else None, "runId": "original-run"}


def arm(pilot, original, chat=501):
    binding, digest = pilot._binding(chat)
    pilot._deliver_card_once(chat, 9, binding, digest, "interaction:" + original["interactionId"],
                             {"text": "原生登录卡"}, native_source=original)
    return binding, digest


def close(pilot, monkeypatch, original, state="completed"):
    call = pilot.gateway._request
    seen = []
    def request(route, body, **kwargs):
        if route != "telegram-task":
            return call(route, body, **kwargs)
        assert body["operation"] == "card_state" and body["source"] == original
        seen.append(body)
        return {"source": original, "state": state}
    monkeypatch.setattr(pilot.gateway, "_request", request)
    pilot.native_card_closures.next_poll = 0
    pilot.reconcile()
    return seen


@pytest.mark.parametrize("kind", ["credential", "business_input", "execution_authorization"])
@pytest.mark.parametrize("terminal", ["completed", "failed", "expired", "superseded", "declined"])
def test_native_positive_original_receipt_closes_once_and_restarts(tmp_path, monkeypatch, kind, terminal):
    original = {**source(), "type": kind}
    with fixture(tmp_path) as (wire, _, create):
        pilot = create()
        arm(pilot, original)
        calls = close(pilot, monkeypatch, original, terminal)
        assert len(wire["deliveries"]) == 2 and len(calls) == 1
        assert wire["deliveries"][1]["text"] == text(kind, terminal)
        assert wire["deliveries"][1]["reply_parameters"]["message_id"] == 701
        assert "reply_markup" not in wire["deliveries"][1]
        assert not wire["admissions"]
        pilot.db.close()
        pilot = create()
        assert not close(pilot, monkeypatch, original, terminal)
        assert len(wire["deliveries"]) == 2
        pilot.db.close()


@pytest.mark.parametrize("reason", ["legacy", "unknown", "changed", "revoked", "boolean"])
def test_native_without_original_proof_or_binding_cannot_close(tmp_path, monkeypatch, reason):
    original = source()
    with fixture(tmp_path) as (wire, bindings, create):
        pilot = create()
        if reason == "unknown":
            wire["lost_delivery"] = True
        if reason == "boolean":
            call = pilot.api.call
            def invalid(method, **body):
                result = call(method, **body)
                if method == "sendMessage": result["message_id"] = True
                return result
            monkeypatch.setattr(pilot.api, "call", invalid)
        arm(pilot, original if reason != "legacy" else None) if reason != "legacy" else pilot._deliver_card_once(
            501,9,*pilot._binding(501),"interaction:"+original["interactionId"],{"text":"旧卡"})
        if reason == "changed": bindings["501"]["credential_ref"] += "-new"
        if reason == "revoked": wire["verified"].remove("501")
        wire["lost_delivery"] = False
        assert not close(pilot, monkeypatch, original)
        assert len(wire["deliveries"]) == 1
        pilot.db.close()
        pilot = create()
        assert not close(pilot, monkeypatch, original)
        assert len(wire["deliveries"]) == 1
        pilot.db.close()


def test_native_unknown_close_never_replays_even_after_restart(tmp_path, monkeypatch):
    original = source()
    with fixture(tmp_path) as (wire, _, create):
        pilot = create()
        arm(pilot, original)
        wire["lost_delivery"] = True
        close(pilot, monkeypatch, original)
        assert pilot.db.execute("SELECT state FROM telegram_native_card_closures").fetchone()[0] == "delivery_unknown"
        pilot.db.close()
        wire["lost_delivery"] = False
        pilot = create()
        assert not close(pilot, monkeypatch, original)
        assert len(wire["deliveries"]) == 2
        pilot.db.close()


def test_native_crash_crossing_send_preserves_unknown(tmp_path, monkeypatch):
    original = source()
    with fixture(tmp_path) as (wire, _, create):
        pilot = create()
        arm(pilot, original)
        call = pilot.api.call
        def crash(method, **body):
            call(method, **body)
            raise SystemExit("after physical send")
        monkeypatch.setattr(pilot.api, "call", crash)
        with pytest.raises(SystemExit): close(pilot, monkeypatch, original)
        pilot.db.close()
        pilot = create()
        assert not close(pilot, monkeypatch, original)
        assert len(wire["deliveries"]) == 2
        assert pilot.db.execute("SELECT state FROM telegram_native_card_closures").fetchone()[0] == "delivery_unknown"
        pilot.db.close()


def test_explicit_429_only_retries_after_due_with_original_pair(tmp_path, monkeypatch):
    original = source()
    with fixture(tmp_path) as (wire, _, create):
        pilot = create()
        arm(pilot, original)
        physical = pilot.api.call
        def limited(*_, **__): raise TelegramRejected(429, 60)
        monkeypatch.setattr(pilot.api, "call", limited)
        close(pilot, monkeypatch, original)
        assert pilot.db.execute("SELECT state,attempt FROM telegram_native_card_closures").fetchone()[:] == ("retry_wait",1)
        monkeypatch.setattr(pilot.api, "call", physical)
        pilot.db.close()
        pilot = create()
        assert not close(pilot, monkeypatch, original)
        with pilot.db: pilot.db.execute("UPDATE telegram_native_card_closures SET not_before=0")
        close(pilot, monkeypatch, original)
        assert len(wire["deliveries"]) == 2
        assert pilot.db.execute("SELECT state,attempt FROM telegram_native_card_closures").fetchone()[:] == ("delivered",2)
        pilot.db.close()


@pytest.mark.parametrize("host_type,level,task", [("nanobot","L2",False),("pi","L2",False),
    ("nanobot","L3",False),("pi","L3",False),("nanobot","L3",True),("pi","L3",True)])
def test_private_state_exact_source_only_and_no_presentation_or_resume(tmp_path, monkeypatch, host_type, level, task):
    request = {"subject":"alpha","instance":"office","hostType":host_type,"endpoint":"ep-a",
        "endpointKey":"telegram:8001:501","conversation":"original-chat","userDir":str(tmp_path)}
    record = {"credential_ref":"tg-" + "a" * 64}
    run = {"run_id":"original-run"}
    original = source(task)
    card = {**_interaction(interaction_id=original["interactionId"]), "type":"credential", "taskId":original["taskId"]}
    calls = []
    observed = {"state":"completed"}
    route = {"taskId":original["taskId"],"userSubject":"alpha","agentHost":host_type,
             "originEndpointId":"ep-a","activeConversationRef":"original-chat"} if task else None
    class Host:
        runs = SimpleNamespace(lookup=lambda *_, **__: run)
        worker_payload = lambda self,*_: request
        @asynccontextmanager
        async def private_session(self, _):
            async def call_tool(name, args, **kwargs):
                calls.append((name,args))
                assert name == "agentbridge_host_interaction_present" and args["state_only"] is True
                return SimpleNamespace(isError=False, structuredContent={"endpoint":{"endpointId":"ep-a"},"task":route,
                    "interaction":{"interactionId":card["interactionId"],"type":"credential","taskId":card["taskId"],**observed}})
            yield SimpleNamespace(call_tool=call_tool),{},level
    async def verified(*_, **__): return True
    async def paired(*_, **__): return {}
    monkeypatch.setattr("nanobot_agentbridge.native_card_state.verify_telegram_binding", verified)
    monkeypatch.setattr("nanobot_agentbridge.telegram_reception.scope", paired)
    host = Host()
    pinned = remember(host,record,card,run="original-run",request_key="tg:8001:501:9")
    assert remember(host,record,card,run="original-run",request_key="tg:8001:501:9") == pinned
    value = asyncio.run(read(host,record,{"source":pinned}))
    assert value == {"source":pinned,"state":"completed"}
    assert len(calls) == 1
    for changed in ({**pinned,"runId":"other-run"},{**pinned,"interactionId":str(uuid4())},
                    {**pinned,"sourceRef":str(uuid4())}):
        with pytest.raises(web.HTTPForbidden): asyncio.run(read(host,record,{"source":changed}))
    assert len(calls) == 1
    run["run_id"] = "changed-run"
    with pytest.raises(web.HTTPForbidden): asyncio.run(read(host,record,{"source":pinned}))
    run["run_id"] = "original-run"
    with pytest.raises(web.HTTPForbidden): asyncio.run(read(host,{"credential_ref":"new-pair"},{"source":pinned}))
    assert len(calls) == 1
    request["endpoint"] = "another-endpoint"
    with pytest.raises(web.HTTPForbidden): asyncio.run(read(host,record,{"source":pinned}))
    request["endpoint"] = "ep-a"
    assert remember(host,record,{**card,"type":"business_input","taskId":None},run="original-run",request_key="original") is None


def test_same_task_two_interactions_and_other_subject_have_separate_originals(tmp_path, monkeypatch):
    one, two, foreign = source(), source(), source()
    two["taskId"] = one["taskId"]
    with fixture(tmp_path) as (wire, _, create):
        pilot = create()
        for item, chat in ((one,501),(two,501),(foreign,502)): arm(pilot,item,chat)
        original = pilot.gateway._request
        def request(route,body,**kw):
            if route != "telegram-task": return original(route,body,**kw)
            return {"source":body["source"],"state":"completed"}
        monkeypatch.setattr(pilot.gateway,"_request",request)
        pilot.reconcile()
        assert len(wire["deliveries"]) == 6
        assert [(row["chat_id"],row["reply_parameters"]["message_id"]) for row in wire["deliveries"][3:]] == [(501,701),(501,702),(502,703)]
        pilot.native_card_closures.next_poll=0;pilot.reconcile()
        assert len(wire["deliveries"]) == 6
        pilot.db.close()


def test_existing_mirror_or_historical_card_never_becomes_native_watch(tmp_path):
    original = source()
    with fixture(tmp_path) as (wire,_,create):
        pilot = create()
        with pilot.db:
            pilot.db.execute("INSERT INTO telegram_card_deliveries VALUES(?,?,?,?,'delivered',?)",
                (pilot.bot_id,501,"interaction:"+original["interactionId"],str(uuid4()),101))
        arm(pilot,original)
        assert not wire["deliveries"]
        assert not pilot.db.execute("SELECT 1 FROM telegram_native_card_watches").fetchone()
        pilot.db.close()


def test_native_closed_watch_does_not_starve_more_than_eight_new_cards(tmp_path,monkeypatch):
    with fixture(tmp_path) as (wire,_,create):
        pilot=create()
        originals=[source() for _ in range(10)]
        for original in originals: arm(pilot,original)
        gateway=pilot.gateway._request
        def request(route,body,**kw):
            if route!='telegram-task': return gateway(route,body,**kw)
            return {'source':body['source'],'state':'expired'}
        monkeypatch.setattr(pilot.gateway,'_request',request)
        pilot.reconcile()
        assert len(wire['deliveries'])==18
        pilot.native_card_closures.next_poll=0;pilot.reconcile()
        assert len(wire['deliveries'])==20
        checked=[row[0] for row in pilot.db.execute('SELECT checked_at FROM telegram_native_card_watches')]
        pilot.native_card_closures.next_poll=0;pilot.reconcile()
        assert checked==[row[0] for row in pilot.db.execute('SELECT checked_at FROM telegram_native_card_watches')]
        pilot.db.close()


@pytest.mark.parametrize('level',['L2','L3'])
def test_actual_pi_private_host_card_and_state_routes_retain_original_run(tmp_path,monkeypatch,level):
    """Actual Host.action/Delivery and original per-run card store, isolated MCP boundary."""
    import sqlite3
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).parents[1]/'integrations/pi-agentbridge'))
    from pi_agentbridge.server import Host as PiHost,binding_key as pi_key
    from pi_agentbridge.cards import Cards
    record={'user_subject':'alpha','endpoint_id':'ep-a','endpoint_key':'telegram:8001:501',
            'session_key':'original-chat','credential_ref':'tg-'+64*'a'}
    req={'subject':'alpha','instance':'office','hostType':'pi','endpoint':'ep-a','endpointKey':record['endpoint_key'],
         'conversation':'original-chat','userDir':str(tmp_path),'trustedCardOrigins':['https://bridge.example.test']}
    ident=str(uuid4());key='tg:8001:501:9';run='original-run'
    original={**_interaction(interaction_id=ident),'type':'credential','taskId':None}
    original['presentation']['url']='https://bridge.example.test/auth/synthetic'
    requests=[]
    current={'state':'pending'}
    class Session:
        async def call_tool(self,name,args,**_):
            requests.append((name,args))
            assert name=='agentbridge_host_interaction_present'
            card={**original,**current}
            if args.get('state_only'):
                card={k:card[k] for k in ('interactionId','type','taskId','state')}
            return SimpleNamespace(isError=False,structuredContent={'interaction':card,'task':None,'endpoint':{'endpointId':'ep-a'}})
    async def verified(*_,**__):return True
    monkeypatch.setattr('nanobot_agentbridge.telegram_identity.verify_telegram_binding',verified)
    monkeypatch.setattr('nanobot_agentbridge.native_card_state.verify_telegram_binding',verified)
    monkeypatch.setattr('nanobot_agentbridge.telegram_reception.scope',verified)
    central=SimpleNamespace(level=level,session=Session(),meta={},require_binding=verified)
    @asynccontextmanager
    async def borrowed(*_,**__):yield central
    @asynccontextmanager
    async def opened(*_,**__):yield central.session,{},level
    class Local:
        private_session=opened
        worker_payload=lambda self,*_:req
    host=Local();host.instance='office';host.level=level;host.coordinator=None;host.commands=None
    host.card_origins=['https://bridge.example.test']
    host.runs=SimpleNamespace(lookup=lambda *_,**__: {'run_id':run,'state':'accepted'})
    db=sqlite3.connect(tmp_path/'pi-local.sqlite3');db.row_factory=sqlite3.Row
    host.cards=Cards(db,['https://bridge.example.test'])
    db.execute("INSERT INTO cards VALUES(?,?,'waiting',1,NULL)",(run,ident));db.commit()
    host.ledger=SimpleNamespace(db=db,lookup=lambda *_,**__: {'id':run,'state':'accepted'})
    host.bindings={pi_key(record):record};host.bound_session=borrowed
    host.control_sessions=SimpleNamespace(borrow=borrowed)
    action=lambda name,body:PiHost.action(host,name,body)
    try:
        result=asyncio.run(action('card',{'binding':record,'requestKey':key,'runId':run}))
        pinned=result['nativeCardSource']
        assert pinned['runId']==run and pinned['taskId'] is None and pinned['interactionId']==ident
        current['state']='completed'
        observed=asyncio.run(action('telegram-task',{'binding':record,'operation':'card_state','source':pinned}))
        assert observed=={'source':pinned,'state':'completed'}
        assert [args.get('state_only',False) for _,args in requests]==[False,True]
    finally:
        db.close()


def test_positive_original_offline_longer_than_a_day_still_closes_once(tmp_path,monkeypatch):
    import time
    original=source()
    with fixture(tmp_path) as (wire,_,create):
        pilot=create();arm(pilot,original);pilot.db.close()
        future=time.time()+25*3600
        monkeypatch.setattr('nanobot_agentbridge.native_card_closures.time.time',lambda:future)
        pilot=create()
        close(pilot,monkeypatch,original,'expired')
        assert len(wire['deliveries'])==2 and wire['deliveries'][1]['text']==text('credential','expired')
        pilot.native_card_closures.next_poll=0;pilot.reconcile()
        assert len(wire['deliveries'])==2
        pilot.db.close()


def test_close_cannot_reuse_original_platform_receipt(tmp_path,monkeypatch):
    original=source()
    with fixture(tmp_path) as (wire,_,create):
        pilot=create();arm(pilot,original)
        call=pilot.api.call
        def reused(method,**body):
            value=call(method,**body)
            if method=='sendMessage':value['message_id']=701
            return value
        monkeypatch.setattr(pilot.api,'call',reused)
        close(pilot,monkeypatch,original)
        assert pilot.db.execute('SELECT state,receipt FROM telegram_native_card_closures').fetchone()[:]==('delivery_unknown',None)
        pilot.native_card_closures.next_poll=0;pilot.reconcile()
        assert len(wire['deliveries'])==2
        pilot.db.close()
