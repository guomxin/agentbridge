"""All hosts consume exact original bytes through one fenced per-item ledger."""
import base64
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

import pytest

from agentbridge.core import actionable_results as actions, workspace_media_delivery as media
from agentbridge.core.endpoint_instances import endpoint_instances
from agentbridge.workspace.telegram_pilot_links import mirror_binding
from tests.support.latency_acceptance import OWNER
from tests.support.nanobot_telegram_setup import INSTANCE
from tests.test_nanobot_workspace_chat_mirror import mirror, pair_fixture  # noqa: F401
from tests.test_pi_workspace_images import pi, paired  # noqa: F401
from tests.test_workspace_query_projection import openclaw, register  # noqa: F401
from tests.test_workspace_attachment_text_delivery import admit, collect


def claim(service,scope):
    return service.claim_host_notifications(user_subject=scope['subject'],agent_host=scope['host'],
        host_instance_id=scope['instance'],endpoint_key=scope['endpoint_key'],media_token_id=scope['token_id'],
        include_workspace_chat=True)['notifications']


def control(service,scope,item,action,**extra):
    return media.handle(service,**scope,pairing_ref=item['pairingRef'],delivery_id=item['deliveryId'],
        attempt_count=item['attemptCount'],action=action,**extra)


def oc_scope(token,endpoint):
    return dict(subject=OWNER,host='openclaw',instance='openclaw-gateway',endpoint_key=endpoint['endpoint_key'],token_id=token)


@pytest.mark.parametrize('host',['openclaw','nanobot','pi'])
def test_exact_originals_each_have_a_distinct_send_permission_and_receipt(request,host):
    if host=='openclaw':
        fixture,token,endpoint=request.getfixturevalue('openclaw')
        service=fixture.service
        scope=oc_scope(token,endpoint)
        admit(fixture)
    else:
        if host=='pi':
            fixture=request.getfixturevalue('pi');pair=request.getfixturevalue('paired')
            service,subject,account,instance=fixture.service,'user-a',fixture.accounts['a']['account_id'],'pi-office'
            endpoint,token=pair['endpoint_key'],pair['token_id']
        else:
            service,_,accounts,subject,_,_,_=request.getfixturevalue('mirror')
            account,instance,endpoint=accounts[subject],INSTANCE,'telegram:8001:501'
        with service.tasks._connect() as db:
            web=mirror_binding(db,subject,instance,endpoint_instances(db,subject))
            row=dict(db.execute('SELECT * FROM client_endpoints WHERE endpoint_id=?',(web['endpoint_id'],)).fetchone())
        if host=='pi':
            service.tasks.ensure_endpoint(user_subject=subject,token_id=token,agent_host=host,
                **{k:row[k] for k in ('endpoint_key','client_type','external_subject','conversation_ref','account_id')},
                route=json.loads(row['route_json']),capabilities=json.loads(row['capabilities_json']))
        else: token=web['token_id']
        register(service,subject,host,instance,token)
        scope=dict(subject=subject,host=host,instance=instance,endpoint_key=endpoint,token_id=token)
        service.workspace.accept_chat_request(account_id=account,user_subject=subject,message='原图的独立说明。',
            idempotency_key=uuid4().hex,attachments=[{'fileName':f'{i}.png','mimeType':'image/png',
                'content':base64.b64encode(b'\x89PNG\r\n\x1a\n'+bytes([i])).decode()} for i in range(2)],
            tasks=service.tasks,attachment_store=service.timeline_attachments,media_base_url='https://fixture.invalid',instance_id=instance)
    receipts=[]
    for ordinal in range(2):
        item,=claim(service,scope)
        assert item['deliveryMode']==media.MODE and (item['itemIndex'],item['itemCount'])==(ordinal+1,2)
        assert control(service,scope,item,'begin')=={'allowed':False}
        issued=control(service,scope,item,'issue')
        raw=base64.b64decode(issued['resource']['contentBase64'])
        assert raw==b'\x89PNG\r\n\x1a\n'+bytes([ordinal])
        assert issued['resource']['sha256']==hashlib.sha256(raw).hexdigest()
        assert issued['leaseExpiresAt'] and issued['resource']['expiresAt']
        assert not claim(service,scope)
        assert control(service,scope,item,'begin')=={'allowed':True}
        assert control(service,scope,item,'begin')=={'allowed':False}
        assert control(service,scope,item,'ack',outcome='sent',receipt=ordinal+100)=={'recorded':True}
        assert control(service,scope,item,'ack',outcome='sent',receipt=ordinal+100)=={'recorded':True}
        assert control(service,scope,item,'ack',outcome='sent',receipt=ordinal+200)=={'recorded':False}
        receipts.append(item['deliveryId'])
    assert len(set(receipts))==2 and not claim(service,scope)
    with service.tasks._connect() as db:
        assert not db.execute('SELECT 1 FROM operations').fetchone()
        if host=='pi':
            assert all(row[0]=='superseded' for row in db.execute('SELECT state FROM pi_workspace_image_deliveries'))


