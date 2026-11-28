"""A lost report ACK can read its original fact, but cannot acquire execution rights."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentbridge.core import artifact_report_receipts as receipts
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.tasks import TaskHubStore
from tests.support.latency_acceptance import OWNER
from tests.test_workspace_query_projection import openclaw, register  # noqa: F401


def original(case, *, strict=True):
    fixture,token,endpoint=case
    service=fixture.service
    task,_=service.tasks.ensure_task(user_subject=OWNER,agent_host='openclaw',host_task_key='original-artifact',
        origin_endpoint_id=endpoint['endpoint_id'],active_conversation_ref=endpoint['conversation_ref'],title='合成文件')
    service.host_contract.acquire_coordinator_lease(task_id=task['task_id'],user_subject=OWNER,
        host_instance_id='openclaw-gateway',agent_host='openclaw',lease_seconds=60)
    artifact,_=service.tasks.link_artifact(task_id=task['task_id'],user_subject=OWNER,artifact={
        'artifact_type':'certificate_scan','source_ref':'synthetic-original','filename':'original.pdf',
        'content_type':'application/pdf','byte_size':16,'download_url':'https://fixture.invalid/download/original/file',
        'expires_at':'2099-01-01T00:00:00+00:00'})
    files=[{'artifact_id':artifact['artifact_id'],'state':'attachment_sent','attempt_count':1,'error_code':None}]
    report=dict(user_subject=OWNER,agent_host='openclaw',task_id=task['task_id'],delivery_ref='tool-result:original',channel='telegram',files=files)
    if strict:report['receipt_context']={'hostInstanceId':'openclaw-gateway','tokenId':token}
    history=dict(subject=OWNER,host='openclaw',instance='openclaw-gateway',token=token,
        **{key:report[key] for key in ('task_id','delivery_ref','channel','files')})
    return service,report,history


def expire(service, task):
    with service.tasks._connect() as db:
        db.execute('UPDATE agent_task_coordinator_leases SET expires_at=? WHERE task_id=?',
            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),task))


def counts(service):
    with service.tasks._connect() as db:
        return {name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in (
            'task_events','artifact_report_receipt_custody','notification_outbox','operations','agent_host_dispatches')}


def test_committed_report_lost_ack_recovers_after_expiry_without_lease_or_writes(openclaw):
    service,args,history=original(openclaw)
    sent=service.report_host_artifact_delivery(**args);expire(service,args['task_id']);before=counts(service)
    restarted=SimpleNamespace(tasks=TaskHubStore(service.db_path,maintain_on_startup=False))
    with (patch.object(service,'invoke',side_effect=AssertionError('No business operation')),
          patch.object(service.host_contract,'acquire_coordinator_lease',side_effect=AssertionError('No new execution lease'))):
        result=receipts.historical(restarted,**history)
    assert result['eventId']==sent['eventId'] and result['delivery']==sent['delivery'] and result['historyReceipt']
    assert result['task']=={'taskId':args['task_id']}  # Never expose a newer mutable task summary.
    assert counts(service)==before
    with service.tasks._connect() as db:
        db.execute("UPDATE agent_task_coordinator_leases SET state='released' WHERE task_id=?",(args['task_id'],))
        db.execute("UPDATE client_endpoints SET state='inactive' WHERE endpoint_id=?",(openclaw[2]['endpoint_id'],))
    assert receipts.historical(service,**history)['eventId']==sent['eventId']


@pytest.mark.parametrize('change',[
    {'subject':'foreign'},{'host':'pi'},{'instance':'foreign'},{'token':'new-token'},
    {'delivery_ref':'tool-result:new'},{'channel':'webchat'},{'task_id':'foreign-task'},
])
def test_changed_original_identity_has_no_history_shortcut(openclaw,change):
    service,args,history=original(openclaw);service.report_host_artifact_delivery(**args);expire(service,args['task_id'])
    assert receipts.historical(service,**{**history,**change}) is None


@pytest.mark.parametrize('change',[{'state':'failed'},{'attempt_count':2},{'error_code':'TELEGRAM_DELIVERY_UNKNOWN'},
                                  {'artifact_id':'another-original'}])
def test_each_reported_item_is_immutable_for_history_recovery(openclaw,change):
    service,args,history=original(openclaw);service.report_host_artifact_delivery(**args);expire(service,args['task_id'])
    changed=[{**history['files'][0],**change}]
    assert receipts.historical(service,**{**history,'files':changed}) is None


@pytest.mark.parametrize('mutation',[
    "UPDATE agent_task_coordinator_leases SET version=version+1",
    "UPDATE agent_task_coordinator_leases SET host_instance_id='other'",
    "UPDATE agent_task_coordinator_leases SET lease_id='other'",
    "UPDATE client_endpoints SET token_id='other' WHERE client_type='telegram'",
    "UPDATE client_endpoints SET route_json='{}' WHERE client_type='telegram'",
    "UPDATE task_events SET payload_json='{}' WHERE event_type='task.artifact.delivery'",
    "DELETE FROM artifact_report_receipt_custody",
])
def test_replacement_identity_or_cleaned_evidence_is_never_reconstructed(openclaw,mutation):
    service,args,history=original(openclaw);service.report_host_artifact_delivery(**args)
    with service.tasks._connect() as db:db.execute(mutation)
    before=counts(service);assert receipts.historical(service,**history) is None;assert counts(service)==before


def test_legacy_reports_cannot_be_retroactively_assigned_new_proof(openclaw):
    service,args,history=original(openclaw,strict=False);service.report_host_artifact_delivery(**args)
    service.report_host_artifact_delivery(**{**args,'receipt_context':{'hostInstanceId':history['instance'],'tokenId':history['token']}})
    assert receipts.historical(service,**history) is None
    assert counts(service)['artifact_report_receipt_custody']==0


def test_report_and_original_custody_are_one_transaction(openclaw):
    service,args,history=original(openclaw)
    with patch.object(receipts,'persist',side_effect=OSError('synthetic disk failure')):
        with pytest.raises(OSError):service.report_host_artifact_delivery(**args)
    assert receipts.historical(service,**history) is None
    with service.tasks._connect() as db:
        assert not db.execute("SELECT 1 FROM task_events WHERE event_type='task.artifact.delivery'").fetchone()


@pytest.mark.parametrize('workspace_origin',[False,True])
def test_original_companion_notification_selects_its_actual_target_endpoint(openclaw,workspace_origin):
    fixture,token,origin=openclaw;service=fixture.service
    if workspace_origin:
        target=origin
        with service.tasks._connect() as db:
            origin=dict(db.execute('SELECT * FROM client_endpoints WHERE endpoint_id=?',(fixture.account['endpoint_id'],)).fetchone())
    else:
        target,_=service.tasks.ensure_endpoint(user_subject=OWNER,token_id=token,agent_host='openclaw',
            endpoint_key='telegram:default:502',client_type='telegram',external_subject='502',account_id='default',
            conversation_ref='agent:main:telegram:direct:502',route={'channel':'telegram','to':'502','accountId':'default'},
            capabilities=['timeline_message','trusted_interaction'])
    service,args,history=original((fixture,token,origin))
    with service.tasks._connect() as db:
        delivery=db.execute("SELECT delivery_id FROM notification_outbox WHERE task_id=? AND endpoint_id=? AND json_extract(payload_json,'$.eventType')='task.artifact.ready'",
                            (args['task_id'],target['endpoint_id'])).fetchone()
    assert delivery is not None
    args['delivery_ref']=history['delivery_ref']=delivery[0]
    first=service.report_host_artifact_delivery(**args);expire(service,args['task_id'])
    assert receipts.historical(service,**history)['eventId']==first['eventId']
    with service.tasks._connect() as db:
        proof=json.loads(db.execute('SELECT proof_json FROM artifact_report_receipt_custody WHERE event_id=?',(first['eventId'],)).fetchone()[0])
    assert proof['origin']['endpoint_id']==origin['endpoint_id'] and proof['target']['endpoint_id']==target['endpoint_id']


def test_actual_private_mcp_history_ack_does_not_bypass_original_report_lease(openclaw):
    from starlette.testclient import TestClient
    from agentbridge.mcp.central import create_central_mcp_server,validate_central_mcp_server_config
    fixture,_,endpoint=openclaw;service=fixture.service
    identities=McpIdentityTokenStore(service.db_path)
    identity=identities.issue(user_subject=OWNER,expected_principal_ref=OWNER,label='synthetic-report-recovery')
    register(service,OWNER,'openclaw','openclaw-gateway',identity['token_id'])
    with service.tasks._connect() as db:
        db.execute('UPDATE client_endpoints SET token_id=? WHERE endpoint_id=?',(identity['token_id'],endpoint['endpoint_id']))
    service,args,history=original((fixture,identity['token_id'],endpoint))
    profile=json.loads((Path(__file__).parents[1]/'schemas/agent-host/v1/test-vectors.json').read_text())['profiles'][0]['value']
    profile.update(implementation={'name':'openclaw','version':'0.4.134'},hostInstanceId='openclaw-gateway')
    metadata={'io.agentbridge/host-context':{'version':'1','agentHost':'openclaw','hostInstanceId':'openclaw-gateway','hostVersion':'0.4.134'},
              'io.agentbridge/host-profile':profile}
    config=validate_central_mcp_server_config(host='127.0.0.1',port=8790,public_base_url='http://testserver',tls_cert=None,tls_key=None)
    app=create_central_mcp_server(service=service,identity_store=identities,config=config,auth_card_base_url='http://127.0.0.1:8780')
    with TestClient(app.streamable_http_app()) as client:
        def call(name,body,meta=metadata):
            reply=client.post('/mcp',headers={'Authorization':'Bearer '+identity['token'],'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-06-18'},
                json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':body,'_meta':meta}})
            assert reply.status_code==200
            return reply.json()['result']
        assert call('agentbridge_host_register',{})['structuredContent']['acceptedLevel']=='L3'
        body={key:value for key,value in args.items() if key not in ('user_subject','receipt_context')};body['receipt_v2']=True
        first=call('agentbridge_host_artifact_delivery_report',body)['structuredContent'];expire(service,args['task_id']);before=counts(service)
        with patch.object(service.host_contract,'acquire_coordinator_lease',side_effect=AssertionError('must not acquire')):
            again=call('agentbridge_host_artifact_delivery_report',body)['structuredContent']
        assert again['historyReceipt'] and again['eventId']==first['eventId'];assert counts(service)==before
        for extra in ({'delivery_ref':'tool-result:never-received'},{'files':[{**body['files'][0],'attempt_count':2}]},{'receipt_v2':False}):
            assert call('agentbridge_host_artifact_delivery_report',{**body,**extra})['isError']
        assert call('agentbridge_host_artifact_delivery_report',body,meta={})['isError']
        assert counts(service)==before


def test_openclaw_artifact_outcome_ack_accepts_sent_and_terminal_unknown(openclaw):
    fixture,token,origin=openclaw;service=fixture.service
    with service.tasks._connect() as db:
        target,_=service.tasks.ensure_endpoint(user_subject=OWNER,token_id=token,agent_host='openclaw',
            endpoint_key='telegram:default:503',client_type='telegram',external_subject='503',account_id='default',
            conversation_ref='agent:main:telegram:direct:503',route={'channel':'telegram','to':'503','accountId':'default'},
            capabilities=['timeline_message','trusted_interaction'])
    service,args,_=original((fixture,token,origin))
    claimed=service.claim_host_notifications(user_subject=OWNER,agent_host='openclaw',endpoint_key=target['endpoint_key'],
        host_instance_id='openclaw-gateway',limit=10,lease_seconds=60)
    artifact=[item for item in claimed['notifications'] if item.get('deliveryMode')=='artifact']
    assert len(artifact)==1
    ack=service.acknowledge_host_notification(user_subject=OWNER,agent_host='openclaw',endpoint_key=target['endpoint_key'],
        delivery_id=artifact[0]['deliveryId'],succeeded=True,attempt_count=artifact[0]['attemptCount'],
        artifact_outcome='attachment_sent',host_instance_id='openclaw-gateway')
    assert ack['delivery']['state']=='acknowledged'

    service.tasks.link_artifact(task_id=args['task_id'],user_subject=OWNER,artifact={
        'artifact_type':'certificate_scan','source_ref':'synthetic-original-unknown','filename':'unknown.pdf',
        'content_type':'application/pdf','byte_size':16,'download_url':'https://fixture.invalid/download/unknown/file',
        'expires_at':'2099-01-01T00:00:00+00:00'})
    claimed2=service.claim_host_notifications(user_subject=OWNER,agent_host='openclaw',endpoint_key=target['endpoint_key'],
        host_instance_id='openclaw-gateway',limit=10,lease_seconds=60)
    artifact2=[item for item in claimed2['notifications'] if item.get('deliveryMode')=='artifact']
    assert len(artifact2)==1
    ack2=service.acknowledge_host_notification(user_subject=OWNER,agent_host='openclaw',endpoint_key=target['endpoint_key'],
        delivery_id=artifact2[0]['deliveryId'],succeeded=False,attempt_count=artifact2[0]['attemptCount'],
        artifact_outcome='delivery_unknown',host_instance_id='openclaw-gateway')
    assert ack2['delivery']['state']=='failed'
    again=service.claim_host_notifications(user_subject=OWNER,agent_host='openclaw',endpoint_key=target['endpoint_key'],
        host_instance_id='openclaw-gateway',limit=10,lease_seconds=60)
    assert not [item for item in again['notifications'] if item.get('deliveryId')==artifact2[0]['deliveryId']]
