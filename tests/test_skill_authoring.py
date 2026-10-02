import copy
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from bscli.core.central_service import CentralCapabilityService
from bscli.core.business_skills import skill_catalog, SkillRejected, SkillStore
from bscli.core.user_grants import UserGrantConflict


class SkillAuthoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=CentralCapabilityService(home=self.tmp.name,base_url='http://oa.test')
        self.a=self.s.skill_authoring
        self.proposal={'name':'周报方法','description':'归纳输入事项','selection':{'use_when':'整理周报','not_for':'提交业务表单','output':'完成、风险、下周计划'},'instructions':'仅使用用户输入。没有依据时注明未知。按完成、风险、计划归纳。'}
        self.admin={'username':'reviewer','role':'admin'}

    def draft(self):
        return self.a.save('alice',proposal=self.proposal,request_key=str(uuid4()))

    def sample(self,d):
        t=self.a.test_start('alice',draft_id=d['draft_id'],expected_revision=d['revision'],profile='use',prompt='合成例子：修复一个缺陷',request_key=str(uuid4()))
        self.a.test_result('alice',test_id=t['test_id'],output='完成：修复缺陷。风险与计划：未知。')
        return t

    def submit(self,d):
        self.sample(d)
        return self.a.submit('alice',draft_id=d['draft_id'],expected_revision=d['revision'],request_key=str(uuid4()),reason='个人使用')

    def approve(self,r,**kw):
        return self.a.decide(actor=self.admin,request_id=r['request_id'],decision='approve',reason='已复核',reviewed_tests=True,**kw)

    def test_no_grants_user_private_draft_and_publication(self):
        d=self.draft()
        self.assertIsNone(self.s.user_grants.get('alice'))
        self.assertEqual(self.a.list('bob')['items'],[])
        with self.assertRaises(KeyError):self.a.get('bob',d['draft_id'])
        with self.assertRaises(KeyError):self.a.export('bob',d['draft_id'])
        r=self.submit(d)
        self.assertEqual(skill_catalog(self.s,'alice')['items'],[])
        with self.assertRaises(PermissionError): self.a.decide(actor={'role':'viewer'},request_id=r['request_id'],decision='approve',reason='x')
        with self.assertRaises(ValueError): self.a.dispatch('alice','decide',{})
        self.approve(r)
        self.assertEqual(skill_catalog(self.s,'alice')['items'][0]['version'],'1.0.0')
        self.assertEqual(skill_catalog(self.s,'bob')['items'],[])
        self.assertIsNone(self.s.user_grants.get('alice'))
        self.s.skills.bind('alice',d['skill_id'],'use')

    def test_idempotency_and_revision(self):
        args={'proposal':self.proposal,'request_key':'one'}
        d=self.a.save('alice',**args)
        self.assertEqual(d,self.a.save('alice',**args))
        with self.assertRaises(UserGrantConflict):self.a.save('alice',proposal={**self.proposal,'name':'other'},request_key='one')
        with self.assertRaises(UserGrantConflict):self.a.save('alice',draft_id=d['draft_id'],expected_revision=7,proposal=self.proposal,request_key='two')
        with self.assertRaises(ValueError):self.a.test_start('alice',draft_id=d['draft_id'],expected_revision=None,profile='use',prompt='x',request_key='t')

    def test_snapshot_and_evidence_are_frozen(self):
        d=self.draft();r=self.submit(d)
        self.sample(d)
        updated=self.a.save('alice',draft_id=d['draft_id'],expected_revision=1,proposal={**self.proposal,'name':'changed'},request_key='edit')
        detail=self.a.review_detail(r['request_id'])
        self.assertEqual(detail['bundle']['manifest']['name'],'周报方法')
        self.assertEqual(len(detail['tests']),1)
        self.approve(r)
        self.assertEqual(self.s.skills.current(d['skill_id'])['manifest']['name'],'周报方法')
        with self.assertRaises(ValueError):self.a.submit('alice',draft_id=d['draft_id'],expected_revision=updated['revision'],request_key='submit2',reason='new')

    def test_atomic_audit_failure_and_human_review_required(self):
        d=self.draft();r=self.submit(d)
        with self.assertRaises(ValueError):self.a.decide(actor=self.admin,request_id=r['request_id'],decision='approve',reason='x')
        def fail(*args):raise RuntimeError('audit failure')
        with self.assertRaises(RuntimeError):self.approve(r,audit_callback=fail)
        self.assertEqual(self.a.review_detail(r['request_id'])['state'],'submitted')
        self.assertEqual(skill_catalog(self.s,'alice')['items'],[])
        self.assertEqual(self.s.skills.config('user:alice')['revision'],0)

    def test_withdraw_and_reject_do_not_publish(self):
        d=self.draft();r=self.submit(d)
        with self.assertRaises(KeyError):self.a.withdraw('bob',r['request_id'])
        self.a.withdraw('alice',r['request_id'])
        with self.assertRaises(UserGrantConflict):self.approve(r)
        r=self.submit(d)
        self.a.decide(actor=self.admin,request_id=r['request_id'],decision='changes_requested',reason='补充示例')
        self.assertEqual(skill_catalog(self.s,'alice')['items'],[])

    def test_update_preserves_old_binding_and_approved_audience(self):
        d=self.draft();self.approve(self.submit(d));binding=self.s.skills.bind('alice',d['skill_id'],'use')
        d2=self.a.save('alice',draft_id=d['draft_id'],expected_revision=1,proposal={**self.proposal,'instructions':'新版方法'},request_key='v2')
        self.approve(self.submit(d2))
        restarted=SkillStore(self.s.db_path)
        self.assertEqual(restarted.binding('alice',binding)['version'],'1.0.0')
        self.assertEqual(restarted.current(d['skill_id'])['manifest']['version'],'1.0.1')
        with self.assertRaises(SkillRejected):restarted.save('user:bob',{d['skill_id']:{'profiles':['use']}},expected_revision=0,actor='admin',reason='bypass')

    def test_concurrent_approval_publishes_once(self):
        d=self.draft();r=self.submit(d)
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.approve(r),range(2)))
        self.assertEqual([x['published_version'] for x in results],['1.0.0','1.0.0'])
        self.assertEqual(self.s.skills.config('user:alice')['revision'],1)

    def test_automatic_requires_opt_in_and_no_automatic_publication(self):
        with self.assertRaises(PermissionError):self.a.save('alice',proposal=self.proposal,request_key='auto',provenance={'kind':'automatic'})
        self.a.preferences('alice',value={'auto_draft':True},expected_revision=0)
        d=self.a.save('alice',proposal=self.proposal,request_key='auto',provenance={'kind':'automatic'})
        self.assertFalse(d['published']);self.assertEqual(self.a.reviews('alice')['items'],[])
        with self.assertRaises(UserGrantConflict):self.a.preferences('alice',value={'auto_draft':False},expected_revision=0)

    def test_import_revalidates_and_drops_identity_and_history(self):
        d=self.draft();export=self.a.export('alice',d['draft_id'])
        imported=self.a.save('bob',proposal=export['proposal'],request_key='import',provenance={'kind':'import'})
        self.assertNotEqual(d['skill_id'],imported['skill_id'])
        self.assertEqual(self.a.get('bob',imported['draft_id'])['tests'],[])
        for bad in [{'references':{'../../escape.md':'x'}},{'instructions':'Bearer abcdefghijklmnopqrstuvwxyz'},{'profiles':{'bad':{'all':['made.up']}}}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):self.a.save('alice',proposal={**self.proposal,**bad},request_key=str(uuid4()))

    def test_archive_pending_is_rejected(self):
        d=self.draft();r=self.submit(d)
        with self.assertRaises(UserGrantConflict):self.a.archive('alice',draft_id=d['draft_id'],expected_revision=1)
        self.a.withdraw('alice',r['request_id']);self.a.archive('alice',draft_id=d['draft_id'],expected_revision=1)
        with self.assertRaises(ValueError):self.a.save('alice',draft_id=d['draft_id'],expected_revision=1,proposal=self.proposal,request_key='archive-edit')

    def test_restore_requires_new_review_and_retains_publication(self):
        d=self.draft();self.approve(self.submit(d))
        self.a.archive('alice',draft_id=d['draft_id'],expected_revision=1)
        restored=self.a.restore('alice',draft_id=d['draft_id'],expected_revision=1,target_revision=1,request_key='restore')
        self.assertEqual(restored['revision'],2)
        self.assertEqual(self.s.skills.current(d['skill_id'])['manifest']['version'],'1.0.0')
        self.assertEqual(self.a.restore('alice',draft_id=d['draft_id'],expected_revision=1,target_revision=1,request_key='restore'),restored)
        with self.assertRaises(ValueError):self.a.submit('alice',draft_id=d['draft_id'],expected_revision=2,request_key='new',reason='restore')

    def test_auto_dedup_budget_and_disable(self):
        self.a.preferences('alice',value={'auto_draft':True},expected_revision=0)
        args={'proposal':self.proposal,'provenance':{'kind':'automatic'}}
        d=self.a.save('alice',request_key='auto1',**args)
        self.assertEqual(self.a.save('alice',request_key='auto2',**args),d)
        for i in range(2):self.a.save('alice',proposal={**self.proposal,'name':str(i)},provenance={'kind':'automatic'},request_key=str(i))
        with self.assertRaises(ValueError):self.a.save('alice',proposal={**self.proposal,'name':'fourth'},provenance={'kind':'automatic'},request_key='fourth')
        self.a.preferences('alice',value={'auto_draft':False},expected_revision=1)
        with self.assertRaises(PermissionError):self.a.save('alice',request_key='auto3',**args)

    def test_private_published_metadata_is_not_visible_to_other_users(self):
        d=self.draft();self.approve(self.submit(d))
        with self.assertRaises(SkillRejected):self.s.skills.available_item('bob',d['skill_id'],'use')
        self.s.skills.record_load('bob',d['skill_id'],'use','SKILL.md',{'status':'rejected'})
        self.assertEqual(self.s.skills.load_history('bob')[0]['name'],'未知业务助手')
