import base64
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bscli.database.independent import IndependentDatabase, DatabaseGrants, DatabaseRejected, validate_sql
from bscli.database.sources import Sources, SourceConflict, validate
from bscli.database.reports import Reports
from tests.database_fixtures import configured_source


class MultiSourceTests(unittest.TestCase):
    def test_grant_migration_is_idempotent_and_never_restores_revocation(self):
        with tempfile.TemporaryDirectory() as home:
            path=Path(home)/'grants.db'
            with closing(sqlite3.connect(path)) as c,c:
                c.execute('CREATE TABLE database_grants(subject TEXT PRIMARY KEY,capabilities TEXT,revision INTEGER)')
                c.execute('INSERT INTO database_grants VALUES (?,?,?)',('a','["database.free.read"]',4))
            grants=DatabaseGrants(path)
            self.assertEqual(grants.get('a')['revision'],4)
            self.assertEqual(grants.get('a','other')['capabilities'],[])
            grants.set('a',[])
            self.assertEqual(DatabaseGrants(path).get('a')['capabilities'],[])

    def test_catalog_source_filter_grants_and_disable(self):
        with tempfile.TemporaryDirectory() as home:
            configured_source(home)
            configured_source(home,'equipment',pack='generic')
            runtime=IndependentDatabase(home)
            runtime.grants.set('a',['database.logs.analyze'])
            runtime.grants.set('b',['database.free.read'],source_id='equipment')
            self.assertEqual([x['source_id'] for x in runtime.catalog('a')['sources']],['taihua_primary'])
            with self.assertRaises(DatabaseRejected): runtime.catalog('a','equipment')
            runtime.grants.set('a',['database.free.read'],source_id='equipment')
            self.assertTrue(runtime.catalog('a')['selection_required'])
            self.assertEqual([x['name'] for x in runtime.catalog('a','equipment')['capabilities']],['database.free.read'])
            with self.assertRaises(DatabaseRejected): runtime.execute('a','database.logs.query',{},'equipment')
            Sources(home).write('equipment','disable',revision=1,actor='admin',reason='fixture')
            self.assertEqual(runtime.catalog('b')['sources'],[])
            self.assertEqual(runtime.catalog('a')['sources'][0]['source_id'],'taihua_primary')

    def test_sql_whitelist_is_per_source(self):
        self.assertTrue(validate_sql('SELECT count(*) FROM public.devices',['public.devices']))
        for sql in ('SELECT * FROM analysis.users','SELECT * FROM devices','SELECT * FROM pg_catalog.pg_roles',
                    'WITH d AS (DELETE FROM public.devices RETURNING *) SELECT * FROM d'):
            with self.subTest(sql=sql), self.assertRaises(DatabaseRejected): validate_sql(sql,['public.devices'])

    def test_draft_preflight_activation_conflict_and_failure(self):
        with tempfile.TemporaryDirectory() as home:
            record=configured_source(home,'equipment',pack='generic')
            sources=Sources(home)
            config={k:v for k,v in record['draft'].items() if k!='credential_ref'}
            config['name']='New name'
            sources.write('equipment','save',revision=1,actor='admin',reason='edit',config=config)
            self.assertEqual(sources.get('equipment')['active']['name'],'equipment')
            with self.assertRaises(ValueError): sources.write('equipment','enable',revision=2,actor='admin',reason='enable')
            with patch('bscli.database.sources.preflight',side_effect=DatabaseRejected('DATABASE_ROLE_REJECTED')):
                result=sources.write('equipment','preflight',revision=2,actor='admin',reason='check')
            self.assertEqual(result['preflight']['status'],'failed')
            self.assertEqual(result['active']['name'],'equipment')
            with patch('bscli.database.sources.preflight',return_value=[]):
                sources.write('equipment','preflight',revision=3,actor='admin',reason='check')
            sources.write('equipment','enable',revision=4,actor='admin',reason='enable')
            self.assertEqual(sources.get('equipment')['active']['name'],'New name')
            with self.assertRaises(SourceConflict): sources.write('equipment','disable',revision=4,actor='admin',reason='stale')
            self.assertNotIn('credential_ref',json.dumps(sources.public(sources.get('equipment'))))

    def test_reject_unsupported_engine_and_non_tls_new_source(self):
        with tempfile.TemporaryDirectory() as home:
            record=configured_source(home,'equipment',pack='generic')
            config={k:v for k,v in record['draft'].items() if k!='credential_ref'}
            for changes in ({'engine':'mysql'},{'sslmode':'disable'},{'port':True},{'allowed_relations':['public.devices;delete']},{'template_pack':'taihua_logs'}):
                with self.subTest(changes=changes), self.assertRaises(ValueError): validate({**config,**changes})

    def test_cursor_bound_to_source_and_revision(self):
        from bscli.database.content import compile_query
        args={'start_date':'2026-09-01','end_date_exclusive':'2026-09-02'}
        plan=compile_query('database.logs.query',args,source_scope='one:1:')
        args['after']={'id':'1','date':'2026-09-01','scope':plan.scope}
        compile_query('database.logs.query',args,source_scope='one:1:')
        for scope in ('two:1:','one:2:'):
            with self.assertRaises(ValueError): compile_query('database.logs.query',args,source_scope=scope)

    def test_report_permission_idempotency_csv_and_revocation(self):
        with tempfile.TemporaryDirectory() as home:
            configured_source(home,'equipment',pack='generic')
            runtime=IndependentDatabase(home)
            runtime.grants.set('a',['database.free.read'],source_id='equipment')
            reports=Reports(runtime)
            args={'query_capability':'database.free.read','query_arguments':{'sql':'SELECT name FROM public.devices'},'request_key':'once'}
            with self.assertRaises(DatabaseRejected): reports.export('a','equipment',args)
            runtime.grants.set('a',['database.free.read','database.report.export'],source_id='equipment')
            query={'columns':['name'],'rows':[{'name':'=1+2'},{'name':'中文设备'}],'queried_at':datetime.now(timezone.utc).isoformat(),'sql_sha256':'fingerprint'}
            with patch.object(runtime,'_execute',return_value=query) as execute:
                result=reports.export('a','equipment',args)
                again=reports.export('a','equipment',args)
                self.assertEqual(result['report_id'],again['report_id'])
                self.assertEqual(execute.call_count,1)
                with self.assertRaisesRegex(DatabaseRejected,'KEY_CONFLICT'):
                    reports.export('a','equipment',{**args,'query_arguments':{'sql':'select 1'}})
            rid=result['report_id']
            delivery=reports.download('a','equipment',{'report_id':rid})
            self.assertEqual(delivery['reportExpiresAt'], delivery['expires_at'])
            self.assertEqual(delivery['downloadExpiresAt'], delivery['download_expires_at'])
            self.assertLess(delivery['downloadExpiresAt'], delivery['reportExpiresAt'])
            stored = reports.payloads.load(rid)
            stored['download_expires_at'] = '2000-01-01T00:00:00+00:00'
            reports.payloads.save(rid, stored)
            with self.assertRaisesRegex(DatabaseRejected, 'DOWNLOAD_EXPIRED'):
                reports.web_download('a', 'equipment', rid)
            with patch.object(runtime, '_execute', side_effect=AssertionError('must not requery')):
                renewed = reports.download('a', 'equipment', {'report_id': rid})
            self.assertEqual(renewed['sha256'], delivery['sha256'])
            self.assertEqual(renewed['file'], delivery['file'])
            text=base64.b64decode(delivery['file']['content_base64']).decode('utf-8-sig')
            self.assertIn("'=1+2",text)
            self.assertIn('中文设备',text)
            self.assertEqual(reports.web_download('a','equipment',rid)['body'],base64.b64decode(delivery['file']['content_base64']))
            with self.assertRaises(DatabaseRejected): reports.download('b','equipment',{'report_id':rid})
            with self.assertRaises(DatabaseRejected): reports.download('a','taihua_primary',{'report_id':rid})
            runtime.grants.set('a',[],source_id='equipment')
            with self.assertRaises(DatabaseRejected): reports.download('a','equipment',{'report_id':rid})
            runtime.grants.set('a',['database.free.read','database.report.export'],source_id='equipment')
            with self.assertRaisesRegex(DatabaseRejected,'AUTHORIZATION_CHANGED'): reports.download('a','equipment',{'report_id':rid})

    def test_report_expires_and_never_reexecutes_failed_or_expired_key(self):
        with tempfile.TemporaryDirectory() as home:
            configured_source(home)
            runtime=IndependentDatabase(home)
            runtime.grants.set('a',['database.free.read','database.report.export'])
            reports=Reports(runtime)
            args={'query_capability':'database.free.read','query_arguments':{'sql':'select 1'},'request_key':'failure'}
            with patch.object(runtime,'_execute',side_effect=DatabaseRejected('DATABASE_QUERY_FAILED')) as execute:
                with self.assertRaises(DatabaseRejected): reports.export('a','taihua_primary',args)
                with self.assertRaisesRegex(DatabaseRejected,'REPORT_FAILED'): reports.export('a','taihua_primary',args)
                self.assertEqual(execute.call_count,1)

    def test_legacy_tools_registry_scopes_and_plan_are_retired(self):
        from bscli.core.central_service import CentralCapabilityService
        from bscli.core.mcp_identities import McpIdentityTokenStore
        from bscli.core.transforms import build_transform_registry
        with tempfile.TemporaryDirectory() as home:
            service=CentralCapabilityService(home=home,base_url='http://127.0.0.1:1')
            self.assertFalse(any(x.name.startswith('taihua.analytics.') for x in service.registry.list()))
            self.assertFalse(any('taihua_personal_compare' in x.name for x in build_transform_registry().list()))
            identities=McpIdentityTokenStore(service.db_path)
            token=identities.issue(user_subject='a',expected_principal_ref='unused')
            with closing(sqlite3.connect(service.db_path)) as c,c:
                # Old stored metadata remains authenticatable, but no longer grants analytics.
                table=c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%identity%' ").fetchall()
            with self.assertRaises(ValueError): identities.issue(user_subject='a',expected_principal_ref='unused',scopes=['taihua:analytics:read'])
            result=service.invoke(user_subject='a',capability_name='taihua.analytics.result.get',arguments={'result_id':'a'*32})
            self.assertEqual(result['error']['code'],'CAPABILITY_RETIRED')


if __name__=='__main__': unittest.main()
