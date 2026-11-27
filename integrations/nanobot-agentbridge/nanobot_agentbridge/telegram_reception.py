"""Fence new private intake to its original platform second and current pairing epoch."""
from datetime import datetime,timezone
import json
import re
import sqlite3

from .telegram_media_receipts import BINDING_FIELDS,_path,_pin_source,prepare_source,received_time
from .workspace_query_delivery import pairing_ref


async def scope(session,meta,record):
    from aiohttp import web
    matched=re.fullmatch(r'telegram:([1-9][0-9]*):([1-9][0-9]*)',record['endpoint_key'])
    if not matched:
        raise web.HTTPForbidden()
    result=await session.call_tool('agentbridge_host_telegram_endpoint',{
        'bot_id':matched[1],'sender_id':matched[2]},meta=meta)
    value=result.structuredContent or {}
    if (result.isError or value.get('endpointId')!=record['endpoint_id']
            or value.get('endpointKey')!=record['endpoint_key'] or value.get('conversationRef')!=record['session_key']
            or not pairing_ref(value.get('pairingRef')) or type(value.get('generationScoped')) is not bool
            or (value['generationScoped'] and (type(value.get('receptionFloor')) is not int or value['receptionFloor']<=0
                or record['credential_ref']!='tg-'+value['pairingRef']))):
        raise web.HTTPForbidden()
    return value


async def receive(host,record,body):
    from aiohttp import web
    from .session_pool import open_host_session
    req=host.worker_payload(record,None)
    async with open_host_session(host,req,lane='telegram-reception') as (session,meta,level):
        current=await scope(session,meta,record)
    if not current['generationScoped']:
        return {'accepted':True,'generationScoped':False}
    if level!='L3':
        raise web.HTTPForbidden()
    stamp=received_time(body.get('receivedAt'))
    if stamp.timestamp()<=current['receptionFloor']:
        raise web.HTTPForbidden()
    result=await prepare_source(host,record,body)
    if result['pairingRef']!=current['pairingRef']:
        raise web.HTTPForbidden()
    return {'accepted':True,'generationScoped':True,**result}


async def validate(host,record,key):
    """Called by accept/history before admitting a new role or model execution."""
    from aiohttp import web
    matched=re.fullmatch(r'tg:([1-9][0-9]*):([1-9][0-9]*):([1-9][0-9]*)',str(key))
    if not matched or record['endpoint_key']!='telegram:'+matched[1]+':'+matched[2]:
        raise web.HTTPForbidden()
    from .session_pool import open_host_session
    async with open_host_session(host,host.worker_payload(record,None),lane='telegram-reception') as (session,meta,level):
        current=await scope(session,meta,record)
    if not current['generationScoped']:
        return None
    path=_path(host,record)
    if level!='L3' or not path.is_file() or path.is_symlink():
        raise web.HTTPForbidden()
    with sqlite3.connect(path) as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_media_sources'").fetchone()
        row=db.execute('SELECT received_at,pairing FROM telegram_media_sources WHERE subject=? AND request_key=?',
                       (record['user_subject'],key)).fetchone() if exists else None
    if not row or row[1]!=current['pairingRef'] or received_time(row[0]).timestamp()<=current['receptionFloor']:
        raise web.HTTPForbidden()
    body={'messageId':int(matched[3]),'receivedAt':row[0],'pairingRef':row[1]}
    _pin_source(path,record,body)
    return body


def preflight(pilot,msg,chat,message,binding,digest):
    from agentbridge.workspace.gateway import GatewayRequestError
    from .telegram import TelegramUnavailable
    date=msg.get('date')
    stamp=None
    if type(date) is int and date>0:
        try:
            stamp=datetime.fromtimestamp(date,timezone.utc).isoformat()
        except (ValueError,OverflowError):
            pass
    try:
        value=pilot.gateway._request('telegram-received-source',{
            'binding':binding,'messageId':message,'receivedAt':stamp})
        if value.get('accepted') is not True or type(value.get('generationScoped')) is not bool:
            raise TelegramUnavailable('Original intake acknowledgement unavailable')
        if value['generationScoped']:
            if value.get('receivedAt')!=stamp or not pairing_ref(value.get('pairingRef')):
                raise TelegramUnavailable('Original intake scope unavailable')
            frozen=json.dumps({k:binding[k] for k in BINDING_FIELDS},sort_keys=True)
            with pilot.db:
                pilot.db.execute('INSERT OR IGNORE INTO telegram_inbound_sources VALUES(?,?,?,?,?,?)',
                    (pilot.bot_id,chat,message,frozen,value['pairingRef'],stamp))
                original=pilot.db.execute('SELECT binding_json,pairing,received_at FROM telegram_inbound_sources WHERE bot=? AND chat=? AND message=?',
                    (pilot.bot_id,chat,message)).fetchone()
                if tuple(original)!=(frozen,value['pairingRef'],stamp):
                    raise TelegramUnavailable('Original intake scope changed')
        return True
    except GatewayRequestError as exc:
        if exc.code not in {pilot.host_type.upper()+'_BINDING_UNAVAILABLE',pilot.host_type.upper()+'_INVALID_REQUEST'}:
            raise TelegramUnavailable('Original intake scope temporarily unavailable') from None
        with pilot.db:
            first=pilot.db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,'reception_rejected',NULL,NULL)",
                (pilot.bot_id,chat,message,binding['user_subject'],digest)).rowcount
        if first:
            pilot.deliver(chat,message,'这条消息无法确认属于当前配对，未开始处理。请在完成配对后重新发送。')
        return False
