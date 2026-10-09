"""Loopback-only MCP fixture with IPC fault controls; never installed in wheel."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time

import uvicorn
from agentbridge.core.central_service import CentralCapabilityService
import agentbridge.core.write_catalog as central_module
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config


class Worker:
    def __init__(self, session): self.session = session
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def restore_session_state(self, state): pass
    def capture_session_state(self): return {"cookies": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    home = Path(args.home)
    service = CentralCapabilityService(home=home, base_url='https://oa.invalid',
        smartlight_base_url='https://smartlight.invalid/smartlight', worker_factory=lambda session, adapter: Worker(session))
    def invoke(name, worker, arguments):
        with closing(sqlite3.connect(service.db_path)) as db, db:
            db.execute('INSERT INTO fixture_calls(owner,system,capability,arguments) VALUES (?,?,?,?)',
                       (worker.session['user_subject'],worker.session['system_id'],name,json.dumps(arguments,sort_keys=True)))
        if (home/'crash-read').exists() and name != 'fixture.write.boundary':
            (home/'crash-read').unlink()
            os._exit(73)
        return {'items':[], 'fixtureOwner':worker.session['user_subject'], 'fixtureArguments':arguments}
    for system, adapter in service._adapters_by_system.items():
        adapter.invoke_capability = invoke
        service._worker_factories_by_system[system] = lambda session, adapter: Worker(session)
        for user in ('alice','bob'):
            service.sessions.get_or_create(user_subject=user,system_id=system,expected_principal_ref=user)
    def unknown_write(adapter, worker, plan, *, enter_commit_boundary):
        enter_commit_boundary()
        invoke('fixture.write.boundary',worker,{})
        raise RuntimeError('synthetic readback failure after commit boundary')
    central_module.submit_leave_request = unknown_write
    with closing(sqlite3.connect(service.db_path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS fixture_calls(owner TEXT,system TEXT,capability TEXT,arguments TEXT)')
    store = McpIdentityTokenStore(service.db_path)
    from tests.authorization_fixtures import grant_permissions
    from agentbridge.core.user_grants import PERMISSIONS
    for user in ('alice', 'bob'):
        if service.user_grants.get(user) is None:
            grant_permissions(service.user_grants, user, PERMISSIONS)
    tokens_file = home/'fixture-tokens.json'
    if tokens_file.exists(): tokens=json.loads(tokens_file.read_text())
    else:
        tokens={user:store.issue(user_subject=user,expected_principal_ref=user,ttl_seconds=3600)['token'] for user in ('alice','bob')}
        tokens_file.write_text(json.dumps(tokens))
    listener=socket.socket()
    listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    listener.bind(('127.0.0.1',args.port))
    port=listener.getsockname()[1]
    config=validate_central_mcp_server_config(host='127.0.0.1',port=port,public_base_url=f'http://127.0.0.1:{port}',tls_cert=None,tls_key=None)
    mcp=create_central_mcp_server(service=service,identity_store=store,config=config,auth_card_base_url='http://127.0.0.1:8780')
    server=uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(),log_level='error'))
    thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True)
    thread.start()
    deadline=time.monotonic()+20
    while not server.started:
        if not thread.is_alive() or time.monotonic()>deadline: raise RuntimeError('fixture server failed to start')
        time.sleep(.02)
    print(json.dumps({'port':port,'tokens':tokens}),flush=True)
    for line in sys.stdin:
        try:
            message=json.loads(line)
            if message['action']=='activate':
                user=message['user']; system=message.get('system','oa')
                session=service.sessions.get_or_create(user_subject=user,system_id=system,expected_principal_ref=user)
                service.sessions.activate(session['session_id'],observed_principal_ref=user)
                service.session_states.save(session['session_id'],{'cookies':[]})
                with closing(sqlite3.connect(service.db_path)) as db:
                    challenges=db.execute("SELECT challenge_id FROM auth_challenges WHERE user_subject=? AND system_id=? AND state IN ('pending','processing')",(user,system)).fetchall()
                for (challenge,) in challenges:
                    if service.challenges.get(challenge)['state']=='pending':
                        csrf=service.challenges.issue_csrf(challenge)
                        service.challenges.claim(challenge,csrf_token=csrf,csrf_cookie=csrf)
                    service.challenges.complete(challenge,result={'sessionId':session['session_id']})
                result={'sessionId':session['session_id']}
            elif message['action']=='notification_endpoint':
                user=message['user']
                endpoint,_=service.tasks.ensure_endpoint(user_subject=user,token_id=store.verify(tokens[user])['token_id'],
                    agent_host='openclaw',endpoint_key='fixture-notify:'+user,client_type='telegram',
                    external_subject=user,conversation_ref='fixture-notify:'+user,capabilities=['timeline_message'])
                result={'endpointId':endpoint['endpoint_id']}
            elif message['action']=='arm_read_crash':
                (home/'crash-read').touch()
                result={}
            elif message['action']=='authorize_synthetic_write':
                user=message['user']
                session=service.sessions.find(user_subject=user,system_id='oa')
                spec=service.registry.get('oa.leave.submit')
                operation,_=service.operations.create(user_subject=user,capability_name='oa.leave.submit.prepare',
                    capability_version='1',input_summary={})
                authorization=service.write_authorizations.create(
                    user_subject=user,system_id='oa',session_id=session['session_id'],
                    capability_name=spec.name,capability_version=spec.version,
                    prepare_operation_id=operation['operation_id'],
                    plan={'user_subject':user,'business_intent':'submit_leave_request',
                          'session_binding':{k:session[k] for k in ('session_id','expected_principal_ref','downstream_principal_ref','last_verified_at')}},
                    summary={'title':'Synthetic write','system':'fixture','fields':[]},
                    card_base_url='http://127.0.0.1:8780')
                interaction=service._execution_authorization_interaction(authorization)
                service.observe_host_task(user_subject=user,task_id=message['taskId'],interaction_ids=[interaction['interactionId']])
                csrf=service.write_authorizations.issue_csrf(authorization['authorization_id'])
                service.write_authorizations.decide(authorization['authorization_id'],decision='approve',csrf_token=csrf,csrf_cookie=csrf)
                result={'interactionId':interaction['interactionId'],'taskId':message['taskId']}
            elif message['action']=='query':
                with closing(sqlite3.connect(service.db_path)) as db, db:
                    db.row_factory=sqlite3.Row
                    result=[dict(row) for row in db.execute(message['sql'],message.get('parameters',[]))]
            else: raise ValueError('unknown fixture control')
            print(json.dumps({'ok':True,'result':result}),flush=True)
        except Exception as error:
            print(json.dumps({'ok':False,'error':repr(error)}),flush=True)
    server.should_exit=True
    thread.join(10)


if __name__=='__main__': main()
