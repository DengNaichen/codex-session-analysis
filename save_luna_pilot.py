"""Validate and persist the Luna pilot without changing frozen source snapshots."""
from pathlib import Path
import collections,datetime,hashlib,json,sqlite3
root=Path(__file__).parent
folder=root/'experiments/luna-v1'
sample=json.loads((folder/'sample.json').read_text())
source={s['event_id']:s for s in sample['samples']}
source_db=sqlite3.connect('file:'+sample['source_database']+'?mode=ro',uri=True)
for item in source.values():
    original=source_db.execute('SELECT session_id,turn_id,text,source_file,source_line FROM events WHERE event_id=?',(item['event_id'],)).fetchone()
    assert original and original[0]==item['session_id'] and original[1]==item['turn_id']
    assert (original[2] or '')[:6000]==item['target']['text']
    assert (original[3],original[4])==(item['source_file'],item['source_line'])
source_db.close()
labels=[]
for n in (1,2):
    obj=json.loads((folder/f'labels{n}.json').read_text())
    assert obj['model']=='gpt-5.6-luna' and obj['rubric_version']=='v1'
    rows=obj['labels'];expected={s['event_id'] for s in json.loads((folder/f'batch{n}.json').read_text())}
    assert len(rows)==len(expected) and {r['event_id'] for r in rows}==expected
    labels.extend(rows)
allowed={'stage':{'discussion','execution','acceptance','unknown'},'response_requirement':{'none','later','blocking','unknown'},'necessity':{'necessary','redundant','not_applicable','unknown'},'can_batch':{'yes','no','unknown'},'confidence':{'high','medium','low'}}
functions={'delivery','decision','discussion','coordination','progress','correction'}
for row in labels:
    s=source[row['event_id']]
    for key,values in allowed.items():assert row[key] in values,(row['event_id'],key)
    assert row['functions'] and set(row['functions'])<=functions
    assert row['response_requirement']!='none' or row['necessity']=='not_applicable',('necessity_scope',row['event_id'])
    assert isinstance(row['reason'],str) and row['reason'].strip()
    assert row['evidence'] and row['evidence'] in s['target']['text'],('target_quote',row['event_id'])
    prior='\n'.join([s['task_opening']['text']]+[p['text'] for p in s['prior_messages']])
    assert not row['prior_evidence'] or row['prior_evidence'] in prior,('prior_quote',row['event_id'])
    assert row['necessity']!='redundant' or row['prior_evidence'],('redundant_without_evidence',row['event_id'])
assert len(labels)==100 and len({r['event_id'] for r in labels})==100
inputs={name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in ['sample.json','rubric.md','labels1.json','labels2.json']}
run_id=hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()[:24]
db=root/'data/classification_runs.sqlite'
con=sqlite3.connect(db)
con.execute('PRAGMA foreign_keys=ON')
con.executescript('''CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY,model TEXT NOT NULL,rubric_version TEXT NOT NULL,source_snapshot_id TEXT NOT NULL,created_at TEXT NOT NULL,inputs_json TEXT NOT NULL,rubric_text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS classifications(run_id TEXT REFERENCES runs(run_id),event_id INTEGER,session_id TEXT,turn_id TEXT,stage TEXT,functions_json TEXT,response_requirement TEXT,necessity TEXT,can_batch TEXT,confidence TEXT,evidence TEXT,prior_evidence TEXT,reason TEXT,input_json TEXT,output_json TEXT,PRIMARY KEY(run_id,event_id));
CREATE INDEX IF NOT EXISTS labels_necessity ON classifications(necessity,response_requirement);
''')
with con:
    con.execute('INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,?)',(run_id,'gpt-5.6-luna','v1',sample['snapshot_id'],datetime.datetime.now(datetime.timezone.utc).isoformat(),json.dumps(inputs),(folder/'rubric.md').read_text()))
    for r in labels:
        s=source[r['event_id']]
        con.execute('INSERT OR IGNORE INTO classifications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(run_id,r['event_id'],s['session_id'],s['turn_id'],r['stage'],json.dumps(r['functions']),r['response_requirement'],r['necessity'],r['can_batch'],r['confidence'],r['evidence'],r['prior_evidence'],r['reason'],json.dumps(s,ensure_ascii=False),json.dumps(r,ensure_ascii=False)))
assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
assert not con.execute('PRAGMA foreign_key_check').fetchall()
assert con.execute('SELECT count(*) FROM classifications WHERE run_id=?',(run_id,)).fetchone()[0]==100
con.close();db.chmod(0o600)
summary={'run_id':run_id,'model':'gpt-5.6-luna','source_snapshot_id':sample['snapshot_id'],'samples':100,'sessions':len({s['session_id'] for s in source.values()}),'distributions':{key:dict(collections.Counter(r[key] for r in labels)) for key in allowed},'limitations':['分层样本比例不代表总体比例','尚未与用户校准，不是金标准','使用截断上下文，无法推断实际打断或工时损失']}
(folder/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))

(root/'web/pilot.json').write_text(json.dumps({'summary':summary,'items':[{'sample':source[r['event_id']],'label':r} for r in labels]},ensure_ascii=False))
