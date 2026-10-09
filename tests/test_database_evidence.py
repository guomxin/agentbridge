"""Transport completeness and authenticated original-reading contracts."""
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from agentbridge.database.content import add_evidence, compile_query
from agentbridge.database.evidence import prepare_evidence_page, text_revision
from agentbridge.database.independent import DatabaseRejected
from tests import test_database_scope as scope_tests

WINDOW = scope_tests.WINDOW


class EvidenceTests(unittest.TestCase):
    def page(self, bodies, args=None, capability='database.logs.content_analyze'):
        args = args or WINDOW
        plan = compile_query(capability, args)
        result = {'source_id': 'sample', 'rows': [
            {'id': i+1, 'log_date': '2026-09-01', 'content': body, 'fullname': '测试作者'}
            for i, body in enumerate(bodies)], 'columns': [], 'truncated': False}
        if plan.include_descendants:
            result['resolved_department_scope'] = {'departments': [{'id': '3', 'name': '测试部门'}]}
        add_evidence(result, plan, len(bodies))
        return prepare_evidence_page(result, plan, args, 'https://workspace.example'), plan

    def test_compact_sources_and_no_repeated_body(self):
        result, _ = self.page(['项目已交付\n计划开展验收'])
        row = result['rows'][0]
        self.assertNotIn('content', row)
        self.assertNotIn('plain_text', row)
        self.assertEqual(len(row['passages']), 2)
        self.assertIn('测试作者 · 2026-09-01', row['source_label'])
        self.assertEqual(row['source_url'], 'https://workspace.example/database/logs/sample/1?revision='+text_revision('项目已交付\n计划开展验收'))
        self.assertTrue(result['content_complete'])
        self.assertEqual(len(result['analysis']['coverage_manifest']), 1)

    def test_character_pagination_keeps_scope_even_when_row_limit_not_hit(self):
        args = {**WINDOW, 'department_id': '3', 'include_descendants': True, 'max_chars': 6000}
        result, _ = self.page(['进展🙂'*500]*10, args)
        self.assertTrue(result['has_more'])
        self.assertLess(result['returned'], 10)
        self.assertEqual(result['next_cursor']['id'], str(result['rows'][-1]['id']))
        expected = hashlib.sha256(json.dumps(result['resolved_department_scope'], sort_keys=True).encode()).hexdigest()
        self.assertEqual(result['next_cursor']['department_scope'], expected)
        compile_query('database.logs.content_analyze', {**args, 'after': result['next_cursor']})
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, indent=2).encode('utf-16-le'))//2, 6000)

    def test_long_log_continuation_reads_every_character_and_guards_revision(self):
        body = '建设进展🙂'*2500
        for capability in ('database.logs.query', 'database.logs.content_analyze'):
            args = {'log_id': '1', 'max_chars': 6000}
            result, _ = self.page([body], args, capability)
            parts = []
            for _ in range(100):
                row = result['rows'][0]
                parts.extend(p['text'] for p in row['passages'])
                if row['next_text_offset'] is None:
                    break
                args.update(text_offset=row['next_text_offset'], expected_revision=row['source_revision_hash'])
                result, _ = self.page([body], args, capability)
            self.assertEqual(''.join(parts), body)
            with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_CONTENT_CHANGED'):
                self.page([body+'修改'], args, capability)

    def test_exact_log_does_not_assume_daily_and_bad_continuation_rejected(self):
        plan = compile_query('database.logs.query', {'log_id': '9007199254740993'})
        self.assertEqual(plan.params, [9007199254740993])
        self.assertNotIn('type_code =', plan.statement)
        for args in ({'log_id': '1', 'text_offset': 20}, {'log_id':'1', 'start_date':'2026-09-01'},
                     {**WINDOW, 'text_offset': 0}):
            with self.assertRaises(ValueError):
                compile_query('database.logs.content_analyze', args)

    def test_original_permission_current_content_and_missing(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, conn, cursor = scope_tests.ScopeTests().fixture(root)
            with patch('agentbridge.database.sources.connect') as connect, patch('agentbridge.database.sources.check_role'):
                connect.return_value.__enter__.return_value = conn
                cursor.fetchmany.side_effect = [[{'id': 1, 'log_date': '2026-09-01', 'type_code': 'WEEKLY',
                    'fullname': '测试作者', 'content': '<p>计划开展验收</p><script>evil()</script><p>已提交材料</p>'}], []]
                result = runtime.read_original('reader', 'taihua_primary', '1')
                self.assertEqual(result['log_type'], 'WEEKLY')
                self.assertNotIn('evil()', str(result))
                self.assertEqual(result['paragraphs'][0]['text'], '计划开展验收')
                cursor.fetchmany.side_effect = [[]]
                with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_LOG_NOT_FOUND'):
                    runtime.read_original('reader', 'taihua_primary', '1')
                runtime.grants.set('reader', ['database.logs.analyze'])
                with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_CAPABILITY_DENIED'):
                    runtime.read_original('reader', 'taihua_primary', '1')
                with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_CAPABILITY_DENIED'):
                    runtime.read_original('other', 'taihua_primary', '1')

    def test_large_rows_stop_fetching_with_lookahead_and_authorization_change_fails(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, conn, cursor = scope_tests.ScopeTests().fixture(root)
            body = '施工进展'*6000
            with patch('agentbridge.database.sources.connect') as connect, patch('agentbridge.database.sources.check_role'):
                connect.return_value.__enter__.return_value = conn
                cursor.fetchmany.side_effect = [[{'id': i, 'log_date':'2026-09-01', 'content':body}]
                                               for i in range(1, 201)]
                result = runtime.execute('reader', 'database.logs.content_analyze', WINDOW)
                self.assertLess(cursor.fetchmany.call_count, 200)
                self.assertTrue(result['has_more'])
                self.assertEqual(result['next_cursor']['id'], '1')
                self.assertFalse(result['content_complete'])
                with patch.object(runtime, 'unchanged', side_effect=DatabaseRejected('DATABASE_AUTHORIZATION_CHANGED')):
                    with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_AUTHORIZATION_CHANGED'):
                        runtime.read_original('reader', 'taihua_primary', '1')


if __name__ == '__main__':
    unittest.main()
