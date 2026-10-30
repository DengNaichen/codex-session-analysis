#!/usr/bin/env python3
"""Freeze a content-addressed local dataset and build an indexed SQLite database."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sessions(session_id TEXT PRIMARY KEY, session_kind TEXT, project TEXT, parent_session_id TEXT, payload_json TEXT NOT NULL);
CREATE TABLE source_files(source_file TEXT PRIMARY KEY, bytes INTEGER, sha256 TEXT NOT NULL);
CREATE TABLE events(event_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), turn_id TEXT, timestamp TEXT, local_timestamp TEXT, kind TEXT, role TEXT, phase TEXT, text TEXT, name TEXT, source_file TEXT NOT NULL REFERENCES source_files(source_file), source_line INTEGER NOT NULL, payload_json TEXT NOT NULL, UNIQUE(source_file,source_line));
CREATE INDEX events_session_time ON events(session_id,timestamp);
CREATE INDEX events_turn ON events(session_id,turn_id);
CREATE INDEX events_kind_role ON events(kind,role);
CREATE TABLE turns(session_id TEXT NOT NULL REFERENCES sessions(session_id), turn_id TEXT NOT NULL, started_at TEXT, completed_at TEXT, status TEXT, total_ms REAL, first_output_ms REAL, commentary_count INTEGER, final_count INTEGER, request_text TEXT, payload_json TEXT NOT NULL, PRIMARY KEY(session_id,turn_id));
CREATE INDEX turns_duration ON turns(total_ms);
CREATE TABLE replies(event_id INTEGER PRIMARY KEY REFERENCES events(event_id), session_id TEXT NOT NULL, turn_id TEXT NOT NULL, phase TEXT, elapsed_from_start_ms REAL, gap_since_previous_reply_ms REAL, generation_ms REAL, payload_json TEXT NOT NULL, FOREIGN KEY(session_id,turn_id) REFERENCES turns(session_id,turn_id));
CREATE TABLE semantic_sessions(session_id TEXT PRIMARY KEY REFERENCES sessions(session_id), title TEXT, category TEXT, origin TEXT, confidence TEXT, summary TEXT, fragmentation_reason TEXT, suggestion TEXT, payload_json TEXT NOT NULL);
CREATE TABLE semantic_flags(flag_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES semantic_sessions(session_id), event_id INTEGER NOT NULL REFERENCES events(event_id), turn_id TEXT, label TEXT, reason TEXT);
CREATE INDEX semantic_flags_label ON semantic_flags(label);
CREATE TABLE linear_issues(issue_id TEXT PRIMARY KEY, title TEXT, status TEXT, url TEXT, checked_at TEXT, payload_json TEXT NOT NULL);
CREATE TABLE session_issues(session_id TEXT REFERENCES semantic_sessions(session_id), issue_id TEXT, verified INTEGER NOT NULL, PRIMARY KEY(session_id,issue_id));
CREATE VIEW reply_analysis AS SELECT e.session_id,e.turn_id,e.timestamp,e.phase,e.text,s.project,r.elapsed_from_start_ms,r.gap_since_previous_reply_ms,r.generation_ms,t.total_ms AS turn_total_ms,e.source_file,e.source_line FROM replies r JOIN events e USING(event_id) JOIN sessions s ON s.session_id=e.session_id LEFT JOIN turns t ON t.session_id=e.session_id AND t.turn_id=e.turn_id;
'''

def dumps(x):
    return json.dumps(x,ensure_ascii=False,separators=(',',':'))

def read_rows(path):
    with path.open() as stream:
        return [json.loads(line) for line in stream]

def build_db(path, dataset, semantic, metadata):
    con=sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        with con:
            con.executemany('INSERT INTO metadata VALUES (?,?)',[(k,dumps(v)) for k,v in metadata.items()])
            for s in read_rows(dataset/'sessions.jsonl'):
                con.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',(s['session_id'],s.get('session_kind'),s.get('cwd'),s.get('parent_session_id'),dumps(s)))
            for s in read_rows(dataset/'manifest.jsonl'):
                con.execute('INSERT INTO source_files VALUES (?,?,?)',(s['source_file'],s['bytes'],s['sha256']))
            event_ids={}
            for i,e in enumerate(read_rows(dataset/'events.jsonl'),1):
                con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',(i,e['session_id'],e.get('turn_id'),e.get('timestamp'),e.get('local_timestamp'),e.get('kind'),e.get('role'),e.get('phase'),e.get('text'),e.get('name'),e['source_file'],e['source_line'],dumps(e)))
                event_ids[(e['source_file'],e['source_line'])]=(i,e['session_id'],e.get('turn_id'))
            timing=json.loads((dataset/'timing.json').read_text())
            for t in timing['turns']:
                if not t.get('turn_id'):
                    raise ValueError('Turn without ID cannot be persisted as a verified timing row')
                con.execute('INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?)',tuple(t.get(k) for k in ['session_id','turn_id','started_at','completed_at','status','total_ms','first_output_ms','commentary_count','final_count','request_text'])+(dumps(t),))
            for r in timing['replies']:
                eid,sid,tid=event_ids[(r['source_file'],r['source_line'])]
                if (sid,tid)!=(r['session_id'],r['turn_id']):raise ValueError('Reply evidence mismatch')
                con.execute('INSERT INTO replies VALUES (?,?,?,?,?,?,?,?)',(eid,sid,tid,r.get('phase'),r.get('elapsed_from_start_ms'),r.get('gap_since_previous_reply_ms'),r.get('generation_ms'),dumps(r)))
            issues=json.loads((semantic/'linear_context.json').read_text())
            verified={x['id'] for x in issues}
            for x in issues:
                con.execute('INSERT INTO linear_issues VALUES (?,?,?,?,?,?)',(x['id'],x.get('title'),x.get('status'),x.get('url'),x.get('checked_at'),dumps(x)))
            for file in ('review1.json','review2.json'):
                for s in json.loads((semantic/file).read_text())['sessions']:
                    con.execute('INSERT INTO semantic_sessions VALUES (?,?,?,?,?,?,?,?,?)',tuple(s.get(k) for k in ['session_id','title','category','origin','confidence','summary','fragmentation_reason','suggestion'])+(dumps(s),))
                    for f in s['flags']:
                        name,line=f['key'].rsplit(':',1);eid,sid,tid=event_ids[(name,int(line))]
                        if (sid,tid)!=(s['session_id'],f['turn_id']):raise ValueError('Semantic evidence mismatch')
                        con.execute('INSERT INTO semantic_flags(session_id,event_id,turn_id,label,reason) VALUES (?,?,?,?,?)',(sid,eid,tid,f['label'],f['reason']))
                    for issue in set(s.get('linear_tickets',[])):
                        con.execute('INSERT INTO session_issues VALUES (?,?,?)',(s['session_id'],issue,int(issue in verified)))
        assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
        return {t:con.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['sessions','events','turns','replies','semantic_sessions','semantic_flags','linear_issues']}
    finally:
        con.close()

def save(dataset, semantic, storage):
    inputs={}
    for name in ('sessions.jsonl','events.jsonl','primary_messages.jsonl','manifest.jsonl','report.json','timing.json'):
        inputs['cleaned/'+name]=(dataset/name).read_bytes()
    for name in ('review1.json','review2.json','batch1.json','batch2.json','linear_context.json'):
        inputs['semantic/'+name]=(semantic/name).read_bytes()
    for name in ('clean_sessions.py','analyze_timing.py','save_dataset.py','verify_semantics.py'):
        inputs['pipeline/'+name]=(Path(__file__).parent/name).read_bytes()
    hashes={k:hashlib.sha256(v).hexdigest() for k,v in sorted(inputs.items())}
    version=hashlib.sha256(dumps(hashes).encode()).hexdigest()[:24]
    storage.mkdir(parents=True,exist_ok=True,mode=0o700)
    snapshots=storage/'snapshots';snapshots.mkdir(exist_ok=True,mode=0o700)
    target=snapshots/version
    if target.exists():
        receipt=json.loads((target/'receipt.json').read_text())
        for name,h in receipt['files'].items():
            if hashlib.sha256((target/name).read_bytes()).hexdigest()!=h:
                raise ValueError('Existing snapshot was modified: '+name)
    else:
        staging=Path(tempfile.mkdtemp(prefix='.building-',dir=snapshots))
        try:
            for name,content in inputs.items():
                file=staging/name;file.parent.mkdir(parents=True,exist_ok=True,mode=0o700);file.write_bytes(content);file.chmod(0o600)
            metadata={'schema_version':1,'snapshot_id':version,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'timezone':'Asia/Shanghai','policy':'Versioned local snapshot; original Codex sessions are read-only. Semantic judgments are not measured time loss.'}
            counts=build_db(staging/'sessions.sqlite',staging/'cleaned',staging/'semantic',metadata)
            (staging/'sessions.sqlite').chmod(0o600)
            hashes['sessions.sqlite']=hashlib.sha256((staging/'sessions.sqlite').read_bytes()).hexdigest()
            receipt={**metadata,'counts':counts,'files':hashes}
            (staging/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n');(staging/'receipt.json').chmod(0o600)
            staging.rename(target)
        except BaseException:
            shutil.rmtree(staging)
            raise
    # Atomic pointer update; older snapshots remain intact.
    fd,temp=tempfile.mkstemp(prefix='.current-',dir=storage)
    with os.fdopen(fd,'w') as stream:
        json.dump({'snapshot_id':version,'database':str(target/'sessions.sqlite'),'receipt':str(target/'receipt.json')},stream,indent=2)
    os.replace(temp,storage/'current.json')
    print(json.dumps({'snapshot':str(target),'counts':receipt['counts']},ensure_ascii=False,indent=2))
    return target

if __name__=='__main__':
    root=Path(__file__).parent
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,default=root/'output/initial')
    p.add_argument('--semantic-dir',type=Path,default=root/'semantic')
    p.add_argument('--storage',type=Path,default=root/'data')
    a=p.parse_args();save(a.dataset,a.semantic_dir,a.storage)
