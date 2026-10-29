"""Public skill request compatibility frozen before the 2I refactor."""
from copy import deepcopy
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
import unittest

from pydantic import ValidationError

from agentbridge.core.skill_actions import SKILL_ACTION_DEFINITIONS, SKILL_ACTIONS, SkillActionName, parse_skill_action
from agentbridge.core.skill_authoring import SkillAuthoring
from agentbridge.core import skill_contracts, skill_quality
from agentbridge.core.skill_workbench import SkillWorkbench


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/skill_action_contract.json').read_text())


def recorder_authoring():
    """Record routing and exact provided values without storage or business calls."""
    authoring = SimpleNamespace(workbench=SimpleNamespace())
    for action in FIXTURE['actions']:
        def make_recorder(target, handler):
            def record(owner, **data):
                return {'target': target, 'handler': handler, 'owner': owner, 'data': data}
            return record
        target = authoring if action['target'] == 'authoring' else authoring.workbench
        setattr(target, action['handler'], make_recorder(action['target'], action['handler']))
    return authoring


def dispatch_outcome(authoring, action, data):
    try:
        return {'result': SkillAuthoring.dispatch(authoring, 'alice', action, data)}
    except Exception as error:
        return {'error': type(error).__name__, 'message': str(error)}


def transport_outcome(data):
    try:
        return {'result': skill_contracts.SkillAuthoringData.model_validate(data).model_dump(
            exclude_unset=True, exclude_none=True)}
    except ValidationError as error:
        return {'error': 'ValidationError', 'issues': [
            {'loc': list(e['loc']), 'type': e['type'], 'message': e['msg']} for e in error.errors()]}


