"""Check the real worker loop with deterministic, tool-free completion callbacks."""
from contextlib import closing, contextmanager
import json
import threading
from unittest.mock import patch

import pytest

from agentbridge.core.central_service import CentralCapabilityService


@pytest.fixture
def workbench(tmp_path):
    service = CentralCapabilityService(home=tmp_path, base_url='http://oa.test')
    worker = service.skill_authoring.workbench
    try:
        yield worker
    finally:
        worker.close()
        assert worker._thread is None or not worker._thread.is_alive()


def _completion(label, kind='preference'):
    return {'text': json.dumps({'kind': kind, 'summary': label, 'proposal': None}), 'model': label}


@contextmanager
def _finish_signal(workbench, count=1):
    done = threading.Event()
    completed = []
    original_finish = workbench._finish

    def finish(job, result=None, error=None):
        value = original_finish(job, result, error)
        completed.append(job['job_id'])
        if len(completed) >= count:
            done.set()
        return value

    with patch.object(workbench, '_finish', side_effect=finish):
        yield done, completed


def _insert_capture(workbench, dispatch_id):
    with closing(workbench.store.connect()) as db, db:
        db.execute('INSERT INTO skill_auto_capture VALUES (?,?,?,?,?)',
                   (dispatch_id, 'alice', 'synthetic completed material', 'queued',
                    '2026-10-04T00:00:00+00:00'))


def _assert_capture_processed(workbench, dispatch_id):
    with closing(workbench.store.connect()) as db:
        assert db.execute('SELECT state FROM skill_auto_capture WHERE dispatch_id=?',
                          (dispatch_id,)).fetchone()[0] == 'processed'


def test_enqueue_commits_job_and_event_before_waking_the_idle_real_worker(workbench):
    idle = threading.Event()
    committed_at_wake = []
    completion_calls = []
    original_wait = workbench._wake.wait
    original_set = workbench._wake.set

    def wait(timeout=None):
        idle.set()
        return original_wait(timeout)

    def wake():
        if not workbench._stop.is_set():
            with closing(workbench.store.connect()) as db:
                rows = db.execute(
                    "SELECT j.job_id,e.object_id FROM skill_jobs j JOIN skill_authoring_events e "
                    "ON e.object_id=j.job_id WHERE e.kind='job.queued' AND j.owner_subject='alice'",
                ).fetchall()
                committed_at_wake.append([tuple(row) for row in rows])
        original_set()

    def complete(_system, prompt):
        completion_calls.append(json.loads(prompt)['material'])
        return _completion('idle-worker')

    with _finish_signal(workbench) as (done, completed), \
            patch.object(workbench._wake, 'wait', side_effect=wait), \
            patch.object(workbench._wake, 'set', side_effect=wake):
        try:
            workbench.start(complete)
            assert idle.wait(5), 'worker did not reach its idle wait'
            job = workbench.generate('alice', material='synthetic preference', request_key='wake')
            assert done.wait(5), 'committed enqueue did not complete through the worker loop'
            assert completed == [job['job_id']]
            assert committed_at_wake == [[(job['job_id'], job['job_id'])]]
            assert completion_calls == ['synthetic preference']
            result = workbench.jobs('alice', job['job_id'])
            assert result['state'] == 'succeeded'
            assert result['result']['model'] == 'idle-worker'
            assert result['result']['saved'] is False
            assert workbench.authoring.list('alice')['items'] == []
        finally:
            workbench.close()


def test_start_replaces_live_callbacks_without_starting_another_thread(workbench):
    entered_old = threading.Event()
    release_old = threading.Event()
    calls = []
    old_capture_calls = []

    def old_complete(_system, _prompt):
        calls.append('old-complete')
        entered_old.set()
        assert release_old.wait(5), 'test did not release the original completion callback'
        return _completion('old')

    def new_capture(row):
        calls.append('new-capture')
        assert row['dispatch_id'] == 'live-capture'
        assert row['owner_subject'] == 'alice'

    def new_complete(_system, _prompt):
        calls.append('new-complete')
        _assert_capture_processed(workbench, 'live-capture')
        return _completion('replacement', kind='none')

    with _finish_signal(workbench, count=2) as (done, completed):
        original_thread = None
        try:
            first = workbench.generate('alice', material='first', request_key='first')
            workbench.start(old_complete, capture=lambda row: old_capture_calls.append(row))
            assert entered_old.wait(5), 'first completion callback did not start'
            original_thread = workbench._thread
            _insert_capture(workbench, 'live-capture')
            workbench.start(new_complete, capture=new_capture)
            assert workbench._thread is original_thread
            assert original_thread.is_alive()
            second = workbench.generate('alice', material='second', request_key='second')
            release_old.set()
            assert done.wait(5), 'replacement callback did not complete the next job'
            assert completed == [first['job_id'], second['job_id']]
            assert calls == ['old-complete', 'new-capture', 'new-complete']
            assert old_capture_calls == []
            assert workbench.jobs('alice', first['job_id'])['result']['model'] == 'old'
            second_result = workbench.jobs('alice', second['job_id'])
            assert second_result['state'] == 'succeeded'
            assert second_result['result']['model'] == 'replacement'
            assert workbench.authoring.list('alice')['items'] == []
        finally:
            release_old.set()
            workbench.close()
            if original_thread is not None:
                original_thread.join(timeout=5)
                assert not original_thread.is_alive()


def test_close_stops_worker_and_restart_processes_capture_before_new_generation(workbench):
    calls = []

    def old_complete(_system, _prompt):
        calls.append('old-complete')
        return _completion('before-close')

    def new_capture(row):
        calls.append('restart-capture')
        assert row['dispatch_id'] == 'restart-capture'

    def new_complete(_system, _prompt):
        calls.append('restart-complete')
        _assert_capture_processed(workbench, 'restart-capture')
        return _completion('after-restart', kind='none')

    with _finish_signal(workbench) as (done, completed):
        first = workbench.generate('alice', material='before close', request_key='before-close')
        workbench.start(old_complete)
        assert done.wait(5), 'initial worker did not finish'
        previous_thread = workbench._thread
        workbench.close()
        assert workbench._stop.is_set()
        assert not previous_thread.is_alive()
        assert workbench.jobs('alice', first['job_id'])['state'] == 'succeeded'
        done.clear()
        _insert_capture(workbench, 'restart-capture')
        second = workbench.generate('alice', material='after close', request_key='after-close')
        assert workbench.jobs('alice', second['job_id'])['state'] == 'queued'
        try:
            workbench.start(new_complete, capture=new_capture)
            assert workbench._thread is not previous_thread
            assert done.wait(5), 'restarted worker did not finish'
            assert completed == [first['job_id'], second['job_id']]
            assert calls == ['old-complete', 'restart-capture', 'restart-complete']
            result = workbench.jobs('alice', second['job_id'])
            assert result['state'] == 'succeeded'
            assert result['result']['model'] == 'after-restart'
            assert result['result']['saved'] is False
            assert workbench.authoring.list('alice')['items'] == []
        finally:
            workbench.close()
        assert not workbench._thread.is_alive()
