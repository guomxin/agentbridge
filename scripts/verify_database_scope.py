"""Read-only live acceptance using an existing subject's grants; no grant changes."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--subject', required=True)
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--date', required=True)
    args = parser.parse_args()
    if args.candidate:
        for name in ('content', 'independent'):
            key = 'agentbridge.database.' + name
            spec = importlib.util.spec_from_file_location(key, args.candidate / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            spec.loader.exec_module(module)
    from agentbridge.database.independent import IndependentDatabase, DatabaseRejected
    from datetime import date, timedelta
    runtime = IndependentDatabase(args.home)
    def call(cap, arguments):
        return runtime.execute(args.subject, cap, arguments, args.source_id)
    directory = []
    params = {'entity': 'departments'}
    while True:
        r = call('database.directory', params)
        directory.extend(r['rows'])
        if not r['has_more']: break
        params['after_id'] = r['next_after_id']
        assert len(directory) < 10000
    assert r['directory_semantics']['hierarchy_available']
    ids = {x['id'] for x in directory}
    assert len(ids) == len(directory)
    roots = sorted({x['parent_id'] for x in directory if x.get('parent_id') in ids}, key=int)
    assert roots, 'No hierarchy available for acceptance'
    window = {'start_date': args.date, 'end_date_exclusive': str(date.fromisoformat(args.date)+timedelta(days=1))}
    checks = []
    for root in roots[:3]:
        expected = {root}
        while True:
            expanded = expected | {x['id'] for x in directory if x.get('parent_id') in expected}
            if expanded == expected: break
            expected = expanded
        query = {**window, 'department_id': root, 'include_descendants': True, 'page_size': 7}
        seen = set()
        pages = 0
        while True:
            r = call('database.logs.content_analyze', query)
            assert {x['id'] for x in r['resolved_department_scope']['departments']} == expected
            current = {x['id'] for x in r['rows']}
            assert not seen & current
            seen |= current
            pages += 1
            if not r['has_more']: break
            query['after'] = r['next_cursor']
            assert pages < 500
        assert len(seen) == r['total_matching'], 'Dataset changed during acceptance'
        aggregate = call('database.logs.analyze', {**window, 'department_id': root, 'include_descendants': True, 'group_by': 'none'})
        assert aggregate['rows'][0]['log_count'] == len(seen)
        child = next((x for x in expected if x != root), root)
        overlap = call('database.logs.analyze', {**window, 'department_ids': [root, child], 'include_descendants': True, 'group_by': 'none'})
        assert overlap['rows'][0]['log_count'] == len(seen)
        checks.append({'root_id': root, 'departments': len(expected), 'logs': len(seen), 'pages': pages})
    print(json.dumps({'status': 'passed', 'directory_count': len(directory), 'checks': checks,
                      'authorization': 'existing_subject_grants', 'business_writes': 0}))


if __name__ == '__main__':
    main()
