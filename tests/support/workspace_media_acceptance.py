"""Isolated media Workspace and strict original-file transport, with no update reader.

Live outbound is only available under the existing fixed Full/wheel scope verifier.
The inbound validator is an importable hook for the separately reviewed exclusive
handoff controller: this module never calls getUpdates, acknowledges or starts a poller.
"""
import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import threading
import time
from types import MethodType,SimpleNamespace
from uuid import uuid4

from tests.support.workspace_query_acceptance import Fixture,INSTANCES
from tests.support.workspace_query_channel_probe import Channel,Gateway,MockBot,live_scope
from tests.support.nanobot_image_probe import sample_png
from tests.support.nanobot_mcp_fixture import private_file
from agentbridge.core import workspace_media_delivery,workspace_query_delivery
from nanobot_agentbridge.notifications import notifications
from nanobot_agentbridge.telegram_media_receipts import validate_receipt
from nanobot_agentbridge.workspace_query_delivery import pairing_ref
from nanobot_agentbridge.telegram import TelegramAPI,TelegramUnavailable


def pdf():
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 120] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
        b'<< /Length 61 >>\nstream\nBT /F1 12 Tf 20 70 Td (AgentBridge synthetic media only) Tj ET\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    # Calculate stream length from its exact bytes, not a hand-maintained count.
    stream=b'BT /F1 12 Tf 20 70 Td (AgentBridge synthetic media only) Tj ET\n'
    objects[3]=b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'endstream'
    out=b'%PDF-1.4\n';offsets=[0]
    for i,value in enumerate(objects,1):
        offsets.append(len(out));out+=str(i).encode()+b' 0 obj\n'+value+b'\nendobj\n'
    xref=len(out);out+=b'xref\n0 6\n0000000000 65535 f \n'
    out+=b''.join(f'{n:010d} 00000 n \n'.encode() for n in offsets[1:])
    return out+b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n'


def assets(home,run_id):
    home.mkdir(mode=0o700)
    result=[]
    for suffix,mime,body in [('png','image/png',sample_png((43,117,181))),('pdf','application/pdf',pdf())]:
        name=f'agentbridge-media-{run_id}.{suffix}';path=home/name
        private_file(path,body)
        result.append({'fileName':name,'mimeType':mime,'byteSize':len(body),'sha256':hashlib.sha256(body).hexdigest(),'path':str(path)})
    return result


class MediaFixture(Fixture):
    def __init__(self,home,*,run_id=None,channel_identities=None):
        self.run_id=run_id or uuid4().hex
        if len(self.run_id)!=32 or any(c not in '0123456789abcdef' for c in self.run_id):
            raise ValueError('Expected fixed media run identifier')
        self.marker='仅核对本轮合成附件，不调用业务。验收 '+self.run_id
        self.answer='已收到本轮合成原图；这是隔离媒体投递验收。'
        self.original_environment={name:os.environ.get(name) for name in (
            'AGENTBRIDGE_SESSION_KEY_FILE','AGENTBRIDGE_NANOBOT_ROUTES_FILE','AGENTBRIDGE_PI_ROUTES_FILE')}
        super().__init__(home,deliveries=False,channel_identities=channel_identities)
        self.assets=assets(self.home/'assets',self.run_id)
        self.inbound=[]
        for peer in self.hosts.values():
            peer.send_dispatch=MethodType(self._dispatch,peer)
        # There is no OA/model step in this fixture; even a mistaken quick query
        # is denied by the actual capability call, not answered by a fake success.
        def deny(*_,**__): raise PermissionError('Media fixture cannot call business capabilities')
        self.service.invoke=deny
        private_file(self.home/'media-manifest.json',json.dumps(self.manifest(),ensure_ascii=False).encode())

    def close(self):
        try:
            super().close()
        finally:
            for name,value in self.original_environment.items():
                if value is None:
                    os.environ.pop(name,None)
                else:
                    os.environ[name]=value

    def manifest(self):
        return {'schema':'agentbridge.media-acceptance-manifest.v1','runId':self.run_id,'marker':self.marker,
            'assets':self.assets,'workspaceUpload':'png only; original upload policy unchanged',
            'telegramInbound':'send PNG or PDF as original document with the exact marker caption'}

    def _dispatch(self,peer,dispatch,binding,message,attachments,trial):
        if message!=self.marker or trial or len(attachments)!=1:
            raise PermissionError('Only the exact isolated original-image question is admitted')
        image=attachments[0];expected=self.assets[0]
        if (image.get('fileName'),image.get('mimeType'))!=(expected['fileName'],expected['mimeType']):
            raise PermissionError('Unreviewed image metadata')
        raw=base64.b64decode(image.get('content',''),validate=True)
        if len(raw)!=expected['byteSize'] or hashlib.sha256(raw).hexdigest()!=expected['sha256']:
            raise PermissionError('Unreviewed original bytes')
        run='media-fixture-'+uuid4().hex;peer.outcomes[run]=self.answer
        yield {'type':'accepted','runId':run,'status':'started'}
        yield {'type':'chat','runId':run,'state':'final','text':self.answer}

    def report(self):
        value=super().report()
        with self.service.tasks._connect() as db:
            value.update(mediaOut=[dict(row) for row in db.execute('SELECT state,count(*) AS count FROM workspace_media_deliveries GROUP BY state')],
                mediaIn=db.execute('SELECT count(*) FROM telegram_media_receipts').fetchone()[0],
                businessOperations=db.execute('SELECT count(*) FROM operations').fetchone()[0])
        return {**value,'model':'none; fixed media receipt text','inbound':list(self.inbound)}