class SkillActionContractTests(unittest.TestCase):
    def test_public_schemas_and_legacy_imports_match_frozen_baseline(self):
        self.assertEqual(FIXTURE['baseline'], '8dca9e0317e8cf3db6351d21544f1a96fbf71aa0')
        for name, expected in FIXTURE['schemas'].items():
            with self.subTest(model=name):
                current = deepcopy(getattr(skill_contracts, name).model_json_schema())
                # Assert additive fields separately; retain the original contract
                # for all prior fields and definitions rather than replacing it.
                current.get('properties', {}).pop('standard_metadata', None)
                current.get('properties', {}).pop('expected_version', None)
                current.get('$defs', {}).pop('SkillStandardMetadata', None)
                current.get('$defs', {}).pop('SkillExecution', None)
                current.get('$defs', {}).get('SkillProposal', {}).get('properties', {}).pop('standard_metadata', None)
                for added in ('description', 'execution'):
                    current.get('$defs', {}).get('SkillDependencies', {}).get('properties', {}).pop(added, None)
                    if name == 'SkillDependencies': current.get('properties', {}).pop(added, None)
                self.assertEqual(current, expected)
        for name in ('StrictModel', 'SkillSelection', 'SkillDatabaseDependencies', 'SkillDependencies',
                     'SkillMethod', 'SkillProposal', 'SkillAuthoringData'):
            self.assertIs(getattr(skill_quality, name), getattr(skill_contracts, name))
        from agentbridge.mcp.central import SkillAuthoringData
        self.assertIs(SkillAuthoringData, skill_contracts.SkillAuthoringData)

    def test_declarations_match_original_routes_and_handler_signatures(self):
        additions = {'pending_cases', 'save_pending_cases', 'set_regression'}
        self.assertEqual(len(SKILL_ACTION_DEFINITIONS), 31)
        self.assertEqual([name for name in SKILL_ACTIONS if name not in additions], [action['name'] for action in FIXTURE['actions']])
        self.assertEqual(get_args(SkillActionName), tuple(SKILL_ACTIONS))
        self.assertEqual(len(SKILL_ACTIONS), len(SKILL_ACTION_DEFINITIONS))
        fields = set()
        for expected in FIXTURE['actions']:
            with self.subTest(action=expected['name']):
                definition = SKILL_ACTIONS[expected['name']]
                self.assertEqual((definition.target, definition.handler), (expected['target'], expected['handler']))
                target = SkillAuthoring if definition.target == 'authoring' else SkillWorkbench
                signature = inspect.signature(getattr(target, definition.handler))
                signature = signature.replace(parameters=list(signature.parameters.values())[1:])
                baseline_signature = signature
                if definition.name == 'feedback':
                    # The second Skill batch adds only optional source references;
                    # retain the historical fixture for every existing parameter.
                    for name in ('test_id', 'binding_id'):
                        self.assertIsNone(signature.parameters[name].default)
                    baseline_signature = signature.replace(parameters=[p for p in signature.parameters.values()
                        if p.name not in {'test_id', 'binding_id'}])
                self.assertEqual(str(baseline_signature), expected['signature'])
                params = [p for p in signature.parameters.values() if p.name != 'owner' and not p.name.startswith('_')]
                required = tuple(p.name for p in params if p.default is inspect.Parameter.empty)
                optional = tuple(p.name for p in params if p.default is not inspect.Parameter.empty)
                self.assertEqual(definition.required, required)
                self.assertEqual(definition.optional, optional)
                self.assertFalse(set(required) & set(optional))
                fields.update(required + optional)
        for name in additions:
            definition = SKILL_ACTIONS[name]
            signature = inspect.signature(getattr(SkillWorkbench, definition.handler))
            params = [p for p in signature.parameters.values() if p.name not in {'self', 'owner'}]
            self.assertEqual(definition.required, tuple(p.name for p in params if p.default is inspect.Parameter.empty))
            self.assertEqual(definition.optional, ())
            fields.update(definition.required)
        self.assertEqual(fields, set(skill_contracts.SkillAuthoringData.model_fields))

    def test_new_metadata_and_version_fields_are_strict_and_review_is_not_an_action(self):
        self.assertEqual(transport_outcome({'expected_version': 0})['result'], {'expected_version': 0})
        self.assertEqual(transport_outcome({'expected_version': True})['error'], 'ValidationError')
        metadata = skill_contracts.SkillStandardMetadata.model_validate({'license': 'MIT', 'compatibility': 'Text only'})
        self.assertEqual(metadata.model_dump(), {'license': 'MIT', 'compatibility': 'Text only'})
        for invalid in ({'license': 1}, {'compatibility': 'x' * 501}, {'allowed_tools': ['write']}, {'license': ''}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                skill_contracts.SkillStandardMetadata.model_validate(invalid)
        for action in ('review_report', 'decide'):
            with self.assertRaises(ValueError): parse_skill_action(action, {})
        parsed = parse_skill_action('set_regression', {'draft_id': 'draft', 'expected_revision': 1, 'job_id': None})
        self.assertIsNone(parsed.arguments['job_id'])

    def test_dispatch_shapes_values_errors_and_routes_match_frozen_baseline(self):
        authoring = recorder_authoring()
        for case in FIXTURE['dispatchCases']:
            with self.subTest(case=case['label']):
                data = deepcopy(case['data'])
                expected = case['expected']
                if case['label'] in {'feedback:unrelated:test_id', 'feedback:unrelated:binding_id'}:
                    expected = {'result': {'target': 'workbench', 'handler': 'feedback', 'owner': 'alice', 'data': data}}
                self.assertEqual(dispatch_outcome(authoring, case['action'], data), expected)
                self.assertEqual(data, case['data'])

    def test_mcp_validation_and_serialization_match_frozen_baseline(self):
        for index, case in enumerate(FIXTURE['transportCases']):
            with self.subTest(case=index):
                data = deepcopy(case['data'])
                self.assertEqual(transport_outcome(data), case['expected'])
                self.assertEqual(data, case['data'])

    def test_handler_replacement_is_observed_for_both_domains(self):
        authoring = recorder_authoring()
        self.assertEqual(SkillAuthoring.dispatch(authoring, 'alice', 'list', {})['handler'], 'list')
        authoring.list = lambda owner: {'owner': owner, 'replacement': 'authoring'}
        self.assertEqual(SkillAuthoring.dispatch(authoring, 'bob', 'list', {}),
                         {'owner': 'bob', 'replacement': 'authoring'})
        authoring.workbench = SimpleNamespace(jobs=lambda owner, **data: {'owner': owner, 'replacement': data})
        self.assertEqual(SkillAuthoring.dispatch(authoring, 'bob', 'jobs', {'job_id': None}),
                         {'owner': 'bob', 'replacement': {'job_id': None}})

    def test_request_preserves_missing_null_and_nested_values_without_defaults(self):
        omitted = parse_skill_action('generate', {'material': '合成材料', 'request_key': 'one'})
        self.assertEqual(dict(omitted.arguments), {'material': '合成材料', 'request_key': 'one'})
        original = {'value': {'auto_draft': None}, 'expected_revision': None}
        parsed = parse_skill_action('preferences', original)
        self.assertEqual(dict(parsed.arguments), original)
        self.assertIs(parsed.arguments['value'], original['value'])
        with self.assertRaises(TypeError):
            parsed.arguments['owner'] = 'other'
        original['expected_revision'] = 3
        self.assertIsNone(parsed.arguments['expected_revision'])

    def test_workspace_and_mcp_keep_different_null_and_type_boundaries(self):
        authoring = recorder_authoring()
        raw = {'draft_id': None}
        self.assertEqual(dispatch_outcome(authoring, 'list', raw),
                         {'error': 'ValueError', 'message': '草稿操作参数无效'})
        self.assertEqual(SkillAuthoring.dispatch(authoring, 'alice', 'list', transport_outcome(raw)['result'])['data'], {})
        self.assertEqual(SkillAuthoring.dispatch(authoring, 'alice', 'discover', {'limit': True})['data'], {'limit': True})
        self.assertEqual(transport_outcome({'limit': True})['error'], 'ValidationError')

    def test_malformed_python_keys_preserve_short_circuit_order(self):
        authoring = recorder_authoring()
        self.assertEqual(dispatch_outcome(authoring, 'missing', {1: 'x'}),
                         {'error': 'ValueError', 'message': '草稿操作无效'})
        self.assertEqual(dispatch_outcome(authoring, 'list', {'_job': {}, 1: 'x'}),
                         {'error': 'ValueError', 'message': '草稿操作无效'})
        self.assertEqual(dispatch_outcome(authoring, 'list', {1: 'x', '_job': {}}),
                         {'error': 'AttributeError', 'message': "'int' object has no attribute 'startswith'"})

    def test_domain_failures_are_not_reclassified_as_shape_errors(self):
        authoring = recorder_authoring()
        error = TypeError('domain failure')
        def fail(owner):
            raise error
        authoring.list = fail
        with self.assertRaises(TypeError) as raised:
            SkillAuthoring.dispatch(authoring, 'alice', 'list', {})
        self.assertIs(raised.exception, error)


if __name__ == '__main__':
    unittest.main()
