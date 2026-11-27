"""Cross-endpoint projection uses only the admitted Workspace request and its original OA read."""
from datetime import timedelta
import json
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

import pytest

from agentbridge.core import actionable_results as actions, quick_queries
from agentbridge.core import workspace_query_delivery as queries
from agentbridge.core.endpoint_instances import instance_key
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.telegram_result_views import ResultViewService
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract, OWNER
from tests.test_pi_workspace import pi  # noqa: F401
from tests.test_pi_workspace_result_views import paired_query


def register(service, subject, host, instance, token, version=None):
    if version is None:
        version = queries.VERSIONS[host]
    with service.tasks._connect() as db:
        db.execute("INSERT OR REPLACE INTO agent_host_compatibility VALUES (?,?,'L3','synthetic-contract','test','now')",
                   (host, version))
    vectors = Path(__file__).resolve().parents[1] / 'schemas/agent-host/v1/test-vectors.json'
    profile = json.loads(vectors.read_text())["profiles"][0]["value"]
    profile.update(implementation={"name": host, "version": version}, hostInstanceId=instance)
    assert service.host_contract.negotiate(user_subject=subject, token_id=token, profile=profile)['acceptedLevel'] == 'L3'


@pytest.fixture
def openclaw(tmp_path):
    fixture = LatencyAcceptanceFixture(tmp_path, seed=False, latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
    try:
        service = fixture.service
        service.skills.save("user:"+OWNER, {"oa-pending":{"profiles":["single","batch"]}},
                            expected_revision=0, actor="test", reason="synthetic user assignment")
        token = McpIdentityTokenStore(service.db_path).issue(user_subject=OWNER, expected_principal_ref=OWNER,
                                                             label='synthetic-openclaw')['token_id']
        register(service, OWNER, 'openclaw', 'openclaw-gateway', token)
        endpoint, _ = service.tasks.ensure_endpoint(user_subject=OWNER, token_id=token, agent_host='openclaw',
            endpoint_key='telegram:default:501', client_type='telegram', external_subject='501', account_id='default',
            conversation_ref='agent:main:telegram:direct:501', route={'channel':'telegram','to':'501','accountId':'default'},
            capabilities=['timeline_message','trusted_interaction'])
        yield fixture, token, endpoint
    finally:
        fixture.close()


def claim(service, subject, host, instance, token, endpoint):
    for receipt in range(1, 5):
        reply = service.claim_host_notifications(user_subject=subject, agent_host=host, host_instance_id=instance,
            endpoint_key=endpoint, include_workspace_chat=True, query_token_id=token)
        assert reply['endpoint']['agentHost'] == host
        cards = [item for item in reply['notifications'] if item['deliveryMode'] == queries.MODE]
        texts = [item for item in reply['notifications'] if item['deliveryMode'] == 'workspace_text_v2']
        for item in texts:
            args = dict(subject=subject, host=host, instance=instance, token_id=token, endpoint_key=endpoint,
                        pairing_ref=item['pairingRef'], delivery_id=item['deliveryId'], attempt_count=item['attemptCount'])
            assert queries.handle(service, action='issue', **args)['publicText']
            assert queries.handle(service, action='begin', **args)['allowed']
            assert queries.handle(service, action='ack', outcome='sent', receipt=receipt, **args)['recorded']
        if cards or not texts:
            return cards
    raise AssertionError("Unexpected number of Workspace messages")


@pytest.mark.parametrize(("version", "supported"), [
    ("0.4.134", True), ("0.4.135", True), ("0.4.133", False), ("0.4.136", False),
])
def test_openclaw_query_protocol_requires_exact_supported_version(openclaw, version, supported):
    fixture, token, _ = openclaw
    instance = "openclaw-gateway"
    register(fixture.service, OWNER, "openclaw", instance, token, version)
    with fixture.service.tasks._connect() as db:
        assert queries.registered(db, OWNER, "openclaw", instance_key("openclaw", instance), token) is supported


@pytest.mark.parametrize('button', [False, True])
def test_shortcut_projects_without_a_model_and_binds_one_send(openclaw, button):
    fixture, token, endpoint = openclaw
    svc = fixture.service
    body = {'requestKey':uuid4().hex}
    if not button:
        body['userMessage'] = '查询我的 OA 待办'
    result = quick_queries.queue_query(svc, fixture.account, body)
    task = result['taskId']
    response = actions.execute_preparation(svc, OWNER, task, result['commandKey'], runtime_context={
        'endpointId':fixture.account['endpoint_id'], 'hostType':'openclaw', 'hostInstanceId':'openclaw-gateway'})
    assert response['status'] == 'succeeded'
    with patch.object(svc, 'invoke', side_effect=AssertionError('Projection must not run OA')):
        item, = claim(svc, OWNER, 'openclaw', 'openclaw-gateway', token, endpoint['endpoint_key'])
        scope = dict(subject=OWNER, host='openclaw', instance='openclaw-gateway', token_id=token, endpoint_key=endpoint['endpoint_key'],
                     pairing_ref=item['pairingRef'], delivery_id=item['deliveryId'], attempt_count=item['attemptCount'])
        def control(action, **kwargs):
            return queries.handle(svc, **{**scope, **kwargs}, action=action)
        assert control('begin') == {'allowed':False}
        assert control('issue', pairing_ref='0'*64) == {'available':False}
        issued = control('issue')
        assert issued['publicText'] == '本次 OA 查询共有 1 项待办。'
        assert issued['resultView']['sourceTaskId'] == task
        card = issued['resultView']['presentation']['url'].rsplit('/',1)[-1]
        assert ResultViewService(svc).read(card)['selectionAvailable']
        assert control('begin') == {'allowed':True}
        assert control('begin') == {'allowed':False}
        with svc.tasks._connect() as db:
            db.execute('UPDATE workspace_query_deliveries SET due=?', ((actions._now()-timedelta(hours=1)).isoformat(),))
        assert not claim(svc, OWNER,'openclaw','openclaw-gateway',token,endpoint['endpoint_key'])
        assert control('ack', outcome='sent', receipt=7) == {'recorded':True}
        assert control('ack', outcome='sent', receipt=7) == {'recorded':True}
        assert control('ack', outcome='sent', receipt=8) == {'recorded':False}
        with svc.tasks._connect() as db:
            assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM agent_host_dispatches').fetchone()[0] == 0


def test_normal_pi_uses_accepted_run_and_original_pair(pi):
    service = pi.service
    identity = McpIdentityTokenStore(service.db_path).issue(user_subject='user-a', expected_principal_ref='user-a',
                                                           label='workspace-pi:pi-office:pi-a')
    register(service, 'user-a','pi','pi-office',identity['token_id'])
    legacy, scope, task, _, _, links, pair = paired_query(pi, identity=identity, query_v2=True)
    assert not legacy
    item, = claim(service,'user-a','pi','pi-office',identity['token_id'],scope['endpoint_key'])
    scope.update(host='pi',delivery_id=item['deliveryId'],attempt_count=1)
    assert item['sourceTaskId'] == task and item['pairingRef'] == scope['pairing_ref']
    issued = queries.handle(service, **scope, action='issue')
    assert issued['resultView']['sourceTaskId'] == task
    links.revoke(pi.accounts['a']['account_id'],'pi-office','8801')
    assert pair()['pairingRef'] != scope['pairing_ref']
    assert queries.handle(service, **scope, action='begin') == {'allowed':False}
    with pytest.raises(actions.ActionableResultError):
        ResultViewService(service).read(issued['resultView']['presentation']['url'].rsplit('/',1)[-1])


def test_shortcut_login_projects_only_its_verified_continuation(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    session = service.sessions.find(user_subject=OWNER, system_id='oa')
    service.sessions.mark_expired(session['session_id'], 'synthetic expiry')
    query = quick_queries.queue_query(service, fixture.account, {'requestKey':uuid4().hex, 'userMessage':'查询我的 OA 待办'})
    response = actions.execute_preparation(service, OWNER, query['taskId'], query['commandKey'], runtime_context={
        'endpointId':fixture.account['endpoint_id'], 'hostType':'openclaw', 'hostInstanceId':'openclaw-gateway'})
    card = service.interactions.get(response['interaction']['interactionId'], user_subject=OWNER)
    assert not claim(service, OWNER, 'openclaw', 'openclaw-gateway', token, endpoint['endpoint_key'])
    challenge = service._credential_challenge_id(card)
    csrf = service.challenges.issue_csrf(challenge)
    service.challenges.claim(challenge, csrf_token=csrf, csrf_cookie=csrf)
    service.challenges.complete(challenge, result={'principal':OWNER})
    service.sessions.activate(card['session_id'], observed_principal_ref=OWNER)
    resumed = service.resume_interaction(user_subject=OWNER, interaction_id=card['interaction_id'])
    assert resumed['status'] == 'succeeded'
    item, = claim(service, OWNER, 'openclaw','openclaw-gateway',token,endpoint['endpoint_key'])
    scope = dict(subject=OWNER,host='openclaw',instance='openclaw-gateway',token_id=token,endpoint_key=endpoint['endpoint_key'],
                 pairing_ref=item['pairingRef'],delivery_id=item['deliveryId'],attempt_count=1)
    assert queries.handle(service, action='issue', **scope)['resultView']['sourceTaskId'] == query['taskId']
    with service.tasks._connect() as db:
        assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 2
        db.execute("UPDATE operations SET idempotency_key='unrelated-read' WHERE status='succeeded'")
    assert queries.handle(service, action='begin', **scope) == {'allowed':False}


@pytest.mark.parametrize(('status','message'), [
    ('failed','本次 OA 待办查询未完成，请在工作台查看原请求。'),
    ('canceled','本次 OA 待办查询已停止。'),
    ('outcome_unknown','OA 待办查询结果尚未确认，请在工作台查看原请求。'),
])
def test_closed_shortcut_without_operation_does_not_invent_a_result(openclaw,status,message):
    fixture, token, endpoint = openclaw
    service = fixture.service
    result = quick_queries.queue_query(service,fixture.account,{'requestKey':uuid4().hex})
    task = dict(task_id=result['taskId'], user_subject=OWNER)
    if status == 'canceled':
        service.tasks.cancel_task(**task,reason='synthetic stop')
    elif status == 'outcome_unknown':
        service.tasks.mark_task_outcome_unknown(**task,error_code='SYNTHETIC_UNKNOWN')
    else:
        service.tasks.fail_task(**task,error_code='SYNTHETIC_FAILURE',message='private diagnostic')
    item, = claim(service,OWNER,'openclaw','openclaw-gateway',token,endpoint['endpoint_key'])
    scope = dict(subject=OWNER,instance='openclaw-gateway',host='openclaw',token_id=token,endpoint_key=endpoint['endpoint_key'],
                 pairing_ref=item['pairingRef'],delivery_id=item['deliveryId'],attempt_count=item['attemptCount'])
    issued = queries.handle(service,action='issue',**scope)
    assert issued['publicText'] == message and 'resultView' not in issued
    with service.tasks._connect() as db:
        assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0


def test_fast_read_before_gateway_acceptance_projects_once(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    dispatch, _ = service.workspace.accept_chat_request(account_id=fixture.account['account_id'],
        user_subject=OWNER, message='列出我的 OA 待办', idempotency_key=uuid4().hex, attachments=[],
        tasks=service.tasks, attachment_store=service.timeline_attachments, media_base_url='https://fixture.invalid')
    leased = service.workspace.claim_next_host_dispatch(account_id=fixture.account['account_id'],claim_owner='test')
    run = uuid4().hex
    task, _ = service.tasks.ensure_task(user_subject=OWNER,agent_host='openclaw',host_task_key=run,
        origin_endpoint_id=dispatch['origin_endpoint_id'], active_conversation_ref=dispatch['conversation_ref'],title='合成查询')
    op, _ = service.operations.create(user_subject=OWNER,capability_name=queries.CAPABILITY,capability_version='1',input_summary={})
    completed = service.operations.mark_succeeded(op['operation_id'],{'items':[],'count':0,'coverage':{'status':'complete'}})
    service.tasks.link_operation(task_id=task['task_id'],user_subject=OWNER,operation=completed)
    assert not claim(service,OWNER,'openclaw','openclaw-gateway',token,endpoint['endpoint_key'])
    service.workspace.mark_host_dispatch_accepted(dispatch['dispatch_id'],claim_token=leased['claim_token'],run_id=run)
    item, = claim(service,OWNER,'openclaw','openclaw-gateway',token,endpoint['endpoint_key'])
    assert item['sourceTaskId'] == task['task_id']
    scope = dict(subject=OWNER, instance='openclaw-gateway', host='openclaw', token_id=token,
                 endpoint_key=endpoint['endpoint_key'], pairing_ref=item['pairingRef'],
                 delivery_id=item['deliveryId'], attempt_count=item['attemptCount'])
    value = queries.handle(service,action='issue',**scope)
    assert value['publicText'] == '本次 OA 查询没有待办。' and 'resultView' not in value
    with service.tasks._connect() as db:
        assert db.execute('SELECT count(*) FROM workspace_query_deliveries').fetchone()[0] == 1
