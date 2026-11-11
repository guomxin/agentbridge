"""Real Workspace/query stores with only history source readers synthesized.

No model or real OA runs. Stored query projections are not assistant answers.
The private oracle never enters prompts, source rows or the browser workspace.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlsplit

from agentbridge.browser.read_budget import ReadBudgetExceeded
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.authorization_fixtures import grant_permissions
from tests.support.interaction_acceptance import ReadOnlyGateway, free_port
from tests.test_workspace import PASSWORD, _create_account

ROOT = Path(__file__).resolve().parents[2]
OWNER, OTHER = 'single-query-alice', 'single-query-bob'
PROMPT = '查我最近一条还没结束的OA已发事项'
CASES = ('latest', 'empty', 'unknown', 'failure', 'all-ended', 'continue')
ALLOWED = frozenset({'oa.workflow.history.query', 'oa.workflow.history.next'})


def private_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refuse a symlink evidence file')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    path.chmod(0o600)


def denied(*_args, **_kwargs):
    raise PermissionError('Synthetic fixture forbids real downstream and business execution')


def source_identity():
    paths = ['agentbridge/core/oa_history_query_service.py', 'agentbridge/core/oa_history_queries.py',
        'agentbridge/core/central_service.py', 'agentbridge/mcp/central.py',
        'agentbridge/workspace/oa_history.py', 'agentbridge/workspace/static/workspace_oa_history.mjs',
        'tests/support/single_query_acceptance.py', 'scripts/serve-single-query-acceptance.py']
    return {'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'files_sha256': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in paths if (ROOT/name).is_file()}}


class OfflineWorker:
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def restore_session_state(self, state): self.state = deepcopy(state)
    def capture_session_state(self): return deepcopy(self.state)


class SyntheticHistorySource:
    def __init__(self, home, stop):
        self.home, self.stop = home, stop
        self.local, self.lock = threading.local(), threading.Lock()
        self.calls, self.rows = [], {}
        anchor = datetime.now(timezone.utc)-timedelta(days=1)
        for case_index, case in enumerate(CASES):
            current = []
            for rank in range(224 if case != 'empty' else 0):
                identity = str(100000+case_index*1000+rank)
                running = case != 'all-ended' and 5 <= rank < 34
                unknown = case == 'unknown' and rank == 0
                state = 'unknown' if unknown else 'in_progress' if running else 'ended'
                current.append({'affair_id':identity,'title':f'合成 OA 已发事项 {rank+1:03d}', 'category':'合成事项',
                    'initiated_at':(anchor-timedelta(hours=rank)).isoformat(), 'processed_at':None,
                    'workflow_state':state,'workflow_state_text':{'unknown':'未核实','in_progress':'流转中','ended':'已结束'}[state],
                    'current_nodes':None if running or unknown else [],
                    'current_handlers':['合成经办人甲','合成协办人乙'] if running else None if unknown else [],
                    'observed_at':anchor.isoformat(),'unavailable_fields':['current_nodes'] if running else
                        ['workflow_state','current_nodes','current_handlers'] if unknown else [],
                    '_source':{'affair_id':identity,'summary_id':str(int(identity)+1000000),
                        'process_id':str(int(identity)+2000000),'case_id':str(int(identity)+3000000)}})
            # Deliberately oldest first: source page order cannot prove latest.
            self.rows[case] = list(reversed(current))
        self.by_id = {row['affair_id']:(case,row) for case,rows in self.rows.items() for row in rows}

    def marker(self, name):
        path = self.home/name
        if not path.exists() and not path.is_symlink(): return None
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32:
            raise ValueError('Synthetic controls require small regular files')
        value = path.read_text().strip(); path.unlink(); return value

    def read(self, kind, *, case, page=None, affair=None):
        entry = {'kind':kind,'case':case,'page':page,'affair_id':affair,'status':'started','returned_rows':0}
        started = time.monotonic()
        with self.lock: self.calls.append(entry)
        try:
            if self.marker('fail-next-single-query-read') is not None:
                raise ValueError('Synthetic source unavailable')
            if self.marker('timeout-next-single-query-read') is not None:
                raise ReadBudgetExceeded('Synthetic one-call source deadline')
            delay = self.marker('delay-next-single-query-read')
            if delay is not None:
                seconds = float(delay or 2)
                if not 0 <= seconds <= 5: raise ValueError('Synthetic delay must be 0..5 seconds')
                self.stop.wait(seconds)
            if case == 'failure': raise ValueError('Synthetic source unavailable')
            if kind == 'page':
                rows = self.rows[case]; items = deepcopy(rows[(page-1)*50:page*50])
                result = {'total':len(rows),'pages':(len(rows)+49)//50,'page':page,'size':50,'items':items}
                entry['returned_rows'] = len(items)
                entry['row_ids'] = [row['affair_id'] for row in items]
            else:
                row = self.by_id[affair][1]
                if row['workflow_state'] == 'unknown': raise ValueError('Synthetic current state remains unverified')
                result = {key:deepcopy(row[key]) for key in ('workflow_state','workflow_state_text','current_handlers','observed_at')}
                result.update(current_nodes=[{'name':'合成财务复核','handlers':['合成经办人甲']},
                    {'name':'合成项目核对','handlers':['合成协办人乙']}],unavailable_fields=[])
            entry['status'] = 'succeeded'; return result
        except Exception as error:
            entry.update(status='failed',error_type=type(error).__name__); raise
        finally:
            entry['duration_ms'] = round((time.monotonic()-started)*1000,3)
            self.save()

    def read_page(self, _adapter, _worker, *, collection, page_number, temporal):
        if collection != 'sent': raise ValueError('Synthetic source exposes only Sent')
        return self.read('page',case=getattr(self.local,'case','latest'),page=page_number)

    def read_nodes(self, _adapter, _worker, *, collection, row):
        if collection != 'sent' or row['affair_id'] not in self.by_id: raise ValueError('Unknown synthetic source')
        case,_ = self.by_id[row['affair_id']]
        return self.read('detail',case=case,affair=row['affair_id'])

    def save(self):
        with self.lock:
            private_json(self.home/'source-reads.json',{'syntheticOnly':True,'source_reads':deepcopy(self.calls),
                'page_calls':sum(c['kind']=='page' for c in self.calls),'detail_calls':sum(c['kind']=='detail' for c in self.calls),
                'returned_source_rows_including_anchors':sum(c['returned_rows'] for c in self.calls),
                'distinct_source_row_ids':len({identity for c in self.calls for identity in c.get('row_ids',[])}),
                'realOaCalls':0,'modelStarted':False})


class ReadOnlyQueryService(CentralCapabilityService):
    def invoke(self, *, capability_name, **kwargs):
        entry = {'capability':capability_name,'arguments':deepcopy(kwargs.get('arguments',{})),
            'subject':kwargs.get('user_subject'),'status':'started','caller':getattr(self.source.local,'caller','workspace-or-host')}
        self.query_calls.append(entry)
        try:
            if capability_name not in ALLOWED:
                raise PermissionError('Synthetic fixture allows only history query/next')
            response = super().invoke(capability_name=capability_name, **kwargs)
            entry.update(status=response.get('status'),operation_id=response.get('operationId'))
            result = response.get('result')
            if isinstance(result,dict):
                entry.update(query_id=result.get('query_id'),state=result.get('state'),
                    delivery=deepcopy(result.get('delivery')),returned_count=result.get('returned_count'),
                    has_more=result.get('has_more'),selection=deepcopy(result.get('selection')))
            return response
        except Exception as error:
            entry.update(status='rejected',error_type=type(error).__name__); raise
        finally:
            private_json(self.source.home/'tool-calls.json',{'calls':self.query_calls,'modelToolCallsObserved':False,
                'note':'These are actual Central invocations; seed/Workspace calls are not model calls.'})

    def resume_interaction(self, **_kwargs): return denied()


class SingleQueryAcceptanceFixture:
    def __repr__(self): return 'SingleQueryAcceptanceFixture(synthetic-only)'
    def __init__(self, home, *, seed=True):
        self.home = Path(home).expanduser().absolute()
        if self.home.is_symlink() or (self.home.exists() and any(self.home.iterdir())):
            raise ValueError('Use a new empty isolated home')
        self.home.mkdir(parents=True,mode=0o700,exist_ok=True); self.home.chmod(0o700)
        self.key = self.home/'session.key'; self.key.write_bytes(os.urandom(32)); self.key.chmod(0o600)
        self.previous_key = os.environ.get('AGENTBRIDGE_SESSION_KEY_FILE')
        os.environ['AGENTBRIDGE_SESSION_KEY_FILE'] = str(self.key)
        self.stop = threading.Event(); self.server = self.thread = self.app = None
        self.source = SyntheticHistorySource(self.home,self.stop)
        self.service = ReadOnlyQueryService(home=self.home/'central',base_url='https://oa.invalid/seeyon/',worker_factory=lambda *_:OfflineWorker())
        self.service.source, self.service.query_calls = self.source, []
        for system in self.service._worker_factories_by_system:
            self.service._worker_factories_by_system[system] = (lambda *_:OfflineWorker()) if system == 'oa' else denied
        self.service.oa_history_query_service.read_page = self.source.read_page
        self.service.oa_history_query_service.read_nodes = self.source.read_nodes
        self.accounts = {}
        for subject,name in ((OWNER,'singlequery'),(OTHER,'singleother')):
            grant_permissions(self.service.user_grants,subject,['oa.workflow.read'] if subject == OWNER else [])
            account = _create_account(self.service,user_subject=subject,username=name,endpoint_key='synthetic:'+name)
            self.accounts[subject] = account
            session = self.service.sessions.get_or_create(user_subject=subject,system_id='oa',expected_principal_ref=subject)
            self.service.sessions.activate(session['session_id'],observed_principal_ref=subject)
            self.service.session_states.save(session['session_id'],{'cookies':[]})
        self.code_start = source_identity()
        self.tasks = self.seed() if seed else {}
        self.app = WorkspaceApplication(service=self.service,gateway=ReadOnlyGateway())
        private_json(self.home/'login.json',{'syntheticOnly':True,'password':PASSWORD,
            'accounts':{subject:account['username'] for subject,account in self.accounts.items()}})
        private_json(self.home/'oracle.json',{'controllerOnly':True,'latest_target_id':'100005',
            'source_count':224,'in_progress_count':29,'more_recent_ended_count':5,
            'answerStatus':'No assistant ran; stored projections are not model answers.'})
        self.source.save()

    def seed_query(self, case):
        account = self.accounts[OWNER]
        task,_ = self.service.tasks.ensure_task(user_subject=OWNER,agent_host='openclaw',
            host_task_key='single-query:'+case,origin_endpoint_id=account['endpoint_id'],
            active_conversation_ref=account['openclaw_session_key'],title=PROMPT+' · 合成 '+case)
        self.source.local.case, self.source.local.caller = case,'seed'
        try:
            response = self.service.invoke(user_subject=OWNER,capability_name='oa.workflow.history.query',
                arguments={'collection':'sent','temporal':{'kind':'all'},'selection':{'kind':'latest',
                    'count':12 if case == 'continue' else 1,'workflow_state':'in_progress'}},
                task_id=task['task_id'],host_type='workspace',origin_endpoint_id=account['endpoint_id'])
            if response['status'] != 'succeeded':
                raise RuntimeError('Synthetic query seed failed: '+str((response.get('error') or {}).get('code')))
            self.service.tasks.link_operation(task_id=task['task_id'],user_subject=OWNER,
                operation=self.service.operations.get(response['operationId']))
            return {'task_id':task['task_id'],'query_id':response['result']['query_id'],
                'state':response['result']['state'],'selection':response['result'].get('selection')}
        finally:
            self.source.local.case, self.source.local.caller = 'latest','workspace-or-host'

    def seed(self):
        account = self.accounts[OWNER]
        for index in range(160):
            self.service.tasks.append_timeline_message(user_subject=OWNER,source_endpoint_id=account['endpoint_id'],
                message_key='single-query-history:'+str(index),role='user' if index%2 else 'assistant',
                text=f'合成旧历史 {index+1:03d}。'+'仅用于测试内部滚动和历史保留，不是本轮模型回答。'*18)
        return {case:self.seed_query(case) for case in CASES}

    def audit(self):
        with closing(sqlite3.connect(self.service.db_path)) as db:
            return {'businessDecisions':db.execute('SELECT count(*) FROM business_draft_decisions').fetchone()[0],
                'authorizations':db.execute('SELECT count(*) FROM write_authorizations').fetchone()[0],
                'commits':db.execute('SELECT count(*) FROM write_authorizations WHERE commit_operation_id IS NOT NULL').fetchone()[0],
                'realOaCalls':0,'modelStarted':False,'coordinatorStarted':False}

    def controls(self):
        account = self.accounts[OWNER]
        for marker in ('expire-primary-session','restore-primary-session','revoke-primary-read','restore-primary-read'):
            try:
                if self.source.marker(marker) is None: continue
                session = self.service.sessions.find(user_subject=OWNER,system_id='oa')
                if marker == 'expire-primary-session': self.service.sessions.mark_expired(session['session_id'],'Synthetic expiry')
                elif marker == 'restore-primary-session': self.service.sessions.activate(session['session_id'],observed_principal_ref=OWNER)
                else: grant_permissions(self.service.user_grants,OWNER,['oa.workflow.read'] if marker.startswith('restore') else [])
                private_json(self.home/(marker+'-receipt.json'),{'syntheticOnly':True,'applied':True})
            except Exception as error:
                private_json(self.home/(marker+'-receipt.json'),{'syntheticOnly':True,'errorType':type(error).__name__})

    def start(self):
        port = free_port(); self.url = f'http://127.0.0.1:{port}'
        self.server = create_workspace_http_server(config=validate_workspace_server_config(host='127.0.0.1',port=port,
            public_base_url=self.url,tls_cert=None,tls_key=None),application=self.app)
        original = self.server.RequestHandlerClass
        fixture = self
        class Handler(original):
            def log_message(self,*_args): pass
            def do_POST(self):
                path = urlsplit(self.path).path
                import re
                if path not in {'/api/login','/api/logout'} and not re.fullmatch(r'/api/tasks/[0-9a-f-]{36}/oa-history/next',path):
                    self.close_connection = True
                    return self._json(403,{'error':{'code':'SYNTHETIC_QUERY_ONLY','message':'本夹具只允许登录和原查询续读。'}})
                try: return super().do_POST()
                finally: private_json(fixture.home/'business-audit.json',fixture.audit())
            def do_PUT(self):
                self.close_connection = True
                return self._json(403,{'error':{'code':'SYNTHETIC_QUERY_ONLY','message':'本夹具不允许业务写入。'}})
            do_PATCH = do_DELETE = do_PUT
        self.server.RequestHandlerClass = Handler
        self.thread = threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.05),daemon=True);self.thread.start()
        private_json(self.home/'public.json',{'fixture':'single-query-acceptance-v1','home':str(self.home),'workspaceUrl':self.url,
            'sourceRoot':str(ROOT),'tasks':self.tasks,'longHistoryMessages':160,'sourceBoundary':'Only read_page/read_nodes are synthetic; real HTTP/auth/grants/session/stores/query engine.',
            'modelStarted':False,'storedProjectionsAreModelAnswers':False,'codeStart':self.code_start,
            'faultMarkers':['fail-next-single-query-read','timeout-next-single-query-read','delay-next-single-query-read',
                'expire-primary-session','restore-primary-session','revoke-primary-read','restore-primary-read']})
        private_json(self.home/'business-audit.json',self.audit());return self.url

    def close(self):
        self.stop.set()
        if self.server:
            if self.thread:self.server.shutdown()
            self.server.server_close()
        if self.thread:self.thread.join(timeout=3)
        if self.app:self.app.close()
        private_json(self.home/'business-audit.json',self.audit())
        private_json(self.home/'code-end.json',source_identity())
        if self.previous_key is None:os.environ.pop('AGENTBRIDGE_SESSION_KEY_FILE',None)
        else:os.environ['AGENTBRIDGE_SESSION_KEY_FILE']=self.previous_key
