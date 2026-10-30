#!/usr/bin/env python3
"""Local, standard-library-only Codex session normalizer. Never edits input."""
import argparse
import collections
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

TAGS = ('environment_context', 'recommended_plugins', 'INSTRUCTIONS',
        'permissions instructions', 'app-context', 'skills_instructions')

def clean_text(text):
    for tag in TAGS:
        text = re.sub(r'<' + re.escape(tag) + r'>.*?</' + re.escape(tag) + r'>', '', text, flags=re.S)
    text = re.sub(r'^# AGENTS\.md instructions for [^\n]*', '', text, flags=re.M)
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[PRIVATE_KEY]', text, flags=re.S)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16})\b', '[SECRET]', text)
    text = re.sub(r'(?i)(\b(?:api[_-]?key|password|passwd|secret|access[_-]?token|authorization)\b[\s"\x27]*[:=]\s*)(?:"[^"\n]*"|\x27[^\x27\n]*\x27|[^\s,;]+)', r'\1[SECRET]', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [SECRET]', text)
    text = re.sub(r'https?://[^\s<>"\x27]+', lambda m: m[0].split('?')[0].split('#')[0] if '@' not in m[0] else '[PRIVATE_URL]', text)
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[EMAIL]', text)
    text = re.sub(r'/(?:Users|home)/[^/\s<>"\x27]+', '/HOME', text)
    return text.strip()

def content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n'.join(x.get('text', '') for x in content if isinstance(x, dict) and isinstance(x.get('text'), str))

def normalize(records, source, timezone):
    meta = next((r['payload'] for _, r in records if r.get('type') == 'session_meta'), {})
    sid = meta.get('id') or meta.get('session_id') or source
    origin = meta.get('source')
    sub = isinstance(origin, dict) and 'subagent' in origin
    parent = meta.get('parent_thread_id')
    if sub:
        value = origin.get('subagent', {})
        if isinstance(value, dict):
            parent = parent or value.get('thread_spawn', {}).get('parent_thread_id')
    kind = 'subagent' if sub else ('child' if parent else 'primary')
    candidates = []; turn = None; models = set(); unknown = collections.Counter()
    for line, r in records:
        p = r.get('payload', {})
        if not isinstance(p, dict):
            continue
        typ = r.get('type'); pt = p.get('type')
        if typ == 'turn_context' or (typ == 'event_msg' and pt == 'task_started'):
            turn = p.get('turn_id', turn)
        if typ == 'turn_context' and p.get('model'):
            models.add(p['model'])
        base = {'session_id': sid, 'session_kind': kind, 'turn_id': p.get('turn_id') or turn,
                'timestamp': r.get('timestamp'), 'source_file': source, 'source_line': line}
        ts = base['timestamp']
        try:
            parsed = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
            base['local_timestamp'] = parsed.astimezone(ZoneInfo(timezone)).isoformat() if parsed.tzinfo else None
        except (ValueError, TypeError, AttributeError):
            base['local_timestamp'] = None
        event = None; rank = 0
        if typ == 'event_msg' and pt == 'item_completed':
            i = p.get('item', {}); it = i.get('type'); rank = 3
            if it in ('UserMessage', 'AgentMessage'):
                event = {'kind': 'message', 'role': 'user' if it == 'UserMessage' else 'assistant',
                         'text': clean_text(content_text(i.get('content'))), 'phase': i.get('phase')}
            elif it in ('CommandExecution', 'McpToolCall', 'FileChange', 'CollabAgentToolCall', 'SubAgentActivity', 'Extension', 'ImageView'):
                event = {'kind': 'operation', 'operation_type': it, 'name': i.get('tool') or i.get('kind') or it,
                         'status': i.get('status'), 'exit_code': i.get('exit_code'), 'duration': i.get('duration')}
                if it == 'FileChange':
                    event['file_count'] = len(i.get('changes', {}))
            elif it not in ('Reasoning', 'ContextCompaction', 'FunctionCallOutput'):
                unknown[str(it)] += 1
            if event:
                event['record_id'] = i.get('id')
        elif typ == 'response_item':
            rank = 2
            if pt == 'message' and p.get('role') in ('user', 'assistant'):
                event = {'kind': 'message', 'role': p['role'], 'text': clean_text(content_text(p.get('content'))), 'phase': p.get('phase')}
            elif pt in ('function_call', 'custom_tool_call'):
                event = {'kind': 'operation', 'operation_type': 'raw_call', 'name': p.get('name'), 'status': p.get('status')}
            if event:
                event['record_id'] = p.get('call_id') or p.get('id')
        elif typ == 'event_msg' and pt in ('user_message', 'agent_message'):
            rank = 1
            event = {'kind': 'message', 'role': 'user' if pt == 'user_message' else 'assistant',
                     'text': clean_text(p.get('message', '')), 'phase': p.get('phase')}
        elif typ == 'event_msg' and pt in ('task_started', 'task_complete', 'turn_aborted'):
            event = {'kind': 'lifecycle', 'event': pt, 'duration_ms': p.get('duration_ms')}
        elif typ == 'realtime_item' and pt == 'transcript_segment':
            event = {'kind': 'voice_transcript', 'role': p.get('role'), 'text': clean_text(p.get('text', ''))}
        if event:
            candidates.append((rank, {**base, **event}))
    # Select one representation per turn and role. Repeated human messages within
    # the chosen representation remain distinct unless they share an explicit ID.
    best = {}
    for rank, e in candidates:
        if e['kind'] in ('message', 'operation'):
            key = (e['turn_id'], e['kind'], e.get('role'))
            best[key] = max(best.get(key, 0), rank)
    events = []; seen = set(); dropped = collections.Counter()
    for rank, e in candidates:
        key = (e['turn_id'], e['kind'], e.get('role'))
        if key in best and rank < best[key]:
            dropped['alternate_representation'] += 1; continue
        if e['kind'] == 'message' and not e['text']:
            dropped['empty_or_context_only'] += 1; continue
        rid = e.get('record_id')
        identity = (e['kind'], e.get('role'), rid)
        if rid and identity in seen:
            dropped['repeated_id'] += 1; continue
        if rid:
            seen.add(identity)
        events.append(e)
    summary = {'session_id': sid, 'session_kind': kind, 'parent_session_id': parent,
               'root_session_id': meta.get('session_id'), 'source_file': source, 'cwd': clean_text(meta.get('cwd') or ''), 'models': sorted(models),
               'event_count': len(events), 'user_message_count': sum(e['kind'] == 'message' and e['role'] == 'user' for e in events),
               'dropped': dict(dropped), 'unknown_item_types': dict(unknown)}
    return summary, events

def run(input_root, output, timezone):
    ZoneInfo(timezone)
    if output.resolve() == input_root.resolve() or input_root.resolve() in output.resolve().parents:
        raise ValueError('Output must be outside input directory')
    output.mkdir(parents=True, exist_ok=False)
    stats = collections.Counter(); issues = []; sessions = []; manifest = []; session_map = {}; seen_events = set()
    with (output/'events.jsonl').open('w') as all_events, (output/'primary_messages.jsonl').open('w') as human:
        for folder in ('sessions', 'archived_sessions'):
            for path in sorted((input_root/folder).rglob('*.jsonl')):
                stats['input_files'] += 1
                source = str(path.relative_to(input_root)); records = []
                # Capture a fixed byte snapshot even if a live session grows.
                with path.open('rb') as f:
                    data = f.read(path.stat().st_size)
                manifest.append({'source_file': source, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
                for line, raw in enumerate(data.splitlines(), 1):
                    try:
                        record = json.loads(raw)
                        if not isinstance(record, dict):
                            raise ValueError('not an object')
                        records.append((line, record)); stats['input_records'] += 1
                    except (ValueError, UnicodeError):
                        issues.append({'source_file': source, 'line': line, 'reason': 'invalid_json_record'})
                summary, events = normalize(records, source, timezone)
                sid = summary['session_id']
                if sid not in session_map:
                    summary['source_files'] = [source]
                    summary['event_count'] = 0
                    summary['user_message_count'] = 0
                    session_map[sid] = summary
                    sessions.append(summary)
                    stats[summary['session_kind'] + '_sessions'] += 1
                else:
                    stats['additional_files_for_existing_session'] += 1
                    session_map[sid]['source_files'].append(source)
                for e in events:
                    identity = (sid, e['kind'], e.get('role'), e.get('record_id')) if e.get('record_id') else (sid, e['kind'], e.get('turn_id'), e.get('timestamp'), e.get('role'), e.get('text'), e.get('event'), e.get('name'))
                    if identity in seen_events:
                        stats['duplicate_events'] += 1
                        continue
                    seen_events.add(identity)
                    session_map[sid]['event_count'] += 1
                    session_map[sid]['user_message_count'] += int(e['kind'] == 'message' and e.get('role') == 'user')
                    all_events.write(json.dumps(e, ensure_ascii=False)+'\n'); stats['output_events'] += 1
                    if e['session_kind'] == 'primary' and e['kind'] == 'message':
                        human.write(json.dumps(e, ensure_ascii=False)+'\n'); stats['primary_messages'] += 1
        if not stats['input_files']:
            raise ValueError('No session files found')
    for name, rows in [('sessions', sessions), ('manifest', manifest)]:
        with (output/(name+'.jsonl')).open('w') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False)+'\n')
    report = {'schema_version': 1, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'timezone': timezone, 'counts': dict(stats), 'issues': issues,
              'limitations': ['Heuristic redaction is not a guarantee of anonymization.',
                'Per-turn representation selection may omit events in partially mirrored histories.',
                'Primary messages may include automation prompts or copied/forked history; user role does not prove human authorship.',
                'Session and turn elapsed time is not human working time.',
                'Operations use completed UI items when present, otherwise raw calls; granularity differs.',
                'Voice transcripts remain separate and are not deduplicated against text messages.']}
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report['counts'], ensure_ascii=False, indent=2))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-root', type=Path, default=Path.home()/'.codex')
    parser.add_argument('--output', type=Path, required=True, help='New output directory; existing directories are rejected')
    parser.add_argument('--timezone', default='Asia/Shanghai')
    args = parser.parse_args()
    run(args.input_root.expanduser(), args.output.expanduser(), args.timezone)
