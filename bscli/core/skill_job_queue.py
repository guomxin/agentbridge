"""Durable skill jobs with explicit storage, event, and worker dependencies.

Schema initialization and atomic draft publication remain with the authoring
workbench. This queue never calls a model or saves an authored draft.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
from typing import Callable
from uuid import uuid4

from bscli.core.business_skills import _json
from bscli.core.user_grants import UserGrantConflict


class SkillJobQueue:
    def __init__(
        self,
        *,
        connect: Callable[[], sqlite3.Connection],
        event: Callable[[sqlite3.Connection, str, str, str, dict], None],
        wake: Callable[[], None],
        timestamp: Callable[[], str],
        validate_text: Callable[[object, str, int], str],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._connect = connect
        self._event = event
        self._wake = wake
        self._timestamp = timestamp
        self._validate_text = validate_text
        self._now = now if now is not None else lambda: datetime.now(timezone.utc)

    def enqueue(self, owner, kind, payload, request_key):
        self._validate_text(request_key, '请求标识', 128)
        digest = sha256(_json({'kind': kind, 'payload': payload}).encode()).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute(
                'SELECT * FROM skill_jobs WHERE owner_subject=? AND request_key=?',
                (owner, request_key),
            ).fetchone()
            if old:
                if old['input_hash'] != digest:
                    raise UserGrantConflict('请求标识已用于其他后台任务')
                return self.public(old)
            if payload.get('automatic'):
                if not self.auto_allowed(db, owner, payload['scope']):
                    raise PermissionError('自动沉淀范围已关闭')
                count = db.execute(
                    "SELECT count(*) FROM skill_jobs WHERE owner_subject=? AND created_at>=? "
                    "AND json_extract(payload_json,'$.automatic')=1",
                    (owner, self._timestamp()[:10]),
                ).fetchone()[0]
                if count >= 3:
                    raise ValueError('今日自动沉淀已达 3 次')
            count = db.execute(
                'SELECT count(*) FROM skill_jobs WHERE owner_subject=? AND created_at>=?',
                (owner, self._timestamp()[:10]),
            ).fetchone()[0]
            active = db.execute(
                "SELECT count(*) FROM skill_jobs WHERE owner_subject=? AND state IN ('queued','running')",
                (owner,),
            ).fetchone()[0]
            if count >= 20 or active >= 3:
                raise ValueError('后台任务额度已满，请等待或明日重试')
            job_id = str(uuid4())
            db.execute(
                'INSERT INTO skill_jobs VALUES (?,?,?,?,?,?,?,NULL,?,0,NULL,NULL,NULL,?,?)',
                (job_id, owner, kind, 'queued', request_key, digest, _json(payload),
                 '[]', self._timestamp(), self._timestamp()),
            )
            self._event(db, owner, 'job.queued', job_id, {'kind': kind})
            row = db.execute('SELECT * FROM skill_jobs WHERE job_id=?', (job_id,)).fetchone()
        self._wake()
        return self.public(row)

    @staticmethod
    def public(row):
        payload = json.loads(row['payload_json'])
        return {
            key: row[key]
            for key in ('job_id', 'kind', 'state', 'attempts', 'error', 'created_at', 'updated_at')
        } | {
            'draft_id': payload.get('draft_id'), 'revision': payload.get('revision'),
            'automatic': payload.get('automatic', False), 'scope': payload.get('scope'),
            'progress': len(json.loads(row['progress_json'])),
            'result': json.loads(row['result_json']) if row['result_json'] else None,
        }

    def jobs(self, owner, job_id=None):
        with closing(self._connect()) as db:
            if job_id:
                row = db.execute(
                    'SELECT * FROM skill_jobs WHERE job_id=? AND owner_subject=?',
                    (job_id, owner),
                ).fetchone()
                if not row:
                    raise KeyError('后台任务不存在或不可访问')
                return self.public(row)
            return {'items': [self.public(row) for row in db.execute(
                'SELECT * FROM skill_jobs WHERE owner_subject=? ORDER BY created_at DESC LIMIT 50',
                (owner,),
            )]}

    def cancel(self, owner, job_id):
        with closing(self._connect()) as db, db:
            row = db.execute(
                'SELECT * FROM skill_jobs WHERE job_id=? AND owner_subject=?',
                (job_id, owner),
            ).fetchone()
            if not row:
                raise KeyError('后台任务不存在或不可访问')
            if row['state'] in {'queued', 'running'}:
                db.execute(
                    "UPDATE skill_jobs SET state='canceled',claim_token=NULL,updated_at=? WHERE job_id=?",
                    (self._timestamp(), job_id),
                )
                self._event(db, owner, 'job.canceled', job_id, {})
        return self.jobs(owner, job_id)

    def retry(self, owner, job_id):
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(
                'SELECT * FROM skill_jobs WHERE job_id=? AND owner_subject=?',
                (job_id, owner),
            ).fetchone()
            if not row:
                raise KeyError('后台任务不存在或不可访问')
            if row['state'] != 'failed' or row['attempts'] >= 3:
                raise ValueError('仅可重试失败任务，最多尝试三次')
            payload = json.loads(row['payload_json'])
            if payload.get('automatic') and not self.auto_allowed(db, owner, payload['scope']):
                raise PermissionError('自动沉淀已关闭')
            db.execute(
                "UPDATE skill_jobs SET state='queued',error=NULL,claim_token=NULL,updated_at=? WHERE job_id=?",
                (self._timestamp(), job_id),
            )
        self._wake()
        return self.jobs(owner, job_id)

    @staticmethod
    def auto_allowed(db, owner, scope):
        pref = db.execute(
            'SELECT value_json FROM skill_authoring_preferences WHERE owner_subject=?', (owner,),
        ).fetchone()
        if not pref or not json.loads(pref[0]).get('auto_draft'):
            return False
        row = db.execute(
            'SELECT enabled FROM skill_auto_scopes WHERE owner_subject=? AND scope=?',
            (owner, scope),
        ).fetchone()
        return bool(row and row[0])

    @staticmethod
    def latest_report(db, owner, draft_id, revision):
        row = db.execute(
            "SELECT result_json FROM skill_jobs WHERE owner_subject=? AND kind='evaluation' "
            "AND state='succeeded' AND json_extract(payload_json,'$.draft_id')=? "
            "AND json_extract(payload_json,'$.revision')=? ORDER BY created_at DESC LIMIT 1",
            (owner, draft_id, revision),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def claim(self):
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute(
                "UPDATE skill_jobs SET state='failed',error='工作进程中断，已达重试上限',claim_token=NULL "
                "WHERE state='running' AND lease_until<? AND attempts>=3",
                (self._timestamp(),),
            )
            row = db.execute(
                "SELECT * FROM skill_jobs WHERE state='queued' OR "
                "(state='running' AND lease_until<? AND attempts<3) ORDER BY created_at LIMIT 1",
                (self._timestamp(),),
            ).fetchone()
            if not row:
                return None
            token = str(uuid4())
            db.execute(
                "UPDATE skill_jobs SET state='running',attempts=attempts+1,claim_token=?,lease_until=?,updated_at=? WHERE job_id=?",
                (token, (self._now() + timedelta(minutes=5)).isoformat(),
                 self._timestamp(), row['job_id']),
            )
            return dict(db.execute('SELECT * FROM skill_jobs WHERE job_id=?', (row['job_id'],)).fetchone())

    def active(self, job, progress=None):
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT * FROM skill_jobs WHERE job_id=? AND claim_token=? AND state='running'",
                (job['job_id'], job['claim_token']),
            ).fetchone()
            if not row:
                raise UserGrantConflict('后台任务已取消或由其他进程接续')
            payload = json.loads(row['payload_json'])
            if payload.get('automatic') and not self.auto_allowed(db, row['owner_subject'], payload['scope']):
                raise PermissionError('自动沉淀已关闭')
            db.execute(
                'UPDATE skill_jobs SET lease_until=?,progress_json=?,updated_at=? WHERE job_id=?',
                ((self._now() + timedelta(minutes=5)).isoformat(),
                 _json(progress) if progress is not None else row['progress_json'],
                 self._timestamp(), job['job_id']),
            )

    def finish(self, job, result=None, error=None):
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE skill_jobs SET state=?,result_json=?,error=?,claim_token=NULL,updated_at=? "
                "WHERE job_id=? AND claim_token=? AND state='running'",
                ('failed' if error else 'succeeded', _json(result) if result is not None else None,
                 error, self._timestamp(), job['job_id'], job['claim_token']),
            )
