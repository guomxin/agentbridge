"""Private skill drafts and immutable, administrator-approved publication.

Authored instructions never grant business access. Model test outputs are explicitly
unreviewed evidence; only the authenticated control plane can accept and publish them.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from uuid import uuid4

from bscli.core.business_skills import PROTOCOL, _json, validate_skill_bundle
from bscli.core.skill_actions import dispatch_skill_action
from bscli.core.user_grants import UserGrantConflict


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, name, maximum=500, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"{name}不能为空且不能超过{maximum}字符")
    return value.strip()


def revision(value):
    if type(value) is not int or value < 1:
        raise ValueError("需要有效修订号")
    return value


class SkillAuthoring:
    def __init__(self, service):
        self.service = service
        self.store = service.skills
        with closing(self.store.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS skill_drafts (
                    draft_id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL, skill_id TEXT NOT NULL UNIQUE,
                    revision INTEGER NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS skill_drafts_owner ON skill_drafts(owner_subject,updated_at);
                CREATE TABLE IF NOT EXISTS skill_draft_revisions (
                    draft_id TEXT NOT NULL, revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL, provenance_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(draft_id,revision));
                CREATE TABLE IF NOT EXISTS skill_authoring_commands (
                    owner_subject TEXT NOT NULL, request_key TEXT NOT NULL, input_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL, PRIMARY KEY(owner_subject,request_key));
                CREATE TABLE IF NOT EXISTS skill_draft_tests (
                    test_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    owner_subject TEXT NOT NULL, profile TEXT NOT NULL, prompt TEXT NOT NULL,
                    output TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT);
                CREATE TABLE IF NOT EXISTS skill_review_requests (
                    request_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, draft_revision INTEGER NOT NULL,
                    owner_subject TEXT NOT NULL, content_hash TEXT NOT NULL, audience_json TEXT NOT NULL,
                    profiles_json TEXT NOT NULL, expected_publication_revision INTEGER NOT NULL,
                    state TEXT NOT NULL, reason TEXT NOT NULL, reviewer TEXT, decision_reason TEXT,
                    published_version TEXT, created_at TEXT NOT NULL, decided_at TEXT);
                CREATE INDEX IF NOT EXISTS skill_reviews_owner ON skill_review_requests(owner_subject,created_at);
                CREATE TABLE IF NOT EXISTS skill_review_tests (
                    request_id TEXT NOT NULL, test_id TEXT NOT NULL, PRIMARY KEY(request_id,test_id));
                CREATE TABLE IF NOT EXISTS skill_authoring_preferences (
                    owner_subject TEXT PRIMARY KEY, revision INTEGER NOT NULL, value_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_authoring_events (
                    event_id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL, kind TEXT NOT NULL,
                    object_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
        from bscli.core.skill_workbench import SkillWorkbench
        self.workbench = SkillWorkbench(self)

    def _event(self, db, owner, kind, object_id, payload):
        db.execute("INSERT INTO skill_authoring_events VALUES (?,?,?,?,?,?)",
                   (str(uuid4()), owner, kind, object_id, _json(payload), now()))

    def _command(self, db, owner, key, data):
        text(key, "请求标识", 128)
        digest = sha256(_json(data).encode()).hexdigest()
        row = db.execute("SELECT * FROM skill_authoring_commands WHERE owner_subject=? AND request_key=?", (owner, key)).fetchone()
        if row:
            if row['input_hash'] != digest:
                raise UserGrantConflict("请求标识已用于另一操作")
            return digest, json.loads(row['result_json'])
        return digest, None

    def _receipt(self, db, owner, key, digest, result):
        db.execute("INSERT INTO skill_authoring_commands VALUES (?,?,?,?)", (owner, key, digest, _json(result)))
        return result

    def _owned(self, db, owner, draft_id, expected_revision=None):
        row = db.execute("SELECT * FROM skill_drafts WHERE draft_id=? AND owner_subject=?", (draft_id, owner)).fetchone()
        if not row:
            raise KeyError("草稿不存在或不可访问")
        if expected_revision is not None and row['revision'] != revision(expected_revision):
            raise UserGrantConflict("草稿已更新，请刷新后再操作")
        return row

    def _bundle(self, db, draft_id, rev):
        row = db.execute("SELECT * FROM skill_draft_revisions WHERE draft_id=? AND revision=?", (draft_id, rev)).fetchone()
        if not row:
            raise KeyError("草稿修订不存在")
        return row, json.loads(row['payload_json'])

    def normalize(self, skill_id, proposal):
        from bscli.core.skill_contracts import SkillProposal
        from pydantic import ValidationError
        try:
            proposal = SkillProposal.model_validate(proposal).model_dump(exclude_none=True)
        except ValidationError as exc:
            fields = ', '.join('.'.join(str(p) for p in e['loc']) for e in exc.errors())
            raise ValueError('草稿字段无效：' + fields) from exc
        method = proposal.pop('method', None)
        if method:
            sections = [('输入', 'inputs'), ('步骤', 'steps'), ('异常处理', 'exceptions'), ('验收', 'acceptance')]
            proposal['instructions'] += '\n\n' + '\n\n'.join('## ' + title + '\n' + '\n'.join(f'{i+1}. {v}' for i, v in enumerate(method[key])) for title, key in sections if method[key])
        refs = proposal.get('references', {})
        profiles = proposal.get('profiles', {'use': {}})
        if not isinstance(refs, dict) or not isinstance(profiles, dict) or not profiles or len(profiles) > 8:
            raise ValueError("参考文件或模式无效")
        body = text(proposal.get('instructions'), "处理方法", 40000)
        # A generated bundle contains instructions, never credentials or executable assets.
        serialized = _json(proposal)
        if re.search(r'(?i)(-----BEGIN .*PRIVATE KEY|Bearer\s+[A-Za-z0-9._~+/=-]{16,}|(?:password|cookie|api_key)\s*[:=]\s*["\']?[^\s"\']{8,}|https?://[^\s"<>]+/(?:input|authorize|auth)/[A-Za-z0-9_-]{20,})', serialized):
            raise ValueError("草稿含疑似凭据或临时交互链接，请移除后保存")
        manifest = {'schemaVersion': PROTOCOL, 'id': skill_id, 'version': '0.0.0',
                    'name': text(proposal.get('name'), '名称', 120),
                    'description': text(proposal.get('description'), '简介'),
                    'entrypoint': 'SKILL.md', 'status': 'enabled',
                    'executionMode': proposal.get('executionMode', 'read_exploration'),
                    'profiles': profiles, 'resources': list(refs),
                    'required_resources': proposal.get('required_resources', {p: list(refs) for p in profiles}),
                    'selection': proposal.get('selection')}
        if not all(isinstance(k, str) and k.startswith('references/') for k in refs):
            raise ValueError("参考文件必须位于 references 目录")
        return validate_skill_bundle(manifest, {'SKILL.md': body, **refs})

    def _provenance(self, owner, data):
        if not isinstance(data, dict) or set(data) - {'kind', 'task_ids', 'summary', 'complete', 'scope'}:
            raise ValueError("来源字段无效")
        kind = data.get('kind', 'request')
        if kind not in {'request', 'interaction', 'import', 'revision', 'automatic'}:
            raise ValueError("来源类型无效")
        ids = data.get('task_ids', [])
        if not isinstance(ids, list) or len(ids) > 5 or any(not isinstance(i, str) for i in ids):
            raise ValueError("一次最多关联五个任务")
        facts = []
        for tid in dict.fromkeys(ids):
            task = self.service.tasks.get_task(tid, user_subject=owner)
            facts.append({'task_id': tid, 'status': task['status']})
        return {'kind': kind, 'tasks': facts,
                'summary': text(data.get('summary', ''), '过程摘要', 3000, empty=True),
                'completeness': 'host_reported_complete' if data.get('complete') is True else 'partial_or_unknown',
                'verification': 'task_status_only' if facts else 'host_statement_only'}

    def save(self, owner, *, proposal, request_key, draft_id=None, expected_revision=None, provenance=None, _job=None):
        source_input = provenance or {}
        provenance = self._provenance(owner, source_input)
        automatic = provenance['kind'] == 'automatic'
        if automatic and _job is None:
            request_key = 'automatic:' + sha256(_json({'proposal':proposal,'source':source_input,'draft_id':draft_id,'revision':expected_revision}).encode()).hexdigest()
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if _job is not None:
                active = db.execute("SELECT 1 FROM skill_jobs WHERE job_id=? AND owner_subject=? AND claim_token=? AND state='running'", (_job['job_id'], owner, _job['claim_token'])).fetchone()
                if not active: raise UserGrantConflict('后台任务已取消或已被接续')
                if automatic and not self.workbench._auto_allowed(db, owner, _job['scope']):
                    raise PermissionError('自动沉淀范围已关闭')
            if automatic:
                pref = db.execute('SELECT value_json FROM skill_authoring_preferences WHERE owner_subject=?',(owner,)).fetchone()
                if not pref or not json.loads(pref[0])['auto_draft']:
                    raise PermissionError('请先开启自动沉淀草稿')
                if _job is None and not self.workbench._auto_allowed(db, owner, source_input.get('scope', '')):
                    raise PermissionError('自动保存需要已开启的会话范围，请使用后台生成')
            digest, previous = self._command(db, owner, request_key, {'op': 'save', 'proposal': proposal,
                'draft_id': draft_id, 'revision': expected_revision, 'provenance': source_input})
            if previous:
                return previous
            if automatic:
                count = db.execute("SELECT count(*) FROM skill_authoring_events WHERE owner_subject=? AND kind='draft.automatic' AND created_at>=?",(owner,now()[:10])).fetchone()[0]
                if count >= 3: raise ValueError('今日自动沉淀已达3次，仍可手动创作')
            if draft_id:
                draft = self._owned(db, owner, draft_id, expected_revision)
                if expected_revision is None or draft['state'] == 'archived':
                    raise ValueError("更新草稿需要当前修订号且不能已归档")
                if draft['revision'] >= 500: raise ValueError('单个草稿最多500个修订')
                sid, rev = draft['skill_id'], draft['revision'] + 1
            else:
                if db.execute("SELECT count(*) FROM skill_drafts WHERE owner_subject=? AND state!='archived'", (owner,)).fetchone()[0] >= 100:
                    raise ValueError("最多保留100份活动草稿，请先归档不再使用的草稿")
                draft_id, sid, rev = str(uuid4()), 'usr-' + uuid4().hex, 1
            bundle = self.normalize(sid, proposal)
            if rev == 1:
                db.execute('INSERT INTO skill_drafts VALUES (?,?,?,?,?,?,?)', (draft_id, owner, sid, rev, 'editing', now(), now()))
            else:
                db.execute("UPDATE skill_drafts SET revision=?,state='editing',updated_at=? WHERE draft_id=?", (rev, now(), draft_id))
            db.execute('INSERT INTO skill_draft_revisions VALUES (?,?,?,?,?,?)',
                       (draft_id, rev, _json(bundle), bundle['content_hash'], _json(provenance), now()))
            self._event(db, owner, 'draft.automatic' if automatic else 'draft.saved', draft_id, {'revision': rev, 'content_hash': bundle['content_hash']})
            result = self._receipt(db, owner, request_key, digest, {'draft_id': draft_id, 'skill_id': sid,
                'revision': rev, 'status': 'draft_saved', 'content_hash': bundle['content_hash'], 'published': False})
            if _job:
                # Save and completion are atomic; cancellation cannot race the draft commit.
                db.execute("UPDATE skill_jobs SET state='succeeded',result_json=?,claim_token=NULL,updated_at=? WHERE job_id=?", (_json({'saved': True, 'classification': 'method', **_job.get('metadata', {}), **result}), now(), _job['job_id']))
            return result

    def list(self, owner):
        with closing(self.store.connect()) as db:
            rows = db.execute('''SELECT d.*,r.payload_json,
                (SELECT version FROM skill_publications p WHERE p.skill_id=d.skill_id) AS published_version,
                (SELECT state FROM skill_review_requests q WHERE q.draft_id=d.draft_id AND q.draft_revision=d.revision ORDER BY created_at DESC LIMIT 1) AS review_state
                FROM skill_drafts d JOIN skill_draft_revisions r ON r.draft_id=d.draft_id AND r.revision=d.revision WHERE owner_subject=? ORDER BY updated_at DESC LIMIT 100''', (owner,)).fetchall()
            return {'items': [{**{k:r[k] for k in r.keys() if k != 'payload_json'},
                              'name': json.loads(r['payload_json'])['manifest']['name']} for r in rows]}

    def get(self, owner, draft_id):
        with closing(self.store.connect()) as db:
            draft = self._owned(db, owner, draft_id)
            row, bundle = self._bundle(db, draft_id, draft['revision'])
            return {**dict(draft), 'revisions':[dict(r) for r in db.execute('SELECT revision,content_hash,created_at FROM skill_draft_revisions WHERE draft_id=? ORDER BY revision DESC LIMIT 100',(draft_id,))], 'bundle': bundle, 'provenance': json.loads(row['provenance_json']),
                    'tests': [dict(r) for r in db.execute('SELECT * FROM skill_draft_tests WHERE draft_id=? ORDER BY created_at DESC LIMIT 30', (draft_id,))],
                    'requests': [self._review_public(r) for r in db.execute('SELECT * FROM skill_review_requests WHERE draft_id=? ORDER BY created_at DESC LIMIT 30', (draft_id,))]}

    def test_start(self, owner, *, draft_id, expected_revision, profile, prompt, request_key):
        text(prompt, '测试问题', 3000)
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            digest, previous = self._command(db, owner, request_key, {'op':'test', 'draft_id':draft_id, 'revision':expected_revision, 'profile':profile, 'prompt':prompt})
            if previous: return previous
            revision(expected_revision)
            draft = self._owned(db, owner, draft_id, expected_revision)
            _, bundle = self._bundle(db, draft_id, draft['revision'])
            if profile not in bundle['manifest']['profiles']:
                raise ValueError('模式不存在')
            if db.execute('SELECT count(*) FROM skill_draft_tests WHERE draft_id=? AND revision=?',(draft_id,draft['revision'])).fetchone()[0] >= 50:
                raise ValueError('每个修订最多50次样例，请修订后重试')
            tid = str(uuid4())
            db.execute('INSERT INTO skill_draft_tests VALUES (?,?,?,?,?,?,NULL,?,?,NULL)', (tid,draft_id,draft['revision'],owner,profile,prompt,'running',now()))
            return self._receipt(db, owner, request_key, digest, {'test_id':tid,'status':'running',
                'mode':'isolated_sample','bundle':bundle,'prompt':prompt,
                'instruction':'仅使用合成示例评估草稿，不调用业务工具，不写业务。提供示例回答及不足，再用 test_result 保存。报告待用户和管理员复核，不代表真实业务通过。'})

    def test_result(self, owner, *, test_id, output):
        text(output, '测试结果', 16000)
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM skill_draft_tests WHERE test_id=? AND owner_subject=?', (test_id,owner)).fetchone()
            if not row: raise KeyError('测试不存在')
            if row['status'] == 'reported':
                if row['output'] != output: raise UserGrantConflict('测试结果已保存，不能覆盖')
            elif row['status'] == 'running':
                db.execute("UPDATE skill_draft_tests SET output=?,status='reported',finished_at=? WHERE test_id=?", (output,now(),test_id))
            else: raise ValueError('测试状态不允许提交')
            return {'test_id':test_id,'status':'reported','verification':'待人工复核的模型样例，不是独立通过证明'}

    @staticmethod
    def _review_public(row):
        result = dict(row)
        result['audience'] = json.loads(result.pop('audience_json'))
        result['profiles'] = json.loads(result.pop('profiles_json'))
        return result

    def submit(self, owner, *, draft_id, expected_revision, request_key, reason, audience=None, profiles=None):
        reason = text(reason, '发布说明', 1000)
        audience = audience or [owner]
        if not isinstance(audience,list) or not audience or len(audience)>100 or any(not isinstance(x,str) or not x or len(x)>256 for x in audience):
            raise ValueError('发布范围无效')
        audience = sorted(set(audience))
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            digest, previous = self._command(db,owner,request_key,{'op':'submit','draft_id':draft_id,'revision':expected_revision,'reason':reason,'audience':audience,'profiles':profiles})
            if previous: return previous
            revision(expected_revision)
            draft = self._owned(db,owner,draft_id,expected_revision)
            if draft['state']=='archived': raise ValueError('草稿已归档')
            _, bundle = self._bundle(db,draft_id,draft['revision'])
            profiles = profiles or list(bundle['manifest']['profiles'])
            if not isinstance(profiles,list) or not profiles or any(not isinstance(p,str) for p in profiles) or len(set(profiles))!=len(profiles) or any(p not in bundle['manifest']['profiles'] for p in profiles):
                raise ValueError('申请模式无效')
            tested = {r[0] for r in db.execute("SELECT profile FROM skill_draft_tests WHERE draft_id=? AND revision=? AND status='reported'", (draft_id,draft['revision']))}
            report = self.workbench.latest_report(db, owner, draft_id, draft['revision'])
            if report and report['passed'] and report['content_hash'] == bundle['content_hash']:
                tested.update(c['profile'] for c in report['cases'] if c['kind'] == 'output')
            if not set(profiles)<=tested: raise ValueError('请先为每个申请模式完成样例试运行，结果由管理员复核')
            pending=db.execute("SELECT * FROM skill_review_requests WHERE draft_id=? AND state='submitted'",(draft_id,)).fetchone()
            if pending: raise UserGrantConflict('已有待审批申请，请先撤回或等待处理')
            current=db.execute('SELECT revision FROM skill_publications WHERE skill_id=?',(draft['skill_id'],)).fetchone()
            rid=str(uuid4())
            db.execute('INSERT INTO skill_review_requests VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,NULL)',
                (rid,draft_id,draft['revision'],owner,bundle['content_hash'],_json(audience),_json(profiles),current[0] if current else 0,'submitted',reason,now()))
            db.execute("INSERT INTO skill_review_tests SELECT ?,test_id FROM skill_draft_tests WHERE draft_id=? AND revision=? AND status='reported'",(rid,draft_id,draft['revision']))
            from bscli.core.skill_quality import diagnostics, bundle_diff
            prior = self.store.current(draft['skill_id'], db) if current else None
            report = self.workbench.latest_report(db, owner, draft_id, draft['revision'])
            prior_audience = [x[0][5:] for x in db.execute("SELECT owner FROM skill_config WHERE owner LIKE 'user:%' AND json_type(value_json,?) IS NOT NULL", ('$.' + draft['skill_id'],))]
            quality = {'diagnostics': diagnostics(bundle), 'diff': bundle_diff(prior, bundle), 'evaluation': report,
                       'audience_diff': {'added': sorted(set(audience)-set(prior_audience)), 'removed': sorted(set(prior_audience)-set(audience))}}
            db.execute('INSERT INTO skill_review_quality VALUES (?,?)', (rid, _json(quality)))
            self._event(db,owner,'release.submitted',rid,{'draft_id':draft_id,'revision':draft['revision']})
            return self._receipt(db,owner,request_key,digest,{'request_id':rid,'status':'submitted','message':'已提交，等待管理控制台审批','published':False})

    def withdraw(self, owner, request_id):
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT * FROM skill_review_requests WHERE request_id=? AND owner_subject=?',(request_id,owner)).fetchone()
            if not r: raise KeyError('申请不存在')
            if r['state'] not in {'submitted','withdrawn'}: raise UserGrantConflict('申请已处理，不能撤回')
            db.execute("UPDATE skill_review_requests SET state='withdrawn' WHERE request_id=?",(request_id,))
            self._event(db,owner,'release.withdrawn',request_id,{})
            return {'status':'withdrawn'}

    def reviews(self, owner=None):
        with closing(self.store.connect()) as db:
            return {'items':[self._review_public(r) for r in db.execute(
                '''SELECT q.*,json_extract(r.payload_json,'$.manifest.name') AS name
                   FROM skill_review_requests q JOIN skill_draft_revisions r ON q.draft_id=r.draft_id AND q.draft_revision=r.revision'''
                +(' WHERE q.owner_subject=?' if owner else '')+' ORDER BY q.created_at DESC LIMIT 100', (owner,) if owner else ())]}

    def review_detail(self, request_id):
        with closing(self.store.connect()) as db:
            r=db.execute('SELECT * FROM skill_review_requests WHERE request_id=?',(request_id,)).fetchone()
            if not r: raise KeyError('申请不存在')
            _,bundle=self._bundle(db,r['draft_id'],r['draft_revision'])
            quality = db.execute('SELECT snapshot_json FROM skill_review_quality WHERE request_id=?', (request_id,)).fetchone()
            return {**self._review_public(r),'bundle':bundle, 'quality': json.loads(quality[0]) if quality else None, 'tests':[dict(t) for t in db.execute(
                "SELECT t.profile,t.prompt,t.output,t.status FROM skill_draft_tests t JOIN skill_review_tests e USING(test_id) WHERE e.request_id=?",(request_id,))]}

    def decide(self, *, actor, request_id, decision, reason, reviewed_tests=False, manual_quality_reason=None, audit_callback=None):
        if actor.get('role')!='admin': raise PermissionError('需要管理员审批权限')
        if decision not in {'approve','changes_requested','rejected'}: raise ValueError('审批决定无效')
        reason=text(reason,'审批意见',1000)
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT * FROM skill_review_requests WHERE request_id=?',(request_id,)).fetchone()
            if not r: raise KeyError('申请不存在')
            if r['state']!='submitted':
                if decision=='approve' and r['state']=='published': return self._review_public(r)
                raise UserGrantConflict('申请已处理，请刷新')
            before=self._review_public(r)
            version=None
            if decision=='approve':
                if reviewed_tests is not True: raise ValueError('请复核草稿、样例结果及使用范围后批准')
                quality = db.execute('SELECT snapshot_json FROM skill_review_quality WHERE request_id=?', (request_id,)).fetchone()
                report = json.loads(quality[0]).get('evaluation') if quality else None
                if report and (not report['passed'] or report['content_hash'] != r['content_hash']):
                    raise ValueError('独立评测未通过或内容不匹配，请退回修改并重新申请')
                if quality and not report:
                    text(manual_quality_reason, '无独立评测时的人工验证说明', 1000)
                    snapshot = json.loads(quality[0]); snapshot['manual_quality_reason'] = manual_quality_reason
                    db.execute('UPDATE skill_review_quality SET snapshot_json=? WHERE request_id=?', (_json(snapshot), request_id))
                _,bundle=self._bundle(db,r['draft_id'],r['draft_revision'])
                if bundle['content_hash']!=r['content_hash']: raise ValueError('申请内容不一致')
                sid=bundle['manifest']['id']
                current=db.execute('SELECT * FROM skill_publications WHERE skill_id=?',(sid,)).fetchone()
                pubrev=current['revision'] if current else 0
                if pubrev!=r['expected_publication_revision']: raise UserGrantConflict('发布版本已变化，请重新申请')
                profiles=json.loads(r['profiles_json']); audience=json.loads(r['audience_json'])
                # Check every recipient at review time; never create identities or grants.
                # The author is already authenticated; publishing a method needs no business grants.
                tables={t[0] for t in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for subject in audience:
                    known = subject == r['owner_subject'] or self.service.user_grants.get(subject) is not None
                    for table in ('workspace_accounts','mcp_identity_tokens'):
                        if not known and table in tables:
                            known = db.execute(f'SELECT 1 FROM {table} WHERE user_subject=? LIMIT 1',(subject,)).fetchone() is not None
                    if not known: raise ValueError('共享接收用户不存在')
                version=f'1.0.{pubrev}'
                published=deepcopy(bundle)
                published['manifest']['version']=version
                published['manifest']['profiles']={p:bundle['manifest']['profiles'][p] for p in profiles}
                published['manifest']['required_resources']={p:bundle['manifest']['required_resources'][p] for p in profiles}
                published=validate_skill_bundle(published['manifest'],published['resources'])
                db.execute('INSERT INTO skill_versions VALUES (?,?,?,?)',(sid,version,published['content_hash'],_json(published)))
                # Audience reduction must revoke old bindings, not merely hide the catalog.
                for cfg in db.execute("SELECT owner,value_json,revision FROM skill_config WHERE owner LIKE 'user:%'").fetchall():
                    subject=cfg['owner'][5:]; value=json.loads(cfg['value_json'])
                    if sid in value and subject not in audience:
                        del value[sid]
                        db.execute('UPDATE skill_config SET value_json=?,revision=revision+1 WHERE owner=?',(_json(value),cfg['owner']))
                        db.execute('UPDATE skill_bindings SET revoked=1 WHERE skill_id=? AND user_subject=?',(sid,subject))
                for subject in audience:
                    cfg=self.store.config('user:'+subject,db); value=cfg['value']; old=value.get(sid,{})
                    value[sid]={'profiles':profiles,'source_id':old.get('source_id',''),'detail':old.get('detail','standard')}
                    db.execute('INSERT INTO skill_config VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET value_json=excluded.value_json,revision=excluded.revision',('user:'+subject,_json(value),cfg['revision']+1))
                    for bound in db.execute('SELECT binding_id,profile FROM skill_bindings WHERE skill_id=? AND user_subject=? AND revoked=0',(sid,subject)).fetchall():
                        if bound['profile'] not in profiles: db.execute('UPDATE skill_bindings SET revoked=1 WHERE binding_id=?',(bound['binding_id'],))
                db.execute('INSERT INTO skill_publications VALUES (?,?,?,?,?,?) ON CONFLICT(skill_id) DO UPDATE SET version=excluded.version,request_id=excluded.request_id,revision=excluded.revision,updated_at=excluded.updated_at',
                    (sid,version,r['owner_subject'],request_id,pubrev+1,now()))
            state='published' if decision=='approve' else decision
            db.execute('UPDATE skill_review_requests SET state=?,reviewer=?,decision_reason=?,published_version=?,decided_at=? WHERE request_id=?',
                (state,actor['username'],reason,version,now(),request_id))
            after=self._review_public(db.execute('SELECT * FROM skill_review_requests WHERE request_id=?',(request_id,)).fetchone())
            self._event(db,r['owner_subject'],'release.'+state,request_id,{'reviewer':actor['username'],'reason':reason,'version':version})
            if audit_callback: audit_callback(db,before,after)
            return after

    def preferences(self, owner, *, value=None, expected_revision=None):
        with closing(self.store.connect()) as db, db:
            if value is not None: db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM skill_authoring_preferences WHERE owner_subject=?',(owner,)).fetchone()
            current={'revision':row['revision'] if row else 0,'value':json.loads(row['value_json']) if row else {'auto_draft':False}}
            if value is None: return current
            if not isinstance(value,dict) or set(value)!={'auto_draft'} or type(value['auto_draft']) is not bool:
                raise ValueError('自动沉淀设置无效')
            if type(expected_revision) is not int or expected_revision!=current['revision']:
                raise UserGrantConflict('设置已更新，请刷新')
            db.execute('INSERT INTO skill_authoring_preferences VALUES (?,?,?) ON CONFLICT(owner_subject) DO UPDATE SET revision=excluded.revision,value_json=excluded.value_json',(owner,current['revision']+1,_json(value)))
            self._event(db,owner,'preferences.changed',owner,value)
            return {'revision':current['revision']+1,'value':value}

    def archive(self, owner, *, draft_id, expected_revision):
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            revision(expected_revision)
            self._owned(db,owner,draft_id,expected_revision)
            if db.execute("SELECT 1 FROM skill_review_requests WHERE draft_id=? AND state='submitted'",(draft_id,)).fetchone():
                raise UserGrantConflict('请先撤回待审批申请')
            db.execute("UPDATE skill_drafts SET state='archived',updated_at=? WHERE draft_id=?",(now(),draft_id))
            self._event(db,owner,'draft.archived',draft_id,{})
            return {'status':'archived','published_versions_unchanged':True}

    def restore(self, owner, *, draft_id, expected_revision, target_revision, request_key):
        revision(target_revision)
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            digest,previous=self._command(db,owner,request_key,{'op':'restore','draft_id':draft_id,'expected_revision':expected_revision,'target_revision':target_revision})
            if previous: return previous
            revision(expected_revision)
            draft=self._owned(db,owner,draft_id,expected_revision)
            _,bundle=self._bundle(db,draft_id,target_revision)
            if draft['revision'] >= 500: raise ValueError('单个草稿最多500个修订')
            rev=draft['revision']+1
            db.execute("UPDATE skill_drafts SET revision=?,state='editing',updated_at=? WHERE draft_id=?",(rev,now(),draft_id))
            db.execute('INSERT INTO skill_draft_revisions VALUES (?,?,?,?,?,?)',(draft_id,rev,_json(bundle),bundle['content_hash'],_json({'kind':'revision','restored_from':target_revision,'completeness':'partial_or_unknown'}),now()))
            self._event(db,owner,'draft.restored',draft_id,{'revision':rev,'restored_from':target_revision})
            return self._receipt(db,owner,request_key,digest,{'draft_id':draft_id,'revision':rev,'status':'draft_saved','published':False})

    def export(self, owner, draft_id):
        draft=self.get(owner,draft_id); bundle=draft['bundle']; manifest=bundle['manifest']
        return {'format':'agentbridge.skill-draft.v1','proposal':{**{k:manifest[k] for k in ('name','description','selection','profiles','executionMode','required_resources')},
            'instructions':bundle['resources']['SKILL.md'],'references':{k:v for k,v in bundle['resources'].items() if k!='SKILL.md'}}}

    def dispatch(self, owner, action, data):
        return dispatch_skill_action(self, owner, action, data)
