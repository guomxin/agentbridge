"""Pure skill diagnostics and deterministic evaluation helpers.

No model output is executable and no diagnostic claims semantic correctness.
"""
from __future__ import annotations

import difflib
import json
import re

# Preserve existing imports while keeping contracts separate from quality helpers.
from agentbridge.core.skill_contracts import (
    SkillAuthoringData, SkillDatabaseDependencies, SkillDependencies,
    SkillMethod, SkillProposal, SkillSelection, StrictModel,
)


def proposal_from_bundle(bundle):
    m = bundle['manifest']
    return {**{k: m[k] for k in ('name', 'description', 'selection', 'profiles', 'executionMode', 'required_resources')},
            **({'standard_metadata': m['standard_metadata']} if 'standard_metadata' in m else {}),
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
    for key in ('name', 'description', 'selection', 'profiles', 'executionMode', 'required_resources', 'standard_metadata'):
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
        if not isinstance(case, dict) or set(case) - {'prompt', 'profile', 'kind', 'contains', 'excludes', 'expected_trigger', 'feedback_id'}:
            raise ValueError('评测样本字段无效')
        prompt = case.get('prompt')
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 3000:
            raise ValueError('评测问题需要 1 至 3000 字符')
        feedback_id = case.get('feedback_id')
        if feedback_id is not None and (not isinstance(feedback_id, str) or not re.fullmatch(r'[0-9a-f-]{36}', feedback_id)):
            raise ValueError('反馈来源标识无效')
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
        if feedback_id is not None: row['feedback_id'] = feedback_id
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


def validate_pending_cases(value, profiles):
    """Bound an unfinished editor draft without treating it as runnable evidence."""
    if not isinstance(value, dict) or set(value) != {'cases', 'repeats'}:
        raise ValueError('待测题需要题目及重复次数')
    if not isinstance(value['repeats'], str) or value['repeats'] not in {'1', '2', '3'}:
        raise ValueError('重复次数为 1 至 3')
    cases = value['cases']
    if not isinstance(cases, list) or len(cases) > 12:
        raise ValueError('最多保存 12 个待测题')
    result = []
    required = {'prompt', 'profile', 'kind', 'contains', 'excludes'}
    for case in cases:
        if not isinstance(case, dict) or set(case) - (required | {'feedback_id'}) or not required <= set(case):
            raise ValueError('待测题字段无效')
        for key, limit in [('prompt',3000), ('profile',128), ('kind',32), ('contains',10020), ('excludes',10020)]:
            if not isinstance(case[key], str) or len(case[key]) > limit:
                raise ValueError('待测题字段过长或不是文本')
        if case['profile'] and case['profile'] not in profiles:
            raise ValueError('请重新选择当前修订的模式')
        if case['kind'] not in {'output','trigger_true','trigger_false'}:
            raise ValueError('待测题类型无效')
        feedback_id = case.get('feedback_id')
        if 'feedback_id' in case and (not isinstance(feedback_id, str) or not re.fullmatch(r'[0-9a-f-]{36}',feedback_id)):
            raise ValueError('反馈来源标识无效')
        result.append(dict(case))
    if len(json.dumps(result, ensure_ascii=False)) > 48000:
        raise ValueError('待测题总内容过长，请缩短后保存')
    return {'cases': result, 'repeats': value['repeats']}


EVALUATION_SCHEMA = 'agentbridge.skill-evaluation.v2'


def _candidate_case_results(report, cases):
    """Require the complete candidate matrix, independently of score summaries."""
    repeats, rows = report.get('repeats'), report.get('rows')
    if type(repeats) is not int or not 1 <= repeats <= 3 or not isinstance(rows, list):
        raise ValueError('评测行或重复次数不完整')
    expected = {(index, repeat) for index in range(len(cases)) for repeat in range(repeats)}
    found = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('评测行无效')
        if row.get('variant') != 'candidate':
            continue
        index, repeat = row.get('case'), row.get('repeat')
        if type(index) is not int or type(repeat) is not int:
            raise ValueError('评测行缺少样例或重复序号')
        key = (index, repeat)
        if key not in expected or key in found or row.get('skipped'):
            raise ValueError('评测行重复、越界或未完成')
        output = row.get('output')
        if not isinstance(output, str) or not output.strip() or type(row.get('passed')) is not bool:
            raise ValueError('评测行缺少有效输出')
        passed = grade_output(cases[index], output)['passed']
        if passed != row['passed']:
            raise ValueError('评测行与输出断言不一致')
        found[key] = passed
    if set(found) != expected:
        raise ValueError('评测行未覆盖全部样例与重复次数')
    return [all(found[index, repeat] for repeat in range(repeats)) for index in range(len(cases))]


def publication_evidence(bundle, revision, tested_profiles, report):
    """One publication rule for inspection, submission, and frozen review evidence.

    Only the latest complete report is considered; reports are never combined.
    New output cases belong to their profile. Trigger selection is a shared gate.
    Legacy reports retain their original all-or-nothing evaluation semantics.
    """
    names = list(bundle['manifest']['profiles'])
    samples = set(tested_profiles)
    cases, results = [], []
    reason = None
    legacy = report is not None and report.get('schema') != EVALUATION_SCHEMA
    if report is not None:
        if report.get('revision') != revision or report.get('content_hash') != bundle['content_hash']:
            reason = '独立评测的修订或内容不匹配，请重新评测。'
        else:
            try:
                cases = validate_cases(report.get('cases'))
                if any(c['profile'] not in names for c in cases):
                    raise ValueError('评测模式不存在')
                if legacy:
                    if report.get('passed') is not True:
                        reason = '旧版独立评测未整体通过，请重新评测。'
                    results = [report.get('passed') is True] * len(cases)
                else:
                    results = _candidate_case_results(report, cases)
            except (ValueError, TypeError):
                reason = '独立评测证据不完整或不一致，请重新评测。'
    shared_indexes = [i for i, case in enumerate(cases) if case['kind'] == 'trigger']
    if not reason and any(not results[i] for i in shared_indexes):
        reason = '共享的助手选择评测未通过，所有模式均需重新核对。'
    profiles = []
    for profile in names:
        indexes = [i for i, case in enumerate(cases) if case['kind'] == 'output' and case['profile'] == profile]
        blocked = bool(reason) or any(not results[i] for i in indexes)
        passed = bool(indexes) and not blocked
        source = 'evaluation' if passed else 'sample' if profile in samples else 'none'
        evidence = (reason or '本模式输出断言未通过，请修订或重新评测。') if blocked else (
            '本修订本模式输出断言通过，语义待人工复核。' if passed else
            '本修订已有样例，待人工复核。' if source == 'sample' else '本修订尚无本模式输出样例。')
        profiles.append({'profile': profile, 'passed': passed, 'blocked': blocked,
                         'eligible': not blocked and source != 'none', 'source': source,
                         'evidence': evidence, 'case_indexes': indexes})
    return {'schema': 'agentbridge.skill-publication-evidence.v1', 'revision': revision,
            'content_hash': bundle['content_hash'], 'report_job_id': report.get('job_id') if report else None,
            'basis': 'latest_complete_report_only',
            'shared': {'passed': not bool(reason), 'blocked': bool(reason), 'reason': reason,
                       'case_indexes': shared_indexes}, 'profiles': profiles}


def candidate_review_problems(report, profiles):
    """Select unresolved candidate notes from one frozen report, never its controls."""
    if not report or not isinstance(report.get('reviews'), dict):
        return []
    cases, found = report.get('cases', []), []
    for row in report.get('rows', []):
        index = row.get('case')
        if row.get('variant') != 'candidate' or type(index) is not int or not 0 <= index < len(cases):
            continue
        case = cases[index]
        if case.get('kind', 'output') != 'trigger' and case.get('profile', 'use') not in profiles:
            continue
        review = report['reviews'].get(row.get('key'), {})
        if review.get('status') == 'problem':
            found.append({'row_key': row['key'], 'profile': case.get('profile', 'use'),
                          'kind': case.get('kind', 'output'), 'case': index, 'repeat': row.get('repeat'),
                          'note': review.get('note', ''), 'review_version': review.get('version')})
    return found


def standard_document(source, *, required=False):
    """Read the supported scalar headers and remove repeated pasted headers safely."""
    metadata = {}
    body = source
    while body.startswith(('---\n', '---\r\n')):
        match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)(.*)\Z', body, re.S)
        probe = match[1] if match else body
        if (not required or metadata) and not re.search(r'^(?:name|description|license|compatibility):', probe, re.M):
            break  # A leading Markdown horizontal rule is not a metadata header.
        if not match:
            raise ValueError('SKILL.md 缺少完整 YAML 元数据')
        current = {}
        for line in match[1].splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            key, sep, value = line.partition(':')
            if not sep or key not in {'name', 'description', 'license', 'compatibility'} or key in current:
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
            current[key] = value
        for key in ('license', 'compatibility'):
            if key in current and key in metadata and current[key] != metadata[key]:
                raise ValueError('重复 YAML 元数据内容冲突：' + key)
        # The first title describes the imported package; subsequent pasted
        # headers cannot replace it. Declarative metadata is never discarded.
        metadata = {**current, **metadata}
        body = match[2].lstrip('\r\n')
    if required and not metadata:
        raise ValueError('SKILL.md 缺少 YAML 元数据')
    return metadata, body


