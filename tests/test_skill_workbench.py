import json
from contextlib import closing
from copy import deepcopy
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.skill_quality import (EVALUATION_SCHEMA, grade_output, import_standard,
    publication_evidence, validate_cases)
from agentbridge.core.user_grants import UserGrantConflict


class SkillWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s = CentralCapabilityService(home=self.tmp.name, base_url='http://oa.test')
        self.a = self.s.skill_authoring; self.w = self.a.workbench
        self.p = {'name': '周报方法', 'description': '根据给定文字整理周报',
                  'selection': {'use_when': '整理周报', 'not_for': '查询和提交业务', 'output': '完成与计划'},
                  'instructions': '输入用户文字。步骤：区分计划和完成，核对依据。材料不足时追问。'}
        self.d = self.a.save('alice', proposal=self.p, request_key='draft')

    def run_jobs(self, complete):
        # These assertions concern completed evidence; slice scheduling has its own tests.
        for _ in range(40):
            if not self.w.run_once(complete):
                return
        self.fail('synthetic jobs did not complete within their bounded evaluation budget')

    def evaluate(self, key='eval', **kwargs):
        return self.w.evaluate('alice', draft_id=self.d['draft_id'], expected_revision=1,
            cases=[{'prompt': '计划修复登录问题', 'contains': ['计划'], 'excludes': ['已修复']}], request_key=key, **kwargs)

    def test_evaluation_is_independent_and_expectations_are_not_leaked(self):
        j = self.evaluate(repeats=2); calls = []
        def complete(system, prompt):
            calls.append((system, prompt))
            self.assertNotIn('已修复', system + prompt)
            self.assertNotIn('contains', system + prompt)
            return {'text': '计划：修复登录问题', 'model': 'test/model'}
        self.run_jobs(complete)
        report = self.w.jobs('alice', j['job_id'])['result']
        self.assertEqual(len(calls), 4)
        self.assertEqual(report['scores']['candidate'], {'passed': 2, 'total': 2})
        self.assertTrue(report['passed']); self.assertTrue(report['semantic_review_required'])
        self.assertEqual(report['content_hash'], self.a.get('alice', self.d['draft_id'])['bundle']['content_hash'])
        with self.assertRaises(KeyError): self.w.jobs('bob', j['job_id'])

    def test_cancellation_during_model_call_discards_result(self):
        j = self.evaluate()
        def complete(*args):
            self.w.cancel('alice', j['job_id'])
            return {'text': '计划：修复', 'model': 'test'}
        self.run_jobs(complete)
        self.assertEqual(self.w.jobs('alice', j['job_id'])['state'], 'canceled')
        self.assertIsNone(self.w.jobs('alice', j['job_id'])['result'])

    def test_generation_is_atomic_private_and_classified(self):
        j = self.w.generate('alice', material='做个周报助手', request_key='generate')
        self.run_jobs(lambda *_: {'text': json.dumps({'kind': 'method', 'summary': '提取方法', 'proposal': self.p})})
        done = self.w.jobs('alice', j['job_id'])
        self.assertEqual(done['state'], 'succeeded'); self.assertTrue(done['result']['saved'])
        self.assertEqual(len(self.a.list('alice')['items']), 2)
        self.assertEqual(self.a.list('bob')['items'], [])
        self.assertEqual(self.a.reviews('alice')['items'], [])
        j2 = self.w.generate('alice', material='以后用三段', request_key='preference')
        self.run_jobs(lambda *_: {'text': '{"kind":"preference","summary":"偏好三段","proposal":null}'})
        self.assertFalse(self.w.jobs('alice', j2['job_id'])['result']['saved'])
        self.assertEqual(len(self.a.list('alice')['items']), 2)

    def test_automatic_scope_disable_and_similar_candidate(self):
        self.a.preferences('alice', value={'auto_draft': True}, expected_revision=0)
        with self.assertRaises(PermissionError): self.w.generate('alice', material='已完成过程', request_key='a', automatic=True, scope='chat')
        self.w.scopes('alice', scope='chat', enabled=True)
        j = self.w.generate('alice', material='已完成过程', request_key='a', automatic=True, scope='chat')
        self.run_jobs(lambda *_: {'text': json.dumps({'kind': 'method', 'proposal': self.p})})
        result = self.w.jobs('alice', j['job_id'])['result']
        self.assertFalse(result['saved']); self.assertEqual(result['similar'][0]['draft_id'], self.d['draft_id'])
        j2 = self.w.generate('alice', material='新过程', request_key='b', automatic=True, scope='chat')
        self.w.scopes('alice', scope='chat', enabled=False)
        self.run_jobs(lambda *_: self.fail('disabled job must not invoke model'))
        self.assertEqual(self.w.jobs('alice', j2['job_id'])['state'], 'failed')

    def test_generation_retries_invalid_json_once_without_reusing_model_text(self):
        j = self.w.generate('alice', material='做个周报助手', request_key='format')
        calls = []
        def complete(system, prompt):
            calls.append((system, prompt))
            self.assertNotIn('不可信模型附言', system + prompt)
            return {'text': '不可信模型附言' if len(calls) == 1 else
                    json.dumps({'kind': 'method', 'proposal': self.p})}
        self.run_jobs(complete)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertTrue(self.w.jobs('alice', j['job_id'])['result']['saved'])
        self.assertEqual(len(self.a.list('alice')['items']), 2)

        j = self.w.generate('alice', material='做个助手', request_key='invalid')
        failures = []
        self.run_jobs(lambda *_: failures.append(1) or {'text': '```'})
        self.assertEqual(len(failures), 2)
        self.assertEqual(self.w.jobs('alice', j['job_id'])['state'], 'failed')
        self.assertEqual(len(self.a.list('alice')['items']), 2)

    def test_cancellation_after_invalid_generation_skips_format_retry(self):
        j = self.w.generate('alice', material='做个助手', request_key='cancel-format')
        calls = []
        def complete(*_):
            calls.append(1)
            self.w.cancel('alice', j['job_id'])
            return {'text': 'not json'}
        self.run_jobs(complete)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.w.jobs('alice', j['job_id'])['state'], 'canceled')
        self.assertEqual(len(self.a.list('alice')['items']), 1)

    def test_disable_during_generation_prevents_save(self):
        self.a.preferences('alice', value={'auto_draft': True}, expected_revision=0)
        self.w.scopes('alice', scope='chat', enabled=True)
        j = self.w.generate('alice', material='新方法', request_key='g', automatic=True, scope='chat')
        def complete(*args):
            self.a.preferences('alice', value={'auto_draft': False}, expected_revision=1)
            return {'text': json.dumps({'kind':'method', 'proposal':{**self.p, 'name':'完全不同', 'description':'测试独立内容'}})}
        self.run_jobs(complete)
        self.assertEqual(len(self.a.list('alice')['items']), 1)
        self.assertEqual(self.w.jobs('alice', j['job_id'])['state'], 'failed')

    def test_expired_claim_resumes_and_stale_worker_cannot_finish(self):
        j = self.evaluate(); old = self.w.claim()
        with closing(self.w.store.connect()) as db, db:
            db.execute("UPDATE skill_jobs SET lease_until='2000-01-01' WHERE job_id=?", (j['job_id'],))
        new = self.w.claim()
        self.assertNotEqual(old['claim_token'], new['claim_token'])
        self.w._finish(old, {'forged': True})
        self.assertEqual(self.w.jobs('alice', j['job_id'])['state'], 'running')
        self.w._finish(new, {'real': True})
        self.assertEqual(self.w.jobs('alice', j['job_id'])['result'], {'real': True})

    def test_failure_sanitized_retry_and_idempotency(self):
        j = self.evaluate()
        self.assertEqual(self.evaluate()['job_id'], j['job_id'])
        with self.assertRaises(UserGrantConflict): self.evaluate(repeats=2)
        def fail(*args): raise RuntimeError('Bearer do-not-leak')
        self.run_jobs(fail)
        self.assertNotIn('Bearer', self.w.jobs('alice', j['job_id'])['error'])
        self.w.retry('alice', j['job_id'])
        self.run_jobs(lambda *_: {'text':'计划：修复'})
        self.assertEqual(self.w.jobs('alice', j['job_id'])['state'], 'succeeded')

    def test_standard_import_roundtrip_and_path_rejection(self):
        pack = self.w.export('alice', self.d['draft_id'])
        result = self.w.import_package('bob', package=pack, request_key='import')
        self.assertNotEqual(result['skill_id'], self.d['skill_id'])
        self.assertEqual(self.a.get('bob', result['draft_id'])['bundle']['resources']['SKILL.md'].strip(), self.p['instructions'])
        for path in ('../secret.md', 'references/../../secret.md', 'scripts/run.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.w.import_package('bob', package={**pack, 'files':{**pack['files'], path:'bad'}}, request_key=path)
        with self.assertRaises(ValueError): import_standard({'format':'agentskills.files.v1','files':{'SKILL.md':'---\nname: !evil\ndescription: x\n---\nx'}})

    def test_failed_evaluation_blocks_submission_and_review_keeps_its_frozen_report(self):
        self.evaluate(); self.run_jobs(lambda *_: {'text':'已修复'})
        t = self.a.test_start('alice', draft_id=self.d['draft_id'], expected_revision=1, profile='use', prompt='试一下', request_key='sample')
        self.a.test_result('alice', test_id=t['test_id'], output='计划修复')
        with self.assertRaisesRegex(ValueError, '未通过'):
            self.a.submit('alice', draft_id=self.d['draft_id'], expected_revision=1, request_key='blocked-submit', reason='发布')
        self.evaluate('better'); self.run_jobs(lambda *_: {'text':'计划修复'})
        r = self.a.submit('alice', draft_id=self.d['draft_id'], expected_revision=1, request_key='submit', reason='发布')
        self.evaluate('later-failure'); self.run_jobs(lambda *_: {'text':'已修复'})
        detail = self.a.review_detail(r['request_id'])
        self.assertTrue(detail['quality']['evaluation']['passed'])
        self.assertFalse(self.w.inspect('alice', self.d['draft_id'])['publication_evidence']['profiles'][0]['eligible'])
        approved = self.a.decide(actor={'role':'admin', 'username':'reviewer'}, request_id=r['request_id'], decision='approve', reason='核对冻结证据', reviewed_tests=True)
        self.assertEqual(approved['state'], 'published')

    def _profile_draft(self):
        return self.a.save('alice', proposal={**self.p, 'profiles': {'preview': {}, 'fill': {}}}, request_key='profiles')

    def _profile_evaluation(self, draft, *, key='profiles-eval', profiles=('preview', 'fill'), failed=(), trigger=None, repeats=1):
        cases = [{'prompt': profile, 'profile': profile, 'contains': ['计划']} for profile in profiles]
        if trigger is not None:
            cases.append({'prompt':'trigger', 'profile':'fill', 'kind':'trigger', 'expected_trigger':False})
        job = self.w.evaluate('alice', draft_id=draft['draft_id'], expected_revision=draft['revision'],
                              cases=cases, request_key=key, repeats=repeats)
        self.run_jobs(lambda _system, prompt: {'text': trigger if prompt == 'trigger' else '已完成' if prompt in failed else '计划'})
        return self.w.jobs('alice', job['job_id'])['result']

    def _profile_sample(self, draft, profile='preview'):
        test = self.a.test_start('alice', draft_id=draft['draft_id'], expected_revision=draft['revision'],
                                profile=profile, prompt='合成计划', request_key='sample-' + profile)
        self.a.test_result('alice', test_id=test['test_id'], output='计划')

    def test_mixed_output_report_allows_only_passing_profile_through_frozen_approval(self):
        draft = self._profile_draft()
        report = self._profile_evaluation(draft, failed=('fill',), trigger='false', repeats=2)
        self.assertEqual(report['schema'], EVALUATION_SCHEMA)
        self.assertEqual(report['repeats'], 2)
        self.assertFalse(report['passed'])
        evidence = self.w.inspect('alice', draft['draft_id'])['publication_evidence']
        modes = {p['profile']: p for p in evidence['profiles']}
        self.assertTrue(modes['preview']['passed']); self.assertTrue(modes['preview']['eligible'])
        self.assertEqual(modes['preview']['source'], 'evaluation')
        self.assertTrue(modes['fill']['blocked']); self.assertFalse(modes['fill']['eligible'])
        self.assertFalse(evidence['shared']['blocked'])
        with self.assertRaisesRegex(ValueError, '未通过'):
            self.a.submit('alice', draft_id=draft['draft_id'], expected_revision=1,
                          profiles=['preview', 'fill'], request_key='both', reason='同时申请')
        request = self.a.submit('alice', draft_id=draft['draft_id'], expected_revision=1,
                                profiles=['preview'], request_key='preview', reason='仅申请预览')
        frozen = self.a.review_detail(request['request_id'])['quality']
        self.assertEqual(frozen['publication_evidence']['selected_profiles'], ['preview'])
        self.assertEqual(frozen['publication_evidence']['report_job_id'], report['job_id'])
        self._profile_evaluation(draft, key='later', failed=('preview', 'fill'))
        self.a.save('alice', draft_id=draft['draft_id'], expected_revision=1,
                    proposal={**self.p, 'profiles': {'preview': {}, 'fill': {}}, 'instructions': '已编辑的新方法'}, request_key='edit-after-submit')
        approved = self.a.decide(actor={'role':'admin', 'username':'reviewer'}, request_id=request['request_id'],
                                 decision='approve', reason='复核冻结的预览结果', reviewed_tests=True)
        self.assertEqual(approved['state'], 'published')
        published = self.s.skills.current(draft['skill_id'])
        self.assertEqual(list(published['manifest']['profiles']), ['preview'])
        self.assertNotIn('已编辑的新方法', published['resources']['SKILL.md'])
        self.assertEqual(self.a.review_detail(request['request_id'])['quality'], frozen)

    def test_shared_trigger_failure_blocks_every_profile_even_with_reported_samples(self):
        draft = self._profile_draft()
        self._profile_sample(draft)
        self._profile_evaluation(draft, trigger='true')
        evidence = self.w.inspect('alice', draft['draft_id'])['publication_evidence']
        self.assertTrue(evidence['shared']['blocked'])
        self.assertEqual(evidence['shared']['case_indexes'], [2])
        self.assertTrue(all(p['blocked'] and not p['eligible'] for p in evidence['profiles']))
        with self.assertRaisesRegex(ValueError, '未通过'):
            self.a.submit('alice', draft_id=draft['draft_id'], expected_revision=1,
                          profiles=['preview'], request_key='shared-failed', reason='人工样例不能覆盖共享失败')

    def test_sample_cannot_override_its_failed_output_and_other_profile_report_requires_manual_reason(self):
        draft = self._profile_draft()
        self._profile_sample(draft)
        self._profile_evaluation(draft, failed=('preview',))
        with self.assertRaisesRegex(ValueError, '未通过'):
            self.a.submit('alice', draft_id=draft['draft_id'], expected_revision=1,
                          profiles=['preview'], request_key='failed-output', reason='不能覆盖失败')
        self._profile_evaluation(draft, key='fill-only', profiles=('fill',), trigger='false')
        evidence = self.w.inspect('alice', draft['draft_id'])['publication_evidence']
        preview = next(p for p in evidence['profiles'] if p['profile'] == 'preview')
        self.assertEqual((preview['passed'], preview['eligible'], preview['source']), (False, True, 'sample'))
        request = self.a.submit('alice', draft_id=draft['draft_id'], expected_revision=1,
                                profiles=['preview'], request_key='manual-preview', reason='人工核对预览')
        args = {'actor':{'role':'admin', 'username':'reviewer'}, 'request_id':request['request_id'],
                'decision':'approve', 'reason':'已复核', 'reviewed_tests':True}
        with self.assertRaisesRegex(ValueError, '人工验证说明'):
            self.a.decide(**args)
        self.a.decide(**args, manual_quality_reason='预览只有人工样例，逐句核对计划与完成。')
        self.assertIn('预览只有人工样例', self.a.review_detail(request['request_id'])['quality']['manual_quality_reason'])

    def test_latest_report_does_not_combine_output_coverage_from_separate_reports(self):
        draft = self._profile_draft()
        first = self._profile_evaluation(draft, key='preview-only', profiles=('preview',))
        second = self._profile_evaluation(draft, key='fill-only', profiles=('fill',))
        evidence = self.w.inspect('alice', draft['draft_id'])['publication_evidence']
        self.assertEqual(evidence['report_job_id'], second['job_id'])
        self.assertNotEqual(first['job_id'], second['job_id'])
        self.assertEqual({p['profile']:p['eligible'] for p in evidence['profiles']}, {'preview':False, 'fill':True})
        with self.assertRaisesRegex(ValueError, '每个申请模式'):
            self.a.submit('alice', draft_id=draft['draft_id'], expected_revision=1,
                          profiles=['preview', 'fill'], request_key='no-merge', reason='不能拼接报告')
        self._profile_evaluation(draft, key='complete')
        self.a.submit('alice', draft_id=draft['draft_id'], expected_revision=1,
                      profiles=['preview', 'fill'], request_key='complete-submit', reason='同一完整报告')

    def test_profile_evidence_rejects_missing_duplicate_inconsistent_and_stale_candidate_rows(self):
        draft = self._profile_draft()
        report = self._profile_evaluation(draft, trigger='false', repeats=2)
        bundle = self.a.get('alice', draft['draft_id'])['bundle']
        def evidence(value): return publication_evidence(bundle, 1, ['preview', 'fill'], value)
        for label, mutate in [
            ('missing row', lambda r: r['rows'].pop(0)),
            ('duplicate row', lambda r: r['rows'].append(deepcopy(r['rows'][0]))),
            ('missing repeat', lambda r: r.update(repeats=3)),
            ('missing repeats declaration', lambda r: r.pop('repeats')),
            ('invalid repeat', lambda r: r['rows'][0].update(repeat=99)),
            ('invalid mode', lambda r: r['cases'][0].update(profile='unknown')),
            ('row verdict differs from output', lambda r: r['rows'][0].update(passed=False)),
            ('empty output', lambda r: r['rows'][0].update(output='')),
            ('stale revision', lambda r: r.update(revision=2)),
            ('stale content', lambda r: r.update(content_hash='other')),
        ]:
            with self.subTest(label=label):
                changed = deepcopy(report); mutate(changed)
                result = evidence(changed)
                self.assertTrue(result['shared']['blocked'])
                self.assertTrue(all(p['blocked'] and not p['eligible'] for p in result['profiles']))
        # Aggregate flags and baseline scores cannot override candidate row evidence.
        report['passed'] = False
        report['scores']['candidate']['passed'] = 0
        for row in report['rows']:
            if row['variant'] == 'without_skill': row['passed'] = False
        self.assertTrue(all(p['eligible'] for p in evidence(report)['profiles']))
        report['rows'][0].update(output='已完成', passed=False)
        report['passed'] = True
        modes = {p['profile']: p for p in evidence(report)['profiles']}
        self.assertTrue(modes['preview']['blocked'])
        self.assertTrue(modes['fill']['eligible'])

    def test_legacy_reports_remain_global_and_frozen_legacy_failures_cannot_be_approved(self):
        draft = self._profile_draft()
        report = self._profile_evaluation(draft, failed=('fill',))
        report.pop('schema'); report.pop('repeats')
        with closing(self.w.store.connect()) as db, db:
            db.execute('UPDATE skill_jobs SET result_json=? WHERE job_id=?', (json.dumps(report), report['job_id']))
        self._profile_sample(draft)
        evidence = self.w.inspect('alice', draft['draft_id'])['publication_evidence']
        self.assertTrue(all(p['blocked'] for p in evidence['profiles']))
        with self.assertRaisesRegex(ValueError, '未通过'):
            self.a.submit('alice', draft_id=draft['draft_id'], expected_revision=1,
                          profiles=['preview'], request_key='legacy-failed', reason='不能拆开旧报告')
        passing = self._profile_evaluation(draft, key='passing')
        passing.pop('schema'); passing.pop('repeats')
        with closing(self.w.store.connect()) as db, db:
            db.execute('UPDATE skill_jobs SET result_json=? WHERE job_id=?', (json.dumps(passing), passing['job_id']))
        request = self.a.submit('alice', draft_id=draft['draft_id'], expected_revision=1,
                                profiles=['preview'], request_key='legacy-passing', reason='旧报告整体通过')
        # Model a pending review created before this change: its failed report
        # remains frozen even though the latest report now passes.
        frozen = self.a.review_detail(request['request_id'])['quality']
        frozen['evaluation'] = report
        frozen.pop('publication_evidence')
        with closing(self.w.store.connect()) as db, db:
            db.execute('UPDATE skill_review_quality SET snapshot_json=? WHERE request_id=?',
                       (json.dumps(frozen), request['request_id']))
        with self.assertRaisesRegex(ValueError, '独立评测未通过'):
            self.a.decide(actor={'role':'admin', 'username':'reviewer'}, request_id=request['request_id'],
                          decision='approve', reason='不能沿用新报告', reviewed_tests=True,
                          manual_quality_reason='人工复核不能覆盖失败')

    def test_no_empty_or_vacuous_evaluations_and_trigger_negatives(self):
        for cases in ([], [{'prompt':'x'}], [{'prompt':'x','contains':['']}], [{'prompt':'x','kind':'trigger','expected_trigger':'false'}]):
            with self.assertRaises(ValueError): validate_cases(cases)
        case=validate_cases([{'prompt':'查询原始记录','kind':'trigger','expected_trigger':False}])[0]
        self.assertTrue(grade_output(case, 'false')['passed'])
        self.assertFalse(grade_output(case, 'true')['passed'])
        self.assertFalse(grade_output(case, 'false, but maybe')['passed'])

    def test_previous_report_reuses_only_this_owned_drafts_inputs_across_revisions(self):
        job = self.evaluate(repeats=2)
        self.run_jobs(lambda *_: {'text': '计划修复'})
        original = self.w.jobs('alice', job['job_id'])['result']
        second = self.a.save('alice', draft_id=self.d['draft_id'], expected_revision=1,
                             proposal={**self.p, 'instructions': '新修订需重新评测。'}, request_key='revision-2')
        for owner in ('alice', 'bob'):
            other = self.a.save(owner, proposal=self.p, request_key='other')
            self.w.evaluate(owner, draft_id=other['draft_id'], expected_revision=1,
                            cases=[{'prompt': '别人的问题', 'contains': ['他人']}], request_key='other-eval')
            self.run_jobs(lambda *_: {'text': '他人'})
        inspected = self.w.inspect('alice', self.d['draft_id'])
        previous = inspected['previous_report']
        self.assertEqual(previous['job_id'], job['job_id'])
        self.assertEqual(previous['revision'], 1)
        self.assertEqual(previous['repeats'], 2)
        self.assertEqual(previous['cases'], original['cases'])
        self.assertFalse({'passed', 'scores', 'rows'} & previous.keys())
        self.assertIsNone(inspected['evaluation'])
        self.assertFalse(inspected['publication_evidence']['profiles'][0]['eligible'])
        with self.assertRaises(KeyError): self.w.inspect('bob', self.d['draft_id'])
        rerun = self.w.evaluate('alice', draft_id=self.d['draft_id'], expected_revision=second['revision'],
            cases=previous['cases'], repeats=previous['repeats'], request_key='rerun')
        self.run_jobs(lambda *_: {'text': '这次缺少所需关键词'})
        self.assertFalse(self.w.jobs('alice', rerun['job_id'])['result']['passed'])
        self.assertEqual(self.w.inspect('alice', self.d['draft_id'])['previous_report']['revision'], 2)

    def test_sample_feedback_freezes_revision_mode_and_prompt_without_assertions(self):
        sample = self.a.test_start('alice', draft_id=self.d['draft_id'], expected_revision=1,
                                   profile='use', prompt='下周计划修复登录', request_key='sample')
        self.a.test_result('alice', test_id=sample['test_id'], output='已完成登录修复')
        self.a.save('alice', draft_id=self.d['draft_id'], expected_revision=1,
                    proposal={**self.p, 'profiles': {'preview': {}}}, request_key='remove-old-mode')
        feedback = self.a.dispatch('alice', 'feedback', {'skill_id': self.d['skill_id'], 'profile': 'use',
            'test_id': sample['test_id'], 'rating': 'incorrect', 'comment': '这是计划，不能写成已经完成。'})
        self.assertEqual(feedback['version'], 'draft:1')
        self.assertEqual(feedback['source']['revision'], 1)
        self.assertEqual(feedback['source']['profile'], 'use')
        self.assertEqual(feedback['source']['prompt'], '下周计划修复登录')
        self.assertFalse({'contains', 'excludes', 'passed'} & feedback['source'].keys())
        inspected = self.w.inspect('alice', self.d['draft_id'])
        self.assertEqual(inspected['feedback']['items'][0]['source'], feedback['source'])
        self.assertEqual(inspected['feedback']['items'][0]['comment'], '这是计划，不能写成已经完成。')
        self.assertEqual(inspected['feedback']['items'][0]['profile'], 'use')
        self.assertFalse(inspected['publication_evidence']['profiles'][0]['eligible'])
        with self.assertRaisesRegex(ValueError, '评测模式不存在'):
            self.w.evaluate('alice', draft_id=self.d['draft_id'], expected_revision=2, request_key='old-mode',
                cases=[{'prompt': feedback['source']['prompt'], 'profile': 'use', 'contains': ['计划']}])
        with self.assertRaises(ValueError):
            validate_cases([{'prompt': feedback['source']['prompt'], 'profile': 'preview'}])

    def test_feedback_source_rejects_foreign_mismatched_unfinished_and_multiple_references(self):
        sample = self.a.test_start('alice', draft_id=self.d['draft_id'], expected_revision=1,
                                   profile='use', prompt='合成问题', request_key='sample')
        args = {'skill_id': self.d['skill_id'], 'profile': 'use', 'rating': 'incorrect', 'test_id': sample['test_id']}
        with self.assertRaisesRegex(ValueError, '已保存'): self.w.feedback('alice', **args)
        self.a.test_result('alice', test_id=sample['test_id'], output='答案')
        for owner, changes, exception in [
            ('bob', {}, KeyError), ('alice', {'profile': 'fill'}, ValueError),
            ('alice', {'skill_id': 'another-skill'}, ValueError),
            ('alice', {'version': '1.0.0'}, UserGrantConflict),
            ('alice', {'binding_id': 'another-binding'}, ValueError),
        ]:
            with self.subTest(owner=owner, changes=changes), self.assertRaises(exception):
                self.w.feedback(owner, **{**args, **changes})
        self.assertEqual(self.w.metrics()['feedback'], [])

    def _publish_sample(self, revision, *, profiles=('use',)):
        for profile in profiles:
            sample = self.a.test_start('alice', draft_id=self.d['draft_id'], expected_revision=revision,
                profile=profile, prompt='计划修复', request_key=f'publish-sample-{revision}-{profile}')
            self.a.test_result('alice', test_id=sample['test_id'], output='计划修复')
        request = self.a.submit('alice', draft_id=self.d['draft_id'], expected_revision=revision,
            request_key=f'publish-{revision}', reason='个人使用', profiles=list(profiles))
        return self.a.decide(actor={'role': 'admin', 'username': 'reviewer'}, request_id=request['request_id'],
            decision='approve', reason='核对样例', reviewed_tests=True, manual_quality_reason='逐句核对输入与输出')

    def test_binding_feedback_keeps_original_version_and_does_not_restore_execution_access(self):
        from agentbridge.core.business_skills import SkillRejected
        self._publish_sample(1)
        binding = self.s.skills.bind('alice', self.d['skill_id'], 'use')
        self.a.save('alice', draft_id=self.d['draft_id'], expected_revision=1,
                    proposal={**self.p, 'instructions': '新的方法内容'}, request_key='second')
        self._publish_sample(2)
        with closing(self.w.store.connect()) as db, db:
            db.execute('UPDATE skill_bindings SET revoked=1 WHERE binding_id=?', (binding,))
        args = {'skill_id': self.d['skill_id'], 'profile': 'use', 'rating': 'incorrect', 'binding_id': binding}
        result = self.w.feedback('alice', **args)
        self.assertEqual(result['version'], '1.0.0')
        self.assertEqual(result['source']['revision'], 1)
        self.assertEqual(result['source']['binding_id'], binding)
        with self.assertRaises(SkillRejected): self.s.skills.binding('alice', binding)
        with self.assertRaises(KeyError): self.w.feedback('bob', **args)
        with self.assertRaises(ValueError): self.w.feedback('alice', **{**args, 'profile': 'fill'})
        with self.assertRaises(UserGrantConflict): self.w.feedback('alice', **args, version='1.0.1')
        with self.assertRaises(UserGrantConflict):
            self.w.feedback('alice', skill_id=self.d['skill_id'], profile='use', rating='incorrect', version='1.0.0')
        current = self.w.feedback('alice', skill_id=self.d['skill_id'], profile='use', rating='useful', version='1.0.1')
        self.assertEqual(current['source']['kind'], 'catalog')
        self.assertEqual(current['version'], '1.0.1')

    def test_feedback_context_preserves_legacy_writes_unknown_modes_and_atomicity(self):
        import sqlite3
        from agentbridge.core.skill_workbench import SkillWorkbench
        sample = self.a.test_start('alice', draft_id=self.d['draft_id'], expected_revision=1,
                                   profile='use', prompt='问题', request_key='sample')
        self.a.test_result('alice', test_id=sample['test_id'], output='答案')
        with closing(self.w.store.connect()) as db, db:
            db.execute('DROP TABLE skill_feedback_context')
            db.execute('INSERT INTO skill_feedback VALUES (?,?,?,?,?,?,?)',
                ('old', 'alice', self.d['skill_id'], '1.0.0', 'useful', '旧版评价', '2026-01-01'))
        restored = SkillWorkbench(self.a)
        self.assertEqual(restored.metrics('alice')['feedback'][0]['profile'], 'unknown')
        self.assertEqual(restored.inspect('alice', self.d['draft_id'])['feedback']['items'][0]['source'], {})
        with closing(self.w.store.connect()) as db, db:
            self.assertEqual(len(db.execute('PRAGMA table_info(skill_feedback)').fetchall()), 7)
            db.execute('INSERT INTO skill_feedback VALUES (?,?,?,?,?,?,?)',
                ('old-after-upgrade', 'alice', self.d['skill_id'], '1.0.0', 'useful', '旧版仍可写入', '2026-01-02'))
            db.execute("CREATE TRIGGER fail_context BEFORE INSERT ON skill_feedback_context BEGIN SELECT RAISE(ABORT,'injected'); END")
        args = {'skill_id': self.d['skill_id'], 'profile': 'use', 'rating': 'incorrect', 'test_id': sample['test_id']}
        with self.assertRaises(sqlite3.IntegrityError): restored.feedback('alice', **args)
        with closing(self.w.store.connect()) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM skill_feedback').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM skill_feedback_context').fetchone()[0], 0)
            db.execute('DROP TRIGGER fail_context')
        restored.feedback('alice', **args)
        metrics = restored.metrics('alice')['feedback']
        self.assertEqual({item['profile'] for item in metrics}, {'unknown', 'use'})
        self.assertEqual(restored.metrics('bob')['feedback'], [])

    def test_feedback_metrics_separate_modes_and_recent_items_remain_private_and_bounded(self):
        self.a.save('alice', draft_id=self.d['draft_id'], expected_revision=1,
                    proposal={**self.p, 'profiles': {'preview': {}, 'fill': {}}}, request_key='modes')
        for profile in ('preview', 'fill'):
            sample = self.a.test_start('alice', draft_id=self.d['draft_id'], expected_revision=2,
                profile=profile, prompt='计划修复', request_key='sample-' + profile)
            self.a.test_result('alice', test_id=sample['test_id'], output='计划修复')
            self.w.feedback('alice', skill_id=self.d['skill_id'], profile=profile, rating='useful', test_id=sample['test_id'])
        self.assertEqual({(r['version'], r['profile'], r['count']) for r in self.w.metrics('alice')['feedback']},
                         {('draft:2', 'preview', 1), ('draft:2', 'fill', 1)})
        with closing(self.w.store.connect()) as db, db:
            for index in range(22):
                db.execute('INSERT INTO skill_feedback VALUES (?,?,?,?,?,?,?)',
                    (f'own-{index}', 'alice', self.d['skill_id'], '1.0.0', 'useful', f'我的-{index}', f'2099-01-{index + 1:02}'))
            db.execute('INSERT INTO skill_feedback VALUES (?,?,?,?,?,?,?)',
                ('foreign', 'bob', self.d['skill_id'], '1.0.0', 'incorrect', '他人的私密纠错', '2100-01-01'))
        items = self.w.inspect('alice', self.d['draft_id'])['feedback']['items']
        self.assertEqual(len(items), 20)
        self.assertEqual(items[0]['feedback_id'], 'own-21')
        self.assertTrue(all(item['comment'].startswith('我的-') for item in items))

    def test_mcp_feedback_accepts_owned_sample_reference_but_not_client_attribution(self):
        from tests.test_skill_transport_contracts import mcp_authoring, mcp_call
        with mcp_authoring() as (service, token, client):
            draft = service.skill_authoring.save('user-a', proposal=self.p, request_key='draft')
            sample = service.skill_authoring.test_start('user-a', draft_id=draft['draft_id'], expected_revision=1,
                profile='use', prompt='下周计划修复', request_key='sample')
            service.skill_authoring.test_result('user-a', test_id=sample['test_id'], output='计划修复')
            data = {'skill_id': draft['skill_id'], 'profile': 'use', 'rating': 'incorrect',
                    'test_id': sample['test_id'], 'comment': '请保留计划状态'}
            result = mcp_call(client, token, 'feedback', data).json()['result']
            self.assertFalse(result['isError'], result)
            self.assertEqual(result['structuredContent']['source']['revision'], 1)
            self.assertEqual(result['structuredContent']['version'], 'draft:1')
            forged = mcp_call(client, token, 'feedback', {**data, 'source': {'revision': 999}}).json()['result']
            self.assertTrue(forged['isError'])
            wrong_mode = mcp_call(client, token, 'feedback', {**data, 'profile': 'fill'}).json()['result']
            self.assertTrue(wrong_mode['isError'])

    def test_internal_job_token_cannot_be_supplied_through_public_dispatch(self):
        with self.assertRaises(ValueError): self.a.dispatch('alice', 'save', {'proposal':self.p,'request_key':'hack','_job':{}})
        with self.assertRaises(KeyError): self.w.inspect('bob', self.d['draft_id'])
        self.assertEqual(self.w.discover('bob')['items'], [])

    def test_independent_report_can_replace_quick_sample_and_resources_stay_pinned(self):
        d = self.a.save('alice', proposal={**self.p, 'references':{'references/extra.md':'旧资料'}, 'required_resources':{'use':[]}}, request_key='resource')
        self.w.evaluate('alice', draft_id=d['draft_id'], expected_revision=1,
                        cases=[{'prompt':'整理计划', 'contains':['计划']}], request_key='resource-eval')
        self.run_jobs(lambda *_:{'text':'计划修复'})
        r = self.a.submit('alice', draft_id=d['draft_id'], expected_revision=1, request_key='resource-submit', reason='个人使用')
        self.a.decide(actor={'role':'admin','username':'admin'},request_id=r['request_id'],decision='approve',reason='核对依据',reviewed_tests=True)
        binding = self.s.skills.bind('alice', d['skill_id'], 'use')
        self.assertEqual(self.w.resource('alice',binding_id=binding,path='references/extra.md')['content'],'旧资料')
        from agentbridge.core.business_skills import SkillRejected
        with self.assertRaises(SkillRejected): self.w.resource('bob',binding_id=binding,path='references/extra.md')
        with self.assertRaises(KeyError): self.w.resource('alice',binding_id=binding,path='../secret')
        self.w.feedback('alice',skill_id=d['skill_id'],profile='use',version='1.0.0',rating='useful')
        self.assertEqual(self.w.metrics('bob')['feedback'], [])
        self.assertEqual(self.w.metrics('alice')['feedback'][0]['count'],1)
        with self.assertRaises(SkillRejected): self.w.composition('bob',steps=[{'skill_id':d['skill_id'],'profile':'use','input':'文字','output':'周报'}])

    def test_manual_review_requires_explicit_validation_description(self):
        t=self.a.test_start('alice',draft_id=self.d['draft_id'],expected_revision=1,profile='use',prompt='试一下',request_key='s')
        self.a.test_result('alice',test_id=t['test_id'],output='计划修复')
        r=self.a.submit('alice',draft_id=self.d['draft_id'],expected_revision=1,request_key='r',reason='个人使用')
        args={'actor':{'role':'admin','username':'admin'},'request_id':r['request_id'],'decision':'approve','reason':'审核','reviewed_tests':True}
        with self.assertRaises(ValueError):self.a.decide(**args)
        self.a.decide(**args,manual_quality_reason='逐句与合成输入对照，无业务执行')
        self.assertIsNone(self.a.review_detail(r['request_id'])['quality']['evaluation'])

    def test_cancel_generation_in_flight_never_saves_draft(self):
        j=self.w.generate('alice',material='做个助手',request_key='cancel-generation')
        def complete(*args):
            self.w.cancel('alice',j['job_id'])
            return {'text':json.dumps({'kind':'method','proposal':self.p})}
        self.run_jobs(complete)
        self.assertEqual(len(self.a.list('alice')['items']),1)
        self.assertEqual(self.w.jobs('alice',j['job_id'])['state'],'canceled')

    def test_completed_workspace_capture_is_durable_and_opted_in(self):
        from unittest.mock import patch
        from tests.test_workspace import _create_account, FakeGateway
        from agentbridge.workspace.application import WorkspaceApplication
        from agentbridge.core.skill_workbench import SkillWorkbench
        account=_create_account(self.s,user_subject='alice',username='alice',endpoint_key='telegram:*:alice')
        self.a.preferences('alice',value={'auto_draft':True},expected_revision=0)
        self.w.scopes('alice',scope='workspace:'+account['account_id'],enabled=True)
        with patch.object(WorkspaceApplication,'_ensure_dispatch_worker'), patch.object(SkillWorkbench,'start'):
            with closing(WorkspaceApplication(service=self.s,gateway=FakeGateway())) as app:
                app.send_chat_stream(account,message='先读取分页，再核对依据。',idempotency_key='method')
                d=self.s.workspace.claim_next_host_dispatch(account_id=account['account_id'],claim_owner='test')
                d=self.s.workspace.mark_host_dispatch_accepted(d['dispatch_id'],claim_token=d['claim_token'],run_id='method')
                app._finish_accepted_dispatch(d,account,{'state':'final','text':'已按分页读取并逐项核对。'})
                with closing(self.w.store.connect()) as db:
                    capture=dict(db.execute('SELECT * FROM skill_auto_capture').fetchone())
                self.assertEqual(capture['state'],'queued')
                app._capture_skill_method(capture)
                app._capture_skill_method(capture)
                jobs=self.w.jobs('alice')['items']
                self.assertEqual(len(jobs),1);self.assertTrue(jobs[0]['automatic'])


    def test_report_preserves_baseline_identity_after_new_publication(self):
        self._publish_sample(1)
        original = self.s.skills.current(self.d['skill_id'])
        job = self.evaluate('frozen-baseline')
        self.a.save('alice', draft_id=self.d['draft_id'], expected_revision=1,
                    proposal={**self.p,'instructions':'新的方法'}, request_key='update-before-eval')
        self._publish_sample(2)
        self.run_jobs(lambda *_: {'text':'计划修复登录问题','model':'synthetic'})
        report = self.w.jobs('alice',job['job_id'])['result']
        self.assertEqual(report['revision'],1)
        self.assertEqual(report['baseline'], {'version':'1.0.0','content_hash':original['content_hash']})
        self.assertEqual(self.s.skills.current(self.d['skill_id'])['manifest']['version'],'1.0.1')



    def test_fixed_regression_survives_ad_hoc_tests_and_new_revision(self):
        from agentbridge.core.skill_quality import case_fingerprint
        first = self.evaluate('fixed', repeats=2)
        with self.assertRaises(KeyError):
            self.w.set_regression('alice',draft_id=self.d['draft_id'],expected_revision=1,job_id=first['job_id'])
        self.run_jobs(lambda *_: {'text':'计划修复'})
        fixed = self.w.set_regression('alice',draft_id=self.d['draft_id'],expected_revision=1,job_id=first['job_id'])
        self.evaluate('temporary'); self.run_jobs(lambda *_: {'text':'计划修复'})
        self.assertEqual(self.w.regression('alice',self.d['draft_id']),fixed)
        self.assertNotIn('passed',fixed)
        self.a.save('alice',draft_id=self.d['draft_id'],expected_revision=1,proposal={**self.p,'instructions':'新方法'},request_key='next')
        self.assertEqual(self.w.inspect('alice',self.d['draft_id'])['regression'],fixed)
        with self.assertRaises(UserGrantConflict):
            self.w.set_regression('alice',draft_id=self.d['draft_id'],expected_revision=1,job_id=None)
        with self.assertRaises(KeyError): self.w.regression('bob',self.d['draft_id'])
        other = self.a.save('alice',proposal={**self.p,'name':'另一个'},request_key='other')
        with self.assertRaises(KeyError):
            self.w.set_regression('alice',draft_id=other['draft_id'],expected_revision=1,job_id=first['job_id'])
        self.assertNotEqual(case_fingerprint(fixed['cases'],1),fixed['case_fingerprint'])
        attributed=[{**c,'feedback_id':'origin'} for c in fixed['cases']]
        self.assertEqual(case_fingerprint(attributed,2),fixed['case_fingerprint'])
        self.assertIsNone(self.w.set_regression('alice',draft_id=self.d['draft_id'],expected_revision=2,job_id=None))

    def test_load_feedback_is_original_scoped_idempotent_and_reusable(self):
        from agentbridge.core.skill_workbench import TaskFeedbackError
        self._publish_sample(1)
        binding = self.s.skills.bind('alice',self.d['skill_id'],'use')
        self.s.skills.record_load('alice',self.d['skill_id'],'use','SKILL.md',
            {'status':'succeeded','version':'1.0.0','binding_id':binding})
        event = self.w.load_feedback_history('alice')[0]
        self.assertTrue(event['feedback']['available'])
        self.a.save('alice',draft_id=self.d['draft_id'],expected_revision=1,proposal={**self.p,'instructions':'新方法'},request_key='next')
        self._publish_sample(2)
        with closing(self.w.store.connect()) as db, db:
            db.execute('UPDATE skill_bindings SET revoked=1 WHERE binding_id=?',(binding,))
        args={'rating':'incorrect','comment':'不要补效果。','request_key':'load-feedback-key'}
        saved=self.w.record_load_feedback('alice',event['event_id'],**args)
        self.assertEqual(saved['version'],'1.0.0')
        self.assertEqual(self.w.record_load_feedback('alice',event['event_id'],**{**args,'request_key':'different-request-key'}),saved)
        with self.assertRaises(TaskFeedbackError): self.w.record_load_feedback('alice',event['event_id'],**{**args,'comment':'另一评价'})
        with self.assertRaises(KeyError): self.w.record_load_feedback('bob',event['event_id'],**args)
        self.assertEqual(self.w.load_feedback_history('alice')[0]['feedback']['receipt'],saved)
        cases=[{'prompt':'整理计划','contains':['计划'],'feedback_id':saved['feedback_id']}]
        job=self.w.evaluate('alice',draft_id=self.d['draft_id'],expected_revision=2,cases=cases,request_key='from-feedback')
        self.run_jobs(lambda *_:{'text':'计划修复'})
        self.assertEqual(self.w.jobs('alice',job['job_id'])['result']['cases'][0]['feedback_id'],saved['feedback_id'])
        other=self.a.save('alice',proposal={**self.p,'name':'另一助手'},request_key='other')
        with self.assertRaises(KeyError): self.w.evaluate('alice',draft_id=other['draft_id'],expected_revision=1,cases=cases,request_key='forged')
        self.s.skills.record_load('alice',self.d['skill_id'],'use','SKILL.md',{'status':'succeeded','version':'1.0.1'})
        self.assertFalse(self.w.load_feedback_history('alice')[0]['feedback']['available'])

if __name__ == '__main__': unittest.main()
