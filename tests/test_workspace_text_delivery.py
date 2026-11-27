"""Original Workspace messages are frozen into independently receipted Telegram parts."""
from datetime import timedelta
from uuid import uuid4

import pytest

from agentbridge.core import actionable_results as actions
from agentbridge.core import workspace_query_delivery as delivery
from agentbridge.core import workspace_text_delivery as text_delivery
from tests.support.latency_acceptance import OWNER
from tests.test_workspace_query_projection import openclaw  # noqa: F401


def admit(fixture):
    service = fixture.service
    dispatch, _ = service.workspace.accept_chat_request(account_id=fixture.account['account_id'], user_subject=OWNER,
        message='请列出我的待办记录', idempotency_key=uuid4().hex, attachments=[], tasks=service.tasks,
        attachment_store=service.timeline_attachments, media_base_url='https://fixture.invalid')
    claim = service.workspace.claim_next_host_dispatch(account_id=fixture.account['account_id'], claim_owner='fixture')
    service.workspace.mark_host_dispatch_accepted(dispatch['dispatch_id'], claim_token=claim['claim_token'], run_id=dispatch['idempotency_key'])
    return dispatch


def text_claim(service, endpoint, token):
    return text_delivery.claim(service, OWNER,'openclaw','openclaw-gateway',endpoint['endpoint_key'],token,10,30)


def control(service, endpoint, token, item, action, **kwargs):
    return delivery.handle(service, subject=OWNER, host='openclaw', instance='openclaw-gateway', token_id=token,
        endpoint_key=endpoint['endpoint_key'], pairing_ref=item['pairingRef'], delivery_id=item['deliveryId'],
        attempt_count=item['attemptCount'], action=action, **kwargs)


@pytest.mark.parametrize('length',[49_999,50_000,50_001])
def test_long_answer_retains_character_boundaries_and_order(openclaw,length):
    fixture, token, endpoint = openclaw
    service = fixture.service
    dispatch = admit(fixture)
    original = ('中文😀< & > *\n' * 5001)[:length-1]+'尾'
    assert len(original) == length
    service.tasks.append_workspace_response(user_subject=OWNER, source_endpoint_id=dispatch['origin_endpoint_id'],
        message_key='workspace:assistant:'+dispatch['idempotency_key'], text=original)
    question, = text_claim(service,endpoint,token)
    assert '提问' in control(service,endpoint,token,question,'issue')['publicText']
    assert control(service,endpoint,token,question,'begin')['allowed']
    assert not text_claim(service,endpoint,token)
    assert control(service,endpoint,token,question,'ack',outcome='sent',receipt=1)['recorded']
    parts = []
    while items := text_claim(service,endpoint,token):
        item, = items
        assert item['partIndex'] == len(parts)+1
        contents = control(service,endpoint,token,item,'issue')['publicText']
        prefix, part = contents.split('\n',1)
        assert prefix.startswith('工作台回复') and part
        assert control(service,endpoint,token,item,'begin')['allowed']
        assert not control(service,endpoint,token,item,'begin')['allowed']
        assert control(service,endpoint,token,item,'ack',outcome='sent',receipt=2+len(parts))['recorded']
        parts.append(part)
    assert ''.join(parts) == original
    with service.tasks._connect() as db:
        assert not db.execute("SELECT 1 FROM notification_outbox WHERE payload_type='timeline_message' AND endpoint_id=? AND state='pending'",
                              (endpoint['endpoint_id'],)).fetchone()
        assert db.execute("SELECT count(*) FROM workspace_text_deliveries WHERE state='sent'").fetchone()[0] == 1+len(parts)


def test_text_only_retries_after_proven_refusal_and_settles_original_attempt(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    dispatch = admit(fixture)
    item, = text_claim(service,endpoint,token)
    assert control(service,endpoint,token,item,'issue')['available']
    assert control(service,endpoint,token,item,'begin')['allowed']
    assert control(service,endpoint,token,item,'ack',outcome='retry_after',retry_after_seconds=2)['recorded']
    assert not text_claim(service,endpoint,token)
    with service.tasks._connect() as db:
        db.execute("UPDATE workspace_text_deliveries SET due=?", ((actions._now()-timedelta(seconds=1)).isoformat(),))
    next_attempt, = text_claim(service,endpoint,token)
    assert next_attempt['attemptCount'] == 2 and next_attempt['deliveryId'] == item['deliveryId']
    assert not control(service,endpoint,token,item,'ack',outcome='sent',receipt=12)['recorded']
    assert control(service,endpoint,token,next_attempt,'issue')['available']
    assert control(service,endpoint,token,next_attempt,'begin')['allowed']
    assert control(service,endpoint,token,next_attempt,'ack',outcome='delivery_unknown')['recorded']
    assert not text_claim(service,endpoint,token)
    assert control(service,endpoint,token,next_attempt,'ack',outcome='sent',receipt=12)['recorded']
    assert not control(service,endpoint,token,next_attempt,'begin')['allowed']
    service.tasks.append_workspace_response(user_subject=OWNER, source_endpoint_id=dispatch['origin_endpoint_id'],
        message_key='workspace:assistant:'+dispatch['idempotency_key'], text='公开前缀'+'文'*50001+'/results/private-synthetic-value')
    assert not text_claim(service,endpoint,token)


@pytest.mark.parametrize('ambiguous',[False, True])
def test_unpaired_or_ambiguous_admission_cannot_send_to_a_later_private_chat(openclaw,ambiguous):
    fixture, token, endpoint = openclaw
    service = fixture.service
    def add_sender():
        return service.tasks.ensure_endpoint(user_subject=OWNER,token_id=token,agent_host='openclaw',
            endpoint_key='telegram:default:502',client_type='telegram',external_subject='502',account_id='default',
            conversation_ref='agent:main:telegram:direct:502',route={'channel':'telegram','to':'502','accountId':'default'},
            capabilities=['timeline_message','direct_status'])[0]
    if ambiguous:
        new_peer = add_sender()
    else:
        with service.tasks._connect() as db:
            db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?", (endpoint['endpoint_id'],))
    dispatch = admit(fixture)
    if not ambiguous:
        new_peer = add_sender()
    service.tasks.append_workspace_response(user_subject=OWNER,source_endpoint_id=dispatch['origin_endpoint_id'],
        message_key='workspace:assistant:'+dispatch['idempotency_key'],text='这条旧答复不可转交到新私聊')
    assert not text_claim(service,new_peer,token)
    with service.tasks._connect() as db:
        assert db.execute('SELECT selection FROM workspace_query_destinations').fetchone()[0] == ('ambiguous' if ambiguous else 'unpaired')
        assert not db.execute("""SELECT 1 FROM notification_outbox WHERE endpoint_id=? AND payload_type='timeline_message'
                                 AND state='pending'""",(new_peer['endpoint_id'],)).fetchone()
