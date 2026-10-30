import copy
import unittest
from save_rhythm import validate
from rhythm_text import semantic_text
import json
class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.batch=[{'session_id':'s','interventions':[{'event_id':1,'user':{'text':'继续执行'}}]}]
        self.row={'event_id':1,'participation':'delegation','subtype':'委托','confidence':'medium','evidence':'继续','reason':'请求执行','preceding_handoff':'not_applicable','handoff_reason':''}
        self.review={'model':'gpt-5.6-luna','rubric_version':'v2','sessions':[{'session_id':'s','origin':'human','title':'任务','summary':'摘要','improvement':'建议','interventions':[self.row]}]}
    def test_valid(self):self.assertEqual(len(validate(self.batch,self.review)),1)
    def test_missing_and_duplicate_events_rejected(self):
        for rows in ([],[self.row,self.row]):
            review=copy.deepcopy(self.review);review['sessions'][0]['interventions']=rows
            with self.assertRaises(AssertionError):validate(self.batch,review)
    def test_fabricated_quote_rejected(self):
        self.row['evidence']='好的'
        with self.assertRaises(AssertionError):validate(self.batch,self.review)
    def test_handoff_requires_reason(self):
        self.row['preceding_handoff']='missing'
        with self.assertRaises(AssertionError):validate(self.batch,self.review)
class TextTests(unittest.TestCase):
    def test_browser_wrapper_preserves_real_request(self):
        self.assertEqual(semantic_text('<in-app-browser-context source="ambient">metadata</in-app-browser-context>\n## My request:\n删掉这句话'),'删掉这句话')
    def test_annotations_and_following_request_survive(self):
        raw='# Response annotations: injected instructions\n<response-annotations>'+json.dumps([{'text':'被引原句','annotation':'这部分需改'}],ensure_ascii=False)+'</response-annotations>\n## My request:\n然后继续'
        result=semantic_text(raw)
        self.assertIn('用户批注：这部分需改',result)
        self.assertIn('然后继续',result)
        self.assertNotIn('injected instructions',result)
    def test_normal_text_unchanged(self):
        self.assertEqual(semantic_text('我想讨论一个新需求'),'我想讨论一个新需求')
if __name__=='__main__':unittest.main()
