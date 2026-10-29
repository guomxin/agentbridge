"""Fixed skill contracts narrow execution independently of profile spelling."""
from copy import deepcopy
from dataclasses import replace
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.business_skills import SkillRejected, validate_binding, validate_skill_bundle
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.skill_execution import effective_skill_execution


class SkillExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.service = CentralCapabilityService(home=self.temp.name, base_url='http://oa.test')
        self.service.user_grants.save('alice', ['oa.workflow.read', 'taihua.work_log.create', 'oa.missed_punch.approve'],
            expected_revision=0, actor='fixture', reason='synthetic contract checks')
        self.proposal = {'name': '测试方法', 'description': '使用声明的能力',
            'selection': {'use_when': '测试', 'not_for': '未声明业务', 'output': '结果'}, 'instructions': '核对输入与结果。'}

    def bundle(self, profiles=None, mode='read_exploration'):
        return self.service.skill_authoring.normalize('test-contract', {**self.proposal,
            'profiles': profiles or {'use': {}}, 'executionMode': mode})

    @staticmethod
    def binding(bundle, profile='use'):
        return {'snapshot': bundle, 'profile': profile, 'settings': {'source_id': ''}}

    def validate(self, bundle, capability, profile='use'):
        validate_binding(self.service, 'alice', self.binding(bundle, profile), capability=capability)

    def test_read_contract_survives_profile_rename_and_pure_text_cannot_call_business(self):
        for name in ('preview', 'readonly', 'use'):
            bundle = self.bundle({name: {'all': ['oa.workflow.read']}})
            policy = effective_skill_execution(self.binding(bundle, name))
            self.assertEqual(policy['effects'], ['read']); self.assertFalse(policy['legacy'])
            self.validate(bundle, 'oa.workflow.pending.list', name)
            with self.assertRaises(SkillRejected): self.validate(bundle, 'taihua.work_log.create.prepare', name)
        pure = self.bundle()
        self.assertEqual(effective_skill_execution(self.binding(pure))['capabilities'], [])
        for capability in ('oa.workflow.pending.list', 'database.directory', 'taihua.work_log.create.prepare'):
            with self.subTest(capability=capability), self.assertRaises(SkillRejected): self.validate(pure, capability)

    def test_limits_follow_metadata_not_profile_or_capability_substrings(self):
        profile = {'all': ['oa.workflow.read'], 'any': ['oa.missed_punch.approve'],
                   'execution': {'effects': ['read', 'controlled_write'], 'allow_batch': True}}
        bundle = self.bundle({'single': profile}, 'controlled_action')
        self.validate(bundle, 'oa.workflow.pending.batch.prepare', 'single')
        narrower = deepcopy(bundle)
        narrower['manifest']['profiles']['single']['execution']['allow_batch'] = False
        with self.assertRaisesRegex(SkillRejected, '批量'): self.validate(narrower, 'oa.workflow.pending.batch.prepare', 'single')
        spec = self.service.registry.get('oa.workflow.pending.batch.prepare')
        renamed = replace(spec, name='oa.workflow.multiple.prepare')
        self.service.registry.register(renamed)
        policy = narrower['manifest']['profiles']['single']['execution']
        policy['capabilities'].append(renamed.name)
        with self.assertRaisesRegex(SkillRejected, '批量'): self.validate(narrower, renamed.name, 'single')
        ordinary = replace(spec, name='oa.batch_notes.read', effect='read', batch=False)
        self.service.registry.register(ordinary); policy['capabilities'].append(ordinary.name)
        self.validate(narrower, ordinary.name, 'single')
        with self.assertRaises(TypeError): replace(spec, batch='false')

    def test_explicit_scope_is_distinct_from_minimum_dependencies_and_commits_are_mapped(self):
        profile = {'all': ['taihua.work_log.create'], 'execution': {'effects': ['read', 'controlled_write'],
            'allow_batch': False, 'capabilities': ['taihua.work_log.create.prepare']}}
        bundle = self.bundle({'use': profile}, 'durable_plan')
        self.validate(bundle, 'taihua.work_log.create.prepare')
        self.validate(bundle, 'taihua.work_log.create')
        self.assertNotIn('taihua.work_log.create', effective_skill_execution(self.binding(bundle))['capabilities'])
        for name in ('oa.workflow.pending.list', 'oa.missed_punch.approval.prepare'):
            with self.assertRaises(SkillRejected): self.validate(bundle, name)
        for invalid in ('taihua.work_log.create', 'database.directory', 'made.up.capability'):
            bad = deepcopy(profile); bad['execution']['capabilities'] = [invalid]
            with self.subTest(capability=invalid), self.assertRaises(ValueError): self.bundle({'use': bad}, 'durable_plan')
        optional = deepcopy(profile)
        optional['execution']['capabilities'].append('oa.workflow.pending.list')
        with_optional = self.bundle({'use': optional}, 'durable_plan')
        self.validate(with_optional, 'oa.workflow.pending.list')
        self.service.user_grants.save('alice', ['taihua.work_log.create'], expected_revision=1, actor='fixture', reason='remove optional permission')
        self.validate(with_optional, 'oa.workflow.pending.list')  # Skill scope is not the business grant.
        with self.assertRaises(PermissionError):
            self.service.user_grants.require_capability('alice', 'oa.workflow.pending.list')
        old = self.service.user_grants.get('alice')
        self.service.user_grants.save('alice', [], expected_revision=old['revision'], actor='fixture', reason='revoke')
        with self.assertRaisesRegex(SkillRejected, '依赖不可用'): self.validate(bundle, 'taihua.work_log.create')

    def test_batch_only_contract_maps_every_supported_commit_but_no_single_prepare_or_other_write(self):
        from agentbridge.adapters.seeyon_pending_actions import PENDING_ACTION_CAPABILITY_DEFINITIONS
        from agentbridge.adapters.seeyon_pending_batch import PENDING_BATCH_TYPES
        profile = {'all': ['oa.workflow.read'], 'execution': {'effects': ['read', 'controlled_write'],
            'allow_batch': True, 'capabilities': ['oa.workflow.pending.batch.prepare']}}
        bundle = self.bundle({'use': profile}, 'controlled_action')
        covered = {'meeting_reply', 'missed_punch'}
        self.validate(bundle, 'oa.meeting.reply')
        self.validate(bundle, 'oa.missed_punch.approve')
        for item in PENDING_ACTION_CAPABILITY_DEFINITIONS:
            if item['profile'] not in PENDING_BATCH_TYPES:
                continue
            covered.add(item['profile'])
            self.validate(bundle, item['commit_capability'])
            with self.assertRaises(SkillRejected): self.validate(bundle, item['prepare_capability'])
        self.assertEqual(covered, set(PENDING_BATCH_TYPES))
        for name in ('oa.meeting.reply.prepare', 'oa.missed_punch.approval.prepare',
                     'taihua.work_log.create', 'smartlight.alarm.work_area.revoke'):
            with self.subTest(capability=name), self.assertRaises(SkillRejected): self.validate(bundle, name)
        # Invalid stale policy data cannot use the batch closure to bypass the boolean.
        bundle['manifest']['profiles']['use']['execution']['allow_batch'] = False
        with self.assertRaises(SkillRejected): self.validate(bundle, 'oa.meeting.reply')
        with self.assertRaises(SkillRejected): self.validate(bundle, 'oa.missed_punch.approve')
        with self.assertRaises(SkillRejected): self.validate(bundle, 'oa.workflow.pending.batch.prepare')

    def test_new_contract_rejects_invalid_effects_unknown_dependencies_and_pure_expansion(self):
        for policy in ({'effects': ['controlled_write'], 'allow_batch': False},
                       {'effects': ['read', 'read'], 'allow_batch': False},
                       {'effects': ['read'], 'allow_batch': True},
                       {'effects': ['read'], 'allow_batch': False, 'capabilities': ['database.directory']}):
            with self.subTest(policy=policy), self.assertRaises(ValueError): self.bundle({'use': {'execution': policy}})
        with self.assertRaises(ValueError): self.bundle({'use': {'all': ['unknown.permission']}})
        with self.assertRaises(ValueError): self.bundle({'use': {'description': ' '}})
        candidate = self.bundle()
        candidate['manifest']['profiles']['use']['execution'].pop('capabilities')
        with self.assertRaises(ValueError): validate_skill_bundle(candidate['manifest'], candidate['resources'])

    def test_legacy_snapshot_remains_unchanged_with_original_name_constraints(self):
        original = self.bundle({'preview': {'all': ['oa.workflow.read']}, 'use': {'all': ['oa.workflow.read']}})
        for profile in original['manifest']['profiles'].values(): profile.pop('execution')
        original = validate_skill_bundle(original['manifest'], original['resources'])
        before = deepcopy(original)
        self.assertTrue(effective_skill_execution(self.binding(original, 'preview'))['legacy'])
        self.assertIsNone(effective_skill_execution(self.binding(original, 'use'))['capabilities'])
        with self.assertRaises(SkillRejected): self.validate(original, 'taihua.work_log.create.prepare', 'preview')
        self.validate(original, 'taihua.work_log.create.prepare', 'use')
        self.validate(original, 'database.directory', 'preview')
        self.assertEqual(original, before)

    def test_all_builtins_have_exact_contracts_and_registered_batch_flags(self):
        for item in self.service.skills.registry.items.values():
            for profile in item['manifest']['profiles']:
                execution = effective_skill_execution(self.binding(item, profile))
                self.assertFalse(execution['legacy']); self.assertIsInstance(execution['capabilities'], list)
        work_log = self.service.skills.registry.get('oa-work-log')
        self.assertEqual(effective_skill_execution(self.binding(work_log, 'preview'))['effects'], ['read'])
        from agentbridge.database.independent import CAPABILITIES
        for sid, profile in [('log-review', 'review'), ('issue-tracking', 'track'), ('similar-cases', 'search')]:
            policy = effective_skill_execution(self.binding(self.service.skills.registry.get(sid), profile))
            self.assertEqual(set(policy['capabilities']), set(CAPABILITIES))
        self.assertEqual({spec.name for spec in self.service.registry.list() if spec.batch},
            {'oa.workflow.pending.batch.prepare', 'oa.missed_punch.approval.batch.prepare'})
