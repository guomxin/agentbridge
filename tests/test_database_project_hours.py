import base64
import csv
from datetime import date
import io
import json
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

from pglast import parse_sql
import pytest

from agentbridge.database.content import compile_query
from agentbridge.database.independent import main
from tests.test_database_source_selection import database_client


QUERY = {'start_date': '2026-10-01', 'end_date_exclusive': '2026-11-01',
         'group_by': 'project_hours_top10', 'keyword': '设备'}
EXPECTED_PROJECTS = [201, 199, 200, 1, 2, 3, 4, 5, 6, 7]


@pytest.fixture
def logs():
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.create_function('strpos', 2, lambda text, part: text.find(part) + 1)
    db.executescript("""
        ATTACH ':memory:' AS analysis;
        CREATE TABLE analysis.projects (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE analysis.users (id INTEGER PRIMARY KEY, dept_id INTEGER);
        CREATE TABLE analysis.departments (id INTEGER PRIMARY KEY, parent_id INTEGER);
        CREATE TABLE analysis.work_logs
            (log_date TEXT, type_code TEXT, user_id INTEGER, project_id INTEGER, hours NUMERIC, content TEXT);
        INSERT INTO analysis.departments VALUES (10, NULL), (11, 10), (20, NULL);
        INSERT INTO analysis.users VALUES (1, 11), (2, 20);
    """)
    db.executemany('INSERT INTO analysis.projects VALUES (?, ?)',
                   [(i, '同名项目' if i >= 199 else f'项目{i}') for i in range(1, 202)])
    db.executemany('INSERT INTO analysis.work_logs VALUES (?, ?, ?, ?, ?, ?)',
                   [('2026-10-01', 'DAILY', 1, i, 20 if i >= 199 else 1, '设备') for i in range(1, 202)])
    db.executemany('INSERT INTO analysis.work_logs VALUES (?, ?, ?, ?, ?, ?)', [
        ('2026-10-01', 'DAILY', 1, 201, 7, '设备'),
        ('2026-10-31', 'DAILY', 1, None, 1000, '设备'),
        ('2026-11-01', 'DAILY', 1, 1, 200, '设备'),
        ('2026-10-01', 'DAILY', 1, 8, 200, '付款'),
        ('2026-10-01', 'WEEKLY', 1, 9, 200, '设备'),
        ('2026-10-01', 'DAILY', 2, 12, 200, '模板'),
    ])
    try:
        yield db
    finally:
        db.close()


def run_sql(db, statement, params):
    # Parse the PostgreSQL query and evaluate it on synthetic rows. Only the
    # bind, composite DISTINCT and array syntax need SQLite equivalents.
    assert len(parse_sql(statement.replace('%s', 'NULL'))) == 1
    statement = statement.replace('%s', '?').replace('= ANY(?)', 'IN (SELECT value FROM json_each(?))')
    statement = statement.replace('DISTINCT (l.user_id,l.log_date)', 'DISTINCT json_array(l.user_id,l.log_date)')
    bindings = []
    for value in params:
        if isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, list):
            value = json.dumps(value)
        bindings.append(value)
    return db.execute(statement, bindings)


def query(logs, **overrides):
    plan = compile_query('database.logs.analyze', {**QUERY, **overrides})
    return [dict(row) for row in run_sql(logs, plan.statement, plan.params)]


def test_ranks_all_groups_before_limiting_and_separates_unassigned(logs):
    rows = query(logs)
    assert [row['group_id'] for row in rows] == [*EXPECTED_PROJECTS, None]
    assert [row['project_rank'] for row in rows] == [*range(1, 11), None]
    assert [row['registered_hours'] for row in rows] == [27, 20, 20, *([1] * 7), 1000]
    assert rows[0]['log_count'] == 2 and rows[0]['logged_person_days'] == 1
    assert rows[-1]['group_label'] == '未关联项目'
    assert rows[-1]['unassigned_project_logs'] == 1
    assert rows[-1]['unassigned_project_hours'] == 1000
    ordinary = query(logs, group_by='project')
    assert [row['group_id'] for row in ordinary] == [*range(1, 202), None]
    assert 'project_rank' not in ordinary[0]


