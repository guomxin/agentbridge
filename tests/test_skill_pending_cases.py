"""One private unfinished test sheet, with derived feedback progress."""
from copy import deepcopy
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.user_grants import UserGrantConflict


class SkillPendingCasesTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.service=CentralCapabilityService(home=self.temp.name,base_url='http://oa.test')
        self.a=self.service.skill_authoring;self.w=self.a.workbench
        self.proposal={'name':'周报方法','description':'只整理原文',
            'selection':{'use_when':'整理周报','not_for':'查询业务','output':'计划'},'instructions':'保留原文计划。'}
        self.d=self.a.save('alice',proposal=self.proposal,request_key='draft')
        self.value={'cases':[{'prompt':' 明天补测试。\n','profile':'use','kind':'output',
            'contains':'明天\n','excludes':'还没写完'}],'repeats':'2'}
        sample=self.a.test_start('alice',draft_id=self.d['draft_id'],expected_revision=1,profile='use',prompt='明天补测试。',request_key='sample')
        self.a.test_result('alice',test_id=sample['test_id'],output='计划：明天补测试。')
        self.feedback=self.w.feedback('alice',skill_id=self.d['skill_id'],profile='use',test_id=sample['test_id'],rating='incorrect',comment='不要补效果。')

    def save(self,*,owner='alice',revision=1,version=0,value=None,draft_id=None):
        return self.w.save_pending_cases(owner,draft_id=draft_id or self.d['draft_id'],expected_revision=revision,
            expected_version=version,value=self.value if value is None else value)

    def inspect(self):
        return self.w.inspect('alice',self.d['draft_id'])

    def progress(self):
        return self.inspect()['feedback']['items'][0]['progress']

    def evaluate(self,key,*,complete=True,output='计划：明天补测试。'):
        job=self.w.evaluate('alice',draft_id=self.d['draft_id'],expected_revision=1,cases=[
            {'prompt':'明天补测试。','contains':['明天'],'feedback_id':self.feedback['feedback_id']},
            {'prompt':'明天继续查。','contains':['明天'],'feedback_id':self.feedback['feedback_id']}],request_key=key)
        if complete:self.w.run_once(lambda *_:{'text':output,'model':'synthetic'})
        return job

    def test_partial_raw_values_survive_refresh_and_method_revision(self):
        value=deepcopy(self.value);value['cases'][0].update(prompt='',profile='',contains='\n',excludes='')
        self.assertIsNone(self.inspect()['pending_cases'])
        saved=self.save(value=value)
        self.assertEqual(saved['value'],value)
        self.assertEqual(saved['source_revision'],1)
        self.assertEqual(self.save(value=value),saved)
        self.a.save('alice',draft_id=self.d['draft_id'],expected_revision=1,proposal={**self.proposal,'name':'新名称'},request_key='next')
        self.assertEqual(self.inspect()['pending_cases'],saved)
        with self.assertRaises(UserGrantConflict):self.save(value=value)
        updated=self.save(revision=2,version=1,value=value)
        self.assertEqual(updated['source_revision'],2);self.assertEqual(updated['version'],2)
        self.assertEqual(self.save(revision=2,version=2,value={'cases':[],'repeats':'1'})['version'],3)

    def test_two_tabs_and_users_cannot_overwrite_or_read_another_sheet(self):
        self.save()
        changed=deepcopy(self.value);changed['cases'][0]['prompt']='另一题'
        with self.assertRaises(UserGrantConflict):self.save(value=changed)
        with self.assertRaises(KeyError):self.save(owner='bob')
        with self.assertRaises(KeyError):self.w.inspect('bob',self.d['draft_id'])
        self.assertEqual(self.save(version=1,value=changed)['version'],2)
        self.assertEqual(self.inspect()['pending_cases']['value'],changed)

    def test_removed_profile_requires_reselection_but_saved_text_is_not_lost(self):
        old=self.save()
        self.a.save('alice',draft_id=self.d['draft_id'],expected_revision=1,
            proposal={**self.proposal,'profiles':{'preview':{}}},request_key='profile-change')
        self.assertEqual(self.inspect()['pending_cases'],old)
        with self.assertRaises(ValueError):self.save(revision=2,version=1)
        changed=deepcopy(self.value);changed['cases'][0]['profile']=''
        self.assertEqual(self.save(revision=2,version=1,value=changed)['value'],changed)

    def test_incomplete_feedback_case_is_saved_and_other_skill_or_owner_is_rejected(self):
        value=deepcopy(self.value);value['cases'][0].update(prompt='',contains='',excludes='',feedback_id=self.feedback['feedback_id'])
        saved=self.save(value=value)
        self.assertTrue(self.progress()['pending_saved']);self.assertIsNone(self.progress()['latest_job'])
        other=self.a.save('alice',proposal={**self.proposal,'name':'另一助手'},request_key='other')
        with self.assertRaises(KeyError):self.save(value=value,draft_id=other['draft_id'])
        self.assertEqual(self.inspect()['pending_cases'],saved)
        self.save(version=1,value={'cases':[],'repeats':'1'})
        self.assertFalse(self.progress()['pending_saved'])

    def test_latest_attempt_replaces_old_success_and_new_job_has_no_old_review(self):
        value=deepcopy(self.value);value['cases'][0]['feedback_id']=self.feedback['feedback_id']
        saved=self.save(value=value)
        first=self.evaluate('first')
        self.w.review_report('alice',job_id=first['job_id'],row_key='0:0:candidate',status='problem',note='补效果',expected_version=0)
        progress=self.progress()['latest_job']
        self.assertEqual(progress['case_indexes'],[0,1]);self.assertEqual(progress['candidate'],{'passed':2,'total':2})
        self.assertEqual(progress['review'],{'acceptable':0,'problem':1,'unreviewed':1})
        self.assertEqual(self.inspect()['pending_cases'],saved)  # Running never deletes the saved sheet.
        second=self.evaluate('second',complete=False)
        progress=self.progress()['latest_job']
        self.assertEqual(progress['job_id'],second['job_id']);self.assertEqual(progress['state'],'queued')
        self.assertEqual(progress['candidate'],{'passed':0,'total':2});self.assertEqual(progress['review']['problem'],0)
        self.w.run_once(lambda *_: (_ for _ in ()).throw(ValueError('synthetic failure')))
        self.assertEqual(self.progress()['latest_job']['state'],'failed')
        third=self.evaluate('third',complete=False);self.w.cancel('alice',third['job_id'])
        self.assertEqual(self.progress()['latest_job']['state'],'canceled')
        fourth=self.evaluate('fourth')
        self.assertEqual(self.progress()['latest_job']['review'],{'acceptable':0,'problem':0,'unreviewed':2})
        self.assertEqual(self.progress()['latest_job']['job_id'],fourth['job_id'])

    def test_pending_limits_and_invalid_fields_do_not_create_or_change_sheet(self):
        for changed in ({'repeats':2},{'repeats':'4'},{'extra':True},{'cases':[{}]},
                        {'cases':[self.value['cases'][0]]*13}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):self.save(value={**self.value,**changed})
        for field,bad in [('profile','missing'),('prompt','x'*3001),('kind',[]),('contains',[]),('feedback_id','forged')]:
            value=deepcopy(self.value);value['cases'][0][field]=bad
            with self.subTest(field=field),self.assertRaises(ValueError):self.save(value=value)
        with self.assertRaises(ValueError):self.save(version=True)
        self.assertIsNone(self.inspect()['pending_cases'])
