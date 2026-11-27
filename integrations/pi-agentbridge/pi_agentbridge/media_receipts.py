"""Settle an original image claim after unpairing, without permitting another send."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from starlette.exceptions import HTTPException
from nanobot_agentbridge.private_files import private_text

BINDING_FIELDS = ('user_subject', 'endpoint_id', 'endpoint_key', 'session_key', 'credential_ref')
ROUTE_FIELDS = (*BINDING_FIELDS, 'workspaceEndpointId', 'workspaceEndpointKey', 'tokenFile')
IDENTIFIER = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def record_claim(host, record, claim):
    if claim.get('available') is not True:
        return
    delivery, attempt, ordinal = (claim.get(k) for k in ('deliveryId', 'attempt', 'ordinal'))
    pairing = record.get('credential_ref', '').removeprefix('tg-')
    if (not isinstance(delivery, str) or not IDENTIFIER.fullmatch(delivery)
            or type(attempt) is not int or attempt < 1 or type(ordinal) is not int or not 0 <= ordinal < 4
            or not re.fullmatch(r'[0-9a-f]{64}', pairing)
            or any(not isinstance(record.get(k), str) or not record[k] for k in ROUTE_FIELDS)):
        raise HTTPException(403)
    path = Path(host.worker_payload(record, None)['userDir']) / 'coordinated-runs.sqlite3'
    pin = json.dumps({k:record[k] for k in ROUTE_FIELDS}, sort_keys=True)
    token_hash = hashlib.sha256(private_text(Path(record['tokenFile'])).strip().encode()).hexdigest()
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS pi_workspace_image_claims(
            delivery TEXT PRIMARY KEY, attempt INTEGER NOT NULL, ordinal INTEGER NOT NULL,
            pairing TEXT NOT NULL, route_json TEXT NOT NULL, token_hash TEXT NOT NULL)''')
        db.execute('''INSERT INTO pi_workspace_image_claims VALUES(?,?,?,?,?,?)
            ON CONFLICT(delivery) DO UPDATE SET attempt=excluded.attempt
            WHERE attempt<=excluded.attempt AND ordinal=excluded.ordinal AND pairing=excluded.pairing
                AND route_json=excluded.route_json AND token_hash=excluded.token_hash''',
            (delivery,attempt,ordinal,pairing,pin,token_hash))
        saved = db.execute('SELECT attempt,ordinal,pairing,route_json,token_hash FROM pi_workspace_image_claims WHERE delivery=?',
                           (delivery,)).fetchone()
        if saved != (attempt,ordinal,pairing,pin,token_hash):
            raise HTTPException(403)


async def acknowledge(host, body):
    """The receipt route has neither a pairing lookup nor an issuing operation."""
    if (set(body) - {'instance','binding','action','delivery_id','attempt','receipt','pairingRef'}
            or body.get('action') != 'ack' or body.get('instance') != host.instance
            or not isinstance(body.get('binding'), dict) or set(body['binding']) != set(BINDING_FIELDS)
            or not isinstance(body.get('delivery_id'), str) or not IDENTIFIER.fullmatch(body['delivery_id'])
            or not isinstance(body.get('pairingRef'), str) or not re.fullmatch(r'[0-9a-f]{64}', body['pairingRef'])
            or type(body.get('attempt')) is not int or body['attempt'] < 1
            or (body.get('receipt') is not None and
                (type(body['receipt']) is not int or not 0 < body['receipt'] < 2**53))):
        raise HTTPException(403)
    binding = body['binding']
    for original in host.bindings.values():
        if original.get('user_subject') != binding['user_subject'] or original.get('endpoint_key','').startswith('telegram:'):
            continue
        path = Path(host.worker_payload(original, None)['userDir']) / 'coordinated-runs.sqlite3'
        if not path.is_file():
            continue
        with sqlite3.connect(path) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pi_workspace_image_claims'").fetchone()
            row = db.execute('SELECT attempt,pairing,route_json,token_hash FROM pi_workspace_image_claims WHERE delivery=?',
                             (body.get('delivery_id'),)).fetchone() if exists else None
        if not row or row[:2] != (body['attempt'], body['pairingRef']):
            continue
        pin = json.loads(row[2])
        if ({k:pin[k] for k in BINDING_FIELDS} != binding or pin['workspaceEndpointId'] != original['endpoint_id']
                or pin['workspaceEndpointKey'] != original['endpoint_key'] or pin['tokenFile'] != original['tokenFile']
                or hashlib.sha256(private_text(Path(original['tokenFile'])).strip().encode()).hexdigest() != row[3]):
            continue
        async with host.control_sessions.borrow(original, lane='workspace-image-receipt') as central:
            if central.level != 'L3':
                raise HTTPException(403)
            result = await central.session.call_tool('agentbridge_host_telegram_workspace_media', {
                'action':'ack','endpoint_key':binding['endpoint_key'],'pairing_ref':body['pairingRef'],
                'delivery_id':body['delivery_id'],'attempt':body['attempt'],
                **({'receipt':body['receipt']} if body.get('receipt') is not None else {})}, meta=central.meta)
            value = result.structuredContent
            if result.isError or not isinstance(value, dict) or type(value.get('recorded')) is not bool:
                raise HTTPException(502)
            return {'recorded':value['recorded']}
    raise HTTPException(403)
