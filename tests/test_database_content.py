from tests.database_fixtures import configured_source
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agentbridge.database.content import compile_query, add_evidence, plain_text, INPUT_SCHEMAS, CONTENT_MODES
from agentbridge.database.independent import IndependentDatabase, DatabaseRejected, CAPABILITIES


WINDOW = {'start_date': '2026-09-01', 'end_date_exclusive': '2026-09-12'}


class ContentQueryTests(unittest.TestCase):
    def test_literal_keywords_shared_by_query_and_statistics(self):
        args = {**WINDOW, 'keywords': ["%' OR 1=1 --", '_'], 'keyword_mode': 'all', 'department_id': '12'}
        plans = [compile_query(cap, args) for cap in ('database.logs.query', 'database.logs.analyze')]
        for plan in plans:
            self.assertNotIn("%' OR 1=1 --", plan.statement)
            self.assertIn('strpos(lower(l.content),lower(%s))>0', plan.statement)
            self.assertIn('u.dept_id = %s', plan.statement)
            self.assertEqual(plan.params[-2:], ["%' OR 1=1 --", '_'])
        self.assertEqual(plans[0].params, plans[1].params)
        self.assertEqual(plans[0].statement.split(' WHERE ')[1].split(' ORDER BY ')[0],
                         plans[1].statement.split(' WHERE ')[1].split(' GROUP BY ')[0])

    def test_cursor_bound_to_capability_mode_filters_and_direction(self):
        cap = 'database.logs.content_analyze'
        plan = compile_query(cap, {**WINDOW, 'keyword': '项目'})
        cursor = {'date': '2026-09-03', 'id': '123', 'scope': plan.scope}
        args = {**WINDOW, 'keyword': '项目', 'after': cursor}
        paged = compile_query(cap, {**args, 'page_size': 1})
        self.assertIn('(l.log_date,l.id) > (%s,%s)', paged.statement)
        self.assertNotIn('(l.log_date,l.id)', paged.count_statement)
        self.assertEqual(paged.count_params, plan.params)
        for changed in ({'keyword': '故障'}, {'mode': 'issues'}, {'order': 'desc'}, {'user_id': 1}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                compile_query(cap, {**args, **changed})
        with self.assertRaises(ValueError):
            compile_query('database.logs.query', args)

    def test_legacy_defaults_and_descending_cursor(self):
        plan = compile_query('database.logs.query', WINDOW)
        self.assertEqual(plan.limit, 200)
        self.assertEqual(plan.params[-1], 'DAILY')
        self.assertTrue(plan.statement.endswith('ORDER BY l.log_date DESC,l.id DESC'))
        nullable = compile_query('database.logs.query', {**WINDOW, 'user_id': None, 'project_id': None})
        self.assertEqual(nullable.statement, plan.statement)
        self.assertEqual(nullable.params, plan.params)
        after = {'date': '2026-09-03', 'id': 4, 'scope': plan.scope}
        self.assertIn('(l.log_date,l.id) < (%s,%s)', compile_query('database.logs.query', {**WINDOW, 'after': after}).statement)

    def test_comment_date_and_author_semantics(self):
        args = {**WINDOW, 'user_id': 1, 'commenter_id': 2, 'keywords': ['付款', '设备'],
                'search_in': 'both', 'keyword_mode': 'all'}
        plan = compile_query('database.comments.analyze', args)
        self.assertIn('WHERE m.created_at >= %s', plan.statement)
        self.assertIn('l.user_id = %s AND m.user_id = %s', plan.statement)
        self.assertEqual(plan.params[-4:], ['付款', '付款', '设备', '设备'])
        self.assertIn('l.content AS log_content', plan.statement)
        alternate = compile_query('database.comments.analyze', {**args, 'date_basis': 'log_date'})
        self.assertIn('WHERE l.log_date >= %s', alternate.statement)
        self.assertTrue(alternate.statement.endswith('ORDER BY m.created_at ASC,m.id ASC'))

    def test_bad_arguments_rejected_instead_of_silently_widened(self):
        invalid = [
            {'keywords': []}, {'keyword': ''}, {'keyword': '  '}, {'keyword': '\x00'},
            {'keywords': ['ok', 3]}, {'keywords': ['x']*9}, {'keyword': 'x'*101},
            {'keyword': 'x', 'keywords': ['y']}, {'keyword_mode': 'regex'},
            {'page_size': True}, {'page_size': 0}, {'page_size': 201}, {'page_size': '50'},
            {'order': 'desc;delete'}, {'user_id': True}, {'user_id': []}, {'user_id': '١'},
            {'department_id': 0}, {'project_id': 2**63}, {'mode': []}, {'mode': 'delete'},
            {'after': {'date': '2026-01-01', 'id': 1}}, {'subject': 'another-account'},
            {'start_date': None}, {'start_date': '2026-09-12'}, {'log_type': 'ALL'},
            {'commenter_id': 1}, {'search_in': 'comments'},
        ]
        for extra in invalid:
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, 'INVALID_DATABASE_ARGUMENTS'):
                compile_query('database.logs.content_analyze', {**WINDOW, **extra})
        for extra in ({'date_basis': 'anything'}, {'mode': 'summary'}, {'search_in': []}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                compile_query('database.comments.analyze', {**WINDOW, **extra})

    def test_html_plain_text_and_source_evidence(self):
        raw = '<p>未完成设备付款</p><script>send_secrets()</script><div>收到不代表完成</div>'
        self.assertNotIn('send_secrets', plain_text(raw))
        self.assertEqual(plain_text('电压 x < y'), '电压 x < y')
        plan = compile_query('database.logs.content_analyze', {**WINDOW, 'keyword': '付款', 'mode': 'issues'})
        result = add_evidence({'rows': [{'id': 1, 'log_date': '2026-09-01', 'content': raw}], 'truncated': True}, plan, 20)
        row = result['rows'][0]
        self.assertEqual(row['content'], raw)
        self.assertEqual(row['evidence_id'], 'log:1')
        self.assertTrue(row['passages'][0]['evidence_id'].startswith('log:1:paragraph:'))
        match = row['matches'][0]
        self.assertEqual(row['plain_text'][match['start']:match['end']], match['text'])
        self.assertEqual(result['analysis']['status'], 'evidence_ready')
        self.assertEqual(result['analysis']['performed_by'], 'calling_agent')
        self.assertEqual(result['total_matching'], 20)
        self.assertEqual(result['next_cursor']['id'], '1')
        self.assertEqual(result['coverage'], 'page')

    def test_no_results_and_final_page_never_claim_full_coverage(self):
        plan = compile_query('database.logs.content_analyze', WINDOW)
        result = add_evidence({'rows': [], 'truncated': False}, plan, 10)
        self.assertIsNone(result['next_cursor'])
        self.assertFalse(result['has_more'])
        self.assertEqual(result['coverage'], 'page')
        self.assertEqual(result['snapshot_scope'], 'single_request')

    def test_all_modes_and_schemas_are_discoverable(self):
        self.assertEqual(set(INPUT_SCHEMAS), set(CAPABILITIES)|{'database.report.download'})
        for mode in CONTENT_MODES:
            plan = compile_query('database.logs.content_analyze', {**WINDOW, 'mode': mode})
            result = add_evidence({'rows': [], 'truncated': False}, plan, 0)
            self.assertEqual(result['analysis']['mode'], mode)
        with tempfile.TemporaryDirectory() as root:
            configured_source(root)
            runtime = IndependentDatabase(root)
            runtime.grants.set('a', ['database.logs.content_analyze'])
            item = runtime.catalog('a')['capabilities'][0]
            self.assertIn('mode', item['input_schema']['properties'])
            self.assertFalse(item['input_schema']['additionalProperties'])


class ContentExecutionTests(unittest.TestCase):
    def test_paged_executor_counts_in_transaction_and_keeps_authorization_separate(self):
        with tempfile.TemporaryDirectory() as root:
            configured_source(root)
            runtime = IndependentDatabase(root)
            runtime.grants.set('a', ['database.logs.content_analyze'])
            with patch.object(runtime, '_execute') as load:
                with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_CAPABILITY_DENIED'):
                    runtime.execute('a', 'database.comments.analyze', WINDOW)
                load.assert_not_called()
            config = SimpleNamespace(enabled=True, privileges_reviewed=True, single_instance=True,
                host='unused', port=5432, dbname='unused', username='readonly', sslmode='require',
                source_id='test', credential_version='1', validate=lambda: None)
            role = {k: True for k in ('readonly','repeatable','default_readonly','analysis_access')}
            role.update({k: False for k in ('privileged','writable','sequence_update','sequence_usage','temp')})
            connection = MagicMock()
            connection.execute.side_effect = lambda sql, *args: MagicMock(fetchone=lambda: {'total_matching': 3} if 'total_matching' in sql else role)
            cursor = connection.cursor.return_value.__enter__.return_value
            cursor.description = [SimpleNamespace(name=n) for n in ('id','log_date','content')]
            cursor.fetchmany.return_value = [{'id': i, 'log_date': '2026-09-01', 'content': '计划完成\n未找到结果'} for i in (1,2,3)]
            with patch('agentbridge.database.sources.connect') as connect:
                connect.return_value.__enter__.return_value = connection
                first = runtime.execute('a', 'database.logs.content_analyze', {**WINDOW, 'page_size': 2})
                self.assertEqual(first['returned'], 2)
                self.assertTrue(first['has_more'])
                self.assertEqual(first['total_matching'], 3)
                self.assertEqual(cursor.fetchmany.call_args.args, (1,))  # Bound raw text before transport pagination.
                self.assertIn('READ ONLY', connection.execute.call_args_list[0].args[0])
                connection.rollback.assert_called_once()
                cursor.fetchmany.side_effect = [[{'id': 3, 'log_date': '2026-09-01', 'content': '明确后续'}],[]]
                last = runtime.execute('a', 'database.logs.content_analyze', {**WINDOW, 'page_size': 2, 'after': first['next_cursor']})
                self.assertFalse(last['has_more'])
                self.assertEqual(len({r['id'] for r in first['rows']+last['rows']}), last['total_matching'])
                self.assertEqual(last['coverage'], 'page')
                # Revocation during a read must prevent the enriched evidence from returning.
                def revoke(_):
                    runtime.grants.set('a', [])
                    return []
                cursor.fetchmany.side_effect = revoke
                with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_AUTHORIZATION_CHANGED'):
                    runtime.execute('a', 'database.logs.content_analyze', WINDOW)

    def test_comment_evidence_has_both_sources(self):
        plan = compile_query('database.comments.analyze', {**WINDOW, 'keyword': '付款', 'search_in': 'both'})
        rows = [{'id': 7, 'created_at': '2026-09-03 10:00:00', 'content': '收到',
                 'work_log_id': 1, 'log_content': '采购待付款'}]
        result = add_evidence({'rows': rows, 'truncated': False}, plan, 1)
        self.assertEqual(rows[0]['evidence_id'], 'comment:7')
        self.assertEqual(rows[0]['log_evidence_id'], 'log:1')
        self.assertEqual(rows[0]['matches'], [])
        self.assertTrue(rows[0]['log_matches'])
        self.assertEqual(result['analysis']['mode'], 'feedback')


if __name__ == '__main__':
    unittest.main()
