#!/usr/bin/env python3
"""Join cleaned messages to raw lifecycle records from the exact cleaning snapshot."""
import argparse
import collections
import datetime as dt
import hashlib
import json
from pathlib import Path


def epoch(value):
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def iso(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat() if value is not None else None


def nonnegative(value, quality, metric):
    if value is None:
        return None
    if value < 0:
        quality['negative_' + metric] += 1
        return None
    return round(value, 3)


def json_rows(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def read_lifecycle(input_root, cleaned, quality):
    """Read only snapshot bytes; never use later appended records from live logs."""
    lifecycle = collections.defaultdict(list)
    source_sessions = {}
    for row in json_rows(cleaned / 'sessions.jsonl'):
        for source in row.get('source_files', [row['source_file']]):
            source_sessions[source] = row['session_id']
    for entry in json_rows(cleaned / 'manifest.jsonl'):
        path = input_root / entry['source_file']
        try:
            with path.open('rb') as stream:
                data = stream.read(entry['bytes'])
        except FileNotFoundError:
            quality['missing_source_files'] += 1
            continue
        if hashlib.sha256(data).hexdigest() != entry['sha256']:
            quality['snapshot_hash_mismatches'] += 1
            continue
        quality['verified_source_files'] += 1
        sid = source_sessions.get(entry['source_file'])
        if sid is None:
            quality['unmapped_source_files'] += 1
            continue
        for raw in data.splitlines():
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(record, dict):
                continue
            p = record.get('payload')
            if not isinstance(p, dict) or record.get('type') != 'event_msg':
                continue
            if p.get('type') in ('task_started', 'task_complete', 'turn_aborted') and p.get('turn_id'):
                lifecycle[(sid, p['turn_id'])].append(p)
                quality['raw_lifecycle_records'] += 1
    return lifecycle


def analyze(events, lifecycle, quality=None):
    quality = quality if quality is not None else collections.Counter()
    messages = collections.defaultdict(list)
    for event in events:
        if event.get('kind') != 'message':
            continue
        if not event.get('turn_id'):
            quality['messages_without_turn_id'] += 1
        messages[(event['session_id'], event.get('turn_id'))].append(event)
    turns = []; replies = []
    for sid, tid in sorted(set(messages) | set(lifecycle), key=lambda k: (k[0], k[1] or '')):
        records = lifecycle.get((sid, tid), [])
        # Identical lifecycle copies can occur in session continuations.
        records = list({json.dumps(p, sort_keys=True): p for p in records}.values())
        ends = [p for p in records if p['type'] in ('task_complete', 'turn_aborted')]
        end = max(ends, key=lambda p: epoch(p.get('completed_at')) or float('-inf'), default={})
        starts = [epoch(p.get('started_at')) for p in records]
        starts = [x for x in starts if x is not None]
        start = epoch(end.get('started_at'))
        if start is None:
            start = min(starts) if starts else None
        finish = epoch(end.get('completed_at'))
        if finish is not None and start is not None and finish < start:
            quality['negative_lifecycle_span'] += 1
            finish = None
        duration = end.get('duration_ms')
        # Keep the runtime's duration: whole-second endpoint clocks may disagree.
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            duration = (finish - start) * 1000 if start is not None and finish is not None else None
        total = nonnegative(duration, quality, 'total_ms')
        first = end.get('time_to_first_token_ms')
        if not isinstance(first, (int, float)) or isinstance(first, bool):
            first = None
        first = nonnegative(first, quality, 'first_output_ms')
        if first is not None and total is not None and first > total:
            quality['first_output_exceeds_total'] += 1
            first = None
        group = messages.get((sid, tid), [])
        group.sort(key=lambda e: (epoch(e.get('timestamp')) if epoch(e.get('timestamp')) is not None else float('inf'), e.get('source_file', ''), e.get('source_line', 0)))
        users = [e for e in group if e.get('role') == 'user']
        agents = [e for e in group if e.get('role') == 'assistant']
        status = {'task_complete': 'completed', 'turn_aborted': 'aborted'}.get(end.get('type'), 'incomplete' if records else 'unknown')
        turns.append(dict(session_id=sid, turn_id=tid, started_at=iso(start), completed_at=iso(finish), status=status, total_ms=total, first_output_ms=first, user_message_count=len(users), assistant_message_count=len(agents), commentary_count=sum(e.get('phase') == 'commentary' for e in agents), final_count=sum(e.get('phase') in ('final_answer', 'final') for e in agents), request_text='\n\n'.join(e.get('text', '') for e in users)[:1000]))
        if agents:
            quality['turns_with_replies'] += 1
        if agents and records:
            quality['reply_turns_with_lifecycle'] += 1
        previous = None
        for e in agents:
            stamp = epoch(e.get('timestamp'))
            elapsed = nonnegative((stamp - start) * 1000 if stamp is not None and start is not None else None, quality, 'elapsed_from_start_ms')
            # Whole-second lifecycle endpoints have up to 1 second quantization.
            if elapsed is not None and finish is not None and stamp > finish + 1:
                quality['reply_timestamp_after_completion'] += 1
                elapsed = None
            gap = nonnegative((stamp - previous) * 1000 if stamp is not None and previous is not None else None, quality, 'gap_since_previous_reply_ms')
            previous = stamp
            replies.append({**{k: e.get(k) for k in ('session_id', 'turn_id', 'record_id', 'source_file', 'source_line', 'timestamp', 'phase', 'text')}, 'elapsed_from_start_ms': elapsed, 'gap_since_previous_reply_ms': gap, 'generation_ms': None})
            quality['replies_with_lifecycle'] += int(bool(records))
            quality['replies_with_valid_elapsed'] += int(elapsed is not None)
    quality['turns'] = len(turns); quality['replies'] = len(replies)
    quality['reply_lifecycle_association_rate'] = quality['replies_with_lifecycle'] / len(replies) if replies else None
    quality['reply_elapsed_coverage_rate'] = quality['replies_with_valid_elapsed'] / len(replies) if replies else None
    quality['turns_with_total_ms'] = sum(t['total_ms'] is not None for t in turns)
    quality['turns_with_first_output_ms'] = sum(t['first_output_ms'] is not None for t in turns)
    return {'turns': turns, 'replies': replies, 'quality': dict(quality), 'definitions': {
        'identity': 'Turns join on session_id + turn_id, across all snapshot source files.',
        'total_ms': 'Runtime lifecycle duration_ms; if absent, completed_at minus started_at. Wall elapsed, not human work time or a single message generation time.',
        'first_output_ms': 'Lifecycle time_to_first_token_ms only; first token need not be a visible assistant message.',
        'elapsed_from_start_ms': 'Assistant record timestamp minus lifecycle started_at. A message record is not proof of its first-token time. Negative or post-completion timestamps are null.',
        'gap_since_previous_reply_ms': 'Interval between successive assistant record timestamps in the same turn; includes tools, waiting and other work. NOT message generation duration. Replay timestamps can collapse to zero.',
        'generation_ms': 'Null: logs do not provide trustworthy per-message begin/done boundaries.',
        'timestamps': 'Lifecycle started_at/completed_at are Unix seconds (coarse); outer log timestamps may be replay/write times. Runtime duration_ms is retained even when endpoint subtraction differs.',
        'coverage': 'Includes primary, child and subagent sessions from the cleaned snapshot. Missing lifecycle stays unknown; no invented start/end.',
    }}


def run(input_root, cleaned, output):
    quality = collections.Counter()
    lifecycle = read_lifecycle(input_root, cleaned, quality)
    with (cleaned / 'events.jsonl').open() as stream:
        result = analyze((json.loads(line) for line in stream), lifecycle, quality)
    output.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(json.dumps(result['quality'], ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-root', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--cleaned', type=Path, default=Path('output/initial'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    run(args.input_root.expanduser(), args.cleaned, args.output or args.cleaned / 'timing.json')
