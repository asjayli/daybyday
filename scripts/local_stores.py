"""Read Cursor's canonical timestamped bubbles; classify additional local stores."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
from itertools import islice
from urllib.parse import unquote, urlparse


def cursor_records(path, warnings, stamp):
    workspaces, folders = {}, {}
    user_dir = path.parent.parent
    for workspace in (user_dir / "workspaceStorage").glob("*/workspace.json"):
        dbpath = workspace.parent / "state.vscdb"
        try:
            folder = json.loads(workspace.read_text()).get("folder", "")
            if not folder.startswith("file:"):
                continue
            cwd = unquote(urlparse(folder).path)
            folders[workspace.parent.name] = cwd
            if not dbpath.is_file():
                continue
            with closing(sqlite3.connect(dbpath.as_uri()+"?mode=ro", uri=True)) as db:
                row = db.execute("SELECT value FROM ItemTable WHERE key='composer.composerData'").fetchone()
                if row:
                    for item in json.loads(row[0]).get("allComposers", []):
                        workspaces[item["composerId"]] = cwd
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            warnings.append(f"{dbpath}: workspace metadata {type(exc).__name__}")
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='composerHeaders'").fetchone():
            for sid, workspace_id in db.execute("SELECT composerId,workspaceId FROM composerHeaders"):
                if workspace_id in folders:
                    workspaces[sid] = folders[workspace_id]
        for key, raw in db.execute("SELECT key,value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"):
            try:
                obj = json.loads(raw)
                sid = key.split(":")[1]
                role = {1: "user", 2: "assistant"}.get(obj.get("type"))
                text = obj.get("text", "")
                if role and isinstance(text, str) and text.strip():
                    yield sid, workspaces.get(sid), role, stamp(obj.get("createdAt")), text, f"{path}#cursorDiskKV={key}"
            except (ValueError, TypeError, AttributeError, IndexError) as exc:
                warnings.append(f"{path}#{key}: {type(exc).__name__}")


def cursor_session_ids(path):
    """Only supersede transcript mirrors if canonical dated text actually exists."""
    if not path.is_file():
        return set()
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as db:
        return {key.split(":")[1] for key, in db.execute(
            "SELECT key FROM cursorDiskKV WHERE key LIKE 'bubbleId:%' "
            "AND json_valid(value) AND json_extract(value,'$.createdAt') IS NOT NULL "
            "AND length(json_extract(value,'$.text'))>0")}


def inventory(home):
    results = []
    native = home / ".config/Trae CN/ModularData/ai-agent/database.db"
    for relative in (".trae", ".trae-cn", ".copilot", ".config/crush", ".config/Trae CN"):
        path = home / relative
        if not path.exists():
            continue
        item = {"path": str(path)}
        if relative == ".trae":
            children = [p.name for p in path.iterdir()]
            item.update(status="resources_only" if set(children) <= {"skills"} else "needs_inspection",
                        reason="目录项：" + ", ".join(children), session_store=str(native))
        elif relative == ".trae-cn":
            item.update(status="redirected", reason="编辑器扩展/技能目录；会话库在应用用户数据目录", session_store=str(native))
        elif relative == ".config/Trae CN":
            if native.exists():
                with native.open("rb") as handle:
                    sqlite_header = handle.read(16) == b"SQLite format 3\x00"
                encrypted_evidence = None
                if not sqlite_header:
                    for log in sorted((home / ".config/Trae CN/logs").glob("*/Modular/ai-agent*_stdout.log"), reverse=True):
                        with log.open(errors="replace") as handle:
                            for line_no, line in enumerate(islice(handle, 300), 1):
                                if "Database is already encrypted" in line:
                                    encrypted_evidence = f"{log}:{line_no}"
                                    break
                        if encrypted_evidence:
                            break
                item.update(status="needs_adapter" if sqlite_header else ("encrypted_store" if encrypted_evidence else "opaque_store"),
                            session_store=str(native), encryption_evidence=encrypted_evidence,
                            reason="会话库存在；" + ("普通 SQLite，需适配" if sqlite_header else ("应用日志确认数据库已加密，不能用 sqlite3 读取" if encrypted_evidence else "非明文 SQLite，具体格式尚未确认")),
                            next_action="使用客户端官方会话导出（需含消息时间），接入导出适配；不伪造日期或把空缓存当完整历史")
            else:
                item.update(status="no_sessions", reason="标准会话库不存在")
        else:
            probes = ([path / "session-state", path / "sessions"] if relative == ".copilot"
                      else [home / ".local/share/crush/crush.db", home / ".local/share/crush/sessions"])
            exists = [str(p) for p in probes if p.exists()]
            item.update(status="needs_adapter" if exists else "no_sessions",
                        reason="发现会话存储" if exists else "标准会话存储不存在；配置/技能/启动日志不等于会话",
                        checked_paths=[str(p) for p in probes])
        results.append(item)
    # Preserve discovery for agents outside the explicitly investigated set.
    names = ("windsurf", "cline", "roo", "aider", "qwen", "goose", "augment", "deepseek")
    known = {item["path"] for item in results}
    for base in [home, home / ".config", home / ".local/share"]:
        if base.is_dir():
            for p in base.iterdir():
                if p.is_dir() and str(p) not in known and any(n in p.name.lower() for n in names):
                    results.append({"path": str(p), "status": "needs_inspection", "reason": "新增候选，需检查会话格式"})
    return results
