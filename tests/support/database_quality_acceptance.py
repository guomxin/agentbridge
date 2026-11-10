"""Opt-in real-host, synthetic-source log analysis blind acceptance.

The oracle stays in the controller process. The model receives only the question,
scope, ordinary skill contracts and evidence obtained through real MCP tools.
PostgreSQL I/O is synthetic; authorization, binding, query validation, cursors,
evidence fragmentation and the OpenClaw grant/bind/chat path remain real.
This harness never scores prose or fills reviewer decisions automatically.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from unittest.mock import patch
from uuid import uuid4

from agentbridge.core.business_skills import SkillRegistry
from agentbridge.database.content import add_evidence, compile_query
from agentbridge.database.evidence import prepare_evidence_page
from agentbridge.database.independent import DatabaseRejected, IndependentDatabase
from tests.database_fixtures import configured_source
from tests.support.interaction_acceptance import write_metadata
from tests.support.skill_host_acceptance import (
    ALICE, BOB, ROOT, SkillHostAcceptance, private_text,
)

SOURCE = 'quality_fixture'
CAPABILITIES = ['database.directory', 'database.logs.query', 'database.logs.content_analyze']
SKILLS = ('log-review', 'issue-tracking', 'similar-cases')
CASE_IDS = ('same-name', 'empty', 'long-text', 'pagination', 'adjacent-irrelevant',
            'progress-not-completion', 'time-window')
CASES_PATH = ROOT / 'tests/fixtures/analysis_quality.json'
TRANSPORT = {'after', 'page_size', 'max_chars', 'text_offset', 'expected_revision',
             'evidence_format', 'include_diagnostics', 'mode', 'order'}
TOOLS = ['agentbridge_identity_status', 'agentbridge_skill_catalog', 'agentbridge_skill_get',
         'agentbridge_skill_authoring', 'database_capabilities', 'database_execute']


def load_blind_cases(path=CASES_PATH):
    """Project an oracle into a strict allowlist; do not pass labels to the host."""
    package = json.loads(Path(path).read_text(encoding='utf-8'))
    cases = package['cases']
    if tuple(case['id'] for case in cases) != CASE_IDS:
        raise ValueError('This baseline requires the seven frozen case IDs in order')
    result = []
    for case in cases:
        rows = []
        for index, source in enumerate(case['sources']):
            paragraphs = list(source['paragraphs'])
            if paragraphs:
                paragraphs[0] *= source.get('repeat_first', 1)
            departments = case['scope'].get('department_ids', [case['scope'].get('department_id', '3')])
            rows.append({'id': int(source['id']), 'content': '\n'.join(paragraphs),
                         'log_date': case['scope'].get('start_date', '2026-09-01'),
                         'type_code': 'DAILY', 'user_id': '1', 'fullname': '合成来源作者',
                         'username': 'synthetic', 'department_id': str(departments[index % len(departments)]),
                         'current_department': '合成部门', 'project_id': None, 'project': None,
                         'hours': None, 'status': None})
        result.append({'id': case['id'], 'question': case['question'],
                       'scope': deepcopy(case['scope']), 'rows': rows})
    return result


def validate_selection(selected_ids, variant='standard'):
    if (not isinstance(selected_ids, (tuple, list)) or not selected_ids or
            any(not isinstance(value, str) or value not in CASE_IDS for value in selected_ids) or
            len(selected_ids) != len(set(selected_ids))):
        raise ValueError('Select nonempty, distinct known case IDs')
    if variant not in {'standard', 'paraphrase'}:
        raise ValueError('Unknown prompt variant')
    if variant == 'paraphrase' and tuple(selected_ids) != ('pagination',):
        raise ValueError('The paraphrase variant supports only --cases pagination')
    return tuple(selected_ids)


def parse_case_ids(value):
    return validate_selection(tuple(part.strip() for part in value.split(',')))


def select_blind_cases(cases, selected_ids, variant='standard'):
    selected = validate_selection(selected_ids, variant)
    by_id = {case['id']: case for case in cases}
    return [deepcopy(by_id[case_id]) for case_id in selected]


def question_prompt(case, variant='standard'):
    # Do not add expected states, paragraph numbers, item IDs or exclusion labels.
    validate_selection((case['id'],), variant)
    if variant == 'paraphrase':
        return '把甲部门、乙部门及其下级2026年9月1日至7日的工作进展一起整理一下。'
    return {
        'same-name': '汇总甲部门2026年9月1日至2日甲、乙项目的接口联调进展。',
        'empty': '2026年9月1日有哪些完成事项？',
        'long-text': '读完1号日志，汇总所有事项并说明是否完成。',
        'pagination': '汇总甲、乙部门及下级2026年9月1日至7日的进展。',
        'adjacent-irrelevant': '查找2026年9月1日至7日APP登录失败的处理案例。',
        'progress-not-completion': '看2026年9月1日日志里联系厂家处理的事项，完成了吗？',
        'time-window': '看看2026年9月1日至2日的材料验收，完成了吗？',
    }[case['id']]


def department_ids(arguments):
    raw = arguments.get('department_ids', [arguments['department_id']] if 'department_id' in arguments else [])
    return {str(int(value)) for value in raw}


def validate_scope(case, arguments):
    """The fixture cannot expand the requested dataset or silently drop filters."""
    scope = {key: value for key, value in case['scope'].items() if key not in TRANSPORT}
    supplied = {key: value for key, value in arguments.items() if key not in TRANSPORT}
    if 'log_id' in supplied:
        if str(supplied['log_id']) not in {str(row['id']) for row in case['rows']}:
            raise DatabaseRejected('DATABASE_LOG_NOT_FOUND')
        # Exact-ID fragment reads may omit the original date/department filters.
        if 'log_id' in scope and str(supplied['log_id']) != str(scope['log_id']):
            raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')
        # Exact-ID reads of a previously scoped synthetic record are allowed.
        if not any(key in supplied for key in ('start_date', 'end_date_exclusive', 'department_id', 'department_ids')):
            return
    if 'log_id' in scope and 'log_id' not in supplied:
        raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')
    for key in ('start_date', 'end_date_exclusive'):
        if key in scope and supplied.get(key) != scope[key]:
            raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')
    try:
        allowed, selected = department_ids(scope), department_ids(supplied)
    except (TypeError, ValueError):
        raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS') from None
    if allowed and (not selected or not selected <= allowed):
        raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')
    if supplied.get('include_descendants', False) and not scope.get('include_descendants', False):
        raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')


def synthetic_page(case, capability, arguments, *, source_scope, base_url=''):
    if capability not in CAPABILITIES[1:]:
        raise DatabaseRejected('DATABASE_CAPABILITY_DENIED')
    # Capacity is a source-side transport policy, never part of the short user
    # question. A normal/default max_chars still encounters the long-text cap.
    effective = deepcopy(arguments)
    effective['max_chars'] = min(arguments.get('max_chars', 12000), case['scope'].get('max_chars', 14000))
    try:
        plan = compile_query(capability, effective, source_scope=source_scope)
    except (ValueError, TypeError):
        raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS') from None
    validate_scope(case, arguments)
    rows = deepcopy(case['rows'])
    if 'log_id' in arguments:
        rows = [row for row in rows if str(row['id']) == str(arguments['log_id'])]
    selected = department_ids(arguments)
    if selected:
        if arguments.get('include_descendants') and '3' in selected:
            selected.add('4')
        rows = [row for row in rows if row['department_id'] in selected]
    for key, field in (('user_id', 'user_id'), ('project_id', 'project_id'), ('log_type', 'type_code')):
        if key in arguments:
            rows = [row for row in rows if str(row.get(field)) == str(arguments[key])]
    words = [arguments['keyword']] if 'keyword' in arguments else arguments.get('keywords', [])
    if words:
        match = all if arguments.get('keyword_mode', 'any') == 'all' else any
        rows = [row for row in rows if match(word.lower() in row['content'].lower() for word in words)]
    rows.sort(key=lambda row: (row['log_date'], row['id']), reverse=plan.order == 'desc')
    total = len(rows)
    resolved = None
    fingerprint = None
    if plan.department_roots:
        resolved = {'root_ids': [str(value) for value in plan.department_roots],
                    'include_descendants': plan.include_descendants, 'basis': 'current_department_membership',
                    'status_filter': 'none', 'departments': [
                        {'id': str(value), 'name': {3: '甲部门', 4: '乙部门'}.get(value, '合成部门')}
                        for value in sorted({*plan.department_roots, *([4] if plan.include_descendants and 3 in plan.department_roots else [])})]}
        fingerprint = hashlib.sha256(json.dumps(resolved, sort_keys=True).encode()).hexdigest()
    if 'after' in arguments:
        cursor = arguments['after']
        if plan.include_descendants and cursor.get('department_scope') != fingerprint:
            raise DatabaseRejected('DATABASE_SCOPE_CHANGED')
        boundary = (cursor['date'], int(cursor['id']))
        rows = [row for row in rows if ((row['log_date'], row['id']) > boundary
                if plan.order == 'asc' else (row['log_date'], row['id']) < boundary)]
    # A one-row backend page forces the pagination fixture through next_cursor.
    limit = min(plan.limit, 1 if case['id'] == 'pagination' else plan.limit)
    result = {'query_id': uuid4().hex, 'capability': capability, 'source_id': SOURCE,
              'source_name': '合成日志固定题', 'source_revision': 1,
              'queried_at': datetime.now(timezone.utc).isoformat(), 'columns': [],
              'rows': rows[:limit], 'truncated': len(rows) > limit, 'row_limit': limit,
              'transport_policy': {'max_chars': effective['max_chars'],
                                   'note': '本源返回有界正文片段；按返回的续读位置读完全文。'},
              'sql_sha256': hashlib.sha256(plan.statement.encode()).hexdigest()}
    add_evidence(result, plan, total)
    if resolved:
        result['resolved_department_scope'] = resolved
        if result['next_cursor'] and plan.include_descendants:
            result['next_cursor']['department_scope'] = fingerprint
    for row in result['rows']:
        row['evidence_id'] = SOURCE + ':' + row['evidence_id']
        for passage in row.get('passages', []):
            passage['evidence_id'] = SOURCE + ':' + passage['evidence_id']
    return prepare_evidence_page(result, plan, effective, base_url)


def observed_coverage(case, calls):
    """Transport observation only: no claim about semantic coverage/correctness."""
    lengths = {str(row['id']): len(row['content']) for row in case['rows']}
    intervals = {key: [] for key in lengths}
    successful = [call for call in calls if call.get('status') == 'succeeded' and
                  call['capability'] in CAPABILITIES[1:]]
    terminal = False
    for call in successful:
        response = call['response']
        if 'log_id' not in call['arguments'] and response.get('has_more') is False:
            terminal = True
        for row in response.get('rows', []):
            key = str(row['id'])
            if key not in lengths:
                continue
            begin = row.get('text_offset', 0)
            end = row.get('next_text_offset')
            if end is None:
                end = row.get('total_text_chars', lengths[key])
            intervals[key].append([begin, end])
    complete = {}
    for key, spans in intervals.items():
        cursor = 0
        for begin, end in sorted(spans):
            if begin > cursor:
                break
            cursor = max(cursor, end)
        complete[key] = bool(spans) and cursor >= lengths[key]
    if 'log_id' in case['scope']:
        terminal = bool(successful)
    return {'basis': 'actual returned evidence intervals; not semantic completeness',
            'calls': len(successful), 'expected_sources': len(lengths),
            'source_intervals': intervals, 'source_text_complete': complete,
            'last_record_page_observed': terminal,
            'all_evidence_observed': terminal and all(complete.values())}


def review_template(case, output, coverage):
    sentences = [text.strip() for line in output.splitlines()
                 for text in re.split(r'(?<=[。！？])', line) if text.strip()]
    return {'case_id': case['id'], 'reviewer': None, 'verdict': None,
            'observed_transport': coverage,
            'claims': [{'claim_id': index + 1, 'text': text, 'evidence': [],
                        'state_supported': None, 'relevance': None, 'verdict': None, 'note': ''}
                       for index, text in enumerate(sentences)],
            'normalized_answer': {'query_scope': None, 'seen_sources': [], 'complete': None,
                                  'unread_fragments': None, 'items': [], 'unsupported_metrics': [],
                                  'reviewed_all_claims': False}}


def quality_source_identity(skill_root):
    paths = {Path(__file__), ROOT / 'tests/test_database_quality_acceptance.py', CASES_PATH}
    paths.update((ROOT / 'agentbridge/database').glob('*.py'))
    for sid in SKILLS:
        paths.update(path for path in (skill_root / sid).rglob('*') if path.is_file())
    return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


class DatabaseQualityAcceptance(SkillHostAcceptance):
    def __init__(self, home, *, skill_root=None, label='baseline', cases_path=CASES_PATH,
                 selected_cases=CASE_IDS, prompt_variant='standard'):
        self.selected_case_ids = validate_selection(selected_cases, prompt_variant)
        self.prompt_variant = prompt_variant
        self.blind_cases = load_blind_cases(cases_path)
        self.selected_blind_cases = select_blind_cases(self.blind_cases, self.selected_case_ids, prompt_variant)
        self.active_case = None
        self.database_calls = []
        self.skill_root = Path(skill_root) if skill_root else ROOT / 'agentbridge/business_skills'
        self.quality_source_start = quality_source_identity(self.skill_root)
        registry = SkillRegistry(self.skill_root)
        registry.items = {sid: registry.get(sid) for sid in SKILLS}
        self.frozen_skills = deepcopy(registry.items)
        # Preserve the original host fixture and its text-only candidate guard.
        with patch('agentbridge.core.business_skills.SkillRegistry', return_value=registry):
            super().__init__(Path(home))
        self.service.text_only = True  # All ordinary business adapters deny.
        fixture = self

        class FixtureDatabase(IndependentDatabase):
            def __init__(self, selected_home, **kwargs):
                if Path(selected_home).resolve() != fixture.service.home.resolve():
                    raise ValueError('Synthetic database runtime cannot open another home')
                super().__init__(selected_home, **kwargs)

            def execute(self, subject, capability, arguments, source_id=None, **kwargs):
                entry = {'capability': capability, 'arguments': deepcopy(arguments),
                         'source_id': source_id, 'subject': subject, 'status': 'started'}
                fixture.database_calls.append(entry)
                try:
                    result = super().execute(subject, capability, arguments, source_id, **kwargs)
                except Exception as error:
                    entry.update(status='rejected', code=str(error) if isinstance(error, DatabaseRejected) else type(error).__name__)
                    raise
                entry.update(status='succeeded', response=deepcopy(result))
                return result

            def _execute(self, subject, capability, arguments, grant, source_id, **kwargs):
                current, record = self.snapshot(subject, capability, source_id)
                if current != grant or subject != ALICE or source_id != SOURCE or fixture.active_case is None:
                    raise DatabaseRejected('DATABASE_CAPABILITY_DENIED')
                if kwargs.get('original') or kwargs.get('export'):
                    raise DatabaseRejected('DATABASE_CAPABILITY_DENIED')
                if capability == 'database.directory':
                    result = fixture.directory(arguments)
                else:
                    result = synthetic_page(fixture.active_case, capability, arguments,
                        source_scope=f'{SOURCE}:{record["revision"]}:{subject}:{grant["revision"]}:',
                        base_url=self.original_base_url)
                self.unchanged(subject, capability, source_id, grant, record)
                return result

        self.runtime_type = FixtureDatabase
        self.runtime_patch = patch('agentbridge.database.independent.IndependentDatabase', FixtureDatabase)
        self.runtime_patch.start()
        self.report.update(suite='database-quality', label=label,
            selected_cases=list(self.selected_case_ids), prompt_variant=prompt_variant,
            fixture_cases_sha256=hashlib.sha256(Path(cases_path).read_bytes()).hexdigest(),
            frozen_skills={sid: {'version': bundle['manifest']['version'], 'content_hash': bundle['content_hash']}
                           for sid, bundle in self.frozen_skills.items()},
            semantic_review='pending; no automatic prose score',
            evidence_boundary={**self.report['evidence_boundary'],
                'business': 'synthetic database I/O and fixed-scope rows; no PostgreSQL or production business access',
                'oracle': 'controller-only; item/state/destination/excluded-source labels never sent to model'})

    def finalize_source_identity(self):
        super().finalize_source_identity()
        end = quality_source_identity(self.skill_root)
        changed = sorted(path for path in self.quality_source_start.keys() | end.keys()
                         if self.quality_source_start.get(path) != end.get(path))
        self.report['quality_code_identity'] = {'files_sha256': self.quality_source_start,
                                                'unchanged': not changed, 'changed_files': changed}

    def _seed(self):
        configured_source(self.service.home, SOURCE)
        runtime = IndependentDatabase(self.service.home)
        runtime.grants.set(ALICE, CAPABILITIES, source_id=SOURCE)
        self.service.skills.save('user:' + ALICE,
            {sid: {'profiles': list(self.service.skills.current(sid)['manifest']['profiles']),
                   'source_id': SOURCE, 'detail': 'standard'} for sid in SKILLS},
            expected_revision=0, actor='synthetic-reviewer', reason='隔离合成日志盲测')
        return {sid: sid for sid in SKILLS}

    def configure(self, **kwargs):
        super().configure(**kwargs)
        path = self.home / 'openclaw.json'
        config = json.loads(path.read_text())
        config['tools']['allow'] = TOOLS
        config['plugins']['entries']['agentbridge-interactions']['config']['boundSkillToolGuard'] = True
        write_metadata(path, config)

    def directory(self, arguments):
        if not isinstance(arguments, dict) or set(arguments) - {'entity', 'keyword', 'parent_id', 'after_id'}:
            raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')
        entity = arguments.get('entity')
        rows = {'departments': [{'id': '3', 'name': '甲部门', 'parent_id': None},
                                {'id': '4', 'name': '乙部门', 'parent_id': '3'}],
                'users': [{'id': '1', 'username': 'synthetic', 'fullname': '合成来源作者', 'dept_id': '3'}],
                'projects': []}.get(entity)
        if rows is None or not isinstance(arguments.get('keyword', ''), str):
            raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')
        keyword = arguments.get('keyword', '')
        rows = [row for row in rows if any(keyword in str(row.get(key, '')) for key in ('name', 'fullname', 'username'))]
        if 'parent_id' in arguments:
            if entity != 'departments':
                raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')
            rows = [row for row in rows if row.get('parent_id') == str(arguments['parent_id'])]
        if 'after_id' in arguments:
            try:
                rows = [row for row in rows if int(row['id']) > int(arguments['after_id'])]
            except (TypeError, ValueError):
                raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS') from None
        return {'query_id': uuid4().hex, 'sql_sha256': hashlib.sha256(b'synthetic-directory').hexdigest(),
                'truncated': False, 'rows': rows, 'source_id': SOURCE, 'has_more': False,
                'next_after_id': None, 'directory_semantics': {'status_meaning': 'unverified', 'hierarchy_available': True}}

    def run_blind_case(self, case, **kwargs):
        self.active_case = case
        start = len(self.database_calls)
        try:
            if case['id'] not in self.selected_case_ids:
                raise ValueError('The case was not selected for this run')
            rows = super().run_case({'id': case['id'], 'prompt': question_prompt(case, self.prompt_variant)}, **kwargs)
            row = rows[0]
            calls = deepcopy(self.database_calls[start:])
            coverage = observed_coverage(case, calls)
            successful = [event for event in row['skill_events'] if event.get('status') == 'succeeded']
            # Route selection is recorded, but these semantic cases do not
            # prescribe a unique assistant. All selected methods must be bound.
            row['checks'].pop('skill_selection', None)
            row['checks'].update(allowed_skill_loaded=bool(successful) and
                all(event['skill_id'] in SKILLS for event in successful),
                all_evidence_observed=coverage['all_evidence_observed'],
                no_business_calls=not row['synthetic_calls'],
                no_rejected_database_calls=all(call['status'] == 'succeeded' for call in calls))
            row.update(database_calls=calls, observed_transport=coverage, prompt_variant=self.prompt_variant)
            private_text(self.home / 'evidence' / (case['id'] + '.answer.txt'), row['output'])
            write_metadata(self.home / 'evidence' / (case['id'] + '.manual-review.json'),
                           review_template(case, row['output'], coverage))
            self.save_report()
            return row
        finally:
            self.active_case = None

    def close(self):
        try:
            super().close()
        finally:
            if hasattr(self, 'runtime_patch'):
                self.runtime_patch.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--node', required=True, type=Path)
    parser.add_argument('--openclaw', required=True, type=Path)
    parser.add_argument('--provider-plugin', required=True, type=Path)
    parser.add_argument('--source-config', type=Path)
    parser.add_argument('--source-agent-dir', type=Path)
    parser.add_argument('--ca', type=Path)
    parser.add_argument('--model', default='openai/gpt-5.6-sol')
    parser.add_argument('--label', choices=('baseline', 'candidate'), default='baseline')
    parser.add_argument('--skill-root', type=Path, help='Frozen built-in skill directory; never a production draft')
    parser.add_argument('--cases', type=parse_case_ids, default=CASE_IDS,
                        help='Comma-separated, distinct case IDs; default all seven')
    parser.add_argument('--prompt-variant', choices=('standard', 'paraphrase'), default='standard',
                        help='Whitelisted short wording; paraphrase currently supports pagination only')
    parser.add_argument('--timeout', type=int, default=180)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', action='store_true', help='Offline fixture/config checks only; no model authentication')
    mode.add_argument('--run', action='store_true', help='Run the selected blind cases against an isolated real host')
    args = parser.parse_args()
    if args.run and (not args.source_config or not args.source_agent_dir):
        parser.error('--run requires explicit official model source config and agent directory')
    try:
        validate_selection(args.cases, args.prompt_variant)
    except ValueError as error:
        parser.error(str(error))
    fixture = DatabaseQualityAcceptance(args.home, skill_root=args.skill_root, label=args.label,
        selected_cases=args.cases, prompt_variant=args.prompt_variant)
    try:
        fixture.configure(provider_plugin=args.provider_plugin.resolve(), model=args.model, ca=args.ca,
                          mcp_url='http://127.0.0.1:1/mcp' if args.prepare else None)
        fixture.validate_host(node=args.node, openclaw=args.openclaw)
        if args.run:
            fixture.prepare_provider(node=args.node, openclaw=args.openclaw, provider_plugin=args.provider_plugin)
            fixture.prepare_auth(node=args.node, sdk=args.openclaw.parent / 'dist/plugin-sdk/agent-runtime.js',
                                 source_config=args.source_config, source_agent_dir=args.source_agent_dir)
            fixture.start_gateway(node=args.node, openclaw=args.openclaw)
            for case in fixture.selected_blind_cases:
                row = fixture.run_blind_case(case, node=args.node, openclaw=args.openclaw, timeout=args.timeout)
                print(json.dumps({'id': case['id'], 'elapsed_seconds': row['elapsed_seconds'],
                                  'checks': row['checks'], 'semantic_review': 'pending'}), flush=True)
        fixture.save_report()
    finally:
        fixture.finalize_source_identity()
        fixture.report['automatic_transport_status'] = ('not_run' if not args.run else
            'passed' if ([row['id'] for row in fixture.report['cases']] == list(fixture.selected_case_ids)
                         and all(all(row['checks'].values()) for row in fixture.report['cases'])
                         and fixture.report['quality_code_identity']['unchanged']
                         and all(fixture.report['code_identity'][key] for key in
                                 ('relevant_content_unchanged', 'git_head_unchanged', 'tracked_diff_unchanged')))
            else 'failed')
        fixture.save_report()
        fixture.close()
    print(json.dumps({'home': str(fixture.home), 'model_run_attempted': args.run,
                      'report': str(fixture.home / 'evidence/report.json'),
                      'automatic_transport_status': fixture.report['automatic_transport_status'],
                      'selected_cases': list(fixture.selected_case_ids), 'prompt_variant': fixture.prompt_variant,
                      'semantic_review': 'pending'}))
    return int(fixture.report['automatic_transport_status'] == 'failed')


if __name__ == '__main__':
    raise SystemExit(main())
