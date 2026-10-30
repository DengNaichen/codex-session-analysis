"""Reproducible stratified 100-reply pilot from a frozen, read-only snapshot."""
from pathlib import Path
import sqlite3,json,random,collections
root=Path(__file__).parent
pointer=json.loads((root/'data/current.json').read_text())
con=sqlite3.connect('file:'+pointer['database']+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
rows=[dict(r) for r in con.execute("""SELECT e.*,r.elapsed_from_start_ms,t.commentary_count,t.total_ms FROM replies r JOIN events e USING(event_id) JOIN sessions s ON s.session_id=e.session_id LEFT JOIN turns t ON t.session_id=e.session_id AND t.turn_id=e.turn_id LEFT JOIN semantic_sessions sem ON sem.session_id=e.session_id WHERE s.session_kind='primary' AND coalesce(sem.origin,'unknown') NOT IN ('automation','test') ORDER BY e.session_id,e.timestamp,e.event_id""")]
pools=collections.defaultdict(list)
for r in rows:
 text=r['text'] or ''
 bucket='question' if any(x in text for x in ['?','？','确认','是否','需要你','请你']) else 'final' if r['phase']=='final_answer' else 'long_execution' if (r['commentary_count'] or 0)>=10 else 'other_progress'
 r['sample_bucket']=bucket;pools[bucket].append(r)
rng=random.Random(20260919);selected=[];used=set();sessions=collections.Counter()
for bucket in ['question','final','long_execution','other_progress']:
 rng.shuffle(pools[bucket]);n=0
 for r in pools[bucket]:
  if sessions[r['session_id']]>=5:continue
  selected.append(r);used.add(r['event_id']);sessions[r['session_id']]+=1;n+=1
  if n==25:break
for r in rows:
 if len(selected)>=100:break
 if r['event_id'] not in used and sessions[r['session_id']]<5:
  selected.append(r);used.add(r['event_id']);sessions[r['session_id']]+=1
assert len(selected)==100
out=[]
for r in selected:
 prior=[dict(x) for x in con.execute("SELECT event_id,role,text,timestamp FROM events WHERE session_id=? AND kind='message' AND (timestamp<? OR (timestamp=? AND event_id<?)) ORDER BY timestamp DESC,event_id DESC LIMIT 8",(r['session_id'],r['timestamp'],r['timestamp'],r['event_id']))][::-1]
 first=con.execute("SELECT event_id,text FROM events WHERE session_id=? AND role='user' AND kind='message' AND (timestamp<? OR (timestamp=? AND event_id<?)) ORDER BY timestamp,event_id LIMIT 1",(r['session_id'],r['timestamp'],r['timestamp'],r['event_id'])).fetchone()
 def excerpt(text,n):return {'text':(text or '')[:n],'truncated':len(text or '')>n}
 out.append({'event_id':r['event_id'],'session_id':r['session_id'],'turn_id':r['turn_id'],'source_file':r['source_file'],'source_line':r['source_line'],'sample_bucket':r['sample_bucket'],'task_opening':excerpt(first['text'] if first else '',1600),'prior_messages':[{'event_id':p['event_id'],'role':p['role'],**excerpt(p['text'],1400)} for p in prior],'target':{'phase':r['phase'],**excerpt(r['text'],6000)}})
folder=root/'experiments/luna-v1'
for i in [0,1]:(folder/f'batch{i+1}.json').write_text(json.dumps(out[i::2],ensure_ascii=False,indent=2))
(folder/'sample.json').write_text(json.dumps({'snapshot_id':pointer['snapshot_id'],'source_database':pointer['database'],'seed':20260919,'selection':'25 per heuristic sampling bucket, max 5 per session; buckets are NOT semantic labels','samples':out},ensure_ascii=False,indent=2))
print(json.dumps({'samples':len(out),'sessions':len(sessions),'buckets':dict(collections.Counter(x['sample_bucket'] for x in out))}))
con.close()
