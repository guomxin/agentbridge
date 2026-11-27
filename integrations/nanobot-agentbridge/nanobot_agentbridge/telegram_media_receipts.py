"""Private intake proofs for original Telegram media; no model or Bot side effects."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .media import DOCUMENT_FORMATS, FORMATS, MAX_FILE, validate_attachments
from .private_files import private_text
from .session_pool import open_host_session
from .telegram_identity import identifier, verify_telegram_binding
from .workspace_query_delivery import BINDING_FIELDS, pairing_ref

FAILURES = {"unsupported", "unreadable", "too_large", "expired"}
META = ("messageId", "ordinal", "fileName", "mimeType", "byteSize", "sha256", "receivedAt", "pairingRef", "failure")
SOURCE_FIELDS = (*BINDING_FIELDS, "workspaceEndpointId", "workspaceEndpointKey", "tokenFile")
TTL = 7 * 86400


def received_time(value):
    if not isinstance(value,str):
        raise ValueError("Original Telegram reception time is required")
    date=datetime.fromisoformat(value)
    if (date.tzinfo is None or date.utcoffset()!=timedelta(0) or date.microsecond
            or date.isoformat()!=value or date.timestamp()<=0 or date>datetime.now(timezone.utc)+timedelta(seconds=60)):
        raise ValueError("Invalid original Telegram reception time")
    return date


def source_key(record,body):
    route=re.fullmatch(r"telegram:([1-9][0-9]{0,15}):([1-9][0-9]{0,15})",record["endpoint_key"])
    message=body.get("messageId")
    if (not route or any(int(x)>=2**53 for x in route.groups())
            or type(message) is not int or not 0<message<2**53):
        raise ValueError("Invalid original private source")
    received_time(body.get("receivedAt"))
    return f"tg:{route[1]}:{route[2]}:{message}",route.groups()


def _path(host,record):
    return Path(host.worker_payload(record,None)["userDir"])/"coordinated-runs.sqlite3"


def _initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS telegram_media_sources(
            subject TEXT NOT NULL,request_key TEXT NOT NULL,endpoint TEXT NOT NULL,received_at TEXT NOT NULL,
            pairing TEXT NOT NULL,route_json TEXT NOT NULL,token_hash TEXT NOT NULL,
            PRIMARY KEY(subject,request_key));
        CREATE TABLE IF NOT EXISTS telegram_media_manifests(
            subject TEXT NOT NULL,request_key TEXT NOT NULL,ordinal INTEGER NOT NULL,metadata_json TEXT NOT NULL,
            PRIMARY KEY(subject,request_key,ordinal));
    """)


async def prepare_source(host,record,body):
    """Freeze the generation before downloading or admitting an attachment to a model."""
    from aiohttp import web
    key,(bot,sender)=source_key(record,body)
    if not all(isinstance(record.get(k),str) and record[k] for k in SOURCE_FIELDS):
        raise web.HTTPForbidden()
    req=host.worker_payload(record,None)
    host_type=req.get("hostType","nanobot")
    async with open_host_session(host,req,lane="telegram-media") as (session,meta,level):
        if level!="L3" or not await verify_telegram_binding(session,host.instance,record["user_subject"],meta,
                                                           record["endpoint_key"],host_type=host_type):
            raise web.HTTPForbidden()
        answer=await session.call_tool("agentbridge_host_telegram_media_source",{
            "agent_host":host_type,"endpoint_key":record["endpoint_key"],
            "source_message_id":body["messageId"],"received_at":body["receivedAt"]},meta=meta)
        value=answer.structuredContent or {}
        if (answer.isError or value.get("recorded") is not True or not pairing_ref(value.get("pairingRef"))
                or value.get("sourceEndpointId")!=record["endpoint_id"] or value.get("receivedAt")!=body["receivedAt"]):
            raise web.HTTPForbidden()
    route={k:record[k] for k in SOURCE_FIELDS}
    frozen=(record["user_subject"],key,record["endpoint_id"],body["receivedAt"],value["pairingRef"],
            json.dumps(route,sort_keys=True),hashlib.sha256(private_text(Path(record["tokenFile"])).strip().encode()).hexdigest())
    path=_path(host,record)
    if path.is_symlink():
        raise web.HTTPForbidden()
    path.touch(mode=0o600,exist_ok=True)
    with sqlite3.connect(path) as db:
        _initialize(db)
        db.execute("INSERT OR IGNORE INTO telegram_media_sources VALUES(?,?,?,?,?,?,?)",frozen)
        saved=db.execute("SELECT * FROM telegram_media_sources WHERE subject=? AND request_key=?",frozen[:2]).fetchone()
        if saved!=frozen:
            raise web.HTTPForbidden()
    return {"recorded":True,"pairingRef":value["pairingRef"],"receivedAt":body["receivedAt"]}


