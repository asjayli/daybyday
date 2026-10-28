import argparse
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from reports import prepare, publish
from collect import collect, save


class FreshnessTests(unittest.TestCase):
    def test_morning_afternoon_and_unrelated_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            args = argparse.Namespace(store=store, period="day", date="2026-10-27", timezone=None,
                                      refresh=False, budget=4000, root=[])
            state = {"signature": "morning", "reply": "尚未验证", "at": "2026-10-28T08:00:00+08:00"}
            calls = []

            def fake_command(command, **kwargs):
                calls.append(command)
                if command[1].endswith("collect.py"):
                    out = Path(command[command.index("--out")+1])
                    out.mkdir(parents=True)
                    session = {"agent": "pi", "session_id": "s", "cwd": "/p", "source": "/original/s.jsonl",
                               "messages": [{"period": "target", "at": "2026-10-27T12:00:00+08:00", "role": "user", "text": "task"},
                                            {"period": "followup", "at": state["at"], "role": "assistant", "text": state["reply"]}]}
                    evidence = out / "session.json"
                    evidence.write_text(json.dumps(session))
                    index = {"collected_at": state["at"], "sessions": [{"evidence": str(evidence)}],
                             "coverage": {}, "warnings": [], "additional_candidates": []}
                    (out / "index.json").write_text(json.dumps(index))
                else:
                    Path(command[2]).with_name("brief.md").write_text("brief")

            def run():
                output = io.StringIO()
                with redirect_stdout(output):
                    prepare(args)
                return json.loads(output.getvalue())

            def finish(result):
                draft = Path(result["draft"])
                draft.write_text(draft.read_text().replace("待填写", state["reply"]).replace("状态：待完成", "状态：已完成"))
                with redirect_stdout(io.StringIO()):
                    publish(argparse.Namespace(manifest=Path(result["manifest"])))

            with patch("reports.source_signature", side_effect=lambda *a: state["signature"]), patch("reports.subprocess.run", side_effect=fake_command):
                morning = run()
                finish(morning)
                count = len(calls)
                self.assertEqual(run()["status"], "cached")
                self.assertEqual(len(calls), count)  # Metadata-only check; no transcript parse.
                state["signature"] = "unrelated-session-appended"
                unchanged = run()
                self.assertEqual(unchanged["status"], "cached")
                self.assertEqual(unchanged["reason"], "source_changed_but_relevant_evidence_unchanged")
                state.update(signature="afternoon", reply="验证已通过，旧阻塞已解除", at="2026-10-28T16:00:00+08:00")
                afternoon = run()
                self.assertEqual(afternoon["status"], "prepared")
                self.assertEqual(afternoon["reason"], "evidence_changed")
                self.assertNotEqual(afternoon["manifest"], morning["manifest"])
                finish(afternoon)
                self.assertIn("旧阻塞已解除", Path(afternoon["report"]).read_text())
                self.assertTrue(Path(afternoon["manifest"]).with_name("previous-report.md").exists())

    def test_changed_daily_report_invalidates_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            daily = store / "daily/2026-09-01.md"
            daily.parent.mkdir()
            daily.write_text("first")
            args = argparse.Namespace(store=store, period="month", date="2026-09-01", timezone=None,
                                      refresh=False, budget=4000, root=[])
            def run():
                out = io.StringIO()
                with redirect_stdout(out):
                    prepare(args)
                return json.loads(out.getvalue())
            result = run()
            draft = Path(result["draft"])
            draft.write_text(draft.read_text().replace("待填写", "摘要").replace("状态：待完成", "状态：已完成"))
            with redirect_stdout(io.StringIO()):
                publish(argparse.Namespace(manifest=Path(result["manifest"])))
            self.assertEqual(run()["status"], "cached")
            daily.write_text("changed after afternoon update")
            self.assertEqual(run()["status"], "prepared")

    def test_new_session_followup_in_same_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / ".pi/agent/sessions"
            root.mkdir(parents=True)
            for name, day in [("old", 27), ("new", 28), ("report-self", 28)]:
                rows = [{"type": "session", "id": name, "cwd": "/project"},
                        {"timestamp": f"2026-10-{day}T01:00:00Z", "message": {"role": "assistant", "content": name}}]
                (root / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            tz = ZoneInfo("Asia/Shanghai")
            with patch.dict("os.environ", {"PI_SESSION_FILE": str(root / "report-self.jsonl")}):
                result = collect(home, {}, datetime(2026,10,27,tzinfo=tz), datetime(2026,10,28,tzinfo=tz), datetime(2026,10,28,16,tzinfo=tz))
            self.assertEqual({s["session_id"] for s in result["sessions"]}, {"old", "new"})
            new = next(s for s in result["sessions"] if s["session_id"] == "new")
            self.assertTrue(new["continuation_only"])
            out = home / "evidence"
            save(result, out)
            index = json.loads((out / "index.json").read_text())
            self.assertEqual(index["sessions"][1]["target_messages"], 0)


if __name__ == "__main__":
    unittest.main()
