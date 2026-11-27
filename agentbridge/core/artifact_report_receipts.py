"""Read-only recovery of an already committed, identity-bound artifact report."""
from datetime import datetime, timezone
import hashlib
import json
import re

from .endpoint_instances import endpoint_instances, instance_key


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def request_hash(channel, files):
    if not isinstance(channel, str) or not channel or not isinstance(files, list) or not 1 <= len(files) <= 20:
        raise ValueError('Original artifact report required')
    normalized=[]
    for item in files:
        if (not isinstance(item, dict) or set(item)-{'artifact_id','state','attempt_count','error_code'}
                or not isinstance(item.get('artifact_id'), str) or not 1<=len(item['artifact_id'])<=128
                or item.get('state') not in {'attachment_sent', 'fallback_link_sent', 'failed'}
                or type(item.get('attempt_count', 0)) is not int or not 0 <= item.get('attempt_count', 0) <= 3
                or item.get('error_code') is not None and not isinstance(item['error_code'], str)):
            raise ValueError('Original artifact report fields required')
        normalized.append([item['artifact_id'], item['state'], item.get('attempt_count', 0), item.get('error_code')])
    if len({row[0] for row in normalized}) != len(normalized):
        raise ValueError('Duplicate artifact report item')
    return hashlib.sha256(canonical([channel, normalized]).encode()).hexdigest()


def endpoint_identity(row):
    if row is None:
        return None
    return {key:row[key] for key in ('endpoint_id', 'endpoint_key', 'user_subject', 'token_id', 'agent_host',
                                    'client_type', 'external_subject', 'account_id', 'conversation_ref', 'route_json')}


def capture(db, *, task, subject, host, channel, delivery_ref, files, context):
    """Called inside the same transaction as the first report event only."""
    if not context or host != 'openclaw' or channel != 'telegram':
        return None
    instance,token=context.get('hostInstanceId'),context.get('tokenId')
    if not isinstance(instance,str) or not isinstance(token,str):
        return None
    lease=db.execute('SELECT * FROM agent_task_coordinator_leases WHERE task_id=?', (task['task_id'],)).fetchone()
    if (not lease or (lease['user_subject'],lease['agent_host'],lease['host_instance_id']) != (subject,host,instance)
            or lease['state']!='active' or datetime.fromisoformat(lease['expires_at'])<=datetime.now(timezone.utc)):
        return None
    origin=db.execute('SELECT * FROM client_endpoints WHERE endpoint_id=?', (task['origin_endpoint_id'],)).fetchone()
    notification=db.execute('SELECT * FROM notification_outbox WHERE delivery_id=? AND task_id=? AND user_subject=?',
                            (delivery_ref,task['task_id'],subject)).fetchone()
    if notification:
        envelope=json.loads(notification['payload_json'])
        if (notification['payload_type']!='task_event' or envelope.get('eventType')!='task.artifact.ready'
                or [item.get('artifact_id') for item in files]!=[(envelope.get('payload') or {}).get('artifactId')]):
            return None
        target=db.execute('SELECT * FROM client_endpoints WHERE endpoint_id=?', (notification['endpoint_id'],)).fetchone()
    elif delivery_ref.startswith('tool-result:'):
        target=origin
    else:
        return None
    owners=endpoint_instances(db,subject)
    if (not origin or not target or target['client_type']!='telegram' or target['token_id']!=token
            or target['user_subject']!=subject or target['agent_host']!=host or target['state']!='active'
            or owners.get(origin['endpoint_id'])!=instance_key(host,instance)
            or owners.get(target['endpoint_id'])!=instance_key(host,instance)):
        return None
    route=json.loads(target['route_json'])
    if (route.get('channel')!='telegram' or str(route.get('to','')).removeprefix('telegram:')!=target['external_subject']
            or not re.fullmatch('[1-9][0-9]*',target['external_subject'])
            or (route.get('accountId') or 'default')!=(target['account_id'] or 'default')
            or route.get('threadId') not in (None,'')):
        return None
    return {'subject':subject,'host':host,'instance':instance,'token':token,
            'origin':endpoint_identity(origin),'target':endpoint_identity(target),
            'leaseId':lease['lease_id'],'leaseVersion':lease['version'],
            'requestHash':request_hash(channel,files),'channel':channel,'deliveryRef':delivery_ref}


def persist(db, *, task_id, event_id, proof):
    if proof is not None:
        event=db.execute('SELECT payload_json FROM task_events WHERE event_id=? AND task_id=?',(event_id,task_id)).fetchone()
        proof={**proof,'eventHash':hashlib.sha256(event['payload_json'].encode()).hexdigest()}
        db.execute('INSERT INTO artifact_report_receipt_custody VALUES(?,?,?)',
                   (event_id,task_id,canonical(proof)))


def historical(service, *, subject, host, instance, token, task_id, delivery_ref, channel, files):
    digest=request_hash(channel,files)
    with service.tasks._connect() as db:
        db.execute('BEGIN')
        row=db.execute('''SELECT e.*, c.proof_json FROM task_events e
            JOIN artifact_report_receipt_custody c USING(event_id,task_id)
            WHERE e.task_id=? AND e.user_subject=? AND e.event_type='task.artifact.delivery'
            AND e.causation_ref=? ORDER BY e.created_at DESC,e.rowid DESC LIMIT 1''',
            (task_id,subject,delivery_ref)).fetchone()
        if not row:
            return None
        proof=json.loads(row['proof_json'])
        if proof.get('eventHash')!=hashlib.sha256(row['payload_json'].encode()).hexdigest():
            return None
        if any(proof.get(key)!=value for key,value in {'subject':subject,'host':host,'instance':instance,'token':token,
            'channel':channel,'deliveryRef':delivery_ref,'requestHash':digest}.items()):
            return None
        task=db.execute('SELECT * FROM agent_tasks WHERE task_id=? AND user_subject=?', (task_id,subject)).fetchone()
        lease=db.execute('SELECT * FROM agent_task_coordinator_leases WHERE task_id=?', (task_id,)).fetchone()
        if (not task or task['agent_host']!=host or task['origin_endpoint_id']!=proof['origin']['endpoint_id']
                or not lease or (lease['user_subject'],lease['agent_host'],lease['host_instance_id'],lease['lease_id'],lease['version'])
                !=(subject,host,instance,proof['leaseId'],proof['leaseVersion'])):
            return None
        for name in ('origin','target'):
            endpoint=db.execute('SELECT * FROM client_endpoints WHERE endpoint_id=?', (proof[name]['endpoint_id'],)).fetchone()
            if endpoint_identity(endpoint)!=proof[name]:
                return None
        event=json.loads(row['payload_json'])
        if event.get('channel')!=channel or event.get('deliveryRef')!=delivery_ref:
            return None
        # This branch returns only the original receipt, never the current task
        # summary, resource bytes, new events, a route change or an execution lease.
        return {'protocolVersion':'0.1','schemaVersion':'agentbridge.host-artifact-delivery.v1','status':'succeeded',
                'task':{'taskId':task_id},'delivery':event,'eventId':row['event_id'],'reused':True,'historyReceipt':True}