def manifest(body):
    metadata={k:body.get(k) for k in META}
    if (type(metadata["ordinal"]) is not int or not 0<=metadata["ordinal"]<4 or not pairing_ref(metadata["pairingRef"])):
        raise ValueError("Invalid original attachment identity")
    received_time(metadata["receivedAt"])
    failure=metadata["failure"]
    content=body.get("contentBase64")
    if failure is not None:
        if (failure not in FAILURES or content is not None or metadata["fileName"]!="附件"
                or metadata["mimeType"]!="application/octet-stream" or type(metadata["byteSize"]) is not int
                or metadata["byteSize"]!=0 or metadata["sha256"] is not None):
            raise ValueError("Invalid attachment failure fact")
    else:
        if (not isinstance(metadata["fileName"],str) or not 0<len(metadata["fileName"])<=256
                or metadata["mimeType"] not in FORMATS|DOCUMENT_FORMATS or type(metadata["byteSize"]) is not int
                or not 0<metadata["byteSize"]<=MAX_FILE or not isinstance(metadata["sha256"],str)
                or not re.fullmatch(r"[a-f0-9]{64}",metadata["sha256"])):
            raise ValueError("Invalid original attachment manifest")
        if content is not None:
            value={"type":"image" if metadata["mimeType"] in FORMATS else "document",
                   "mimeType":metadata["mimeType"],"fileName":metadata["fileName"],"content":content}
            attachment,=validate_attachments([value],telegram=True)
            if len(attachment.data)!=metadata["byteSize"] or hashlib.sha256(attachment.data).hexdigest()!=metadata["sha256"]:
                raise ValueError("Original attachment digest mismatch")
    return metadata


def _pin_source(path,record,body):
    from aiohttp import web
    key,_=source_key(record,body)
    if not path.is_file() or path.is_symlink():
        raise web.HTTPForbidden()
    with sqlite3.connect(path) as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_media_sources'").fetchone()
        row=db.execute("SELECT endpoint,received_at,pairing,route_json,token_hash FROM telegram_media_sources WHERE subject=? AND request_key=?",
                       (record["user_subject"],key)).fetchone() if exists else None
    expected_route={k:record.get(k) for k in SOURCE_FIELDS}
    if (not row or row[:3]!=(record["endpoint_id"],body["receivedAt"],body["pairingRef"])
            or json.loads(row[3])!=expected_route
            or row[4]!=hashlib.sha256(private_text(Path(record["tokenFile"])).strip().encode()).hexdigest()):
        raise web.HTTPForbidden()
    return key


def _freeze_manifest(path,record,key,metadata):
    from aiohttp import web
    original=json.dumps(metadata,sort_keys=True,ensure_ascii=False,separators=(",",":"))
    with sqlite3.connect(path) as db:
        db.execute("INSERT OR IGNORE INTO telegram_media_manifests VALUES(?,?,?,?)",
                   (record["user_subject"],key,metadata["ordinal"],original))
        row=db.execute("SELECT metadata_json FROM telegram_media_manifests WHERE subject=? AND request_key=? AND ordinal=?",
                       (record["user_subject"],key,metadata["ordinal"])).fetchone()
        if not row or row[0]!=original:
            raise web.HTTPForbidden()


