"""Private original media is independent of model admission and immutable per item."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from unittest.mock import patch

import pytest

from agentbridge.auth.timeline_attachment import TrustedTimelineAttachmentApplication
from agentbridge.core import telegram_media_receipts as media
from agentbridge.core.timeline_attachments import TimelineAttachmentExpired
from tests.support.latency_acceptance import OWNER
from tests.test_workspace_query_projection import openclaw  # noqa: F401
from tests.test_workspace_query_projection import register
from tests.test_pi_workspace import pi  # noqa: F401
from tests.test_pi_workspace_result_views import paired_query
from agentbridge.core.mcp_identities import McpIdentityTokenStore


def source(fixture, token, endpoint, message=101, stamp=None):
    service=fixture.service
    body=dict(subject=OWNER,host='openclaw',instance='openclaw-gateway',token=token,
              endpoint_key=endpoint['endpoint_key'],source_message_id=message,
              received_at=(stamp or datetime.now(timezone.utc).replace(microsecond=0)).isoformat())
    proof=media.source(service,**body)
    return body,proof


def original(body, proof, raw=b'\x89PNG\r\n\x1a\noriginal', mime='image/png', name='原图.png', ordinal=0):
    return dict(**body,pairing_ref=proof['pairingRef'],ordinal=ordinal,file_name=name,mime_type=mime,
                byte_size=len(raw),sha256=hashlib.sha256(raw).hexdigest(),content_base64=base64.b64encode(raw).decode())


@pytest.mark.parametrize(('raw','mime','name'),[(b'\x89PNG\r\n\x1a\noriginal','image/png','图.png'),
    (b'%PDF-1.4\nsynthetic','application/pdf','文件.pdf'),('列一,列二\n甲,乙'.encode(),'text/csv','表.csv'),
    (b'<script>parent.stolen=true</script>','text/html','download-only.html')])
def test_owned_original_download_and_receipt_never_use_public_media_route(openclaw, raw, mime, name):
    fixture,token,endpoint=openclaw
    svc=fixture.service
    body,proof=source(*openclaw)
    args=original(body,proof,raw,mime,name)
    with patch.object(svc,'invoke',side_effect=AssertionError('Media cannot execute business or models')):
        result=media.append(svc,**args)
        receipt=result['mediaReceipt']
        assert receipt['state']=='stored' and receipt['sourceEndpointId']==endpoint['endpoint_id']
        assert receipt['receivedAt']==body['received_at']
        assert datetime.fromisoformat(receipt['expiresAt'])==datetime.fromisoformat(body['received_at'])+timedelta(days=7)
        assert media.append(svc,**{**args,'content_base64':None})==result
        aid=receipt['attachmentId']
        media.authorize_download(svc,OWNER,aid)
        assert svc.timeline_attachments.ready_payload(aid)['body']==raw
        with pytest.raises(PermissionError): media.authorize_download(svc,'other-user',aid)
        assert TrustedTimelineAttachmentApplication(attachment_store=svc.timeline_attachments).get_file(aid).status==404
    with svc.tasks._connect() as db:
        entry=db.execute('SELECT * FROM user_timeline WHERE entry_id=?',(receipt['entryId'],)).fetchone()
        assert entry['role']=='user' and entry['text']=='原消息附件' and not entry['task_id']
        attachment=json.loads(entry['payload_json'])['attachments'][0]
        assert attachment['type']==('image' if mime.startswith('image/') else 'document')
        assert db.execute('SELECT count(*) FROM telegram_media_receipts').fetchone()[0]==1
        assert not db.execute('SELECT 1 FROM operations UNION ALL SELECT 1 FROM agent_host_dispatches').fetchone()
        db.execute("UPDATE client_endpoints SET state='inactive' WHERE endpoint_id=?",(endpoint['endpoint_id'],))
    assert media.append(svc,**{**args,'content_base64':None})==result
    assert media.source(svc,**body)==proof
    with pytest.raises((PermissionError,ValueError)): media.authorize_download(svc,OWNER,aid)
    with pytest.raises((PermissionError,ValueError)): media.append(svc,**{**args,'ordinal':1})
    with pytest.raises(PermissionError): media.append(svc,**{**args,'file_name':'changed.png'})
    svc.timeline_attachments.clock=lambda:datetime.fromisoformat(receipt['expiresAt'])+timedelta(seconds=1)
    with pytest.raises(TimelineAttachmentExpired): svc.timeline_attachments.ready_payload(aid)
    assert media.append(svc,**{**args,'content_base64':None})==result


def test_original_source_precedes_download_and_cannot_move_to_later_credentials(openclaw):
    fixture,token,endpoint=openclaw
    svc=fixture.service
    body,proof=source(*openclaw)
    args=original(body,proof)
    with pytest.raises(PermissionError): media.append(svc,**{**args,'source_message_id':102})
    assert media.append(svc,**{**args,'content_base64':None})=={'recorded':False}
    with pytest.raises(PermissionError): source(*openclaw,message=103,stamp=datetime.now(timezone.utc)-timedelta(hours=1))
    with pytest.raises(ValueError): source(*openclaw,message=104,stamp=datetime.now(timezone.utc)+timedelta(hours=1))
    with svc.tasks._connect() as db:
        db.execute('UPDATE mcp_identity_tokens SET created_at=? WHERE token_id=?',
                   ((datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),token))
    assert media.source(svc,**body)==proof  # Original source recovery doesn't acquire a new pair.
    with pytest.raises(PermissionError): source(*openclaw,message=105)


@pytest.mark.parametrize('failure',list(media.FAILURES))
def test_fixed_failure_is_visible_without_bytes_or_fabricated_assistant(openclaw,failure):
    fixture,token,endpoint=openclaw
    svc=fixture.service
    body,proof=source(*openclaw)
    args=dict(**body,pairing_ref=proof['pairingRef'],ordinal=0,file_name='附件',mime_type='application/octet-stream',
              byte_size=0,sha256=None,failure=failure)
    receipt=media.append(svc,**args)['mediaReceipt']
    assert receipt['state']=='unavailable' and receipt['attachmentId'] is None
    with svc.tasks._connect() as db:
        row=db.execute('SELECT role,payload_json FROM user_timeline WHERE entry_id=?',(receipt['entryId'],)).fetchone()
        assert row['role']=='user'
        payload=json.loads(row['payload_json'])['attachments'][0]
        assert media.failure_attachment(payload)['reason']==media.FAILURES[failure]
        assert not db.execute('SELECT 1 FROM timeline_attachments').fetchone()
    for extra in ({'content_base64':'AAAA'},{'file_name':'secret-url-or-token'},{'failure':'raw-error'}):
        with pytest.raises(ValueError): media.append(svc,**{**args,**extra})
    with pytest.raises(PermissionError): media.append(svc,**original(body,proof))


@pytest.mark.parametrize('changes',[{'sha256':'0'*64},{'content_base64':'invalid base64'},
    {'file_name':'../secret'},{'ordinal':4},{'mime_type':'application/x-executable'},
    {'mime_type':'application/pdf'},{'byte_size':True},{'pairing_ref':'0'*64},
    {'received_at':'2026-11-27T01:00:00'}, {'token':'different-token'}, {'instance':'foreign-instance'}])
def test_changed_or_unauthorized_media_never_leaves_partial_rows(openclaw,changes):
    fixture,token,endpoint=openclaw
    body,proof=source(*openclaw)
    with pytest.raises((PermissionError,ValueError)):
        media.append(fixture.service,**{**original(body,proof),**changes})
    with fixture.service.tasks._connect() as db:
        assert not db.execute('SELECT 1 FROM telegram_media_receipts UNION ALL SELECT 1 FROM timeline_attachments').fetchone()


def test_original_expiry_and_independent_ordinal_survive_restart(openclaw):
    fixture,token,endpoint=openclaw
    svc=fixture.service
    body,proof=source(*openclaw)
    first=media.append(svc,**original(body,proof))['mediaReceipt']
    second=media.append(svc,**original(body,proof,b'\xff\xd8\xffsecond','image/jpeg','第二.jpg',1))['mediaReceipt']
    assert first['entryId']!=second['entryId'] and first['attachmentId']!=second['attachmentId']
    svc.timeline_attachments.clock=lambda:datetime.fromisoformat(first['expiresAt'])+timedelta(seconds=1)
    with pytest.raises(ValueError): media.append(svc,**original(body,proof,ordinal=2))
    expired=media.append(svc,**dict(**body,pairing_ref=proof['pairingRef'],ordinal=2,file_name='附件',
        mime_type='application/octet-stream',byte_size=0,sha256=None,failure='expired'))['mediaReceipt']
    assert expired['expiresAt']==first['expiresAt'] and expired['state']=='unavailable'


def test_pi_current_pair_projects_documents_and_fixed_failures_without_model_or_task(pi):
    svc=pi.service
    identity=McpIdentityTokenStore(svc.db_path).issue(user_subject='user-a',expected_principal_ref='user-a',
        label='workspace-pi:pi-office:pi-a')
    register(svc,'user-a','pi','pi-office',identity['token_id'])
    _,scope,_,_,endpoint,links,pair=paired_query(pi,identity=identity,query_v2=True)
    body=dict(subject='user-a',host='pi',instance='pi-office',token=identity['token_id'],
        endpoint_key=scope['endpoint_key'],source_message_id=501,
        received_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    proof=media.source(svc,**body)
    args=original(body,proof,b'col\nvalue','text/csv','original.csv')
    receipt=media.append(svc,**args)['mediaReceipt']
    fail=media.append(svc,**dict(**body,pairing_ref=proof['pairingRef'],ordinal=1,file_name='附件',
        mime_type='application/octet-stream',byte_size=0,sha256=None,failure='unsupported'))['mediaReceipt']
    first=pi.app.list_timeline(pi.accounts['a'])
    again=pi.app.list_timeline(pi.accounts['a'])
    for timeline in (first,again):
        by_id={row['entry_id']:row for row in timeline}
        assert by_id[receipt['entryId']]['payload']['attachments'][0]['type']=='document'
        assert by_id[fail['entryId']]['payload']['attachments'][0]['reason']==media.FAILURES['unsupported']
    assert not any(row['entry_id']==receipt['entryId'] for row in pi.app.list_timeline(pi.accounts['b']))
    assert pi.app.timeline_attachment(pi.accounts['a'],receipt['attachmentId'])['body']==b'col\nvalue'
    links.revoke(pi.accounts['a']['account_id'],'pi-office','8801')
    assert media.append(svc,**{**args,'content_base64':None})['mediaReceipt']==receipt
    with pytest.raises((PermissionError,ValueError)):
        pi.app.timeline_attachment(pi.accounts['a'],receipt['attachmentId'])
    assert not any(row['entry_id']==receipt['entryId'] for row in pi.app.list_timeline(pi.accounts['a']))
    assert pair()['pairingRef']!=proof['pairingRef']
    with pytest.raises(PermissionError): media.append(svc,**{**args,'ordinal':2})


def test_actual_private_mcp_media_tools_preserve_identity_and_are_not_model_tools(pi):
    from pathlib import Path
    from starlette.testclient import TestClient
    from tests.test_pi_workspace import private_json
    from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
    svc=pi.service
    identities=McpIdentityTokenStore(svc.db_path)
    identity=identities.issue(user_subject='user-a',expected_principal_ref='user-a',label='workspace-pi:pi-office:pi-a')
    pi.spec.update(adapterVersion='0.1.8',requiredLevel='L3')
    private_json(pi.route_file,pi.manifest)
    with svc.workspace._connect() as db:
        db.execute("UPDATE workspace_host_instances SET adapter_version='0.1.8' WHERE instance_id='pi-office'")
    register(svc,'user-a','pi','pi-office',identity['token_id'])
    _,scope,*_=paired_query(pi,identity=identity,query_v2=True)
    profile=json.loads((Path(__file__).parents[1]/'schemas/agent-host/v1/test-vectors.json').read_text())['profiles'][0]['value']
    profile.update(implementation={'name':'pi','version':'0.1.8'},hostInstanceId='pi-office')
    metadata={'io.agentbridge/host-context':{'version':'1','agentHost':'pi','hostInstanceId':'pi-office','hostVersion':'0.1.8'},
              'io.agentbridge/host-profile':profile}
    config=validate_central_mcp_server_config(host='127.0.0.1',port=8790,public_base_url='http://testserver',tls_cert=None,tls_key=None)
    app=create_central_mcp_server(service=svc,identity_store=identities,config=config,auth_card_base_url='http://127.0.0.1:8780')
    with TestClient(app.streamable_http_app()) as client:
        def call(name,args=None,meta=metadata):
            response=client.post('/mcp',headers={'Authorization':'Bearer '+identity['token'],
                'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-06-18'},
                json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args or {},'_meta':meta}})
            assert response.status_code==200
            return response.json()['result']
        registered=call('agentbridge_host_register')
        assert registered.get('structuredContent',{}).get('acceptedLevel')=='L3',registered
        planes=call('agentbridge_server_profile')['structuredContent']['toolPlanes']
        for tool in ('agentbridge_host_telegram_media_source','agentbridge_host_telegram_media_append'):
            assert tool in planes['hostControl']['levels']['L3'] and tool not in planes['model']['toolNames']
        body=dict(agent_host='pi',endpoint_key=scope['endpoint_key'],source_message_id=1001,
            received_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        assert call('agentbridge_host_telegram_media_source',body,meta={})['isError']
        proof=call('agentbridge_host_telegram_media_source',body)['structuredContent']
        raw=b'%PDF-1.4\noriginal'
        args={**body,'pairing_ref':proof['pairingRef'],'ordinal':0,'file_name':'original.pdf','mime_type':'application/pdf',
            'byte_size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'content_base64':base64.b64encode(raw).decode()}
        receipt=call('agentbridge_host_telegram_media_append',args)['structuredContent']
        assert receipt['recorded'] and receipt['mediaReceipt']['state']=='stored'
        assert call('agentbridge_host_telegram_media_append',{**args,'content_base64':None})['structuredContent']==receipt
