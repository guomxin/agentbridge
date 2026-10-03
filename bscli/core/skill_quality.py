"""Pure skill contracts, diagnostics and deterministic evaluation helpers.

No model output is executable and no diagnostic claims semantic correctness.
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SkillSelection(StrictModel):
    use_when: str = Field(min_length=1, max_length=500)
    not_for: str = Field(min_length=1, max_length=500)
    output: str = Field(min_length=1, max_length=500)


class SkillDatabaseDependencies(StrictModel):
    all: list[str] = Field(default_factory=list)
    any: list[str] = Field(default_factory=list)


class SkillDependencies(SkillDatabaseDependencies):
    database: SkillDatabaseDependencies | None = None


class SkillMethod(StrictModel):
    inputs: list[str] = Field(default_factory=list, max_length=20)
    steps: list[str] = Field(default_factory=list, max_length=30)
    exceptions: list[str] = Field(default_factory=list, max_length=20)
    acceptance: list[str] = Field(default_factory=list, max_length=20)


class SkillProposal(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    selection: SkillSelection
    instructions: str = Field(min_length=1, max_length=40000)
    profiles: dict[str, SkillDependencies] = Field(default_factory=lambda: {'use': SkillDependencies()})
    executionMode: Literal['read_exploration', 'durable_plan', 'controlled_action'] = 'read_exploration'
    references: dict[str, str] = Field(default_factory=dict)
    required_resources: dict[str, list[str]] | None = None
    method: SkillMethod | None = None


class SkillAuthoringData(StrictModel):
    """Legacy action envelope with discoverable, typed fields; dispatcher checks action shape."""
    proposal: SkillProposal | None = None
    draft_id: str | None = None
    expected_revision: int | None = None
    request_key: str | None = None
    provenance: dict | None = None
    profile: str | None = None
    prompt: str | None = None
    test_id: str | None = None
    output: str | None = None
    reason: str | None = None
    audience: list[str] | None = None
    profiles: list[str] | None = None
    request_id: str | None = None
    value: dict | None = None
    target_revision: int | None = None
    material: str | None = None
    scope: str | None = None
    automatic: bool | None = None
    task_ids: list[str] | None = None
    enabled: bool | None = None
    cases: list[dict] | None = None
    repeats: int | None = None
    job_id: str | None = None
    package: dict | None = None
    skill_id: str | None = None
    rating: str | None = None
    comment: str | None = None
    version: str | None = None
    binding_id: str | None = None
    path: str | None = None
    query: str | None = None
    limit: int | None = None
    steps: list[dict] | None = None


def proposal_from_bundle(bundle):
    m = bundle['manifest']
    return {**{k: m[k] for k in ('name', 'description', 'selection', 'profiles', 'executionMode', 'required_resources')},
            'instructions': bundle['resources']['SKILL.md'],
            'references': {k: v for k, v in bundle['resources'].items() if k != 'SKILL.md'}}


def diagnostics(bundle):
    body = bundle['resources']['SKILL.md']
    checks = []
    for code, pattern, label in [
        ('inputs', r'输入|材料|input', '说明需要的输入和材料'),
        ('steps', r'步骤|流程|step|^\s*\d+[.、]', '说明处理步骤'),
        ('exceptions', r'不足|缺失|失败|异常|无结果|空结果|取消|unknown|error', '说明输入缺失和异常处理'),
        ('acceptance', r'核对|校验|验收|依据|验证|检查|verify|check', '说明结果核验方式'),
    ]:
        checks.append({'code': code, 'level': 'info' if re.search(pattern, body, re.I | re.M) else 'warning', 'message': label})
    if len(body) > 12000:
        checks.append({'code': 'length', 'level': 'warning', 'message': '主说明较长，建议将可选细节移入参考资料'})
    if re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)', json.dumps(bundle, ensure_ascii=False)):
        checks.append({'code': 'personal_data', 'level': 'warning', 'message': '疑似包含邮箱或手机号，请复核脱敏；检测不能覆盖所有业务隐私'})
    return {'checks': checks, 'verification': 'static_only', 'message': '静态检查不证明方法正确，也不替代人工脱敏复核'}


def bundle_diff(before, after):
    changes = []
    old = before or {'manifest': {}, 'resources': {}}
    for key in ('name', 'description', 'selection', 'profiles', 'executionMode', 'required_resources'):
        a, b = old['manifest'].get(key), after['manifest'].get(key)
        if a != b:
            changes.append({'field': key, 'before': a, 'after': b})
    for key in sorted(set(old['resources']) | set(after['resources'])):
        a, b = old['resources'].get(key, ''), after['resources'].get(key, '')
        if a != b:
            changes.append({'field': key, 'diff': '\n'.join(difflib.unified_diff(a.splitlines(), b.splitlines(), fromfile='线上', tofile='候选', lineterm=''))})
    return changes


def normalized_words(value):
    value = re.sub(r'\s+', '', value.lower())
    return {value[i:i+2] for i in range(max(len(value)-1, 0))}


def similarity(a, b):
    left, right = normalized_words(a), normalized_words(b)
    return len(left & right) / max(len(left | right), 1)


def validate_cases(cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 12:
        raise ValueError('评测需要 1 至 12 个样本')
    result = []
    for case in cases:
        if not isinstance(case, dict) or set(case) - {'prompt', 'profile', 'kind', 'contains', 'excludes', 'expected_trigger'}:
            raise ValueError('评测样本字段无效')
        prompt = case.get('prompt')
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 3000:
            raise ValueError('评测问题需要 1 至 3000 字符')
        kind = case.get('kind', 'output')
        if kind not in {'output', 'trigger'}:
            raise ValueError('评测类型无效')
        row = {'prompt': prompt, 'profile': case.get('profile', 'use'), 'kind': kind}
        for key in ('contains', 'excludes'):
            values = case.get(key, [])
            if not isinstance(values, list) or len(values) > 20 or any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in values):
                raise ValueError('断言必须为不超过 20 项的非空短文本')
            row[key] = values
        if kind == 'trigger':
            if type(case.get('expected_trigger')) is not bool:
                raise ValueError('触发样本需要 expected_trigger 布尔值')
            row['expected_trigger'] = case['expected_trigger']
        elif not row['contains'] and not row['excludes']:
            raise ValueError('结果评测至少需要一条包含或排除断言')
        result.append(row)
    return result


def grade_output(case, output):
    if case['kind'] == 'trigger':
        actual = output.strip().lower()
        passed = actual == ('true' if case['expected_trigger'] else 'false')
        return {'passed': passed, 'checks': [{'rule': 'expected_trigger', 'passed': passed}]}
    checks = [{'rule': 'contains', 'value': v, 'passed': v in output} for v in case['contains']]
    checks += [{'rule': 'excludes', 'value': v, 'passed': v not in output} for v in case['excludes']]
    return {'passed': bool(checks) and all(c['passed'] for c in checks), 'checks': checks}


def export_standard(bundle):
    m = bundle['manifest']
    header = '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in [('name', m['id']), ('description', m['description'])])
    return {'format': 'agentskills.files.v1', 'files': {**bundle['resources'], 'SKILL.md': f'---\n{header}\n---\n\n{bundle["resources"]["SKILL.md"]}'}}


def import_standard(package):
    # Deliberately accept a documented YAML scalar subset, never YAML object tags.
    if not isinstance(package, dict) or package.get('format') != 'agentskills.files.v1':
        raise ValueError('需要 agentskills.files.v1 文件映射')
    files = package.get('files')
    if not isinstance(files, dict) or not 1 <= len(files) <= 16 or any(not isinstance(k, str) or not isinstance(v, str) for k, v in files.items()):
        raise ValueError('Skill 文件映射无效')
    if sum(len(v) for v in files.values()) > 48000:
        raise ValueError('Skill 包过大')
    source = files.get('SKILL.md', '')
    match = re.match(r'\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z', source, re.S)
    if not match:
        raise ValueError('SKILL.md 缺少 YAML 元数据')
    meta = {}
    for line in match[1].splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        key, sep, value = line.partition(':')
        if not sep or key not in {'name', 'description', 'license', 'compatibility'} or key in meta:
            raise ValueError('当前导入仅支持 name/description/license/compatibility 标量元数据')
        value = value.strip()
        if value.startswith('"'):
            try: value = json.loads(value)
            except ValueError as exc: raise ValueError('元数据引号格式无效') from exc
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        elif any(c in value for c in ('!', '&', '*', '|', '>')):
            raise ValueError('请将 YAML 元数据转换为单行字符串')
        if not isinstance(value, str): raise ValueError('元数据必须是字符串')
        meta[key] = value
    name = meta.get('name', '')
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 64:
        raise ValueError('标准名称必须是最多 64 字符的小写字母数字及连字符')
    desc = meta.get('description', '')
    if not desc or len(desc) > 500:
        raise ValueError('当前平台简介需为 1 至 500 字符')
    return {'name': name, 'description': desc, 'selection': {'use_when': desc, 'not_for': '未声明的业务操作与外部能力', 'output': '按照方法说明输出，发布前复核'},
            'instructions': match[2], 'references': {k: v for k, v in files.items() if k != 'SKILL.md'}, 'profiles': {'use': {}}}
