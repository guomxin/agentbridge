"""Per-original Workspace media delivery, with an explicit handoff from legacy senders."""
import base64
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from agentbridge.workspace.telegram_pair_migration import permits_delivery

from . import actionable_results as actions, workspace_query_delivery as query
from .endpoint_instances import endpoint_instances, instance_key
from .store_transactions import store_transaction

MODE='workspace_media_v2'
TABLE='workspace_media_deliveries'
_JOIN='''SELECT d.*,p.subject,p.source,p.host,p.instance,p.endpoint_id,p.endpoint_key,
    p.token_id,p.pair_proof,p.endpoint_hash,p.question_hash
    FROM workspace_media_deliveries d JOIN workspace_query_destinations p USING(question_id)'''


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS workspace_media_deliveries(
        delivery_id TEXT PRIMARY KEY,question_id TEXT NOT NULL,dispatch_id TEXT NOT NULL,
        attachment_id TEXT NOT NULL,ordinal INTEGER NOT NULL,item_count INTEGER NOT NULL,
        resource_json TEXT NOT NULL,state TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,
        due TEXT NOT NULL,issued INTEGER NOT NULL DEFAULT 0,receipt INTEGER,outcome TEXT,
        UNIQUE(question_id,ordinal));
        CREATE INDEX IF NOT EXISTS workspace_media_due ON workspace_media_deliveries(state,due);''')


def _manifest(image):
    return {name:image[key] for name,key in (
        ('fileName','filename'),('mimeType','content_type'),('byteSize','byte_size'),
        ('sha256','content_hash'),('expiresAt','expires_at'))}


def enqueue(db,dispatch,entry,images):
    pin=db.execute("SELECT * FROM workspace_query_destinations WHERE question_id=? AND selection='paired'",
                   (entry['entry_id'],)).fetchone()
    if not pin or (pin['subject'],pin['source'],pin['host'])!=(dispatch['user_subject'],dispatch['origin_endpoint_id'],dispatch['agent_host']):
        return
    for image in images:
        db.execute('''INSERT OR IGNORE INTO workspace_media_deliveries
            (delivery_id,question_id,dispatch_id,attachment_id,ordinal,item_count,resource_json,state,due)
            VALUES(?,?,?,?,?,?,?,'pending',?)''',
            (str(uuid4()),entry['entry_id'],dispatch['dispatch_id'],image['attachment_id'],image['ordinal'],len(images),
             json.dumps(_manifest(image),sort_keys=True),actions._now().isoformat()))


def _handoff(db,row):
    """Acquire only an original item which no legacy sender ever claimed."""
    legacy=db.execute('''SELECT state,attempt_count FROM notification_outbox
        WHERE event_id=? AND endpoint_id=? AND payload_type='timeline_message' ''',
        (row['question_id'],row['endpoint_id'])).fetchall()
    if any(item['attempt_count'] or item['state'] not in {'pending','superseded'} for item in legacy):
        return False
    if row['host']=='pi':
        old=db.execute('''SELECT ordinal,state,attempt FROM pi_workspace_image_deliveries
            WHERE dispatch_id=? AND destination=?''',
            (row['dispatch_id'],row['endpoint_id'])).fetchall()
        own=next((item for item in old if item['ordinal']==row['ordinal']),None)
        started=any(item['attempt'] or item['state'] not in {'pending','superseded'} for item in old)
        # An earlier release may already have transferred this particular item.
        # Preserve that owner; otherwise an active old group must finish there.
        if started and (not own or own['state']!='superseded'):
            return False
        if not started:
            # Transfer every untouched original atomically so the legacy poller
            # cannot send its second image while v2 is still sending the first.
            db.execute('''UPDATE pi_workspace_image_deliveries SET state='superseded'
                WHERE dispatch_id=? AND destination=? AND attempt=0 AND state='pending' ''',
                (row['dispatch_id'],row['endpoint_id']))
    db.execute('''UPDATE notification_outbox SET state='superseded'
        WHERE event_id=? AND endpoint_id=? AND payload_type='timeline_message' AND attempt_count=0 AND state='pending' ''',
        (row['question_id'],row['endpoint_id']))
    return True


def _legacy_predecessor_pending(db,row):
    return row['host']=='pi' and db.execute('''SELECT 1 FROM pi_workspace_image_deliveries
        WHERE dispatch_id=? AND destination=? AND entry_id=? AND ordinal<?
            AND state IN ('pending','claimed','sending') LIMIT 1''',
        (row['dispatch_id'],row['endpoint_id'],row['question_id'],row['ordinal'])).fetchone() is not None


def _current(db,row,*,read_body,service):
    current=query.pair(db,row['subject'],row['source'],row['endpoint_id'],endpoint_instances(db,row['subject']))
    if not current or (current[1],current[0]['_binding_hash'])!=(row['pair_proof'],row['endpoint_hash']):
        return None
    image=db.execute('''SELECT i.*,q.payload_json,q.text FROM timeline_attachments i
        JOIN agent_host_dispatches d ON d.dispatch_id=? AND d.user_subject=i.user_subject
            AND d.message_key=i.message_key AND d.agent_host=? AND d.instance_id=? AND d.origin_endpoint_id=?
        JOIN user_timeline q ON q.entry_id=? AND q.user_subject=d.user_subject AND q.source_endpoint_id=d.origin_endpoint_id
            AND q.role='user' AND q.task_id IS NULL AND q.dedupe_key='message:'||d.message_key
        WHERE i.attachment_id=? AND i.ordinal=? AND i.user_subject=? AND i.state='ready'
            AND json_extract(d.attachment_refs_json,'$['||i.ordinal||']')=i.attachment_id
            AND json_extract(q.payload_json,'$.attachments['||i.ordinal||'].attachmentId')=i.attachment_id''',
        (row['dispatch_id'],row['host'],row['instance'],row['source'],row['question_id'],row['attachment_id'],row['ordinal'],row['subject'])).fetchone()
    if (not image or set(json.loads(image['payload_json']))-{'attachments'}
            or actions._hash(image['text'])!=row['question_hash']
            or actions._date(image['expires_at'])<=service.timeline_attachments.clock()
            or json.dumps(_manifest(image),sort_keys=True)!=row['resource_json']):
        return None
    if not read_body:
        return _manifest(image)
    # This transaction already holds the writer lock. Do not enter the store's
    # separate expiry-maintenance writer; verify the exact frozen bytes here.
    try:
        raw=service.timeline_attachments._cache_path(row['attachment_id']).read_bytes()
    except OSError:
        return None
    if len(raw)!=image['byte_size'] or hashlib.sha256(raw).hexdigest()!=image['content_hash']:
        return None
    return {**_manifest(image),'contentBase64':base64.b64encode(raw).decode('ascii')}


def claim(service,subject,host,instance,endpoint,token,limit,lease,*,pairing_protocol=None):
    scope=instance_key(host,instance)
    now=actions._now()
    with store_transaction(service.tasks) as db:
        if (not query.registered(db,subject,host,scope,token) or
                not permits_delivery(db, host, subject, instance, endpoint, token, pairing_protocol)):
            return []
        db.execute('''UPDATE workspace_media_deliveries SET state='unknown',outcome='delivery_unknown'
            WHERE state='sending' AND due<=? AND question_id IN
            (SELECT question_id FROM workspace_query_destinations WHERE subject=? AND host=? AND instance=? AND endpoint_key=? AND token_id=?)''',
            (now.isoformat(),subject,host,scope[1],endpoint,token))
        rows=db.execute(_JOIN+''' WHERE p.subject=? AND p.host=? AND p.instance=? AND p.endpoint_key=? AND p.token_id=?
            AND d.state IN ('pending','leased','retry_wait') AND d.due<=?
            AND NOT EXISTS(SELECT 1 FROM workspace_media_deliveries previous WHERE previous.question_id=d.question_id
                AND previous.ordinal<d.ordinal AND previous.state IN ('pending','leased','retry_wait','sending'))
            ORDER BY d.due,d.rowid LIMIT ?''',(subject,host,scope[1],endpoint,token,now.isoformat(),limit)).fetchall()
        result=[]
        for row in rows:
            if _legacy_predecessor_pending(db,row):
                continue
            if row['attempt']>=5 or not _current(db,row,read_body=False,service=service):
                db.execute("UPDATE workspace_media_deliveries SET state='unavailable' WHERE delivery_id=?",(row['delivery_id'],))
                continue
            if row['attempt']==0 and not _handoff(db,row):
                db.execute("UPDATE workspace_media_deliveries SET state='legacy' WHERE delivery_id=?",(row['delivery_id'],))
                continue
            due=(now+timedelta(seconds=max(5,min(300,lease)))).isoformat()
            db.execute("UPDATE workspace_media_deliveries SET state='leased',attempt=attempt+1,issued=0,due=? WHERE delivery_id=?",(due,row['delivery_id']))
            result.append({'deliveryMode':MODE,'deliveryId':row['delivery_id'],'attemptCount':row['attempt']+1,
                'pairingRef':row['pair_proof'],'itemIndex':row['ordinal']+1,'itemCount':row['item_count'],'requiresRouteCheck':True})
        return result


def handle(service,*,subject,host,instance,endpoint_key,token_id,pairing_ref,delivery_id,attempt_count,action,
           outcome=None,receipt=None,retry_after_seconds=None,pairing_protocol=None):
    key={'issue':'available','begin':'allowed','ack':'recorded'}[action]
    with store_transaction(service.tasks) as db:
        row=db.execute(_JOIN+' WHERE d.delivery_id=?',(delivery_id,)).fetchone()
        expected=(subject,host,instance_key(host,instance)[1],endpoint_key,token_id,pairing_ref,attempt_count)
        if not row or tuple(row[x] for x in ('subject','host','instance','endpoint_key','token_id','pair_proof','attempt'))!=expected:
            return {key:False}
        if action=='ack':
            return {key:query.settle(db,row,outcome,receipt,retry_after_seconds,table=TABLE)}
        if (any(x is not None for x in (outcome,receipt,retry_after_seconds)) or row['state']!='leased'
                or not permits_delivery(db, host, subject, instance, endpoint_key, token_id, pairing_protocol)
                or actions._date(row['due'])<=actions._now() or not query.registered(db,subject,host,(host,row['instance']),token_id)):
            return {key:False}
        if _legacy_predecessor_pending(db,row):
            return {key:False}
        resource=_current(db,row,read_body=True,service=service)
        if resource is None:
            return {key:False}
        if action=='begin':
            if not row['issued']:
                return {key:False}
            db.execute("UPDATE workspace_media_deliveries SET state='sending' WHERE delivery_id=?",(delivery_id,))
            return {key:True}
        db.execute('UPDATE workspace_media_deliveries SET issued=1 WHERE delivery_id=?',(delivery_id,))
        return {key:True,'leaseExpiresAt':row['due'],'resource':resource}
