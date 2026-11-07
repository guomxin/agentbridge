"""Validate two explicit local inputs before replaying a synthetic text candidate."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from agentbridge.core.business_skills import validate_skill_bundle
from agentbridge.core.skill_contracts import SkillProposal
from agentbridge.core.skill_quality import proposal_from_bundle, _standard_name


@dataclass
class Candidate:
    identity: dict
    bundle: dict
    proposal: dict
    cases: list
    input_hashes: dict

    def matches_copy(self, bundle):
        """Only a fixture ID and publication version may differ."""
        manifest = bundle['manifest']
        original = {**self.bundle['manifest'], 'id': manifest['id'], 'version': manifest['version']}
        return original == manifest and self.bundle['resources'] == bundle['resources']


def _read_json(path: Path, maximum):
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('请提供本地 JSON 文件的绝对路径')
    # Opening with NONBLOCK prevents an input FIFO from hanging before fstat.
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    with ExitStack() as stack:
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, directory)
        for component in path.parts[1:-1]:
            directory = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
            stack.callback(os.close, directory)
        fd = os.open(path.name, flags, dir_fd=directory)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > maximum:
            raise ValueError('候选输入必须是当前用户的常规文件，并符合大小限制')
        with open(fd, 'rb', closefd=False) as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError('候选输入超过大小限制')
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('候选 JSON 含重复字段')
        return result
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique), sha256(raw).hexdigest()


def _reference(path):
    return (isinstance(path, str) and path.startswith('references/') and path.endswith('.md')
            and str(PurePosixPath(path)) == path and '..' not in PurePosixPath(path).parts
            and not any(ch in path for ch in ('\\', ':', '\x00')))


def _check_material(value):
    text = json.dumps(value, ensure_ascii=False)
    if re.search(r'(?i)-----BEGIN .*PRIVATE KEY|\bBearer\s+[\w.+/=-]{16,}|(?:password|cookie|api_key)\s*[:=]\s*[\\"\x27]*[^\s"\x27]{8,}|'
                 r'https?://[^\s"<>]+/(?:input|authorize|auth)/[\w-]{16,}|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)', text):
        raise ValueError('候选输入含疑似凭据或个人信息，请先脱敏')


def _check_source(package, expected):
    if not isinstance(package, dict) or package.get('format') != 'agentbridge.skill-draft.v1':
        raise ValueError('候选需要 AgentBridge 原生 v1 草稿导出')
    source = package.get('source')
    if (not isinstance(source, dict) or set(source) - {'skill_id', 'revision', 'content_hash', 'manifest', 'standard_name'}
            or not {'skill_id', 'revision', 'content_hash', 'manifest'} <= set(source)
            or ('standard_name' in source and not _standard_name(source['standard_name']))):
        raise ValueError('此导出缺少可核验的来源，请重新导出草稿')
    if set(package) != {'format', 'source', 'proposal'}:
        raise ValueError('候选导出包含未知字段')
    if (type(expected.get('revision')) is not int or expected['revision'] < 1 or
            not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', expected.get('skill_id') or '') or
            not re.fullmatch(r'[a-f0-9]{64}', expected.get('content_hash') or '') or
            type(source['revision']) is not int or any(source[k] != v for k, v in expected.items())):
        raise ValueError('候选来源与单独提供的预期修订不一致')
    manifest, proposal = source['manifest'], package['proposal']
    if not isinstance(manifest, dict) or manifest.get('id') != expected['skill_id'] or manifest.get('version') != '0.0.0':
        raise ValueError('导出包含的原生草稿身份无效')
    _check_material(proposal)
    SkillProposal.model_validate(proposal)
    refs = proposal.get('references', {})
    if not all(_reference(path) for path in refs):
        raise ValueError('候选资料必须是 references 目录内的 Markdown 内容')
    bundle = validate_skill_bundle(manifest, {'SKILL.md': proposal.get('instructions'), **refs})
    if bundle['content_hash'] != expected['content_hash'] or proposal_from_bundle(bundle) != proposal:
        raise ValueError('导出内容或原始资料顺序与固定修订不一致')
    if manifest['executionMode'] != 'read_exploration' or any(
            p.get('all') or p.get('any') or 'database' in p or
            p.get('execution') != {'effects': ['read'], 'allow_batch': False, 'capabilities': []}
            for p in manifest['profiles'].values()):
        raise ValueError('候选回放仅支持显式纯文字只读模式，不能含业务、数据库、批量或写入能力')
    # Native storage sorts object keys; preserve the frozen array order when
    # saving the isolated copy, including nonalphabetic reference declarations.
    return bundle, {**proposal, 'references': {p: refs[p] for p in manifest['resources']}}


def _strings(value, *, max_count, max_length):
    if not isinstance(value, list) or len(value) > max_count or any(
            not isinstance(x, str) or not x.strip() or len(x) > max_length for x in value):
        raise ValueError('候选题目检查项必须是有界非空文字列表')
    return value


def _cases(package, bundle):
    if (not isinstance(package, dict) or set(package) != {'format', 'synthetic_redacted', 'cases'}
            or package['format'] != 'agentbridge.skill-host-cases.v1' or package['synthetic_redacted'] is not True
            or not isinstance(package['cases'], list) or not 2 <= len(package['cases']) <= 25):
        raise ValueError('请提供 2 至 25 道题，并明确候选及题目均为已脱敏的合成材料')
    _check_material(package['cases'])
    result, prompts = {}, set()
    manifest = bundle['manifest']
    for row in package['cases']:
        required_fields = {'id', 'prompt', 'profile', 'manual'}
        if (not isinstance(row, dict) or not required_fields <= set(row)
                or set(row) - required_fields - {'contains', 'excludes', 'forbidden_resources', 'optional_resource', 'paraphrase_of'}):
            raise ValueError('候选题字段无效；账号和工具范围由夹具固定')
        case_id, profile = row['id'], row['profile']
        if not isinstance(case_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', case_id) or case_id in result:
            raise ValueError('候选题 ID 无效或重复')
        _strings([row['prompt']], max_count=1, max_length=3000)
        _strings([row['manual']], max_count=1, max_length=1000)
        normalized = re.sub(r'\W+', '', row['prompt'].casefold())
        if not normalized or normalized in prompts:
            raise ValueError('候选题需要不同的自然问法')
        prompts.add(normalized)
        for field in ('contains', 'excludes'):
            _strings(row.get(field, []), max_count=12, max_length=300)
        if profile is not None and (not isinstance(profile, str) or profile not in manifest['profiles']):
            raise ValueError('候选题指定了不存在的模式')
        required = ['SKILL.md', *manifest['required_resources'][profile]] if profile is not None else []
        forbidden = _strings(row.get('forbidden_resources', []), max_count=16, max_length=200)
        if not set(forbidden) <= set(manifest['resources']) or set(forbidden).intersection(required):
            raise ValueError('禁止资料与候选清单或该模式必读资料冲突')
        if 'optional_resource' in row:
            optional = row['optional_resource']
            if profile is None or optional not in manifest['resources'] or optional in required or optional in forbidden:
                raise ValueError('可选资料与候选清单或该模式要求冲突')
        if 'paraphrase_of' in row and not isinstance(row['paraphrase_of'], str):
            raise ValueError('留出题须引用一个基准题 ID')
        result[case_id] = {**row, 'skill': 'candidate' if profile is not None else None,
                           'no_read': True, 'required_resources': required}
    holdouts = [row for row in result.values() if 'paraphrase_of' in row]
    if not any(row['profile'] is not None for row in result.values()) or not any(row['profile'] is not None for row in holdouts):
        raise ValueError('需要至少一道候选正例及其独立留出改写')
    for row in holdouts:
        base = result.get(row['paraphrase_of'])
        if not base or 'paraphrase_of' in base or base['profile'] != row['profile']:
            raise ValueError('留出改写必须引用同一预期模式的独立基准题')
        phrase = re.sub(r'\W+', '', row['prompt'].casefold())
        source_text = [*bundle['resources'].values(), *manifest['selection'].values(),
                       manifest['name'], manifest['description'], *(p.get('description', '') for p in manifest['profiles'].values())]
        if any(phrase in re.sub(r'\W+', '', text.casefold()) for text in source_text):
            raise ValueError('留出改写不能照抄候选方法或资料中已有的原句')
    return list(result.values())


def load_candidate(export_path, cases_path, expected):
    package, export_hash = _read_json(export_path, 256 * 1024)
    cases, cases_hash = _read_json(cases_path, 64 * 1024)
    bundle, proposal = _check_source(package, expected)
    return Candidate(dict(expected), bundle, proposal, _cases(cases, bundle),
                     {'export_sha256': export_hash, 'cases_sha256': cases_hash})
