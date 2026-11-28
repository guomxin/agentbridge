"""Exact history receipt reconciliation, without current pairing or new publication."""
import hashlib
import json
import sqlite3

from .telegram_media_receipts import BINDING_FIELDS, _path, _pin_source


def manifest(body):
    return json.dumps({k: body.get(k) for k in (
        'messageId', 'role', 'runId', 'projectionMode', 'sourceState')}
        | {'textSha256': hashlib.sha256(body['text'].encode()).hexdigest()},
        sort_keys=True, separators=(',', ':'))


def freeze(host, record, key, body, source):
    """Only called after current source, original run and public text validation."""
    from aiohttp import web
    if source is None:
        return  # A legacy row must never acquire invented historical authority.
    path = _path(host, record)
    _pin_source(path, record, source)
    frozen = manifest(body)
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS telegram_history_manifests(
            subject TEXT NOT NULL, request_key TEXT NOT NULL, role TEXT NOT NULL, manifest TEXT NOT NULL,
            PRIMARY KEY(subject,request_key,role))''')
        db.execute('INSERT OR IGNORE INTO telegram_history_manifests VALUES(?,?,?,?)',
                   (record['user_subject'], key, body['role'], frozen))
        previous = db.execute('SELECT manifest FROM telegram_history_manifests WHERE subject=? AND request_key=? AND role=?',
                              (record['user_subject'], key, body['role'])).fetchone()
        if not previous or previous[0] != frozen:
            raise web.HTTPForbidden()


async def recover_route(host, body, *, host_type):
    from aiohttp import web
    from .session_pool import open_host_session
    from .telegram_media_receipts import source_key
    from .telegram_timeline import _central_publish
    from .workspace_query_delivery import ensure_web_owners
    binding = body.get('binding')
    if (not isinstance(binding, dict) or set(binding) != set(BINDING_FIELDS)
            or body.get('projectionMode') not in {'telegram_received_v2', 'host_result_v2'}
            or body.get('role') not in {'user', 'assistant'} or not isinstance(body.get('text'), str)):
        raise web.HTTPForbidden()
    for owner in await ensure_web_owners(host, binding['user_subject']):
        path = _path(host, owner)
        if not path.is_file() or path.is_symlink():
            continue
        # The platform timestamp is read from the already frozen row, never invented by the caller.
        key = f"tg:{binding['endpoint_key'].removeprefix('telegram:')}:{body.get('messageId')}"
        with sqlite3.connect(path) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'telegram_media_sources', 'telegram_history_manifests'} <= tables:
                continue
            source = db.execute('SELECT received_at,pairing,route_json FROM telegram_media_sources WHERE subject=? AND request_key=?',
                                (owner['user_subject'], key)).fetchone()
            saved = db.execute('SELECT manifest FROM telegram_history_manifests WHERE subject=? AND request_key=? AND role=?',
                               (owner['user_subject'], key, body['role'])).fetchone()
        if not source or not saved:
            continue
        pin = json.loads(source[2])
        if ({k: pin.get(k) for k in BINDING_FIELDS} != binding or pin.get('workspaceEndpointId') != owner['endpoint_id']
                or pin.get('workspaceEndpointKey') != owner['endpoint_key'] or pin.get('tokenFile') != owner['tokenFile']
                or saved[0] != manifest(body)):
            raise web.HTTPForbidden()
        original = {'messageId': body['messageId'], 'receivedAt': source[0], 'pairingRef': source[1]}
        source_key(pin, original)
        _pin_source(path, pin, original)
        if host_type == 'pi':
            async with host.control_sessions.borrow(owner, lane='telegram-history-receipt') as central:
                if central.level != 'L3':
                    raise web.HTTPForbidden()
                return await _central_publish(central.session, central.meta, host.instance, pin, body, host_type, receipt_only=True, source=original)
        async with open_host_session(host, host.worker_payload(owner, None), lane='telegram-history-receipt') as (session, meta, level):
            if level != 'L3':
                raise web.HTTPForbidden()
            return await _central_publish(session, meta, host.instance, pin, body, host_type, receipt_only=True, source=original)
    return {'recorded': False}
