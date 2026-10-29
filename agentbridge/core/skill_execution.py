"""Skill execution restrictions intersect business authorization; they never grant it."""
from __future__ import annotations

from copy import deepcopy


EFFECTS = ('read', 'reversible_write', 'controlled_write')


def effective_skill_execution(binding):
    """Read a fixed contract; legacy snapshots keep their original scope and hash."""
    manifest, profile = binding['snapshot']['manifest'], binding['profile']
    declared = manifest['profiles'][profile].get('execution')
    if declared is not None:
        return {**deepcopy(declared), 'legacy': False}
    return {'effects': ['read'] if profile == 'preview' else list(EFFECTS),
            'allow_batch': profile != 'single', 'capabilities': None, 'legacy': True}


def dependency_capabilities(requirements):
    """Default scope from minimum dependencies; explicit optional scope is separate."""
    from agentbridge.core.user_grants import PERMISSIONS
    permissions = set(requirements.get('all', []) + requirements.get('any', []))
    names = {name for permission in permissions for name in PERMISSIONS[permission]['capabilities']}
    # These public entries have contextual permission rules rather than a single
    # capability-to-permission mapping. Runtime checks remain authoritative.
    if 'oa.workflow.read' in permissions:
        names.add('oa.workflow.pending.batch.prepare')
    if permissions.intersection({'oa.addressbook.read', 'oa.addressbook.private.read'}):
        names.update({'oa.addressbook.group.list', 'oa.addressbook.group.members'})
    if any(p.startswith('oa.') and p.endswith(('.draft', '.submit')) for p in permissions):
        names.add('oa.template.list')
    database = requirements.get('database') or {}
    names.update(database.get('all', []) + database.get('any', []))
    return names


def normalize_profile_execution(service, execution_mode, profiles):
    """Resolve a public scope independently of minimum availability dependencies."""
    from agentbridge.core.skill_catalog import public_capability_catalog
    catalog = {item['name']: item for item in public_capability_catalog(service)}
    result = deepcopy(profiles)
    for profile, requirements in result.items():
        names = dependency_capabilities(requirements)
        eligible = {name for name in names if name in catalog}
        supplied = requirements.get('execution')
        if supplied is None:
            effects = (['read'] if execution_mode == 'read_exploration' or not names else
                       [effect for effect in EFFECTS if effect == 'read' or any(catalog[n]['effect'] == effect
                            and not catalog[n].get('batch', False) for n in eligible)])
            policy = {'effects': effects, 'allow_batch': False}
        else:
            policy = deepcopy(supplied)
            if len(policy['effects']) != len(set(policy['effects'])):
                raise ValueError('执行效果不能重复')
            if execution_mode == 'read_exploration' and policy['effects'] != ['read']:
                raise ValueError('只读探索仅允许 read 效果，请修正模式合同')
            if not names and (policy['effects'] != ['read'] or policy['allow_batch'] or policy.get('capabilities')):
                raise ValueError('纯文字模式没有业务依赖，不能声明业务调用或写入效果')
        explicit = policy.get('capabilities')
        if explicit is not None:
            business = bool(requirements.get('all') or requirements.get('any'))
            database = requirements.get('database') or {}
            database = bool(database.get('all') or database.get('any'))
            eligible = {name for name in catalog if (database if name.startswith('database.') else business)}
        allowed = {name for name in eligible if catalog[name]['effect'] in policy['effects']
                   and (policy['allow_batch'] or not catalog[name].get('batch', False))}
        if explicit is not None:
            if len(explicit) != len(set(explicit)) or any(name not in allowed for name in explicit):
                raise ValueError('模式能力范围必须属于已声明的业务或数据库类别，并符合效果与批量限制；不能包含内部提交能力')
            allowed = set(explicit)
        policy['effects'] = [effect for effect in EFFECTS if effect in policy['effects']]
        policy['capabilities'] = sorted(allowed)
        requirements['execution'] = policy
    return result


def validate_skill_execution(service, binding, capability):
    from agentbridge.core.business_skills import SkillRejected
    contract = effective_skill_execution(binding)
    if capability.startswith('database.'):
        from agentbridge.database.independent import CAPABILITIES
        if capability not in CAPABILITIES and capability != 'database.report.download':
            raise SkillRejected('SKILL_CAPABILITY_UNAVAILABLE', '业务助手能力不存在或已退役')
        effect, batch = 'read', False
    else:
        spec = service.registry.get(capability)
        effect, batch = spec.effect, spec.batch
    if effect not in contract['effects']:
        raise SkillRejected('SKILL_PREVIEW_ONLY', '本次业务助手模式仅允许预览或读取，不能准备业务写入')
    if batch and not contract['allow_batch']:
        raise SkillRejected('SKILL_SINGLE_ONLY', '本次业务助手模式不允许批量办理')
    allowed = contract['capabilities']
    if allowed is None:
        return  # Legacy scopes still intersect existing business authorization.
    if capability in allowed:
        return
    # Authorization and recovery refer to private commits. Only a registered
    # prepare/commit relationship may extend a fixed public contract internally.
    from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS
    if any(prepare in allowed and definition['commit_capability'] == capability
           and (contract['allow_batch'] or not service.registry.get(prepare).batch)
           for prepare, definition in _TRUSTED_WRITE_DEFINITIONS.items()):
        return
    from agentbridge.adapters.seeyon_pending_batch import PENDING_BATCH_PREPARE_CAPABILITY, PENDING_BATCH_TYPES
    if contract['allow_batch'] and PENDING_BATCH_PREPARE_CAPABILITY in allowed:
        from agentbridge.adapters.seeyon_pending_actions import PENDING_ACTION_CAPABILITY_DEFINITIONS
        from agentbridge.adapters.seeyon_missed_punch import MISSED_PUNCH_APPROVE_CAPABILITY
        from agentbridge.adapters.seeyon_meeting_reply import COMMIT as MEETING_REPLY_COMMIT
        batch_commits = {item['commit_capability'] for item in PENDING_ACTION_CAPABILITY_DEFINITIONS
                         if item['profile'] in PENDING_BATCH_TYPES}
        batch_commits.update({MISSED_PUNCH_APPROVE_CAPABILITY, MEETING_REPLY_COMMIT})
        if capability in batch_commits:
            # The controlled-write executor still verifies the authorized frozen
            # batch item and its exact commit. This does not expose single prepares.
            return
    if capability == 'database.report.download' and 'database.report.export' in allowed:

        return  # Download still requires the owner's current artifact receipt.
    raise SkillRejected('SKILL_CAPABILITY_NOT_ALLOWED', '该能力不在本次业务助手模式的执行范围内')