def validate_original_document(update,manifest,*,sender,min_received,authorized):
    """Validate before any download. The owner controller retains offset/ACK ownership."""
    if authorized() is not True: raise PermissionError('Original media scope is no longer current')
    msg=update.get('message') if isinstance(update,dict) else None
    if (not isinstance(msg,dict) or set(update)!={'update_id','message'} or type(update['update_id']) is not int
            or type(msg.get('message_id')) is not int or msg['message_id']<=0
            or msg.get('chat',{}).get('type')!='private' or msg['chat'].get('id')!=sender
            or msg.get('from',{}).get('id')!=sender or msg['from'].get('is_bot') is not False
            or type(msg.get('date')) is not int or not min_received<=msg['date']<=int(time.time())+60
            or msg.get('caption')!=manifest['marker'] or any(k in msg for k in ('text','photo','video','audio','voice',
                'forward_origin','via_bot','sender_chat','business_connection_id','message_thread_id','reply_to_message'))):
        raise PermissionError('Only the exact original private media message is accepted')
    doc=msg.get('document')
    if not isinstance(doc,dict) or not isinstance(doc.get('file_id'),str) or not doc['file_id'] or len(doc['file_id'])>512:
        raise ValueError('Expected one original Telegram document')
    matching=[asset for asset in manifest['assets'] if (asset['fileName'],asset['mimeType'],asset['byteSize'])==
        (doc.get('file_name'),doc.get('mime_type'),doc.get('file_size'))]
    if len(matching)!=1: raise PermissionError('Document is outside the frozen synthetic manifest')
    return {**matching[0],'fileId':doc['file_id'],'messageId':msg['message_id'],
        'receivedAt':datetime.fromtimestamp(msg['date'],timezone.utc).isoformat()}


def validate_original_bytes(item,raw,*,authorized):
    if (authorized() is not True or not isinstance(raw,bytes) or len(raw)!=item['byteSize']
            or hashlib.sha256(raw).hexdigest()!=item['sha256']):
        raise PermissionError('Original synthetic media bytes changed')


def prepare_original_document(request,binding,update,manifest,*,sender,min_received,authorized):
    """Controller hook after its durable single-update handoff; never owns offset/ACK.

    request is the actual candidate private gateway. Freeze its source and retain
    the independent caption before the controller downloads the reviewed file.
    """
    item=validate_original_document(update,manifest,sender=sender,min_received=min_received,authorized=authorized)
    body={'binding':binding,'messageId':item['messageId'],'receivedAt':item['receivedAt']}
    source=request('telegram-media-source',body)
    if (source.get('recorded') is not True or source.get('receivedAt')!=item['receivedAt']
            or not pairing_ref(source.get('pairingRef'))):
        raise PermissionError('Candidate did not freeze the original media source')
    projected=request('telegram-timeline',{'binding':binding,'messageId':item['messageId'],'role':'user',
        'text':manifest['marker'],'runId':None,'deliveredMessageIds':[],'projectionMode':'telegram_received_v2'})
    if projected.get('recorded') is not True:
        raise PermissionError('Candidate did not retain the independent caption')
    return {**item,'body':{**body,'pairingRef':source['pairingRef'],'ordinal':0,'fileName':item['fileName'],
        'mimeType':item['mimeType'],'byteSize':item['byteSize'],'sha256':item['sha256'],'failure':None}}


