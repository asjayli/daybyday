import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from collect import collect
from local_stores import inventory
from coverage_report import render, unresolved


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        tz = ZoneInfo("Asia/Shanghai")
        self.start, self.end, self.now = (datetime(2026, 10, day, tzinfo=tz) for day in (27, 28, 29))

    def tearDown(self):
        self.tmp.cleanup()

    def test_cursor_database_and_mirror(self):
        dbpath = self.home / ".config/Cursor/User/globalStorage/state.vscdb"
        dbpath.parent.mkdir(parents=True)
        with sqlite3.connect(dbpath) as db:
            db.execute("CREATE TABLE cursorDiskKV(key TEXT,value TEXT)")
            db.execute("CREATE TABLE composerHeaders(composerId TEXT,workspaceId TEXT)")
            db.execute("INSERT INTO composerHeaders VALUES('s1','workspace1')")
            db.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("bubbleId:s1:m1", json.dumps({"type": 1, "text": "task", "createdAt": "2026-10-27T08:00:00Z"})))
            db.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("bubbleId:old:m1", json.dumps({"type": 2, "text": "old", "createdAt": "2026-05-01T08:00:00Z"})))
        db.close()
        workspace = self.home / ".config/Cursor/User/workspaceStorage/workspace1/workspace.json"
        workspace.parent.mkdir(parents=True)
        workspace.write_text(json.dumps({"folder": "file:///code/project"}))
        before = dbpath.read_bytes()
        export = self.home / ".cursor/projects/p/agent-transcripts/s1.jsonl"
        export.parent.mkdir(parents=True)
        export.write_text(json.dumps({"role": "user", "message": {"content": "task"}})+"\n")
        result = collect(self.home, {}, self.start, self.end, self.now)
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(result["sessions"][0]["agent"], "cursor-db")
        self.assertEqual(result["sessions"][0]["cwd"], "/code/project")
        self.assertEqual(result["coverage"]["cursor"]["mirrors_superseded"], 1)
        stats = result["coverage"]["cursor-db"]
        self.assertEqual(stats["total_messages"], 2)
        self.assertEqual(stats["target_messages"], 1)
        self.assertEqual(stats["status"], "active")
        self.assertEqual(dbpath.read_bytes(), before)

    def test_zero_does_not_mean_failure(self):
        folder = self.home / ".pi/agent/sessions"
        folder.mkdir(parents=True)
        (folder / "old.jsonl").write_text(json.dumps({"timestamp": "2026-05-01T00:00:00Z", "message": {"role": "user", "content": "old"}})+"\n")
        (self.home / ".gemini/tmp").mkdir(parents=True)
        result = collect(self.home, {}, self.start, self.end, self.now)
        self.assertEqual(result["coverage"]["pi"]["status"], "no_activity_in_window")
        self.assertEqual(result["coverage"]["gemini"]["status"], "no_session_files")
        self.assertEqual(result["coverage"]["codex"]["status"], "missing")
        self.assertIn("历史可解析，当日无活动", render(result))

    def test_inventory_resource_dirs_and_encrypted_database(self):
        for path in [".trae/skills", ".trae-cn/extensions", ".copilot/logs", ".config/crush/skills"]:
            (self.home / path).mkdir(parents=True)
        p = self.home / ".config/Trae CN/ModularData/ai-agent/database.db"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"encrypted-by-application")
        log = self.home / ".config/Trae CN/logs/20261027/Modular/ai-agent_stdout.log"
        log.parent.mkdir(parents=True)
        log.write_text("[DB] Database is already encrypted\n")
        results = inventory(self.home)
        states = {Path(i["path"]).name: i["status"] for i in results}
        self.assertEqual(states[".trae"], "resources_only")
        self.assertEqual(states[".trae-cn"], "redirected")
        self.assertEqual(states[".copilot"], "no_sessions")
        self.assertEqual(states["crush"], "no_sessions")
        self.assertEqual(states["Trae CN"], "encrypted_store")
        self.assertEqual(len(unresolved(results)), 1)


if __name__ == "__main__":
    unittest.main()
