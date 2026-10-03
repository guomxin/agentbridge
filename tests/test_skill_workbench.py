import json
from contextlib import closing
from tempfile import TemporaryDirectory
import unittest

from bscli.core.central_service import CentralCapabilityService
from bscli.core.skill_quality import grade_output, import_standard, validate_cases
from bscli.core.user_grants import UserGrantConflict


class SkillWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s = CentralCapabilityService(home=self.tmp.name, base_url='http://oa.test')
        self.a = self.s.skill_authoring; self.w = self.a.workbench
        self.p = {'name': '周报方法', 'description': '根据给定文字整理周报',
                  'selection': {'use_when': '整理周报', 'not_for': '查询和提交业务', 'output': '完成与计划'},
                  'instructions': '输入用户文字。步骤：区分计划和完成，核对依据。材料不足时追问。'}
        self.d = self.a.save('alice', proposal=self.p, request_key='draft')

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
        self.w.run_once(complete)
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
        self.w.run_once(complete)
        self.assertEqual(self.w.jobs('alice', j['job_id'])['state'], 'canceled')
        self.assertIsNone(self.w.jobs('alice', j['job_id'])['result'])

    def test_generation_is_atomic_private_and_classified(self):
        j = self.w.generate('alice', material='做个周报助手', request_key='generate')
        self.w.run_once(lambda *_: {'text': json.dumps({'kind': 'method', 'summary': '提取方法', 'proposal': self.p})})
        done = self.w.jobs('alice', j['job_id'])
        self.assertEqual(done['state'], 'succeeded'); self.assertTrue(done['result']['saved'])
        self.assertEqual(len(self.a.list('alice')['items']), 2)
        self.assertEqual(self.a.list('bob')['items'], [])
        self.assertEqual(self.a.reviews('alice')['items'], [])
        j2 = self.w.generate('alice', material='以后用三段', request_key='preference')
        self.w.run_once(lambda *_: {'text': '{"kind":"preference","summary":"偏好三段","proposal":null}'})
        self.assertFalse(self.w.jobs('alice', j2['job_id'])['result']['saved'])
        self.assertEqual(len(self.a.list('alice')['items']), 2)

    def test_automatic_scope_disable_and_similar_candidate(self):
        self.a.preferences('alice', value={'auto_draft': True}, expected_revision=0)
        with self.assertRaises(PermissionError): self.w.generate('alice', material='已完成过程', request_key='a', automatic=True, scope='chat')
        self.w.scopes('alice', scope='chat', enabled=True)
        j = self.w.generate('alice', material='已完成过程', request_key='a', automatic=True, scope='chat')
        self.w.run_once(lambda *_: {'text': json.dumps({'kind': 'method', 'proposal': self.p})})
        result = self.w.jobs('alice', j['job_id'])['result']
        self.assertFalse(result['saved']); self.assertEqual(result['similar'][0]['draft_id'], self.d['draft_id'])
        j2 = self.w.generate('alice', material='新过程', request_key='b', automatic=True, scope='chat')
        self.w.scopes('alice', scope='chat', enabled=False)
        self.w.run_once(lambda *_: self.fail('disabled job must not invoke model'))
        self.assertEqual(self.w.jobs('alice', j2['job_id'])['state'], 'failed')

    def test_generation_retries_invalid_json_once_without_reusing_model_text(self):
        j = self.w.generate('alice', material='做个周报助手', request_key='format')
        calls = []
        def complete(system, prompt):
            calls.append((system, prompt))
            self.assertNotIn('不可信模型附言', system + prompt)
            return {'text': '不可信模型附言' if len(calls) == 1 else
                    json.dumps({'kind': 'method', 'proposal': self.p})}
        self.w.run_once(complete)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertTrue(self.w.jobs('alice', j['job_id'])['result']['saved'])
        self.assertEqual(len(self.a.list('alice')['items']), 2)

        j = self.w.generate('alice', material='做个助手', request_key='invalid')
        failures = []
        self.w.run_once(lambda *_: failures.append(1) or {'text': '```'})
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
        self.w.run_once(complete)
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
        self.w.run_once(complete)
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
        self.w.run_once(fail)
        self.assertNotIn('Bearer', self.w.jobs('alice', j['job_id'])['error'])
        self.w.retry('alice', j['job_id'])
        self.w.run_once(lambda *_: {'text':'计划：修复'})
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

    def test_freeze_report_and_reject_failed_evaluation(self):
        self.evaluate(); self.w.run_once(lambda *_: {'text':'已修复'})
        t = self.a.test_start('alice', draft_id=self.d['draft_id'], expected_revision=1, profile='use', prompt='试一下', request_key='sample')
        self.a.test_result('alice', test_id=t['test_id'], output='计划修复')
        r = self.a.submit('alice', draft_id=self.d['draft_id'], expected_revision=1, request_key='submit', reason='发布')
        self.evaluate('better'); self.w.run_once(lambda *_: {'text':'计划修复'})
        detail = self.a.review_detail(r['request_id'])
        self.assertFalse(detail['quality']['evaluation']['passed'])
        with self.assertRaises(ValueError): self.a.decide(actor={'role':'admin'}, request_id=r['request_id'], decision='approve', reason='x', reviewed_tests=True)

    def test_no_empty_or_vacuous_evaluations_and_trigger_negatives(self):
        for cases in ([], [{'prompt':'x'}], [{'prompt':'x','contains':['']}], [{'prompt':'x','kind':'trigger','expected_trigger':'false'}]):
            with self.assertRaises(ValueError): validate_cases(cases)
        case=validate_cases([{'prompt':'查询原始记录','kind':'trigger','expected_trigger':False}])[0]
        self.assertTrue(grade_output(case, 'false')['passed'])
        self.assertFalse(grade_output(case, 'true')['passed'])
        self.assertFalse(grade_output(case, 'false, but maybe')['passed'])

    def test_internal_job_token_cannot_be_supplied_through_public_dispatch(self):
        with self.assertRaises(ValueError): self.a.dispatch('alice', 'save', {'proposal':self.p,'request_key':'hack','_job':{}})
        with self.assertRaises(KeyError): self.w.inspect('bob', self.d['draft_id'])
        self.assertEqual(self.w.discover('bob')['items'], [])

    def test_independent_report_can_replace_quick_sample_and_resources_stay_pinned(self):
        d = self.a.save('alice', proposal={**self.p, 'references':{'references/extra.md':'旧资料'}, 'required_resources':{'use':[]}}, request_key='resource')
        self.w.evaluate('alice', draft_id=d['draft_id'], expected_revision=1,
                        cases=[{'prompt':'整理计划', 'contains':['计划']}], request_key='resource-eval')
        self.w.run_once(lambda *_:{'text':'计划修复'})
        r = self.a.submit('alice', draft_id=d['draft_id'], expected_revision=1, request_key='resource-submit', reason='个人使用')
        self.a.decide(actor={'role':'admin','username':'admin'},request_id=r['request_id'],decision='approve',reason='核对依据',reviewed_tests=True)
        binding = self.s.skills.bind('alice', d['skill_id'], 'use')
        self.assertEqual(self.w.resource('alice',binding_id=binding,path='references/extra.md')['content'],'旧资料')
        from bscli.core.business_skills import SkillRejected
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
        self.w.run_once(complete)
        self.assertEqual(len(self.a.list('alice')['items']),1)
        self.assertEqual(self.w.jobs('alice',j['job_id'])['state'],'canceled')

    def test_completed_workspace_capture_is_durable_and_opted_in(self):
        from unittest.mock import patch
        from tests.test_workspace import _create_account, FakeGateway
        from bscli.workspace.application import WorkspaceApplication
        from bscli.core.skill_workbench import SkillWorkbench
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


if __name__ == '__main__': unittest.main()
