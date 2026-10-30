"""Validate semantic evidence links without asserting that interpretations are facts."""
import json
from pathlib import Path
from collections import Counter
root=Path(__file__).parent
sessions=[]
for n in (1,2):
    batch=json.loads((root/f'semantic/batch{n}.json').read_text())
    reviews=json.loads((root/f'semantic/review{n}.json').read_text())['sessions']
    lookup={s['session_id']:{m['key']:m['turn_id'] for m in s['messages']} for s in batch}
    assert len(reviews)==len(lookup)
    assert {s['session_id'] for s in reviews}==set(lookup)
    for s in reviews:
        assert s['origin'] in ('human','automation','test','mixed','uncertain')
        assert s['confidence'] in ('high','medium','low')
        for flag in s['flags']:
            assert lookup[s['session_id']][flag['key']]==flag['turn_id']
    sessions.extend(reviews)
assert len({s['session_id'] for s in sessions})==len(sessions)
print(json.dumps({'sessions':len(sessions),'flags':sum(len(s['flags']) for s in sessions),'origins':dict(Counter(s['origin'] for s in sessions))},ensure_ascii=False))
