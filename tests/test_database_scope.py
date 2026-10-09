"""Generic scope contracts: no company-specific names or identifiers."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.database_fixtures import configured_source
from agentbridge.database.content import compile_query
from agentbridge.database.independent import IndependentDatabase, DatabaseRejected, rejection_result, validate_sql

WINDOW = {'start_date': '2026-09-01', 'end_date_exclusive': '2026-09-02'}


class ScopeTests(unittest.TestCase):
    def test_collection_hierarchy_and_cursor_contract(self):
        for cap in ('database.logs.query', 'database.logs.analyze', 'database.logs.content_analyze', 'database.comments.analyze'):
            p = compile_query(cap, {**WINDOW, 'department_ids': ['8', 3, 8], 'include_descendants': True})
            self.assertEqual(p.params[0], [3, 8])
            self.assertIn('UNION SELECT', p.statement)  # Deduplicates overlapping roots and terminates cycles.
            self.assertNotIn('UNION ALL', p.statement)
            self.assertNotIn('status =', p.statement)
        for args in ({'department_ids': []}, {'department_ids': [True]},
                     {'department_id': 1, 'department_ids': [2]}, {'include_descendants': True},
                     {'department_id': 1, 'include_descendants': 'true'}, {'department_ids': [1]*201}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                compile_query('database.logs.query', {**WINDOW, **args})
        with self.assertRaises(ValueError):
            compile_query('database.logs.query', {**WINDOW, 'department_id': 9007199254740993})
        self.assertEqual(compile_query('database.logs.query', {**WINDOW, 'department_id': '9007199254740993'}).department_roots,
                         (9007199254740993,))
        with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_SQL_UNSUPPORTED'):
            validate_sql('WITH RECURSIVE x AS (SELECT id FROM analysis.departments) SELECT * FROM x')

    def fixture(self, root):
        configured_source(root)
        runtime = IndependentDatabase(root)
        runtime.grants.set('reader', ['database.directory', 'database.logs.content_analyze'])
        conn = MagicMock()
        def execute(sql, params=None):
            result = MagicMock()
            if 'AS available' in sql:
                result.fetchone.return_value = {'available': True}
            elif 'total_matching' in sql:
                result.fetchone.return_value = {'total_matching': 2}
            elif sql.startswith('SELECT id FROM'):
                result.fetchall.return_value = [{'id': x} for x in params[0]]
            elif 'selected_departments' in sql:
                result.fetchall.return_value = [{'id': 3, 'name': 'Root'}, {'id': 8, 'name': 'Child'}]
            return result
        conn.execute.side_effect = execute
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.description = [SimpleNamespace(name=k) for k in ('id', 'log_date', 'content')]
        cursor.fetchmany.side_effect = [[{'id': 1, 'log_date': '2026-09-01', 'content': '计划'},
                                       {'id': 2, 'log_date': '2026-09-01', 'content': '已交付'}]]
        return runtime, conn, cursor

    def test_scope_in_same_transaction_and_changed_scope_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, conn, cursor = self.fixture(root)
            args = {**WINDOW, 'department_id': '3', 'include_descendants': True, 'page_size': 1}
            with patch('agentbridge.database.sources.connect') as connect, patch('agentbridge.database.sources.check_role'):
                connect.return_value.__enter__.return_value = conn
                first = runtime.execute('reader', 'database.logs.content_analyze', args)
                self.assertEqual(first['resolved_department_scope']['status_filter'], 'none')
                self.assertEqual(len(first['resolved_department_scope']['departments']), 2)
                self.assertIn('department_scope', first['next_cursor'])
                cursor.fetchmany.side_effect = [[{'id': 2, 'log_date': '2026-09-01', 'content': '已交付'}], []]
                last = runtime.execute('reader', 'database.logs.content_analyze', {**args, 'after': first['next_cursor']})
                self.assertFalse(last['has_more'])
                forged = {**first['next_cursor'], 'department_scope': '0'*64}
                with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_SCOPE_CHANGED'):
                    runtime.execute('reader', 'database.logs.content_analyze', {**args, 'after': forged})
                with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_CAPABILITY_DENIED'):
                    runtime.execute('other', 'database.logs.content_analyze', args)

    def test_unknown_roots_and_missing_hierarchy_never_widen_query(self):
        for missing in ('root', 'hierarchy'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as root:
                runtime, conn, cursor = self.fixture(root)
                original = conn.execute.side_effect
                def execute(sql, params=None):
                    r = original(sql, params)
                    if missing == 'root' and sql.startswith('SELECT id FROM'): r.fetchall.return_value = []
                    if missing == 'hierarchy' and 'AS available' in sql: r.fetchone.return_value = {'available': False}
                    return r
                conn.execute.side_effect = execute
                with patch('agentbridge.database.sources.connect') as connect, patch('agentbridge.database.sources.check_role'):
                    connect.return_value.__enter__.return_value = conn
                    code = 'DATABASE_DEPARTMENT_NOT_FOUND' if missing == 'root' else 'DATABASE_HIERARCHY_UNAVAILABLE'
                    with self.assertRaisesRegex(DatabaseRejected, code):
                        runtime.execute('reader', 'database.logs.content_analyze', {**WINDOW, 'department_id': 3, 'include_descendants': True})
                    cursor.execute.assert_not_called()

    def test_directory_paging_retains_filters_and_long_identifiers(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, conn, cursor = self.fixture(root)
            cursor.description = [SimpleNamespace(name=k) for k in ('id', 'name', 'parent_id', 'status')]
            cursor.fetchmany.side_effect = [[{'id': 9007199254740993, 'name': 'Child', 'parent_id': 3, 'status': False}], []]
            with patch('agentbridge.database.sources.connect') as connect, patch('agentbridge.database.sources.check_role'):
                connect.return_value.__enter__.return_value = conn
                r = runtime.execute('reader', 'database.directory', {'entity': 'departments', 'keyword': 'Child', 'parent_id': '3', 'after_id': '8'})
            self.assertEqual(r['rows'][0]['id'], '9007199254740993')
            self.assertEqual(cursor.execute.call_args.args[1], ['Child', 3, 8])
            self.assertIn('parent_id = %s', cursor.execute.call_args.args[0])
            self.assertIn('id > %s', cursor.execute.call_args.args[0])
            self.assertEqual(r['directory_semantics']['status_meaning'], 'unverified')

    def test_error_keeps_machine_code_and_human_explanation(self):
        r = rejection_result(DatabaseRejected('DATABASE_SQL_UNSUPPORTED'))
        self.assertEqual(r['code'], r['error']['code'])
        self.assertIn('include_descendants', r['error']['message'])


if __name__ == '__main__':
    unittest.main()
