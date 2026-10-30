import collections
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from analyze_timing import analyze, read_lifecycle, json_rows


def message(sid='s', tid='t', timestamp='2026-01-01T00:00:03Z', phase='final_answer', source='one', line=1):
    return dict(kind='message', session_id=sid, turn_id=tid, role='assistant', timestamp=timestamp, phase=phase, source_file=source, source_line=line, text='reply', record_id=f'{source}-{line}')


class TimingTests(unittest.TestCase):
    def test_runtime_duration_and_message_gap_are_distinct(self):
        events = [message(timestamp='2026-01-01T00:00:01Z', phase='commentary'), message(line=2)]
        life = {('s', 't'): [{'type': 'task_complete', 'started_at': 1767225600, 'completed_at': 1767225604, 'duration_ms': 3750, 'time_to_first_token_ms': 250}]}
        result = analyze(events, life)
        self.assertEqual(result['turns'][0]['total_ms'], 3750)
        self.assertEqual(result['turns'][0]['first_output_ms'], 250)
        self.assertEqual(result['replies'][1]['gap_since_previous_reply_ms'], 2000)
        self.assertEqual(result['replies'][1]['elapsed_from_start_ms'], 3000)
        self.assertTrue(all(r['generation_ms'] is None for r in result['replies']))

    def test_session_identity_and_missing_lifecycle(self):
        result = analyze([message(), message(sid='other')], {('s', 't'): [{'type': 'task_started', 'started_at': 1767225600}]})
        self.assertEqual(len(result['turns']), 2)
        other = next(t for t in result['turns'] if t['session_id'] == 'other')
        self.assertEqual(other['status'], 'unknown')
        self.assertIsNone(other['total_ms'])
        self.assertIsNone(other['started_at'])
        self.assertEqual(result['quality']['reply_lifecycle_association_rate'], .5)

    def test_invalid_times_are_excluded(self):
        life = {('s', 't'): [{'type': 'task_complete', 'started_at': 1767225602, 'completed_at': 1767225604, 'duration_ms': -1, 'time_to_first_token_ms': -2}]}
        result = analyze([message(timestamp='2026-01-01T00:00:01Z'), message(timestamp='2026-01-01T00:00:08Z', line=2)], life)
        self.assertTrue(all(r['elapsed_from_start_ms'] is None for r in result['replies']))
        self.assertIsNone(result['turns'][0]['total_ms'])
        self.assertIsNone(result['turns'][0]['first_output_ms'])
        self.assertEqual(result['quality']['negative_elapsed_from_start_ms'], 1)
        self.assertEqual(result['quality']['reply_timestamp_after_completion'], 1)

    def test_continuation_files_and_fixed_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cleaned = root / 'cleaned'; cleaned.mkdir()
            sources = ['one.jsonl', 'two.jsonl']; manifest = []
            payloads = [{'type': 'task_started', 'turn_id': 't', 'started_at': 1767225600}, {'type': 'task_complete', 'turn_id': 't', 'started_at': 1767225600, 'completed_at': 1767225604, 'duration_ms': 4000}]
            for source, payload in zip(sources, payloads):
                data = (json.dumps({'type': 'event_msg', 'payload': payload}) + '\n').encode()
                (root/source).write_bytes(data)
                manifest.append(dict(source_file=source, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
            with (root/sources[1]).open('a') as stream:
                stream.write('{"later":"ignored"}\n')
            (cleaned/'manifest.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in manifest))
            (cleaned/'sessions.jsonl').write_text(json.dumps(dict(session_id='s', source_file=sources[0], source_files=sources))+'\n')
            quality = collections.Counter()
            life = read_lifecycle(root, cleaned, quality)
            result = analyze([message(source='two.jsonl')], life, quality)
            self.assertEqual(len(life[('s', 't')]), 2)
            self.assertEqual(result['turns'][0]['total_ms'], 4000)
            self.assertEqual(quality['verified_source_files'], 2)

    def test_real_snapshot_completeness(self):
        cleaned = Path(__file__).parent/'output/initial'
        if not (cleaned/'timing.json').exists():
            self.skipTest('Real snapshot not present')
        result = json.loads((cleaned/'timing.json').read_text())
        events = list(json_rows(cleaned/'events.jsonl'))
        assistants = [e for e in events if e['kind'] == 'message' and e.get('role') == 'assistant']
        self.assertEqual(len(result['replies']), len(assistants))
        identities = {(t['session_id'], t['turn_id']) for t in result['turns']}
        self.assertEqual(len(identities), len(result['turns']))
        self.assertTrue(all((r['session_id'], r['turn_id']) in identities for r in result['replies']))
        self.assertEqual(sum(t['assistant_message_count'] for t in result['turns']), len(assistants))
        self.assertGreater(result['quality']['reply_lifecycle_association_rate'], .99)
        self.assertGreater(result['quality']['reply_elapsed_coverage_rate'], .90)
        self.assertTrue(all(r['generation_ms'] is None for r in result['replies']))
        self.assertTrue(all(r['elapsed_from_start_ms'] is None or r['elapsed_from_start_ms'] >= 0 for r in result['replies']))
        self.assertEqual(result['quality'].get('snapshot_hash_mismatches', 0), 0)


if __name__ == '__main__':
    unittest.main()
