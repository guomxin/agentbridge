"""Original private-channel media facts, independently retained and receipted per item."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re

from . import telegram_timeline_projection as history
from . import workspace_query_delivery as delivery
from .endpoint_instances import instance_key
from .store_transactions import store_transaction
from .timeline_attachments import RECEIVED_DOCUMENT_TYPES, TIMELINE_ATTACHMENT_TTL_SECONDS, public_attachment

MAX_FILE = 6 * 1024 * 1024
MAX_TOTAL = 12 * 1024 * 1024
FAILURES = {'unsupported':'此附件类型暂不支持。', 'unreadable':'原附件读取失败。',
            'too_large':'原附件超过同步大小上限。', 'expired':'原附件的保留期限已结束。'}
IMAGES = {'image/png':b'\x89PNG\r\n\x1a\n', 'image/jpeg':b'\xff\xd8\xff', 'image/webp':b'RIFF'}


def failure_attachment(value):
    failure=value.get('failure')
    if not isinstance(failure, str) or failure not in FAILURES:
        return None
    ordinal=value.get('ordinal',0)
    return {'type':'document','fileName':'附件','state':'unavailable','failure':failure,
            'reason':FAILURES[failure],'ordinal':ordinal if type(ordinal) is int and 0<=ordinal<4 else 0,
            'mediaUrl':None,'createdAt':value.get('createdAt'),'expiresAt':value.get('expiresAt')}


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS telegram_media_sources(
        subject TEXT NOT NULL, host TEXT NOT NULL, instance TEXT NOT NULL, endpoint_key TEXT NOT NULL,
        source_message_id INTEGER NOT NULL, endpoint_id TEXT NOT NULL, token_id TEXT NOT NULL,
        pair_proof TEXT NOT NULL, received_at TEXT NOT NULL,
        PRIMARY KEY(subject,host,instance,endpoint_key,source_message_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS telegram_media_receipts(
        subject TEXT NOT NULL, host TEXT NOT NULL, instance TEXT NOT NULL,
        endpoint_id TEXT NOT NULL, endpoint_key TEXT NOT NULL, token_id TEXT NOT NULL, pair_proof TEXT NOT NULL,
        source_message_id INTEGER NOT NULL, ordinal INTEGER NOT NULL, manifest_json TEXT NOT NULL,
        entry_id TEXT NOT NULL, attachment_id TEXT, expires_at TEXT NOT NULL,
        PRIMARY KEY(subject,host,instance,endpoint_key,source_message_id,ordinal))''')


def _received_time(received):
    if not isinstance(received,str) or len(received)>40:
        raise ValueError('Invalid original received time')
    try:
        stamp=datetime.fromisoformat(received)
        if stamp.tzinfo is None or stamp.utcoffset()!=timedelta(0):
            raise ValueError('UTC source time required')
    except (ValueError,TypeError):
        raise ValueError('Invalid original received time') from None
    return stamp.astimezone(timezone.utc)


def _current(db, subject, host, instance, token, endpoint_key):
    row=db.execute('SELECT * FROM client_endpoints WHERE user_subject=? AND agent_host=? AND endpoint_key=?',
                   (subject,host,endpoint_key)).fetchone()
    if not row:
        raise PermissionError('Original private endpoint unavailable')
    return history._source(db,subject,host,instance,token,endpoint_key,row['external_subject'],
        row['account_id'],row['conversation_ref'],json.loads(row['route_json']))


def source(service, *, subject, host, instance, token, endpoint_key, source_message_id, received_at, pairing_protocol=None):
    stamp=_received_time(received_at)
    if type(source_message_id) is not int or not 0<source_message_id<2**53:
        raise ValueError('Original private message required')
    if stamp>service.timeline_attachments.clock()+timedelta(seconds=60):
        raise ValueError('Received time is in the future')
    key=(subject,host,instance,endpoint_key,source_message_id)
    with store_transaction(service.tasks) as db:
        if not delivery.registered(db,subject,host,instance_key(host,instance),token):
            raise PermissionError('Registered personal media transport required')
        old=db.execute('''SELECT * FROM telegram_media_sources WHERE subject=? AND host=? AND instance=?
            AND endpoint_key=? AND source_message_id=?''',key).fetchone()
        if old:
            if (old['token_id'],old['received_at'])!=(token,stamp.isoformat()):
                raise PermissionError('Original received source cannot be replaced')
            endpoint_id,proof=old['endpoint_id'],old['pair_proof']
        else:
            from agentbridge.workspace.telegram_pair_migration import permits_delivery,reception_boundary
            if not permits_delivery(db,host,subject,instance,endpoint_key,token,pairing_protocol):
                raise PermissionError("Current reception protocol required")
            endpoint,proof=_current(db,subject,host,instance,token,endpoint_key)
            if host in {'nanobot','pi'}:
                boundary=reception_boundary(db,subject,instance,endpoint['account_id'],endpoint['external_subject'],token)
                if boundary['generationScoped'] and (stamp.microsecond or stamp.timestamp()<=boundary['receptionFloor']):
                    raise PermissionError("Original platform second does not prove the current grant")
            # Telegram dates have second precision. Endpoint creation is lazy,
            # so its timestamp is not the start of the user's authorization.
            grant=db.execute('SELECT created_at FROM mcp_identity_tokens WHERE token_id=?',(token,)).fetchone()
            effective=_received_time(grant[0]).timestamp()
            if host in {'pi','nanobot'}:
                link=db.execute('''SELECT confirmed_at FROM workspace_telegram_pilot_links
                    WHERE owner=? AND instance=? AND bot=? AND sender=?''',
                    (subject,instance,endpoint['account_id'],endpoint['external_subject'])).fetchone()
                if link:
                    effective=max(effective,link[0])
            if stamp.timestamp()<math.floor(effective):
                raise PermissionError('Original message predates the current authorization')
            endpoint_id=endpoint['endpoint_id']
            db.execute('INSERT INTO telegram_media_sources VALUES(?,?,?,?,?,?,?,?,?)',
                       (*key,endpoint_id,token,proof,stamp.isoformat()))
    return {'recorded':True,'sourceEndpointId':endpoint_id,'pairingRef':proof,'receivedAt':stamp.isoformat()}


def _manifest(message, ordinal, name, mime, size, digest, received, failure):
    if (type(message) is not int or not 0 < message < 2**53 or type(ordinal) is not int or not 0 <= ordinal < 4
            or not isinstance(name,str) or not name.strip() or len(name)>256 or '/' in name or '\\' in name
            or any(ord(ch)<32 for ch in name) or not isinstance(received,str) or len(received)>40):
        raise ValueError('Invalid original media identity')
    stamp=_received_time(received)
    received=stamp.isoformat()
    if failure is not None:
        if not isinstance(failure,str) or failure not in FAILURES or (name,mime,size,digest) != ('附件','application/octet-stream',0,None):
            raise ValueError('Failure must be a fixed public media disposition')
    elif (mime not in IMAGES and mime not in RECEIVED_DOCUMENT_TYPES or type(size) is not int
          or not 0 < size <= MAX_FILE or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest)):
        raise ValueError('Unsupported original media metadata')
    return dict(sourceMessageId=message,ordinal=ordinal,fileName=name,mimeType=mime,byteSize=size,
                sha256=digest,receivedAt=received,failure=failure), stamp


def _bytes(manifest, content):
    if not isinstance(content,str) or not 0<len(content)<=4*((MAX_FILE+2)//3):
        raise ValueError('Invalid bounded media body')
    try:
        raw=base64.b64decode(content,validate=True)
    except (ValueError,TypeError):
        raise ValueError('Invalid media encoding') from None
    if len(raw)!=manifest['byteSize'] or hashlib.sha256(raw).hexdigest()!=manifest['sha256']:
        raise ValueError('Original media content changed')
    mime=manifest['mimeType']
    if mime in IMAGES:
        if not raw.startswith(IMAGES[mime]) or mime=='image/webp' and raw[8:12]!=b'WEBP':
            raise ValueError('Original image signature does not match')
    elif mime=='application/pdf':
        if not raw.startswith(b'%PDF-'):
            raise ValueError('Original PDF signature does not match')
    else:
        try:
            text=raw.decode('utf-8-sig')
        except UnicodeError:
            raise ValueError('Original text file must be UTF-8') from None
        if not text.strip() or any(ord(ch)<32 and ch not in '\n\r\t' for ch in text):
            raise ValueError('Invalid original text document')


def _receipt(row):
    manifest=json.loads(row['manifest_json'])
    return {**manifest,'sourceEndpointId':row['endpoint_id'],'entryId':row['entry_id'],
            'attachmentId':row['attachment_id'],'expiresAt':row['expires_at'],
            'state':'unavailable' if manifest['failure'] else 'stored'}


def append(service, *, subject, host, instance, token, endpoint_key, pairing_ref, source_message_id,
           ordinal, file_name, mime_type, byte_size, sha256, received_at, content_base64=None, failure=None, pairing_protocol=None):
    manifest,stamp=_manifest(source_message_id,ordinal,file_name,mime_type,byte_size,sha256,received_at,failure)
    if not isinstance(pairing_ref,str) or not re.fullmatch('[0-9a-f]{64}',pairing_ref):
        raise ValueError('Original pairing generation required')
    if failure is not None and content_base64 is not None:
        raise ValueError('Failure cannot contain resource bytes')
    encoded=json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    key=(subject,host,instance,endpoint_key,source_message_id,ordinal)
    created=[]
    try:
        with store_transaction(service.tasks) as db:
            if not delivery.registered(db,subject,host,instance_key(host,instance),token):
                raise PermissionError('Registered personal media transport required')
            old=db.execute('''SELECT * FROM telegram_media_receipts WHERE subject=? AND host=? AND instance=?
                AND endpoint_key=? AND source_message_id=? AND ordinal=?''',key).fetchone()
            if old:
                if (old['token_id'],old['pair_proof'],old['manifest_json']) != (token,pairing_ref,encoded):
                    raise PermissionError('Original media receipt identity or content changed')
                # A lost response may be recovered after revocation/expiry. It
                # yields only its old receipt, never bytes or another admission.
                return {'recorded':True,'mediaReceipt':_receipt(old)}
            if content_base64 is None and failure is None:
                return {'recorded':False}
            received=db.execute('''SELECT * FROM telegram_media_sources WHERE subject=? AND host=? AND instance=?
                AND endpoint_key=? AND source_message_id=?''',key[:-1]).fetchone()
            if not received or (received['token_id'],received['pair_proof'],received['received_at'])!=(token,pairing_ref,manifest['receivedAt']):
                raise PermissionError('Original independent received source required')
            from agentbridge.workspace.telegram_pair_migration import permits_delivery
            if not permits_delivery(db,host,subject,instance,endpoint_key,token,pairing_protocol):
                raise PermissionError("Current reception protocol required")
            endpoint,proof=_current(db,subject,host,instance,token,endpoint_key)
            if received['endpoint_id']!=endpoint['endpoint_id']:
                raise PermissionError('Original received endpoint changed')
            if proof!=pairing_ref:
                raise PermissionError('Original private pairing changed')
            now=service.timeline_attachments.clock()
            expiry=stamp+timedelta(seconds=TIMELINE_ATTACHMENT_TTL_SECONDS)
            if stamp>now+timedelta(seconds=60):
                raise ValueError('Received media timestamp outside accepted window')
            if expiry<=now and failure!='expired':
                raise ValueError('Original resource retention ended; use an expired disposition')
            totals=db.execute('''SELECT coalesce(sum(json_extract(manifest_json,'$.byteSize')),0) AS size
                FROM telegram_media_receipts WHERE subject=? AND host=? AND instance=? AND endpoint_key=?
                AND source_message_id=?''',key[:-1]).fetchone()['size']
            if totals+byte_size>MAX_TOTAL:
                raise ValueError('Original message exceeds total media budget')
            material=json.dumps([host,instance,endpoint['endpoint_id'],source_message_id,ordinal],separators=(',',':'))
            message_key='telegram-media:'+hashlib.sha256(material.encode()).hexdigest()
            attachment_id=None
            if failure:
                attachment={'type':'document','fileName':'附件','state':'unavailable','failure':failure,
                            'reason':FAILURES[failure],'ordinal':ordinal,'createdAt':manifest['receivedAt'],
                            'expiresAt':expiry.isoformat(),'mediaUrl':None}
            else:
                _bytes(manifest,content_base64)
                saved,=service.timeline_attachments._create_many_in_connection(db,user_subject=subject,message_key=message_key,
                    attachments=[{'mimeType':mime_type,'fileName':file_name,'content':content_base64}],
                    media_base_url=service.trusted_card_base_url,created_paths=created,received_at=stamp,
                    ordinal_offset=ordinal,allow_documents=True)
                attachment_id=saved['attachment_id']
                attachment=public_attachment(saved)
            entry,_=service.tasks._append_timeline_message_in_connection(db,user_subject=subject,
                source_endpoint_id=endpoint['endpoint_id'],message_key=message_key,role='user',text='原消息附件',
                payload={'attachments':[attachment]},_telegram_projection=True,enqueue_notifications=False)
            db.execute('INSERT INTO telegram_media_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (subject,host,instance,endpoint['endpoint_id'],endpoint_key,token,pairing_ref,source_message_id,ordinal,
                 encoded,entry['entry_id'],attachment_id,expiry.isoformat()))
            row=db.execute('SELECT * FROM telegram_media_receipts WHERE entry_id=?',(entry['entry_id'],)).fetchone()
            result={'recorded':True,'mediaReceipt':_receipt(row)}
        return result
    except BaseException:
        service.timeline_attachments._discard_created_files(created)
        raise


def authorize_download(service, subject, attachment_id):
    """Keep a remembered URL subject to the same current source scope as history."""
    from .endpoint_instances import endpoint_instances
    with service.tasks._connect() as db:
        row=db.execute('SELECT * FROM telegram_media_receipts WHERE attachment_id=?',(attachment_id,)).fetchone()
        if not row:
            return
        if row['subject']!=subject or endpoint_instances(db,subject).get(row['endpoint_id']) != instance_key(row['host'],row['instance']):
            raise PermissionError('Original resource source is no longer available')
        source=db.execute('SELECT * FROM client_endpoints WHERE endpoint_id=?',(row['endpoint_id'],)).fetchone()
        _,proof=history._source(db,subject,row['host'],row['instance'],row['token_id'],row['endpoint_key'],
            source['external_subject'],source['account_id'],source['conversation_ref'],json.loads(source['route_json']))
        if proof!=row['pair_proof']:
            raise PermissionError('Original resource pairing is no longer available')