def append_original_document(request,ticket,raw,*,authorized):
    validate_original_bytes(ticket,raw,authorized=authorized)
    result=request('telegram-media-append',{**ticket['body'],'contentBase64':base64.b64encode(raw).decode()})
    if not validate_receipt(result,ticket['body']['binding'],ticket['body']):
        raise PermissionError('Candidate returned a different original media receipt')
    return result


class MediaGateway(Gateway):
    def _request(self,route,body,**options):
        if body.get('binding')!=self.binding or route not in {'telegram-notifications','telegram-workspace-result-view','telegram-workspace-media-v2'}:
            raise PermissionError('Unreviewed media fixture route')
        async def call(name,args,meta):
            token=self.fixture.tokens[self.host_type,self.fixture.owner]
            if name=='agentbridge_host_notification_claim':
                response=self.fixture.service.claim_host_notifications(user_subject=self.fixture.owner,agent_host=self.host_type,
                    host_instance_id=INSTANCES[self.host_type],endpoint_key=self.binding['endpoint_key'],
                    query_token_id=token,media_token_id=token)
                if any(x['deliveryMode'] not in {'workspace_media_v2','workspace_text_v2'} for x in response['notifications']):
                    raise PermissionError('Unexpected non-media acceptance delivery')
            elif name in {'agentbridge_host_workspace_media_delivery','agentbridge_host_workspace_result_view'}:
                handler=workspace_media_delivery.handle if name.endswith('media_delivery') else workspace_query_delivery.handle
                response=handler(self.fixture.service,subject=self.fixture.owner,instance=INSTANCES[self.host_type],
                    host=args.pop('agent_host'),token_id=token,**args)
            else: raise PermissionError('Unexpected private media fixture control')
            return SimpleNamespace(isError=False,structuredContent=response)
        @asynccontextmanager
        async def paired(req): yield SimpleNamespace(call_tool=call),{},'L3'
        host=SimpleNamespace(coordinator=True,worker_payload=lambda *_:self.req,card_origins=frozenset())
        return asyncio.run(notifications(host,self.record,route,body,paired_session=paired))


class OriginalOnly:
    def __init__(self,api,fixture,*,sender,authorized):
        self.api,self.fixture,self.sender,self.authorized=api,fixture,sender,authorized
        self.receipts=[]
    def call(self,method,**params):
        if self.authorized() is not True: raise TelegramUnavailable('Media acceptance scope expired')
        if method in {'getMe','getWebhookInfo'}: return self.api.call(method,**params)
        allowed={'工作台提问\n'+self.fixture.marker,'工作台回复\n'+self.fixture.answer}
        if (method!='sendMessage' or params.get('chat_id')!=self.sender or params.get('text') not in allowed
                or params.get('reply_markup')):
            raise PermissionError('Only the exact original synthetic text may be sent')
        result=self.api.call(method,**params);self.receipts.append({'kind':'text','messageId':result.get('message_id')})
        return result
    def send_document(self,chat,filename,content_type,data):
        matched=[a for a in self.fixture.assets if (a['fileName'],a['mimeType'])==(filename,content_type)]
        if chat!=self.sender or len(matched)!=1: raise PermissionError('Unreviewed original document')
        validate_original_bytes(matched[0],data,authorized=self.authorized)
        result=self.api.send_document(chat,filename,content_type,data)
        self.receipts.append({'kind':'document','messageId':result.get('message_id'),'sha256':matched[0]['sha256']})
        return result


