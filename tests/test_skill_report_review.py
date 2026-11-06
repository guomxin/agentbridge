"""Manual review is separate evidence; comparisons require aligned known models."""
from contextlib import closing
from copy import deepcopy
from tempfile import TemporaryDirectory
import json
import unittest

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.skill_quality import candidate_matrix_passed, compare_reports, report_score_counts
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

    def test_submission_freezes_only_the_seen_report_and_replays_the_original_receipt(self):
        values = dict(draft_id=self.draft['draft_id'], expected_revision=1, request_key='submit', reason='已核对')
        first = self.evaluate()
        second = self.evaluate('second')
        for stale in (first['job_id'], None, 'foreign-job'):
            with self.subTest(stale=stale), self.assertRaisesRegex(UserGrantConflict, '评测报告已更新'):
                self.authoring.submit('alice', **values, expected_report_job_id=stale)
        with self.assertRaises(KeyError):
            self.authoring.submit('bob', **values, expected_report_job_id=second['job_id'])
        self.assertEqual(self.authoring.reviews('alice')['items'], [])
        seen = {**values, 'expected_report_job_id':second['job_id']}
        self.review(second, status='acceptable')
        submitted = self.authoring.submit('alice', **seen)
        self.evaluate('third')
        self.assertEqual(self.authoring.submit('alice', **seen), submitted)
        snapshot = self.authoring.review_detail(submitted['request_id'])['quality']['evaluation']
        self.assertEqual(snapshot['job_id'], second['job_id'])
        self.assertEqual(snapshot['reviews']['0:0:candidate']['status'], 'acceptable')
        with self.assertRaisesRegex(UserGrantConflict, '请求标识'):
            self.authoring.submit('alice', **values, expected_report_job_id=first['job_id'])

    def test_explicit_no_report_allows_sample_submission_only_while_there_is_no_report(self):
        sample = self.authoring.test_start('alice', draft_id=self.draft['draft_id'], expected_revision=1,
            profile='use', prompt='明天补测试。', request_key='sample')
        self.authoring.test_result('alice', test_id=sample['test_id'], output='明天补测试。')
        values = dict(draft_id=self.draft['draft_id'], expected_revision=1, request_key='submit', reason='检查样例')
        submitted = self.authoring.submit('alice', **values, expected_report_job_id=None)
        self.assertIsNone(self.authoring.review_detail(submitted['request_id'])['quality']['evaluation'])
        # Missing and explicit null have distinct meanings, even on retries.
        with self.assertRaisesRegex(UserGrantConflict, '请求标识'):
            self.authoring.submit('alice', **values)
        self.authoring.withdraw('alice', submitted['request_id'])
        report = self.evaluate()
        with closing(self.workbench.store.connect()) as db, db:
            legacy = dict(report)
            legacy.pop('job_id')
            db.execute('UPDATE skill_jobs SET result_json=? WHERE job_id=?', (json.dumps(legacy), report['job_id']))
        self.assertEqual(self.workbench.inspect('alice', self.draft['draft_id'])['evaluation']['job_id'], report['job_id'])
        values['request_key'] = 'legacy-submit'
        with self.assertRaisesRegex(UserGrantConflict, '评测报告已更新'):
            self.authoring.submit('alice', **values, expected_report_job_id=None)
        # An older caller without this field still freezes the latest report.
        received = self.authoring.submit('alice', **values)
        self.assertEqual(self.authoring.review_detail(received['request_id'])['quality']['evaluation']['job_id'], report['job_id'])

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

    def test_changed_evaluation_protocol_suppresses_regression_even_with_identical_models_and_cases(self):
        first = self.evaluate()
        second = self.evaluate('second', text='已完成')
        self.assertEqual(compare_reports(first, second)['regressed_cases'], [0])
        first.pop('evaluation_protocol')  # Historical reports have no protocol field.
        comparison = compare_reports(first, second)
        self.assertTrue(comparison['inputs_equal'])
        self.assertEqual(comparison['models'], 'known_same')
        self.assertFalse(comparison['protocols_equal'])
        self.assertEqual(comparison['regressed_cases'], [])
        second['evaluation_protocol'] = 'legacy-text-v1'
        self.assertTrue(compare_reports(first, second)['protocols_equal'])
        self.assertEqual(compare_reports(first, second)['regressed_cases'], [0])

    def test_display_scores_reconstruct_only_complete_historical_variants(self):
        report = {'cases': [{}, {}], 'repeats': 1,
                  'scores': {'published': {'passed': 1, 'total': 2}, 'candidate': {'passed': 2, 'total': 2}},
                  'rows': [{'variant':'published', 'case':0, 'repeat':0, 'output':'合成输出', 'passed':True},
                           {'variant':'published', 'case':1, 'repeat':0, 'skipped':'线上版本无此模式', 'passed':False},
                           {'variant':'candidate', 'case':0, 'repeat':0, 'output':'合成输出', 'passed':True}]}
        original = deepcopy(report)
        counts = report_score_counts(report)
        self.assertEqual(counts['published'], {'passed': 1, 'total': 1, 'skipped': 1})
        self.assertEqual(counts['candidate'], report['scores']['candidate'])
        self.assertIsNot(counts['candidate'], report['scores']['candidate'])
        self.assertEqual(report, original)
        for bad in ({'case': 0}, {'case': True}, {'repeat': 1}, {'output': '矛盾的输出'}, {'skipped': True}):
            with self.subTest(bad=bad):
                conflicting = deepcopy(report)
                conflicting['rows'][1].update(bad)
                self.assertEqual(report_score_counts(conflicting)['published'], report['scores']['published'])

    def test_empty_or_incomplete_candidate_matrix_cannot_be_reported_as_passed(self):
        report = self.evaluate(repeats=2)
        self.assertTrue(candidate_matrix_passed(report))
        candidates = [row for row in report['rows'] if row['variant'] == 'candidate']
        for rows in ([], [candidates[0]] * 2, [candidates[0], {**candidates[1], 'skipped': '不适用'}]):
            with self.subTest(rows=rows):
                self.assertFalse(candidate_matrix_passed({**report, 'rows': rows}))
        self.assertFalse(candidate_matrix_passed({**report, 'cases': [], 'rows': []}))

    def test_reading_historical_job_and_frozen_review_projects_skips_without_rewriting_evidence(self):
        report = self.evaluate()
        for field in ('evaluation_protocol', 'display_scores', 'reviews'):
            report.pop(field, None)
        for score in report['scores'].values():
            score.pop('skipped')
        report['scores']['published'] = {'passed': 0, 'total': 1}
        report['rows'].append({'key':'0:0:published', 'case':0, 'repeat':0, 'variant':'published',
                               'passed':False, 'skipped':'线上版本无此模式'})
        raw = json.dumps(report, ensure_ascii=False)
        with closing(self.workbench.store.connect()) as db, db:
            db.execute('UPDATE skill_jobs SET result_json=? WHERE job_id=?', (raw, report['job_id']))
        request = self.authoring.submit('alice', draft_id=self.draft['draft_id'], expected_revision=1,
            expected_report_job_id=report['job_id'], request_key='freeze-old-report', reason='核对旧版报告')
        with closing(self.workbench.store.connect()) as db:
            frozen = db.execute('SELECT snapshot_json FROM skill_review_quality WHERE request_id=?', (request['request_id'],)).fetchone()[0]
        self.assertNotIn('display_scores', json.loads(frozen)['evaluation'])
        displays = (self.workbench.jobs('alice', report['job_id'])['result'],
                    self.workbench.inspect('alice', self.draft['draft_id'])['evaluation'],
                    self.authoring.review_detail(request['request_id'])['quality']['evaluation'])
        for display in displays:
            self.assertEqual(display['scores']['published'], {'passed': 0, 'total': 1})
            self.assertEqual(display['display_scores']['published'], {'passed': 0, 'total': 0, 'skipped': 1})
            self.assertNotIn('evaluation_protocol', display)
        with closing(self.workbench.store.connect()) as db:
            self.assertEqual(db.execute('SELECT result_json FROM skill_jobs WHERE job_id=?', (report['job_id'],)).fetchone()[0], raw)
            self.assertEqual(db.execute('SELECT snapshot_json FROM skill_review_quality WHERE request_id=?', (request['request_id'],)).fetchone()[0], frozen)

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
