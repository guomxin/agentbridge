"""Score reviewer-normalized analysis answers against a fixed, source-bound oracle.

This checks labeled claims, not the meaning of arbitrary prose. A reviewer must
first map every sentence/table claim to items and flag unsupported metrics.
"""
import argparse
import json
from pathlib import Path


def evaluate(case, answer):
    errors={k:[] for k in ('scope','completeness','relevance','support')}
    if answer.get('query_scope') != case['scope']:
        errors['scope'].append('query_scope_changed')
    sources={s['id']:s for s in case['sources']}
    expected={item['id']:item for item in case['items']}
    items=answer.get('items',[])
    actual={item['id']:item for item in items}
    if len(items)!=len(actual): errors['completeness'].append('duplicate_item')
    missing=set(expected)-set(actual)
    if missing: errors['completeness'].append('missing_items:'+','.join(sorted(missing)))
    seen=set(answer.get('seen_sources',[]))
    if seen-set(sources): errors['scope'].append('outside_scope_sources')
    if set(sources)-seen: errors['completeness'].append('unread_sources')
    if answer.get('complete') is not True:
        errors['completeness'].append('coverage_not_complete')
    if answer.get('unread_fragments',0):
        errors['completeness'].append('unread_fragments')
    if (missing or set(sources)-seen or answer.get('unread_fragments',0)) and answer.get('complete') is True:
        errors['support'].append('unjustified_complete_claim')
    for key,item in actual.items():
        target=expected.get(key)
        if target is None:
            errors['support'].append('unsupported_item:'+key)
            continue
        if item.get('destination')!=target['destination']:
            errors['relevance'].append('wrong_destination:'+key)
        if item.get('state')!=target['state']:
            errors['support'].append('unsupported_state:'+key)
        reference={'source':target['source'],'paragraph':target['paragraph']}
        if item.get('evidence')!=reference:
            errors['support'].append('wrong_evidence:'+key)
    if answer.get('unsupported_metrics'):
        errors['support'].append('unsupported_effort_attendance_or_performance')
    if answer.get('reviewed_all_claims') is not True:
        errors['support'].append('human_claim_review_required')
    return {'case_id':case['id'],'passed':not any(errors.values()),'errors':errors}


def oracle_answer(case):
    return {'query_scope':case['scope'],'seen_sources':[s['id'] for s in case['sources']],
            'complete':True,'unread_fragments':0,'reviewed_all_claims':True,'unsupported_metrics':[],
            'items':[{'id':i['id'],'state':i['state'],'destination':i['destination'],
                      'evidence':{'source':i['source'],'paragraph':i['paragraph']}} for i in case['items']]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',required=True,type=Path)
    parser.add_argument('--answers',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    cases=json.loads(args.cases.read_text(encoding='utf-8'))['cases']
    submission=json.loads(args.answers.read_text(encoding='utf-8'))
    answers=submission['answers']
    results=[evaluate(case,answers.get(case['id'],{})) for case in cases]
    report={'schema':'agentbridge.analysis-quality-result.v1','run':submission.get('run',{}),
            'basis':'reviewer-normalized claims; not an automatic semantic judge; reviewer identity is in run',
            'passed':all(r['passed'] for r in results),'cases':results,
            'failure_counts':{k:sum(bool(r['errors'][k]) for r in results) for k in ('scope','completeness','relevance','support')}}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'failure_counts':report['failure_counts']},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
