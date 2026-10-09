"""Realistic transport envelopes, discovery paging, and actionable recovery."""
import copy
import tempfile
import unittest
from unittest.mock import patch

from agentbridge.database.independent import IndependentDatabase, DatabaseRejected, CAPABILITIES, rejection_result, validate_sql
from agentbridge.database.evidence import response_chars
from tests.database_fixtures import configured_source
from tests import test_database_scope as scopes


class TransportTests(unittest.TestCase):
    def test_denied_text_function_can_be_rewritten_without_expanding_access(self):
        with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_FUNCTION_DENIED') as caught:
            validate_sql("SELECT id FROM analysis.work_logs WHERE strpos(content,'无法登录') > 0")
        failure = rejection_result(caught.exception)
        self.assertEqual(failure['recovery']['action'], 'inspect_schema')
        self.assertFalse(failure['recovery']['retry_same_request'])
        self.assertTrue(validate_sql("SELECT id FROM analysis.work_logs WHERE content LIKE '%无法登录%'"))
        with tempfile.TemporaryDirectory() as root:
            configured_source(root)
            runtime = IndependentDatabase(root)
            runtime.grants.set('reader', ['database.logs.query'])
            with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_CAPABILITY_DENIED'):
                runtime.execute('reader', 'database.free.read', {
                    'sql': "SELECT id FROM analysis.work_logs WHERE content LIKE '%无法登录%'"})

    def test_discovery_is_bounded_complete_and_details_are_authorized(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=IndependentDatabase(root)
            for i in range(24):
                sid=f'source_{i:02}'
                configured_source(root,sid)
                runtime.grants.set('reader',list(CAPABILITIES),source_id=sid)
            after=None
            seen=[]
            while True:
                page=runtime.discover('reader',after_source_id=after)
                self.assertLess(response_chars(page),12000)
                self.assertNotIn('capabilities',page)  # No duplicated legacy projection.
                for source in page['sources']:
                    seen.append(source['source_id'])
                    self.assertTrue(all('input_schema' not in c for c in source['capabilities']))
                if not page['has_more']: break
                after=page['next_after_source_id']
            self.assertEqual(seen,[f'source_{i:02}' for i in range(24)])
            for capability in (*CAPABILITIES,'database.report.download'):
                detail=runtime.discover('reader','source_00',capability)
                self.assertLess(response_chars(detail),14000)
                self.assertIn('input_schema',detail['capability'])
            runtime.grants.set('reader',[],source_id='source_00')
            with self.assertRaisesRegex(DatabaseRejected,'DATABASE_CAPABILITY_DENIED'):
                runtime.discover('reader','source_00','database.logs.query')
            with self.assertRaisesRegex(DatabaseRejected,'INVALID_DATABASE_ARGUMENTS'):
                runtime.discover('reader',capability='database.logs.query')
            with self.assertRaisesRegex(DatabaseRejected,'INVALID_DATABASE_ARGUMENTS'):
                runtime.discover('reader',after_source_id='../invalid')

    def test_realistic_scope_envelope_at_every_budget_for_both_read_paths(self):
        for capability, mode in [('database.logs.query', None), *[
                ('database.logs.content_analyze', mode) for mode in ('summary', 'progress', 'issues', 'experience')]]:
            for budget in (4000,6000,12000,14000):
                with self.subTest(capability=capability,mode=mode,budget=budget),tempfile.TemporaryDirectory() as root:
                    runtime,conn,cursor=scopes.ScopeTests().fixture(root)
                    runtime.grants.set('reader',[capability])
                    original=conn.execute.side_effect
                    def execute(sql,params=None):
                        r=original(sql,params)
                        if 'selected_departments' in sql:
                            r.fetchall.return_value=[{'id':i,'name':f'测试部门{i}'} for i in range(3,11)]
                        return r
                    conn.execute.side_effect=execute
                    cursor.fetchmany.side_effect=[[{'id':1,'log_date':'2026-09-01','fullname':'测试作者',
                        'current_department':'测试部门','content':'讨论剩余工作；计划验证。\n安全检查发现问题，尚未解决。'}],
                        [{'id':2,'log_date':'2026-09-01','fullname':'另一作者','content':'已提交资料，等待盖章。'}],[]]
                    args={**scopes.WINDOW,'department_id':'3','include_descendants':True,'max_chars':budget}
                    if mode is not None:
                        args['mode'] = mode
                    with patch('agentbridge.database.sources.connect') as connect,patch('agentbridge.database.sources.check_role'):
                        connect.return_value.__enter__.return_value=conn
                        result=runtime.execute('reader',capability,args)
                    self.assertLessEqual(response_chars({'status':'succeeded',**result}),budget)
                    self.assertNotIn('executed_sql',result)
                    self.assertNotIn('columns',result)
                    self.assertNotIn('review_contract',result['analysis'])
                    self.assertEqual(result['analysis']['status'], 'evidence_ready')
                    self.assertEqual(len(result['analysis']['coverage_manifest']),result['returned'])
                    self.assertIn('passages',result['rows'][0])

    def test_budget_failure_supplies_transport_patch_without_changing_scope(self):
        with tempfile.TemporaryDirectory() as root:
            runtime,conn,cursor=scopes.ScopeTests().fixture(root)
            original=conn.execute.side_effect
            def execute(sql,params=None):
                r=original(sql,params)
                if 'selected_departments' in sql:
                    r.fetchall.return_value=[{'id':i,'name':'部门名称'*15+str(i)} for i in range(3,120)]
                return r
            conn.execute.side_effect=execute
            rows=[{'id':1,'log_date':'2026-09-01','content':'事项一；事项二。'}]
            args={**scopes.WINDOW,'department_id':'3','include_descendants':True,'max_chars':4000,'include_diagnostics':True}
            with patch('agentbridge.database.sources.connect') as connect,patch('agentbridge.database.sources.check_role'):
                connect.return_value.__enter__.return_value=conn
                cursor.fetchmany.side_effect=[copy.deepcopy(rows),[]]
                with self.assertRaises(DatabaseRejected) as caught:
                    runtime.execute('reader','database.logs.content_analyze',args)
                error=rejection_result(caught.exception)
                self.assertEqual(error['code'],'DATABASE_EVIDENCE_TOO_LARGE')
                recovery=error['recovery']
                self.assertEqual(recovery['action'],'adjust_transport')
                self.assertEqual(set(recovery['arguments_patch']),{'include_diagnostics','max_chars'})
                cursor.fetchmany.side_effect=[copy.deepcopy(rows),[]]
                result=runtime.execute('reader','database.logs.content_analyze',{**args,**recovery['arguments_patch']})
                self.assertEqual(result['resolved_department_scope']['department_count'],117)
                self.assertEqual(result['resolved_department_scope']['root_ids'],['3'])
                self.assertFalse(result['resolved_department_scope']['members_included'])
                self.assertLess(response_chars(result),12000)
        for code in ('DATABASE_SQL_UNSUPPORTED','DATABASE_CAPABILITY_DENIED','INVALID_DATABASE_ARGUMENTS'):
            self.assertFalse(rejection_result(DatabaseRejected(code))['recovery']['retry_same_request'])
