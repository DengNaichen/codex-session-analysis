#!/usr/bin/env python3
"""Validate contextual Luna labels, retain run versions, and export review data."""
import collections
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
from rhythm_text import semantic_text

PARTICIPATION={'discussion','key_decision','acceptance','delegation','extra_intervention','automation','unknown'}
HANDOFF={'not_applicable','actionable','partial','missing','unknown'}
ORIGIN={'human','automation','test','mixed','uncertain'}

def validate(batch,review):
    assert review['model']=='gpt-5.6-luna' and review['rubric_version']=='v2'
    actual=review['sessions'];source={s['session_id']:s for s in batch}
    assert len(actual)==len(source) and {s['session_id'] for s in actual}==set(source),'Session coverage mismatch'
    for s in actual:
        assert s['origin'] in ORIGIN
        for key in ('title','summary','improvement'):assert isinstance(s[key],str)
        expected={e['event_id']:e for e in source[s['session_id']]['interventions']}
        rows=s['interventions']
        assert len(rows)==len(expected) and {e['event_id'] for e in rows}==set(expected),(s['session_id'],'Event coverage mismatch')
        for r in rows:
            assert r['participation'] in PARTICIPATION,(r['event_id'],'participation')
            assert r['preceding_handoff'] in HANDOFF,(r['event_id'],'handoff')
            assert r['confidence'] in {'high','medium','low'}
            assert isinstance(r['reason'],str) and r['reason'].strip()
            assert isinstance(r['subtype'],str) and r['subtype'].strip()
            assert isinstance(r['handoff_reason'],str)
            text=expected[r['event_id']]['user']['text']+'\n'+expected[r['event_id']]['user'].get('semantic_text','')
            assert (r['evidence'] in text and (r['evidence'] or not text)),(r['event_id'],'Evidence not exact')
            assert len(r['evidence'])<=80,(r['event_id'],'Evidence too long')
            if r['preceding_handoff']!='not_applicable':
                assert r['handoff_reason'].strip(),(r['event_id'],'Missing handoff reason')
    return actual

def save(root):
    folder=root/'experiments/rhythm-v2'
    manifest=json.loads((folder/'manifest.json').read_text())
    sessions=[];source={};files=['manifest.json','rubric.md']
    for i in (1,2,3):
        batch=json.loads((folder/f'batch{i}.json').read_text());review=json.loads((folder/f'review{i}.json').read_text())
        sessions.extend(validate(batch,review));source.update({s['session_id']:s for s in batch})
        files.extend([f'batch{i}.json',f'review{i}.json'])
    assert len(sessions)==manifest['sessions'] and len(source)==len(sessions)
    assert sum(len(s['interventions']) for s in sessions)==manifest['interventions']
    with sqlite3.connect('file:'+manifest['source_database']+'?mode=ro',uri=True) as src:
        for s in source.values():
            for e in s['interventions']:
                original=src.execute('SELECT session_id,role,text,turn_id FROM events WHERE event_id=?',(e['event_id'],)).fetchone()
                assert original and original[0]==s['session_id'] and original[1]=='user'
                assert (original[2] or '')[:3000]==e['user']['text'] and original[3]==e['turn_id']
                assert semantic_text(original[2])[:3000]==e['user']['semantic_text']
    fingerprints={name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in files}
    for name in ('prepare_rhythm.py','rhythm_text.py','save_rhythm.py'):
        fingerprints['pipeline/'+name]=hashlib.sha256((root/name).read_bytes()).hexdigest()
    run_id=hashlib.sha256(json.dumps(fingerprints,sort_keys=True).encode()).hexdigest()[:24]
    output=[]
    for s in sessions:
        input_by_id={e['event_id']:e for e in source[s['session_id']]['interventions']}
        output.append({**s,'project':source[s['session_id']]['project'],'interventions':[{'label':r,'input':input_by_id[r['event_id']]} for r in s['interventions']]})
    db=root/'data/classification_runs.sqlite';con=sqlite3.connect(db);con.execute('PRAGMA foreign_keys=ON')
    con.executescript('''CREATE TABLE IF NOT EXISTS rhythm_runs(run_id TEXT PRIMARY KEY,model TEXT NOT NULL,rubric_version TEXT NOT NULL,source_snapshot_id TEXT NOT NULL,created_at TEXT NOT NULL,inputs_json TEXT NOT NULL,rubric_text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rhythm_sessions(run_id TEXT REFERENCES rhythm_runs(run_id),session_id TEXT,title TEXT,origin TEXT,summary TEXT,improvement TEXT,payload_json TEXT,PRIMARY KEY(run_id,session_id));
CREATE TABLE IF NOT EXISTS interventions(run_id TEXT,event_id INTEGER,session_id TEXT,turn_id TEXT,timestamp TEXT,participation TEXT,subtype TEXT,confidence TEXT,evidence TEXT,reason TEXT,preceding_handoff TEXT,handoff_reason TEXT,input_json TEXT,output_json TEXT,PRIMARY KEY(run_id,event_id),FOREIGN KEY(run_id,session_id) REFERENCES rhythm_sessions(run_id,session_id));
CREATE INDEX IF NOT EXISTS interventions_participation ON interventions(run_id,participation);
''')
    with con:
        con.execute('INSERT OR IGNORE INTO rhythm_runs VALUES (?,?,?,?,?,?,?)',(run_id,'gpt-5.6-luna','v2',manifest['snapshot_id'],dt.datetime.now(dt.timezone.utc).isoformat(),json.dumps(fingerprints),(folder/'rubric.md').read_text()))
        for s in output:
            con.execute('INSERT OR IGNORE INTO rhythm_sessions VALUES (?,?,?,?,?,?,?)',(run_id,s['session_id'],s['title'],s['origin'],s['summary'],s['improvement'],json.dumps(s,ensure_ascii=False)))
            for item in s['interventions']:
                r=item['label'];e=item['input']
                con.execute('INSERT OR IGNORE INTO interventions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(run_id,r['event_id'],s['session_id'],e['turn_id'],e['timestamp'],r['participation'],r['subtype'],r['confidence'],r['evidence'],r['reason'],r['preceding_handoff'],r['handoff_reason'],json.dumps(e,ensure_ascii=False),json.dumps(r,ensure_ascii=False)))
    assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert not con.execute('PRAGMA foreign_key_check').fetchall()
    count=con.execute('SELECT count(*) FROM interventions WHERE run_id=?',(run_id,)).fetchone()[0]
    assert count==manifest['interventions'];con.close();db.chmod(0o600)
    labels=[r for s in sessions for r in s['interventions']]
    summary={'run_id':run_id,'model':'gpt-5.6-luna','rubric_version':'v2','source_snapshot_id':manifest['snapshot_id'],'sessions':len(sessions),'interventions':count,'participation':dict(collections.Counter(r['participation'] for r in labels)),'handoff':dict(collections.Counter(r['preceding_handoff'] for r in labels)),'confidence':dict(collections.Counter(r['confidence'] for r in labels)),'limitations':['标签是基于有限上下文的模型判断，尚未由用户校准。','统计的是用户角色消息，不证明真实打断或通知。','验收反馈和新增要求不自动算返工；首次委托单列。','验收路径只依据用户消息前提供的两条助手消息，缺失可能来自上下文窗口。']}
    (folder/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    payload={'summary':summary,'sessions':output}
    temp=root/'web/.rhythm.json.tmp';temp.write_text(json.dumps(payload,ensure_ascii=False));temp.replace(root/'web/rhythm.json')
    print(json.dumps(summary,ensure_ascii=False,indent=2));return summary

if __name__=='__main__':save(Path(__file__).parent)