def normalize_standard_metadata(value):
    from agentbridge.core.skill_contracts import SkillStandardMetadata
    from pydantic import ValidationError
    try:
        normalized = SkillStandardMetadata.model_validate(value).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise ValueError('标准元数据仅支持 license（最多1000字符）和 compatibility（最多500字符）字符串') from exc
    if any(not v.strip() for v in normalized.values()):
        raise ValueError('标准元数据不能为空白')
    return normalized


def export_standard(bundle):
    m = bundle['manifest']
    embedded, body = standard_document(bundle['resources']['SKILL.md'])
    metadata = {k: embedded[k] for k in ('license', 'compatibility') if k in embedded}
    declared = normalize_standard_metadata(m.get('standard_metadata', {}))
    if any(k in metadata and metadata[k] != v for k, v in declared.items()):
        raise ValueError('正文与标准元数据冲突，请先修正草稿')
    metadata.update(declared)
    metadata = normalize_standard_metadata(metadata)
    header = '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in [
        ('name', m['id']), ('description', m['description']), *metadata.items()])
    return {'format': 'agentskills.files.v1', 'files': {**bundle['resources'], 'SKILL.md': f'---\n{header}\n---\n\n{body}'}}


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
    meta, body = standard_document(source, required=True)
    name = meta.get('name', '')
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 64:
        raise ValueError('标准名称必须是最多 64 字符的小写字母数字及连字符')
    desc = meta.get('description', '')
    if not desc or len(desc) > 500:
        raise ValueError('当前平台简介需为 1 至 500 字符')
    metadata = normalize_standard_metadata({k: meta[k] for k in ('license', 'compatibility') if k in meta})
    return {'name': name, 'description': desc, 'selection': {'use_when': desc, 'not_for': '未声明的业务操作与外部能力', 'output': '按照方法说明输出，发布前复核'},
            'instructions': body, 'references': {k: v for k, v in files.items() if k != 'SKILL.md'}, 'profiles': {'use': {}},
            **({'standard_metadata': metadata} if metadata else {})}


