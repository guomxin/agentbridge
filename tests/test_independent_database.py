from tests.database_fixtures import configured_source
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from agentbridge.database.independent import DatabaseGrants, DatabaseRejected, IndependentDatabase, log_query, validate_sql


class IndependentDatabaseTests(unittest.TestCase):
    def test_mcp_uses_authenticated_central_subject_not_model_identity(self):
        from starlette.testclient import TestClient
        from agentbridge.core.mcp_identities import McpIdentityTokenStore
        from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
        with tempfile.TemporaryDirectory() as root:
            store = McpIdentityTokenStore(Path(root)/'tokens.db')
            identity = store.issue(user_subject='central-a',expected_principal_ref='unused')
            service = MagicMock()
            service.home = Path(root)
            configured_source(root)
            runtime = IndependentDatabase(root)
            runtime.grants.set('central-a',['database.logs.analyze'])
            server = create_central_mcp_server(service=service,identity_store=store,
                config=validate_central_mcp_server_config(host='127.0.0.1',port=8790,public_base_url='http://testserver',tls_cert=None,tls_key=None),
                auth_card_base_url='http://127.0.0.1:8780')
            headers = {'Accept':'application/json, text/event-stream','Authorization':'Bearer '+identity['token'],'MCP-Protocol-Version':'2025-06-18'}
            with TestClient(server.streamable_http_app()) as client:
                def call(name,arguments):
                    return client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':'1','method':'tools/call','params':{'name':name,'arguments':arguments}}).json()['result']['structuredContent']
                self.assertEqual(call('database_capabilities',{})['sources'][0]['capabilities'][0]['name'],'database.logs.analyze')
                with patch.object(IndependentDatabase,'_execute',return_value={'query_id':'q','sql_sha256':'h','truncated':False}) as execute:
                    self.assertEqual(call('database_execute',{'capability':'database.logs.analyze','arguments':{}})['status'],'succeeded')
                    self.assertEqual(execute.call_args.args[0],'central-a')
                    runtime.grants.set('central-a',[])
                    self.assertEqual(call('database_execute',{'capability':'database.logs.analyze','arguments':{}})['code'],'DATABASE_CAPABILITY_DENIED')
                    self.assertEqual(execute.call_count,1)
                    runtime.grants.set('central-a',['database.logs.content_analyze','database.comments.analyze'])
                    discovered = call('database_capabilities',{})['sources'][0]['capabilities']
                    self.assertEqual({item['name'] for item in discovered}, {'database.logs.content_analyze','database.comments.analyze'})
                    self.assertTrue(all('input_schema' not in item for item in discovered))
                    for capability in ('database.logs.content_analyze','database.comments.analyze'):
                        detail = call('database_capabilities',{'source_id':'taihua_primary','capability':capability})
                        self.assertIn('input_schema',detail['capability'])
                        self.assertEqual(call('database_execute',{'capability':capability,'arguments':{}})['status'],'succeeded')
                        self.assertEqual(execute.call_args.args[0],'central-a')
            service.start_login.assert_not_called()
            service.invoke.assert_not_called()
    def test_grant_is_subject_scoped_and_revocable_without_tokens(self):
        with tempfile.TemporaryDirectory() as root:
            a = DatabaseGrants(Path(root)/'grants.db')
            b = DatabaseGrants(Path(root)/'grants.db')
            with self.assertRaises(DatabaseRejected):
                a.authorize('a','database.free.read')
            a.set('a',['database.free.read'])
            self.assertEqual(b.authorize('a','database.free.read')['revision'],1)
            with self.assertRaises(DatabaseRejected):
                b.authorize('b','database.free.read')
            a.set('a',[])
            with self.assertRaises(DatabaseRejected):
                b.authorize('a','database.free.read')

    def test_denied_before_source_or_business_session(self):
        with tempfile.TemporaryDirectory() as root:
            configured_source(root)
            runtime = IndependentDatabase(root)
            with self.assertRaisesRegex(DatabaseRejected,'DATABASE_CAPABILITY_DENIED'):
                runtime.execute('a','database.free.read',{'sql':'select 1'})

    def test_sql_supports_joins_ctes_aggregates_and_windows(self):
        queries = [
            'SELECT count(*),sum(hours) FROM analysis.work_logs',
            'WITH q AS (SELECT user_id,sum(hours) h FROM analysis.work_logs GROUP BY user_id) SELECT q.*,u.fullname FROM q JOIN analysis.users u ON u.id=q.user_id',
            "SELECT date_trunc('month',log_date)::date,count(*) FROM analysis.work_logs GROUP BY 1",
            'SELECT row_number() OVER (ORDER BY log_date),id FROM analysis.work_logs',
            "SELECT id FROM analysis.work_logs WHERE content ILIKE '%培训%'",
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(validate_sql(query))

    def test_rejects_writes_escape_functions_and_other_tables(self):
        queries = [
            'DELETE FROM analysis.work_logs',
            'SELECT 1; SELECT 2',
            'WITH q AS (DELETE FROM analysis.work_logs RETURNING *) SELECT * FROM q',
            'SELECT * INTO TEMP x FROM analysis.work_logs',
            'SELECT * FROM analysis.work_logs FOR UPDATE',
            "SELECT pg_sleep(1)","SELECT set_config('search_path','public',false)",
            "SELECT pg_read_file('/etc/passwd')",'SELECT * FROM public.work_log',
            'SELECT * FROM pg_catalog.pg_authid','SELECT * FROM analysis.work_logs l, generate_series(1,100) x',
            "SELECT 'x'::public.custom_type",'SELECT public.sum(hours) FROM analysis.work_logs',
            'WITH RECURSIVE q AS (SELECT 1) SELECT * FROM q',
            'SELECT 1 OPERATOR(public.+) 2',
            'WITH q AS (WITH pg_authid AS (SELECT 1) SELECT 1) SELECT * FROM pg_authid',
        ]
        for query in queries:
            with self.subTest(query=query), self.assertRaises(DatabaseRejected):
                validate_sql(query)

    def test_query_parameterizes_person_without_identity_mapping(self):
        sql, params = log_query({'start_date':'2026-01-01','end_date_exclusive':'2026-09-01','user_id':'300002378','group_by':'person'},aggregate=True)
        self.assertNotIn('300002378',sql)
        self.assertEqual(params[-1],300002378)
        self.assertIn('count(DISTINCT (l.user_id,l.log_date))',sql)
        self.assertNotIn('status=true',sql)

    def test_invalid_input_cannot_change_query_structure(self):
        for arguments in [
            {'start_date':'2026-01-01','end_date_exclusive':'2026-09-01','group_by':'project;DELETE'},
            {'start_date':'2026-01-01','end_date_exclusive':'2026-09-01','user_id':'1 OR 1=1'},
            {'start_date':'2026-01-01','end_date_exclusive':'2026-09-01','subject':'someone'},
            {'start_date':'2026-09-01','end_date_exclusive':'2026-01-01'},
        ]:
            with self.subTest(arguments=arguments),self.assertRaises(DatabaseRejected):
                log_query(arguments,aggregate=True)

if __name__ == '__main__':
    unittest.main()