class MockMediaBot(MockBot):
    def send_document(self,chat,name,mime,raw):
        if chat!=self.sender: raise PermissionError('Unexpected mock recipient')
        self.count+=1
        return {'message_id':self.count,'chat':{'id':chat,'type':'private'},'from':{'id':self.bot},
            'document':{'file_id':'synthetic-original','file_size':len(raw)}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',choices=('nanobot','pi'),required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=int,default=300)
    parser.add_argument('--run-id')
    parser.add_argument('--self-check',action='store_true')
    parser.add_argument('--live-scope',type=Path)
    args=parser.parse_args()
    if args.output.exists() or not 5<=args.seconds<=480 or args.live_scope and (args.self_check or not args.run_id):
        parser.error('Use a fresh private output; live requires a fixed run ID and excludes self-check')
    os.umask(0o077);scope=None
    if args.live_scope:
        scope,credential,authorized=live_scope(args.live_scope,host=args.host)
        if scope.get('mediaRunId')!=args.run_id or scope.get('mediaDirection')!='outbound-original-only':
            raise PermissionError('Expected the separately reviewed original-media scope')
        from nanobot_agentbridge.telegram_credentials import bot_token
        bot,sender=scope['botId'],scope['senderId']
        raw_api=TelegramAPI(bot_token({'botCredential':credential['botCredential']}),private_sender=lambda chat:chat==sender and authorized())
    else:
        bot,sender,authorized=(8001 if args.host=='nanobot' else 8801),501,lambda:True
        raw_api=MockMediaBot(bot,sender)
    fixture=MediaFixture(args.output,run_id=args.run_id,channel_identities={args.host:(str(bot),str(sender))})
    api=OriginalOnly(raw_api,fixture,sender=sender,authorized=authorized)
    channel=Channel(fixture,args.host,api)
    old=channel.pilot.gateway
    channel.pilot.gateway=SimpleNamespace(_request=MethodType(MediaGateway._request,old))
    stopped=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,lambda *_:stopped.set())
    try:
        me,hook=api.call('getMe'),api.call('getWebhookInfo')
        if me.get('id')!=bot or me.get('is_bot') is not True or hook.get('url'):
            raise PermissionError('Original reviewed Bot identity or webhook changed')
        account=fixture.accounts[fixture.owner]
        choices=fixture.app.hosts.choices(account['account_id'])
        selected=fixture.app.hosts.set_default(account['account_id'],INSTANCES[args.host],expected_revision=choices['revision'])
        public={'signin':fixture.signin,'manifest':fixture.manifest(),'host':args.host,
            'realTelegram':bool(scope),'getUpdatesCalls':0,'businessWrites':0,'modelCalls':0,
            'candidateCommit':scope['candidateCommit'] if scope else None,
            'boundary':'candidate Workspace/central + real shared sender; synthetic private session and fixed host answer; SDK qualification separate'}
        print(json.dumps(public,ensure_ascii=False),flush=True)
        if args.self_check:
            image=fixture.assets[0]
            fixture.app.send_chat_stream(account,message=fixture.marker,idempotency_key=uuid4().hex,
                attachments=[{**{k:image[k] for k in ('fileName','mimeType')},'content':base64.b64encode(Path(image['path']).read_bytes()).decode()}],
                instance_id=INSTANCES[args.host],expected_revision=selected['revision'])
        deadline=time.monotonic()+args.seconds
        while not stopped.wait(.1) and time.monotonic()<deadline:
            if authorized() is not True: raise PermissionError('Original scope expired')
            channel.poll()
            report={**fixture.report(),**public,'wireReceipts':api.receipts,'senderLedger':channel.report()}
            report['realTelegramCalls']=len(api.receipts)+2 if scope else 0
            pending=args.output/'media-evidence.tmp'
            private_file(pending,json.dumps(report,ensure_ascii=False).encode())
            pending.replace(args.output/'media-evidence.json')
            if args.self_check and len(report['senderLedger'])==3 and all(x['acknowledged'] for x in report['senderLedger']):
                assert len(api.receipts)==3 and sum(x['kind']=='document' for x in api.receipts)==1
                assert report['businessOperations']==0
                print(json.dumps({'selfCheck':'passed','originalDocuments':1,'texts':2,'businessOperations':0,'getUpdatesCalls':0}),flush=True)
                break
        else:
            if args.self_check: raise RuntimeError('Media self-check did not settle its three original deliveries')
    finally:
        channel.close();fixture.close()
        if isinstance(raw_api,TelegramAPI): raw_api.close()


if __name__=='__main__': main()
