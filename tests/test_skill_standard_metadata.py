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
        proposal['name'] = '更新后的显示标题'
        self.a.save('alice', draft_id=draft['draft_id'], expected_revision=1, proposal=proposal, request_key='edit')
        exported = self.a.workbench.export('alice', draft['draft_id'])
        imported = import_standard(exported)
        self.assertEqual(imported['name'], 'text-helper')
        self.assertEqual(imported['standard_metadata'], metadata)
        self.assertEqual(imported['instructions'], proposal['instructions'])
        self.assertEqual(imported['profiles'], {'use': {}})
        other = self.a.workbench.import_package('bob', package=exported, request_key='import')
        self.assertNotEqual(other['skill_id'], draft['skill_id'])
        self.assertEqual(self.a.workbench.export('bob', other['draft_id']), exported)
        with self.assertRaises(KeyError):
            self.a.workbench.export('bob', draft['draft_id'])

    def test_export_uses_original_import_name_only_for_a_valid_external_name(self):
        proposal = import_standard(self.package)
        for provenance, title, use_original in [
            ('request', 'text-helper', False), ('import', '中文草稿', False),
            ('import', '2-' + 'x' * 62, True),
        ]:
            with self.subTest(provenance=provenance, title=title):
                saved = self.a.save('alice', proposal={**proposal, 'name': title}, request_key=title+provenance,
                                    provenance={'kind': provenance})
                exported = self.a.workbench.export('alice', saved['draft_id'])
                self.assertEqual(import_standard(exported)['name'], title if use_original else saved['skill_id'])

    def test_full_resource_budget_survives_standard_headers_utf8_and_json_escapes(self):
        for c in ('文', '📎', '\x01'):
            with self.subTest(c=repr(c)):
                resources = {'SKILL.md': c * 40_000, 'references/example.md': c * 8_000}
                proposal = {**import_standard(self.package), 'instructions': resources['SKILL.md'],
                    'description': '字段\u0085包括\u2028换行\u2029记号',
                    'references': {'references/example.md': resources['references/example.md']},
                    'standard_metadata': {'license': 'M' + ('\x01' * 999), 'compatibility': '文' * 500}}
                saved = self.a.save('alice', proposal=proposal, request_key='edge-' + repr(c))
                pack = self.a.workbench.export('alice', saved['draft_id'])
                again = self.a.workbench.import_package('bob', package=pack, request_key='edge-' + repr(c))
                self.assertEqual(self.a.get('bob', again['draft_id'])['bundle']['resources'], resources)
                self.assertEqual(self.a.workbench.export('bob', again['draft_id']), pack)
                pack['files']['references/example.md'] += 'x'
                with self.assertRaisesRegex(ValueError, '48,000'):
                    self.a.workbench.import_package('bob', package=pack, request_key='over')

    def test_standard_header_and_file_envelope_are_bounded(self):
        source = self.package['files']['SKILL.md']
        with self.assertRaisesRegex(ValueError, '元数据头'):
            import_standard({**self.package, 'files': {'SKILL.md': source.replace('name:', '#' + 'x'*16_384 + '\nname:')}})
        with self.assertRaisesRegex(ValueError, '384 KiB'):
            import_standard({**self.package, 'files': {'SKILL.md': source, 'references/' + '文'*140_000 + '.md': 'x'}})

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
