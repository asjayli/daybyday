import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backfill import backfill
from reports import prepare


class BackfillTests(unittest.TestCase):
    def test_snapshots_blanks_midnight_and_no_rescan(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            store = Path(tmp) / "reports"
            root = home / ".pi/agent/sessions"
            root.mkdir(parents=True)
            rows = [{"type":"session","id":"s1","cwd":"/project"}]
            for at, text in [("2026-03-31T15:59:59Z", "before range"),
                             ("2026-03-31T16:00:00Z", "开始处理"),
                             ("2026-04-01T15:59:59Z", "尚未验证"),
                             ("2026-04-02T16:00:00Z", "验证已完成"),
                             ("2026-04-03T16:00:00Z", "FUTURE_SHOULD_NOT_APPEAR")]:
                rows.append({"timestamp":at,"message":{"role":"assistant","content":text}})
            source = root / "s1.jsonl"
            source.write_text("\n".join(json.dumps(r) for r in rows))
            args = argparse.Namespace(year=2026,start="2026-04-01",end="2026-04-03",store=store,
                                      home=home,timezone="Asia/Shanghai",root=[],rebuild=False)
            with redirect_stderr(io.StringIO()):
                result = backfill(args)
            self.assertEqual(result["generated"],3)
            self.assertEqual(result["active_days"],2)
            self.assertEqual(result["placeholders"],1)
            first = (store/"daily/2026-04-01.md").read_text()
            last = (store/"daily/2026-04-03.md").read_text()
            self.assertIn("尚未验证",first)
            self.assertNotIn("验证已完成",first)
            self.assertIn("验证已完成",last)
            self.assertNotIn("FUTURE_SHOULD_NOT_APPEAR",last)
            self.assertNotIn("## 下一步建议",first)
            self.assertIn("空白占位",(store/"daily/2026-04-02.md").read_text())
            stamps = {p:p.stat().st_mtime_ns for p in store.rglob('*') if p.is_file()}
            source.write_text(source.read_text()+"\n"+json.dumps(rows[-1]))
            with patch("backfill.collect",side_effect=AssertionError("history must not scan")):
                again = backfill(args)
            self.assertFalse(again["scanned"])
            self.assertEqual(stamps,{p:p.stat().st_mtime_ns for p in store.rglob('*') if p.is_file()})
            # Explicit history retrieval also bypasses all live checks.
            with patch("reports.source_signature",side_effect=AssertionError("must not check")), redirect_stdout(io.StringIO()):
                prepare(argparse.Namespace(store=store,period="day",date="2026-04-01",mode="history",
                                           timezone="Asia/Shanghai",refresh=False,root=[],budget=4000))

    def test_existing_live_report_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);store=base/"store";home=base/"home";home.mkdir()
            target=store/"daily/2026-04-01.md";target.parent.mkdir(parents=True)
            target.write_text("live with later developments")
            target.with_suffix('.meta.json').write_text('{"mode":"live"}')
            args=argparse.Namespace(year=2026,start="2026-04-01",end="2026-04-01",store=store,
                                    home=home,timezone="Asia/Shanghai",root=[],rebuild=False)
            with redirect_stderr(io.StringIO()):
                backfill(args)
            self.assertIn('空白占位',target.read_text())
            backups=list(store.rglob('previous-report.md'))
            self.assertEqual(len(backups),1)
            self.assertEqual(backups[0].read_text(),'live with later developments')


if __name__ == '__main__':
    unittest.main()
