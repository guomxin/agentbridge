from tempfile import TemporaryDirectory
import json
import unittest

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.skill_catalog import authoring_catalog, generation_catalog, public_capability_catalog


class SkillAuthoringCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.service = CentralCapabilityService(home=self.tmp.name, base_url='http://oa.test')

    def test_public_contracts_omit_commits_and_preserve_actual_schemas(self):
        from agentbridge.core.write_catalog import _TRUSTED_WRITE_COMMITS
        catalog = public_capability_catalog(self.service)
        names = {item['name'] for item in catalog}
        self.assertFalse(names & set(_TRUSTED_WRITE_COMMITS))
        self.assertIn('oa.workflow.pending.list', names)
        self.assertIn('database.logs.query', names)
        for item in catalog:
            self.assertIs(type(item['batch']), bool)
            self.assertIn('instruction', item['preconditions'])
            if item['name'].startswith('database.'):
                self.assertTrue(item['preconditions']['sourceRequired'])
                self.assertIsNone(item['outputSchema'])  # No invented database schema.
            else:
                spec = self.service.registry.get(item['name'])
                self.assertEqual(item['inputSchema'], spec.input_schema)
                self.assertEqual(item['outputSchema'], spec.output_schema)
                self.assertEqual(item['effect'], spec.effect)

    def test_editor_catalog_is_static_design_information_without_grants_or_large_schemas(self):
        value = authoring_catalog(self.service)
        self.assertEqual(value['schema'], 'agentbridge.skill-authoring-catalog.v1')
        self.assertTrue(value['business']); self.assertTrue(value['database'])
        self.assertTrue(all(set(item) == {'name', 'description', 'effect', 'batch'} for item in value['capabilities']))
        self.assertNotIn('inputSchema', json.dumps(value))
        self.assertIn('不代表当前账号', value['message'])

    def test_short_chinese_request_selects_relevant_contracts_and_plain_text_needs_none(self):
        value = generation_catalog(self.service, '做个查待办的助手')
        names = [item['name'] for item in value['contracts']]
        self.assertIn('oa.workflow.pending.list', names)
        self.assertLessEqual(len(names), 6)
        self.assertLessEqual(sum(len(json.dumps(item, ensure_ascii=False, separators=(',', ':'))) for item in value['contracts']), 20000)
        self.assertEqual(generation_catalog(self.service, '只把逗号替换为句号')['contracts'], [])

    def test_existing_selected_dependencies_take_priority_without_loading_every_contract(self):
        value = generation_catalog(self.service, '保留现有方法，只完善异常说明',
            {'profiles': {'custom': {'all': ['taihua.work_log.my.read']}}})
        self.assertIn('taihua.work_log.my.list', [item['name'] for item in value['contracts']])
        self.assertLess(len(value['contracts']), len(public_capability_catalog(self.service)))
        self.assertIn('省略的合同不表示不支持', value['limitations'])

    def test_generation_uses_contract_context_but_evaluation_does_not(self):
        authoring = self.service.skill_authoring
        proposal = {'name': '文字整理', 'description': '按原文整理计划',
                    'selection': {'use_when': '整理计划', 'not_for': '业务写入', 'output': '计划'},
                    'instructions': '只整理给定文字，不编造。'}
        job = authoring.workbench.generate('alice', material='做个查待办的助手', request_key='generate')
        captured = []
        def complete(system, prompt):
            captured.append(system)
            return {'text': json.dumps({'kind': 'method', 'summary': '合成方法', 'proposal': proposal}, ensure_ascii=False), 'model': 'synthetic'}
        authoring.workbench.run_once(complete)
        self.assertIn('agentbridge.skill-generation-catalog.v1', captured[0])
        self.assertIn('oa.workflow.pending.list', captured[0])
        self.assertIn('inputSchema', captured[0]); self.assertIn('outputSchema', captured[0])
        self.assertLess(len(captured[0]), 56000)
        saved = authoring.workbench.jobs('alice', job['job_id'])
        self.assertEqual(saved['state'], 'succeeded')
        evaluated = authoring.workbench.evaluate('alice', draft_id=saved['result']['draft_id'], expected_revision=1,
            cases=[{'prompt':'明天修复', 'contains':['计划']}], request_key='evaluate')
        systems = []
        authoring.workbench.run_once(lambda system, _: systems.append(system) or {'text':'计划：明天修复','model':'synthetic'})
        self.assertTrue(systems)
        self.assertTrue(all('skill-generation-catalog' not in system and 'oa.workflow.pending.list' not in system for system in systems))
        self.assertEqual(authoring.workbench.jobs('alice', evaluated['job_id'])['state'], 'succeeded')

    def test_long_existing_method_is_not_silently_truncated_for_generation(self):
        authoring = self.service.skill_authoring
        proposal = {'name':'长方法', 'description':'完整保留说明',
                    'selection':{'use_when':'整理文字','not_for':'业务写入','output':'摘要'},
                    'instructions':'逐项核对原文。' * 4000}
        draft = authoring.save('alice', proposal=proposal, request_key='long-draft')
        job = authoring.workbench.generate('alice', material='完善异常说明', draft_id=draft['draft_id'],
            expected_revision=1, request_key='long-generation')
        authoring.workbench.run_once(lambda *_: self.fail('oversized source must be rejected before model invocation'))
        result = authoring.workbench.jobs('alice', job['job_id'])
        self.assertEqual(result['state'], 'failed')
        self.assertIn('未截断已有方法', result['error'])
        self.assertEqual(authoring.get('alice',draft['draft_id'])['revision'],1)
