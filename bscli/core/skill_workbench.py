"""Durable, identity-scoped authoring and tool-free evaluation jobs."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import threading
import sqlite3
from pathlib import Path
from uuid import uuid4

from bscli.core.business_skills import _json, skill_catalog
from bscli.core.skill_quality import (bundle_diff, diagnostics, export_standard,
    import_standard, proposal_from_bundle, similarity, validate_cases)
from bscli.core.skill_job_executor import SkillJobDependencies, SkillJobExecutor
from bscli.core.skill_job_queue import SkillJobQueue
from bscli.core.user_grants import UserGrantConflict


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class SkillWorkbench:
    def __init__(self, authoring):
        self.authoring = authoring
        self.store = authoring.store
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._complete = None
        self._capture = None
        with closing(self.store.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS skill_jobs (
                    job_id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL, kind TEXT NOT NULL,
                    state TEXT NOT NULL, request_key TEXT NOT NULL, input_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL, result_json TEXT, progress_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT, lease_until TEXT,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner_subject,request_key));
                CREATE INDEX IF NOT EXISTS skill_jobs_pending ON skill_jobs(state,created_at);
                CREATE TABLE IF NOT EXISTS skill_auto_scopes (
                    owner_subject TEXT NOT NULL, scope TEXT NOT NULL, enabled INTEGER NOT NULL,
                    PRIMARY KEY(owner_subject,scope));
                CREATE TABLE IF NOT EXISTS skill_review_quality (
                    request_id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_feedback (
                    feedback_id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL,
                    skill_id TEXT NOT NULL, version TEXT NOT NULL, rating TEXT NOT NULL,
                    comment TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_auto_capture (
                    dispatch_id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL,
                    assistant_text TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')

    def status(self):
        return {'mode': 'tool_free_completion', 'worker_configured': self._complete is not None,
                'scope': '合成文字与方法选择评测，不证明真实业务执行成功'}

    def _job_queue(self):
        from bscli.core.skill_authoring import text
        # Lightweight adapters resolve the live store, event handler, and clock;
        # schema initialization and the worker thread remain owned here.
        return SkillJobQueue(
            connect=lambda: self.store.connect(),
            event=lambda db, owner, kind, object_id, payload: self.authoring._event(
                db, owner, kind, object_id, payload),
            wake=lambda: self._wake.set(),
            timestamp=lambda: timestamp(),
            validate_text=text,
            now=lambda: datetime.now(timezone.utc),
        )

    def _enqueue(self, owner, kind, payload, request_key):
        return self._job_queue().enqueue(owner, kind, payload, request_key)

    @staticmethod
    def _public(row):
        return SkillJobQueue.public(row)

    def jobs(self, owner, job_id=None):
        return self._job_queue().jobs(owner, job_id)

    def cancel(self, owner, job_id):
        return self._job_queue().cancel(owner, job_id)

    def retry(self, owner, job_id):
        return self._job_queue().retry(owner, job_id)

    def scopes(self, owner, scope=None, enabled=None):
        from bscli.core.skill_authoring import text
        with closing(self.store.connect()) as db, db:
            if scope is not None:
                text(scope, '会话范围', 256)
                if type(enabled) is not bool: raise ValueError('范围开关必须是布尔值')
                db.execute('INSERT INTO skill_auto_scopes VALUES (?,?,?) ON CONFLICT(owner_subject,scope) DO UPDATE SET enabled=excluded.enabled', (owner, scope, int(enabled)))
                self.authoring._event(db, owner, 'scope.changed', scope, {'enabled': enabled})
            return {'items': [dict(r) for r in db.execute('SELECT scope,enabled FROM skill_auto_scopes WHERE owner_subject=?', (owner,))]}

    @staticmethod
    def _auto_allowed(db, owner, scope):
        return SkillJobQueue.auto_allowed(db, owner, scope)

    def generate(self, owner, *, material, request_key, draft_id=None, expected_revision=None,
                 scope='manual', automatic=False, task_ids=None):
        from bscli.core.skill_authoring import text
        text(material, '提炼材料', 16000)
        text(scope, '会话范围', 256)
        if type(automatic) is not bool: raise ValueError('自动标识无效')
        source = self.authoring._provenance(owner, {'kind': 'automatic' if automatic else 'interaction', 'task_ids': task_ids or [], 'complete': False})
        prior = None
        if draft_id:
            draft = self.authoring.get(owner, draft_id)
            if draft['revision'] != expected_revision: raise UserGrantConflict('草稿修订已变化')
            prior = proposal_from_bundle(draft['bundle'])
        with closing(self.store.connect()) as db:
            if automatic:
                if not self._auto_allowed(db, owner, scope): raise PermissionError('请先开启自动沉淀及当前会话范围')
                n = db.execute("SELECT count(*) FROM skill_jobs WHERE owner_subject=? AND kind='generation' AND created_at>=? AND json_extract(payload_json,'$.automatic')=1", (owner, timestamp()[:10])).fetchone()[0]
                existing = db.execute('SELECT 1 FROM skill_jobs WHERE owner_subject=? AND request_key=?', (owner, request_key)).fetchone()
                if n >= 3 and not existing: raise ValueError('今日自动沉淀已达 3 次')
        payload = {'material': material, 'draft_id': draft_id, 'revision': expected_revision, 'prior': prior,
                   'scope': scope, 'automatic': automatic, 'provenance': source}
        return self._enqueue(owner, 'generation', payload, request_key)

    def evaluate(self, owner, *, draft_id, expected_revision, cases, request_key, repeats=1):
        if type(repeats) is not int or not 1 <= repeats <= 3: raise ValueError('重复次数为 1 至 3')
        draft = self.authoring.get(owner, draft_id)
        if draft['revision'] != expected_revision: raise UserGrantConflict('草稿修订已变化')
        cases = validate_cases(cases)
        if any(c['profile'] not in draft['bundle']['manifest']['profiles'] for c in cases): raise ValueError('评测模式不存在')
        with closing(self.store.connect()) as db:
            row = db.execute('SELECT payload_json FROM skill_versions WHERE skill_id=? ORDER BY rowid DESC LIMIT 1', (draft['skill_id'],)).fetchone()
        baseline = json.loads(row[0]) if row else None
        return self._enqueue(owner, 'evaluation', {'draft_id': draft_id, 'revision': expected_revision,
            'bundle': draft['bundle'], 'baseline': baseline, 'cases': cases, 'repeats': repeats}, request_key)

    def latest_report(self, db, owner, draft_id, revision):
        return SkillJobQueue.latest_report(db, owner, draft_id, revision)

    def inspect(self, owner, draft_id):
        draft = self.authoring.get(owner, draft_id)
        with closing(self.store.connect()) as db:
            pub = db.execute('SELECT version,payload_json FROM skill_versions WHERE skill_id=? ORDER BY rowid DESC LIMIT 1', (draft['skill_id'],)).fetchone()
            report = self.latest_report(db, owner, draft_id, draft['revision'])
        before = json.loads(pub['payload_json']) if pub else None
        return {'diagnostics': diagnostics(draft['bundle']), 'diff': bundle_diff(before, draft['bundle']),
                'published_version': pub['version'] if pub else None, 'evaluation': report,
                'similar': self.similar(owner, draft['bundle']['manifest']['name'] + draft['bundle']['manifest']['description'], exclude=draft_id)}

    def similar(self, owner, query, exclude=None):
        with closing(self.store.connect()) as db:
            rows = db.execute("SELECT d.draft_id,r.payload_json FROM skill_drafts d JOIN skill_draft_revisions r ON d.draft_id=r.draft_id AND d.revision=r.revision WHERE d.owner_subject=? AND d.state!='archived'", (owner,)).fetchall()
        found = []
        for row in rows:
            if row['draft_id'] == exclude: continue
            m = json.loads(row['payload_json'])['manifest']
            score = similarity(query, m['name'] + m['description'])
            if score >= .2: found.append({'draft_id': row['draft_id'], 'name': m['name'], 'score': round(score, 3)})
        return sorted(found, key=lambda x: -x['score'])[:5]

    def export(self, owner, draft_id):
        return export_standard(self.authoring.get(owner, draft_id)['bundle'])

    def import_package(self, owner, *, package, request_key):
        return self.authoring.save(owner, proposal=import_standard(package), request_key=request_key, provenance={'kind': 'import'})

    def adopt(self, owner, *, job_id, request_key, draft_id=None, expected_revision=None):
        job = self.jobs(owner, job_id)
        candidate = (job.get('result') or {}).get('candidate')
        if job['state'] != 'succeeded' or not candidate: raise ValueError('此任务没有可采用的方法候选')
        return self.authoring.save(owner, proposal=candidate, request_key=request_key,
            draft_id=draft_id, expected_revision=expected_revision,
            provenance={'kind':'revision' if draft_id else 'interaction', 'summary':'用户采用后台方法候选', 'complete':False})

    def feedback(self, owner, *, skill_id, profile, rating, comment='', version=None):
        from bscli.core.skill_authoring import text
        item = self.store.available_item(owner, skill_id, profile)
        if rating not in {'useful', 'incorrect', 'not_applicable'}: raise ValueError('反馈类型无效')
        text(comment, '反馈', 1000, empty=True)
        current = item['manifest']['version']
        if version is not None and version != current: raise UserGrantConflict('版本已变化，请刷新后反馈')
        with closing(self.store.connect()) as db, db:
            db.execute('INSERT INTO skill_feedback VALUES (?,?,?,?,?,?,?)', (str(uuid4()), owner, skill_id, current, rating, comment, timestamp()))
        return {'status': 'recorded', 'version': current}

    def metrics(self, owner=None):
        where, args = (' WHERE owner_subject=?', (owner,)) if owner else ('', ())
        with closing(self.store.connect()) as db:
            feedback = [dict(r) for r in db.execute('SELECT skill_id,version,rating,count(*) AS count FROM skill_feedback' + where + ' GROUP BY skill_id,version,rating', args)]
            jobs = [dict(r) for r in db.execute('SELECT kind,state,count(*) AS count FROM skill_jobs' + where + ' GROUP BY kind,state', args)]
        return {'feedback': feedback, 'jobs': jobs, 'verification': 'user_feedback_not_independent_success_rate'}

    def recipients(self, owner):
        # Expose display identities only, never token/account/login configuration.
        with closing(self.store.connect()) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_accounts'").fetchone()
            rows = db.execute('SELECT DISTINCT user_subject,username FROM workspace_accounts ORDER BY username LIMIT 100').fetchall() if exists else []
        return {'items': [{'subject': owner, 'name': '仅自己'}] + [
            {'subject': r['user_subject'], 'name': r['username']} for r in rows if r['user_subject'] != owner]}

    def resource(self, owner, *, binding_id, path):
        binding = self.store.binding(owner, binding_id)
        from bscli.core.business_skills import validate_binding
        validate_binding(self.authoring.service, owner, binding)
        resources = binding['snapshot']['resources']
        if path not in resources: raise KeyError('参考资料不存在')
        return {'path': path, 'content': resources[path], 'version': binding['version'], 'binding_id': binding_id}

    def discover(self, owner, query='', limit=12):
        if not isinstance(query, str) or len(query) > 2000 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('助手检索参数无效')
        catalog = skill_catalog(self.authoring.service, owner)
        items = catalog['items']
        if query:
            items = sorted(items, key=lambda x: -similarity(query, x['name'] + x['description'] + _json(x['selection'])))
        return {**catalog, 'items': items[:limit], 'has_more': len(items) > limit, 'total': len(items),
                'ranking': 'text_similarity', 'message': '候选排序可能遗漏匹配，必要时查看完整目录'}

    def composition(self, owner, *, steps):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8: raise ValueError('组合需要 1 至 8 个步骤')
        result = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or set(step) != {'skill_id', 'profile', 'input', 'output'}:
                raise ValueError('步骤需要 skill_id/profile/input/output')
            if any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in step.values()):
                raise ValueError('步骤字段需要非空短文本')
            item = self.store.available_item(owner, step['skill_id'], step['profile'])
            from bscli.core.business_skills import dependency_state
            settings = self.store.config('user:' + owner)['value'][step['skill_id']]
            dep = dependency_state(self.authoring.service, owner, item['manifest'], step['profile'], settings.get('source_id'))
            result.append({**step, 'step': i+1, 'version': item['manifest']['version'], 'dependencies': dep})
        return {'steps': result, 'available': all(s['dependencies']['available'] for s in result),
                'execution': '现有持久计划执行；每步加载并重新校验版本和权限，此预检不创建业务授权或执行任务'}

    def start(self, complete, capture=None):
        self._complete = complete
        self._capture = capture
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='skill-workbench', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set(); self._wake.set()
        if self._thread: self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self._capture:
                    with closing(self.store.connect()) as db:
                        pending = db.execute("SELECT * FROM skill_auto_capture WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
                    if pending:
                        self._capture(dict(pending))
                        with closing(self.store.connect()) as db, db:
                            db.execute("UPDATE skill_auto_capture SET state='processed' WHERE dispatch_id=?", (pending['dispatch_id'],))
                worked = self.run_once(self._complete)
            except (sqlite3.Error, OSError):
                if not Path(self.store.db_path).exists(): return
                worked = False
            if not worked:
                self._wake.wait(2); self._wake.clear()

    def claim(self):
        return self._job_queue().claim()

    def _active(self, job, progress=None):
        return self._job_queue().active(job, progress)

    def _finish(self, job, result=None, error=None):
        return self._job_queue().finish(job, result, error)

    def _job_executor(self):
        # Resolve collaborators at use time, including replacements made while a
        # model callback is running. The save port keeps its original transaction.
        return SkillJobExecutor(SkillJobDependencies(
            claim=lambda: self.claim(),
            active=lambda job, *args, **kwargs: self._active(job, *args, **kwargs),
            finish=lambda job, *args, **kwargs: self._finish(job, *args, **kwargs),
            generation=lambda job, payload, complete: self._generation(job, payload, complete),
            evaluation=lambda job, payload, complete: self._evaluation(job, payload, complete),
            call=lambda complete, system, prompt: self._call(complete, system, prompt),
            similar=lambda owner, query, **kwargs: self.similar(owner, query, **kwargs),
            normalize=lambda skill_id, proposal: self.authoring.normalize(skill_id, proposal),
            save=lambda owner, **arguments: self.authoring.save(owner, **arguments),
        ))

    def run_once(self, complete):
        return self._job_executor().run_once(complete)

    @staticmethod
    def _call(complete, system, prompt):
        return SkillJobExecutor.call(complete, system, prompt)

    def _generation(self, job, payload, complete):
        return self._job_executor().generation(job, payload, complete)

    def _evaluation(self, job, payload, complete):
        return self._job_executor().evaluation(job, payload, complete)
