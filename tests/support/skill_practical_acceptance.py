"""Real Workspace/skill stores with deterministic text completions, no business access."""
from tests.support.interaction_acceptance import AcceptanceFixture, OWNER, write_metadata
from tests.test_workspace import _create_account

PROPOSAL = {
    'name': '周报整理助手', 'description': '只整理用户给出的完成事项、风险和计划。',
    'selection': {'use_when': '整理周报', 'not_for': '查询或提交业务', 'output': '完成、风险、计划'},
    'instructions': '只依据原文，不补阶段、效果或目的。计划和未完成状态必须保留。\n' + '核对原文与输出，不添加事实。\n' * 80,
    'profiles': {'use': {}},
}

class SkillPracticalAcceptanceFixture(AcceptanceFixture):
    def _seed(self):
        self.account = _create_account(self.service, user_subject=OWNER, username='cardreview', endpoint_key='telegram:*:cardreview')
        _create_account(self.service, user_subject='interaction-other', username='cardother', endpoint_key='telegram:*:cardother')
        self.meta.update(account_id=self.account['account_id'], flows=[])
        a = self.service.skill_authoring
        d = a.save(OWNER, proposal=PROPOSAL, request_key='weekly')
        sample = a.test_start(OWNER, draft_id=d['draft_id'], expected_revision=1, profile='use',
            prompt='整理成周报：完成联调，下周补测试。', request_key='sample')
        a.test_result(OWNER, test_id=sample['test_id'], output='完成：完成联调。计划：下周补测试。')
        req = a.submit(OWNER, draft_id=d['draft_id'], expected_revision=1, request_key='publish', reason='隔离合成验收')
        a.decide(actor={'role':'admin','username':'synthetic-reviewer'}, request_id=req['request_id'], decision='approve',
            reason='合成文字验收', reviewed_tests=True, manual_quality_reason='仅合成材料，不证明真实模型质量。')
        job = a.workbench.evaluate(OWNER, draft_id=d['draft_id'], expected_revision=1,
            cases=[{'prompt':'整理成周报：完成联调，下周补测试。','contains':['联调','下周'],'excludes':['稳定性']}], request_key='old-report')
        a.workbench.run_once(self.complete)
        a.save(OWNER, draft_id=d['draft_id'], expected_revision=1, proposal={**PROPOSAL,'instructions':PROPOSAL['instructions']+'保持不确定性。'}, request_key='revision-2')
        a.save(OWNER, proposal={**PROPOSAL,'name':'空白评测助手'}, request_key='empty')
        for i in range(160):
            self.service.tasks.append_timeline_message(user_subject=OWNER, source_endpoint_id=self.account['endpoint_id'],
                message_key=f'skill-history-{i}', role='assistant', text=f'合成历史 {i}：' + '仅供滚动和长历史验收。'*12)
        self.meta.update(draft_id=d['draft_id'],skill_id=d['skill_id'],old_job_id=job['job_id'])
        write_metadata(self.meta_path,self.meta)

    @staticmethod
    def complete(system, prompt):
        if '仅输出 true 或 false' in system:
            return {'text':'true','model':'synthetic-completion'}
        return {'text': '完成：完成联调。计划：下周补测试。' if '联调' in prompt else '风险：登录问题还没定位。计划：明天继续查。',
                'model':'synthetic-completion'}

    def __init__(self, home):
        super().__init__(home)
        self.app._skill_completion = self.complete
        if not self.meta.get('load_feedback_seeded'):
            binding = self.service.skills.bind(OWNER, self.meta['skill_id'], 'use')
            self.service.skills.record_load(OWNER,self.meta['skill_id'],'use','SKILL.md',
                {'status':'succeeded','version':'1.0.0','binding_id':binding})
            self.meta['load_feedback_seeded'] = True
            write_metadata(self.meta_path,self.meta)
