"""Short-lived, owner/source-bound CSV delivery; no historical result API."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import base64
import csv
import hashlib
import io
import json
import re
import sqlite3
from uuid import uuid4

from bscli.core.data_source_secrets import ProtectedJsonStore
from bscli.database.independent import DatabaseRejected, _SLOTS, _SOURCE_LOCK, _SOURCE_SLOTS
import threading
import time
from bscli.database.report_lease import report_lease


class Reports:
    def __init__(self, runtime):
        self.runtime=runtime
        self.path=runtime.home/'database'/'reports.sqlite3'
        self.payloads=ProtectedJsonStore(runtime.home/'database'/'reports',purpose='agentbridge.database-csv.v1')
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, owner TEXT, source TEXT, request_key TEXT, fingerprint TEXT, status TEXT, expires TEXT, UNIQUE(owner,source,request_key))')

    def cleanup(self):
        now=datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.path)) as c:
            candidates=c.execute("SELECT id FROM reports WHERE status IN ('running','failed','expired') OR expires<?",(now,)).fetchall()
        active=False
        for (rid,) in candidates:
            with report_lease(self._lease_path(rid)) as acquired:
                if not acquired:
                    active=True
                    continue
                with closing(sqlite3.connect(self.path)) as c,c:
                    c.execute('BEGIN IMMEDIATE')
                    status,expires=c.execute('SELECT status,expires FROM reports WHERE id=?',(rid,)).fetchone()
                    if expires<now or status in ('running','failed','expired'):
                        # A free OS lease proves no executor owns this running row.
                        # Even a complete file is discarded: never guess that
                        # post-query authorization checks or final commit finished.
                        self.payloads.delete(rid)
                        c.execute('UPDATE reports SET status=? WHERE id=?',
                                  ('expired' if expires<now or status=='expired' else 'failed',rid))
        with closing(sqlite3.connect(self.path)) as c:
            known={self.payloads.path(rid).name for (rid,) in c.execute('SELECT id FROM reports')}
        cutoff=time.time()-3600
        for path in self.payloads.root.glob('*'):
            if path.is_symlink() or not path.is_file(): continue
            orphan=(path.suffix=='.bin' and path.name not in known) or (path.suffix=='.tmp' and not active)
            try:
                if orphan and path.stat().st_mtime<cutoff: path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass  # Another cleanup cycle already removed the orphan.

    def _lease_path(self, rid):
        return self.path.parent/'report-leases'/(self.payloads.path(rid).stem+'.lock')

    def export(self, owner, sid, args):
        self.cleanup()
        rid=uuid4().hex
        # Acquire before publishing running, hold through payload + terminal row.
        try:
            with report_lease(self._lease_path(rid),blocking=True):
                return self._export(owner,sid,args,rid)
        finally:
            with closing(sqlite3.connect(self.path)) as c:
                published=c.execute('SELECT 1 FROM reports WHERE id=?',(rid,)).fetchone()
            if not published:
                self._lease_path(rid).unlink(missing_ok=True)

    def _export(self, owner, sid, args, rid):
        if not isinstance(args,dict) or set(args)-{'query_capability','query_arguments','request_key','format'} or args.get('format','csv')!='csv':
            raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')
        cap=args.get('query_capability'); query=args.get('query_arguments'); key=args.get('request_key')
        if cap not in ('database.free.read','database.logs.query','database.logs.analyze') or not isinstance(query,dict) or not isinstance(key,str) or not 1<=len(key)<=128:
            raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')
        if set(query)&{'after','page_size'}: raise DatabaseRejected('DATABASE_EXPORT_FILTERS_ONLY')
        grant,record=self.runtime.snapshot(owner,cap,sid)
        export_grant,_=self.runtime.snapshot(owner,'database.report.export',sid)
        fingerprint=hashlib.sha256(json.dumps(args,sort_keys=True,ensure_ascii=True).encode()).hexdigest()
        now=datetime.now(timezone.utc)
        expires=(now+timedelta(hours=24)).isoformat()
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT id,fingerprint,status FROM reports WHERE owner=? AND source=? AND request_key=?',(owner,sid,key)).fetchone()
            if old:
                if old[1]!=fingerprint: raise DatabaseRejected('DATABASE_REPORT_KEY_CONFLICT')
                if old[2]!='ready': raise DatabaseRejected('DATABASE_REPORT_'+old[2].upper())
                return self._public(self._load(owner,sid,old[0]))
            c.execute('INSERT INTO reports VALUES (?,?,?,?,?,?,?)',(rid,owner,sid,key,fingerprint,'running',expires))
        global_slot=False; local_slot=False; slot=None
        try:
            global_slot=_SLOTS.acquire(blocking=False)
            if not global_slot: raise DatabaseRejected('DATABASE_BUSY')
            with _SOURCE_LOCK: slot=_SOURCE_SLOTS.setdefault(sid,threading.BoundedSemaphore(2))
            local_slot=slot.acquire(blocking=False)
            if not local_slot: raise DatabaseRejected('DATABASE_BUSY')
            result=self.runtime._execute(owner,cap,query,grant,sid,export=True)
            stream=io.StringIO(newline='')
            writer=csv.writer(stream)
            def cell(value):
                value='' if value is None else str(value)
                return "'"+value if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r')) else value
            writer.writerow([cell(k) for k in result['columns']])
            for row in result['rows']: writer.writerow([cell(row.get(k)) for k in result['columns']])
            data=stream.getvalue().encode('utf-8-sig')
            if len(data)>record['active']['export_bytes']: raise DatabaseRejected('DATABASE_EXPORT_LIMIT_EXCEEDED')
            self.runtime.unchanged(owner,cap,sid,grant,record)
            self.runtime.unchanged(owner,'database.report.export',sid,export_grant,record)
            payload={'report_id':rid,'owner':owner,'source_id':sid,'source_revision':record['revision'],
                'query_capability':cap,'grant':grant,'export_grant':export_grant,'expires_at':expires,
                'filename':f'{sid}-{now:%Y%m%d-%H%M%S}.csv','content_type':'text/csv; charset=utf-8',
                'content_base64':base64.b64encode(data).decode(),'byte_size':len(data),'row_count':len(result['rows']),
                'sha256':hashlib.sha256(data).hexdigest(),'queried_at':result['queried_at']}
            self.payloads.save(rid,payload)
            with closing(sqlite3.connect(self.path)) as c,c: c.execute("UPDATE reports SET status='ready' WHERE id=?",(rid,))
            self.runtime.grants.audit(owner,'database.report.export','succeeded',{'source_id':sid,'report_id':rid,'row_count':payload['row_count'],'sql_sha256':result['sql_sha256']})
            return self._public(payload)
        except Exception:
            self.payloads.delete(rid)
            with closing(sqlite3.connect(self.path)) as c,c: c.execute("UPDATE reports SET status='failed' WHERE id=?",(rid,))
            raise
        finally:
            if local_slot: slot.release()
            if global_slot: _SLOTS.release()

    def _load(self,owner,sid,rid):
        if not isinstance(rid,str) or not re.fullmatch('[0-9a-f]{32}',rid): raise DatabaseRejected('DATABASE_REPORT_DENIED')
        with closing(sqlite3.connect(self.path)) as c:
            row=c.execute('SELECT status,expires FROM reports WHERE id=? AND owner=? AND source=?',(rid,owner,sid)).fetchone()
        if not row or row[0]!='ready' or row[1]<=datetime.now(timezone.utc).isoformat(): raise DatabaseRejected('DATABASE_REPORT_UNAVAILABLE')
        try: value=self.payloads.load(rid)
        except (OSError,ValueError): raise DatabaseRejected('DATABASE_REPORT_UNAVAILABLE') from None
        grant,record=self.runtime.snapshot(owner,value['query_capability'],sid)
        export_grant,_=self.runtime.snapshot(owner,'database.report.export',sid)
        if grant!=value['grant'] or export_grant!=value['export_grant'] or record['revision']!=value['source_revision']:
            raise DatabaseRejected('DATABASE_AUTHORIZATION_CHANGED')
        return value

    def _public(self,value):
        return {**{k:value[k] for k in ('report_id','source_id','filename','expires_at','byte_size','row_count','sha256','queried_at')},
                'reportExpiresAt':value['expires_at'],'download_capability':'database.report.download','authentication_required':True,'note':'报告留存期限见 reportExpiresAt；下载入口期限见 downloadExpiresAt。入口过期而报告仍留存时，以原 report_id 调用 database.report.download 续取，无需重新查询。'}

    def download(self,owner,sid,args):
        if not isinstance(args,dict) or set(args)!={'report_id'}: raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')
        rid=args['report_id']
        if not isinstance(rid,str) or not re.fullmatch('[0-9a-f]{32}',rid): raise DatabaseRejected('DATABASE_REPORT_DENIED')
        with report_lease(self._lease_path(rid),blocking=True):
            return self._download(owner,sid,args)

    def _download(self,owner,sid,args):
        value=self._load(owner,sid,args['report_id'])
        value['download_expires_at']=min(value['expires_at'],(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat())
        self.payloads.save(value['report_id'],value)
        self.runtime.grants.audit(owner,'database.report.download','succeeded',{'source_id':sid,'report_id':value['report_id']})
        # Existing host delivery contract consumes this file and hides base64 from the model.
        return {**self._public(value),'download_expires_at':value['download_expires_at'],'downloadExpiresAt':value['download_expires_at'],'schemaVersion':'agentbridge.protected_csv_delivery.v1',
                'file':{k:value[k] for k in ('filename','content_type','content_base64')}}

    def web_download(self,owner,sid,rid):
        value=self._load(owner,sid,rid)
        if value.get('download_expires_at','')<=datetime.now(timezone.utc).isoformat():
            raise DatabaseRejected('DATABASE_DOWNLOAD_EXPIRED')
        return {'body':base64.b64decode(value['content_base64']), 'filename':value['filename'],'content_type':value['content_type']}
