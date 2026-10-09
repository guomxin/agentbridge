"""Versioned PostgreSQL sources. No business-system identity or credentials in catalogs."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from agentbridge.core.data_source_secrets import DataSourceSecretStore

LEGACY = 'taihua_primary'
TAIHUA_RELATIONS = ['analysis.' + n for n in ('users', 'departments', 'projects', 'work_logs', 'work_log_comments')]
COMMON = ['database.schema', 'database.free.read', 'database.report.export']
TAIHUA = ['database.directory', 'database.logs.query', 'database.logs.analyze',
          'database.logs.content_analyze', 'database.comments.analyze']


class SourceConflict(ValueError):
    pass


def source_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value):
        raise ValueError('数据源标识无效')
    return value


def validate(value):
    keys = {'source_id','name','description','engine','host','port','dbname','username','sslmode',
            'allowed_relations','template_pack','statement_timeout_ms','export_rows','export_bytes'}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError('数据源配置字段无效')
    source_id(value['source_id'])
    for key in ('name','host','dbname','username'):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 256 or any(ord(c)<32 for c in value[key]):
            raise ValueError('数据源名称或连接参数无效')
    if not isinstance(value['description'], str) or len(value['description']) > 2000:
        raise ValueError('数据源说明无效')
    if value['engine'] != 'postgresql' or value['template_pack'] not in ('generic','taihua_logs'):
        raise ValueError('不支持此数据库引擎或能力包')
    if value['sslmode'] not in ('verify-full','disable'):
        raise ValueError('TLS 配置无效')
    # Existing intranet source retains its approved transport. New sources require TLS.
    if value['sslmode'] == 'disable' and (value['source_id'],value['host'],value['port'],value['dbname']) != (LEGACY,'10.10.50.101',35432,'sisyphus'):
        raise ValueError('新数据源必须启用 verify-full TLS')
    for key,low,high in [('port',1,65535),('statement_timeout_ms',1000,30000),('export_rows',1,100000),('export_bytes',1024,10485760)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError('查询预算或端口无效')
    relations = value['allowed_relations']
    if not isinstance(relations,list) or not 1 <= len(relations) <= 100 or any(not isinstance(r,str) or not re.fullmatch(r'[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*',r) or r.split('.')[0] in ('pg_catalog','information_schema') for r in relations):
        raise ValueError('请填写完整限定的开放表或视图（schema.table）')
    if value['template_pack']=='taihua_logs' and not set(TAIHUA_RELATIONS).issubset(relations):
        raise ValueError('日志系统能力包需要五个 analysis 视图')
    return {**value, 'allowed_relations':sorted(set(relations))}


class Sources:
    def __init__(self, home, *, initialize=True):
        # initialize=False is for a catalog already initialized by its owner.
        # Keep key-file validation per handle; never cache grants or source records.
        self.home=Path(home)
        self.path=self.home/'database'/'catalog.sqlite3'
        if initialize:
            self.path.parent.mkdir(parents=True,exist_ok=True)
        self.secrets=DataSourceSecretStore(self.home/'database'/'credentials')
        if initialize:
            self._initialize()

    def _initialize(self):
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS source_audit (id TEXT PRIMARY KEY, source TEXT, actor TEXT, reason TEXT, action TEXT, at TEXT, revision INTEGER)')

    def migrate(self):
        """Idempotent import; existing registration always wins, including disabled sources."""
        if self.get(LEGACY, required=False): return
        path=self.home/'analytics'/'source.json'
        if not path.exists(): return
        from agentbridge.database.legacy_source import LegacySourceConfig
        old=LegacySourceConfig.load(path)
        old.validate()
        value=validate(dict(source_id=LEGACY,name='日志库',description='日志、人员、部门、项目及评论',engine='postgresql',
            host=old.host,port=old.port,dbname=old.dbname,username=old.username,sslmode=old.sslmode,
            allowed_relations=TAIHUA_RELATIONS,template_pack='taihua_logs',statement_timeout_ms=10000,export_rows=10000,export_bytes=10485760))
        ref=f'{LEGACY}:{uuid4().hex}'
        self.secrets.save(ref,DataSourceSecretStore(self.home/'analytics'/'credentials').load(f'{LEGACY}:{old.credential_version}'))
        record={'source_id':LEGACY,'revision':1,'state':'enabled' if old.enabled and old.privileges_reviewed and old.single_instance else 'disabled',
                'active':{**value,'credential_ref':ref},'draft':{**value,'credential_ref':ref},'draft_revision':1,
                'preflight':{'status':'migrated','revision':1},'migration':'legacy-v1'}
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('INSERT OR IGNORE INTO sources VALUES (?,?)',(LEGACY,json.dumps(record)))

    def get(self, sid, *, required=True):
        source_id(sid)
        with closing(sqlite3.connect(self.path)) as c:
            row=c.execute('SELECT record FROM sources WHERE id=?',(sid,)).fetchone()
        if not row:
            if required: raise ValueError('数据源不存在')
            return None
        return json.loads(row[0])

    def list(self):
        self.migrate()
        with closing(sqlite3.connect(self.path)) as c:
            return [json.loads(r[0]) for r in c.execute('SELECT record FROM sources ORDER BY id')]

    @staticmethod
    def public(record):
        result=json.loads(json.dumps(record))
        for key in ('active','draft'):
            if result.get(key): result[key].pop('credential_ref',None)
        return result

    @staticmethod
    def capabilities(config):
        return COMMON + (TAIHUA if config['template_pack']=='taihua_logs' else [])

    def write(self, sid, action, *, revision, actor, reason, config=None, password=None):
        source_id(sid)
        if type(revision) is not int or revision<0 or not isinstance(reason,str) or not reason.strip():
            raise ValueError('请提供版本与变更原因')
        old=self.get(sid,required=False)
        if (old or {}).get('revision',0)!=revision: raise SourceConflict('数据源已变化，请刷新后重试')
        new=json.loads(json.dumps(old)) if old else {'source_id':sid,'revision':0,'state':'draft','active':None}
        if action=='save':
            config=validate(config)
            if config['source_id']!=sid: raise ValueError('数据源标识不一致')
            ref=(new.get('draft') or {}).get('credential_ref')
            if password is not None:
                if not isinstance(password,str) or not password or len(password)>4096: raise ValueError('凭据无效')
                ref=f'{sid}:{uuid4().hex}'
                self.secrets.save(ref,{'password':password})
            if not ref: raise ValueError('请安全配置只读数据库密码')
            new.update(draft={**config,'credential_ref':ref},draft_revision=revision+1,preflight={'status':'pending'})
        elif action=='preflight':
            if not old: raise ValueError('请先保存草稿')
            try:
                metadata=preflight(self,old['draft'])
                new['preflight']={'status':'passed','revision':old['draft_revision'],'objects':metadata}
            except Exception as exc:
                from agentbridge.database.independent import DatabaseRejected
                new['preflight']={'status':'failed','revision':old['draft_revision'],
                                  'code':str(exc) if isinstance(exc,DatabaseRejected) else 'DATABASE_PREFLIGHT_FAILED'}
        elif action=='enable':
            if not old or old['preflight'].get('status')!='passed' or old['preflight'].get('revision')!=old['draft_revision']:
                raise ValueError('当前草稿尚未通过只读预检')
            new.update(active=old['draft'],state='enabled')
        elif action=='disable':
            if not old: raise ValueError('数据源不存在')
            new['state']='disabled'
        else: raise ValueError('数据源操作无效')
        new['revision']=revision+1
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT record FROM sources WHERE id=?',(sid,)).fetchone()
            if (json.loads(row[0]) if row else {}).get('revision',0)!=revision: raise SourceConflict('数据源已变化，请刷新后重试')
            c.execute('INSERT INTO sources VALUES (?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',(sid,json.dumps(new)))
            c.execute('INSERT INTO source_audit VALUES (?,?,?,?,?,?,?)',(uuid4().hex,sid,actor,reason,action,datetime.now(timezone.utc).isoformat(),new['revision']))
        return self.public(new)


def connect(sources, config):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(host=config['host'],port=config['port'],dbname=config['dbname'],user=config['username'],
        password=sources.secrets.load(config['credential_ref'])['password'],sslmode=config['sslmode'],connect_timeout=5,
        options=f"-c default_transaction_read_only=on -c statement_timeout={config['statement_timeout_ms']} -c lock_timeout=1000 -c idle_in_transaction_session_timeout=10000 -c work_mem=4096 -c search_path=pg_catalog -c timezone=Asia/Shanghai",row_factory=dict_row)


def check_role(conn):
    from agentbridge.database.postgres_policy import ROLE_SQL
    from agentbridge.database.independent import DatabaseRejected
    row=conn.execute(ROLE_SQL).fetchone()
    if not row or not all(row[k] for k in ('readonly','repeatable','default_readonly')) or any(row[k] for k in ('privileged','writable','sequence_update','sequence_usage','temp')):
        raise DatabaseRejected('DATABASE_ROLE_REJECTED')


def preflight(sources,config):
    from agentbridge.database.independent import DatabaseRejected
    with connect(sources,config) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        check_role(conn)
        rows=conn.execute("SELECT table_schema,table_name,column_name,data_type FROM information_schema.columns WHERE (table_schema||'.'||table_name)=ANY(%s) ORDER BY table_schema,table_name,ordinal_position",(config['allowed_relations'],)).fetchall()
        found={r['table_schema']+'.'+r['table_name'] for r in rows}
        if found!=set(config['allowed_relations']): raise DatabaseRejected('DATABASE_RELATION_UNREADABLE')
        for relation in config['allowed_relations']:
            if not conn.execute('SELECT has_table_privilege(%s,\'SELECT\') AS ok',(relation,)).fetchone()['ok']:
                raise DatabaseRejected('DATABASE_RELATION_UNREADABLE')
        if config['template_pack']=='taihua_logs':
            from agentbridge.database.content import INPUT_SCHEMAS,compile_query
            for cap in ('database.logs.query','database.logs.analyze','database.logs.content_analyze','database.comments.analyze'):
                plan=compile_query(cap,{'start_date':'2000-01-01','end_date_exclusive':'2000-01-02'})
                conn.execute('EXPLAIN '+plan.statement,plan.params)
            conn.execute('EXPLAIN SELECT id,username,fullname,dept_id,status FROM analysis.users')
            conn.execute('EXPLAIN SELECT id,name,status FROM analysis.projects')
            conn.execute('EXPLAIN SELECT id,name,status FROM analysis.departments')
        conn.rollback()
        return rows
