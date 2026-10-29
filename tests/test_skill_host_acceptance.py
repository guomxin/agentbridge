"""Offline safety and evidence checks for the optional real-model host harness."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.core.business_skills import skill_bundle, skill_catalog, validate_binding
from tests.support.skill_host_acceptance import (
    ALICE, BOB, CASES_PATH, ROOT, SkillHostAcceptance, host_config,
    compare_source_identity, isolated_environment, observe, source_identity,
)


class SkillHostAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def fixture(self):
        fixture = SkillHostAcceptance(self.home)
        self.addCleanup(fixture.close)
        return fixture

    def test_nonempty_runtime_is_never_adopted(self):
        (self.home / 'production.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'empty isolated'):
            SkillHostAcceptance(self.home)
        self.assertEqual(list(self.home.iterdir()), [self.home / 'production.json'])

    def test_identity_covers_candidate_plugin_and_new_fixture_sources(self):
        identity = source_identity()
        for name in ('integrations/openclaw-agentbridge/index.js', 'integrations/openclaw-agentbridge/lib/business-skills.js',
                     'tests/support/skill_host_acceptance.py', 'tests/support/skill_host_acceptance_auth.mjs',
                     'tests/support/skill_host_acceptance_cache.mjs', 'tests/fixtures/skill_host_cases.json'):
            self.assertEqual(identity['files_sha256'][name], hashlib.sha256((ROOT / name).read_bytes()).hexdigest())
        self.assertRegex(identity['git_head'], '^[0-9a-f]{40}$')
        changed = {**identity, 'files_sha256': {**identity['files_sha256'], 'tests/fixtures/skill_host_cases.json': 'changed'}}
        comparison = compare_source_identity(identity, changed)
        self.assertFalse(comparison['relevant_content_unchanged'])
        self.assertEqual(comparison['changed_files'], ['tests/fixtures/skill_host_cases.json'])

    def test_allowed_read_uses_real_grants_and_all_other_calls_fail_closed(self):
        fixture = self.fixture()
        result = fixture.service.invoke(user_subject=ALICE, capability_name='oa.workflow.pending.list', arguments={})
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['result']['synthetic'])
        self.assertEqual(result['result']['items'][0]['missing'], ['报价'])
        with self.assertRaises(PermissionError):
            fixture.service.invoke(user_subject=BOB, capability_name='oa.workflow.pending.list', arguments={})
        for capability in ('oa.leave.prepare', 'oa.leave.submit', 'unknown.read'):
            with self.subTest(capability=capability), self.assertRaisesRegex(PermissionError, 'SYNTHETIC_ONLY'):
                fixture.service.invoke(user_subject=ALICE, capability_name=capability, arguments={})
        with self.assertRaisesRegex(PermissionError, 'SYNTHETIC_ONLY'):
            fixture.service.resume_interaction(user_subject=ALICE, interaction_id='anything')
        self.assertEqual(len(fixture.calls), 1)

    def test_required_resource_is_pinned_and_missing_permission_is_visible(self):
        fixture = self.fixture()
        sid = fixture.skills['evidence']
        binding_id = fixture.service.skills.bind(ALICE, sid, 'use')
        binding = fixture.service.skills.binding(ALICE, binding_id)
        validate_binding(fixture.service, ALICE, binding)
        self.assertEqual(skill_bundle(binding)['loaded_resources'], ['SKILL.md', 'references/evidence.md'])
        bob = next(item for item in skill_catalog(fixture.service, BOB)['items'] if item['id'] == sid)
        self.assertFalse(bob['profiles']['use']['available'])
        self.assertFalse(fixture.service.skills.load_history(ALICE))

    def test_optional_reference_requires_explicit_read_and_authoring_writes_are_denied(self):
        fixture = self.fixture()
        sid = fixture.skills['weekly']
        binding_id = fixture.service.skills.bind(ALICE, sid, 'use')
        binding = fixture.service.skills.binding(ALICE, binding_id)
        bundle = skill_bundle(binding)
        self.assertEqual(bundle['loaded_resources'], ['SKILL.md'])
        self.assertEqual(bundle['optional_resources'], ['references/weekly-check.md'])
        result = fixture.service.skill_authoring.dispatch(ALICE, 'resource',
            {'binding_id': binding_id, 'path': 'references/weekly-check.md'})
        self.assertIn('原文核对', result['content'])
        self.assertEqual(len(fixture.resource_calls), 1)
        for action in ('save', 'submit', 'test', 'test_result', 'generate', 'evaluate', 'policy'):
            with self.subTest(action=action), self.assertRaisesRegex(PermissionError, 'SYNTHETIC_ONLY'):
                fixture.service.skill_authoring.dispatch(ALICE, action, {})
        with self.assertRaises(ValueError):
            fixture.service.skill_authoring.dispatch(BOB, 'resource',
                {'binding_id': binding_id, 'path': 'references/weekly-check.md'})

    def test_host_config_has_only_candidate_and_explicit_model_plugins(self):
        config = host_config(self.home, mcp_url='http://127.0.0.1:12345/mcp', gateway_port=22345,
                             gateway_token='synthetic-only', provider_plugin=Path('/synthetic/codex'), model='openai/test')
        self.assertEqual(config['plugins']['load']['paths'][-1], str(ROOT / 'integrations/openclaw-agentbridge'))
        self.assertNotIn('channels', config)
        self.assertNotIn('mcp', config)
        self.assertEqual(config['agents']['list'][0]['skills'], [])
        self.assertEqual(config['agents']['defaults']['model']['fallbacks'], [])
        self.assertNotIn('exec', config['tools']['allow'])
        self.assertFalse(any('prepare' in tool or 'database' in tool for tool in config['tools']['allow']))
        self.assertFalse(config['browser']['enabled'])

    def test_environment_does_not_inherit_business_or_model_secrets(self):
        with patch.dict(os.environ, {'PRODUCTION_TOKEN': 'secret', 'OPENAI_API_KEY': 'secret',
                                     'CODEX_HOME': '/production/codex', 'OPENCLAW_CONFIG_PATH': '/production/config'}):
            env = isolated_environment(self.home, {ALICE: 'synthetic-a', BOB: 'synthetic-b'})
        self.assertNotIn('PRODUCTION_TOKEN', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('CODEX_HOME', env)
        self.assertEqual(env['OPENCLAW_CONFIG_PATH'], str(self.home / 'openclaw.json'))
        self.assertEqual(env['SKILL_HOST_BOB_TOKEN'], 'synthetic-b')

    def test_observations_do_not_confuse_strings_with_model_or_semantic_proof(self):
        case = {'skill': 'evidence', 'read': True, 'contains': ['报价']}
        result = observe(case, {'payloads': [{'text': '缺报价'}]}, [], [], {'evidence': 'evidence-id'}, 0)
        self.assertTrue(result['checks']['contains'])
        self.assertFalse(result['checks']['skill_selection'])
        self.assertFalse(result['checks']['required_resources'])
        self.assertFalse(result['checks']['synthetic_read'])
        self.assertIsNone(result['semantic_pass'])
        result = observe({'skill': None}, None, [], [], {}, 'timeout')
        self.assertFalse(result['checks']['agent_finished'])

    def test_cases_include_twenty_natural_prompts_and_independent_boundaries(self):
        cases = json.loads(CASES_PATH.read_text())
        self.assertEqual(len(cases), 20)
        self.assertEqual(len({case['id'] for case in cases}), 20)
        self.assertTrue(any(case.get('owner') == BOB and case.get('no_read') for case in cases))
        self.assertTrue(any(case.get('follow_up') for case in cases))
        self.assertEqual({case.get('skill') for case in cases}, {'weekly', 'plan', 'evidence', None})

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the official-auth helper contract')
    def test_auth_helper_copies_access_only_and_rejects_stale_or_other_profiles(self):
        # Pure transform only: this imports no OpenClaw runtime and reads no credentials.
        script = '''
          import assert from 'node:assert/strict';
          import {shortLivedCredential} from './tests/support/skill_host_acceptance_auth.mjs';
          import {publicRuntimeMetadata} from './tests/support/skill_host_acceptance_cache.mjs';
          const now = 100000;
          const source = {type:'oauth', provider:'openai', access:'a'.repeat(40), refresh:'NEVER_COPY',
                          expires:now+300000, email:'private@example.invalid'};
          const safe = shortLivedCredential({apiKey:source.access}, source, now);
          assert.deepEqual(Object.keys(safe).sort(), ['copyToAgents','expires','provider','token','type']);
          assert.equal(safe.type, 'token');
          assert.equal(safe.copyToAgents, false);
          assert.throws(()=>shortLivedCredential({apiKey:source.access}, {...source,expires:now}, now));
          assert.throws(()=>shortLivedCredential({apiKey:source.access}, {...source,provider:'other'}, now));
          const url='https://registry.npmjs.org/@openai/codex/-/codex-0.144.3-darwin-arm64.tgz';
          const metadata=publicRuntimeMetadata({url,time:now,reqHeaders:{Authorization:'NEVER_COPY'},
            resHeaders:{'content-type':'application/octet-stream','set-cookie':'NEVER_COPY'},options:{token:'NEVER_COPY'}},url);
          assert.equal(JSON.stringify(metadata).includes('NEVER_COPY'),false);
          assert.deepEqual(metadata.reqHeaders,{});
          assert.throws(()=>publicRuntimeMetadata({url:'https://private.invalid'},url));
        '''
        done = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the candidate-plugin MCP contract')
    def test_candidate_identity_router_binds_only_its_synthetic_workspace_owner(self):
        fixture = self.fixture()
        endpoint = fixture.start_mcp()
        config = host_config(self.home, mcp_url=endpoint, gateway_port=22345,
            gateway_token='synthetic-only', provider_plugin=Path('/synthetic/codex'), model='openai/test')
        script = '''
          import {AgentBridgeIdentityRouter} from './integrations/openclaw-agentbridge/lib/identity-router.js';
          import {resolvePluginConfig} from './integrations/openclaw-agentbridge/lib/config.js';
          let raw=''; for await (const chunk of process.stdin) raw += chunk;
          const input=JSON.parse(raw);
          const router=new AgentBridgeIdentityRouter({config:resolvePluginConfig(input.config), hostConfig:{}, env:input.env});
          await router.refreshIdentityProfiles();
          const result=await router.resolveWorkspaceSession(input.sessionKey);
          const catalog=result.bound ? await result.client.callTool('agentbridge_skill_catalog', {}) : null;
          const foreign=await router.resolveWorkspaceSession('agent:main:agentbridge-workspace:direct:unowned');
          const direct=router.resolveToolContext({sessionKey:'agent:main:telegram:direct:skill-host-alice'});
          console.log(JSON.stringify({bound:result.bound, catalogItems:catalog?.items?.length,
                                     foreignBound:foreign.bound, sender:result.binding?.senderId,
                                     directBound:direct.bound, directSender:direct.binding?.senderId}));
        '''
        done = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT, capture_output=True,
            text=True, timeout=30, input=json.dumps({'config': config['plugins']['entries']['agentbridge-interactions']['config'],
                'env': {'SKILL_HOST_ALICE_TOKEN': fixture.tokens[ALICE], 'SKILL_HOST_BOB_TOKEN': fixture.tokens[BOB]},
                'sessionKey': fixture.accounts[ALICE]['openclaw_session_key']}))
        self.assertEqual(done.returncode, 0, 'Candidate identity router process failed')
        result = json.loads(done.stdout)
        self.assertEqual(result, {'bound': True, 'catalogItems': 3, 'foreignBound': False, 'sender': ALICE,
                                 'directBound': True, 'directSender': ALICE})


if __name__ == '__main__':
    unittest.main()
