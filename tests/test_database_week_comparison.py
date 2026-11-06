import base64
import csv
from datetime import date
from decimal import Decimal
import io
import json
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pglast import parse_sql

from agentbridge.database.content import compile_query
from agentbridge.database.independent import DatabaseRejected, IndependentDatabase
from agentbridge.database.reports import _csv_cell
from agentbridge.database.sources import Sources
from tests.database_fixtures import configured_source


PERIODS = {
    'baseline_start_date': '2026-10-26',
    'start_date': '2026-11-09',
    'end_date_exclusive': '2026-11-16',
}


class WeekComparisonTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.create_function('strpos', 2, lambda text, sub: text.find(sub) + 1)
        self.db.executescript("""
            ATTACH ':memory:' AS analysis;
            CREATE TABLE analysis.work_logs
                (id INTEGER, log_date TEXT, type_code TEXT, user_id INTEGER, project_id INTEGER, hours NUMERIC, content TEXT);
            CREATE TABLE analysis.users (id INTEGER, dept_id INTEGER);
            CREATE TABLE analysis.departments (id INTEGER, parent_id INTEGER);
            CREATE TABLE analysis.projects (id INTEGER);
            INSERT INTO analysis.departments VALUES (1, NULL), (2, 1), (9, NULL);
            INSERT INTO analysis.users VALUES (1, 2), (3, 1), (9, 9);
            INSERT INTO analysis.projects VALUES (7);
        """)
        self.db.executemany('INSERT INTO analysis.work_logs VALUES (?, ?, ?, ?, ?, ?, ?)', [
            (1, '2026-10-26', 'DAILY', 1, None, 2, '登录'),
            (2, '2026-10-26', 'DAILY', 1, 7, 3.5, '登录'),
            (3, '2026-11-01', 'DAILY', 3, 7, 1, '登录'),
            (4, '2026-11-09', 'DAILY', 1, 7, 4, '登录'),
            (5, '2026-11-10', 'DAILY', 1, 7, 4, '登录'),
            (6, '2026-11-10', 'DAILY', 1, None, 1, '登录'),
            (7, '2026-11-15', 'DAILY', 3, None, 1.5, '登录'),
            (8, '2026-10-25', 'DAILY', 1, 7, 90, '登录'),
            (9, '2026-11-02', 'DAILY', 1, 7, 90, '登录'),
            (10, '2026-11-16', 'DAILY', 1, 7, 90, '登录'),
            (11, '2026-10-30', 'DAILY', 9, 7, 90, '登录'),
            (12, '2026-11-11', 'DAILY', 9, 7, 90, '登录'),
            (13, '2026-10-30', 'DAILY', 1, 7, 90, '联调'),
            (14, '2026-11-11', 'DAILY', 1, 7, 90, '联调'),
            (15, '2026-11-11', 'WEEKLY', 1, 7, 6, '登录'),
        ])

    def run_plan(self, plan):
        # Parse the PostgreSQL statement. Execute its joins, filters and aggregates
        # against local rows, adapting PG's bind, pair-count, array and cast syntax.
        self.assertEqual(len(parse_sql(plan.statement.replace('%s', 'NULL'))), 1)
        sql = plan.statement.replace('%s', '?').replace('NULL::text', 'NULL')
        sql = sql.replace('DISTINCT (l.user_id,l.log_date)', 'DISTINCT json_array(l.user_id,l.log_date)')
        sql = sql.replace('= ANY(?)', 'IN (SELECT value FROM json_each(?))')
        params = []
        for value in plan.params:
            if isinstance(value, list):
                value = json.dumps(value)
            elif isinstance(value, date):
                value = value.isoformat()
            params.append(value)
        return self.db.execute(sql, params).fetchall()

    def test_cross_month_distinct_days_and_bounded_department_scope(self):
        plan = compile_query('database.logs.analyze', {
            **PERIODS, 'department_ids': [1, 2], 'include_descendants': True, 'keyword': '登录',
        })
        self.assertEqual(plan.department_roots, (1, 2))
        self.assertTrue(plan.include_descendants)
        rows = self.run_plan(plan)
        values = {r['metric']: (r['baseline_value'], r['current_value'], r['delta'], r['change_percent'])
                  for r in rows}
        self.assertEqual(len(rows), 6)
        self.assertEqual(values, {
            'log_count': (3, 4, 1, 33.33), 'registered_hours': (6.5, 10.5, 4, 61.54),
            'logged_person_days': (2, 3, 1, 50), 'people': (2, 2, 0, 0),
            'unassigned_project_logs': (1, 2, 1, 100), 'unassigned_project_hours': (2, 2.5, 0.5, 25),
        })
        for row in rows:
            for key, value in {**PERIODS, 'baseline_end_date_exclusive': '2026-11-02'}.items():
                self.assertEqual(row[key], value)

    def test_person_project_keyword_and_ordinary_aggregate_agree(self):
        filters = {'user_id': '1', 'project_id': 7, 'keyword': '登录', 'group_by': 'none'}
        rows = self.run_plan(compile_query('database.logs.analyze', {**PERIODS, **filters}))
        for value, start, end in [('baseline_value', '2026-10-26', '2026-11-02'),
                                  ('current_value', '2026-11-09', '2026-11-16')]:
            ordinary, = self.run_plan(compile_query('database.logs.analyze', {
                **filters, 'start_date': start, 'end_date_exclusive': end,
            }))
            for row in rows:
                self.assertEqual(row[value], ordinary[row['metric']])
        hours = next(r for r in rows if r['metric'] == 'registered_hours')
        self.assertEqual((hours['baseline_value'], hours['current_value']), (3.5, 8))

    def test_weekly_empty_baseline_and_adjacent_windows(self):
        rows = self.run_plan(compile_query('database.logs.analyze', {
            **PERIODS, 'baseline_start_date': '2026-11-02', 'log_type': 'WEEKLY',
        }))
        current = {r['metric']: r['current_value'] for r in rows}
        self.assertEqual(current, {
            'log_count': 1, 'registered_hours': 6, 'logged_person_days': 1,
            'people': 1, 'unassigned_project_logs': 0, 'unassigned_project_hours': 0,
        })
        for row in rows:
            self.assertEqual(row['baseline_value'], 0)
            self.assertEqual(row['delta'], row['current_value'])
            self.assertIsNone(row['change_percent'])

    def test_no_current_matches_preserves_decline_and_both_empty_stays_zero(self):
        for log_id, baseline_hours in [(2, 3.5), (999, 0)]:
            with self.subTest(log_id=log_id):
                rows = self.run_plan(compile_query('database.logs.analyze', {**PERIODS, 'log_id': log_id}))
                self.assertEqual(len(rows), 6)
                hours = next(r for r in rows if r['metric'] == 'registered_hours')
                self.assertEqual(hours['baseline_value'], baseline_hours)
                for row in rows:
                    self.assertEqual(row['current_value'], 0)
                    self.assertEqual(row['delta'], -row['baseline_value'])
                    self.assertEqual(row['change_percent'], -100 if row['baseline_value'] else None)

    def test_downloaded_report_preserves_negative_values_and_empty_percent(self):
        self.db.execute("INSERT INTO analysis.work_logs VALUES (100, '2026-10-26', 'DAILY', 1, 7, 2, '核对')")
        filters = {**PERIODS, 'log_id': 100}
        plan = compile_query('database.logs.analyze', filters)
        pg_rows = [dict(row) for row in self.run_plan(plan)]
        for row in pg_rows:
            for name in ('baseline_value', 'current_value', 'delta', 'change_percent'):
                if row[name] is not None:
                    row[name] = Decimal(str(row[name]))

        source = 'taihua_primary'
        request = {'query_capability': 'database.logs.analyze', 'query_arguments': filters, 'request_key': 'weeks'}
        with tempfile.TemporaryDirectory() as home, patch('agentbridge.database.sources.connect') as connect:
            record = configured_source(home, source)
            runtime = IndependentDatabase(home)
            runtime.grants.set('reader', ['database.logs.analyze'], source_id=source)
            with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_CAPABILITY_DENIED'):
                runtime.execute('reader', 'database.report.export', request, source)
            connect.assert_not_called()

            runtime.grants.set('reader', ['database.logs.analyze', 'database.report.export'], source_id=source)
            connection = connect.return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = {
                'readonly': True, 'repeatable': True, 'default_readonly': True,
                'privileged': False, 'writable': False, 'sequence_update': False, 'sequence_usage': False, 'temp': False,
            }
            cursor = connection.cursor.return_value.__enter__.return_value
            cursor.description = [SimpleNamespace(name=name) for name in pg_rows[0]]
            cursor.fetchmany.side_effect = [pg_rows, []]
            report = runtime.execute('reader', 'database.report.export', request, source)
            cursor.execute.assert_called_once_with(plan.statement, plan.params)
            report_args = {'report_id': report['report_id']}
            download = runtime.execute('reader', 'database.report.download', report_args, source)
            text = base64.b64decode(download['file']['content_base64']).decode('utf-8-sig')
            rows = {row['metric']: row for row in csv.DictReader(io.StringIO(text))}
            self.assertEqual(len(rows), 6)
            self.assertEqual(rows['registered_hours']['delta'], '-2')
            self.assertEqual(Decimal(rows['registered_hours']['change_percent']), Decimal('-100'))
            self.assertEqual(rows['unassigned_project_hours']['change_percent'], '')

            config = dict(record['active'])
            del config['credential_ref']
            Sources(home).write(source, 'save', revision=record['revision'], actor='admin', reason='复核来源版本', config=config)
            with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_AUTHORIZATION_CHANGED'):
                runtime.execute('reader', 'database.report.download', report_args, source)

    def test_formula_protection_distinguishes_text_from_finite_numeric_types(self):
        for text in ('-2', '=1+2', '+1', '@name', ' \t-2'):
            with self.subTest(text=text):
                self.assertEqual(_csv_cell(text), "'" + text)
        for number in (-2, -1.25, Decimal('-3.40')):
            with self.subTest(number=number):
                self.assertEqual(str(_csv_cell(number)), str(number))
        for value in (float('-inf'), Decimal('-Infinity')):
            with self.subTest(value=value):
                self.assertEqual(_csv_cell(value), "'" + str(value))

    def test_invalid_windows_or_grouping_are_rejected(self):
        for override in [
            {'end_date_exclusive': '2026-11-15'}, {'end_date_exclusive': '2026-11-17'},
            {'baseline_start_date': '2026-11-03'}, {'baseline_start_date': '2026-11-16'},
            {'baseline_start_date': 'bad date'}, {'baseline_start_date': None},
            {'baseline_start_date': '9999-12-31'}, {'group_by': 'day'},
        ]:
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, 'INVALID_DATABASE_ARGUMENTS'):
                compile_query('database.logs.analyze', {**PERIODS, **override})
        with self.assertRaisesRegex(ValueError, 'INVALID_DATABASE_ARGUMENTS'):
            compile_query('database.logs.query', PERIODS)
