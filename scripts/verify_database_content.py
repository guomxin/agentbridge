"""Live read-only content acceptance, with isolated grants and no body output."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile


def load_candidate(path):
    for name, file in [('agentbridge.database.content', path.with_name('content.py')),
                       ('database_content_candidate', path)]:
        spec = importlib.util.spec_from_file_location(name, file)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--module', type=Path, help='Optional candidate independent.py alongside content.py')
    args = parser.parse_args()
    if args.module:
        module = load_candidate(args.module)
    else:
        from agentbridge.database import independent as module
    with tempfile.TemporaryDirectory(prefix='database-content-acceptance-') as root:
        runtime = module.IndependentDatabase.__new__(module.IndependentDatabase)
        runtime.home = args.home
        runtime.grants = module.DatabaseGrants(Path(root)/'grants.sqlite3')
        runtime.grants.set('content-acceptance', list(module.CAPABILITIES))
        checks, facts = [], {}

        def call(capability, arguments):
            return runtime.execute('content-acceptance', capability, arguments)

        window = {'start_date': '2026-08-31', 'end_date_exclusive': '2026-09-07'}
        catalog = runtime.catalog('content-acceptance')['capabilities']
        assert len(catalog) == 7 and all('input_schema' in item for item in catalog)
        checks.append('seven_discoverable_capability_schemas')
        total = call('database.logs.analyze', {**window, 'group_by': 'none'})['rows'][0]['log_count']
        seen, pages, after = set(), 0, None
        while True:
            arguments = {**window, 'page_size': 37, 'mode': 'summary'}
            if after:
                arguments['after'] = after
            result = call('database.logs.content_analyze', arguments)
            assert result['total_matching'] == total, 'Live dataset changed during acceptance; retry on a stable range.'
            assert result['snapshot_scope'] == 'single_request' and result['coverage'] == 'page'
            assert result['analysis']['status'] == 'evidence_ready'
            for row in result['rows']:
                assert row['id'] not in seen
                assert row['evidence_id'] == f'log:{row["id"]}'
                assert 'log_content' not in row and 'commenter_id' not in row
                seen.add(row['id'])
            pages += 1
            if not result['has_more']:
                break
            after = result['next_cursor']
            assert after and pages < 100
        assert len(seen) == total and pages > 1
        checks.append('all_log_pages_reconcile_without_duplicates_on_stable_range')
        facts.update(window=window, matching_logs=total, pages=pages)
        for mode in ('topics','progress','issues','experience','collaboration','changes'):
            result = call('database.logs.content_analyze', {**window, 'mode': mode, 'page_size': 1})
            assert result['analysis']['mode'] == mode and result['rows'][0]['passages']
        checks.append('all_seven_content_modes_have_source_bound_evidence')
        conditions = {**window, 'keywords': ['项目','付款'], 'keyword_mode': 'all'}
        searched = call('database.logs.query', conditions)
        counted = call('database.logs.analyze', {**conditions, 'group_by': 'none'})['rows'][0]['log_count']
        assert searched['total_matching'] == counted
        assert all('项目' in r['content'] and '付款' in r['content'] for r in searched['rows'])
        assert all(r['matches'] for r in searched['rows'])
        facts['project_and_payment_logs'] = counted
        checks.append('literal_keyword_search_matches_aggregate')
        none = call('database.logs.query', {**window, 'keyword': "%_' OR 1=1 --"})
        assert none['total_matching'] == 0 and not none['rows']
        checks.append('literal_wildcards_and_sql_text_do_not_expand_results')
        initial = call('database.logs.query', {**window, 'page_size': 1})
        try:
            call('database.logs.query', {**window, 'page_size': 1, 'after': initial['next_cursor'], 'keyword': 'changed'})
        except module.DatabaseRejected as exc:
            assert str(exc) == 'INVALID_DATABASE_ARGUMENTS'
        else:
            raise AssertionError('changed filters accepted')
        checks.append('cursor_rejects_changed_scope')
        comments_window = {'start_date': '2026-09-01', 'end_date_exclusive': '2026-09-12'}
        basis_window = {'start_date': '2026-09-09', 'end_date_exclusive': '2026-09-10'}
        for basis in ('comment_created_at','log_date'):
            result = call('database.comments.analyze', {**basis_window, 'date_basis': basis, 'page_size': 200})
            column = 'm.created_at' if basis == 'comment_created_at' else 'l.log_date'
            sql = f"SELECT count(*) AS n FROM analysis.work_log_comments m JOIN analysis.work_logs l ON l.id=m.work_log_id WHERE {column} >= DATE '2026-09-09' AND {column} < DATE '2026-09-10' AND l.type_code='DAILY'"
            expected = call('database.free.read', {'sql': sql})['rows'][0]['n']
            assert result['total_matching'] == expected and result['rows']
            assert all(r['evidence_id'].startswith('comment:') and r['log_evidence_id'].startswith('log:') for r in result['rows'])
            facts[f'comments_by_{basis}'] = expected
        checks.append('both_comment_date_bases_reconcile_with_free_sql')
        for mode in ('questions','followup'):
            result = call('database.comments.analyze', {**comments_window, 'mode': mode, 'page_size': 1})
            assert result['analysis']['mode'] == mode
        checks.append('all_comment_modes_have_explicit_evidence_contract')
        # Comment paging uses timestamp/id positions, not log_date.
        ids, after, expected_count = set(), None, None
        for _ in range(100):
            result = call('database.comments.analyze', {**comments_window, 'page_size': 2, **({'after': after} if after else {})})
            expected_count = result['total_matching'] if expected_count is None else expected_count
            assert expected_count == result['total_matching']
            for row in result['rows']:
                assert row['id'] not in ids
                ids.add(row['id'])
            if not result['has_more']:
                break
            after = result['next_cursor']
        assert len(ids) == expected_count
        checks.append('comment_timestamp_pagination_reconciles')
        runtime.grants.set('content-acceptance', ['database.logs.content_analyze'])
        for denied in ('database.comments.analyze','database.free.read'):
            try:
                call(denied, comments_window)
            except module.DatabaseRejected as exc:
                assert str(exc) == 'DATABASE_CAPABILITY_DENIED'
            else:
                raise AssertionError('separate capability grant bypassed')
        checks.append('content_grant_does_not_imply_comments_or_free_sql')
        print(json.dumps({'status': 'passed', 'checks': checks, 'facts': facts,
                          'queried_at': result['queried_at'], 'business_api_calls': 0,
                          'production_grants_changed': False, 'bodies_in_report': False}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
