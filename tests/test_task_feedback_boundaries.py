"""Atomic task attribution and idempotent feedback boundaries."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.skill_workbench import TaskFeedbackError
from agentbridge.workspace.application import WorkspaceApplication
from tests.support.task_feedback_acceptance import TaskFeedbackAcceptanceFixture, OWNER


class TaskFeedbackBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = TaskFeedbackAcceptanceFixture(Path(self.temp.name))
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        self.workbench = self.service.skill_authoring.workbench
        self.case = next(case for case in self.fixture.meta['feedback_cases'] if case['state'] == 'succeeded')
        self.body = {'rating': 'useful', 'comment': '已核对结果边界。', 'requestKey': 'task-feedback-boundary-01'}

    def record(self, case=None, body=None):
        return WorkspaceApplication(service=self.service).task_feedback({'user_subject': OWNER},
            (case or self.case)['task_id'], body or self.body)

    def rows(self, sql, args=()):
        with sqlite3.connect(self.service.db_path) as db:
            return db.execute(sql, args).fetchall()

    def test_concurrent_different_keys_same_payload_make_one_immutable_receipt(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda key: self.record(body={**self.body, 'requestKey': key}),
                ['task-feedback-parallel-one', 'task-feedback-parallel-two']))
        self.assertEqual(values[0], values[1])
        self.assertEqual(self.rows('SELECT count(*) FROM skill_feedback'), [(1,)])
        source = json.loads(self.rows('SELECT source_json FROM skill_feedback_context')[0][0])
        self.assertEqual(source['task_id'], self.case['task_id'])
        self.assertEqual(source['binding_id'], self.case['binding_id'])
        self.assertEqual(source['version'], '1.0.0')

    def test_changed_payload_conflict_returns_original_receipt(self):
        original = self.record()
        for key in (self.body['requestKey'], 'another-feedback-request-key'):
            with self.subTest(key=key), self.assertRaises(TaskFeedbackError) as caught:
                self.record(body={**self.body, 'rating': 'incorrect', 'requestKey': key})
            self.assertEqual(caught.exception.code, 'TASK_FEEDBACK_CONFLICT')
            self.assertEqual(caught.exception.feedback, original['feedback'])
        self.assertEqual(self.rows('SELECT count(*) FROM skill_feedback'), [(1,)])

    def test_same_key_in_another_task_keeps_distinct_original_attribution(self):
        other = next(case for case in self.fixture.meta['feedback_cases'] if case['state'] == 'failed')
        first, second = self.record(), self.record(other)
        self.assertNotEqual(first['feedback']['receipt']['feedback_id'], second['feedback']['receipt']['feedback_id'])
        sources = [json.loads(row[0]) for row in self.rows('SELECT source_json FROM skill_feedback_context')]
        self.assertEqual({row['task_id'] for row in sources}, {self.case['task_id'], other['task_id']})

    def test_context_write_failure_rolls_back_feedback_and_retry_is_safe(self):
        with sqlite3.connect(self.service.db_path) as db:
            db.execute("CREATE TRIGGER fail_feedback_context BEFORE INSERT ON skill_feedback_context BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.record()
        self.assertEqual(self.rows('SELECT count(*) FROM skill_feedback'), [(0,)])
        with sqlite3.connect(self.service.db_path) as db:
            db.execute('DROP TRIGGER fail_feedback_context')
        result = self.record()
        self.assertEqual(self.workbench.task_feedback(OWNER, self.case['task_id']), result['feedback'])

    def test_client_cannot_choose_skill_or_reference_non_task_feedback(self):
        for extra in ({'skill_id': self.case['skill_id']}, {'binding_id': self.case['binding_id']}, {'version': '1.0.1'}, {'test_id': 'test'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.record(body={**self.body, **extra})
        with self.assertRaises(ValueError):
            self.workbench.feedback(OWNER, skill_id=self.case['skill_id'], profile='use', task_id=self.case['task_id'],
                rating='useful', request_key=self.body['requestKey'])
        self.assertEqual(self.rows('SELECT count(*) FROM skill_feedback'), [(0,)])

    def test_input_validation_is_bounded_and_does_not_write(self):
        for changes in ({'rating': []}, {'rating': 'success'}, {'comment': 'x' * 1001}, {'comment': None},
                        {'requestKey': ''}, {'requestKey': '含空格的 无效请求标识'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.record(body={**self.body, **changes})
        self.assertEqual(self.rows('SELECT count(*) FROM skill_feedback'), [(0,)])

    def test_feedback_of_unknown_task_never_changes_execution_state_or_success_evidence(self):
        case = next(case for case in self.fixture.meta['feedback_cases'] if case['state'] == 'outcome_unknown')
        before = self.service.tasks.get_task(case['task_id'], user_subject=OWNER)
        result = self.record(case)
        self.assertEqual(result['feedback']['receipt']['status'], 'recorded')
        self.assertEqual(self.service.tasks.get_task(case['task_id'], user_subject=OWNER), before)
        self.assertEqual(self.workbench.metrics(OWNER)['verification'], 'user_feedback_not_independent_success_rate')
        self.assertEqual(self.fixture.oa.writes, [])
        self.assertEqual(self.fixture.transport.write_calls, 0)
