import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from save_dataset import save

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.dataset=self.root/'input';self.semantic=self.root/'semantic';self.storage=self.root/'data'
        self.dataset.mkdir();self.semantic.mkdir()
        def write(name,rows):
            (self.dataset/name).write_text(''.join(json.dumps(r)+'\n' for r in rows))
        write('sessions.jsonl',[{'session_id':'s','session_kind':'primary','cwd':'/project'}])
        event={'session_id':'s','turn_id':'t','kind':'message','role':'assistant','text':'hello','source_file':'sessions/a.jsonl','source_line':2}
        write('events.jsonl',[event]);write('primary_messages.jsonl',[event]);write('manifest.jsonl',[{'source_file':'sessions/a.jsonl','bytes':20,'sha256':'example'}])
        (self.dataset/'report.json').write_text('{}')
        (self.dataset/'timing.json').write_text(json.dumps({'turns':[{'session_id':'s','turn_id':'t','total_ms':1000}],'replies':[{**event,'elapsed_from_start_ms':1000}]}))
        s={'session_id':'s','origin':'human','flags':[{'key':'sessions/a.jsonl:2','turn_id':'t','label':'clarification','reason':'example'}],'linear_tickets':['TOK-1']}
        (self.semantic/'review1.json').write_text(json.dumps({'sessions':[s]}));(self.semantic/'review2.json').write_text('{"sessions":[]}')
        for name in ['batch1.json','batch2.json','linear_context.json']:(self.semantic/name).write_text('[]')
    def save(self):
        with contextlib.redirect_stdout(io.StringIO()):return save(self.dataset,self.semantic,self.storage)
    def test_repeat_is_idempotent_and_relations_queryable(self):
        path=self.save();before=(path/'sessions.sqlite').read_bytes()
        self.assertEqual(path,self.save());self.assertEqual(before,(path/'sessions.sqlite').read_bytes())
        with sqlite3.connect(path/'sessions.sqlite') as db:
            self.assertEqual(db.execute('SELECT text,turn_total_ms FROM reply_analysis').fetchone(),('hello',1000.0))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('SELECT verified FROM session_issues').fetchone()[0],0)
    def test_new_data_keeps_previous_snapshot(self):
        old=self.save();(self.dataset/'report.json').write_text('{"new":true}');new=self.save()
        self.assertNotEqual(old,new);self.assertTrue((old/'sessions.sqlite').exists())
        self.assertEqual(json.loads((self.storage/'current.json').read_text())['snapshot_id'],new.name)
    def test_invalid_evidence_does_not_publish(self):
        old=self.save();pointer=(self.storage/'current.json').read_bytes()
        x=json.loads((self.semantic/'review1.json').read_text());x['sessions'][0]['flags'][0]['turn_id']='wrong'
        (self.semantic/'review1.json').write_text(json.dumps(x))
        with self.assertRaisesRegex(ValueError,'Semantic evidence mismatch'):self.save()
        self.assertEqual(pointer,(self.storage/'current.json').read_bytes())
        self.assertEqual(len(list((self.storage/'snapshots').iterdir())),1)
    def test_detects_modified_snapshot(self):
        path=self.save();(path/'sessions.sqlite').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'modified'):self.save()

if __name__=='__main__':unittest.main()
