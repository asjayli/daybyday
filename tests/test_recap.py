import argparse
from contextlib import redirect_stdout
from datetime import datetime, date
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from adapters import extra_records
from collect import collect, stamp, text_of, redact, db_records
from brief import candidate, PENDING, briefing
from reports import period_bounds, prepare, publish, inputs_for, make_template


class RecapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.tz = ZoneInfo("Asia/Shanghai")
        self.start = datetime(2026, 10, 27, tzinfo=self.tz)
        self.end = datetime(2026, 10, 28, tzinfo=self.tz)
        self.now = datetime(2026, 10, 28, 10, tzinfo=self.tz)

    def tearDown(self):
        self.tmp.cleanup()

    def write_jsonl(self, relative, rows):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def test_midnight_followup_dedup_and_undated(self):
        rows = [{"type": "session", "id": "test", "cwd": "/project"}]
        for ts, text in [("2026-10-26T15:59:59Z", "before"),
                         ("2026-10-26T16:00:00Z", "start"),
                         ("2026-10-26T16:00:00Z", "start"),
                         ("2026-10-27T15:59:59Z", "end"),
                         ("2026-10-27T16:00:00Z", "followup")]:
            rows.append({"timestamp": ts, "message": {"role": "user", "content": text}})
        self.write_jsonl(".pi/agent/sessions/test.jsonl", rows)
        self.write_jsonl(".cursor/projects/p/agent-transcripts/test.jsonl", [{"role": "user", "message": {"content": "undated"}}])
        result = collect(self.home, {}, self.start, self.end, self.now)
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual([m["text"] for m in result["sessions"][0]["messages"]], ["start", "end", "followup"])
        self.assertEqual(result["sessions"][0]["messages"][-1]["period"], "followup")
        self.assertEqual(result["coverage"]["cursor"]["undated_messages"], 1)

    def test_timestamp_units_and_dst(self):
        ts = self.start.timestamp()
        self.assertEqual(stamp(str(ts)), stamp(ts*1000))
        self.assertIsNone(stamp("2026-10-27T00:00:00"))
        tz = ZoneInfo("America/New_York")
        a = datetime(2026, 3, 8, tzinfo=tz)
        b = datetime(2026, 3, 9, tzinfo=tz)
        self.assertEqual(b.timestamp()-a.timestamp(), 23*3600)
        self.assertEqual(period_bounds("week", date(2027, 1, 1))[2], "2026-W53")

    def test_kimi_generations_and_zcode_desktop(self):
        p = self.write_jsonl("wire.jsonl", [
            {"timestamp": self.start.timestamp(), "message": {"type": "TurnBegin", "payload": {"user_input": "task"}}},
            {"timestamp": self.start.timestamp(), "message": {"type": "ContentPart", "payload": {"type": "text", "text": "answer"}}}])
        rows = list(extra_records(p, "kimi", [], stamp, text_of))
        self.assertEqual([r[4] for r in rows], ["task", "answer"])
        p = self.write_jsonl("new-wire.jsonl", [
            {"type": "turn.prompt", "time": self.start.timestamp()*1000, "input": [{"type": "text", "text": "task"}]},
            {"type": "context.append_loop_event", "time": self.start.timestamp()*1000, "event": {"type": "content.part", "part": {"type": "text", "text": "done"}}}])
        self.assertEqual(len(list(extra_records(p, "kimi-code", [], stamp, text_of))), 2)
        p = self.home / "desktop.json"
        p.write_text(json.dumps({"meta": {"taskId": "task1", "workspacePath": "/p"}, "messages": [{"role": "user", "timestamp": self.start.timestamp()*1000, "content": "hello"}]}))
        row = list(extra_records(p, "zcode-desktop", [], stamp, text_of))[0]
        self.assertEqual(row[:3], ("task1", "/p", "user"))

    @unittest.skipUnless(shutil.which("zstd"), "zstd unavailable")
    def test_deepseek_compressed(self):
        rows = [{"type": "session", "id": "ds1", "cwd": "/p"},
                {"type": "user/message", "time": self.start.timestamp()*1000, "data": {"content": [{"type": "text", "text": "task"}]}},
                {"type": "assistant/message", "time": self.start.timestamp()*1000, "data": {"message": {"content": [{"type": "text", "text": "answer"}]}}}]
        plain = self.write_jsonl("session.v3.jsonl", rows)
        compressed = self.home / "session.v3.jsonl.zstd"
        subprocess.run(["zstd", "-q", str(plain), "-o", str(compressed)], check=True)
        parsed = list(extra_records(compressed, "deepseek-harness", [], stamp, text_of))
        self.assertEqual([r[4] for r in parsed], ["task", "answer"])
        self.assertEqual(parsed[0][0], "ds1")

    def test_sqlite_readonly(self):
        path = self.home / "test.db"
        with sqlite3.connect(path) as db:
            db.executescript("CREATE TABLE session(id TEXT,directory TEXT); CREATE TABLE message(id TEXT,session_id TEXT,time_created INTEGER,data TEXT); CREATE TABLE part(id TEXT,message_id TEXT,data TEXT);")
            db.execute("INSERT INTO session VALUES('s','/p')")
            db.execute("INSERT INTO message VALUES('m','s',?,?)", (self.start.timestamp()*1000, '{"role":"assistant"}'))
            db.execute("INSERT INTO part VALUES('p','m',?)", ('{"type":"text","text":"done"}',))
        db.close()
        before = path.read_bytes()
        rows = list(db_records(path, self.start, self.now))
        self.assertEqual(rows[0][4], "done")
        self.assertEqual(before, path.read_bytes())

    def test_redaction_and_old_error_not_pending(self):
        for value in ["root / secret123", "password=secret123", "mysql://root:secret123@localhost", "密码是 secret123"]:
            self.assertNotIn("secret123", redact(value))
        messages = [{"role": "assistant", "text": "测试失败"}, {"role": "assistant", "text": "已修复，测试 0 失败"}]
        self.assertEqual(candidate(messages, PENDING, 100), "")

    def test_fixed_store_publish_cache_and_refresh(self):
        args = argparse.Namespace(store=self.home, period="month", date="2026-09-01", timezone=None,
                                  refresh=False, budget=4000, root=[])
        def prep():
            output = io.StringIO()
            with redirect_stdout(output):
                prepare(args)
            return json.loads(output.getvalue())
        first = prep()
        self.assertEqual(prep()["manifest"], first["manifest"])
        manifest = Path(first["manifest"])
        draft = Path(first["draft"])
        with self.assertRaises(SystemExit):
            publish(argparse.Namespace(manifest=manifest))
        draft.write_text(draft.read_text().replace("待填写", "无数据，未补编。").replace("状态：待完成", "状态：已完成"))
        with redirect_stdout(io.StringIO()):
            publish(argparse.Namespace(manifest=manifest))
        with patch("subprocess.run", side_effect=AssertionError("cache must not scan")):
            result = prep()
        self.assertEqual(result["status"], "cached")
        self.assertEqual(Path(result["report"]), self.home / "monthly/2026-09.md")
        args.refresh = True
        refreshed = prep()
        self.assertNotEqual(refreshed["manifest"], first["manifest"])

    def test_year_avoids_month_daily_double_count(self):
        for relative in ["monthly/2025-01.md", "daily/2025-01-01.md", "daily/2025-02-01.md"]:
            p = self.home / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("report")
        sources, missing = inputs_for(self.home, "year", date(2025, 1, 1), date(2025, 12, 31))
        self.assertEqual({p.name for p in sources}, {"2025-01.md", "2025-02-01.md"})
        self.assertIn("2025-02-02", missing)

    def test_brief_budget(self):
        source = self.home / "session.json"
        source.write_text(json.dumps({"messages": [{"period": "target", "role": "user", "text": "任务"*1000}, {"period": "target", "role": "assistant", "text": "已完成"*1000}]}))
        index = {"date": "2026-10-27", "window": ["start", "end"], "collected_at": "now", "coverage": {}, "sessions": [
            {"agent": "pi", "cwd": "/p", "evidence": str(source), "target_messages": 2, "followup_messages": 0} for _ in range(60)]}
        output = briefing(index, 4000)
        self.assertLessEqual(len(output), 4001)
        self.assertIn("预算限制", output)


if __name__ == "__main__":
    unittest.main()
