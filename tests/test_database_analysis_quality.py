import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('quality',ROOT/'scripts/evaluate_database_analysis.py')
quality=importlib.util.module_from_spec(spec); spec.loader.exec_module(quality)
CASES=json.loads((ROOT/'tests/fixtures/analysis_quality.json').read_text(encoding='utf-8'))['cases']


class QualityTests(unittest.TestCase):
    def test_oracle_covers_all_named_risks(self):
        self.assertEqual(len(CASES),7)
        for case in CASES:
            with self.subTest(case=case['id']):
                self.assertTrue(quality.evaluate(case,quality.oracle_answer(case))['passed'])

    def test_each_failure_dimension_is_separately_reported(self):
        case=next(c for c in CASES if c['id']=='adjacent-irrelevant')
        mutations={
            'scope':lambda a:a['query_scope'].update(start_date='2020-01-01'),
            'completeness':lambda a:a.update(complete=False),
            'relevance':lambda a:a['items'][1].update(destination='main'),
            'support':lambda a:a['items'][1].update(state='completed'),
        }
        for dimension,mutate in mutations.items():
            with self.subTest(dimension=dimension):
                answer=copy.deepcopy(quality.oracle_answer(case)); mutate(answer)
                result=quality.evaluate(case,answer)
                self.assertEqual([k for k,v in result['errors'].items() if v],[dimension])

    def test_negative_claims_empty_results_and_unread_fragments_fail(self):
        for case in CASES:
            answer=copy.deepcopy(quality.oracle_answer(case))
            answer['unsupported_metrics']=['日志条数折算人日']
            self.assertFalse(quality.evaluate(case,answer)['passed'])
            answer=copy.deepcopy(quality.oracle_answer(case)); answer['unread_fragments']=1
            result=quality.evaluate(case,answer)
            self.assertTrue(result['errors']['completeness']); self.assertTrue(result['errors']['support'])
        empty=next(c for c in CASES if c['id']=='empty')
        answer=quality.oracle_answer(empty); answer['items']=[{'id':'invented-completion'}]
        self.assertFalse(quality.evaluate(empty,answer)['passed'])

    def test_fixed_cases_pass_through_real_query_and_evidence_contracts(self):
        from agentbridge.database.content import compile_query, add_evidence
        from agentbridge.database.evidence import prepare_evidence_page
        for case in CASES:
            with self.subTest(case=case['id']):
                args=copy.deepcopy(case['scope'])
                plan=compile_query('database.logs.content_analyze',args)
                rows=[]
                for source in case['sources']:
                    paragraphs=list(source['paragraphs'])
                    if paragraphs: paragraphs[0]*=source.get('repeat_first',1)
                    rows.append({'id':int(source['id']),'content':'\n'.join(paragraphs),
                                 'log_date':'2026-09-01','fullname':'合成评测来源'})
                result={'source_id':'fixture','rows':rows,'columns':[],'truncated':False}
                add_evidence(result,plan,len(rows))
                result=prepare_evidence_page(result,plan,args,'https://fixture.invalid')
                for row in result['rows']:
                    self.assertIn('revision=',row['source_url'])
                    self.assertTrue(row['passages'])
                if case['id']=='long-text':
                    self.assertFalse(result['content_complete'])
                    self.assertIsNotNone(result['rows'][0]['next_text_offset'])


if __name__=='__main__': unittest.main()
