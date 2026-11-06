"""Offline safety and evidence checks for the optional real-model host harness."""
import json
import hashlib
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.core.business_skills import skill_bundle, skill_catalog, validate_binding
from tests.support.skill_host_acceptance import (
    ALICE, BOB, AUTHORING_CASES, CASES_PATH, ROOT, SkillHostAcceptance, host_config,
    compare_source_identity, isolated_environment, main as harness_main, observe, source_identity,
)
from tests.support.skill_host_acceptance_candidate import load_candidate


class SkillHostAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def fixture(self, **kwargs):
        fixture = SkillHostAcceptance(self.home, **kwargs)
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
                     'integrations/openclaw-agentbridge/lib/agentbridge-tools.json',
                     'tests/support/skill_host_acceptance.py', 'tests/support/skill_host_acceptance_auth.mjs',
                     'tests/support/skill_host_acceptance_cache.mjs', 'tests/fixtures/skill_host_cases.json'):
            self.assertEqual(identity['files_sha256'][name], hashlib.sha256((ROOT / name).read_bytes()).hexdigest())
        self.assertRegex(identity['git_head'], '^[0-9a-f]{40}$')
        changed = {**identity, 'files_sha256': {**identity['files_sha256'], 'tests/fixtures/skill_host_cases.json': 'changed'}}
        comparison = compare_source_identity(identity, changed)
        self.assertFalse(comparison['relevant_content_unchanged'])
        self.assertEqual(comparison['changed_files'], ['tests/fixtures/skill_host_cases.json'])
        catalog = 'integrations/openclaw-agentbridge/lib/agentbridge-tools.json'
        changed = {**identity, 'files_sha256': {**identity['files_sha256'], catalog: 'changed'}}
        self.assertEqual(compare_source_identity(identity, changed)['changed_files'], [catalog])

    def test_authoring_suite_restricts_identity_draft_and_business_effects(self):
        fixture = self.fixture(authoring=True)
        authoring = fixture.service.skill_authoring
        seed = fixture.authoring_seed
        self.assertFalse(fixture.skills)
        self.assertFalse(authoring.reviews(ALICE)['items'])
        self.assertEqual(seed['report_origin'], 'synthetic/no-model')
        self.assertIn('synthetic text report', fixture.report['evidence_boundary']['evaluation'])
        for action in ('submit', 'approve', 'report_review', 'test_result', 'generate', 'evaluate', 'import_standard'):
            with self.subTest(action=action), self.assertRaisesRegex(PermissionError, 'SYNTHETIC_ONLY'):
                authoring.dispatch(ALICE, action, {'draft_id': seed['draft_id']})
        for owner, draft_id in ((BOB, seed['draft_id']), (ALICE, 'other-draft')):
            with self.assertRaises(PermissionError):
                authoring.dispatch(owner, 'get', {'draft_id': draft_id})
        proposal = authoring.export(ALICE, draft_id=seed['draft_id'])['proposal']
        for changes in ({'all': ['oa.workflow.read']}, {'database': {'all': ['database.query']}},
                        {'execution': {'effects': ['controlled_write'], 'allow_batch': False}},
                        {'execution': {'effects': ['read'], 'allow_batch': True}},
                        {'execution': {'effects': ['read'], 'allow_batch': False, 'capabilities': ['oa.workflow.pending.list']}}):
            with self.subTest(changes=changes), self.assertRaises(PermissionError):
                authoring.dispatch(ALICE, 'save', {'draft_id': seed['draft_id'], 'expected_revision': 1,
                    'request_key': 'unsafe', 'proposal': {**proposal, 'profiles': {'use': changes}}})
        with self.assertRaises(PermissionError):
            fixture.service.invoke(user_subject=ALICE, capability_name='oa.leave.prepare', arguments={})
        self.assertEqual(authoring.get(ALICE, seed['draft_id'])['revision'], 1)

    def test_authoring_control_plane_evidence_requires_real_calls_and_persistence(self):
        fixture = self.fixture(authoring=True)
        authoring, seed = fixture.service.skill_authoring, fixture.authoring_seed
        dispatch = lambda action, **data: authoring.dispatch(ALICE, action, data)
        proposal = deepcopy(authoring.export(ALICE, draft_id=seed['draft_id'])['proposal'])
        proposal.update(standard_metadata={'license': 'MIT', 'compatibility': '仅处理文字'},
            profiles={'use': {'description': '整理文字', 'execution': {'effects': ['read'], 'allow_batch': False, 'capabilities': []}}})
        dispatch('save', draft_id=seed['draft_id'], expected_revision=1, request_key='metadata', proposal=proposal)
        dispatch('export_standard', draft_id=seed['draft_id'])
        self.assertTrue(all(fixture.authoring_observation('metadata', fixture.authoring_calls)['checks'].values()))
        self.assertFalse(fixture.authoring_observation('metadata', [])['checks']['metadata_saved'])
        pending = {'cases': [{'prompt': '整理：完成联调。', 'profile': 'use', 'kind': 'output', 'contains': '联调', 'excludes': ''}], 'repeats': '1'}
        dispatch('save_pending_cases', draft_id=seed['draft_id'], expected_revision=2, expected_version=0, value=pending)
        dispatch('pending_cases', draft_id=seed['draft_id'])
        for kind in ('pending_save', 'pending_read'):
            self.assertTrue(all(fixture.authoring_observation(kind, fixture.authoring_calls)['checks'].values()))
        dispatch('set_regression', draft_id=seed['draft_id'], expected_revision=2, job_id=seed['job_id'])
        self.assertTrue(fixture.authoring_observation('pin', fixture.authoring_calls)['checks']['regression_pinned'])
        dispatch('set_regression', draft_id=seed['draft_id'], expected_revision=2, job_id=None)
        self.assertTrue(fixture.authoring_observation('unpin', fixture.authoring_calls)['checks']['regression_null_sent_and_cleared'])
        self.assertFalse(fixture.authoring_observation('unpin', [])['checks']['regression_null_sent_and_cleared'])
        dispatch('archive', draft_id=seed['draft_id'], expected_revision=2)
        self.assertTrue(fixture.authoring_observation('archive', fixture.authoring_calls)['checks']['draft_archived'])
        self.assertFalse(authoring.reviews(ALICE)['items'])
        self.assertFalse(fixture.calls)
        self.assertEqual(len(AUTHORING_CASES), 6)

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

    def test_handoff_modes_have_the_same_read_only_scope_and_separate_pinned_materials(self):
        fixture = self.fixture()
        skill_id = fixture.skills['handoff']
        states = {}
        for profile, required, optional in [('a', 'references/note.md', 'references/questions.md'),
                                            ('b', 'references/questions.md', 'references/note.md')]:
            with self.subTest(profile=profile):
                binding_id = fixture.service.skills.bind(ALICE, skill_id, profile)
                binding = fixture.service.skills.binding(ALICE, binding_id)
                validate_binding(fixture.service, ALICE, binding)
                states[profile] = binding['snapshot']['manifest']['profiles'][profile]
                bundle = skill_bundle(binding)
                self.assertEqual(bundle['loaded_resources'], ['SKILL.md', required])
                self.assertEqual(bundle['optional_resources'], [optional])
                self.assertNotIn('## ' + optional, bundle['content'])
        self.assertEqual(states['a']['execution'], states['b']['execution'])
        self.assertEqual(states['a']['execution']['effects'], ['read'])
        self.assertEqual(states['a']['execution']['capabilities'], [])
        self.assertNotEqual(states['a']['description'], states['b']['description'])

    def candidate_input(self, *, permissions=None):
        source = SkillHostAcceptance(self.home / 'source')
        self.addCleanup(source.close)
        authoring = source.service.skill_authoring
        proposal = {'name': '发布说明', 'description': '按材料写发布说明。', 'instructions': '仅依据给出的材料。',
            'selection': {'use_when': '整理发布说明', 'not_for': '普通润色', 'output': '简报或核对清单'},
            'profiles': {'summary': {'description': '给读者做简报', **(permissions or {})}, 'audit': {'description': '给审核人做核对清单'}},
            'references': {'references/z.md': '按原文概括。', 'references/a.md': '核对遗漏。'},
            'required_resources': {'summary': ['references/z.md'], 'audit': ['references/a.md']}}
        draft = authoring.save(ALICE, proposal=proposal, request_key='candidate')
        proposal['instructions'] += '不得补写效果。'
        authoring.save(ALICE, draft_id=draft['draft_id'], expected_revision=1, proposal=proposal, request_key='candidate-rev2')
        original = authoring.get(ALICE, draft['draft_id'])
        expected = {'skill_id': original['skill_id'], 'revision': 2, 'content_hash': original['bundle']['content_hash']}
        export_path, cases_path = (self.home / 'candidate.json').resolve(), (self.home / 'cases.json').resolve()
        export_path.write_text(json.dumps(authoring.export(ALICE, draft['draft_id']), ensure_ascii=False, sort_keys=True))
        cases = {'format':'agentbridge.skill-host-cases.v1', 'synthetic_redacted': True, 'cases': [
            {'id':'base', 'prompt':'写个发布简报：手册已更新。', 'profile':'summary', 'manual':'保留更新。',
             'forbidden_resources':['references/a.md']},
            {'id':'held-out', 'prompt':'把手册更新这件事简短说给读者。', 'profile':'summary', 'manual':'不补效果。', 'paraphrase_of':'base'},
            {'id':'adjacent', 'prompt':'只润色：手册已更新。', 'profile':None, 'manual':'直接润色。'}]}
        cases_path.write_text(json.dumps(cases, ensure_ascii=False))
        return export_path, cases_path, expected

    def test_candidate_separates_verified_original_copy_publication_and_actual_event_binding(self):
        export, cases, expected = self.candidate_input()
        candidate = load_candidate(export, cases, expected)
        self.assertEqual(list(candidate.proposal['references']), ['references/z.md', 'references/a.md'])
        self.assertEqual(candidate.cases[0]['required_resources'], ['SKILL.md', 'references/z.md'])
        fixture = SkillHostAcceptance(self.home / 'copy', candidate=candidate)
        self.addCleanup(fixture.close)
        replay = fixture.report['candidate_replay']
        self.assertEqual(replay['automatic_status'], 'not_run'); self.assertEqual(replay['expected_source'], expected)
        copy = replay['isolated_copy']
        self.assertNotEqual(copy['skill_id'], expected['skill_id'])
        self.assertNotEqual(copy['draft_content_hash'], expected['content_hash'])
        self.assertNotEqual(copy['published_content_hash'], copy['draft_content_hash'])
        self.assertEqual(copy['draft_revision'], 1)
        self.assertEqual(skill_catalog(fixture.service, BOB)['items'], [])
        store = fixture.service.skills
        bid = store.bind(ALICE, copy['skill_id'], 'summary')
        details = store.binding(ALICE, bid)
        store.record_load(ALICE, copy['skill_id'], 'summary', 'SKILL.md',
            {'status':'succeeded', 'binding_id':bid, 'version':details['version'], **skill_bundle(details)})
        events = store.load_history(ALICE)
        record = fixture.candidate_bindings(ALICE, events)[0]
        self.assertTrue(record['verified'])
        self.assertEqual((record['binding_id'],record['content_hash']), (bid,copy['published_content_hash']))
        self.assertFalse(fixture.candidate_bindings(BOB, events)[0]['verified'])
        self.assertFalse(fixture.candidate_bindings(ALICE, [{**events[0], 'profile':'audit'}])[0]['verified'])
        self.assertFalse(fixture.candidate_bindings(ALICE, [{**events[0], 'event_id':'unknown'}])[0]['verified'])
        with self.assertRaises(PermissionError):
            fixture.service.invoke(user_subject=ALICE, capability_name='oa.workflow.pending.list', arguments={})
        self.assertEqual(fixture.service.denied_business_attempts, [{'capability':'oa.workflow.pending.list'}])
        with self.assertRaises(PermissionError):
            fixture.service.skill_authoring.dispatch(ALICE, 'submit', {})
        with self.assertRaises(PermissionError):
            fixture.service.skill_authoring.dispatch(BOB, 'discover', {})

    def test_candidate_requires_matching_independent_source_and_frozen_proposal(self):
        export, cases, expected = self.candidate_input()
        package = json.loads(export.read_text())
        for invalid in ({**expected, 'revision':1}, {**expected, 'skill_id':'different'}, {**expected, 'content_hash':'a'*64}):
            with self.subTest(expected=invalid), self.assertRaisesRegex(ValueError, '预期修订'):
                load_candidate(export, cases, invalid)
        for patcher, message in [
            (lambda x:x.pop('source'), '来源'),
            (lambda x:x['proposal'].update(instructions='改动后的正文'), '固定修订'),
            (lambda x:x['source']['manifest']['resources'].reverse(), '固定修订'),
            (lambda x:x['proposal'].update(description='悄悄换简介'), '固定修订'),
            (lambda x:x['source'].update(revision=True), '预期修订'),
            (lambda x:x['proposal']['references'].update({'../outside.md':'文本'}), '资料必须')]:
            altered=deepcopy(package);patcher(altered);export.write_text(json.dumps(altered,ensure_ascii=False))
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                load_candidate(export,cases,expected)

    def test_candidate_rejects_a_valid_business_contract_instead_of_silently_removing_its_tools(self):
        export, cases, expected = self.candidate_input(permissions={'all':['oa.workflow.read']})
        with self.assertRaisesRegex(ValueError, '纯文字只读'):
            load_candidate(export, cases, expected)

    def test_candidate_rejects_unsafe_files_and_malformed_holdouts_before_host_authentication(self):
        export,cases,expected=self.candidate_input()
        symlink=self.home.resolve()/'indirect';symlink.symlink_to(export)
        with self.assertRaises(OSError):load_candidate(symlink,cases,expected)
        parent=self.home.resolve()/'via-directory';parent.symlink_to(self.home.resolve(),target_is_directory=True)
        with self.assertRaises(OSError):load_candidate(parent/export.name,cases,expected)
        package=json.loads(cases.read_text())
        for change in [lambda x:x.update(synthetic_redacted=False),lambda x:x['cases'][0].update(id='../case'),
            lambda x:x['cases'][0].update(owner=BOB),lambda x:x['cases'][0].update(prompt='联系 a@example.test'),
            lambda x:x['cases'][1].update(profile='audit'),lambda x:x['cases'][1].update(paraphrase_of='missing'),
            lambda x:x['cases'][1].update(prompt=x['cases'][0]['prompt']+'！'),lambda x:x['cases'][1].update(prompt='仅依据给出的材料。'),
            lambda x:x['cases'][0].update(forbidden_resources=['references/z.md']),
            lambda x:x['cases'][0].update(optional_resource='../../outside.md')]:
            data=deepcopy(package);change(data);cases.write_text(json.dumps(data,ensure_ascii=False))
            with self.assertRaises(ValueError):load_candidate(export,cases,expected)
        cases.write_bytes(b'x'*(65536+1))
        with self.assertRaises(ValueError):load_candidate(export,cases,expected)
        export.write_bytes(b'x'*(256*1024+1))
        with self.assertRaises(ValueError):load_candidate(export,cases,expected)

    def test_candidate_cli_preserves_failed_evidence_and_preparing_a_copy_never_counts_as_running_it(self):
        export,cases,expected=self.candidate_input()
        def synthetic_failure(fixture, case, **_):
            row={'id':case['id'], 'step':0, 'checks':{'agent_finished':False}, 'semantic_pass':None}
            fixture.report['cases'].append(row)
            return [row]
        for mode, wanted_exit, wanted_state in [('prepare',0,'not_run'), ('run',1,'failed')]:
            run_home=self.home/('cli-'+mode)
            args=['skill_host_acceptance','--suite','candidate','--'+mode,'--home',str(run_home),
                  '--node','/synthetic/node','--openclaw','/synthetic/openclaw','--provider-plugin','/synthetic/provider',
                  '--candidate-export',str(export),'--candidate-cases',str(cases),'--expect-skill-id',expected['skill_id'],
                  '--expect-revision','2','--expect-content-hash',expected['content_hash'],
                  '--source-config','/synthetic/config','--source-agent-dir','/synthetic/agent']
            with self.subTest(mode=mode), ExitStack() as mocks:
                mocks.enter_context(patch.object(sys,'argv',args));mocks.enter_context(redirect_stdout(StringIO()))
                for method in ('configure','validate_host','prepare_provider','prepare_auth','start_gateway'):
                    mocks.enter_context(patch.object(SkillHostAcceptance,method))
                mocks.enter_context(patch.object(SkillHostAcceptance,'run_case',synthetic_failure))
                self.assertEqual(harness_main(),wanted_exit)
            report=json.loads((run_home/'evidence/report.json').read_text())
            self.assertEqual(report['candidate_replay']['automatic_status'],wanted_state)
            self.assertEqual(report['candidate_replay']['semantic_review'],'pending')
            self.assertEqual(len(report['cases']),0 if mode=='prepare' else 3)

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

    def test_selection_counts_exact_successful_skills_and_preserves_failed_attempts(self):
        skills = {'weekly': 'weekly-id', 'plan': 'plan-id'}
        weekly = {'skill_id': 'weekly-id', 'profile': 'use', 'status': 'succeeded'}
        plan = {'skill_id': 'plan-id', 'profile': 'use', 'status': 'succeeded'}
        denied = {'skill_id': 'plan-id', 'status': 'failed', 'error_code': 'SKILL_UNAVAILABLE'}
        for event_list, expected_skill, matches in [
            ([weekly, plan], 'weekly', False), ([weekly, weekly, denied], 'weekly', True),
            ([weekly], None, False), ([denied], None, True), ([denied], 'plan', False),
        ]:
            with self.subTest(events=event_list, expected=expected_skill):
                result = observe({'skill': expected_skill}, {'payloads': [{'text': '明天补测试。'}]},
                                 event_list, [], skills, 0)
                self.assertEqual(result['checks']['skill_selection'], matches)
                self.assertEqual(result['skill_loads']['failed_attempts'], [denied] if denied in event_list else [])
        self.assertEqual(observe({'skill': None}, None, [denied], [], skills, 1)['skill_loads']['successful'], [])

    def test_selection_rejects_wrong_modes_and_optional_reads_of_the_other_modes_material(self):
        case = next(case for case in json.loads(CASES_PATH.read_text()) if case['id'] == 'handoff-note')
        skills = {'handoff': 'fixture-handoff'}
        note, questions = 'references/note.md', 'references/questions.md'
        expected = {'skill_id': skills['handoff'], 'profile': 'a', 'status': 'succeeded', 'loaded_resources': ['SKILL.md', note]}
        wrong = {**expected, 'profile': 'b', 'loaded_resources': ['SKILL.md', questions]}
        def check(events, resource_calls=(), attempts=()):
            return observe(case, None, events, [], skills, 0, resource_calls, resource_attempts=attempts)
        self.assertEqual(check([expected])['skill_loads']['successful_bindings'], [{'skill_id': 'fixture-handoff', 'profile': 'a'}])
        for events in ([wrong], [expected, wrong], [{**expected, 'profile': None}]):
            with self.subTest(events=events):
                self.assertFalse(check(events)['checks']['skill_selection'])
        self.assertFalse(check([wrong])['checks']['required_resources'])
        self.assertFalse(check([expected, {**expected, 'loaded_resources': ['SKILL.md']}])['checks']['required_resources'])
        self.assertFalse(check([{**expected, 'loaded_resources': ['SKILL.md', note, questions]}])['checks']['forbidden_resources'])
        self.assertFalse(check([expected], [{'path': questions}])['checks']['forbidden_resources'])
        self.assertFalse(check([expected], attempts=[{'path': questions, 'status': 'rejected'}])['checks']['forbidden_resources'])
        denied = {**wrong, 'status': 'rejected', 'error_code': 'SKILL_UNAVAILABLE'}
        observed = check([expected, denied])
        self.assertTrue(all(observed['checks'][key] for key in ('skill_selection', 'required_resources', 'forbidden_resources')))
        self.assertEqual(observed['skill_loads']['failed_attempts'][0]['profile'], 'b')

    def test_cases_include_same_material_different_goals_and_independent_boundaries(self):
        cases = json.loads(CASES_PATH.read_text())
        self.assertEqual(len(cases), 25)
        self.assertEqual(len({case['id'] for case in cases}), len(cases))
        contrasts = [case for case in cases if case['id'].startswith('same-material-')]
        self.assertEqual({case['skill'] for case in contrasts}, {None, 'plan', 'weekly'})
        self.assertEqual(len(contrasts), 3)
        self.assertTrue(all(case['no_read'] and case['prompt'].endswith('：明天补测试。') for case in contrasts))
        self.assertTrue(any(case.get('owner') == BOB and case.get('no_read') for case in cases))
        self.assertTrue(any(case.get('follow_up') for case in cases))
        self.assertEqual({case.get('skill') for case in cases}, {'weekly', 'plan', 'evidence', 'handoff', None})
        handoffs = [case for case in cases if case['skill'] == 'handoff']
        self.assertEqual({case['profile'] for case in handoffs}, {'a', 'b'})
        self.assertEqual(len({case['prompt'].split('：', 1)[1] for case in handoffs}), 1)
        self.assertTrue(all(case['no_read'] and case['required_resources'] and case['forbidden_resources'] for case in handoffs))

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
        self.assertEqual(result, {'bound': True, 'catalogItems': 4, 'foreignBound': False, 'sender': ALICE,
                                 'directBound': True, 'directSender': ALICE})


if __name__ == '__main__':
    unittest.main()
