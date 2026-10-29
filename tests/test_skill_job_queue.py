"""Exercise the extracted queue against real, temporary SQLite transactions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
from uuid import uuid4

import pytest

from agentbridge.core.skill_authoring import text
from agentbridge.core.skill_job_queue import SkillJobQueue
from agentbridge.core.user_grants import UserGrantConflict


class QueueFixture:
    def __init__(self, path):
        self.path = path
        self.time = datetime(2026, 10, 4, 4, 0, tzinfo=timezone.utc)
        self.events = []
        self.wakes = []
        self.fail_event = None
        with closing(self.connect()) as db:
            # Production schema initialization remains the workbench's job.
            db.executescript('''
                CREATE TABLE skill_jobs (
                    job_id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL, kind TEXT NOT NULL,
                    state TEXT NOT NULL, request_key TEXT NOT NULL, input_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL, result_json TEXT, progress_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT, lease_until TEXT,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner_subject,request_key));
                CREATE INDEX skill_jobs_pending ON skill_jobs(state,created_at);
                CREATE TABLE skill_auto_scopes (
                    owner_subject TEXT NOT NULL, scope TEXT NOT NULL, enabled INTEGER NOT NULL,
                    PRIMARY KEY(owner_subject,scope));
                CREATE TABLE skill_authoring_preferences (
                    owner_subject TEXT PRIMARY KEY, revision INTEGER NOT NULL, value_json TEXT NOT NULL);
                CREATE TABLE skill_authoring_events (
                    event_id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL, kind TEXT NOT NULL,
                    object_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')
        self.queue = self.make_queue()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def stamp(self):
        return self.time.isoformat()

    def event(self, db, owner, kind, object_id, payload):
        assert db.in_transaction
        state = db.execute('SELECT state FROM skill_jobs WHERE job_id=?', (object_id,)).fetchone()[0]
        assert state == {'job.queued': 'queued', 'job.canceled': 'canceled', 'job.claimed': 'running'}[kind]
        db.execute('INSERT INTO skill_authoring_events VALUES (?,?,?,?,?,?)',
                   (str(uuid4()), owner, kind, object_id, json.dumps(payload), self.stamp()))
        self.events.append((owner, kind, object_id, payload))
        if self.fail_event == kind:
            raise RuntimeError('event storage unavailable')

    def wake(self):
        with closing(self.connect()) as db:
            # A separate connection can see the committed enqueue/retry.
            self.wakes.append(db.execute("SELECT count(*) FROM skill_jobs WHERE state='queued'").fetchone()[0])

    def make_queue(self):
        return SkillJobQueue(connect=self.connect, event=self.event, wake=self.wake,
                             timestamp=self.stamp, validate_text=text, now=lambda: self.time)

    def allow(self, owner='alice', scope='chat', *, preference=True, enabled=True):
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO skill_authoring_preferences VALUES (?,1,?)',
                       (owner, json.dumps({'auto_draft': preference})))
            db.execute('INSERT OR REPLACE INTO skill_auto_scopes VALUES (?,?,?)',
                       (owner, scope, int(enabled)))

    def raw(self, job_id):
        with closing(self.connect()) as db:
            return dict(db.execute('SELECT * FROM skill_jobs WHERE job_id=?', (job_id,)).fetchone())

    def complete(self, owner, key, *, kind='evaluation', payload=None, error=None):
        job = self.queue.enqueue(owner, kind, payload or {}, key)
        claimed = self.queue.claim()
        assert claimed['job_id'] == job['job_id']
        self.queue.finish(claimed, result={'key': key}, error=error)
        self.time += timedelta(seconds=1)
        return job


@pytest.fixture
def fixture(tmp_path):
    return QueueFixture(tmp_path / 'queue.db')


def test_constructor_does_not_connect_initialize_schema_or_wake():
    def forbidden(*_args):
        raise AssertionError('construction must only bind dependencies')
    SkillJobQueue(connect=forbidden, event=forbidden, wake=forbidden,
                  timestamp=forbidden, validate_text=forbidden)


def test_enqueue_event_and_job_commit_together_before_wake(fixture):
    job = fixture.queue.enqueue('alice', 'generation', {'material': 'synthetic'}, 'new')
    assert fixture.wakes == [1]
    with closing(fixture.connect()) as db:
        row = db.execute('SELECT * FROM skill_authoring_events').fetchone()
        assert row['object_id'] == job['job_id']
        assert row['owner_subject'] == 'alice'
        assert row['kind'] == 'job.queued'
        assert json.loads(row['payload_json']) == {'kind': 'generation'}


def test_event_failure_rolls_back_enqueue_and_never_wakes(fixture):
    fixture.fail_event = 'job.queued'
    with pytest.raises(RuntimeError, match='event storage unavailable'):
        fixture.queue.enqueue('alice', 'evaluation', {}, 'new')
    with closing(fixture.connect()) as db:
        assert db.execute('SELECT count(*) FROM skill_jobs').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM skill_authoring_events').fetchone()[0] == 0
    assert fixture.wakes == []
    fixture.fail_event = None
    assert fixture.queue.enqueue('alice', 'evaluation', {}, 'new')['state'] == 'queued'


@pytest.mark.parametrize('request_key', [None, '', ' \t', 'x' * 129])
def test_request_key_validation_keeps_original_error_and_has_no_effect(fixture, request_key):
    with pytest.raises(ValueError, match='请求标识不能为空且不能超过128字符'):
        fixture.queue.enqueue('alice', 'evaluation', {}, request_key)
    assert fixture.queue.jobs('alice') == {'items': []}
    assert fixture.wakes == []


def test_idempotent_reuse_precedes_quota_and_disabled_automatic_scope(fixture):
    fixture.allow()
    payload = {'automatic': True, 'scope': 'chat', 'material': 'synthetic'}
    first = fixture.queue.enqueue('alice', 'generation', payload, 'same')
    fixture.queue.enqueue('alice', 'evaluation', {}, 'second')
    fixture.queue.enqueue('alice', 'evaluation', {}, 'third')
    fixture.allow(preference=False, enabled=False)
    assert fixture.queue.enqueue('alice', 'generation', dict(reversed(list(payload.items()))), 'same') == first
    assert len(fixture.wakes) == 3
    with pytest.raises(UserGrantConflict, match='请求标识已用于其他后台任务'):
        fixture.queue.enqueue('alice', 'generation', {**payload, 'material': 'different'}, 'same')
    with pytest.raises(UserGrantConflict):
        fixture.queue.enqueue('alice', 'evaluation', payload, 'same')
    assert len(fixture.queue.jobs('alice')['items']) == 3


def test_owner_isolation_public_shape_and_newest_first(fixture):
    first = fixture.queue.enqueue('alice', 'generation', {'material': 'private'}, 'same')
    fixture.time += timedelta(seconds=1)
    second = fixture.queue.enqueue('alice', 'evaluation', {'draft_id': 'draft', 'revision': 2}, 'second')
    bob = fixture.queue.enqueue('bob', 'generation', {'material': 'other'}, 'same')
    assert bob['job_id'] != first['job_id']
    assert [job['job_id'] for job in fixture.queue.jobs('alice')['items']] == [second['job_id'], first['job_id']]
    for operation in (fixture.queue.jobs, fixture.queue.cancel, fixture.queue.retry):
        with pytest.raises(KeyError, match='后台任务不存在或不可访问'):
            operation('bob', first['job_id'])
    assert set(first) == {'job_id', 'kind', 'state', 'attempts', 'error', 'created_at', 'updated_at',
                          'draft_id', 'revision', 'name', 'automatic', 'scope', 'progress', 'progress_stats', 'result'}
    assert first['progress'] == 0 and first['result'] is None


def test_concurrent_identical_enqueue_creates_one_job_and_one_event(fixture):
    barrier = threading.Barrier(2)
    def enqueue():
        queue = fixture.make_queue()
        barrier.wait(timeout=5)
        return queue.enqueue('alice', 'evaluation', {'draft_id': 'draft'}, 'same')
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = list(executor.map(lambda _: enqueue(), range(2)))
    assert jobs[0]['job_id'] == jobs[1]['job_id']
    assert len(fixture.queue.jobs('alice')['items']) == 1
    assert len(fixture.events) == len(fixture.wakes) == 1


def test_concurrent_enqueue_cannot_exceed_three_active_jobs(fixture):
    barrier = threading.Barrier(6)
    def enqueue(number):
        queue = fixture.make_queue()
        barrier.wait(timeout=5)
        try:
            return queue.enqueue('alice', 'evaluation', {}, f'job-{number}')
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=6) as executor:
        outcomes = list(executor.map(enqueue, range(6)))
    assert sum(isinstance(value, dict) for value in outcomes) == 3
    assert outcomes.count('后台任务额度已满，请等待或明日重试') == 3
    assert len(fixture.events) == len(fixture.wakes) == 3
    assert fixture.queue.enqueue('bob', 'evaluation', {}, 'independent')['state'] == 'queued'


def test_daily_quota_counts_finished_jobs_and_resets_by_timestamp_day(fixture):
    first = None
    for number in range(20):
        job = fixture.complete('alice', f'job-{number}')
        first = first or job
    with pytest.raises(ValueError, match='后台任务额度已满'):
        fixture.queue.enqueue('alice', 'evaluation', {}, 'overflow')
    assert fixture.queue.enqueue('alice', 'evaluation', {}, 'job-0')['job_id'] == first['job_id']
    fixture.time += timedelta(days=1)
    assert fixture.queue.enqueue('alice', 'evaluation', {}, 'next-day')['state'] == 'queued'


def test_automatic_quota_is_three_per_owner_across_scopes_and_preserves_manual_jobs(fixture):
    fixture.allow(scope='first')
    fixture.allow(scope='second')
    for number, scope in enumerate(('first', 'second', 'first')):
        fixture.complete('alice', f'auto-{number}', kind='generation',
                         payload={'automatic': True, 'scope': scope})
    with pytest.raises(ValueError, match='今日自动沉淀已达 3 次'):
        fixture.queue.enqueue('alice', 'generation', {'automatic': True, 'scope': 'second'}, 'fourth')
    assert fixture.queue.enqueue('alice', 'generation', {}, 'manual')['state'] == 'queued'
    fixture.allow(owner='bob', scope='first')
    assert fixture.queue.enqueue('bob', 'generation', {'automatic': True, 'scope': 'first'}, 'auto')['state'] == 'queued'


def test_auto_allowed_uses_the_callers_transaction_and_both_owner_scope_gates(fixture):
    with closing(fixture.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO skill_authoring_preferences VALUES (?,1,?)', ('alice', '{"auto_draft":true}'))
        db.execute('INSERT INTO skill_auto_scopes VALUES (?,?,1)', ('alice', 'chat'))
        assert SkillJobQueue.auto_allowed(db, 'alice', 'chat') is True
        assert SkillJobQueue.auto_allowed(db, 'alice', 'other') is False
        assert SkillJobQueue.auto_allowed(db, 'bob', 'chat') is False
        with closing(fixture.connect()) as outsider:
            assert SkillJobQueue.auto_allowed(outsider, 'alice', 'chat') is False
        db.rollback()
    fixture.allow(preference=False)
    with pytest.raises(PermissionError, match='自动沉淀范围已关闭'):
        fixture.queue.enqueue('alice', 'generation', {'automatic': True, 'scope': 'chat'}, 'disabled')
    fixture.allow(enabled=False)
    with closing(fixture.connect()) as db:
        assert SkillJobQueue.auto_allowed(db, 'alice', 'chat') is False


def test_concurrent_claims_are_exclusive_and_follow_creation_order(fixture):
    first = fixture.queue.enqueue('alice', 'evaluation', {}, 'first')
    fixture.time += timedelta(seconds=1)
    second = fixture.queue.enqueue('bob', 'evaluation', {}, 'second')
    barrier = threading.Barrier(2)
    def claim():
        queue = fixture.make_queue()
        barrier.wait(timeout=5)
        return queue.claim()
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: claim(), range(2)))
    assert {job['job_id'] for job in claims} == {first['job_id'], second['job_id']}
    assert len({job['claim_token'] for job in claims}) == 2
    assert all(job['attempts'] == 1 for job in claims)
    assert fixture.queue.claim() is None
    assert {job['lease_until'] for job in claims} == {(fixture.time + timedelta(minutes=5)).isoformat()}


def test_claim_selects_the_oldest_job_before_newer_owners(fixture):
    first = fixture.queue.enqueue('alice', 'evaluation', {}, 'first')
    fixture.time += timedelta(seconds=1)
    fixture.queue.enqueue('bob', 'evaluation', {}, 'second')
    assert fixture.queue.claim()['job_id'] == first['job_id']


def test_lease_renewal_progress_and_expired_reclaim_reject_stale_worker(fixture):
    queued = fixture.queue.enqueue('alice', 'evaluation', {}, 'job')
    old = fixture.queue.claim()
    fixture.time += timedelta(minutes=1)
    progress = [{'key': '0:0:candidate', 'passed': True}]
    fixture.queue.active(old, progress)
    renewed = fixture.raw(queued['job_id'])
    assert renewed['lease_until'] == (fixture.time + timedelta(minutes=5)).isoformat()
    fixture.queue.active(old)
    assert json.loads(fixture.raw(queued['job_id'])['progress_json']) == progress
    assert fixture.queue.jobs('alice', queued['job_id'])['progress'] == 1
    fixture.time += timedelta(minutes=5)
    assert fixture.queue.claim() is None
    fixture.time += timedelta(microseconds=1)
    new = fixture.queue.claim()
    assert new['attempts'] == 2 and new['claim_token'] != old['claim_token']
    assert json.loads(new['progress_json']) == progress
    with pytest.raises(UserGrantConflict, match='后台任务已取消或由其他进程接续'):
        fixture.queue.active(old, [])
    fixture.queue.finish(old, result={'stale': True})
    assert fixture.queue.jobs('alice', queued['job_id'])['state'] == 'running'
    fixture.queue.finish(new, result={'real': True})
    assert fixture.queue.jobs('alice', queued['job_id'])['result'] == {'real': True}


def test_third_expired_attempt_becomes_failed_without_a_fourth_claim(fixture):
    queued = fixture.queue.enqueue('alice', 'evaluation', {}, 'job')
    for attempt in (1, 2, 3):
        claimed = fixture.queue.claim()
        assert claimed['attempts'] == attempt
        fixture.time += timedelta(minutes=5, microseconds=1)
    assert fixture.queue.claim() is None
    raw = fixture.raw(queued['job_id'])
    assert raw['state'] == 'failed' and raw['claim_token'] is None
    assert raw['error'] == '工作进程中断，已达重试上限'
    assert raw['updated_at'] == claimed['updated_at']
    with pytest.raises(ValueError, match='仅可重试失败任务，最多尝试三次'):
        fixture.queue.retry('alice', queued['job_id'])


@pytest.mark.parametrize('claim_first', [False, True])
def test_cancel_is_idempotent_and_revokes_running_worker(fixture, claim_first):
    queued = fixture.queue.enqueue('alice', 'evaluation', {}, 'job')
    claimed = fixture.queue.claim() if claim_first else None
    assert fixture.queue.cancel('alice', queued['job_id'])['state'] == 'canceled'
    assert fixture.queue.cancel('alice', queued['job_id'])['state'] == 'canceled'
    assert [event[1] for event in fixture.events] == ['job.queued'] + (['job.claimed'] if claim_first else []) + ['job.canceled']
    assert len(fixture.wakes) == 1
    if claimed:
        with pytest.raises(UserGrantConflict):
            fixture.queue.active(claimed)
        fixture.queue.finish(claimed, result={'unexpected': True})
        assert fixture.queue.jobs('alice', queued['job_id'])['result'] is None


def test_cancel_event_failure_rolls_back_state_and_claim_token(fixture):
    queued = fixture.queue.enqueue('alice', 'evaluation', {}, 'job')
    claimed = fixture.queue.claim()
    fixture.fail_event = 'job.canceled'
    with pytest.raises(RuntimeError, match='event storage unavailable'):
        fixture.queue.cancel('alice', queued['job_id'])
    assert fixture.raw(queued['job_id'])['claim_token'] == claimed['claim_token']
    assert fixture.queue.jobs('alice', queued['job_id'])['state'] == 'running'
    with closing(fixture.connect()) as db:
        assert db.execute('SELECT count(*) FROM skill_authoring_events').fetchone()[0] == 2


def test_retry_preserves_attempts_progress_and_daily_identity(fixture):
    queued = fixture.queue.enqueue('alice', 'evaluation', {}, 'job')
    with pytest.raises(ValueError, match='仅可重试失败任务'):
        fixture.queue.retry('alice', queued['job_id'])
    for attempt in (1, 2, 3):
        job = fixture.queue.claim()
        assert job['attempts'] == attempt
        fixture.queue.active(job, [{'key': 'completed-case'}])
        fixture.queue.finish(job, error='可重试错误')
        if attempt < 3:
            retried = fixture.queue.retry('alice', queued['job_id'])
            assert retried['state'] == 'queued'
            assert retried['error'] is None and retried['progress'] == 1
            assert retried['attempts'] == attempt and retried['created_at'] == queued['created_at']
    with pytest.raises(ValueError, match='最多尝试三次'):
        fixture.queue.retry('alice', queued['job_id'])
    assert len(fixture.wakes) == 3
    assert [event[1] for event in fixture.events] == ['job.queued'] + ['job.claimed'] * 3


def test_scope_reduction_blocks_active_and_retry_without_discarding_progress(fixture):
    fixture.allow()
    queued = fixture.queue.enqueue('alice', 'generation', {'automatic': True, 'scope': 'chat'}, 'job')
    job = fixture.queue.claim()
    fixture.queue.active(job, [{'key': 'saved-progress'}])
    previous = fixture.raw(queued['job_id'])
    fixture.allow(enabled=False)
    fixture.time += timedelta(minutes=1)
    with pytest.raises(PermissionError, match='自动沉淀已关闭'):
        fixture.queue.active(job, [])
    assert fixture.raw(queued['job_id']) == previous
    fixture.queue.finish(job, error='自动沉淀已关闭')
    with pytest.raises(PermissionError, match='自动沉淀已关闭'):
        fixture.queue.retry('alice', queued['job_id'])
    assert len(fixture.wakes) == 1
    fixture.allow()
    assert fixture.queue.retry('alice', queued['job_id'])['progress'] == 1


def test_latest_report_filters_owner_draft_revision_kind_and_state_on_same_connection(fixture):
    old = fixture.complete('alice', 'old', payload={'draft_id': 'draft', 'revision': 1})
    fixture.complete('bob', 'foreign', payload={'draft_id': 'draft', 'revision': 1})
    fixture.complete('alice', 'revision', payload={'draft_id': 'draft', 'revision': 2})
    fixture.complete('alice', 'generation', kind='generation', payload={'draft_id': 'draft', 'revision': 1})
    fixture.complete('alice', 'failed', payload={'draft_id': 'draft', 'revision': 1}, error='failed')
    with closing(fixture.connect()) as db:
        assert SkillJobQueue.latest_report(db, 'alice', 'draft', 1) == {'key': 'old'}
        assert SkillJobQueue.latest_report(db, 'alice', 'other', 1) is None
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE skill_jobs SET result_json=? WHERE job_id=?',
                   ('{"uncommitted":true}', old['job_id']))
        assert fixture.queue.latest_report(db, 'alice', 'draft', 1) == {'uncommitted': True}
        db.rollback()
    fixture.complete('alice', 'newest', payload={'draft_id': 'draft', 'revision': 1})
    with closing(fixture.connect()) as db:
        assert fixture.queue.latest_report(db, 'alice', 'draft', 1) == {'key': 'newest'}


def test_slices_rotate_owners_then_jobs_even_with_identical_timestamps(fixture):
    alice_first = fixture.queue.enqueue('alice', 'evaluation', {}, 'alice-first')
    alice_second = fixture.queue.enqueue('alice', 'evaluation', {}, 'alice-second')
    bob = fixture.queue.enqueue('bob', 'evaluation', {}, 'bob')
    expected = [alice_first, bob, alice_second, bob, alice_first, bob]
    for index, queued in enumerate(expected):
        claimed = fixture.queue.claim()
        assert claimed['job_id'] == queued['job_id']
        assert claimed['attempts'] == 1
        fixture.queue.yield_job(claimed, [{'key': str(index), 'output': 'done'}])
        current = fixture.queue.jobs(claimed['owner_subject'], claimed['job_id'])
        assert current['state'] == 'queued' and current['attempts'] == 0
        assert current['progress_stats']['unrecorded_calls_possible'] is False
        with pytest.raises(UserGrantConflict):
            fixture.queue.active(claimed)
        with pytest.raises(UserGrantConflict):
            fixture.queue.yield_job(claimed, [])


def test_claim_event_failure_rolls_back_claim_and_service_order(fixture):
    queued = fixture.queue.enqueue('alice', 'evaluation', {}, 'job')
    fixture.fail_event = 'job.claimed'
    with pytest.raises(RuntimeError, match='event storage unavailable'):
        fixture.queue.claim()
    assert fixture.raw(queued['job_id'])['state'] == 'queued'
    assert fixture.raw(queued['job_id'])['attempts'] == 0
    with closing(fixture.connect()) as db:
        assert db.execute("SELECT count(*) FROM skill_authoring_events WHERE kind='job.claimed'").fetchone()[0] == 0


def test_yield_rejects_cancellation_or_revoked_automatic_scope(fixture):
    queued = fixture.queue.enqueue('alice', 'evaluation', {}, 'cancel')
    job = fixture.queue.claim()
    fixture.queue.cancel('alice', queued['job_id'])
    with pytest.raises(UserGrantConflict):
        fixture.queue.yield_job(job, [{'key': 'untrusted'}])
    assert fixture.queue.jobs('alice', queued['job_id'])['progress'] == 0
    assert fixture.queue.jobs('alice', queued['job_id'])['progress_stats']['unrecorded_calls_possible'] is True
    fixture.allow()
    automatic = fixture.queue.enqueue('alice', 'generation', {'automatic': True, 'scope': 'chat'}, 'auto')
    active = fixture.queue.claim()
    fixture.allow(enabled=False)
    with pytest.raises(PermissionError):
        fixture.queue.yield_job(active, [])
    assert fixture.raw(automatic['job_id'])['state'] == 'running'


def test_failure_retry_and_lease_recovery_preserve_progress_across_normal_yields(fixture):
    queued = fixture.queue.enqueue('alice', 'evaluation', {}, 'retry-slices')
    first = fixture.queue.claim()
    progress = [{'key': 'first', 'output': 'done', 'usage': {'input': 1, 'output': 2, 'totalTokens': 3, 'cost_estimate_usd': .1}}]
    fixture.queue.active(first, progress)
    fixture.queue.finish(first, error='failed after an unknown call')
    fixture.queue.retry('alice', queued['job_id'])
    resumed = fixture.queue.claim()
    assert resumed['attempts'] == 2 and json.loads(resumed['progress_json']) == progress
    fixture.queue.yield_job(resumed, progress)
    current = fixture.queue.jobs('alice', queued['job_id'])
    assert current['attempts'] == 1 and current['state'] == 'queued'
    assert current['progress_stats']['unrecorded_calls_possible'] is True
    assert current['progress_stats']['cost_complete'] is False
    active = fixture.queue.claim()
    assert active['attempts'] == 2
    fixture.time += timedelta(minutes=6)
    recovered = fixture.queue.claim()
    assert recovered['attempts'] == 3 and json.loads(recovered['progress_json']) == progress
    with pytest.raises(UserGrantConflict):
        fixture.queue.yield_job(active, [])
    fixture.queue.yield_job(recovered, progress)
    assert fixture.queue.claim()['attempts'] == 3


def test_cancel_serializes_its_observed_state_with_concurrent_completion(fixture):
    queued = fixture.queue.enqueue('alice', 'evaluation', {}, 'cancel-complete-race')
    job = fixture.queue.claim()
    observed, release = threading.Event(), threading.Event()

    class PausedRead:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            row = self.cursor.fetchone()
            self.cursor.close()
            observed.set()
            assert release.wait(5), 'cancellation barrier was not released'
            return row

    class PausedConnection:
        def __init__(self):
            self.db = fixture.connect()

        def __enter__(self):
            self.db.__enter__()
            return self

        def __exit__(self, *arguments):
            return self.db.__exit__(*arguments)

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, *arguments):
            cursor = self.db.execute(sql, *arguments)
            if sql.startswith('SELECT * FROM skill_jobs WHERE job_id=? AND owner_subject=?'):
                return PausedRead(cursor)
            return cursor

    cancel_queue = fixture.make_queue()
    cancel_queue._connect = PausedConnection
    completing_queue = fixture.make_queue()
    completing_queue._connect = lambda: sqlite3.connect(fixture.path, timeout=0)
    with ThreadPoolExecutor(max_workers=1) as executor:
        canceling = executor.submit(cancel_queue.cancel, 'alice', queued['job_id'])
        try:
            assert observed.wait(5)
            # A worker cannot complete between the cancellation read and its write.
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                completing_queue.finish(job, result={'saved': True})
        finally:
            release.set()
        assert canceling.result(timeout=5)['state'] == 'canceled'
    fixture.queue.finish(job, result={'saved': True})
    current = fixture.queue.jobs('alice', queued['job_id'])
    assert current['state'] == 'canceled' and current['result'] is None


def test_completion_before_cancel_preserves_the_success_receipt(fixture):
    queued = fixture.queue.enqueue('alice', 'generation', {}, 'completed-before-cancel')
    job = fixture.queue.claim()
    fixture.queue.finish(job, result={'saved': True})
    canceled = fixture.queue.cancel('alice', queued['job_id'])
    assert canceled['state'] == 'succeeded' and canceled['result'] == {'saved': True}
    assert not any(event[1] == 'job.canceled' for event in fixture.events)
