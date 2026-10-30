from pathlib import Path
import json,sqlite3,collections
from rhythm_text import semantic_text
root=Path(__file__).parent;out=root/'experiments/rhythm-v2'
pointer=json.loads((root/'data/current.json').read_text());db=sqlite3.connect('file:'+pointer['database']+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
sessions=[dict(s) for s in db.execute("SELECT session_id,project FROM sessions WHERE session_kind='primary' ORDER BY session_id")]
def clip(s,n):return {'text':(s or '')[:n],'truncated':len(s or '')>n}
items=[]
for s in sessions:
 events=[dict(e) for e in db.execute("SELECT event_id,turn_id,role,phase,text,timestamp,source_file,source_line FROM events WHERE session_id=? AND kind='message' ORDER BY timestamp,event_id",(s['session_id'],))]
 msgs=[]
 for i,e in enumerate(events):
  if e['role']!='user':continue
  prev=[x for x in events[:i] if x['role']=='assistant'][-2:]
  next_assistant=next((x for x in events[i+1:] if x['role']=='assistant'),None)
  msgs.append({**{k:e[k] for k in ('event_id','turn_id','timestamp','source_file','source_line')},'user':{**clip(e['text'],3000),'semantic_text':semantic_text(e['text'])[:3000],'semantic_truncated':len(semantic_text(e['text']))>3000},'preceding_assistant':[{'event_id':x['event_id'],'phase':x['phase'],**clip(x['text'],2200)} for x in prev],'next_assistant':({'event_id':next_assistant['event_id'],**clip(next_assistant['text'],500)} if next_assistant else None)})
 items.append({**s,'interventions':msgs})
# Greedy balance by user message count while keeping each session intact.
batches=[[],[],[]];sizes=[0,0,0]
for s in sorted(items,key=lambda x:-len(x['interventions'])):
 k=sizes.index(min(sizes));batches[k].append(s);sizes[k]+=len(s['interventions'])
for i,b in enumerate(batches,1):(out/f'batch{i}.json').write_text(json.dumps(b,ensure_ascii=False,indent=1))
(out/'manifest.json').write_text(json.dumps({'snapshot_id':pointer['snapshot_id'],'source_database':pointer['database'],'sessions':len(items),'interventions':sum(sizes),'batch_counts':sizes,'scope':'All primary sessions in fixed snapshot; user-role messages may be automated. No claim of actual notifications or attention switches.'},ensure_ascii=False,indent=2))
print({'sessions':len(items),'interventions':sum(sizes),'batches':sizes});db.close()
