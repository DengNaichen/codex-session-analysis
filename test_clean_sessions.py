"""Synthetic fixtures only: no personal session contents are loaded."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import clean_sessions as cleaner


STAMP = '2026-09-19T02:00:00Z'


def rec(kind, payload):
    return {'timestamp': STAMP, 'type': kind, 'payload': payload}


def meta(identifier='session-a', **kwargs):
    return rec('session_meta', {'id': identifier, 'source': 'cli', **kwargs})


def message(identifier, text='继续', role='user'):
    return rec('event_msg', {'type': 'item_completed', 'item': {
        'type': 'UserMessage' if role == 'user' else 'AgentMessage',
        'id': identifier, 'content': [{'type': 'text', 'text': text}],
    }})


def normalized(*records):
    return cleaner.normalize(list(enumerate(records, 1)), 'sessions/fixture.jsonl', 'Asia/Shanghai')


class NormalizationTests(unittest.TestCase):
    def test_context_removed_but_adjacent_user_request_preserved(self):
        text = ('<recommended_plugins>injected plugin catalog</recommended_plugins>\n'
                '# AGENTS.md instructions for /Users/example/project\n'
                '<INSTRUCTIONS>injected repository rules</INSTRUCTIONS>\n'
                '<environment_context>injected environment</environment_context>\n'
                '请分析我的工作模式，保留这段真实请求。')
        summary, events = normalized(meta(), message('msg-a', text))
        self.assertEqual(events[0]['text'], '请分析我的工作模式，保留这段真实请求。')
        self.assertEqual(summary['user_message_count'], 1)

    def test_context_only_messages_are_dropped(self):
        summary, events = normalized(meta(), message('context', '<environment_context>cwd</environment_context>'))
        self.assertEqual(events, [])
        self.assertEqual(summary['dropped']['empty_or_context_only'], 1)

    def test_mirrored_representations_and_repeated_id_count_once(self):
        summary, events = normalized(
            meta(), rec('turn_context', {'turn_id': 'turn-a', 'model': 'fixture-model'}),
            rec('event_msg', {'type': 'user_message', 'message': '继续'}),
            rec('response_item', {'type': 'message', 'role': 'user', 'id': 'msg-a',
                                  'content': [{'type': 'input_text', 'text': '继续'}]}),
            message('msg-a'), message('msg-a'),
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(summary['user_message_count'], 1)
        self.assertEqual(summary['dropped']['alternate_representation'], 2)
        self.assertEqual(summary['dropped']['repeated_id'], 1)

    def test_identical_text_with_distinct_ids_is_preserved(self):
        _, events = normalized(meta(), message('msg-a'), message('msg-b'))
        self.assertEqual([e['record_id'] for e in events], ['msg-a', 'msg-b'])

    def test_primary_and_subagent_use_own_meta_id(self):
        primary, _ = normalized(meta('root', session_id='shared'))
        child, events = normalized(meta('child', session_id='shared', source={
            'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}), message('child-msg'))
        self.assertEqual(primary['session_kind'], 'primary')
        self.assertEqual(child['session_kind'], 'subagent')
        self.assertEqual(child['parent_session_id'], 'root')
        self.assertEqual(child['session_id'], 'child')
        self.assertEqual(child['root_session_id'], 'shared')
        self.assertEqual(events[0]['session_id'], 'child')

    def test_sensitive_values_redacted(self):
        original = ('email alice@example.com path /Users/alice/project '
                    'api_key="very-private-value" token sk-abcdefghijklmnopqrst '
                    'Bearer abc.def.xyz https://example.com/path?token=private#secret '
                    'https://user:pass@example.com/private\n'
                    '-----BEGIN PRIVATE KEY-----\nprivate-key-material\n-----END PRIVATE KEY-----')
        cleaned = cleaner.clean_text(original)
        for sensitive in ('alice', 'very-private-value', 'sk-abcdefghijklmnopqrst',
                          'abc.def.xyz', 'token=private', '#secret', 'user:pass', 'private-key-material'):
            self.assertNotIn(sensitive, cleaned)
        self.assertIn('/HOME/project', cleaned)
        self.assertIn('https://example.com/path', cleaned)
        self.assertIn('[PRIVATE_KEY]', cleaned)

    def test_timestamp_converted_and_source_line_retained(self):
        _, events = normalized(meta(), message('msg-a'))
        self.assertEqual(events[0]['local_timestamp'], '2026-09-19T10:00:00+08:00')
        self.assertEqual(events[0]['source_line'], 2)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / 'input'
        (self.source / 'sessions').mkdir(parents=True)
        (self.source / 'archived_sessions').mkdir()
        self.output = self.base / 'output'

    def write_session(self, name, records, suffix=''):
        path = self.source / name
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records) + suffix)
        return path

    def run_cleaner(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cleaner.run(self.source, self.output, 'Asia/Shanghai')

    def read_rows(self, filename):
        return [json.loads(line) for line in (self.output / filename).read_text().splitlines()]

    def test_corruption_reported_source_unchanged_and_primary_corpus_excludes_subagent(self):
        path = self.write_session('sessions/root.jsonl', [meta('root', session_id='shared'), message('a', '请实现')],
                                  '{broken json\n[]\n')
        before = path.read_bytes()
        self.write_session('archived_sessions/child.jsonl', [meta('child', session_id='shared', source={
            'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}), message('b', '内部子任务')])
        self.run_cleaner()
        self.assertEqual(path.read_bytes(), before)
        report = json.loads((self.output / 'report.json').read_text())
        self.assertEqual(report['counts']['input_files'], 2)
        self.assertEqual(report['counts']['primary_sessions'], 1)
        self.assertEqual(report['counts']['subagent_sessions'], 1)
        self.assertEqual([i['line'] for i in report['issues']], [3, 4])
        self.assertTrue(all(i['reason'] == 'invalid_json_record' for i in report['issues']))
        self.assertEqual([r['session_id'] for r in self.read_rows('primary_messages.jsonl')], ['root'])
        self.assertEqual({r['session_id'] for r in self.read_rows('events.jsonl')}, {'root', 'child'})
        self.assertEqual(len(self.read_rows('manifest.jsonl')), 2)

    def test_existing_output_never_overwritten(self):
        self.output.mkdir()
        marker = self.output / 'events.jsonl'
        marker.write_text('existing valuable output')
        with self.assertRaises(FileExistsError):
            self.run_cleaner()
        self.assertEqual(marker.read_text(), 'existing valuable output')

    def test_output_cannot_equal_or_be_nested_under_source(self):
        for target in (self.source, self.source / 'new-output'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                cleaner.run(self.source, target, 'Asia/Shanghai')
        self.assertFalse((self.source / 'new-output').exists())

    def test_same_thread_multiple_files_merge_without_losing_unique_events(self):
        self.write_session('sessions/a.jsonl', [meta('same'), message('a')])
        self.write_session('archived_sessions/b.jsonl', [meta('same'), message('a'), message('b')])
        self.run_cleaner()
        sessions = self.read_rows('sessions.jsonl')
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['event_count'], 2)
        self.assertEqual(sessions[0]['user_message_count'], 2)
        self.assertEqual(set(sessions[0]['source_files']), {'sessions/a.jsonl', 'archived_sessions/b.jsonl'})
        self.assertEqual([r['record_id'] for r in self.read_rows('events.jsonl')], ['a', 'b'])
        report = json.loads((self.output / 'report.json').read_text())
        self.assertEqual(report['counts']['primary_sessions'], 1)
        self.assertEqual(report['counts']['duplicate_events'], 1)


if __name__ == '__main__':
    unittest.main()
