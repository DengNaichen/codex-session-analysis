#!/usr/bin/env python3
"""Bundle cleaned data for the local dashboard; no network access."""
import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('dataset', type=Path)
p.add_argument('--output', type=Path, default=Path(__file__).parent/'web/data.json')
a=p.parse_args()
def rows(name):
    with (a.dataset/(name+'.jsonl')).open() as f:
        return [json.loads(line) for line in f]
data={'sessions':rows('sessions'),'events':rows('events'),'report':json.loads((a.dataset/'report.json').read_text())}
data['timing']=json.loads((a.dataset/'timing.json').read_text()) if (a.dataset/'timing.json').exists() else {}
semantic_dir=Path(__file__).parent/'semantic'
data['semantic']={'sessions':[]}
for name in ('review1.json','review2.json'):
    path=semantic_dir/name
    if path.exists(): data['semantic']['sessions'].extend(json.loads(path.read_text())['sessions'])
data['linear']=json.loads((semantic_dir/'linear_context.json').read_text()) if (semantic_dir/'linear_context.json').exists() else []
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')))
print(f'Prepared {len(data["sessions"])} sessions, {len(data["events"])} events')
