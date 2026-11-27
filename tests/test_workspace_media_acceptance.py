from datetime import datetime,timedelta
import os
import time
from pathlib import Path

import pytest

from tests.support.workspace_media_acceptance import (MediaFixture,OriginalOnly,MockMediaBot,assets,
    validate_original_document,validate_original_bytes,prepare_original_document,append_original_document)


def test_original_media_manifest_rejects_other_messages_and_changed_original_bytes(tmp_path):
    manifest={'marker':'synthetic exact caption','assets':assets(tmp_path/'assets','a'*32)}
    source=manifest['assets'][1]
    now=int(time.time())
    frame={'update_id':5,'message':{'message_id':7,'date':now,'chat':{'type':'private','id':501},
        'from':{'id':501,'is_bot':False},'caption':manifest['marker'],
        'document':{'file_id':'fixed-file','file_name':source['fileName'],'mime_type':source['mimeType'],'file_size':source['byteSize']}}}
    scope=dict(sender=501,min_received=now,authorized=lambda:True)
    item=validate_original_document(frame,manifest,**scope)
    raw=Path(source['path']).read_bytes()
    validate_original_bytes(item,raw,authorized=lambda:True)
    for changes in ({'caption':'other'},{'date':now-1},{'photo':[]},{'forward_origin':{}},{'text':'same caption'},
                    {'from':{'id':502,'is_bot':False}},{'chat':{'id':501,'type':'group'}},
                    {'document':{**frame['message']['document'],'file_size':1}}):
        changed={**frame,'message':{**frame['message'],**changes}}
        with pytest.raises((ValueError,PermissionError)): validate_original_document(changed,manifest,**scope)
    with pytest.raises(PermissionError):validate_original_bytes(item,b'changed',authorized=lambda:True)
    with pytest.raises(PermissionError):validate_original_document(frame,manifest,**{**scope,'authorized':lambda:False})
    calls=[]
    def request(route,body):
        calls.append((route,body))
        if route=='telegram-media-source':return {'recorded':True,'pairingRef':'a'*64,'receivedAt':body['receivedAt']}
        if route=='telegram-timeline':return {'recorded':True}
        if route=='telegram-media-append':return {'recorded':True,'mediaReceipt':{**body,
            'sourceEndpointId':body['binding']['endpoint_id'],'sourceMessageId':body['messageId'],
            'entryId':'00000000-0000-4000-8000-000000000001','attachmentId':'a'*43,'state':'stored',
            'expiresAt':(datetime.fromisoformat(body['receivedAt'])+timedelta(days=7)).isoformat()}}
        pytest.fail('Unapproved update reader or business operation')
    ticket=prepare_original_document(request,{'user_subject':'fixture','endpoint_id':'original-endpoint'},frame,manifest,**scope)
    assert [route for route,_ in calls]==['telegram-media-source','telegram-timeline']
    receipt=append_original_document(request,ticket,raw,authorized=lambda:True)
    assert receipt['recorded']
    for field,value in [('sourceMessageId',8),('sourceEndpointId','other'),('ordinal',1),('sha256','b'*64),
                        ('expiresAt',datetime.now().isoformat()),('state','unavailable')]:
        bad={**receipt,'mediaReceipt':{**receipt['mediaReceipt'],field:value}}
        with pytest.raises(PermissionError):append_original_document(lambda *_:bad,ticket,raw,authorized=lambda:True)


def test_real_transport_wrapper_cannot_send_unreviewed_text_files_or_read_updates(tmp_path):
    original_environment={key:os.environ.get(key) for key in (
        'AGENTBRIDGE_SESSION_KEY_FILE','AGENTBRIDGE_NANOBOT_ROUTES_FILE','AGENTBRIDGE_PI_ROUTES_FILE')}
    fixture=MediaFixture(tmp_path/'fixture')
    try:
        raw_api=MockMediaBot(8801,501)
        api=OriginalOnly(raw_api,fixture,sender=501,authorized=lambda:True)
        asset=fixture.assets[0];raw=Path(asset['path']).read_bytes()
        assert api.send_document(501,asset['fileName'],asset['mimeType'],raw)['document']['file_size']==len(raw)
        assert api.call('sendMessage',chat_id=501,text='工作台提问\n'+fixture.marker)['message_id']
        for method,params in [('getUpdates',{'offset':1}),('sendMessage',{'chat_id':501,'text':'outside marker'}),
            ('sendMessage',{'chat_id':502,'text':'工作台提问\n'+fixture.marker})]:
            with pytest.raises(PermissionError):api.call(method,**params)
        with pytest.raises(PermissionError):api.send_document(501,asset['fileName'],asset['mimeType'],b'changed')
        with pytest.raises(PermissionError):fixture.service.invoke(capability_name='oa.workflow.pending.list')
        assert fixture.report()['businessOperations']==0
    finally:fixture.close()
    assert {key:os.environ.get(key) for key in original_environment}==original_environment
