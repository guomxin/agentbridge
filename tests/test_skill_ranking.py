import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agentbridge.core.business_skills import SkillRejected, validate_binding
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.skill_ranking import rank_skill_candidates
from agentbridge.core.skill_workbench import SkillWorkbench


CASES = json.loads((Path(__file__).parent / 'fixtures/skill_ranking.json').read_text())['cases']


class SkillRankingTests(unittest.TestCase):
    def test_shared_host_and_central_acceptance_cases(self):
        workbench = SkillWorkbench.__new__(SkillWorkbench)
        workbench.authoring = SimpleNamespace(service=object())
        for case in CASES:
            with self.subTest(case=case['name']):
                items = copy.deepcopy(case['items'])
                limit = case.get('limit', 100)
                ranked = rank_skill_candidates(items, case['query'])
                self.assertEqual([i['id'] for i in ranked[:limit]], case['expected'])
                self.assertEqual(items, case['items'])
                # Ranking must preserve availability, status, and exclusion metadata.
                self.assertTrue(all(any(item is original for original in items) for item in ranked))
                with patch('agentbridge.core.skill_workbench.skill_catalog', return_value={'items': items}):
                    result = workbench.discover('alice', case['query'], limit)
                self.assertEqual([i['id'] for i in result['items']], case['expected'])
                self.assertEqual(result['has_more'], len(items) > limit)
                self.assertEqual(result['total'], len(items))

    def test_explicit_discovery_keeps_assignment_and_execution_checks(self):
        with TemporaryDirectory() as folder:
            service = CentralCapabilityService(home=folder, base_url='http://oa.test')
            workbench, store = service.skill_authoring.workbench, service.skills
            store.save('user:alice', {'oa-work-log': {'profiles': ['preview']}},
                       expected_revision=0, actor='admin', reason='fixture')
            self.assertEqual(workbench.discover('bob', 'oa-work-log')['items'], [])
            items = workbench.discover('alice', 'oa-work-log')['items']
            self.assertEqual([i['id'] for i in items], ['oa-work-log'])
            self.assertFalse(items[0]['profiles']['preview']['available'])
            binding = store.binding('alice', store.bind('alice', 'oa-work-log', 'preview'))
            with self.assertRaises(SkillRejected):
                validate_binding(service, 'alice', binding)
            store.save('global', {'oa-work-log': 'disabled'},
                       expected_revision=0, actor='admin', reason='fixture')
            self.assertEqual(workbench.discover('alice', 'oa-work-log')['items'][0]['status'], 'disabled')
            with self.assertRaises(SkillRejected):
                store.bind('alice', 'oa-work-log', 'preview')
