"""Read-only evidence/source acceptance under existing database grants."""
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
    parser.add_argument('--date', required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--max-chars', type=int, default=12000)
    parser.add_argument('--capability', choices=['database.logs.query', 'database.logs.content_analyze'], default='database.logs.content_analyze')
    parser.add_argument('--department-id')
    parser.add_argument('--log-type', choices=['DAILY','WEEKLY'])
    args = parser.parse_args()
    if args.candidate:
        for name in ('content', 'evidence', 'independent'):
            key = 'agentbridge.database.' + name
            spec = importlib.util.spec_from_file_location(key, args.candidate / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            spec.loader.exec_module(module)
    from agentbridge.database.independent import IndependentDatabase
    from datetime import date, timedelta
    runtime = IndependentDatabase(args.home, original_base_url=args.base_url)
    from agentbridge.database.evidence import response_chars
    discovery = runtime.discover(args.subject)
    assert response_chars(discovery) < 14000
    detail = runtime.discover(args.subject, args.source_id, args.capability)
    assert response_chars(detail) < 14000
    def call(cap, arguments):
        return runtime.execute(args.subject, cap, arguments, args.source_id)
    query = {'start_date': args.date, 'end_date_exclusive': str(date.fromisoformat(args.date)+timedelta(days=1)),
             'page_size': 200, 'max_chars': args.max_chars}
    if args.department_id:
        query.update(department_id=args.department_id, include_descendants=True)
    if args.log_type:
        query['log_type'] = args.log_type
    seen, pages, chars, sources = set(), 0, [], 0
    while True:
        result = call(args.capability, query)
        chars.append(response_chars({'status':'succeeded', **result}))
        assert chars[-1] <= args.max_chars
        assert len(result['analysis']['coverage_manifest']) == len(result['rows'])
        for row in result['rows']:
            assert row['id'] not in seen
            seen.add(row['id'])
            original = runtime.read_original(args.subject, args.source_id, str(row['id']))
            assert original['revision'] == row['source_revision_hash'], 'Content changed during acceptance'
            assert row['source_url'].startswith(args.base_url+'/database/logs/')
            paragraphs = {p['number']:p['text'].strip() for p in original['paragraphs'] if p['text'].strip()}
            if row.get('content_complete', True):
                assert [p['text'] for p in row['passages']] == list(paragraphs.values())
            else:
                while row['next_text_offset'] is not None:
                    row = call(args.capability, {'log_id':str(row['id']), 'max_chars':args.max_chars,
                        'text_offset':row['next_text_offset'], 'expected_revision':row['source_revision_hash']})['rows'][0]
            sources += 1
        pages += 1
        if not result['has_more']: break
        query['after'] = result['next_cursor']
        assert pages < 500
    assert len(seen) == result['total_matching'], 'Dataset changed during acceptance'
    print(json.dumps({'status':'passed', 'capability':args.capability, 'budget':args.max_chars,
                      'discovery_chars':response_chars(discovery), 'schema_chars':response_chars(detail),
                      'logs':len(seen), 'pages':pages, 'max_response_chars':max(chars),
                      'originals_verified':sources, 'authorization':'existing_subject_grants', 'business_writes':0}))


if __name__ == '__main__':
    main()
