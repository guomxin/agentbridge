"""Isolated Skill report review fixture; all completions are synthetic.

Run with ``python -m tests.support.skill_review_acceptance --home <empty-dir>``.
Restarting the same fixture directory keeps edits and review receipts intact.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import tempfile
import threading

from tests.support.interaction_acceptance import OWNER, write_metadata
from tests.support.skill_practical_acceptance import PROPOSAL, SkillPracticalAcceptanceFixture
from tests.test_workspace import PASSWORD


CASES = [
    {'prompt': '整理成周报：完成联调，下周补测试。', 'profile': 'use',
     'contains': ['联调', '下周'], 'excludes': ['提升稳定性']},
    {'prompt': '整理成周报：登录问题还没定位，明天继续查。', 'profile': 'use',
     'contains': ['明天'], 'excludes': ['已解决']},
]
REGRESSION_MARKER = '合成验收故障：第二题故意输出已解决。'
MODEL_MARKER = '合成验收：使用另一个模型名称。'


class SkillReviewAcceptanceFixture(SkillPracticalAcceptanceFixture):
    def __init__(self, home):
        super().__init__(home)
        if not self.meta.get('review_seeded'):
            main = self._review_draft('复核验收助手', 'review-main', changed_model=False)
            changed = self._review_draft('模型变化验收助手', 'review-model', changed_model=True)
            self.meta.update(review_seeded=True, review=main, changed_model=changed)
            write_metadata(self.meta_path, self.meta)
        self.app._skill_completion = self.complete

    @staticmethod
    def complete(system, prompt):
        if '仅输出 true 或 false' in system:
            return {'text': 'true', 'model': 'synthetic-completion'}
        model = 'synthetic-completion-v2' if MODEL_MARKER in system else 'synthetic-completion'
        if '登录' in prompt:
            output = ('风险：登录问题已解决。计划：明天继续查。'
                      if REGRESSION_MARKER in system else '风险：登录问题还没定位。计划：明天继续查。')
        else:
            output = '完成：完成联调。计划：下周补测试。'
        return {'text': output, 'model': model}

    def _review_draft(self, name, key, *, changed_model):
        a = self.service.skill_authoring
        proposal = {**PROPOSAL, 'name': name}
        draft = a.save(OWNER, proposal=proposal, request_key=key + '-draft')
        draft_id = draft['draft_id']
        sample = a.test_start(OWNER, draft_id=draft_id, expected_revision=1, profile='use',
            prompt=CASES[1]['prompt'], request_key=key + '-sample')
        a.test_result(OWNER, test_id=sample['test_id'], output=self.complete('', CASES[1]['prompt'])['text'])
        feedback = a.workbench.feedback(OWNER, skill_id=draft['skill_id'], profile='use',
            test_id=sample['test_id'], rating='incorrect', comment='不能把还没定位改成已解决。')
        request = a.submit(OWNER, draft_id=draft_id, expected_revision=1,
            request_key=key + '-publish', reason='仅用于隔离合成验收')
        a.decide(actor={'role': 'admin', 'username': 'synthetic-reviewer'},
            request_id=request['request_id'], decision='approve', reason='合成样例验收',
            reviewed_tests=True, manual_quality_reason='仅合成材料，不证明真实模型质量。')
        cases = [dict(CASES[0]), {**CASES[1], 'feedback_id': feedback['feedback_id']}]
        fixed = a.workbench.evaluate(OWNER, draft_id=draft_id, expected_revision=1,
            cases=cases, repeats=2, request_key=key + '-fixed')
        a.workbench.run_once(self.complete)
        a.workbench.set_regression(OWNER, draft_id=draft_id, expected_revision=1, job_id=fixed['job_id'])
        instructions = proposal['instructions'] + REGRESSION_MARKER
        if changed_model:
            instructions += '\n' + MODEL_MARKER
        a.save(OWNER, draft_id=draft_id, expected_revision=1,
            proposal={**proposal, 'instructions': instructions}, request_key=key + '-revision-2')
        current = a.workbench.evaluate(OWNER, draft_id=draft_id, expected_revision=2,
            cases=cases, repeats=2, request_key=key + '-current')
        a.workbench.run_once(self.complete)
        return {'draft_id': draft_id, 'skill_id': draft['skill_id'], 'fixed_job_id': fixed['job_id'],
                'current_job_id': current['job_id'], 'feedback_id': feedback['feedback_id']}

    def public_metadata(self):
        return {'fixture': 'synthetic-skill-review-v1', 'workspace_url': self.workspace_url,
                'username': 'cardreview', 'other_username': 'cardother', 'synthetic_password': PASSWORD,
                'review': self.meta['review'], 'changed_model': self.meta['changed_model'],
                'evidence_boundary': 'Real UI/stores; synthetic model outputs; no downstream business access'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path)
    args = parser.parse_args()
    home = args.home or Path(tempfile.mkdtemp(prefix='agentbridge-skill-review-'))
    fixture = SkillReviewAcceptanceFixture(home)
    servers = fixture.servers()
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(json.dumps({**fixture.public_metadata(), 'home': str(fixture.home)}, ensure_ascii=False, indent=2), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: fixture.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: fixture.stop_event.set())
    try:
        fixture.stop_event.wait()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        fixture.close()


if __name__ == '__main__':
    main()
