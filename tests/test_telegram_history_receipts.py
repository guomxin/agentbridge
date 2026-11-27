import hashlib

import pytest

from tests.support.latency_acceptance import OWNER
from tests.test_workspace_query_projection import openclaw  # noqa: F401


def test_original_telegram_source_has_durable_content_bound_receipt(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    append = service.append_host_timeline_message
    body = dict(user_subject=OWNER, token_id=token, agent_host='openclaw', host_instance_id='openclaw-gateway',
                endpoint_key=endpoint['endpoint_key'], client_type='telegram', external_subject='501',
                account_id=None, conversation_ref=endpoint['conversation_ref'],
                route={'channel':'telegram', 'to':'telegram:501', 'threadId':None},
                message_key='tg:fixture-message:assistant', role='assistant', text='中😀'*25500, projection_v2=True)
    receipt = append(**body)['projectionReceipt']
    assert receipt == {'entryId':receipt['entryId'], 'messageKey':body['message_key'], 'role':'assistant',
                       'sourceEndpointId':endpoint['endpoint_id'], 'textSha256':hashlib.sha256(body['text'].encode()).hexdigest()}
    assert append(**body)['projectionReceipt'] == receipt
    for changes in ({'text':'同键不同内容'}, {'role':'user'}, {'text':'/results/synthetic-private'},
                    {'token_id':'foreign'}, {'endpoint_key':'tg:no-such-source'}, {'client_type':'web'},
                    {'route':{'channel':'telegram','to':'501','threadId':'group'}}, {'account_id':'wrong'}):
        with pytest.raises((PermissionError,ValueError)):
            append(**{**body, **changes})
    with service.tasks._connect() as db:
        db.execute('DELETE FROM user_timeline WHERE entry_id=?', (receipt['entryId'],))
    result = append(**body)
    assert result['projectionReceipt'] == receipt
    assert result['entry'] == {'entryId':receipt['entryId'],'role':'assistant','retained':False}
    assert append(**{**body, 'message_key':'tg:second-message:assistant'})['projectionReceipt']['entryId'] != receipt['entryId']
    with service.tasks._connect() as db:
        assert not db.execute('SELECT 1 FROM user_timeline WHERE entry_id=?',(receipt['entryId'],)).fetchone()
        assert db.execute('SELECT count(*) FROM telegram_history_receipts').fetchone()[0] == 2
        assert not db.execute('SELECT 1 FROM operations UNION ALL SELECT 1 FROM agent_host_dispatches').fetchone()
