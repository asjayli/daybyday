"""Integration tests: source integrity, incremental cache, crash recovery and real CLI flows."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
import io
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect import collect, records
from reports import prepare, publish, aggregated_brief
from freshness import source_signature
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

    def scan(self, cache=True, self_extra=()):
        return collect(self.home, {}, self.start, self.end, datetime.now(self.tz),
                       cache_dir=self.base / 'cache' if cache else None, self_paths=self_extra)

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


    # --- 兼容并发活动会话：发布窗口、半行写入、自身会话标记 ---

    def append_line(self, path, text, at):
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'timestamp': at.isoformat(),
                                     'message': {'role': 'assistant', 'content': text}}) + '\n')

    def test_relevant_append_after_evidence_at_allows_publish(self):
        path = self.source()
        result = self.prep()
        self.fill(result)
        evidence_at = datetime.fromisoformat(json.loads(
            Path(result['manifest']).read_text(encoding='utf-8'))['evidence_at'])
        # 同项目相关会话在证据截止之后继续追加：属下一次更新范围，不得拒绝本次发布。
        self.append_line(path, '刚又推进了一步', evidence_at + timedelta(minutes=5))
        self.finish(result)
        self.assertTrue(Path(result['report']).exists())

    def test_backdated_relevant_append_still_rejects_publish(self):
        path = self.source()
        result = self.prep()
        self.fill(result)
        evidence_at = datetime.fromisoformat(json.loads(
            Path(result['manifest']).read_text(encoding='utf-8'))['evidence_at'])
        # 追加的消息时间早于证据截止：真实证据变化，仍须拒绝。
        self.append_line(path, '截止前的补充', evidence_at - timedelta(minutes=1))
        with self.assertRaises(SystemExit):
            self.finish(result)
        self.assertFalse(Path(result['report']).exists())

    def concurrent_append(self, at):
        """Intercept prepare's post-collect signature check to append like an active session."""
        real, calls = source_signature, []

        def fake(home, roots, tz):
            if len(calls) == 1:  # 第二次调用即 collect 之后的复查：此刻模拟并发写入
                self.append_line(self.concurrent_path, '采集期间的并发消息', at)
            calls.append(1)
            return real(home, roots, tz)
        return patch('reports.source_signature', side_effect=fake)

    def test_prepare_survives_concurrent_append_during_collect(self):
        self.concurrent_path = self.source()
        # 晚于证据截止的并发追加（如正在请求回顾的活跃会话）：prepare 不得无限重试。
        with self.concurrent_append(datetime.now(self.tz) + timedelta(minutes=5)):
            result = self.prep()
        self.assertEqual(result['status'], 'prepared')
        self.fill(result)
        self.finish(result)
        self.assertTrue(Path(result['report']).exists())

    def test_prepare_rejects_backdated_append_during_collect(self):
        self.concurrent_path = self.source()
        # 窗口内的回溯写入仍是真实证据变化，必须拒绝并留痕。
        with self.concurrent_append(self.start + timedelta(hours=2)):
            with self.assertRaises(SystemExit):
                self.prep()
        failed = sorted((self.store / 'evidence' / 'daily' / str(self.day)).glob('*/failed.json'))
        self.assertTrue(any('source_changed_during_collection' in p.read_text(encoding='utf-8')
                            for p in failed))

    def test_partial_trailing_line_is_not_a_warning(self):
        path = self.source('halfline', at=self.start)
        with path.open('a', encoding='utf-8') as handle:
            handle.write('{"timestamp": "' + self.start.isoformat())  # 正在写入的半行
        result = self.scan()
        self.assertEqual([w for w in result['warnings'] if 'halfline' in w], [])

    def test_self_session_by_env_excluded_from_candidates(self):
        self.source()
        # CLAUDE_CODE_SESSION_ID 对应 .claude/projects 下的同名会话文件
        mine = self.home / '.claude' / 'projects' / 'p' / 'me-now.jsonl'
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text(json.dumps({'type': 'session', 'id': 'me-now', 'cwd': '/project'}) + '\n' +
                        json.dumps({'timestamp': (self.end + timedelta(hours=2)).isoformat(),
                                    'message': {'role': 'user', 'content': '帮我回顾昨天'}}) + '\n', encoding='utf-8')
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'me-now'}):
            result = self.scan()
        self.assertNotIn('me-now', {s['session_id'] for s in result['sessions']})
        self.assertIn('me-now', {Path(p).stem for p in result.get('self_excluded', [])})

    def test_self_session_by_arg_and_midnight_retention(self):
        self.source()
        mine = self.home / '.pi/agent/sessions/mine.jsonl'
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'session', 'id': 'mine', 'cwd': '/project'},
            {'timestamp': self.start.isoformat(), 'message': {'role': 'user', 'content': '昨天的工作'}},
            {'timestamp': (self.end + timedelta(hours=1)).isoformat(),
             'message': {'role': 'user', 'content': '今天继续，顺便回顾昨天'}}]) + '\n', encoding='utf-8')
        others = self.home / '.pi/agent/sessions/today-only.jsonl'
        others.write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'session', 'id': 'today-only', 'cwd': '/project'},
            {'timestamp': (self.end + timedelta(hours=3)).isoformat(),
             'message': {'role': 'user', 'content': '今天的新会话'}}]) + '\n', encoding='utf-8')
        result = self.scan(self_extra=[str(others)])
        # 显式标记的纯今日会话被排除；跨午夜含昨日消息的自身会话保留昨日证据。
        self.assertNotIn('today-only', {s['session_id'] for s in result['sessions']})
        mine_kept = self.scan(self_extra=[str(mine)])
        self.assertIn('mine', {s['session_id'] for s in mine_kept['sessions']})


    def test_publish_revalidation_ignores_current_env_self_markers(self):
        # manifest 固化的排除清单是发布重验证的唯一依据；当前进程 env 不得改变证据集合。
        # 同 cwd 的今日会话放在 .claude/projects 下，env 标识可以精确定位它。
        self.source()
        other = self.home / '.claude' / 'projects' / 'p' / 'today-c.jsonl'
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text(json.dumps({'type': 'session', 'id': 'today-c', 'cwd': '/project'}) + '\n' +
                         json.dumps({'timestamp': (self.end + timedelta(hours=1)).isoformat(),
                                     'message': {'role': 'user', 'content': '今天继续'}}) + '\n', encoding='utf-8')
        env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CODE_SESSION_ID'}
        with patch.dict(os.environ, env, clear=True):
            result = self.prep()
            self.fill(result)
            evidence_at = datetime.fromisoformat(json.loads(
                Path(result['manifest']).read_text(encoding='utf-8'))['evidence_at'])
            self.append_line(other, '又推进一步', evidence_at + timedelta(minutes=3))
            with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'today-c'}):
                self.finish(result)
        self.assertTrue(Path(result['report']).exists())

    def test_message_exactly_at_evidence_cutoff_allows_publish(self):
        path = self.source()
        result = self.prep()
        self.fill(result)
        evidence_at = datetime.fromisoformat(json.loads(
            Path(result['manifest']).read_text(encoding='utf-8'))['evidence_at'])
        # 半开区间：恰等于截止时间的消息归下一次更新，不拒绝本次发布。
        self.append_line(path, '恰好截止', evidence_at)
        self.finish(result)
        self.assertTrue(Path(result['report']).exists())

    def test_wildcard_session_id_env_is_ignored(self):
        self.source()
        keep = self.home / '.claude' / 'projects' / 'p' / 'keep.jsonl'
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text(json.dumps({'type': 'session', 'id': 'keep', 'cwd': '/project'}) + '\n' +
                        json.dumps({'timestamp': self.start.isoformat(),
                                    'message': {'role': 'user', 'content': '昨日的真实工作'}}) + '\n', encoding='utf-8')
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': '*'}):
            result = self.scan()
        self.assertIn('keep', {s['session_id'] for s in result['sessions']})

    def test_self_path_missing_warns_and_is_ignored(self):
        self.source()
        err = io.StringIO()
        with redirect_stderr(err):
            result = self.scan(self_extra=[str(self.base / 'no-such-session.jsonl')])
        self.assertIn('不存在', err.getvalue())
        self.assertEqual(len(result['sessions']), 1)

    @unittest.skipUnless(shutil.which('zstd'), 'zstd unavailable')
    def test_zstd_torn_tail_is_reported_as_damage(self):
        plain = self.base / 'torn.jsonl'
        plain.write_text(json.dumps({'type': 'session', 'id': 'ds', 'cwd': '/p'}) + '\n' +
                         json.dumps({'type': 'user/message', 'time': self.start.timestamp() * 1000,
                                     'data': {'content': [{'type': 'text', 'text': '任务'}]}}) + '\n' +
                         '{"type": "user/message", "time": ', encoding='utf-8')
        target = self.home / '.dsh' / 'sessions' / 'session.v3.jsonl.zstd'
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['zstd', '-q', str(plain), '-o', str(target)], check=True)
        result = self.scan()
        self.assertTrue([w for w in result['warnings'] if 'session.v3.jsonl.zstd' in w])


if __name__ == '__main__':
    unittest.main()
