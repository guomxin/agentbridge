import json
from contextlib import closing
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.skill_job_executor import SkillJobExecutor
from agentbridge.core.user_grants import UserGrantConflict


class SkillJobExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = self.restart()
        self.authoring = self.service.skill_authoring
        self.workbench = self.authoring.workbench
        self.proposal = {
            'name': '周报方法', 'description': '根据提供的文字区分计划与完成',
            'selection': {
                'use_when': '整理周报', 'not_for': '查询或提交业务',
                'output': '完成事项与后续计划',
            },
            'instructions': '核对输入依据，区分计划和完成，不推断业务成功。',
        }
        self.draft = self.authoring.save('alice', proposal=self.proposal, request_key='draft')

    def restart(self):
        return CentralCapabilityService(home=self.tmp.name, base_url='http://oa.test')

    def evaluation(self, *, repeats=1):
        return self.workbench.evaluate(
            'alice', draft_id=self.draft['draft_id'], expected_revision=1,
            cases=[{'prompt': '计划整理周报', 'contains': ['计划']}],
            request_key='evaluation', repeats=repeats,
        )

    def generation(self):
        return self.workbench.generate('alice', material='做个周报助手', request_key='generation')

    def method_response(self):
        return {'text': json.dumps({'kind': 'method', 'proposal': self.proposal}), 'model': 'fixture'}

    def test_interrupted_evaluation_resumes_persisted_progress_after_restart(self):
        job = self.evaluation(repeats=2)
        initial_calls = []

        class WorkerInterrupted(BaseException):
            pass

        def interrupted_completion(system, prompt):
            initial_calls.append((system, prompt))
            if len(initial_calls) == 2:
                raise WorkerInterrupted()
            return {'text': '计划：此前已记录的评测输出', 'model': 'before-restart'}

        with self.assertRaises(WorkerInterrupted):
            self.workbench.run_once(interrupted_completion)
        before = self.workbench.jobs('alice', job['job_id'])
        self.assertEqual((before['state'], before['progress']), ('running', 1))
        with closing(self.workbench.store.connect()) as db, db:
            db.execute("UPDATE skill_jobs SET lease_until='2000-01-01' WHERE job_id=?", (job['job_id'],))

        restarted = self.restart().skill_authoring.workbench
        resumed_calls = []

        def completion(system, prompt):
            resumed_calls.append((system, prompt))
            return {'text': '计划：恢复后的评测输出', 'model': 'after-restart'}

        self.assertTrue(restarted.run_once(completion))
        after = restarted.jobs('alice', job['job_id'])
        self.assertEqual((after['state'], after['attempts'], after['progress']), ('succeeded', 2, 4))
        self.assertEqual(len(resumed_calls), 3)
        rows = after['result']['rows']
        self.assertEqual([row['key'] for row in rows], [
            '0:0:candidate', '0:0:without_skill', '1:0:candidate', '1:0:without_skill',
        ])
        self.assertEqual(rows[0]['model'], 'before-restart')
        self.assertEqual(after['result']['scores']['candidate'], {'passed': 2, 'total': 2})
        self.assertFalse(restarted.run_once(lambda *_: self.fail('completed job must not run again')))

    def test_reclaimed_generation_cannot_save_or_complete_with_old_token(self):
        job = self.generation()
        reclaimed = []

        def completion(*_):
            with closing(self.workbench.store.connect()) as db, db:
                db.execute("UPDATE skill_jobs SET lease_until='2000-01-01' WHERE job_id=?", (job['job_id'],))
            reclaimed.append(self.restart().skill_authoring.workbench.claim())
            return self.method_response()

        with patch.object(self.authoring, 'save', wraps=self.authoring.save) as save:
            self.workbench.run_once(completion)
        save.assert_not_called()
        current = self.workbench.jobs('alice', job['job_id'])
        self.assertEqual((current['state'], current['attempts']), ('running', 2))
        self.assertIsNone(current['result'])
        self.assertEqual(len(self.authoring.list('alice')['items']), 1)
        self.workbench._active(reclaimed[0])

    def test_final_job_update_failure_rolls_back_draft_receipt_and_event(self):
        job = self.generation()
        with closing(self.workbench.store.connect()) as db, db:
            db.execute('''CREATE TRIGGER reject_skill_job_success
                BEFORE UPDATE OF state ON skill_jobs WHEN NEW.state='succeeded'
                BEGIN SELECT RAISE(ABORT, 'synthetic completion failure'); END''')
        self.workbench.run_once(lambda *_: self.method_response())
        failed = self.workbench.jobs('alice', job['job_id'])
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['error'], '模型服务暂不可用，任务可重试')
        self.assertIsNone(failed['result'])
        with closing(self.workbench.store.connect()) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM skill_drafts').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM skill_draft_revisions').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM skill_authoring_events WHERE kind='draft.saved'").fetchone()[0], 1)
            self.assertIsNone(db.execute('SELECT 1 FROM skill_authoring_commands WHERE request_key=?', ('job:' + job['job_id'],)).fetchone())
            db.execute('DROP TRIGGER reject_skill_job_success')

        self.workbench.retry('alice', job['job_id'])
        self.workbench.run_once(lambda *_: self.method_response())
        succeeded = self.workbench.jobs('alice', job['job_id'])
        self.assertEqual((succeeded['state'], succeeded['attempts']), ('succeeded', 2))
        self.assertTrue(succeeded['result']['saved'])
        self.assertEqual(len(self.authoring.list('alice')['items']), 2)
        self.assertEqual(self.authoring.reviews('alice')['items'], [])

    def test_model_callback_replacement_of_active_guard_is_observed(self):
        job = self.generation()

        def rejected(*_args, **_kwargs):
            raise UserGrantConflict('替换后的作业检查拒绝保存')

        def completion(*_):
            self.workbench._active = rejected
            return self.method_response()

        self.workbench.run_once(completion)
        failed = self.workbench.jobs('alice', job['job_id'])
        self.assertEqual(failed['error'], '替换后的作业检查拒绝保存')
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(len(self.authoring.list('alice')['items']), 1)

    def test_model_callback_replacement_of_draft_save_is_observed(self):
        job = self.generation()
        saved = []
        original = self.authoring.save

        def replacement(owner, **arguments):
            saved.append((owner, arguments['_job']['job_id']))
            return original(owner, **arguments)

        def completion(*_):
            self.authoring.save = replacement
            return self.method_response()

        self.workbench.run_once(completion)
        self.assertEqual(saved, [('alice', job['job_id'])])
        self.assertTrue(self.workbench.jobs('alice', job['job_id'])['result']['saved'])

    def test_legacy_non_generation_kind_retains_evaluation_fallback(self):
        job = self.evaluation()
        with closing(self.workbench.store.connect()) as db, db:
            db.execute("UPDATE skill_jobs SET kind='legacy-evaluation' WHERE job_id=?", (job['job_id'],))
        calls = []
        self.workbench.run_once(lambda *_: calls.append(1) or {'text': '计划整理'})
        result = self.workbench.jobs('alice', job['job_id'])
        self.assertEqual(result['kind'], 'legacy-evaluation')
        self.assertEqual((result['state'], len(calls)), ('succeeded', 2))
        self.assertTrue(result['result']['passed'])


class SkillCompletionContractTests(unittest.TestCase):
    def test_invalid_completions_and_text_length_boundary(self):
        for response in (None, '', [], {}, {'text': None}, {'text': 1}, {'text': '  '}):
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, '模型返回空结果或无效响应'):
                SkillJobExecutor.call(lambda *_: response, 'system', 'prompt')
        response = {'text': 'x' * 48000, 'model': 'fixture', 'usage': {'output': 1}}
        self.assertIs(SkillJobExecutor.call(lambda *_: response, 'system', 'prompt'), response)
        with self.assertRaisesRegex(ValueError, '模型结果过长'):
            SkillJobExecutor.call(lambda *_: {'text': 'x' * 48001}, 'system', 'prompt')


if __name__ == '__main__':
    unittest.main()
