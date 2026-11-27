"""Actual Pi private wrapper + actual MCP + shared consumer + loopback Bot wire."""
import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime,timedelta,timezone
from email.parser import BytesParser
from email.policy import default
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import create_central_mcp_server,validate_central_mcp_server_config
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.test_pi_workspace import pi,private_json  # noqa:F401
from tests.test_workspace_query_projection import register
from tests.test_workspace_query_delivery import drain,close

sys.path.insert(0,str(Path(__file__).parents[1]/'integrations/pi-agentbridge'))
from pi_agentbridge.server import Host,BINDING_FIELDS
from nanobot_agentbridge.notifications import TerminalMessages
from nanobot_agentbridge.telegram import TelegramAPI


def test_actual_pi_media_routes_original_upload_and_original_receipt_after_revoke(pi,tmp_path):
    svc=pi.service;identities=McpIdentityTokenStore(svc.db_path)
    identity=identities.issue(user_subject='user-a',expected_principal_ref='user-a',label='workspace-pi:pi-office:pi-a')
    pi.spec.update(adapterVersion='0.1.8',requiredLevel='L3');private_json(pi.route_file,pi.manifest)
    with svc.workspace._connect() as db:
        db.execute("UPDATE workspace_host_instances SET adapter_version='0.1.8' WHERE instance_id='pi-office'")
    register(svc,'user-a','pi','pi-office',identity['token_id'])
    account=pi.accounts['a']['account_id'];links=TelegramPilotLinks(svc)
    challenge=links.begin(account,'pi-office','8801')
    confirmed=links.present('user-a','pi-office','8801','501',challenge['code'])
    links.confirm(account,'pi-office','8801',challenge['challenge_id'],confirmed)
    endpoint=links.ensure_endpoint('user-a',identity['token_id'],'pi-office','8801','501')
    with svc.tasks._connect() as db:
        row=dict(db.execute('SELECT * FROM client_endpoints WHERE endpoint_id=?',(endpoint['workspaceEndpointId'],)).fetchone())
    svc.tasks.ensure_endpoint(user_subject='user-a',token_id=identity['token_id'],agent_host='pi',
        **{k:row[k] for k in ('endpoint_key','client_type','external_subject','conversation_ref','account_id')},
        route=json.loads(row['route_json']),capabilities=json.loads(row['capabilities_json']))
    pi.app.hosts.set_default(account,'pi-office',expected_revision=1)
    token=tmp_path/'personal.token';token.write_text(identity['token']);token.chmod(0o600)
    owner={'user_subject':'user-a','endpoint_id':endpoint['workspaceEndpointId'],'endpoint_key':endpoint['workspaceEndpointKey'],
        'session_key':row['conversation_ref'],'credential_ref':'original-web-generation','tokenFile':str(token)}
    record={**owner,'endpoint_id':endpoint['endpointId'],'endpoint_key':endpoint['endpointKey'],
        'session_key':endpoint['conversationRef'],'credential_ref':'tg-'+endpoint['pairingRef'],
        'workspaceEndpointId':owner['endpoint_id'],'workspaceEndpointKey':owner['endpoint_key']}
    binding={k:record[k] for k in BINDING_FIELDS}
    profile=json.loads((Path(__file__).parents[1]/'schemas/agent-host/v1/test-vectors.json').read_text())['profiles'][0]['value']
    profile.update(implementation={'name':'pi','version':'0.1.8'},hostInstanceId='pi-office')
    meta={'io.agentbridge/host-context':{'version':'1','agentHost':'pi','hostInstanceId':'pi-office','hostVersion':'0.1.8'},
        'io.agentbridge/host-profile':profile}
    config=validate_central_mcp_server_config(host='127.0.0.1',port=8790,public_base_url='http://testserver',tls_cert=None,tls_key=None)
    app=create_central_mcp_server(service=svc,identity_store=identities,config=config,auth_card_base_url='http://127.0.0.1:8780')
    ledger=sqlite3.connect(tmp_path/'pi-ledger.db');calls=[];routes=[];live=[True]
    with TestClient(app.streamable_http_app()) as client:
        async def call_tool(name,args=None,**options):
            calls.append((name,args))
            response=client.post('/mcp',headers={'Authorization':'Bearer '+identity['token'],
                'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-06-18'},
                json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args or {},'_meta':options.get('meta',meta)}})
            assert response.status_code==200
            value=response.json()['result']
            return SimpleNamespace(isError=value.get('isError',False),structuredContent=value.get('structuredContent'))
        central=SimpleNamespace(session=SimpleNamespace(call_tool=call_tool),meta=meta,level='L3')
        @asynccontextmanager
        async def borrowed(original,**_):
            assert original is owner or original is record
            yield central
        @asynccontextmanager
        async def checkout(original,**_):
            assert original is record
            async def check(): return central
            yield check
        async def current(*_): return record if live[0] else None
        host=SimpleNamespace(instance='pi-office',level='L3',coordinator=SimpleNamespace(program=tmp_path/'coordinator-unused'),commands=None,
            cards=SimpleNamespace(origins=[]),ledger=SimpleNamespace(db=ledger,lookup=lambda *_:None),bindings={'owner':owner},
            control_sessions=SimpleNamespace(borrow=borrowed,checkout=checkout),
            telegram=SimpleNamespace(current=current,delivery_hint=current,verified=current,bot='8801'),
            user_directory=lambda _:tmp_path,spec={'mcpUrl':'http://testserver'})
        host.worker_payload=lambda rec,run:Host.worker_payload(host,rec,run)
        def request(route,body,**_):
            routes.append((route,body))
            return asyncio.run(Host.action(host,route,{'instance':'pi-office',**body}))
        assert asyncio.run(call_tool('agentbridge_host_register')).structuredContent['acceptedLevel']=='L3'
        body={'binding':binding,'messageId':1001,'receivedAt':(datetime.now(timezone.utc).replace(microsecond=0)+timedelta(seconds=1)).isoformat()}
        history={'binding':binding,'messageId':1001,'role':'user','text':'原附件说明','runId':None,
                 'deliveredMessageIds':[],'projectionMode':'telegram_received_v2'}
        with pytest.raises(HTTPException):
            request('telegram-timeline',history)
        assert request('telegram-received-source',body)['accepted']
        assert request('telegram-timeline',history)=={'recorded':True}
        source=request('telegram-media-source',body)
        original={**body,'pairingRef':source['pairingRef'],'ordinal':0,'fileName':'原文件.txt','mimeType':'text/plain',
            'byteSize':3,'sha256':hashlib.sha256(b'abc').hexdigest(),'contentBase64':'YWJj','failure':None}
        stored=request('telegram-media-append',original)
        assert pi.app.timeline_attachment(pi.accounts['a'],stored['mediaReceipt']['attachmentId'])['body']==b'abc'
        raw=b'\x89PNG\r\n\x1a\noriginal';wire=[]
        svc.workspace.accept_chat_request(account_id=account,user_subject='user-a',message='原图说明。',
            idempotency_key='pi-real-wrapper-media',attachments=[{'fileName':'原图.png','mimeType':'image/png',
            'content':base64.b64encode(raw).decode()}],tasks=svc.tasks,attachment_store=svc.timeline_attachments,
            media_base_url='https://fixture.invalid',instance_id='pi-office')
        class Bot(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def do_POST(self):
                payload=self.rfile.read(int(self.headers['Content-Length']))
                if self.path.endswith('/sendDocument'):
                    mime=BytesParser(policy=default).parsebytes(('Content-Type: '+self.headers['Content-Type']+'\r\n\r\n').encode()+payload)
                    parts={p.get_param('name',header='content-disposition'):p for p in mime.iter_parts()}
                    assert parts['chat_id'].get_payload(decode=True)==b'501'
                    data=parts['document'].get_payload(decode=True)
                    wire.append(('document',data))
                    result={'message_id':701,'chat':{'id':501,'type':'private'},'from':{'id':8801},
                        'document':{'file_id':'synthetic-file','file_size':len(data)}}
                else:
                    assert self.path.endswith('/sendMessage')
                    message=json.loads(payload);assert message['chat_id']==501
                    wire.append(('text',message['text']))
                    result={'message_id':702,'chat':{'id':501,'type':'private'},'from':{'id':8801}}
                answer=json.dumps({'ok':True,'result':result}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(answer)));self.end_headers();self.wfile.write(answer)
        server=ThreadingHTTPServer(('127.0.0.1',0),Bot);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        api=TelegramAPI('synthetic-token',connection_factory=lambda:HTTPConnection(*server.server_address,timeout=5),private_sender=lambda chat:live[0] and chat==501)
        db=sqlite3.connect(tmp_path/'pilot.db');db.row_factory=sqlite3.Row
        pilot=SimpleNamespace(db=db,gateway=SimpleNamespace(_request=request),host_type='pi',bot_id=8801,api=api,
            _binding=lambda chat:(binding,'same') if live[0] else (None,None),navigation=SimpleNamespace(remember=lambda *_:None))
        terminal=TerminalMessages(pilot)
        try:
            terminal.poll(501,binding,'same');drain(terminal)
            assert [data for kind,data in wire if kind=='document']==[raw]
            assert len([data for kind,data in wire if kind=='text'])==1
            with svc.tasks._connect() as conn:
                assert tuple(conn.execute('SELECT state,receipt FROM workspace_media_deliveries').fetchone())==('sent',701)
            ack=next(body for route,body in routes if route=='telegram-workspace-media-v2' and body.get('action')=='ack')
            links.revoke(account,'pi-office','8801');live[0]=False
            assert request('telegram-workspace-media-v2',ack)=={'recorded':True}
            assert request('telegram-media-append',{**original,'contentBase64':None})==stored
            assert request('telegram-timeline',{**history,'receiptOnly':True})=={'recorded':True}
            assert request('telegram-timeline',{**history,'messageId':1002,'receiptOnly':True})=={'recorded':False}
            assert len(wire)==2
            with pytest.raises(HTTPException): request('telegram-media-source',{**body,'messageId':1002})
            with pytest.raises((PermissionError,ValueError)): pi.app.timeline_attachment(pi.accounts['a'],stored['mediaReceipt']['attachmentId'])
            assert not any('commit' in name or 'resume' in name for name,_ in calls)
        finally:
            close(terminal);api.close();db.close();server.shutdown();server.server_close();thread.join();ledger.close()