def test_unknown_original_is_not_replayed_and_does_not_swallow_other_media_or_text(openclaw):
    fixture,token,endpoint=openclaw
    svc=fixture.service;scope=oc_scope(token,endpoint)
    admit(fixture)
    item,=claim(svc,scope)
    assert control(svc,scope,item,'issue')['available']
    assert control(svc,scope,item,'begin')['allowed']
    assert control(svc,scope,item,'ack',outcome='delivery_unknown')['recorded']
    following,=claim(svc,scope)
    assert following['itemIndex']==2 and following['deliveryId']!=item['deliveryId']
    assert collect(svc,endpoint,token)==['工作台提问\n请比较这两张图片。']
    with svc.tasks._connect() as db:
        db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?",(endpoint['endpoint_id'],))
    assert control(svc,scope,item,'ack',outcome='sent',receipt=333)['recorded']
    assert not control(svc,scope,following,'issue')['available']
    assert not control(svc,scope,item,'issue')['available']


@pytest.mark.parametrize('failure',['pair','expiry','file_bytes','manifest','identity'])
def test_begin_rechecks_pair_original_expiry_and_bytes_after_issuance(openclaw,failure):
    fixture,token,endpoint=openclaw;svc=fixture.service;scope=oc_scope(token,endpoint)
    admit(fixture);item,=claim(svc,scope)
    assert control(svc,scope,item,'issue')['available']
    with svc.tasks._connect() as db:
        if failure=='pair': db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?",(endpoint['endpoint_id'],))
        elif failure=='expiry': db.execute("UPDATE timeline_attachments SET expires_at='2000-01-01T00:00:00+00:00'")
        elif failure=='manifest': db.execute("UPDATE timeline_attachments SET filename='changed.png'")
        elif failure=='file_bytes':
            aid=db.execute('SELECT attachment_id FROM workspace_media_deliveries WHERE delivery_id=?',(item['deliveryId'],)).fetchone()[0]
            svc.timeline_attachments._cache_path(aid).write_bytes(b'changed')
        else: scope={**scope,'token_id':'foreign'}
    assert not control(svc,scope,item,'begin')['allowed']
    assert not control(svc,scope,item,'ack',outcome='sent',receipt=1)['recorded']


def test_explicit_429_retries_only_after_due_and_rejects_old_attempt(openclaw):
    fixture,token,endpoint=openclaw;svc=fixture.service;scope=oc_scope(token,endpoint)
    admit(fixture);item,=claim(svc,scope)
    control(svc,scope,item,'issue');control(svc,scope,item,'begin')
    assert control(svc,scope,item,'ack',outcome='retry_after',retry_after_seconds=20)['recorded']
    assert not claim(svc,scope)
    with svc.tasks._connect() as db:
        db.execute('UPDATE workspace_media_deliveries SET due=? WHERE delivery_id=?',
            ((actions._now()-timedelta(seconds=1)).isoformat(),item['deliveryId']))
    newer,=claim(svc,scope)
    assert newer['deliveryId']==item['deliveryId'] and newer['attemptCount']==2
    assert not control(svc,scope,item,'ack',outcome='sent',receipt=1)['recorded']


