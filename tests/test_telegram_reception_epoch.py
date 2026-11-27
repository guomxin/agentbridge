"""Real central epochs reject old platform seconds; old receipt ACK grants no new authority."""
from datetime import datetime,timezone
import hashlib

import pytest

from agentbridge.core import telegram_media_receipts
from tests.test_telegram_pairing_revocation import migration_setup, mirror, pair_fixture  # noqa: F401
from tests.support.nanobot_telegram_setup import INSTANCE


def setup_epoch(mirror, pair_fixture):
    service,links,accounts,owner,_,token=migration_setup(mirror,pair_fixture)
    old=links.ensure_endpoint(owner,token,INSTANCE,8001,501)
    assert old['generationScoped'] is False and old['receptionFloor'] is None
    challenge=links.begin(accounts[owner],INSTANCE,8001,migrate=True)
    proof=links.present(owner,INSTANCE,8001,501,challenge['code'],token_id=token,protocol=2)
    links.confirm(accounts[owner],INSTANCE,8001,challenge['challenge_id'],proof)
    current=links.ensure_endpoint(owner,token,INSTANCE,8001,501,protocol=2)
    return service,links,accounts,owner,token,current


def test_source_rejects_backlog_same_second_and_rollback_before_new_storage(mirror,pair_fixture):
    service,links,accounts,owner,token,endpoint=setup_epoch(mirror,pair_fixture)
    floor=endpoint['receptionFloor']
    assert endpoint['generationScoped'] is True and type(floor) is int
    args=dict(subject=owner,host='nanobot',instance=INSTANCE,token=token,endpoint_key=endpoint['endpointKey'],source_message_id=100)
    stamp=lambda n: datetime.fromtimestamp(n,timezone.utc).isoformat()
    for date in [stamp(floor-1),stamp(floor),datetime.fromtimestamp(floor+0.5,timezone.utc).isoformat()]:
        with pytest.raises((ValueError,PermissionError)):
            telegram_media_receipts.source(service,**args,received_at=date,pairing_protocol=2)
    with pytest.raises(PermissionError):
        telegram_media_receipts.source(service,**args,received_at=stamp(floor+1))
    first=telegram_media_receipts.source(service,**args,received_at=stamp(floor+1),pairing_protocol=2)
    assert first['pairingRef']==endpoint['pairingRef']
    links.revoke(accounts[owner],INSTANCE,8001)
    assert telegram_media_receipts.source(service,**args,received_at=stamp(floor+1))==first
    with pytest.raises(PermissionError):
        telegram_media_receipts.source(service,**{**args,'source_message_id':101},received_at=stamp(floor+1),pairing_protocol=2)
    with pytest.raises(PermissionError):
        telegram_media_receipts.source(service,**args,received_at=stamp(floor+2),pairing_protocol=2)
    with service.tasks._connect() as db:
        assert db.execute('SELECT count(*) FROM telegram_media_sources').fetchone()[0]==1
        assert not db.execute('SELECT 1 FROM user_timeline').fetchone()


def test_history_exact_original_credentials_only_ack_after_revoke_and_pruning(mirror,pair_fixture):
    service,links,accounts,owner,token,endpoint=setup_epoch(mirror,pair_fixture)
    body=dict(user_subject=owner,token_id=token,agent_host='nanobot',host_instance_id=INSTANCE,
        endpoint_key=endpoint['endpointKey'],client_type='telegram',external_subject='501',account_id='8001',
        conversation_ref=endpoint['conversationRef'],route={'channel':'telegram','accountId':'8001','to':'501'},
        message_key='nano-original-text',role='user',text='原问题',projection_v2=True,pairing_protocol=2)
    append=service.append_host_timeline_message
    with pytest.raises(PermissionError):
        append(**{**body,'pairing_protocol':None})
    with pytest.raises(PermissionError):
        append(**body)
    with pytest.raises(PermissionError):
        append(**{**body,"projection_v2":False})
    received_at=datetime.fromtimestamp(endpoint['receptionFloor']+1,timezone.utc).isoformat()
    telegram_media_receipts.source(service,subject=owner,host='nanobot',instance=INSTANCE,token=token,
        endpoint_key=endpoint['endpointKey'],source_message_id=100,received_at=received_at,pairing_protocol=2)
    body.update(source_message_id=100,received_at=received_at,pairing_ref=endpoint['pairingRef'])
    receipt=append(**body)['projectionReceipt']
    assert append(**{**body,'projection_v2':False})['entry']['entryId']==receipt['entryId']
    with service.tasks._connect() as db:
        db.execute('DELETE FROM user_timeline WHERE entry_id=?',(receipt['entryId'],))
    links.revoke(accounts[owner],INSTANCE,8001)
    ack={**body,'projection_receipt_only':True,'pairing_protocol':None}
    result=append(**ack)
    assert result['projectionReceipt']==receipt and result['entry']['retained'] is False
    assert append(**{**ack,'message_key':'absent'})=={'status':'unavailable','recorded':False}
    for changes in [{'text':'改写'}, {'role':'assistant'}, {'token_id':'foreign'}, {'host_instance_id':'foreign'},
                    {'external_subject':'502'}, {'route':{'channel':'telegram','accountId':'8001','to':'502'}},
                    {'endpoint_key':'telegram:8001:502'}]:
        with pytest.raises((PermissionError,ValueError)):
            append(**{**ack,**changes})
    with pytest.raises((PermissionError,ValueError)):
        append(**body)
    with service.tasks._connect() as db:
        assert not db.execute('SELECT 1 FROM user_timeline').fetchone()
        assert db.execute('SELECT count(*) FROM telegram_history_receipts').fetchone()[0]==1
        # A legacy receipt cannot receive a scope retroactively through a read.
        db.execute('UPDATE telegram_history_receipts SET scope_json=NULL')
    with pytest.raises((PermissionError,ValueError)):
        append(**ack)
    with service.tasks._connect() as db:
        assert db.execute('SELECT scope_json FROM telegram_history_receipts').fetchone()[0] is None