def test_filters_are_applied_before_ranking_and_empty_windows_are_distinct(logs):
    one, = query(logs, project_id=201, department_ids=[10], include_descendants=True, user_id=1)
    assert (one['project_rank'], one['group_id'], one['registered_hours']) == (1, 201, 27)
    assert query(logs, project_id=201, department_id=20) == []
    weekly, = query(logs, log_type='WEEKLY')
    assert (weekly['project_rank'], weekly['group_id']) == (1, 9)
    unassigned, = query(logs, start_date='2026-10-31')
    assert (unassigned['project_rank'], unassigned['group_id'], unassigned['registered_hours']) == (None, None, 1000)
    assert query(logs, keyword='没有这样的词') == []
    with pytest.raises(ValueError, match='INVALID_DATABASE_ARGUMENTS'):
        query(logs, start_date='2026-10-25', baseline_start_date='2026-10-18')


def test_ranked_mcp_query_cli_and_csv_use_the_authorized_source(database_client, logs, monkeypatch, capsys):
    runtime, _, call = database_client
    source = 'team_logs'
    analyze = 'database.logs.analyze'
    runtime.grants.set('reader', [analyze], source_id=source)
    detail = call('database_capabilities', {'source_id': source, 'capability': analyze})['structuredContent']
    groups = detail['capability']['input_schema']['properties']['group_by']['enum']
    assert 'project_hours_top10' in groups

    def invoke(capability, arguments, sid=source):
        return call('database_execute', {'capability': capability, 'arguments': arguments, 'source_id': sid})['structuredContent']

    with patch('agentbridge.database.sources.connect') as connect:
        conn = connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.return_value = {
            'readonly': True, 'repeatable': True, 'default_readonly': True, 'privileged': False,
            'writable': False, 'sequence_update': False, 'sequence_usage': False, 'temp': False,
        }
        cursor = conn.cursor.return_value.__enter__.return_value

        def execute(statement, params):
            result = run_sql(logs, statement, params)
            cursor.description = [SimpleNamespace(name=col[0]) for col in result.description]
            cursor.fetchmany.side_effect = lambda size: [dict(row) for row in result.fetchmany(size)]

        cursor.execute.side_effect = execute
        export = {'query_capability': analyze, 'query_arguments': QUERY, 'request_key': 'project-ranking'}
        assert invoke(analyze, QUERY, sid='private_logs')['code'] == 'DATABASE_CAPABILITY_DENIED'
        assert invoke('database.free.read', {'sql': 'SELECT 1'})['code'] == 'DATABASE_CAPABILITY_DENIED'
        assert invoke('database.report.export', export)['code'] == 'DATABASE_CAPABILITY_DENIED'
        connect.assert_not_called()
        ranked = invoke(analyze, QUERY)
        assert ranked['status'] == 'succeeded'
        assert ranked['source_id'] == source and ranked['truncated'] is False
        assert [row['group_id'] for row in ranked['rows']] == [*EXPECTED_PROJECTS, None]

        arguments_file = runtime.home / 'project-query.json'
        arguments_file.write_text(json.dumps(QUERY), encoding='utf-8')
        monkeypatch.setattr(sys, 'argv', ['database', '--home', str(runtime.home), '--subject', 'reader',
                                         'execute', '--source-id', source, '--capability', analyze,
                                         '--arguments-file', str(arguments_file)])
        main()
        assert json.loads(capsys.readouterr().out)['rows'] == ranked['rows']
        runtime.grants.set('reader', [analyze, 'database.report.export'], source_id=source)
        report = invoke('database.report.export', export)
        download = invoke('database.report.download', {'report_id': report['report_id']})['result']
        data = base64.b64decode(download['file']['content_base64']).decode('utf-8-sig')
        exported = list(csv.DictReader(io.StringIO(data)))
        assert [row['group_id'] for row in exported] == [*(str(i) for i in EXPECTED_PROJECTS), '']
        assert exported[-1]['group_label'] == '未关联项目' and exported[-1]['project_rank'] == ''
