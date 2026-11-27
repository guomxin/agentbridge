"""Shared private adapters and the actual poller fence intake and reconcile original text."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'integrations/nanobot-agentbridge'))
from nanobot_agentbridge.telegram_reception import receive,validate
from nanobot_agentbridge.telegram_timeline import publish_timeline
from nanobot_agentbridge.telegram_history_receipts import recover_route
from nanobot_agentbridge.telegram_media_receipts import BINDING_FIELDS
from tests.support.nanobot_telegram_loopback import fixture as transport


@pytest.fixture
def epoch_host(tmp_path,monkeypatch):
    token=tmp_path/'personal.token';token.write_text('synthetic-original-personal-token');token.chmod(0o600)
    owner=dict(user_subject='fixture-owner',endpoint_id=str(uuid4()),endpoint_key='workspace:original',
               session_key='web-session',credential_ref='web-grant',tokenFile=str(token))
    record={**owner,'endpoint_id':str(uuid4()),'endpoint_key':'telegram:8001:501','session_key':'private-session',
            'credential_ref':'tg-'+'a'*64,'workspaceEndpointId':owner['endpoint_id'],'workspaceEndpointKey':owner['endpoint_key']}
    state=dict(pair='a'*64,floor=int(time.time())-2,paired=True,scoped=True,host='nanobot',calls=[],receipts={},source={})
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    host=SimpleNamespace(instance='isolated-host',bindings={'owner':owner},runs=SimpleNamespace(_db=db,
        lookup=lambda *_,**__:None,matches_message=lambda *_:False),worker_payload=lambda r,_:{
            'hostType':state['host'],'userDir':str(tmp_path),'endpointKey':r['endpoint_key']})
    async def call(name,args,**_):
        state['calls'].append((name,args))
        if name=='agentbridge_host_telegram_endpoint':
            value={'endpointId':record['endpoint_id'],'endpointKey':record['endpoint_key'],
                   'conversationRef':record['session_key'],'pairingRef':state['pair'],
                   'generationScoped':state['scoped'],'receptionFloor':state['floor'] if state['scoped'] else None}
            return SimpleNamespace(isError=not state['paired'],structuredContent=value)
        if name=='agentbridge_host_telegram_media_source':
            key=args['source_message_id'];value={'recorded':True,'sourceEndpointId':record['endpoint_id'],
                'pairingRef':state['pair'],'receivedAt':args['received_at']}
            state['source'].setdefault(key,value)
            return SimpleNamespace(isError=not state['paired'] or state['source'][key]!=value,structuredContent=value)
        assert name=='agentbridge_host_timeline_append' and args['projection_v2']
        key=args['message_key'];receipt=state['receipts'].get(key)
        if args.get('projection_receipt_only'):
            if receipt is None:
                return SimpleNamespace(isError=False,structuredContent={'status':'unavailable','recorded':False})
            assert receipt['textSha256']==hashlib.sha256(args['text'].encode()).hexdigest()
            value={'status':'succeeded','endpoint':{'endpointId':record['endpoint_id']},'projectionReceipt':receipt,
                   'entry':{'entryId':receipt['entryId'],'role':args['role'],'retained':False}}
        else:
            assert state['paired']
            receipt={'entryId':str(uuid4()),'sourceEndpointId':record['endpoint_id'],'messageKey':key,
                     'role':args['role'],'textSha256':hashlib.sha256(args['text'].encode()).hexdigest()}
            state['receipts'].setdefault(key,receipt);receipt=state['receipts'][key]
            value={'status':'succeeded','endpoint':{'endpointId':record['endpoint_id']},'projectionReceipt':receipt,
                   'entry':{'entryId':receipt['entryId'],'role':args['role'],'text':args['text']}}
        return SimpleNamespace(isError=False,structuredContent=value)
    session=SimpleNamespace(call_tool=call)
    @asynccontextmanager
    async def opened(*_,**__):
        yield session,{},'L3'
    @asynccontextmanager
    async def borrowed(*_,**__):
        yield SimpleNamespace(session=session,meta={},level='L3')
    async def verified(*_,**__):
        return state['paired']
    host.control_sessions=SimpleNamespace(borrow=borrowed)
    monkeypatch.setattr('nanobot_agentbridge.session_pool.open_host_session',opened)
    monkeypatch.setattr('nanobot_agentbridge.telegram_media_receipts.open_host_session',opened)
    monkeypatch.setattr('nanobot_agentbridge.telegram_identity.verify_telegram_binding',verified)
    monkeypatch.setattr('nanobot_agentbridge.telegram_media_receipts.verify_telegram_binding',verified)
    yield host,owner,record,state,token
    db.close()


def stamp(seconds):
    return datetime.fromtimestamp(seconds,timezone.utc).isoformat()


@pytest.mark.parametrize('host_type',['nanobot','pi'])
def test_original_source_required_before_admission_and_first_history(epoch_host,host_type):
    from aiohttp import web
    host,_,record,state,_=epoch_host;state['host']=host_type
    body={'messageId':7,'receivedAt':stamp(state['floor']+1)}
    text={'messageId':7,'role':'user','text':'原公开问题','runId':None,'deliveredMessageIds':[],
          'projectionMode':'telegram_received_v2'}
    async def run():
        with pytest.raises(web.HTTPForbidden):
            await validate(host,record,'tg:8001:501:7')
        with pytest.raises(web.HTTPForbidden):
            await publish_timeline(host,record,text)
        assert not state['receipts']
        assert not host.runs._db.execute("SELECT name FROM sqlite_master WHERE name='telegram_received_sources'").fetchone()
        for date in [stamp(state['floor']-1),stamp(state['floor']),None,stamp(state['floor']+0.5),stamp(time.time()+120)]:
            with pytest.raises((web.HTTPForbidden,ValueError)):
                await receive(host,record,{**body,'receivedAt':date})
        assert not state['source']
        result=await receive(host,record,body)
        assert result['generationScoped'] and result['receivedAt']==body['receivedAt']
        assert (await validate(host,record,'tg:8001:501:7'))['pairingRef']==state['pair']
        assert await publish_timeline(host,record,text)=={'recorded':True}
        assert len(state['receipts'])==1
        for key in ['tg:8001:502:7','tg:8001:501:8']:
            with pytest.raises(web.HTTPForbidden):
                await validate(host,record,key)
        state['pair']='b'*64
        with pytest.raises(web.HTTPForbidden):
            await validate(host,record,'tg:8001:501:7')
        with pytest.raises(web.HTTPForbidden):
            await validate(host,{**record,'credential_ref':'tg-'+state['pair']},'tg:8001:501:7')
    asyncio.run(run())


@pytest.mark.parametrize('host_type',['nanobot','pi'])
def test_original_text_receipt_only_after_revoke_never_admits_missing_role(epoch_host,host_type):
    from aiohttp import web
    host,_,record,state,token=epoch_host;state['host']=host_type
    body={'messageId':7,'role':'user','text':'原公开问题','runId':None,'deliveredMessageIds':[],
          'projectionMode':'telegram_received_v2','binding':{k:record[k] for k in BINDING_FIELDS}}
    async def run():
        await receive(host,record,{'messageId':7,'receivedAt':stamp(state['floor']+1)})
        assert await recover_route(host,body,host_type=host_type)=={'recorded':False}
        await publish_timeline(host,record,body)
        state['paired']=False;state['calls'].clear()
        assert await recover_route(host,body,host_type=host_type)=={'recorded':True}
        assert len(state['calls'])==1 and state['calls'][0][1]['projection_receipt_only'] is True
        assert await recover_route(host,{**body,'role':'assistant'},host_type=host_type)=={'recorded':False}
        for change in [{'text':'同键篡改'},{'runId':'new-run'},{'binding':{**body['binding'],'credential_ref':'new-generation'}}]:
            with pytest.raises(web.HTTPForbidden):
                await recover_route(host,{**body,**change},host_type=host_type)
        with pytest.raises(web.HTTPForbidden):
            await publish_timeline(host,record,body)
        token.write_text('synthetic-rotated-token')
        with pytest.raises(web.HTTPForbidden):
            await recover_route(host,body,host_type=host_type)
        assert len(state['receipts'])==1
    asyncio.run(run())


def test_inherited_intake_preserves_legacy_behavior_without_inventing_receipt_authority(epoch_host):
    host,_,record,state,_=epoch_host;state['scoped']=False
    async def run():
        assert await receive(host,record,{'messageId':7,'receivedAt':None})=={'accepted':True,'generationScoped':False}
        assert await validate(host,record,'tg:8001:501:7') is None
        body={'messageId':7,'role':'user','text':'继承路径','runId':None,'deliveredMessageIds':[],
              'projectionMode':'telegram_received_v2','binding':{k:record[k] for k in BINDING_FIELDS}}
        await publish_timeline(host,record,body)
        assert await recover_route(host,body,host_type='nanobot')=={'recorded':False}
    asyncio.run(run())


def test_poller_restart_queries_original_receipt_before_current_binding(tmp_path,monkeypatch):
    with transport(tmp_path) as (state,_,create):
        pilot=create();binding,digest=pilot._binding(501)
        with pilot.db:
            pilot.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,'binding_changed',NULL,NULL)",(8001,501,7,binding['user_subject'],digest))
            pilot.db.execute('INSERT INTO telegram_inbound_sources VALUES(?,?,?,?,?,?)',
                (8001,501,7,json.dumps(binding),'a'*64,stamp(int(time.time()))))
            pilot.timeline.queue(501,7,'user','原问题',projection_mode='telegram_received_v2')
        pilot.db.close();pilot=create();calls=[]
        def publish(original,*args,**kwargs):
            calls.append((original,args,kwargs));assert original==binding and kwargs['receipt_only']
            return True
        monkeypatch.setattr(pilot.gateway,'publish_telegram_timeline',publish)
        monkeypatch.setattr(pilot,'_binding',lambda *_:pytest.fail('receipt-only may not resolve current pair'))
        pilot.timeline.flush()
        assert len(calls)==1 and not state['admissions'] and not state['deliveries']
        assert pilot.db.execute('SELECT status FROM telegram_projection_outcomes').fetchone()[0]=='acknowledged'
        assert not pilot.db.execute('SELECT 1 FROM telegram_chat_projection').fetchone()
        pilot.db.close()


def test_poller_old_platform_message_stops_before_caption_download_or_model(tmp_path,monkeypatch):
    from agentbridge.workspace.gateway import GatewayRequestError
    from tests.support.nanobot_telegram_loopback import update
    with transport(tmp_path) as (state,_,create):
        pilot=create();original=pilot.gateway._request;calls=[]
        def request(route,body,**kwargs):
            if route=='telegram-received-source':
                calls.append(body)
                raise GatewayRequestError('NANOBOT_BINDING_UNAVAILABLE','synthetic denial')
            return original(route,body,**kwargs)
        monkeypatch.setattr(pilot.gateway,'_request',request)
        for msg in [update(1,message=7,date=int(time.time())-30),
                    update(2,message=8,date=int(time.time())-30,caption='旧附件说明',document={'file_id':'old'})]:
            pilot.ingest(msg);pilot.ingest(msg)
        assert len(calls)==2
        assert not state['admissions'] and not state['downloads'] and not state['timeline']
        assert not pilot.db.execute('SELECT 1 FROM telegram_chat_projection').fetchone()
        assert not pilot.db.execute('SELECT 1 FROM telegram_inbound_sources').fetchone()
        assert all('请在完成配对后重新发送' in row['text'] for row in state['deliveries'])
        pilot.db.close()


def test_poller_source_outage_leaves_offset_and_first_admission_unconfirmed(tmp_path,monkeypatch):
    from agentbridge.workspace.gateway import GatewayRequestError
    from nanobot_agentbridge.telegram import TelegramUnavailable
    from tests.support.nanobot_telegram_loopback import update
    with transport(tmp_path) as (state,_,create):
        pilot=create();original=pilot.gateway._request
        def request(route,body,**kwargs):
            if route=='telegram-received-source':
                raise GatewayRequestError('GATEWAY_UNAVAILABLE','synthetic timeout')
            return original(route,body,**kwargs)
        monkeypatch.setattr(pilot.gateway,'_request',request)
        with pytest.raises(TelegramUnavailable):
            pilot.ingest(update(1,message=7))
        assert not pilot._record(501,7) and not state['admissions'] and not state['timeline']
        pilot.db.close()


def test_nano_local_binding_digest_changes_with_explicit_pair_generation(monkeypatch):
    from nanobot_agentbridge.telegram_routes import TelegramRoutes
    fields=tuple(BINDING_FIELDS)
    monkeypatch.setitem(sys.modules,'nanobot_agentbridge.server',SimpleNamespace(
        BINDING_KEYS=fields,binding_key=lambda r:tuple(r[k] for k in fields),identity_denial=lambda _:False))
    owner={'user_subject':'fixture-owner','endpoint_id':str(uuid4()),'endpoint_key':'workspace:original',
           'session_key':'original-web','credential_ref':'web-grant'}
    host=SimpleNamespace(instance='fixture-instance',bindings={'owner':owner},worker_payload=lambda r,_:r)
    routes=TelegramRoutes(host,{'telegramPilot':{'botId':8001,'username':'fixture_bot'}})
    original=routes.record(owner,'501');state={'scoped':False,'pair':'a'*64}
    async def call(name,args,**_):
        assert name=='agentbridge_host_telegram_endpoint'
        return SimpleNamespace(isError=False,structuredContent={
            'endpointId':original['endpoint_id'],'endpointKey':original['endpoint_key'],
            'conversationRef':original['session_key'],'workspaceEndpointId':owner['endpoint_id'],
            'workspaceEndpointKey':owner['endpoint_key'],'generationScoped':state['scoped'],'pairingRef':state['pair']})
    @asynccontextmanager
    async def opened(*_,**__):
        yield SimpleNamespace(call_tool=call),{},'L3'
    async def verified(*_,**__): return True
    monkeypatch.setattr('nanobot_agentbridge.session_pool.open_host_session',opened)
    monkeypatch.setattr('nanobot_agentbridge.telegram_routes.verify_telegram_binding',verified)
    async def run():
        body={'botId':'8001','senderId':'501'}
        old=(await routes.action('telegram-route',body))['binding']
        assert old['credential_ref']==original['credential_ref']
        state['scoped']=True
        first=(await routes.action('telegram-route',body))['binding']
        state['pair']='b'*64
        second=(await routes.action('telegram-route',body))['binding']
        assert {old['credential_ref'],first['credential_ref'],second['credential_ref']}=={
            original['credential_ref'],'tg-'+'a'*64,'tg-'+'b'*64}
        assert old['endpoint_id']==first['endpoint_id']==second['endpoint_id']
    asyncio.run(run())
