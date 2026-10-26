from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.workspace.credential_health import credential_health, preparation_health, record_delivery
from tests.test_workspace_current import app_account, _task


NOW = datetime(2026,10,26,tzinfo=timezone.utc)


def register(app, subject, *, ttl=86400*30, state='active', host='openclaw', instance='host'):
    store = McpIdentityTokenStore(app.service.tasks.db_path, clock=lambda:NOW)
    token = store.issue(user_subject=subject,expected_principal_ref=subject,ttl_seconds=max(300,ttl))
    with app.service.tasks._connect() as db:
        db.execute('UPDATE mcp_identity_tokens SET state=?,expires_at=? WHERE token_id=?',
                   (state,(NOW+timedelta(seconds=ttl)).isoformat(),token['token_id']))
        db.execute('INSERT INTO agent_host_registrations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (str(uuid4()),subject,token['token_id'],instance,host,'test','{}','L0','test',NOW.isoformat(),NOW.isoformat()))
    return store,token


@pytest.mark.parametrize('ttl,state,expected,blocked',[(86400*30,'active','valid',False),
    (86400*7,'active','expiring',False),(86400,'active','expiring',False),(0,'active','unavailable',True),
    (-1,'active','unavailable',True),(86400*30,'revoked','unavailable',True)])
def test_thresholds_and_revocation(app_account, ttl,state,expected,blocked):
    app,account=app_account
    register(app,account['user_subject'],ttl=ttl,state=state)
    health=credential_health(app.service,account['user_subject'],now=NOW)
    assert health['state']==expected and health['blocked']==blocked
    assert not any(x in json.dumps(health) for x in ['token_id','token_hash','expected_principal_ref'])


def test_subject_host_isolation_and_renewal(app_account):
    app,account=app_account
    register(app,'other',ttl=86400*30)
    register(app,account['user_subject'],host='reference-host',instance='reference')
    store,token=register(app,account['user_subject'],ttl=-1)
    assert credential_health(app.service,account['user_subject'],now=NOW)['blocked']
    store.renew(token['token_id'],new_expires_at=(NOW+timedelta(days=30)).isoformat(),expected_revision=0,
                request_id='renew',actor='admin',reason='test')
    assert credential_health(app.service,account['user_subject'],now=NOW)['state']=='valid'
    assert store.verify(token['token'])


def command_task(app,account):
    task=_task(app.service,account)
    summary={'command':{'state':'pending','kind':'oa_pending_query','commandKey':str(uuid4()),'requestedAt':NOW.isoformat()}}
    with app.service.tasks._connect() as db:
        db.execute('UPDATE agent_tasks SET summary_json=? WHERE task_id=?',(json.dumps(summary),task['task_id']))
    return app.service.tasks.get_task(task['task_id'],user_subject=account['user_subject'])


def test_delivery_failure_persists_without_mutating_intent_or_task_status(app_account):
    app,account=app_account
    task=command_task(app,account)
    command={'taskId':task['task_id'],'commandKey':task['summary']['command']['commandKey']}
    record_delivery(app.service,account,command,'FORBIDDEN')
    assert preparation_health(app.service,task,now=NOW)['state']=='authentication_required'
    assert app.service.tasks.get_task(task['task_id'],user_subject=account['user_subject'])['summary']==task['summary']
    record_delivery(app.service,{'user_subject':'other'},command,'accepted')
    assert preparation_health(app.service,task,now=NOW)['state']=='authentication_required'
    record_delivery(app.service,account,command,'accepted')
    assert preparation_health(app.service,task,now=NOW) is None
    assert preparation_health(app.service,task,now=NOW+timedelta(seconds=30))['state']=='delayed'
    assert preparation_health(app.service,task,now=NOW+timedelta(minutes=10))['state']=='expired'
    task['summary']['command']['state']='succeeded'
    assert preparation_health(app.service,task,now=NOW) is None


def test_mixed_hosts_do_not_claim_all_authentication_blocked(app_account):
    app,account=app_account
    register(app,account['user_subject'],ttl=-1)
    register(app,account['user_subject'],instance='second')
    assert credential_health(app.service,account['user_subject'],now=NOW)['state']=='partial'
    assert not credential_health(app.service,account['user_subject'],now=NOW)['blocked']


def test_worker_records_gateway_auth_failure_but_does_not_execute(app_account):
    from agentbridge.workspace.gateway import GatewayRequestError
    from unittest.mock import patch
    app,account=app_account
    task=command_task(app,account)
    command={'taskId':task['task_id'],'commandKey':task['summary']['command']['commandKey']}
    class Gateway:
        def call(self,*args,**kwargs):
            raise GatewayRequestError('FORBIDDEN','Synthetic authentication rejection with private content')
    app.gateway=Gateway()
    with patch.object(app.store,'issue_gateway_grant',return_value={'session_key':'s','endpoint_key':'e','grant':'secret'}):
        app._wake_preparation(account,command)
    assert preparation_health(app.service,task,now=NOW)['state']=='authentication_required'
    with app.service.tasks._connect() as db:
        rows=db.execute("SELECT payload_json FROM task_events WHERE task_id=? AND event_type='task.prepare.delivery'",(task['task_id'],)).fetchall()
        assert [json.loads(r[0]) for r in rows]==[{'code':'FORBIDDEN'}]
        assert db.execute('SELECT count(*) FROM task_operations WHERE task_id=?',(task['task_id'],)).fetchone()[0]==0


def test_projection_does_not_mask_terminal_or_running_business(app_account):
    app,account=app_account
    register(app,account['user_subject'],ttl=-1)
    task=command_task(app,account)
    for status in ['failed','succeeded','outcome_unknown','canceled']:
        assert preparation_health(app.service,{**task,'status':status},now=NOW) is None
    assert preparation_health(app.service,{**task,'current_operation_id':'already-started'},now=NOW+timedelta(minutes=15)) is None
    task['summary']['command']['state']='running'
    assert preparation_health(app.service,task,now=NOW) is None
