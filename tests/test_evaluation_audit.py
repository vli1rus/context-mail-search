import json
import math
import tempfile
import unittest
from pathlib import Path

from mailsearch.data import Letter, load_letters
from mailsearch.engine import SearchEngine
from mailsearch.evaluate import evaluate, metrics, read_qrels, read_queries


class EvaluationAudit(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine([
            Letter('a','box','item','person','2020-01-01','alpha'),
            Letter('b','other','item','person','2020-01-01','beta'),
            Letter('c','box','item','person','2021-01-01','gamma')])
        self.query = [('q','item','box','2020-06-01')]

    def run_eval(self, judgments):
        return evaluate(self.engine,self.query,judgments,['lexical'],[1],1,1)[0]['lexical']

    def test_invalid_judgments(self):
        for g in ({},{'q':{'missing':1}},{'q':{'b':1}},{'q':{'c':1}}):
            with self.subTest(g=g), self.assertRaises(ValueError):
                self.run_eval(g)
        result=self.run_eval({'q':{}})
        self.assertEqual(result['judged_without_positives'],1)
        self.assertNotIn('Recall@1',result)
        self.assertEqual(self.run_eval(None)['scored_queries'],0)

    def test_graded_and_duplicate_ranking(self):
        recall,ndcg,mrr=metrics(['x','b','a'],{'a':2,'b':1},3)
        self.assertEqual((recall,mrr),(1,.5))
        self.assertAlmostEqual(ndcg,(1/math.log2(3)+3/2)/(3+1/math.log2(3)))
        with self.assertRaises(ValueError):
            metrics(['a','a'],{'a':1},2)

    def test_file_contracts(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'rows.jsonl'
            path.write_text(json.dumps(dict(query_id='q',query='item',mailbox='box',as_of='2020-06-01')))
            self.assertEqual(list(read_queries(path)),self.query)
            for f,row in [(read_qrels,dict(query_id='q',doc_id='a',relevance=0)),
                          (load_letters,self.engine.letters[0].serializable())]:
                path.write_text((json.dumps(row)+'\n')*2)
                with self.assertRaises(ValueError):
                    f(path)

    def test_global_idf_uses_future_documents(self):
        from dataclasses import replace
        a=replace(self.engine.letters[0],body='alpha')
        b=replace(a,doc_id='b',body='beta')
        future=replace(a,doc_id='future',date='2022-01-01')
        def first(letters):
            engine=SearchEngine(letters)
            hit=engine.search('alpha beta','lexical',1,mailbox='box',as_of='2020-06-01')[0]
            return engine.letters[hit.index].doc_id
        self.assertEqual((first([a,b]),first([a,b,future])),('a','b'))
