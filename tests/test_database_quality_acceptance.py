"""Offline boundaries for the optional log-analysis real-host blind harness."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.business_skills import skill_catalog, validate_binding
from agentbridge.database.independent import DatabaseRejected
from tests.support.database_quality_acceptance import (
    ALICE, BOB, CAPABILITIES, SOURCE, DatabaseQualityAcceptance,
    load_blind_cases, observed_coverage, question_prompt, review_template, synthetic_page,
)


class DatabaseQualityAcceptanceTests(unittest.TestCase):
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
