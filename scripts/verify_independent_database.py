"""Live read-only acceptance; isolated grants, no production user grants changed."""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--module',required=True,type=Path)
    parser.add_argument('--home',required=True,type=Path)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('database_candidate',args.module)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix='database-acceptance-') as root:
        runtime = module.IndependentDatabase.__new__(module.IndependentDatabase)
        runtime.home = args.home
        runtime.grants = module.DatabaseGrants(Path(root)/'grants.db')
        runtime.grants.set('acceptance',list(module.CAPABILITIES), source_id='taihua_primary')
        checks = []
        def call(capability, arguments):
            return runtime.execute('acceptance',capability,arguments)
        schema = call('database.schema',{})
        assert len({r['table_name'] for r in schema['rows']}) == 5
        checks.append('five_views')
        lookup = call('database.directory',{'entity':'users','keyword':'xinguomao'})
        assert len(lookup['rows']) == 1
        checks.append('directory_lookup')
        window = {'start_date':'2026-08-31','end_date_exclusive':'2026-09-07'}
        total = call('database.logs.analyze',{**window,'group_by':'none'})
        for group in ['day','month','person','department','project']:
            result = call('database.logs.analyze',{**window,'group_by':group})
            if not result['truncated']:
                from decimal import Decimal
                assert sum(int(r['log_count']) for r in result['rows']) == int(total['rows'][0]['log_count'])
                assert sum(Decimal(r['registered_hours']) for r in result['rows']) == Decimal(total['rows'][0]['registered_hours'])
            checks.append('group_'+group)
        personal = call('database.logs.analyze',{**window,'user_id':'300002517','group_by':'none'})
        assert personal['rows'][0]['log_count'] == 3 and personal['rows'][0]['registered_hours'] == '3.5'
        checks.append('historical_sample_3_logs_3_5_hours')
        free = call('database.free.read',{'sql':"SELECT count(*) AS n,sum(hours) AS h FROM analysis.work_logs WHERE log_date >= DATE '2026-08-31' AND log_date < DATE '2026-09-07' AND type_code='DAILY'"})
        assert free['rows'][0]['n'] == total['rows'][0]['log_count']
        assert free['rows'][0]['h'] == total['rows'][0]['registered_hours']
        checks.append('standard_free_reconciliation')
        details = call('database.logs.query',{'start_date':'2017-01-01','end_date_exclusive':'2026-09-12'})
        assert details['truncated'] and len(details['rows']) == 200
        checks.append('bounded_details')
        runtime.grants.set('acceptance',[], source_id='taihua_primary')
        try:
            call('database.free.read',{'sql':'SELECT 1'})
        except module.DatabaseRejected as exc:
            assert str(exc) == 'DATABASE_CAPABILITY_DENIED'
        else:
            raise AssertionError('revocation failed')
        checks.append('revocation')
        print(json.dumps({'status':'passed','checks':checks,'window_total':total['rows'],
                          'database_read_at':total['queried_at'],'business_api_calls':0,
                          'production_grants_changed':False},ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
