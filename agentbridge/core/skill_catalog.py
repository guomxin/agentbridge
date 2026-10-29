"""Public capability contracts for skill editing and bounded generation context.

This describes supported methods, never the caller's grants or live business
data. The runtime still checks the current identity, source and authorization.
"""
from __future__ import annotations

import json

from agentbridge.core.skill_ranking import _grams
from agentbridge.core.user_grants import PERMISSIONS

# Public capability names are English while users usually describe Chinese
# tasks. These search aliases rank context only; they never authorize actions.
_DOMAIN_TERMS = {'pending': ('待办',), 'workflow': ('流程',), 'work_log': ('日志',),
                 'meeting': ('会议',), 'meeting_room': ('会议室',), 'business_trip': ('出差',),
                 'leave': ('请假',), 'missed_punch': ('补签',), 'addressbook': ('通讯录',),
                 'certificate': ('证照',), 'project': ('项目',), 'alarm': ('告警',),
                 'energy': ('能耗',), 'inspection': ('巡检',), 'maintenance': ('维修',),
                 'yuque': ('语雀',), 'database': ('数据库',), 'logs': ('日志',), 'comments': ('评论',)}


def public_capability_catalog(service):
    scopes = set()
    for spec in service.registry.list():
        try:
            scopes.update(service.planning_required_scopes(spec.name))
        except KeyError:
            continue  # Internal capabilities without a public planning policy.
    result = []
    for item in service.planning_catalog(granted_scopes=scopes)['capabilities']:
        spec = service.registry.get(item['name'])
        write = bool(item.get('mayRequireTrustedInteraction'))
        result.append({
            'name': item['name'], 'description': item['description'], 'effect': item['effect'],
            'inputSchema': item['inputSchema'], 'outputSchema': item['outputSchema'],
            'batch': bool(getattr(spec, 'batch', False)),
            'preconditions': {
                'requiredScopes': item['requiredScopes'],
                'planningDescriptor': item.get('planningDescriptor'),
                'trustedInteraction': write,
                'instruction': ('先准备可信交互，再由用户授权，中心提交并核验；结果未知不重放。' if write
                                else '执行时核对当前身份和业务权限；读取失败不能当作空结果。'),
            },
        })
    from agentbridge.database.independent import CAPABILITIES, INPUT_SCHEMAS
    for name, description in CAPABILITIES.items():
        result.append({'name': name, 'description': description, 'effect': 'read', 'batch': False,
                       'inputSchema': INPUT_SCHEMAS.get(name), 'outputSchema': None,
                       'preconditions': {'sourceRequired': True,
                           'instruction': '执行时发现获准数据源并读取单项能力合同；目录不代表授权。未声明固定输出 schema，以实际结构化响应为准。'}})
    return sorted(result, key=lambda item: item['name'])


def authoring_catalog(service):
    capabilities = public_capability_catalog(service)
    names = {item['name'] for item in capabilities}
    from agentbridge.database.independent import CAPABILITIES
    return {'schema': 'agentbridge.skill-authoring-catalog.v1',
            'business': [{'id': key, 'label': value.get('label', key),
                          'capabilities': [name for name in value.get('capabilities', []) if name in names]}
                         for key, value in PERMISSIONS.items()],
            'database': [{'name': name, 'description': description} for name, description in CAPABILITIES.items()],
            'capabilities': [{key: item[key] for key in ('name', 'description', 'effect', 'batch')} for item in capabilities],
            'message': '此目录用于编写方法，不代表当前账号获准执行；保存和发布不会增加业务权限。'}


def generation_catalog(service, material, prior=None):
    """Select complete relevant contracts within a small, explicit prompt budget."""
    catalog = public_capability_catalog(service)
    explicit = set()
    for profile in (prior or {}).get('profiles', {}).values():
        for permission in [*profile.get('all', []), *profile.get('any', [])]:
            explicit.update(PERMISSIONS.get(permission, {}).get('capabilities', []))
        db = profile.get('database', {})
        explicit.update([*db.get('all', []), *db.get('any', [])])
        explicit.update(profile.get('execution', {}).get('capabilities', []))
    query = str(material).lower()
    grams = _grams(query)
    def score(item):
        labels = ' '.join(value.get('label', '') for value in PERMISSIONS.values() if item['name'] in value.get('capabilities', []))
        alias_matches = sum(term in query for part in item['name'].split('.') for term in _DOMAIN_TERMS.get(part, ()))
        return (item['name'] in explicit or item['name'].lower() in query,
                alias_matches * 3 + len(grams & _grams(item['description'] + ' ' + labels)))
    ranked = sorted(catalog, key=score, reverse=True)
    selected, omitted, used = [], [], 0
    for item in ranked:
        priority, relevance = score(item)
        if not priority and relevance < 2:
            continue
        length = len(json.dumps(item, ensure_ascii=False, separators=(',', ':')))
        if len(selected) >= 6 or used + length > 20000:
            omitted.append(item['name'])
            continue
        selected.append(item); used += length
    public = {item['name'] for item in catalog}
    return {'schema': 'agentbridge.skill-generation-catalog.v1',
            'business': {key: {'label': value.get('label', key),
                             'capabilities': [name for name in value.get('capabilities', []) if name in public]}
                         for key, value in PERMISSIONS.items()},
            'database': [item['name'] for item in catalog if item['name'].startswith('database.')],
            'contracts': selected, 'omitted_contracts': omitted,
            'limitations': '只提供相关的公开合同，省略的合同不表示不支持；缺参数依据时明确待核对，不编造参数。无业务需求使用纯文字方法。'}
