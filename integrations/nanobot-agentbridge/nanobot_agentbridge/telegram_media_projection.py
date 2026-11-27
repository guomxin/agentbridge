"""Durably project original media independently of model admission and public text."""
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
import time

from .telegram_media_receipts import BINDING_FIELDS, TTL, manifest, received_time, validate_receipt
from .workspace_query_delivery import pairing_ref


class MediaProjection:
    def __init__(self,pilot):
        self.pilot=pilot
        self.executor=None
        self.pending=None
        pilot.db.executescript("""
            CREATE TABLE IF NOT EXISTS telegram_media_intakes(
                bot INTEGER,chat INTEGER,message INTEGER,binding_json TEXT NOT NULL,received_at TEXT NOT NULL,
                pairing TEXT,status TEXT NOT NULL,PRIMARY KEY(bot,chat,message));
            CREATE TABLE IF NOT EXISTS telegram_media_projection(
                bot INTEGER,chat INTEGER,message INTEGER,ordinal INTEGER,metadata_json TEXT NOT NULL,
                content BLOB,state TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,probe INTEGER NOT NULL DEFAULT 0,receipt_json TEXT,
                PRIMARY KEY(bot,chat,message,ordinal));
        """)
        # A platform file may be downloaded again, but an interrupted model handoff
        # is never replayed. Preserve its independent reception and any original bytes.
        with pilot.db:
            rows=pilot.db.execute("""SELECT i.* FROM telegram_media_intakes i JOIN messages m USING(bot,chat,message)
                WHERE i.bot=? AND m.state='reading_media'""",(pilot.bot_id,)).fetchall()
            for row in rows:
                if row["pairing"] and not pilot.db.execute("SELECT 1 FROM telegram_media_projection WHERE bot=? AND chat=? AND message=?",
                                                        (row["bot"],row["chat"],row["message"])).fetchone():
                    self.stage(row["chat"],row["message"],failure="unreadable")
                pilot.db.execute("UPDATE messages SET state='media_interrupted' WHERE bot=? AND chat=? AND message=?",
                                 (row["bot"],row["chat"],row["message"]))

    def begin(self,chat,message,binding,received_at):
        received_time(received_at)
        frozen=json.dumps({k:binding[k] for k in BINDING_FIELDS},sort_keys=True)
        self.pilot.db.execute("INSERT INTO telegram_media_intakes VALUES(?,?,?,?,?,NULL,'received')",
                              (self.pilot.bot_id,chat,message,frozen,received_at))

    def source(self,chat,message):
        row=self.pilot.db.execute("SELECT * FROM telegram_media_intakes WHERE bot=? AND chat=? AND message=?",
                                  (self.pilot.bot_id,chat,message)).fetchone()
        if not row:
            raise ValueError("Missing original media reception")
        value=self.pilot.gateway._request("telegram-media-source",{"binding":json.loads(row["binding_json"]),
              "messageId":message,"receivedAt":row["received_at"]})
        if (value.get("recorded") is not True or value.get("receivedAt")!=row["received_at"]
                or not pairing_ref(value.get("pairingRef")) or row["pairing"] not in (None,value["pairingRef"])):
            raise ValueError("Original media pairing was not confirmed")
        with self.pilot.db:
            self.pilot.db.execute("UPDATE telegram_media_intakes SET pairing=?,status='pinned' WHERE bot=? AND chat=? AND message=?",
                                  (value["pairingRef"],self.pilot.bot_id,chat,message))
        return value["pairingRef"]

    def stage(self,chat,message,*,attachment=None,blob=None,failure=None,ordinal=0):
        row=self.pilot.db.execute("SELECT * FROM telegram_media_intakes WHERE bot=? AND chat=? AND message=?",
                                  (self.pilot.bot_id,chat,message)).fetchone()
        if not row or not pairing_ref(row["pairing"]):
            raise ValueError("Media must retain its original paired source")
        metadata={"messageId":message,"ordinal":ordinal,"receivedAt":row["received_at"],"pairingRef":row["pairing"],"failure":failure}
        if failure is None:
            metadata.update(fileName=attachment["fileName"],mimeType=attachment["mimeType"],
                            byteSize=len(blob),sha256=hashlib.sha256(blob).hexdigest())
        else:
            metadata.update(fileName="附件",mimeType="application/octet-stream",byteSize=0,sha256=None)
        manifest(metadata)
        raw=json.dumps(metadata,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        with self.pilot.db:
            self.pilot.db.execute("""INSERT OR IGNORE INTO telegram_media_projection
                (bot,chat,message,ordinal,metadata_json,content,state) VALUES(?,?,?,?,?,?,'pending')""",
                (self.pilot.bot_id,chat,message,ordinal,raw,blob))
            old=self.pilot.db.execute("SELECT metadata_json FROM telegram_media_projection WHERE bot=? AND chat=? AND message=? AND ordinal=?",
                                     (self.pilot.bot_id,chat,message,ordinal)).fetchone()
            if old[0]!=raw:
                raise ValueError("Original media manifest changed")

    def _finish(self,row,state,receipt=None):
        with self.pilot.db:
            self.pilot.db.execute("""UPDATE telegram_media_projection SET state=?,content=NULL,receipt_json=?
                WHERE bot=? AND chat=? AND message=? AND ordinal=?""",
                (state,json.dumps(receipt,sort_keys=True) if receipt else None,row["bot"],row["chat"],row["message"],row["ordinal"]))

    def drain(self):
        if self.pending:
            row,body,future=self.pending
            if not future.done():
                return
            self.pending=None
            try:
                result=future.result()
                record=body["binding"]
                metadata=manifest(body)
                if validate_receipt(result,record,metadata):
                    self._finish(row,"acknowledged",result["mediaReceipt"])
                elif (result.get("recorded") is False and body.get("contentBase64") is None
                      and metadata["failure"] is None):
                    if time.time()>=received_time(metadata["receivedAt"]).timestamp()+TTL or row["content"] is None:
                        self._finish(row,"expired")
                    else:
                        with self.pilot.db:
                            self.pilot.db.execute("UPDATE telegram_media_projection SET probe=0 WHERE bot=? AND chat=? AND message=? AND ordinal=?",
                                                  (row["bot"],row["chat"],row["message"],row["ordinal"]))
            except (RuntimeError,OSError,ValueError,TypeError):
                # An ambiguous append is reconciled by original manifest before
                # sending the same bytes again. Never redownload or rerun a model.
                pass
        row=self.pilot.db.execute("""SELECT p.*,i.binding_json FROM telegram_media_projection p JOIN telegram_media_intakes i USING(bot,chat,message)
            WHERE p.bot=? AND p.state='pending' AND p.next_attempt<=? ORDER BY p.rowid LIMIT 1""",
            (self.pilot.bot_id,time.time())).fetchone()
        if not row:
            return
        if row["attempt"]>=24:
            self._finish(row,"delivery_unknown")
            return
        metadata=json.loads(row["metadata_json"])
        body={"binding":json.loads(row["binding_json"]),**metadata}
        if not row["probe"] and row["content"] is not None and time.time()<received_time(metadata["receivedAt"]).timestamp()+TTL:
            body["contentBase64"]=base64.b64encode(row["content"]).decode("ascii")
        with self.pilot.db:
            self.pilot.db.execute("""UPDATE telegram_media_projection SET probe=1,attempt=attempt+1,next_attempt=?
                WHERE bot=? AND chat=? AND message=? AND ordinal=?""",
                (time.time()+min(300,5*2**min(row["attempt"],6)),row["bot"],row["chat"],row["message"],row["ordinal"]))
        if self.executor is None:
            self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix="telegram-media")
        future=self.executor.submit(self.pilot.gateway._request,"telegram-media-append",body,timeout=20)
        self.pending=(dict(row),body,future)

    def close(self):
        if self.executor:
            self.executor.shutdown(wait=True,cancel_futures=True)
