"""Explicit public skill actions and shape-only request dispatch.

This boundary does not normalize values or apply defaults. MCP has a typed
legacy envelope; Workspace passes JSON directly. Domain methods own all value,
identity, revision and transaction checks for both entry points.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping


@dataclass(frozen=True)
class SkillActionDefinition:
    name: str
    target: Literal['authoring', 'workbench']
    handler: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()


# Only these declarations select handlers. User input never names a method.
SKILL_ACTION_DEFINITIONS = (
    SkillActionDefinition('save', 'authoring', 'save',
                          ('proposal', 'request_key'), ('draft_id', 'expected_revision', 'provenance')),
    SkillActionDefinition('list', 'authoring', 'list'),
    SkillActionDefinition('get', 'authoring', 'get', ('draft_id',)),
    SkillActionDefinition('test', 'authoring', 'test_start',
                          ('draft_id', 'expected_revision', 'profile', 'prompt', 'request_key')),
    SkillActionDefinition('test_result', 'authoring', 'test_result', ('test_id', 'output')),
    SkillActionDefinition('submit', 'authoring', 'submit',
                          ('draft_id', 'expected_revision', 'request_key', 'reason'), ('audience', 'profiles')),
    SkillActionDefinition('withdraw', 'authoring', 'withdraw', ('request_id',)),
    SkillActionDefinition('preferences', 'authoring', 'preferences', (), ('value', 'expected_revision')),
    SkillActionDefinition('restore', 'authoring', 'restore',
                          ('draft_id', 'expected_revision', 'target_revision', 'request_key')),
    SkillActionDefinition('archive', 'authoring', 'archive', ('draft_id', 'expected_revision')),
    SkillActionDefinition('export', 'authoring', 'export', ('draft_id',)),
    SkillActionDefinition('requests', 'authoring', 'reviews'),
    SkillActionDefinition('inspect', 'workbench', 'inspect', ('draft_id',)),
    SkillActionDefinition('generate', 'workbench', 'generate', ('material', 'request_key'),
                          ('draft_id', 'expected_revision', 'scope', 'automatic', 'task_ids')),
    SkillActionDefinition('evaluate', 'workbench', 'evaluate',
                          ('draft_id', 'expected_revision', 'cases', 'request_key'), ('repeats',)),
    SkillActionDefinition('jobs', 'workbench', 'jobs', (), ('job_id',)),
    SkillActionDefinition('cancel_job', 'workbench', 'cancel', ('job_id',)),
    SkillActionDefinition('retry_job', 'workbench', 'retry', ('job_id',)),
    SkillActionDefinition('scopes', 'workbench', 'scopes', (), ('scope', 'enabled')),
    SkillActionDefinition('export_standard', 'workbench', 'export', ('draft_id',)),
    SkillActionDefinition('import_standard', 'workbench', 'import_package', ('package', 'request_key')),
    SkillActionDefinition('feedback', 'workbench', 'feedback', ('skill_id', 'profile', 'rating'),
                          ('comment', 'version')),
    SkillActionDefinition('metrics', 'workbench', 'metrics'),
    SkillActionDefinition('recipients', 'workbench', 'recipients'),
    SkillActionDefinition('discover', 'workbench', 'discover', (), ('query', 'limit')),
    SkillActionDefinition('resource', 'workbench', 'resource', ('binding_id', 'path')),
    SkillActionDefinition('composition', 'workbench', 'composition', ('steps',)),
    SkillActionDefinition('adopt', 'workbench', 'adopt', ('job_id', 'request_key'),
                          ('draft_id', 'expected_revision')),
)

SKILL_ACTIONS: Mapping[str, SkillActionDefinition] = MappingProxyType({
    definition.name: definition for definition in SKILL_ACTION_DEFINITIONS
})


@dataclass(frozen=True)
class SkillActionRequest:
    definition: SkillActionDefinition
    arguments: Mapping[str, Any]


def parse_skill_action(action, data) -> SkillActionRequest:
    # Preserve the legacy short-circuit order, including malformed Python input.
    if action not in SKILL_ACTIONS or not isinstance(data, dict) or any(k.startswith('_') for k in data):
        raise ValueError('草稿操作无效')
    definition = SKILL_ACTIONS[action]
    supplied = data.keys()
    if (not set(definition.required) <= supplied
            or supplied - set(definition.required) - set(definition.optional)):
        raise ValueError('草稿操作参数无效')
    return SkillActionRequest(definition, MappingProxyType(dict(data)))


def dispatch_skill_action(authoring, owner, action, data):
    request = parse_skill_action(action, data)
    definition = request.definition
    target = authoring if definition.target == 'authoring' else authoring.workbench
    # Resolve a declared handler on every call, preserving live method binding.
    method = getattr(target, definition.handler)
    return method(owner, **request.arguments)
