"""Manual review is separate evidence; comparisons require aligned known models."""
from contextlib import closing
from copy import deepcopy
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.skill_quality import compare_reports
from agentbridge.core.user_grants import UserGrantConflict


class SkillReportReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.service = CentralCapabilityService(home=self.temp.name, base_url='http://oa.test')
        self.authoring = self.service.skill_authoring
        self.workbench = self.authoring.workbench
        self.proposal = {'name':'周报整理', 'description':'只整理给定文字',
            'selection':{'use_when':'整理周报','not_for':'查询业务','output':'完成和计划'},
            'instructions':'输入用户原文，步骤：区分完成和计划，缺失时追问，核对原文。'}
        self.draft = self.authoring.save('alice', proposal=self.proposal, request_key='draft')

    def run_jobs(self, complete):
        # These assertions concern completed evidence; slice scheduling has its own tests.
        for _ in range(40):
            if not self.workbench.run_once(complete):
                return
        self.fail('synthetic jobs did not complete within their bounded evaluation budget')

    def evaluate(self, key='first', *, revision=1, text='计划：修复', model='test/model', repeats=1):
        job = self.workbench.evaluate('alice', draft_id=self.draft['draft_id'], expected_revision=revision,
            cases=[{'prompt':'明天修复','contains':['计划']}], repeats=repeats, request_key=key)
        self.run_jobs(lambda *_: {'text':text,'model':model})
        return self.workbench.jobs('alice', job['job_id'])['result']

    def review(self, report, **changes):
        values = {'job_id':report['job_id'],'row_key':'0:0:candidate',
                  'status':'problem','note':'补写了原文没有的效果。','expected_version':0}
        return self.workbench.review_report('alice', **{**values,**changes})

    def test_review_is_scoped_persistent_and_never_changes_report_or_scores(self):
        report = self.evaluate()
        with closing(self.workbench.store.connect()) as db:
            frozen = db.execute('SELECT result_json FROM skill_jobs WHERE job_id=?',(report['job_id'],)).fetchone()[0]
        saved = self.review(report)
        self.assertEqual(saved['version'],1)
        self.assertEqual(self.review(report),saved)  # Safe retry after losing response.
        loaded = self.workbench.inspect('alice',self.draft['draft_id'])['evaluation']
        self.assertEqual(loaded['reviews']['0:0:candidate'],saved)
        self.assertTrue(loaded['passed'])
        self.assertEqual(loaded['scores'],report['scores'])
        self.assertEqual(self.workbench.jobs('alice')['items'][0]['result']['reviews'],loaded['reviews'])
        with closing(self.workbench.store.connect()) as db:
            self.assertEqual(db.execute('SELECT result_json FROM skill_jobs WHERE job_id=?',(report['job_id'],)).fetchone()[0],frozen)
        with self.assertRaises(KeyError):
            self.workbench.review_report('bob',job_id=report['job_id'],row_key='0:0:candidate',status='acceptable',note='',expected_version=0)
        next_report = self.evaluate('second')
        self.assertEqual(next_report['reviews'],{})
        self.assertEqual(self.workbench.jobs('alice',report['job_id'])['result']['reviews']['0:0:candidate'],saved)

    def test_stale_review_cannot_overwrite_and_rows_must_exist(self):
        report = self.evaluate()
        self.review(report)
        with self.assertRaises(UserGrantConflict): self.review(report,status='acceptable',note='')
        result = self.review(report,status='acceptable',note='已核对',expected_version=1)
        self.assertEqual(result['version'],2)
        cleared = self.review(report,status='unreviewed',note='',expected_version=2)
        self.assertEqual(cleared['version'],3)
        for changes in ({'expected_version':True},{'expected_version':-1},{'status':[]},
                        {'status':'passed'},{'note':'x'*1001},{'row_key':'not-a-row'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.review(report,**changes)
        queued = self.workbench.evaluate('alice',draft_id=self.draft['draft_id'],expected_revision=1,
            cases=[{'prompt':'明天修复','contains':['计划']}],request_key='queued')
        with self.assertRaises(KeyError): self.review({'job_id':queued['job_id']})

    def test_submitted_review_snapshot_does_not_change_retroactively(self):
        report = self.evaluate()
        self.review(report,status='acceptable',note='已核对')
        request = self.authoring.submit('alice',draft_id=self.draft['draft_id'],expected_revision=1,
            request_key='submit',reason='个人使用')
        before = self.authoring.review_detail(request['request_id'])['quality']
        self.assertEqual(before['evaluation']['reviews']['0:0:candidate']['status'],'acceptable')
        self.review(report,expected_version=1)
        self.assertEqual(self.authoring.review_detail(request['request_id'])['quality'],before)

    def test_fixed_report_comparison_marks_only_new_candidate_regressions(self):
        first = self.evaluate(repeats=2)
        self.workbench.set_regression('alice',draft_id=self.draft['draft_id'],expected_revision=1,job_id=first['job_id'])
        same = self.workbench.inspect('alice',self.draft['draft_id'])['comparison']
        self.assertTrue(same['same_report']); self.assertEqual(same['regressed_cases'],[])
        second = self.evaluate('second',text='已完成',repeats=2)
        comparison = self.workbench.inspect('alice',self.draft['draft_id'])['comparison']
        self.assertEqual(comparison['source_job_id'],first['job_id'])
        self.assertTrue(comparison['inputs_equal']); self.assertEqual(comparison['models'],'known_same')
        self.assertEqual(comparison['regressed_cases'],[0])
        self.assertEqual(compare_reports(second,first)['regressed_cases'],[])
        changed = self.evaluate('changed',text='已完成',model='other/model',repeats=2)
        self.assertEqual(compare_reports(first,changed)['models'],'changed')
        self.assertEqual(compare_reports(first,changed)['regressed_cases'],[])

    def test_unknown_incomplete_models_and_changed_inputs_are_not_comparable(self):
        first = self.evaluate(repeats=2)
        second = self.evaluate('second',text='已完成',repeats=2)
        for variant in ('missing','blank','skipped','incomplete'):
            candidate = deepcopy(second)
            if variant=='missing': candidate['rows'][0].pop('model')
            elif variant=='blank': candidate['rows'][0]['model']=' '
            elif variant=='skipped': candidate['rows'][0]['skipped']='未完成'
            else: candidate['rows'].pop(0)
            result = compare_reports(first,candidate)
            self.assertTrue(result['inputs_equal'])
            self.assertEqual(result['models'],'unknown'); self.assertEqual(result['regressed_cases'],[])
        candidate = deepcopy(second); candidate['cases'][0]['contains']=['明天']
        self.assertFalse(compare_reports(first,candidate)['inputs_equal'])
        self.assertIsNone(compare_reports(first,None))
        self.assertFalse(compare_reports({},second))

    def test_model_identity_is_compared_by_case_and_repeat_not_as_set(self):
        first = self.evaluate(repeats=2)
        second = self.evaluate('second',text='已完成',repeats=2)
        for report, order in ((first,['model-a','model-b']),(second,['model-b','model-a'])):
            for row in report['rows']:
                if row['variant']=='candidate': row['model']=order[row['repeat']]
        comparison = compare_reports(first,second)
        self.assertEqual(comparison['models'],'changed')
        self.assertEqual(comparison['regressed_cases'],[])

    def test_candidate_problems_need_exact_admin_reasons_and_keep_frozen_reviews(self):
        report = self.evaluate()
        self.review(report)
        request = self.authoring.submit('alice', draft_id=self.draft['draft_id'], expected_revision=1,
                                        request_key='problem-submit', reason='个人使用')
        detail = self.authoring.review_detail(request['request_id'])
        frozen = deepcopy(detail['quality']['evaluation'])
        self.assertEqual([p['row_key'] for p in detail['quality']['candidate_problems']], ['0:0:candidate'])
        self.review(report, status='acceptable', note='稍后改成可接受', expected_version=1)
        args = {'actor': {'username': 'admin', 'role': 'admin'}, 'request_id': request['request_id'],
                'decision': 'approve', 'reason': '已复核', 'reviewed_tests': True}
        for reasons in (None, {}, {'0:0:candidate': ' '}, {'0:0:candidate': 'x' * 1001},
                        {'0:0:candidate': '接受', '0:0:without_skill': '接受'}, {'0:0:candidate': True}):
            with self.subTest(reasons=reasons), self.assertRaises(ValueError):
                self.authoring.decide(**args, problem_resolutions=reasons)
        self.assertEqual(self.authoring.review_detail(request['request_id'])['state'], 'submitted')
        with self.assertRaises(PermissionError):
            self.authoring.decide(**{**args, 'actor': {'role': 'viewer'}}, problem_resolutions={'0:0:candidate': '接受'})
        reasons = {'0:0:candidate': '仅保留为合成示例，实际使用必须以原文核对。'}
        def fail(*_): raise RuntimeError('audit unavailable')
        with self.assertRaises(RuntimeError):
            self.authoring.decide(**args, problem_resolutions=reasons, audit_callback=fail)
        self.assertNotIn('problem_resolutions', self.authoring.review_detail(request['request_id'])['quality'])
        self.authoring.decide(**args, problem_resolutions=reasons)
        quality = self.authoring.review_detail(request['request_id'])['quality']
        self.assertEqual(quality['problem_resolutions'], reasons)
        self.assertEqual(quality['evaluation'], frozen)
        self.assertEqual(quality['candidate_problems'][0]['review_version'], 1)
        self.assertTrue(quality['evaluation']['passed'])

    def test_only_selected_candidate_output_and_shared_trigger_need_resolution(self):
        proposal = {**self.proposal, 'profiles': {'preview': {}, 'fill': {}}}
        self.authoring.save('alice', draft_id=self.draft['draft_id'], expected_revision=1,
                            proposal=proposal, request_key='two-modes')
        job = self.workbench.evaluate('alice', draft_id=self.draft['draft_id'], expected_revision=2,
            cases=[{'prompt': '预览计划', 'profile': 'preview', 'contains': ['计划']},
                   {'prompt': '填写计划', 'profile': 'fill', 'contains': ['计划']},
                   {'prompt': '整理周报', 'profile': 'fill', 'kind': 'trigger', 'expected_trigger': True}], request_key='modes')
        self.run_jobs(lambda system, _: {'text': 'true' if 'true 或 false' in system else '计划：修复', 'model': 'synthetic'})
        for row_key in ('0:0:candidate', '0:1:candidate', '0:2:candidate', '0:0:without_skill'):
            self.workbench.review_report('alice', job_id=job['job_id'], row_key=row_key,
                status='problem', note='需要解释', expected_version=0)
        request = self.authoring.submit('alice', draft_id=self.draft['draft_id'], expected_revision=2,
            request_key='selected', reason='仅预览', profiles=['preview'])
        quality = self.authoring.review_detail(request['request_id'])['quality']
        self.assertEqual([p['row_key'] for p in quality['candidate_problems']], ['0:0:candidate', '0:2:candidate'])
        result = self.authoring.decide(actor={'username': 'admin', 'role': 'admin'}, request_id=request['request_id'],
            decision='approve', reason='逐项核对', reviewed_tests=True,
            problem_resolutions={'0:0:candidate': '该合成输入的差异可接受', '0:2:candidate': '触发范围已逐句核对'})
        self.assertEqual(result['state'], 'published')

    def test_problem_can_be_returned_without_acceptance_or_retested(self):
        report = self.evaluate()
        self.review(report)
        request = self.authoring.submit('alice', draft_id=self.draft['draft_id'], expected_revision=1,
            request_key='return', reason='个人使用')
        self.authoring.decide(actor={'username': 'admin', 'role': 'admin'}, request_id=request['request_id'],
            decision='changes_requested', reason='请修正问题')
        self.evaluate('new-report')
        request = self.authoring.submit('alice', draft_id=self.draft['draft_id'], expected_revision=1,
            request_key='retest', reason='已重新评测')
        self.assertEqual(self.authoring.review_detail(request['request_id'])['quality']['candidate_problems'], [])
        result = self.authoring.decide(actor={'username': 'admin', 'role': 'admin'}, request_id=request['request_id'],
            decision='approve', reason='新结果已核对', reviewed_tests=True)
        self.assertEqual(result['state'], 'published')