def validate_receipt(value,record,metadata):
    if not isinstance(value,dict) or value.get("recorded") is not True:
        return False
    receipt=value.get("mediaReceipt")
    if not isinstance(receipt,dict):
        return False
    expected={"sourceEndpointId":record["endpoint_id"],"sourceMessageId":metadata["messageId"],
              **{k:metadata[k] for k in ("ordinal","fileName","mimeType","byteSize","sha256","receivedAt","failure")}}
    if any(receipt.get(k)!=v or type(receipt.get(k)) is not type(v) for k,v in expected.items()):
        return False
    expiry=(received_time(metadata["receivedAt"])+timedelta(seconds=TTL)).isoformat()
    return (identifier(receipt.get("entryId")) and receipt.get("expiresAt")==expiry
            and ((metadata["failure"] is None and receipt.get("state")=="stored" and isinstance(receipt.get("attachmentId"),str) and re.fullmatch(r"[A-Za-z0-9_-]{43}",receipt["attachmentId"]) is not None)
                 or (metadata["failure"] is not None and receipt.get("state")=="unavailable" and receipt.get("attachmentId") is None)))


async def _append(session,meta,record,body,host_type):
    from aiohttp import web
    metadata=manifest(body)
    names={"messageId":"source_message_id","fileName":"file_name","mimeType":"mime_type","byteSize":"byte_size",
           "receivedAt":"received_at","pairingRef":"pairing_ref"}
    args={names.get(k,k):v for k,v in metadata.items()}
    result=await session.call_tool("agentbridge_host_telegram_media_append",{
        "agent_host":host_type,"endpoint_key":record["endpoint_key"],**args,"content_base64":body.get("contentBase64")},meta=meta)
    value=result.structuredContent or {}
    if result.isError:
        raise web.HTTPServiceUnavailable()
    if value.get("recorded") is False and body.get("contentBase64") is None and metadata["failure"] is None:
        return {"recorded":False}
    if not validate_receipt(value,record,metadata):
        raise web.HTTPServiceUnavailable()
    return {"recorded":True,"mediaReceipt":value["mediaReceipt"]}


async def publish_media(host,record,body):
    """The first manifest is frozen only under the current original pairing."""
    from aiohttp import web
    metadata=manifest(body)
    path=_path(host,record)
    key=_pin_source(path,record,body)
    verified=await prepare_source(host,record,body)
    if verified["pairingRef"]!=metadata["pairingRef"]:
        raise web.HTTPForbidden()
    _freeze_manifest(path,record,key,metadata)
    req=host.worker_payload(record,None)
    async with open_host_session(host,req,lane="telegram-media") as (session,meta,level):
        if level!="L3":
            raise web.HTTPForbidden()
        return await _append(session,meta,record,body,req.get("hostType","nanobot"))


async def recover_route(host,body,*,host_type):
    """Original exact manifest/token may reconcile; an absent source returns no route."""
    from aiohttp import web
    binding=body.get("binding")
    if not isinstance(binding,dict) or set(binding)!=set(BINDING_FIELDS):
        raise web.HTTPForbidden()
    metadata=manifest(body)
    for owner in host.bindings.values():
        if owner.get("user_subject")!=binding.get("user_subject") or owner.get("endpoint_key","").startswith("telegram:"):
            continue
        path=_path(host,owner)
        if not path.is_file() or path.is_symlink():
            continue
        key,_=source_key(binding,body)
        with sqlite3.connect(path) as db:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_media_sources'").fetchone()
            row=db.execute("SELECT route_json FROM telegram_media_sources WHERE subject=? AND request_key=?",
                           (owner["user_subject"],key)).fetchone() if exists else None
            saved=db.execute("SELECT metadata_json FROM telegram_media_manifests WHERE subject=? AND request_key=? AND ordinal=?",
                             (owner["user_subject"],key,metadata["ordinal"])).fetchone() if row else None
        if not saved:
            continue
        pin=json.loads(row[0])
        if ({k:pin.get(k) for k in BINDING_FIELDS}!=binding or pin.get("workspaceEndpointId")!=owner["endpoint_id"]
                or pin.get("workspaceEndpointKey")!=owner["endpoint_key"] or pin.get("tokenFile")!=owner["tokenFile"]
                or json.loads(saved[0])!=metadata):
            raise web.HTTPForbidden()
        _pin_source(path,pin,body)
        req=host.worker_payload(owner,None)
        if host_type=="pi":
            async with host.control_sessions.borrow(owner,lane="telegram-media-receipt") as central:
                if central.level!="L3":
                    raise web.HTTPForbidden()
                return await _append(central.session,central.meta,pin,body,host_type)
        async with open_host_session(host,req,lane="telegram-media-receipt") as (session,meta,level):
            if level!="L3":
                raise web.HTTPForbidden()
            return await _append(session,meta,pin,body,host_type)
    return None
