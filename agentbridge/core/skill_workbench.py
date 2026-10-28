"""Durable, identity-scoped authoring and tool-free evaluation jobs."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import re
import threading
import sqlite3
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentbridge.core.business_skills import _json, skill_catalog
from agentbridge.core.skill_quality import (bundle_diff, diagnostics, export_standard,
    import_standard, proposal_from_bundle, publication_evidence, similarity, validate_cases, case_fingerprint, compare_reports,
    validate_pending_cases)
from agentbridge.core.skill_job_executor import SkillJobDependencies, SkillJobExecutor
from agentbridge.core.skill_job_queue import SkillJobQueue
from agentbridge.core.skill_ranking import rank_skill_candidates
from agentbridge.core.user_grants import UserGrantConflict
from agentbridge.core.task_state_rules import TERMINAL_TASK_STATUSES


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class TaskFeedbackError(RuntimeError):
    def __init__(self, code, message, *, feedback=None):
        super().__init__(message)
        self.code, self.feedback = code, feedback


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
                CREATE TABLE IF NOT EXISTS skill_feedback_context (
                    feedback_id TEXT PRIMARY KEY, profile TEXT NOT NULL,
                    source_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_regressions (
                    draft_id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL, job_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_report_reviews (
                    job_id TEXT NOT NULL, row_key TEXT NOT NULL, owner_subject TEXT NOT NULL,
                    status TEXT NOT NULL, note TEXT NOT NULL, version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(job_id,row_key));
                CREATE TABLE IF NOT EXISTS skill_pending_cases (
                    draft_id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL,
                    source_revision INTEGER NOT NULL, version INTEGER NOT NULL,
                    value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_auto_capture (
                    dispatch_id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL,
                    assistant_text TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')

    def status(self):
        return {'mode': 'tool_free_completion', 'worker_configured': self._complete is not None,
                'scope': '合成文字与方法选择评测，不证明真实业务执行成功'}

    def _job_queue(self):
        from agentbridge.core.skill_authoring import text
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
        result = self._job_queue().jobs(owner, job_id)
        with closing(self.store.connect()) as db:
            for job in [result] if job_id else result['items']:
                if job['kind'] == 'evaluation' and job['state'] == 'succeeded':
                    job['result'] = self._report_reviews(db, owner, job['result'])
        return result

    @staticmethod
    def _report_reviews(db, owner, report):
        if not report or not report.get('job_id') or not isinstance(report.get('rows'), list):
            return report
        keys = {row.get('key') for row in report['rows']}
        reviews = {row['row_key']: {k: row[k] for k in ('row_key','status','note','version','updated_at')}
                   for row in db.execute('SELECT * FROM skill_report_reviews WHERE job_id=? AND owner_subject=?',
                                         (report['job_id'], owner)) if row['row_key'] in keys}
        return {**report, 'reviews': reviews}

    def review_report(self, owner, *, job_id, row_key, status, note, expected_version):
        from agentbridge.core.skill_authoring import text
        text(job_id, '评测标识', 128)
        text(row_key, '结果行标识', 128)
        if not isinstance(status, str) or status not in {'unreviewed', 'acceptable', 'problem'}:
            raise ValueError('复核状态无效')
        note = text(note, '复核说明', 1000, empty=True)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError('复核版本无效')
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT result_json FROM skill_jobs WHERE job_id=? AND owner_subject=? AND kind='evaluation' AND state='succeeded'",
                             (job_id, owner)).fetchone()
            if not job:
                raise KeyError('已完成的评测不存在或不可访问')
            report = json.loads(job['result_json'])
            rows = [row for row in report.get('rows', []) if row.get('key') == row_key]
            if len(rows) != 1 or rows[0].get('skipped') or not isinstance(rows[0].get('output'), str) or not rows[0]['output'].strip():
                raise ValueError('只能复核此报告中已生成的输出')
            existing = db.execute('SELECT * FROM skill_report_reviews WHERE job_id=? AND row_key=? AND owner_subject=?',
                                  (job_id, row_key, owner)).fetchone()
            current_version = existing['version'] if existing else 0
            if expected_version != current_version:
                if existing and existing['status'] == status and existing['note'] == note:
                    return {k: existing[k] for k in ('row_key','status','note','version','updated_at')}
                raise UserGrantConflict('此结果的复核已在另一页面修改，请刷新后再试。')
            if existing and existing['status'] == status and existing['note'] == note:
                return {k: existing[k] for k in ('row_key','status','note','version','updated_at')}
            value = {'row_key': row_key, 'status': status, 'note': note,
                     'version': current_version + 1, 'updated_at': timestamp()}
            db.execute('INSERT INTO skill_report_reviews VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_id,row_key) DO UPDATE SET '
                       'status=excluded.status,note=excluded.note,version=excluded.version,updated_at=excluded.updated_at',
                       (job_id, row_key, owner, status, note, value['version'], value['updated_at']))
            return value

    def cancel(self, owner, job_id):
        return self._job_queue().cancel(owner, job_id)

    def retry(self, owner, job_id):
        return self._job_queue().retry(owner, job_id)

    def scopes(self, owner, scope=None, enabled=None):
        from agentbridge.core.skill_authoring import text
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
        from agentbridge.core.skill_authoring import text
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
            for case in cases:
                if case.get('feedback_id') and not db.execute(
                        'SELECT 1 FROM skill_feedback WHERE feedback_id=? AND owner_subject=? AND skill_id=?',
                        (case['feedback_id'], owner, draft['skill_id'])).fetchone():
                    raise KeyError('反馈来源不存在或不属于此助手')
            row = db.execute('SELECT payload_json FROM skill_versions WHERE skill_id=? ORDER BY rowid DESC LIMIT 1', (draft['skill_id'],)).fetchone()
        baseline = json.loads(row[0]) if row else None
        return self._enqueue(owner, 'evaluation', {'draft_id': draft_id, 'revision': expected_revision,
            'bundle': draft['bundle'], 'baseline': baseline, 'cases': cases, 'repeats': repeats}, request_key)

    def latest_report(self, db, owner, draft_id, revision):
        return self._report_reviews(db, owner, SkillJobQueue.latest_report(db, owner, draft_id, revision))

    @staticmethod
    def _pending_cases(db, owner, draft_id):
        row = db.execute('SELECT * FROM skill_pending_cases WHERE draft_id=? AND owner_subject=?', (draft_id,owner)).fetchone()
        return ({'source_revision':row['source_revision'], 'version':row['version'],
                 'value':json.loads(row['value_json']), 'updated_at':row['updated_at']} if row else None)

    def save_pending_cases(self, owner, *, draft_id, expected_revision, expected_version, value):
        from agentbridge.core.skill_authoring import text, revision
        text(draft_id, '草稿标识', 128)
        revision(expected_revision)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError('待测题版本无效')
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            draft = self.authoring._owned(db,owner,draft_id,expected_revision)
            if draft['state'] == 'archived':
                raise ValueError('草稿已归档')
            _, bundle = self.authoring._bundle(db,draft_id,expected_revision)
            value = validate_pending_cases(value,bundle['manifest']['profiles'])
            for case in value['cases']:
                if case.get('feedback_id') and not db.execute(
                        'SELECT 1 FROM skill_feedback WHERE feedback_id=? AND owner_subject=? AND skill_id=?',
                        (case['feedback_id'],owner,draft['skill_id'])).fetchone():
                    raise KeyError('反馈来源不存在或不属于此助手')
            existing = self._pending_cases(db,owner,draft_id)
            current_version = existing['version'] if existing else 0
            if existing and existing['source_revision'] == expected_revision and existing['value'] == value:
                return existing
            if current_version != expected_version:
                raise UserGrantConflict('待测题已在另一页面修改，请刷新后再保存。')
            db.execute('INSERT INTO skill_pending_cases VALUES (?,?,?,?,?,?) ON CONFLICT(draft_id) DO UPDATE SET '
                       'source_revision=excluded.source_revision,version=excluded.version,value_json=excluded.value_json,updated_at=excluded.updated_at',
                       (draft_id,owner,expected_revision,current_version+1,_json(value),timestamp()))
            return self._pending_cases(db,owner,draft_id)

    def _feedback_progress(self, db, owner, draft_id, feedback, pending):
        """Derive progress from existing links, including a later failed or unfinished attempt."""
        pending_ids = {case.get('feedback_id') for case in pending['value']['cases']} if pending else set()
        by_id = {item['feedback_id']:item for item in feedback}
        for item in feedback:
            item['progress'] = {'pending_saved':item['feedback_id'] in pending_ids,'latest_job':None}
        remaining = set(by_id)
        if not remaining:
            return
        jobs = db.execute("SELECT * FROM skill_jobs WHERE owner_subject=? AND kind='evaluation' "
                          "AND json_extract(payload_json,'$.draft_id')=? ORDER BY created_at DESC,rowid DESC", (owner,draft_id))
        for job in jobs:
            payload = json.loads(job['payload_json'])
            linked = {}
            for index, case in enumerate(payload.get('cases',[])):
                if case.get('feedback_id') in remaining:
                    linked.setdefault(case['feedback_id'],[]).append(index)
            if not linked:
                continue
            result = json.loads(job['result_json']) if job['result_json'] else None
            rows = result.get('rows',[]) if result else json.loads(job['progress_json'])
            reviews = self._report_reviews(db,owner,result).get('reviews',{}) if result else {}
            for feedback_id, indexes in linked.items():
                candidate_rows = [r for r in rows if r.get('variant')=='candidate' and r.get('case') in indexes]
                review_counts = {'acceptable':0,'problem':0,'unreviewed':0}
                for row in candidate_rows:
                    if row.get('output') and not row.get('skipped'):
                        review_counts[reviews.get(row.get('key'),{}).get('status','unreviewed')] += 1
                by_id[feedback_id]['progress']['latest_job'] = {
                    'job_id':job['job_id'],'revision':payload['revision'],'state':job['state'],'case_indexes':indexes,
                    'candidate':{'passed':sum(r.get('passed') is True for r in candidate_rows),
                                 'total':len(indexes)*payload.get('repeats',1)}, 'review':review_counts}
                remaining.remove(feedback_id)
            if not remaining:
                break

    def inspect(self, owner, draft_id):
        draft = self.authoring.get(owner, draft_id)
        with closing(self.store.connect()) as db:
            pub = db.execute('SELECT version,payload_json FROM skill_versions WHERE skill_id=? ORDER BY rowid DESC LIMIT 1', (draft['skill_id'],)).fetchone()
            report = self.latest_report(db, owner, draft_id, draft['revision'])
            previous = db.execute("""SELECT job_id,payload_json,created_at FROM skill_jobs
                WHERE owner_subject=? AND kind='evaluation' AND state='succeeded'
                AND json_extract(payload_json,'$.draft_id')=? ORDER BY created_at DESC,rowid DESC LIMIT 1""",
                (owner, draft_id)).fetchone()
            previous_report = None
            if previous:
                payload = json.loads(previous['payload_json'])
                # Reusable inputs are not evidence for this revision. Leave all
                # scores and assertions' previous outcomes out of the template.
                previous_report = {'job_id': previous['job_id'], 'revision': payload['revision'],
                    'content_hash': payload['bundle']['content_hash'], 'cases': payload['cases'],
                    'repeats': payload.get('repeats', 1), 'created_at': previous['created_at']}
            feedback = [self._feedback_public(row) for row in db.execute('''
                SELECT f.*,c.profile,c.source_json FROM skill_feedback f
                LEFT JOIN skill_feedback_context c USING(feedback_id)
                WHERE f.owner_subject=? AND f.skill_id=? ORDER BY f.created_at DESC,f.rowid DESC LIMIT 20''',
                (owner, draft['skill_id']))]
            pending = self._pending_cases(db,owner,draft_id)
            self._feedback_progress(db,owner,draft_id,feedback,pending)
        before = json.loads(pub['payload_json']) if pub else None
        regression = self.regression(owner, draft_id)
        source = self.jobs(owner, regression['job_id'])['result'] if regression and report else None
        return {'diagnostics': diagnostics(draft['bundle']), 'diff': bundle_diff(before, draft['bundle']),
                'published_version': pub['version'] if pub else None, 'evaluation': report,
                'regression': regression, 'comparison': compare_reports(source, report),
                'previous_report': previous_report, 'pending_cases':pending, 'feedback': {'items': feedback},
                'publication_evidence': publication_evidence(draft['bundle'], draft['revision'], draft['tested_profiles'], report),
                'similar': self.similar(owner, draft['bundle']['manifest']['name'] + draft['bundle']['manifest']['description'], exclude=draft_id)}

    def set_regression(self, owner, *, draft_id, expected_revision, job_id):
        from agentbridge.core.skill_authoring import text, revision
        text(draft_id, "草稿标识", 128)
        revision(expected_revision)
        if job_id is not None: text(job_id, "评测标识", 128)
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self.authoring._owned(db, owner, draft_id, expected_revision)
            if job_id is None:
                db.execute('DELETE FROM skill_regressions WHERE draft_id=? AND owner_subject=?', (draft_id, owner))
            else:
                row = db.execute("SELECT payload_json FROM skill_jobs WHERE job_id=? AND owner_subject=? AND kind='evaluation' AND state='succeeded'",
                                 (job_id, owner)).fetchone()
                if not row or json.loads(row[0]).get('draft_id') != draft_id:
                    raise KeyError('只能固定此助手已完成的评测题目')
                db.execute('INSERT INTO skill_regressions VALUES (?,?,?) ON CONFLICT(draft_id) DO UPDATE SET job_id=excluded.job_id',
                           (draft_id, owner, job_id))
        return self.regression(owner, draft_id)

    def regression(self, owner, draft_id):
        with closing(self.store.connect()) as db:
            self.authoring._owned(db, owner, draft_id)
            row = db.execute('''SELECT j.job_id,j.payload_json FROM skill_regressions r JOIN skill_jobs j USING(job_id)
                WHERE r.draft_id=? AND r.owner_subject=? AND j.owner_subject=? AND j.state='succeeded' ''', (draft_id, owner, owner)).fetchone()
        if not row: return None
        payload = json.loads(row['payload_json'])
        return {'job_id': row['job_id'], 'revision': payload['revision'], 'cases': payload['cases'],
                'repeats': payload['repeats'], 'case_fingerprint': case_fingerprint(payload['cases'], payload['repeats'])}

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

    @staticmethod
    def _feedback_public(row):
        item = dict(row)
        item['profile'] = item.get('profile') or 'unknown'
        item['source'] = json.loads(item.pop('source_json') or '{}')
        item.pop('owner_subject', None)
        return item

    @staticmethod
    def _task_receipt(row):
        return {"status": "recorded", **{key: row[key] for key in
                ("feedback_id", "rating", "comment", "version", "profile")}}

    def _task_feedback_context(self, db, owner, task_id):
        task = self.authoring.service.tasks._select_owned_task(db, task_id, owner)
        binding = db.execute('''SELECT b.* FROM skill_task_bindings t JOIN skill_bindings b USING(binding_id)
            WHERE t.task_id=? AND t.user_subject=? AND b.user_subject=?''', (task_id, owner, owner)).fetchone()
        receipt = None
        if binding:
            # Only this task's original binding is attribution; current catalog
            # settings and revoked execution permission do not rewrite history.
            receipt = db.execute('''SELECT f.*,c.profile,c.source_json FROM skill_feedback f
                JOIN skill_feedback_context c USING(feedback_id)
                WHERE f.owner_subject=? AND json_extract(c.source_json,'$.task_id')=?
                  AND json_extract(c.source_json,'$.binding_id')=?
                  AND f.skill_id=? AND f.version=? AND c.profile=?
                ORDER BY f.created_at,f.feedback_id LIMIT 1''',
                (owner, task_id, binding['binding_id'], binding['skill_id'], binding['version'], binding['profile'])).fetchone()
        return task, binding, receipt

    def task_feedback(self, owner, task_id):
        """Read attribution only; this does not grant access to task results."""
        with closing(self.store.connect()) as db:
            task, binding, receipt = self._task_feedback_context(db, owner, task_id)
            version_exists = bool(binding and db.execute('SELECT 1 FROM skill_versions WHERE skill_id=? AND version=?',
                (binding['skill_id'], binding['version'])).fetchone())
            return {'available': bool(version_exists and task['status'] in TERMINAL_TASK_STATUSES and not receipt),
                    'receipt': self._task_receipt(receipt) if receipt else None}

    def feedback(self, owner, *, skill_id, profile, rating, comment='', version=None, test_id=None, binding_id=None):
        """Preserve the published skill-action contract for existing callers."""
        return self._record_feedback(owner, skill_id=skill_id, profile=profile, rating=rating, comment=comment,
            version=version, test_id=test_id, binding_id=binding_id)

    @staticmethod
    def _load_feedback_context(db, owner, event_id):
        row = db.execute('''SELECT e.payload_json,b.* FROM skill_load_events e JOIN skill_bindings b ON e.dedupe_key=b.binding_id
            WHERE e.event_id=? AND e.user_subject=? AND b.user_subject=?''', (event_id, owner, owner)).fetchone()
        if not row: raise KeyError('使用记录没有可核实的原版本绑定')
        event = json.loads(row['payload_json'])
        if event.get('status') != 'succeeded' or any(event.get(k) != row[k] for k in ('skill_id','version','profile')):
            raise KeyError('使用记录与原版本绑定不一致')
        return row

    def load_feedback_history(self, owner):
        items = self.store.load_history(owner)
        with closing(self.store.connect()) as db:
            for item in items:
                item['feedback'] = {'available': False, 'receipt': None}
                try: self._load_feedback_context(db, owner, item['event_id'])
                except KeyError: continue
                fid = str(uuid5(NAMESPACE_URL, _json(['agentbridge.load-feedback.v1', owner, item['event_id']])))
                row = db.execute('SELECT f.*,c.profile FROM skill_feedback f JOIN skill_feedback_context c USING(feedback_id) WHERE f.feedback_id=? AND f.owner_subject=?', (fid, owner)).fetchone()
                item['feedback'] = {'available': row is None, 'receipt': self._task_receipt(row) if row else None}
        return items

    def record_load_feedback(self, owner, event_id, *, rating, comment, request_key):
        if not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', request_key):
            raise ValueError('反馈请求标识无效')
        return self._record_feedback(owner, load_event_id=event_id, rating=rating, comment=comment)

    def record_task_feedback(self, owner, task_id, *, rating, comment, request_key):
        """Task attribution is server-owned and separate from skill tool inputs."""
        return self._record_feedback(owner, task_id=task_id, rating=rating, comment=comment, request_key=request_key)

    def _record_feedback(self, owner, *, skill_id=None, profile=None, rating, comment='', version=None,
                         test_id=None, binding_id=None, task_id=None, request_key=None, load_event_id=None):
        from agentbridge.core.skill_authoring import text
        if not isinstance(rating, str) or rating not in {'useful', 'incorrect', 'not_applicable'}: raise ValueError('反馈类型无效')
        comment = text(comment, '反馈', 1000, empty=True)
        if test_id is not None and binding_id is not None: raise ValueError('反馈只能关联一种来源')
        if (task_id is None) != (request_key is None): raise ValueError('原任务反馈需要任务和请求标识')
        if task_id is not None:
            if any(value is not None for value in (skill_id, profile, version, test_id, binding_id)):
                raise ValueError('原任务反馈的业务助手由服务器确定')
            text(task_id, '任务标识', 128)
            if not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', request_key):
                raise ValueError('反馈请求标识无效')
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if load_event_id is not None:
                binding = self._load_feedback_context(db, owner, load_event_id)
                binding_id, skill_id, profile = (binding[k] for k in ('binding_id', 'skill_id', 'profile'))
                fid = str(uuid5(NAMESPACE_URL, _json(['agentbridge.load-feedback.v1', owner, load_event_id])))
                existing = db.execute('SELECT f.*,c.profile FROM skill_feedback f JOIN skill_feedback_context c USING(feedback_id) WHERE f.feedback_id=? AND f.owner_subject=?', (fid, owner)).fetchone()
                if existing:
                    receipt = self._task_receipt(existing)
                    if existing['rating'] != rating or existing['comment'] != comment:
                        raise TaskFeedbackError('SKILL_FEEDBACK_CONFLICT', '这次使用已有评价，不能覆盖。', feedback={'available': False, 'receipt': receipt})
                    return receipt
            if task_id is not None:
                task, task_binding, existing = self._task_feedback_context(db, owner, task_id)
                if existing:
                    receipt = self._task_receipt(existing)
                    if existing['rating'] != rating or existing['comment'] != comment:
                        raise TaskFeedbackError('TASK_FEEDBACK_CONFLICT', '此任务已有反馈，原反馈不能被另一页面覆盖。',
                            feedback={'available': False, 'receipt': receipt})
                    return receipt
                if not task_binding: raise KeyError('原任务没有可反馈的业务助手绑定')
                if task['status'] not in TERMINAL_TASK_STATUSES:
                    raise TaskFeedbackError('TASK_FEEDBACK_NOT_AVAILABLE', '任务尚未结束，请在任务结束后反馈。')
                binding_id, skill_id, profile = (task_binding[key] for key in ('binding_id', 'skill_id', 'profile'))
            if test_id is not None:
                text(test_id, '试用标识', 128)
                sample = db.execute('''SELECT t.*,d.skill_id FROM skill_draft_tests t JOIN skill_drafts d USING(draft_id)
                    WHERE t.test_id=? AND t.owner_subject=? AND d.owner_subject=?''', (test_id, owner, owner)).fetchone()
                if not sample: raise KeyError('试用不存在或不可访问')
                if sample['status'] != 'reported': raise ValueError('只能反馈已保存的试用')
                if sample['skill_id'] != skill_id or sample['profile'] != profile:
                    raise ValueError('反馈助手或模式与试用不一致')
                snapshot, _ = self.authoring._bundle(db, sample['draft_id'], sample['revision'])
                current = 'draft:' + str(sample['revision'])
                source = {'kind': 'sample', 'test_id': test_id, 'draft_id': sample['draft_id'],
                    'revision': sample['revision'], 'profile': profile, 'version': current,
                    'content_hash': snapshot['content_hash'], 'prompt': sample['prompt']}
            elif binding_id is not None:
                text(binding_id, '任务绑定', 128)
                # Feedback describes the owner's historical use; it never
                # executes that binding or restores revoked business access.
                binding = db.execute('SELECT * FROM skill_bindings WHERE binding_id=? AND user_subject=?',
                                     (binding_id, owner)).fetchone()
                if not binding: raise KeyError('任务绑定不存在或不可访问')
                if binding['skill_id'] != skill_id or binding['profile'] != profile:
                    raise ValueError('反馈助手或模式与任务绑定不一致')
                snapshot = db.execute('SELECT content_hash FROM skill_versions WHERE skill_id=? AND version=?',
                                      (skill_id, binding['version'])).fetchone()
                if not snapshot: raise ValueError('任务原版本不可用')
                current = binding['version']
                source = {'kind': 'binding', 'binding_id': binding_id, 'profile': profile,
                          'version': current, 'content_hash': snapshot['content_hash']}
                publication = db.execute('''SELECT d.draft_id,q.draft_revision FROM skill_review_requests q
                    JOIN skill_drafts d USING(draft_id) WHERE d.skill_id=? AND d.owner_subject=?
                    AND q.published_version=? AND q.state='published' LIMIT 1''', (skill_id, owner, current)).fetchone()
                if publication:
                    source.update(draft_id=publication['draft_id'], revision=publication['draft_revision'])
            else:
                item = self.store.available_item(owner, skill_id, profile)
                current = item['manifest']['version']
                source = {'kind': 'catalog', 'profile': profile, 'version': current,
                          'content_hash': item['content_hash']}
            if version is not None and version != current: raise UserGrantConflict('版本与反馈来源不一致，请刷新后反馈')
            if task_id is not None:
                source.update(task_id=task_id, task_status=task['status'], request_key=request_key)
                feedback_id = str(uuid5(NAMESPACE_URL, _json(['agentbridge.task-feedback.v1', owner, task_id, binding_id, request_key])))
            elif load_event_id is not None:
                source['load_event_id'] = load_event_id
                feedback_id = fid
            else:
                feedback_id = str(uuid4())
            # Keep the original seven-column table writable by the previous
            # release; attribution is an atomic, optional companion record.
            db.execute('INSERT INTO skill_feedback VALUES (?,?,?,?,?,?,?)',
                       (feedback_id, owner, skill_id, current, rating, comment, timestamp()))
            db.execute('INSERT INTO skill_feedback_context VALUES (?,?,?)', (feedback_id, profile, _json(source)))
        if task_id is not None or load_event_id is not None:
            return {'status': 'recorded', 'feedback_id': feedback_id, 'version': current, 'profile': profile,
                    'rating': rating, 'comment': comment}
        return {'status': 'recorded', 'feedback_id': feedback_id, 'version': current, 'profile': profile, 'source': source}

    def metrics(self, owner=None):
        where, args = (' WHERE owner_subject=?', (owner,)) if owner else ('', ())
        with closing(self.store.connect()) as db:
            feedback = [dict(r) for r in db.execute("SELECT f.skill_id,f.version,COALESCE(c.profile,'unknown') AS profile,f.rating,count(*) AS count "
                'FROM skill_feedback f LEFT JOIN skill_feedback_context c USING(feedback_id)' + where +
                " GROUP BY f.skill_id,f.version,COALESCE(c.profile,'unknown'),f.rating", args)]
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
        from agentbridge.core.business_skills import validate_binding
        validate_binding(self.authoring.service, owner, binding)
        resources = binding['snapshot']['resources']
        if path not in resources: raise KeyError('参考资料不存在')
        return {'path': path, 'content': resources[path], 'version': binding['version'], 'binding_id': binding_id}

    def discover(self, owner, query='', limit=12):
        if not isinstance(query, str) or len(query) > 2000 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('助手检索参数无效')
        catalog = skill_catalog(self.authoring.service, owner)
        items = rank_skill_candidates(catalog['items'], query)
        return {**catalog, 'items': items[:limit], 'has_more': len(items) > limit, 'total': len(items),
                'ranking': 'explicit_identity_then_positive_text',
                'message': '名称或 ID 命中优先，仍须检查适用范围与权限；必要时查看完整目录'}

    def composition(self, owner, *, steps):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8: raise ValueError('组合需要 1 至 8 个步骤')
        result = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or set(step) != {'skill_id', 'profile', 'input', 'output'}:
                raise ValueError('步骤需要 skill_id/profile/input/output')
            if any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in step.values()):
                raise ValueError('步骤字段需要非空短文本')
            item = self.store.available_item(owner, step['skill_id'], step['profile'])
            from agentbridge.core.business_skills import dependency_state
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
