"""Shared Python intake helpers through the real private MCP tools and owned store."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timedelta,timezone
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import create_central_mcp_server,validate_central_mcp_server_config
from tests.test_pi_workspace import pi,private_json
from tests.test_pi_workspace_result_views import paired_query
from tests.test_workspace_query_projection import register
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"integrations/nanobot-agentbridge"))
from nanobot_agentbridge.telegram_media_receipts import BINDING_FIELDS,prepare_source,publish_media,recover_route


def test_shared_private_intake_projects_original_resource_and_only_recovers_after_revoke(pi,tmp_path,monkeypatch):
    from aiohttp import web
    svc=pi.service
    identities=McpIdentityTokenStore(svc.db_path)
    identity=identities.issue(user_subject='user-a',expected_principal_ref='user-a',label='workspace-pi:pi-office:pi-a')
    pi.spec.update(adapterVersion='0.1.8',requiredLevel='L3');private_json(pi.route_file,pi.manifest)
    with svc.workspace._connect() as db:
        db.execute("UPDATE workspace_host_instances SET adapter_version='0.1.8' WHERE instance_id='pi-office'")
    register(svc,'user-a','pi','pi-office',identity['token_id'])
    _,scope,_,_,endpoint,links,_=paired_query(pi,identity=identity,query_v2=True)
    profile=json.loads((Path(__file__).parents[1]/'schemas/agent-host/v1/test-vectors.json').read_text())['profiles'][0]['value']
    profile.update(implementation={'name':'pi','version':'0.1.8'},hostInstanceId='pi-office')
    meta={'io.agentbridge/host-context':{'version':'1','agentHost':'pi','hostInstanceId':'pi-office','hostVersion':'0.1.8'},
          'io.agentbridge/host-profile':profile}
    config=validate_central_mcp_server_config(host='127.0.0.1',port=8790,public_base_url='http://testserver',tls_cert=None,tls_key=None)
    app=create_central_mcp_server(service=svc,identity_store=identities,config=config,auth_card_base_url='http://127.0.0.1:8780')
    token=tmp_path/'personal.token';token.write_text(identity['token']);token.chmod(0o600)
    owner={'user_subject':'user-a','endpoint_id':endpoint['workspaceEndpointId'],'endpoint_key':endpoint['workspaceEndpointKey'],
           'session_key':'original-workspace-session','credential_ref':'original-web-generation','tokenFile':str(token)}
    record={**owner,'endpoint_id':endpoint['endpointId'],'endpoint_key':endpoint['endpointKey'],
            'session_key':endpoint['conversationRef'],'credential_ref':'tg-'+endpoint['pairingRef'],
            'workspaceEndpointId':owner['endpoint_id'],'workspaceEndpointKey':owner['endpoint_key']}
    host=SimpleNamespace(instance='pi-office',bindings={'owner':owner},worker_payload=lambda r,_:{
        'userDir':str(tmp_path),'hostType':'pi','subject':'user-a','endpoint':r['endpoint_id'],'endpointKey':r['endpoint_key']})
    calls=[]
    with TestClient(app.streamable_http_app()) as client:
        async def call_tool(name,args=None,**options):
            calls.append((name,args))
            response=client.post('/mcp',headers={'Authorization':'Bearer '+identity['token'],
                'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-06-18'},
                json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args or {},'_meta':options.get('meta',meta)}})
            assert response.status_code==200
            value=response.json()['result']
            return SimpleNamespace(isError=value.get('isError',False),structuredContent=value.get('structuredContent'))
        session=SimpleNamespace(call_tool=call_tool)
        @asynccontextmanager
        async def opened(*_,**__):
            yield session,meta,'L3'
        @asynccontextmanager
        async def borrowed(original,**__):
            assert original is owner
            yield SimpleNamespace(session=session,meta=meta,level='L3')
        monkeypatch.setattr('nanobot_agentbridge.telegram_media_receipts.open_host_session',opened)
        host.control_sessions=SimpleNamespace(borrow=borrowed)
        async def run():
            assert (await call_tool('agentbridge_host_register')).structuredContent['acceptedLevel']=='L3'
            body={'binding':{k:record[k] for k in BINDING_FIELDS},'messageId':1001,'receivedAt':(datetime.now(timezone.utc).replace(microsecond=0)+timedelta(seconds=1)).isoformat()}
            source=await prepare_source(host,record,body)
            body.update(pairingRef=source['pairingRef'],ordinal=0,fileName='原文件.txt',mimeType='text/plain',byteSize=3,
                        sha256=hashlib.sha256(b'abc').hexdigest(),contentBase64='YWJj',failure=None)
            result=await publish_media(host,record,body)
            receipt=result['mediaReceipt']
            assert svc.timeline_attachments.ready_payload(receipt['attachmentId'])['body']==b'abc'
            assert pi.app.timeline_attachment(pi.accounts['a'],receipt['attachmentId'])['body']==b'abc'
            links.revoke(pi.accounts['a']['account_id'],'pi-office','8801')
            probe={k:v for k,v in body.items() if k!='contentBase64'}
            assert await recover_route(host,probe,host_type='pi')==result
            assert calls[-1][0]=='agentbridge_host_telegram_media_append' and calls[-1][1]['content_base64'] is None
            with pytest.raises(web.HTTPForbidden):
                await publish_media(host,record,body)
            with pytest.raises((PermissionError,ValueError)):
                pi.app.timeline_attachment(pi.accounts['a'],receipt['attachmentId'])
            assert not any('commit' in name or 'resume' in name for name,_ in calls)
        asyncio.run(run())
