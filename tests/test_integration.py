"""Integration tests: source integrity, incremental cache, crash recovery and real CLI flows."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
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
from collect import collect, records
from reports import prepare, publish, aggregated_brief
from backfill import backfill

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.store = self.base / 'store'
        self.tz = ZoneInfo('UTC')
        self.day = datetime.now(self.tz).date() - timedelta(days=1)
        self.start = datetime.combine(self.day, datetime.min.time(), tzinfo=self.tz)
        self.end = self.start + timedelta(days=1)

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, name='s', text='尚未验证', at=None):
        path = self.home / '.pi/agent/sessions' / (name + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'type': 'session', 'id': name, 'cwd': '/project'}) + '\n' +
                        json.dumps({'timestamp': (at or self.start).isoformat(),
                                    'message': {'role': 'assistant', 'content': text}}) + '\n', encoding='utf-8')
        return path

    def prep(self, **extra):
        args = dict(store=self.store, home=self.home, period='day', date=str(self.day), timezone='UTC',
                    refresh=False, budget=4000, root=[], mode='live')
        args.update(extra)
        output = io.StringIO()
        with redirect_stdout(output):
            prepare(argparse.Namespace(**args))
        return json.loads(output.getvalue())

    def fill(self, result):
        path = Path(result['draft'])
        path.write_text(path.read_text().replace('待填写', '有证据的摘要').replace('状态：待完成', '状态：已完成'), encoding='utf-8')

    def finish(self, result):
        with redirect_stdout(io.StringIO()):
            publish(argparse.Namespace(manifest=Path(result['manifest'])))

    def scan(self, cache=True):
        return collect(self.home, {}, self.start, self.end, datetime.now(self.tz),
                       cache_dir=self.base / 'cache' if cache else None)

    def test_incremental_cache_reuses_records_and_invalidates_changes(self):
        path = self.source()
        self.source('other', '另一份旧证据')
        first = self.scan()
        with patch('collect.records', side_effect=AssertionError('unchanged source parsed')):
            second = self.scan()
        self.assertEqual(first['sessions'], second['sessions'])
        self.assertEqual(second['cache']['hits'], 2)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'timestamp': (self.start + timedelta(hours=1)).isoformat(),
                                     'message': {'role': 'assistant', 'content': '验证通过'}}) + '\n')
        third = self.scan()
        self.assertEqual(third['cache'], {'hits': 1, 'misses': 1})
        self.assertEqual(sum(len(s['messages']) for s in third['sessions']), 3)
        path.write_text('{}\n', encoding='utf-8')
        self.assertEqual(len(self.scan()['sessions']), 1)  # truncation cannot resurrect old evidence

    def test_cache_metadata_dependency_and_corruption(self):
        p = self.home / '.kimi/sessions/s'
        p.mkdir(parents=True)
        (p / 'wire.jsonl').write_text(json.dumps({'timestamp': self.start.timestamp(),
            'message': {'type': 'TurnBegin', 'payload': {'user_input': '任务'}}}), encoding='utf-8')
        state = p / 'state.json'
        state.write_text('{"cwd":"/old"}')
        self.assertEqual(self.scan()['sessions'][0]['cwd'], '/old')
        state.write_text('{"cwd":"/new"}')
        self.assertEqual(self.scan()['sessions'][0]['cwd'], '/new')
        for cache in (self.base / 'cache').glob('*.json'):
            cache.write_text('{broken')
        self.assertEqual(self.scan()['sessions'][0]['cwd'], '/new')

    def test_daily_source_append_rejects_publish(self):
        self.source()
        result = self.prep()
        self.fill(result)
        self.source(text='验证通过')
        with self.assertRaises(SystemExit):
            self.finish(result)
        self.assertFalse(Path(result['report']).exists())

    def test_unrelated_append_does_not_deadlock_publish(self):
        self.source()
        result = self.prep()
        self.fill(result)
        # A transcript outside the requested date and associated projects is irrelevant.
        p = self.source('unrelated', at=self.start - timedelta(days=10))
        self.finish(result)
        self.assertTrue(Path(result['report']).exists())

    def test_evidence_tampering_rejects_publish(self):
        self.source()
        result = self.prep()
        self.fill(result)
        manifest = json.loads(Path(result['manifest']).read_text())
        index = json.loads(Path(manifest['sources'][0]).read_text())
        Path(index['sessions'][0]['evidence']).write_text('{}')
        with self.assertRaises(SystemExit):
            self.finish(result)

    def test_history_live_history_transitions_and_protocol(self):
        self.source()
        history = self.prep(mode='history')
        self.assertEqual(history['status'], 'completed')
        self.assertIn('checked_at', history)
        self.assertFalse(history['source_checked'])  # history protocol never claims live freshness
        cached = self.prep(mode='history')
        self.assertEqual(cached['status'], 'cached')
        live = self.prep()
        self.fill(live)
        self.finish(live)
        self.assertEqual(json.loads(Path(live['report']).with_suffix('.meta.json').read_text())['mode'], 'live')
        self.assertTrue(Path(live['manifest']).with_name('previous-report.md').exists())
        self.assertEqual(self.prep()['status'], 'cached')

    def test_metadata_write_failure_recovers_without_stale_cache(self):
        self.source()
        result = self.prep()
        self.fill(result)
        from storage import atomic_json
        def fail_meta(path, value):
            if str(path).endswith('.meta.json'):
                raise OSError('simulated disk error')
            return atomic_json(path, value)
        with patch('reports.atomic_json', side_effect=fail_meta):
            with self.assertRaises(OSError):
                self.finish(result)
        target = Path(result['report'])
        self.assertTrue(target.with_suffix('.publishing.json').exists())
        next_result = self.prep()
        self.assertEqual(next_result['status'], 'prepared')
        self.fill(next_result)
        self.finish(next_result)
        self.assertFalse(target.with_suffix('.publishing.json').exists())
        self.assertEqual(self.prep()['status'], 'cached')

    def test_real_concurrent_publishers(self):
        self.source()
        result = self.prep()
        self.fill(result)
        command = [sys.executable, str(SCRIPTS / 'reports.py'), 'publish', result['manifest']]
        a = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        b = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        a.communicate(timeout=20)
        b.communicate(timeout=20)
        self.assertEqual(sorted([a.returncode, b.returncode]), [0, 1])

    def test_backfill_failure_records_progress_and_resume(self):
        self.source()
        args = argparse.Namespace(year=self.day.year, start=str(self.day), end=str(self.day), store=self.store,
                                  home=self.home, timezone='UTC', root=[], rebuild=False)
        with patch('backfill.collect', side_effect=ValueError('synthetic failure')):
            with self.assertRaises(ValueError), redirect_stderr(io.StringIO()):
                backfill(args)
        failures = list(self.store.glob('evidence/backfill/*/*/failed.json'))
        self.assertEqual(len(failures), 1)
        state = json.loads(failures[0].read_text())
        self.assertEqual(state['remaining'], [str(self.day)])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(backfill(args)['status'], 'completed')

    def test_dst_cli_and_utf8_with_ascii_locale(self):
        root = self.home / '.pi/agent/sessions'
        root.mkdir(parents=True)
        rows = [{'type': 'session', 'id': 'dst', 'cwd': '/p'}]
        for ts, text in [('2025-03-09T04:59:59Z', '前一天'), ('2025-03-09T05:00:00Z', '开始'),
                         ('2025-03-10T03:59:59Z', '结束'), ('2025-03-10T04:00:00Z', '后续')]:
            rows.append({'timestamp': ts, 'message': {'role': 'user', 'content': text}})
        (root / 's.jsonl').write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows), encoding='utf-8')
        import os
        env = dict(os.environ, LANG='C', LC_ALL='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0', PYTHONIOENCODING='utf-8')
        out = self.base / 'dst'
        subprocess.run([sys.executable, str(SCRIPTS / 'collect.py'), '--home', str(self.home), '--out', str(out),
                        '--date', '2025-03-09', '--timezone', 'America/New_York'], env=env, check=True, capture_output=True)
        index = json.loads((out / 'index.json').read_text())
        session = json.loads(Path(index['sessions'][0]['evidence']).read_text())
        self.assertEqual([m['text'] for m in session['messages'] if m['period'] == 'target'], ['开始', '结束'])
        self.assertEqual(index['window'], ['2025-03-09T00:00:00-05:00', '2025-03-10T00:00:00-04:00'])

    def test_aggregate_budget_retains_sections_and_discloses_omissions(self):
        sources = []
        for n in range(50):
            p = self.base / f'{n}.md'
            p.write_text('# 报告\n\n## 工作进展\n' + '进展' * 1000 + '\n## 断点\n' + '待办' * 1000, encoding='utf-8')
            sources.append(p)
        brief = aggregated_brief(sources, [], self.day, self.day, 2000)
        self.assertLessEqual(len(brief), 2000)
        self.assertIn('预算不足', brief)


if __name__ == '__main__':
    unittest.main()
