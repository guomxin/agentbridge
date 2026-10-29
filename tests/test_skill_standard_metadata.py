"""Standard package round trips preserve declarations without importing authority."""
from copy import deepcopy
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.business_skills import SkillRegistry
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.skill_quality import export_standard, import_standard, proposal_from_bundle


class SkillStandardMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.service = CentralCapabilityService(home=self.temp.name, base_url='http://oa.test')
        self.a = self.service.skill_authoring
        self.package = {'format': 'agentskills.files.v1', 'files': {'SKILL.md':
            '---\nname: text-helper\ndescription: Organize supplied text\nlicense: MIT\n'
            'compatibility: Text only\n---\n\nPreserve the original facts.'}}

    def test_import_edit_native_export_and_standard_roundtrip_keep_metadata(self):
        draft = self.a.workbench.import_package('alice', package=self.package, request_key='import')
        bundle = self.a.get('alice', draft['draft_id'])['bundle']
        metadata = {'license': 'MIT', 'compatibility': 'Text only'}
        self.assertEqual(bundle['manifest']['standard_metadata'], metadata)
        proposal = proposal_from_bundle(bundle)
        self.assertEqual(self.a.export('alice', draft['draft_id'])['proposal']['standard_metadata'], metadata)
        proposal['instructions'] += '\nKeep plans separate.'
        self.a.save('alice', draft_id=draft['draft_id'], expected_revision=1, proposal=proposal, request_key='edit')
        exported = self.a.workbench.export('alice', draft['draft_id'])
        imported = import_standard(exported)
        self.assertEqual(imported['standard_metadata'], metadata)
        self.assertEqual(imported['instructions'], proposal['instructions'])
        self.assertEqual(imported['profiles'], {'use': {}})

    def test_pasted_and_builtin_frontmatter_exports_have_one_header(self):
        proposal = import_standard(self.package)
        proposal['instructions'] = self.package['files']['SKILL.md']
        draft = self.a.save('alice', proposal=proposal, request_key='pasted')
        bundle = self.a.get('alice', draft['draft_id'])['bundle']
        self.assertFalse(bundle['resources']['SKILL.md'].startswith('---'))
        exported = export_standard(bundle)['files']['SKILL.md']
        self.assertEqual(exported.count('\n---\n'), 1)
        self.assertEqual(exported.count('license:'), 1)
        builtin = SkillRegistry().get('oa-work-log')
        before = deepcopy(builtin)
        result = export_standard(builtin)['files']['SKILL.md']
        self.assertEqual(result.count('\n---\n'), 1)
        self.assertEqual(builtin, before)

    def test_invalid_conflicting_or_unknown_metadata_is_not_silently_lost(self):
        proposal = import_standard(self.package)
        for metadata in ({'license': ' '}, {'license': 1}, {'license': 'x' * 1001},
                         {'compatibility': 'x' * 501}, {'allowed_tools': ['submit']}, ['MIT']):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.a.normalize('test-helper', {**proposal, 'standard_metadata': metadata})
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.a.normalize('test-helper', {**proposal, 'instructions': self.package['files']['SKILL.md'],
                'standard_metadata': {'license': 'Apache-2.0'}})
        for line in ('allowed-tools: submit', 'license: !unsafe', 'license: ' + 'x' * 1001):
            source = self.package['files']['SKILL.md'].replace('license: MIT', line)
            with self.subTest(line=line[:30]), self.assertRaises(ValueError):
                import_standard({'format': 'agentskills.files.v1', 'files': {'SKILL.md': source}})

    def test_optional_metadata_does_not_change_existing_bundle_shape(self):
        proposal = import_standard(self.package)
        proposal.pop('standard_metadata')
        bundle = self.a.normalize('test-helper', proposal)
        self.assertNotIn('standard_metadata', bundle['manifest'])
        self.assertNotIn('standard_metadata', proposal_from_bundle(bundle))
        proposal['instructions'] = '---\n\n## 输入\n原始文本\n\n---\n\n## 步骤\n保留事实。'
        bundle = self.a.normalize('test-helper', proposal)
        self.assertEqual(bundle['resources']['SKILL.md'], proposal['instructions'])
        self.assertTrue(export_standard(bundle)['files']['SKILL.md'].endswith(proposal['instructions']))

    def test_new_references_are_optional_and_explicit_historical_requirements_survive_edit(self):
        proposal = import_standard({**self.package, 'files': {**self.package['files'], 'references/example.md': 'Optional example'}})
        bundle = self.a.normalize('text-helper', proposal)
        self.assertEqual(bundle['manifest']['required_resources'], {'use': []})
        proposal['required_resources'] = {'use': ['references/example.md']}
        original = self.a.normalize('text-helper', proposal)
        edited = proposal_from_bundle(original); edited['instructions'] += '\nEdited explanation.'
        self.assertEqual(self.a.normalize('text-helper', edited)['manifest']['required_resources'],
                         {'use': ['references/example.md']})