def test_legacy_claimed_originals_are_never_transferred_to_a_new_sender(openclaw):
    fixture,token,endpoint=openclaw;svc=fixture.service;scope=oc_scope(token,endpoint)
    admit(fixture)
    with svc.tasks._connect() as db:
        question=db.execute('SELECT question_id FROM workspace_media_deliveries LIMIT 1').fetchone()[0]
        # Old OpenClaw could have claimed one multi-image envelope. Freeze it
        # as already claimed; v2 must not assume that any member remains unsent.
        assert db.execute('''UPDATE notification_outbox SET state='acknowledged',attempt_count=1
            WHERE event_id=? AND endpoint_id=? AND payload_type='timeline_message' ''',
            (question,endpoint['endpoint_id'])).rowcount==1
    assert not claim(svc,scope)
    assert not claim(svc,scope)
    with svc.tasks._connect() as db:
        assert [r[0] for r in db.execute('SELECT state FROM workspace_media_deliveries')]==['legacy','legacy']


def test_sending_lease_expiry_is_unknown_without_a_new_attempt(openclaw):
    fixture,token,endpoint=openclaw;svc=fixture.service;scope=oc_scope(token,endpoint)
    admit(fixture);first,=claim(svc,scope)
    control(svc,scope,first,'issue');control(svc,scope,first,'begin')
    with svc.tasks._connect() as db:
        db.execute('UPDATE workspace_media_deliveries SET due=? WHERE delivery_id=?',
            ((actions._now()-timedelta(seconds=1)).isoformat(),first['deliveryId']))
    following,=claim(svc,scope)
    assert following['itemIndex']==2
    with svc.tasks._connect() as db:
        assert tuple(db.execute('SELECT state,attempt FROM workspace_media_deliveries WHERE delivery_id=?',
            (first['deliveryId'],)).fetchone())==('unknown',1)
    assert control(svc,scope,first,'ack',outcome='sent',receipt=101)['recorded']


def test_a_claimed_legacy_pi_item_keeps_its_original_consumer(pi,paired):
    svc=pi.service;subject='user-a';instance='pi-office';token=paired['token_id']
    with svc.tasks._connect() as db:
        web=mirror_binding(db,subject,instance,endpoint_instances(db,subject))
        row=dict(db.execute('SELECT * FROM client_endpoints WHERE endpoint_id=?',(web['endpoint_id'],)).fetchone())
    svc.tasks.ensure_endpoint(user_subject=subject,token_id=token,agent_host='pi',
        **{k:row[k] for k in ('endpoint_key','client_type','external_subject','conversation_ref','account_id')},
        route=json.loads(row['route_json']),capabilities=json.loads(row['capabilities_json']))
    register(svc,subject,'pi',instance,token)
    svc.workspace.accept_chat_request(account_id=pi.accounts['a']['account_id'],user_subject=subject,message='图。',
        idempotency_key=uuid4().hex,attachments=[{'fileName':'one.png','mimeType':'image/png',
            'content':base64.b64encode(b'\x89PNG\r\n\x1a\noriginal').decode()}],tasks=svc.tasks,
        attachment_store=svc.timeline_attachments,media_base_url='https://fixture.invalid',instance_id=instance)
    from agentbridge.workspace.pi_timeline_media import delivery
    old=delivery(svc,**paired,action='claim')
    assert old['available']
    scope=dict(subject=subject,host='pi',instance=instance,endpoint_key=paired['endpoint_key'],token_id=token)
    assert not claim(svc,scope)
    original=delivery(svc,**paired,action='read',delivery_id=old['deliveryId'],attempt=old['attempt'])
    assert original['available']
    with svc.tasks._connect() as db:
        assert db.execute('SELECT state FROM workspace_media_deliveries').fetchone()[0]=='legacy'
        assert db.execute('SELECT state FROM pi_workspace_image_deliveries').fetchone()[0]=='claimed'
