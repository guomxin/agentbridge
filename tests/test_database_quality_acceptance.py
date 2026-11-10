"""Offline boundaries for the optional log-analysis real-host blind harness."""
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.core.business_skills import skill_catalog, validate_binding
from agentbridge.database.independent import DatabaseRejected
from tests.support import database_quality_acceptance as harness
from tests.support.database_quality_acceptance import (
    ALICE, BOB, CAPABILITIES, SOURCE, DatabaseQualityAcceptance,
    load_blind_cases, observed_coverage, question_prompt, review_template, synthetic_page,
    parse_case_ids, select_blind_cases,
)


class DatabaseQualityAcceptanceTests(unittest.TestCase):
    def test_case_selection_rejects_empty_duplicate_and_unknown_ids(self):
        for value in ('', ' ', ',', 'empty,', ',empty', 'empty,,long-text', 'empty,empty', 'unknown', 'EMPTY'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_case_ids(value)
        self.assertEqual(parse_case_ids('long-text, empty'), ('long-text', 'empty'))
        for selected in ([], ['empty', 'empty'], ['unknown']):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                select_blind_cases(load_blind_cases(), selected)

    def test_only_selected_cases_are_dispatched_and_result_names_that_scope(self):
        instances = []

        class FakeFixture:
            def __init__(self, home, *, selected_cases, prompt_variant, **kwargs):
                self.home = home
                self.selected_case_ids = selected_cases
                self.prompt_variant = prompt_variant
                self.selected_blind_cases = select_blind_cases(load_blind_cases(), selected_cases, prompt_variant)
                self.report = {'cases': [], 'selected_cases': list(selected_cases), 'prompt_variant': prompt_variant}
                self.dispatched = []
                instances.append(self)

            def configure(self, **kwargs): pass
            def validate_host(self, **kwargs): pass
            def prepare_provider(self, **kwargs): pass
            def prepare_auth(self, **kwargs): pass
            def start_gateway(self, **kwargs): pass
            def save_report(self): pass
            def close(self): pass

            def finalize_source_identity(self):
                self.report.update(quality_code_identity={'unchanged': True}, code_identity={
                    key: True for key in ('relevant_content_unchanged', 'git_head_unchanged', 'tracked_diff_unchanged')})

            def run_blind_case(self, case, **kwargs):
                self.dispatched.append(case['id'])
                row = {'id': case['id'], 'checks': {'fixture_ok': True}, 'elapsed_seconds': 0}
                self.report['cases'].append(row)
                return row

        argv = ['blind', '--run', '--home', '/synthetic/home', '--node', '/synthetic/node',
                '--openclaw', '/synthetic/openclaw', '--provider-plugin', '/synthetic/provider',
                '--source-config', '/synthetic/config', '--source-agent-dir', '/synthetic/agent',
                '--cases', 'empty,long-text']
        with patch.object(harness, 'DatabaseQualityAcceptance', FakeFixture), patch('sys.argv', argv), redirect_stdout(StringIO()):
            self.assertEqual(harness.main(), 0)
        fixture = instances[0]
        self.assertEqual(fixture.dispatched, ['empty', 'long-text'])
        self.assertEqual(fixture.report['selected_cases'], ['empty', 'long-text'])
        self.assertEqual(fixture.report['automatic_transport_status'], 'passed')

    def test_paraphrase_changes_only_whitelisted_wording_not_scope_or_evidence(self):
        cases = load_blind_cases()
        snapshot = deepcopy(cases)
        selected = select_blind_cases(cases, ('pagination',), 'paraphrase')
        self.assertEqual(selected[0], next(case for case in snapshot if case['id'] == 'pagination'))
        prompt = question_prompt(selected[0], 'paraphrase')
        self.assertEqual(prompt, '把甲部门、乙部门及其下级2026年9月1日至7日的工作进展一起整理一下。')
        for marker in ('{', 'source_id', 'scope', 'items', 'destination', 'state', 'max_chars'):
            self.assertNotIn(marker, prompt)
        self.assertEqual(cases, snapshot)
        for ids, variant in ((('empty',), 'paraphrase'), (('pagination', 'empty'), 'paraphrase'),
                             (('pagination',), 'unknown')):
            with self.subTest(ids=ids, variant=variant), self.assertRaises(ValueError):
                select_blind_cases(cases, ids, variant)

    def test_blind_projection_and_prompt_do_not_include_oracle_fields(self):
        cases = load_blind_cases()
        self.assertEqual(len(cases), 7)
        for case in cases:
            self.assertEqual(set(case), {'id', 'question', 'scope', 'rows'})
            self.assertNotIn('items', case)
            self.assertNotIn('excluded_sources', case)
            prompt = question_prompt(case)
            for marker in ('"destination"', '"state"', '"paragraph"', 'repeat_first', 'excluded_sources'):
                self.assertNotIn(marker, prompt)
            self.assertNotIn(SOURCE, prompt)
            self.assertNotIn('max_chars', prompt)
            self.assertNotIn('{', prompt)
            if case['id'] == 'long-text':
                self.assertEqual(prompt, '读完1号日志，汇总所有事项并说明是否完成。')
            else:
                self.assertIn('2026年9月', prompt)
        by_id = {case['id']: question_prompt(case) for case in cases}
        self.assertIn('联系厂家处理的事项', by_id['progress-not-completion'])
        self.assertEqual(by_id['progress-not-completion'], '仅按2026年9月1日日志，联系厂家处理的事项完成了吗？')
        self.assertIn('材料验收', by_id['time-window'])
        last = next(case for case in cases if case['id'] == 'time-window')
        self.assertNotIn('9月5日', json.dumps(last, ensure_ascii=False))

    def test_real_fragments_require_all_intervals_and_revision_bound_continuation(self):
        case = next(case for case in load_blind_cases() if case['id'] == 'long-text')
        args = deepcopy(case['scope'])
        calls = []
        while True:
            result = synthetic_page(case, CAPABILITIES[2], args, source_scope='fixed:')
            calls.append({'capability': CAPABILITIES[2], 'arguments': deepcopy(args),
                          'status': 'succeeded', 'response': result})
            row = result['rows'][0]
            if row['next_text_offset'] is None:
                break
            self.assertFalse(observed_coverage(case, calls)['all_evidence_observed'])
            args = {**case['scope'], 'text_offset': row['next_text_offset'],
                    'expected_revision': row['source_revision_hash']}
        self.assertGreater(len(calls), 2)
        self.assertTrue(observed_coverage(case, calls)['all_evidence_observed'])
        missing_middle = calls[:1] + calls[2:]
        self.assertFalse(observed_coverage(case, missing_middle)['all_evidence_observed'])
        with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_CONTENT_CHANGED'):
            synthetic_page(case, CAPABILITIES[2], {**args, 'expected_revision': '0' * 64}, source_scope='fixed:')
        ordinary = synthetic_page(case, CAPABILITIES[2], {'log_id': 1}, source_scope='fixed:')
        self.assertEqual(ordinary['transport_policy']['max_chars'], 6000)
        self.assertFalse(ordinary['content_complete'])

    def test_cursor_scope_and_record_paging_cannot_be_bypassed(self):
        case = next(case for case in load_blind_cases() if case['id'] == 'pagination')
        args = deepcopy(case['scope'])
        first = synthetic_page(case, CAPABILITIES[2], args, source_scope='fixed:')
        self.assertEqual(len(first['rows']), 1)
        self.assertTrue(first['has_more'])
        final = synthetic_page(case, CAPABILITIES[2], {**args, 'after': first['next_cursor']}, source_scope='fixed:')
        self.assertFalse(final['has_more'])
        self.assertNotEqual(first['rows'][0]['id'], final['rows'][0]['id'])
        forged = {**first['next_cursor'], 'scope': 'forged'}
        with self.assertRaisesRegex(DatabaseRejected, 'INVALID_DATABASE_ARGUMENTS'):
            synthetic_page(case, CAPABILITIES[2], {**args, 'after': forged}, source_scope='fixed:')
        with self.assertRaisesRegex(DatabaseRejected, 'INVALID_DATABASE_ARGUMENTS'):
            synthetic_page(case, CAPABILITIES[2], {**args, 'start_date': '2020-01-01'}, source_scope='fixed:')
        equivalent = synthetic_page(case, CAPABILITIES[2],
            {**args, 'department_ids': [4, 3]}, source_scope='fixed:')
        self.assertEqual(equivalent['total_matching'], 2)

    def test_real_assignment_grants_and_binding_still_enforce_identity_and_capability(self):
        with TemporaryDirectory() as root:
            fixture = DatabaseQualityAcceptance(Path(root))
            try:
                fixture.active_case = fixture.blind_cases[0]
                catalog = skill_catalog(fixture.service, ALICE)
                self.assertEqual({item['id'] for item in catalog['items']},
                                 {'log-review', 'issue-tracking', 'similar-cases'})
                binding_id = fixture.service.skills.bind(ALICE, 'log-review', 'review', SOURCE)
                binding = fixture.service.skills.binding(ALICE, binding_id)
                validate_binding(fixture.service, ALICE, binding, capability=CAPABILITIES[2])
                runtime = fixture.runtime_type(fixture.service.home)
                result = runtime.execute(ALICE, CAPABILITIES[2], fixture.active_case['scope'], SOURCE)
                self.assertIn('passages', result['rows'][0])
                with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_CAPABILITY_DENIED'):
                    runtime.execute(BOB, CAPABILITIES[2], fixture.active_case['scope'], SOURCE)
                with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_CAPABILITY_DENIED'):
                    runtime.execute(ALICE, 'database.free.read', {'sql': 'SELECT 1'}, SOURCE)
                with self.assertRaisesRegex(ValueError, 'another home'):
                    fixture.runtime_type(Path(root) / 'outside')
                fixture.runtime_type(fixture.service.home).grants.set(ALICE, [], source_id=SOURCE)
                with self.assertRaises(Exception):
                    validate_binding(fixture.service, ALICE, binding, capability=CAPABILITIES[2])
            finally:
                fixture.close()

    def test_review_stays_pending_and_does_not_create_normalized_oracle_answers(self):
        case = load_blind_cases()[0]
        review = review_template(case, '完成甲项目。\n乙项目仍在进行。', {'all_evidence_observed': True})
        self.assertIsNone(review['verdict'])
        self.assertFalse(review['normalized_answer']['reviewed_all_claims'])
        self.assertEqual(review['normalized_answer']['items'], [])
        self.assertTrue(all(claim['verdict'] is None and not claim['evidence'] for claim in review['claims']))


if __name__ == '__main__':
    unittest.main()
