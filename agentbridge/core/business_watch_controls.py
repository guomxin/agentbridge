"""Audited local maintenance controls for observations, separate from write pauses."""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import sqlite3
from uuid import uuid4


class BusinessWatchControlConflict(ValueError):
    code = 'WATCH_CONTROL_CONFLICT'


class BusinessWatchControls:
    def __init__(self,db_path,*,initialize=True):
        self.db_path=Path(db_path)
        if not initialize:
            return
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS business_watch_controls (
                    scope TEXT NOT NULL,target TEXT NOT NULL,paused INTEGER NOT NULL,
                    version INTEGER NOT NULL,reason TEXT NOT NULL,actor TEXT NOT NULL,updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope,target)
                );
                CREATE TABLE IF NOT EXISTS business_watch_control_events (
                    event_id TEXT PRIMARY KEY,scope TEXT NOT NULL,target TEXT NOT NULL,
                    paused INTEGER NOT NULL,version INTEGER NOT NULL,reason TEXT NOT NULL,
                    actor TEXT NOT NULL,created_at TEXT NOT NULL
                );
            ''')

    @contextmanager
    def _connect(self,*,read_only=False):
        db=(sqlite3.connect(self.db_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=30)
            if read_only else sqlite3.connect(self.db_path,timeout=30))
        db.row_factory=sqlite3.Row
        try:
            if read_only: db.execute('PRAGMA query_only=ON')
            yield db
            if not read_only: db.commit()
        except Exception:
            db.rollback();raise
        finally: db.close()

    def is_paused(self,subject,*,connection=None):
        if connection is None:
            with self._connect() as db:
                return self.is_paused(subject,connection=db)
        return bool(connection.execute("SELECT 1 FROM business_watch_controls WHERE paused=1 AND "
            "((scope='global' AND target='*') OR (scope='system' AND target='oa') OR (scope='user' AND target=?)) LIMIT 1",
            (subject,)).fetchone())

    def set(self,*,scope,target,paused,expected_version,reason,actor):
        if (not isinstance(scope,str) or scope not in {'global','system','user'} or not isinstance(target,str) or not target.strip()
            or target!=target.strip()
            or len(target)>256 or any(ord(c)<32 for c in target)
            or (scope=='global' and target!='*') or (scope=='system' and target!='oa')
            or type(paused) is not bool or type(expected_version) is not int or expected_version<0
            or not isinstance(reason,str) or not 3<=len(reason.strip())<=500
            or not isinstance(actor,str) or not actor.strip() or len(actor)>200 or any(ord(c)<32 for c in actor)):
            raise ValueError('关注运行控制参数不合法。')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now=datetime.now(timezone.utc).isoformat()
            old=db.execute('SELECT version FROM business_watch_controls WHERE scope=? AND target=?',(scope,target)).fetchone()
            if (old['version'] if old else 0)!=expected_version:
                raise BusinessWatchControlConflict('运行设置已变化，请读取当前版本后再试。')
            version=expected_version+1
            db.execute('INSERT INTO business_watch_controls VALUES (?,?,?,?,?,?,?) ON CONFLICT(scope,target) DO UPDATE SET '
                'paused=excluded.paused,version=excluded.version,reason=excluded.reason,actor=excluded.actor,updated_at=excluded.updated_at',
                (scope,target,int(paused),version,reason.strip(),actor,now))
            db.execute('INSERT INTO business_watch_control_events VALUES (?,?,?,?,?,?,?,?)',
                (str(uuid4()),scope,target,int(paused),version,reason.strip(),actor,now))
            predicate,parameters=("1=1",[]) if scope!='user' else ('user_subject=?',[target])
            if paused:
                db.execute('UPDATE business_watches SET version=version+1,next_check_at=NULL,updated_at=? '
                    "WHERE state='active' AND "+predicate,[now,*parameters])
            else:
                # A remaining global/system/user pause must continue to hold.
                db.execute('UPDATE business_watches SET version=version+1,next_check_at=?,updated_at=? '
                    "WHERE state='active' AND expires_at>? AND "+predicate+
                    " AND NOT EXISTS (SELECT 1 FROM business_watch_controls p WHERE p.paused=1 AND "
                    "((p.scope='global' AND p.target='*') OR (p.scope='system' AND p.target='oa') OR "
                    "(p.scope='user' AND p.target=business_watches.user_subject)))",[now,now,now,*parameters])
            return {'scope':scope,'target':target,'paused':paused,'version':version,'reason':reason.strip(),'actor':actor,'updated_at':now}

    def summary(self):
        with self._connect(read_only=True) as db:
            now=datetime.now(timezone.utc).isoformat()
            tables={r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'business_watches' not in tables:
                raise ValueError('业务关注账本尚未初始化。')
            states={r['effective_state']:r['n'] for r in db.execute("SELECT CASE WHEN state IN ('active','paused') "
                "AND expires_at<=? THEN 'expired' ELSE state END AS effective_state,count(*) n "
                "FROM business_watches GROUP BY effective_state",(now,))}
            controls=[dict(r) for r in db.execute('SELECT * FROM business_watch_controls ORDER BY scope,target')] if 'business_watch_controls' in tables else []
            predicate=(" AND NOT EXISTS (SELECT 1 FROM business_watch_controls p WHERE p.paused=1 AND "
                "((p.scope='global' AND p.target='*') OR (p.scope='system' AND p.target='oa') OR "
                "(p.scope='user' AND p.target=business_watches.user_subject)))") if controls else ''
            due=db.execute("SELECT count(*) FROM business_watches WHERE state='active' AND allow_mark_read=1 "
                "AND next_check_at<=? AND expires_at>?"+predicate,(now,now)).fetchone()[0]
            checks={r['status']:r['n'] for r in db.execute('SELECT status,count(*) n FROM business_watch_checks GROUP BY status')} if 'business_watch_checks' in tables else {}
        return {'states':states,'due':due,'checks':checks,'controls':controls}