def case_fingerprint(cases, repeats):
    from hashlib import sha256
    rows = [{k: v for k, v in row.items() if k != 'feedback_id'} for row in cases]
    return 'sha256:' + sha256(json.dumps({'cases': rows, 'repeats': repeats}, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def compare_reports(source, current):
    """Compare fixed questions and aligned candidate rows, never infer environment equality."""
    if not source or not current:
        return None
    result = {'source_job_id': source.get('job_id'), 'source_revision': source.get('revision'),
              'same_report': bool(source.get('job_id') and source.get('job_id') == current.get('job_id')),
              'inputs_equal': False, 'models': 'unknown', 'regressed_cases': [],
              'limitations': '仅比较题目、断言、重复次数及候选输出记录的模型；不证明运行环境完全一致。'}
    try:
        old_cases, new_cases = validate_cases(source.get('cases')), validate_cases(current.get('cases'))
        result['inputs_equal'] = case_fingerprint(old_cases, source['repeats']) == case_fingerprint(new_cases, current['repeats'])
        if not result['inputs_equal']:
            return result
        old_passed = _candidate_case_results(source, old_cases)
        new_passed = _candidate_case_results(current, new_cases)
        def models(report):
            values = {(r['case'], r['repeat']): r.get('model') for r in report['rows'] if r['variant'] == 'candidate'}
            return values if all(isinstance(v, str) and v.strip() for v in values.values()) else None
        old_models, new_models = models(source), models(current)
        if old_models is not None and new_models is not None:
            result['models'] = 'known_same' if old_models == new_models else 'changed'
        if result['models'] == 'known_same' and not result['same_report']:
            result['regressed_cases'] = [i for i, passed in enumerate(old_passed) if passed and not new_passed[i]]
    except (ValueError, KeyError, TypeError):
        # Older/incomplete reports remain readable, without claiming comparability.
        pass
    return result
