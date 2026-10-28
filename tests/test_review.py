"""Regression tests for the combined review findings; synthetic evidence only."""
import argparse
from contextlib import redirect_stdout
from datetime import date, datetime
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from reports import prepare, publish, inputs_for
from freshness import source_signature
from collect import collect, redact
from backfill import frozen


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def prep(self, **kwargs):
        args = dict(store=self.root, period='month', date='2025-09-01', timezone='UTC',
                    refresh=False, budget=4000, root=[])
        args.update(kwargs)
        output = io.StringIO()
        with redirect_stdout(output):
            prepare(argparse.Namespace(**args))
        return json.loads(output.getvalue())

    def fill(self, result):
        draft = Path(result['draft'])
        draft.write_text(draft.read_text().replace('待填写', '摘要').replace('状态：待完成', '状态：已完成'))

    def finish(self, result):
        with redirect_stdout(io.StringIO()):
            publish(argparse.Namespace(manifest=Path(result['manifest'])))

    def daily(self, name='2025-09-01', text='evidence'):
        p = self.root / 'daily' / (name + '.md')
        p.parent.mkdir(exist_ok=True)
        p.write_text(text)
        return p

    def test_new_source_blocks_publish(self):
        self.daily()
        result = self.prep()
        self.fill(result)
        self.daily('2025-09-02')
        with self.assertRaises(SystemExit):
            self.finish(result)

    def test_kimi_state_invalidates_signature(self):
        p = self.root / '.kimi/sessions/s'
        p.mkdir(parents=True)
        (p / 'wire.jsonl').write_text('{}')
        state = p / 'state.json'
        state.write_text('{"cwd":"/old"}')
        before = source_signature(self.root, [], None)
        state.write_text('{"cwd":"/new-project"}')
        self.assertNotEqual(before, source_signature(self.root, [], None))

    def test_cursor_workspace_invalidates_signature(self):
        p = self.root / '.config/Cursor/User/workspaceStorage/w'
        p.mkdir(parents=True)
        state = p / 'workspace.json'
        state.write_text('{"folder":"file:///old"}')
        before = source_signature(self.root, [], None)
        state.write_text('{"folder":"file:///new"}')
        self.assertNotEqual(before, source_signature(self.root, [], None))

    def test_bad_desktop_does_not_abort_collection(self):
        p = self.root / '.zcode/v2/sessions'
        p.mkdir(parents=True)
        (p / 'session_bad.json').write_text('[]')
        result = collect(self.root, {}, datetime(2025, 1, 1, tzinfo=ZoneInfo('UTC')),
                         datetime(2025, 1, 2, tzinfo=ZoneInfo('UTC')),
                         datetime(2025, 1, 2, tzinfo=ZoneInfo('UTC')))
        self.assertEqual(result['coverage']['zcode-desktop']['status'], 'partial')

    def test_pagination_respects_budget(self):
        ev = self.root / 'session.json'
        ev.write_text(json.dumps({'messages': [
            {'period': 'target', 'role': 'user', 'text': 'x' * 1000},
            {'period': 'target', 'role': 'assistant', 'text': 'y' * 1000}]}))
        index = self.root / 'index.json'
        index.write_text(json.dumps({'sessions': [
            {'agent': 'pi', 'cwd': '/p', 'evidence': str(ev)}] * 10}))
        output = subprocess.check_output([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/brief.py'),
                                          str(index), '--limit', '10', '--budget', '2000'], text=True)
        self.assertLessEqual(len(output), 2000)
        self.assertIn('--offset', output)

    def test_corrupt_metadata_rebuilds_and_preserves_original(self):
        target = self.root / 'monthly/2025-09.md'
        target.parent.mkdir()
        target.write_text('old report')
        meta = target.with_suffix('.meta.json')
        meta.write_text('{broken')
        self.assertFalse(frozen(target))
        result = self.prep()
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(target.read_text(), 'old report')
        self.assertEqual(meta.read_text(), '{broken')

    def test_timezone_cutoff(self):
        self.daily('2025-09-01')
        self.daily('2025-09-02')
        sources, missing = inputs_for(self.root, 'month', date(2025, 9, 1), date(2025, 9, 30),
                                     today=date(2025, 9, 3))
        self.assertEqual(len(sources), 2)
        self.assertEqual(missing, [])

    def test_path_redaction_false_positive(self):
        self.assertEqual(redact('root /app/project'), 'root /app/project')
        self.assertNotIn('secret123', redact('root / secret123'))

    def test_checked_at_on_prepared_and_reused(self):
        first = self.prep()
        self.assertIn('checked_at', first)
        second = self.prep()
        self.assertIn('checked_at', second)
        self.assertTrue(second['reused'])

    def test_existing_lock_and_target_edit_reject_publication(self):
        result = self.prep()
        self.fill(result)
        target = Path(result['report'])
        target.parent.mkdir(exist_ok=True)
        lock = target.with_suffix('.lock')
        lock.touch()
        with self.assertRaises(SystemExit):
            self.finish(result)
        lock.unlink()
        target.write_text('another writer')
        with self.assertRaises(SystemExit):
            self.finish(result)
        self.assertEqual(target.read_text(), 'another writer')


if __name__ == '__main__':
    unittest.main()
